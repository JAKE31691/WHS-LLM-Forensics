# -*- coding: utf-8 -*-
r"""Local Storage 파서 — 행위 재구성의 1순위 아티팩트.

    <APP>\Local Storage\leveldb\

여기에 앱이 스스로 남긴 행위 기록이 **평문 JSON** 으로 들어 있다.
복호화도 헥스도 필요 없다. 실측(B01)에서 키 21종 중 6종이 행위를 직접 말했다.

    antalytics_queue_v1              이벤트 83건 · 24종 (이름 + 밀리초 + 계정)
    claude-conversation-ledger       대화별 시각 + d=1 삭제 플래그
    conversation-revisits-device     대화별 마지막 열람 시각
    static-composer-draft-data       전송하지 않은 입력 본문
    epitaxy-context-usage-logout-at  로그아웃 시각
    persisted.desktop-version-seen   앱 버전 + 최초 실행 시각

★ 옛 버전을 버리지 않는다.
  LevelDB 는 덮어쓰지 않고 뒤에 이어 붙인다. 같은 키의 스냅샷이 여러 벌
  남아 있고, 그 차이가 곧 증거다. 실측에서 장부 스냅샷 하나는 d=1 이
  없었고 다음 스냅샷에는 있었다 — 그 사이가 삭제 시각 구간이다.
  최신 것만 읽으면 이 구간을 통째로 잃는다.

이 파서는 행위를 모른다. '무엇이 적혀 있다' 까지만 말하고, 그게 어떤
행위인지는 rules.py 가 판정한다.
"""
import json
import os
import re

from chattrace import leveldb

from .schema import Observation, iso_to_ms, utc_ms

# claude.ai 로컬스토리지 키는 '_https://claude.ai\x00\x01<키>' 꼴이다
PREFIX = b'_https://claude.ai\x00\x01'
META = b'META:'

WANTED = {
    'antalytics_queue_v1': 'app_event_queue',
    'claude-conversation-ledger': 'conversation_ledger',
    'conversation-revisits-device': 'conversation_revisits',
    'static-composer-draft-data': 'composer_draft',
    'epitaxy-context-usage-logout-at': 'logout_at',
    'persisted.desktop-version-seen': 'app_version_seen',
    'chat-draft-write-signal': 'draft_write_signal',
    # 초안을 지운 시각. Pro 환경 실측에서 확인한 키다(2026-09-21).
    # write-signal 은 "쳤다" 를 말하고 이쪽은 "지웠다" 를 말한다 — Q5 직결.
    'chat-draft-wipe-completed-at': 'draft_wiped_at',
    'has_completed_desktop_first_login': 'first_login_done',
    'spa:locale': 'locale',
}

# 접두어가 붙은 채로 UUID 가 따라오는 키들
PREFIXED = {
    'conversation-store:unsaved:': 'conversation_unsaved',
}


def _decode_value(b):
    """LocalStorage 값은 앞 1바이트가 인코딩 표시다. 0=UTF-16LE, 1=UTF-8."""
    if not b:
        return ''
    if b[0] == 0:
        return b[1:].decode('utf-16-le', 'replace')
    if b[0] == 1:
        return b[1:].decode('utf-8', 'replace')
    try:
        return b.decode('utf-8')
    except UnicodeDecodeError:
        return b.decode('latin1', 'replace')


def _split_key(raw):
    """레코드 키 -> 로컬스토리지 키 이름. 아니면 None."""
    if not raw.startswith(PREFIX):
        return None
    return raw[len(PREFIX):].decode('latin1', 'replace')


def find_stores(root):
    r"""수집물 아래에서 Local Storage\leveldb 폴더를 전부 찾는다."""
    out = []
    for dp, dn, fns in os.walk(root):
        low = dp.lower().replace('/', '\\')
        if low.endswith('local storage\\leveldb'):
            out.append(dp)
    return out


def parse(root, ids, log=None, env_profile=''):
    """-> [Observation]

    같은 키의 모든 스냅샷을 순서(seq)와 함께 낸다. 최신만 남기지 않는다.
    """
    say = log or (lambda *a: None)
    obs = []
    stores = find_stores(root)
    if not stores:
        say('   Local Storage 를 찾지 못했다')
        return obs

    for sp in stores:
        try:
            recs, errs = leveldb.read_store(sp, False, None)
        except OSError as e:
            say('   %s 읽기 실패 — %s' % (sp, e))
            continue
        rel = os.path.relpath(sp, root)
        say('   [%s] 레코드 %s개' % (rel[-44:], format(len(recs), ',')))

        for r in recs:
            raw = bytes(r['key'])
            name = _split_key(raw)
            if name is None:
                continue
            kind = WANTED.get(name)
            suffix = ''
            if kind is None:
                for pre, k in PREFIXED.items():
                    if name.startswith(pre):
                        kind, suffix = k, name[len(pre):]
                        break
            if kind is None:
                continue

            loc = '%s+0x%X seq=%s key=%s' % (
                os.path.basename(r.get('file') or ''), r.get('offset') or 0,
                r.get('seq'), name[:60])

            if r['op'] == 'del':
                obs.append(Observation(
                    ids.next('obs'), 'localstorage', rel, loc,
                    kind + '_deleted', {'key': name, 'suffix': suffix},
                    seq=r.get('seq'), env_profile=env_profile,
                    note='이 키가 지워진 시점의 기록'))
                continue

            text = _decode_value(bytes(r['value'] or b''))
            obs.extend(_expand(ids, kind, name, suffix, text, rel, loc,
                               r.get('seq'), env_profile))
    return obs


