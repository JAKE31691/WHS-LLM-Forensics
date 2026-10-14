# -*- coding: utf-8 -*-
r"""설치 순서 확정 — 우리 분석 6위의 고유 발견.

    "설치 4단계가 $MFT 엔트리 번호 오름차순으로 확정된다:
     패키지 등록(231834) → 프로그램 본체(231837)
     → 사용자 컨테이너(236382) → 앱 저장소(236466)"

왜 엔트리 번호인가. 시각만 보면 네 단계가 같은 초에 몰려 순서를 못 정한다.
NTFS 는 레코드를 번호 순으로 할당하므로, 번호가 작을수록 먼저 만들어졌다.
시각이 동률이어도 순서가 확정된다.

★ 이 순서가 깨지면 그 자체가 소견이다. 예를 들어 사용자 컨테이너가
  프로그램 본체보다 번호가 작으면, 전에 설치했다 지운 잔재가 있다는 뜻이다.

AppRepository 매니페스트도 함께 본다 — 파일명 자체가 버전이다.
"""
import os
import re

from chattrace import mft as mftmod

from .schema import Observation

# (단계 이름, 경로 정규식). 순서는 '정상이라면 이래야 한다' 는 기대값이다.
#
# ★ '패키지 등록' 은 AppRepository 다. ProgramData\Packages 가 아니다.
#   우리 분석은 231834 → 231837 → 236382 → 236466 이라고 적었는데,
#   ProgramData\Packages 로 잡으면 236390 이 나와 순서가 뒤집힌다.
#   AppRepository 로 잡아야 231834 자리가 맞는다.
#   ProgramData\Packages 는 그보다 한참 뒤에 만들어지는 별개 단계라
#   참고용으로만 둔다.
STAGES = (
    ('패키지 등록', r'^programdata\\microsoft\\windows\\apprepository'
                r'\\packages\\claude_[^\\]+$'),
    ('프로그램 본체', r'^program files\\windowsapps\\claude_[^\\]+$'),
    ('사용자 컨테이너', r'^users\\[^\\]+\\appdata\\local\\packages\\claude_[^\\]+$'),
    ('앱 저장소',
     r'^users\\[^\\]+\\appdata\\local\\packages\\claude_[^\\]+'
     r'\\localcache\\roaming\\claude$'),
)

# 순서 판정에는 안 쓰지만 있으면 기록해 둔다
EXTRA_STAGES = (
    ('ProgramData 패키지', r'^programdata\\packages\\claude_[^\\]+$'),
)

MANIFEST_RX = re.compile(
    r'^programdata\\microsoft\\windows\\apprepository\\'
    r'(claude_([0-9.]+)_[^\\]+)\.xml$', re.I)


def find_mft(root):
    for dp, dn, fns in os.walk(root):
        for f in fns:
            if f == '$MFT':
                return os.path.join(dp, f)
    return None


def parse(root, ids, log=None, env_profile=''):
    say = log or (lambda *a: None)
    obs = []

    # ── AppRepository 매니페스트 — 파일명이 곧 버전 ──────────
    for dp, dn, fns in os.walk(root):
        for f in fns:
            rel = os.path.relpath(os.path.join(dp, f), root)
            m = MANIFEST_RX.match(rel.replace('/', '\\').lower())
            if not m:
                continue
            obs.append(Observation(
                ids.next('obs'), 'apprepository', rel, f,
                'msix_manifest',
                {'package': m.group(1), 'version': m.group(2)},
                env_profile=env_profile,
                note='파일명 자체가 버전이다 (우리 분석 6위)'))

    # ── $MFT 엔트리 번호로 설치 순서 ────────────────────────
    mpath = find_mft(root)
    if not mpath:
        say('   $MFT 가 없다 — 설치 순서는 판정 불가')
        obs.append(Observation(
            ids.next('obs'), 'mft', '', '$MFT', 'install_order_absent',
            {}, status='partial', note='$MFT 가 수집물에 없다',
            env_profile=env_profile))
        return obs

    rx = [(name, re.compile(pat)) for name, pat in STAGES + EXTRA_STAGES]

    def keep(low):
        return any(r.match(low) for _n, r in rx)

    try:
        dmap = mftmod.build_dir_map(mpath)
        recs = mftmod.scan(mpath, dmap=dmap, path_filter=keep)
    except Exception as e:
        say('   $MFT 해석 실패 — %s' % e)
        return obs

    for r in recs:
        low = r['path'].lower()
        stage = ''
        for name, cre in rx:
            if cre.match(low):
                stage = name
                break
        obs.append(Observation(
            ids.next('obs'), 'mft', r['path'], 'entry=%d' % r['entry'],
            'install_stage',
            {'stage': stage, 'entry': r['entry'], 'seq': r['seq'],
             'is_dir': r['is_dir'], 'in_use': r['in_use'],
             'path_status': r.get('path_status', '')},
            ts={'ts_mft_si_created': mftmod.filetime_iso(r['si_created']),
                'ts_mft_si_modified': mftmod.filetime_iso(r['si_modified'])},
            env_profile=env_profile))
    say('   설치 단계 %d개 (엔트리 번호로 순서 확정)'
        % sum(1 for o in obs if o.fact_type == 'install_stage'))
    return obs
