# -*- coding: utf-8 -*-
"""저수준 디코딩 원시 함수 모음.

varint / zigzag / crc32c / snappy / V8 structured-clone.
외부 의존성 없음 — 증거 사본만 있으면 어디서든 돈다.
"""
import struct


# ────────────────────────────────────────────────────────────────
# varint
# ────────────────────────────────────────────────────────────────
class Truncated(Exception):
    """버퍼가 중간에 끊김 — 조각난 레코드에서 정상적으로 발생한다."""


def uvarint(buf, p):
    """부호 없는 LEB128. 길이·카운트에 쓰인다. -> (값, 다음위치)"""
    n = 0
    shift = 0
    while True:
        if p >= len(buf):
            raise Truncated()
        b = buf[p]
        p += 1
        n |= (b & 0x7F) << shift
        if not (b & 0x80):
            return n, p
        shift += 7
        if shift > 70:
            raise Truncated()


def zvarint(buf, p):
    """zigzag LEB128. V8 의 정수값에 쓰인다. -> (값, 다음위치)

    주의: 길이는 uvarint, 값은 zvarint 다. 이 둘을 섞으면 정확히 2배 틀린다.
    """
    n, p = uvarint(buf, p)
    return (n >> 1) ^ -(n & 1), p


# ────────────────────────────────────────────────────────────────
# crc32c (Castagnoli) + LevelDB 마스킹
# ────────────────────────────────────────────────────────────────
_POLY = 0x82F63B78
_TBL = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ (_POLY if _c & 1 else 0)
    _TBL.append(_c)

_MASK_DELTA = 0xA282EAD8


