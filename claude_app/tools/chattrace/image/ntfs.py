# -*- coding: utf-8 -*-
"""NTFS 볼륨 파서 — 이미지에서 파일 내용을 직접 꺼낸다.

mft.py 는 이미 뽑아 둔 $MFT 파일을 읽는다. 여기서는 그 앞 단계, 즉 디스크
이미지에서 $MFT 자체를 끄집어내고 임의의 파일 내용을 읽는 일을 한다.
레코드 해석은 mft.parse_record 를 그대로 쓴다. 두 벌 만들지 않는다.

  이미지 ──▶ 파티션 ──▶ [여기] ──▶ $MFT 파일 ──▶ mft.scan ──▶ 경로 목록
                           │                                    │
                           └────── 데이터런으로 파일 내용 ◀──────┘

ACL 을 거치지 않는다. 마운트와 달리 '남의 AppData' 라는 개념 자체가 없다.

까다로운 곳
  * $MFT 의 위치를 알려면 $MFT 를 읽어야 한다. 부트섹터가 첫 레코드의
    클러스터만 알려주므로, 그 레코드를 원시 클러스터로 직접 읽어 자기
    데이터런을 얻는 것으로 매듭을 푼다.
  * 데이터런 오프셋은 '앞 런과의 차이'이고 부호가 있다. 부호를 놓치면
    디스크 뒤쪽으로만 가다가 엉뚱한 데이터를 읽는다.
  * 희소(sparse) 런은 디스크에 자리가 없다. 0 으로 채워야 길이가 맞는다.
  * 큰 파일은 속성이 레코드에 안 들어가 $ATTRIBUTE_LIST 로 흩어진다.
    따라가지 않으면 파일이 조용히 잘린다.
"""
import struct

from .. import mft as mftmod
from . import wof as wofmod

SECTOR = 512

ATTR_STANDARD_INFORMATION = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_INDEX_ROOT = 0x90
ATTR_REPARSE_POINT = 0xC0
ATTR_END = 0xFFFFFFFF


class FileData(object):
    """파일 내용과 '그 내용을 믿어도 되는가'를 함께 들고 다닌다.

    status
        ok            내용 전체를 읽었다
        sparse        디스크에 내용이 없다 (희소)
        wof_failed    시스템 압축인데 못 풀었다
        wof_external  내용이 이 볼륨에 없다
        partial       일부만 읽었다
        encrypted     EFS
        no_data       $DATA 가 없다
        no_record     레코드를 못 읽었다
    """
    __slots__ = ('data', 'status', 'note', 'compression', 'declared_size')

    def __init__(self, data, status, note='', compression='', declared_size=None):
        self.data = data
        self.status = status
        self.note = note
        self.compression = compression
        self.declared_size = declared_size

    @property
    def ok(self):
        return self.status == 'ok'

    def __len__(self):
        return len(self.data) if self.data else 0

    def __repr__(self):
        return '<FileData %s %s B%s>' % (
            self.status, format(len(self), ','),
            ' ' + self.compression if self.compression else '')

FLAG_COMPRESSED = 0x0001
FLAG_ENCRYPTED = 0x4000
FLAG_SPARSE = 0x8000

MFT_ENTRY = 0          # $MFT 자신


class NtfsError(Exception):
    pass


# ── LZNT1 ────────────────────────────────────────────────────────
def lznt1_decompress(data):
    """NTFS 압축 속성 해제.

    4 KB 블록 단위. 블록 머리 2바이트의 최상위 비트가 압축 여부이고,
    길이/거리 토큰의 비트 배분이 '지금까지 낸 바이트 수'에 따라 달라진다.
    고정 비트로 읽으면 앞부분만 맞고 뒤로 갈수록 무너진다.
    """
    out = bytearray()
    p = 0
    n = len(data)
    while p + 2 <= n:
        hdr = struct.unpack_from('<H', data, p)[0]
        p += 2
        if hdr == 0:
            break
        size = (hdr & 0x0FFF) + 1
        block = data[p:p + size]
        p += size
        if not (hdr & 0x8000):
            out += block
            continue
        chunk = bytearray()
        q = 0
        bl = len(block)
        while q < bl:
            flags = block[q]
            q += 1
            for i in range(8):
                if q >= bl:
                    break
                if flags & (1 << i):
                    if q + 2 > bl:
                        q = bl
                        break
                    tok = block[q] | (block[q + 1] << 8)
                    q += 2
                    pos = len(chunk) - 1
                    lmask, oshift = 0x0FFF, 12
                    while pos >= 0x10:
                        pos >>= 1
                        lmask >>= 1
                        oshift -= 1
                    length = (tok & lmask) + 3
                    delta = (tok >> oshift) + 1
                    src = len(chunk) - delta
                    if src < 0:
                        q = bl
                        break
                    for _ in range(length):
                        chunk.append(chunk[src])
                        src += 1
                else:
                    chunk.append(block[q])
                    q += 1
        out += chunk
    return bytes(out)


