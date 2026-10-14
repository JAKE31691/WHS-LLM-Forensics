# -*- coding: utf-8 -*-
r"""팀 합의 6열 표 — 시각 / 사용자 행위 / 아티팩트 / 경로 / 등급 / 근거.

2026-10-09 결정. 기존 4열(행위 분류·아티팩트·경로·등급)을 대체한다.
달라진 핵심은 **근거 열**이다 — "왜 그렇게 판정했나" 를 표에서 바로
읽을 수 있어야 한다는 요구다.

한 줄의 단위
    **판정규칙 1개 × 파일 1개.** 같은 규칙이 같은 파일에서 열 번 섰어도
    한 줄이다. 시각은 그중 가장 이른 것.

시각
    `YYYY-MM-DD HH:MM:SS.mmm KST`. 내부는 전부 UTC 로 다루지만 사람이
    읽는 표는 현지 시각이어야 한다. 없으면 `-`.

경로
    **원본 기기에서의 절대경로.** 수집물 안의 상대경로가 아니다.

근거
    **해석이 앞, 원시 근거가 뒤.**

        앱 버전 2.2553.1 | version="2.2553.1"; @000005.ldb+0x53CA; 외 2건

    사람이 먼저 읽는 것은 "그래서 무슨 일이 있었나" 다. key=value 와
    오프셋은 그 말을 의심할 때 따라가는 것이라 뒤에 둔다.

정렬
    시각 있는 줄을 시간순으로 먼저, `-` 줄은 행위 코드순으로 뒤에.
"""
from __future__ import print_function

import csv
import datetime
import io
import json
import os
import re

from . import actions
from .schema import iso_to_ms

COLS = ['시각', '사용자 행위', '아티팩트', '경로', '등급', '근거']
#: 해석을 **따로 칸으로** 둔 판. 한 칸에 섞으면 둘 다 읽기 나쁘다.
#   내용 = 그래서 무슨 일이 있었나 (판정 결과를 사람 말로)
#   근거 = 그 말의 출처 (key=value 와 원본에서의 위치)
COLS_FULL = COLS[:-1] + ['내용', '근거']

DIRECT = '직접'
SUPPORT = '보조'

KST = 9 * 3600
MAXLEN = 300
MAX_EV = 3          # 근거에 펼쳐 보일 관찰 수. 나머지는 '외 N건'

SEP = chr(92)       # 역슬래시. 문자열 안에서 이스케이프가 꼬이는 걸 막는다


def tz_offset_hours(infs):
    """R-TZ-DERIVE-01 이 역산한 기기 타임존(시간). 못 구하면 None.

    같은 로그아웃이 Local Storage(UTC)와 main.log(현지) 양쪽에 남아
    그 차이로 기기 타임존을 역산한다. 추측이 아니라 관측이다.
    """
    for i in infs:
        d = i.as_dict() if hasattr(i, 'as_dict') else dict(i)
        if d.get('rule_id') == 'R-TZ-DERIVE-01':
            v = (d.get('detail') or {}).get('offset_hours')
            if isinstance(v, (int, float)):
                return v
    return None


def _kst(ms, unknown_tz=False):
    """Unix 밀리초 -> 'YYYY-MM-DD HH:MM:SS.mmm KST'. 없으면 '-'.

    unknown_tz 면 기기 타임존을 역산하지 못한 것이다. 숫자는 로그에
    적힌 그대로이고 어느 지역 시각인지는 모른다 — KST 라고 우기지 않고
    '(현지)' 로 표시한다.
    """
    if ms is None:
        return '-'
    dt = datetime.datetime(1970, 1, 1) + datetime.timedelta(
        seconds=ms / 1000.0 + KST)
    tail = '(현지)' if unknown_tz else 'KST'
    return dt.strftime('%Y-%m-%d %H:%M:%S.') + '%03d %s' % (ms % 1000, tail)


_DRIVE = re.compile(r'^(?:kape' + re.escape(SEP) + r')?'
                    r'([A-Za-z])' + re.escape(SEP) + r'(.+)$')


