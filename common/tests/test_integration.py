"""Synthetic fixtures only: no real user accounts, tokens, or evidence."""
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

import pytest

COMMON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COMMON))
import acquisition as a
import main as integrated
from worker import SnapshotSource, FOUR_FILES, redact_public_outputs


def record(root, source, data, ids, *, failed=False):
    member = source.replace(":", "").replace("\\", "/")
    path = root / member
    if not failed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return {"source_path": source, "collected_path": None if failed else member,
            "definition_ids": ids, "companion_for": [], "collection_role": "primary",
            "status": "failed" if failed else "ok", "collected_size": len(data),
            "size_before": len(data), "mtime_ns_before": None,
            "sha256": hashlib.sha256(data).hexdigest(), "error": "read denied" if failed else None}


def history_bytes(tmp_path, urls):
    path = tmp_path / "history-source"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER, typed_count INTEGER, last_visit_time INTEGER, hidden INTEGER)")
        db.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, from_visit INTEGER, transition INTEGER, segment_id INTEGER, visit_duration INTEGER)")
        for index, url in enumerate(urls, 1):
            db.execute("INSERT INTO urls VALUES (?, ?, ?, 1, 0, 13300000000000000, 0)", (index, url, "fixture"))
            db.execute("INSERT INTO visits VALUES (?, ?, 13300000000000000, 0, 1, 0, 0)", (index, index))
    return path.read_bytes()


def manifest(files):
    return {"source": {"mode": "e01", "path": "fixture.E01"}, "files": files,
            "warnings": [], "user_profiles": []}


@pytest.fixture
def mixed(tmp_path):
    root = tmp_path / "files"
    browser = r"D:\Users\fixture-user\AppData\Local\Google\Chrome\User Data\Profile 2"
    files = [record(root, browser + r"\History", history_bytes(tmp_path, ["https://chatgpt.com/c/fixture"]), ["_common_browser_files"]),
             record(root, browser + r"\Preferences", b"{}", ["_common_browser_files"]),
             record(root, r"D:\Users\fixture-user\AppData\Roaming\Claude\logs\main.log", b"fixture log\n", ["_common_claude_data"])]
    return root, manifest(files)


def test_two_environments_and_original_paths(mixed):
    root, doc = mixed
    detected, errors = a.detect_environments(root, doc)
    assert not errors
    assert {name for name, values in detected.items() if values} == {"chatgpt_web", "claude_app"}
    assert detected["chatgpt_web"][0].startswith("D:\\Users\\fixture-user\\")
    for entry in doc["files"]:
        assert hashlib.sha256((root / entry["collected_path"]).read_bytes()).hexdigest() == entry["sha256"]


def test_chrome_presence_and_lookalike_domain_do_not_detect(tmp_path):
    root = tmp_path / "files"
    source = r"C:\Users\u\AppData\Local\Google\Chrome\User Data\Default\History"
    doc = manifest([record(root, source, history_bytes(tmp_path, ["https://notchatgpt.com/", "https://claude.ai.evil.test/"]), ["_common_browser_files"])])
    detected, errors = a.detect_environments(root, doc)
    assert not errors and not any(detected.values())


def test_shared_registry_presence_does_not_detect(tmp_path):
    doc = manifest([record(tmp_path, r"C:\Windows\System32\config\SOFTWARE", b"unrelated registry", ["install_appx", "_common_claude_system"])])
    assert not any(a.detect_environments(tmp_path, doc)[0].values())


def test_both_services_from_cache_and_utf16(tmp_path):
    source = r"C:\Users\u\AppData\Local\Google\Chrome\User Data\Default\Cache\Cache_Data\data_1"
    doc = manifest([record(tmp_path, source, b"https://chatgpt.com/ " + "https://claude.ai/".encode("utf-16le"), ["_common_browser_dirs"])])
    found, errors = a.detect_environments(tmp_path, doc)
    assert found["chatgpt_web"] and found["claude_web"] and not errors


def test_failed_browser_is_unknown_not_positive(tmp_path):
    doc = manifest([record(tmp_path, r"C:\Users\u\AppData\Local\Google\Chrome\User Data\Default\History", b"", ["_common_browser_files"], failed=True)])
    assert not any(a.detect_environments(tmp_path, doc)[0].values())


def test_failed_app_artifact_is_not_a_detected_environment(tmp_path):
    item = record(tmp_path, r"C:\Users\u\.codex\auth.json", b"",
                  ["login_auth"], failed=True)
    detected, errors = a.detect_environments(tmp_path, manifest([item]))
    assert not errors and not any(detected.values())