# ── 데이터런 ─────────────────────────────────────────────────────
def decode_runlist(data, base_lcn=0):
    """데이터런 목록 -> [(lcn 또는 None, 클러스터수)]

    None 은 희소 런. 실제 디스크 자리가 없고 읽으면 0 이어야 한다.
    """
    runs = []
    p = 0
    lcn = base_lcn
    n = len(data)
    while p < n:
        h = data[p]
        p += 1
        if h == 0:
            break
        lenlen = h & 0x0F
        offlen = (h >> 4) & 0x0F
        if lenlen == 0 or p + lenlen + offlen > n:
            break
        length = int.from_bytes(data[p:p + lenlen], 'little')
        p += lenlen
        if offlen == 0:
            runs.append((None, length))         # 희소
        else:
            delta = int.from_bytes(data[p:p + offlen], 'little', signed=True)
            p += offlen
            lcn += delta
            runs.append((lcn, length))
    return runs


# ── 속성 ─────────────────────────────────────────────────────────
class Attribute(object):
    __slots__ = ('type', 'name', 'resident', 'flags', 'value',
                 'runs', 'real_size', 'alloc_size', 'init_size',
                 'start_vcn', 'last_vcn', 'comp_unit')

    def __init__(self):
        self.type = 0
        self.name = ''
        self.resident = True
        self.flags = 0
        self.value = b''
        self.runs = []
        self.real_size = 0
        self.alloc_size = 0
        self.init_size = 0
        self.start_vcn = 0
        self.last_vcn = 0
        self.comp_unit = 0

    @property
    def compressed(self):
        return bool(self.flags & FLAG_COMPRESSED) and self.comp_unit > 0

    @property
    def encrypted(self):
        return bool(self.flags & FLAG_ENCRYPTED)


def parse_attributes(rec):
    """픽스업이 끝난 레코드에서 속성을 모두 뽑는다. -> [Attribute]"""
    out = []
    if len(rec) < 48 or rec[:4] != b'FILE':
        return out
    p = struct.unpack_from('<H', rec, 20)[0]
    n = len(rec)
    while p + 16 <= n:
        atype = struct.unpack_from('<I', rec, p)[0]
        if atype == ATTR_END:
            break
        alen = struct.unpack_from('<I', rec, p + 4)[0]
        if alen < 16 or p + alen > n:
            break
        a = Attribute()
        a.type = atype
        nonres = rec[p + 8]
        nlen = rec[p + 9]
        noff = struct.unpack_from('<H', rec, p + 10)[0]
        a.flags = struct.unpack_from('<H', rec, p + 12)[0]
        if nlen:
            a.name = rec[p + noff:p + noff + nlen * 2].decode('utf-16-le', 'replace')
        if nonres:
            a.resident = False
            (a.start_vcn, a.last_vcn) = struct.unpack_from('<QQ', rec, p + 16)
            roff = struct.unpack_from('<H', rec, p + 32)[0]
            a.comp_unit = struct.unpack_from('<H', rec, p + 34)[0]
            (a.alloc_size, a.real_size, a.init_size) = struct.unpack_from('<QQQ', rec, p + 40)
            if 0 < roff < alen:
                a.runs = decode_runlist(rec[p + roff:p + alen])
        else:
            vlen = struct.unpack_from('<I', rec, p + 16)[0]
            voff = struct.unpack_from('<H', rec, p + 20)[0]
            a.value = rec[p + voff:p + voff + vlen]
            a.real_size = len(a.value)
        out.append(a)
        p += alen
    return out


