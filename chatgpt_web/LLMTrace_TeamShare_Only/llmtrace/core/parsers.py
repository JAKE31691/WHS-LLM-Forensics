from __future__ import annotations

from typing import Callable, Dict, List, Optional

import llmtrace.common.parsers  # register common parsers
from llmtrace.common.parsers.base import PARSERS
from .schema import Ids, Problem, RawRecord


def run_profile_parsers(input_root: str, profile, problems: Optional[List[Problem]] = None,
                        ids: Optional[Ids] = None,
                        progress: Optional[Callable[[Dict[str, object]], None]] = None,
                        ) -> List[RawRecord]:
    """Run first-stage artifact parsers selected by a surface profile.

    First-stage parser contract
    ---------------------------
    * Read the artifact's native structure.
    * Recover logical records as faithfully as practical.
    * Return RawRecord objects.
    * ``RawRecord.record`` MUST contain only source-artifact fields/values.
    * Do NOT add fact_type, evidence_kind, service IDs, converted semantic
      fields, or behavior meaning at this stage.

    collection.py may define::

        PARSER_PLAN = [
          {'parser': 'chromium_history', 'artifact': 'history'},
          {'parser': 'chromium_cache', 'artifact': 'cache'},
        ]

    Common parser signature::

        fn(input_root, step, profile) -> iterable[RawRecord]

    A surface-specific first-stage parser package may additionally expose::

        parse_raw(input_root, profile) -> iterable[RawRecord]
    """
    rows: List[RawRecord] = []
    problems = problems if problems is not None else []
    ids = ids or Ids()
    plan = getattr(profile.collection, 'PARSER_PLAN', []) if profile.collection else []
    total = len(plan)
    for index, step in enumerate(plan, 1):
        name = step.get('parser')
        if name not in PARSERS:
            raise RuntimeError(
                f'Common parser {name!r} requested by profile {profile.name!r} is not registered. '
                'Implement it once under llmtrace/common/parsers and register it with @register(name).'
            )
        parsed: List[RawRecord] = []
        problem_count_before = len(problems)
        try:
            runtime_step = dict(step)
            runtime_step.update({
                '_problem_sink': problems,
                '_ids': ids,
                '_profile_name': profile.name,
            })
            parsed = list(PARSERS[name](input_root, runtime_step, profile))
            rows.extend(parsed)
        except Exception as exc:
            problems.append(Problem(
                id=ids.next('problem'), stage='parse', severity='error',
                code='parser_failed', message=f'{type(exc).__name__}: {exc}',
                profile=profile.name, artifact=str(step.get('artifact') or ''),
                parser=str(name), source_path=str(input_root),
            ))
        if progress is not None:
            progress({
                'event': 'artifact_processed',
                'index': index,
                'total': total,
                'artifact': str(step.get('artifact') or ''),
                'parser': str(name or ''),
                'source_count': len({row.source_path for row in parsed if row.source_path}),
                'record_count': len(parsed),
                'failure_count': len(problems) - problem_count_before,
            })

    mod = profile.parsers
    if mod is not None and hasattr(mod, 'parse_raw'):
        try:
            rows.extend(mod.parse_raw(input_root, profile))
        except Exception as exc:
            problems.append(Problem(
                id=ids.next('problem'), stage='parse', severity='error',
                code='profile_parser_failed', message=f'{type(exc).__name__}: {exc}',
                profile=profile.name, parser=f'{profile.name}.parsers',
                source_path=str(input_root),
            ))

    if not plan and (mod is None or not hasattr(mod, 'parse_raw')):
        raise RuntimeError(
            f'Profile {profile.name!r} has no first-stage parser plan. '
            'Define collection.PARSER_PLAN or implement '
            'profiles/<surface>/parsers.parse_raw().'
        )
    return rows
