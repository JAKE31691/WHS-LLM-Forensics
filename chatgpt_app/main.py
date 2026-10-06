"""Command-line entry point for the ChatGPT Desktop artifact analyzer."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import sys
import time
import zipfile
from pathlib import Path

from artifact_definitions import ARTIFACT_DEFINITIONS, validate_definitions
from action_rules import detect_confirmed_observations, link_confirmed_observations, rows_without_conditions
from collect import CollectionSetupError, CollectionSummary, E01Reader, LiveReader, collect_artifacts
from parsers import parse_artifacts
from report import prepare_report_rows, write_csv_report, write_html_report


VERSION = "1.0.0"
AUTHOR = "jbsquad"
CONTACT = "jbsquad000@gmail.com"
TARGET = "ChatGPT Desktop"
COLLECTION_ARTIFACTS = tuple(dict.fromkeys(item.artifact for item in ARTIFACT_DEFINITIONS))


def launcher_command() -> str:
    """Show the batch launcher only when it actually started this process."""
    return "chatgpt-app.cmd" if os.environ.get("CHATGPT_APP_ENTRY") == "chatgpt-app.cmd" else "python main.py"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"{TARGET} artifact collector",
        usage=f"{launcher_command()} <E01 file path or live> <absolute output path>",
    )
    parser.add_argument("source", help="E01 file path or live")
    parser.add_argument("output", help="absolute directory path for results")
    return parser


def validate_source(value: str, parser: argparse.ArgumentParser) -> tuple[str, str]:
    if value == "live":
        return "Live", "live"

    source = Path(value)
    if source.suffix.lower() != ".e01":
        parser.error("source must be an E01 file path or 'live'")
    if not source.is_file():
        parser.error(f"E01 file does not exist or is not a file: {source}")
    try:
        with source.open("rb"):
            pass
    except OSError as exc:
        parser.error(f"E01 file cannot be read: {source} ({exc})")
    return "E01", str(source.resolve())


def validate_output(value: str, parser: argparse.ArgumentParser) -> Path:
    output = Path(value)
    if not output.is_absolute():
        parser.error("output must be an absolute directory path")
    if output.exists() and not output.is_dir():
        parser.error(f"output path is not a directory: {output}")

    # Catch a file in an existing ancestor without creating an empty result folder.
    for parent in output.parents:
        if parent.exists() and not parent.is_dir():
            parser.error(f"output parent is not a directory: {parent}")
    existing = [name for name in (
        "collected_artifacts.zip", "parsed_artifacts.json",
        "LLM_Artifact_Report.csv", "LLM_Artifact_Report.html",
    ) if (output / name).exists()]
    if existing:
        parser.error(f"output already contains analysis results: {', '.join(existing)}")
    return output


def windows_name() -> str:
    if platform.system() != "Windows":
        return platform.platform()
    version = sys.getwindowsversion()
    return "Windows 11" if version.build >= 22000 else f"Windows {platform.release()}"


def is_64_bit_os() -> bool:
    return platform.machine().lower() in {"amd64", "x86_64", "arm64", "aarch64", "ia64"}


def print_run_info(source_type: str, source: str, output: Path) -> None:
    print(f"LLM Artifact Analyzer v{VERSION}")
    print(f"Author: {AUTHOR}")
    print(f"Contact: {CONTACT}")
    print()
    print("Command line:")
    print(f'  {launcher_command()} "{source}" "{output}"')
    print()
    print("System info:")
    print(f"  Machine name: {platform.node()}")
    print(f"  64-bit: {is_64_bit_os()}")
    print(f"  User: {getpass.getuser()}")
    print(f"  OS: {windows_name()}")
    print()
    print("Source info:")
    print(f"  Type: {source_type}")
    print(f"  Source: {source}")
    print()
    print("Output info:")
    print(f"  Path: {output}")
    print()
    print("Target:")
    print(f"  {TARGET}")
    print()
    print("Loading artifact definitions...")
    print(f"  Found {len(COLLECTION_ARTIFACTS)} artifact definitions.")
    print()


def print_collection_summary(summary: CollectionSummary) -> None:
    print("Artifact collection:")
    for index, artifact in enumerate(COLLECTION_ARTIFACTS, start=1):
        print(f"  [{index}/{len(COLLECTION_ARTIFACTS)}] {artifact}")
        print(f"        Found: {summary.per_artifact.get(artifact, 0)} files")
        print()
    print("Artifact extraction completed.")
    print(f"  Artifact definitions : {len(COLLECTION_ARTIFACTS)}")
    print(f"  Files found          : {summary.found}")
    print(f"  Files extracted      : {summary.extracted}")
    print(f"  Extraction failures  : {summary.failures}")
    if summary.warnings:
        print()
        print("Collection diagnostics:")
        for warning in summary.warnings:
            level = "Error" if warning.startswith("cannot ") else "Warning"
            print(f"  {level}: {warning}")
    print()


def print_parsing_diagnostics(parsed: dict) -> None:
    for artifact in parsed.get("artifacts", []):
        source = artifact.get("source_path") or artifact.get("collected_path") or "unknown artifact"
        if artifact.get("status") == "failed":
            print(f"  Error: {source}: {artifact.get('error', 'parsing failed')}")
        details = artifact.get("parse_details") or {}
        errors = details.get("errors", []) if isinstance(details, dict) else []
        if not isinstance(errors, list):
            errors = []
        for error in errors[:5]:
            print(f"  Error: {source}: {error}")
        if len(errors) > 5:
            print(f"  Error: {source}: {len(errors) - 5} more parsing errors in parsed_artifacts.json")


def print_action_summary(rows: tuple[dict[str, str], ...]) -> tuple[int, int, int]:
    """Count distinct artifact names per behavior and grade, not file rows."""
    print("Analyzing user actions...")
    by_behavior: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        by_behavior.setdefault(row["행위 분류"], {"직접": set(), "보조": set()})[
            row["아티팩트 등급"]].add(row["아티팩트"])
    for index, (behavior, grades) in enumerate(by_behavior.items(), start=1):
        print(f"  [{index}] {behavior}")
        print(f"      Direct evidence     : {len(grades['직접'])}")
        print(f"      Supporting evidence: {len(grades['보조'])}")
    direct = sum(len(grades["직접"]) for grades in by_behavior.values())
    supporting = sum(len(grades["보조"]) for grades in by_behavior.values())
    print()
    print("User action analysis completed.")
    print(f"  Actions detected   : {len(by_behavior)}")
    print(f"  Direct evidence    : {direct}")
    print(f"  Supporting evidence: {supporting}")
    print()
    return len(by_behavior), direct, supporting


def main(argv: list[str] | None = None) -> int:
    started = time.perf_counter()
    parser = build_parser()
    args = parser.parse_args(argv)
    source_type, source = validate_source(args.source, parser)
    output = validate_output(args.output, parser)
    validate_definitions()
    print_run_info(source_type, source, output)
    summary: CollectionSummary | None = None
    try:
        reader = E01Reader(source) if source_type == "E01" else LiveReader(output)
        try:
            summary = collect_artifacts(reader, output, ARTIFACT_DEFINITIONS)
        finally:
            existing_warnings = len(reader.warnings) if hasattr(reader, "warnings") else 0
            reader.close()
            if summary is not None and hasattr(reader, "warnings"):
                summary.warnings.extend(reader.warnings[existing_warnings:])
    except (CollectionSetupError, OSError, ValueError) as exc:
        print(f"Collection error: {exc}", file=sys.stderr)
        return 1
    print_collection_summary(summary)
    try:
        parsed_path = output / "parsed_artifacts.json"
        counts = parse_artifacts(summary.archive_path, parsed_path)
        with parsed_path.open("r", encoding="utf-8") as handle:
            parsed = json.load(handle)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Parsing error: {exc}", file=sys.stderr)
        return 1
    print("Artifact parsing:")
    print(f"  Output              : {parsed_path}")
    for status, count in counts.items():
        print(f"  {status:19}: {count}")
    print_parsing_diagnostics(parsed)
    print()
    try:
        observations = detect_confirmed_observations(parsed)
        analysis = link_confirmed_observations(parsed, observations)
        for rejected in analysis.rejected:
            print(f"  Warning: action observation rejected: {rejected}")
        manifest = {"files": []}
        if summary.archive_path and summary.archive_path.is_file():
            with zipfile.ZipFile(summary.archive_path) as archive:
                manifest = json.loads(archive.read("_collection_manifest.json"))
        report_rows = prepare_report_rows((*analysis.rows, *rows_without_conditions(manifest)))
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        print(f"User action analysis error: {exc}", file=sys.stderr)
        return 1
    actions_detected, _, _ = print_action_summary(report_rows)
    try:
        csv_path = output / "LLM_Artifact_Report.csv"
        row_count = write_csv_report(csv_path, report_rows)
    except (OSError, ValueError) as exc:
        print(f"CSV report error: {exc}", file=sys.stderr)
        return 1
    print("Generating CSV report...")
    print(f"  {csv_path}")
    print("  Status: OK")
    print()
    try:
        html_path = output / "LLM_Artifact_Report.html"
        html_count = write_html_report(html_path, report_rows)
        if html_count != row_count:
            raise ValueError("CSV and HTML row counts differ")
    except (OSError, ValueError) as exc:
        print(f"HTML report error: {exc}", file=sys.stderr)
        return 1
    print("Generating HTML report...")
    print(f"  {html_path}")
    print("  Status: OK")
    print()
    print("Execution summary:")
    print(f"  Target              : {TARGET}")
    print(f"  Artifact definitions : {len(COLLECTION_ARTIFACTS)}")
    print(f"  Files found          : {summary.found}")
    print(f"  Files extracted      : {summary.extracted}")
    print(f"  Actions detected     : {actions_detected}")
    print(f"  Failed               : {summary.failures + counts.get('failed', 0)}")
    print()
    print(f"Total execution time: {time.perf_counter() - started:.2f} sec")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
