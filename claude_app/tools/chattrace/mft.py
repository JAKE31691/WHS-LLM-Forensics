# -*- coding: utf-8 -*-
"""NTFS $MFT 파서.

MFTECmd 를 대체하려는 게 아니다. 범용 파싱은 그쪽이 낫다.
여기서 필요한 건 '이 수집물 안의 Claude 아티팩트가 언제 바뀌었나' 하나이고,
그걸 위해 외부 도구·런타임 없이 도는 최소 파서를 둔다.

설계 메모
  * 605 MB / 62만 레코드를 통째로 메모리에 올리지 않는다. 2패스 스트리밍.
      1패스 - 디렉터리만 모아 경로 복원용 지도를 만든다
      2패스 - 전체를 흘리면서 경로를 조립하고 필요한 것만 남긴다
  * 시각은 100나노초 원본을 그대로 들고 다닌다. 마이크로초로 줄이면
    삭제 순서를 다툴 때 근거가 약해진다.
  * $STANDARD_INFORMATION 과 $FILE_NAME 시각을 둘 다 뽑는다.
    두 값이 어긋나면 시각 조작(timestomping) 정황이다.
"""
import datetime
import os
import struct

REC_SIZE = 1024
SECTOR = 512
SIG = b'FILE'
BAAD = b'BAAD'

ATTR_STANDARD_INFORMATION = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30

FLAG_IN_USE = 0x01
FLAG_IS_DIR = 0x02

ROOT_ENTRY = 5

# $FILE_NAME 이름공간. Win32 이름을 쓰고 DOS 8.3 은 버린다.
NS_POSIX, NS_WIN32, NS_DOS, NS_WIN32_DOS = 0, 1, 2, 3
_NS_RANK = {NS_WIN32: 3, NS_WIN32_DOS: 3, NS_POSIX: 2, NS_DOS: 1}

_EPOCH = datetime.datetime(1601, 1, 1)


