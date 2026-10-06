"""Explicit action conditions and user-defined behavior/artifact/grade links."""

from __future__ import annotations

import base64
import binascii
import codecs
import struct
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlparse

from artifact_definitions import ARTIFACT_DEFINITIONS, ArtifactDefinition, Grade


CSV_COLUMNS = ("행위 분류", "아티팩트", "경로", "아티팩트 등급")


@dataclass(frozen=True)
class ActionRule:
    definition_id: str
    behavior: str
    artifact: str
    grade: Grade


@dataclass(frozen=True)
class ConfirmedObservation:
    """A separate action detector's claim, anchored to a parsed record.

    `record_path` starts with the index in the artifact's `records` array.
    Nested keys and indices can identify a SQLite row or another nested item.
    `basis` records the verified condition; it is not inferred by this module.
    """

    definition_id: str
    collected_path: str
    record_path: tuple[str | int, ...]
    basis: str


@dataclass(frozen=True)
class RuleResult:
    rows: tuple[dict[str, str], ...]
    rejected: tuple[str, ...]


def rules_from_definitions(
    definitions: Sequence[ArtifactDefinition] = ARTIFACT_DEFINITIONS,
) -> tuple[ActionRule, ...]:
    """Copy the user's explicit behavior/grade combinations without inference."""
    seen: set[str] = set()
    rules: list[ActionRule] = []
    for definition in definitions:
        if definition.id in seen:
            raise ValueError(f"duplicate action rule ID: {definition.id}")
        if definition.grade not in ("직접", "보조"):
            raise ValueError(f"invalid evidence grade for {definition.id}: {definition.grade}")
        seen.add(definition.id)
        rules.append(ActionRule(definition.id, definition.behavior,
                                definition.artifact, definition.grade))
    return tuple(rules)


ACTION_RULES = rules_from_definitions()
DETECTABLE_RULE_IDS = frozenset({
    "install_prefetch", "install_appx", "run_prefetch", "run_userassist", "run_global_state", "login_auth",
    "login_codex_db", "login_cookies", "login_history", "login_cache", "chat_codex_db",
    "switch_codex_db", "chat_cache", "prompt_edit_cache", "memory_cache",
    "upload_cache", "download_cache", "temporary_chat_cache", "connector_log",
})

CACHE_MARKER_RULES: dict[str, tuple[str, ...]] = {
    "create_time": ("chat_cache", "prompt_edit_cache"),
    "user-": ("login_cache",),
    "memory_percentage_used": ("memory_cache",),
    "memcite": ("memory_cache",),
    "multimodal_text": ("upload_cache",),
    "다운로드]": ("download_cache",),
    "New Chat": ("temporary_chat_cache",),
}


def rows_without_conditions(
    collection_manifest: Mapping[str, Any],
    rules: Sequence[ActionRule] = ACTION_RULES,
    conditional_ids: frozenset[str] = DETECTABLE_RULE_IDS,
) -> tuple[dict[str, str], ...]:
    """Map successfully extracted files for definitions without a detector.

    These are inventory-based evidence rows, not confirmed action observations.
    The caller merges them with detector rows before preparing both reports.
    """
    by_id = {rule.definition_id: rule for rule in rules}
    rows: list[dict[str, str]] = []
    for file in collection_manifest.get("files", []):
        if not isinstance(file, dict) or file.get("status") != "ok":
            continue
        source_path = file.get("source_path")
        if not isinstance(source_path, str) or not source_path or not file.get("collected_path"):
            continue
        for definition_id in dict.fromkeys(file.get("definition_ids") or []):
            if definition_id in conditional_ids:
                continue
            rule = by_id.get(definition_id)
            if rule is None:
                continue
            rows.append(dict(zip(CSV_COLUMNS, (
                rule.behavior, rule.artifact, source_path, rule.grade,
            ))))
    return tuple(rows)


def _windows_profile(path: str) -> str | None:
    parts = PureWindowsPath(path).parts
    if len(parts) >= 3 and parts[1].casefold() == "users":
        return str(PureWindowsPath(*parts[:3])).casefold()
    return None


def _positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _parsed_tables(artifact: Mapping[str, Any], table_name: str):
    for index, table in enumerate(artifact.get("records", [])):
        if isinstance(table, dict) and table.get("name") == table_name:
            for row_index, row in enumerate(table.get("rows", [])):
                if isinstance(row, dict):
                    yield index, row_index, row


