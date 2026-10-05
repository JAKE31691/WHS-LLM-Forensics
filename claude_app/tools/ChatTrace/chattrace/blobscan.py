# -*- coding: utf-8 -*-
"""blob / 캐시 / 비할당 바이트 더미 전용 스캐너.

여기서 다루는 건 '구조를 신뢰할 수 없는 데이터' 다.
LevelDB 레코드 경계가 깨졌거나, HTTP 캐시 블록이거나, 프리블록 잔재거나,
디스크 슬랙에서 긁어온 조각.

핵심 두 가지 (R5 실측으로 확인):
  1) 본문 상당수가 snappy 로 눌려 있다. 압축을 안 풀고 regex 만 돌리면
     'LONG-0' 기준 38건 → 풀면 53건. 약 39% 를 놓친다.
  2) Session Storage 의 프롬프트는 UTF-16LE 로만 존재한다.
     ASCII 로 '"prompt":"' 를 찾으면 정확히 0건이다.
"""
import re
import zlib

from .codec import (Truncated, looks_utf16 as codec_looks_utf16,
                     snappy_decompress, uvarint)

# ── 압축 스트림 탐지 ────────────────────────────────────────────
IDB_SNAPPY = b'\xff\x11\x02'          # IndexedDB 값 래퍼 + snappy
GZIP_MAGIC = b'\x1f\x8b\x08'
ZLIB_MAGICS = (b'\x78\x01', b'\x78\x9c', b'\x78\xda', b'\x78\x5e')

MIN_INFLATE = 64                      # 이보다 작게 풀리면 오탐으로 본다


def _try_brotli(raw):
    try:
        import brotli
    except ImportError:
        return None
    try:
        return brotli.decompress(raw)
    except Exception:
        return None


MAX_ATTEMPTS = 400        # 매직 종류별 해제 시도 상한


def decompress_regions(buf, max_regions=4000):
    """버퍼 안에서 풀 수 있는 압축 구간을 전부 찾아 푼다.

    -> [(방식, 원본오프셋, 해제된바이트)]
    오탐이 있어도 상관없다. 안 풀리면 버리고, 풀리면 스캔 대상이 늘 뿐이다.

    다만 zlib 매직(78 01 등)은 2바이트라 큰 바이너리에서 우연히 수만 번
    걸린다. 시도 횟수에 상한을 두지 않으면 Code Cache 한 폴더에서 몇 분씩
    잡아먹는다. 그래서 매직별로 MAX_ATTEMPTS 까지만 본다.
    """
    out = []
    n = len(buf)

    for i, m in enumerate(re.finditer(re.escape(IDB_SNAPPY), buf)):
        if i >= MAX_ATTEMPTS or len(out) >= max_regions:
            break
        try:
            out.append(('snappy', m.start(), snappy_decompress(buf[m.end():])))
        except (Truncated, IndexError, MemoryError):
            pass

    for i, m in enumerate(re.finditer(re.escape(GZIP_MAGIC), buf)):
        if i >= MAX_ATTEMPTS or len(out) >= max_regions:
            break
        try:
            d = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(buf[m.start():])
            if len(d) >= MIN_INFLATE:
                out.append(('gzip', m.start(), d))
        except zlib.error:
            pass

    for magic in ZLIB_MAGICS:
        for i, m in enumerate(re.finditer(re.escape(magic), buf)):
            if i >= MAX_ATTEMPTS or len(out) >= max_regions:
                break
            try:
                d = zlib.decompressobj().decompress(buf[m.start():])
                if len(d) >= MIN_INFLATE:
                    out.append(('zlib', m.start(), d))
            except zlib.error:
                pass

    if n and (buf[:1] == b'\x1b' or b'br' in buf[:16]):
        d = _try_brotli(buf)
        if d:
            out.append(('brotli', 0, d))
    return out


def views(buf, decompress=True):
    """스캔해야 할 모든 '평문 뷰' 를 낸다.

    원본 그대로 + 압축 해제본 + UTF-16 널 제거본.
    -> [(뷰이름, 바이트, 원본오프셋기준함수)]
    """
    vs = [('raw', buf, lambda o: o)]
    if decompress:
        for kind, off, d in decompress_regions(buf):
            vs.append(('%s@0x%X' % (kind, off), d, lambda o, b=off: b))
    if _looks_utf16(buf):
        # (1) 한 바이트씩 걸러낸 뷰 — ASCII 계열에만 통한다.
        #     'A\x00' -> 'A' 로 잘 되지만, 한글은 '경'=AC BD 에서 AC 만
        #     남아 깨진다. 영어 프롬프트용.
        for shift in (0, 1):
            vs.append(('utf16le+%d' % shift, bytes(buf[shift::2]),
                       lambda o, s=shift: o * 2 + s))
        # (2) 제대로 디코드한 뷰 — 한글·CJK 는 이쪽이라야 잡힌다.
        try:
            txt8 = buf.decode('utf-16-le', 'ignore').encode('utf-8')
        except Exception:
            txt8 = b''
        if txt8:
            vs.append(('utf16-decoded', txt8, _mk_remap(txt8)))
    return vs


def _mk_remap(txt8):
    """utf-8 뷰의 바이트 오프셋을 원본 UTF-16 오프셋으로 되돌린다."""
    def remap(o):
        try:
            return len(txt8[:o].decode('utf-8', 'ignore').encode('utf-16-le'))
        except Exception:
            return o
    return remap


def _looks_utf16(buf, sample=4096):
    """codec.looks_utf16 을 쓴다 — 한글 UTF-16 을 놓치던 판정을 고쳤다."""
    return codec_looks_utf16(buf, sample)