def test_export_preserves_hashes_paths_and_failures(mixed, tmp_path):
    root, doc = mixed
    stage = tmp_path / "export"
    selected = a.export_environment(root, doc, "chatgpt_web", stage)
    archive = tmp_path / "artifacts.zip"
    a.write_archive(stage, archive, selected)
    with zipfile.ZipFile(archive) as z:
        for item in selected["files"]:
            assert item["collected_path"].startswith("D/Users/")
            assert hashlib.sha256(z.read(item["collected_path"])).hexdigest() == item["sha256"]
    assert len(selected["files"]) == 2


def test_target_warnings_exclude_other_environment(tmp_path):
    root = tmp_path / "files"
    item = record(root, r"C:\Users\u\.codex\auth.json", b"{}", ["login_auth"])
    doc = manifest([item])
    doc["warnings"] = [
        "artifact not found: Prefetch (ChatGPT installer.exe) (%SYSTEMDRIVE%\\Windows\\Prefetch\\*.pf)",
        "artifact not found: Claude Desktop application data (%LOCALAPPDATA%\\Claude)",
        "E01 volume has no verified drive letter; skipped",
    ]
    stage = tmp_path / "stage"
    selected = a.export_environment(root, doc, "chatgpt_app", stage)
    warnings = selected["warnings"]
    assert len(warnings) == 2
    assert "Prefetch (ChatGPT installer.exe)" in warnings[0]
    assert warnings[1].startswith("E01 volume")
    assert json.loads((stage / "_collection_manifest.json").read_text(encoding="utf-8"))["warnings"] == warnings


def test_archive_clamps_pre_1980_zip_time_without_changing_evidence(tmp_path):
    root = tmp_path / "files"
    item = record(root, r"C:\Users\u\.codex\auth.json", b"old timestamp", ["login_auth"])
    original = root / item["collected_path"]
    old_ns = 157766400 * 1_000_000_000  # 1975-01-01 UTC
    os.utime(original, ns=(old_ns, old_ns))
    before = original.stat().st_mtime_ns
    item["mtime_ns_before"] = before
    archive_path = tmp_path / "collected_artifacts.zip"
    a.write_archive(root, archive_path, manifest([item]))
    assert original.stat().st_mtime_ns == before
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.getinfo(item["collected_path"]).date_time[0] == 1980
        assert archive.read(item["collected_path"]) == b"old timestamp"
        stored = json.loads(archive.read("_collection_manifest.json"))
    assert stored["files"][0]["mtime_ns_before"] == before


def test_claude_app_does_not_receive_browser_local_storage(tmp_path):
    item = record(tmp_path, r"C:\Users\u\AppData\Local\Google\Chrome\User Data\Default\Local Storage\leveldb\000001.log", b"https://claude.ai", ["_common_browser_dirs"])
    assert not a.belongs(item, "claude_app")


def test_snapshot_source_reports_failed_copy(tmp_path):
    item = record(tmp_path, r"D:\Users\u\AppData\Local\Google\Chrome\User Data\Default\History", b"", ["_common_browser_files"], failed=True)
    source = SnapshotSource(tmp_path, manifest([item]), {"source": "fixture.E01", "source_type": "E01"})
    assert source.drive == "D:"
    assert source.is_file(item["source_path"])
    assert source.list_dir(r"D:\Users") == [("u", True)]
    with pytest.raises(OSError, match="read denied"):
        source.copy_to(item["source_path"], tmp_path / "unused")


def test_orchestrator_only_detected_folders_and_continues(mixed, tmp_path):
    root, doc = mixed
    def collect(source, work):
        import shutil
        shutil.copytree(root, work / "files")
        return doc
    calls = []
    def worker(name, stage, destination, context):
        calls.append(name)
        destination.mkdir()
        return 1 if name == "chatgpt_web" else 0
    output = tmp_path / "output"
    assert integrated.run("fixture.E01", output, acquire_fn=collect, worker=worker) == 1
    assert calls == ["chatgpt_web", "claude_app"]
    assert {p.name for p in output.iterdir()} == {"chatgpt_web", "claude_app"}


def test_no_detected_environment_leaves_no_report_folders(tmp_path):
    def no_worker(*args):
        pytest.fail("An undetected environment was analyzed")
    assert integrated.run("fixture.E01", tmp_path / "empty", acquire_fn=lambda *_: manifest([]), worker=no_worker) == 0
    assert list((tmp_path / "empty").iterdir()) == []


