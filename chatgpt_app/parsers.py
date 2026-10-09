"""Format-based parsers for collected ChatGPT Desktop evidence.

Only ZIP members are parsed. A definition describes where to look and the
user-provided evidence grade; a parsed file alone never establishes an action.
"""

from __future__ import annotations

import base64
import bisect
import csv
import codecs
import fnmatch
import hashlib
import io
import json
import os
import re
import sqlite3
import struct
import tempfile
import tomllib
import zipfile
from collections import defaultdict
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
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
    "memory_percentage_used": b"memory_percentage_used",
    "memcite": b"memcite",
    "multimodal_text": b"multimodal_text",
    "다운로드]": "다운로드]".encode("utf-8"),
}
PARSER_NAMES = {
    "json": "JSON parser", "jsonl": "JSONL parser", "toml": "TOML parser",
    "log": "Text/Log parser", "sqlite": "SQLite parser",
    "chromium_history": "SQLite parser", "chromium_cookies": "SQLite parser",
    "sqlite_companion": "SQLite parser", "registry_key": "python-registry",
    "registry_hive": "python-registry", "prefetch": "pyscca", "ntfs_metadata": "mft",
    "chromium_cache": "ccl_chromium_reader.ccl_chromium_cache",
    "leveldb": "ccl_chromium_reader LevelDB",
    "session_storage": "ccl_chromium_reader LevelDB",
}


def _safe_value(value: Any, *, name: str = "") -> Any:
    """Preserve typed values where practical; omit secrets rather than altering them."""
    if isinstance(value, dict):
        return {str(key): _safe_value(item, name=str(key)) for key, item in value.items()}
    if any(word in name.casefold() for word in SECRET_NAMES):
        result: dict[str, Any] = {"omitted": "sensitive field"}
        if name.casefold() in {"id_token", "access_token"}:
            result["present"] = isinstance(value, str) and bool(value.strip())
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
    if (name.casefold() == "url" and isinstance(value, str)
            and "http://localhost:1455/auth/callback?code=" in value):
        # OAuth authorization codes are credentials. Their existence and URL
        # structure remain observable; the original is retained only in ZIP.
        value = re.sub(r"(?i)([?&](?:code|state)=)[^&#]*", r"\1<redacted>",
                       value.split("#", 1)[0])
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
    text = _decode(raw)
    value = json.loads(text)
    return {"data": _safe_value(value),
            "source_locations": _json_source_locations(text, isinstance(value, list))}


def _json_source_locations(text: str, root_is_list: bool) -> list[dict[str, Any]]:
    """Index source value positions without copying their potentially secret values.

    Lines and Unicode character columns are 1-based. Duplicate object keys use
    the final occurrence, matching json.loads. Paths refer to parsed records.
    """
    decoder = json.JSONDecoder()
    starts = [0] + [match.end() for match in re.finditer(r"\r\n|\r|\n", text)]
    positions: dict[tuple[Any, ...], int] = {}

    def skip(index):
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        return index

    def visit(index, path):
        index = skip(index)
        positions[path] = index
        if text[index] == "{":
            index = skip(index + 1)
            while text[index] != "}":
                key, index = decoder.raw_decode(text, index)
                index = skip(index)
                child = (*path, key)
                # Remove descendants of a replaced duplicate key as well.
                if child in positions:
                    for previous in list(positions):
                        if previous[:len(child)] == child:
                            del positions[previous]
                index = skip(visit(index + 1, child))
                if text[index] == ",":
                    index = skip(index + 1)
                else:
                    break
            return index + 1
        if text[index] == "[":
            index = skip(index + 1)
            number = 0
            while text[index] != "]":
                index = skip(visit(index, (*path, number)))
                number += 1
                if text[index] == ",":
                    index = skip(index + 1)
                else:
                    break
            return index + 1
        return decoder.raw_decode(text, index)[1]

    visit(0, ())
    locations = []
    for path, index in positions.items():
        record_path = path if root_is_list else (0, *path)
        if not record_path:
            continue
        line = bisect.bisect_right(starts, index)
        locations.append({"record_path": list(record_path), "line": line,
                          "column": index - starts[line - 1] + 1})
    return locations


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
                "source_locations": [
                    {"record_path": [ti, "rows", ri],
                     "source_locator": f"table={table['name']};row={ri + 1}",
                     "kind": "logical", "row_basis": "1-based SELECT order, not SQLite rowid"}
                    for ti, table in enumerate(tables) for ri in range(len(table["rows"]))],
                "status": "partial" if any(t["truncated"] for t in tables) else "ok"}