# ── 프롬프트 탐색 전략 ──────────────────────────────────────────
# 신뢰도 순서대로 둔다. 앞쪽이 구조적 근거가 강하다.
PROMPT_RULES = [
    # 1) 탐색 파라미터에 실린 프롬프트 (Session Storage). 계정 삭제 후에도 잔존
    ('nav-params', rb'"prompt"\s*:\s*"((?:[^"\\]|\\.){2,4000})"', 95),
    # 2) JSON 파편의 human 메시지
    ('json-human', rb'"sender"\s*:\s*"human"[^{}]{0,400}?"text"\s*:\s*"'
                   rb'((?:[^"\\]|\\.){2,4000})"', 90),
    ('json-human-rev', rb'"text"\s*:\s*"((?:[^"\\]|\\.){2,4000})"'
                       rb'[^{}]{0,400}?"sender"\s*:\s*"human"', 90),
    # 3) 첨부 추출 텍스트
    ('extracted', rb'"extracted_content"\s*:\s*"((?:[^"\\]|\\.){2,4000})"', 60),
]

# V8 구조체 안의 프롬프트는 따옴표가 없다 — 태그+길이 뒤에 이름이 온다
V8_PROMPT_FIELDS = (b'prompt', b'text')


def scan_prompts(buf, source='', decompress=True):
    """모든 뷰에서 사용자 프롬프트를 긁는다. -> 소견 리스트"""
    return scan_prompts_views(views(buf, decompress), source)


def scan_prompts_views(vs, source=''):
    """뷰를 이미 만들어 뒀을 때 쓴다 (압축 해제 중복 방지)."""
    hits = []
    seen = set()
    for vname, view, remap in vs:
        for rule, pat, conf in PROMPT_RULES:
            for m in re.finditer(pat, view, re.S):
                txt = _clean(m.group(1))
                if not _plausible(txt):
                    continue
                k = txt[:200]
                if k in seen:
                    continue
                seen.add(k)
                hits.append({'source': source, 'view': vname, 'rule': rule,
                             'confidence': conf, 'offset': remap(m.start()),
                             'text': txt})
    return hits


def scan_v8_prompts(buf, source='', decompress=True):
    """V8 구조체에서 prompt/text 필드 바로 뒤의 문자열을 꺼낸다.

    구조가 살아 있지만 전체 트리 파싱이 실패한 조각에 쓴다.
    필드 이름은 따옴표 없이 [태그][길이][이름] 형태로 박혀 있다.
    """
    return scan_v8_prompts_views(views(buf, decompress), source)


def scan_v8_prompts_views(vs, source=''):
    hits = []
    for vname, view, remap in vs:
        for fld in V8_PROMPT_FIELDS:
            for m in re.finditer(re.escape(fld), view):
                i = m.start()
                if not (i >= 2 and view[i - 2] in (0x22, 0x53)
                        and view[i - 1] == len(fld)):
                    continue                       # 태그/길이가 안 맞으면 오탐
                p = m.end()
                if p >= len(view) or view[p] not in (0x22, 0x63, 0x53):
                    continue
                tag = view[p]
                try:
                    ln, q = uvarint(view, p + 1)
                except Truncated:
                    continue
                if not (2 <= ln <= 8000) or q + ln > len(view):
                    continue
                raw = view[q:q + ln]
                try:
                    if tag == 0x63:
                        txt = raw.decode('utf-16-le')
                    elif tag == 0x22:
                        txt = raw.decode('latin-1')
                    else:
                        txt = raw.decode('utf-8')
                except UnicodeDecodeError:
                    continue
                if not _plausible(txt):
                    continue
                hits.append({'source': source, 'view': vname,
                             'rule': 'v8-field-%s' % fld.decode(),
                             'confidence': 80, 'offset': remap(i),
                             'text': _clean(txt)})
    return hits


_ESC = {'\\n': '\n', '\\t': '\t', '\\r': '', '\\"': '"', '\\\\': '\\', '\\/': '/'}


def _clean(b):
    s = b.decode('utf-8', 'replace') if isinstance(b, (bytes, bytearray)) else b
    for k, v in _ESC.items():
        s = s.replace(k, v)
    s = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), s)
    return s.strip()


def _plausible(s):
    """사람이 친 문장처럼 보이는가 — base64/해시/UI 문자열 걸러내기."""
    if len(s) < 4 or len(s) > 20000:
        return False
    if s.count('�') > len(s) * 0.05:
        return False
    printable = sum(1 for c in s if c.isprintable() or c in '\n\t')
    if printable < len(s) * 0.9:
        return False
    if re.fullmatch(r'[A-Za-z0-9+/=_-]{24,}', s):        # 토큰/해시류
        return False
    if re.fullmatch(r'[0-9a-f-]{16,}', s):               # UUID/헥스
        return False
    return bool(re.search(r'[A-Za-z가-힣]', s))


# ── 캐시 파일 ────────────────────────────────────────────────
SIMPLE_CACHE_MAGIC = b'\x30\x5c\x72\xa7\x1b\x6d\xfb\xfc'


def cache_entry_url(buf):
    """Chromium Simple Cache 엔트리 헤더에서 키(URL)를 꺼낸다."""
    if buf[:8] != SIMPLE_CACHE_MAGIC or len(buf) < 24:
        return None
    try:
        klen = int.from_bytes(buf[16:20], 'little')
        if 0 < klen < 4096 and 24 + klen <= len(buf):
            return buf[24:24 + klen].decode('utf-8', 'replace')
    except Exception:
        pass
    return None