def parse_attribute_list(value):
    """$ATTRIBUTE_LIST 항목 -> [(속성종류, 이름, 시작VCN, 레코드번호)]"""
    out = []
    p = 0
    n = len(value)
    while p + 26 <= n:
        atype, rlen = struct.unpack_from('<IH', value, p)
        if rlen < 24 or p + rlen > n:
            break
        nlen = value[p + 6]
        noff = value[p + 7]
        start_vcn = struct.unpack_from('<Q', value, p + 8)[0]
        ref = struct.unpack_from('<Q', value, p + 16)[0]
        name = ''
        if nlen:
            name = value[p + noff:p + noff + nlen * 2].decode('utf-16-le', 'replace')
        out.append((atype, name, start_vcn, ref & 0xFFFFFFFFFFFF))
        p += rlen
    return out


# ── 스트림 ───────────────────────────────────────────────────────
class NtfsStream(object):
    """속성 하나를 파일처럼 읽는다. seek/read/pread 를 제공한다."""

    def __init__(self, vol, attr, runs=None):
        self.vol = vol
        self.attr = attr
        self.runs = runs if runs is not None else attr.runs
        self.size = attr.real_size
        self._pos = 0
        self._map = None

    # 런 목록을 (시작VCN, lcn, 길이) 로 펼쳐 이진탐색이 되게 한다.
    def _build_map(self):
        m = []
        vcn = 0
        for lcn, cnt in self.runs:
            m.append((vcn, lcn, cnt))
            vcn += cnt
        self._map = m
        self._vcn_total = vcn
        return m

    def _cluster(self, vcn):
        """VCN -> (lcn 또는 None). 매핑 밖이면 None."""
        m = self._map if self._map is not None else self._build_map()
        lo, hi = 0, len(m) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            s, lcn, cnt = m[mid]
            if vcn < s:
                hi = mid - 1
            elif vcn >= s + cnt:
                lo = mid + 1
            else:
                return None if lcn is None else lcn + (vcn - s)
        return None

    def _raw_clusters(self, vcn, count):
        """연속된 VCN 범위를 읽는다. 희소 구간은 0 으로 채운다."""
        cs = self.vol.cluster_size
        out = bytearray()
        i = 0
        while i < count:
            lcn = self._cluster(vcn + i)
            # 물리적으로 이어지는 만큼 한 번에 읽는다. 클러스터마다
            # 따로 읽으면 EWF 청크 해제가 수십 배 늘어난다.
            run = 1
            if lcn is None:
                while i + run < count and self._cluster(vcn + i + run) is None:
                    run += 1
                out += b'\x00' * (cs * run)
            else:
                while (i + run < count and
                       self._cluster(vcn + i + run) == lcn + run):
                    run += 1
                out += self.vol.read_clusters(lcn, run)
            i += run
        return bytes(out)

    def _read_compressed(self, vcn, count):
        """압축 속성. 압축 단위(보통 16 클러스터)를 통째로 풀어서 자른다."""
        cs = self.vol.cluster_size
        unit = 1 << self.attr.comp_unit
        out = bytearray()
        start_unit = vcn // unit
        end_unit = (vcn + count + unit - 1) // unit
        for u in range(start_unit, end_unit):
            base = u * unit
            lcns = [self._cluster(base + k) for k in range(unit)]
            if all(l is None for l in lcns):
                block = b'\x00' * (cs * unit)            # 전부 희소
            elif all(l is not None for l in lcns):
                block = self._raw_clusters(base, unit)   # 압축 안 된 단위
            else:
                nalloc = sum(1 for l in lcns if l is not None)
                raw = self._raw_clusters(base, nalloc)
                block = lznt1_decompress(raw)
                if len(block) < cs * unit:
                    block += b'\x00' * (cs * unit - len(block))
            lo = max(0, vcn - base)
            hi = min(unit, vcn + count - base)
            out += block[lo * cs:hi * cs]
        return bytes(out)

    def pread(self, offset, size):
        if self.attr.resident:
            return self.attr.value[offset:offset + size]
        if offset >= self.size or size <= 0:
            return b''
        size = min(size, self.size - offset)
        cs = self.vol.cluster_size
        first = offset // cs
        last = (offset + size + cs - 1) // cs
        if self.attr.compressed:
            data = self._read_compressed(first, last - first)
        else:
            data = self._raw_clusters(first, last - first)
        rel = offset - first * cs
        out = data[rel:rel + size]
        # 초기화되지 않은 꼬리는 디스크에 옛 데이터가 남아 있어도 0 이어야
        # 한다. 그대로 내보내면 남의 파일 조각이 섞여 들어간다.
        init = self.attr.init_size or self.size
        if offset + len(out) > init:
            keep = max(0, init - offset)
            out = out[:keep] + b'\x00' * (len(out) - keep)
        return out

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


