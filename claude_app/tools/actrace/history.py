# -*- coding: utf-8 -*-
r"""브라우저 기록 — 우리 분석 7위. "어떻게 여기까지 왔나".

    %LocalAppData%\Microsoft\Edge\User Data\Default\History

실험에서 접근 경로 전체가 복원됐다.

    11:02:06  explore.microsoft.com/edge/welcome
    11:02:23  bing.com/search?q=google        ← keyword_search_terms 에 남음
    11:02:42  google.co.kr/search?q=claude    ← urls.url 에만 남음
    11:02:55  claude.ai/ · claude.ai/login    제목 "Sign in - Claude" = 비로그인

★ 검색어는 두 곳을 다 봐야 한다.
  브라우저 기본 검색엔진으로 한 검색만 keyword_search_terms 에 구조화되고,
  사이트 내 검색은 URL 쿼리스트링에만 남는다. 한쪽만 보면 절반을 놓친다.

★ 제목이 로그인 상태를 말한다.
  "Sign in - Claude" 가 보이면 그 시점엔 로그인 전이다.

다운로드는 downloads 표에 원본 URL 과 저장 경로가 함께 남는다 —
설치 파일을 어디서 받았는지가 여기서 나온다.
"""
import os
import re
from urllib.parse import parse_qs, unquote, urlparse

from chattrace import sqlitefs

from .schema import Observation, utc_ms

_CHROME_EPOCH_MS = 11644473600000

# 관심 도메인. 설치 경로 추적이 목적이라 AI 서비스와 검색엔진을 본다.
INTEREST = ('claude.ai', 'anthropic', 'chatgpt', 'openai', 'gemini',
            'copilot', 'perplexity')
SEARCH_HOSTS = ('google.', 'bing.com', 'duckduckgo', 'naver.com',
                'daum.net', 'yandex.')
SEARCH_KEYS = ('q', 'query', 'p', 'wd', 'text', 'search_query')

HIST_NAMES = ('history',)


