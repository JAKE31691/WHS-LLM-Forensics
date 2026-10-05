# -*- coding: utf-8 -*-
r"""Q2 파서 — "누구 계정으로 썼나".

세 갈래를 본다.

    plan-usage-history.json        457 B 평문. 조직 UUID 가 바뀌는 지점이
                                   곧 계정 전환 시각이다 (실측 6/6 대응)
    계정 식별자 디렉터리 네 갈래       삭제 층위를 모두 견딘다
        local-agent-mode-sessions\<계정>\<조직>\
        claude-code-sessions\<계정>\<조직>\
        skills-plugin\<조직>\<계정>\        ← 순서가 반대다
        spaces-present\<계정앞8>\<조직앞8>\link-key
    Network\Cookies                sessionKey 의 존부가 로그인 상태다

★ 토큰 값은 기록하지 않는다.
  쿠키 이름과 위치·시각만 남기고 값은 손대지 않는다. 보고서가 그대로
  자격증명이 되면 안 된다.

★ 식별자는 삭제를 견딘다.
  선행 연구 다섯 회차 전부에서 로그아웃·계정전환·계정삭제 세 층위를
  모두 넘겨 계정 UUID 가 남았다. 그래서 '내용이 없다' 와 '계정이 없었다'
  는 다른 말이다.
"""
import io
import json
import os
import re

from chattrace import sqlitefs

from .schema import Observation, utc_ms

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'

# (폴더명, 첫 칸이 계정인가)
ID_DIRS = (
    ('local-agent-mode-sessions', True),
    ('claude-code-sessions', True),
    ('skills-plugin', False),          # 조직이 먼저다
)

# 로그인 상태를 말해 주는 쿠키
SESSION_COOKIES = ('sessionkey', 'sessionkeylc',
                   'sessionkeyv3', 'sessionkeyv3lc')
ORG_COOKIES = ('lastactiveorg',)

# 크로미움 시각 = 1601 기준 마이크로초
_CHROME_EPOCH_MS = 11644473600000


