# -*- coding: utf-8 -*-
"""EWF(EnCase E01) 리더. 표준 라이브러리만 쓴다.

libewf/pyewf 는 컴파일이 필요하고 현장 분석 PC 에 깔기 어렵다. E01 v1 은
구조가 단순해서 struct + zlib 로 충분히 읽힌다.

구조
    [13B 파일 헤더] [섹션] [섹션] ...
    섹션 = 76B 서술자(종류16 · 다음오프셋8 · 크기8 · 패딩40 · adler32 4)
    'disk'/'volume' 이 디스크 기하를, 'sectors' 가 실제 데이터를,
    'table' 이 청크별 오프셋을 담는다. 세그먼트(.E01 .E02 …)마다 반복된다.

주의할 곳
  * table 항목의 최상위 비트가 압축 여부다. 나머지 31비트 + base_offset 이
    파일 내 위치. 길이는 '다음 항목 - 이 항목'으로만 알 수 있어서, 표의
    마지막 청크는 길이를 모른다. 그래서 마지막만 넉넉히 읽고 zlib 가
    스스로 끝을 찾게 둔다.
  * 비압축 청크는 뒤에 adler32 4바이트가 붙는다. 잘라내야 한다.
  * 세그먼트 번호는 .E01~.E99 다음 .EAA~.EZZ 로 넘어간다. 22개짜리면
    안 겪지만, 100개 넘는 이미지에서 조용히 순서가 틀어지면 최악이라
    정렬 규칙을 처음부터 맞춰 둔다.
"""
import os
import re
import struct
import zlib
from array import array

# FTK Imager 는 사건 정보에 비ASCII 가 있으면 [U+AD8C] 같은 꼴로 바꿔 넣는다.
# 한국어로 적은 사건번호·조사자가 전부 이 모양이 되므로 되돌려 준다.
_ESCAPED = re.compile(r'\[U\+([0-9A-Fa-f]{4,6})\]')


def _unescape(s):
    def one(m):
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return m.group(0)
    return _ESCAPED.sub(one, s)

SIG_E01 = b'EVF\x09\x0d\x0a\xff\x00'
SIG_L01 = b'LVF\x09\x0d\x0a\xff\x00'
SIG_EX01 = b'EVF2\x0d\x0a\x81\x00'

HDR_LEN = 13
SECTION_HDR = 76


class EwfError(Exception):
    pass


def _seg_key(ext, allow_alpha=True):
    """확장자를 세그먼트 번호로. E01->1 … E99->99, EAA->100, EAB->101 …

    EnCase 는 99 를 넘기면 숫자 대신 두 글자를 쓴다. 문자열 정렬에 맡기면
    E99 뒤에 EAA 가 아니라 앞에 와서 순서가 조용히 뒤집힌다.

    두 글자 형식은 .log 나 .lnk 같은 흔한 확장자와 겹친다(L+AA~ZZ). 그래서
    '이 파일이 이미지인가' 를 판정할 때는 숫자 형식만 받는다. 체인은 언제나
    01 로 시작하므로 그것으로 충분하고, 뒤따르는 조각을 줄 세울 때만
    두 글자를 허용한다.
    """
    e = ext.upper().lstrip('.')
    if len(e) != 3 or e[0] not in 'ELS':
        return None
    body = e[1:]
    if body.isdigit():
        n = int(body)
        return n if n >= 1 else None
    if allow_alpha and body.isalpha():
        return 100 + (ord(body[0]) - 65) * 26 + (ord(body[1]) - 65)
    return None


def find_segments(path):
    """첫 세그먼트 경로를 받아 체인 전체를 번호순으로 돌려준다."""
    d = os.path.dirname(os.path.abspath(path)) or '.'
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    # 입력 자체는 숫자 형식(.E01 등)이어야 체인으로 본다
    if _seg_key(ext, allow_alpha=False) is None:
        return [os.path.abspath(path)]

    pat = re.compile(r'^' + re.escape(stem) + r'\.[EeLlSs][0-9A-Za-z]{2}$')
    found = []
    try:
        names = os.listdir(d)
    except OSError:
        names = [base]
    for nm in names:
        if not pat.match(nm):
            continue
        k = _seg_key(os.path.splitext(nm)[1])
        if k is not None:
            found.append((k, os.path.join(d, nm)))
    if not found:
        return [os.path.abspath(path)]
    found.sort()
    return [p for _, p in found]


