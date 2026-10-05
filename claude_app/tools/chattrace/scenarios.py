# -*- coding: utf-8 -*-
"""시나리오별 복원 엔진."""
import os
import re
import datetime
import struct

from . import carve, config, leveldb, sqlitefs
from .codec import Truncated, decode_ssv, harvest_strings

MAX_TEXT = 20000


# ────────────────────────────────────────────────────────────────
# 디코드된 객체에서 의미 있는 것 골라내기
# ────────────────────────────────────────────────────────────────
def walk(obj, depth=0):
    """중첩 구조를 평평하게 — dict 만 골라 낸다."""
    if depth > 30:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            for x in walk(v, depth + 1):
                yield x
    elif isinstance(obj, list):
        for v in obj:
            for x in walk(v, depth + 1):
                yield x


def classify(d):
    """스키마를 가정하지 않는다 — 있는 필드로 판정한다.

    Claude 가 업데이트되면 필드가 바뀔 수 있으므로 하드코딩된 스키마 대신
    '무엇이 들어 있는가' 로 분류한다.
    """
    k = set(d.keys())
    if 'file_name' in k or 'file_uuid' in k:
        return 'attachment'
    if ('text' in k or 'content' in k) and ('sender' in k or 'index' in k
                                            or 'start_timestamp' in k):
        return 'message'
    if 'uuid' in k and ('name' in k or 'summary' in k or 'chat_messages' in k):
        return 'conversation'
    if 'prompt' in k:
        return 'prompt'
    if 'text' in k and isinstance(d.get('text'), str) and len(d['text']) > 20:
        return 'message'
    return None


def _txt(v):
    """text / content 어느 쪽이든 본문 문자열로 편다.

    content 는 [{type, text, citations, start_timestamp, ...}] 블록 배열이고
    희소 배열로 직렬화되면 {'0': {...}} 형태로 온다. 둘 다 받는다.
    """
    if isinstance(v, str):
        return v[:MAX_TEXT]
    if isinstance(v, dict):
        v = [v[k] for k in sorted(v, key=lambda x: int(x) if x.isdigit() else 0)]
    if isinstance(v, list):
        out = []
        for e in v:
            if isinstance(e, dict):
                for kk in ('text', 'thinking', 'input', 'content'):
                    if isinstance(e.get(kk), str) and e[kk]:
                        out.append(e[kk])
                        break
            elif isinstance(e, str):
                out.append(e)
        return '\n'.join(out)[:MAX_TEXT]
    return ''


class BlobIndex(object):
    """외부로 빠진 IndexedDB 값(.blob 파일)을 찾아주는 색인.

    Chromium 은 값이 임계치를 넘으면 DB 에 크기만 남기고 본문을
    IndexedDB\\<오리진>.indexeddb.blob\\<db>\\<디렉터리>\\<번호> 에 쓴다.
    번호 체계에 의존하면 버전마다 깨지므로 '크기' 로 맞춘다 — DB 레코드가
    선언한 바이트 수와 파일 크기가 정확히 일치하는지로 대응시킨다.
    """

    def __init__(self, root=None, extra_dirs=()):
        self.by_size = {}
        self.files = []
        self.ambiguous = 0
        dirs = list(extra_dirs)
        if root:
            idb = os.path.join(root, 'IndexedDB')
            if os.path.isdir(idb):
                dirs.append(idb)
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for dp, _dn, fns in os.walk(d):
                for f in fns:
                    p = os.path.join(dp, f)
                    try:
                        sz = os.path.getsize(p)
                    except OSError:
                        continue
                    self.by_size.setdefault(sz, []).append(p)
                    self.files.append(p)

    # blob 저장소 경로일수록 낮은 점수 = 우선 선택
    @staticmethod
    def _rank(p):
        low = p.lower().replace('\\', '/')
        if '.indexeddb.blob/' in low:
            return 0
        if '.blob/' in low:
            return 1
        if 'indexeddb' in low:
            return 2
        return 3

    def find(self, ref):
        """선언된 크기와 일치하는 blob 파일을 찾는다.

        ★ 크기만으로 고르면 안 된다. 디스크 이미지 전체를 입력으로 주면
          우연히 같은 크기인 파일(예: Windows\\System32 의 아무 파일)이
          walk 순서에 따라 먼저 걸려 진짜 blob 을 가린다. 실제로
          96,845 B 더미가 blob 을 밀어내 43행이 소실된 사례를 확인했다.
          그래서 blob 저장소 경로를 우선한다.
        """
        cand = self.by_size.get(ref.get('size'), [])
        if not cand:
            return None
        if len(cand) == 1:
            return cand[0]
        ranked = sorted(cand, key=lambda p: (self._rank(p), len(p)))
        self.ambiguous += 1
        return ranked[0]

    def ambiguity(self):
        """크기가 겹쳐 순위로 고른 횟수. 리포트에 남겨 판단 근거를 준다."""
        return self.ambiguous


