from __future__ import annotations

import getpass
import platform
import sys
from pathlib import Path
from typing import Any, Dict, Iterable

from llmtrace import __version__


AUTHOR = 'jbsquad'
CONTACT = 'jbsquad000@gmail.com'

LAUNCHERS = {
    'chatgpt_web': 'chatgpt-web.cmd',
    'claude_app': 'claude-desktop.cmd',
    'chatgpt_desktop': 'chatgpt-desktop.cmd',
    'claude_web': 'claude-web.cmd',
}

ARTIFACT_LABELS = {
    'history': 'Browser History',
    'cache': 'Cache Data',
    'local_storage': 'Local Storage',
    'session_storage': 'Session Storage',
    'sessions': 'Browser Sessions',
    'cookies': 'Browser Authentication Data / Cookies',
    'preferences': 'Application Preferences',
    'dips': 'DIPS Database',
    'favicons': 'Favicons Database',
    'indexeddb': 'IndexedDB / LevelDB',
}


def source_type(source: Path, is_live: bool) -> str:
    if is_live:
        return 'LIVE SYSTEM'
    if source.is_file():
        return source.suffix.lstrip('.').upper() or 'FILE'
    return 'COLLECTED FOLDER'


def target_name(metadata: Dict[str, Any]) -> str:
    service = str(metadata.get('service') or metadata.get('profile_id') or 'Unknown')
    surface = str(metadata.get('surface') or '').lower()
    label = {
        'web': 'Web',
        'app': 'Desktop',
        'desktop': 'Desktop',
    }.get(surface, surface.title())
    return ' '.join(part for part in (service, label) if part)


