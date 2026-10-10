"""Conservative Claude Desktop surface processing.

No Claude behavior semantics are asserted until environment experiments supply
validated rules.  Native records are still promoted to thin Observations so
the report's source viewer remains useful and fully traceable.
"""

from __future__ import annotations

from typing import Any, Iterable

from llmtrace.core.schema import Ids, Observation, RawRecord


def process(records: Iterable[RawRecord], ids: Ids, profile) -> list[Observation]:
    out = []
    for raw in records:
        out.append(Observation(
            id=ids.next('obs'),
            source_artifact=raw.artifact,
            source_path=raw.source_path,
            source_locator=raw.source_locator,
            fact_type='',
            fact={'dataset': raw.dataset},
            evidence_kind='context',
            parser=raw.parser,
            parser_status='ok',
            seq=raw.ordinal,
            env_profile='claude_app',
            parsed_record=raw.record,
        ))
    return out


RAW_MAPPINGS: list[dict[str, Any]] = []
