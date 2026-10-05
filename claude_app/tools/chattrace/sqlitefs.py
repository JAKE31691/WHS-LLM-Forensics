# -*- coding: utf-8 -*-
"""SQLite 원시 파서 — 페이지 / 프리블록 / 비할당 영역 / WAL / 롤백 저널.

왜 sqlite3 모듈을 안 쓰고 직접 파싱하나:
  DELETE 는 셀 포인터만 지우고 셀 본문은 그 자리에 남긴다(프리블록).
  sqlite3 로 SELECT 하면 살아 있는 행만 보이므로 삭제된 쿠키·세션은
  절대 나오지 않는다. R5 에서 Cookies 가 삭제 전후 28,672 B 로 동일했던
  이유가 이것이다 — 파일은 안 줄고 내부가 프리블록으로 바뀌었다.
"""
import os
import struct

DB_HDR = 100
WAL_MAGIC = (0x377F0682, 0x377F0683)
JOURNAL_MAGIC = b'\xd9\xd5\x05\xf9\x20\xa1\x63\xd7'

PTYPE = {0x02: 'interior-index', 0x05: 'interior-table',
         0x0A: 'leaf-index', 0x0D: 'leaf-table'}


def _varint(buf, p):
    """SQLite 가변정수 — LevelDB 와 다르다. 빅엔디안, 최대 9바이트."""
    n = 0
    for i in range(9):
        if p >= len(buf):
            raise ValueError('varint truncated')
        b = buf[p]
        p += 1
        if i == 8:
            n = (n << 8) | b
        else:
            n = (n << 7) | (b & 0x7F)
            if not (b & 0x80):
                break
    if n >= 1 << 63:
        n -= 1 << 64
    return n, p


def page_size(data):
    ps = struct.unpack_from('>H', data, 16)[0]
    return 65536 if ps == 1 else ps


# ────────────────────────────────────────────────────────────────
# 레코드(셀) 디코딩
# ────────────────────────────────────────────────────────────────
def decode_record(payload):
    """SQLite 레코드 포맷 -> 파이썬 값 리스트."""
    hdr_len, p = _varint(payload, 0)
    types = []
    q = p
    while q < hdr_len:
        t, q = _varint(payload, q)
        types.append(t)
    vals = []
    b = hdr_len
    for t in types:
        if t == 0:
            vals.append(None)
        elif t in (1, 2, 3, 4, 5, 6):
            sz = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}[t]
            raw = payload[b:b + sz]
            v = int.from_bytes(raw, 'big', signed=True) if len(raw) == sz else None
            vals.append(v)
            b += sz
        elif t == 7:
            vals.append(struct.unpack_from('>d', payload, b)[0] if b + 8 <= len(payload) else None)
            b += 8
        elif t == 8:
            vals.append(0)
        elif t == 9:
            vals.append(1)
        elif t >= 12 and t % 2 == 0:
            sz = (t - 12) // 2
            vals.append(payload[b:b + sz])
            b += sz
        elif t >= 13:
            sz = (t - 13) // 2
            vals.append(payload[b:b + sz].decode('utf-8', 'replace'))
            b += sz
        else:
            vals.append(None)
    return vals


def iter_live_cells(page, page_no, ps):
    """살아 있는 셀 — 셀 포인터 배열이 가리키는 것들."""
    base = DB_HDR if page_no == 1 else 0
    ptype = page[base]
    if ptype not in PTYPE:
        return
    nheader = 12 if ptype in (0x02, 0x05) else 8
    ncell = struct.unpack_from('>H', page, base + 3)[0]
    ptr = base + nheader
    for i in range(ncell):
        if ptr + 2 > len(page):
            return
        off = struct.unpack_from('>H', page, ptr)[0]
        ptr += 2
        if off == 0 or off >= len(page):
            continue
        try:
            if ptype == 0x0D:                       # leaf-table
                plen, p = _varint(page, off)
                rowid, p = _varint(page, p)
                yield {'page': page_no, 'offset': off, 'rowid': rowid,
                       'payload': page[p:p + plen], 'state': 'live'}
            elif ptype == 0x0A:                     # leaf-index
                plen, p = _varint(page, off)
                yield {'page': page_no, 'offset': off, 'rowid': None,
                       'payload': page[p:p + plen], 'state': 'live'}
        except ValueError:
            continue


def iter_freeblocks(page, page_no):
    """프리블록 체인 — DELETE 가 남긴 자리. 여기 옛 레코드 본문이 있다.

    프리블록 헤더 4바이트 = 2바이트 다음 프리블록 오프셋 + 2바이트 크기
    """
    base = DB_HDR if page_no == 1 else 0
    if page[base] not in PTYPE:
        return
    off = struct.unpack_from('>H', page, base + 1)[0]
    seen = set()
    while off and off not in seen and off + 4 <= len(page):
        seen.add(off)
        nxt, size = struct.unpack_from('>HH', page, off)
        yield {'page': page_no, 'offset': off, 'size': size,
               'data': bytes(page[off + 4:off + size]), 'state': 'freeblock'}
        off = nxt