def _as_list(v):
    if isinstance(v, dict):
        return [v[k] for k in sorted(v, key=lambda x: int(x) if x.isdigit() else 0)]
    return v if isinstance(v, list) else []


def _conversation_rows(obj, meta):
    """대화 트리를 대화 1행 + 메시지 N행으로 편다 (index 순서 유지).

    트리는 최상위 'tree' 에만 있는 게 아니다. clientState 같은 상위 객체
    안에 여러 대화가 중첩돼 들어가기도 하므로 구조 전체를 훑어
    chat_messages 를 가진 dict 를 전부 찾는다.
    """
    rows = []
    for tree in walk(obj):
        if 'chat_messages' not in tree:
            continue
        rows += _one_tree(tree, obj, meta)
    return rows


def _one_tree(tree, obj, meta):
    rows = []
    # 레코드가 스스로 신고하는 크기·턴수. 정답지 없이 완전성을 재는 유일한 근거다.
    declared_bytes = obj.get('approxBytes') if isinstance(obj, dict) else None
    declared_msgs = obj.get('messageCount') if isinstance(obj, dict) else None
    conv = dict(meta)
    conv.update({'item': 'conversation',
                 'uuid': tree.get('uuid') or (obj.get('conversationUuid')
                                              if isinstance(obj, dict) else '') or '',
                 'title': tree.get('name') or '', 'sender': '',
                 'created_at': tree.get('created_at') or '',
                 'text': tree.get('summary') or '',
                 'model': tree.get('model') or '',
                 'msg_count': len(_as_list(tree.get('chat_messages'))),
                 'declared_msgs': declared_msgs,
                 'declared_bytes': declared_bytes,
                 'decode': 'strict'})
    rows.append(conv)
    for m in _as_list(tree.get('chat_messages')):
        if not isinstance(m, dict):
            continue
        r = dict(meta)
        r.update({'item': 'message', 'uuid': m.get('uuid') or '',
                  'conversation': conv['uuid'], 'title': conv['title'],
                  'sender': m.get('sender') or '', 'index': m.get('index'),
                  'created_at': m.get('created_at') or '',
                  'text': _txt(m.get('text')) or _txt(m.get('content')),
                  'model': conv['model'], 'decode': 'strict',
                  'parent': m.get('parent_message_uuid') or ''})
        rows.append(r)
        for a in _as_list(m.get('files')) + _as_list(m.get('attachments')):
            if isinstance(a, dict):
                ar = dict(meta)
                ar.update({'item': 'attachment', 'uuid': a.get('file_uuid') or '',
                           'conversation': conv['uuid'],
                           'title': a.get('file_name') or '',
                           'sender': m.get('sender') or '',
                           'created_at': a.get('created_at') or '',
                           'text': _txt(a.get('extracted_content')),
                           'decode': 'strict'})
                rows.append(ar)
    return rows