def filetime_str(v):
    """FILETIME(100ns) -> 'YYYY-MM-DD HH:MM:SS.fffffff'. MFTECmd 와 같은 형식."""
    if not v:
        return ''
    try:
        dt = _EPOCH + datetime.timedelta(microseconds=v // 10)
    except (OverflowError, OSError):
        return ''
    return '%s.%07d' % (dt.strftime('%Y-%m-%d %H:%M:%S'), v % 10000000)


def filetime_dt(v):
    """정렬용 datetime. 100ns 잔여는 버린다(비교에는 영향 없음)."""
    if not v:
        return None
    try:
        return _EPOCH + datetime.timedelta(microseconds=v // 10)
    except (OverflowError, OSError):
        return None


class MftError(Exception):
    pass


def _apply_fixup(raw):
    """업데이트 시퀀스 복원.

    NTFS 는 섹터마다 마지막 2바이트를 서명으로 바꿔 저장하고 원본은
    배열에 따로 둔다. 되돌리지 않으면 512바이트마다 값이 깨진다.
    """
    off_usa, cnt_usa = struct.unpack_from('<HH', raw, 4)
    if cnt_usa == 0 or off_usa + cnt_usa * 2 > len(raw):
        return None
    usn = raw[off_usa:off_usa + 2]
    b = bytearray(raw)
    for i in range(1, cnt_usa):
        end = i * SECTOR - 2
        if end + 2 > len(b):
            break
        if bytes(b[end:end + 2]) != usn:
            return None                       # 서명 불일치 = 손상
        src = off_usa + i * 2
        b[end:end + 2] = raw[src:src + 2]
    return bytes(b)


def parse_record(raw, entry):
    """레코드 1개 해석. -> dict 또는 None"""
    if len(raw) < 48 or raw[:4] != SIG:
        return None
    fixed = _apply_fixup(raw)
    if fixed is None:
        return None

    seq = struct.unpack_from('<H', fixed, 16)[0]
    attr_off = struct.unpack_from('<H', fixed, 20)[0]
    flags = struct.unpack_from('<H', fixed, 22)[0]
    base_ref = struct.unpack_from('<Q', fixed, 32)[0]

    r = {
        'entry': entry, 'seq': seq,
        'in_use': bool(flags & FLAG_IN_USE),
        'is_dir': bool(flags & FLAG_IS_DIR),
        'is_extension': (base_ref & 0xFFFFFFFFFFFF) != 0,
        'has_attr_list': False,
        'dos_only': False,          # Win32 이름이 확장 레코드에 있어 8.3 만 얻은 경우
        'links': [],                # 하드링크: (parent, parent_seq, name, ns)
        'name': '', 'parent': None, 'parent_seq': None, 'size': 0,
        'si_created': 0, 'si_modified': 0, 'si_mft_modified': 0, 'si_accessed': 0,
        'fn_created': 0, 'fn_modified': 0, 'fn_mft_modified': 0, 'fn_accessed': 0,
    }

    p = attr_off
    best = -1
    n = len(fixed)
    while p + 16 <= n:
        atype = struct.unpack_from('<I', fixed, p)[0]
        if atype == 0xFFFFFFFF:
            break
        alen = struct.unpack_from('<I', fixed, p + 4)[0]
        if alen < 16 or p + alen > n:
            break
        if atype == ATTR_ATTRIBUTE_LIST:
            r['has_attr_list'] = True
        if fixed[p + 8] == 0:                 # 상주 속성만 본다
            clen, coff = struct.unpack_from('<IH', fixed, p + 16)
            s = p + coff
            body = fixed[s:s + clen]
            if atype == ATTR_STANDARD_INFORMATION and len(body) >= 32:
                (r['si_created'], r['si_modified'],
                 r['si_mft_modified'], r['si_accessed']) = struct.unpack_from('<QQQQ', body, 0)
            elif atype == ATTR_FILE_NAME and len(body) >= 66:
                ref = struct.unpack_from('<Q', body, 0)[0]
                nlen = body[64]
                ns = body[65]
                need = 66 + nlen * 2
                if need > len(body):
                    p += alen
                    continue
                nm = body[66:need].decode('utf-16-le', 'replace')
                par = ref & 0xFFFFFFFFFFFF
                pseq = ref >> 48
                fc, fm, fr, fa = struct.unpack_from('<QQQQ', body, 8)
                # 하드링크는 $FILE_NAME 이 여러 개고 부모도 시각도 서로 다르다.
                # 하나로 뭉치면 값이 어긋나므로 링크 단위로 들고 간다.
                # DOS 8.3 은 같은 경로의 별칭이라 링크 목록에서 제외한다.
                if ns != NS_DOS:
                    r['links'].append({
                        'parent': par, 'parent_seq': pseq, 'name': nm, 'ns': ns,
                        'fn_created': fc, 'fn_modified': fm,
                        'fn_mft_modified': fr, 'fn_accessed': fa,
                        'size': struct.unpack_from('<Q', body, 48)[0]
                                if len(body) >= 56 else 0,
                    })
                rank = _NS_RANK.get(ns, 0)
                if rank > best:
                    best = rank
                    r['name'] = nm
                    r['parent'] = par
                    r['parent_seq'] = pseq
                    (r['fn_created'], r['fn_modified'],
                     r['fn_mft_modified'], r['fn_accessed']) = struct.unpack_from('<QQQQ', body, 8)
                    if len(body) >= 56:
                        r['size'] = struct.unpack_from('<Q', body, 48)[0]
        p += alen

    # 기본 레코드에 DOS 이름만 있고 속성목록이 있으면, 진짜 이름은 확장
    # 레코드에 있다. 8.3 이름을 그대로 쓰면 경로가 틀려 보이므로 표시해 둔다.
    if best == _NS_RANK[NS_DOS] and r['has_attr_list']:
        r['dos_only'] = True
    return r


def _iter_records(path, chunk=4096):
    """레코드를 스트리밍으로 흘린다. -> (entry, raw)"""
    size = os.path.getsize(path)
    total = size // REC_SIZE
    with open(path, 'rb') as f:
        entry = 0
        while entry < total:
            want = min(chunk, total - entry)
            buf = f.read(want * REC_SIZE)
            if not buf:
                break
            for i in range(len(buf) // REC_SIZE):
                yield entry + i, buf[i * REC_SIZE:(i + 1) * REC_SIZE]
            entry += want


def build_dir_map(path, log=None):
    """1패스 — 디렉터리만 모아 경로 복원용 지도를 만든다.

    파일까지 전부 들고 있으면 62만 건에 수백 MB 가 든다. 경로를 거슬러
    오를 때 필요한 건 조상 디렉터리뿐이므로 그것만 남긴다.
    -> {entry: (parent_entry, parent_seq, name, own_seq)}
    """
    dmap = {}
    n = bad = 0
    for entry, raw in _iter_records(path):
        if raw[:4] != SIG:
            continue
        hdr_flags = struct.unpack_from('<H', raw, 22)[0]
        if not (hdr_flags & FLAG_IS_DIR):
            continue
        r = parse_record(raw, entry)
        if r is None:
            bad += 1
            continue
        if r['name']:
            dmap[entry] = (r['parent'], r['parent_seq'], r['name'], r['seq'])
            n += 1
    if log:
        log.info('   디렉터리 %s개 수집 (손상 %d)', format(n, ','), bad)
    return dmap


# 경로 신뢰도
PATH_OK = 'ok'                 # 루트까지 시퀀스가 전부 맞음
PATH_REUSED = 'reused'         # 조상 슬롯이 재사용됨 — 경로를 믿으면 안 된다
PATH_MISSING = 'missing'       # 조상이 $MFT 에 없음
PATH_CYCLE = 'cycle'           # 순환 참조 (손상)


def resolve_path(entry, parent, parent_seq, name, dmap, max_depth=96):
    """부모 참조를 거슬러 전체 경로를 조립한다. -> (경로, 신뢰도)

    ★ 부모의 시퀀스 번호를 반드시 대조한다.
      파일이 가리키는 부모 슬롯이 그 사이 다른 디렉터리에 재할당됐을 수
      있다. 그대로 따라가면 '그럴듯하지만 틀린 경로' 가 만들어진다.
      삭제 파일을 다루는 이 도구에서는 치명적이라, 불일치를 발견하면
      경로를 조립하되 신뢰도를 reused 로 낮춘다.
      (MFTECmd 는 같은 상황을 PathUnknown 으로 표기한다)
    """
    if not name:
        return '', PATH_MISSING
    parts = [name]
    seen = {entry}
    cur, expect = parent, parent_seq
    status = PATH_OK
    depth = 0
    while cur is not None and cur != ROOT_ENTRY and depth < max_depth:
        if cur in seen:
            parts.append('<순환>')
            status = PATH_CYCLE
            break
        seen.add(cur)
        d = dmap.get(cur)
        if d is None:
            parts.append('<미상-%d>' % cur)
            status = PATH_MISSING
            break
        if expect is not None and d[3] != expect and status == PATH_OK:
            status = PATH_REUSED          # 슬롯 재사용 — 이 위로는 믿을 수 없다
        parts.append(d[2])
        cur, expect = d[0], d[1]
        depth += 1
    return '\\'.join(reversed(parts)), status


def scan(path, dmap=None, path_filter=None, log=None):
    """2패스 — 전체를 흘리며 경로를 조립하고 조건에 맞는 것만 낸다.

    path_filter: 소문자 경로를 받아 bool 을 내는 함수. None 이면 전부.
    """
    if dmap is None:
        dmap = build_dir_map(path, log)
    out = []
    total = parsed = skipped = 0
    for entry, raw in _iter_records(path):
        total += 1
        if raw[:4] != SIG:
            skipped += 1
            continue
        r = parse_record(raw, entry)
        if r is None or not r['name']:
            skipped += 1
            continue
        parsed += 1
        # 하드링크가 있으면 경로가 여럿이다. 링크마다 따로 조립한다.
        links = r['links'] or [{
            'parent': r['parent'], 'parent_seq': r['parent_seq'],
            'name': r['name'], 'ns': NS_WIN32,
            'fn_created': r['fn_created'], 'fn_modified': r['fn_modified'],
            'fn_mft_modified': r['fn_mft_modified'],
            'fn_accessed': r['fn_accessed'], 'size': r['size']}]
        for lk in links:
            lk['path'], lk['path_status'] = resolve_path(
                entry, lk['parent'], lk['parent_seq'], lk['name'], dmap)
        keep = [lk for lk in links if lk['path'] and
                (path_filter is None or path_filter(lk['path'].lower()))]
        if not keep:
            continue
        # 대표값은 신뢰할 수 있는 경로를 우선한다.
        keep.sort(key=lambda lk: (lk['path_status'] != PATH_OK, len(lk['path'])))
        r['path'] = keep[0]['path']
        r['path_status'] = keep[0]['path_status']
        r['links'] = keep
        out.append(r)
    if log:
        log.info('   레코드 %s개 · 해석 %s · 제외 %s · 채택 %s',
                 format(total, ','), format(parsed, ','),
                 format(skipped, ','), format(len(out), ','))
    return out


# ── Claude 아티팩트 필터 ────────────────────────────────────────
CLAUDE_HINTS = ('claude_pzs8sxrjxfjjc', '\\claude\\', 'claude-local',
                'indexeddb', 'session storage', 'local storage')


def claude_filter(low):
    return any(h in low for h in CLAUDE_HINTS)
