# -*- coding: utf-8 -*-
"""비할당·조각 영역용 카빙 스캐너.

구조가 남아 있으면 leveldb/sqlitefs 파서가 처리한다.
여기는 '구조가 없는 바이트 더미' 전용이다 — 캐시 블록, 프리블록 본문,
슬랙, 메모리 덤프, 이미지의 비할당 영역.
"""
import re

from .codec import looks_utf16 as codec_looks_utf16

# ── 왜 이런 형태로 찾나 ───────────────────────────────────────────
# JSON 이 그대로 저장된 게 아니라 V8 structured clone 으로 저장되므로
# 키 이름 앞뒤에 따옴표가 없다. 태그(0x22) + 길이 varint + 이름 순이다.
# 그래서 '"start_timestamp"' 로 찾으면 0건이고 'start_timestamp' 로 찾아야 한다.
# (R5 에서 실제로 겪은 함정)
FIELD_NAMES = [
    'uuid', 'conversation_uuid', 'parent_message_uuid', 'leaf_message_uuid',
    'name', 'summary', 'prompt', 'text', 'content', 'sender',
    'created_at', 'updated_at', 'start_timestamp', 'stop_timestamp',
    'file_name', 'file_size', 'file_uuid', 'extracted_content',
    'model', 'settings', 'attachments', 'files', 'account', 'organization',
]

PATTERNS = {
    'uuid': rb'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    'iso8601': rb'20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}',
    'email': rb'[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,64}\.[A-Za-z]{2,10}',
    'url': rb'https?://[A-Za-z0-9./_%#?=&:+-]{6,200}',
    'jwt': rb'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}',
    'sk_token': rb'sk-ant-[A-Za-z0-9_-]{16,}',
    'session_key': rb'sessionKey=?[A-Za-z0-9_-]{16,}',
}

# 값을 통째로 리포트에 쓰면 안 되는 것들 — 존재/오프셋/길이만 남긴다.
SECRET_KINDS = {'jwt', 'sk_token', 'session_key'}


def redact(kind, value):
    if kind not in SECRET_KINDS:
        return value
    s = value.decode('latin-1', 'replace') if isinstance(value, bytes) else str(value)
    head = s[:12]
    return '%s…[%d자 생략]' % (head, max(0, len(s) - 12))


def scan(buf, source='', patterns=None, markers=None, context=0):
    """바이트 더미에서 패턴을 찾는다. -> 소견 리스트"""
    hits = []
    # 빈 dict 은 '패턴 없이 마커만' 이라는 뜻이다. or 를 쓰면 falsy 라
    # 전체 패턴셋으로 되돌아가 마커 집계가 오염된다.
    pats = PATTERNS if patterns is None else patterns
    for kind, pat in pats.items():
        for m in re.finditer(pat, buf, re.I):
            v = m.group(0)
            hits.append({
                'kind': kind, 'encoding': 'ascii', 'offset': m.start(),
                'value': redact(kind, v), 'source': source,
                'context': buf[max(0, m.start() - context):m.end() + context].hex()
                           if context else '',
            })
    # UTF-16LE 는 널 바이트를 걷어내고 다시 훑는 쪽이 정확하다
    if _looks_utf16(buf):
        for shift in (0, 1):                    # 정렬이 한 바이트 밀린 경우 대비
            stripped = bytes(buf[shift::2])
            for kind, pat in pats.items():
                for m in re.finditer(pat, stripped, re.I):
                    hits.append({
                        'kind': kind, 'encoding': 'utf-16le',
                        'offset': m.start() * 2 + shift,
                        'value': redact(kind, m.group(0)), 'source': source,
                        'context': '',
                    })
    for mk in (markers or []):
        mb = mk.encode('ascii', 'ignore')
        for enc, needle in (('ascii', mb), ('utf-16le', mk.encode('utf-16-le'))):
            start = 0
            while True:
                i = buf.find(needle, start)
                if i < 0:
                    break
                hits.append({'kind': 'marker', 'encoding': enc, 'offset': i,
                             'value': mk, 'source': source, 'context': ''})
                start = i + 1
    return hits


def _looks_utf16(buf, sample=4096):
    """codec.looks_utf16 을 쓴다 — 한글 UTF-16 을 놓치던 판정을 고쳤다."""
    return codec_looks_utf16(buf, sample)


def field_offsets(buf, names=None):
    """V8 필드 이름이 실제로 박혀 있는 위치를 찾는다 (따옴표 없이)."""
    out = []
    for nm in (names or FIELD_NAMES):
        nb = nm.encode('ascii')
        start = 0
        while True:
            i = buf.find(nb, start)
            if i < 0:
                break
            # 앞 2바이트가 [문자열태그][길이] 형태인지 확인 → 오탐 억제
            tagged = i >= 2 and buf[i - 2] in (0x22, 0x53) and buf[i - 1] == len(nb)
            out.append({'name': nm, 'offset': i, 'v8_tagged': tagged})
            start = i + 1
    return out


def strings(buf, min_len=6, encodings=('ascii', 'utf-16le')):
    """고전적 strings — 최후의 수단."""
    out = []
    if 'ascii' in encodings:
        for m in re.finditer(rb'[\x20-\x7e]{%d,}' % min_len, buf):
            out.append((m.start(), 'ascii', m.group(0).decode('ascii')))
    if 'utf-16le' in encodings:
        for m in re.finditer(rb'(?:[\x20-\x7e]\x00){%d,}' % min_len, buf):
            out.append((m.start(), 'utf-16le', m.group(0).decode('utf-16-le')))
    return out