def abs_path(o):
    r"""수집물 안의 상대경로 -> 원본 기기에서의 절대경로.

    수집물은 같은 폴더를 두 벌 담는 일이 많다 —

        app\Local Storage\leveldb                         robocopy 사본
        kape\C\Users\Kwon\AppData\...\Local Storage\leveldb   KAPE 사본

    robocopy 사본은 원본 위치를 잃었고 KAPE 사본은 드라이브부터 들고
    있다. 그래서 **두 경로를 다 보고 복원되는 쪽**을 고른다. dedupe 가
    합칠 때 버린 경로를 duplicate_paths 에 남겨 둔 덕이다.

    이미지에서 꺼낸 것은 `C\Users\...` 꼴이라 그대로 맞는다.
    """
    cands = [o.get('source_path') or ''] + list(o.get('duplicate_paths') or [])
    for c in cands:
        m = _DRIVE.match(c.replace('/', SEP))
        if m:
            return '%s:%s%s' % (m.group(1).upper(), SEP, m.group(2))
    # $MFT 에서 나온 경로는 드라이브 문자가 없다 (파일시스템 안의 경로라
    # 볼륨을 모른다). 윈도우가 설치된 파티션을 본 것이므로 C: 로 적는다.
    p = (cands[0] or '').replace('/', SEP)
    if p and not re.match(r'^[A-Za-z]:', p) and not p.startswith(
            ('app' + SEP, 'kape' + SEP)):
        return 'C:' + SEP + p.lstrip(SEP)
    return p or '-'


def _artifact_name(path):
    """경로 -> 파일명. 폴더면 폴더명 (LevelDB 처럼 폴더인 것이 많다)."""
    p = (path or '').replace('/', SEP).rstrip(SEP)
    return p.split(SEP)[-1] if p else '-'


def _q(v):
    """값 하나를 근거에 적을 꼴로. 문자열은 따옴표, 길면 자른다."""
    if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
        if len(v) > 60:
            v = v[:57] + '…(총 %d자)' % len(v)
        return v
    v = str(v)
    if len(v) > 60:
        return '"%s…(총 %d자)"' % (v[:57], len(v))
    return '"%s"' % v


def _pairs(o, cap=3, indexed=False, index=None):
    r"""관찰 하나를 key=value 꼴로. 위치(locator)를 끝에 붙인다.

    indexed=True 면 다른 조의 표기에 맞춰 **어느 레코드의 어느 필드인지**
    를 앞에 박는다. 그쪽은 parsed_artifacts.json 의 인덱스를 쓰고
    우리는 관찰 id 를 쓴다 — 둘 다 "원본 어디서 왔나" 를 가리키지만
    중간 파일 구조가 달라 인덱스를 그대로 흉내낼 수는 없다.
    id 는 observations.jsonl 에서 그대로 찾아진다.

        localstorage.records[obs:0001].version="2.2553.1"; @000005.ldb+0x53CA
    """
    out = []
    pre = ''
    if indexed:
        # parsed_artifacts.json 의 실제 인덱스를 쓴다. 그래야 다른 조
        # 도구로도 'artifacts[12].records[99]' 를 그대로 따라갈 수 있다.
        ij = (index or {}).get(o.get('id'))
        if ij:
            pre = 'artifacts[%d].records[%d].' % ij
        else:
            pre = '%s.records[%s].' % (o.get('source_artifact') or '?',
                                       o.get('id') or '?')
    for k, v in list((o.get('fact') or {}).items())[:cap]:
        out.append('%s%s=%s' % (pre, k, _q(v)))
    loc = (o.get('source_locator') or '').strip()
    if loc:
        out.append('@%s' % loc)
    return '; '.join(out)


def _head(inf):
    """내용 — 그래서 무슨 일이 있었나. 판정 결과를 사람 말로."""
    basis = inf.get('confidence_basis') or {}
    h = (inf.get('note') or '').strip()
    if not h:
        h = (basis.get('why_confirmed') or basis.get('why') or '').strip()
    if not h:
        h = (inf.get('when_basis') or '').strip()
    return h.replace('\n', ' ').strip() or '-'


def _reason(inf, obs_list, extra=0, head_first=True, index=None):
    """근거.

    head_first=True  해석이 앞, 원시 근거가 뒤 (한 칸에 다 넣을 때)
    head_first=False 원시 근거만 (칸을 가르거나 팀 형식일 때)
    """
    basis = inf.get('confidence_basis') or {}
    head = (inf.get('note') or '').strip()
    if not head:
        head = (basis.get('why_confirmed') or basis.get('why') or '').strip()
    if not head:
        head = (inf.get('when_basis') or '').strip()

    chunks = []
    for o in obs_list[:MAX_EV]:
        c = _pairs(o, indexed=not head_first, index=index)
        # 같은 파일의 같은 값이 여러 관찰에 걸쳐 나오면 한 번만 적는다
        if c and c not in chunks:
            chunks.append(c)
    rest = max(0, len(obs_list) - MAX_EV) + extra
    if rest:
        chunks.append('외 %d건' % rest)
    raw = '; '.join(chunks)

    if not head_first:
        s = raw or head
        if len(s) > MAXLEN:
            s = s[:MAXLEN - 3] + '...'
        return s.replace('\n', ' ').strip() or '-'

    if head and raw:
        room = MAXLEN - len(head) - 3
        if room < 30:
            s = head[:MAXLEN - 3] + '...' if len(head) > MAXLEN else head
        else:
            if len(raw) > room:
                raw = raw[:room - 3] + '...'
            s = '%s | %s' % (head, raw)
    else:
        s = head or raw
        if len(s) > MAXLEN:
            s = s[:MAXLEN - 3] + '...'
    return s.replace('\n', ' ').strip() or '-'


