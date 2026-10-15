"""Shared read-only acquisition and trace-based environment discovery.

Detection selects an analyzer, never a user action or evidence grade.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile

REPOSITORY = Path(__file__).resolve().parent.parent
ENVIRONMENTS = ("chatgpt_app", "chatgpt_web", "claude_app", "claude_web")
BROWSER = r"%LOCALAPPDATA%\Google\Chrome\User Data"
BROWSER_FILES = ("History", "Favicons", "DIPS", "Web Data", "Preferences",
                 r"Network\Cookies", "Cookies")
BROWSER_DIRS = ("Sessions", "Session Storage", "Local Storage", "IndexedDB", r"Cache\Cache_Data")
CLAUDE_ROOTS = (r"%LOCALAPPDATA%\Packages\Claude_*",
                r"%LOCALAPPDATA%\Claude", r"%USERPROFILE%\AppData\Roaming\Claude")


def backend():
    path = str(REPOSITORY / "chatgpt_app")
    if path not in sys.path:
        sys.path.insert(0, path)
    import collect
    import artifact_definitions
    return collect, artifact_definitions


def collection_definitions():
    _, definitions = backend()
    D = definitions.ArtifactDefinition
    extra = []

    def add(identifier, label, paths, kind="file"):
        # These are acquisition definitions, not action rules. The grade is
        # required by the original collector's DTO and is never analyzed.
        extra.append(D(identifier, "environment acquisition", label, tuple(paths),
                       kind, "보조", "Collection only; no behavior classification"))

    files = [BROWSER + r"\Local State"]
    for relative in BROWSER_FILES:
        base = BROWSER + "\\*\\" + relative
        files.append(base)
        if relative not in ("Preferences",):
            files.extend(base + suffix for suffix in ("-wal", "-shm", "-journal"))
    add("_common_browser_files", "Chrome databases and preferences", files)
    add("_common_browser_dirs", "Chrome storage and cache",
        [BROWSER + "\\*\\" + relative for relative in BROWSER_DIRS], "directory")
    add("_common_claude_data", "Claude Desktop application data", CLAUDE_ROOTS, "directory")
    add("_common_claude_install", "Claude Desktop package manifests",
        (r"%SYSTEMDRIVE%\ProgramData\Microsoft\Windows\AppRepository\Claude_*.xml",
         r"%SYSTEMDRIVE%\Program Files\WindowsApps\Claude_*\AppxManifest.xml"))
    add("_common_claude_prefetch", "Claude Prefetch", (r"%SYSTEMDRIVE%\Windows\Prefetch\*CLAUDE*.pf",))
    add("_common_claude_system", "Claude shared system artifacts",
        (r"%SYSTEMDRIVE%\Windows\System32\config\SYSTEM",
         r"%SYSTEMDRIVE%\Windows\System32\config\SYSTEM.LOG*",
         r"%SYSTEMDRIVE%\Windows\System32\config\SOFTWARE",
         r"%SYSTEMDRIVE%\Windows\System32\config\SOFTWARE.LOG*",
         r"%USERPROFILE%\NTUSER.DAT", r"%USERPROFILE%\NTUSER.DAT.LOG*",
         r"%SYSTEMDRIVE%\$MFT"))
    return (*definitions.ARTIFACT_DEFINITIONS, *extra)


def member_path(root, member):
    normalized = str(member).replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(p in (".", "..") or ":" in p for p in path.parts):
        raise ValueError(f"Unsafe collected path: {member}")
    destination = (root / Path(*path.parts)).resolve()
    if not destination.is_relative_to(root.resolve()) or destination == root.resolve():
        raise ValueError(f"Unsafe collected path: {member}")
    return destination


def extract_archive(archive_path, root):
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("_collection_manifest.json"))
        for record in manifest["files"]:
            if record.get("status") != "ok":
                continue
            destination = member_path(root, record["collected_path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with archive.open(record["collected_path"]) as source, destination.open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != record["sha256"]:
                raise ValueError(f"Collected hash mismatch: {record['source_path']}")
            mtime = record.get("mtime_ns_before")
            if mtime is not None:
                os.utime(destination, ns=(mtime, mtime))
    return manifest


def acquire(source, work, *, reader_factory=None):
    collect, _ = backend()
    reader = (reader_factory(source, work) if reader_factory is not None else
              collect.LiveReader(work.parent) if source == "live" else collect.E01Reader(source))
    try:
        summary = collect.collect_artifacts(reader, work, collection_definitions())
    finally:
        reader.close()
    manifest = extract_archive(summary.archive_path, work / "files")
    manifest["warnings"] = list(dict.fromkeys([*manifest.get("warnings", []), *reader.warnings]))
    return manifest


DOMAIN_PATTERNS = {
    "chatgpt_web": re.compile(rb"(?<![a-z0-9.-])\.?(?:[a-z0-9-]+\.)*(?:chatgpt\.com|chat\.openai\.com)(?![a-z0-9.-])", re.I),
    "claude_web": re.compile(rb"(?<![a-z0-9.-])\.?(?:[a-z0-9-]+\.)*claude\.ai(?![a-z0-9.-])", re.I),
}


def scan_signatures(path, patterns):
    """Scan every byte, including UTF-16 text and chunk-boundary matches."""
    found = set()
    previous = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            data = previous + chunk
            # NUL removal supports UTF-16 ASCII origins without decoding an
            # entire binary database and without modifying collected bytes.
            for name, pattern in patterns.items():
                if pattern.search(data) or pattern.search(data.replace(b"\x00", b"")):
                    found.add(name)
            previous = data[-2048:]
    return found


def sqlite_domains(path):
    """Inspect service-bearing columns; titles and free-form text do not qualify."""
    # Even a read-only SQLite connection may update shared-memory sidecars.
    # Recovery belongs on a separate working copy, never the collected snapshot.
    with tempfile.TemporaryDirectory(prefix=".sqlite-probe-", dir=path.parent) as temporary:
        work = Path(temporary) / path.name
        shutil.copy2(path, work)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(path) + suffix)
            if sidecar.is_file():
                shutil.copy2(sidecar, Path(str(work) + suffix))
        return _sqlite_domains_copy(work)


def _sqlite_domains_copy(path):
    found = set()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, fields in (("urls", ("url",)), ("cookies", ("host_key",)),
                              ("favicons", ("url",)), ("bounces", ("site",))):
            if table not in tables:
                continue
            columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
            for field in fields:
                if field not in columns:
                    continue
                for (value,) in db.execute(f'SELECT "{field}" FROM "{table}"'):
                    raw = str(value or "").encode("utf-8")
                    found.update(name for name, pattern in DOMAIN_PATTERNS.items() if pattern.search(raw))
    return found


def detect_environments(root, manifest):
    found = {name: [] for name in ENVIRONMENTS}
    errors = []
    registry_signatures = {
        "chatgpt_app": re.compile(rb"openai\.codex_|chatgpt\.exe|bcranv\.pbqrk_", re.I),
        "claude_app": re.compile(rb"claude_(?:[0-9]|pzs8sxrjxfjjc)|claude\.exe|pynhqr\.rkr", re.I),
    }
    for record in manifest["files"]:
        # A discovered path that could not be acquired is not an analyzable
        # environment trace. Keep its failure in the manifest instead.
        if record.get("status") != "ok":
            continue
        source = record["source_path"]
        low = source.replace("/", "\\").lower()
        ids = set(record.get("definition_ids", []))
        matches = set()
        # The input contains app-specific data, not merely a shared Windows hive.
        if "\\.codex\\" in low or "\\packages\\openai.codex_" in low:
            matches.add("chatgpt_app")
        if ids & {"install_prefetch", "run_prefetch"}:
            matches.add("chatgpt_app")  # native collector verified Process EXE
        if any(i in ids for i in ("_common_claude_data", "_common_claude_install", "_common_claude_prefetch")):
            matches.add("claude_app")
        if record.get("status") == "ok":
            path = member_path(root, record["collected_path"])
            try:
                if "\\google\\chrome\\user data\\" in low:
                    name = PureWindowsPath(source).name.lower()
                    if name in ("history", "cookies", "favicons", "dips", "web data"):
                        matches.update(sqlite_domains(path))
                    elif not name.endswith(("-wal", "-shm", "-journal")):
                        # Origin-scoped IndexedDB filenames also identify a service.
                        rawpath = low.encode("utf-8")
                        matches.update(n for n, p in DOMAIN_PATTERNS.items() if p.search(rawpath))
                        matches.update(scan_signatures(path, DOMAIN_PATTERNS))
                elif PureWindowsPath(source).name.lower() in ("software", "system", "ntuser.dat", "amcache.hve"):
                    matches.update(scan_signatures(path, registry_signatures))
            except (OSError, sqlite3.Error, ValueError) as exc:
                errors.append(f"cannot inspect {source}: {type(exc).__name__}: {exc}")
        for name in matches:
            if source not in found[name]:
                found[name].append(source)
    return found, errors


def belongs(record, environment):
    ids = set(record.get("definition_ids", [])) | set(record.get("companion_for", []))
    if environment == "chatgpt_app":
        return any(not identifier.startswith("_common_") for identifier in ids)
    if environment in ("chatgpt_web", "claude_web"):
        return any(identifier.startswith("_common_browser_") for identifier in ids)
    if any(identifier.startswith("_common_claude_") for identifier in ids):
        return True
    # ActTrace uses browser History/Cookies as contextual artifacts. Browser
    # Local Storage is not desktop Local Storage and is excluded from this view.
    name = PureWindowsPath(record["source_path"]).name.lower()
    return "_common_browser_files" in ids and (
        name in ("local state", "preferences") or
        any(name == base or name.startswith(base + "-") for base in ("history", "cookies")))


def export_environment(root, manifest, environment, destination):
    destination.mkdir(parents=True, exist_ok=False)
    selected = []
    for original in manifest["files"]:
        if not belongs(original, environment):
            continue
        record = dict(original)
        if environment == "chatgpt_app":
            for key in ("definition_ids", "companion_for"):
                record[key] = [i for i in record.get(key, []) if not i.startswith("_common_")]
            record["collection_role"] = "primary" if record["definition_ids"] else "companion"
        selected.append(record)
        if record.get("status") == "ok":
            target = member_path(destination, record["collected_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            # Real copies: native SQLite parsers may use journal sidecars. Never
            # hard-link the shared snapshot or originals into writable parsers.
            shutil.copy2(member_path(root, record["collected_path"]), target)
    document = dict(manifest, files=selected)
    (destination / "_collection_manifest.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return document


def write_archive(stage, destination, manifest):
    with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as archive:
        for record in manifest["files"]:
            if record.get("status") == "ok":
                path = member_path(stage, record["collected_path"])
                with path.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                if digest != record["sha256"]:
                    raise ValueError(f"Collected copy changed: {record['source_path']}")
                archive.write(path, record["collected_path"])
        archive.writestr("_collection_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
