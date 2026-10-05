# -*- coding: utf-8 -*-
r"""팀 합의 통일 산출물 — 공통 CSV 열 · 공통 CLI 출력 · 아티팩트 등급.

2026-10-05 팀 결정

    cli 입력   이미지 · 환경 · 저장 경로
    csv        행위 분류 | 아티팩트 | 경로 | 아티팩트 등급
    html       개요(환경) · 행위 토글 · 아티팩트 | 경로 | 아티팩트 등급
    아티팩트 등급   직접 / 보조

'직접' 과 '보조' 를 사람 느낌으로 가르면 같은 입력에서 사람마다 다른 표가
나온다. 그러면 통일한 의미가 없다. 그래서 기계가 따질 수 있는 조작적
정의를 박아 둔다.

    직접  그 아티팩트 **한 종류만으로** 확정(confirmed) 판정이 선 경우.
          다른 아티팩트를 끌어올 필요가 없었다 → 행위와 직접 귀속.

    보조  그 밖 전부.
            · 여러 아티팩트를 함께 봐야 결론이 선 경우
            · 확정 아래 등급(유력 · 추정 · 판정불가)인 경우

한 아티팩트가 같은 행위에서 두 역할을 다 했다면 '직접' 을 적는다. 더 센
쪽을 적어야 '이 경로만 봐도 된다' 는 사실이 표에서 사라지지 않는다.

★ 이 4열 표는 **요약**이다. 어느 추론이 어느 관찰에서 나왔는지는
  behaviors.csv 의 supported_by · source_artifacts 와
  observations.jsonl 에 그대로 남는다. 통일 열 때문에 추적성을 버리지
  않는다.
"""
from __future__ import print_function

import csv
import io
import os
import platform
import socket

DIRECT = '직접'
SUPPORT = '보조'

#: 팀 합의 열 제목. 순서까지 합의 사항이다 — 바꾸면 통일이 깨진다.
CSV_COLS = ['행위 분류', '아티팩트', '경로', '아티팩트 등급']

GRADE_DEF = {
    DIRECT: '그 아티팩트 한 종류만으로 확정 판정이 선 경우 (행위와 직접 귀속)',
    SUPPORT: '다른 아티팩트와 함께 봐야 하거나, 확정 아래 등급인 경우 '
             '(추측 · 정황상 증거)',
}


def grade(confidence, n_artifacts):
    """추론 하나가 아티팩트 하나에 주는 등급."""
    if confidence == 'confirmed' and n_artifacts == 1:
        return DIRECT
    return SUPPORT


def _names():
    """B 코드 -> 표준 행위 이름.

    규칙이 추론마다 제 나름의 이름을 붙이는 경우가 있다 ('윈도우 사용자
    귀속' 처럼). 그걸 그대로 '행위 분류' 에 쓰면 같은 B01 이 세 갈래로
    쪼개져 분류가 아니게 된다. 통일 표에는 **표준 이름만** 쓴다.
    추론별 이름은 behaviors.csv 의 behavior_name 에 그대로 남는다.
    """
    from .rules import BEHAVIORS            # 순환 임포트를 피해 늦게 받는다
    return dict((k, v[0]) for k, v in BEHAVIORS.items())


def rows(obs, infs, behav_order=None, names=None):
    """(행위 · 아티팩트 · 경로) 하나를 한 줄로 모은다.

    같은 묶음에 추론이 여러 개 붙으면 등급은 더 센 쪽(직접)을 쓴다.
    """
    if names is None:
        try:
            names = _names()
        except Exception:
            names = {}
    art = {}
    for o in obs:
        art[o.id] = (o.source_artifact, o.source_path)

    acc = {}
    for i in infs:
        d = i.as_dict() if hasattr(i, 'as_dict') else dict(i)
        pairs = []
        for oid in (d.get('supported_by') or []):
            p = art.get(oid)
            if p and p not in pairs:
                pairs.append(p)
        kinds = set(a for a, _ in pairs)
        g = grade(d.get('confidence'), len(kinds))
        bid = d.get('behavior_id') or ''
        label = ('%s %s' % (bid, names.get(bid)
                            or d.get('behavior_name') or '')).strip()
        for a, p in pairs:
            key = (bid, a, p)
            cur = acc.get(key)
            if cur is None:
                cur = acc[key] = {'b': bid, '행위 분류': label,
                                  '아티팩트': a, '경로': p,
                                  '아티팩트 등급': g,
                                  'infs': [], 'rules': []}
            elif g == DIRECT:
                cur['아티팩트 등급'] = DIRECT
            cur['infs'].append(d.get('id'))
            r = d.get('rule_id')
            if r and r not in cur['rules']:
                cur['rules'].append(r)

    order = list(behav_order or sorted(names))

    def k(r):
        try:
            bi = order.index(r['b'])
        except ValueError:
            bi = len(order)
        return (bi, r['b'], 0 if r['아티팩트 등급'] == DIRECT else 1,
                r['아티팩트'], r['경로'])

    return sorted(acc.values(), key=k)