def rows(obs, infs, env_profile='', index=None):
    """판정규칙 1개 × 파일 1개 = 한 줄. 시각은 그중 가장 이른 것."""
    off_h = tz_offset_hours(infs)
    by_id = {}
    for o in obs:
        by_id[o.id] = o.as_dict() if hasattr(o, 'as_dict') else dict(o)

    acc = {}
    for i in infs:
        d = i.as_dict() if hasattr(i, 'as_dict') else dict(i)
        rule = d.get('rule_id') or ''
        code = actions.code_of(d.get('behavior_id'), rule) or 'ZZZ'
        name = (actions.name_of(code, env_profile) if code != 'ZZZ'
                else (d.get('behavior_name') or '?'))
        sup = [by_id[x] for x in (d.get('supported_by') or []) if x in by_id]
        kinds = set(x['source_artifact'] for x in sup)
        # ★ 현지 시각으로 기록된 아티팩트인가 (앱 로그가 그렇다).
        #   그냥 UTC 로 읽으면 KST 변환에서 9시간이 밀린다 — 실제로
        #   22:05 사건이 다음날 07:05 로 찍혀 나왔다. 보고서에서 9시간은
        #   결론을 뒤집는 크기다.
        is_local = any((x.get('tz_source') or '').lower().startswith('local')
                       for x in sup)
        grade = (DIRECT if (d.get('confidence') == 'confirmed'
                            and len(kinds) == 1) else SUPPORT)
        groups = {}
        for o in sup:
            groups.setdefault(abs_path(o), []).append(o)
        if not groups:
            groups = {'-': []}
        ms = iso_to_ms(d.get('when_from') or '')
        unknown_tz = False
        if ms is not None and is_local:
            # 현지 벽시계로 적힌 값이다. 역산한 오프셋만큼 빼서 UTC 로
            # 맞춘다.
            if off_h is None:
                # 오프셋을 못 구했다. 그래도 KST 라고 우기지는 않는다 —
                # 숫자는 적힌 그대로 두고 '(현지)' 로 표시해 넘긴다.
                ms = ms - KST * 1000
                unknown_tz = True
            else:
                ms = ms - int(off_h * 3600 * 1000)
        for path, mine in groups.items():
            key = (code, rule, path)
            cur = acc.get(key)
            if cur is None:
                acc[key] = {
                    'code': code, '사용자 행위': name,
                    '아티팩트': _artifact_name(path), '경로': path,
                    '등급': grade, '규칙': rule,
                    '_obs': list(mine), '_ms': ms, '_inf': d, '_extra': 0,
                    '_tzq': unknown_tz}
            else:
                if grade == DIRECT:
                    cur['등급'] = DIRECT
                cur['_extra'] += len(mine)
                if ms is not None and (cur['_ms'] is None or ms < cur['_ms']):
                    cur['_ms'] = ms
                    cur['_inf'] = d
                    cur['_obs'] = list(mine)
                    cur['_tzq'] = unknown_tz

    out = []
    for r in acc.values():
        r['시각'] = _kst(r.pop('_ms'), r.pop('_tzq', False))
        inf, ob, ex = r.pop('_inf'), r.pop('_obs'), r.pop('_extra')
        # 팀 형식 — 원시 근거만, 레코드 인덱스를 박아 다른 조와 대조한다
        r['근거'] = _reason(inf, ob, ex, head_first=False, index=index)
        # 상세판 — 내용과 근거를 **따로** 둔다
        r['내용'] = _head(inf)
        r['_raw'] = _reason(inf, ob, ex, head_first=False)
        out.append(r)
    out.sort(key=lambda r: (r['시각'] == '-', r['시각'], r['code']))
    return out


def write_csv(outdir, rws, name='report.csv', cols=None):
    p = os.path.join(outdir, name)
    COLS_ = cols or COLS
    # .csv 는 BOM 을 넣는다 — 없으면 엑셀이 cp949 로 읽어 한글이 깨진다
    with io.open(p, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLS_, extrasaction='ignore')
        w.writeheader()
        for r in rws:
            w.writerow(r)
    return p
