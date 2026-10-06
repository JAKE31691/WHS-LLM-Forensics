"""Format-based parsers for collected ChatGPT Desktop evidence.

Only ZIP members are parsed. A definition describes where to look and the
user-provided evidence grade; a parsed file alone never establishes an action.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import sqlite3
import tempfile
import tomllib
import zipfile
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from artifact_definitions import ARTIFACT_DEFINITIONS, ArtifactDefinition


MAX_RECORDS = 1000
MAX_VALUE_BYTES = 65536
MAX_TEXT_BYTES = 4 * 1024 * 1024
SECRET_NAMES = ("token", "secret", "password", "cookie", "authorization", "credential", "api_key",
                "encrypted_value")
JWT_CLAIMS = frozenset({
    "auth_time", "pwd_auth_time", "iat", "exp", "iss", "aud", "auth_provider",
    "amr", "chatgpt_account_id", "chatgpt_user_id", "user_id", "sub",
})
SQLITE_TYPES = {"sqlite", "chromium_history", "chromium_cookies"}
GROUP_TYPES = {"chromium_cache", "leveldb", "session_storage"}
CACHE_FILE_MARKERS: dict[str, bytes] = {
    "create_time": b"create_time",
    "user-": b"user-",
    "memory_percentage_used": b"memory_percentage_used",
    "memcite": b"memcite",
    "multimodal_text": b"multimodal_text",
    "다운로드]": "다운로드]".encode("utf-8"),
    "New Chat": b"New Chat",
}
PARSER_NAMES = {
    "json": "JSON parser", "jsonl": "JSONL parser", "toml": "TOML parser",
    "log": "Text/Log parser", "sqlite": "SQLite parser",
    "chromium_history": "SQLite parser", "chromium_cookies": "SQLite parser",
    "sqlite_companion": "SQLite parser", "registry_key": "python-registry",
    "registry_hive": "python-registry", "prefetch": "pyscca", "ntfs_metadata": "mft",
    "chromium_cache": "ccl_chromium_reader cache",
    "leveldb": "ccl_chromium_reader LevelDB",
    "session_storage": "ccl_chromium_reader LevelDB",
}


def _safe_value(value: Any, *, name: str = "") -> Any:
    """Preserve typed values where practical; omit secrets rather than altering them."""
    if isinstance(value, dict):
        return {str(key): _safe_value(item, name=str(key)) for key, item in value.items()}
    if any(word in name.casefold() for word in SECRET_NAMES):
        result: dict[str, Any] = {"omitted": "sensitive field"}
        if isinstance(value, str) and value.count(".") == 2 and len(value) < 1024 * 1024:
            try:
                payload = value.split(".")[1]
                decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
                claims = json.loads(decoded)
                if isinstance(claims, dict):
                    result["jwt_claims"] = {key: _safe_value(item, name=key)
                                            for key, item in claims.items() if key in JWT_CLAIMS}
                    result["signature_verified"] = False
            except (ValueError, UnicodeError, TypeError):
                pass
        return result
    if isinstance(value, bytes):
        if len(value) > MAX_VALUE_BYTES:
            return {"type": "bytes", "size": len(value), "sha256": hashlib.sha256(value).hexdigest(),
                    "omitted": "value exceeds output limit"}
        return {"type": "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return {"type": type(value).__name__, "repr": str(value)}


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeError:
            continue
    raise UnicodeError("text cannot be decoded without replacing source bytes")


def _parse_json(raw: bytes) -> dict[str, Any]:
    return {"data": _safe_value(json.loads(_decode(raw)))}


def _parse_jsonl(raw: bytes) -> dict[str, Any]:
    records, errors = [], []
    for number, line in enumerate(_decode(raw).splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append({"line": number, "value": _safe_value(json.loads(line))})
        except (ValueError, TypeError) as exc:
            errors.append({"line": number, "error": str(exc)})
        if len(records) >= MAX_RECORDS:
            return {"data": {"records": records, "errors": errors, "truncated": True}, "status": "partial"}
    return {"data": {"records": records, "errors": errors}, "status": "partial" if errors else "ok"}


def _parse_toml(raw: bytes) -> dict[str, Any]:
    return {"data": _safe_value(tomllib.loads(raw.decode("utf-8-sig")))}


def _parse_text(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_TEXT_BYTES:
        return {"status": "partial", "data": {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                                                "omitted": "text exceeds output limit"}}
    lines = _decode(raw).splitlines()
    sanitized = [({"line": number, "omitted": "sensitive line"} if any(
        word in line.casefold() for word in SECRET_NAMES) else
        {"line": number, "text": line}) for number, line in enumerate(lines, 1)]
    return {"data": {"lines": sanitized}}


def _parse_metadata(record: dict[str, Any]) -> dict[str, Any]:
    return {"data": {key: record.get(key) for key in
                     ("size_before", "size_after", "mtime_ns_before", "mtime_ns_after", "sha256")}}


def _sqlite_text(raw: bytes) -> str | bytes:
    """Keep malformed SQLite TEXT bytes intact instead of replacing evidence."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw


