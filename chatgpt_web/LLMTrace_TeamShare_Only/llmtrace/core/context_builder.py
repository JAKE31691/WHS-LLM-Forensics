from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List

from .engine import matches
from .schema import Observation


def build_sections(observations: Iterable[Observation], context_mod: Any) -> List[Dict[str, Any]]:
    obs = list(observations)
    defs = getattr(context_mod, 'CONTEXT_SECTIONS', []) if context_mod else []
    if not defs:
        defs = [
            {'id': 'state', 'title': 'State / Residual', 'evidence_kinds': ['state', 'residual']},
            {'id': 'events', 'title': 'Event Evidence', 'evidence_kinds': ['event']},
        ]
    sections = []
    for d in defs:
        rows = []
        for o in obs:
            if d.get('evidence_kinds') and o.evidence_kind not in d['evidence_kinds']:
                continue
            if d.get('artifacts') and o.source_artifact not in d['artifacts']:
                continue
            if d.get('fact_types') and o.fact_type not in d['fact_types']:
                continue
            selector = d.get('match')
            if selector and not matches(o, {'match': selector}):
                continue
            rows.append(o)
        sections.append({'id': d.get('id', ''), 'title': d.get('title', ''), 'description': d.get('description', ''), 'rows': rows})
    return sections


def entity_index(observations: Iterable[Observation]) -> Dict[str, Dict[str, List[str]]]:
    """Generic relation index for HTML navigation, not an inference engine."""
    keys = (
        'account_id', 'user_id', 'organization_id', 'workspace_account_id', 'client_id',
        'conversation_id', 'message_id', 'turn_id', 'turn_exchange_id', 'request_id',
        'file_id', 'library_file_id', 'library_directory_id',
    )
    out: Dict[str, Dict[str, List[str]]] = {k: defaultdict(list) for k in keys}
    for o in observations:
        for k in keys:
            v = o.fact.get(k)
            if v is not None and o.id:
                out[k][str(v)].append(o.id)
    return {k: dict(v) for k, v in out.items()}