class _Segment(object):
    """세그먼트 파일 하나. 섹션 목록과 표를 들고 있는다."""

    def __init__(self, path, index):
        self.path = path
        self.index = index
        self.fh = open(path, 'rb')
        sig = self.fh.read(8)
        if sig == SIG_EX01:
            raise EwfError('Ex01(EnCase7) 형식은 아직 지원하지 않는다: %s' % path)
        if sig not in (SIG_E01, SIG_L01):
            raise EwfError('EWF 서명이 아니다: %s' % path)
        self.fh.read(5)                       # fields_start(1) + 세그먼트번호(2) + 패딩(2)
        self.sections = []                    # (종류, 데이터시작, 데이터크기, 섹션시작)
        self.volume = None

    def walk(self):
        """섹션 사슬을 따라간다. next 오프셋이 스스로를 가리키면 끝."""
        off = HDR_LEN
        seen = set()
        size = os.path.getsize(self.path)
        while 0 < off < size:
            if off in seen:                   # 사슬이 순환하면 손상된 파일이다
                break
            seen.add(off)
            self.fh.seek(off)
            hdr = self.fh.read(SECTION_HDR)
            if len(hdr) < SECTION_HDR:
                break
            stype = hdr[:16].rstrip(b'\x00').decode('latin1', 'replace')
            nxt, ssize = struct.unpack_from('<QQ', hdr, 16)
            dstart = off + SECTION_HDR
            dsize = max(0, ssize - SECTION_HDR)
            self.sections.append((stype, dstart, dsize, off))
            if stype in ('volume', 'disk', 'data') and self.volume is None:
                self.fh.seek(dstart)
                self.volume = self.fh.read(min(dsize, 1128))
            if stype == 'done' or nxt == off or nxt == 0:
                break
            off = nxt
        return self.sections

    def read_at(self, off, n):
        self.fh.seek(off)
        return self.fh.read(n)

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


def _parse_volume(buf):
    """디스크 기하. -> (청크수, 청크당섹터, 섹터크기, 총섹터)"""
    if buf is None or len(buf) < 24:
        raise EwfError('volume/disk 섹션을 읽지 못했다')
    chunk_count, spc, bps = struct.unpack_from('<III', buf, 4)
    sectors32 = struct.unpack_from('<I', buf, 16)[0]
    sectors64 = struct.unpack_from('<Q', buf, 16)[0]
    # v1 은 u32, 확장형은 u64. 상위 4바이트가 0이면 어느 쪽으로 읽어도 같다.
    sectors = sectors64 if sectors32 == 0 else sectors32
    if not (spc and bps):
        raise EwfError('디스크 기하가 비어 있다')
    if not sectors:
        sectors = chunk_count * spc
    return chunk_count, spc, bps, sectors