def chrome_ms(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return ''
    if v <= 0:
        return ''
    return utc_ms(v // 1000 - _CHROME_EPOCH_MS)


def _find(root, name):
    out = []
    for dp, dn, fns in os.walk(root):
        if os.path.basename(dp).lower() == name.lower():
            out.append(dp)
        for f in fns:
            if f.lower() == name.lower():
                out.append(os.path.join(dp, f))
    return out


def parse(root, ids, log=None, env_profile=''):
    say = log or (lambda *a: None)
    obs = []
    obs += _plan_usage(root, ids, say, env_profile)
    obs += _id_dirs(root, ids, say, env_profile)
    obs += _cookies(root, ids, say, env_profile)
    return obs


# ── plan-usage-history.json ─────────────────────────────────────
def _plan_usage(root, ids, say, env):
    obs = []
    files = _find(root, 'plan-usage-history.json')
    for p in files:
        rel = os.path.relpath(p, root)
        try:
            o = json.load(io.open(p, encoding='utf-8'))
        except Exception as e:
            obs.append(Observation(ids.next('obs'), 'plan_usage', rel,
                                   'plan-usage-history.json', 'plan_sample',
                                   {}, status='failed', note=str(e),
                                   env_profile=env))
            continue
        for k, s in enumerate(o.get('samples') or []):
            if not isinstance(s, dict):
                continue
            obs.append(Observation(
                ids.next('obs'), 'plan_usage', rel,
                'samples[%d]' % k, 'plan_sample',
                {'org': s.get('org') or '', 'usage': s.get('u') or {}},
                ts={'ts_sample': utc_ms(s.get('t'))}, env_profile=env))
    if files:
        say('   plan-usage-history %d개 · 표본 %d건'
            % (len(files), sum(1 for o in obs if o.fact_type == 'plan_sample')))
    return obs


# ── 계정/조직 식별자 디렉터리 ───────────────────────────────────
def _id_dirs(root, ids, say, env):
    obs = []
    rx = re.compile(UUID, re.I)
    found = 0
    for dirname, acct_first in ID_DIRS:
        for base in _find(root, dirname):
            for a in sorted(os.listdir(base)):
                pa = os.path.join(base, a)
                if not os.path.isdir(pa) or not rx.fullmatch(a):
                    continue
                kids = [b for b in sorted(os.listdir(pa))
                        if os.path.isdir(os.path.join(pa, b)) and rx.fullmatch(b)]
                for b in (kids or ['']):
                    acct, org = (a, b) if acct_first else (b, a)
                    found += 1
                    obs.append(Observation(
                        ids.next('obs'), 'identity_dir',
                        os.path.relpath(pa, root), dirname,
                        'account_org_pair',
                        {'source_dir': dirname, 'account': acct, 'org': org,
                         'order': '계정/조직' if acct_first else '조직/계정'},
                        actor=acct, env_profile=env))
    # spaces-present 는 앞 8자리만 쓴다
    for base in _find(root, 'spaces-present'):
        for a in sorted(os.listdir(base)):
            pa = os.path.join(base, a)
            if not os.path.isdir(pa):
                continue
            for b in sorted(os.listdir(pa)):
                if not os.path.isdir(os.path.join(pa, b)):
                    continue
                found += 1
                obs.append(Observation(
                    ids.next('obs'), 'identity_dir', os.path.relpath(pa, root),
                    'spaces-present', 'account_org_prefix',
                    {'source_dir': 'spaces-present',
                     'account_prefix': a, 'org_prefix': b},
                    env_profile=env))
    if found:
        say('   계정 식별자 디렉터리 %d쌍' % found)
    return obs


# ── Cookies ─────────────────────────────────────────────────────
def _cookies(root, ids, say, env):
    r"""쿠키는 이름과 시각만 낸다. 값은 읽지 않는다.

    살아 있는 행과 지워진 조각을 모두 본다. 정식 API 로 열면 최신 상태만
    보이지만, 페이지를 직접 훑으면 지워진 행도 남아 있다.
    """
    obs = []
    files = [p for p in _find(root, 'Cookies') if os.path.isfile(p)]
    for p in files:
        rel = os.path.relpath(p, root)
        try:
            live, dead, _stats = sqlitefs.scan_db(p)
        except Exception as e:
            obs.append(Observation(ids.next('obs'), 'cookies', rel, 'Cookies',
                                   'cookie', {}, status='failed',
                                   note=str(e), env_profile=env))
            continue
        seen = set()
        for cells, state in ((live, 'live'), (dead, 'deleted')):
            for c in cells:
                # 셀은 원시 payload 다. 레코드로 풀어야 값이 나온다.
                pl = c.get('payload') or c.get('data') or b''
                if not pl:
                    continue
                try:
                    rec = sqlitefs.decode_record(pl)
                except Exception:
                    continue
                if not isinstance(rec, (list, tuple)) or len(rec) < 5:
                    continue
                # 테이블 행과 인덱스 항목은 열 배치가 다르다.
                #   테이블(rowid 있음) : 0 creation_utc · 1 host_key ·
                #                       2 top_frame_site_key · 3 name · …
                #   인덱스(rowid 없음) : 0 host_key · 1 top_frame_site_key ·
                #                       2 source_scheme · 3 name · … · 끝 rowid
                # 인덱스에는 시각이 없다. 없는 시각을 있는 척하면 안 되므로
                # 어디서 왔는지 표시하고 시각은 비운다.
                vals = list(rec)
                is_table = c.get('rowid') is not None
                host = nm = ''
                created = 0
                if is_table and len(vals) > 3 and isinstance(vals[1], str):
                    host = vals[1]
                    nm = vals[3] if isinstance(vals[3], str) else ''
                    created = vals[0] if isinstance(vals[0], int) else 0
                elif len(vals) > 3 and isinstance(vals[0], str):
                    host = vals[0]
                    nm = vals[3] if isinstance(vals[3], str) else ''
                if 'claude.ai' not in (host or '').lower():
                    continue
                if not nm or nm == host:
                    continue
                origin = 'table' if is_table else 'index'
                key = (state, origin, host, nm)
                if key in seen:
                    continue
                seen.add(key)
                low = nm.lower()
                kind = ('session' if low in SESSION_COOKIES else
                        'org' if low in ORG_COOKIES else 'other')
                obs.append(Observation(
                    ids.next('obs'), 'cookies', rel,
                    'page=%s offset=%s' % (c.get('page'), c.get('offset')),
                    'cookie',
                    {'host': host, 'name': nm, 'kind': kind,
                     'state': state, 'origin': origin},
                    ts={'ts_cookie_created': chrome_ms(created)},
                    env_profile=env,
                    note='값은 기록하지 않는다'))
    if files:
        n = sum(1 for o in obs if o.fact_type == 'cookie')
        say('   Cookies %d개 · claude.ai 쿠키 %d건' % (len(files), n))
    return obs
