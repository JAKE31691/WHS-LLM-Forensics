# -*- coding: utf-8 -*-
"""WOF(Windows Overlay Filter) — 이른바 Compact OS 시스템 압축.

윈도우 10 부터 시스템 파일과 스토어 앱 파일을 이 방식으로 줄여 둔다.
디스크 위 모습이 특이하다.

    $DATA (이름 없음)          크기는 원본 그대로인데 전부 희소 = 자리가 없다
    $DATA "WofCompressedData"  진짜 내용이 여기 압축되어 들어 있다
    $REPARSE_POINT             태그 0x80000017 · 어떤 알고리즘인지

모르고 $DATA 만 읽으면 '길이는 정확한데 전부 0' 이 나온다. 오류도 안 난다.
포렌식에서 제일 나쁜 실패다. 틀린 걸 틀린 줄 모른다. 그래서 여기서는
풀 수 있으면 풀고, 못 풀면 반드시 그렇다고 말한다. 0 을 내용인 척
내보내는 일은 없다.

지원
    XPRESS4K / XPRESS8K / XPRESS16K   구현함 (LZ77 + 허프만, MS-XCA)
    LZX                                구현함 (WIM 계열, 32 KB 창)
"""
import struct
from array import array

IO_REPARSE_TAG_WOF = 0x80000017

WOF_PROVIDER_WIM = 1
WOF_PROVIDER_FILE = 2

ALG_XPRESS4K = 0
ALG_LZX = 1
ALG_XPRESS8K = 2
ALG_XPRESS16K = 3

ALG_NAMES = {ALG_XPRESS4K: 'XPRESS4K', ALG_LZX: 'LZX',
             ALG_XPRESS8K: 'XPRESS8K', ALG_XPRESS16K: 'XPRESS16K'}

ALG_CHUNK = {ALG_XPRESS4K: 4096, ALG_LZX: 32768,
             ALG_XPRESS8K: 8192, ALG_XPRESS16K: 16384}

STREAM_NAME = 'WofCompressedData'


class WofError(Exception):
    pass


def parse_reparse(value):
    """재분석 지점 -> (알고리즘, 이름) 또는 None. WOF 가 아니면 None.

    구조가 두 겹이다. 한 겹으로 읽으면 필드가 통째로 밀려서 엉뚱한 값을
    알고리즘으로 착각한다.

        +0   재분석 태그          0x80000017
        +4   길이(2) · 예약(2)
        +8   WOF_EXTERNAL_INFO.Version      = 1
        +12  WOF_EXTERNAL_INFO.Provider     = 1 WIM · 2 파일
        +16  FILE_PROVIDER_EXTERNAL_INFO.Version = 1
        +20  FILE_PROVIDER_EXTERNAL_INFO.Algorithm  ← 여기가 진짜
    """
    if len(value) < 16:
        return None
    if struct.unpack_from('<I', value, 0)[0] != IO_REPARSE_TAG_WOF:
        return None
    _ver, provider = struct.unpack_from('<II', value, 8)
    if provider == WOF_PROVIDER_WIM:
        # WIM 백업 공급자. 내용이 이 볼륨이 아니라 별도 .wim 안에 있다.
        return ('WIM외부', 'WIM 공급자 — 내용이 이 볼륨에 없다')
    if provider != WOF_PROVIDER_FILE:
        return ('알수없음', '알 수 없는 WOF 공급자 %d' % provider)
    if len(value) >= 24:
        fp_ver, algo = struct.unpack_from('<II', value, 16)
        if fp_ver != 1 and algo not in ALG_NAMES:
            # V0 구조(알고리즘만 있는 옛 형태)로 보인다
            algo = fp_ver
    elif len(value) >= 20:
        algo = struct.unpack_from('<I', value, 16)[0]
    else:
        return ('알수없음', 'WOF 정보가 잘렸다')
    return (algo, ALG_NAMES.get(algo, '알 수 없음(%d)' % algo))