def unallocated_gap(page, page_no):
    """셀 포인터 배열 끝 ~ 셀 콘텐츠 시작 사이의 공백. 삭제 잔재가 흔히 남는다."""
    base = DB_HDR if page_no == 1 else 0
    ptype = page[base]
    if ptype not in PTYPE:
        return None
    nheader = 12 if ptype in (0x02, 0x05) else 8
    ncell = struct.unpack_from('>H', page, base + 3)[0]
    cstart = struct.unpack_from('>H', page, base + 5)[0]
    if cstart == 0:
        cstart = 65536
    start = base + nheader + ncell * 2
    if cstart <= start or cstart > len(page):
        return None
    return {'page': page_no, 'offset': start, 'size': cstart - start,
            'data': bytes(page[start:cstart]), 'state': 'unallocated'}


def scan_db(path):
    """DB 파일 전체를 페이지 단위로 훑는다. -> (살아있는 셀, 빈영역 조각, 통계)"""
    with open(path, 'rb') as f:
        data = f.read()
    if data[:15] != b'SQLite format 3':
        raise ValueError('SQLite 헤더 아님')
    ps = page_size(data)
    npages = len(data) // ps
    live, dead = [], []
    stats = []
    for i in range(npages):
        pno = i + 1
        page = data[i * ps:(i + 1) * ps]
        base = DB_HDR if pno == 1 else 0
        if base >= len(page) or page[base] not in PTYPE:
            continue
        lc = list(iter_live_cells(page, pno, ps))
        fb = list(iter_freeblocks(page, pno))
        ua = unallocated_gap(page, pno)
        live += lc
        dead += fb
        if ua and ua['size'] > 8:
            dead.append(ua)
        stats.append({'page': pno, 'type': PTYPE[page[base]], 'cells': len(lc),
                      'freeblocks': len(fb), 'free_bytes': sum(x['size'] for x in fb),
                      'unalloc_bytes': ua['size'] if ua else 0,
                      'frag_bytes': page[base + 7]})
    return live, dead, stats


# ────────────────────────────────────────────────────────────────
# WAL
# ────────────────────────────────────────────────────────────────
def scan_wal(path):
    """WAL 프레임을 순서대로 낸다.

    같은 페이지 번호가 여러 프레임에 나타나면 그 사이의 모든 중간 상태가
    보존돼 있다는 뜻이다. 마지막 프레임만이 '현재'이고 앞의 것들이 과거다.
    """
    with open(path, 'rb') as f:
        data = f.read()
    if len(data) < 32:
        return [], {}
    magic, fmt, ps, ckpt, salt1, salt2 = struct.unpack_from('>IIIIII', data, 0)
    if magic not in WAL_MAGIC:
        raise ValueError('WAL 매직 아님')
    meta = {'format': fmt, 'page_size': ps, 'checkpoint_seq': ckpt,
            'salt1': salt1, 'salt2': salt2}
    frames = []
    p = 32
    idx = 0
    while p + 24 + ps <= len(data):
        pno, dbsize, s1, s2 = struct.unpack_from('>IIII', data, p)
        frames.append({'frame': idx, 'page': pno, 'db_size_after': dbsize,
                       'salt_match': (s1 == salt1 and s2 == salt2),
                       'offset': p + 24, 'data': data[p + 24:p + 24 + ps]})
        p += 24 + ps
        idx += 1
    return frames, meta


def scan_journal(path):
    """롤백 저널 — 변경 '이전' 페이지 원본이 들어 있다."""
    with open(path, 'rb') as f:
        data = f.read()
    if len(data) < 28 or data[:8] != JOURNAL_MAGIC:
        return [], {}
    nrec, _nonce, initsize, sectsz, ps = struct.unpack_from('>IIIII', data, 8)
    out = []
    p = sectsz if sectsz and sectsz < len(data) else 512
    i = 0
    while p + 4 + ps + 4 <= len(data):
        pno = struct.unpack_from('>I', data, p)[0]
        out.append({'index': i, 'page': pno, 'offset': p + 4,
                    'data': data[p + 4:p + 4 + ps]})
        p += 4 + ps + 4
        i += 1
    return out, {'records_declared': nrec, 'initial_pages': initsize, 'page_size': ps}


def sidecars(dbpath):
    """DB 옆의 -wal / -journal / -shm 을 찾아준다."""
    found = {}
    for suf, key in (('-wal', 'wal'), ('-journal', 'journal'), ('-shm', 'shm')):
        cand = dbpath + suf
        if os.path.exists(cand):
            found[key] = cand
    base = os.path.splitext(dbpath)[0]
    for suf, key in (('-journal', 'journal'),):
        cand = base + suf
        if key not in found and os.path.exists(cand):
            found[key] = cand
    return found