class EwfImage(object):
    """E01 체인을 하나의 연속된 디스크처럼 보이게 한다.

    read(n)/seek()/tell() 과 pread(off, n) 을 함께 제공한다. NTFS 층은
    pread 만 쓰고, 파일류 인터페이스는 다른 코드와 맞추기 위한 것이다.
    """

    format_name = 'EWF (E01)'

    def __init__(self, path, cache_chunks=96):
        self.paths = find_segments(path)
        self.segments = []
        self._pos = 0
        self._cache = {}
        self._cache_order = []
        self._cache_max = cache_chunks

        try:
            for i, p in enumerate(self.paths):
                seg = _Segment(p, i)
                seg.walk()
                self.segments.append(seg)
        except Exception:
            self.close()
            raise

        vol = None
        for seg in self.segments:
            if seg.volume:
                vol = seg.volume
                break
        try:
            (self.chunk_count, self.sectors_per_chunk,
             self.sector_size, self.sector_count) = _parse_volume(vol)
        except Exception:
            self.close()
            raise
        self.chunk_size = self.sectors_per_chunk * self.sector_size
        self.size = self.sector_count * self.sector_size

        self._build_table()

    # ── 청크 표 ─────────────────────────────────────────────
    def _build_table(self):
        """모든 세그먼트의 table 섹션을 모아 전역 청크 표를 만든다.

        200만 청크면 배열로 약 30 MB. 파일 오프셋·길이·압축여부·세그먼트를
        따로 담는다. dict 로 들면 수백 MB 라 배열이어야 한다.
        """
        offs = array('q')
        lens = array('i')
        segs = array('H')
        flags = bytearray()

        for seg in self.segments:
            # table 다음에 오는 것이 table2(사본)이므로 종류로만 거르면
            # 청크가 두 배가 된다. table 만 쓰고 table2 는 예비로 둔다.
            for si, (stype, dstart, dsize, soff) in enumerate(seg.sections):
                if stype != 'table':
                    continue
                buf = seg.read_at(dstart, min(dsize, 24))
                if len(buf) < 24:
                    continue
                nent, _pad, base = struct.unpack_from('<IIQ', buf, 0)
                if nent == 0 or nent > 4000000:
                    continue
                raw = seg.read_at(dstart + 24, nent * 4)
                if len(raw) < nent * 4:
                    nent = len(raw) // 4
                ent = array('I')
                ent.frombytes(raw[:nent * 4])
                # 이 표가 가리키는 sectors 데이터가 어디서 끝나는지.
                # 마지막 청크 길이를 모르기 때문에 이 값이 상한이 된다.
                limit = soff
                for k in range(nent):
                    v = ent[k]
                    comp = 1 if (v & 0x80000000) else 0
                    o = (v & 0x7FFFFFFF) + base
                    if k + 1 < nent:
                        nxt = (ent[k + 1] & 0x7FFFFFFF) + base
                        ln = nxt - o
                        if ln <= 0:
                            ln = -1
                    else:
                        ln = limit - o
                        if ln <= 0 or ln > self.chunk_size * 4:
                            ln = -1
                    offs.append(o)
                    lens.append(ln)
                    segs.append(seg.index)
                    flags.append(comp)

        if not offs:
            raise EwfError('table 섹션을 하나도 읽지 못했다')
        self._off, self._len, self._seg, self._flag = offs, lens, segs, flags
        self.table_chunks = len(offs)
        if self.table_chunks < self.chunk_count:
            # 세그먼트가 빠졌다는 뜻. 뒤쪽을 읽으면 조용히 0이 나오는 대신
            # 여기서 분명히 알린다.
            missing = self.chunk_count - self.table_chunks
            self.truncated = missing
        else:
            self.truncated = 0

    # ── 청크 읽기 ───────────────────────────────────────────
    def _chunk(self, idx):
        c = self._cache.get(idx)
        if c is not None:
            return c
        if idx >= self.table_chunks:
            raise EwfError('청크 %d 가 표에 없다. 세그먼트가 빠졌을 수 있다 '
                           '(표 %d / 선언 %d)'
                           % (idx, self.table_chunks, self.chunk_count))
        seg = self.segments[self._seg[idx]]
        off = self._off[idx]
        ln = self._len[idx]
        comp = self._flag[idx]
        if ln < 0:
            ln = self.chunk_size * 2 + 4096     # 길이 미상 — 넉넉히 읽는다
        raw = seg.read_at(off, ln)
        if comp:
            try:
                data = zlib.decompress(raw)
            except zlib.error:
                # 넉넉히 읽은 꼬리가 다음 섹션을 물었을 때. 스트림으로 풀면
                # zlib 가 알아서 자기 끝에서 멈춘다.
                try:
                    data = zlib.decompressobj().decompress(raw)
                except zlib.error as e:
                    raise EwfError('청크 %d 압축 해제 실패: %s' % (idx, e))
        else:
            data = raw[:self.chunk_size]        # 꼬리 4바이트는 adler32
        if len(data) > self.chunk_size:
            data = data[:self.chunk_size]
        self._cache[idx] = data
        self._cache_order.append(idx)
        if len(self._cache_order) > self._cache_max:
            old = self._cache_order.pop(0)
            self._cache.pop(old, None)
        return data

    # ── 공개 인터페이스 ─────────────────────────────────────
    def pread(self, offset, size):
        """디스크 오프셋에서 size 바이트. 끝을 넘으면 짧게 돌려준다."""
        if offset < 0:
            raise ValueError('음수 오프셋')
        if size <= 0 or offset >= self.size:
            return b''
        size = min(size, self.size - offset)
        out = []
        cs = self.chunk_size
        idx, rel = divmod(offset, cs)
        need = size
        while need > 0:
            data = self._chunk(idx)
            part = data[rel:rel + need]
            if not part:
                break
            out.append(part)
            need -= len(part)
            idx += 1
            rel = 0
        return b''.join(out)

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self._pos
        b = self.pread(self._pos, n)
        self._pos += len(b)
        return b

    def seek(self, off, whence=0):
        if whence == 1:
            off += self._pos
        elif whence == 2:
            off += self.size
        self._pos = max(0, off)
        return self._pos

    def tell(self):
        return self._pos

    def close(self):
        for s in self.segments:
            s.close()
        self.segments = []
        self._cache = {}
        self._cache_order = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def info(self):
        return {
            'format': self.format_name,
            'segments': len(self.paths),
            'sector_size': self.sector_size,
            'sector_count': self.sector_count,
            'chunk_size': self.chunk_size,
            'chunk_count': self.chunk_count,
            'size': self.size,
            'truncated_chunks': self.truncated,
        }

    def metadata(self):
        """header/header2 섹션의 사건 정보. 있으면 보고서에 적어 둔다."""
        out = {}
        for seg in self.segments[:1]:
            for stype, dstart, dsize, _ in seg.sections:
                if stype not in ('header', 'header2'):
                    continue
                raw = seg.read_at(dstart, min(dsize, 65536))
                try:
                    txt = zlib.decompress(raw)
                except zlib.error:
                    continue
                if txt[:2] == b'\xff\xfe':
                    s = txt.decode('utf-16-le', 'replace')
                else:
                    s = txt.decode('utf-8', 'replace')
                lines = [l for l in s.replace('\r\n', '\n').split('\n') if l]
                # 마지막 두 줄이 '필드명\t...' / '값\t...' 형태다
                for i in range(len(lines) - 1):
                    keys = lines[i].split('\t')
                    vals = lines[i + 1].split('\t')
                    if len(keys) < 3 or len(keys) != len(vals):
                        continue
                    if not any(k in ('c', 'n', 'a', 'e', 'av', 'ov') for k in keys):
                        continue
                    names = {'c': '사건번호', 'n': '증거번호', 'a': '설명',
                             'e': '조사자', 't': '비고', 'av': '취득도구',
                             'ov': '운영체제', 'm': '취득시각', 'u': '검증시각'}
                    for k, v in zip(keys, vals):
                        if v and k in names:
                            out.setdefault(names[k], _unescape(v))
                    break
                if out:
                    break
        return out
