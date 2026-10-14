# -*- coding: utf-8 -*-
r"""팀 합의 산출물 4종 (2026-10-13).

    collected_artifacts.zip    원본 아티팩트 수집본 + 수집 대장
    parsed_artifacts.json      파싱값 (근거가 가리키는 인덱스의 원본)
    LLM_Artifact_Report.csv    6열 표
    LLM_Artifact_Report.html   보고서

★ `parsed_artifacts.json` 이 핵심이다. 다른 조의 근거 표기
  `artifacts[12].records[99].value.created_at="..."` 는 **이 파일의
  인덱스**를 가리킨다. 이 파일을 내야 우리 근거도 같은 표기를 쓸 수 있다.
  전에는 중간 파일이 없어 관찰 id(`obs:0847`)로 대신했는데, 그러면
  남의 도구로는 못 따라간다.

구조 (schema_version 1.2 — 다른 조와 맞춘 것)

    {
      "schema_version": "1.2",
      "source": {"mode": "collection|E01", "path": "..."},
      "collection_manifest": "_collection_manifest.json",
      "artifacts": [
        {
          "artifact":        "Local Storage",            친근한 이름
          "source_path":     "C:\\Users\\...\\leveldb",   원본 절대경로
          "collected_path":  "C/Users/.../leveldb",       zip 안 경로
          "parser": "...", "status": "success|partial|error",
          "records": [ {...} ],          관찰 하나 = 레코드 하나
          "source_location": [ {...} ]   레코드별 원본 위치
        }
      ]
    }

한 아티팩트 = (아티팩트 종류, 원본 경로) 한 쌍이다. 같은 LevelDB 를
여러 관찰이 가리켜도 아티팩트는 하나고 records 가 여럿이다.
"""
from __future__ import print_function

import io
import json
import os
import zipfile

from . import evidence

SCHEMA = '1.2'

SEP = chr(92)       # 역슬래시

#: 내부 아티팩트 이름 -> 보고서에 쓰는 이름
FRIENDLY = {
    'localstorage': 'Local Storage',
    'indexeddb': 'IndexedDB',
    'sessionstorage': 'Session Storage',
    'cookies': 'Cookies',
    'applog': 'Application Log',
    'prefetch': 'Prefetch',
    'mft': '$MFT',
    'bam': 'BAM',
    'userassist': 'UserAssist',
    'registry': 'Registry',
    'browser_history': 'Browser History',
    'identity_dir': 'Account Identity Directory',
    'plan_usage': 'plan-usage-history.json',
}

STATUS = {'ok': 'success', 'partial': 'partial', 'error': 'error'}


def _friendly(name):
    return FRIENDLY.get(name, name)


def _zip_path(abs_path):
    r"""원본 절대경로 -> zip 안 경로. `C:\Users\x` -> `C/Users/x`.

    수집물 안의 `app\` / `kape\` 를 그대로 쓰면 수집 도구마다 달라져
    다른 조와 대조가 안 된다. 드라이브 문자를 폴더로 펴서 맞춘다.
    """
    p = (abs_path or '').replace(SEP, '/')
    if len(p) > 2 and p[1] == ':':
        p = p[0].upper() + p[2:]
    return p.lstrip('/')


