"""ChatGPT Web surface processing (RawRecord -> Observation).

The common parsers preserve native records.  This module is the first place
where Chromium values are joined, URLs are split and ChatGPT-specific fields
are named.  Every Observation retains ``parsed_record`` for analyst review.
"""

from __future__ import annotations

import gzip
import json
import re
import zlib
from collections.abc import Mapping
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlsplit

from llmtrace.core.schema import Ids, Observation, RawRecord


_CHATGPT_HOSTS = {'chatgpt.com', 'auth.openai.com'}
_URL_RE = re.compile(rb'https?://[^\x00\s"<>]+', re.I)
_URL_TEXT_RE = re.compile(r'https?://[^\x00\s"<>]+', re.I)


def _text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        for encoding in ('utf-8', 'utf-16-le', 'latin1'):
            try:
                return raw.decode(encoding).strip('\x00')
            except UnicodeDecodeError:
                continue
        return raw.decode('utf-8', 'replace').strip('\x00')
    return str(value)


def _query(value: str) -> dict[str, Any]:
    return {
        key: values[-1] if len(values) == 1 else values
        for key, values in parse_qs(value, keep_blank_values=True).items()
    }


def _url(value: Any) -> dict[str, Any]:
    text = _text(value)
    try:
        parsed = urlsplit(text)
    except ValueError:
        return {'url': text, 'scheme': '', 'hostname': '', 'path': '', 'query': {}}
    fragment_route, _, fragment_query = parsed.fragment.partition('?')
    return {
        'url': text,
        'scheme': parsed.scheme.lower(),
        'hostname': (parsed.hostname or '').lower(),
        'path': unquote(parsed.path or '/'),
        'query': _query(parsed.query),
        'fragment_route': unquote(fragment_route).lstrip('/'),
        'fragment_query': _query(fragment_query),
    }


def _cache_headers(metadata: Any) -> dict[str, str]:
    """Return normalized response headers preserved by the cache parser."""
    if not isinstance(metadata, Mapping):
        return {}
    candidates = (
        metadata.get('_attributes'),
        metadata.get('attributes'),
        metadata.get('http_header_attributes'),
    )
    out: dict[str, str] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        for key, value in candidate.items():
            if isinstance(value, (list, tuple)):
                text = _text(value[-1]) if value else ''
            else:
                text = _text(value)
            if text:
                out[str(key).lower()] = text
    return out


def _cache_status_code(metadata: Any) -> int | None:
    """Extract the HTTP response status preserved in Chromium cache metadata."""
    if not isinstance(metadata, Mapping):
        return None
    declarations = (
        metadata.get('_declarations')
        or metadata.get('declarations')
        or metadata.get('http_header_declarations')
        or []
    )
    if isinstance(declarations, str):
        declarations = [declarations]
    for declaration in declarations:
        match = re.search(r'HTTP/\S+\s+(\d{3})\b', _text(declaration), re.I)
        if match:
            return int(match.group(1))
    return None


def _content_disposition_filename(value: Any) -> str:
    text = _text(value)
    if not text:
        return ''
    encoded = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", text, re.I)
    if encoded:
        return unquote(encoded.group(1).strip().strip('"'))
    plain = re.search(r'filename\s*=\s*(?:"([^"]+)"|([^;]+))', text, re.I)
    if plain:
        return (plain.group(1) or plain.group(2) or '').strip()
    return ''


def _chrome_time(value: Any) -> str:
    if value in (None, '', 0, '0'):
        return ''
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return _text(value)
    try:
        # Chromium timestamps are microseconds since 1601-01-01 UTC.
        if abs(number) > 10_000_000_000:
            dt = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=number)
        else:
            dt = datetime.fromtimestamp(number, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return _text(value)
    return dt.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list, int, float, bool)) or value is None:
        return value
    text = _text(value).strip()
    if not text:
        return None
    candidates = [text]
    # LevelDB values sometimes carry a one-character serialization marker.
    if len(text) > 1 and text[0] not in '[{' and text[1] in '[{':
        candidates.append(text[1:])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
    return text