# ── 초안 복원 (앱 2.9939.x) ──────────────────────────────────────
# 대화 본문(react-query-cache)이 사라진 버전에서도 입력창 초안은 남는다.
# 키에 대화 식별자가 들어 있어 귀속이 되고, updatedAt 으로 시각도 나온다.
#
#   키   00 <dbid> 01 01 01 <varint 문자수> <UTF-16BE 문자열>
#        └ KeyPrefix  └ objectStore └ index └ 키타입 1=string
#        dbid 는 환경마다 다르다 (실측 FREE01=5 · T7=4)
#
#   값   22 04 "text"      22 <len> <본문>
#        22 09 "updatedAt" 4e <8B double ms>
#
# ★ 키가 UTF-16**BE** 다. LE 로 읽으면 글자가 깨져 하나도 안 걸린다.

IDB_KEYHEAD = re.compile(rb'\x00.\x01\x01\x01', re.S)
DRAFT_KEY = re.compile(r'store:chat-draft:(.+)$')
_ASCII_ONLY = re.compile(r'^[\x20-\x7e]+$')


def _uvarint(b, p):
    n = shift = 0
    while p < len(b):
        c = b[p]
        p += 1
        n |= (c & 0x7F) << shift
        if not c & 0x80:
            return n, p
        shift += 7
    return None, p


def idb_key_strings(val):
    """레코드 값에 박힌 IndexedDB 문자열 키를 전부 낸다."""
    out = []
    for m in IDB_KEYHEAD.finditer(val):
        p = m.end()
        n, p = _uvarint(val, p)
        if not n or n <= 0 or n > 400 or p + n * 2 > len(val):
            continue
        k = val[p:p + n * 2].decode('utf-16-be', 'replace')
        if _ASCII_ONLY.match(k):
            out.append(k)
    return out


def _v8_strings_after(val, name):
    """V8 객체에서 '<name>' 키 뒤에 오는 문자열들."""
    tag = b'"' + bytes([len(name)]) + name.encode()
    out, i = [], 0
    while True:
        i = val.find(tag, i)
        if i < 0:
            break
        p = i + len(tag)
        if p < len(val) and val[p] in (0x22, 0x63):
            t = val[p]
            ln, q = _uvarint(val, p + 1)
            if ln and 0 < ln <= len(val) - q:
                out.append(val[q:q + ln].decode(
                    'latin1' if t == 0x22 else 'utf-16-le', 'replace'))
        i = p
    return out


def _v8_double_after(val, name):
    tag = b'"' + bytes([len(name)]) + name.encode()
    i = val.find(tag)
    if i < 0:
        return None
    p = i + len(tag)
    if p < len(val) and val[p] == 0x4E and p + 9 <= len(val):
        try:
            return struct.unpack_from('<d', val, p + 1)[0]
        except Exception:
            return None
    return None


def _ms_iso(ms):
    if not ms:
        return ''
    try:
        d = datetime.datetime.fromtimestamp(ms / 1000.0,
                                            datetime.timezone.utc)
        return d.strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (int(ms) % 1000)
    except Exception:
        return ''


