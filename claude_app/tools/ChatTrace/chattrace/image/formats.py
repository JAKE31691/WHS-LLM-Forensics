# -*- coding: utf-8 -*-
"""이미지 형식 판별과 열기.

지원
    .E01/.Ex  EnCase EWF v1        (ewf.py)
    .dd .raw .img .001             원시 이미지 · 분할 원시
    .vmdk                          VMware — 희소(sparse) · 평면(flat) · 분할

전부 같은 인터페이스를 낸다. pread(offset, size) · size · sector_size.
그래야 위층(파티션·NTFS)이 형식을 몰라도 된다.
"""
import os
import re
import struct
import zlib

from .ewf import EwfImage, EwfError, SIG_E01, SIG_L01, SIG_EX01

SECTOR = 512


class ImageError(Exception):
    pass


# ── 원시 이미지 ──────────────────────────────────────────────────
class RawImage(object):
    """dd/raw. 분할본(.001 .002 …)도 한 덩어리로 본다."""

    format_name = '원시 이미지 (dd/raw)'

    def __init__(self, path, sector_size=SECTOR):
        self.paths = self._chain(path)
        self.sector_size = sector_size
        self._fh = []
        self._start = []
        pos = 0
        for p in self.paths:
            self._start.append(pos)
            pos += os.path.getsize(p)
            self._fh.append(open(p, 'rb'))
        self.size = pos
        self.sector_count = self.size // sector_size

    @staticmethod
    def _chain(path):
        d = os.path.dirname(os.path.abspath(path)) or '.'
        base = os.path.basename(path)
        stem, ext = os.path.splitext(base)
        if not re.match(r'^\.\d{3}$', ext):
            return [os.path.abspath(path)]
        pat = re.compile(r'^' + re.escape(stem) + r'\.(\d{3})$')
        got = []
        for nm in os.listdir(d):
            m = pat.match(nm)
            if m:
                got.append((int(m.group(1)), os.path.join(d, nm)))
        got.sort()
        return [p for _, p in got] or [os.path.abspath(path)]

    def pread(self, offset, size):
        if offset < 0 or size <= 0 or offset >= self.size:
            return b''
        size = min(size, self.size - offset)
        out = []
        need = size
        for i, st in enumerate(self._start):
            if need <= 0:
                break
            end = st + os.path.getsize(self.paths[i])
            if offset >= end:
                continue
            f = self._fh[i]
            f.seek(offset - st)
            b = f.read(min(need, end - offset))
            if not b:
                break
            out.append(b)
            offset += len(b)
            need -= len(b)
        return b''.join(out)

    def close(self):
        for f in self._fh:
            try:
                f.close()
            except Exception:
                pass
        self._fh = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def info(self):
        return {'format': self.format_name, 'segments': len(self.paths),
                'sector_size': self.sector_size, 'size': self.size,
                'sector_count': self.sector_count}

    def metadata(self):
        return {}


# ── VMDK ─────────────────────────────────────────────────────────
VMDK_SPARSE_MAGIC = b'KDMV'
VMDK_COWD_MAGIC = b'COWD'