def detect_confirmed_observations(
    parsed_artifacts: Mapping[str, Any],
) -> tuple[ConfirmedObservation, ...]:
    """Apply narrow, research-backed field conditions to parsed observations.

    Unsupported artifacts remain unreported. A path match, nonempty table,
    cache remnant, or missing record never establishes an action on its own.
    JWT claims are decoded metadata; their signatures are not verified here.
    """
    artifacts = [item for item in parsed_artifacts.get("artifacts", [])
                 if isinstance(item, dict) and item.get("status") in {"success", "partial"}
                 and isinstance(item.get("records"), list) and item.get("collected_path")]
    observations: list[ConfirmedObservation] = []
    authenticated_accounts: dict[str, set[str]] = {}

    def add(artifact: Mapping[str, Any], definition_id: str,
            record_path: tuple[str | int, ...], basis: str) -> None:
        if definition_id in artifact.get("definition_ids", ()):
            observations.append(ConfirmedObservation(
                definition_id, artifact["collected_path"], record_path, basis,
            ))

    for artifact in artifacts:
        records = artifact["records"]
        first = records[0] if records else None
        definitions = set(artifact.get("definition_ids", ()))

        for record_index, record in enumerate(records):
            if not isinstance(record, dict) or record.get("record_type") != "cache_file_marker":
                continue
            for marker_index, marker in enumerate(record.get("matched_markers") or []):
                for definition_id in CACHE_MARKER_RULES.get(marker, ()):
                    add(artifact, definition_id, (record_index, "matched_markers", marker_index),
                        f"collected Cache_Data file contains the exact UTF-8 byte marker {marker!r}")

        if isinstance(first, dict) and definitions.intersection({"install_prefetch", "run_prefetch"}):
            executable = first.get("executable_filename")
            count = first.get("run_count")
            raw_time = (artifact.get("raw_timestamp") or {}).get("last_run_filetime")
            if isinstance(executable, str) and _positive_number(count) and isinstance(raw_time, list) and raw_time:
                name = PureWindowsPath(executable).name.casefold()
                if name == "chatgpt installer.exe":
                    add(artifact, "install_prefetch", (0, "run_count"),
                        "pyscca identified the ChatGPT installer executable and a recorded run")
                if name == "chatgpt.exe":
                    add(artifact, "run_prefetch", (0, "run_count"),
                        "pyscca identified ChatGPT.exe with a recorded run time")

        if "install_appx" in definitions:
            for key_index, key in enumerate(records):
                if not isinstance(key, dict) or key.get("definition_id") != "install_appx":
                    continue
                if key.get("status") not in {"ok", "partial"}:
                    continue
                for record_index, record in enumerate(key.get("records") or []):
                    if not isinstance(record, dict):
                        continue
                    registry_path = record.get("path")
                    if not isinstance(registry_path, str):
                        continue
                    package = PureWindowsPath(registry_path).name.casefold()
                    last_written = record.get("last_written_iso_from_parser")
                    if (package.startswith("openai.codex_")
                            and package.endswith("_2p2nqsd0c76g0")
                            and isinstance(last_written, str) and last_written):
                        add(artifact, "install_appx",
                            (key_index, "records", record_index, "last_written_iso_from_parser"),
                            "AppX AllUserStore contains a per-user OpenAI.Codex package registration key")

        if "run_userassist" in definitions:
            for key_index, key in enumerate(records):
                if not isinstance(key, dict) or key.get("definition_id") != "run_userassist":
                    continue
                if key.get("status") not in {"ok", "partial"}:
                    continue
                for record_index, record in enumerate(key.get("records") or []):
                    if not isinstance(record, dict) or not str(record.get("path", "")).casefold().endswith("\\count"):
                        continue
                    for value_index, value in enumerate(record.get("values") or []):
                        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
                            continue
                        decoded_name = codecs.decode(value["name"], "rot_13").casefold()
                        if ("\\windowsapps\\openai.codex_" not in decoded_name
                                or not decoded_name.endswith("\\app\\chatgpt.exe")):
                            continue
                        raw = value.get("value")
                        if not isinstance(raw, dict) or not isinstance(raw.get("base64"), str):
                            continue
                        try:
                            binary = base64.b64decode(raw["base64"], validate=True)
                        except (ValueError, binascii.Error):
                            continue
                        if len(binary) != 72 or struct.unpack_from("<Q", binary, 60)[0] == 0:
                            continue
                        add(artifact, "run_userassist",
                            (key_index, "records", record_index, "values", value_index, "value", "base64"),
                            "UserAssist Count records a ChatGPT.exe GUI launch and last-execution FILETIME")

        if isinstance(first, dict) and "run_global_state" in definitions:
            if _positive_number(first.get("desktop-first-seen-at-ms")):
                add(artifact, "run_global_state", (0, "desktop-first-seen-at-ms"),
                    "desktop-first-seen-at-ms contains a positive source timestamp")

        if isinstance(first, dict) and "login_auth" in definitions:
            tokens = first.get("tokens")
            if isinstance(tokens, dict):
                id_token = tokens.get("id_token")
                access_token = tokens.get("access_token")
                id_claims = id_token.get("jwt_claims", {}) if isinstance(id_token, dict) else {}
                access_claims = access_token.get("jwt_claims", {}) if isinstance(access_token, dict) else {}
                account_id = tokens.get("account_id")
                if (isinstance(id_claims, dict) and isinstance(access_claims, dict)
                        and _positive_number(id_claims.get("auth_time"))
                        and _positive_number(access_claims.get("iat"))
                        and isinstance(account_id, str) and account_id):
                    add(artifact, "login_auth", (0, "tokens", "id_token", "jwt_claims", "auth_time"),
                        "auth.json contains an account ID, authentication time, and issued access-token metadata")
                    profile = _windows_profile(artifact.get("source_path", ""))
                    if profile:
                        authenticated_accounts.setdefault(profile, set()).add(account_id)

        if "chat_codex_db" in definitions:
            for table_index, row_index, row in _parsed_tables(artifact, "local_thread_catalog"):
                if (row.get("source_kind") == "chatgpt"
                        and isinstance(row.get("host_id"), str)
                        and row["host_id"].startswith("chatgpt:")
                        and isinstance(row.get("thread_id"), str) and row["thread_id"]
                        and row.get("source_created_at") not in (None, "")):
                    add(artifact, "chat_codex_db", (table_index, "rows", row_index, "thread_id"),
                        "local_thread_catalog has a ChatGPT-hosted thread ID and source creation time")

        if "switch_codex_db" in definitions:
            hosts: dict[str, tuple[int, int]] = {}
            for table_index, row_index, row in _parsed_tables(artifact, "local_thread_catalog_hosts"):
                host = row.get("host_id")
                if row.get("host_kind") != "chatgpt" or not isinstance(host, str):
                    continue
                parts = host.split(":")
                if len(parts) == 3 and parts[0] == "chatgpt" and parts[1] and parts[2]:
                    hosts[host] = (table_index, row_index)
            accounts_with_threads = set()
            for _, _, row in _parsed_tables(artifact, "local_thread_catalog"):
                host = row.get("host_id")
                if (isinstance(host, str) and host in hosts and row.get("source_kind") == "chatgpt"
                        and isinstance(row.get("thread_id"), str) and row["thread_id"]):
                    accounts_with_threads.add(host.split(":")[1])
            if len(accounts_with_threads) >= 2:
                table_index, row_index = next(iter(hosts.values()))
                add(artifact, "switch_codex_db", (table_index, "rows", row_index, "host_id"),
                    "local_thread_catalog links ChatGPT threads to at least two account IDs")

        if "login_history" in definitions:
            for table_index, row_index, row in _parsed_tables(artifact, "urls"):
                url = row.get("url")
                if not isinstance(url, str):
                    continue
                try:
                    parsed_url = urlparse(url)
                except ValueError:
                    continue
                query = {key.casefold(): values for key, values in parse_qs(parsed_url.query).items()}
                originator = query.get("originator", [""])[0].casefold()
                redirect = query.get("redirect_uri", [""])[0]
                if (parsed_url.hostname == "auth.openai.com" and originator == "codex desktop"
                        and query.get("code_challenge")
                        and redirect.startswith("http://localhost:")
                        and "/auth/callback" in redirect):
                    add(artifact, "login_history", (table_index, "rows", row_index, "url"),
                        "Chrome History contains a Codex Desktop PKCE authorization request")

        if "connector_log" in definitions:
            for line_index, line in enumerate(records):
                if not isinstance(line, dict) or not isinstance(line.get("text"), str):
                    continue
                if "plugin_install_succeeded" in line["text"]:
                    add(artifact, "connector_log", (line_index, "text"),
                        "desktop log contains the plugin_install_succeeded event marker")

    for artifact in artifacts:
        profile = _windows_profile(artifact.get("source_path", ""))
        if not profile or profile not in authenticated_accounts:
            continue
        definitions = set(artifact.get("definition_ids", ()))
        if "login_codex_db" in definitions:
            for table_index, row_index, row in _parsed_tables(artifact, "local_thread_catalog"):
                host = row.get("host_id")
                if (row.get("source_kind") == "chatgpt" and isinstance(host, str)
                        and any(f":{account_id}:" in host for account_id in authenticated_accounts[profile])):
                    add(artifact, "login_codex_db", (table_index, "rows", row_index, "host_id"),
                        "ChatGPT host ID matches the account in an authenticated auth.json record")
        if "login_cookies" in definitions:
            for table_index, row_index, row in _parsed_tables(artifact, "cookies"):
                host = row.get("host_key")
                if (isinstance(host, str) and host.lstrip(".").casefold() == "auth.openai.com"
                        and row.get("name") == "oai-client-auth-session"
                        and _positive_number(row.get("creation_utc"))):
                    add(artifact, "login_cookies", (table_index, "rows", row_index, "name"),
                        "OpenAI client-auth session cookie accompanies a verified auth.json record")

    return tuple(observations)


