#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""ActTrace — Claude Desktop 행위 재구성.

ChatTrace 가 "무엇을 말했나" 를 답한다면 이 도구는 "누가 · 언제 · 무엇을
했나" 를 답한다. 대화 복원은 여기서 부수적이다.

    아티팩트 ──파싱──▶ observation ──추론──▶ inference ──▶ 보고서
                (사실)                   (행위)

ChatTrace 를 라이브러리로 쓴다. 포크하지 않는다 — 포크하면 한쪽만 고치고
잊는다. 이미지 입력(E01/dd/vmdk)도 ChatTrace 의 image 층을 그대로 쓴다.

    ActTrace -i <수집물|E01> -o <출력>
"""
from __future__ import print_function

import argparse
import csv
import datetime
import hashlib
import io
import json
import os
import sys
from collections import Counter, defaultdict

from chattrace import anim

from . import (applog, content, history, identity, install_order,
               localstorage, prefetch, registry, rules, uniform,
               evidence, bundle)
from .schema import LEVEL_KO, Ids, Observation, UNDECIDED, write_jsonl


def _now_iso():
    """UTC ISO8601 밀리초. 공통 명세 §6 의 시각 규약."""
    return datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.%f')[:23] + 'Z'


def say(text=''):
    """애니메이션이 도는 중에도 안전하게 한 줄 찍는다.

    그냥 print 하면 그리는 중간에 끼어들어 그림이 찢어진다. UI 가
    지우고 → 찍고 → 다시 그리는 순서를 보장한다.
    """
    if anim.UI.enabled:
        anim.UI.log(text)
    else:
        print(text)


def _fix_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def _load_conversations(path):
    """ChatTrace 결과가 있으면 초안 대조에 쓴다. 없어도 돌아간다."""
    if not path:
        return []
    p = path
    if os.path.isdir(p):
        p = os.path.join(p, 'conversations.json')
    if not os.path.exists(p):
        return []
    try:
        j = json.load(io.open(p, encoding='utf-8'))
    except Exception:
        return []
    c = j.get('conversations') if isinstance(j, dict) else j
    if isinstance(c, dict):
        c = list(c.values())
    return c or []


def _csv(path, cols, rows):
    with io.open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)


INF_COLS = ['id', 'question', 'behavior_id', 'behavior_name', 'confidence',
            'when_from', 'when_to', 'span_sec', 'actor', 'rule_id',
            'when_basis', 'note', 'alternatives', 'supported_by',
            'source_artifacts']


SPEC_VERSION = '1'


def _merge_key(o):
    """같은 사실인가를 판정하는 열쇠. 경로만 빼고 전부 본다."""
    return (o.source_artifact, o.fact_type, o.source_locator, o.seq, o.actor,
            json.dumps(o.fact, sort_keys=True, ensure_ascii=False),
            json.dumps(o.ts, sort_keys=True))


def _merge_dupes(got, seen):
    """내용이 같고 경로만 다른 관찰을 하나로 합친다.

    ★ 왜 필요한가 — 수집물은 KAPE 와 robocopy 가 Claude 폴더를 **둘 다**
      담는 일이 흔하다. 그러면 같은 LevelDB 를 두 번 읽어 같은 사실이 두 벌
      생긴다. 수가 부푸는 것으로 끝나지 않는다. 식별자 없이 관찰을 순회하는
      규칙은 **없던 사건을 만든다** — 실측에서 R-REPLY-TIMING-01 이 응답을
      8번이 아니라 16번 받았다고 냈다.

      수집 스크립트를 고치는 것은 반쪽이다. 남이 뜬 수집물에는 안 먹고,
      KAPE 가 원래 그렇게 동작한다. 도구가 걸러야 한다.

      지우지 않고 **합친다.** 버린 쪽의 경로는 duplicate_paths 에 남는다.
    """
    out = []
    merged = 0
    for o in got:
        k = _merge_key(o)
        keep = seen.get(k)
        if keep is None:
            seen[k] = o
            out.append(o)
            continue
        if (o.source_path and o.source_path != keep.source_path
                and o.source_path not in keep.duplicate_paths):
            keep.duplicate_paths.append(o.source_path)
        merged += 1
    return out, merged

#: 통일 CLI 출력의 '아티팩트 정의' 목록. 순서는 파싱 순서와 같다.
#  이름을 영문으로 둔 이유 — 다른 환경의 출력과 줄 단위로 맞춰 본다.
ARTIFACT_DEFS = ('Local Storage', 'Prefetch', 'Application Log',
                 'Account Identity', 'Registry', 'Install Order',
                 'Browser History', 'Conversation Content')
TOOL_VERSION = '1.4.0'


def _sha256(path, cap=None):
    """입력의 해시. 같은 입력에 같은 결과가 나오는지 나중에 확인해야 한다.

    폴더는 통째로 해시할 수 없으므로 파일 목록과 크기로 지문을 만든다.
    내용까지 보장하지는 않지만 '다른 수집물을 넣었다' 는 즉시 드러난다.
    """
    h = hashlib.sha256()
    try:
        if os.path.isdir(path):
            for dp, dn, fns in os.walk(path):
                dn.sort()
                for fn in sorted(fns):
                    p = os.path.join(dp, fn)
                    rel = os.path.relpath(p, path).replace('\\', '/')
                    try:
                        h.update(('%s:%d\n' % (rel, os.path.getsize(p)))
                                 .encode('utf-8'))
                    except OSError:
                        continue
            return 'dirdigest:' + h.hexdigest()
        with open(path, 'rb') as f:
            n = 0
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                h.update(b)
                n += len(b)
                if cap and n >= cap:
                    return 'sha256:%s(first %d B)' % (h.hexdigest(), n)
        return 'sha256:' + h.hexdigest()
    except OSError as e:
        return 'unavailable: %s' % e


def _write_run(outdir, meta):
    """run.json — 무엇을 · 언제 · 어떤 입력으로 돌렸나."""
    with io.open(os.path.join(outdir, 'run.json'), 'w',
                 encoding='utf-8') as f:
        f.write(json.dumps(meta, ensure_ascii=False, indent=2,
                           sort_keys=False))


def _write_summary(outdir, obs, infs, meta, nbad):
    """summary.txt — 사람이 먼저 여는 파일. 기계용 파일은 따로 있다."""
    from collections import Counter as _C
    lv = _C(i.confidence for i in infs)
    qs = _C(i.question for i in infs)
    lines = [
        '%s %s — 실행 요약' % (meta.get('tool', ''), meta.get('tool_version', '')),
        '=' * 56,
        '',
        '  입력        %s  (%s)' % (meta.get('input', ''),
                                    meta.get('input_kind', '')),
        '  지문        %s' % meta.get('input_sha256', ''),
        '  환경        %s' % meta.get('env_profile', ''),
        '  시작        %s' % meta.get('started_at', ''),
        '  종료        %s' % meta.get('finished_at', ''),
        '',
        '  관찰        %s건' % format(len(obs), ','),
        '  추론        %s건' % format(len(infs), ','),
        '  문제        %s건' % format(nbad, ','),
        '',
        '  판정 등급',
    ]
    for k, ko in (('confirmed', '확정'), ('suspected', '유력'),
                  ('presumed', '추정'), ('undecided', '판정불가')):
        lines.append('    %-8s %4d건' % (ko, lv.get(k, 0)))
    lines += ['', '  질문별']
    for q in ('Q1', 'Q2', 'Q3', 'Q4', 'Q5'):
        lines.append('    %s      %4d건' % (q, qs.get(q, 0)))
    lines += [
        '',
        '  ※ 판정불가는 실패가 아니다. 근거 부재를 근거 부재라고 적은 것이다.',
        '  ※ 자세한 내용은 behaviors.csv · findings.csv 를 본다.',
        '',
    ]
    # BOM 을 넣는다. 없으면 PowerShell·메모장이 cp949 로 읽어 깨진다.
    with io.open(os.path.join(outdir, 'summary.txt'), 'w',
                 encoding='utf-8-sig') as f:
        f.write('\n'.join(lines))


def _write(outdir, obs, infs, env_profile='', meta=None, root=None):
    write_jsonl(os.path.join(outdir, 'observations.jsonl'), obs)
    write_jsonl(os.path.join(outdir, 'inferences.jsonl'), infs)

    # obs id -> 아티팩트 종류. 추론 한 줄만 보고도 '어느 아티팩트에서
    # 나온 근거인가' 를 알게 한다. 전에는 supported_by 의 id 를 들고
    # observations.jsonl 을 되짚어야 출처가 나왔다.
    art = dict((o.id, o.source_artifact) for o in obs)

    rows = []
    for i in sorted(infs, key=lambda x: (x.when_from or 'zzzz', x.behavior_id)):
        d = i.as_dict()
        d['alternatives'] = ' | '.join(d['alternatives'])
        # 중복 없이, 근거로 쓰인 순서 그대로
        seen = []
        for oid in i.supported_by:
            a = art.get(oid)
            if a and a not in seen:
                seen.append(a)
        d['source_artifacts'] = ' | '.join(seen)
        d['supported_by'] = ' '.join(d['supported_by'][:8])
        rows.append(d)
    _csv(os.path.join(outdir, 'behaviors.csv'), INF_COLS, rows)

    # 중요한 것만 — 확정·유력
    _csv(os.path.join(outdir, 'findings.csv'), INF_COLS,
         [r for r in rows if r['confidence'] in ('confirmed', 'suspected')])

    # 실행 기록 — 횟수와 마지막 8회 시각을 한 표로.
    # '실행 몇 회' 와 '그중 시각을 아는 게 몇 회' 는 다른 값이라 둘 다 낸다.
    runs = []
    for o in obs:
        if o.fact_type != 'program_run_summary':
            continue
        f = o.fact
        row = {'name': f.get('name'), 'hash': f.get('hash'),
               'run_count': f.get('run_count'),
               'recorded_runs': f.get('recorded_runs'),
               'unrecorded_runs': f.get('unrecorded_runs'),
               'full_path': f.get('full_path'),
               'pf_created': o.ts.get('ts_pf_created', ''),
               'source_path': o.source_path}
        for k, t in enumerate(f.get('run_times') or [], 1):
            row['run_%d' % k] = t
        runs.append(row)
    if runs:
        runs.sort(key=lambda r: -(r.get('run_count') or 0))
        _csv(os.path.join(outdir, 'program_runs.csv'),
             ['name', 'hash', 'run_count', 'recorded_runs', 'unrecorded_runs',
              'pf_created', 'full_path'] +
             ['run_%d' % k for k in range(1, 9)] + ['source_path'], runs)

    # 아티팩트별 요약 — 공통 명세 §5 의 열 구성
    #   무엇을 · 어디서 · 몇 건 · 언제부터 언제까지 · 누가 읽었나 · 상태
    # 시각 범위가 있어야 "이 아티팩트가 어느 구간을 덮는가" 를 말할 수 있다.
    groups = {}
    for o in obs:
        key = (o.source_artifact, o.source_path)
        g = groups.setdefault(key, {'n': 0, 'ts': [], 'parser': o.parser,
                                    'status': 'ok'})
        g['n'] += 1
        g['ts'] += [v for v in (o.ts or {}).values() if v]
        if o.parser_status == 'error':
            g['status'] = 'error'
        elif o.parser_status == 'partial' and g['status'] == 'ok':
            g['status'] = 'partial'
    _csv(os.path.join(outdir, 'artifacts.csv'),
         ['artifact', 'path', 'observations', 'ts_min', 'ts_max',
          'parser', 'status'],
         [{'artifact': a, 'path': p, 'observations': g['n'],
           'ts_min': min(g['ts']) if g['ts'] else '',
           'ts_max': max(g['ts']) if g['ts'] else '',
           'parser': g['parser'], 'status': g['status']}
          for (a, p), g in sorted(groups.items())])

    # 못 읽은 것 — ★ 문제가 없어도 파일은 만든다.
    #   파일이 없으면 '문제가 없었다' 와 '도구가 죽어 안 만들었다' 가
    #   구분되지 않는다. 빈 표라도 헤더는 남긴다.
    # 팀 합의 통일 4열 — 행위 분류 | 아티팩트 | 경로 | 아티팩트 등급
    #   다른 환경의 표와 그대로 맞춰 보는 요약이다. 추적성은
    #   behaviors.csv 와 observations.jsonl 에 그대로 남는다.
    from . import rules as _r
    urows = uniform.rows(obs, infs, sorted(_r.BEHAVIORS))
    uniform.write_csv(outdir, urows, name='report_4col.csv')

    # 팀 합의 6열 (2026-10-09) — 시각/행위/아티팩트/경로/등급/근거
    #   판정규칙 1개 × 파일 1개 = 한 줄. 시각은 KST.
    # ★ parsed_artifacts.json 을 먼저 만든다. 근거 열이 그 파일의
    #   인덱스(artifacts[i].records[j])를 가리키기 때문이다 — 중간 파일
    #   없이는 남의 도구로 근거를 따라갈 수 없다.
    pdoc, pindex = bundle.build_parsed(obs, meta or {})
    erows = evidence.rows(obs, infs, env_profile, index=pindex)
    #  report.csv        팀 형식 — 원시 근거만. 다른 조 표와 그대로 대조한다
    #  report_상세.csv   해석을 앞에 붙인 판 — 사람이 읽는다
    evidence.write_csv(outdir, erows, name='LLM_Artifact_Report.csv')
    evidence.write_csv(outdir, erows, name='LLM_Artifact_Report_상세.csv',
                       cols=evidence.COLS_FULL)
    # parsed_artifacts.json 은 zip 을 묶은 뒤에 쓴다 — 수집 결과
    # (collected_members · collection_status)가 doc 에 채워져야 한다.

    bad = [o for o in obs if o.parser_status != 'ok']
    _csv(os.path.join(outdir, 'problems.csv'),
         ['id', 'source_artifact', 'source_path', 'parser',
          'parser_status', 'parser_note'],
         [o.as_dict() for o in bad])
    return len(bad), urows, erows, pdoc


def _console(obs, infs, nbad, secs, outdir):
    W = 66
    print()
    print('=' * W)
    print('  ActTrace — 행위 재구성')
    print('=' * W)
    print('  관찰 %s건 · 추론 %s건%s'
          % (format(len(obs), ','), format(len(infs), ','),
             (' · 못 읽음 %d' % nbad) if nbad else ''))
    print('  ' + '-' * (W - 4))

    byq = defaultdict(list)
    for i in infs:
        byq[i.question].append(i)
    QT = {'Q1': '이 PC 에서 썼나',
          'Q2': '누구 계정으로 썼나',
          'Q3': '무엇을 묻고 받았나',
          'Q4': '무엇을 넣고 뺐나',
          'Q5': '무엇을 감추려 했나',
          'Q3/Q5': '입력과 그 흔적'}
    for q in sorted(byq):
        print()
        print('  [%s] %s' % (q, QT.get(q, '')))
        for i in sorted(byq[q], key=lambda x: (x.behavior_id,
                                               x.when_from or 'zzzz')):
            when = i.when_from or '—'
            if i.when_to and i.when_to != i.when_from:
                when = '%s ~ %s' % (i.when_from[11:23], i.when_to[11:23])
            elif when != '—':
                when = when[:23]
            print('    %-9s %-26s %-22s %s'
                  % (LEVEL_KO.get(i.confidence, i.confidence),
                     i.behavior_name[:26], when, (i.note or '')[:36]))

    # 실행 기록은 화면에도 표로 낸다 — 섀도우 AI 사건에서 제일 먼저 보는 값
    runs = [i for i in infs if (i.detail or {}).get('run_count')]
    if runs:
        print()
        print('  [실행 기록] 횟수는 온전하고, 시각은 마지막 8회만 남는다')
        print('    %-16s %-9s %5s %5s %6s  %s'
              % ('실행파일', '해시', '횟수', '시각', '미상', '마지막 실행'))
        tot = seen = 0
        for i in sorted(runs, key=lambda x: -(x.detail.get('run_count') or 0)):
            d = i.detail
            n = len(d.get('run_times') or [])
            tot += d.get('run_count') or 0
            seen += n
            mark = ' [메인]' if d.get('is_main_process') else ''
            warn = '' if d.get('counter_complete') else ' ※재생성'
            # run_times 는 최신순이다. [-1] 을 쓰면 가장 오래된 게 나온다.
            last = (d.get('run_times') or [''])[0]
            print('    %-16s %-9s %5d %5d %6d  %s%s%s'
                  % ((d.get('name') or '')[:16], d.get('hash'),
                     d.get('run_count') or 0, n, d.get('unrecorded_runs') or 0,
                     last[:23], mark, warn))
        if tot:
            print('    %-16s %-9s %5d %5d %6d  시각을 아는 비율 %.0f%%'
                  % ('합계', '', tot, seen, tot - seen, 100.0 * seen / tot))

    # Q2 요약 — '누구 계정으로 썼나' 의 답을 한 자리에 모은다.
    # 추론이 흩어져 있으면 사람이 다시 조립해야 한다.
    accts, orgs, wins, tz = set(), set(), set(), ''
    switches = []
    for i in infs:
        d = i.detail or {}
        if d.get('account'):
            accts.add(d['account'])
        if d.get('org'):
            orgs.add(d['org'])
        for k in ('accounts',):
            for v in (d.get(k) or []):
                accts.add(v)
        if d.get('sid'):
            wins.add(d['sid'])
        if i.rule_id == 'R-TZ-DERIVE-01':
            tz = 'UTC%+g' % d.get('offset_hours', 0)
        if i.behavior_id == 'B04' and (i.when_from or d.get('uuid_to')):
            switches.append(i)
    if accts or orgs or wins:
        print()
        print('  [Q2 요약] 누구 계정으로 썼나')
        if wins:
            for s_ in sorted(wins):
                print('    윈도우 사용자  SID %s' % s_)
        for a in sorted(accts):
            print('    Claude 계정    %s' % a)
        for g in sorted(orgs):
            print('    조직           %s' % g)
        if tz:
            print('    기기 타임존    %s (역산)' % tz)
        for i in sorted(switches, key=lambda x: x.when_from or ''):
            print('    전환/전이      %-23s %s'
                  % ((i.when_from or '')[:23], (i.note or '')[:40]))

    und = [i for i in infs if i.confidence == UNDECIDED]
    if und:
        print()
        print('  판정 불가 %d건 — 근거 부재는 결론이 아니다' % len(und))
    print()
    print('  ' + '-' * (W - 4))
    print('  소요 %.1f초' % secs)
    print('  출력 %s' % outdir)
    print('=' * W)
    print()


def main(argv=None):
    _fix_console()
    ap = argparse.ArgumentParser(
        prog='ActTrace',
        description='ActTrace — Claude Desktop 행위 재구성 '
                    '(관찰/추론 2층 · 신뢰도 등급)')
    # 통일 입력 형식 (팀 합의) — 이미지 · 환경 · 저장 경로 를 순서대로.
    #   ActTrace <이미지|수집물> <환경> <저장경로>
    # 기존 -i/-o/--profile 도 그대로 받는다. 이미 돌고 있는 스크립트를
    # 깨뜨리면서 형식을 통일할 이유가 없다.
    ap.add_argument('args', nargs='*', metavar='인자',
                    help='<이미지|수집물> [<환경>] <저장경로> 순서로 줘도 된다')
    ap.add_argument('-i', '--input',
                    help='수집물 폴더 또는 디스크 이미지(E01/dd/vmdk)')
    ap.add_argument('-o', '--output', help='출력 폴더')
    ap.add_argument('--conversations',
                    help='ChatTrace 결과 폴더. 주면 초안이 전송됐는지 대조한다')
    # 공통 명세 §1 의 환경 코드. 설치 형태는 뒤에 붙인다 —
    #   claude-desktop/msix, claude-desktop/nsis
    ap.add_argument('--profile', default=None,
                    help='환경 프로파일 (공통 명세 §1 의 코드. 결과에 기록된다)')
    ap.add_argument('--all-programs', action='store_true',
                    help='Claude 외 프로그램의 Prefetch 도 전부 읽는다')
    ap.add_argument('--no-anim', action='store_true',
                    help='진행 중 애니메이션을 끄고 로그만 흘린다')
    ap.add_argument('--tag', default=None,
                    help='애니메이션 이름표 (기본 CK, 빈 값이면 뗀다)')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args(argv)

    # 통일 입력 형식을 기존 플래그와 섞어 받는다. 플래그가 이미 채운
    # 자리는 건드리지 않는다 — 그래야 둘을 같이 써도 놀랄 일이 없다.
    pos = list(a.args or [])
    if len(pos) >= 1 and not a.input:
        a.input = pos[0]
    if len(pos) >= 3:
        if a.profile is None:
            a.profile = pos[1]
        if not a.output:
            a.output = pos[2]
    elif len(pos) == 2 and not a.output:
        a.output = pos[1]
    if a.profile is None:
        a.profile = 'claude-desktop/msix'
    if not a.input or not a.output:
        ap.error('입력과 출력이 필요하다.\n'
                 '  통일 형식 : ActTrace <이미지|수집물> <환경> <저장경로>\n'
                 '  기존 형식 : ActTrace -i <이미지|수집물> -o <저장경로>')

    import time
    if a.no_anim:
        anim.UI.enabled = False
    if a.tag is not None:
        anim.UI.set_tag(a.tag)
    t0 = time.time()
    started_at = _now_iso()
    os.makedirs(a.output, exist_ok=True)

    # 통일 CLI 출력의 머리 — 무엇을 · 어디서 · 어디로. 파싱보다 먼저
    # 찍는다. 도중에 죽어도 '무슨 입력으로 돌렸나' 는 화면에 남는다.
    meta = {
        'tool': 'ActTrace',
        'tool_version': TOOL_VERSION,
        'spec_version': SPEC_VERSION,
        'env_profile': a.profile,
        'input': os.path.abspath(a.input),
        'input_kind': ('collection' if os.path.isdir(a.input)
                       else os.path.splitext(a.input)[1].lstrip('.').upper()
                       or 'image'),
        'input_sha256': _sha256(a.input),
        'output': os.path.abspath(a.output),
        'started_at': started_at,
    }
    uniform.header(say, meta, ['ActTrace'] + list(argv or sys.argv[1:]),
                   ARTIFACT_DEFS)

    root = a.input
    if os.path.isfile(a.input):
        # 이미지면 ChatTrace 의 image 층으로 먼저 꺼낸다
        from chattrace.image import extract as imgex
        from chattrace.image import formats as imgfmt
        if not imgfmt.looks_like_image(a.input):
            print('  입력이 폴더도 이미지도 아니다: %s' % a.input)
            return 2
        root = os.path.join(a.output, '_추출')
        say('  ── 디스크 이미지에서 꺼낸다 (마운트하지 않는다) ──')
        with anim.UI.stage('디스크 이미지에서 꺼내는 중') as st:
            res = imgex.extract_case(
                a.input, root, with_system=True, log=say,
                progress=lambda d, t: st.update(
                    done=d, total=t,
                    note='%s / %s' % (format(d, ','), format(t, ','))))
        say('  파일 %s개 · %.1f초'
            % (format(res.files_written, ','), res.seconds))
    elif not os.path.isdir(a.input):
        print('  입력 경로가 없다: %s' % a.input)
        return 2

    ids = Ids()
    obs = []
    # 통일 산출물의 화면을 깨끗하게 유지한다. 파서가 흘리는 한국어 진행 로그는
    # -v 를 줄 때만 낸다 — 기본 화면은 합의한 구획만 보여준다.
    plog = say if a.verbose else (lambda *_a, **_k: None)
    plog('  ── 파싱 ──')
    # (통일 출력에 쓸 정의명, 진행 표시 문구, 파서)
    STEPS = (
        ('Local Storage', 'Local Storage 를 읽는 중',
         lambda: localstorage.parse(
             root, ids, log=plog, env_profile=a.profile)),
        ('Prefetch', 'Prefetch 를 푸는 중', lambda: prefetch.parse(
            root, ids, log=plog, env_profile=a.profile,
            only_claude=not a.all_programs)),
        ('Application Log', '앱 로그를 훑는 중', lambda: applog.parse(
            root, ids, log=plog, env_profile=a.profile)),
        ('Account Identity', '계정 식별자를 모으는 중',
         lambda: identity.parse(
             root, ids, log=plog, env_profile=a.profile)),
        ('Registry', '레지스트리를 여는 중', lambda: registry.parse(
            root, ids, log=plog, env_profile=a.profile)),
        ('Install Order', '설치 순서를 맞추는 중',
         lambda: install_order.parse(
             root, ids, log=plog, env_profile=a.profile)),
        ('Browser History', '브라우저 기록을 읽는 중',
         lambda: history.parse(
             root, ids, log=plog, env_profile=a.profile)),
    )
    # ★ 파서 하나가 죽어도 끝까지 돈다. 다만 조용히 넘기지는 않는다 —
    #   실패를 error 관찰로 남겨야 '안 했다' 와 '못 봤다' 가 갈린다.
    def _step(name, fn):
        try:
            return fn()
        except Exception as e:
            say('   ! %s 파서가 멈췄다 — %s: %s' % (name, type(e).__name__, e))
            return [Observation(
                ids.next('obs'), name, '', '', 'parser_failed',
                {'error': '%s: %s' % (type(e).__name__, e)},
                parser=name, status='error', env_profile=a.profile,
                note='%s 파서가 예외로 중단됐다. 이 아티팩트는 보지 못했다'
                     % name)]

    # 정의별 집계 — 'Found: n files' 는 실제로 읽은 파일 수다.
    #   관찰 수와 파일 수는 다른 값이라 둘 다 센다.
    groups = {}
    # 경로만 다른 같은 사실을 합치는 색인. 파서 사이를 가로지른다 —
    # Local Storage 를 robocopy 사본과 KAPE 사본에서 각각 읽기 때문이다.
    _seen = {}
    _merged = [0]

    def _tally(name, got):
        got, m = _merge_dupes(got, _seen)
        _merged[0] += m
        g = groups.setdefault(name, {'files': 0, 'obs': 0, 'bad': 0,
                                     'merged': 0})
        g['merged'] += m
        g['files'] += len({o.source_path for o in got if o.source_path})
        g['obs'] += len(got)
        # problems.csv · 꼬리의 Failed 와 같은 기준으로 센다 (ok 가 아닌 것).
        # 기준이 다르면 같은 화면에 서로 다른 숫자가 나와 사람을 헷갈린다.
        g['bad'] += sum(1 for o in got if o.parser_status != 'ok')
        return got

    for k, (defname, title, fn) in enumerate(STEPS, 1):
        with anim.UI.stage(title, total=len(STEPS)) as st:
            st.update(done=k, note='%d/%d' % (k, len(STEPS)))
            got = _step(title, fn)
        obs += _tally(defname, got)
    convs = _load_conversations(a.conversations)
    with anim.UI.stage('대화 내용을 대조하는 중'):
        got = _step('content', lambda: content.parse(
            root, ids, log=plog, env_profile=a.profile, convs=convs))
    obs += _tally('Conversation Content', got)
    if _merged[0]:
        say('   경로만 다른 같은 사실 %s건을 합쳤다' % format(_merged[0], ','))
    plog('   관찰 %s건' % format(len(obs), ','))
    if convs:
        plog('   대화 %d개를 초안·식별자 대조에 쓴다' % len(convs))

    plog('  ── 규칙 적용 ──')
    with anim.UI.stage('행위를 판정하는 중', total=len(obs)) as st:
        st.update(done=len(obs))
        infs = rules.run(obs, ids, convs)
    plog('   추론 %s건' % format(len(infs), ','))

    nbad, urows, erows, pdoc = _write(a.output, obs, infs, a.profile,
                                     meta, root)

    meta.update({
        'finished_at': _now_iso(),
        'observations': len(obs),
        'inferences': len(infs),
        'problems': nbad,
        'merged_duplicates': _merged[0],
    })
    _write_run(a.output, meta)
    _write_summary(a.output, obs, infs, meta, nbad)

    # 통일 CLI 출력 — 수집 · 분석 · 산출물 · 요약
    nfiles, nobs, nfail = uniform.collection(say, groups, ARTIFACT_DEFS)
    nact, _nd, _ns = uniform.analysis(say, urows)
    say('   팀 합의 6열 report.csv — %d줄 (판정규칙 1개 × 파일 1개)' % len(erows))
    uniform.generated(say, 'CSV', os.path.join(os.path.abspath(a.output),
                                               'LLM_Artifact_Report.csv'))
    # 원본 아티팩트 수집본 — collected_path 가 parsed_artifacts.json 과
    # 1:1 로 맞아야 '근거 -> 원본' 추적이 끊기지 않는다.
    try:
        zp, zn = bundle.write_zip(a.output, root, pdoc, log=say)
        uniform.generated(say, 'ZIP', zp)
    except Exception as e:
        say('   collected_artifacts.zip 실패: %s: %s' % (type(e).__name__, e))
    bundle.write_parsed(a.output, pdoc)
    uniform.generated(say, 'JSON', os.path.join(os.path.abspath(a.output),
                                                'parsed_artifacts.json'))

    # 사람이 브라우저로 보는 보고서 — 개요 · 행위 토글 · 아티팩트 등급
    htmlp = os.path.join(os.path.abspath(a.output),
                         'LLM_Artifact_Report.html')
    html_ok = True
    try:
        from . import report as _rep
        _rep.write_html(a.output, obs, infs, meta, erows)
    except Exception as e:
        html_ok = False
        say('   report.html 생성 실패: %s: %s' % (type(e).__name__, e))
    uniform.generated(say, 'HTML', htmlp, html_ok)
    uniform.footer(say, meta, ARTIFACT_DEFS, nfiles, nobs, nact, nbad,
                   time.time() - t0)

    # ── 여기까지가 팀 합의 통일 출력. 아래는 ActTrace 고유 상세다.
    _console(obs, infs, nbad, time.time() - t0, os.path.abspath(a.output))

    # 종료 코드 — 공통 명세 §7
    #   0 정상 · 1 부분 실패 · 2 입력 자체를 못 읽음
    # ★ 추론 0건은 실패가 아니다. 아무 행위도 없는 이미지라면 정상이다.
    #   '못 찾았다' 와 '실패했다' 를 섞으면 자동화에서 오판한다.
    if any(o.parser_status == 'error' for o in obs):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