def chrome_ms(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return ''
    if v <= 0:
        return ''
    return utc_ms(v // 1000 - _CHROME_EPOCH_MS)


def _find(root):
    out = []
    for dp, dn, fns in os.walk(root):
        for f in fns:
            if f.lower() in HIST_NAMES and 'user data' in dp.lower():
                out.append(os.path.join(dp, f))
            elif f.lower() == 'places.sqlite':
                out.append(os.path.join(dp, f))
    return out


def _query_terms(url):
    """URL 쿼리스트링에서 검색어를 꺼낸다. 사이트 내 검색은 여기에만 남는다."""
    try:
        u = urlparse(url)
    except ValueError:
        return ''
    if not any(h in (u.netloc or '').lower() for h in SEARCH_HOSTS):
        return ''
    try:
        qs = parse_qs(u.query)
    except ValueError:
        return ''
    for k in SEARCH_KEYS:
        if k in qs and qs[k] and qs[k][0].strip():
            return unquote(qs[k][0])
    return ''


def _looks_like_search(term):
    """검색어처럼 보이는가.

    주소창에 친 것도 keyword_search_terms 에 들어간다. file:// 경로와
    추적용 해시까지 '검색어' 로 보고하면 보고서가 지저분해지고, 그게
    쌓이면 진짜 검색어가 묻힌다.
    """
    t = (term or '').strip()
    if len(t) < 2 or len(t) > 120:
        return False
    if re.match(r'^[a-z][a-z0-9+.-]*://', t, re.I):
        return False                       # file:// http:// 등
    if re.match(r'^[A-Za-z]:[\\/]', t):
        return False                       # C:\... 같은 경로
    # 공백 없이 40자를 넘으면 사람이 친 검색어가 아니라 토큰이다.
    # 실제 검색어는 이렇게 길면서 띄어쓰기가 없는 경우가 거의 없다.
    if ' ' not in t and len(t) > 40:
        return False
    return True


def _browser_of(path):
    low = path.lower().replace('/', '\\')
    if '\\microsoft\\edge\\' in low:
        return 'Edge'
    if '\\google\\chrome\\' in low:
        return 'Chrome'
    if 'mozilla\\firefox' in low:
        return 'Firefox'
    return '알수없음'


def parse(root, ids, log=None, env_profile=''):
    say = log or (lambda *a: None)
    obs = []
    files = _find(root)
    if not files:
        say('   브라우저 기록을 찾지 못했다')
        return obs

    for p in files:
        rel = os.path.relpath(p, root)
        browser = _browser_of(rel)
        try:
            live, dead, _st = sqlitefs.scan_db(p)
        except Exception as e:
            obs.append(Observation(
                ids.next('obs'), 'browser_history', rel, browser,
                'browser_visit', {}, status='failed', note=str(e),
                env_profile=env_profile))
            continue

        n_url = n_search = n_dl = 0
        seen = set()
        for cells, state in ((live, 'live'), (dead, 'deleted')):
            for c in cells:
                pl = c.get('payload') or b''
                if not pl:
                    continue
                try:
                    rec = sqlitefs.decode_record(pl)
                except Exception:
                    continue
                vals = list(rec)
                strs = [v for v in vals if isinstance(v, str) and v]
                if not strs:
                    continue

                # urls 표: id, url, title, visit_count, typed_count,
                #          last_visit_time, hidden
                url = next((v for v in strs
                            if v.startswith('http://') or v.startswith('https://')),
                           '')
                if url:
                    low = url.lower()
                    term = _query_terms(url)
                    hot = any(h in low for h in INTEREST)
                    if not hot and not term:
                        continue
                    title = ''
                    for v in strs:
                        if v is not url and not v.startswith('http'):
                            title = v
                            break
                    # last_visit_time 은 큰 정수다
                    ts = ''
                    for v in vals:
                        if isinstance(v, int) and v > 12000000000000000:
                            ts = chrome_ms(v)
                            break
                    key = (state, url[:200], ts)
                    if key in seen:
                        continue
                    seen.add(key)
                    n_url += 1
                    obs.append(Observation(
                        ids.next('obs'), 'browser_history', rel,
                        'page=%s offset=%s' % (c.get('page'), c.get('offset')),
                        'browser_visit',
                        {'browser': browser, 'url': url[:400],
                         'title': title[:200], 'state': state,
                         'search_term_in_url': term,
                         'is_download': '/download' in low},
                        ts={'ts_visit': ts}, env_profile=env_profile))
                    if term:
                        n_search += 1
                    continue

                # keyword_search_terms 표: keyword_id, url_id,
                #                          lower_term, term
                if len(strs) <= 2 and len(vals) >= 3:
                    term = strs[-1]
                    if not _looks_like_search(term):
                        continue
                    key = (state, 'kw', term.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    n_search += 1
                    obs.append(Observation(
                        ids.next('obs'), 'browser_history', rel,
                        'page=%s offset=%s' % (c.get('page'), c.get('offset')),
                        'browser_search',
                        {'browser': browser, 'term': term[:200],
                         'state': state, 'source': 'keyword_search_terms'},
                        env_profile=env_profile))
                    continue

                # downloads 표: 원본 URL 과 저장 경로가 같이 있다
                localish = [v for v in strs
                            if re.match(r'^[A-Za-z]:\\', v) or v.startswith('\\\\')]
                if localish:
                    src = next((v for v in strs if v.startswith('http')), '')
                    key = (state, 'dl', localish[0])
                    if key in seen:
                        continue
                    seen.add(key)
                    n_dl += 1
                    obs.append(Observation(
                        ids.next('obs'), 'browser_history', rel,
                        'page=%s offset=%s' % (c.get('page'), c.get('offset')),
                        'browser_download',
                        {'browser': browser, 'target_path': localish[0][:300],
                         'source_url': src[:400], 'state': state},
                        env_profile=env_profile))
        say('   %s 기록 — 방문 %d · 검색 %d · 다운로드 %d'
            % (browser, n_url, n_search, n_dl))
    return obs