def write_csv(outdir, rws):
    """report.csv — 팀 합의 4열. 다른 환경의 표와 그대로 맞춰 본다."""
    p = os.path.join(outdir, 'report.csv')
    # .csv 는 BOM 을 넣는다 (공통 명세 인코딩 규약). 없으면 엑셀·
    # PowerShell 이 cp949 로 읽어 한글이 깨진다.
    with io.open(p, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction='ignore')
        w.writeheader()
        for r in rws:
            w.writerow(r)
    return p


# ── 통일 CLI 출력 ────────────────────────────────────────────────
# 팀이 맞춘 구획 이름은 영문 그대로 둔다. 다른 환경의 출력과 줄 단위로
# 맞춰 봐야 하므로 번역하면 대조가 안 된다. 우리 도구 고유의 상세는
# 이 블록 **뒤에** 한국어로 따로 낸다.

def header(say, meta, argv, defs):
    say('%s v%s' % (meta.get('tool', ''), meta.get('tool_version', '')))
    say('Author: WHS ChatTrace/ActTrace')
    say('')
    say('Command line:')
    say('  %s' % ' '.join(argv))
    say('')
    say('System info:')
    say('  Machine name: %s' % (socket.gethostname() or '-'))
    say('  64-bit: %s' % (platform.machine().endswith('64')))
    say('  User: %s' % (os.environ.get('USERNAME') or
                        os.environ.get('USER') or '-'))
    say('  OS: %s %s' % (platform.system(), platform.release()))
    say('')
    say('Source info:')
    say('  Type: %s' % meta.get('input_kind', '-'))
    say('  Source: %s' % meta.get('input', '-'))
    say('  SHA-256: %s' % meta.get('input_sha256', '-'))
    say('')
    say('Output info:')
    say('  Path: %s' % meta.get('output', '-'))
    say('')
    say('Target:')
    say('  Claude Desktop (%s)' % meta.get('env_profile', '-'))
    say('')
    say('Loading artifact definitions...')
    say('  Found %d artifact definitions.' % len(defs))
    say('')


def collection(say, groups, defs):
    """아티팩트 정의별 수집 결과.

    groups = {정의명: {'files': n, 'obs': n, 'bad': n}}
    """
    say('Artifact collection:')
    tot_f = tot_o = tot_b = 0
    for k, nm in enumerate(defs, 1):
        g = groups.get(nm) or {}
        f, o, b = g.get('files', 0), g.get('obs', 0), g.get('bad', 0)
        tot_f += f
        tot_o += o
        tot_b += b
        say('  [%d/%d] %s' % (k, len(defs), nm))
        say('        Found: %d files' % f)
        if b:
            say('        Failed: %d' % b)
    say('')
    say('Artifact extraction completed.')
    say('  Artifact definitions : %d' % len(defs))
    say('  Files found          : %d' % tot_f)
    say('  Observations         : %d' % tot_o)
    say('  Extraction failures  : %d' % tot_b)
    say('')
    return tot_f, tot_o, tot_b


def analysis(say, rws):
    """행위별 직접/보조 증거 수. report.csv 의 줄 수와 정확히 같다."""
    say('Analyzing user actions...')
    say('')
    seen = []
    agg = {}
    for r in rws:
        key = r['행위 분류']
        if key not in agg:
            agg[key] = {DIRECT: 0, SUPPORT: 0}
            seen.append(key)
        agg[key][r['아티팩트 등급']] += 1
    for k, key in enumerate(seen, 1):
        say('  [%d] %s' % (k, key))
        say('      Direct evidence     : %d' % agg[key][DIRECT])
        say('      Supporting evidence : %d' % agg[key][SUPPORT])
        say('')
    d = sum(a[DIRECT] for a in agg.values())
    s = sum(a[SUPPORT] for a in agg.values())
    say('User action analysis completed.')
    say('  Actions detected    : %d' % len(seen))
    say('  Direct evidence     : %d' % d)
    say('  Supporting evidence : %d' % s)
    say('')
    return len(seen), d, s


def generated(say, label, path, ok=True):
    say('Generating %s report...' % label)
    say('  %s' % path)
    say('  Status: %s' % ('OK' if ok else 'FAILED'))
    say('')


def footer(say, meta, defs, nfiles, nobs, nact, nbad, secs):
    say('Execution summary:')
    say('  Target               : Claude Desktop')
    say('  Artifact definitions : %d' % len(defs))
    say('  Files found          : %d' % nfiles)
    say('  Observations         : %d' % nobs)
    say('  Actions detected     : %d' % nact)
    say('  Failed               : %d' % nbad)
    say('')
    say('Total execution time: %.2f sec' % secs)
    say('')
