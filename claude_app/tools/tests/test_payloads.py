# -*- coding: utf-8 -*-
r"""외부 블롭·snappy 경로를 통제된 입력으로 검증한다.

왜 이 파일이 필요한가
---------------------
실수집물(B01/T7)은 무료 계정 기준선이라 업로드가 하나도 없다. 그래서
외부 블롭을 풀어야만 보이는 것이 입력에 아예 없고, 도구를 돌려도
"안 나왔다"가 '기능이 죽었다'인지 '찾을 게 없었다'인지 갈리지 않는다.

그래서 **답을 아는 입력**을 만든다.

  양성 통제  표식을 외부 블롭 '안에만' 넣는다.
             못 찾으면 기능이 죽은 것이다 — 변명의 여지가 없다.
  음성 통제  어디에도 넣지 않은 문자열을 찾게 한다 (공통 명세 §9).
             나오면 오탐이다.

이 파일은 계정도 VM 도 수집물도 필요 없다. 어디서나 돈다.

    python tests\test_payloads.py
"""
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from chattrace import codec                                    # noqa: E402
from chattrace.scenarios import BlobIndex                      # noqa: E402
from actrace.content import _payloads, LOCALPATH_RX, FILENAME_RX  # noqa: E402
from actrace.app import _now_iso                               # noqa: E402

# 표식. 양성은 블롭 안에만, 음성은 어디에도 넣지 않는다.
CONTROL = rb'C:\TestData\CONTROL-A.pdf'
NEVER = rb'NEVER-0000'

_fail = []


def check(cond, label, detail=''):
    if cond:
        print('  [OK]   %s' % label)
    else:
        print('  [실패] %s' % label)
        if detail:
            print('         %s' % detail)
        _fail.append(label)


# ── 실측 형식대로 값을 만든다 (codec.py 주석의 래퍼) ──────────────
#   FF 11 01 <크기 varint> <블롭번호 varint>   외부 블롭
#   FF 11 02 <snappy 스트림>                   내부 + snappy

