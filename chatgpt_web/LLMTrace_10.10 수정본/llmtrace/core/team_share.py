from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, Iterable

from llmtrace import __version__

from .io import write_csv
from .raw_io import json_default


TEAM_SHARE_COLUMNS = ['시각', '사용자 행위', '아티팩트', '경로', '등급', '근거']
DIRECT_GRADE = '직접'
ATTRIBUTION_GRADE = '보조'
DEFAULT_CSV_FILENAME = 'LLM_Artifact_Report.csv'
DEFAULT_HTML_FILENAME = 'LLM_Artifact_Report.html'
PARSED_ARTIFACTS_FILENAME = 'parsed_artifacts.json'
CSV_MULTI_VALUE_SEPARATOR = ' | '
KST = timezone(timedelta(hours=9), name='KST')


def _parse_iso_timestamp(value: str) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _representative_time(when_from: str, when_to: str) -> tuple[str, float | None]:
    """Return the earliest available boundary in KST."""
    return _earliest_time((when_from, when_to))


def _format_kst(point: datetime) -> str:
    local = point.astimezone(KST)
    base = local.strftime('%Y-%m-%d %H:%M:%S')
    if local.microsecond:
        base += f'.{local.microsecond // 1000:03d}'
    return base + ' KST'


def _earliest_time(values: Iterable[Any]) -> tuple[str, float | None]:
    points = [
        point for point in (_parse_iso_timestamp(str(value or '')) for value in values)
        if point is not None
    ]
    if not points:
        return '-', None
    point = min(points)
    return _format_kst(point), point.timestamp()


def _observation_timestamp(row: Dict[str, Any]) -> str:
    timestamps = row.get('timestamps') or {}
    if isinstance(timestamps, dict):
        preferred = ('ts_event', 'ts_record', 'ts_start', 'ts_end')
        for key in preferred:
            if timestamps.get(key):
                return str(timestamps[key])
        for value in timestamps.values():
            if value:
                return str(value)
    for key, value in row.items():
        if str(key).startswith('ts_') and value:
            return str(value)
    return ''


def _write_raw_parsing_data(profile: Any, out: Path,
                            observations: Iterable[Any]) -> Dict[str, Any]:
    """Write every parsed record to one JSON file for separate evidence review."""
    observation_rows = [_row_dict(item) for item in observations]
    evidence_addresses = _evidence_addresses(observation_rows)
    config = profile.team_share
    records = []
    for row in observation_rows:
        artifact = str(row.get('source_artifact') or '')
        address = evidence_addresses.get(str(row.get('id') or ''), {})
        records.append({
            'observation_id': str(row.get('id') or ''),
            'evidence_address': str(address.get('base') or ''),
            'source_artifact': artifact,
            'artifact_name': _artifact_name(artifact, config),
            'timestamp': _observation_timestamp(row),
            'parser': str(row.get('parser') or artifact),
            'source_path': str(row.get('source_path') or ''),
            'source_locator': str(row.get('source_locator') or ''),
            'parsed_record': row.get('parsed_record'),
        })

    artifacts = sorted({
        (row['source_artifact'], row['artifact_name']) for row in records
    }, key=lambda item: (item[1], item[0]))
    payload = {
        'format': 'LLMTrace parsed artifacts',
        'version': 1,
        'record_count': len(records),
        'artifacts': [
            {'value': value, 'label': label} for value, label in artifacts
        ],
        'records': records,
    }
    json_path = out / PARSED_ARTIFACTS_FILENAME
    json_text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        default=json_default,
    )
    json_path.write_text(
        json_text,
        encoding='utf-8',
    )
    return {
        'records': len(records),
        'file': str(json_path),
        'filename': PARSED_ARTIFACTS_FILENAME,
    }


def _row_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, 'as_dict'):
        return value.as_dict()
    return {}


def _condition_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ('all_refs', 'any_refs') and isinstance(child, (list, tuple)):
                refs.update(str(item) for item in child)
            else:
                refs.update(_condition_refs(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            refs.update(_condition_refs(child))
    return refs


def _minimum_refs(profile: Any, base_rule_id: str) -> set[str]:
    levels = getattr(profile.rules, 'RULE_LEVELS', {}).get(base_rule_id, {})
    minimum = levels.get('minimum') or {}
    return _condition_refs(minimum.get('when') or {})


def _action_id(inference: Dict[str, Any], config: Any) -> str:
    behavior_id = str(inference.get('behavior_id') or '')
    if config is not None and hasattr(config, 'action_id_for_rule'):
        mapped = config.action_id_for_rule(inference)
        if mapped:
            return str(mapped)
    if behavior_id.startswith('B'):
        return f'A{behavior_id[1:]}'
    return behavior_id or 'ACTION'


def _action_title(action_id: str, inference: Dict[str, Any], config: Any) -> str:
    configured = getattr(config, 'ACTION_TITLES', {}) if config is not None else {}
    return str(configured.get(action_id) or inference.get('behavior_name') or action_id)


def _artifact_name(artifact: str, config: Any) -> str:
    configured = getattr(config, 'ARTIFACT_NAMES', {}) if config is not None else {}
    return str(configured.get(artifact) or artifact or 'unknown')


def _original_source_path(value: Any) -> str:
    """Restore an acquired Windows path to its source-volume absolute path."""
    text = str(value or '').strip()
    if not text:
        return '-'
    match = re.search(
        r'(?i)[\\/]_acquired[\\/]([a-z])[\\/](.+)$',
        text,
    )
    if match:
        return f'{match.group(1).upper()}:\\{match.group(2).replace("/", chr(92))}'
    return text


def _artifact_basename(source_path: str, fallback: str) -> str:
    text = str(source_path or '').rstrip('\\/')
    if not text or text == '-':
        return fallback
    return PureWindowsPath(text).name or fallback


def _nested_value(value: Any, dotted: str) -> Any:
    current = value
    for part in str(dotted).split('.'):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _rule_ref_fields(profile: Any, base_rule_id: str) -> Dict[str, list[str]]:
    rules = list(getattr(profile.rules, 'INFERENCE_RULES', ()))
    rules.extend(getattr(profile.rules, 'SUPPORTING_INFERENCE_RULES', ()))
    rule = next((item for item in rules if item.get('rule_id') == base_rule_id), None)
    output: Dict[str, list[str]] = {}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get('ref')
            match = value.get('match')
            if ref and isinstance(match, dict):
                output.setdefault(str(ref), []).extend(str(key) for key in match)
            for child in value.values():
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)

    if rule is not None:
        walk(rule)
    return {
        ref: list(dict.fromkeys(fields))
        for ref, fields in output.items()
    }


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=json_default)


