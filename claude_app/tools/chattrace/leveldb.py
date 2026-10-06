# -*- coding: utf-8 -*-
"""LevelDB 원시 파서 (.log / .ldb) + Chromium IndexedDB 키 해석.

★ 설계 원칙 — plyvel 을 쓰지 않는다 ★
  plyvel 로 DB 를 열면 LevelDB 가 복구(recovery)를 수행한다. .log 를 .ldb 로
  말아 넣고 CURRENT/MANIFEST 를 갱신하며, 덮어쓰기로 밀려난 옛 값과 삭제
  툼스톤을 조용히 버린다. 우리가 복원하려는 대상이 바로 그것들이므로
  라이브러리로 여는 순간 증거가 사라진다.
  따라서 이 모듈은 파일을 바이트로만 읽는다. plyvel 은 --use-plyvel 로
  '사본에 한해' 라이브 뷰 대조용으로만 선택 사용한다.
"""
import os
import struct

from .codec import Truncated, crc32c, snappy_decompress, unmask_crc, uvarint

BLOCK_SIZE = 32768
HDR = 7

R_ZERO, R_FULL, R_FIRST, R_MIDDLE, R_LAST = 0, 1, 2, 3, 4

TYPE_DEL, TYPE_PUT = 0, 1


# ────────────────────────────────────────────────────────────────
# .log  (write-ahead log)
# ────────────────────────────────────────────────────────────────
def iter_physical_records(data, verify_crc=False):
    """32KB 블록 안의 물리 레코드를 순회한다. -> (오프셋, 타입, 페이로드, crc_ok)"""
    off = 0
    n = len(data)
    while off + HDR <= n:
        room = BLOCK_SIZE - (off % BLOCK_SIZE)
        if room < HDR:                      # 블록 꼬리 패딩
            off += room
            continue
        crc, ln, typ = struct.unpack_from('<IHB', data, off)
        if typ == R_ZERO and ln == 0:
            off += room
            continue
        if typ > R_LAST or off + HDR + ln > n:
            off += 1                        # 손상 구간 — 1바이트씩 재동기화
            continue
        payload = data[off + HDR:off + HDR + ln]
        ok = None
        if verify_crc:
            ok = crc32c(bytes([typ]) + payload) == unmask_crc(crc)
        yield off, typ, payload, ok
        off += HDR + ln


def iter_batches(data, verify_crc=False):
    """물리 레코드를 논리 WriteBatch 로 재조립한다. -> (시작오프셋, 배치바이트, crc_ok)"""
    buf = bytearray()
    start = None
    allok = True
    for off, typ, payload, ok in iter_physical_records(data, verify_crc):
        if ok is False:
            allok = False
        if typ == R_FULL:
            yield off, bytes(payload), ok
            buf, start, allok = bytearray(), None, True
        elif typ == R_FIRST:
            buf = bytearray(payload)
            start = off
            allok = ok is not False
        elif typ == R_MIDDLE:
            if start is not None:
                buf += payload
        elif typ == R_LAST:
            if start is not None:
                buf += payload
                yield start, bytes(buf), allok
            buf, start, allok = bytearray(), None, True


def parse_batch(batch, origin_offset=0, origin_file=''):
    """WriteBatch 를 풀어 개별 키/값 조작을 낸다.

    배치 헤더 = 8바이트 시퀀스번호 + 4바이트 항목수
    항목 = 1바이트 타입 (0=삭제, 1=입력) + varint 키길이 + 키 [+ varint 값길이 + 값]
    """
    if len(batch) < 12:
        return
    seq, count = struct.unpack_from('<QI', batch, 0)
    p = 12
    i = 0
    while p < len(batch) and i < count:
        t = batch[p]
        p += 1
        try:
            klen, p = uvarint(batch, p)
            key = batch[p:p + klen]
            p += klen
            if t == TYPE_PUT:
                vlen, p = uvarint(batch, p)
                val = batch[p:p + vlen]
                p += vlen
            elif t == TYPE_DEL:
                val = None
            else:
                return                      # 알 수 없는 타입 = 배치 손상
        except Truncated:
            return
        yield {
            'seq': seq + i,
            'op': 'del' if t == TYPE_DEL else 'put',
            'key': key,
            'value': val,
            'offset': origin_offset,
            'file': origin_file,
            'source': 'log',
        }
        i += 1