def uvarint_enc(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def idb_external(size, num=3):
    """ObjectStoreData 값 = <버전 varint> + SSV 블롭."""
    return b'\x01' + b'\xFF\x11\x01' + uvarint_enc(size) + uvarint_enc(num)


def snappy_literal(data):
    """리터럴만 쓰는 최소 snappy 스트림. 청크당 60바이트."""
    out = bytearray(uvarint_enc(len(data)))
    for i in range(0, len(data), 60):
        chunk = data[i:i + 60]
        out.append(((len(chunk) - 1) << 2) | 0)
        out += chunk
    return bytes(out)


def idb_snappy(data):
    return b'\x01' + b'\xFF\x11\x02' + snappy_literal(data)


def upload_record(marker):
    """R1 이 실측한 업로드 레코드 모양. 표식을 localPath 에 넣는다."""
    return (b'\x00\x06file_kind\x00blob\x00\x09file_name"CONTROL-A.pdf"'
            b'\x00\x0bsize_bytes\x00\x09localPath"' + marker + b'"\x00')


def main():
    root = tempfile.mkdtemp(prefix='acttrace-control-')
    try:
        # 진짜 수집물과 같은 자리에 블롭을 놓는다. root 바로 밑이 아니라
        # app\IndexedDB\... 아래다 — 이게 지난 버그의 핵심이었다.
        blobdir = os.path.join(root, 'app', 'IndexedDB',
                               'https_claude.ai_0.indexeddb.blob', '1', '00')
        os.makedirs(blobdir)
        blobfile = os.path.join(blobdir, '03')
        body = upload_record(CONTROL)
        body += b'\x00' * (4096 - len(body))          # 크기를 4096 으로 고정
        with open(blobfile, 'wb') as f:
            f.write(body)

        # 크기가 같은 미끼. 정답이 아닌 쪽을 고르면 안 된다.
        decoydir = os.path.join(root, 'Windows', 'System32')
        os.makedirs(decoydir)
        with open(os.path.join(decoydir, 'decoy.dat'), 'wb') as f:
            f.write(b'X' * 4096)

        val = idb_external(4096, 3)

        print('')
        print('  통제 입력 검증 — 외부 블롭 · snappy')
        print('  ' + '-' * 58)

        # ── 1. 양성 통제: 외부 블롭 ────────────────────────────
        blobs = BlobIndex(extra_dirs=[root])
        bufs = _payloads(val, blobs)
        joined = b''.join(b for b, _ in bufs)
        check(len(bufs) == 2,
              '외부 블롭을 풀어 버퍼가 늘었다 (%d개)' % len(bufs))
        check(CONTROL in joined,
              '양성 통제 — 블롭 안의 표식을 찾았다',
              '찾은 버퍼 %d개, 표식 없음' % len(bufs))

        # 실제 파싱 경로(정규식)까지 통과하는지 본다
        hits = [m.group(1) for b, _ in bufs for m in LOCALPATH_RX.finditer(b)]
        check(any(h == CONTROL for h in hits),
              'localPath 정규식이 원본 경로를 뽑았다',
              '뽑힌 것: %r' % hits)
        names = [m.group(1) for b, _ in bufs for m in FILENAME_RX.finditer(b)]
        check(any(b'CONTROL-A.pdf' in n for n in names),
              'file_name 도 같이 뽑혔다', '뽑힌 것: %r' % names[:3])

        # 미끼가 아니라 진짜 블롭을 골랐나
        src = [s for _, s in bufs if s]
        check(any('03' in s for s in src),
              '크기가 같은 미끼 대신 블롭 저장소를 골랐다',
              '고른 출처: %r' % src)

        # ── 2. 회귀 방지: 옛 호출은 못 찾아야 한다 ──────────────
        # BlobIndex(root) 는 root\IndexedDB 한 곳만 본다. 수집물은
        # root\app\IndexedDB 라 색인이 비고, 32건이 조용히 버려졌다.
        old = BlobIndex(root)
        old_bufs = _payloads(val, old)
        check(len(old_bufs) == 1,
              '회귀 방지 — 옛 호출 BlobIndex(root) 로는 못 찾는다',
              '옛 호출로도 %d개가 나왔다. 이 테스트가 무의미해졌다'
              % len(old_bufs))

        # ── 3. 양성 통제: snappy ──────────────────────────────
        sval = idb_snappy(upload_record(CONTROL))
        sbufs = _payloads(sval, blobs)
        sjoin = b''.join(b for b, _ in sbufs)
        check(len(sbufs) == 2 and CONTROL in sjoin,
              '양성 통제 — snappy 압축을 풀고 표식을 찾았다',
              '버퍼 %d개' % len(sbufs))

        # ── 4. 음성 통제 (공통 명세 §9) ────────────────────────
        allbytes = joined + sjoin
        check(NEVER not in allbytes,
              '음성 통제 — 넣지 않은 문자열은 안 나온다')

        # ── 5. 포장 안 된 값은 그대로 ──────────────────────────
        plain = b'\x01\xFF\x15\xFE' + upload_record(CONTROL)
        pbufs = _payloads(plain, blobs)
        check(len(pbufs) == 1 and CONTROL in pbufs[0][0],
              '포장 안 된 값은 원본 그대로 넘어간다 (기존 동작 유지)')

        # ── 6. 시각 형식 (공통 명세 §6 ②: 밀리초까지) ──────────
        ts = _now_iso()
        ok = re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$', ts)
        check(bool(ok), '시각이 밀리초까지 나온다 (%s)' % ts,
              '명세 §6 ② 위반. %f 가 빠지면 ".Z" 가 된다')

        print('  ' + '-' * 58)
        if _fail:
            print('  실패 %d건' % len(_fail))
            for f in _fail:
                print('    - %s' % f)
        else:
            print('  전부 통과')
        print('')
        return 1 if _fail else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
