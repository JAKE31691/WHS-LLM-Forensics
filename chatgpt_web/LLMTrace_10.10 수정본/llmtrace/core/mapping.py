from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

from .schema import Ids, Observation, RawRecord


def _get(obj: Any, dotted: str):
    cur: Any = obj
    for p in dotted.split('.'):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _match_value(value: Any, cond: Any) -> bool:
    if not isinstance(cond, dict):
        return value == cond
    for op, wanted in cond.items():
        if op == 'eq' and value != wanted:
            return False
        if op == 'neq' and value == wanted:
            return False
        if op == 'exists' and ((value is not None) != bool(wanted)):
            return False
        if op == 'in' and value not in wanted:
            return False
        if op == 'regex' and not re.search(wanted, '' if value is None else str(value)):
            return False
        if op == 'prefix' and not str(value or '').startswith(str(wanted)):
            return False
        if op == 'suffix' and not str(value or '').endswith(str(wanted)):
            return False
        if op == 'contains' and str(wanted) not in str(value or ''):
            return False
    return True


def _mapped_value(record: Any, source: Any):
    if callable(source):
        return source(record)
    if isinstance(source, str):
        return _get(record, source)
    return source


def apply_mappings(records: Iterable[RawRecord], mappings: List[Dict[str, Any]], ids: Ids,
                   profile=None) -> List[Observation]:
    """Second-stage surface interpretation: RawRecord -> Observation.

    Unlike v0.2, unmatched raw records are NOT silently promoted to
    Observations. The first stage is evidence recovery; the second stage must
    explicitly say what is meaningful for this surface.

    Mapping shape (minimal example)::

        {
          'artifact': 'history',
          'dataset': 'urls',
          'when': {'url': {'contains': 'chatgpt.com'}},
          'fact_type': 'url_record',
          'evidence_kind': 'event',
          'fact': {'url': 'url', 'title': 'title'}
        }
    """
    out: List[Observation] = []
    for raw in records:
        record = raw.record
        for m in mappings:
            if m.get('artifact') and m['artifact'] != raw.artifact:
                continue
            if m.get('dataset') and m['dataset'] != raw.dataset:
                continue
            conds = m.get('when') or {}
            if not all(_match_value(_get(record, k), v) for k, v in conds.items()):
                continue

            # A mapping may delegate complex surface semantics to a function.
            factory = m.get('factory')
            if callable(factory):
                made = factory(raw, ids, profile)
                if made is None:
                    continue
                if isinstance(made, Observation):
                    out.append(made)
                else:
                    out.extend(made)
                if not m.get('continue', False):
                    break
                continue

            fact: Dict[str, Any] = {}
            for target, source in (m.get('fact') or {}).items():
                fact[target] = _mapped_value(record, source)

            timestamps: Dict[str, str] = {}
            for target, source in (m.get('timestamps') or {}).items():
                v = _mapped_value(record, source)
                if v not in (None, ''):
                    timestamps[target] = str(v)

            seq = None
            if 'seq' in m:
                seq = _mapped_value(record, m.get('seq'))

            out.append(Observation(
                id=ids.next('obs'),
                source_artifact=raw.artifact,
                source_path=raw.source_path,
                source_locator=raw.source_locator,
                fact_type=str(m.get('fact_type') or ''),
                fact=fact,
                evidence_kind=str(m.get('evidence_kind') or 'context'),
                parser=raw.parser,
                parser_status='ok',
                timestamps=timestamps,
                tz_source=str(m.get('tz_source') or 'UTC'),
                seq=seq,
                env_profile=getattr(getattr(profile, 'profile', None), 'PROFILE_ID', '') if profile else '',
                parsed_record=record,
            ))
            if not m.get('continue', False):
                break
    return out


def process_surface(records: Iterable[RawRecord], profile, ids: Ids) -> List[Observation]:
    """Run a surface's second-stage processor.

    Preferred extension point:
        profiles/<surface>/mappings.py::process(records, ids, profile)

    Declarative fallback:
        RAW_MAPPINGS (FACT_MAPPINGS is accepted as a compatibility alias).
    """
    mod = profile.mappings
    if mod is not None and hasattr(mod, 'process'):
        return list(mod.process(list(records), ids, profile))
    mappings = []
    if mod is not None:
        mappings = getattr(mod, 'RAW_MAPPINGS', None)
        if mappings is None:
            mappings = getattr(mod, 'FACT_MAPPINGS', [])
    return apply_mappings(records, list(mappings or []), ids, profile)