# ── 볼륨 ─────────────────────────────────────────────────────────
class NtfsVolume(object):
    def __init__(self, reader, offset=0):
        self.reader = reader
        self.offset = offset
        boot = reader.pread(offset, 512)
        if len(boot) < 512 or boot[3:11] != b'NTFS    ':
            raise NtfsError('NTFS 부트섹터가 아니다 (오프셋 %d)' % offset)

        self.sector_size = struct.unpack_from('<H', boot, 11)[0]
        spc = boot[13]
        if spc > 0x80:                 # 0x80 초과는 2의 거듭제곱 지수(음수)
            spc = 1 << (256 - spc)
        self.sectors_per_cluster = spc
        self.cluster_size = self.sector_size * spc
        self.total_sectors = struct.unpack_from('<Q', boot, 40)[0]
        self.mft_lcn = struct.unpack_from('<Q', boot, 48)[0]
        self.mftmirr_lcn = struct.unpack_from('<Q', boot, 56)[0]
        self.serial = struct.unpack_from('<Q', boot, 72)[0]

        v = struct.unpack_from('<b', boot, 64)[0]
        self.record_size = (1 << -v) if v < 0 else v * self.cluster_size
        v = struct.unpack_from('<b', boot, 68)[0]
        self.index_size = (1 << -v) if v < 0 else v * self.cluster_size

        if not (self.cluster_size and self.record_size):
            raise NtfsError('NTFS 기하가 비정상이다')
        self.size = self.total_sectors * self.sector_size

        self._mft_stream = None
        self._boot = boot
        self._bootstrap_mft()

    # ── 원시 읽기 ───────────────────────────────────────────
    def read_clusters(self, lcn, count):
        return self.reader.pread(self.offset + lcn * self.cluster_size,
                                 count * self.cluster_size)

    # ── $MFT 자체 부팅 ──────────────────────────────────────
    def _bootstrap_mft(self):
        """$MFT 의 위치를 알려면 $MFT 를 읽어야 하는 문제를 푼다.

        부트섹터가 첫 레코드의 클러스터를 알려 주므로, 그 레코드만 원시로
        읽어 자기 $DATA 런을 얻는다. 그 런으로 $MFT 전체를 스트림으로 연 뒤,
        속성이 흩어져 있으면 확장 레코드까지 따라가 런을 잇는다.
        """
        raw = self.reader.pread(self.offset + self.mft_lcn * self.cluster_size,
                                self.record_size)
        rec = mftmod._apply_fixup(raw)
        if rec is None or rec[:4] != b'FILE':
            raise NtfsError('$MFT 첫 레코드를 읽지 못했다')
        attrs = parse_attributes(rec)
        data = [a for a in attrs if a.type == ATTR_DATA and not a.name]
        if not data:
            raise NtfsError('$MFT 에 $DATA 가 없다')
        base = data[0]
        runs = list(base.runs)
        stream = NtfsStream(self, base, runs)
        self._mft_stream = stream

        alist = [a for a in attrs if a.type == ATTR_ATTRIBUTE_LIST]
        if alist:
            # $MFT 가 조각나 런 목록이 여러 레코드로 흩어진 경우.
            # 방금 만든 (불완전할 수 있는) 스트림으로 확장 레코드를 읽는다.
            val = alist[0].value
            if not alist[0].resident:
                val = NtfsStream(self, alist[0]).pread(0, alist[0].real_size)
            extra = []
            for atype, name, svcn, ref in parse_attribute_list(val):
                if atype != ATTR_DATA or name or ref == MFT_ENTRY:
                    continue
                er = self.record(ref)
                if er is None:
                    continue
                for a in parse_attributes(er):
                    if a.type == ATTR_DATA and not a.name and a.start_vcn == svcn:
                        extra.append((svcn, a.runs))
            if extra:
                extra.sort()
                merged = list(base.runs)
                lcn_cursor = 0
                for _svcn, rl in extra:
                    merged.extend(rl)
                stream.runs = merged
                stream._map = None
        self.mft_record_count = stream.size // self.record_size

    # ── 레코드 접근 ─────────────────────────────────────────
    def record_raw(self, entry):
        """디스크에 있는 그대로. mft.parse_record 에 줄 때는 이쪽이다.

        parse_record 가 스스로 픽스업을 하기 때문에, 이미 적용된 것을 주면
        두 번 적용되어 서명 대조에 걸리고 조용히 None 이 나온다. 겉보기엔
        '그런 파일이 없다' 로 보여서 찾기 어렵다.
        """
        if self._mft_stream is None:
            return None
        raw = self._mft_stream.pread(entry * self.record_size, self.record_size)
        if len(raw) < 48 or raw[:4] != b'FILE':
            return None
        return raw

    def record(self, entry):
        """픽스업까지 끝난 레코드. parse_attributes 에 줄 때는 이쪽이다."""
        raw = self.record_raw(entry)
        if raw is None:
            return None
        return mftmod._apply_fixup(raw)

    def iter_mft(self, chunk=512):
        """($MFT 원본 그대로) 레코드를 순서대로 흘린다. -> (번호, 원시바이트)

        픽스업은 적용하지 않는다. mft.parse_record 가 스스로 하기 때문에
        여기서 미리 손대면 두 번 적용되어 깨진다.
        """
        rs = self.record_size
        total = self.mft_record_count
        entry = 0
        while entry < total:
            want = min(chunk, total - entry)
            buf = self._mft_stream.pread(entry * rs, want * rs)
            if not buf:
                break
            got = len(buf) // rs
            for i in range(got):
                yield entry + i, buf[i * rs:(i + 1) * rs]
            if got < want:
                break
            entry += want

    def dump_mft(self, out_path, progress=None):
        """$MFT 를 파일로 그대로 뽑는다. 기존 mft.scan 이 그대로 먹는다."""
        rs = self.record_size
        total = self.mft_record_count
        step = 2048
        done = 0
        with open(out_path, 'wb') as f:
            while done < total:
                want = min(step, total - done)
                buf = self._mft_stream.pread(done * rs, want * rs)
                if not buf:
                    break
                f.write(buf)
                done += len(buf) // rs
                if progress:
                    progress(done, total)
        return done * rs

    # ── 속성 모으기 ─────────────────────────────────────────
    def all_attributes(self, entry):
        """기본 레코드 + 확장 레코드의 속성을 전부 모은다.

        큰 파일·조각난 파일은 속성이 여러 레코드에 흩어진다. 기본 레코드만
        보면 $DATA 가 아예 없거나 앞부분만 있는 채로 조용히 잘린다.
        """
        rec = self.record(entry)
        if rec is None:
            return []
        attrs = parse_attributes(rec)
        alist = [a for a in attrs if a.type == ATTR_ATTRIBUTE_LIST]
        if not alist:
            return attrs
        val = alist[0].value
        if not alist[0].resident:
            val = NtfsStream(self, alist[0]).pread(0, alist[0].real_size)
        seen = set()
        for atype, anm, svcn, ref in parse_attribute_list(val):
            if ref == entry or ref in seen:
                continue
            seen.add(ref)
            er = self.record(ref)
            if er is None:
                continue
            attrs.extend(parse_attributes(er))
        return attrs

    def data_stream(self, entry, name='', attrs=None):
        """파일의 $DATA 스트림. 속성이 흩어져 있으면 모아서 잇는다."""
        if attrs is None:
            attrs = self.all_attributes(entry)
        cand = [a for a in attrs if a.type == ATTR_DATA and a.name == name]
        if not cand:
            return None

        resident = [a for a in cand if a.resident]
        if resident:
            return NtfsStream(self, resident[0])

        # 조각난 속성은 start_vcn 순으로 이어 붙인다. 순서를 안 맞추면
        # 파일 중간이 뒤섞인 채로 나오는데, 겉보기엔 정상이라 더 위험하다.
        cand.sort(key=lambda a: a.start_vcn)
        head = cand[0]
        runs = []
        seen_vcn = set()
        for a in cand:
            if a.start_vcn in seen_vcn:
                continue                     # 같은 조각이 두 번 잡힌 경우
            seen_vcn.add(a.start_vcn)
            runs.extend(a.runs)
        st = NtfsStream(self, head, runs)
        st.size = max(a.real_size for a in cand) or head.real_size
        return st

    # ── 파일 내용 ───────────────────────────────────────────
    def file_content(self, entry, max_bytes=None):
        """파일 내용을 읽는다. -> FileData

        '읽었다'와 '읽은 척했다'를 구분하는 게 이 함수의 요점이다.
        시스템 압축(WOF) 파일은 $DATA 가 통째로 비어 있어서, 모르고 읽으면
        길이가 정확한 0 덩어리가 나온다. 오류도 안 난다. 그래서 상태를
        값에 실어 돌려준다. 호출 쪽이 무시할 수 없게.
        """
        attrs = self.all_attributes(entry)
        if not attrs:
            return FileData(None, 'no_record', '레코드를 읽지 못했다')

        st = self.data_stream(entry, '', attrs)
        wof_alg = None
        for a in attrs:
            if a.type == ATTR_REPARSE_POINT and a.resident:
                got = wofmod.parse_reparse(a.value)
                if got:
                    wof_alg, wof_name = got
                    break

        if wof_alg is not None:
            comp = None
            for a in attrs:
                if a.type == ATTR_DATA and a.name == wofmod.STREAM_NAME:
                    comp = a
                    break
            if comp is None:
                return FileData(None, 'wof_missing',
                                '시스템 압축(%s) 인데 압축 스트림이 없다' % wof_name,
                                compression=wof_name)
            if wof_alg == 'WIM외부':
                return FileData(None, 'wof_external',
                                '내용이 이 볼륨에 없다 (WIM 공급자)',
                                compression=wof_name)
            size = st.size if st is not None else 0
            raw = NtfsStream(self, comp).pread(0, comp.real_size)
            try:
                data = wofmod.decompress(raw, size, wof_alg)
            except Exception as e:
                return FileData(None, 'wof_failed',
                                '시스템 압축(%s) 해제 실패: %s' % (wof_name, e),
                                compression=wof_name, declared_size=size)
            if len(data) != size:
                return FileData(data, 'partial',
                                '시스템 압축(%s) 일부만 풀렸다 (%d/%d)'
                                % (wof_name, len(data), size),
                                compression=wof_name, declared_size=size)
            if max_bytes is not None:
                data = data[:max_bytes]
            return FileData(data, 'ok', '', compression=wof_name,
                            declared_size=size)

        if st is None:
            return FileData(None, 'no_data', '$DATA 속성이 없다')
        if st.attr.encrypted:
            return FileData(None, 'encrypted', 'EFS 로 암호화되어 있다')

        n = st.size if max_bytes is None else min(st.size, max_bytes)
        data = st.pread(0, n)

        # 비상주인데 런이 전부 희소면 디스크에 내용이 없다는 뜻이다.
        # 위에서 WOF 도 아니었으니, 진짜 희소 파일이거나 우리가 모르는
        # 무언가다. 어느 쪽이든 '0 을 읽었다'고 해선 안 된다.
        if (not st.attr.resident and st.size and st.runs and
                all(l is None for l, _ in st.runs)):
            return FileData(data, 'sparse',
                            '$DATA 가 전부 희소다 — 디스크에 내용이 없다',
                            declared_size=st.size)
        return FileData(data, 'ok', '',
                        compression='NTFS압축' if st.attr.compressed else '',
                        declared_size=st.size)

    def read_file(self, entry, name='', max_bytes=None):
        """예전 호출부와의 호환용. 내용만 주고 상태는 버린다."""
        if name:
            st = self.data_stream(entry, name)
            if st is None:
                return None
            n = st.size if max_bytes is None else min(st.size, max_bytes)
            return st.pread(0, n)
        return self.file_content(entry, max_bytes).data

    def info(self):
        return {
            'sector_size': self.sector_size,
            'cluster_size': self.cluster_size,
            'total_sectors': self.total_sectors,
            'size': self.size,
            'mft_lcn': self.mft_lcn,
            'record_size': self.record_size,
            'mft_records': self.mft_record_count,
            'serial': '%016X' % self.serial,
        }
