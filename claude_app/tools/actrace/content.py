# -*- coding: utf-8 -*-
r"""Q3·Q4·Q5 — 무엇을 묻고, 넣고 뺐고, 감추려 했나.

우리 선행 연구가 찾아 둔 자리를 그대로 판다.

  R1 · 업로드가 남긴 원본 경로
      IndexedDB 000003.log 에 업로드 레코드가 통째로 남는다.
      file_kind "blob" → file_uuid → file_name → created_at
      → size_bytes → localPath "C:\TestData\TEST-A-paper-small.pdf"
      ASCII 로 저장돼 인코딩을 바꾸지 않아도 읽힌다.
      같은 파일을 다시 올리면 새 uuid 가 발급된다.

  R3 · 임시 대화가 남긴 것
      내용은 안 남는다. 그러나 **식별자는 남는다.**
      Session Storage 의 incognito_temporary_conversation_uuid 키.
      지운 흔적조차 남는다 — 값이 있다가 길이 0 으로 바뀐다.

  행위 07 · 프롬프트 수정
      수정으로 지운 원문이 남는 정도가 아니라 **사본이 3배로 늘었다.**
      수정은 기존 메시지를 갱신하지 않고 새 메시지를 만들어서,
      원문과 수정본이 별개 레코드로 공존한다.
      메시지 UUID 는 UUIDv7 이라 상위 48비트가 생성 밀리초다.

★ UUIDv7 역산이 이 모듈의 핵심 도구다.
  식별자 하나에서 시각이 나온다. 내용이 사라져도 '언제' 는 남는다.
"""
import os
import re

from chattrace import leveldb, codec
from chattrace.scenarios import BlobIndex

from .schema import Observation, utc_ms

UUID_RX = re.compile(
    rb'([0-9a-f]{8})-([0-9a-f]{4})-7([0-9a-f]{3})-([89ab][0-9a-f]{3})'
    rb'-([0-9a-f]{12})', re.I)

# R1 이 찾은 업로드 레코드의 필드. ASCII 로 그대로 박혀 있다.
LOCALPATH_RX = re.compile(
    rb'localPath"?.{0,4}?([A-Za-z]:\\[^\x00-\x1f"]{2,200})')
FILENAME_RX = re.compile(rb'file_name"?.{0,4}?([^\x00-\x1f"]{1,120})')


def _v8_cut(buf, m, g=1):
    """정규식이 잡은 문자열을 V8 길이 바이트로 자른다.

    V8 문자열은 `0x22 <길이> <바이트>` 다. 객체의 마지막 속성이면 바로
    뒤에 객체 끝 태그 `{`(0x7B)가 붙는데, 정규식은 따옴표·제어문자에서만
    끊으니 `S01-chart.png{` 처럼 그것까지 먹는다. 앞의 길이를 읽을 수
    있으면 그 길이로 자르고, 아니면(JSON 문자열 등) 잡은 그대로 둔다.
    """
    s, b = m.start(g), m.group(g)
    pre = bytearray(buf[max(0, s - 3):s])
    # 길이는 varint 다 — 127 자까지 1바이트, 그 위로 2바이트
    for k in (1, 2):
        if len(pre) < k + 1 or pre[-k - 1] != 0x22:
            continue
        lb = pre[-k:]
        if any(x & 0x80 for x in lb[:-1]) != (k > 1) or lb[-1] & 0x80:
            continue
        n = 0
        for i, x in enumerate(lb):
            n |= (x & 0x7F) << (7 * i)
        if 0 < n <= len(b):
            return b[:n]
    return b

# R3 이 찾은 임시 대화 키
TEMP_RX = re.compile(rb'incognito_temporary_conversation_uuid')

LEVELDB_EXT = ('.log', '.ldb', '.sst')


def uuid7_ms(u):
    """UUIDv7 -> 생성 밀리초. 상위 48비트가 Unix 밀리초다."""
    try:
        h = u.replace('-', '')
        return int(h[:12], 16)
    except (ValueError, AttributeError):
        return 0


def _stores(root):
    out = []
    for dp, dn, fns in os.walk(root):
        if any(f.endswith(LEVELDB_EXT) for f in fns):
            out.append(dp)
    return out


def _label(path, root):
    low = os.path.relpath(path, root).lower().replace('/', '\\')
    for key, name in (('session storage', 'session'),
                      ('local storage', 'local'),
                      ('indexeddb', 'indexeddb')):
        if key in low:
            return name
    return 'other'


def _payloads(val, blobs):
    """값 하나에서 '읽을 수 있는 바이트' 를 전부 낸다.

    Chromium 은 IndexedDB 값을 그냥 두지 않는다. 크면 외부 .blob 파일로
    빼고(FF 11 01), 중간 크기면 snappy 로 압축한다(FF 11 02). 둘 다
    정규식이 안 먹는다 — 찾는 글자가 원문에 없기 때문이다.

    ★ 실측(2026-09-21, Pro 환경): ObjectStoreData 275건 중 220건(80%)이
      외부로 빠져 있었다. 이걸 풀지 않으면 업로드 레코드를 통째로 놓친다.
      업로드가 있는 대화일수록 커져서 외부로 빠지므로, 하필 찾으려는
      것만 빠지는 구조다.

    -> [(바이트, 출처설명)]
    """
    out = [(val, '')]
    try:
        blob = leveldb.idb_value_blob(val)
        kind, payload = codec.unwrap_idb_value(blob)
    except Exception:
        return out
    if kind == 'snappy' and payload:
        out.append((payload, 'snappy 압축을 푼 본문'))
    elif kind == 'external' and blobs is not None:
        path = blobs.find(payload)
        if path:
            try:
                with open(path, 'rb') as f:
                    out.append((f.read(), '외부 블롭 %s'
                                % os.path.basename(path)))
            except OSError:
                pass
    return out


