# -*- coding: utf-8 -*-
"""이미지 파서 자체 점검.

이 도구에서 실제로 버그가 났던 자리를 고정 벡터로 묶어 둔다. 전부
'값은 그럴듯한데 틀린' 부류였고, 그런 건 눈으로 봐서는 안 잡힌다.

  * 데이터런 오프셋의 부호        놓치면 디스크 뒤쪽만 읽는다
  * 세그먼트 번호 E99 -> EAA      문자열 정렬이면 순서가 뒤집힌다
  * WOF 재분석 지점의 두 겹 구조   한 겹으로 읽으면 알고리즘을 오인한다
  * $MFT 픽스업 두 번 적용        조용히 '그런 파일 없음' 이 된다
  * GPT GUID 혼합 엔디언          타입을 전부 오인한다

정답 이미지가 있어야만 확인되는 것(EWF 청크 조립·XPRESS 해제)은 여기서
못 한다. 그건 마운트한 이미지를 정답지로 두고 따로 대조했다.
"""
import struct

from . import ewf, ntfs, partition, wof
from .. import mft as mftmod


class Fail(Exception):
    pass


def _eq(name, got, want):
    if got != want:
        raise Fail('%s\n      기대 %r\n      실제 %r' % (name, want, got))


def t_runlist():
    # 80 00 00 20  = 길이 0x20, 오프셋 없음(희소)
    _eq('희소 런', ntfs.decode_runlist(bytes([0x01, 0x20, 0x00])),
        [(None, 0x20)])
    # 11 30 60 = 길이1바이트(0x30), 오프셋1바이트(0x60)
    _eq('단일 런', ntfs.decode_runlist(bytes([0x11, 0x30, 0x60])),
        [(0x60, 0x30)])
    # 이어지는 런의 오프셋은 '앞 런과의 차이'이고 부호가 있다.
    # 0xFF = -1 이므로 0x60 -> 0x5F 가 되어야 한다. 부호를 놓치면 0x15F.
    _eq('음수 델타', ntfs.decode_runlist(bytes([0x11, 0x30, 0x60,
                                             0x11, 0x10, 0xFF])),
        [(0x60, 0x30), (0x5F, 0x10)])
    # 2바이트 음수 델타 0xF000 = -4096
    _eq('2바이트 음수 델타',
        ntfs.decode_runlist(bytes([0x21, 0x10, 0x00, 0x10,
                                   0x21, 0x10, 0x00, 0xF0])),
        [(0x1000, 0x10), (0x0000, 0x10)])
    _eq('끝 표시', ntfs.decode_runlist(bytes([0x11, 0x30, 0x60, 0x00,
                                           0x11, 0x10, 0x05])),
        [(0x60, 0x30)])


def t_segment_order():
    _eq('E01', ewf._seg_key('.E01'), 1)
    _eq('E99', ewf._seg_key('.E99'), 99)
    _eq('EAA', ewf._seg_key('.EAA'), 100)
    _eq('EAB', ewf._seg_key('.EAB'), 101)
    _eq('EBA', ewf._seg_key('.EBA'), 126)
    if not ewf._seg_key('.E99') < ewf._seg_key('.EAA'):
        raise Fail('E99 다음이 EAA 여야 한다')
    _eq('확장자 아님 txt', ewf._seg_key('.txt'), None)
    _eq('L01 도 세그먼트', ewf._seg_key('.L01'), 1)
    # .log 는 두 글자 형식(L+AA~ZZ)과 겹친다. 이미지 판정에서는 걸러야 한다.
    _eq('log 는 이미지 아님', ewf._seg_key('.log', allow_alpha=False), None)
    _eq('txt 는 어느 쪽이든 아님', ewf._seg_key('.txt', allow_alpha=False), None)
    _eq('E01 은 숫자형식', ewf._seg_key('.E01', allow_alpha=False), 1)


def t_wof_reparse():
    def buf(provider, algo):
        return (struct.pack('<IHH', wof.IO_REPARSE_TAG_WOF, 16, 0)
                + struct.pack('<II', 1, provider)
                + struct.pack('<II', 1, algo))
    _eq('XPRESS4K', wof.parse_reparse(buf(2, 0)), (0, 'XPRESS4K'))
    _eq('LZX', wof.parse_reparse(buf(2, 1)), (1, 'LZX'))
    _eq('XPRESS8K', wof.parse_reparse(buf(2, 2)), (2, 'XPRESS8K'))
    _eq('XPRESS16K', wof.parse_reparse(buf(2, 3)), (3, 'XPRESS16K'))
    got = wof.parse_reparse(buf(1, 0))
    if not got or got[0] != 'WIM외부':
        raise Fail('WIM 공급자를 구분해야 한다: %r' % (got,))
    # 한 겹으로 읽으면 오프셋 16(=FILE_PROVIDER 버전, 항상 1)을 알고리즘으로
    # 착각해 전부 LZX 로 보인다. 그 회귀를 여기서 막는다.
    if wof.parse_reparse(buf(2, 2))[0] == 1:
        raise Fail('알고리즘 필드를 오프셋 20 에서 읽어야 한다')
    _eq('WOF 아님', wof.parse_reparse(struct.pack('<IHH', 0xA000000C, 0, 0)),
        None)
    _eq('너무 짧음', wof.parse_reparse(b'\x17\x00\x00\x80'), None)


