# -*- coding: utf-8 -*-
"""명령줄 진입점."""
import argparse
import datetime
import io
import logging
import os
import sys

from . import config, report, scenarios

BANNER = u'ChatTrace - Claude Desktop 대화 아티팩트 복원'


def setup_log(outdir, verbose):
    os.makedirs(outdir, exist_ok=True)
    lg = logging.getLogger('cdar')
    lg.setLevel(logging.DEBUG if verbose else logging.INFO)
    lg.handlers = []
    fmt = logging.Formatter('%(asctime)s %(levelname)-7s %(message)s',
                            '%H:%M:%S')
    try:
        sh = logging.StreamHandler(io.TextIOWrapper(sys.stdout.buffer,
                                                    encoding='utf-8',
                                                    errors='replace'))
    except Exception:
        sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    lg.addHandler(sh)
    fh = logging.FileHandler(os.path.join(outdir, 'cdar.log'), encoding='utf-8')
    fh.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-7s %(name)s %(message)s'))
    lg.addHandler(fh)
    return lg


def guard_original(root, allow, lg):
    """원본 경로에 직접 붙는 걸 막는다.

    쓰기를 하지 않더라도 원본을 건드리는 순간 증거 무결성 주장이 약해진다.
    무엇보다 앱이 실행 중이면 파일이 그 아래에서 계속 바뀐다.
    """
    live = config.default_app_root().lower()
    if os.path.abspath(root).lower().startswith(live) and not allow:
        lg.error('원본 앱 저장소를 가리키고 있다: %s', root)
        lg.error('먼저 사본을 떠라:')
        lg.error(r'  robocopy "%s" "D:\case\T1" /E /COPY:DAT /DCOPY:T /R:1 /W:1',
                 config.default_app_root())
        lg.error('의도한 것이면 --allow-original 을 붙여라.')
        return False
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(prog='cdar', description=BANNER)
    ap.add_argument('--root', required=True,
                    help='분석 대상 루트 (증거 사본의 Claude 폴더)')
    ap.add_argument('--out', required=True, help='결과 출력 폴더')
    ap.add_argument('--scenario', default='all',
                    choices=['1', '2', '3', 'all'],
                    help='1=정상 2=대화삭제 3=계정삭제')
    ap.add_argument('--baseline',
                    help='시나리오 3 대조용 — 삭제 이전 시점 수집분의 루트')
    ap.add_argument('--marker', action='append', default=[],
                    help='실험용 고유 문자열 (여러 번 지정 가능)')
    ap.add_argument('--blobdir', action='append', default=[],
                    help='외부 블롭(.blob)을 따로 수집해 둔 폴더')
    ap.add_argument('--verify-crc', action='store_true',
                    help='LevelDB 레코드 CRC 검증 (느리지만 손상 판별에 필요)')
    ap.add_argument('--allow-original', action='store_true')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args(argv)

    lg = setup_log(a.out, a.verbose)
    lg.info(BANNER)
    lg.info('대상 %s', a.root)

    if not os.path.isdir(a.root):
        lg.error('루트가 없다: %s', a.root)
        return 2
    if not guard_original(a.root, a.allow_original, lg):
        return 3

    specs = config.resolve(a.root)
    if not specs:
        lg.error('아티팩트를 하나도 못 찾았다. --root 가 Claude 폴더를 '
                 '가리키는지 확인해라 (하위에 IndexedDB / Local Storage 가 '
                 '있어야 한다).')
        return 4
    for s in specs:
        lg.info('  발견 %-12s %s', s['key'], s['path'])

    stores, files = scenarios.load(a.root, lg, a.verify_crc)
    blobs = scenarios.BlobIndex(a.root, a.blobdir)
    lg.info('외부 블롭 후보 %d개 색인', len(blobs.files))
    files += blobs.files

    rows = []
    want = ('1', '2', '3') if a.scenario == 'all' else (a.scenario,)
    try:
        if '1' in want:
            rows += scenarios.scenario1(stores, lg, blobs)
        if '2' in want:
            rows += scenarios.scenario2(stores, a.root, lg, a.marker, blobs)
        if '3' in want:
            rows += scenarios.scenario3(a.root, lg, a.baseline, a.marker)
    except KeyboardInterrupt:
        lg.warning('사용자 중단 — 여기까지의 결과만 저장한다')

    # ── 출력 ────────────────────────────────────────────────
    ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    fields = ['scenario', 'status', 'state', 'store', 'item', 'decode',
              'conversation', 'uuid', 'title', 'sender', 'index', 'model',
              'created_at', 'parent', 'file', 'offset', 'seq', 'key_kind', 'external',
              'msg_count', 'text']
    csv_path = report.write_csv(rows, os.path.join(a.out, 'findings.csv'), fields)
    msgs = [r for r in rows if r.get('item') in ('message', 'prompt', 'conversation')]
    report.write_csv(msgs, os.path.join(a.out, 'messages.csv'), fields)
    js = {
        'schema': report.SCHEMA,
        'generated': ts,
        'root': os.path.abspath(a.root),
        'baseline': os.path.abspath(a.baseline) if a.baseline else None,
        'scenarios': list(want),
        'markers': a.marker,
        'artifacts': specs,
        'parse_errors': {st.spec['key']: st.errs for st in stores if st.errs},
        'counts': _counts(rows),
        'findings': rows,
    }
    report.write_json(js, os.path.join(a.out, 'findings.json'))
    report.write_json(report.custody(files),
                      os.path.join(a.out, 'chain_of_custody.json'))
    report.write_html(
        [(u'시나리오 1 — 정상 상태', [r for r in rows if r['scenario'] == 1], fields),
         (u'시나리오 2 — 삭제/덮어쓰기 복원', [r for r in rows if r['scenario'] == 2], fields),
         (u'시나리오 3 — 계정 삭제 후', [r for r in rows if r['scenario'] == 3], fields)],
        os.path.join(a.out, 'report.html'))

    lg.info('─' * 60)
    for k, v in sorted(_counts(rows).items()):
        lg.info('  %-34s %d', k, v)
    lg.info('─' * 60)
    lg.info('결과 → %s', os.path.abspath(a.out))
    return 0


def _counts(rows):
    c = {}
    for r in rows:
        for k in ('item', 'state', 'status', 'decode'):
            if r.get(k):
                key = '%s=%s' % (k, r[k])
                c[key] = c.get(key, 0) + 1
    c['총 소견'] = len(rows)
    return c


if __name__ == '__main__':
    sys.exit(main())