def drafts(stores, log=None):
    """입력창 초안을 대화별로 복원한다.

    보낸 메시지가 아니라 **치던 중의 모습**이다. 섞지 않도록 item 은
    'draft' 로 따로 둔다. 같은 대화 안에서 시각순으로 쌓이면 타자 과정이
    그대로 보인다 — 오타를 고친 흔적까지.
    """
    rows = []
    for st in stores:
        # app.py 는 경로 문자열을, cli.py 는 Store 객체를 준다. 둘 다 받는다.
        if isinstance(st, str):
            if 'indexeddb' not in st.lower():
                continue
            path, origin = st, ''
            try:
                recs, _errs = leveldb.read_store(st, False, None)
            except Exception:
                continue
        else:
            if st.spec.get('key') != 'indexeddb':
                continue
            path, origin = st.spec.get('path', ''), st.spec.get('origin', '')
            recs = st.recs
        for r in recs:
            val = bytes(r.get('value') or b'')
            if not val or r.get('op') != 'put':
                continue
            conv = None
            for k in idb_key_strings(val):
                m = DRAFT_KEY.search(k)
                if m:
                    conv = m.group(1)
                    break
            if conv is None:
                continue
            texts = [t for t in _v8_strings_after(val, 'text') if t.strip()]
            if not texts:
                continue
            body = max(texts, key=len)
            if body.strip() in ('text', 'doc', 'paragraph'):
                continue
            rows.append({
                'scenario': 1, 'tier': 1, 'item': 'draft',
                'store': 'indexeddb', 'origin': origin,
                'file': r.get('file'), 'offset': r.get('offset'),
                'seq': r.get('seq'), 'state': 'draft',
                'conversation': '' if conv == 'chorus-unified-composer' else conv,
                'title': '(새 대화 입력창)' if conv == 'chorus-unified-composer' else '',
                'uuid': '', 'sender': 'human',
                'created_at': _ms_iso(_v8_double_after(val, 'updatedAt')),
                'text': body, 'decode': 'idb-key+v8',
                'key_kind': 'store:chat-draft:%s' % conv})
    if log and rows:
        convs = len({r['conversation'] or '(새 대화)' for r in rows})
        log.info('   초안 %d건 · 대화 %d개에 귀속', len(rows), convs)
    return rows


def extract(value, meta, blobs=None):
    """레코드 값 하나에서 대화 요소를 뽑는다.

    1차: 엄격 디코드 (외부 블롭이면 그 파일까지 따라간다)
    2차: 실패하면 문자열 수확(구제 모드) — 조각난 데이터용
    """
    items = []
    blob = leveldb.idb_value_blob(value)
    strict_ok = False
    try:
        obj = decode_ssv(blob)
        if isinstance(obj, dict) and '__external_blob' in obj:
            ref = obj['__external_blob']
            path = blobs.find(ref) if blobs else None
            meta = dict(meta)
            meta['external'] = '%s B → %s' % (ref['size'],
                                              os.path.basename(path) if path else '미수집')
            if not path:
                meta.update({'item': 'external-ref', 'uuid': '', 'title': '',
                             'sender': '', 'created_at': '', 'text': '',
                             'decode': 'external(미해결)'})
                return [meta], False
            with open(path, 'rb') as f:
                obj = decode_ssv(f.read())
        strict_ok = True
        conv = _conversation_rows(obj, meta)
        if conv:
            return conv, True
        for d in walk(obj):
            c = classify(d)
            if not c:
                continue
            row = dict(meta)
            row.update({
                'item': c,
                'uuid': d.get('uuid') or d.get('conversation_uuid') or '',
                'title': d.get('name') or d.get('summary') or d.get('file_name') or '',
                'sender': d.get('sender') or '',
                'created_at': d.get('created_at') or d.get('start_timestamp') or '',
                'text': _txt(d.get('text') or d.get('content') or d.get('prompt')),
                'decode': 'strict',
            })
            items.append(row)
    except (Truncated, Exception):
        pass
    if not items:
        for off, tag, s in harvest_strings(blob, min_len=8):
            if len(s) < 8:
                continue
            row = dict(meta)
            row.update({'item': 'fragment', 'uuid': '', 'title': '', 'sender': '',
                        'created_at': '', 'text': s[:MAX_TEXT],
                        'decode': 'salvage(%s)' % tag,
                        'frag_offset': off})
            items.append(row)
    return items, strict_ok


# ────────────────────────────────────────────────────────────────
# 저장소 로딩
# ────────────────────────────────────────────────────────────────
class Store(object):
    def __init__(self, spec, recs, errs):
        self.spec = spec
        self.recs = recs
        self.errs = errs

    @property
    def label(self):
        return '%s(%s)' % (self.spec['key'], self.spec.get('origin', ''))


