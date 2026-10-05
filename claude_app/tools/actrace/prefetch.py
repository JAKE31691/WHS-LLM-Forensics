# -*- coding: utf-8 -*-
r"""Prefetch(.pf) 파서 — "이 PC 에서 실행했나" 의 1차 증거.

    C:\Windows\Prefetch\CLAUDE.EXE-0659240F.pf

윈도우 10 부터 파일이 MAM 압축(XPRESS Huffman)돼 있다. 그 해제기는
ChatTrace 의 image 층에 이미 있다 — WOF(Compact OS) 때문에 만들어 둔 것을
그대로 쓴다. 다만 MAM 은 LZ 창이 덩어리 경계를 넘어 이어져서, WOF 처럼
덩어리마다 독립으로 풀면 실패한다(실측 285개 중 114개).

구조는 추측하지 않고 실측으로 확정했다.

    +0x000  버전            31 = 윈도우 11, 30 = 윈도우 10
    +0x004  'SCCA'
    +0x010  실행파일 이름     UTF-16LE 60바이트
    +0x04C  경로 해시        파일명 뒤에 붙는 8자리와 같다
    +0x064  파일이름 목록 오프셋 / +0x068 크기
    +0x06C  볼륨정보 오프셋   / +0x070 개수
    +0x080  마지막 실행 시각   FILETIME 8개, 최신순
    +0x0C8  실행 횟수        ★ 284개 전부 조건 만족, 233개는 정확히 일치

실행 횟수 오프셋은 후보 8곳을 놓고 '찍힌 시각 개수 이상이어야 한다' 는
조건으로 걸러서 정했다. 0xCC·0xD0 은 각각 129/284, 73/284 로 탈락했다.
"""
import datetime
import os
import struct

from chattrace.image import wof

from .schema import Observation

SIG = b'SCCA'
_EPOCH = datetime.datetime(1601, 1, 1)
# 실행 시각으로 인정할 범위. 밖이면 빈 칸이거나 쓰레기다.
_LO = int((datetime.datetime(2000, 1, 1) - _EPOCH).total_seconds() * 10000000)
_HI = int((datetime.datetime(2100, 1, 1) - _EPOCH).total_seconds() * 10000000)

OFF_RUNTIMES = 0x80
OFF_RUNCOUNT = 0xC8
N_RUNTIMES = 8


