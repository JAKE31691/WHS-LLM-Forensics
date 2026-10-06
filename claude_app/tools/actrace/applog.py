# -*- coding: utf-8 -*-
r"""앱 로그 파서 — 앱이 언제 뜨고 졌나.

    %LocalAppData%\Claude\logs\main.log

★ 이 경로는 MSIX 컨테이너 **밖**이다. 앱 자체는
  %LocalAppData%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude
  에 있지만 로그만 바깥에 남는다. 컨테이너 안만 뒤지면 못 찾는다.

로그 줄은 대개 이런 꼴이다.

    2026-09-20 17:12:16.370 > [info] main: ...

시각에 타임존이 안 붙는다. 로컬 시각이므로 UTC 로 바꾸려면 기기 타임존을
알아야 하는데, 그 근거가 이 파일에 없다. 그래서 **바꾸지 않고 로컬인 채로
두고 tz_source 에 'local(unknown offset)' 을 적는다.** 모르는 것을 아는 척
변환하면 타임라인이 통째로 밀린다.
"""
import io
import os
import re

from .schema import Observation

# 밀리초는 있을 수도 없을 수도 있다. 실측 로그는 '2026-09-19 11:04:19
# [info] Starting app' 처럼 초까지만 찍는다. 밀리초를 필수로 두면
# 8,648줄에서 0줄이 잡힌다(실제로 그랬다).
LINE = re.compile(
    r'^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:[.,](\d{1,6}))?\s*'
    r'(?:>\s*)?\[(\w+)\]\s*(.*)$')

# 무엇을 행위로 볼 것인가. 실측 로그에 실제로 나오는 문구만 적는다.
# 추측한 문구를 넣으면 조용히 0건이 되고, 0건은 '행위가 없었다' 로
# 잘못 읽힌다.
INTEREST = (
    ('app_start', re.compile(r'^Starting app\b', re.I)),
    ('page_load', re.compile(r'^Loaded https://claude\.ai', re.I)),
    ('account', re.compile(r'account details provided', re.I)),
    ('session_load', re.compile(r'Loaded \d+ persisted sessions', re.I)),
    ('oauth_fail', re.compile(r'oauth failed', re.I)),
    ('config_write', re.compile(r'^Config file written', re.I)),
    ('mcp_config', re.compile(r'claude_desktop_config\.json', re.I)),
    ('crash', re.compile(r'process gone|crashed', re.I)),
)

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'

# ★ 우리 분석이 2위로 꼽은 이유가 여기 있다.
#   로그인/로그아웃 시각에 **계정 UUID 가 평문으로** 붙는다. 그리고
#   preserving / clearing 이 '동일 계정 재로그인' 과 '계정 전환' 을 가른다.
#   쿠키가 지워진 기기에서도 마지막 사용 계정이 특정된다.
#
#   Login-state transition (loggedOut: true → false,
#       uuid: 7612a620-… → 88c48471-…), clearing oauth cache
TRANSITION = re.compile(
    r'Login-state transition\s*\(\s*loggedOut:\s*(\w+)\s*(?:→|->)\s*(\w+)\s*,\s*'
    r'uuid:\s*(<none>|' + UUID + r')\s*(?:→|->)\s*(<none>|' + UUID + r')\s*\)'
    r'\s*,?\s*(preserving|clearing)?', re.I)

# [LocalAgentModeSessionManager] Initialization succeeded — accountId=…, orgId=…
ACCT_ORG = re.compile(r'accountId=(' + UUID + r'|null)\s*,\s*orgId=('
                      + UUID + r'|null)', re.I)

FIRST_RUN = re.compile(r'is_first_run:\s*(true|false)', re.I)


def find(root):
    out = []
    for dp, dn, fns in os.walk(root):
        if os.path.basename(dp).lower() != 'logs':
            continue
        if 'leveldb' in dp.lower():
            continue
        for f in fns:
            if f.lower().endswith('.log'):
                out.append(os.path.join(dp, f))
    return out


def parse(root, ids, log=None, env_profile='', max_lines=400000):
    say = log or (lambda *a: None)
    obs = []
    logs = find(root)
    if not logs:
        say('   앱 로그를 찾지 못했다')
        return obs
    total = kept = 0
    for p in sorted(logs):
        rel = os.path.relpath(p, root)
        try:
            fh = io.open(p, encoding='utf-8', errors='replace')
        except OSError:
            continue
        with fh:
            for ln, line in enumerate(fh, 1):
                if ln > max_lines:
                    break
                total += 1
                m = LINE.match(line.strip())
                if not m:
                    continue
                day, clock, msec, level, msg = m.groups()
                msec = msec or '000'
                when = {'ts_log_local': '%sT%s.%s' % (day, clock, msec)}
                loc = '%s:%d' % (os.path.basename(p), ln)

                def add(ft, fact, actor=''):
                    obs.append(Observation(
                        ids.next('obs'), 'applog', rel, loc, ft, fact,
                        ts=when, tz_source='local(unknown offset)',
                        actor=actor, env_profile=env_profile))

                # ── 로그인 상태 전이 — 계정 UUID 가 평문으로 나온다 ──
                t = TRANSITION.search(msg)
                if t:
                    was, now, u_from, u_to, cache = t.groups()
                    logged_out = (now or '').lower() == 'true'
                    u_from = '' if (u_from or '').lower() == '<none>' else u_from
                    u_to = '' if (u_to or '').lower() == '<none>' else u_to
                    kept += 1
                    add('app_log_login_transition', {
                        'logged_out_before': (was or '').lower() == 'true',
                        'logged_out_after': logged_out,
                        'uuid_from': u_from, 'uuid_to': u_to,
                        'oauth_cache': (cache or '').lower(),
                        'same_account': bool(u_from and u_to and u_from == u_to),
                        'message': msg[:200]},
                        actor=u_to or u_from)
                    continue

                ao = ACCT_ORG.search(msg)
                if ao:
                    acct, org = ao.groups()
                    kept += 1
                    add('app_log_account_org',
                        {'account': '' if acct.lower() == 'null' else acct,
                         'org': '' if org.lower() == 'null' else org,
                         'message': msg[:160]},
                        actor='' if acct.lower() == 'null' else acct)
                    continue

                fr = FIRST_RUN.search(msg)
                if fr:
                    kept += 1
                    add('app_log_first_run',
                        {'is_first_run': fr.group(1).lower() == 'true',
                         'message': msg[:160]})
                    continue

                kind = None
                for k, rx in INTEREST:
                    if rx.search(msg):
                        kind = k
                        break
                if kind is None:
                    continue
                kept += 1
                add('app_log_' + kind, {'level': level, 'message': msg[:200]})
    say('   앱 로그 %d개 · %s줄 중 %d줄 채택'
        % (len(logs), format(total, ','), kept))
    return obs