def crc32c(data, crc=0):
    crc ^= 0xFFFFFFFF
    t = _TBL
    for b in data:
        crc = t[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def unmask_crc(m):
    """LevelDB 는 crc 를 회전시켜 저장한다. 원래 값으로 되돌린다."""
    m = (m - _MASK_DELTA) & 0xFFFFFFFF
    return ((m << 15) | (m >> 17)) & 0xFFFFFFFF


# ────────────────────────────────────────────────────────────────
# snappy (순수 파이썬 디컴프레서)
#   Chromium LevelDB 의 .ldb 블록은 기본적으로 snappy 압축이다.
#   python-snappy 는 Windows 빌드가 까다로워서 직접 구현한다.
# ────────────────────────────────────────────────────────────────
def snappy_decompress(src):
    n, p = uvarint(src, 0)
    out = bytearray()
    ln = len(src)
    while p < ln:
        tag = src[p]
        kind = tag & 0x03
        if kind == 0:                       # literal
            sz = tag >> 2
            p += 1
            if sz >= 60:
                extra = sz - 59
                sz = int.from_bytes(src[p:p + extra], 'little')
                p += extra
            sz += 1
            out += src[p:p + sz]
            p += sz
            continue
        if kind == 1:                       # copy, 1바이트 오프셋
            length = 4 + ((tag >> 2) & 0x07)
            off = ((tag >> 5) << 8) | src[p + 1]
            p += 2
        elif kind == 2:                     # copy, 2바이트 오프셋
            length = (tag >> 2) + 1
            off = int.from_bytes(src[p + 1:p + 3], 'little')
            p += 3
        else:                               # copy, 4바이트 오프셋
            length = (tag >> 2) + 1
            off = int.from_bytes(src[p + 1:p + 5], 'little')
            p += 5
        if off == 0 or off > len(out):
            raise Truncated()
        start = len(out) - off
        for i in range(length):             # 겹치는 복사 허용
            out.append(out[start + i])
    if len(out) != n:
        raise Truncated()
    return bytes(out)


# ────────────────────────────────────────────────────────────────
# V8 structured clone
# ────────────────────────────────────────────────────────────────
TAG = {
    0xFF: 'version', 0x00: 'padding',
    0x5F: 'undefined', 0x30: 'null', 0x54: 'true', 0x46: 'false',
    0x49: 'int32', 0x55: 'uint32', 0x4E: 'double', 0x5A: 'bigint',
    0x53: 'utf8', 0x22: 'onebyte', 0x63: 'twobyte',
    0x6F: 'obj{', 0x7B: '}obj',
    0x61: 'sparse[', 0x40: ']sparse',
    0x41: 'dense[', 0x24: ']dense',
    0x3B: 'map{', 0x3A: '}map',
    0x27: 'set{', 0x2C: '}set',
    0x44: 'date', 0x5E: 'ref', 0x42: 'arraybuffer', 0x5C: 'hostobject',
}

_STR_TAGS = (0x22, 0x63, 0x53)


class V8Decoder:
    """엄격 모드 디코더 — 온전한 레코드의 시작점을 알 때 쓴다."""

    def __init__(self, buf, pos=0, max_depth=64):
        self.b = buf
        self.p = pos
        self.max_depth = max_depth

    # --- 원시 읽기 ---
    def _u(self):
        v, self.p = uvarint(self.b, self.p)
        return v

    def _z(self):
        v, self.p = zvarint(self.b, self.p)
        return v

    def _str(self, tag):
        n = self._u()
        raw = self.b[self.p:self.p + n]
        if len(raw) < n:
            raise Truncated()
        self.p += n
        if tag == 0x22:
            return raw.decode('latin-1')
        if tag == 0x63:
            return raw.decode('utf-16-le', 'replace')
        return raw.decode('utf-8', 'replace')

    # --- 값 ---
    def read(self, depth=0):
        if depth > self.max_depth:
            raise Truncated()
        if self.p >= len(self.b):
            raise Truncated()
        t = self.b[self.p]
        self.p += 1

        if t == 0xFF:                       # 버전 헤더는 건너뛴다
            self._u()
            return self.read(depth)
        if t == 0x00:
            return self.read(depth)
        if t in (0x5F, 0x30):
            return None
        if t == 0x54:
            return True
        if t == 0x46:
            return False
        if t == 0x49:
            return self._z()
        if t == 0x55:
            return self._u()
        if t == 0x4E:
            v = struct.unpack_from('<d', self.b, self.p)[0]
            self.p += 8
            return v
        if t == 0x44:                       # Date
            v = struct.unpack_from('<d', self.b, self.p)[0]
            self.p += 8
            return {'__date_ms': v}
        if t in _STR_TAGS:
            return self._str(t)
        if t == 0x5A:                       # BigInt
            bits = self._u()
            self.p += (bits >> 4) * 8 if bits >= 16 else 8
            return '<bigint>'
        if t == 0x5E:                       # 객체 참조
            return {'__ref': self._u()}
        if t == 0x6F:                       # 객체
            d = {}
            while self.p < len(self.b) and self.b[self.p] != 0x7B:
                k = self.read(depth + 1)
                v = self.read(depth + 1)
                d[str(k)] = v
            self.p += 1                     # '{'
            self._u()                       # 속성 개수
            return d
        if t == 0x41:                       # dense array
            cnt = self._u()
            arr = [self.read(depth + 1) for _ in range(min(cnt, 100000))]
            if self.p < len(self.b) and self.b[self.p] == 0x24:
                self.p += 1
                self._u()
                self._u()
            return arr
        if t == 0x61:                       # sparse array
            self._u()
            arr = {}
            while self.p < len(self.b) and self.b[self.p] != 0x40:
                k = self.read(depth + 1)
                v = self.read(depth + 1)
                arr[str(k)] = v
            self.p += 1
            self._u()
            self._u()
            # 0..n-1 로 촘촘하면 리스트로 되돌린다 (chat_messages 가 이 형태다)
            if arr and all(str(i) in arr for i in range(len(arr))):
                return [arr[str(i)] for i in range(len(arr))]
            return arr
        if t == 0x3B:                       # Map
            d = {}
            while self.p < len(self.b) and self.b[self.p] != 0x3A:
                k = self.read(depth + 1)
                v = self.read(depth + 1)
                d[str(k)] = v
            self.p += 1
            self._u()
            return d
        if t == 0x27:                       # Set
            s = []
            while self.p < len(self.b) and self.b[self.p] != 0x2C:
                s.append(self.read(depth + 1))
            self.p += 1
            self._u()
            return s
        if t == 0x42:                       # ArrayBuffer
            n = self._u()
            self.p += n
            return '<arraybuffer %d>' % n
        if t == 0x5C:                       # Blink 호스트 객체 (Blob / File)
            return self._host()
        raise Truncated()

    # Blink 호스트 객체 — 첨부파일 추적에 필요하다
    _HOST = {0x62: 'blob', 0x66: 'file', 0x69: 'blob_index',
             0x65: 'file_index', 0x6C: 'file_list', 0x4C: 'file_list_index'}

    def _host(self):
        if self.p >= len(self.b):
            raise Truncated()
        h = self.b[self.p]
        self.p += 1
        name = self._HOST.get(h)
        if name in ('blob_index', 'file_index'):
            return {'__%s' % name: self._u()}
        if name == 'blob':                  # uuid + type + 8바이트 크기
            uuid = self._str(0x53)
            mime = self._str(0x53)
            size = struct.unpack_from('<Q', self.b, self.p)[0]
            self.p += 8
            return {'__blob': uuid, 'type': mime, 'size': size}
        if name in ('file_list_index', 'file_list'):
            n = self._u()
            return {'__file_list': [self._u() for _ in range(min(n, 4096))]}
        raise Truncated()


# Chromium IndexedDB 값 래퍼 (실측)
#   FF 11 01 <크기 varint> <블롭번호 varint>  : 값이 너무 커서 외부 파일로 뺐다
#   FF 11 02 <snappy 스트림>                  : 내부 저장 + snappy 압축
#   FF 15 FE ... FF 10 ...                    : 래퍼 없이 바로 SSV
# ★ 01 을 모르면 긴 대화가 통째로 누락된다. DB 에는 크기만 있고 본문은
#   IndexedDB\<오리진>.indexeddb.blob\<db>\<디렉터리>\<번호> 에 따로 있다.
W_EXTERNAL, W_SNAPPY = 0x01, 0x02


def unwrap_idb_value(blob):
    """IDB 값 래퍼를 벗긴다. -> (종류, 페이로드바이트 또는 참조정보)"""
    if len(blob) >= 3 and blob[0] == 0xFF and blob[2] in (W_EXTERNAL, W_SNAPPY):
        kind = blob[2]
        if kind == W_SNAPPY:
            return 'snappy', snappy_decompress(blob[3:])
        size, p = uvarint(blob, 3)
        num = 0
        if p < len(blob):
            num, p = uvarint(blob, p)
        return 'external', {'size': size, 'blob_number': num}
    return 'plain', blob


def ssv_payload_start(blob):
    """Blink SSV 엔벨로프를 건너뛴 실제 V8 데이터 시작 위치.

    실측한 선두 (R5, Chromium 기반 Electron):
        FF 15              Blink 버전 21
        FE <8B offset><4B size>   trailer 위치 (전부 0 이면 trailer 없음)
        FF 10              V8 직렬화 버전 16
        6F ...             객체 시작
    0xFE 를 모르면 여기서 파싱이 멈춘다 — 이 한 바이트 때문에 엄격
    디코드가 전부 실패하고 salvage 로만 떨어진다.
    """
    p = 0
    for _ in range(8):
        if p >= len(blob):
            break
        t = blob[p]
        if t == 0xFF:                       # 버전 태그
            p += 1
            _, p = uvarint(blob, p)
        elif t == 0xFE:                     # trailer offset 태그
            p += 13                         # 1 + 8(오프셋) + 4(크기)
        else:
            break
    return p


def decode_ssv(blob):
    """IDB 래퍼 + Blink SSV 엔벨로프를 벗기고 V8 값을 꺼낸다.

    외부 블롭으로 빠진 값은 여기서 참조 정보만 돌려준다 — 본문은
    scenarios.resolve_external() 이 .blob 파일에서 읽어 온다.
    """
    kind, payload = unwrap_idb_value(blob)
    if kind == 'external':
        return {'__external_blob': payload}
    return V8Decoder(payload, ssv_payload_start(payload)).read()


def harvest_strings(buf, min_len=3, max_len=4096):
    """구제(salvage) 모드 — 구조가 깨진 영역에서 문자열만 긁어낸다.

    삭제/덮어쓰기된 조각은 시작점을 모르므로 엄격 파싱이 불가능하다.
    태그 바이트 + 길이 varint 가 맞물리는 지점만 수확한다.
    반환: [(오프셋, 태그명, 문자열)]
    """
    out = []
    n = len(buf)
    i = 0
    while i < n:
        t = buf[i]
        if t in _STR_TAGS:
            try:
                ln, p = uvarint(buf, i + 1)
            except Truncated:
                i += 1
                continue
            if min_len <= ln <= max_len and p + ln <= n:
                raw = buf[p:p + ln]
                try:
                    if t == 0x22:
                        s = raw.decode('ascii')
                    elif t == 0x63:
                        if ln % 2:
                            i += 1
                            continue
                        s = raw.decode('utf-16-le')
                    else:
                        s = raw.decode('utf-8')
                except UnicodeDecodeError:
                    i += 1
                    continue
                if sum(c.isprintable() for c in s) > len(s) * 0.8:
                    out.append((i, TAG[t], s))
                    i = p + ln
                    continue
        i += 1
    return out

def looks_utf16(buf, sample=4096):
    """버퍼가 UTF-16LE 텍스트인지 판정한다.

    널바이트 비율만 보면 안 된다. 영어를 UTF-16LE 로 쓰면 'A\x00' 이라
    널이 절반이지만, 한글은 '경' = AC BD 처럼 널이 아예 없다. 실측하면
    한국어 문장의 널 비율은 0.12 수준이라 0.3~0.7 창에 안 들어온다.
    그 결과 UTF-16 뷰가 만들어지지 않아 카빙 단계에서 통째로 놓친다.

    그래서 두 갈래로 본다.
      1) 널 비율 0.3~0.7        -> ASCII 계열 UTF-16
      2) 디코드했을 때 한글/CJK 비중이 높다 -> 동아시아어 UTF-16
    """
    s = buf[:sample]
    if len(s) < 8:
        return False
    if 0.3 < (s.count(0) / float(len(s))) < 0.7:
        return True
    if len(s) % 2:
        s = s[:-1]
    try:
        t = s.decode('utf-16-le')
    except UnicodeDecodeError:
        return False
    if not t:
        return False
    cjk = 0
    for c in t:
        o = ord(c)
        if 0xAC00 <= o <= 0xD7A3 or 0x3040 <= o <= 0x30FF or 0x4E00 <= o <= 0x9FFF:
            cjk += 1
    return cjk > len(t) * 0.2
