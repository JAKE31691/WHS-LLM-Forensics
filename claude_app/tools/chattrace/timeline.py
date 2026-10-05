# -*- coding: utf-8 -*-
"""타임라인 — '무엇을' 에 '언제' 를 붙인다.

ChatTrace 본체는 "이런 대화가 있었고 지워졌다" 까지만 말한다.
삭제 시각은 못 댄다. 이 모듈이 세 출처를 하나의 시간축으로 합친다.

    $MFT          파일이 언제 바뀌었나          100나노초
    LevelDB seq   삭제가 어느 구간에 일어났나   중앙값 11.5초로 좁혀짐
    main.log      앱이 언제 뜨고 졌나           초

삭제 시각 추정 원리
  LevelDB 시퀀스 번호는 쓰기 순서다. 레코드 중 일부는 writtenAt 같은
  밀리초 시각을 품고 있어 (시퀀스, 시각) 기준점이 된다. 삭제 표시
  (툼스톤)에는 시각이 없지만 시퀀스는 있으므로, 앞뒤 기준점 사이로
  구간을 좁힐 수 있다. 단정하지 않고 구간으로 낸다 — 그게 정직하다.
"""
import datetime
import os
import re

from . import leveldb, mft, scenarios
from .codec import decode_ssv

# 레코드 안에서 시각으로 쓸 수 있는 필드 (밀리초 epoch)
TIME_KEYS = ('writtenAt', 'fetchedAt', 'conversationUpdatedAt',
             'updatedAt', 'timestamp')
MS_MIN, MS_MAX = 1.5e12, 2.5e12          # 2017 ~ 2049 사이만 시각으로 인정

SRC_MFT, SRC_DB, SRC_LOG, SRC_CHAT = 'MFT', 'DB', 'LOG', '대화'


def _ms_to_dt(v):
    try:
        return datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=float(v))
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def _iso_to_dt(s):
    if not s or not isinstance(s, str):
        return None
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,7}))?', s)
    if not m:
        return None
    us = int((m.group(7) or '0').ljust(6, '0')[:6])
    try:
        return datetime.datetime(*[int(m.group(i)) for i in range(1, 7)], microsecond=us)
    except ValueError:
        return None


def fmt(dt):
    return dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:23] if dt else ''


# ────────────────────────────────────────────────────────────────
# 1) LevelDB 시퀀스 ↔ 시각 기준점
# ────────────────────────────────────────────────────────────────
def build_anchors(recs, blobs=None):
    """(시퀀스, 시각) 기준점을 모은다. -> 시퀀스 오름차순 리스트"""
    anchors = []
    for r in recs:
        if r['op'] != 'put' or not r['value']:
            continue
        try:
            o = decode_ssv(leveldb.idb_value_blob(r['value']))
        except Exception:
            continue
        if isinstance(o, dict) and '__external_blob' in o:
            continue                      # 외부 blob 은 여기서 열지 않는다
        if not isinstance(o, (dict, list)):
            continue
        t = None
        for d in scenarios.walk(o):
            for k in TIME_KEYS:
                v = d.get(k)
                if isinstance(v, (int, float)) and MS_MIN < v < MS_MAX:
                    t = _ms_to_dt(v)
                    break
            if t:
                break
        if t:
            anchors.append((r['seq'], t))
    anchors.sort()
    return anchors


def bracket(seq, anchors, ceiling=None):
    """시퀀스를 앞뒤 기준점 사이로 좁힌다. -> (하한, 상한, 비고)

    두 가지를 처리한다.

    1) 기준점 시각이 완전히 단조롭지 않다(실측 3/90 역전). 그대로
       'seq 이하의 최대' 와 'seq 이상의 최소' 를 쓰면 하한이 상한을
       넘어 음수 구간이 나온다. 뒤집히면 순서를 바로잡고 모순으로 표시한다.

    2) 마지막 기준점보다 뒤에 있는 삭제는 상한이 없다. 이때는 파일의
       $MFT 변경 시각을 천장으로 쓴다. 파일이 마지막으로 쓰인 뒤에
       기록이 더 붙을 수는 없기 때문이다.
    """
    lo = hi = None
    for s, t in anchors:
        if s <= seq and (lo is None or t > lo):
            lo = t
        if s >= seq and (hi is None or t < hi):
            hi = t

    note = ''
    if hi is None and ceiling is not None:
        hi = ceiling
        note = '상한은 파일 변경시각'
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
        note = ('%s · 기준점 모순' % note).strip(' ·')
    return lo, hi, note