def test_publication_hides_credentials_without_changing_counts(tmp_path):
    secret = "fixture-cookie-secret-unique"
    document = {"artifacts": [{"records": [{"table": "cookies", "rows": [
        {"host_key": ".claude.ai", "name": "sessionKey", "value": secret, "creation_utc": 123}]}]}]}
    (tmp_path / "parsed_artifacts.json").write_text(json.dumps(document), encoding="utf-8")
    (tmp_path / "LLM_Artifact_Report.csv").write_text("시각,사용자 행위,아티팩트,경로,등급,근거\n-,로그인,Cookies,D:\\Cookies,보조," + secret, encoding="utf-8-sig")
    (tmp_path / "LLM_Artifact_Report.html").write_text("<p>" + secret + "</p>", encoding="utf-8")
    redact_public_outputs(tmp_path)
    for file in tmp_path.iterdir():
        assert secret not in file.read_text(encoding="utf-8-sig")
    parsed = json.loads((tmp_path / "parsed_artifacts.json").read_text(encoding="utf-8"))
    row = parsed["artifacts"][0]["records"][0]["rows"][0]
    assert row["value"] == {"omitted": "sensitive field"}
    assert row["creation_utc"] == 123 and row["name"] == "sessionKey"


def test_redaction_preserves_embedded_json_and_short_cookie_values(tmp_path):
    document = {"records": [{"host_key": ".claude.ai", "name": "fixture", "value": "1"}],
                "text": '{"access_token":"fixture-secret"}'}
    (tmp_path / "parsed_artifacts.json").write_text(json.dumps(document), encoding="utf-8")
    (tmp_path / "LLM_Artifact_Report.csv").write_text('record.value=""1""', encoding="utf-8-sig")
    (tmp_path / "LLM_Artifact_Report.html").write_text('{"access_token":"fixture-secret"}', encoding="utf-8")
    redact_public_outputs(tmp_path)
    assert json.loads((tmp_path / "LLM_Artifact_Report.html").read_text(encoding="utf-8"))["access_token"] == "[redacted]"
    assert '[redacted]' in (tmp_path / "LLM_Artifact_Report.csv").read_text(encoding="utf-8-sig")


def test_full_mixed_run_publishes_only_the_two_selected_environments(mixed, tmp_path, capfd):
    root, doc = mixed
    def collect(source, work):
        import shutil
        shutil.copytree(root, work / "files")
        return doc
    output = tmp_path / "complete-output"
    code = integrated.run("fixture.E01", output, acquire_fn=collect)
    assert code in (0, 1)  # incomplete synthetic app data may produce partial status
    assert {p.name for p in output.iterdir()} == {"chatgpt_web", "claude_app"}
    assert {p.name for p in (output / "claude_app").iterdir()} == set(FOUR_FILES)
    assert {p.name for p in (output / "chatgpt_web").iterdir()} == set(FOUR_FILES[1:])
    console = capfd.readouterr().out
    first = console.index("  ChatGPT Web", console.index("Target:"))
    separator = console.index("=" * 72, first)
    second = console.index("  Claude Desktop", separator)
    assert first < separator < second
    web_output = console[first:separator]
    assert "Files found          : 2" in web_output
    assert "Files extracted      : 2" in web_output
    assert "Files extracted      : N/A" not in web_output
    assert console.count("Total execution time:") >= 2
    assert console.startswith("LLM Artifact Analyzer v1.0.0\nAuthor: jbsquad")
    assert console.count("\nTarget:\n") == 2
    assert "Environment detection:" not in console
    assert "ActTrace — integrated artifact analyzer" not in console
    assert "Analyzing chatgpt_web ->" not in console
    assert ".actrace-" not in console


def test_native_cli_section_uses_published_paths(tmp_path):
    destination = tmp_path / "claude_app"
    temporary = tmp_path / ".actrace-fixture"
    raw = ("ActTrace v1.4.0\nSource info:\n  Source: fixture.E01\n"
           "Target:\n  Claude Desktop (msix)\nLoading artifact definitions...\n"
           f"Generating CSV report...\n  {temporary / 'LLM_Artifact_Report.csv'}\n"
           "Total execution time: 1.25 sec\nActTrace extra epilogue\n")
    section = integrated.native_cli_section(raw, destination)
    assert section.startswith("Loading artifact definitions...")
    assert str(destination / "LLM_Artifact_Report.csv") in section
    assert ".actrace-fixture" not in section
    assert section.endswith("Total execution time: 1.25 sec")


