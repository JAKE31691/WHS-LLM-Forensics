#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ChatTrace — Claude Desktop 대화 복원 CLI.

우선순위 복원 알고리즘
    ① 전체 세션    LevelDB 레코드 → V8 디코드 → 대화 트리 (외부 blob 추적)
    ② 단일 파편    트리 파싱 실패분에서 메시지/응답 객체만 회수
    ③ 고유문자열   실험용 마커의 정확한 일치만 (--marker)

각 단계는 앞 단계가 덮지 못한 데이터만 상대한다.

프롬프트 정규식 카빙은 폐기했다. 실측에서 실제 사용자 프롬프트는 0건이고
잡힌 103건이 전부 앱 내장 시스템 템플릿이었으며, 영어 프롬프트는 20개 중
2개만 걸려 "없다" 는 잘못된 결론을 부른다. 판단은 ① 구조 복원으로 한다.

    python ChatTrace -i <아티팩트_경로> -o <출력_경로> --target-mode all
"""
from __future__ import print_function

import argparse
import codecs
import datetime
import io
import json
import logging
import os
import sys

from . import anim, blobscan, carve, leveldb, report, scenarios, timeline
from .codec import Truncated, decode_ssv

LOG = logging.getLogger('chattrace')

LEVELDB_EXT = ('.log', '.ldb', '.sst')
SKIP_EXT = ('.png', '.jpg', '.jpeg', '.gif', '.ico', '.woff', '.woff2',
            '.ttf', '.dll', '.exe', '.pak', '.bin.map')
MAX_SCAN = 256 << 20                    # 파일 하나당 스캔 상한


# ────────────────────────────────────────────────────────────────
# 입력 탐색 — KAPE 출력이든 앱 폴더든 평평한 폴더든 받는다
# ────────────────────────────────────────────────────────────────
def discover(root):
    """-i 아래를 훑어 LevelDB 저장소 / 일반 파일 / blob 후보를 분류한다."""
    stores, files = [], []
    for dp, dn, fns in os.walk(root):
        dn[:] = [d for d in dn if d.lower() not in ('$recycle.bin',)]
        has_ldb = any(f.endswith(LEVELDB_EXT) for f in fns)
        if has_ldb:
            stores.append(dp)
        for f in fns:
            p = os.path.join(dp, f)
            if f.lower().endswith(SKIP_EXT):
                continue
            try:
                if os.path.getsize(p) == 0:
                    continue
            except OSError:
                continue
            files.append(p)
    return stores, files


# 대화 흔적이 실제로 나오는 경로. R5 실측: 파일 1,651개를 훑어서 소견이
# 나온 건 5개뿐이었고 전부 아래 경로였다. Cache 96개 / Code Cache 1,208개는
# 0건. 그래서 기본은 이쪽만 본다.
HIGH_YIELD = ('indexeddb', 'session storage', 'local storage',
              'network', 'blob_storage', 'sentry', 'logs', 'outside')


def filter_scope(files, root, scope):
    """스캔 대상을 고수익 경로로 좁힌다. -> (대상, 건너뛴수)"""
    if scope == 'everything':
        return files, 0
    keep = []
    for p in files:
        rel = os.path.relpath(p, root).lower().replace('\\', '/')
        if any(k in rel for k in HIGH_YIELD):
            keep.append(p)
    return keep, len(files) - len(keep)


def store_label(path, root):
    rel = os.path.relpath(path, root).replace('\\', '/')
    low = rel.lower()
    for key, name in (('session storage', 'session'),
                      ('local storage', 'local'),
                      ('indexeddb', 'indexeddb'),
                      ('gpucache', 'gpucache')):
        if key in low:
            return name
    return rel


# ────────────────────────────────────────────────────────────────
# ① 전체 세션
# ────────────────────────────────────────────────────────────────
def tier1_sessions(stores, root, blobs, verify_crc=False, stage=None):
    """대화 트리 단위 복원. 살아 있는 것 + 삭제/덮어쓰기된 과거 버전 전부."""
    rows, covered, stats = [], set(), {'records': 0, 'stores': 0}
    for _i, sp in enumerate(stores):
        if stage is not None:
            stage.update(done=_i, note=os.path.basename(sp)[:28])
        try:
            recs, errs = leveldb.read_store(sp, verify_crc, LOG)
        except OSError as e:
            LOG.warning('%s 읽기 실패 — %s', sp, e)
            continue
        if not recs:
            continue
        stats['stores'] += 1
        stats['records'] += len(recs)
        label = store_label(sp, root)
        live = leveldb.live_view(recs)
        shadow = leveldb.shadowed(recs)
        LOG.info('  [%s] 레코드 %d · 유효키 %d · 삭제/덮어쓰기 %d',
                 label, len(recs), len(live), len(shadow))

        work = [(r, 'live') for r in live.values()]
        work += [(r, st) for r, st in shadow if r['op'] == 'put']
        for r, state in work:
            meta = {'store': label, 'state': state, 'file': r['file'],
                    'offset': r['offset'], 'seq': r['seq'],
                    'key': leveldb.idb_decode_key(r['key']),
                    'key_kind': leveldb.idb_kind(r['key'])}
            try:
                items, _ok = scenarios.extract(r['value'], meta, blobs)
            except Exception as e:
                LOG.debug('레코드 추출 실패 seq=%s — %s', r['seq'], e)
                continue
            for it in items:
                if it.get('item') in ('conversation', 'message', 'attachment'):
                    it['tier'] = 1
                    rows.append(it)
                    t = (it.get('text') or '').strip()
                    if len(t) >= 8:
                        covered.add(t[:200])
    return rows, covered, stats


# ────────────────────────────────────────────────────────────────
# ② 단일 메시지 파편
# ────────────────────────────────────────────────────────────────
def scan_files(files, root, covered, markers=(), decompress=True,
               want_fragments=True, stage=None):
    """②③ 한 패스.

    압축 해제는 파일당 한 번만 한다. ②와 ③에서 따로 돌리면 999MB 수집물
    기준으로 같은 일을 두 번 하게 되고, 실제로 2분을 넘겼다.
    """
    frags, prompts = [], []
    total = len(files)
    for i, p in enumerate(files, 1):
        if stage is not None:
            stage.update(done=i, note='파편 %d · 마커 %d' % (len(frags), len(prompts)))
        elif i % 200 == 0 or i == total:
            LOG.info('   진행 %d/%d  (파편 %d · 마커 %d)',
                     i, total, len(frags), len(prompts))
        buf = _read(p)
        if buf is None:
            continue
        src = os.path.relpath(p, root)
        vs = blobscan.views(buf, decompress)          # ← 단 한 번
        if want_fragments:
            frags += _fragments_from(vs, p, root, covered)
        # ③ 프롬프트 카빙은 제거했다.
        # 실측 결과 실제 사용자 프롬프트는 0건이었고, 잡힌 103건은 전부
        # 앱에 내장된 시스템 템플릿(<mobile_session>, 스킬 정의 등)이었다.
        # 영어 프롬프트는 20개 중 2개만 걸려 "없다" 는 잘못된 결론을 부른다.
        # 마커 추적은 남긴다 — 그건 정확하다.
        prompts += _markers_from(buf, src, p, root, markers)
    return frags, prompts


def _fragments_from(vs, p, root, covered):
    """뷰마다 엄격 디코드를 시도해 메시지 객체만 건진다."""
    rows = []
    for vname, view, remap in vs:
        try:
            obj = decode_ssv(view)
        except (Truncated, Exception):
            continue
        if not isinstance(obj, (dict, list)):
            continue
        for d in scenarios.walk(obj):
            kind = scenarios.classify(d)
            if kind not in ('message', 'conversation', 'attachment'):
                continue
            txt = scenarios._txt(d.get('text') or d.get('content'))
            if len(txt.strip()) < 8 or txt[:200] in covered:
                continue
            covered.add(txt[:200])
            rows.append({
                'tier': 2, 'item': kind, 'store': store_label(p, root),
                'state': 'fragment', 'view': vname,
                'file': os.path.relpath(p, root), 'offset': remap(0),
                'uuid': d.get('uuid') or '', 'sender': d.get('sender') or '',
                'title': d.get('name') or '',
                'created_at': d.get('created_at') or d.get('start_timestamp') or '',
                'text': txt, 'decode': 'strict-fragment', 'confidence': 70,
            })
    return rows


# ────────────────────────────────────────────────────────────────
# ③ 프롬프트
# ────────────────────────────────────────────────────────────────
def _markers_from(buf, src, p, root, markers):
    """지정한 고유 문자열만 찾는다. 정규식 추측 없이 정확한 일치만."""
    rows = []
    if not markers:
        return rows
    for mh in carve.scan(buf, src, patterns={}, markers=list(markers)):
        rows.append({
            'tier': 3, 'item': 'marker', 'store': store_label(p, root),
            'state': 'carved', 'view': mh['encoding'], 'rule': 'marker',
            'confidence': 100, 'file': src, 'offset': mh['offset'],
            'uuid': '', 'sender': '', 'title': '',
            'created_at': '', 'text': mh['value'], 'decode': 'carve',
        })
    return rows


DOC_EXT = ('.md', '.rst', '.txt', '.html', '.htm', '.markdown')
DOC_HINT = ('skills-plugin', 'skill-creator', 'resources', 'docs',
            'node_modules', 'locales')


def _is_doc(src):
    """앱이 설치 시 깔아 둔 문서/템플릿인가 — 사용자 데이터가 아니다."""
    low = src.lower().replace('\\', '/')
    return low.endswith(DOC_EXT) or any(h in low for h in DOC_HINT)


def _read(path):
    try:
        if os.path.getsize(path) > MAX_SCAN:
            LOG.warning('%s 너무 큼 — 건너뜀', path)
            return None
        with open(path, 'rb') as f:
            return f.read()
    except OSError as e:
        LOG.warning('%s 읽기 실패 — %s', path, e)
        return None


# ────────────────────────────────────────────────────────────────
# 대화 쌍 구성
# ────────────────────────────────────────────────────────────────
def build_pairs(rows):
    """대화 단위로 묶고 human/assistant 를 쌍으로 만든다."""
    convs = {}
    for r in rows:
        if r.get('item') == 'conversation':
            u = r.get('uuid') or ''
            cur = convs.get(u)
            if not cur or (r.get('msg_count') or 0) > (cur.get('msg_count') or 0):
                convs[u] = {'uuid': u, 'title': r.get('title') or '',
                            'model': r.get('model') or '',
                            'created_at': r.get('created_at') or '',
                            'state': r.get('state'),
                            'msg_count': r.get('msg_count') or 0,
                            'declared_bytes': r.get('declared_bytes'),
                            'declared_msgs': r.get('declared_msgs'),
                            'messages': []}
    for r in rows:
        if r.get('item') != 'message':
            continue
        u = r.get('conversation') or r.get('uuid') or ''
        c = convs.setdefault(u, {'uuid': u, 'title': r.get('title') or '',
                                 'model': '', 'created_at': '', 'state': r.get('state'),
                                 'msg_count': 0, 'messages': []})
        c['messages'].append({'index': r.get('index'), 'sender': r.get('sender'),
                              'text': r.get('text'), 'uuid': r.get('uuid'),
                              'created_at': r.get('created_at'),
                              'state': r.get('state'), 'tier': r.get('tier')})
    pairs = []
    for c in convs.values():
        seen, uniq = set(), []
        for m in c['messages']:
            k = (m['sender'], (m['text'] or '')[:200])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(m)
        uniq.sort(key=lambda m: m['index'] if isinstance(m['index'], int) else 1 << 30)
        c['messages'] = uniq
        c['recovered'] = len(uniq)
        # 레코드가 신고한 크기와 대비할 실제 복원 바이트
        c['recovered_bytes'] = sum(
            len((m.get('text') or '').encode('utf-8')) for m in uniq)
        q = None
        for m in uniq:
            if m['sender'] == 'human':
                q = m
            elif m['sender'] == 'assistant' and q is not None:
                pairs.append({'conversation': c['uuid'], 'title': c['title'],
                              'prompt': q['text'], 'response': m['text'],
                              'prompt_at': q['created_at'],
                              'state': c['state']})
                q = None
    return list(convs.values()), pairs


def find_mft(root):
    """수집물 안에서 $MFT 를 찾는다. KAPE 는 <dest>\\C\\$MFT 로 한 단계 더 들어간다."""
    for dp, dn, fns in os.walk(root):
        for f in fns:
            if f == '$MFT':
                return os.path.join(dp, f)
    return None


def find_logdir(root):
    r"""앱 로그 폴더를 찾는다.

    컨테이너 밖(%LOCALAPPDATA%\Claude\logs)에 있으므로 수집물에서는
    outside\logs-local\logs 같은 자리에 들어온다. 이름이 logs 이고
    .log 가 들어 있는 폴더를 전부 후보로 본다.
    """
    found = []
    for dp, dn, fns in os.walk(root):
        base = os.path.basename(dp).lower()
        if base == 'logs' and any(f.endswith('.log') for f in fns):
            found.append(dp)
    if not found:
        return None
    # leveldb 의 000003.log 가 아니라 진짜 앱 로그를 고른다
    found.sort(key=lambda d: (0 if 'leveldb' not in d.lower() else 1, len(d)))
    return found[0]


def build_timeline(a, stores, rows):
    """$MFT · LevelDB 시퀀스 · 앱 로그 · 복원된 대화를 하나의 시간축으로."""
    LOG.info('── 타임라인 ──')
    groups = []

    ceilings = {}
    mft_path = a.mft or (None if a.mft_csv else find_mft(a.input))
    if mft_path or a.mft_csv:
        LOG.info('   $MFT %s', a.mft_csv or mft_path)
        groups.append(timeline.mft_events(mft_path, LOG, a.mft_csv, ceilings))
        if ceilings:
            LOG.info('   파일 변경시각 천장 %d개 확보', len(ceilings))
    else:
        LOG.warning('   $MFT 를 못 찾았다 — 파일 시각 없이 진행한다')

    logdir = find_logdir(a.input)
    if logdir:
        groups.append(timeline.log_events(logdir, LOG))

    groups.append(timeline.chat_events(rows, LOG))

    # 삭제 시각 추정 — IndexedDB 저장소의 레코드가 필요하다
    dels = []
    for st in stores:
        try:
            recs, _e = leveldb.read_store(st, False, LOG)
        except OSError:
            continue
        if not recs:
            continue
        anchors = timeline.build_anchors(recs)
        if not anchors:
            continue
        LOG.info('   [%s] 시각 기준점 %d개', store_label(st, a.input), len(anchors))
        dels += timeline.deletion_estimates(recs, anchors, rows, LOG, ceilings)

    ev = timeline.merge(*groups)
    LOG.info('   사건 %s건 · 삭제 추정 %s건',
             format(len(ev), ','), format(len(dels), ','))
    return ev, dels


# ────────────────────────────────────────────────────────────────
def _explain_missing(path):
    """경로가 없을 때, 왜 없는지와 뭘 하면 되는지까지 말한다.

    '경로가 없다' 만 내면 오타인지 폴더를 잘못 짚은 건지 알 수 없다.
    이미지 파일처럼 보이면 같은 폴더에 있는 이미지를 찾아 알려 준다.
    """
    LOG.error('입력 경로가 없다: %s', path)
    ext = os.path.splitext(path)[1].lower()
    parent = os.path.dirname(os.path.abspath(path)) or '.'

    if not os.path.isdir(parent):
        LOG.error('   상위 폴더도 없다: %s', parent)
        LOG.error('   드라이브 문자와 폴더 이름을 확인한다.')
        return

    try:
        from .image.formats import looks_like_image
        cand = sorted(n for n in os.listdir(parent)
                      if looks_like_image(os.path.join(parent, n)))
    except Exception:
        cand = []

    if cand:
        LOG.error('   이 폴더에 있는 이미지 파일:')
        for n in cand[:10]:
            LOG.error('     %s', os.path.join(parent, n))
        if len(cand) > 10:
            LOG.error('     ... 외 %d개', len(cand) - 10)
    elif ext:
        LOG.error('   이 폴더(%s)에는 이미지 파일이 없다.', parent)
        LOG.error('   경로가 예시 그대로는 아닌지 확인한다.')
    else:
        LOG.error('   수집물 폴더나 이미지 파일(E01/dd/vmdk)을 지정한다.')


def _from_image(a):
    """입력이 디스크 이미지면 꺼내서 a.input 을 그 폴더로 바꾼다.

    -> None 이면 계속 진행, 정수면 그 코드로 종료.
    이미지가 아닌 보통 파일이면 아무것도 하지 않고 None 을 돌려준다.
    """
    try:
        from .image import formats as imgfmt
    except ImportError as e:
        LOG.error('이미지 모듈을 불러오지 못했다: %s', e)
        return 2

    if not imgfmt.looks_like_image(a.input):
        LOG.error('입력이 폴더가 아니다. 디스크 이미지도 아닌 것 같다: %s', a.input)
        LOG.error('폴더(수집물)나 이미지 파일(E01/dd/vmdk)을 지정한다.')
        return 2

    from .image import extract as imgex

    # 파티션 목록만 보고 끝
    if a.list_partitions:
        from .image.partition import read_partitions
        img = imgfmt.open_image(a.input)
        try:
            print()
            print('  %s' % imgfmt.describe(img))
            for k, v in sorted(img.metadata().items()):
                print('  %s: %s' % (k, v))
            print()
            print('  %-3s %-14s %-8s %12s  %s'
                  % ('번호', '종류', '파일시스템', '크기(MB)', '이름'))
            print('  ' + '-' * 60)
            for p in read_partitions(img):
                print('  %-3d %-14s %-8s %12.1f  %s'
                      % (p.index, p.type_name, p.fs or '-',
                         p.size / 1048576.0, p.label or ''))
            print()
            print('  분석할 파티션을 직접 고르려면  --partition <번호>')
            print('  생략하면 윈도우가 설치된 가장 큰 NTFS 를 스스로 고른다.')
            print()
        finally:
            img.close()
        return 0

    dest = a.extract_to or os.path.join(a.output, '_추출')
    marker = os.path.join(dest, 'extract_custody.json')
    if a.reuse_extract and os.path.exists(marker):
        LOG.info('── 이미 꺼내 둔 것을 쓴다: %s ──', dest)
        a.input = dest
        return None

    LOG.info('── 디스크 이미지에서 꺼낸다 (마운트하지 않는다) ──')
    os.makedirs(dest, exist_ok=True)

    with anim.UI.stage('디스크 이미지에서 꺼내는 중') as st:
        def _p(done, total):
            st.update(done=done, total=total,
                      note='%s / %s' % (format(done, ','), format(total, ',')))
            if not anim.UI.enabled and total and done % 40000 == 0:
                LOG.info('      %s / %s', format(done, ','), format(total, ','))

        try:
            res = imgex.extract_case(
                a.input, dest, partition=a.partition,
                with_app=a.with_app, with_system=a.with_system,
                include_noise=a.include_noise, exclude=a.exclude,
                log=lambda s: LOG.info('%s', s), progress=_p)
        except Exception as e:
            LOG.error('이미지에서 꺼내지 못했다: %s: %s', type(e).__name__, e)
            LOG.error('파티션 목록을 먼저 보려면 --list-partitions')
            return 3

    LOG.info('   파일 %s개 · %s B · %.1f초',
             format(res.files_written, ','),
             format(res.bytes_written, ','), res.seconds)
    if res.deleted:
        LOG.info('   삭제된 파일 %d개 — deleted_files.csv', len(res.deleted))
    if res.problems:
        # 못 읽은 것을 조용히 넘기지 않는다. 0 바이트를 내용인 척 쓰는 것이
        # 제일 나쁘다.
        kinds = {}
        for p in res.problems:
            kinds[p['kind']] = kinds.get(p['kind'], 0) + 1
        LOG.info('   읽지 못한 항목 %d개: %s', len(res.problems),
                 ' · '.join('%s %d' % kv for kv in sorted(kinds.items())))
    if res.files_written == 0:
        LOG.error('   이 파티션에서 Claude 아티팩트를 찾지 못했다.')
        LOG.error('   --list-partitions 로 목록을 보고 --partition 으로 지정해 본다.')
        return 4

    a.input = dest
    if a.extract_only:
        LOG.info('── 꺼내기만 하고 끝낸다: %s ──', dest)
        return 0
    return None


def _fix_console():
    """콘솔 인코딩 때문에 죽는 일을 막는다.

    ChatTrace.cmd 는 PYTHONUTF8 을 켜 주지만, python -m chattrace 로 직접
    부르거나 다른 셸에서 부르면 cp949 로 떨어져 한글 도움말에서 그대로
    죽는다. 도구가 인코딩 때문에 죽으면 안 된다.
    """
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def main(argv=None):
    _fix_console()
    ap = argparse.ArgumentParser(
        prog='ChatTrace',
        description='ChatTrace - Claude Desktop 대화 복원 '
                    '(전체세션 → 파편 → 프롬프트 순 fallback)')
    if argv is None:
        argv = sys.argv[1:]
    if '--selftest' in argv:
        from .image.selftest import run as _selftest
        return 0 if _selftest() == 0 else 1
    if '--demo-anim' in argv:
        anim._demo()
        return 0
    ap.add_argument('--selftest', action='store_true',
                    help='파서 자체 점검만 하고 끝낸다')
    ap.add_argument('--demo-anim', action='store_true',
                    help='진행 애니메이션만 시험해 보고 끝낸다')
    # 통일 입력 형식 (팀 합의) — 이미지 · 환경 · 저장 경로 를 순서대로.
    #   ChatTrace <이미지|수집물> <환경> <저장경로>
    # 기존 -i/-o 도 그대로 받는다.
    ap.add_argument('args', nargs='*', metavar='인자',
                    help='<이미지|수집물> [<환경>] <저장경로> 순서로 줘도 된다')
    ap.add_argument('-i', '--input',
                    help='아티팩트 경로 (KAPE 출력 루트 / Claude 폴더 / 평폴더) '
                         '또는 디스크 이미지 파일 (E01 / dd / vmdk)')
    ap.add_argument('-o', '--output', help='출력 경로')
    ap.add_argument('--profile', default=None,
                    help='환경 프로파일 (공통 명세 §1 의 코드. 결과에 기록된다)')
    # ── 디스크 이미지 ──────────────────────────────────────
    ap.add_argument('--partition', type=int,
                    help='이미지에서 볼 파티션 번호. 생략하면 윈도우가 설치된 '
                         '가장 큰 NTFS 를 스스로 고른다')
    ap.add_argument('--list-partitions', action='store_true',
                    help='이미지의 파티션 목록만 보고 끝낸다')
    ap.add_argument('--extract-to',
                    help='이미지에서 꺼낸 것을 둘 곳 (기본 <출력>\\_추출)')
    ap.add_argument('--reuse-extract', action='store_true',
                    help='이미 꺼내 둔 것이 있으면 다시 꺼내지 않는다')
    ap.add_argument('--extract-only', action='store_true',
                    help='이미지에서 꺼내기만 하고 분석은 하지 않는다')
    ap.add_argument('--with-app', action='store_true',
                    help='Claude 앱 본체(WindowsApps)까지 꺼낸다. 수백 MB')
    ap.add_argument('--with-system', action='store_true',
                    help='Prefetch · 레지스트리 · 이벤트로그까지 꺼낸다')
    ap.add_argument('--include-noise', action='store_true',
                    help='Cache / Code Cache 등 실측상 성과 없는 경로도 꺼낸다')
    ap.add_argument('--exclude', action='append', default=[],
                    help='이 문자열이 들어간 경로는 제외 (여러 번 가능)')
    ap.add_argument('--target-mode', default='all', choices=['all'],
                    help='all=전체 복원 (prompt_only 는 폐기 — 실측에서 실제 '
                         '사용자 프롬프트 0건, 잡음 103건이었다)')
    ap.add_argument('--scenario', default='auto',
                    choices=['auto', '1', '2', '3'],
                    help='1=정상 2=대화삭제 3=계정삭제 (기록용 라벨)')
    ap.add_argument('--baseline', help='시나리오 3 대조용 — 삭제 이전 수집분')
    ap.add_argument('--marker', action='append', default=[],
                    help='실험용 고유 문자열')
    ap.add_argument('--scope', default='artifacts',
                    choices=['artifacts', 'everything'],
                    help='artifacts=대화 흔적이 나오는 경로만(기본), '
                         'everything=Cache/Code Cache 포함 전수 스캔')
    ap.add_argument('--no-decompress', action='store_true',
                    help='압축 해제 생략 (빠르지만 본문 약 40%% 놓친다)')
    ap.add_argument('--verify-crc', action='store_true')
    ap.add_argument('--min-confidence', type=int, default=0)
    ap.add_argument('--timeline', action='store_true',
                    help='$MFT · LevelDB 시퀀스 · 앱 로그를 엮어 타임라인을 만든다')
    ap.add_argument('--mft', help='$MFT 파일 경로 (생략 시 수집물에서 찾는다)')
    ap.add_argument('--mft-csv', help='MFTECmd 출력 CSV. 있으면 이쪽을 쓴다')
    ap.add_argument('-v', '--verbose', action='store_true')
    ap.add_argument('--no-anim', action='store_true',
                    help='진행 중 애니메이션을 끄고 로그만 흘린다 '
                         '(환경변수 CHATTRACE_NO_ANIM 으로도 된다)')
    ap.add_argument('--tag', default=None,
                    help='진행 애니메이션에 띄울 이름표 (기본 CK, 빈 값이면 뗀다)')
    a = ap.parse_args(argv)

    # 플래그가 이미 채운 자리는 건드리지 않는다.
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
                 '  통일 형식 : ChatTrace <이미지|수집물> <환경> <저장경로>\n'
                 '  기존 형식 : ChatTrace -i <이미지|수집물> -o <저장경로>')

    if a.no_anim:
        anim.UI.enabled = False
    if a.tag is not None:
        anim.UI.set_tag(a.tag)
    os.makedirs(a.output, exist_ok=True)
    _setup_log(a.output, a.verbose)

    t0 = datetime.datetime.now()
    LOG.info('입력 %s', os.path.abspath(a.input))
    LOG.info('모드 %s · 압축해제 %s', a.target_mode,
             '끔' if a.no_decompress else '켬')

    # ── 입력이 디스크 이미지면 먼저 꺼낸다 ─────────────────
    # 마운트하지 않는다. 마운트는 ACL 을 유지해서 남의 AppData 를 못 읽고,
    # 드라이버 설치와 관리자 권한을 요구한다. 원시 파싱은 그게 없다.
    if os.path.isfile(a.input):
        rc = _from_image(a)
        if rc is not None:
            return rc

    if not os.path.isdir(a.input):
        _explain_missing(a.input)
        return 2

    stores, files = discover(a.input)
    LOG.info('LevelDB 저장소 %d개 · 파일 %d개', len(stores), len(files))
    if not stores and not files:
        LOG.error('수집물이 비어 있다.')
        return 4

    blobs = scenarios.BlobIndex(extra_dirs=[a.input])
    LOG.info('외부 blob 후보 %d개 색인', len(blobs.files))

    rows, covered = [], set()
    stats = {'records': 0, 'stores': 0}

    full = True
    if full:
        LOG.info('── ① 전체 세션 ──')
        with anim.UI.stage('대화 트리를 복원하는 중', total=len(stores)) as st:
            r1, covered, stats = tier1_sessions(stores, a.input, blobs,
                                                a.verify_crc, st)
        rows += r1
        LOG.info('   대화 %d · 메시지 %d · 첨부 %d',
                 *[sum(1 for r in r1 if r['item'] == k)
                   for k in ('conversation', 'message', 'attachment')])

        # ── 초안 — 대화 본문이 안 남는 버전(2.9939.x)에서도 이건 남는다
        #    키에 대화 식별자가 있어 귀속되고, updatedAt 으로 시각이 나온다.
        rd = scenarios.drafts(stores, LOG)
        rows += rd

    targets, skipped = filter_scope(files, a.input, a.scope)
    LOG.info('── ② 단일 파편 + ③ 고유문자열 (파일 %d개 한 패스) ──', len(targets))
    if skipped:
        LOG.info('   Cache / Code Cache 등 %d개 건너뜀 — 전수는 --scope everything',
                 skipped)
    with anim.UI.stage('파일을 훑는 중', total=len(targets)) as st:
        r2, r3 = scan_files(targets, a.input, covered, a.marker,
                            not a.no_decompress, want_fragments=full, stage=st)
    rows += r2 + r3
    LOG.info('   ② 추가 회수 %d건', len(r2))
    LOG.info('   ③ 고유문자열 %d건',
             sum(1 for r in r3 if r['item'] == 'marker'))

    if a.min_confidence:
        rows = [r for r in rows if r.get('confidence', 100) >= a.min_confidence]

    tl_events, tl_del = [], []
    if a.timeline:
        tl_events, tl_del = build_timeline(a, stores, rows)

    # 시나리오 3 — 기준선 대조
    survived = recovered_only = None
    if a.baseline:
        survived, recovered_only = _baseline_diff(rows, a.baseline, blobs)

    convs, pairs = build_pairs(rows)
    _write(a, rows, convs, pairs, stores, files, stats, tl_events, tl_del)
    _console(rows, convs, pairs, stats, a, survived, recovered_only,
             (datetime.datetime.now() - t0).total_seconds())
    return 0 if rows else 1


def _baseline_diff(rows, baseline, blobs):
    """삭제 후에도 남은 것 / 미리 떠놨기 때문에 보이는 것을 가른다."""
    LOG.info('── 기준선 대조 %s ──', baseline)
    post = {(r.get('text') or '').strip()[:200] for r in rows if r.get('text')}
    bstores, _bf = discover(baseline)
    brows, _c, _s = tier1_sessions(bstores, baseline, blobs)
    surv = only = 0
    for r in brows:
        t = (r.get('text') or '').strip()[:200]
        if not t:
            continue
        if t in post:
            surv += 1
        else:
            only += 1
    for r in rows:
        r.setdefault('status', 'SURVIVED')
    LOG.info('   삭제 후 잔존 %d · 사전확보분만 %d', surv, only)
    return surv, only


def _write(a, rows, convs, pairs, stores, files, stats, tl_events=(), tl_del=()):
    o = a.output
    fields = ['tier', 'item', 'confidence', 'state', 'status', 'store', 'view',
              'rule', 'sender', 'index', 'conversation', 'uuid', 'title',
              'created_at', 'parent', 'file', 'offset', 'seq', 'key',
              'external', 'msg_count', 'declared_msgs', 'declared_bytes',
              'decode', 'text']
    report.write_csv(rows, os.path.join(o, 'findings.csv'), fields)
    report.write_csv([r for r in rows if r['item'] == 'prompt'],
                     os.path.join(o, 'prompts.csv'), fields)
    report.write_csv(pairs, os.path.join(o, 'pairs.csv'),
                     ['conversation', 'title', 'state', 'prompt_at',
                      'prompt', 'response'])
    report.write_json({
        'tool': 'ChatTrace', 'schema': report.SCHEMA,
        'generated': datetime.datetime.now().isoformat(timespec='seconds'),
        'input': os.path.abspath(a.input), 'target_mode': a.target_mode,
        'env_profile': a.profile,
        'scope': a.scope,
        'scenario': a.scenario, 'baseline': a.baseline,
        'leveldb_stores': stats['stores'], 'leveldb_records': stats['records'],
        'conversations': convs, 'pairs': pairs, 'findings': rows,
    }, os.path.join(o, 'conversations.json'))
    report.write_json(report.custody(files), os.path.join(o, 'chain_of_custody.json'))
    if tl_events:
        report.write_csv(tl_events, os.path.join(o, 'timeline.csv'),
                         ['time', 'source', 'kind', 'subject', 'detail',
                          'evidence', 'note'])
    if tl_del:
        report.write_csv(tl_del, os.path.join(o, 'deleted_when.csv'),
                         ['conversation', 'title', 'state', 'last_message',
                          'deleted_after', 'deleted_before', 'span_sec',
                          'confidence', 'note', 'key_kind', 'seq', 'evidence'])
    sections = []
    if tl_del:
        named = [r for r in tl_del if r.get('title')]
        sections.append((u'삭제 시각 추정 (대화와 엮인 것)', named,
                         ['title', 'deleted_after', 'deleted_before', 'span_sec',
                          'confidence', 'note', 'evidence']))
    if tl_events:
        sections.append((u'타임라인', tl_events,
                         ['time', 'source', 'kind', 'subject', 'detail', 'note']))
    report.write_html(
        sections +
        [(u'복원된 대화 쌍', pairs, ['title', 'state', 'prompt_at', 'prompt', 'response']),
         (u'① 전체 세션', [r for r in rows if r.get('tier') == 1], fields),
         (u'② 단일 파편', [r for r in rows if r.get('tier') == 2], fields),
         (u'③ 프롬프트', [r for r in rows if r.get('tier') == 3], fields)],
        os.path.join(o, 'report.html'))
    # 사람이 읽는 대화록
    # BOM 을 넣는다 — 분석관이 메모장으로 여는 파일이다
    with io.open(os.path.join(o, 'transcript.txt'), 'w',
                 encoding='utf-8-sig') as f:
        # uuid 가 붙은 대화를 먼저, 미연결 조각 묶음은 맨 뒤로
        ordered = sorted(convs, key=lambda x: (not x.get('uuid'),
                                               -(x.get('msg_count') or 0),
                                               -(x.get('recovered') or 0)))
        for c in ordered:
            if not c['messages']:
                continue
            f.write(u'=' * 78 + u'\n')
            if c['uuid']:
                f.write(u'%s  [%s]  %s/%s턴\n' % (c['title'] or '(제목없음)',
                                                  c['state'], c['recovered'],
                                                  c['msg_count'] or '?'))
                f.write(u'uuid %s\n' % c['uuid'])
            else:
                f.write(u'미연결 조각 %d건 — 대화 uuid 를 붙이지 못한 메시지\n'
                        % c['recovered'])
            f.write(u'=' * 78 + u'\n')
            for m in c['messages']:
                f.write(u'\n[%s] %s\n%s\n' % (m['sender'] or '?',
                                              m['created_at'] or '',
                                              m['text'] or ''))
            f.write(u'\n\n')

        # ── 초안 ──────────────────────────────────────────────
        # 보낸 메시지가 아니라 '치던 중' 이다. 대화별로 묶고 시각순으로
        # 쌓으면 타자 과정이 그대로 보인다 — 오타를 고친 흔적까지.
        drows = [r for r in rows if r.get('item') == 'draft']
        if drows:
            by = {}
            for r in drows:
                by.setdefault(r.get('conversation') or '', []).append(r)
            f.write(u'=' * 78 + u'\n')
            f.write(u'입력창 초안 %d건 — 대화 %d개\n' % (len(drows), len(by)))
            f.write(u'보낸 글이 아니라 치던 중의 모습이다. '
                    u'마지막 저장 이후 글자는 남지 않는다.\n')
            f.write(u'=' * 78 + u'\n')
            for conv in sorted(by, key=lambda k: (k == '', k)):
                items = by[conv]
                items.sort(key=lambda r: (r.get('created_at') or '',
                                          int(r.get('seq') or 0)))
                f.write(u'\n── %s  (%d건)\n'
                        % (conv or u'(새 대화 입력창)', len(items)))
                seen = set()
                prev = None
                for r in items:
                    t = (r.get('text') or '').strip()
                    if not t or t in seen:
                        continue
                    seen.add(t)
                    at = r.get('created_at') or ''
                    gap = u''
                    if prev and at:
                        d = _gap_sec(prev, at)
                        if d is not None:
                            gap = u'  +%.1fs' % d
                            if d >= 10:
                                gap += u'  ← 공백'
                    f.write(u'   %-26s%-16s%s\n' % (at, gap, t))
                    prev = at or prev
            f.write(u'\n\n')


def _gap_sec(a, b):
    """두 ISO 시각의 차이(초). 못 재면 None."""
    import datetime as _dt
    try:
        fa = _dt.datetime.strptime(a[:23], '%Y-%m-%dT%H:%M:%S.%f')
        fb = _dt.datetime.strptime(b[:23], '%Y-%m-%dT%H:%M:%S.%f')
        return (fb - fa).total_seconds()
    except Exception:
        return None


def _console(rows, convs, pairs, stats, a, survived, only, secs):
    def n(**kw):
        return sum(1 for r in rows
                   if all(r.get(k) == v for k, v in kw.items()))

    # 복원율은 '선언 턴수가 있는 대화' 에 대해서만 낸다.
    # uuid 없이 회수된 조각을 분자에 넣으면 100%% 를 넘어 무의미해진다.
    known = [c for c in convs if (c.get('msg_count') or 0) > 0]
    declared = sum(c['msg_count'] for c in known)
    got = sum(min(c['recovered'] or 0, c['msg_count']) for c in known)
    rate = (100.0 * got / declared) if declared else 0.0

    # 본문 복원률 — 레코드가 신고한 approxBytes 대비 실제 복원 바이트.
    # 턴 수만 보면 본문이 절반 날아가도 100% 로 표시돼 오해를 부른다.
    byt = [c for c in convs
           if isinstance(c.get('declared_bytes'), (int, float))
           and c['declared_bytes'] > 0]
    dbytes = sum(c['declared_bytes'] for c in byt)
    gbytes = sum(min(c.get('recovered_bytes') or 0, c['declared_bytes'])
                 for c in byt)
    brate = (100.0 * gbytes / dbytes) if dbytes else None
    unlinked = sum(c['recovered'] or 0 for c in convs
                   if (c.get('msg_count') or 0) == 0)
    prompts = n(item='prompt')
    human = sum(1 for c in convs for m in c['messages'] if m['sender'] == 'human')
    mk = [r['text'] for r in rows if r.get('item') == 'marker']

    p = LOG.info
    p('=' * 62)
    p('복원 리포트')
    p('=' * 62)
    p('  입력            %s', os.path.abspath(a.input))
    p('  모드            %s   시나리오 %s', a.target_mode, a.scenario)
    p('  LevelDB         저장소 %d · 레코드 %d', stats['stores'], stats['records'])
    p('  ' + '-' * 58)
    # ★ 레코드 수와 고유 수를 갈라 찍는다.
    #   LevelDB 는 덮어쓸 때 옛 판본을 지우지 않는다. 레코드를 그대로
    #   세면 대화 11개가 '대화 56' 으로 보이고, 같은 메시지의 중복
    #   판본 때문에 메시지 24종이 '메시지 356' 이 된다. 머리줄만 읽은
    #   사람은 56개 대화를 다 살린 줄 안다.
    crows = [r for r in rows if r.get('item') == 'conversation']
    mrows = [r for r in rows if r.get('item') == 'message']
    cuniq = {r.get('uuid') for r in crows if r.get('uuid')}
    muniq = {(r.get('text') or '') for r in mrows if (r.get('text') or '')}
    cbody = {r.get('conversation') for r in mrows if r.get('conversation')}
    msender = sum(1 for r in mrows if r.get('sender'))
    p('  ① 전체 세션    대화 %d개 · 그중 본문 있는 대화 %d개',
      len(cuniq) or len(crows), len(cbody))
    p('                  메시지 %d종 · 발신자까지 붙은 것 %d건',
      len(muniq), msender)
    p('                  (LevelDB 레코드로는 대화 %d · 메시지 %d '
      '— 덮어쓴 옛 판본 포함)', len(crows), len(mrows))
    p('  ② 단일 파편    %d', n(tier=2))
    p('  ③ 고유문자열    %d종 %d건', len(set(mk)), len(mk))
    p('  ' + '-' * 58)
    p('  대화 쌍(Q/A)    %d 쌍', len(pairs))
    p('  사용자 입력      %d 턴 (구조 복원분)', human)
    if declared:
        p('  복원율 (턴)     %.1f%%  (%d / 선언 %d턴, 대화 %d개)',
          rate, got, declared, len(known))
    else:
        p('  복원율 (턴)     선언값 없음 — 절대 기준 산출 불가')
    if brate is not None:
        p('  복원율 (본문)   %.1f%%  (%s / 선언 %s 바이트)',
          brate, format(gbytes, ','), format(int(dbytes), ','))
        p('                  ↑ 턴이 다 나와도 본문은 빠질 수 있다', )
    else:
        p('  복원율 (본문)   approxBytes 없음 — 산출 불가')
    if unlinked:
        p('  미연결 메시지    %d  (대화 uuid 를 못 붙인 조각)', unlinked)
    # 초안 — 대화 본문이 안 남는 버전에서는 이것이 유일한 입력 기록이다
    drows = [r for r in rows if r.get('item') == 'draft']
    if drows:
        dconv = {r.get('conversation') or '(새 대화 입력창)' for r in drows}
        p('  초안            %d건 · 대화 %d개 (치던 중의 입력)',
          len(drows), len(dconv))
    for m in sorted(set(mk)):
        p('    고유문자열     %-14s %d건', m, mk.count(m))
    if survived is not None:
        p('  기준선 대조      잔존 %d · 사전확보분만 %d', survived, only)
    p('  ' + '-' * 58)
    ok = bool(pairs) or bool(prompts) or bool(human)
    if ok:
        p('  결과            성공')
    elif drows:
        # 도구가 실패한 게 아니라 대상에 대화 본문이 없는 것이다.
        # 앱 2.9939.x 부터 react-query-cache 가 사라져 초안만 남는다.
        p('  결과            부분 — 대화 본문 없음 · 초안만 복원')
        p('  해석            이 앱 버전은 대화를 로컬에 남기지 않는다.')
        p('                  남는 것은 입력창 초안뿐이며 전송 여부는 알 수 없다')
    else:
        p('  결과            실패 — 복원된 대화 없음')
        p('  점검            앱 종료 후 수집했는지 / 컴팩션 이후인지 확인')
    p('  소요            %.1f초', secs)
    p('  출력            %s', os.path.abspath(a.output))
    p('=' * 62)


class _AnimHandler(logging.Handler):
    """콘솔 로그를 애니메이션과 같은 자물쇠로 묶는다.

    그냥 print 하면 그리는 중간에 끼어들어 그림이 찢어진다. 지우고 → 찍고
    → 다시 그리는 순서를 UI 가 보장한다. 애니메이션이 꺼져 있으면
    그냥 출력한다.
    """

    def __init__(self, stream):
        logging.Handler.__init__(self)
        self.stream = stream

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            return
        try:
            if anim.UI.enabled:
                anim.UI.log(msg)
            else:
                self.stream.write(msg + '\n')
                self.stream.flush()
        except Exception:
            pass


def _setup_log(outdir, verbose):
    LOG.handlers = []
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    try:
        stream = codecs.getwriter('utf-8')(sys.stdout.buffer, 'replace')
    except AttributeError:
        stream = sys.stdout
    sh = _AnimHandler(stream)
    sh.setFormatter(logging.Formatter('%(message)s'))
    LOG.addHandler(sh)
    fh = logging.FileHandler(os.path.join(outdir, 'chattrace.log'),
                             encoding='utf-8-sig')
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s %(message)s'))
    LOG.addHandler(fh)


if __name__ == '__main__':
    sys.exit(main())
