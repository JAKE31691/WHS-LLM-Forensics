from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from .cli_output import ConsoleReporter, source_type
from .core.profile import load_profile
from .core.team_share_only import run_team_share_only


LIVE_ALIASES = {'live', 'live-system', 'live_system', 'live system', 'system'}


def resolve_live_root() -> Path:
    """Return the current Windows user's live Chromium/Electron data root."""
    user_profile = os.environ.get('USERPROFILE')
    if not user_profile:
        raise ValueError('USERPROFILE 환경 변수가 없어 live system 경로를 결정할 수 없습니다.')
    root = Path(user_profile) / 'AppData'
    if not root.is_dir():
        raise ValueError(f'live system AppData 경로가 없습니다: {root}')
    return root


def resolve_source(value: str) -> tuple[Path, bool]:
    text = str(value or '').strip()
    if text.lower() in LIVE_ALIASES:
        return resolve_live_root(), True
    source = Path(text).expanduser().resolve()
    if not source.exists():
        raise ValueError(f'입력 경로가 없습니다: {source}')
    return source, False


def validate_output(value: str, source: Path) -> Path:
    raw = Path(str(value or '').strip()).expanduser()
    if not raw.is_absolute():
        raise ValueError(f'저장 경로는 절대 경로여야 합니다: {value}')
    output = raw.resolve()
    if source.is_dir() and output.is_relative_to(source.resolve()):
        raise ValueError('저장 경로는 입력 폴더 내부로 지정할 수 없습니다.')
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description='LLMTrace environment launcher used by <environment>.cmd'
    )
    parser.add_argument('--profile', required=True)
    parser.add_argument('source', help='E01/image path or live')
    parser.add_argument('output', help='absolute output directory')
    args = parser.parse_args()

    try:
        profile = load_profile(args.profile)
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f'환경 프로필이 아직 구현되지 않았습니다: {args.profile} '
            '(현재 지원: chatgpt-web, claude-desktop)'
        ) from exc

    try:
        source, is_live = resolve_source(args.source)
        output = validate_output(args.output, source)
    except ValueError as exc:
        parser.error(str(exc))

    reporter = ConsoleReporter(profile, args.source, source, output, is_live)
    reporter.print_header()
    started = time.perf_counter()
    try:
        result = run_team_share_only(
            profile.name,
            str(output),
            input_root=str(source),
            progress=reporter,
            source_context={
                'source_type': source_type(source, is_live),
                'source': str(source),
                'requested_source': args.source,
            },
        )
    except Exception as exc:
        reporter.print_failure(exc, time.perf_counter() - started)
        raise SystemExit(1) from exc
    result['launcher'] = {
        'requested_source': args.source,
        'resolved_source': str(source),
        'live_system': is_live,
    }
    reporter.print_summary(result, time.perf_counter() - started)


if __name__ == '__main__':
    main()