def _decode_userassist(name: str, raw: Any) -> dict[str, Any]:
    """Decode modern UserAssist value fields; keep the original value separately.

    Layout reference: EricZimmerman/RegistryPlugins,
    RegistryPlugin.UserAssist/UserAssist.cs, ProcessKeys (Windows 7+ branch).
    Offsets are relative to registry value data, NOT the hive file.
    """
    result = {"program_name": codecs.decode(name, "rot_13"),
              "name_encoding": "ROT13", "status": "unsupported"}
    if not isinstance(raw, bytes) or len(raw) < 68:
        result["error"] = "modern UserAssist requires REG_BINARY data of at least 68 bytes"
        return result
    run_count, focus_count, focus_time = struct.unpack_from("<iii", raw, 4)
    filetime = struct.unpack_from("<Q", raw, 60)[0]
    result.update({"status": "success", "format": "Windows 7+ UserAssist layout",
                   "run_counter": run_count, "focus_count": focus_count,
                   "focus_time_ms": focus_time, "last_executed_filetime": filetime,
                   "last_executed_utc": None, "last_executed_kst": None,
                   "value_data_offsets": {"run_counter": 4, "focus_count": 8,
                       "focus_time_ms": 12, "last_executed_filetime": 60}})
    if filetime == 0:
        result["timestamp_status"] = "not_recorded"
    else:
        try:
            utc = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=filetime // 10)
            result["last_executed_utc"] = utc.isoformat()
            result["last_executed_kst"] = utc.astimezone(timezone(timedelta(hours=9))).isoformat()
            result["timestamp_status"] = "success"
            result["normalization_precision"] = "microseconds; original FILETIME retains 100ns precision"
        except (OverflowError, ValueError):
            result["status"] = "partial"
            result["timestamp_status"] = "invalid"
            result["error"] = "FILETIME outside supported datetime range"
    return result


def _registry_mru_order(raw: Any) -> dict[str, Any]:
    """Read MRUListEx DWORDs without interpreting shell items."""
    if not isinstance(raw, bytes) or len(raw) % 4:
        return {"status": "failed", "error": "MRUListEx is not DWORD-aligned"}
    numbers = [item[0] for item in struct.iter_unpack("<i", raw)]
    if -1 not in numbers:
        return {"status": "partial", "order": numbers, "error": "MRUListEx has no terminator"}
    end = numbers.index(-1)
    result = {"status": "success" if end == len(numbers) - 1 else "partial",
              "order": numbers[:end], "terminator": -1}
    if end != len(numbers) - 1:
        result["error"] = "data follows MRUListEx terminator"
    return result


def _registry_shell_items(raw: Any, start: int) -> dict[str, Any]:
    """Validate item boundaries without guessing shell item names or paths."""
    if not isinstance(raw, bytes) or start > len(raw):
        return {"status": "unsupported", "error": "shell item bytes unavailable"}
    items = []
    cursor = start
    while cursor + 2 <= len(raw):
        size = struct.unpack_from("<H", raw, cursor)[0]
        if size == 0:
            result = {"status": "success" if cursor + 2 == len(raw) else "partial",
                      "items": items, "terminator_offset": cursor}
            if cursor + 2 != len(raw):
                result["error"] = "bytes follow shell item terminator"
            result.update(_decode_shell_path(raw[start:cursor + 2]))
            return result
        if size < 3 or cursor + size > len(raw):
            return {"status": "partial", "items": items,
                    "error": f"invalid shell item size at value offset {cursor}"}
        items.append({"value_offset": cursor, "size": size, "type_byte": raw[cursor + 2]})
        cursor += size
    return {"status": "partial", "items": items, "error": "shell item terminator missing"}