def load(root, log, verify_crc=False):
    """루트 아래의 모든 LevelDB 저장소를 원시 파싱한다."""
    stores = []
    files = []
    for spec in config.resolve(root):
        if spec['kind'] != 'leveldb':
            continue
        try:
            recs, errs = leveldb.read_store(spec['path'], verify_crc, log)
        except OSError as e:
            log.error('%s 읽기 실패 — %s', spec['path'], e)
            continue
        for fn in os.listdir(spec['path']):
            p = os.path.join(spec['path'], fn)
            if os.path.isfile(p):
                files.append(p)
        log.info('%-28s 레코드 %6d개  (오류 %d)', spec['key'], len(recs), len(errs))
        stores.append(Store(spec, recs, errs))
    return stores, files


# ────────────────────────────────────────────────────────────────
# 시나리오 1 — 정상 상태
# ────────────────────────────────────────────────────────────────
def scenario1(stores, log, blobs=None):
    """현재 살아 있는 값만 본다. 기준선(baseline)을 만드는 단계."""
    rows = []
    for st in stores:
        live = leveldb.live_view(st.recs)
        log.info('[S1] %-20s 유효 키 %d개', st.spec['key'], len(live))
        for key, r in live.items():
            meta = {'scenario': 1, 'store': st.spec['key'],
                    'origin': st.spec.get('origin', ''),
                    'file': r['file'], 'offset': r['offset'], 'seq': r['seq'],
                    'key_kind': leveldb.idb_kind(key) if st.spec['key'] == 'indexeddb'
                                else key[:80].decode('utf-8', 'replace'),
                    'state': 'live'}
            items, _ = extract(r['value'], meta, blobs)
            rows += items
    return rows


# ────────────────────────────────────────────────────────────────
# 시나리오 2 — 대화/세션 삭제
# ────────────────────────────────────────────────────────────────
def scenario2(stores, root, log, markers=None, blobs=None):
    """삭제·덮어쓰기로 현재 뷰에서 사라진 값을 되살린다.

    원리: LevelDB 는 제자리 수정을 하지 않는다(append-only). 삭제는 값을
    지우는 게 아니라 '삭제 표시(tombstone)' 를 뒤에 덧붙이는 것이고,
    수정도 새 값을 덧붙이는 것이다. 컴팩션이 일어나기 전까지 이전 값의
    바이트는 .log/.ldb 안에 그대로 남아 있다. 정식 API 로 열면 최신 시퀀스만
    보이지만, 파일을 바이트로 읽으면 전부 보인다.
    """
    rows = []
    for st in stores:
        sh = leveldb.shadowed(st.recs)
        ndel = sum(1 for _, s in sh if s == 'deleted')
        nov = sum(1 for _, s in sh if s == 'overwritten')
        log.info('[S2] %-20s 삭제 %d · 덮어쓰기 %d · 툼스톤 %d',
                 st.spec['key'], ndel, nov,
                 sum(1 for _, s in sh if s == 'tombstone'))
        for r, state in sh:
            if r['op'] != 'put':
                rows.append({'scenario': 2, 'store': st.spec['key'],
                             'file': r['file'], 'offset': r['offset'],
                             'seq': r['seq'], 'state': 'tombstone',
                             'item': 'tombstone', 'uuid': '', 'title': '',
                             'sender': '', 'created_at': '', 'text': '',
                             'decode': '-',
                             'key_kind': leveldb.idb_kind(r['key'])})
                continue
            meta = {'scenario': 2, 'store': st.spec['key'],
                    'origin': st.spec.get('origin', ''),
                    'file': r['file'], 'offset': r['offset'], 'seq': r['seq'],
                    'key_kind': leveldb.idb_kind(r['key']) if st.spec['key'] == 'indexeddb'
                                else r['key'][:80].decode('utf-8', 'replace'),
                    'state': state}
            items, _ = extract(r['value'], meta, blobs)
            rows += items

    # SQLite 쪽 — 프리블록/비할당 카빙
    rows += sqlite_dead(root, log, markers)
    return rows