def _record_at(records: list[Any], path: tuple[str | int, ...]) -> Any:
    if not path or not isinstance(path[0], int) or isinstance(path[0], bool):
        raise ValueError("record_path must start with a records-array index")
    value: Any = records
    for part in path:
        if isinstance(value, list) and isinstance(part, int) and not isinstance(part, bool):
            if part < 0:
                raise ValueError("negative record index is not allowed")
            value = value[part]
        elif isinstance(value, dict) and isinstance(part, str):
            value = value[part]
        else:
            raise ValueError(f"record_path cannot traverse {type(value).__name__} with {part!r}")
    if value is None or value == {} or value == [] or (
        isinstance(value, dict) and "omitted" in value
    ):
        raise ValueError("record_path points to an empty or omitted value")
    return value


def link_confirmed_observations(
    parsed_artifacts: Mapping[str, Any],
    observations: Sequence[ConfirmedObservation],
    rules: Sequence[ActionRule] = ACTION_RULES,
) -> RuleResult:
    """Make one four-column row per confirmed behavior/artifact relationship.

    A parsed file, nonempty record, or registered rule alone produces no row.
    Repeated confirmations of the same definition and source file share a row;
    different definitions for the same file remain separate rows.
    """
    by_rule = {rule.definition_id: rule for rule in rules}
    if len(by_rule) != len(rules):
        raise ValueError("duplicate action rule ID")
    by_path: dict[str, Mapping[str, Any]] = {}
    for artifact in parsed_artifacts.get("artifacts", []):
        path = artifact.get("collected_path")
        if path:
            if path in by_path:
                raise ValueError(f"duplicate collected artifact path: {path}")
            by_path[path] = artifact

    rows: list[dict[str, str]] = []
    rejected: list[str] = []
    emitted: set[tuple[str, str]] = set()
    for observation in observations:
        try:
            rule = by_rule[observation.definition_id]
            artifact = by_path[observation.collected_path]
            if not observation.basis.strip():
                raise ValueError("missing verified action basis")
            if artifact.get("status") not in {"success", "partial"}:
                raise ValueError("artifact was not parsed successfully")
            if observation.definition_id not in artifact.get("definition_ids", []):
                raise ValueError("rule does not belong to this collected artifact")
            if rule.artifact not in artifact.get("artifact_names", [artifact.get("artifact")]):
                raise ValueError("rule artifact name differs from parsed artifact")
            records = artifact.get("records")
            if not isinstance(records, list):
                raise ValueError("parsed records are absent")
            try:
                _record_at(records, observation.record_path)
            except (IndexError, KeyError, TypeError) as exc:
                raise ValueError(f"record_path does not exist: {observation.record_path}") from exc
            source_path = artifact.get("source_path")
            if observation.record_path and isinstance(observation.record_path[0], int):
                record = records[observation.record_path[0]]
                if isinstance(record, dict) and record.get("record_type") == "cache_file_marker":
                    member = record.get("collected_path")
                    if member not in artifact.get("collected_members", []):
                        raise ValueError("cache marker file is not a collected group member")
                    source_path = record.get("source_path")
            if not isinstance(source_path, str) or not source_path:
                raise ValueError("source_path is absent")
            identity = (rule.definition_id, source_path.casefold())
            if identity not in emitted:
                rows.append(dict(zip(CSV_COLUMNS, (
                    rule.behavior, rule.artifact, source_path, rule.grade,
                ))))
                emitted.add(identity)
        except (KeyError, ValueError) as exc:
            rejected.append(f"{observation.definition_id} @ {observation.collected_path}: {exc}")
    return RuleResult(tuple(rows), tuple(rejected))
