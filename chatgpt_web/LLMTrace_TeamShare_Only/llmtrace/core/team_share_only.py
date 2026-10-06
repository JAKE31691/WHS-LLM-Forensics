from __future__ import annotations

import os
import platform
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .acquisition import prepare_input
from .engine import run_all
from .mapping import process_surface
from .parsers import run_profile_parsers
from .profile import load_profile
from .schema import Ids, Problem
from .team_share import (
    DEFAULT_CSV_FILENAME,
    DEFAULT_HTML_FILENAME,
    export_team_share,
)


def _emit(progress: Optional[Callable[[Dict[str, object]], None]],
          event: str, **values: object) -> None:
    if progress is not None:
        progress({'event': event, **values})


def _safe_inline_javascript(value: str) -> str:
    """Prevent embedded evidence text from closing the surrounding script tag."""
    return re.sub(r'</script', r'<\\/script', value, flags=re.IGNORECASE)


def _bundle_html(source_html: Path, raw_data_dir: Path,
                 destination_html: Path) -> None:
    """Create one self-contained HTML file from the lazy-loaded raw shards."""
    html_text = source_html.read_text(encoding='utf-8')
    marker = '</head>'
    if marker not in html_text:
        raise ValueError(f'HTML head 마감 태그가 없습니다: {source_html}')
    before, after = html_text.split(marker, 1)

    index_path = raw_data_dir / 'index.js'
    if not index_path.is_file():
        raise FileNotFoundError(f'원본 파싱 색인이 없습니다: {index_path}')

    javascript_files = [index_path]
    javascript_files.extend(sorted(raw_data_dir.glob('shard_*.js')))
    with destination_html.open('w', encoding='utf-8', newline='') as stream:
        stream.write(before)
        stream.write('\n<script id="llmtrace-embedded-raw-data">\n')
        for javascript_path in javascript_files:
            stream.write(_safe_inline_javascript(
                javascript_path.read_text(encoding='utf-8')
            ))
            stream.write('\n')
        stream.write('</script>\n</head>')
        stream.write(after)


def _validate_final_directory(output: Path, csv_name: str,
                              html_name: str) -> None:
    if not output.exists():
        return
    if not output.is_dir():
        raise ValueError(f'저장 경로가 폴더가 아닙니다: {output}')
    allowed = {csv_name, html_name}
    unexpected = sorted(item.name for item in output.iterdir()
                        if item.name not in allowed)
    if unexpected:
        preview = ', '.join(unexpected[:5])
        if len(unexpected) > 5:
            preview += f' 외 {len(unexpected) - 5}개'
        raise ValueError(
            '최종 결과 폴더에 다른 파일이 있습니다. '
            '빈 폴더나 이 도구가 만든 결과 폴더를 지정하세요: '
            f'{preview}'
        )


def _publish_team_share(staged: Dict[str, Any], output: Path,
                        progress: Optional[Callable[[Dict[str, object]], None]]) \
        -> Dict[str, Any]:
    source_csv = Path(str(staged['csv']))
    source_html = Path(str(staged['html']))
    raw_data_dir = Path(str((staged.get('raw_data') or {}).get('directory') or ''))
    _validate_final_directory(output, source_csv.name, source_html.name)
    output.mkdir(parents=True, exist_ok=True)

    final_csv = output / source_csv.name
    final_html = output / source_html.name
    temp_csv = output / f'.{source_csv.name}.tmp'
    temp_html = output / f'.{source_html.name}.tmp'

    try:
        _emit(progress, 'report_started', report_type='CSV', path=str(final_csv))
        shutil.copyfile(source_csv, temp_csv)
        os.replace(temp_csv, final_csv)
        _emit(progress, 'report_completed', report_type='CSV', path=str(final_csv))

        _emit(progress, 'report_started', report_type='HTML', path=str(final_html))
        _bundle_html(source_html, raw_data_dir, temp_html)
        os.replace(temp_html, final_html)
        _emit(progress, 'report_completed', report_type='HTML', path=str(final_html))
    finally:
        for temporary in (temp_csv, temp_html):
            if temporary.exists():
                temporary.unlink()

    result = dict(staged)
    result.update({
        'csv': str(final_csv),
        'html': str(final_html),
        'output': str(output),
        'self_contained_html': True,
    })
    result.pop('raw_data', None)
    return result