def parse(root, ids, log=None, env_profile='', convs=None):
    say = log or (lambda *a: None)
    obs = []
    n_up = n_tmp = n_uu = 0
    n_ext = 0
    try:
        blobs = BlobIndex(extra_dirs=[root])
    except Exception:
        blobs = None

    for sp in _stores(root):
        label = _label(sp, root)
        rel = os.path.relpath(sp, root)
        try:
            recs, _errs = leveldb.read_store(sp, False, None)
        except Exception as e:
            # ★ 조용히 넘기지 않는다. 저장소 하나를 통째로 못 읽었는데
            #   아무 기록이 없으면 '행위가 없었다' 로 오독된다.
            obs.append(Observation(
                ids.next('obs'), 'indexeddb', rel, '', 'store_unreadable',
                {'store': label,
                 'error': '%s: %s' % (type(e).__name__, e)},
                status='error', env_profile=env_profile,
                note='저장소를 열지 못했다. 이 경로는 보지 못한 것이다'))
            continue
        for r in recs:
            val = bytes(r.get('value') or b'')
            if not val:
                continue
            loc = '%s+0x%X seq=%s' % (os.path.basename(r.get('file') or ''),
                                      r.get('offset') or 0, r.get('seq'))

            # 값이 포장돼 있으면 풀어서 같이 훑는다. 푸는 데 실패해도
            # 원본은 그대로 남아 있어 기존 동작은 줄지 않는다.
            bufs = _payloads(val, blobs)
            if len(bufs) > 1:
                n_ext += 1

            # ── Q4 업로드: 원본 절대 경로 ────────────────────
            for buf, src in bufs:
                for m in LOCALPATH_RX.finditer(buf):
                    p = _v8_cut(buf, m).decode('latin1', 'replace').rstrip('"\\ ')
                    if len(p) < 4:
                        continue
                    fn = ''
                    fm = FILENAME_RX.search(buf, max(0, m.start() - 400), m.start())
                    if fm:
                        fn = _v8_cut(buf, fm).decode('utf-8', 'replace')[:120]
                    n_up += 1
                    obs.append(Observation(
                        ids.next('obs'), 'indexeddb', rel, loc, 'file_upload',
                        {'local_path': p, 'file_name': fn, 'store': label,
                         'state': 'live' if r['op'] == 'put' else 'deleted',
                         'source': src or '레코드 본문'},
                        seq=r.get('seq'), env_profile=env_profile,
                        note='업로드 레코드에 원본 절대 경로가 남는다 (R1)'))

            # ── Q5 임시 대화: 식별자 잔존 ────────────────────
            if any(TEMP_RX.search(b) for b, _ in bufs):
                found = [m.group(0).decode('latin1')
                         for b, _ in bufs for m in UUID_RX.finditer(b)]
                n_tmp += 1
                obs.append(Observation(
                    ids.next('obs'), label, rel, loc, 'temporary_conversation',
                    {'store': label, 'uuids': found[:8],
                     'value_len': len(val),
                     'cleared': len(val) < 8,
                     'state': 'live' if r['op'] == 'put' else 'deleted'},
                    seq=r.get('seq'), env_profile=env_profile,
                    note='임시 대화는 내용이 안 남아도 식별자는 남는다 (R3). '
                         '값 길이가 0 이면 지운 흔적이다'))

    # ── Q3/Q5 메시지 UUIDv7 -> 시각 ─────────────────────────
    # 대화 복원 결과에서 UUID 를 모아 시각을 역산한다. 내용이 사라져도
    # 식별자만 있으면 '언제' 가 나온다.
    for c in (convs or []):
        cu = (c.get('uuid') or '').strip()
        title = c.get('title') or ''
        state = c.get('state') or ''
        for m in (c.get('messages') or []):
            u = (m.get('uuid') or '').strip()
            if not u or not UUID_RX.fullmatch(u.encode('latin1', 'ignore')):
                continue
            ms = uuid7_ms(u)
            if not ms:
                continue
            n_uu += 1
            obs.append(Observation(
                ids.next('obs'), 'indexeddb', 'conversations.json',
                'uuid=%s' % u, 'message_identity',
                {'conversation': cu, 'title': title[:80],
                 'sender': m.get('sender') or '', 'conv_state': state,
                 'chars': len((m.get('text') or ''))},
                ts={'ts_uuid7': utc_ms(ms)}, env_profile=env_profile,
                note='UUIDv7 상위 48비트가 생성 밀리초다'))

    say('   업로드 %d · 임시대화 %d · 메시지 식별자 %d' % (n_up, n_tmp, n_uu))
    if n_ext:
        say('   포장된 값 %d건을 풀어서 같이 훑음 (외부 블롭·snappy)' % n_ext)
    return obs