def _decode_shell_path(raw: bytes) -> dict[str, Any]:
    """Decode PIDLs with libfwsi; report only a complete, observed absolute path."""
    try:
        import pyfwsi
        items = pyfwsi.item_list()
        items.copy_from_byte_stream(raw)
        components = []
        resolved = None
        complete = True
        for item in items.items:
            component = {"type": type(item).__name__}
            if isinstance(item, pyfwsi.root_folder):
                component["guid"] = str(item.shell_folder_identifier)
                # Namespace roots are retained, not expanded to a guessed user directory.
                if component["guid"].strip("{}").lower() != "20d04fe0-3aea-1069-a2d8-08002b30309d":
                    complete = False
            elif isinstance(item, pyfwsi.volume):
                name = item.name
                component["name"] = name
                if isinstance(name, str) and PureWindowsPath(name).is_absolute():
                    resolved = PureWindowsPath(name)
                else:
                    complete = False
            elif isinstance(item, pyfwsi.file_entry):
                name = item.name
                for extension in item.extension_blocks:
                    if isinstance(extension, pyfwsi.file_entry_extension) and extension.long_name:
                        name = extension.long_name
                        break
                component["name"] = name
                if resolved is not None and isinstance(name, str) and name and name not in {".", ".."} and not any(c in name for c in "\\/:"):
                    resolved /= name
                else:
                    complete = False
            else:
                complete = False
            components.append(component)
        result = {"path_parser": "pyfwsi", "path_components": components,
                  "path_status": "success" if complete and resolved is not None else "partial"}
        if complete and resolved is not None:
            result["absolute_path"] = str(resolved)
        return result
    except (ImportError, OSError, ValueError, TypeError, AttributeError, RuntimeError) as exc:
        return {"path_status": "failed", "path_error": f"{type(exc).__name__}: {exc}"}


def _registry_last_visited(raw: Any) -> dict[str, Any]:
    """Decode the leading UTF-16LE executable; preserve shell item structure."""
    if not isinstance(raw, bytes):
        return {"status": "unsupported", "error": "LastVisitedPidlMRU is not REG_BINARY"}
    end = next((offset for offset in range(0, len(raw) - 1, 2)
                if raw[offset:offset + 2] == b"\0\0"), None)
    if end is None:
        return {"status": "partial", "error": "UTF-16LE executable terminator missing"}
    try:
        executable = raw[:end].decode("utf-16le")
    except UnicodeDecodeError:
        return {"status": "partial", "error": "invalid UTF-16LE executable"}
    items = _registry_shell_items(raw, end + 2)
    result = {"status": items["status"], "executable": executable,
              "executable_value_offset": 0, "shell_items": items}
    if "absolute_path" in items:
        result["absolute_path"] = items["absolute_path"]
    return result


