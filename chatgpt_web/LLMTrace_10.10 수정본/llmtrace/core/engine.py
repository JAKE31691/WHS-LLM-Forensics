from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, urlsplit

from .schema import ContextRecord, Ids, Inference, Observation, SUSPECTED
from .windows import (
    EvidenceWindow,
    build_history_windows,
    contains as window_contains,
    observation_time,
    span_seconds,
)


@dataclass
class RuleBinding:
    refs: Dict[str, Observation]
    extracted: Dict[str, Dict[str, Any]]
    window: Optional[EvidenceWindow] = None


def _obs_dict(o: Observation) -> Dict[str, Any]:
    d = o.as_dict()
    # Rules may address normalized fields directly or through fact/data/payload.
    d.setdefault('artifact', o.source_artifact)
    d.setdefault('data', o.fact)
    d.setdefault('payload', o.fact)
    for k, v in o.fact.items():
        d.setdefault(k, v)
    return d


def _get(obj: Any, dotted: str, default=None):
    if dotted == '':
        return obj
    cur = obj
    for part in dotted.split('.'):
        if isinstance(cur, Observation):
            cur = _obs_dict(cur)
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
                continue
            # Compatibility helpers: query.foo, fragment_query.foo, json.foo,
            # original_request_url.query.foo, tab_url.hostname, url.hostname/path.
            return default
        if isinstance(cur, str) and part in ('scheme', 'hostname', 'path', 'query', 'fragment'):
            try:
                parsed = urlsplit(cur)
                if part == 'query':
                    cur = {k: (v[-1] if len(v) == 1 else v) for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
                else:
                    cur = getattr(parsed, part)
                continue
            except (TypeError, ValueError):
                return default
        return default
    return cur


def _set_if_group(obs: Observation, match_key: str, pattern: str, value: Any):
    """Regex named captures are made available to later association rules.

    We do not mutate fact unless a capture was actually produced.
    """
    try:
        m = re.search(pattern, '' if value is None else str(value))
    except re.error:
        return
    if m:
        for k, v in m.groupdict().items():
            if v is not None:
                obs.fact.setdefault(k, v)


def _match_value(value: Any, cond: Any, obs: Optional[Observation] = None, key: str = '') -> bool:
    if not isinstance(cond, dict):
        return value == cond
    for op, wanted in cond.items():
        if op == 'eq':
            if value != wanted:
                return False
        elif op == 'neq':
            if value == wanted:
                return False
        elif op == 'in':
            if value not in wanted:
                return False
        elif op == 'exists':
            if (value is not None) != bool(wanted):
                return False
        elif op == 'regex':
            if re.search(wanted, '' if value is None else str(value)) is None:
                return False
            if obs is not None:
                _set_if_group(obs, key, wanted, value)
        elif op == 'prefix':
            if not str(value or '').startswith(str(wanted)):
                return False
        elif op == 'suffix':
            if not str(value or '').endswith(str(wanted)):
                return False
        elif op == 'contains':
            if isinstance(value, (list, tuple, set)):
                if wanted not in value:
                    return False
            elif str(wanted) not in str(value or ''):
                return False
        elif op == 'nonempty':
            try:
                present = value is not None and len(value) > 0
            except TypeError:
                present = value not in (None, '')
            if present != bool(wanted):
                return False
        elif op in ('query_eq', 'fragment_query_eq'):
            if value != wanted:
                return False
        else:
            # Unknown operators fail closed rather than silently matching.
            return False
    return True


def _field(obs: Observation, key: str):
    d = _obs_dict(obs)
    v = _get(d, key, None)
    if v is not None:
        return v
    # A frequent normalized representation stores parsed URL pieces in fact.url.
    # If url itself is a mapping, dotted access through fact is already covered.
    return None


def matches(obs: Observation, spec: Dict[str, Any]) -> bool:
    artifact = spec.get('artifact')
    if artifact and obs.source_artifact != artifact:
        return False
    ft = spec.get('fact_type')
    if ft and obs.fact_type != ft:
        return False
    fti = spec.get('fact_type_in')
    if fti and obs.fact_type not in fti:
        return False
    for key, cond in (spec.get('match') or spec.get('requires') or {}).items():
        if key == 'same_conversation_id_membership':
            # Evaluated by state-transition pairing, not a single observation.
            continue
        if not _match_value(_field(obs, key), cond, obs=obs, key=key):
            return False
    return True


def _time(obs: Optional[Observation]) -> str:
    return observation_time(obs)


def _after(a: Optional[Observation], b: Optional[Observation]) -> bool:
    if a is None or b is None:
        return True
    ta, tb = _time(a), _time(b)
    if ta and tb:
        try:
            parsed_a = datetime.fromisoformat(ta.replace('Z', '+00:00'))
            parsed_b = datetime.fromisoformat(tb.replace('Z', '+00:00'))
            return parsed_b >= parsed_a
        except (TypeError, ValueError):
            return tb >= ta
    if a.seq is not None and b.seq is not None:
        return b.seq >= a.seq
    return True


def _select(candidates: List[Observation], select: str, start: Optional[Observation], refs: Dict[str, Observation]) -> Optional[Observation]:
    if not candidates:
        return None
    ordered = sorted(candidates, key=lambda o: (_time(o) or '9999', o.seq if o.seq is not None else 10**18, o.id))
    if select.startswith('first_after_ref:'):
        ref = refs.get(select.split(':', 1)[1])
        after = [o for o in ordered if _after(ref, o)]
        return after[0] if after else None
    if select.startswith('next_after_ref:'):
        ref = refs.get(select.split(':', 1)[1])
        after = [
            o for o in ordered
            if o is not ref and (ref is None or o.id != ref.id) and _after(ref, o)
        ]
        return after[0] if after else None
    if select.startswith('first_same_ref:'):
        # first_same_ref:<ref>:<candidate_field>[:<reference_field>]
        # binds a milestone to an identifier already selected by the rule.
        parts = select.split(':')
        if len(parts) not in (3, 4):
            return None
        reference = refs.get(parts[1])
        if reference is None:
            return None
        candidate_field = parts[2]
        reference_field = parts[3] if len(parts) == 4 else candidate_field
        expected = _field(reference, reference_field)
        if expected is None:
            return None
        same = [o for o in ordered if _field(o, candidate_field) == expected]
        return same[0] if same else None
    if select in ('first_after_start', 'last_after_start'):
        after = [o for o in ordered if _after(start, o)]
        if not after:
            return None
        return after[0] if select == 'first_after_start' else after[-1]
    if select == 'last':
        return ordered[-1]
    return ordered[0]


def _candidate_list(observations: Sequence[Observation], spec: Dict[str, Any]) -> List[Observation]:
    return [o for o in observations if matches(o, spec)]


def _extract_values(obs: Observation, spec: Dict[str, Any]) -> Dict[str, Any]:
    vals = {}
    for name in spec.get('extract') or []:
        vals[name] = _field(obs, name)
    return vals


def _ref_value(refs: Dict[str, Observation], extracted: Dict[str, Dict[str, Any]], token: str):
    if '.' not in token:
        return None
    r, fld = token.split('.', 1)
    if r in extracted and fld in extracted[r] and extracted[r][fld] is not None:
        return extracted[r][fld]
    o = refs.get(r)
    return _field(o, fld) if o else None

_REL = re.compile(r'^\s*([A-Za-z0-9_]+\.[A-Za-z0-9_.-]+)\s*(==|!=)\s*([A-Za-z0-9_]+\.[A-Za-z0-9_.-]+)\s*$')


def _relation_ok(expr: str, refs: Dict[str, Observation], extracted: Dict[str, Dict[str, Any]]) -> bool:
    m = _REL.match(expr or '')
    if not m:
        return False
    a, op, b = m.groups()
    av, bv = _ref_value(refs, extracted, a), _ref_value(refs, extracted, b)
    if av is None or bv is None:
        return False
    return (av == bv) if op == '==' else (av != bv)


def _conversation_prompt_edit_transition(before: Observation, after: Observation) -> bool:
    """Identify an active-branch user-message replacement, not an append."""
    left = _field(before, 'message_sequence')
    right = _field(after, 'message_sequence')
    if not isinstance(left, list) or not isinstance(right, list) or not left or not right:
        return False
    if _field(before, 'current_node') == _field(after, 'current_node'):
        return False

    common = 0
    for old, new in zip(left, right):
        if not isinstance(old, dict) or not isinstance(new, dict):
            break
        old_key = (old.get('message_id'), old.get('role'), old.get('content_sha256'))
        new_key = (new.get('message_id'), new.get('role'), new.get('content_sha256'))
        if old_key != new_key:
            break
        common += 1

    # An after snapshot that merely adds messages is normal prompting, not edit.
    if common == 0 or common >= min(len(left), len(right)):
        return False
    old_tail, new_tail = left[common:], right[common:]
    if not old_tail or not new_tail:
        return False
    if old_tail[0].get('role') != 'user' or new_tail[0].get('role') != 'user':
        return False
    old_user = (old_tail[0].get('message_id'), old_tail[0].get('content_sha256'))
    new_user = (new_tail[0].get('message_id'), new_tail[0].get('content_sha256'))
    if old_user == new_user:
        return False
    return any(
        isinstance(message, dict) and message.get('role') == 'assistant'
        for message in new_tail[1:]
    )


def _memory_source_checksum_changed(before: Observation, after: Observation) -> bool:
    old = _field(before, 'source_checksum')
    new = _field(after, 'source_checksum')
    return bool(old and new and old != new)


def _association_ok(item: Dict[str, Any], refs: Dict[str, Observation], extracted: Dict[str, Dict[str, Any]], policies: Dict[str, Dict[str, Any]]) -> bool:
    left, right = refs.get(item.get('left', '')), refs.get(item.get('right', ''))
    if left is None or right is None:
        return not item.get('required', False)
    if item.get('relation'):
        return _relation_ok(item['relation'], refs, extracted)
    policy = item.get('policy')
    p = policies.get(policy, {})
    fields = p.get('fields') or []
    if policy == 'history_from_visit' and len(fields) >= 2:
        return _field(right, fields[1]) == _field(left, fields[0])
    if policy == 'same_download_url':
        return _field(left, 'url') == _field(right, 'url')
    if policy == 'ordered_after':
        return _after(left, right)
    if policy == 'conversation_prompt_edit_transition':
        return _conversation_prompt_edit_transition(left, right)
    if policy == 'memory_source_checksum_changed':
        return _memory_source_checksum_changed(left, right)
    for fld in fields:
        lv = _field(left, fld)
        rv = _field(right, fld)
        if lv is not None and rv is not None:
            return lv == rv
    return not item.get('required', False)


def _when_ok(when: Dict[str, Any], refs: Dict[str, Observation], extracted: Dict[str, Dict[str, Any]]) -> bool:
    all_refs = when.get('all_refs') or []
    any_refs = when.get('any_refs') or []
    if all_refs and not all(r in refs for r in all_refs):
        return False
    if any_refs and not any(r in refs for r in any_refs):
        return False
    for rel in when.get('relations') or []:
        if not _relation_ok(rel, refs, extracted):
            return False
    field = when.get('field')
    if field:
        value = None
        if '.' in field:
            first, rest = field.split('.', 1)
            if first in refs:
                value = _field(refs[first], rest)
            elif first in extracted:
                value = extracted[first].get(rest)
        if value is None:
            # Search extracted values/refs when a rule names only the extracted field.
            for r, vals in extracted.items():
                if field in vals:
                    value = vals[field]
                    break
            if value is None:
                for o in refs.values():
                    value = _field(o, field)
                    if value is not None:
                        break
        cond = {k: v for k, v in when.items() if k in ('eq', 'neq', 'exists', 'in', 'regex', 'prefix', 'suffix', 'contains')}
        if cond and not _match_value(value, cond):
            return False
    if 'any' in when:
        if not any(_when_ok(x, refs, extracted) for x in when['any']):
            return False
    if 'all' in when:
        if not all(_when_ok(x, refs, extracted) for x in when['all']):
            return False
    return True


def _confidence(rule: Dict[str, Any], refs: Dict[str, Observation], extracted: Dict[str, Dict[str, Any]]) -> str:
    c = rule.get('confidence', SUSPECTED)
    if isinstance(c, str):
        return c
    for item in c or []:
        if _when_ok(item.get('when') or {}, refs, extracted):
            return item.get('level', SUSPECTED)
    return SUSPECTED


def _excluded(rule: Dict[str, Any], refs: Dict[str, Observation], extracted: Dict[str, Dict[str, Any]]) -> bool:
    for item in rule.get('exclude') or []:
        ref = item.get('ref')
        if ref and ref not in refs:
            continue
        if _when_ok(item.get('when') or {}, refs, extracted):
            return True
    return False


def _bind_event_rule(rule: Dict[str, Any], observations: Sequence[Observation],
                     window_policy: Optional[Dict[str, Any]] = None) -> List[RuleBinding]:
    start_spec = rule.get('start')
    starts = _candidate_list(observations, start_spec) if start_spec else [None]
    if start_spec and start_spec.get('required', True) and not starts:
        return []
    if start_spec and not start_spec.get('required', True) and not starts:
        starts = [None]

    policy = dict(window_policy or {})
    policy.update(rule.get('evidence_window') or {})
    use_history_window = bool(
        policy.get('enabled', True)
        and start_spec
        and start_spec.get('artifact') == 'history'
        and policy.get('mode', 'next_matching_history') == 'next_matching_history'
    )
    windows_by_anchor: Dict[int, EvidenceWindow] = {}
    if use_history_window:
        for window in build_history_windows(
            observations,
            lambda o: matches(o, start_spec),
            scope=lambda o: str(_field(o, '_source_scope') or ''),
        ):
            windows_by_anchor[id(window.anchor)] = window

    out = []
    for start in starts:
        window = windows_by_anchor.get(id(start)) if start is not None else None
        start_scope = _field(start, '_source_scope') if start is not None else None

        def same_scope(candidate: Observation) -> bool:
            if not start_scope:
                return True
            return _field(candidate, '_source_scope') == start_scope

        refs: Dict[str, Observation] = {}
        extracted: Dict[str, Dict[str, Any]] = {}
        if start_spec and start is not None:
            refs[start_spec['ref']] = start
            extracted[start_spec['ref']] = _extract_values(start, start_spec)
        ok = True
        cursor = start
        for spec in rule.get('milestones') or []:
            cand = [o for o in _candidate_list(observations, spec) if same_scope(o)]
            if cursor is not None:
                cand = [o for o in cand if _after(cursor, o)]
            if window is not None:
                cand = [o for o in cand if window_contains(window, o)]
            chosen = _select(cand, spec.get('select', 'first_after_start'), start, refs)
            if chosen is None:
                if spec.get('required', False):
                    ok = False
                    break
                continue
            refs[spec['ref']] = chosen
            extracted[spec['ref']] = _extract_values(chosen, spec)
            cursor = chosen
        if not ok:
            continue
        end_spec = rule.get('end')
        if end_spec:
            cand = [o for o in _candidate_list(observations, end_spec) if same_scope(o)]
            if start is not None:
                cand = [o for o in cand if _after(start, o)]
            if window is not None:
                cand = [o for o in cand if window_contains(window, o)]
            chosen = _select(cand, end_spec.get('select', 'first_after_start'), start, refs)
            if chosen is None and end_spec.get('required', False):
                continue
            if chosen is not None:
                refs[end_spec['ref']] = chosen
                extracted[end_spec['ref']] = _extract_values(chosen, end_spec)
        out.append(RuleBinding(refs, extracted, window))
    return out


_LEVEL_ORDER = ('extended', 'standard', 'minimum')


def _matched_rule_level(rule: Dict[str, Any], refs: Dict[str, Observation],
                        extracted: Dict[str, Dict[str, Any]], rules_mod: Any):
    definitions = rule.get('levels')
    if definitions is None:
        definitions = getattr(rules_mod, 'RULE_LEVELS', {}).get(rule.get('rule_id'))
    if not definitions:
        return rule.get('rule_id', ''), ''

    for level in _LEVEL_ORDER:
        item = definitions.get(level)
        if not item or item.get('status') in ('unavailable', 'unsupported', 'unverified'):
            continue
        if _when_ok(item.get('when') or {}, refs, extracted):
            return item.get('rule_id') or rule.get('rule_id', ''), level
    return None, None


def _run_inference_rules(observations: Sequence[Observation], rules_mod: Any, ids: Ids) -> List[Inference]:
    out: List[Inference] = []
    policies = getattr(rules_mod, 'ASSOCIATION_POLICIES', {})
    window_policy = getattr(rules_mod, 'HISTORY_WINDOW_POLICY', {})
    for rule in getattr(rules_mod, 'INFERENCE_RULES', []):
        mode = rule.get('mode', 'event_range')
        if mode != 'event_range':
            continue
        for binding in _bind_event_rule(rule, observations, window_policy):
            refs, extracted = binding.refs, binding.extracted
            if any(not _association_ok(a, refs, extracted, policies) for a in rule.get('association') or []):
                continue
            if _excluded(rule, refs, extracted):
                continue
            matched_rule_id, matched_level = _matched_rule_level(rule, refs, extracted, rules_mod)
            if matched_rule_id is None:
                continue
            start_ref = (rule.get('start') or {}).get('ref')
            end_ref = (rule.get('end') or {}).get('ref')
            start_obs = refs.get(start_ref) if start_ref else None
            end_obs = refs.get(end_ref) if end_ref else None
            if binding.window is not None:
                when_from = binding.window.when_from
                when_to = binding.window.when_to
                when_basis = (
                    'History candidate evidence window: anchor to next matching '
                    'History anchor; this is not an action duration'
                )
            else:
                when_from = _time(start_obs)
                when_to = _time(end_obs)
                when_basis = 'start/end anchors from normalized observations'
            supported = []
            for o in refs.values():
                if o.id and o.id not in supported:
                    supported.append(o.id)
            detail = {
                'base_rule_id': rule.get('rule_id', ''),
                'matched_rule_level': matched_level,
                'refs': {r: {'observation_id': o.id, **(extracted.get(r) or {})} for r, o in refs.items()},
            }
            if binding.window is not None:
                detail['evidence_window'] = {
                    'anchor_observation_id': binding.window.anchor.id,
                    'next_anchor_observation_id': (
                        binding.window.next_anchor.id if binding.window.next_anchor else ''
                    ),
                    'open_ended': binding.window.next_anchor is None,
                }
            detail.update(rule.get('output_extra') or {})
            note = '; '.join(rule.get('notes') or [])
            out.append(Inference(
                id=ids.next('inf'),
                behavior_id=rule.get('behavior_id', ''),
                behavior_name=rule.get('behavior_name', ''),
                question=rule.get('question') or (
                    f'{rule.get("behavior_name", "")} 행위가 확인되는가?'
                    if rule.get('behavior_name') else ''
                ),
                rule_id=matched_rule_id,
                confidence=_confidence(rule, refs, extracted),
                when_from=when_from,
                when_to=when_to,
                span_sec=span_seconds(when_from, when_to),
                when_basis=when_basis,
                actor=next((o.actor for o in refs.values() if o.actor), ''),
                supported_by=supported,
                alternatives=rule.get('alternatives') or [],
                confidence_basis={
                    'rule': matched_rule_id,
                    'base_rule': rule.get('rule_id', ''),
                    'rule_level': matched_level,
                    'refs': list(refs),
                },
                note=note,
                detail=detail,
            ))
    return out


def _supporting_extract_values(obs: Observation, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Keep both declared extracts and matched fields for analyst-facing evidence."""
    fields = list(spec.get('extract') or [])
    fields.extend((spec.get('match') or spec.get('requires') or {}).keys())
    values: Dict[str, Any] = {}
    for field in fields:
        if field == 'same_conversation_id_membership' or field in values:
            continue
        value = _field(obs, str(field))
        if value is not None:
            values[str(field)] = value
    return values


def _supporting_inference(rule: Dict[str, Any], refs: Dict[str, Observation],
                          extracted: Dict[str, Dict[str, Any]], ids: Ids) -> Inference:
    times = [_time(obs) for obs in refs.values() if _time(obs)]
    when_from = min(times) if times else ''
    when_to = max(times) if len(times) > 1 else ''
    rule_id = str(rule.get('rule_id') or '')
    reason = str(rule.get('candidate_reason') or '')
    detail = {
        'base_rule_id': rule_id,
        'matched_rule_level': 'supporting',
        'support_only': True,
        'candidate_reason': reason,
        'refs': {
            ref: {'observation_id': obs.id, **(extracted.get(ref) or {})}
            for ref, obs in refs.items()
        },
    }
    detail.update(rule.get('output_extra') or {})
    notes = list(rule.get('notes') or [])
    notes.append(
        '부분 흔적만으로 생성된 보조 후보이며, 이 결과만으로 해당 사용자 행위를 확정하지 않는다.'
    )
    supported_by = []
    for obs in refs.values():
        if obs.id and obs.id not in supported_by:
            supported_by.append(obs.id)
    return Inference(
        id=ids.next('inf'),
        behavior_id=str(rule.get('behavior_id') or ''),
        behavior_name=str(rule.get('behavior_name') or ''),
        question=rule.get('question') or (
            f'{rule.get("behavior_name", "")} 행위의 부분 정황이 존재하는가?'
            if rule.get('behavior_name') else ''
        ),
        rule_id=rule_id,
        confidence=str(rule.get('confidence') or 'undecided'),
        when_from=when_from,
        when_to=when_to,
        span_sec=span_seconds(when_from, when_to),
        when_basis='supporting artifact timestamp; candidate only',
        actor=next((obs.actor for obs in refs.values() if obs.actor), ''),
        supported_by=supported_by,
        alternatives=list(rule.get('alternatives') or []),
        confidence_basis={
            'rule': rule_id,
            'rule_level': 'supporting',
            'support_only': True,
            'refs': list(refs),
        },
        note='; '.join(notes),
        detail=detail,
    )


def _run_supporting_rules(observations: Sequence[Observation], rules_mod: Any,
                          ids: Ids, strict_inferences: Sequence[Inference]) \
        -> List[Inference]:
    """Emit explicitly non-direct candidates from curated partial evidence.

    A supporting candidate never replaces a minimum rule. Evidence already used
    by a strict inference for the same behavior is skipped to avoid duplicate
    direct/candidate rows for one recovered fact.
    """
    output: List[Inference] = []
    used_by_behavior: Dict[str, set[str]] = {}
    for inference in strict_inferences:
        used_by_behavior.setdefault(inference.behavior_id, set()).update(
            str(item) for item in inference.supported_by if item
        )

    for rule in getattr(rules_mod, 'SUPPORTING_INFERENCE_RULES', []):
        behavior_id = str(rule.get('behavior_id') or '')
        already_used = used_by_behavior.get(behavior_id, set())
        aggregate = rule.get('aggregate') or {}

        if aggregate:
            spec = rule.get('match') or {}
            candidates = [
                obs for obs in _candidate_list(observations, spec)
                if obs.id not in already_used
            ]
            groups: Dict[tuple[Any, ...], List[Observation]] = {}
            group_fields = list(aggregate.get('group_by') or ['_source_scope', 'source_path'])
            for obs in candidates:
                key = tuple(_field(obs, field) for field in group_fields)
                groups.setdefault(key, []).append(obs)
            distinct_by = str(aggregate.get('distinct_by') or '')
            minimum = int(aggregate.get('minimum_distinct') or 2)
            for members in groups.values():
                distinct: Dict[Any, Observation] = {}
                for obs in members:
                    value = _field(obs, distinct_by) if distinct_by else obs.id
                    if value not in (None, ''):
                        distinct.setdefault(value, obs)
                if len(distinct) < minimum:
                    continue
                refs: Dict[str, Observation] = {}
                extracted: Dict[str, Dict[str, Any]] = {}
                base_ref = str(spec.get('ref') or 'support')
                for index, obs in enumerate(distinct.values(), 1):
                    ref = f'{base_ref}_{index}'
                    refs[ref] = obs
                    extracted[ref] = _supporting_extract_values(obs, spec)
                output.append(_supporting_inference(rule, refs, extracted, ids))
            continue

        specs = list(rule.get('match_any') or [])
        if not specs and rule.get('match'):
            specs = [rule['match']]
        seen: set[tuple[Any, ...]] = set()
        dedupe_fields = list(rule.get('dedupe_by') or [])
        for index, spec in enumerate(specs, 1):
            ref = str(spec.get('ref') or f'support_{index}')
            for obs in _candidate_list(observations, spec):
                if obs.id in already_used:
                    continue
                values = tuple(_field(obs, field) for field in dedupe_fields)
                if dedupe_fields and any(value not in (None, '') for value in values):
                    key = (ref, *values)
                else:
                    key = (ref, obs.id)
                if key in seen:
                    continue
                seen.add(key)
                refs = {ref: obs}
                extracted = {ref: _supporting_extract_values(obs, spec)}
                output.append(_supporting_inference(rule, refs, extracted, ids))
    return output


def _simple_context_bind(rule: Dict[str, Any], observations: Sequence[Observation], policies: Dict[str, Dict[str, Any]]):
    specs = rule.get('match')
    if specs:
        refs = {}
        extracted = {}
        for spec in specs:
            cand = _candidate_list(observations, spec)
            if not cand:
                if spec.get('required', False):
                    return []
                continue
            o = cand[0]
            refs[spec.get('ref', f'r{len(refs)}')] = o
            extracted[spec.get('ref', f'r{len(refs)}')] = _extract_values(o, spec)
        if any(not _association_ok(a, refs, extracted, policies) for a in rule.get('association') or []):
            return []
        return [(refs, extracted)] if refs else []
    specs = rule.get('match_any') or []
    out = []
    for spec in specs:
        for o in _candidate_list(observations, spec):
            ref = spec.get('ref', 'match')
            out.append(({ref: o}, {ref: _extract_values(o, spec)}))
    return out


def _run_context_rules(observations: Sequence[Observation], rules_mod: Any, ids: Ids) -> List[ContextRecord]:
    out: List[ContextRecord] = []
    policies = getattr(rules_mod, 'ASSOCIATION_POLICIES', {})
    for rule in getattr(rules_mod, 'CONTEXT_RULES', []):
        for refs, extracted in _simple_context_bind(rule, observations, policies):
            times = [_time(o) for o in refs.values() if _time(o)]
            data = {}
            wanted = rule.get('extract') or []
            for name in wanted:
                for o in refs.values():
                    v = _field(o, name)
                    if v is not None:
                        data[name] = v
                        break
            data['refs'] = {r: {'observation_id': o.id, **(extracted.get(r) or {})} for r, o in refs.items()}
            out.append(ContextRecord(
                id=ids.next('ctx'),
                context_type=rule.get('context_type', ''),
                label=rule.get('label', ''),
                rule_id=rule.get('rule_id', ''),
                supported_by=[o.id for o in refs.values() if o.id],
                when_from=min(times) if times else '',
                when_to=max(times) if times else '',
                confidence=rule.get('confidence', '') if isinstance(rule.get('confidence'), str) else '',
                data=data,
                note='; '.join(rule.get('notes') or []),
            ))
    return out


def _state_role_match(obs: Observation, prefs: Sequence[str]) -> bool:
    if not prefs:
        return True
    role = str(obs.fact.get('state_role') or '')
    source_class = str(obs.fact.get('source_class') or '')
    ek = obs.evidence_kind
    tokens = {role, source_class, ek, f'{obs.source_artifact}_{ek}', f'{obs.source_artifact}_{role}'}
    return any(p in tokens for p in prefs)


def _run_state_transition_rules(observations: Sequence[Observation], rules_mod: Any, ids: Ids) -> List[ContextRecord]:
    out: List[ContextRecord] = []
    policies = getattr(rules_mod, 'ASSOCIATION_POLICIES', {})
    for rule in getattr(rules_mod, 'STATE_TRANSITION_RULES', []):
        prev_spec, cur_spec = rule.get('previous') or {}, rule.get('current') or {}
        prev = [o for o in observations if matches(o, prev_spec) and _state_role_match(o, prev_spec.get('source_preferences') or [])]
        cur = [o for o in observations if matches(o, cur_spec) and _state_role_match(o, cur_spec.get('source_preferences') or [])]
        for a in prev:
            for b in cur:
                refs = {prev_spec.get('ref', 'previous'): a, cur_spec.get('ref', 'current'): b}
                ext: Dict[str, Dict[str, Any]] = {}
                if any(not _association_ok(x, refs, ext, policies) for x in rule.get('association') or []):
                    continue
                # Special transition membership condition.
                req = cur_spec.get('requires') or {}
                if 'same_conversation_id_membership' in req:
                    cid_a = _field(a, 'conversation_id')
                    cid_b = _field(b, 'conversation_id')
                    if cid_a is None or cid_b is None or cid_a != cid_b:
                        continue
                    ma, mb = _field(a, 'membership'), _field(b, 'membership')
                    wanted = req['same_conversation_id_membership'].get('eq')
                    relation_value = (ma == mb)
                    if relation_value != wanted:
                        continue
                out.append(ContextRecord(
                    id=ids.next('ctx'),
                    context_type=rule.get('context_type', 'state_transition'),
                    label=rule.get('label', ''),
                    rule_id=rule.get('rule_id', ''),
                    supported_by=[a.id, b.id],
                    when_from=_time(a),
                    when_to=_time(b),
                    confidence=rule.get('confidence', ''),
                    data={
                        'previous': a.fact,
                        'current': b.fact,
                        'higher_level_interpretation': rule.get('higher_level_interpretation') or {},
                    },
                    note='; '.join(rule.get('notes') or []),
                ))
    return out


def _same_scope(a: Inference, b: Inference, scope: str) -> bool:
    if not scope:
        return True
    # Conservative generic overlap: shared evidence or overlapping extracted IDs.
    if set(a.supported_by) & set(b.supported_by):
        return True
    ad = str(a.detail)
    bd = str(b.detail)
    return any(x and x in bd for x in re.findall(r'[A-Za-z0-9_-]{8,}', ad)[:20])


def _apply_precedence(infs: List[Inference], rules_mod: Any) -> List[Inference]:
    precedence = list(getattr(rules_mod, 'RULE_PRECEDENCE', []))
    # Inline suppresses are also allowed.
    rule_defs = {r.get('rule_id'): r for r in getattr(rules_mod, 'INFERENCE_RULES', [])}
    for rid, r in rule_defs.items():
        for s in r.get('suppresses') or []:
            precedence.append({'higher': rid, 'lower': s.get('rule_id'), 'scope': s.get('scope', '')})
    remove = set()
    for p in precedence:
        highs = [x for x in infs if x.rule_id == p.get('higher') or x.detail.get('base_rule_id') == p.get('higher')]
        lows = [x for x in infs if x.rule_id == p.get('lower') or x.detail.get('base_rule_id') == p.get('lower')]
        for h in highs:
            for l in lows:
                if _same_scope(h, l, p.get('scope', '')):
                    remove.add(l.id)
    return [x for x in infs if x.id not in remove]


def run_all(observations: Iterable[Observation], rules_mod: Any, ids: Optional[Ids] = None):
    ids = ids or Ids()
    obs = list(observations)
    infs = _run_inference_rules(obs, rules_mod, ids)
    infs = _apply_precedence(infs, rules_mod)
    infs.extend(_run_supporting_rules(obs, rules_mod, ids, infs))
    contexts = _run_context_rules(obs, rules_mod, ids)
    contexts.extend(_run_state_transition_rules(obs, rules_mod, ids))
    return infs, contexts