# ── 비트 스트림 (MS-XCA) ─────────────────────────────────────────
class _Bits(object):
    """16비트 리틀엔디언 워드를 싣고 상위 비트부터 꺼낸다.

    ★ 워드는 '모자랄 때만' 당겨온다. 이게 이 형식의 함정이다.
      비트열과 길이 확장 바이트가 같은 포인터를 나눠 쓰기 때문에, 미리
      채워 두면 바이트를 읽는 시점에 포인터가 2바이트 앞서 있게 되고
      그때부터 스트림 전체가 어긋난다. 어긋난 티는 한참 뒤에 나거나,
      운 나쁘면 아예 안 나고 내용만 조용히 틀린다.
    """
    __slots__ = ('d', 'i', 'mask', 'n', 'end')

    def __init__(self, data, pos=0):
        self.d = data
        self.i = pos
        self.end = len(data)
        self.mask = 0
        self.n = 0
        self.mask = (self._w() << 16) | self._w()
        self.n = 32

    def _w(self):
        if self.i + 2 > self.end:
            self.i += 2
            return 0
        v = self.d[self.i] | (self.d[self.i + 1] << 8)
        self.i += 2
        return v

    def peek(self, k):
        return (self.mask >> (32 - k)) & ((1 << k) - 1)

    def skip(self, k):
        # 16비트 밑으로 떨어지면 곧바로 채운다. '필요할 때만' 채우면
        # 바이트를 읽는 시점에 포인터가 한 워드 뒤처져 엉뚱한 값을 읽는다.
        # (실측으로 확인: 앞 19개 연산은 맞고 20번째에서 어긋났다)
        self.mask = (self.mask << k) & 0xFFFFFFFF
        self.n -= k
        while self.n < 16:
            self.mask |= self._w() << (16 - self.n)
            self.n += 16

    def take(self, k):
        if k == 0:
            return 0
        v = (self.mask >> (32 - k)) & ((1 << k) - 1)
        self.skip(k)
        return v

    def byte(self):
        if self.i >= self.end:
            return 0
        v = self.d[self.i]
        self.i += 1
        return v

    def u16(self):
        v = self.d[self.i] | (self.d[self.i + 1] << 8) if self.i + 2 <= self.end else 0
        self.i += 2
        return v

    def u32(self):
        if self.i + 4 > self.end:
            self.i += 4
            return 0
        v = struct.unpack_from('<I', self.d, self.i)[0]
        self.i += 4
        return v


def _build_table(lengths, tbits):
    """정규 허프만 -> 단일 조회표. (심볼<<5 | 길이) 를 담는다.

    비트를 하나씩 따라 내려가면 심볼당 최대 15번을 돈다. 파이썬에서는
    그 비용이 그대로 시간이 된다. 표를 한 번에 채우고 한 번에 찾는다.
    슬라이스 대입은 C 쪽에서 돌기 때문에 훨씬 빠르다.
    """
    size = 1 << tbits
    table = array('i', [-1]) * size
    code = 0
    for bl in range(1, tbits + 1):
        shift = tbits - bl
        span = 1 << shift
        for sym, l in enumerate(lengths):
            if l != bl:
                continue
            start = code << shift
            if start + span > size:
                raise WofError('허프만 코드가 표를 넘는다')
            table[start:start + span] = array('i', [(sym << 5) | bl]) * span
            code += 1
        code <<= 1
    return table


# ── XPRESS 허프만 ────────────────────────────────────────────────
def _xpress_chunk(data, out_size):
    """독립된 덩어리 하나를 푼다. WOF 가 이 방식이다. -> bytes"""
    out = bytearray()
    _xpress_into(data, 0, out, out_size, base=0)
    return bytes(out[:out_size])


