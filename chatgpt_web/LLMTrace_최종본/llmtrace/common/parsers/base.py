from __future__ import annotations

from typing import Callable, Dict, Iterable, List

from llmtrace.core.schema import RawRecord
from llmtrace.core.schema import Problem

# First-stage parsers return RawRecord, never Observation.
# Their job ends at recovering the artifact's own logical records.
PARSERS: Dict[str, Callable[..., Iterable[RawRecord]]] = {}


def register(name: str):
    def deco(fn):
        PARSERS[name] = fn
        return fn
    return deco


def get(name: str):
    return PARSERS[name]


def available() -> List[str]:
    return sorted(PARSERS)


def report_problem(step, *, parser: str, artifact: str, source_path: str,
                   message: str, code: str = 'record_parse_failed') -> None:
    """Report a recoverable per-file problem through the parser runtime step."""
    sink = step.get('_problem_sink')
    ids = step.get('_ids')
    if sink is None or ids is None:
        return
    sink.append(Problem(
        id=ids.next('problem'), stage='parse', severity='error', code=code,
        message=message, profile=str(step.get('_profile_name') or ''),
        artifact=artifact, parser=parser, source_path=source_path,
    ))