def read_log(path, verify_crc=False):
    """.log 파일 하나에서 모든 조작 이력을 시간순으로 뽑는다.

    여기서 나오는 리스트에는 '덮어쓰기로 밀려난 옛 값' 과 '삭제 툼스톤' 이
    전부 들어 있다. 그게 시나리오 2 의 재료다.
    """
    with open(path, 'rb') as f:
        data = f.read()
    name = os.path.basename(path)
    out = []
    for off, batch, ok in iter_batches(data, verify_crc):
        for e in parse_batch(batch, off, name):
            e['crc_ok'] = ok
            out.append(e)
    return out


# ────────────────────────────────────────────────────────────────
# .ldb / .sst  (정렬 테이블)
# ────────────────────────────────────────────────────────────────
SST_MAGIC = 0xDB4775248B80FB57
FOOTER_LEN = 48


def _block_handle(buf, p):
    off, p = uvarint(buf, p)
    size, p = uvarint(buf, p)
    return off, size, p


def _read_block(data, off, size):
    raw = data[off:off + size]
    comp = data[off + size]
    if comp == 0:
        return raw
    if comp == 1:
        return snappy_decompress(raw)
    if comp == 2:
        import zlib
        return zlib.decompress(raw)
    if comp == 4:
        try:
            import zstandard
            return zstandard.ZstdDecompressor().decompress(raw)
        except Exception:
            raise Truncated()
    raise Truncated()


def _iter_block_entries(blk):
    """접두사 압축된 블록 엔트리를 편다."""
    if len(blk) < 4:
        return
    nrestart = struct.unpack_from('<I', blk, len(blk) - 4)[0]
    limit = len(blk) - 4 - nrestart * 4
    if limit <= 0:
        return
    p = 0
    prev = b''
    while p < limit:
        try:
            shared, p = uvarint(blk, p)
            nonshared, p = uvarint(blk, p)
            vlen, p = uvarint(blk, p)
        except Truncated:
            return
        if p + nonshared + vlen > len(blk) or shared > len(prev):
            return
        key = prev[:shared] + blk[p:p + nonshared]
        p += nonshared
        val = blk[p:p + vlen]
        p += vlen
        prev = key
        yield key, val


def read_sst(path):
    """.ldb 전체를 읽어 (내부키, 값) 을 낸다. 내부키 = 사용자키 + 8바이트(seq|type)."""
    with open(path, 'rb') as f:
        data = f.read()
    if len(data) < FOOTER_LEN:
        return []
    magic = struct.unpack_from('<Q', data, len(data) - 8)[0]
    if magic != SST_MAGIC:
        raise ValueError('SST 매직 불일치 — 파일이 아니거나 손상됨')
    foot = len(data) - FOOTER_LEN
    _, _, p = _block_handle(data, foot)             # metaindex
    ioff, isize, _ = _block_handle(data, p)         # index
    name = os.path.basename(path)
    out = []
    index = _read_block(data, ioff, isize)
    for _k, handle in _iter_block_entries(index):
        boff, bsize, _ = _block_handle(handle, 0)
        try:
            blk = _read_block(data, boff, bsize)
        except Exception:
            continue
        for ikey, val in _iter_block_entries(blk):
            if len(ikey) < 8:
                continue
            trailer = struct.unpack_from('<Q', ikey, len(ikey) - 8)[0]
            out.append({
                'seq': trailer >> 8,
                'op': 'put' if (trailer & 0xFF) == TYPE_PUT else 'del',
                'key': ikey[:-8],
                'value': val if (trailer & 0xFF) == TYPE_PUT else None,
                'offset': boff,
                'file': name,
                'source': 'sst',
                'crc_ok': None,
            })
    return out


# ────────────────────────────────────────────────────────────────
# 디렉터리 단위 수집
# ────────────────────────────────────────────────────────────────
def read_store(dirpath, verify_crc=False, log=None):
    """LevelDB 디렉터리 하나를 통째로 읽어 모든 버전의 레코드를 낸다."""
    recs = []
    errs = []
    for fn in sorted(os.listdir(dirpath)):
        full = os.path.join(dirpath, fn)
        if not os.path.isfile(full):
            continue
        try:
            if fn.endswith('.log'):
                recs += read_log(full, verify_crc)
            elif fn.endswith(('.ldb', '.sst')):
                recs += read_sst(full)
        except Exception as e:
            errs.append((fn, '%s: %s' % (type(e).__name__, e)))
            if log:
                log.warning('%s 파싱 실패 — %s', fn, e)
    recs.sort(key=lambda r: r['seq'])
    return recs, errs