def filetime(v):
    if not (_LO < v < _HI):
        return ''
    dt = _EPOCH + datetime.timedelta(microseconds=v // 10)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % ((v // 10000) % 1000)


class PrefetchError(Exception):
    pass


def decode(raw):
    """압축을 풀어 SCCA 본문을 돌려준다."""
    if raw[:3] == b'MAM':
        return wof.mam_decompress(raw)
    if raw[4:8] == SIG:
        return raw
    raise PrefetchError('MAM 도 SCCA 도 아니다')


def parse_one(raw):
    """-> dict. 실패하면 PrefetchError."""
    d = decode(raw)
    if len(d) < 0x100 or d[4:8] != SIG:
        raise PrefetchError('SCCA 서명이 없다')
    ver = struct.unpack_from('<I', d, 0)[0]
    if ver not in (17, 23, 26, 30, 31):
        raise PrefetchError('모르는 버전 %d' % ver)
    if ver < 30:
        # 옛 버전은 배치가 다르다. 틀린 값을 내느니 안 낸다.
        raise PrefetchError('버전 %d 는 아직 지원하지 않는다' % ver)

    name = d[0x10:0x4C].decode('utf-16-le', 'replace').split('\x00')[0]
    phash = struct.unpack_from('<I', d, 0x4C)[0]
    runs = []
    for k in range(N_RUNTIMES):
        v = struct.unpack_from('<Q', d, OFF_RUNTIMES + k * 8)[0]
        t = filetime(v)
        if t:
            runs.append(t)
    count = struct.unpack_from('<I', d, OFF_RUNCOUNT)[0]

    # 실행파일의 전체 경로를 파일이름 목록에서 찾는다
    full = ''
    files = []
    try:
        soff, ssize = struct.unpack_from('<II', d, 0x64)
        if 0 < soff < len(d) and 0 < ssize <= len(d) - soff:
            blob = d[soff:soff + ssize].decode('utf-16-le', 'replace')
            files = [s for s in blob.split('\x00') if s]
            up = name.upper()
            for s in files:
                if s.upper().endswith('\\' + up):
                    full = s
                    break
    except struct.error:
        pass

    return {'version': ver, 'name': name, 'hash': '%08X' % phash,
            'run_count': count, 'run_times': sorted(runs, reverse=True),
            'full_path': full, 'file_count': len(files), 'files': files}


def find(root):
    out = []
    for dp, dn, fns in os.walk(root):
        if 'prefetch' not in dp.lower():
            continue
        for f in fns:
            if f.lower().endswith('.pf'):
                out.append(os.path.join(dp, f))
    return out


def load_manifest_times(root):
    r"""추출물의 manifest.csv 에서 .pf 파일의 원본 시각을 읽는다.

    왜 필요한가 — 실행 횟수를 '전체' 로 볼 수 있으려면 카운터가 처음부터
    센 것이어야 한다. .pf 가 지워졌다 다시 만들어지면 0부터 다시 센다.
    판별은 .pf 의 생성 시각으로 한다. 설치 직후면 처음부터, 한참 뒤면
    '그 시점 이후의 횟수' 일 뿐이다.

    꺼낸 파일에는 원본 타임스탬프가 안 붙으므로 manifest 를 봐야 한다.
    -> {소문자 파일명: (생성, 수정)}
    """
    import csv
    p = os.path.join(root, 'manifest.csv')
    out = {}
    if not os.path.exists(p):
        return out
    try:
        with open(p, encoding='utf-8-sig', newline='') as f:
            for r in csv.DictReader(f):
                path = r.get('path') or ''
                if not path.lower().endswith('.pf'):
                    continue
                out[os.path.basename(path).lower()] = (
                    (r.get('si_created') or '').strip(),
                    (r.get('si_modified') or '').strip())
    except (OSError, csv.Error):
        pass
    return out


def _norm(ts):
    """'2026-09-19 02:04:24.193841' -> '2026-09-19T02:04:24.193Z'"""
    if not ts:
        return ''
    s = ts.replace(' ', 'T')
    if '.' in s:
        head, frac = s.split('.', 1)
        s = head + '.' + (frac + '000')[:3]
    return s + 'Z'


CLAUDE_HINTS = ('CLAUDE', 'ANTHROPIC')


def parse(root, ids, log=None, env_profile='', only_claude=True):
    """-> [Observation]

    only_claude=False 면 전부 낸다. 기본은 Claude 관련만 — 285개를 다
    내면 관찰이 수천 건이 되고 정작 볼 것이 묻힌다.
    """
    say = log or (lambda *a: None)
    obs = []
    pfs = find(root)
    if not pfs:
        say('   Prefetch 를 찾지 못했다')
        return obs
    mtimes = load_manifest_times(root)
    hit = bad = 0
    for p in sorted(pfs):
        base = os.path.basename(p)
        if only_claude and not any(h in base.upper() for h in CLAUDE_HINTS):
            continue
        rel = os.path.relpath(p, root)
        try:
            info = parse_one(open(p, 'rb').read())
        except Exception as e:
            bad += 1
            obs.append(Observation(
                ids.next('obs'), 'prefetch', rel, base, 'prefetch_unreadable',
                {'file': base}, status='failed',
                note='%s: %s' % (type(e).__name__, e), env_profile=env_profile))
            continue
        hit += 1
        created, modified = mtimes.get(base.lower(), ('', ''))
        # 실행 기록 전체를 한 건으로
        obs.append(Observation(
            ids.next('obs'), 'prefetch', rel, base, 'program_run_summary',
            {'name': info['name'], 'hash': info['hash'],
             'run_count': info['run_count'],
             'recorded_runs': len(info['run_times']),
             'unrecorded_runs': max(0, info['run_count']
                                    - len(info['run_times'])),
             'full_path': info['full_path'],
             'loaded_files': info['file_count'],
             'run_times': info['run_times']},
            ts={'ts_run_last': info['run_times'][0] if info['run_times'] else '',
                'ts_run_first_recorded': (info['run_times'][-1]
                                          if info['run_times'] else ''),
                'ts_pf_created': _norm(created),
                'ts_pf_modified': _norm(modified)},
            env_profile=env_profile))
        # 실행 시각 하나하나도 따로 — 타임라인에 박으려면 개별이어야 한다
        for t in info['run_times']:
            obs.append(Observation(
                ids.next('obs'), 'prefetch', rel, base + ' 실행시각',
                'program_run', {'name': info['name'], 'hash': info['hash'],
                                'full_path': info['full_path']},
                ts={'ts_run': t}, env_profile=env_profile))
    say('   Prefetch %s개 중 대상 %d개%s'
        % (format(len(pfs), ','), hit, (' · 해석 실패 %d' % bad) if bad else ''))
    return obs