def t_huffman():
    # 심볼 0,1 은 1비트, 2,3 은 2비트... 가 되도록 정규 허프만을 만든다.
    lens = [1, 2, 3, 3] + [0] * 508
    tbl = wof._build_table(lens, 15)
    # 0 -> '0', 1 -> '10', 2 -> '110', 3 -> '111'
    for bits, sym, ln in (('0' + '0' * 14, 0, 1), ('10' + '0' * 13, 1, 2),
                          ('110' + '0' * 12, 2, 3), ('111' + '0' * 12, 3, 3)):
        v = tbl[int(bits, 2)]
        _eq('허프만 %s' % bits, (v >> 5, v & 31), (sym, ln))


def t_lznt1():
    # 압축 안 된 블록: 머리 2바이트의 최상위 비트가 0 이면 그대로 복사.
    body = b'ChatTrace'
    hdr = struct.pack('<H', (len(body) - 1) & 0x0FFF)
    _eq('LZNT1 비압축 블록', ntfs.lznt1_decompress(hdr + body), body)
    _eq('LZNT1 빈 입력', ntfs.lznt1_decompress(b''), b'')
    _eq('LZNT1 종료 표시', ntfs.lznt1_decompress(b'\x00\x00' + b'garbage'), b'')


def t_guid():
    # GPT 는 앞 세 필드가 리틀엔디언이다. 그냥 hex 로 읽으면 전부 틀린다.
    raw = bytes.fromhex('28732ac11ff8d211ba4b00a0c93ec93b')
    _eq('EFI 시스템 GUID', partition._guid(raw),
        'c12a7328-f81f-11d2-ba4b-00a0c93ec93b')
    _eq('이름 조회', partition.TYPE_NAMES[partition._guid(raw)], 'EFI 시스템')


def t_fixup():
    """픽스업을 두 번 걸면 안 된다. 실제로 이것 때문에 파티션 판정이 틀렸다."""
    rec = bytearray(1024)
    rec[0:4] = b'FILE'
    struct.pack_into('<HH', rec, 4, 48, 3)      # USA 오프셋 48, 항목 3개
    usn = b'\x11\x22'
    rec[48:50] = usn
    rec[50:52] = b'\xAA\xBB'                    # 섹터 0 원래 마지막 2바이트
    rec[52:54] = b'\xCC\xDD'                    # 섹터 1
    rec[510:512] = usn
    rec[1022:1024] = usn
    once = mftmod._apply_fixup(bytes(rec))
    if once is None:
        raise Fail('정상 레코드의 픽스업이 실패했다')
    _eq('섹터0 복원', once[510:512], b'\xAA\xBB')
    _eq('섹터1 복원', once[1022:1024], b'\xCC\xDD')
    # 복원된 것에 또 걸면 서명이 안 맞아 None 이 나와야 한다.
    _eq('두 번 적용은 거부', mftmod._apply_fixup(once), None)


def t_paths():
    from .extract import _safe_rel
    _eq('상대경로', _safe_rel('Users\\Kwon\\a.txt'), 'Users\\Kwon\\a.txt')
    _eq('상위 탈출 차단', _safe_rel('..\\..\\Windows\\x'), 'Windows\\x')
    _eq('슬래시 혼용', _safe_rel('Users/Kwon/a'), 'Users\\Kwon\\a')
    _eq('빈 경로', _safe_rel('\\\\'), '')


def t_ewf_unescape():
    # FTK Imager 는 비ASCII 를 [U+AD8C] 꼴로 바꿔 넣는다.
    _eq('이스케이프 복원', ewf._unescape('[U+AD8C][U+CC2C][U+ACB0]'), '권찬결')
    _eq('건드리지 않음', ewf._unescape('B01-2026-001'), 'B01-2026-001')


TESTS = [
    ('데이터런 부호·희소', t_runlist),
    ('세그먼트 번호 순서', t_segment_order),
    ('WOF 재분석 두 겹 구조', t_wof_reparse),
    ('정규 허프만 조회표', t_huffman),
    ('LZNT1 블록 틀', t_lznt1),
    ('GPT GUID 혼합 엔디언', t_guid),
    ('$MFT 픽스업 중복 방지', t_fixup),
    ('출력 경로 정규화', t_paths),
    ('EWF 사건정보 복원', t_ewf_unescape),
]


def run(out=None):
    """-> 실패 개수"""
    import sys
    w = (out or sys.stdout).write
    bad = 0
    w('\n  이미지 파서 자체 점검\n')
    w('  ' + '-' * 54 + '\n')
    for name, fn in TESTS:
        try:
            fn()
            w('   통과   %s\n' % name)
        except Fail as e:
            bad += 1
            w('   실패   %s\n      %s\n' % (name, e))
        except Exception as e:
            bad += 1
            w('   오류   %s\n      %s: %s\n' % (name, type(e).__name__, e))
    w('  ' + '-' * 54 + '\n')
    w('  %d개 중 %d개 통과\n\n' % (len(TESTS), len(TESTS) - bad))
    return bad