def sqlite_dead(root, log, markers=None):
    """Cookies 등 SQLite 의 삭제 잔재.

    DELETE 는 셀 포인터만 제거한다. 본문은 프리블록으로 남는다.
    WAL 이 있으면 페이지의 과거 버전까지 통째로 남아 있다.
    """
    rows = []
    for spec in config.resolve(root):
        if spec['kind'] != 'sqlite':
            continue
        p = spec['path']
        try:
            live, dead, stats = sqlitefs.scan_db(p)
        except Exception as e:
            log.warning('%s SQLite 파싱 실패 — %s', p, e)
            continue
        fb = sum(1 for d in dead if d['state'] == 'freeblock')
        log.info('[S2] %-20s 페이지 %d · 유효셀 %d · 프리블록 %d · 유휴 %d B',
                 spec['key'], len(stats), len(live), fb,
                 sum(d['size'] for d in dead))
        for d in dead:
            hits = carve.scan(d['data'], source='%s p%d %s' % (
                spec['key'], d['page'], d['state']), markers=markers)
            txt = [s for _o, _e, s in carve.strings(d['data'], 6)]
            if not hits and not txt:
                continue
            rows.append({
                'scenario': 2, 'store': spec['key'], 'file': os.path.basename(p),
                'offset': d['page'] * 0x10000 + d['offset'], 'seq': '',
                'key_kind': 'page %d / %s' % (d['page'], d['state']),
                'state': 'deleted-space', 'item': 'sqlite-remnant',
                'uuid': next((h['value'] for h in hits if h['kind'] == 'uuid'), ''),
                'title': '', 'sender': '', 'created_at': '',
                'text': ' | '.join(txt)[:MAX_TEXT],
                'decode': 'carve', 'hits': [h['kind'] for h in hits],
            })
        # WAL / 저널
        for kind, sp in sqlitefs.sidecars(p).items():
            try:
                if kind == 'wal':
                    frames, meta = sqlitefs.scan_wal(sp)
                    seen = {}
                    for fr in frames:
                        seen.setdefault(fr['page'], []).append(fr)
                    older = [f for lst in seen.values() for f in lst[:-1]]
                    log.info('[S2] %s WAL 프레임 %d개 · 과거판 %d개',
                             spec['key'], len(frames), len(older))
                    for fr in older:
                        hits = carve.scan(fr['data'], source='wal p%d' % fr['page'],
                                          markers=markers)
                        if not hits:
                            continue
                        rows.append({
                            'scenario': 2, 'store': spec['key'],
                            'file': os.path.basename(sp), 'offset': fr['offset'],
                            'seq': fr['frame'], 'key_kind': 'WAL page %d' % fr['page'],
                            'state': 'wal-superseded', 'item': 'wal-frame',
                            'uuid': next((h['value'] for h in hits
                                          if h['kind'] == 'uuid'), ''),
                            'title': '', 'sender': '', 'created_at': '',
                            'text': '', 'decode': 'carve',
                            'hits': sorted({h['kind'] for h in hits}),
                        })
                elif kind == 'journal':
                    recs, meta = sqlitefs.scan_journal(sp)
                    log.info('[S2] %s 롤백저널 페이지 %d개', spec['key'], len(recs))
            except Exception as e:
                log.warning('%s 부속파일 파싱 실패 — %s', sp, e)
    return rows


# ────────────────────────────────────────────────────────────────
# 시나리오 3 — 계정 삭제 / 로그아웃
# ────────────────────────────────────────────────────────────────
BINARY_DIRS = ('cache', 'codecache')