def test_refuses_changed_snapshot_archive(mixed, tmp_path):
    root, doc = mixed
    item = doc["files"][0]
    (root / item["collected_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        a.write_archive(root, tmp_path / "output.zip", doc)


def test_cli_rejects_missing_source_and_relative_output(tmp_path):
    with pytest.raises(SystemExit) as exc:
        integrated.validate_arguments([str(tmp_path / "absent.E01"), str(tmp_path / "output")])
    assert exc.value.code == 2
    source = tmp_path / "fixture.E01"
    source.write_bytes(b"synthetic placeholder")
    with pytest.raises(SystemExit) as exc:
        integrated.validate_arguments([str(source), "relative-output"])
    assert exc.value.code == 2


def test_e01_runtime_names_missing_module_and_interpreter(monkeypatch):
    imported = []
    def imported_module(name):
        imported.append(name)
        if name == "pytsk3":
            raise ModuleNotFoundError("No module named 'pytsk3'")
    monkeypatch.setattr(integrated.importlib, "import_module", imported_module)
    with pytest.raises(RuntimeError, match="pytsk3") as exc:
        integrated.check_e01_runtime()
    assert "python" in str(exc.value).lower()
    assert imported == ["pyewf", "pytsk3", "Registry"]


@pytest.mark.parametrize("mode", ["live", "fixture.E01"])
def test_shared_acquisition_uses_same_read_only_pipeline(tmp_path, mode):
    native, _ = a.backend()
    content = {r"C:\Users\u\.codex\.codex-global-state.json": b"{}"}
    original = dict(content)
    class Reader:
        source_mode = "live" if mode == "live" else "e01"
        source_path = mode
        system_drive = "C:"
        user_profiles = (r"C:\Users\u",)
        profiles = ()
        warnings = []
        closed = False
        def find(self, pattern):
            for path in content:
                if fnmatch.fnmatchcase(path.lower(), pattern.lower()) or path.lower().startswith(pattern.lower().rstrip("\\") + "\\"):
                    yield native.Candidate(path, path)
        def stat(self, candidate):
            return native.FileState(len(content[candidate.original_path]), 1)
        def chunks(self, candidate):
            yield content[candidate.original_path]
        def close(self):
            self.closed = True
    reader = Reader()
    doc = a.acquire(mode, tmp_path, reader_factory=lambda *_: reader)
    assert reader.closed and content == original
    assert len(doc["files"]) == 1 and doc["files"][0]["status"] == "ok"
    assert doc["files"][0]["collected_path"] == "C/Users/u/.codex/.codex-global-state.json"


@pytest.mark.parametrize("path", ["../x", "/x", "C:/x", "C/../../x"])
def test_rejects_unsafe_archive_paths(tmp_path, path):
    with pytest.raises(ValueError):
        a.member_path(tmp_path, path)


@pytest.mark.parametrize("name", a.ENVIRONMENTS)
def test_native_worker_outputs(name, mixed, tmp_path):
    root, doc = mixed
    if name == "chatgpt_app":
        item = record(root, r"D:\Users\fixture-user\.codex\.codex-global-state.json",
            b'{"desktop-first-seen-at-ms": 1700000000000}', ["run_global_state"])
        doc["files"].append(item)
        evidence = root / item["collected_path"]
        os.utime(evidence, ns=(157766400 * 1_000_000_000,) * 2)
        item["mtime_ns_before"] = evidence.stat().st_mtime_ns
    elif name == "chatgpt_web":
        doc["files"].append(record(root,
            r"D:\Users\fixture-user\AppData\Local\Google\Chrome\User Data\Profile 2\Network\Cookies",
            b"", ["_common_browser_files"], failed=True))
    stage = tmp_path / name
    a.export_environment(root, doc, name, stage)
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"source": "fixture.E01", "source_type": "E01", "detection": {}}), encoding="utf-8")
    output = tmp_path / "results" / name
    output.parent.mkdir(exist_ok=True)
    result = subprocess.run([sys.executable, str(COMMON / "worker.py"), name, str(stage), str(output), str(context)],
        env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"), capture_output=True, text=True, encoding="utf-8", errors="replace")
    expected = set(FOUR_FILES)
    if name == "chatgpt_web":
        expected.remove("collected_artifacts.zip")
    elif name == "claude_web":
        expected.remove("collected_artifacts.zip")
        expected.add("LLM_Artifacts.zip")
    assert {p.name for p in output.iterdir()} == expected, result.stdout + result.stderr
    parsed = json.loads((output / "parsed_artifacts.json").read_text(encoding="utf-8"))
    assert parsed["integration"]["source"] == "fixture.E01"
    if name == "chatgpt_web":
        assert "Files found          : 3" in result.stdout
        assert "Files extracted      : 2" in result.stdout
        assert "Extraction failures  : 1" in result.stdout
        assert not parsed["parser_diagnostics"], result.stdout + result.stderr
        assert any(r["source_artifact"] == "history" for r in parsed["records"])
        assert all(r["source_path"].startswith("D:\\Users\\") for r in parsed["records"])
        assert "https://chatgpt.com" in (output / "LLM_Artifact_Report.csv").read_text(encoding="utf-8-sig")
    assert result.returncode in (0, 1), result.stdout + result.stderr