def live_view(recs):
    """시퀀스 번호가 가장 큰 조작만 남긴 '현재 상태'. 시나리오 1 의 뷰."""
    latest = {}
    for r in recs:
        k = bytes(r['key'])
        if k not in latest or r['seq'] >= latest[k]['seq']:
            latest[k] = r
    return {k: v for k, v in latest.items() if v['op'] == 'put'}


def shadowed(recs):
    """현재 상태에서 보이지 않는 과거 버전 — 삭제·덮어쓰기 산물.

    반환: [(레코드, 'deleted'|'overwritten')]
    """
    by_key = {}
    for r in recs:
        by_key.setdefault(bytes(r['key']), []).append(r)
    out = []
    for k, lst in by_key.items():
        lst.sort(key=lambda r: r['seq'])
        newest = lst[-1]
        for r in lst[:-1]:
            if r['op'] == 'put':
                out.append((r, 'deleted' if newest['op'] == 'del' else 'overwritten'))
        if newest['op'] == 'del':
            out.append((newest, 'tombstone'))
    return out


# ────────────────────────────────────────────────────────────────
# Chromium IndexedDB 키
# ────────────────────────────────────────────────────────────────
def idb_prefix(key):
    """IndexedDB 키 앞머리를 해석한다. -> (db_id, store_id, index_id, 남은바이트)

    첫 바이트: 상위3비트=db_id 바이트수-1, 다음3비트=store_id 바이트수-1,
               하위2비트=index_id 바이트수-1
    """
    if not key:
        return None
    b = key[0]
    nd = ((b >> 5) & 0x07) + 1
    ns = ((b >> 2) & 0x07) + 1
    ni = (b & 0x03) + 1
    p = 1
    if len(key) < 1 + nd + ns + ni:
        return None
    db = int.from_bytes(key[p:p + nd], 'little'); p += nd
    st = int.from_bytes(key[p:p + ns], 'little'); p += ns
    ix = int.from_bytes(key[p:p + ni], 'little'); p += ni
    return db, st, ix, key[p:]


IDB_KIND = {0: '메타데이터', 1: '레코드(ObjectStoreData)', 2: '존재플래그', 3: '키생성기'}


def idb_kind(key):
    pr = idb_prefix(key)
    if not pr:
        return '알수없음'
    _db, _st, ix, _rest = pr
    if ix >= 30:
        return '인덱스(IndexData)'
    return IDB_KIND.get(ix, '기타 idx=%d' % ix)


IDBKEY = {0: 'null', 1: 'string', 2: 'date', 3: 'number',
          4: 'array', 5: 'min', 6: 'binary'}


def idb_decode_key(key):
    """레코드 키의 IDBKey 부분을 사람이 읽을 수 있게 편다.

    대화 UUID 는 값 안에만 있는 게 아니라 키에도 그대로 박혀 있다.
    문자열 키는 varint 길이(UTF-16 코드유닛 수) + UTF-16 **빅엔디안** 이다.
    """
    pr = idb_prefix(key)
    if not pr:
        return ''
    _db, _st, ix, rest = pr
    if ix >= 30 or not rest:
        return ''
    t = rest[0]
    try:
        if t == 1:
            n, p = uvarint(rest, 1)
            return rest[p:p + n * 2].decode('utf-16-be', 'replace')
        if t == 3:
            return repr(struct.unpack_from('<d', rest, 1)[0])
        if t == 2:
            return 'date:%r' % struct.unpack_from('<d', rest, 1)[0]
    except Exception:
        return ''
    return IDBKEY.get(t, '')


def idb_value_blob(value):
    """ObjectStoreData 값에서 SSV 블롭 부분만 떼어낸다 (앞의 varint 버전 제거)."""
    if not value:
        return b''
    try:
        _ver, p = uvarint(value, 0)
    except Truncated:
        return value
    if p < len(value) and value[p] == 0xFF:
        return value[p:]
    return value