def build_parsed(obs, meta):
    """관찰을 parsed_artifacts.json 구조로 묶는다.

    묶는 단위는 **(아티팩트 종류, 원본 절대경로)** 다. 그래야 근거의
    `artifacts[i]` 가 '어느 파일' 을 가리키게 된다.
    """
    order = []
    groups = {}
    for o in obs:
        d = o.as_dict() if hasattr(o, 'as_dict') else dict(o)
        key = (d.get('source_artifact') or '', evidence.abs_path(d))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                'artifact': _friendly(key[0]),
                'artifact_names': [key[0]],
                'source_path': key[1],
                'collection_role': 'primary',
                # zip 안 경로는 원본 절대경로를 드라이브 폴더로 편 것이다.
                # 읽어올 자리(_src_rel)와 넣을 자리(collected_path)를
                # 갈라 둔다 — 둘을 섞으면 어느 한쪽이 틀어진다.
                'collected_path': _zip_path(key[1]),
                '_src_rel': (d.get('source_path') or ''),
                'collected_members': [],
                'collection_status': 'ok',
                'parser': d.get('parser') or key[0],
                'collection_method': meta.get('input_kind') or 'collection',
                'status': STATUS.get(d.get('parser_status'), 'success'),
                'records': [],
                'source_location': [],
                '_obs': [],
            }
            order.append(key)
        # 레코드 하나 = 관찰 하나. 시각은 레코드 안에 같이 둔다 —
        # 근거가 '값 + 그 값의 시각' 을 같이 가리켜야 하기 때문이다.
        rec = dict(d.get('fact') or {})
        for k, v in d.items():
            if k.startswith('ts_') and v:
                rec[k] = v
        g['records'].append(rec)
        g['source_location'].append({
            'record_path': [len(g['records']) - 1],
            '_source_locator': d.get('source_locator') or '',
            'kind': 'logical',
        })
        g['_obs'].append(d.get('id'))
        if d.get('parser_status') == 'error':
            g['status'] = 'error'
        elif d.get('parser_status') == 'partial' and g['status'] == 'success':
            g['status'] = 'partial'

    arts = []
    index = {}          # 관찰 id -> (아티팩트 인덱스, 레코드 인덱스)
    for i, key in enumerate(order):
        g = groups[key]
        ids = g.pop('_obs')
        for j, oid in enumerate(ids):
            index[oid] = (i, j)
        arts.append(g)

    doc = {
        'schema_version': SCHEMA,
        'source': {'mode': meta.get('input_kind') or 'collection',
                   'path': meta.get('input') or ''},
        'collection_manifest': '_collection_manifest.json',
        'tool': '%s %s' % (meta.get('tool') or '',
                           meta.get('tool_version') or ''),
        'artifacts': arts,
    }
    return doc, index


def write_parsed(outdir, doc):
    # _src_rel 은 zip 을 묶을 때만 쓰는 내부 값이다. 직렬화 직전에 뺀다 —
    # build_parsed 에서 빼면 write_zip 이 읽을 자리를 잃는다.
    for a in doc.get('artifacts', []):
        a.pop('_src_rel', None)
    p = os.path.join(outdir, 'parsed_artifacts.json')
    # JSON 에는 BOM 을 넣지 않는다 — 파서가 BOM 에서 죽는다
    with io.open(p, 'w', encoding='utf-8') as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=1))
    return p


def write_zip(outdir, root, doc, log=None):
    r"""원본 아티팩트를 zip 으로 묶는다 + 수집 대장을 같이 넣는다.

    parsed_artifacts.json 의 `collected_path` 가 zip 안 경로와 1:1 로
    맞아야 한다. 안 맞으면 '근거 -> 원본' 추적이 끊긴다.

    ★ 여기 들어가는 것은 **수집물 안의 원본 파일**이다. 우리가 만든
      분석 결과가 아니다.
    """
    p = os.path.join(outdir, 'collected_artifacts.zip')
    want = {}
    for a in doc['artifacts']:
        cp = a.get('collected_path') or ''
        if cp:
            want[cp] = a
    n = 0
    skipped = []
    with zipfile.ZipFile(p, 'w', zipfile.ZIP_DEFLATED) as z:
        for cp, a in sorted(want.items()):
            rel0 = a.get('_src_rel') or cp
            src = os.path.join(root, rel0.replace('/', os.sep))
            if os.path.isdir(src):
                members = []
                for dp, dn, fns in os.walk(src):
                    for fn in sorted(fns):
                        full = os.path.join(dp, fn)
                        sub = os.path.relpath(full, src).replace(os.sep, '/')
                        arc = '%s/%s' % (cp.rstrip('/'), sub)
                        try:
                            z.write(full, arc)
                            members.append(arc)
                            n += 1
                        except OSError:
                            skipped.append(arc)
                a['collected_members'] = members
            elif os.path.isfile(src):
                try:
                    z.write(src, cp)
                    a['collected_members'] = [cp]
                    n += 1
                except OSError:
                    skipped.append(cp)
                    a['collection_status'] = 'read_error'
            else:
                a['collection_status'] = 'missing'
                skipped.append(cp)
        manifest = {
            'tool': doc.get('tool'),
            'source': doc.get('source'),
            'artifact_count': len(doc['artifacts']),
            'file_count': n,
            'skipped': skipped[:50],
            'note': 'collected_path 가 parsed_artifacts.json 의 같은 필드와 '
                    '1:1 로 맞는다. 근거 -> 원본 추적의 다리다.',
        }
        z.writestr('_collection_manifest.json',
                   json.dumps(manifest, ensure_ascii=False, indent=1))
    if log:
        log('   collected_artifacts.zip — 파일 %s개%s'
            % (format(n, ','),
               (' · 건너뜀 %d' % len(skipped)) if skipped else ''))
    return p, n