class _VmdkExtent(object):
    """익스텐트 하나. 평면이면 오프셋 고정, 희소면 그레인 표를 쓴다."""

    def __init__(self, path, sectors, kind, flat_offset=0):
        self.path = path
        self.sectors = sectors
        self.kind = kind
        self.fh = open(path, 'rb')
        self.flat_offset = flat_offset * SECTOR
        self.grain_size = 0
        self.gt_entries = 0
        self.gd = None
        self.compressed = False
        if kind == 'SPARSE':
            self._read_sparse_header()

    def _read_sparse_header(self):
        self.fh.seek(0)
        h = self.fh.read(512)
        if h[:4] != VMDK_SPARSE_MAGIC:
            if h[:4] == VMDK_COWD_MAGIC:
                raise ImageError('COWD(ESX 구형) vmdk 는 지원하지 않는다: %s' % self.path)
            raise ImageError('vmdk 희소 헤더가 아니다: %s' % self.path)
        (_ver, flags) = struct.unpack_from('<II', h, 4)
        capacity, grain, _doff, _dsize = struct.unpack_from('<QQQQ', h, 12)
        gte = struct.unpack_from('<I', h, 44)[0]
        rgd, gd = struct.unpack_from('<QQ', h, 48)
        comp = struct.unpack_from('<H', h, 77)[0] if len(h) > 78 else 0
        self.capacity = capacity
        self.grain_size = grain or 128
        self.gt_entries = gte or 512
        self.compressed = bool(flags & 0x10000) or comp == 1
        use = gd if gd and gd != 0xFFFFFFFFFFFFFFFF else rgd
        if not use:
            raise ImageError('vmdk 그레인 디렉터리가 없다: %s' % self.path)
        span = self.grain_size * self.gt_entries
        n_gd = (capacity + span - 1) // span
        self.fh.seek(use * SECTOR)
        raw = self.fh.read(n_gd * 4)
        self.gd = struct.unpack('<%dI' % (len(raw) // 4), raw)
        self._gt_cache = {}

    def _grain_offset(self, grain_index):
        gt_i, gt_off = divmod(grain_index, self.gt_entries)
        if gt_i >= len(self.gd):
            return 0
        gt_sector = self.gd[gt_i]
        if not gt_sector:
            return 0
        tab = self._gt_cache.get(gt_i)
        if tab is None:
            self.fh.seek(gt_sector * SECTOR)
            raw = self.fh.read(self.gt_entries * 4)
            tab = struct.unpack('<%dI' % (len(raw) // 4), raw)
            if len(self._gt_cache) > 512:
                self._gt_cache.clear()
            self._gt_cache[gt_i] = tab
        return tab[gt_off] if gt_off < len(tab) else 0

    def pread(self, offset, size):
        if self.kind == 'FLAT' or self.kind == 'VMFS':
            self.fh.seek(self.flat_offset + offset)
            return self.fh.read(size)
        if self.kind == 'ZERO':
            return b'\x00' * size
        gs = self.grain_size * SECTOR
        out = []
        need = size
        while need > 0:
            gi, rel = divmod(offset, gs)
            sec = self._grain_offset(gi)
            take = min(need, gs - rel)
            if not sec:
                out.append(b'\x00' * take)
            else:
                if self.compressed:
                    self.fh.seek(sec * SECTOR)
                    mh = self.fh.read(12)
                    if len(mh) < 12:
                        out.append(b'\x00' * take)
                        offset += take
                        need -= take
                        continue
                    clen = struct.unpack_from('<I', mh, 8)[0]
                    body = self.fh.read(clen)
                    try:
                        g = zlib.decompress(body)
                    except zlib.error:
                        g = b'\x00' * gs
                else:
                    self.fh.seek(sec * SECTOR)
                    g = self.fh.read(gs)
                if len(g) < gs:
                    g += b'\x00' * (gs - len(g))
                out.append(g[rel:rel + take])
            offset += take
            need -= take
        return b''.join(out)

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


class VmdkImage(object):
    format_name = 'VMware VMDK'

    _EXT = re.compile(
        r'^\s*(RW|RDONLY|NOACCESS)\s+(\d+)\s+(FLAT|SPARSE|ZERO|VMFS|VMFSSPARSE)'
        r'(?:\s+"([^"]+)"(?:\s+(\d+))?)?', re.I)

    def __init__(self, path, sector_size=SECTOR):
        self.path = os.path.abspath(path)
        self.sector_size = sector_size
        self.extents = []
        self._start = []
        desc = self._descriptor(self.path)
        base = os.path.dirname(self.path)
        pos = 0
        for m in self._EXT.finditer(desc):
            _acc, nsec, kind, fname, foff = m.groups()
            nsec = int(nsec)
            kind = kind.upper()
            if kind == 'VMFSSPARSE':
                kind = 'SPARSE'
            if kind == 'ZERO' or not fname:
                ex = _VmdkExtent(self.path, nsec, 'ZERO')
            else:
                p = fname if os.path.isabs(fname) else os.path.join(base, fname)
                if not os.path.exists(p):
                    raise ImageError('vmdk 익스텐트 파일이 없다: %s' % p)
                ex = _VmdkExtent(p, nsec, kind, int(foff or 0))
            self._start.append(pos)
            self.extents.append(ex)
            pos += nsec * SECTOR
        if not self.extents:
            raise ImageError('vmdk 익스텐트를 찾지 못했다: %s' % self.path)
        self.size = pos
        self.sector_count = pos // SECTOR

    @staticmethod
    def _descriptor(path):
        """설명문은 파일 맨 앞(평면형)이나 희소 헤더가 가리키는 곳에 있다."""
        with open(path, 'rb') as f:
            head = f.read(2048)
            if head[:4] == VMDK_SPARSE_MAGIC:
                doff, dsize = struct.unpack_from('<QQ', head, 28)
                if doff and dsize:
                    f.seek(doff * SECTOR)
                    return f.read(dsize * SECTOR).decode('latin1', 'replace')
                # 설명문이 없는 단일 희소 파일 = 자기 자신이 유일한 익스텐트
                cap = struct.unpack_from('<Q', head, 12)[0]
                return 'RW %d SPARSE "%s"' % (cap, os.path.basename(path))
            if b'# Disk DescriptorFile' in head or b'createType' in head:
                with open(path, 'rb') as g:
                    return g.read(65536).decode('latin1', 'replace')
        raise ImageError('vmdk 설명문을 읽지 못했다: %s' % path)

    def pread(self, offset, size):
        if offset < 0 or size <= 0 or offset >= self.size:
            return b''
        size = min(size, self.size - offset)
        out = []
        need = size
        for i, st in enumerate(self._start):
            if need <= 0:
                break
            ex = self.extents[i]
            end = st + ex.sectors * SECTOR
            if offset >= end:
                continue
            take = min(need, end - offset)
            b = ex.pread(offset - st, take)
            if len(b) < take:
                b += b'\x00' * (take - len(b))
            out.append(b)
            offset += take
            need -= take
        return b''.join(out)

    def close(self):
        for e in self.extents:
            e.close()
        self.extents = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def info(self):
        return {'format': self.format_name, 'segments': len(self.extents),
                'sector_size': self.sector_size, 'size': self.size,
                'sector_count': self.sector_count}

    def metadata(self):
        return {}


# ── 판별 ─────────────────────────────────────────────────────────
IMAGE_EXT = ('.e01', '.ex01', '.l01', '.s01', '.dd', '.raw', '.img',
             '.vmdk', '.vhd', '.vhdx', '.bin', '.001')


def looks_like_image(path):
    if not os.path.isfile(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT or re.match(r'^\.e\d{2}$', ext) or re.match(r'^\.\d{3}$', ext):
        return True
    try:
        with open(path, 'rb') as f:
            head = f.read(8)
    except OSError:
        return False
    return head in (SIG_E01, SIG_L01, SIG_EX01) or head[:4] == VMDK_SPARSE_MAGIC


def open_image(path, sector_size=SECTOR):
    """확장자가 아니라 내용으로 고른다. 이름이 바뀐 이미지가 흔하다."""
    if not os.path.exists(path):
        raise ImageError('파일이 없다: %s' % path)
    if os.path.isdir(path):
        raise ImageError('폴더다. 이미지 파일을 지정한다: %s' % path)
    with open(path, 'rb') as f:
        head = f.read(2048)

    if head[:8] in (SIG_E01, SIG_L01):
        return EwfImage(path)
    if head[:8] == SIG_EX01:
        raise ImageError('Ex01(EnCase7) 형식이다. 아직 지원하지 않는다.')
    if head[:4] in (VMDK_SPARSE_MAGIC, VMDK_COWD_MAGIC) or \
            b'# Disk DescriptorFile' in head:
        return VmdkImage(path, sector_size)
    if head[:8] == b'conectix':
        raise ImageError('VHD 형식이다. 아직 지원하지 않는다.')
    if head[:8] == b'vhdxfile':
        raise ImageError('VHDX 형식이다. 아직 지원하지 않는다.')
    if head[:3] == b'QFI':
        raise ImageError('QCOW 형식이다. 아직 지원하지 않는다.')
    if head[:13] == b'AFF' or head[:4] == b'AFF4':
        raise ImageError('AFF 형식이다. 아직 지원하지 않는다.')
    # 나머지는 원시로 본다. 부트서명이 있으면 거의 확실하다.
    return RawImage(path, sector_size)


def describe(img):
    """사람이 읽는 한 줄 요약."""
    i = img.info()
    gb = i['size'] / 1073741824.0
    seg = i.get('segments', 1)
    s = '%s · %.1f GB · %s 바이트/섹터' % (i['format'], gb, i['sector_size'])
    if seg > 1:
        s += ' · 조각 %d개' % seg
    if i.get('truncated_chunks'):
        s += ' · [경고] 청크 %s개 누락' % format(i['truncated_chunks'], ',')
    return s