# ────────────────────────────────────────────────────────────────
# 2) 출처별 사건 수집
# ────────────────────────────────────────────────────────────────
LEVELDB_NAME = re.compile(r'^\d{6}\.(log|ldb)$', re.I)


def _collect_ceilings(recs, ceilings):
    """LevelDB 파일의 최신 변경시각을 파일명별로 모은다.

    삭제 시각의 상한으로 쓴다. 같은 이름(000003.log)이 수집물 여러 곳에
    있으므로 가장 늦은 값을 취한다 — 상한은 넉넉해야 안전하다.
    """
    for r in recs:
        nm = r['name']
        if not LEVELDB_NAME.match(nm):
            continue
        t = mft.filetime_dt(r['si_modified'])
        if t and (nm not in ceilings or t > ceilings[nm]):
            ceilings[nm] = t


def mft_events(mft_path, log=None, csv_path=None, ceilings=None):
    """$MFT 또는 MFTECmd CSV 에서 Claude 아티팩트 사건을 낸다."""
    ev = []
    if csv_path and os.path.exists(csv_path):
        ev = _mft_events_from_csv(csv_path, ceilings)
        if log:
            log.info('   MFTECmd CSV 에서 %s건', format(len(ev), ','))
        return ev
    if not mft_path or not os.path.exists(mft_path):
        return ev
    dmap = mft.build_dir_map(mft_path, log)
    recs = mft.scan(mft_path, dmap, mft.claude_filter, log)
    if ceilings is not None:
        _collect_ceilings(recs, ceilings)
    for r in recs:
        short = _shorten(r['path'])
        flag = [] if r['in_use'] else ['MFT항목 미사용(삭제 정황)']
        if r.get('path_status') != mft.PATH_OK:
            flag.append('경로 신뢰도 %s' % r.get('path_status'))
        for key, kind in (('si_created', '파일 생성'),
                          ('si_modified', '내용 변경'),
                          ('si_mft_modified', 'MFT 변경')):
            t = mft.filetime_dt(r[key])
            if t:
                ev.append({'when': t, 'source': SRC_MFT, 'kind': kind,
                           'subject': short, 'detail': '',
                           'evidence': 'entry=%d seq=%d' % (r['entry'], r['seq']),
                           'note': ' / '.join(flag)})
    if log:
        log.info('   $MFT 에서 %s건 (레코드 %s개)',
                 format(len(ev), ','), format(len(recs), ','))
    return ev


def _mft_events_from_csv(path, ceilings=None):
    import csv as _csv
    import io as _io
    ev = []
    with _io.open(path, encoding='utf-8-sig', errors='replace', newline='') as f:
        for row in _csv.DictReader(f):
            p = ((row.get('ParentPath') or '') + '\\' + (row.get('FileName') or ''))
            if not mft.claude_filter(p.lower()):
                continue
            short = _shorten(p.lstrip('.\\'))
            inuse = (row.get('InUse') or '').strip().lower() == 'true'
            for col, kind in (('Created0x10', '파일 생성'),
                              ('LastModified0x10', '내용 변경'),
                              ('LastRecordChange0x10', 'MFT 변경')):
                t = _iso_to_dt(row.get(col))
                if t:
                    ev.append({'when': t, 'source': SRC_MFT, 'kind': kind,
                               'subject': short, 'detail': '',
                               'evidence': 'entry=%s' % row.get('EntryNumber'),
                               'note': '' if inuse else 'MFT항목 미사용(삭제 정황)'})
    return ev


LOG_LINE = re.compile(
    r'^\[?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)\]?\s*(.*)$')
LOG_INTEREST = ('login', 'logout', 'oauth', 'account', 'quit', 'exit',
                'start', 'ready', 'window', 'sign', 'delete', 'session')


def log_events(logdir, log=None, max_lines=200000):
    """main.log 등에서 앱 동작 사건을 낸다."""
    ev = []
    if not logdir or not os.path.isdir(logdir):
        return ev
    for fn in sorted(os.listdir(logdir)):
        if not fn.endswith('.log'):
            continue
        p = os.path.join(logdir, fn)
        try:
            with open(p, 'rb') as f:
                raw = f.read()
        except OSError:
            continue
        text = raw.decode('utf-8', 'replace')
        for i, line in enumerate(text.splitlines()):
            if i > max_lines:
                break
            m = LOG_LINE.match(line.strip())
            if not m:
                continue
            low = m.group(2).lower()
            if not any(k in low for k in LOG_INTEREST):
                continue
            t = _iso_to_dt(m.group(1).replace(',', '.'))
            if t:
                ev.append({'when': t, 'source': SRC_LOG, 'kind': '앱 동작',
                           'subject': fn, 'detail': m.group(2)[:180],
                           'evidence': '%s:%d' % (fn, i + 1), 'note': ''})
    if log:
        log.info('   앱 로그에서 %s건', format(len(ev), ','))
    return ev