def _parse_sqlite(path: Path) -> dict[str, Any]:
    uri = path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.text_factory = _sqlite_text
        connection.row_factory = sqlite3.Row
        tables = []
        with closing(connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")) as cursor:
            names = [row[0] for row in cursor]
        for name in names:
            if name.startswith("sqlite_"):
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            with closing(connection.execute(f"PRAGMA table_info({quoted})")) as cursor:
                columns = [row[1] for row in cursor]
            rows = []
            with closing(connection.execute(
                    f"SELECT * FROM {quoted} LIMIT ?", (MAX_RECORDS + 1,))) as cursor:
                fetched = cursor.fetchmany(MAX_RECORDS + 1)
            for item in fetched:
                if len(rows) == MAX_RECORDS:
                    break
                rows.append({column: _safe_value(item[column], name=(
                    "cookie" if path.name.casefold() == "cookies" and column.casefold() == "value"
                    else column)) for column in item.keys()})
            tables.append({"name": name, "columns": columns, "rows": rows,
                           "truncated": len(fetched) > MAX_RECORDS})
        return {"data": {"tables": tables},
                "status": "partial" if any(t["truncated"] for t in tables) else "ok"}


def _parse_registry(path: Path, definitions: list[ArtifactDefinition]) -> dict[str, Any]:
    from Registry import Registry

    hive = Registry.Registry(str(path))
    keys = []
    for definition in definitions:
        if not definition.registry_key:
            keys.append({"definition_id": definition.id, "status": "unsupported",
                         "error": "registry key or application-specific schema has not been verified"})
            continue
        matched = [hive.root()]
        for part in definition.registry_key.split("\\"):
            next_keys = []
            for parent in matched:
                for child in parent.subkeys():
                    if fnmatch.fnmatchcase(child.name().casefold(), part.casefold()):
                        next_keys.append(child)
            matched = next_keys
            if not matched:
                break
        if not matched:
            keys.append({"definition_id": definition.id, "status": "missing_key",
                         "error": f"registry key not found: {definition.registry_key}"})
            continue
        observed = []
        pending = list(matched)
        while pending and len(observed) < MAX_RECORDS:
            key = pending.pop()
            values = [{"name": value.name(), "value_type": value.value_type_str(),
                       "value": _safe_value(value.value(), name=value.name())} for value in key.values()]
            observed.append({"path": key.path(), "last_written_iso_from_parser": key.timestamp().isoformat(),
                             "values": values})
            pending.extend(key.subkeys())
        keys.append({"definition_id": definition.id,
                     "status": "partial" if pending else "ok", "key": definition.registry_key,
                     "records": observed, "truncated": bool(pending)})
    return {"data": {"keys": keys}, "status": "partial" if any(k["status"] != "ok" for k in keys) else "ok"}


def _parse_prefetch(path: Path) -> dict[str, Any]:
    import pyscca

    prefetch = pyscca.open(str(path))
    try:
        times = []
        for index in range(8):
            try:
                raw = prefetch.get_last_run_time_as_integer(index)
            except (OSError, IndexError, TypeError):
                break
            if raw:
                times.append(raw)
        return {"raw_timestamp": {"last_run_filetime": times},
                "data": {"executable_filename": prefetch.executable_filename,
                         "format_version": prefetch.format_version, "run_count": prefetch.run_count,
                         "filenames": list(prefetch.filenames)}}
    finally:
        prefetch.close()


def _parse_mft(path: Path) -> dict[str, Any]:
    import mft

    parser = mft.PyMftParser(str(path))
    # PyMftParser transfers its internal reader to entries_json(); metadata
    # methods are unavailable after iteration starts, even if it is truncated.
    entry_count = parser.number_of_entries()
    rows = []
    for number, raw in enumerate(parser.entries_json()):
        if number == MAX_RECORDS:
            break
        rows.append(_safe_value(json.loads(raw)))
    truncated = entry_count > len(rows)
    return {"data": {"entry_count": entry_count, "entries": rows,
                     "truncated": truncated},
            "status": "partial" if truncated else "ok"}


def _parse_cache(directory: Path) -> dict[str, Any]:
    from ccl_chromium_reader import ccl_chromium_cache as cache

    cls = cache.ChromiumBlockFileCache if (directory / "index").exists() else cache.ChromiumSimpleFileCache
    entries = []
    with cls(directory) as parsed:
        for number, key in enumerate(parsed.keys()):
            if number == MAX_RECORDS:
                break
            metadata = []
            for item in parsed.get_metadata(key):
                if item is None:
                    continue
                metadata.append({"request_time": item.request_time.isoformat() if item.request_time else None,
                                 "response_time": item.response_time.isoformat() if item.response_time else None,
                                 "headers": [(name, _safe_value(value, name=name))
                                             for name, value in item.http_header_attributes]})
            bodies = []
            for body in parsed.get_cachefile(key):
                if body is None:
                    bodies.append(None)
                    continue
                try:
                    bodies.append({"size": len(body), "json": _safe_value(json.loads(body))})
                except (ValueError, UnicodeError):
                    bodies.append({"size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                                   "omitted": "non-JSON cache body"})
            entries.append({"key": _safe_value(key, name="cache_key") if not any(
                word in str(key).casefold() for word in SECRET_NAMES) else
                {"sha256": hashlib.sha256(str(key).encode("utf-8")).hexdigest(),
                 "omitted": "sensitive cache key"},
                "metadata": metadata, "bodies": bodies})
    return {"data": {"entries": entries, "truncated": len(entries) == MAX_RECORDS},
            "status": "partial" if len(entries) == MAX_RECORDS else "ok"}


def _scan_cache_file_markers(root: Path, group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Record exact UTF-8 byte hits per collected file, without interpreting actions."""
    matches: list[dict[str, Any]] = []
    overlap = max(map(len, CACHE_FILE_MARKERS.values())) - 1
    for member in group:
        path = root.joinpath(*PurePosixPath(member["collected_path"]).parts)
        found: set[str] = set()
        tail = b""
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                data = tail + chunk
                found.update(label for label, needle in CACHE_FILE_MARKERS.items() if needle in data)
                tail = data[-overlap:]
        if found:
            matches.append({
                "source_path": member["source_path"],
                "collected_path": member["collected_path"],
                "matched_markers": [label for label in CACHE_FILE_MARKERS if label in found],
            })
    return matches


def _parse_leveldb(directory: Path) -> dict[str, Any]:
    from ccl_chromium_reader.storage_formats import ccl_leveldb

    records = []
    db = ccl_leveldb.RawLevelDb(directory)
    try:
        for number, item in enumerate(db.iterate_records_raw()):
            if number == MAX_RECORDS:
                break
            try:
                value = {"json": _safe_value(json.loads(item.value))}
            except (ValueError, UnicodeError):
                value = {"size": len(item.value), "sha256": hashlib.sha256(item.value).hexdigest(),
                         "omitted": "unstructured LevelDB value"}
            records.append({"key": _safe_value(item.key), "value": value,
                            "sequence_number": item.seq, "state": item.state.name,
                            "origin_file": Path(item.origin_file).name, "offset": item.offset})
    finally:
        db.close()
    return {"data": {"records": records, "truncated": len(records) == MAX_RECORDS},
            "status": "partial" if len(records) == MAX_RECORDS else "ok"}


def _write_members(archive: zipfile.ZipFile, records: list[dict[str, Any]], root: Path) -> None:
    for record in records:
        name = record["collected_path"]
        parts = PurePosixPath(name).parts
        if (not name or name.startswith("/") or not parts or len(parts[0]) != 1
                or not parts[0].isalpha() or ".." in parts or any(":" in part for part in parts)):
            raise ValueError(f"unsafe archive member: {name}")
        destination = root.joinpath(*parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with archive.open(name) as source, destination.open("wb") as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
                digest.update(chunk)
        if record.get("sha256") and digest.hexdigest() != record["sha256"]:
            raise ValueError(f"archive member hash does not match collection manifest: {name}")


def _parser_type(definitions: list[ArtifactDefinition], member: str) -> str:
    if member.endswith(("-wal", "-shm")):
        return "sqlite_companion"
    types = {definition.artifact_type for definition in definitions}
    if types == {"directory"}:
        suffix = PurePosixPath(member).suffix.lower()
        return {".json": "json", ".jsonl": "jsonl", ".toml": "toml",
                ".sqlite": "sqlite", ".db": "sqlite", ".log": "log",
                ".txt": "log", ".md": "log"}.get(suffix, "file_metadata")
    for kind in ("chromium_cache", "leveldb", "session_storage", "sqlite", "chromium_history",
                 "chromium_cookies", "registry_key", "registry_hive", "prefetch", "ntfs_metadata",
                 "jsonl", "json", "toml", "log", "directory"):
        if kind in types:
            return kind
    return next(iter(types), "file_metadata")


def _group_key(kind: str, member: str) -> str:
    if kind == "sqlite_companion":
        return member.rsplit("-", 1)[0]
    if kind in GROUP_TYPES:
        return str(PurePosixPath(member).parent)
    return member


def _records_and_details(kind: str, data: Any) -> tuple[list[Any], dict[str, Any]]:
    """Expose observations as records while retaining format metadata separately."""
    if data is None:
        return [], {}
    if kind in {"json", "toml"}:
        return data if isinstance(data, list) else [data], {}
    field = {
        "jsonl": "records", "log": "lines", "chromium_cache": "entries",
        "leveldb": "records", "session_storage": "records",
        "registry_key": "keys", "registry_hive": "keys", "ntfs_metadata": "entries",
        "sqlite": "tables", "chromium_history": "tables", "chromium_cookies": "tables",
    }.get(kind)
    if field and isinstance(data, dict) and field in data:
        return data[field], {key: value for key, value in data.items() if key != field}
    return [data], {}


def parse_artifacts(archive_path: Path, output_path: Path,
                    definitions: tuple[ArtifactDefinition, ...] = ARTIFACT_DEFINITIONS) -> dict[str, int]:
    """Parse each collected file or coherent store once and write one JSON file."""
    if output_path.exists():
        raise FileExistsError(f"parsed output already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lookup = {item.id: item for item in definitions}
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("_collection_manifest.json"))
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        incomplete_cache: dict[str, list[str]] = defaultdict(list)
        failed = []
        for record in manifest["files"]:
            if record["status"] != "ok":
                failed.append(record)
                if any(lookup[item].artifact_type == "chromium_cache"
                       for item in record["definition_ids"] if item in lookup):
                    source = PureWindowsPath(record["source_path"])
                    if len(source.drive) == 2 and source.drive[1] == ":":
                        parent = str(PurePosixPath(source.drive[0].upper(), *source.parts[1:-1]))
                        incomplete_cache[parent].append(source.name)
                continue
            applicable = [lookup[item] for item in record["definition_ids"] if item in lookup]
            kind = _parser_type(applicable, record["collected_path"])
            grouped[_group_key(kind, record["collected_path"])].append(record)

        results = []
        for record in failed:
            results.append(_result_record(record, lookup, "none", "collection_failed", None,
                                          record.get("error", "collection failed")))
        for group_key, group in grouped.items():
            main = next((r for r in group if not r["collected_path"].endswith(("-wal", "-shm"))), group[0])
            applicable = [lookup[item] for item in main["definition_ids"] if item in lookup]
            kind = _parser_type(applicable, main["collected_path"])
            parser_name = PARSER_NAMES.get(kind, "file metadata parser")
            if kind == "chromium_cache" and group_key in incomplete_cache:
                result = _result_record(
                    main, lookup, parser_name, "collection_failed", None,
                    "incomplete cache collection; failed members: "
                    + ", ".join(sorted(incomplete_cache[group_key])), kind,
                    [r["collected_path"] for r in group])
                result["collection_status"] = "incomplete"
                results.append(result)
                continue
            try:
                with tempfile.TemporaryDirectory(dir=output_path.parent) as temporary:
                    root = Path(temporary)
                    _write_members(archive, group, root)
                    path = root.joinpath(*PurePosixPath(main["collected_path"]).parts)
                    if kind in SQLITE_TYPES:
                        parsed = _parse_sqlite(path)
                        parser_name = "SQLite parser"
                    elif kind == "json":
                        parsed = _parse_json(path.read_bytes()); parser_name = "JSON parser"
                    elif kind == "jsonl":
                        parsed = _parse_jsonl(path.read_bytes()); parser_name = "JSONL parser"
                    elif kind == "toml":
                        parsed = _parse_toml(path.read_bytes()); parser_name = "TOML parser"
                    elif kind == "log":
                        parsed = _parse_text(path.read_bytes()); parser_name = "Text/Log parser"
                    elif kind in {"registry_key", "registry_hive"}:
                        parsed = _parse_registry(path, applicable); parser_name = "python-registry"
                    elif kind == "prefetch":
                        parsed = _parse_prefetch(path); parser_name = "pyscca"
                    elif kind == "ntfs_metadata":
                        parsed = _parse_mft(path); parser_name = "mft"
                    elif kind == "chromium_cache":
                        parser_name = "ccl_chromium_reader cache + byte marker scanner"
                        marker_matches = _scan_cache_file_markers(root, group)
                        try:
                            parsed = _parse_cache(path.parent)
                        except Exception as cache_exc:
                            if not marker_matches:
                                raise
                            parsed = {
                                "status": "partial",
                                "data": {"entries": [], "truncated": False,
                                         "errors": [f"Chromium cache parser: {type(cache_exc).__name__}: {cache_exc}"]},
                            }
                        parsed["data"]["file_marker_matches"] = marker_matches
                        parsed["data"]["entries"].extend({
                            "record_type": "cache_file_marker",
                            **match,
                        } for match in marker_matches)
                    elif kind in {"leveldb", "session_storage"}:
                        parsed = _parse_leveldb(path.parent); parser_name = "ccl_chromium_reader LevelDB"
                    elif kind == "sqlite_companion":
                        parsed = {"status": "unsupported", "data": None,
                                  "error": "SQLite sidecar collected without its main database"}
                        parser_name = "SQLite parser"
                    else:
                        parsed = _parse_metadata(main); parser_name = "file metadata parser"
                results.append(_result_record(main, lookup, parser_name, parsed.get("status", "ok"),
                                              parsed.get("data"), parsed.get("error"), kind,
                                              [r["collected_path"] for r in group],
                                              parsed.get("raw_timestamp")))
            except Exception as exc:  # malformed evidence must not stop other files
                results.append(_result_record(main, lookup, parser_name, "failed", None,
                                              f"{type(exc).__name__}: {exc}", kind,
                                              [r["collected_path"] for r in group]))
    payload = {"schema_version": "1.1", "source": manifest["source"],
               "collection_manifest": "_collection_manifest.json", "artifacts": results}
    staged = output_path.with_name(output_path.name + ".partial")
    try:
        staged.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(staged, output_path)
    finally:
        staged.unlink(missing_ok=True)
    return {status: sum(item["status"] == status for item in results)
            for status in ("success", "partial", "failed", "collection_failed")}


def _result_record(record: dict[str, Any], definitions: dict[str, ArtifactDefinition],
                   parser_name: str, status: str, data: Any, error: str | None,
                   kind: str = "file_metadata",
                   members: list[str] | None = None,
                   parsed_timestamps: dict[str, Any] | None = None) -> dict[str, Any]:
    applicable = [definitions[item] for item in record["definition_ids"] if item in definitions]
    raw_mtime = record.get("mtime_ns_before")
    timestamps = {"source_mtime_ns": raw_mtime} if raw_mtime is not None else {}
    timestamps.update(parsed_timestamps or {})
    normalized_status = {"ok": "success", "unsupported": "failed"}.get(status, status)
    records, details = _records_and_details(kind, data)
    result = {"artifact": applicable[0].artifact if applicable else None,
            "artifact_names": list(dict.fromkeys(item.artifact for item in applicable)),
            "definition_ids": record["definition_ids"], "source_path": record["source_path"],
            "collected_path": record.get("collected_path"), "collected_members": members or [],
            "collection_status": record["status"], "parser": parser_name,
            "collection_method": record.get("collection_method"),
            "status": normalized_status,
            "raw_timestamp": timestamps or None,
            "normalized_timestamp": None}
    if normalized_status in {"success", "partial"}:
        result["records"] = records
        if details:
            result["parse_details"] = details
    if error or normalized_status == "failed":
        result["error"] = error or "parser did not return a result"
    return result