def _registry_record_details(definition_id: str, entry: dict[str, Any]) -> None:
    """Add verified registry fields, with no user-action classification."""
    path = PureWindowsPath(entry["path"])
    if definition_id == "install_appx" and path.name.casefold().startswith("openai.codex_"):
        entry["appx"] = {"package_name": path.name, "sid": path.parent.name,
                         "key_last_write_iso": entry["last_written_iso_from_parser"]}
    elif definition_id == "run_amcache" and path.parent.name.casefold() == "inventoryapplicationfile":
        name = next((value["value"] for value in entry["values"]
                     if value["name"].casefold() == "name" and isinstance(value["value"], str)), None)
        entry["amcache_inventory_application_file"] = {
            "name": name, "key_last_write_iso": entry["last_written_iso_from_parser"]}


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
            values = []
            order = None
            if definition.id in {"upload_open_mru", "download_open_mru",
                                 "upload_visited_mru", "download_visited_mru"}:
                mru_value = next((value for value in key.values()
                                  if value.name().casefold() == "mrulistex"), None)
                if mru_value is not None:
                    order = _registry_mru_order(mru_value.value())
            for value in key.values():
                name, raw = value.name(), value.value()
                item = {"name": name, "value_type": value.value_type_str(),
                        "value": _safe_value(raw, name=name)}
                if (definition.id == "run_userassist" and key.name().casefold() == "count"
                        and item["value_type"] == "RegBin"):
                    item["userassist"] = _decode_userassist(name, raw)
                if order is not None and name.casefold() == "mrulistex":
                    item["mru_list_ex"] = order
                elif (definition.id in {"upload_open_mru", "download_open_mru",
                                        "upload_visited_mru", "download_visited_mru"}
                      and name.isdecimal()):
                    item["mru_position"] = (order["order"].index(int(name))
                                            if order and int(name) in order.get("order", ()) else None)
                    if definition.id in {"upload_visited_mru", "download_visited_mru"}:
                        item["last_visited_pidl_mru"] = _registry_last_visited(raw)
                    else:
                        item["open_save_pidl_mru"] = _registry_shell_items(raw, 0)
                    detail_key = ("last_visited_pidl_mru" if "last_visited_pidl_mru" in item
                                  else "open_save_pidl_mru")
                    if item["mru_position"] == 0 and order.get("status") == "success":
                        item[detail_key]["opened_on"] = key.timestamp().isoformat()
                        item[detail_key]["opened_on_basis"] = "key last write; MRUListEx rank 0 only"
                values.append(item)
            entry = {"path": key.path(), "last_written_iso_from_parser": key.timestamp().isoformat(),
                     "values": values}
            _registry_record_details(definition.id, entry)
            observed.append(entry)
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
                         "last_run_filetime": times,
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

    # Use the pinned CCL module's official export path on the collected copy.
    # Its cache_report.csv links decoded response bodies by their SHA-256 hash.
    with tempfile.TemporaryDirectory(dir=directory.parent) as temporary:
        exported = Path(temporary) / "ccl_output"
        messages = io.StringIO()
        with redirect_stdout(messages), redirect_stderr(messages):
            cache.main([str(directory), str(exported)])
        report = exported / "cache_report.csv"
        if not report.is_file():
            raise ValueError("ccl_chromium_cache did not create cache_report.csv")
        entries = []
        with report.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if len(entries) == MAX_RECORDS:
                    break
                key = row.get("key") or ""
                if any(word in key.casefold() for word in SECRET_NAMES) or "?" in key or "#" in key:
                    safe_key = {"sha256": hashlib.sha256(key.encode("utf-8")).hexdigest(),
                                "omitted": "sensitive cache key"}
                else:
                    safe_key = _safe_value(key, name="cache_key")
                metadata = {name: _safe_value(value, name=name)
                            for name, value in row.items() if name not in {"key", "file_hash"}}
                file_hash = row.get("file_hash") or ""
                body = None
                if re.fullmatch(r"[0-9a-f]{64}", file_hash):
                    matches = list((exported / "cache_files").glob(file_hash + ".*"))
                    matches.extend((exported / "cache_files").glob(file_hash))
                    if len(matches) != 1:
                        raise ValueError(f"CCL cache body lookup is ambiguous or missing: {file_hash}")
                    raw = matches[0].read_bytes()
                    if hashlib.sha256(raw).hexdigest() != file_hash:
                        raise ValueError(f"CCL cache body hash mismatch: {file_hash}")
                    try:
                        body = {"size": len(raw), "json": _safe_value(json.loads(raw))}
                    except (ValueError, UnicodeError):
                        body = {"size": len(raw), "sha256": file_hash,
                                "omitted": "non-JSON cache body"}
                entries.append({"key": safe_key, "file_hash": file_hash,
                                "metadata": metadata, "bodies": [body]})
        warnings = len(messages.getvalue().splitlines())
        truncated = len(entries) == MAX_RECORDS
        unindexed_data = not entries and any(
            item.name.startswith(("f_", "data_")) and item.stat().st_size
            for item in directory.iterdir() if item.is_file()
        )
        details = {"entries": entries, "truncated": truncated,
                   "ccl_warnings": warnings}
        if unindexed_data:
            details["errors"] = ["CCL found no indexed entries in a cache with nonempty data files"]
        return {"data": details,
                "source_locations": [
                    {"record_path": [i], "kind": "logical",
                     "source_locator": "key=" + json.dumps(entry["key"], ensure_ascii=False)
                         + f";export_row={i + 1}",
                     "row_basis": "1-based CCL export order; native version and physical offset unavailable"}
                    for i, entry in enumerate(entries)],
                "status": "partial" if truncated or warnings or unindexed_data else "ok"}


