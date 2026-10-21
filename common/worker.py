"""Native analyzer adapters, run in separate processes to isolate imports.

No environment rules are reimplemented here. In particular, Claude Desktop
uses actrace.app; ChatTrace's CLI/conversation reconstruction is never run.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import html
from pathlib import Path, PureWindowsPath
import re
import shutil
import sys
import tempfile
import time
import zipfile

from acquisition import REPOSITORY, member_path, write_archive

FOUR_FILES = ("collected_artifacts.zip", "parsed_artifacts.json",
              "LLM_Artifact_Report.csv", "LLM_Artifact_Report.html")

SECRET_KEYS = {"id_token", "access_token", "refresh_token", "authorization",
               "password", "api_key", "sessionkey", "session_key", "encrypted_value",
               "cookie", "set-cookie", "cookie_value"}


def redact_public_outputs(output):
    """Mask credentials only at publication; native rule inputs stay unchanged."""
    path = output / "parsed_artifacts.json"
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    secrets = set()

    def remember(value):
        if isinstance(value, str) and value:
            secrets.add(value)
        elif isinstance(value, dict):
            for child in value.values():
                remember(child)
        elif isinstance(value, list):
            for child in value:
                remember(child)

    def scrub(value, cookie=False):
        if isinstance(value, dict):
            cookie = cookie or value.get("table") == "cookies" or value.get("source_artifact") == "cookies"
            cookie = cookie or ("host_key" in value and "name" in value)
            result = {}
            for key, item in value.items():
                if key.lower() in SECRET_KEYS or (cookie and key in ("value", "encrypted_value")):
                    # Preserve an already-redacted native value (e.g. JWT claims).
                    if isinstance(item, dict) and "omitted" in item:
                        result[key] = item
                    else:
                        remember(item)
                        result[key] = {"omitted": "sensitive field"}
                else:
                    result[key] = scrub(item, cookie)
            return result
        if isinstance(value, list):
            return [scrub(item, cookie) for item in value]
        return value

    document = scrub(document)
    def sanitize_text(text):
        for secret in sorted(secrets, key=len, reverse=True):
            if len(secret) >= 8:
                for form in {secret, html.escape(secret), json.dumps(secret, ensure_ascii=False)[1:-1],
                             json.dumps(secret, ensure_ascii=True)[1:-1], secret.replace('"', '""')}:
                    text = text.replace(form, "[redacted]")
        # Short cookie values are not globally replaced (e.g. '1'). Mask only
        # explicit credential assignments and cookie value evidence fields.
        def mask_assignment(match):
            raw = match.group(2)
            quote = next((q for q in ('&quot;', '""', '"', "'")
                          if raw.startswith(q) and raw.endswith(q)), '')
            return match.group(1) + quote + "[redacted]" + quote

        text = re.sub(r'''(?i)((?:id_token|access_token|refresh_token|sessionkey|authorization|password|api_key|cookie|set-cookie)\s*(?:&quot;|["'])?\s*[:=]\s*)(&quot;.*?&quot;|""[^"\r\n]*""|"[^"\r\n]*"|'[^'\r\n]*'|[^\s;<]+)''', mask_assignment, text)
        for secret in secrets:
            if len(secret) < 8:
                text = re.sub(r'(\.value\s*=\s*)(""|&quot;|["\x27])' + re.escape(secret) + r'\2',
                              lambda m: m.group(1) + m.group(2) + "[redacted]" + m.group(2), text)
        return text

    def scrub_strings(value):
        if isinstance(value, str):
            return sanitize_text(value)
        if isinstance(value, dict):
            return {key: scrub_strings(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub_strings(item) for item in value]
        return value

    path.write_text(json.dumps(scrub_strings(document), ensure_ascii=False, indent=2), encoding="utf-8")
    for name in ("LLM_Artifact_Report.csv", "LLM_Artifact_Report.html"):
        report = output / name
        encoding = "utf-8-sig" if report.suffix == ".csv" else "utf-8"
        report.write_text(sanitize_text(report.read_text(encoding=encoding)), encoding=encoding)


def integration_info(manifest, context):
    return {
        "source": context["source"], "source_type": context["source_type"],
        "collection": {"files_found": len(manifest["files"]),
            "files": manifest["files"],
            "files_collected": sum(r.get("status") == "ok" for r in manifest["files"]),
            "failures": [r for r in manifest["files"] if r.get("status") != "ok"],
            "warnings": manifest.get("warnings", [])},
        "environment_detection": context["detection"],
    }


def append_collection_info(output, manifest, context):
    path = output / "parsed_artifacts.json"
    doc = json.loads(path.read_text(encoding="utf-8-sig"))
    doc["integration"] = integration_info(manifest, context)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def run_chatgpt_app(stage, output, manifest, context):
    started = time.perf_counter()
    sys.path.insert(0, str(REPOSITORY / "chatgpt_app"))
    from parsers import parse_artifacts
    from action_rules import (detect_confirmed_observations, link_confirmed_observations,
                              rows_without_conditions, rows_for_missing_logout)
    from report import prepare_report_rows, write_csv_report, write_html_report
    # Load the original CLI for its existing diagnostic/summary functions.
    import importlib.util
    spec = importlib.util.spec_from_file_location("chatgpt_native_cli", REPOSITORY / "chatgpt_app" / "main.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    from collect import CollectionSummary
    native.print_run_info(context["source_type"], context["source"], output)
    definitions = {definition.id: definition.artifact
                   for definition in native.ARTIFACT_DEFINITIONS}
    per_artifact = {}
    for entry in manifest["files"]:
        for artifact in {definitions[identifier]
                         for identifier in (*entry.get("definition_ids", []),
                                            *entry.get("companion_for", []))
                         if identifier in definitions}:
            per_artifact[artifact] = per_artifact.get(artifact, 0) + 1
    native.print_collection_summary(CollectionSummary(
        found=len(manifest["files"]),
        extracted=sum(entry.get("status") == "ok" for entry in manifest["files"]),
        failures=sum(entry.get("status") != "ok" for entry in manifest["files"]),
        warnings=manifest.get("warnings", []), per_artifact=per_artifact))
    write_archive(stage, output / FOUR_FILES[0], manifest)
    counts = parse_artifacts(output / FOUR_FILES[0], output / FOUR_FILES[1])
    parsed = json.loads((output / FOUR_FILES[1]).read_text(encoding="utf-8"))
    print("Artifact parsing:")
    print(f"  Output              : {output / FOUR_FILES[1]}")
    for status, count in counts.items():
        print(f"  {status:19}: {count}")
    native.print_parsing_diagnostics(parsed)
    print()
    analysis = link_confirmed_observations(parsed, detect_confirmed_observations(parsed))
    for rejected in analysis.rejected:
        print(f"Warning: {rejected}")
    rows = prepare_report_rows((*analysis.rows,
        *rows_without_conditions(manifest, confirmed_rows=analysis.rows, parsed_artifacts=parsed),
        *rows_for_missing_logout(manifest)))
    actions, direct, supporting = native.print_action_summary(rows)
    count = write_csv_report(output / FOUR_FILES[2], rows)
    print("Generating CSV report...")
    print(f"  {output / FOUR_FILES[2]}")
    print("  Status: OK\n")
    html_count = write_html_report(output / FOUR_FILES[3], rows,
        source_type=context["source_type"], actions_detected=actions,
        direct_evidence_count=direct, supporting_evidence_count=supporting)
    if count != html_count:
        raise ValueError("CSV and HTML row counts differ")
    print("Generating HTML report...")
    print(f"  {output / FOUR_FILES[3]}")
    print("  Status: OK\n")
    print("Execution summary:")
    print(f"  Target              : {native.TARGET}")
    print(f"  Artifact definitions : {len(native.COLLECTION_ARTIFACTS)}")
    print(f"  Files found          : {len(manifest['files'])}")
    print(f"  Files extracted      : {sum(entry.get('status') == 'ok' for entry in manifest['files'])}")
    print(f"  Actions detected     : {actions}")
    print(f"  Failed               : {sum(entry.get('status') != 'ok' for entry in manifest['files']) + counts.get('failed', 0)}")
    print(f"\nTotal execution time: {time.perf_counter() - started:.2f} sec")
    return int(bool(counts.get("failed") or counts.get("collection_failed")))


def extract_chatgpt_web_source(destination):
    """Unpack the pinned ChatGPT Web source into a private temporary directory."""
    archive = REPOSITORY / "chatgpt_web" / "LLMTrace_최종본_찐.zip"
    destination = Path(destination)
    seen = set()
    root = None
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            name = entry.filename
            parts = name.rstrip("/").split("/")
            if (not parts or any(part in ("", ".", "..") for part in parts)
                    or "\\" in name or ":" in name or name.startswith("/")):
                raise ValueError(f"Unsafe ChatGPT Web archive member: {name}")
            if root is None:
                root = parts[0]
            if parts[0] != root:
                raise ValueError("ChatGPT Web archive has multiple source roots")
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"ChatGPT Web archive contains a symbolic link: {name}")
            if len(parts) == 1:
                if not entry.is_dir():
                    raise ValueError("ChatGPT Web archive root is not a directory")
                continue
            relative = tuple(parts[1:])
            if relative in seen:
                raise ValueError(f"Duplicate ChatGPT Web archive member: {name}")
            seen.add(relative)
            target = destination.joinpath(*relative)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as original, target.open("xb") as extracted:
                    shutil.copyfileobj(original, extracted)
    if not (destination / "llmtrace" / "core" / "team_share_only.py").is_file():
        raise ValueError("ChatGPT Web archive lacks its analysis module")
    return destination


def run_chatgpt_web(stage, output, manifest, context):
    with tempfile.TemporaryDirectory(prefix=".chatgpt-web-source-") as temporary:
        source = extract_chatgpt_web_source(temporary)
        sys.path.insert(0, str(source))
        try:
            return _run_chatgpt_web_native(stage, output, manifest, context)
        finally:
            sys.path.remove(str(source))


def _run_chatgpt_web_native(stage, output, manifest, context):
    from llmtrace.core import team_share_only as native
    from llmtrace.cli_output import ConsoleReporter
    from llmtrace.core.profile import load_profile
    from dataclasses import asdict
    started = time.perf_counter()
    reporter = ConsoleReporter(load_profile("chatgpt_web"), context["source"],
                               Path(context["source"]), output,
                               context["source"] == "live")
    reporter.print_header()
    # The native web analyzer sees a collected directory, so its preparation
    # event has no acquisition object. Supply the actual shared-collector
    # counts for this environment to its existing CLI reporter.
    shared_acquisition = {
        "files_found": len(manifest["files"]),
        "files_written": sum(entry.get("status") == "ok" for entry in manifest["files"]),
        "problem_count": sum(entry.get("status") != "ok" for entry in manifest["files"]),
    }

    def report_progress(payload):
        if payload.get("event") == "source_preparation_completed":
            payload = dict(payload, acquisition=shared_acquisition)
        reporter(payload)

    original_parsers = native.run_profile_parsers
    diagnostics = []

    def parse_collected(input_root, profile, **kwargs):
        nonlocal diagnostics
        diagnostics = kwargs["problems"]
        rows = original_parsers(input_root, profile, **kwargs)
        for row in rows:
            # Only transport metadata is mapped. Parsed field values, matching
            # rules, timestamps and evidence levels remain native.
            relative = Path(row.source_path).resolve().relative_to(stage.resolve())
            parts = relative.parts
            if not parts or len(parts[0]) != 1 or not parts[0].isalpha():
                raise ValueError(f"Unverified source drive: {row.source_path}")
            row.source_path = str(PureWindowsPath(parts[0] + ":\\", *parts[1:]))
        return rows

    native.run_profile_parsers = parse_collected
    result = native.run_team_share_only("chatgpt_web", str(output), input_root=str(stage),
        progress=report_progress,
        source_context={"source": context["source"], "requested_source": context["source"],
                        "source_type": context["source_type"]})
    path = output / "parsed_artifacts.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["parser_diagnostics"] = [asdict(problem) for problem in diagnostics]
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    for problem in diagnostics:
        print(f"  {problem.stage}: {problem.message}", flush=True)
    reporter.print_summary(result, time.perf_counter() - started)
    # This native tool publishes three files. Do not rename/add report formats.
    return int(result["status"] != "completed")


class SnapshotSource:
    """Claude Web's source interface backed by the already collected snapshot.

    Directory listings include failed acquisitions so native collection reports
    permission/read errors rather than incorrectly treating those files as absent.
    """
    def __init__(self, stage, manifest, context):
        self.root = str(stage)
        self.kind = context["source_type"]
        self.display = context["source"]
        self.manifest = manifest
        # The native serializer calls ewf.to_json(). Preserve only acquisition
        # metadata actually available; do not invent stored E01 hashes/segments.
        from types import SimpleNamespace
        self.ewf = SimpleNamespace(to_json=lambda: {
            "acquisition": "common collector", "source": manifest.get("source", {}),
            "native_ewf_metadata": "not read"})
        self.records = {r["source_path"].casefold(): r for r in manifest["files"]}
        self.drives = sorted({PureWindowsPath(r["source_path"]).drive for r in manifest["files"]})
        if not self.drives:
            raise ValueError("No verified drive in collected browser artifacts")
        self.drive = self.drives[0]

    def list_dir(self, path):
        prefix = str(PureWindowsPath(path)).rstrip("\\") + "\\"
        children = {}
        for record in self.records.values():
            original = record["source_path"]
            if original.casefold().startswith(prefix.casefold()):
                rest = original[len(prefix):].split("\\")
                children[rest[0]] = len(rest) > 1
        return sorted(children.items(), key=lambda item: item[0].casefold())

    def is_dir(self, path):
        return bool(self.list_dir(path))

    def is_file(self, path):
        return str(PureWindowsPath(path)).casefold() in self.records

    def copy_to(self, path, destination):
        record = self.records[str(PureWindowsPath(path)).casefold()]
        if record.get("status") != "ok":
            raise OSError(record.get("error") or "Shared collection failed")
        source = member_path(Path(self.root), record["collected_path"])
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        mtime = record.get("mtime_ns_before")
        modified = datetime.fromtimestamp(mtime / 1e9, timezone.utc).isoformat() if mtime is not None else None
        return {"size": record["collected_size"], "sha256": record["sha256"],
                "times": {"created": None, "modified": modified, "accessed": None}}

    def evidence_info(self):
        return {"hostname": None, "os": None, "acquisition": "shared read-only snapshot"}

    def close(self):
        pass


def run_claude_web(stage, output, manifest, context):
    tool = REPOSITORY / "claude_web" / "tools"
    sys.path[:0] = [str(tool), str(tool / "third_party")]
    import importlib.util
    spec = importlib.util.spec_from_file_location("claude_web_native_cli", tool / "main.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    import collector
    source = SnapshotSource(stage, manifest, context)
    original_find = collector.find_profiles

    def find_profiles(snapshot):
        profiles, skipped = [], []
        for drive in snapshot.drives:
            snapshot.drive = drive
            current, errors = original_find(snapshot)
            profiles.extend(current)
            skipped.extend(errors)
        return profiles, skipped

    collector.find_profiles = find_profiles
    # E01 was opened by the shared collector. This native preflight must not
    # reopen it or require dissect.target's E01Source object on a collected copy.
    native._check_image = lambda *_: (True, {"requested": False,
        "result": "not_run", "computed": {}, "acquisition": "common read-only collector"})
    return native.execute(source, str(output), "act_trace.cmd")


def run_claude_app(stage, output, manifest, context):
    sys.path.insert(0, str(REPOSITORY / "claude_app" / "tools"))
    from actrace import app
    original_header = app.uniform.header

    def source_header(say, meta, command, definitions):
        meta["collection_digest"] = meta.get("input_sha256")
        meta["input_sha256"] = "not computed; per-file SHA-256 in collection manifest"
        meta["input"] = context["source"]
        meta["input_kind"] = context["source_type"]
        return original_header(say, meta, ["act_trace.cmd", context["source"], str(output)], definitions)

    app.uniform.header = source_header
    # Archive before native parsers can open SQLite sidecars in the working copy.
    write_archive(stage, output / FOUR_FILES[0], manifest)
    # ActTrace also writes internal CSV/JSONL files. Publish only the four
    # requested ChatGPT Desktop filenames, without invoking ChatTrace.main.
    with tempfile.TemporaryDirectory(prefix=".actrace-", dir=output.parent) as temporary:
        native_output = Path(temporary)
        code = app.main([str(stage), str(native_output), "--no-anim"])
        for name in FOUR_FILES[1:]:
            if not (native_output / name).is_file():
                raise RuntimeError(f"ActTrace did not generate {name}")
            shutil.copy2(native_output / name, output / name)
        # Keep every acquired artifact, including files whose parser returned no
        # observations. This also preserves the common hashes and failure list.
        parsed_path = output / FOUR_FILES[1]
        parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
        parsed["source"] = {"mode": "live" if context["source"] == "live" else "E01",
                            "path": context["source"]}
        parsed_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    return code or 0


RUNNERS = {"chatgpt_app": run_chatgpt_app, "chatgpt_web": run_chatgpt_web,
           "claude_app": run_claude_app, "claude_web": run_claude_web}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 4 or args[0] not in RUNNERS:
        raise ValueError("Internal worker requires environment, snapshot, output, context")
    name, stage_text, output_text, context_text = args
    stage, output = Path(stage_text), Path(output_text)
    context = json.loads(Path(context_text).read_text(encoding="utf-8"))
    manifest = json.loads((stage / "_collection_manifest.json").read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=False)
    # Native parsers use temporary SQLite copies. Keep their temp files in our
    # writable workspace, and away from the artifact tree they recursively scan.
    previous_temp = tempfile.tempdir
    with tempfile.TemporaryDirectory(prefix=".native-work-", dir=output.parent) as temporary:
        tempfile.tempdir = temporary
        try:
            code = RUNNERS[name](stage, output, manifest, context)
        finally:
            tempfile.tempdir = previous_temp
    append_collection_info(output, manifest, context)
    redact_public_outputs(output)
    expected = FOUR_FILES if name in ("chatgpt_app", "claude_app") else (
        ("LLM_Artifacts.zip", *FOUR_FILES[1:]) if name == "claude_web" else FOUR_FILES[1:])
    for file in expected:
        if not (output / file).is_file():
            raise RuntimeError(f"{name} did not generate {file}")
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Analysis failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