def run_team_share_only(profile_name: str, output_dir: str, *, input_root: str,
                        partition: Optional[int] = None,
                        include_noise: bool = False,
                        progress: Optional[Callable[[Dict[str, object]], None]] = None,
                        source_context: Optional[Dict[str, str]] = None) \
        -> Dict[str, Any]:
    """Analyze a source while publishing only the two team-share files."""
    profile = load_profile(profile_name)
    ids = Ids()
    problems: list[Problem] = []
    output = Path(output_dir).resolve()
    config = profile.team_share
    _validate_final_directory(
        output,
        getattr(config, 'CSV_FILENAME', DEFAULT_CSV_FILENAME),
        getattr(config, 'HTML_FILENAME', DEFAULT_HTML_FILENAME),
    )
    work_parent = output.parent if output.parent.is_dir() else None

    with tempfile.TemporaryDirectory(
        prefix='.llmtrace-team-share-',
        dir=str(work_parent) if work_parent is not None else None,
    ) as temporary_root:
        work = Path(temporary_root)
        _emit(progress, 'source_preparation_started', input=input_root)
        prepared_input, acquisition = prepare_input(
            input_root,
            profile,
            str(work),
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

        raw_records = run_profile_parsers(
            prepared_input,
            profile,
            problems=problems,
            ids=ids,
            progress=progress,
        )
        _emit(
            progress,
            'artifact_collection_completed',
            raw_record_count=len(raw_records),
            source_count=len({row.source_path for row in raw_records if row.source_path}),
            problem_count=len(problems),
            acquisition=acquisition.as_dict() if acquisition is not None else None,
        )

        try:
            observations = process_surface(raw_records, profile, ids)
        except Exception as exc:
            problems.append(Problem(
                id=ids.next('problem'),
                stage='mapping',
                severity='error',
                code='surface_mapping_failed',
                message=f'{type(exc).__name__}: {exc}',
                profile=profile.name,
                source_path=str(prepared_input),
            ))
            observations = []

        _emit(progress, 'action_analysis_started')
        try:
            inferences, _contexts = run_all(observations, profile.rules, ids)
        except Exception as exc:
            problems.append(Problem(
                id=ids.next('problem'),
                stage='rules',
                severity='error',
                code='rule_engine_failed',
                message=f'{type(exc).__name__}: {exc}',
                profile=profile.name,
            ))
            inferences = []
        _emit(
            progress,
            'action_analysis_completed',
            inferences=inferences,
            contexts=[],
        )

        context = dict(source_context or {})
        source_value = context.get('source') or input_root
        if not context.get('source_type'):
            suffix = Path(source_value).suffix.lstrip('.').upper()
            context['source_type'] = suffix or 'COLLECTED FOLDER'
        context.setdefault('source', source_value)
        context.setdefault('machine', platform.node())
        context.setdefault(
            'os',
            ' '.join(part for part in (platform.system(), platform.release()) if part),
        )

        staged = export_team_share(
            profile,
            str(work / 'team_share'),
            observations=observations,
            inferences=inferences,
            environment=context,
        )
        published = _publish_team_share(staged, output, progress)

    return {
        'profile': profile.metadata,
        'prepared_input': prepared_input,
        'raw_records': len(raw_records),
        'observations': len(observations),
        'inferences': len(inferences),
        'problems': len(problems),
        'status': 'completed_with_problems' if problems else 'completed',
        'acquisition': acquisition.as_dict() if acquisition is not None else None,
        'team_share': published,
        'output': str(output),
    }