def _scan_cache_file_markers(root: Path, group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Record exact UTF-8 byte hits per collected file, without interpreting actions."""
    matches: list[dict[str, Any]] = []
    overlap = 8192
    for member in group:
        path = root.joinpath(*PurePosixPath(member["collected_path"]).parts)
        found: set[str] = set()
        has_children = False
        has_logout_path = False
        has_logout_reason = False
        has_logout_source = False
        has_account_user_id = False
        has_created_time = False
        tail = b""
        consumed = 0
        positions: dict[str, dict[str, int]] = {}
        patterns = {
            **{label: re.escape(needle) for label, needle in CACHE_FILE_MARKERS.items()},
            "account_user_id": rb"account_user_id",
            "created_time": rb"created_time",
            "title_then_create_time": rb'"title"\s*:\s*"(?:\\.|[^"\\])*"\s*,\s*"create_time"\s*:',
            "children": rb'"children"\s*:\s*\[\s*"[^"\]]+"\s*,\s*"[^"\]]+"',
            "title_new_chat": rb'"title"\s*:\s*"New Chat"',
            "logout_path": rb"/auth/logout",
            "logout_reason": rb'"logout_reason"\s*:\s*"confirm_delete_account"',
            "logout_source": rb'"logout_source"\s*:\s*"settings\.confirm-delete-account-modal"',
        }
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                data = tail + chunk
                base_offset = consumed - len(tail)
                for label, pattern in patterns.items():
                    if label not in positions and (match := re.search(pattern, data)):
                        positions[label] = {"byte_offset": base_offset + match.start(),
                                            "byte_length": match.end() - match.start()}
                consumed += len(chunk)
                found.update(label for label, needle in CACHE_FILE_MARKERS.items() if needle in data)
                has_account_user_id |= b"account_user_id" in data
                has_created_time |= b"created_time" in data
                if re.search(rb'"title"\s*:\s*"(?:\\.|[^"\\])*"\s*,\s*"create_time"\s*:', data):
                    found.add("title_then_create_time")
                if re.search(rb'"children"\s*:\s*\[\s*"[^"\]]+"\s*,\s*"[^"\]]+"', data):
                    has_children = True
                if re.search(rb'"title"\s*:\s*"New Chat"', data):
                    found.add("title_new_chat")
                has_logout_path |= b"/auth/logout" in data
                has_logout_reason |= bool(re.search(
                    rb'"logout_reason"\s*:\s*"confirm_delete_account"', data
                ))
                has_logout_source |= bool(re.search(
                    rb'"logout_source"\s*:\s*"settings\.confirm-delete-account-modal"', data
                ))
                tail = data[-overlap:]
        if "create_time" in found and has_children:
            found.add("prompt_edit_children")
        if has_account_user_id and has_created_time:
            found.add("account_user_id_and_created_time")
        if has_logout_path and has_logout_reason and has_logout_source:
            found.add("confirm_delete_account")
        if found:
            components = {
                "account_user_id_and_created_time": ("account_user_id", "created_time"),
                "prompt_edit_children": ("create_time", "children"),
                "confirm_delete_account": ("logout_path", "logout_reason", "logout_source"),
            }
            matches.append({
                "source_path": member["source_path"],
                "collected_path": member["collected_path"],
                "matched_markers": [label for label in (
                    *CACHE_FILE_MARKERS, "title_then_create_time",
                    "account_user_id_and_created_time", "prompt_edit_children",
                    "title_new_chat", "confirm_delete_account"
                ) if label in found],
                "marker_locations": {
                    label: [{"component": component, **positions[component]}
                            for component in components.get(label, (label,))
                            if component in positions]
                    for label in sorted(found)
                },
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
                        parser_name = "ccl_chromium_reader.ccl_chromium_cache + byte marker scanner"
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
                                              parsed.get("raw_timestamp"),
                                              parsed.get("source_locations")))
            except Exception as exc:  # malformed evidence must not stop other files
                results.append(_result_record(main, lookup, parser_name, "failed", None,
                                              f"{type(exc).__name__}: {exc}", kind,
                                              [r["collected_path"] for r in group]))
    payload = {"schema_version": "1.2", "source": manifest["source"],
               "collection_manifest": "_collection_manifest.json", "artifacts": results}
    staged = output_path.with_name(output_path.name + ".partial")
    try:
        staged.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(staged, output_path)
    finally:
        staged.unlink(missing_ok=True)
    return {status: sum(item["status"] == status for item in results)
            for status in ("success", "partial", "failed", "collection_failed")}



def _source_location_index(records: list[Any], kind: str,
                           locations: list[dict[str, Any]], source_path: str) -> list[dict[str, Any]]:
    """Store provenance outside observed records so it cannot affect matching."""
    result = []

    def add(path, locator, category, basis, original=source_path):
        result.append({"record_path": list(path), "_source_locator": locator,
                       "kind": category, "basis": basis, "source_path": original})

    for location in locations:
        path = location.get("record_path", [])
        if "source_locator" in location:
            add(path, location["source_locator"], location.get("kind", "logical"),
                location.get("row_basis", "parser logical location"),
                str(PureWindowsPath(source_path).parent) if kind == "chromium_cache" else source_path)
        elif type(location.get("line")) is int and type(location.get("column")) is int:
            add(path, f"line={location['line']};column={location['column']}", "text",
                "1-based line and character column; original value before decoding/redaction")

    for i, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        if record.get("record_type") == "cache_file_marker":
            for j, marker in enumerate(record.get("matched_markers", [])):
                for position in record.get("marker_locations", {}).get(marker, []):
                    offset = position.get("byte_offset")
                    if type(offset) is int and offset >= 0:
                        add((i, "matched_markers", j), f"offset={offset}", "byte",
                            f"0-based byte; component={position.get('component', marker)}; "
                            f"length={position.get('byte_length')} bytes",
                            record.get("source_path", source_path))
        elif kind in {"log", "jsonl"} and type(record.get("line")) is int:
            add((i,), f"line={record['line']}", "text", "1-based original line")
        elif kind == "prefetch" and "last_run_filetime" in record:
            add((i, "last_run_filetime"), "field=last_run_time", "logical",
                "pyscca last run time field; physical offset unavailable")
        elif "origin_file" in record and type(record.get("offset")) is int:
            add((i,), f"file={record['origin_file']};offset={record['offset']}", "parser",
                "parser-reported offset; physical byte basis not verified")
        elif kind in {"registry_hive", "registry_key"}:
            for j, entry in enumerate(record.get("records", [])):
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    add((i, "records", j), "key=" + entry["path"], "logical",
                        "registry key path; physical offset unavailable")
                    for k, value in enumerate(entry.get("values", [])):
                        if isinstance(value, dict) and isinstance(value.get("name"), str):
                            add((i, "records", j, "values", k),
                                "key=" + entry["path"] + ";value=" + value["name"], "logical",
                                "registry key and value name; physical offset unavailable")
                            for detail in ("open_save_pidl_mru", "last_visited_pidl_mru"):
                                if isinstance(value.get(detail), dict) and "opened_on" in value[detail]:
                                    add((i, "records", j, "values", k, detail, "opened_on"),
                                        "key=" + entry["path"] + ";field=last_write_time", "logical",
                                        "key last write assigned to MRUListEx rank 0 only")
    return result


def _result_record(record: dict[str, Any], definitions: dict[str, ArtifactDefinition],
                   parser_name: str, status: str, data: Any, error: str | None,
                   kind: str = "file_metadata",
                   members: list[str] | None = None,
                   parsed_timestamps: dict[str, Any] | None = None,
                   source_locations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    applicable = [definitions[item] for item in record["definition_ids"] if item in definitions]
    raw_mtime = record.get("mtime_ns_before")
    timestamps = {"source_mtime_ns": raw_mtime} if raw_mtime is not None else {}
    timestamps.update(parsed_timestamps or {})
    normalized_status = {"ok": "success", "unsupported": "failed"}.get(status, status)
    records, details = _records_and_details(kind, data)
    result = {"artifact": applicable[0].artifact if applicable else record.get("artifact_name"),
            "artifact_names": list(dict.fromkeys(item.artifact for item in applicable)),
            "definition_ids": record["definition_ids"], "source_path": record["source_path"],
            "collection_role": record.get("collection_role", "primary"),
            "companion_for": record.get("companion_for", []),
            "collected_path": record.get("collected_path"), "collected_members": members or [],
            "collection_status": record["status"], "parser": parser_name,
            "collection_method": record.get("collection_method"),
            "status": normalized_status,
            "raw_timestamp": timestamps or None,
            "normalized_timestamp": None}
    if normalized_status in {"success", "partial"}:
        result["records"] = records
        result["source_location"] = _source_location_index(
            records, kind, source_locations or [], record["source_path"])
        if source_locations:
            result["source_locations"] = source_locations
        if details:
            result["parse_details"] = details
    if error or normalized_status == "failed":
        result["error"] = error or "parser did not return a result"
    return result
