# -*- coding: utf-8 -*-
"""파티션 테이블(GPT / MBR) 파서.

디스크 이미지 하나에는 보통 파티션이 여러 개 들어 있다. 윈도우 11 이면
대개 이렇다.

    1  EFI 시스템        약 100 MB   FAT32   부팅용
    2  MS 예약           약  16 MB   (없음)  윈도우 내부용
    3  기본 데이터       나머지 전부  NTFS    ← 우리가 볼 곳
    4  복구              약 500 MB~   NTFS    복구 환경

'파티션이 여러 개'라는 건 이 목록이 나온다는 뜻이고, 실제로 볼 것은
보통 셋째 하나다. 그래서 자동 선택은 '가장 큰 NTFS'를 고르되, 사람이
-—partition 으로 뒤집을 수 있게 둔다.
"""
import struct
import uuid

SECTOR = 512

GPT_SIG = b'EFI PART'
MBR_SIG = b'\x55\xaa'

TYPE_NAMES = {
    'c12a7328-f81f-11d2-ba4b-00a0c93ec93b': 'EFI 시스템',
    'e3c9e316-0b5c-4db8-817d-f92df00215ae': 'MS 예약',
    'ebd0a0a2-b9e5-4433-87c0-68b6b72699c7': '기본 데이터',
    'de94bba4-06d1-4d40-a16a-bfd50179d6ac': '윈도우 복구',
    '5808c8aa-7e8f-42e0-85d2-e1e90434cfb3': 'LDM 메타데이터',
    'af9b60a0-1431-4f62-bc68-3311714a69ad': 'LDM 데이터',
    '0fc63daf-8483-4772-8e79-3d69d8477de4': '리눅스 파일시스템',
    '21686148-6449-6e6f-744e-656564454649': 'BIOS 부팅',
}

MBR_TYPE_NAMES = {
    0x07: 'NTFS/exFAT', 0x0B: 'FAT32', 0x0C: 'FAT32 LBA',
    0x06: 'FAT16', 0x0E: 'FAT16 LBA', 0x27: '복구',
    0x83: '리눅스', 0x82: '리눅스 스왑', 0xEE: 'GPT 보호',
    0x05: '확장', 0x0F: '확장 LBA', 0x85: '리눅스 확장',
}

EXTENDED = (0x05, 0x0F, 0x85)


class Partition(object):
    def __init__(self, index, start_lba, sector_count, scheme,
                 type_id='', type_name='', label='', sector_size=SECTOR):
        self.index = index
        self.start_lba = start_lba
        self.sector_count = sector_count
        self.sector_size = sector_size
        self.scheme = scheme
        self.type_id = type_id
        self.type_name = type_name
        self.label = label
        self.fs = ''                 # 부트섹터를 실제로 읽어서 채운다
        self.fs_label = ''

    @property
    def offset(self):
        return self.start_lba * self.sector_size

    @property
    def size(self):
        return self.sector_count * self.sector_size

    def __repr__(self):
        return '<Partition %d %s %s %.1fGB @%d>' % (
            self.index, self.type_name, self.fs or '?',
            self.size / 1073741824.0, self.start_lba)

    def as_dict(self):
        return {
            'index': self.index, 'scheme': self.scheme,
            'start_lba': self.start_lba, 'offset': self.offset,
            'sectors': self.sector_count, 'size': self.size,
            'type_id': self.type_id, 'type_name': self.type_name,
            'label': self.label, 'fs': self.fs, 'fs_label': self.fs_label,
        }


def _guid(b):
    """GPT GUID 는 앞 세 필드가 리틀엔디언이다. 그냥 hex 로 읽으면 틀린다."""
    return str(uuid.UUID(bytes_le=b))


def _probe_fs(reader, off, sector_size):
    """부트섹터를 실제로 읽어 파일시스템을 확인한다.

    파티션 타입 바이트는 '무엇이 들어 있을 예정인가'일 뿐이라 믿을 수 없다.
    포맷을 바꿔도 타입은 그대로인 경우가 흔하다.

    암호화 볼륨을 'NTFS 가 아님' 으로만 말하면 수사관이 원인을 못 찾는다.
    BitLocker 는 흔하게 만나는 상황이라 따로 집어 준다.
    """
    b = reader.pread(off, 512)
    if len(b) < 512:
        return '', ''
    oem = b[3:11]
    if oem == b'-FVE-FS-':
        return 'BitLocker', ''
    if oem == b'NTFS    ':
        return 'NTFS', ''
    if b[3:7] == b'ReFS':
        return 'ReFS', ''
    if oem[:5] == b'EXFAT':
        return 'exFAT', ''
    if b[82:87] == b'FAT32':
        return 'FAT32', b[71:82].decode('latin1', 'replace').strip()
    if b[54:59] in (b'FAT16', b'FAT12'):
        return b[54:59].decode('latin1'), b[43:54].decode('latin1', 'replace').strip()
    if b[:2] in (b'\xeb\x58', b'\xeb\x3c'):
        return 'FAT?', ''
    # LUKS / ext 도 가끔 만난다. 이름만이라도 정확히 말해 준다.
    if b[:6] == b'LUKS\xba\xbe':
        return 'LUKS', ''
    if reader.pread(off + 1080, 2) == b'\x53\xef':   # ext 슈퍼블록 매직
        return 'ext', ''
    return '', ''