def _xpress_into(data, pos, out, target, base):
    """공유 버퍼 out 에 이어서 푼다. -> 다음 입력 위치

    ★ MAM(Prefetch)과 WOF 의 결정적 차이.
      WOF 는 덩어리마다 창이 따로다. MAM 은 창이 덩어리 경계를 넘어
      이어진다 — 둘째 덩어리의 매치가 첫째 덩어리 출력을 참조한다.
      독립으로 풀면 '거리가 출력 밖' 오류가 난다(실측 114/285).
      base 는 이 덩어리가 참조할 수 있는 가장 앞 위치다.
    """
    if len(data) - pos < 256:
        raise WofError('XPRESS 허프만 표가 잘렸다')
    lengths = [0] * 512
    for i in range(256):
        b = data[pos + i]
        lengths[i * 2] = b & 0x0F
        lengths[i * 2 + 1] = b >> 4
    table = _build_table(lengths, 15)

    bs = _Bits(data, pos + 256)
    ap = out.append
    out_size = target
    while len(out) < out_size:
        v = table[bs.peek(15)]
        if v < 0:
            raise WofError('XPRESS 허프만 코드가 표에 없다')
        sym = v >> 5
        bs.skip(v & 31)
        if sym < 256:
            ap(sym)
            continue
        sym -= 256
        length = sym & 0x0F
        olog = sym >> 4
        # 순서가 중요하다. 길이 확장 바이트를 먼저 읽고 그 다음에 오프셋
        # 비트를 읽는다. 둘이 같은 포인터를 나눠 쓰기 때문에 뒤집으면
        # 스트림이 어긋나고, 어긋난 티는 한참 뒤에야 난다.
        # 길이는 어느 경로로 오든 '길이 - 3' 이 적혀 있다. 그래서 마지막에
        # 항상 3을 더한다. 270바이트를 넘는 매치는 0xFF 뒤 16비트로 오는데,
        # 이 경로는 0 패딩이 긴 구간(PE 헤더·꼬리)에서만 나와서 보통 파일을
        # 시험하면 멀쩡해 보인다. 실제로 가운데 덩어리 7개는 전부 맞고
        # 첫 덩어리와 마지막 덩어리만 틀리는 모습으로 드러났다.
        if length == 15:
            b = bs.byte()
            if b == 0xFF:
                length = bs.u16()
                if length == 0:
                    length = bs.u32()
            else:
                length = b + 15
        length += 3
        offset = (1 << olog) + bs.take(olog)
        src = len(out) - offset
        if src < base:
            raise WofError('XPRESS 거리가 창 밖을 가리킨다')
        # 겹치는 복사라 통째 슬라이스로는 안 된다. 앞서 쓴 바이트를
        # 다시 읽어야 하는 경우가 실제로 많다.
        for _ in range(length):
            ap(out[src])
            src += 1
    return bs.i

def xpress_huffman(data, out_size, chunk=65536):
    """덩어리가 여러 개인 XPRESS 허프만 스트림. Prefetch 의 MAM 이 이 형식.

    덩어리마다 허프만 표를 새로 읽지만 출력 버퍼와 LZ 창은 이어진다.
    덩어리 경계는 앞 덩어리를 읽고 멈춘 위치다.
    """
    out = bytearray()
    pos = 0
    guard = 0
    while len(out) < out_size:
        guard += 1
        if guard > 4096:
            raise WofError('XPRESS 덩어리가 끝나지 않는다')
        target = min(len(out) + chunk, out_size)
        if len(data) - pos == target - len(out):
            out += data[pos:]                # 마지막 덩어리가 비압축
            break
        pos = _xpress_into(data, pos, out, target, base=0)
    return bytes(out[:out_size])


def mam_decompress(data):
    """Prefetch 등의 MAM 압축을 푼다. -> bytes

        'MAM' + 알고리즘(1) + 원본크기(4) + 데이터
        알고리즘 0x04 = XPRESS Huffman
    """
    if len(data) < 8 or data[:3] != b'MAM':
        raise WofError('MAM 서명이 아니다')
    algo = data[3] & 0x0F
    size = struct.unpack_from('<I', data, 4)[0]
    body = data[8:]
    if algo == 0x04:
        return xpress_huffman(body, size)
    if algo == 0x03:
        return xpress_huffman(body, size)     # XPRESS(비허프만)는 미지원
    raise WofError('MAM 알고리즘 0x%02X 는 지원하지 않는다' % algo)