def scenario3(root, log, baseline=None, markers=None):
    """계정 삭제 이후에 '무엇이 실제로 남았는가'.

    ★ 이 시나리오에서 가장 중요한 건 복원 성공률이 아니라 라벨링이다.
      - SURVIVED  : 삭제 후 이미지에도 존재 → 진짜 잔존물
      - RECOVERED : 삭제 전 수집분에만 존재 → 우리가 미리 떠놨기 때문에 보이는 것
      이 둘을 섞어서 '복원했다' 고 쓰면 그 보고서는 틀린 주장이 된다.
    """
    rows = []
    post = set()

    # 1) 남아 있는 파일 전수 카빙 — 구조를 가정하지 않는다
    for spec in config.resolve(root):
        targets = []
        if spec['kind'] in ('leveldb',):
            targets = [os.path.join(spec['path'], f)
                       for f in os.listdir(spec['path'])]
        elif spec['kind'] in ('blob', 'text'):
            for dp, _dn, fns in os.walk(spec['path']):
                targets += [os.path.join(dp, f) for f in fns]
        elif spec['kind'] == 'json':
            targets = [spec['path']]
        for t in targets:
            if not os.path.isfile(t):
                continue
            try:
                with open(t, 'rb') as f:
                    buf = f.read()
            except OSError as e:
                log.warning('%s 읽기 실패 — %s', t, e)
                continue
            hits = carve.scan(buf, source=os.path.basename(t), markers=markers)
            harv = harvest_strings(buf, min_len=12) if spec['kind'] == 'leveldb' else []
            for h in hits:
                key = (h['kind'], str(h['value']))
                post.add(key)
                rows.append({
                    'scenario': 3, 'store': spec['key'],
                    'file': os.path.relpath(t, root), 'offset': h['offset'],
                    'seq': '', 'key_kind': h['kind'], 'encoding': h['encoding'],
                    'state': 'present-after-deletion', 'status': 'SURVIVED',
                    'item': h['kind'], 'uuid': h['value'] if h['kind'] == 'uuid' else '',
                    'title': '', 'sender': '', 'created_at': '',
                    'text': str(h['value'])[:MAX_TEXT], 'decode': 'carve',
                })
            for off, tag, s in harv:
                if len(s) < 16:
                    continue
                post.add(('text', s))
                rows.append({
                    'scenario': 3, 'store': spec['key'],
                    'file': os.path.relpath(t, root), 'offset': off, 'seq': '',
                    'key_kind': tag, 'encoding': tag,
                    'state': 'present-after-deletion', 'status': 'SURVIVED',
                    'item': 'fragment', 'uuid': '', 'title': '', 'sender': '',
                    'created_at': '', 'text': s[:MAX_TEXT], 'decode': 'salvage',
                })

    # 2) 삭제 전 수집분과 대조 — 잔존인지 사전확보인지 가른다
    if baseline:
        log.info('[S3] 기준 수집분 대조 시작 — %s', baseline)
        blog = log
        bstores, _bf = load(baseline, blog)
        for st in bstores:
            for key, r in leveldb.live_view(st.recs).items():
                items, _ = extract(r['value'], {
                    'scenario': 3, 'store': st.spec['key'],
                    'file': r['file'], 'offset': r['offset'], 'seq': r['seq'],
                    'key_kind': leveldb.idb_kind(key), 'encoding': '',
                })
                for it in items:
                    t = it.get('text') or ''
                    hit = ('text', t) in post or ('uuid', it.get('uuid')) in post
                    it['state'] = 'baseline-only' if not hit else 'also-after'
                    it['status'] = 'SURVIVED' if hit else 'RECOVERED'
                    rows.append(it)
        log.info('[S3] 잔존 %d건 · 사전확보분 %d건',
                 sum(1 for r in rows if r.get('status') == 'SURVIVED'),
                 sum(1 for r in rows if r.get('status') == 'RECOVERED'))
    else:
        log.warning('[S3] --baseline 없이 실행됨 — 모든 소견이 "잔존" 으로만 '
                    '기록된다. 삭제 전 수집분 없이는 잔존/복원 구분이 불가능하다.')
    return rows