def chat_events(rows, log=None):
    """ChatTrace 복원 결과에서 대화 사건을 낸다."""
    ev = []
    for r in rows:
        if r.get('item') not in ('message', 'conversation'):
            continue
        t = _iso_to_dt(r.get('created_at'))
        if not t:
            continue
        if r['item'] == 'conversation':
            ev.append({'when': t, 'source': SRC_CHAT, 'kind': '대화 생성',
                       'subject': r.get('title') or '(제목없음)',
                       'detail': '', 'evidence': r.get('uuid', '')[:8],
                       'note': r.get('state', '')})
        else:
            who = {'human': '사용자 입력', 'assistant': '모델 응답'}.get(
                r.get('sender'), '메시지')
            ev.append({'when': t, 'source': SRC_CHAT, 'kind': who,
                       'subject': r.get('title') or '',
                       'detail': (r.get('text') or '')[:180].replace('\n', ' '),
                       'evidence': (r.get('uuid') or '')[:8],
                       'note': r.get('state', '')})
    if log:
        log.info('   대화에서 %s건', format(len(ev), ','))
    return ev


# ────────────────────────────────────────────────────────────────
# 3) 삭제 시각 추정
# ────────────────────────────────────────────────────────────────
def deletion_estimates(recs, anchors, rows, log=None, ceilings=None):
    """툼스톤마다 삭제 구간을 추정하고 대화와 엮는다.

    ceilings: {파일명: $MFT 변경시각}. 마지막 기준점 뒤의 삭제에 천장을 준다.
    """
    # 복원된 대화 정보 (uuid -> 제목, 마지막 메시지 시각)
    conv = {}
    for r in rows:
        u = r.get('conversation') or r.get('uuid') or ''
        if not u:
            continue
        c = conv.setdefault(u, {'title': '', 'last': None, 'state': ''})
        if r.get('title') and not c['title']:
            c['title'] = r['title']
        if r.get('state'):
            c['state'] = r['state']
        t = _iso_to_dt(r.get('created_at'))
        if t and (c['last'] is None or t > c['last']):
            c['last'] = t

    out = []
    for r in recs:
        if r['op'] != 'del':
            continue
        key = leveldb.idb_decode_key(r['key'])
        kind = leveldb.idb_kind(r['key'])
        ceil = (ceilings or {}).get(r['file'])
        lo, hi, note = bracket(r['seq'], anchors, ceil)
        if lo is None and hi is None:
            continue
        span = (hi - lo).total_seconds() if (lo and hi) else None
        c = conv.get(key, {})
        out.append({
            'conversation': key,
            'title': c.get('title', ''),
            'state': c.get('state', ''),
            'last_message': fmt(c.get('last')),
            'deleted_after': fmt(lo),
            'deleted_before': fmt(hi),
            'span_sec': '' if span is None else round(span, 1),
            'confidence': _conf(span),
            'note': note,
            'key_kind': kind,
            'seq': r['seq'],
            'evidence': '%s @0x%X seq=%d' % (r['file'], r['offset'], r['seq']),
        })
    out.sort(key=lambda x: x['seq'])
    if log:
        narrow = sum(1 for x in out if x['span_sec'] != '' and x['span_sec'] <= 60)
        log.info('   삭제 표시 %s개 · 60초 이내로 좁혀진 것 %s개',
                 format(len(out), ','), format(narrow, ','))
    return out


def _conf(span):
    if span is None:
        return '경계값 없음'
    if span <= 10:
        return '높음 (10초 이내)'
    if span <= 60:
        return '보통 (1분 이내)'
    if span <= 3600:
        return '낮음 (1시간 이내)'
    return '매우 낮음'


def _shorten(p):
    """긴 경로에서 Claude 이후만 남긴다."""
    low = p.lower()
    for anchor in ('\\claude\\', 'claude_pzs8sxrjxfjjc\\'):
        i = low.find(anchor)
        if i >= 0:
            return p[i + 1:]
    return p


# ────────────────────────────────────────────────────────────────
# 4) 병합
# ────────────────────────────────────────────────────────────────
def merge(*groups):
    ev = []
    for g in groups:
        ev += g or []
    ev.sort(key=lambda e: e['when'])
    for e in ev:
        e['time'] = fmt(e['when'])
    return ev