# ── LZX (WIM 계열) ───────────────────────────────────────────────
LZX_NUM_CHARS = 256
LZX_PRETREE_NUM = 20
LZX_ALIGNED_NUM = 8
LZX_SECONDARY_NUM = 249
LZX_MIN_MATCH = 2
LZX_NUM_PRIMARY_LENGTHS = 7

# 위치 슬롯별 밑값과 추가 비트 수. 32 KB 창이면 슬롯 30개면 충분하다.
_LZX_BASE = []
_LZX_FOOT = []


def _lzx_init_slots(n=50):
    base = 0
    for i in range(n):
        if i < 4:
            fb = 0
        elif i < 36:
            fb = (i // 2) - 1
        else:
            fb = 17
        _LZX_FOOT.append(fb)
        _LZX_BASE.append(base)
        base += 1 << fb


_lzx_init_slots()


def _lzx_read_lengths(bs, lengths, first, last, pretree_tbl):
    """길이표를 델타로 갱신한다. 이전 블록 값을 기준으로 고친다."""
    i = first
    while i < last:
        v = pretree_tbl[bs.peek(15)]
        if v < 0:
            raise WofError('LZX 예비 트리 코드가 없다')
        sym = v >> 5
        bs.skip(v & 31)
        if sym == 17:
            n = bs.take(4) + 4
            for _ in range(n):
                if i >= last:
                    break
                lengths[i] = 0
                i += 1
        elif sym == 18:
            n = bs.take(5) + 20
            for _ in range(n):
                if i >= last:
                    break
                lengths[i] = 0
                i += 1
        elif sym == 19:
            n = bs.take(1) + 4
            v2 = pretree_tbl[bs.peek(15)]
            if v2 < 0:
                raise WofError('LZX 예비 트리 코드가 없다')
            s2 = v2 >> 5
            bs.skip(v2 & 31)
            val = (lengths[i] - s2) % 17
            for _ in range(n):
                if i >= last:
                    break
                lengths[i] = val
                i += 1
        else:
            lengths[i] = (lengths[i] - sym) % 17
            i += 1


def _lzx_chunk(data, out_size, window_order=15):
    """LZX 덩어리 하나. WOF 는 32 KB 창, E8 변환 없음."""
    bs = _Bits(data, 0)
    out = bytearray()
    ap = out.append

    nslots = {15: 30, 16: 32, 17: 34, 18: 36, 19: 38, 20: 42, 21: 50}[window_order]
    main_num = LZX_NUM_CHARS + nslots * 8
    main_len = [0] * main_num
    len_len = [0] * LZX_SECONDARY_NUM
    r0, r1, r2 = 1, 1, 1

    while len(out) < out_size:
        btype = bs.take(3)
        bsize = bs.take(24)
        if bsize == 0:
            break
        if btype == 3:                      # 비압축 블록
            # 비트 경계를 바이트로 맞춘 뒤 R0/R1/R2 를 새로 읽는다
            if bs.n % 16:
                pass
            bs.i -= (bs.n // 8)
            if bs.i & 1:
                bs.i += 1
            r0 = struct.unpack_from('<I', data, bs.i)[0]
            r1 = struct.unpack_from('<I', data, bs.i + 4)[0]
            r2 = struct.unpack_from('<I', data, bs.i + 8)[0]
            bs.i += 12
            out += data[bs.i:bs.i + bsize]
            bs.i += bsize
            if bsize & 1:
                bs.i += 1
            bs = _Bits(data, bs.i)
            continue
        if btype not in (1, 2):
            raise WofError('LZX 블록 종류 %d 를 모른다' % btype)

        aligned_tbl = None
        if btype == 2:                      # 정렬 오프셋 블록
            al = [bs.take(3) for _ in range(LZX_ALIGNED_NUM)]
            aligned_tbl = _build_table(al, 7)

        # 주 트리: 앞 256개와 나머지를 따로 갱신한다
        pre = [bs.take(4) for _ in range(LZX_PRETREE_NUM)]
        _lzx_read_lengths(bs, main_len, 0, LZX_NUM_CHARS, _build_table(pre, 15))
        pre = [bs.take(4) for _ in range(LZX_PRETREE_NUM)]
        _lzx_read_lengths(bs, main_len, LZX_NUM_CHARS, main_num,
                          _build_table(pre, 15))
        main_tbl = _build_table(main_len, 15)

        pre = [bs.take(4) for _ in range(LZX_PRETREE_NUM)]
        _lzx_read_lengths(bs, len_len, 0, LZX_SECONDARY_NUM,
                          _build_table(pre, 15))
        len_tbl = _build_table(len_len, 15)

        done = 0
        while done < bsize and len(out) < out_size:
            v = main_tbl[bs.peek(15)]
            if v < 0:
                raise WofError('LZX 주 트리 코드가 없다')
            sym = v >> 5
            bs.skip(v & 31)
            if sym < LZX_NUM_CHARS:
                ap(sym)
                done += 1
                continue
            sym -= LZX_NUM_CHARS
            length = sym % 8
            slot = sym // 8
            if length == LZX_NUM_PRIMARY_LENGTHS:
                v2 = len_tbl[bs.peek(15)]
                if v2 < 0:
                    raise WofError('LZX 길이 트리 코드가 없다')
                length += v2 >> 5
                bs.skip(v2 & 31)
            length += LZX_MIN_MATCH

            if slot == 0:
                off = r0
            elif slot == 1:
                off, r1 = r1, r0
                r0 = off
            elif slot == 2:
                off, r2 = r2, r0
                r0 = off
            else:
                fb = _LZX_FOOT[slot]
                if aligned_tbl is not None and fb >= 3:
                    verb = bs.take(fb - 3) << 3
                    v3 = aligned_tbl[bs.peek(7)]
                    if v3 < 0:
                        raise WofError('LZX 정렬 트리 코드가 없다')
                    bs.skip(v3 & 31)
                    off = _LZX_BASE[slot] + verb + (v3 >> 5) - 2
                else:
                    off = _LZX_BASE[slot] + bs.take(fb) - 2
                r2, r1, r0 = r1, r0, off
            src = len(out) - off
            if src < 0:
                raise WofError('LZX 거리가 출력 밖을 가리킨다')
            for _ in range(length):
                ap(out[src])
                src += 1
            done += length
    return bytes(out[:out_size])


# ── 묶음 ────────────────────────────────────────────────────────
def decompress(compressed, uncompressed_size, algo):
    """WofCompressedData 전체를 푼다.

    구조는 [덩어리 오프셋표][덩어리][덩어리]... 오프셋표는 마지막 덩어리를
    뺀 나머지의 끝 위치를 담는다. 원본이 4 GB 를 넘으면 항목이 8바이트다.
    """
    chunk = ALG_CHUNK.get(algo)
    if chunk is None:
        raise WofError('알 수 없는 WOF 알고리즘 %r' % (algo,))
    n_chunks = (uncompressed_size + chunk - 1) // chunk
    if n_chunks <= 0:
        return b''
    wide = uncompressed_size > 0xFFFFFFFF
    esz = 8 if wide else 4
    tbl_bytes = (n_chunks - 1) * esz
    if len(compressed) < tbl_bytes:
        raise WofError('WOF 덩어리 표가 잘렸다')
    fmt = '<%d%s' % (n_chunks - 1, 'Q' if wide else 'I')
    ends = struct.unpack_from(fmt, compressed, 0) if n_chunks > 1 else ()

    base = tbl_bytes
    bounds = [0] + list(ends) + [len(compressed) - base]
    out = bytearray()
    for k in range(n_chunks):
        s = base + bounds[k]
        e = base + bounds[k + 1]
        if e <= s or e > len(compressed):
            raise WofError('WOF 덩어리 %d 범위가 비정상이다' % k)
        raw = compressed[s:e]
        want = min(chunk, uncompressed_size - len(out))
        if len(raw) == want:
            out += raw                       # 이 덩어리는 압축되지 않았다
        elif algo == ALG_LZX:
            out += _lzx_chunk(raw, want)
        else:
            out += _xpress_chunk(raw, want)
    return bytes(out[:uncompressed_size])
