from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

CONFIRMED = 'confirmed'
SUSPECTED = 'suspected'
PRESUMED = 'presumed'
UNDECIDED = 'undecided'
CONFIDENCE_VALUES = (CONFIRMED, SUSPECTED, PRESUMED, UNDECIDED)
SCHEMA_VERSION = '0.4'


@dataclass
class RawRecord:
    """One logical record recovered by a first-stage artifact parser.

    IMPORTANT CONTRACT
    ------------------
    ``record`` contains ONLY fields/values recovered from the source artifact's
    own logical record.  Service/surface semantics must never be injected here.

    The remaining attributes are transport/provenance metadata kept OUTSIDE the
    raw record.  When the first-stage raw output is written, only ``record`` is
    written to the artifact JSON/JSONL; provenance is written separately to the
    raw manifest.
    """

    artifact: str
    dataset: str
    record: Any
    source_path: str = ''
    source_locator: str = ''
    parser: str = ''
    ordinal: Optional[int] = None
    output_format: str = 'jsonl'  # jsonl | json

    def raw_value(self) -> Any:
        """Return the unaugmented logical record for first-stage output."""
        return self.record

    def provenance_dict(self) -> Dict[str, Any]:
        """Return metadata separately from the raw logical record."""
        return {
            'artifact': self.artifact,
            'dataset': self.dataset,
            'source_path': self.source_path,
            'source_locator': self.source_locator,
            'parser': self.parser,
            'ordinal': self.ordinal,
            'output_format': self.output_format,
        }


@dataclass
class Observation:
    """Second-stage, surface-aware forensic observation."""

    id: str
    source_artifact: str
    source_path: str
    source_locator: str
    fact_type: str = ''
    fact: Dict[str, Any] = field(default_factory=dict)
    evidence_kind: str = 'event'  # event | state | residual | context
    parser: str = ''
    parser_status: str = 'ok'
    parser_note: str = ''
    timestamps: Dict[str, str] = field(default_factory=dict)
    tz_source: str = 'UTC'
    seq: Optional[int] = None
    actor: str = ''
    env_profile: str = ''
    confidence: str = CONFIRMED
    confidence_basis: Dict[str, Any] = field(default_factory=dict)
    # The recovered source record is retained for analyst review.  Surface
    # mappings may add normalized fields to ``fact`` but must not rewrite this
    # value or use it to hide the parser's original output.
    parsed_record: Any = None

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['schema_version'] = SCHEMA_VERSION
        d['parser'] = self.parser or self.source_artifact
        # Compatibility with the previous Observation contract: expose ts_* at top level.
        d.update(self.timestamps)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'Observation':
        known_ts = {k: v for k, v in d.items() if k.startswith('ts_') and v not in (None, '')}
        fact = d.get('fact') or d.get('data') or {}
        return cls(
            id=str(d.get('id') or d.get('observation_id') or ''),
            source_artifact=str(d.get('source_artifact') or d.get('artifact') or ''),
            source_path=str(d.get('source_path') or ''),
            source_locator=str(d.get('source_locator') or ''),
            fact_type=str(d.get('fact_type') or ''),
            fact=fact if isinstance(fact, dict) else {'value': fact},
            evidence_kind=str(d.get('evidence_kind') or 'event'),
            parser=str(d.get('parser') or ''),
            parser_status=str(d.get('parser_status') or 'ok'),
            parser_note=str(d.get('parser_note') or ''),
            timestamps=d.get('timestamps') or known_ts,
            tz_source=str(d.get('tz_source') or 'UTC'),
            seq=d.get('seq'),
            actor=str(d.get('actor') or ''),
            env_profile=str(d.get('env_profile') or ''),
            confidence=str(d.get('confidence') or CONFIRMED),
            confidence_basis=d.get('confidence_basis') or {},
            parsed_record=d.get('parsed_record', d.get('raw_record')),
        )


@dataclass
class Inference:
    id: str
    behavior_id: str
    behavior_name: str
    rule_id: str
    confidence: str = SUSPECTED
    when_from: str = ''
    when_to: str = ''
    span_sec: Optional[float] = None
    when_basis: str = ''
    actor: str = ''
    supported_by: List[str] = field(default_factory=list)
    contradicted_by: List[str] = field(default_factory=list)
    alternatives: List[str] = field(default_factory=list)
    confidence_basis: Dict[str, Any] = field(default_factory=dict)
    note: str = ''
    detail: Dict[str, Any] = field(default_factory=dict)
    question: str = ''

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['schema_version'] = SCHEMA_VERSION
        return d


@dataclass
class ContextRecord:
    id: str
    context_type: str
    label: str
    rule_id: str
    supported_by: List[str] = field(default_factory=list)
    when_from: str = ''
    when_to: str = ''
    confidence: str = ''
    data: Dict[str, Any] = field(default_factory=dict)
    note: str = ''

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['schema_version'] = SCHEMA_VERSION
        return d


@dataclass
class Problem:
    """Recoverable problem recorded without aborting the whole analysis."""

    id: str
    stage: str
    severity: str
    code: str
    message: str
    profile: str = ''
    artifact: str = ''
    parser: str = ''
    source_path: str = ''

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Ids:
    def __init__(self):
        self._n: Dict[str, int] = {}

    def next(self, prefix: str) -> str:
        self._n[prefix] = self._n.get(prefix, 0) + 1
        return f'{prefix}:{self._n[prefix]:04d}'
