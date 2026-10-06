from __future__ import annotations

import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

from .acquisition import prepare_input
from .context_builder import build_sections
from .engine import run_all
from .forensic_summary import build_forensic_summary, render_forensic_html
from .io import read_observations_jsonl, write_csv, write_jsonl
from .mapping import process_surface
from .parsers import run_profile_parsers
from .profile import load_profile
from .raw_io import write_raw_records
from .report import BEHAVIOR_COLUMNS, render_html
from .schema import Ids, Problem
from .team_share import export_team_share
from llmtrace import __version__


PROBLEM_COLUMNS = [
    'id', 'stage', 'severity', 'code', 'profile', 'artifact', 'parser',
    'source_path', 'message',
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _emit(progress: Optional[Callable[[Dict[str, object]], None]],
          event: str, **values) -> None:
    if progress is not None:
        progress({'event': event, **values})


def _write_run_outputs(out: Path, profile, problems, started_at: str,
                       stage: str, counts, options) -> Dict[str, object]:
    status = 'completed_with_problems' if problems else 'completed'
    finished_at = _utc_now()
    run_data = {
        'schema_version': '0.4',
        'tool': 'LLMTrace Modular',
        'tool_version': __version__,
        'status': status,
        'stage': stage,
        'started_at': started_at,
        'finished_at': finished_at,
        'profile': profile.metadata,
        'runtime': {
            'python': sys.version.split()[0],
            'platform': platform.platform(),
        },
        'options': options,
        'counts': counts,
        'problem_count': len(problems),
    }
    with open(out / 'run.json', 'w', encoding='utf-8') as stream:
        json.dump(run_data, stream, ensure_ascii=False, indent=2)
    write_csv(
        str(out / 'problems.csv'), PROBLEM_COLUMNS,
        [problem.as_dict() for problem in problems],
    )
    return run_data


def run(profile_name: str, out_dir: str, observations_path: Optional[str] = None,
        input_root: Optional[str] = None, *, partition: Optional[int] = None,
        include_noise: bool = False, extract_only: bool = False,
        parse_only: bool = False,
        progress: Optional[Callable[[Dict[str, object]], None]] = None,
        source_context: Optional[Dict[str, str]] = None):
    started_at = _utc_now()
    profile = load_profile(profile_name)
    ids = Ids()
    problems: list[Problem] = []
    out = Path(out_dir)
    internal = out / '_internal'
    raw_out = out / '_raw'
    out.mkdir(parents=True, exist_ok=True)
    internal.mkdir(parents=True, exist_ok=True)

    acquisition = None
    prepared_input = input_root
    raw_records = []
    raw_manifest = None
    options = {
        'input': input_root,
        'observations': observations_path,
        'output': out_dir,
        'partition': partition,
        'include_noise': include_noise,
        'extract_only': extract_only,
        'parse_only': parse_only,
        'source_context': source_context or {},
    }

    # Compatibility/debug path: bypass stage 1/2 when pre-normalized Observations
    # are explicitly supplied.
    if observations_path:
        observations = read_observations_jsonl(observations_path)
    elif input_root:
        _emit(progress, 'source_preparation_started', input=input_root)
        prepared_input, acquisition = prepare_input(
            input_root,
            profile,
            str(out),
            partition=partition,
            include_noise=include_noise,
        )
        _emit(
            progress,
            'source_preparation_completed',
            input=input_root,
            prepared_input=prepared_input,
            acquisition=acquisition.as_dict() if acquisition is not None else None,
        )
        if acquisition is not None:
            with open(internal / 'acquisition.json', 'w', encoding='utf-8') as f:
                json.dump(acquisition.as_dict(), f, ensure_ascii=False, indent=2)
            if extract_only:
                _write_run_outputs(
                    out, profile, problems, started_at, 'acquisition',
                    {'raw_records': 0, 'observations': 0, 'behaviors': 0, 'contexts': 0},
                    options,
                )
                return {
                    'profile': profile.metadata,
                    'input': input_root,
                    'prepared_input': prepared_input,
                    'acquisition': acquisition.as_dict(),
                    'output': str(out),
                    'extract_only': True, 'problems': len(problems),
                }

        # Stage 1: native artifact parsing. No surface semantics are added.
        raw_records = run_profile_parsers(
            prepared_input, profile, problems=problems, ids=ids,
            progress=progress,
        )
        raw_manifest = write_raw_records(str(raw_out), raw_records)
        _emit(
            progress,
            'artifact_collection_completed',
            raw_record_count=len(raw_records),
            source_count=len({row.source_path for row in raw_records if row.source_path}),
            problem_count=len(problems),
            acquisition=acquisition.as_dict() if acquisition is not None else None,
        )

        if parse_only:
            _write_run_outputs(
                out, profile, problems, started_at, 'raw_parsing',
                {'raw_records': len(raw_records), 'observations': 0, 'behaviors': 0, 'contexts': 0},
                options,
            )
            return {
                'profile': profile.metadata,
                'prepared_input': prepared_input,
                'raw_records': len(raw_records),
                'raw_manifest': raw_manifest,
                'acquisition': acquisition.as_dict() if acquisition is not None else None,
                'output': str(out),
                'parse_only': True, 'problems': len(problems),
            }

        # Stage 2: surface-aware interpretation begins here.
        try:
            observations = process_surface(raw_records, profile, ids)
        except Exception as exc:
            problems.append(Problem(
                id=ids.next('problem'), stage='mapping', severity='error',
                code='surface_mapping_failed',
                message=f'{type(exc).__name__}: {exc}', profile=profile.name,
                source_path=str(prepared_input),
            ))
            observations = []
    else:
        raise ValueError('Either observations_path or input_root is required')

    _emit(progress, 'action_analysis_started')
    try:
        inferences, contexts = run_all(observations, profile.rules, ids)
    except Exception as exc:
        problems.append(Problem(
            id=ids.next('problem'), stage='rules', severity='error',
            code='rule_engine_failed', message=f'{type(exc).__name__}: {exc}',
            profile=profile.name,
        ))
        inferences, contexts = [], []
    _emit(
        progress,
        'action_analysis_completed',
        inferences=inferences,
        contexts=contexts,
    )
    sections = build_sections(observations, profile.context)

    write_jsonl(str(internal / 'observations.jsonl'), observations)
    write_jsonl(str(internal / 'inferences.jsonl'), inferences)
    write_jsonl(str(internal / 'context.jsonl'), contexts)
    behavior_path = out / 'behaviors.csv'
    _emit(progress, 'report_started', report_type='CSV', path=str(behavior_path))
    write_csv(str(behavior_path), BEHAVIOR_COLUMNS, [x.as_dict() for x in inferences])
    _emit(progress, 'report_completed', report_type='CSV', path=str(behavior_path))
    forensic_summary = build_forensic_summary(observations)
    html_path = out / 'report.html'
    _emit(progress, 'report_started', report_type='HTML', path=str(html_path))
    render_html(
        str(html_path), profile.metadata, observations, inferences, contexts, sections,
        unresolved=getattr(profile.rules, 'UNRESOLVED_BEHAVIORS', None),
        forensic_summary=forensic_summary,
    )
    _emit(progress, 'report_completed', report_type='HTML', path=str(html_path))
    forensic_html_path = out / 'forensic_info.html'
    _emit(
        progress, 'report_started', report_type='Forensic HTML',
        path=str(forensic_html_path),
    )
    render_forensic_html(str(forensic_html_path), profile.metadata, forensic_summary)
    _emit(
        progress, 'report_completed', report_type='Forensic HTML',
        path=str(forensic_html_path),
    )
    # Supplementary team-facing lane. Main forensic outputs above remain the
    # authoritative case results; this export contains only matched actions and
    # their linked artifact paths, never account identities or message content.
    team_share = None
    context = dict(source_context or {})
    source_value = context.get('source') or input_root or observations_path or ''
    if not context.get('source_type'):
        suffix = Path(source_value).suffix.lstrip('.').upper() if source_value else ''
        context['source_type'] = suffix or (
            'OBSERVATION JSONL' if observations_path else 'COLLECTED FOLDER'
        )
    context.setdefault('source', source_value)
    context.setdefault('machine', platform.node())
    context.setdefault(
        'os', ' '.join(part for part in (platform.system(), platform.release()) if part)
    )
    try:
        team_share = export_team_share(
            profile,
            str(out / 'team_share'),
            observations=observations,
            inferences=inferences,
            environment=context,
        )
    except Exception as exc:
        problems.append(Problem(
            id=ids.next('problem'), stage='report', severity='error',
            code='team_share_export_failed',
            message=f'{type(exc).__name__}: {exc}', profile=profile.name,
            source_path=str(out / 'team_share'),
        ))
    run_data = _write_run_outputs(
        out, profile, problems, started_at, 'complete',
        {
            'raw_records': len(raw_records),
            'observations': len(observations),
            'behaviors': len(inferences),
            'contexts': len(contexts),
            'team_share_artifact_rows': (
                team_share['artifact_rows'] if team_share is not None else 0
            ),
            'team_share_csv_rows': (
                team_share['csv_rows'] if team_share is not None else 0
            ),
        },
        options,
    )
    _emit(progress, 'pipeline_completed', status=run_data['status'])
    return {
        'profile': profile.metadata,
        'prepared_input': prepared_input,
        'raw_records': len(raw_records),
        'raw_manifest': raw_manifest,
        'observations': len(observations),
        'inferences': len(inferences),
        'contexts': len(contexts),
        'problems': len(problems),
        'status': run_data['status'],
        'acquisition': acquisition.as_dict() if acquisition is not None else None,
        'team_share': team_share,
        'forensic_info': {
            'html': str(forensic_html_path),
            'counts': forensic_summary['counts'],
        },
        'output': str(out),
    }