def _read_gpt(reader, sector_size):
    hdr = reader.pread(sector_size, sector_size)
    if hdr[:8] != GPT_SIG:
        return None
    entry_lba, n_entries, entry_size = struct.unpack_from('<QII', hdr, 72)
    if not (0 < n_entries <= 512) or not (128 <= entry_size <= 4096):
        return None
    raw = reader.pread(entry_lba * sector_size, n_entries * entry_size)
    out = []
    for i in range(n_entries):
        e = raw[i * entry_size:(i + 1) * entry_size]
        if len(e) < 56 or e[:16] == b'\x00' * 16:
            continue
        tid = _guid(e[:16])
        first, last = struct.unpack_from('<QQ', e, 32)
        if last < first:
            continue
        name = e[56:128].decode('utf-16-le', 'replace').split('\x00')[0]
        out.append(Partition(
            len(out) + 1, first, last - first + 1, 'GPT',
            type_id=tid, type_name=TYPE_NAMES.get(tid, tid[:8]),
            label=name, sector_size=sector_size))
    return out


def _read_mbr(reader, sector_size):
    mbr = reader.pread(0, 512)
    if mbr[510:512] != MBR_SIG:
        return None
    out = []
    extended_at = []
    for i in range(4):
        e = mbr[446 + i * 16:446 + (i + 1) * 16]
        ptype = e[4]
        lba, cnt = struct.unpack_from('<II', e, 8)
        if ptype == 0 or cnt == 0:
            continue
        if ptype in EXTENDED:
            extended_at.append(lba)
            continue
        out.append(Partition(
            len(out) + 1, lba, cnt, 'MBR',
            type_id='0x%02X' % ptype,
            type_name=MBR_TYPE_NAMES.get(ptype, '타입 0x%02X' % ptype),
            sector_size=sector_size))

    # 확장 파티션 사슬. 논리 파티션의 LBA 는 사슬 기준 상대값이라
    # 기준점을 잘못 잡으면 엉뚱한 곳을 가리킨다.
    for base in extended_at:
        cur = base
        seen = set()
        for _ in range(64):
            if cur in seen:
                break
            seen.add(cur)
            ebr = reader.pread(cur * sector_size, 512)
            if ebr[510:512] != MBR_SIG:
                break
            e = ebr[446:462]
            ptype = e[4]
            lba, cnt = struct.unpack_from('<II', e, 8)
            if ptype and cnt:
                out.append(Partition(
                    len(out) + 1, cur + lba, cnt, 'MBR(논리)',
                    type_id='0x%02X' % ptype,
                    type_name=MBR_TYPE_NAMES.get(ptype, '타입 0x%02X' % ptype),
                    sector_size=sector_size))
            nxt = struct.unpack_from('<I', ebr, 446 + 16 + 8)[0]
            if not nxt:
                break
            cur = base + nxt
    return out


def read_partitions(reader, sector_size=None):
    """파티션 목록. 없으면 이미지 전체를 파티션 1개로 본다.

    파티션 없는 이미지(= 볼륨만 뜬 것)도 흔하다. FTK Imager 에서
    Logical Drive 로 뜨면 그렇게 된다. 그 경우도 그냥 읽혀야 한다.
    """
    if sector_size is None:
        sector_size = getattr(reader, 'sector_size', SECTOR) or SECTOR

    parts = _read_gpt(reader, sector_size)
    if parts is None:
        parts = _read_mbr(reader, sector_size)
    if not parts:
        # 파티션 테이블이 없다 = 볼륨 이미지. 0번 오프셋이 곧 부트섹터다.
        total = getattr(reader, 'size', 0) // sector_size
        p = Partition(1, 0, total, '없음(볼륨 이미지)',
                      type_name='볼륨 전체', sector_size=sector_size)
        p.fs, p.fs_label = _probe_fs(reader, 0, sector_size)
        return [p] if p.fs else []

    for p in parts:
        p.fs, p.fs_label = _probe_fs(reader, p.offset, sector_size)
    return parts


def pick_windows(parts):
    """윈도우가 들어 있을 파티션을 고른다. = 가장 큰 NTFS.

    복구 파티션도 NTFS 라 '첫 NTFS'로 고르면 틀릴 수 있다. 크기로 고르면
    실무에서 거의 틀리지 않는다. 확신할 수 없으므로 호출 쪽에서 \\Windows
    존재 여부로 한 번 더 확인한다.
    """
    ntfs = [p for p in parts if p.fs == 'NTFS']
    if not ntfs:
        return None
    ntfs.sort(key=lambda p: p.size, reverse=True)
    return ntfs[0]