def _condition_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ('all_refs', 'any_refs') and isinstance(child, (list, tuple)):
                refs.update(str(item) for item in child)
            else:
                refs.update(_condition_refs(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            refs.update(_condition_refs(child))
    return refs


def evidence_counts(inference: Any, rules_module: Any) -> tuple[int, int]:
    """Split bound evidence into minimum-rule anchors and added support."""
    detail = getattr(inference, 'detail', {}) or {}
    ref_rows = detail.get('refs') or {}
    all_ids = {
        str(row.get('observation_id'))
        for row in ref_rows.values()
        if isinstance(row, dict) and row.get('observation_id')
    }
    if not all_ids:
        all_ids = {str(item) for item in getattr(inference, 'supported_by', []) if item}

    base_rule = str(detail.get('base_rule_id') or '')
    levels = getattr(rules_module, 'RULE_LEVELS', {}).get(base_rule, {})
    minimum_refs = _condition_refs((levels.get('minimum') or {}).get('when') or {})
    direct_ids = {
        str(ref_rows[name].get('observation_id'))
        for name in minimum_refs
        if name in ref_rows
        and isinstance(ref_rows[name], dict)
        and ref_rows[name].get('observation_id')
    }
    if not direct_ids:
        direct_ids = set(all_ids)
    return len(direct_ids), len(all_ids - direct_ids)


class ConsoleReporter:
    def __init__(self, profile: Any, requested_source: str, resolved_source: Path,
                 output: Path, is_live: bool):
        self.profile = profile
        self.requested_source = requested_source
        self.resolved_source = resolved_source
        self.output = output
        self.is_live = is_live
        self.target = target_name(profile.metadata)
        self.plan = list(getattr(profile.collection, 'PARSER_PLAN', []) or [])
        self.acquisition: Dict[str, Any] | None = None
        self.files_found: int = 0
        self.files_extracted: int | None = None
        self.extraction_failures: int = 0
        self.actions_detected: int = 0
        self.direct_evidence: int = 0
        self.supporting_evidence: int = 0

    @staticmethod
    def _print(value: str = '') -> None:
        print(value, flush=True)

    def print_header(self) -> None:
        launcher = LAUNCHERS.get(self.profile.name, f'{self.profile.name}.cmd')
        os_name = ' '.join(
            part for part in (platform.system(), platform.release()) if part
        )
        self._print(f'LLMTrace Team Share Exporter v{__version__}')
        self._print(f'Author: {AUTHOR}')
        self._print(f'Contact: {CONTACT}')
        self._print()
        self._print('Command line:')
        self._print(
            f'  .\\{launcher} "{self.requested_source}" "{self.output}"'
        )
        self._print()
        self._print('System info:')
        self._print(f'  Machine name: {platform.node()}')
        self._print(f'  64-bit: {sys.maxsize > 2**32}')
        self._print(f'  User: {getpass.getuser()}')
        self._print(f'  OS: {os_name}')
        self._print()
        self._print('Source info:')
        self._print(f'  Type: {source_type(self.resolved_source, self.is_live)}')
        self._print(f'  Source: {self.resolved_source}')
        self._print()
        self._print('Output info:')
        self._print(f'  Path: {self.output}')
        self._print()
        if self.is_live:
            self._print('Live system notice:')
            self._print('  Current user AppData will be read directly.')
            self._print('  Close the target browser/app for a consistent snapshot.')
            self._print()
        self._print('Target:')
        self._print(f'  {self.target}')
        self._print()
        self._print('Loading artifact definitions...')
        self._print(f'  Found {len(self.plan)} artifact definitions.')
        self._print()
        self._print('Artifact collection:')

    def __call__(self, payload: Dict[str, object]) -> None:
        event = payload.get('event')
        if event == 'source_preparation_started':
            if not self.is_live and self.resolved_source.is_file():
                self._print('  Extracting selected files from disk image...')
                self._print()
            return

        if event == 'source_preparation_completed':
            acquisition = payload.get('acquisition')
            self.acquisition = acquisition if isinstance(acquisition, dict) else None
            return

        if event == 'artifact_processed':
            artifact = str(payload.get('artifact') or payload.get('parser') or 'Artifact')
            label = ARTIFACT_LABELS.get(artifact, artifact.replace('_', ' ').title())
            self._print(
                f'  [{payload.get("index")}/{payload.get("total")}] {label}'
            )
            self._print(f'        Found: {payload.get("source_count", 0)} sources')
            self._print(f'        Records: {payload.get("record_count", 0)}')
            failures = int(payload.get('failure_count') or 0)
            if failures:
                self._print(f'        Failures: {failures}')
            self._print()
            return

        if event == 'artifact_collection_completed':
            source_count = int(payload.get('source_count') or 0)
            problem_count = int(payload.get('problem_count') or 0)
            if self.acquisition is not None:
                self.files_found = int(
                    self.acquisition.get('files_found')
                    or self.acquisition.get('files_written')
                    or 0
                )
                self.files_extracted = int(self.acquisition.get('files_written') or 0)
                self.extraction_failures = int(self.acquisition.get('problem_count') or 0)
            else:
                self.files_found = source_count
                self.files_extracted = None
                self.extraction_failures = problem_count

            self._print('Artifact extraction completed.')
            self._print(f'  Artifact definitions : {len(self.plan)}')
            self._print(f'  Files found          : {self.files_found}')
            if self.files_extracted is None:
                reason = 'live system' if self.is_live else 'collected folder'
                self._print(f'  Files extracted      : N/A ({reason})')
            else:
                self._print(f'  Files extracted      : {self.files_extracted}')
            self._print(f'  Extraction failures  : {self.extraction_failures}')
            self._print()
            return

        if event == 'action_analysis_started':
            self._print('Analyzing user actions...')
            self._print()
            return

        if event == 'action_analysis_completed':
            inferences: Iterable[Any] = payload.get('inferences') or []
            rows = list(inferences)
            self.actions_detected = len(rows)
            for index, inference in enumerate(rows, 1):
                direct, supporting = evidence_counts(inference, self.profile.rules)
                self.direct_evidence += direct
                self.supporting_evidence += supporting
                name = getattr(inference, 'behavior_name', '') or getattr(
                    inference, 'behavior_id', 'Unknown action'
                )
                self._print(f'  [{index}] {name}')
                self._print(f'      Direct evidence     : {direct}')
                self._print(f'      Supporting evidence: {supporting}')
                self._print()
            if not rows:
                self._print('  No user actions detected.')
                self._print()
            self._print('User action analysis completed.')
            self._print(f'  Actions detected   : {self.actions_detected}')
            self._print(f'  Direct evidence    : {self.direct_evidence}')
            self._print(f'  Supporting evidence: {self.supporting_evidence}')
            self._print()
            return

        if event == 'report_started':
            self._print(f'Generating {payload.get("report_type")} report...')
            return

        if event == 'report_completed':
            self._print(f'  {payload.get("path")}')
            self._print('  Status: OK')
            self._print()

    def print_summary(self, result: Dict[str, Any], elapsed_seconds: float) -> None:
        team_share = result.get('team_share')
        if isinstance(team_share, dict):
            self._print('Final output:')
            self._print(f'  CSV : {team_share.get("csv", "")}' )
            self._print(f'  HTML: {team_share.get("html", "")}' )
            self._print('  Other output files: 0')
            self._print()

        failed = int(result.get('problems') or 0) + self.extraction_failures
        self._print('Execution summary:')
        self._print(f'  Target               : {self.target}')
        self._print(f'  Artifact definitions : {len(self.plan)}')
        self._print(f'  Files found          : {self.files_found}')
        if self.files_extracted is None:
            self._print('  Files extracted      : N/A')
        else:
            self._print(f'  Files extracted      : {self.files_extracted}')
        self._print(f'  Actions detected     : {self.actions_detected}')
        self._print(f'  Failed               : {failed}')
        self._print()
        self._print(f'Total execution time: {elapsed_seconds:.2f} sec')

    def print_failure(self, exc: BaseException, elapsed_seconds: float) -> None:
        self._print()
        self._print('Execution failed:')
        self._print(f'  {type(exc).__name__}: {exc}')
        self._print()
        self._print(f'Total execution time: {elapsed_seconds:.2f} sec')