def _structured_json(value: Any) -> Any:
    """Return structured JSON from plain or compressed Chromium cache bodies.

    Cache bodies remain byte-for-byte in the raw dataset.  This helper only
    creates the semantic representation used by rules and reports.
    """
    direct = _json(value)
    if isinstance(direct, (dict, list)):
        return direct
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return direct

    raw = bytes(value)
    candidates = [raw]
    for decoder in (gzip.decompress, zlib.decompress):
        try:
            candidates.append(decoder(raw))
        except (OSError, EOFError, zlib.error):
            pass
    try:
        import brotli
    except ImportError:
        brotli = None
    if brotli is not None:
        try:
            candidates.append(brotli.decompress(raw))
        except brotli.error:
            pass

    for candidate in candidates:
        try:
            decoded = json.loads(candidate.decode('utf-8-sig'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(decoded, (dict, list)):
            return decoded
    return direct


def _decoded_text(value: Any) -> str:
    """Decode a plain or compressed text response without executing it."""
    if isinstance(value, str):
        return value
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return _text(value)
    raw = bytes(value)
    candidates = [raw]
    for decoder in (gzip.decompress, zlib.decompress):
        try:
            candidates.append(decoder(raw))
        except (OSError, EOFError, zlib.error):
            pass
    try:
        import brotli
    except ImportError:
        brotli = None
    if brotli is not None:
        try:
            candidates.append(brotli.decompress(raw))
        except brotli.error:
            pass
    for candidate in reversed(candidates):
        try:
            decoded = candidate.decode('utf-8-sig')
        except UnicodeDecodeError:
            continue
        if not decoded:
            continue
        printable = sum(
            1 for character in decoded
            if character.isprintable() or character in '\r\n\t'
        )
        if printable / len(decoded) >= 0.90:
            return decoded
    return ''


def _unix_time(value: Any) -> str:
    if value in (None, ''):
        return ''
    try:
        number = float(value)
        if abs(number) >= 100_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat(
            timespec='microseconds'
        ).replace('+00:00', 'Z')
    except (TypeError, ValueError, OverflowError, OSError):
        return _text(value)


def _message_text(message: dict[str, Any]) -> str:
    content = message.get('content') if isinstance(message, dict) else None
    if not isinstance(content, dict):
        return ''
    parts = content.get('parts')
    if not isinstance(parts, list):
        return _text(content.get('text'))
    rendered = []
    for part in parts:
        if isinstance(part, str):
            rendered.append(part)
        elif isinstance(part, dict):
            rendered.append(json.dumps(part, ensure_ascii=False, sort_keys=True))
        elif part is not None:
            rendered.append(_text(part))
    return '\n'.join(rendered)


def _message_attachments(
    message: dict[str, Any], conversation_id: str, text: str
) -> list[dict[str, Any]]:
    metadata = message.get('metadata') if isinstance(message.get('metadata'), dict) else {}
    author = message.get('author') if isinstance(message.get('author'), dict) else {}
    attachments = metadata.get('attachments') if isinstance(metadata.get('attachments'), list) else []
    out = []
    for index, item in enumerate(attachments):
        if not isinstance(item, dict):
            continue
        file_id = _text(item.get('id') or item.get('file_id'))
        if not file_id:
            continue
        out.append({
            'conversation_id': conversation_id,
            'message_id': _text(message.get('id')),
            'message_role': _text(author.get('role')),
            'message_create_time': _unix_time(message.get('create_time')),
            'message_update_time': _unix_time(message.get('update_time')),
            'request_id': _text(metadata.get('request_id')),
            'turn_id': _text(metadata.get('turn_id')),
            'turn_exchange_id': _text(metadata.get('turn_exchange_id')),
            'attachment_index': index,
            'file_id': file_id,
            'filename': _text(item.get('name') or item.get('file_name')),
            'mime_type': _text(item.get('mime_type')),
            'size': item.get('size') or item.get('size_bytes'),
            'width': item.get('width'),
            'height': item.get('height'),
            'source': _text(item.get('source')),
            'library_file_id': _text(item.get('library_file_id')),
            'is_big_paste': item.get('is_big_paste'),
            'prompt_text': text,
        })
    return out


def _conversation_fact(body: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    mapping = body.get('mapping') if isinstance(body.get('mapping'), dict) else {}
    ordered_nodes: list[tuple[str, dict[str, Any]]] = []
    messages = body.get('messages') if isinstance(body.get('messages'), list) else []
    if messages:
        for message in messages:
            if not isinstance(message, dict):
                continue
            metadata = message.get('metadata') if isinstance(message.get('metadata'), dict) else {}
            ordered_nodes.append((
                _text(message.get('id')),
                {'message': message, 'parent': metadata.get('parent_id')},
            ))
    else:
        current = _text(body.get('current_node'))
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            node = mapping.get(current)
            if not isinstance(node, dict):
                break
            ordered_nodes.append((current, node))
            current = _text(node.get('parent'))
        ordered_nodes.reverse()
    if not ordered_nodes:
        ordered_nodes = [
            (str(node_id), node) for node_id, node in mapping.items()
            if isinstance(node, dict)
        ]
    sequence: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    for node_id, node in ordered_nodes:
        if not isinstance(node, dict) or not isinstance(node.get('message'), dict):
            continue
        message = node['message']
        author = message.get('author') if isinstance(message.get('author'), dict) else {}
        metadata = message.get('metadata') if isinstance(message.get('metadata'), dict) else {}
        text = _message_text(message)
        content = message.get('content') if isinstance(message.get('content'), dict) else {}
        message_attachments = _message_attachments(message, conversation_id, text)
        attachments.extend(message_attachments)
        sequence.append({
            'index': len(sequence),
            'message_id': _text(message.get('id') or node_id),
            'parent_id': _text(node.get('parent')),
            'role': _text(author.get('role')),
            'name': _text(author.get('name')),
            'content_type': _text(content.get('content_type')),
            'text': text,
            'content_sha256': sha256(text.encode('utf-8', 'surrogatepass')).hexdigest(),
            'create_time': _unix_time(message.get('create_time')),
            'update_time': _unix_time(message.get('update_time')),
            'request_id': _text(metadata.get('request_id')),
            'turn_id': _text(metadata.get('turn_id')),
            'turn_exchange_id': _text(metadata.get('turn_exchange_id')),
            'attachment_count': len(message_attachments),
            'attachment_file_ids': [item['file_id'] for item in message_attachments],
        })

    signature_value = [
        (item['message_id'], item['parent_id'], item['role'], item['content_sha256'])
        for item in sequence
    ]
    return {
        'conversation_id': conversation_id or _text(body.get('id')),
        'title': _text(body.get('title')),
        'current_node': _text(body.get('current_node')),
        'is_temporary_chat': body.get('is_temporary_chat'),
        'is_do_not_remember': body.get('is_do_not_remember'),
        'owner': body.get('owner'),
        'create_time': _unix_time(body.get('create_time')),
        'update_time': _unix_time(body.get('update_time')),
        'message_sequence': sequence,
        'message_ids': [item['message_id'] for item in sequence if item['message_id']],
        'user_message_ids': [
            item['message_id'] for item in sequence
            if item['role'] == 'user' and item['message_id']
        ],
        'message_count': len(sequence),
        'attachment_count': len(attachments),
        'attachment_file_ids': [item['file_id'] for item in attachments],
        'attachments': attachments,
        'branch_signature': sha256(
            json.dumps(signature_value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        ).hexdigest(),
    }


def _account_fields(body: dict[str, Any]) -> dict[str, Any]:
    orgs = _find_key(body, 'orgs')
    if isinstance(orgs, dict):
        orgs = orgs.get('data') or orgs.get('items') or []
    if not isinstance(orgs, list):
        orgs = []
    fields = {
        'account_id': _find_key(body, 'id'),
        'email': _find_key(body, 'email'),
        'name': _find_key(body, 'name'),
        'first_name': _find_key(body, 'first_name'),
        'phone_number': _find_key(body, 'phone_number'),
        'country': _find_key(body, 'country'),
        'region': _find_key(body, 'region'),
        'region_code': _find_key(body, 'region_code'),
        'client_id': _find_key(body, 'client_id'),
        'email_domain_type': _find_key(body, 'email_domain_type'),
        'ads_segment_id': _find_key(body, 'ads_segment_id'),
        'address': _find_key(body, 'address'),
        'city': _find_key(body, 'city'),
        'postal_code': _find_key(body, 'postal_code'),
        'organization_ids': [
            _text(item.get('id')) for item in orgs
            if isinstance(item, dict) and item.get('id') is not None
        ],
        'organization_names': [
            _text(item.get('name')) for item in orgs
            if isinstance(item, dict) and item.get('name') is not None
        ],
    }
    return {key: value for key, value in fields.items() if value is not None}


def _find_key(value: Any, wanted: str):
    if isinstance(value, dict):
        if wanted in value:
            return value[wanted]
        for child in value.values():
            found = _find_key(child, wanted)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_key(child, wanted)
            if found is not None:
                return found
    return None


def _find_lists(value: Any, wanted: str) -> list[list[Any]]:
    found: list[list[Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == wanted and isinstance(child, list):
                found.append(child)
            found.extend(_find_lists(child, wanted))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_lists(child, wanted))
    return found


def _memory_actions(body: dict[str, Any]) -> list[dict[str, Any]]:
    actions = body.get('actions') if isinstance(body.get('actions'), list) else []
    out = []
    for item in actions:
        if not isinstance(item, dict):
            continue
        action = item.get('action') if isinstance(item.get('action'), dict) else {}
        out.append({
            'id': _text(item.get('id')),
            'label': _text(item.get('label')),
            'type': _text(action.get('type')),
            'target_action': _text(action.get('targetAction')),
        })
    return out


def _file_fields(item: dict[str, Any], fallback_file_id: str = '') -> dict[str, Any]:
    file_id = _text(item.get('file_id') or item.get('id') or fallback_file_id)
    item_id = _text(item.get('id'))
    library_file_id = _text(item.get('library_file_id'))
    if not library_file_id and item_id.startswith('libfile_'):
        library_file_id = item_id
    return {
        'file_id': file_id,
        'filename': _text(item.get('file_name') or item.get('filename') or item.get('name')),
        'mime_type': _text(item.get('mime_type')),
        'size': item.get('file_size_bytes') or item.get('size_bytes') or item.get('size'),
        'width': item.get('width'),
        'height': item.get('height'),
        'status': _text(item.get('status')),
        'library_file_id': library_file_id,
        'library_directory_id': _text(item.get('library_directory_id')),
        'library_file_category': _text(item.get('library_file_category')),
        'library_artifact_type': _text(item.get('library_artifact_type')),
        'is_library_file': item.get('is_library_file'),
        'file_upload_time': _text(item.get('file_upload_time')),
        'file_processed_time': _text(item.get('file_processed_time')),
        'origination_message_id': _text(item.get('origination_message_id')),
        'download_url': _text(item.get('download_url')),
    }


def _binary_file_fields(value: Any) -> dict[str, Any]:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return {}
    payload = bytes(value)
    fact: dict[str, Any] = {
        'body_recovered': bool(payload),
        'size': len(payload),
        'sha256': sha256(payload).hexdigest(),
        'raw_dataset': '_raw/cache/entries.jsonl',
    }
    if payload.startswith(b'\x89PNG\r\n\x1a\n'):
        fact['content_type_detected'] = 'image/png'
        if len(payload) >= 24:
            fact['width'] = int.from_bytes(payload[16:20], 'big')
            fact['height'] = int.from_bytes(payload[20:24], 'big')
    elif payload.startswith(b'%PDF-'):
        fact['content_type_detected'] = 'application/pdf'
    elif payload.startswith((b'\xff\xd8\xff',)):
        fact['content_type_detected'] = 'image/jpeg'
    elif payload.startswith((b'GIF87a', b'GIF89a')):
        fact['content_type_detected'] = 'image/gif'
    else:
        fact['content_type_detected'] = 'application/octet-stream'
        try:
            decoded = payload.decode('utf-8')
        except UnicodeDecodeError:
            decoded = ''
        if decoded:
            printable = sum(
                1 for character in decoded
                if character.isprintable() or character in '\r\n\t'
            )
            if printable / len(decoded) >= 0.95:
                fact['content_type_detected'] = 'text/plain'
                fact['text_encoding'] = 'utf-8'
                fact['line_count'] = len(decoded.splitlines())
                python_shebang = bool(re.match(
                    r'^#![^\r\n]*\bpython(?:3(?:\.\d+)?)?\b', decoded,
                ))
                python_import = bool(re.search(
                    r'(?m)^\s*(?:from\s+[A-Za-z_][\w.]*\s+import\s+|import\s+[A-Za-z_])',
                    decoded,
                ))
                python_definition = bool(re.search(
                    r'(?m)^\s*(?:async\s+def|def|class)\s+[A-Za-z_]\w*',
                    decoded,
                ))
                if python_shebang or (python_import and python_definition):
                    fact['content_type_detected'] = 'text/x-python'
                    fact['python_source_detected'] = True
    return fact


def _source_scope(path: str) -> str:
    """Stable browser-profile scope shared by all artifacts in that profile."""
    normalized = str(path).replace('/', '\\')
    match = re.search(
        r'(?i)^(.*?\\(?:Google\\Chrome|Microsoft\\Edge)\\User Data\\[^\\]+)(?:\\|$)',
        normalized,
    )
    if match:
        return match.group(1).lower()
    # Curated experiment folders often contain History, Cache, Network, etc.
    # directly below one case directory rather than below Chrome/User Data.
    # Strip only a recognized artifact suffix so different case folders remain
    # isolated while artifacts from the same case can still be associated.
    curated = re.match(
        r'(?i)^(.*?)\\(?:'
        r'Cache\\Cache_Data|Network\\Cookies|Local Storage\\leveldb|'
        r'Sessions(?:\\.*)?|History(?:-journal)?|Preferences|DIPS|Favicons'
        r')(?:\\.*)?$',
        normalized,
    )
    return (curated.group(1) if curated else normalized).lower()


def _parsed_record(raw: RawRecord) -> Any:
    """Keep traceability without copying large cache bodies into observations.

    Native cache bodies remain byte-for-byte available in ``_raw/cache``.  The
    normalized layer keeps the key and metadata plus a deterministic body
    descriptor, which prevents one response body from being duplicated in the
    observation JSONL and again in the HTML report.
    """
    if raw.artifact != 'cache' or not isinstance(raw.record, dict):
        return raw.record
    cooked = dict(raw.record)
    body = cooked.get('body')
    if isinstance(body, str):
        payload = body.encode('utf-8', 'surrogatepass')
    elif isinstance(body, (bytes, bytearray, memoryview)):
        payload = bytes(body)
    elif body is None:
        payload = b''
    else:
        payload = repr(body).encode('utf-8', 'replace')
    cooked['body'] = {
        '_omitted_from_observation': True,
        'length': len(payload),
        'sha256': sha256(payload).hexdigest(),
        'raw_dataset': '_raw/cache/entries.jsonl',
    }
    return cooked


def _observation(
    raw: RawRecord,
    ids: Ids,
    fact_type: str,
    fact: dict[str, Any],
    *,
    kind: str = 'context',
    timestamps: dict[str, str] | None = None,
    seq: int | None = None,
) -> Observation:
    fact.setdefault('_source_scope', _source_scope(raw.source_path))
    return Observation(
        id=ids.next('obs'),
        source_artifact=raw.artifact,
        source_path=raw.source_path,
        source_locator=raw.source_locator,
        fact_type=fact_type,
        fact=fact,
        evidence_kind=kind,
        parser=raw.parser,
        parser_status='ok',
        timestamps={k: v for k, v in (timestamps or {}).items() if v},
        tz_source='UTC',
        seq=seq if seq is not None else raw.ordinal,
        env_profile='chatgpt_web',
        parsed_record=_parsed_record(raw),
    )


def _history_group(records: list[RawRecord], ids: Ids) -> list[Observation]:
    out: list[Observation] = []
    urls: dict[Any, RawRecord] = {}
    visits: list[RawRecord] = []
    for raw in records:
        if raw.dataset == 'urls' and isinstance(raw.record, dict):
            urls[raw.record.get('id')] = raw
        elif raw.dataset == 'visits':
            visits.append(raw)

    # Keep the native URL rows independently visible even when visits are joined.
    for raw in urls.values():
        row = raw.record
        parts = _url(row.get('url'))
        fact = {**parts, 'title': row.get('title'), 'url_id': row.get('id')}
        out.append(_observation(
            raw, ids, 'url_record', fact, kind='context',
            timestamps={'ts_record': _chrome_time(row.get('last_visit_time'))},
        ))

    for raw in visits:
        row = raw.record if isinstance(raw.record, dict) else {}
        url_raw = urls.get(row.get('url'))
        url_row = url_raw.record if url_raw is not None else {}
        parts = _url(url_row.get('url') or row.get('url_text') or '')
        fact = {
            **parts,
            'title': url_row.get('title'),
            'url_id': row.get('url'),
            'visit_id': row.get('id'),
            'from_visit': row.get('from_visit'),
            'transition': row.get('transition'),
            'visit_duration': row.get('visit_duration'),
        }
        path = fact.get('path') or ''
        conversation = re.match(r'^/c/(?P<conversation_id>[^/?#]+)$', path)
        if conversation:
            fact['conversation_id'] = conversation.group('conversation_id')
        fact_type = 'conversation_visit' if conversation else 'url_visit'
        kind = 'event' if fact.get('hostname') in _CHATGPT_HOSTS or fact.get('hostname') == 'accounts.google.com' else 'context'
        out.append(_observation(
            raw, ids, fact_type, fact, kind=kind,
            timestamps={'ts_visit': _chrome_time(row.get('visit_time'))},
        ))

    # Browser download tables are separate from URL visits.
    for raw in records:
        if not isinstance(raw.record, dict):
            continue
        row = raw.record
        if raw.dataset == 'downloads':
            fact = dict(row)
            tab = _text(row.get('tab_url') or row.get('referrer') or '')
            fact['tab_url'] = tab
            fact['download_id'] = row.get('id')
            tab_parts = _url(tab)
            fact['tab_hostname'] = tab_parts.get('hostname')
            fact['tab_path'] = tab_parts.get('path')
            conversation = re.match(
                r'^/c/(?P<conversation_id>[^/?#]+)$',
                tab_parts.get('path') or '',
            )
            if conversation:
                fact['conversation_id'] = conversation.group('conversation_id')
            target_path = _text(row.get('target_path') or row.get('current_path'))
            fact['target_filename'] = target_path.replace('/', '\\').rsplit('\\', 1)[-1]
            received = row.get('received_bytes')
            total = row.get('total_bytes')
            fact['download_complete'] = bool(
                str(row.get('state')) == '1'
                and str(row.get('interrupt_reason')) == '0'
                and received is not None
                and total is not None
                and received == total
            )
            out.append(_observation(
                raw, ids, 'browser_download', fact, kind='event',
                timestamps={
                    'ts_event': _chrome_time(row.get('start_time')),
                    'ts_modified': _chrome_time(row.get('end_time')),
                },
            ))
        elif raw.dataset in ('downloads_url_chains', 'download_url_chains'):
            value = _text(row.get('url'))
            parts = _url(value)
            fact = dict(row)
            fact.update(parts)
            fact['download_id'] = row.get('id')
            fact['file_id'] = parts.get('query', {}).get('id')
            fact['filename'] = parts.get('query', {}).get('fn')
            fact['content_disposition'] = parts.get('query', {}).get('cd')
            out.append(_observation(raw, ids, 'download_url_chain', fact, kind='event'))

    handled = {'urls', 'visits', 'downloads', 'downloads_url_chains', 'download_url_chains'}
    for raw in records:
        if raw.dataset not in handled:
            out.append(_observation(
                raw, ids, f'history_{raw.dataset}_record',
                {'dataset': raw.dataset}, kind='context',
            ))
    return out


def _history(records: list[RawRecord], ids: Ids) -> list[Observation]:
    # SQLite integer IDs are local to one History database.  Never join Chrome,
    # Edge or two user profiles through a process-global url_id dictionary.
    groups: dict[str, list[RawRecord]] = {}
    for raw in records:
        groups.setdefault(_source_scope(raw.source_path), []).append(raw)
    out: list[Observation] = []
    for group in groups.values():
        out.extend(_history_group(group, ids))
    return out


def _cache(raw: RawRecord, ids: Ids) -> list[Observation]:
    row = raw.record if isinstance(raw.record, dict) else {}
    key = _text(row.get('key'))
    # Chromium's simple-cache reader can expose composite keys such as
    # ``1/0/_dk_<origin> <top-frame-origin> <request-url>``.  The request URL
    # is the final URL token, while parsing the whole key yields no hostname.
    urls = _URL_TEXT_RE.findall(key)
    parts = _url(urls[-1] if urls else key)
    body = _structured_json(row.get('body'))
    metadata = row.get('metadata')
    headers = _cache_headers(metadata)
    status_code = _cache_status_code(metadata)
    fact: dict[str, Any] = {**parts, 'key': key, 'metadata': metadata}
    if headers:
        fact['response_headers'] = headers
    if status_code is not None:
        fact['http_status_code'] = status_code
    # Only structured JSON is useful to the semantic layer.  Arbitrary binary
    # or text bodies remain in the raw record and must not inflate observations.
    if isinstance(body, (dict, list)):
        fact['json'] = body

    path = fact.get('path') or ''
    fact_type = 'http_cache_entry'
    extra_observations: list[tuple[str, dict[str, Any], dict[str, str]]] = []
    if re.match(r'^/backend-api/conversation/[^/]+/stream_status$', path):
        fact_type = 'conversation_stream_status'
        match = re.match(r'^/backend-api/conversation/(?P<conversation_id>[^/]+)/stream_status$', path)
        if match:
            fact['conversation_id'] = match.group('conversation_id')
        if isinstance(body, dict):
            stream_status = _text(body.get('status')).strip().upper()
            if not stream_status and body.get('is_streaming') in (True, 1, 'true', 'TRUE'):
                stream_status = 'IS_STREAMING'
            if not stream_status and body.get('complete') in (True, 1, 'true', 'TRUE'):
                stream_status = 'COMPLETE'
            if stream_status == 'COMPLETED':
                stream_status = 'COMPLETE'
            if stream_status:
                fact['stream_status'] = stream_status
    elif re.match(r'^/backend-api/conversation/[^/]+/url_safe$', path):
        # ChatGPT checks external links surfaced in a conversation before the
        # browser renders them.  The request URL therefore preserves both the
        # conversation identifier and the exact external result link.  It does
        # not, by itself, prove which provider-side operation produced the URL.
        fact_type = 'url_safety_check'
        match = re.match(
            r'^/backend-api/conversation/(?P<conversation_id>[^/]+)/url_safe$',
            path,
        )
        if match:
            fact['conversation_id'] = match.group('conversation_id')
        target_url = _text(fact.get('query', {}).get('url'))
        target_parts = _url(target_url)
        fact.update({
            'target_url': target_url,
            'target_hostname': target_parts.get('hostname'),
            'target_path': target_parts.get('path'),
        })
        target_file = re.match(
            r'^/file/d/(?P<external_file_id>[^/?#]+)',
            target_parts.get('path') or '',
        )
        if target_file:
            fact['external_file_id'] = target_file.group('external_file_id')
    elif re.match(r'^/backend-api/conversation/[^/]+/interpreter/download$', path):
        fact_type = 'download_request'
        match = re.match(r'^/backend-api/conversation/(?P<conversation_id>[^/]+)/interpreter/download$', path)
        if match:
            fact['conversation_id'] = match.group('conversation_id')
        query = fact.get('query', {})
        fact['message_id'] = _text(query.get('message_id'))
        fact['sandbox_path'] = unquote(_text(query.get('sandbox_path')))
        fact['download_intent'] = _text(query.get('download_intent')).lower()
        response_status = _text(_find_key(body, 'status')).lower()
        response_url = _text(_find_key(body, 'download_url'))
        response_error = _text(_find_key(body, 'detail'))
        fact.update({
            'download_response_status': response_status,
            'download_response_url': response_url,
            'download_response_error': response_error,
        })
        if (
            (status_code is not None and status_code >= 400)
            or response_status in {'error', 'failed', 'failure'}
            or bool(response_error)
        ):
            fact['download_result'] = 'failed'
        elif response_status == 'success' or response_url:
            fact['download_result'] = 'success'
        else:
            fact['download_result'] = 'unknown'
    elif path == '/backend-api/files/library/nodes' and isinstance(body, dict):
        fact_type = 'file_library_snapshot'
        items = body.get('items') if isinstance(body.get('items'), list) else []
        fact.update({
            'library_item_count': len(items),
            'cursor': body.get('cursor'),
        })
        fact.pop('json', None)
        for index, item in enumerate(items):
            if not isinstance(item, dict) or item.get('kind') not in (None, 'file'):
                continue
            library_fact = {
                **parts,
                **_file_fields(item),
                'library_item_index': index,
                'access_kind': _text(item.get('access_kind')),
                'kind': _text(item.get('kind')),
            }
            if library_fact.get('is_library_file') is None:
                library_fact['is_library_file'] = True
            if not library_fact.get('file_id'):
                continue
            extra_observations.append((
                'library_file',
                library_fact,
                {
                    'ts_event': library_fact.get('file_upload_time') or '',
                    'ts_modified': library_fact.get('file_processed_time') or '',
                },
            ))
    elif re.match(r'^/backend-api/files/[^/]+/simple$', path) and isinstance(body, dict):
        match = re.match(r'^/backend-api/files/(?P<file_id>[^/]+)/simple$', path)
        fact_type = 'file_metadata'
        fact.update(_file_fields(body, match.group('file_id') if match else ''))
        fact.pop('json', None)
    elif re.match(r'^/backend-api/files/download/[^/]+$', path) and isinstance(body, dict):
        match = re.match(r'^/backend-api/files/download/(?P<file_id>[^/]+)$', path)
        fact_type = 'file_content_reference'
        fact.update(_file_fields(body, match.group('file_id') if match else ''))
        intent = _text(fact.get('query', {}).get('download_intent')).lower()
        fact['download_intent'] = intent
        fact['access_mode'] = (
            'download' if intent == 'true'
            else 'preview' if intent == 'false'
            else 'unspecified'
        )
        fact.pop('json', None)
    elif path == '/backend-api/estuary/content':
        fact_type = 'file_content_reference'
        fact['file_id'] = fact.get('query', {}).get('id')
        header_disposition = headers.get('content-disposition', '')
        fact['filename'] = (
            _text(fact.get('query', {}).get('fn'))
            or _text(_find_key(body, 'filename'))
            or _content_disposition_filename(header_disposition)
        )
        fact['content_disposition'] = (
            _text(fact.get('query', {}).get('cd'))
            or header_disposition
        )
        binary_fact = _binary_file_fields(row.get('body'))
        if headers.get('content-type'):
            binary_fact['content_type_header'] = headers['content-type']
        if binary_fact.get('body_recovered'):
            extra_observations.append((
                'file_content',
                {
                    **parts,
                    'file_id': fact['file_id'],
                    'filename': fact['filename'],
                    'content_disposition': fact['content_disposition'],
                    **binary_fact,
                },
                {},
            ))
    elif path == '/backend-api/me':
        fact_type = 'account_observation'
        if isinstance(body, dict):
            fact.update(_account_fields(body))
        fact.pop('json', None)
    elif path == '/backend-api/memories' and isinstance(body, dict):
        fact_type = 'memory_list_snapshot'
        memories = body.get('memories') if isinstance(body.get('memories'), list) else []
        fact.update({
            'memory_count': len(memories),
            'memory_entries': memories,
            'memory_num_tokens': body.get('memory_num_tokens'),
            'memory_max_tokens': body.get('memory_max_tokens'),
            'includes_memory_entries': fact.get('query', {}).get('include_memory_entries'),
        })
        fact.pop('json', None)
    elif path == '/backend-api/memories/about_you/summary/checksum' and isinstance(body, dict):
        fact_type = 'memory_summary_checksum'
        fact.update({
            'cached_generated_at': _text(body.get('cachedGeneratedAtIso')),
            'cached_source_checksum': _text(body.get('cachedSourceChecksum')),
            'source_checksum': _text(body.get('sourceChecksum')),
            'is_stale': body.get('isStale'),
        })
        fact.pop('json', None)
    elif path == '/backend-api/memories/about_you/selection_correction_actions' and isinstance(body, dict):
        fact_type = 'memory_correction_capability'
        fact.update({
            'schema_version': body.get('schemaVersion'),
            'correction_actions': _memory_actions(body),
        })
        fact.pop('json', None)
    else:
        conversation = re.match(
            r'^/backend-api/conversations/(?P<conversation_id>[^/?#]+)$', path
        )
        if conversation and isinstance(body, dict):
            fact_type = 'conversation_snapshot'
            fact.update(_conversation_fact(body, conversation.group('conversation_id')))
            fact.pop('json', None)
            for attachment in fact.get('attachments') or []:
                if not isinstance(attachment, dict):
                    continue
                extra_observations.append((
                    'conversation_attachment',
                    {**parts, **attachment},
                    {
                        'ts_event': _text(attachment.get('message_create_time')),
                        'ts_modified': _text(attachment.get('message_update_time')),
                    },
                ))

    if path == '/auth/logout':
        # The server-rendered logout page embeds the federated logout URL in
        # compressed HTML.  Its explicit reason/source distinguish ordinary
        # logout from an account-delete confirmation flow.
        rendered = _decoded_text(row.get('body'))
        for name in ('logout_reason', 'logout_source', 'logout_correlation_id'):
            match = re.search(
                rf'{name}=([A-Za-z0-9_.-]+)',
                rendered,
            )
            if match:
                fact[name] = match.group(1)
        if fact.get('logout_reason') == 'logout_confirm_modal':
            fact['logout_kind'] = 'ordinary_logout_confirmation'

    for name in ('message_id', 'sandbox_path', 'logout_reason', 'logout_source'):
        value = _find_key(body, name)
        if value is not None:
            fact[name] = value

    meta = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
    timestamp = _text(meta.get('response_time') or meta.get('request_time'))
    timestamps = {'ts_record': timestamp}
    if fact_type == 'conversation_snapshot':
        timestamps.update({
            'ts_created': _text(fact.get('create_time')),
            'ts_modified': _text(fact.get('update_time')),
        })
    elif fact_type == 'memory_summary_checksum':
        timestamps['ts_modified'] = _text(fact.get('cached_generated_at'))
    out = [_observation(
        raw, ids, fact_type, fact,
        kind='event' if fact.get('hostname') in _CHATGPT_HOSTS else 'context',
        timestamps=timestamps,
    )]
    for extra_type, extra_fact, extra_timestamps in extra_observations:
        out.append(_observation(
            raw, ids, extra_type, extra_fact,
            kind='event' if extra_fact.get('hostname') in _CHATGPT_HOSTS else 'context',
            timestamps={**timestamps, **extra_timestamps},
        ))
    return out


def _cookie(raw: RawRecord, ids: Ids) -> Observation:
    row = raw.record if isinstance(raw.record, dict) else {}
    fact = {
        'name': _text(row.get('name')),
        'value': _text(row.get('value')),
        'hostname': _text(row.get('host_key') or row.get('host')).lstrip('.').lower(),
        'path': _text(row.get('path')),
        'is_secure': row.get('is_secure'),
        'is_httponly': row.get('is_httponly'),
    }
    name_match = re.match(r'^(?:history_off_|conv_key_)(?P<conversation_id>.+)$', fact['name'])
    if name_match:
        fact['conversation_id'] = name_match.group('conversation_id')
    return _observation(
        raw, ids, 'cookie_record', fact, kind='state',
        timestamps={'ts_created': _chrome_time(row.get('creation_utc')),
                    'ts_modified': _chrome_time(row.get('last_update_utc') or row.get('last_access_utc'))},
    )


def _local_storage(raw: RawRecord, ids: Ids) -> list[Observation]:
    row = raw.record if isinstance(raw.record, dict) else {}
    key = _text(row.get('script_key') or row.get('key') or row.get('user_key'))
    value = _json(row.get('value'))
    fact: dict[str, Any] = {
        'storage_key': _text(row.get('storage_key')),
        'key': key,
        'value': _text(row.get('value')),
        'json': value,
        'is_live': row.get('is_live'),
        'leveldb_seq_number': row.get('leveldb_seq_number'),
    }
    is_live = row.get('is_live') is not False
    fact_type = 'kv_record' if is_live else 'deletion_record'
    kind = 'state' if is_live else 'residual'
    joined = f'{key} {_text(row.get("value"))}'

    if key == 'oai/apps/accountSwitchSessions' and isinstance(value, list) and value:
        out: list[Observation] = []
        for index, account in enumerate(value):
            if not isinstance(account, dict):
                continue
            workspaces = account.get('workspaces') if isinstance(account.get('workspaces'), list) else []
            account_fact = {
                'storage_key': fact['storage_key'],
                'key': key,
                'is_live': row.get('is_live'),
                'leveldb_seq_number': row.get('leveldb_seq_number'),
                'session_index': index,
                'account_id': account.get('userId'),
                'user_id': account.get('userId'),
                'email': account.get('email'),
                'email_domain_type': account.get('emailDomainType'),
                'phone_number': account.get('phoneNumber'),
                'name': account.get('name'),
                'auth_provider': account.get('authProvider'),
                'last_logged_in_at': _unix_time(account.get('lastLoggedInAt')),
                'user_image_url': account.get('userImageUrl'),
                'has_session_token': bool(account.get('sessionToken')),
                'workspace_account_ids': [
                    item.get('workspaceAccountId') for item in workspaces
                    if isinstance(item, dict) and item.get('workspaceAccountId')
                ],
                'workspace_names': [
                    item.get('workspaceName') for item in workspaces
                    if isinstance(item, dict) and item.get('workspaceName')
                ],
            }
            out.append(_observation(
                raw, ids, 'account_session_observation', account_fact, kind=kind,
                timestamps={'ts_event': account_fact['last_logged_in_at']},
                seq=row.get('leveldb_seq_number'),
            ))
        if out:
            return out

    if 'conversation-history' in key and is_live and isinstance(value, dict):
        items = [
            item for group in _find_lists(value, 'items') for item in group
            if isinstance(item, dict) and item.get('id')
        ]
        if items:
            key_match = re.search(
                r'cache/(?P<user_id>[^/]+)/(?P<workspace_account_id>[^/]+)/conversation-history$',
                key,
            )
            observed_at = _unix_time(_find_key(value, 'timestamp'))
            total = _find_key(value, 'total')
            out: list[Observation] = []
            for index, item in enumerate(items):
                history_fact = {
                    'storage_key': fact['storage_key'],
                    'key': key,
                    'is_live': row.get('is_live'),
                    'leveldb_seq_number': row.get('leveldb_seq_number'),
                    'conversation_index': index,
                    'conversation_id': _text(item.get('id')),
                    'title': _text(item.get('title')),
                    'create_time': _text(item.get('create_time')),
                    'update_time': _text(item.get('update_time')),
                    'is_archived': item.get('is_archived'),
                    'is_temporary_chat': item.get('is_temporary_chat'),
                    'is_do_not_remember': item.get('is_do_not_remember'),
                    'memory_scope': item.get('memory_scope'),
                    'membership': True,
                    'state_role': 'current',
                    'source_class': 'local_storage_current',
                    'list_total': total,
                }
                if key_match:
                    history_fact.update(key_match.groupdict())
                out.append(_observation(
                    raw, ids, 'conversation_history_state', history_fact, kind=kind,
                    timestamps={
                        'ts_record': observed_at,
                        'ts_created': history_fact['create_time'],
                        'ts_modified': history_fact['update_time'],
                    },
                    seq=row.get('leveldb_seq_number'),
                ))
            return out

    if key == 'oai/apps/conversationDrafts' and isinstance(value, dict):
        drafts = value.get('drafts') if isinstance(value.get('drafts'), list) else []
        user_id = value.get('userId')
        if not drafts:
            fact.update({'user_id': user_id, 'draft_count': 0})
            return [_observation(
                raw, ids, 'conversation_draft_cleared', fact, kind=kind,
                seq=row.get('leveldb_seq_number'),
            )]
        out: list[Observation] = []
        for index, draft in enumerate(drafts):
            if not isinstance(draft, dict):
                continue
            draft_fact = dict(fact)
            draft_id = draft.get('id') or draft.get('conversationId') or draft.get('conversation_id')
            draft_text = (
                draft.get('text') or draft.get('draft') or draft.get('content')
                or _find_key(draft, 'text') or ''
            )
            draft_time = (
                draft.get('updatedAt') or draft.get('updated_at')
                or draft.get('timestamp') or draft.get('createdAt')
            )
            draft_fact.update({
                'user_id': user_id,
                'draft_id': _text(draft_id),
                'draft_target_id': _text(draft_id),
                'conversation_id': _text(
                    draft.get('conversationId') or draft.get('conversation_id')
                    or (draft_id if re.fullmatch(
                        r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',
                        _text(draft_id),
                    ) else '')
                ),
                'draft_text': _text(draft_text),
                'draft_index': index,
                'residual_sequence': row.get('leveldb_seq_number'),
            })
            out.append(_observation(
                raw, ids, 'conversation_draft', draft_fact, kind=kind,
                timestamps={'ts_modified': _unix_time(draft_time)},
                seq=row.get('leveldb_seq_number'),
            ))
        return out or [_observation(
            raw, ids, 'conversation_draft_cleared', fact, kind=kind,
            seq=row.get('leveldb_seq_number'),
        )]

    if 'draft' in joined.lower():
        fact_type = 'conversation_draft'
        fact['draft_id'] = 'null_thread' if 'null_thread' in joined else _find_key(value, 'draft_id')
        fact['conversation_id'] = _find_key(value, 'conversation_id')
        fact['draft_text'] = _find_key(value, 'text') or _find_key(value, 'draft')
        fact['residual_sequence'] = row.get('leveldb_seq_number')
    elif 'conversation-history' in key:
        fact_type = 'conversation_history_state'
        fact['conversation_id'] = _find_key(value, 'conversation_id')
        fact['membership'] = is_live
        fact['state_role'] = 'current' if is_live else 'residual'
        fact['source_class'] = f'local_storage_{fact["state_role"]}'
    timestamps: dict[str, str] = {}
    if (
        key == 'nextauth.message'
        and isinstance(value, dict)
        and value.get('event') == 'session'
        and isinstance(value.get('data'), dict)
        and value['data'].get('trigger') == 'signout'
    ):
        timestamps['ts_event'] = _unix_time(value.get('timestamp'))
    return [_observation(
        raw, ids, fact_type, fact, kind=kind,
        timestamps=timestamps, seq=row.get('leveldb_seq_number'),
    )]


def _session_storage(raw: RawRecord, ids: Ids) -> Observation:
    row = raw.record if isinstance(raw.record, dict) else {}
    raw_key = _text(row.get('user_key') or row.get('key'))
    known = 'chatgpt_delete_account_pending_confirm'
    key = known if known in raw_key else raw_key
    value = _json(row.get('value'))
    fact = {'key': key, 'raw_key': raw_key, 'value': _text(row.get('value')), 'json': value,
            'state': _text(row.get('state'))}
    kind = 'residual' if fact['state'].lower() in ('deleted', 'obsolete') else 'state'
    return _observation(raw, ids, 'kv_record', fact, kind=kind, seq=row.get('seq'))


def _session(raw: RawRecord, ids: Ids) -> Observation:
    row = raw.record if isinstance(raw.record, dict) else {}
    payload = row.get('payload')
    raw_bytes = bytes(payload) if isinstance(payload, (bytes, bytearray, memoryview)) else _text(payload).encode('utf-8', 'replace')
    match = _URL_RE.search(raw_bytes)
    url = match.group(0).decode('utf-8', 'replace') if match else ''
    fact = {
        'version': row.get('version'),
        'command_id': row.get('command_id'),
        'original_request_url': url,
    }
    return _observation(raw, ids, 'navigation_record', fact, kind='context')


def _conversation_history_transitions(
    observations: list[Observation], ids: Ids
) -> list[Observation]:
    """Derive active-list removals from ordered Local Storage snapshots.

    CCL can recover several live-looking values for the same LevelDB key from
    different files.  The highest LevelDB sequence is the current value; lower
    sequences are previous snapshots.  A missing conversation is represented
    explicitly only when the same user/workspace key has both states.
    """
    groups: dict[str, list[Observation]] = {}
    for observation in observations:
        if (
            observation.source_artifact == 'local_storage'
            and observation.fact_type == 'conversation_history_state'
            and observation.fact.get('membership') is True
            and observation.fact.get('key')
            and observation.seq is not None
        ):
            groups.setdefault(str(observation.fact['key']), []).append(observation)

    derived: list[Observation] = []
    for key, items in groups.items():
        sequences = [item.seq for item in items if isinstance(item.seq, int)]
        if len(set(sequences)) < 2:
            continue
        ordered_sequences = sorted(set(sequences))
        latest_sequence = ordered_sequences[-1]
        latest_items = [item for item in items if item.seq == latest_sequence]
        previous_items = [item for item in items if item.seq != latest_sequence]
        if not latest_items or not previous_items:
            continue

        for item in items:
            current = item.seq == latest_sequence
            item.fact['state_role'] = 'current' if current else 'residual'
            item.fact['source_class'] = (
                'local_storage_current' if current
                else 'local_storage_residual'
            )
            item.evidence_kind = 'state' if current else 'residual'

        by_sequence = {
            sequence: [item for item in items if item.seq == sequence]
            for sequence in ordered_sequences
        }
        for previous_sequence, current_sequence in zip(
            ordered_sequences, ordered_sequences[1:]
        ):
            previous_snapshot = by_sequence[previous_sequence]
            current_snapshot = by_sequence[current_sequence]
            current_ids = {
                _text(item.fact.get('conversation_id')) for item in current_snapshot
                if item.fact.get('conversation_id')
            }
            previous_by_id = {
                _text(item.fact.get('conversation_id')): item
                for item in previous_snapshot if item.fact.get('conversation_id')
            }
            anchor = current_snapshot[0]
            current_observed_at = _text(anchor.timestamps.get('ts_record'))
            for conversation_id in sorted(set(previous_by_id) - current_ids):
                previous = previous_by_id[conversation_id]
                previous.fact['transition_candidate'] = True
                previous_observed_at = _text(previous.timestamps.get('ts_record'))
                fact = {
                    '_source_scope': anchor.fact.get('_source_scope'),
                    'storage_key': anchor.fact.get('storage_key'),
                    'key': key,
                    'user_id': anchor.fact.get('user_id'),
                    'workspace_account_id': anchor.fact.get('workspace_account_id'),
                    'conversation_id': conversation_id,
                    'title': previous.fact.get('title'),
                    'membership': False,
                    'previous_membership': True,
                    'previous_is_archived': previous.fact.get('is_archived'),
                    'state_role': 'transition_current',
                    'source_class': 'local_storage_transition_current',
                    'transition_type': 'removed_from_active_list',
                    'previous_leveldb_seq_number': previous_sequence,
                    'current_leveldb_seq_number': current_sequence,
                    'previous_observed_at': previous_observed_at,
                    'current_observed_at': current_observed_at,
                }
                derived.append(Observation(
                    id=ids.next('obs'),
                    source_artifact=anchor.source_artifact,
                    source_path=anchor.source_path,
                    source_locator=(
                        f'{anchor.source_locator};derived=active-list-absence;'
                        f'conversation_id={conversation_id}'
                    ),
                    fact_type='conversation_history_state',
                    fact=fact,
                    evidence_kind='state',
                    parser=anchor.parser,
                    parser_status='ok',
                    parser_note='Derived by comparing consecutive ordered snapshots of the same Local Storage key.',
                    timestamps={
                        'ts_record': current_observed_at,
                        'ts_previous': previous_observed_at,
                    },
                    tz_source=anchor.tz_source,
                    seq=current_sequence,
                    actor=anchor.actor,
                    env_profile=anchor.env_profile,
                    parsed_record={
                        'derived_from_observations': [previous.id, anchor.id],
                        'derivation': 'present in previous active list; absent from next active list',
                    },
                ))
    return derived


def process(records: Iterable[RawRecord], ids: Ids, profile) -> list[Observation]:
    rows = list(records)
    out: list[Observation] = []
    history = [row for row in rows if row.artifact == 'history']
    out.extend(_history(history, ids))

    for raw in rows:
        if raw.artifact == 'history':
            continue
        if raw.artifact == 'cache':
            out.extend(_cache(raw, ids))
        elif raw.artifact == 'cookies':
            out.append(_cookie(raw, ids))
        elif raw.artifact == 'local_storage':
            out.extend(_local_storage(raw, ids))
        elif raw.artifact == 'session_storage':
            out.append(_session_storage(raw, ids))
        elif raw.artifact == 'sessions':
            out.append(_session(raw, ids))
        else:
            row = raw.record if isinstance(raw.record, dict) else {}
            timestamps = {
                'ts_record': _chrome_time(
                    row.get('last_visit_time') or row.get('last_updated') or row.get('last_modified')
                )
            }
            out.append(_observation(
                raw, ids, f'{raw.artifact}_{raw.dataset}_record',
                {'dataset': raw.dataset}, kind='context', timestamps=timestamps,
            ))
    out.extend(_conversation_history_transitions(out, ids))
    return out


# Kept for the declarative extension API.  This profile uses process() because
# History tables must be joined and cache/local-storage values need decoding.
RAW_MAPPINGS: list[dict[str, Any]] = []