def _json_path_key(prefix: str, key: Any) -> str:
    text = str(key)
    if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', text):
        return f'{prefix}.{text}' if prefix else text
    return f'{prefix}[{_json_value(text)}]'


def _iter_parsed_items(value: Any, prefix: str = '') \
        -> Iterable[tuple[str, str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            path = _json_path_key(prefix, key)
            yield path, str(key), child
            yield from _iter_parsed_items(child, path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            path = f'{prefix}[{index}]'
            yield path, str(index), child
            yield from _iter_parsed_items(child, path)


def _same_evidence_value(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _find_parsed_value_path(value: Any, target: Any) -> tuple[str, Any] | None:
    if target is None or isinstance(target, set):
        return None
    for path, _key, child in _iter_parsed_items(value):
        try:
            if _same_evidence_value(child, target):
                return path, child
        except (TypeError, ValueError):
            continue
    return None


def _canonical_field_name(value: Any) -> str:
    return re.sub(r'[^a-z0-9]', '', str(value).casefold())


def _parsed_evidence_item(parsed_record: Any, field: str, actual: Any) \
        -> tuple[str, Any] | None:
    exact = _find_parsed_value_path(parsed_record, actual)
    if exact is not None:
        return exact

    leaf = str(field).rsplit('.', 1)[-1]
    canonical_leaf = _canonical_field_name(leaf)
    aliases = {
        'hostname': {'hostname', 'host', 'hostkey', 'domain'},
        'conversationid': {'conversationid', 'conversationuuid'},
        'messageid': {'messageid', 'messageuuid'},
        'fileid': {'fileid', 'fileuuid', 'uploadid'},
        'accountid': {'accountid', 'accountuuid', 'userid'},
    }
    accepted_names = aliases.get(canonical_leaf, {canonical_leaf})
    items = list(_iter_parsed_items(parsed_record))
    for path, key, value in items:
        if _canonical_field_name(key) not in accepted_names:
            continue
        if isinstance(value, (dict, list, tuple)):
            continue
        if type(value) is type(actual):
            return path, value

    if not isinstance(actual, str) or len(actual.strip()) < 2:
        return None
    if canonical_leaf not in {
        'hostname', 'host', 'path', 'scheme', 'url', 'origin', 'route',
    } and not str(field).casefold().startswith('query.'):
        return None
    needle = actual.casefold()
    for path, key, value in items:
        if not isinstance(value, str) or len(value) > 8192:
            continue
        key_name = _canonical_field_name(key)
        if not (
            'url' in key_name
            or key_name in {'key', 'requestkey', 'request', 'origin'}
        ):
            continue
        if needle in value.casefold():
            return path, value
    return None


def _timestamp_parsed_item(parsed_record: Any, timestamp_key: str) \
        -> tuple[str, Any] | None:
    aliases = {
        'ts_visit': (
            'visit_time', 'last_visit_time', 'visit_time_utc',
        ),
        'ts_created': (
            'created_at', 'create_time', 'creation_time', 'creation_utc',
            'date_created', 'source_created_at',
        ),
        'ts_modified': (
            'updated_at', 'update_time', 'modified_at', 'last_modified',
            'last_update_utc',
        ),
        'ts_accessed': (
            'accessed_at', 'last_access_time', 'last_access_utc',
        ),
        'ts_expires': (
            'expires', 'expires_at', 'expiry', 'expires_utc',
        ),
        'ts_start': ('start_time', 'started_at'),
        'ts_end': ('end_time', 'ended_at'),
        'ts_event': (
            'event_time', 'timestamp', 'time', 'date', 'request_time',
            'response_time', 'update_time',
        ),
        'ts_record': (
            'record_time', 'timestamp', 'time', 'date', 'last_visit_time',
            'visit_time', 'last_access_utc', 'last_update_utc',
            'creation_utc', 'request_time', 'response_time',
        ),
    }
    items = list(_iter_parsed_items(parsed_record))
    for alias in aliases.get(timestamp_key, ()):
        wanted = _canonical_field_name(alias)
        for path, key, value in items:
            if _canonical_field_name(key) != wanted:
                continue
            if isinstance(value, (dict, list, tuple)):
                continue
            return path, value
    return None


def _representative_parsed_item(parsed_record: Any) -> tuple[str, Any] | None:
    priorities = (
        'url', 'request_key', 'key', 'conversation_id', 'message_id',
        'file_id', 'host_key', 'name', 'path', 'status', 'title', 'value',
    )
    items = list(_iter_parsed_items(parsed_record))
    for wanted_name in priorities:
        wanted = _canonical_field_name(wanted_name)
        for path, key, value in items:
            if _canonical_field_name(key) != wanted:
                continue
            if isinstance(value, (dict, list, tuple)):
                continue
            return path, value
    for path, _key, value in items:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return path, value
    return None


def _evidence_addresses(observations: Iterable[Dict[str, Any]]) \
        -> Dict[str, Dict[str, Any]]:
    artifact_indices: Dict[tuple[str, str], int] = {}
    record_indices: Dict[tuple[str, str], Dict[str, int]] = {}
    output: Dict[str, Dict[str, Any]] = {}
    for row in observations:
        observation_id = str(row.get('id') or '')
        source_path = _original_source_path(row.get('source_path'))
        artifact_key = (
            str(row.get('source_artifact') or '').casefold(),
            str(source_path or '').casefold(),
        )
        if artifact_key not in artifact_indices:
            artifact_indices[artifact_key] = len(artifact_indices)
        artifact_index = artifact_indices[artifact_key]
        locator = str(row.get('source_locator') or '').strip()
        record_key = locator or observation_id or f'ordinal:{len(output)}'
        by_record = record_indices.setdefault(artifact_key, {})
        if record_key not in by_record:
            by_record[record_key] = len(by_record)
        record_index = by_record[record_key]
        if observation_id:
            output[observation_id] = {
                'artifact_index': artifact_index,
                'record_index': record_index,
                'base': f'artifacts[{artifact_index}].records[{record_index}]',
                'source_locator': locator,
            }
    return output


def _linked_evidence_observations(observations: Iterable[Dict[str, Any]]) \
        -> Dict[str, list[Dict[str, Any]]]:
    rows = list(observations)
    same_record: Dict[tuple[str, str, str], list[Dict[str, Any]]] = {}
    history_urls: Dict[tuple[str, str, str], list[Dict[str, Any]]] = {}
    for row in rows:
        source_path = _original_source_path(row.get('source_path'))
        artifact = str(row.get('source_artifact') or '').casefold()
        locator = str(row.get('source_locator') or '').strip()
        scope = (artifact, str(source_path or '').casefold())
        if locator:
            same_record.setdefault((*scope, locator), []).append(row)
        fact = row.get('fact') or {}
        url_id = fact.get('url_id') if isinstance(fact, dict) else None
        if artifact == 'history' and url_id not in (None, ''):
            history_urls.setdefault((*scope, str(url_id)), []).append(row)

    output: Dict[str, list[Dict[str, Any]]] = {}
    for row in rows:
        observation_id = str(row.get('id') or '')
        if not observation_id:
            continue
        source_path = _original_source_path(row.get('source_path'))
        artifact = str(row.get('source_artifact') or '').casefold()
        locator = str(row.get('source_locator') or '').strip()
        scope = (artifact, str(source_path or '').casefold())
        candidates: list[Dict[str, Any]] = []
        if locator:
            candidates.extend(same_record.get((*scope, locator), ()))
        fact = row.get('fact') or {}
        url_id = fact.get('url_id') if isinstance(fact, dict) else None
        if artifact == 'history' and url_id not in (None, ''):
            candidates.extend(history_urls.get((*scope, str(url_id)), ()))
        seen: set[str] = set()
        output[observation_id] = []
        for candidate in candidates:
            candidate_id = str(candidate.get('id') or '')
            if not candidate_id or candidate_id == observation_id or candidate_id in seen:
                continue
            seen.add(candidate_id)
            output[observation_id].append(candidate)
    return output


def _timestamp_items(observation: Dict[str, Any]) -> list[tuple[str, str]]:
    timestamps = observation.get('timestamps') or {}
    items = []
    if isinstance(timestamps, dict):
        items.extend(
            (str(key), str(value)) for key, value in timestamps.items() if value
        )
    if not items:
        items.extend(
            (str(key), str(value)) for key, value in observation.items()
            if str(key).startswith('ts_') and value
        )
    return items


def _evidence_by_observation(inference: Dict[str, Any], profile: Any) \
        -> Dict[str, Dict[str, Any]]:
    detail = inference.get('detail') or {}
    refs = detail.get('refs') or {}
    minimum_refs = _minimum_refs(profile, str(detail.get('base_rule_id') or ''))
    support_only = bool(detail.get('support_only'))
    default_direct = not minimum_refs and not support_only
    output: Dict[str, Dict[str, Any]] = {}
    for ref_name, ref_value in refs.items():
        if not isinstance(ref_value, dict) or not ref_value.get('observation_id'):
            continue
        observation_id = str(ref_value['observation_id'])
        grade = (
            DIRECT_GRADE
            if not support_only and (default_direct or ref_name in minimum_refs)
            else ATTRIBUTION_GRADE
        )
        item = output.setdefault(observation_id, {
            'grade': grade,
            'refs': [],
        })
        if item['grade'] != DIRECT_GRADE and grade == DIRECT_GRADE:
            item['grade'] = DIRECT_GRADE
        item['refs'].append((str(ref_name), ref_value))
    for observation_id in inference.get('supported_by') or []:
        output.setdefault(str(observation_id), {
            'grade': ATTRIBUTION_GRADE,
            'refs': [],
        })
    return output


def _evidence_text(inference: Dict[str, Any], observation: Dict[str, Any],
                   refs: list[tuple[str, Dict[str, Any]]], profile: Any,
                   address: Dict[str, Any] | None = None,
                   linked_observations: Iterable[Dict[str, Any]] = (),
                   evidence_addresses: Dict[str, Dict[str, Any]] | None = None) \
        -> str:
    observation_id = str(observation.get('id') or '')
    detail = inference.get('detail') or {}
    base_rule_id = str(detail.get('base_rule_id') or '')
    ref_fields = _rule_ref_fields(profile, base_rule_id)
    fact = observation.get('fact') or {}
    parsed_record = observation.get('parsed_record')
    address = address or {}
    evidence_addresses = evidence_addresses or {}
    base = str(address.get('base') or f'observations[{observation_id}]')
    parts: list[str] = []
    emitted: set[str] = set()

    def append_item(path: str, value: Any, item_base: str = base) -> None:
        location = f'{item_base}.{path}' if path else item_base
        if location in emitted:
            return
        emitted.add(location)
        parts.append(f'{location}={_json_value(value)}')

    seen_fields: set[str] = set()
    for ref_name, ref_value in refs:
        candidates = list(ref_fields.get(ref_name, ()))
        candidates.extend(
            str(key) for key in ref_value
            if key != 'observation_id'
        )
        for field in candidates:
            if field in seen_fields:
                continue
            seen_fields.add(field)
            if field == 'source_locator':
                continue
            actual = ref_value.get(field)
            if actual is None:
                actual = _nested_value(fact, field)
            if actual is None:
                continue
            parsed_item = _parsed_evidence_item(parsed_record, field, actual)
            if parsed_item is not None:
                append_item(*parsed_item)
                continue
            linked_item = None
            linked_base = ''
            for linked in linked_observations:
                candidate = _parsed_evidence_item(
                    linked.get('parsed_record'), field, actual,
                )
                if candidate is None:
                    continue
                linked_item = candidate
                linked_address = evidence_addresses.get(
                    str(linked.get('id') or ''), {},
                )
                linked_base = str(linked_address.get('base') or '')
                break
            if linked_item is not None and linked_base:
                append_item(*linked_item, item_base=linked_base)
            else:
                append_item(f'normalized.{field}', actual)

    if not seen_fields:
        representative = _representative_parsed_item(parsed_record)
        if representative is not None:
            append_item(*representative)
        else:
            append_item(
                'normalized.fact_type', observation.get('fact_type') or '',
            )
    for key, value in _timestamp_items(observation):
        parsed_item = _timestamp_parsed_item(parsed_record, key)
        if parsed_item is not None:
            append_item(*parsed_item)
        else:
            append_item(f'timestamps.{key}', value)
    locator = str(address.get('source_locator') or '').strip()
    if locator:
        append_item('_source_locator', locator)
    return '; '.join(parts)


def _build_match_actions(profile: Any, observations: Iterable[Any],
                         inferences: Iterable[Any]) -> list[Dict[str, Any]]:
    config = profile.team_share
    observation_rows = [_row_dict(item) for item in observations]
    observation_map = {
        str(row.get('id')): row
        for row in observation_rows
        if row.get('id')
    }
    evidence_addresses = _evidence_addresses(observation_rows)
    linked_observations = _linked_evidence_observations(observation_rows)
    actions: Dict[str, Dict[str, Any]] = {}

    for inference_value in inferences:
        inference = _row_dict(inference_value)
        inference_detail = inference.get('detail') or {}
        support_only = bool(
            isinstance(inference_detail, dict)
            and inference_detail.get('support_only')
        )
        action_id = _action_id(inference, config)
        action = actions.setdefault(action_id, {
            'id': action_id,
            'title': _action_title(action_id, inference, config),
            'occurrences': 0,
            'matches': [],
            '_rows': {},
        })
        action['occurrences'] += 1
        match_rows: Dict[tuple[str, str], Dict[str, str]] = {}
        evidence_by_observation = _evidence_by_observation(inference, profile)
        grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
        for observation_id, evidence_info in evidence_by_observation.items():
            observation = observation_map.get(observation_id)
            if observation is None:
                continue
            source_kind = str(observation.get('source_artifact') or '')
            source_path = _original_source_path(observation.get('source_path'))
            source_locator = str(observation.get('source_locator') or '').strip()
            path = source_path if source_path != '-' else (source_locator or '-')
            artifact = _artifact_basename(
                source_path,
                _artifact_name(source_kind, config),
            )
            key = (artifact, path)
            group = grouped.setdefault(key, {
                'artifact': artifact,
                'path': path,
                'grade': ATTRIBUTION_GRADE,
                'evidence': [],
                'timestamps': [],
            })
            grade = str(evidence_info.get('grade') or ATTRIBUTION_GRADE)
            if grade == DIRECT_GRADE:
                group['grade'] = DIRECT_GRADE
            group['evidence'].append(_evidence_text(
                inference,
                observation,
                list(evidence_info.get('refs') or []),
                profile,
                evidence_addresses.get(observation_id),
                linked_observations.get(observation_id, ()),
                evidence_addresses,
            ))
            group['timestamps'].extend(value for _, value in _timestamp_items(observation))

        for key, group in grouped.items():
            display_time, sort_time = _earliest_time(group['timestamps'])
            if sort_time is None:
                display_time, sort_time = _representative_time(
                    str(inference.get('when_from') or ''),
                    str(inference.get('when_to') or ''),
                )
            row = {
                '시각': display_time,
                '사용자 행위': action['title'],
                '아티팩트': group['artifact'],
                '경로': group['path'],
                '등급': group['grade'],
                '근거': CSV_MULTI_VALUE_SEPARATOR.join(group['evidence']),
                '_time_sort': sort_time,
                '_action_id': action_id,
            }
            match_rows[key] = row
            existing = action['_rows'].get(key)
            if existing is None or (
                existing['등급'] == ATTRIBUTION_GRADE
                and row['등급'] == DIRECT_GRADE
            ):
                action['_rows'][key] = row

        match_time_candidates = [
            row['_time_sort'] for row in match_rows.values()
            if row.get('_time_sort') is not None
        ]
        if match_time_candidates:
            match_point = datetime.fromtimestamp(min(match_time_candidates), tz=timezone.utc)
            match_display_time = _format_kst(match_point)
            match_sort_time = match_point.timestamp()
        else:
            match_display_time, match_sort_time = _representative_time(
                str(inference.get('when_from') or ''),
                str(inference.get('when_to') or ''),
            )
        action['matches'].append({
            'id': str(inference.get('id') or ''),
            'rule_id': str(inference.get('rule_id') or ''),
            'support_only': support_only,
            'confidence': str(inference.get('confidence') or ''),
            'when_from': str(inference.get('when_from') or ''),
            'when_to': str(inference.get('when_to') or ''),
            'display_time': match_display_time,
            '_sort_time': match_sort_time,
            '_input_order': action['occurrences'],
            'rows': sorted(
                match_rows.values(),
                key=lambda row: (
                    0 if row['등급'] == DIRECT_GRADE else 1,
                    row['아티팩트'], row['경로'],
                ),
            ),
        })

    output = []
    for action_id in sorted(actions):
        action = actions[action_id]
        action['matches'].sort(key=lambda match: (
            match['_sort_time'] is None,
            match['_sort_time'] if match['_sort_time'] is not None else 0,
            match['_input_order'],
        ))
        action['rows'] = sorted(
            action.pop('_rows').values(),
            key=lambda row: (
                0 if row['등급'] == DIRECT_GRADE else 1,
                row['아티팩트'], row['경로'],
            ),
        )
        output.append(action)
    return output


def _build_match_csv_rows(actions: Iterable[Dict[str, Any]]) -> list[Dict[str, str]]:
    """Create one CSV row per inference match and source artifact file."""
    output = []
    for action in actions:
        for match in action['matches']:
            for row in match.get('rows') or []:
                cooked = {column: str(row.get(column) or '-') for column in TEAM_SHARE_COLUMNS}
                cooked['_time_sort'] = row.get('_time_sort')
                cooked['_action_id'] = action['id']
                cooked['_input_order'] = match.get('_input_order') or 0
                output.append(cooked)
    output.sort(key=lambda row: (
        row['_time_sort'] is None,
        row['_time_sort'] if row['_time_sort'] is not None else 0,
        row['_action_id'],
        row['_input_order'],
        row['경로'],
    ))
    return [
        {column: row[column] for column in TEAM_SHARE_COLUMNS}
        for row in output
    ]


def _render_html(profile: Any, actions: list[Dict[str, Any]],
                  environment: Dict[str, Any]) -> str:
    metadata = profile.metadata
    generated = _format_kst(datetime.now(timezone.utc))

    def artifact_table(match_rows: Iterable[Dict[str, str]]) -> str:
        rows = []
        for row in match_rows:
            grade = row['등급']
            grade_class = 'direct' if grade == DIRECT_GRADE else 'attribution'
            rows.append(
                '<tr>'
                f'<td class="evidence-time">{html.escape(row["시각"])}</td>'
                f'<td>{html.escape(row["아티팩트"])}</td>'
                f'<td class="path">{html.escape(row["경로"])}</td>'
                f'<td><span class="grade {grade_class}">'
                f'{html.escape(grade)}</span></td>'
                f'<td class="evidence">{html.escape(row["근거"])}</td></tr>'
            )
        if not rows:
            return '<p class="empty">연결된 아티팩트 경로를 찾지 못했습니다.</p>'
        return (
            '<div class="table-wrap"><table><thead><tr>'
            '<th>시각</th><th>아티팩트</th><th>경로</th><th>등급</th><th>근거</th>'
            '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
        )

    blocks = []
    for action in actions:
        match_blocks = []
        for match_index, match in enumerate(action['matches'], start=1):
            table = artifact_table(match['rows'])
            when_from = match.get('when_from') or ''
            when_to = match.get('when_to') or ''
            when = match.get('display_time') or ''
            date_text, _, clock_text = when.partition(' ')
            original_range = ' ~ '.join(
                value for value in (when_from, when_to)
                if value and value != when_from
            )
            if when_from and when_to and when_from != when_to:
                original_range = f'{when_from} ~ {when_to}'
            else:
                original_range = when_from or when_to
            time_label = (
                f'<span class="match-time" title="'
                f'{html.escape("원본 근거 범위: " + original_range, quote=True)}">'
                f'<span class="match-date">{html.escape(date_text)}</span>'
                f'<span class="match-clock">{html.escape(clock_text)}</span></span>'
                if when else ''
            )
            match_search = html.escape(
                ' '.join(
                    [action['id'], action['title'], str(match_index), when]
                    + [
                        f"{row['아티팩트']} {row['경로']} {row['등급']} {row['근거']}"
                        for row in match['rows']
                    ]
                )
            ).lower()
            match_blocks.append(
                f'<details class="match" data-search="{match_search}"><summary>'
                f'<strong>매칭 {match_index}</strong>{time_label}'
                f'</summary><div class="match-body">{table}</div></details>'
            )
        search = html.escape(
            f"{action['id']} {action['title']} "
            + ' '.join(
                f"{row['아티팩트']} {row['경로']} {row['등급']} {row['근거']}"
                for match in action['matches'] for row in match['rows']
            )
        ).lower()
        blocks.append(
            f'<details class="action" data-search="{search}"><summary>'
            f'<span class="action-id">{html.escape(action["id"])}</span>'
            f'<strong>{html.escape(action["title"])}</strong>'
            f'<span class="summary-meta">매칭 {action["occurrences"]}건</span></summary>'
            f'<div class="action-body"><div class="match-list">'
            f'{"".join(match_blocks)}</div></div></details>'
        )

    if not blocks:
        blocks.append('<p class="empty">규칙에 매칭된 사용자 행위가 없습니다.</p>')

    target = ' '.join(
        part for part in (
            str(metadata.get('service') or ''),
            {'web': 'Web', 'app': 'Desktop'}.get(
                str(metadata.get('surface') or '').lower(),
                str(metadata.get('surface') or ''),
            ),
        ) if part
    )
    source_type = str(environment.get('source_type') or 'Unknown')
    source = str(environment.get('source') or environment.get('input') or '')
    machine = str(environment.get('machine') or '')
    os_name = str(environment.get('os') or '')
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLMTrace 매칭 결과</title><style>
:root{{--ink:#172033;--muted:#64748b;--line:#dce3ed;--panel:#fff;--navy:#17345f;--bg:#f4f7fb}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 Arial,"Malgun Gothic",sans-serif}}
.app-shell{{display:grid;grid-template-columns:190px minmax(0,1fr);min-height:100vh}}.sidebar{{position:sticky;top:0;height:100vh;background:#10294d;color:#fff;padding:22px 12px}}.sidebar-brand{{font-size:20px;font-weight:800;padding:2px 9px 20px}}.sidebar nav{{display:grid;gap:7px}}.nav-tab{{width:100%;border:0;border-radius:9px;padding:11px;text-align:left;background:transparent;color:#cad6e8;font-weight:700;cursor:pointer}}.nav-tab:hover{{background:#ffffff12;color:#fff}}.nav-tab.active{{background:#fff;color:#10294d}}.workspace{{min-width:0}}
main{{width:100%;max-width:1900px;margin:0 auto;padding:26px 32px 40px}}header{{margin-bottom:16px}}h1{{font-size:28px;margin:0 0 6px}}h2{{font-size:19px;margin:0 0 14px}}.view{{display:none}}.view.active{{display:block}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;margin:14px 0;box-shadow:0 4px 18px #1720330a}}
.overview{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}}.overview div{{border:1px solid var(--line);border-radius:10px;padding:11px}}.overview span{{display:block;color:var(--muted);font-size:12px}}.overview strong{{font-size:15px;word-break:break-word}}
.legend{{display:flex;gap:8px;flex-wrap:wrap}}.grade{{display:inline-block;border-radius:999px;padding:3px 9px;font-size:12px;font-weight:700;white-space:nowrap}}.direct{{background:#dff4e9;color:#155e3d}}.attribution{{background:#e8f0ff;color:#244b91}}
.toolbar{{display:flex;gap:9px;flex-wrap:wrap;margin:14px 0}}.toolbar input{{flex:1;min-width:250px;padding:10px 12px;border:1px solid #b8c3d3;border-radius:8px}}.toolbar button{{padding:9px 12px;border:1px solid #9aacbf;background:#fff;border-radius:8px;cursor:pointer}}
details.action{{background:#fff;border:1px solid var(--line);border-radius:12px;margin:9px 0;overflow:hidden}}details.action>summary{{display:flex;align-items:center;gap:10px;padding:14px 16px;cursor:pointer;list-style:none}}details.action>summary::-webkit-details-marker{{display:none}}details.action>summary:after{{content:"＋";margin-left:auto;color:var(--muted);font-size:18px}}details.action[open]>summary:after{{content:"－"}}.action-id{{background:var(--navy);color:#fff;border-radius:6px;padding:3px 7px;font-weight:700}}.summary-meta{{color:var(--muted);margin-left:6px}}.action-body{{border-top:1px solid var(--line);padding:16px}}
.match-list{{display:grid;gap:9px}}details.match{{border:1px solid var(--line);border-radius:9px;overflow:hidden;background:#fbfcfe}}details.match>summary{{display:flex;align-items:center;gap:12px;padding:11px 13px;cursor:pointer;list-style:none}}details.match>summary::-webkit-details-marker{{display:none}}details.match>summary:after{{content:"＋";margin-left:auto;color:var(--muted)}}details.match[open]>summary:after{{content:"－"}}.match-time{{display:inline-flex;align-items:center;gap:8px;color:var(--ink);font-size:13px;font-weight:700}}.match-date,.match-clock{{display:inline-block;padding:2px 7px;border:1px solid var(--line);border-radius:6px;background:#fff}}.match-meta{{color:var(--muted);font-size:12px}}.timezone-note{{margin-left:auto;color:var(--muted);font-size:12px;font-weight:700}}.match-body{{border-top:1px solid var(--line);padding:0 12px;background:#fff}}
.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid var(--line);padding:10px;text-align:left;vertical-align:top}}th{{background:#f7f9fc;color:#31415c}}.evidence-time{{min-width:190px;white-space:nowrap;font-weight:700}}.path{{font-family:Consolas,"Malgun Gothic",monospace;font-size:12px;min-width:360px;word-break:break-all}}.evidence{{font-family:Consolas,"Malgun Gothic",monospace;font-size:12px;min-width:520px;white-space:normal;word-break:break-word}}.hidden{{display:none}}.empty{{border:1px dashed #b8c3d3;border-radius:9px;padding:15px;color:var(--muted)}}footer{{color:var(--muted);font-size:12px;margin:20px 2px}}
@media(max-width:900px){{.app-shell{{grid-template-columns:1fr}}.sidebar{{position:static;height:auto;padding:12px}}.sidebar-brand{{display:none}}.sidebar nav{{grid-template-columns:1fr}}.nav-tab{{text-align:center}}main{{padding:18px}}}}@media(max-width:700px){{main{{padding:14px}}.summary-meta{{display:none}}.path{{min-width:280px}}}}
</style></head><body><div class="app-shell"><aside class="sidebar"><div class="sidebar-brand">LLMTrace</div><nav>
<button class="nav-tab active" type="button" data-view="summary-view">종합분석</button>
</nav></aside><div class="workspace"><main><section id="summary-view" class="view active"><header><h1>LLMTrace 매칭 결과</h1></header>
<section class="panel"><h2>분석 개요</h2><div class="overview">
<div><span>분석 대상</span><strong>{html.escape(target)}</strong></div>
<div><span>입력 형식</span><strong>{html.escape(source_type)}</strong></div>
<div><span>입력 원본</span><strong>{html.escape(source)}</strong></div>
<div><span>분석 시스템</span><strong>{html.escape(machine)} · {html.escape(os_name)}</strong></div>
<div><span>프로필</span><strong>{html.escape(str(metadata.get('profile_id') or ''))} v{html.escape(str(metadata.get('version') or ''))}</strong></div>
<div><span>도구</span><strong>LLM Artifact Analyzer v{html.escape(__version__)}</strong></div>
<div><span>매칭 행위</span><strong>{len(actions)}개</strong></div>
</div>
<div class="legend"><span class="timezone-note">표시 시간대: KST</span></div></section>
<section class="panel"><h2>행위별 매칭 결과</h2><div class="toolbar">
<input id="search" type="search" placeholder="행위명, 아티팩트 또는 경로 검색">
<button id="expand" type="button">모두 펼치기</button><button id="collapse" type="button">모두 접기</button></div>
{''.join(blocks)}</section></section><footer>생성 시각: {html.escape(generated)}</footer>
</main></div></div><script>const cards=[...document.querySelectorAll('details.action')];const matches=[...document.querySelectorAll('details.match')];
document.getElementById('expand').addEventListener('click',()=>[...cards,...matches].filter(x=>!x.classList.contains('hidden')).forEach(x=>x.open=true));
document.getElementById('collapse').addEventListener('click',()=>[...cards,...matches].forEach(x=>x.open=false));
document.getElementById('search').addEventListener('input',e=>{{const q=e.target.value.trim().toLowerCase();cards.forEach(card=>{{const visible=!q||card.dataset.search.includes(q);card.classList.toggle('hidden',!visible);if(visible&&q)card.open=true;card.querySelectorAll('details.match').forEach(match=>{{const matchVisible=!q||match.dataset.search.includes(q);match.classList.toggle('hidden',!matchVisible);if(matchVisible&&q)match.open=true;}});}});}});

</script>
</body></html>'''


def _simple_action_catalog(profile: Any,
                           actions: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """Return configured actions as well as actions that actually matched."""
    action_map = {str(action.get('id') or ''): action for action in actions}
    configured = getattr(profile.team_share, 'ACTION_TITLES', {}) or {}
    action_ids = sorted(set(configured) | set(action_map))
    return [
        action_map.get(action_id, {
            'id': action_id,
            'title': str(configured.get(action_id) or action_id),
            'occurrences': 0,
            'matches': [],
            'rows': [],
        })
        for action_id in action_ids
    ]


def _render_simple_html(profile: Any, actions: list[Dict[str, Any]],
                        environment: Dict[str, Any]) -> str:
    """Render the compact, sidebar-free report including unmatched actions."""
    metadata = profile.metadata
    generated = _format_kst(datetime.now(timezone.utc))
    catalog = _simple_action_catalog(profile, actions)

    def artifact_table(match_rows: Iterable[Dict[str, str]]) -> str:
        rows = []
        for row in match_rows:
            grade = row['등급']
            grade_class = 'direct' if grade == DIRECT_GRADE else 'supporting'
            rows.append(
                '<tr>'
                f'<td class="evidence-time">{html.escape(row["시각"])}</td>'
                f'<td>{html.escape(row["아티팩트"])}</td>'
                f'<td class="path">{html.escape(row["경로"])}</td>'
                f'<td><span class="grade {grade_class}">'
                f'{html.escape(grade)}</span></td>'
                f'<td class="evidence">{html.escape(row["근거"])}</td></tr>'
            )
        if not rows:
            return '<p class="empty">연결된 아티팩트 경로를 찾지 못했습니다.</p>'
        return (
            '<div class="table-wrap"><table><thead><tr>'
            '<th>시각</th><th>아티팩트</th><th>경로</th><th>등급</th><th>근거</th>'
            '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
        )

    blocks = []
    for action in catalog:
        match_blocks = []
        for match_index, match in enumerate(action['matches'], start=1):
            when = match.get('display_time') or ''
            date_text, _, clock_text = when.partition(' ')
            when_from = match.get('when_from') or ''
            when_to = match.get('when_to') or ''
            original_range = (
                f'{when_from} ~ {when_to}'
                if when_from and when_to and when_from != when_to
                else when_from or when_to
            )
            time_label = (
                f'<span class="match-time" title="'
                f'{html.escape("원본 근거 범위: " + original_range, quote=True)}">'
                f'<span class="match-date">{html.escape(date_text)}</span>'
                f'<span class="match-clock">{html.escape(clock_text)}</span></span>'
                if when else '<span class="match-time">시각 미확인</span>'
            )
            match_rows = match.get('rows') or []
            match_search = html.escape(
                ' '.join(
                    [action['id'], action['title'], str(match_index), when]
                    + [
                        f"{row['아티팩트']} {row['경로']} {row['등급']} {row['근거']}"
                        for row in match_rows
                    ]
                ),
                quote=True,
            ).lower()
            match_blocks.append(
                f'<details class="match" data-search="{match_search}"><summary>'
                f'{time_label}<span class="match-meta">근거 {len(match_rows)}건</span>'
                f'</summary><div class="match-body">{artifact_table(match_rows)}'
                f'</div></details>'
            )

        search = html.escape(
            f"{action['id']} {action['title']} "
            + ' '.join(
                f"{row['아티팩트']} {row['경로']} {row['등급']} {row['근거']}"
                for match in action['matches'] for row in match.get('rows') or []
            ),
            quote=True,
        ).lower()
        body = (
            ''.join(match_blocks)
            if match_blocks
            else '<p class="empty">이 분석에서 매칭된 근거가 없습니다.</p>'
        )
        blocks.append(
            f'<details class="action" data-search="{search}"><summary>'
            f'<span class="action-id">{html.escape(action["id"])}</span>'
            f'<span class="action-name">{html.escape(action["title"])}</span>'
            f'<span class="summary-meta">매칭 {action["occurrences"]}건</span>'
            f'</summary><div class="action-body">{body}</div></details>'
        )

    if not blocks:
        blocks.append('<p class="empty">규칙에 매칭된 사용자 행위가 없습니다.</p>')

    matched_action_count = sum(1 for action in actions if action['occurrences'])
    evidence_rows = [
        row
        for action in actions
        for match in action['matches']
        for row in match.get('rows') or []
    ]
    direct_count = sum(1 for row in evidence_rows if row['등급'] == DIRECT_GRADE)
    support_count = sum(
        1 for row in evidence_rows if row['등급'] == ATTRIBUTION_GRADE
    )
    target = ' '.join(
        part for part in (
            str(metadata.get('service') or ''),
            {'web': 'Web', 'app': 'Desktop'}.get(
                str(metadata.get('surface') or '').lower(),
                str(metadata.get('surface') or ''),
            ),
        ) if part
    )
    source_type = str(environment.get('source_type') or 'Unknown')
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLM_Artifact_Report</title><style>
:root{{--ink:#172033;--muted:#64748b;--line:#dce3ed;--panel:#fff;--navy:#17345f;--bg:#f4f7fb}}
*{{box-sizing:border-box}}body{{max-width:1900px;margin:0 auto;padding:26px 32px 40px;background:var(--bg);color:var(--ink);font:14px/1.55 Arial,"Malgun Gothic",sans-serif}}
h1{{font-size:28px;margin:0 0 16px}}h2{{font-size:19px;margin:0 0 14px}}.overview,.results{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;margin:14px 0;box-shadow:0 4px 18px #1720330a}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}}.stats div{{border:1px solid var(--line);border-radius:10px;padding:11px}}.stats span{{display:block;color:var(--muted);font-size:12px}}.stats strong{{font-size:15px;overflow-wrap:anywhere}}
.toolbar{{display:flex;gap:9px;flex-wrap:wrap;margin:14px 0}}.toolbar input{{flex:1;min-width:250px;padding:10px 12px;border:1px solid #b8c3d3;border-radius:8px}}.toolbar button{{padding:9px 12px;border:1px solid #9aacbf;background:#fff;border-radius:8px;cursor:pointer}}
details.action{{background:#fff;border:1px solid var(--line);border-radius:12px;margin:9px 0;overflow:hidden}}details.action>summary,details.match>summary{{display:flex;align-items:center;gap:10px;padding:14px 16px;cursor:pointer;list-style:none}}summary::-webkit-details-marker{{display:none}}summary:after{{content:"＋";margin-left:auto;color:var(--muted);font-size:18px}}details[open]>summary:after{{content:"－"}}details[open]>summary{{border-bottom:1px solid var(--line)}}.action-id{{background:var(--navy);color:#fff;border-radius:6px;padding:3px 7px;font-weight:700}}.action-name{{font-weight:700}}.summary-meta,.match-meta{{color:var(--muted)}}.action-body{{padding:8px 16px 16px}}
details.match{{background:#fbfcfe;border:1px solid var(--line);border-radius:9px;margin:9px 0;overflow:hidden}}.match-time{{display:inline-flex;gap:7px;align-items:center;font-weight:700}}.match-date,.match-clock{{border:1px solid var(--line);border-radius:6px;background:#fff;padding:2px 7px}}.match-body{{background:#fff;padding:0 12px}}
.grade{{display:inline-block;border-radius:999px;padding:3px 9px;font-size:12px;font-weight:700;white-space:nowrap}}.direct{{background:#dff4e9;color:#155e3d}}.supporting{{background:#e8f0ff;color:#244b91}}.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid var(--line);padding:10px;text-align:left;vertical-align:top}}th{{background:#f7f9fc;color:#31415c}}.evidence-time{{min-width:190px;white-space:nowrap;font-weight:700}}.path{{font:12px/1.55 Consolas,"Malgun Gothic",monospace;min-width:360px;word-break:break-all}}.evidence{{font:12px/1.55 Consolas,"Malgun Gothic",monospace;min-width:520px;white-space:normal;word-break:break-word}}.empty{{border:1px dashed #b8c3d3;border-radius:9px;padding:15px;color:var(--muted)}}.hidden{{display:none}}footer{{color:var(--muted);font-size:12px;margin:20px 2px}}
@media(max-width:700px){{body{{padding:14px}}.overview,.results{{padding:14px}}.summary-meta{{display:none}}.path{{min-width:280px}}}}
</style></head><body><h1>LLM_Artifact_Report</h1><main>
<section class="overview"><h2>분석 개요</h2><div class="stats">
<div><span>분석 대상</span><strong>{html.escape(target)}</strong></div>
<div><span>입력 형식</span><strong>{html.escape(source_type)}</strong></div>
<div><span>매칭 행위</span><strong>{matched_action_count}개</strong></div>
<div><span>근거 개수</span><strong>직접 {direct_count} / 보조 {support_count}</strong></div>
</div></section><section class="results"><h2>행위별 매칭 결과</h2><div class="toolbar">
<input id="search" type="search" placeholder="행위명, 아티팩트 또는 경로 검색">
<button id="expand" type="button">모두 펼치기</button><button id="collapse" type="button">모두 접기</button></div>
{''.join(blocks)}</section><footer>생성 시각: {html.escape(generated)}</footer></main>
<script>const cards=[...document.querySelectorAll('details.action')];const matches=[...document.querySelectorAll('details.match')];
document.getElementById('expand').addEventListener('click',()=>[...cards,...matches].filter(x=>!x.classList.contains('hidden')).forEach(x=>x.open=true));
document.getElementById('collapse').addEventListener('click',()=>[...cards,...matches].forEach(x=>x.open=false));
document.getElementById('search').addEventListener('input',e=>{{const q=e.target.value.trim().toLowerCase();cards.forEach(card=>{{const visible=!q||card.dataset.search.includes(q);card.classList.toggle('hidden',!visible);if(visible&&q)card.open=true;card.querySelectorAll('details.match').forEach(match=>{{const matchVisible=!q||match.dataset.search.includes(q);match.classList.toggle('hidden',!matchVisible);if(matchVisible&&q)match.open=true;}});}});}});
</script></body></html>'''


def export_team_share(profile: Any, out_dir: str,
                      observations: Iterable[Any] = (),
                      inferences: Iterable[Any] = (),
                      environment: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Export a case-specific visualization of matched actions and evidence."""
    observations = list(observations)
    inferences = list(inferences)
    actions = _build_match_actions(profile, observations, inferences)
    artifact_rows = [row for action in actions for row in action['rows']]
    csv_rows = _build_match_csv_rows(actions)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    config = profile.team_share
    csv_name = getattr(config, 'CSV_FILENAME', DEFAULT_CSV_FILENAME)
    html_name = getattr(config, 'HTML_FILENAME', DEFAULT_HTML_FILENAME)
    csv_path = out / csv_name
    html_path = out / html_name
    write_csv(str(csv_path), TEAM_SHARE_COLUMNS, csv_rows)
    raw_data = _write_raw_parsing_data(profile, out, observations)
    html_path.write_text(
        _render_simple_html(profile, actions, environment or {}),
        encoding='utf-8',
    )
    return {
        'profile': profile.metadata,
        'mode': 'case_match_export',
        'actions': len(actions),
        'matches': len(inferences),
        'csv_rows': len(csv_rows),
        'artifact_rows': len(artifact_rows),
        'raw_records': raw_data['records'],
        'raw_data': raw_data,
        'csv': str(csv_path),
        'html': str(html_path),
        'output': str(out),
    }