def _expand(ids, kind, name, suffix, text, rel, loc, seq, env):
    """값 하나를 관찰 여러 개로 편다. 이벤트 큐는 배열이라 건마다 나눈다."""
    out = []

    def add(ft, fact, ts=None, actor='', status='ok', note=''):
        out.append(Observation(ids.next('obs'), 'localstorage', rel, loc,
                               ft, fact, ts=ts, seq=seq, actor=actor,
                               env_profile=env, status=status, note=note))

    if kind == 'app_event_queue':
        try:
            arr = json.loads(text)
        except ValueError:
            add(kind, {'raw': text[:200]}, status='partial',
                note='JSON 으로 못 읽었다')
            return out
        if not isinstance(arr, list):
            arr = [arr]
        for e in arr:
            if not isinstance(e, dict):
                continue
            ms = iso_to_ms(e.get('eventTimestamp'))
            props = e.get('properties') or {}
            add('app_event', {
                'event': e.get('eventName') or '',
                'event_id': e.get('eventId') or '',
                'org': e.get('organizationUuid') or '',
                'props': {k: v for k, v in props.items()
                          if k not in ('account_uuid', 'organization_uuid')},
            }, ts={'ts_event': utc_ms(ms) if ms else
                   (e.get('eventTimestamp') or '')},
                actor=e.get('accountUuid') or '')
        return out

    if kind == 'conversation_ledger':
        try:
            arr = json.loads(text)
        except ValueError:
            add(kind, {'raw': text[:200]}, status='partial')
            return out
        # 스냅샷 전체를 하나로도 남긴다 — 이후 스냅샷 대조에 쓴다
        add('ledger_snapshot',
            {'count': len(arr),
             'uuids': [x.get('u') for x in arr if isinstance(x, dict)],
             'deleted': [x.get('u') for x in arr
                         if isinstance(x, dict) and x.get('d')]})
        for x in arr:
            if not isinstance(x, dict):
                continue
            add('ledger_entry', {
                'conversation': x.get('u') or '',
                'deleted_flag': bool(x.get('d')),
                'flags': {k: v for k, v in x.items() if k not in ('u', 'at')},
            }, ts={'ts_record': utc_ms(x.get('at'))})
        return out

    if kind == 'conversation_revisits':
        try:
            o = json.loads(text)
        except ValueError:
            add(kind, {'raw': text[:200]}, status='partial')
            return out
        owner = o.get('owner') or ''
        for pair in o.get('entries') or []:
            if not (isinstance(pair, list) and len(pair) == 2):
                continue
            add('conversation_revisit',
                {'conversation': pair[0], 'evictions': o.get('evictions')},
                ts={'ts_record': utc_ms(pair[1])}, actor=owner)
        return out

    if kind == 'composer_draft':
        try:
            o = json.loads(text)
        except ValueError:
            add(kind, {'raw': text[:200]}, status='partial')
            return out
        add('composer_draft', {
            'text': o.get('text') or '',
            'chars': len(o.get('text') or ''),
            'attachments': o.get('attachmentCount'),
            'files': o.get('fileCount'),
            'session_marker': o.get('sessionMarker'),
        }, ts={'ts_record': utc_ms(o.get('updatedAt'))},
            actor=o.get('accountUuid') or '')
        return out

    if kind == 'logout_at':
        ms = re.sub(r'\D', '', text)[:13]
        add('logout_at', {'raw': text[:40]},
            ts={'ts_record': utc_ms(ms)} if ms else None)
        return out

    if kind == 'draft_wiped_at':
        # logout_at 과 같은 모양 — 밀리초 문자열 하나.
        ms = re.sub(r'\D', '', text)[:13]
        add('draft_wiped_at', {'raw': text[:40]},
            ts={'ts_record': utc_ms(ms)} if ms else None,
            status='ok' if ms else 'partial')
        return out

    if kind == 'app_version_seen':
        try:
            o = json.loads(text)
        except ValueError:
            add(kind, {'raw': text[:200]}, status='partial')
            return out
        add('app_version_seen',
            {'version': o.get('version') or '', 'docs': o.get('docs')},
            ts={'ts_record': utc_ms(o.get('firstSeenEpochMs'))})
        return out

    if kind == 'conversation_unsaved':
        add('conversation_unsaved',
            {'conversation': suffix, 'bytes': len(text)})
        return out

    add(kind, {'key': name, 'value': text[:300]})
    return out
