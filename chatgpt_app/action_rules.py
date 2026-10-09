"""Explicit action conditions and user-defined behavior/artifact/grade links."""

from __future__ import annotations

import base64
import binascii
import codecs
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PureWindowsPath
from typing import Any, Mapping, Sequence

from artifact_definitions import ARTIFACT_DEFINITIONS, ArtifactDefinition, Grade


CSV_COLUMNS = ("시각", "사용자 행위", "아티팩트", "경로", "등급", "근거")
ACTION_TAXONOMY = (
    ("A00", "설치"), ("A01", "실행"), ("A02", "로그인"),
    ("A03", "로그아웃"), ("A04", "다중 계정"), ("A05", "새 대화 생성"),
    ("A06", "프롬프트 입력"), ("A07", "프롬프트 수정"),
    ("A08", "메모리 기능 사용"), ("A09", "파일 업로드"),
    ("A10", "파일 다운로드"), ("A11", "대화 삭제"),
    ("A12", "계정 삭제"), ("A13", "임시 채팅"),
    ("A14", "플러그인 사용"),
)
ACTION_ID_BY_NAME = {name: action_id for action_id, name in ACTION_TAXONOMY}
NOTES_BEHAVIOR = "비고 - Codex/ ChatGPT Work 관련"
ACTION_ALIASES = {
    "로그인(인증)": "로그인", "계정 전환": "다중 계정",
    "메모리 사용 / 관리": "메모리 기능 사용",
    "생성물/파일 다운로드": "파일 다운로드",
    "생성물 / 파일 다운로드": "파일 다운로드",
    "외부 서비스 연동(connector / plugin)": "플러그인 사용",
}
KST = timezone(timedelta(hours=9), name="KST")
UTC = timezone.utc
WINDOWS_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)


class ReportRow(dict[str, str]):
    """Six visible columns; rule/event identities survive report preparation."""

    def __init__(self, values: Mapping[str, str], *, rule_id: str | None = None,
                 event_key: str | None = None):
        super().__init__(values)
        self.rule_id = rule_id
        self.event_key = event_key


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
    identifier_kind: str | None = None
    identifier: str | None = None
    timestamp_path: tuple[str | int, ...] | None = None
    evidence_paths: tuple[tuple[str | int, ...], ...] = ()
    event_key: str | None = None
    nested_json_path: tuple[str | int, ...] | None = None


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
        rules.append(ActionRule(definition.id, ACTION_ALIASES.get(definition.behavior, definition.behavior),
                                definition.artifact, definition.grade))
    return tuple(rules)


ACTION_RULES = rules_from_definitions()
SQLITE_DEFINITION_IDS = frozenset(
    definition.id for definition in ARTIFACT_DEFINITIONS
    if definition.artifact_type in {"sqlite", "chromium_history", "chromium_cookies"}
)
DETECTABLE_RULE_IDS = frozenset({
    "install_prefetch", "install_appx", "run_prefetch", "run_userassist",
    "run_global_state", "run_amcache",
    "login_auth", "login_codex_db", "login_history", "login_cache",
    "switch_codex_db", "chat_codex_db", "chat_cache", "chat_global_state",
    "prompt_input_cache",
    "prompt_edit_cache", "memory_cache",
    "upload_cache", "upload_visited_mru", "download_cache", "download_visited_mru",
    "temporary_chat_cache", "account_delete_cache", "connector_history", "connector_log",
    "connector_global_state", "connector_work_logs", "connector_rollout",
    "logout_auth", "connector_thread_history",
})

CACHE_MARKER_RULES: dict[str, tuple[str, ...]] = {
    "prompt_edit_children": ("prompt_edit_cache",),
    "confirm_delete_account": ("account_delete_cache",),
}

DIRECTORY_REPORT_MARKERS = {
    "connector_package": r"\plugins\cache\openai-curated-remote",
    "work_memories": r"\.codex\memories",
}

NOTES_EVIDENCE = {
    "work_logs": "실행 과정 중 로그; thread_id, cwd, plugin이나 tool 종류, user prompt 등",
    "work_session_index": "대화 세션 index; thread_id, 대화 제목, 수정 시간 등",
    "work_state": "thread/session 메타데이터; thread 정보, 사용모델, 환경, rollout path 등",
    "work_rollout": "thread의 원본에 가까운 실행 기록; user/agent message 등 대화/ 작업 기록, 대화 생성 시간 등",
    "work_thread_history": "rollout에서 추출한 thread history; thread 정보, 대화/ 작업 기록 등",
    "work_memories": "raw memory, memory summary 등 저장된 메모리 기록",
}


def rows_without_conditions(
    collection_manifest: Mapping[str, Any],
    rules: Sequence[ActionRule] = ACTION_RULES,
    conditional_ids: frozenset[str] = DETECTABLE_RULE_IDS,
    *,
    confirmed_rows: Sequence[Mapping[str, str]] = (),
    parsed_artifacts: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], ...]:
    """Map successfully extracted files for definitions without a detector.

    These are inventory-based evidence rows, not confirmed action observations.
    The caller merges them with detector rows before preparing both reports.
    """
    by_id = {rule.definition_id: rule for rule in rules}
    triggered_profiles: set[tuple[str, str]] = set()
    triggered_download_drives: set[str] = set()
    for row in confirmed_rows:
        behavior = row.get("사용자 행위", row.get("행위 분류"))
        artifact = row.get("아티팩트")
        path = row.get("경로")
        if not isinstance(path, str):
            continue
        source = PureWindowsPath(path)
        is_cache = any(part.casefold() == "cache_data" for part in source.parts)
        is_visited_mru = (source.name.casefold() == "ntuser.dat"
                          and artifact == "NTUSER.DAT")
        if (behavior not in {"파일 업로드", "파일 다운로드", "생성물/파일 다운로드"}
                or (artifact not in {"LastVisitedPidlMRU", "Cache_Data"}
                    and not is_cache and not is_visited_mru)):
            continue
        if source.name.casefold() == "ntuser.dat":
            profile = str(source.parent).casefold()
        elif is_cache:
            parts = source.parts
            appdata_index = next((i for i, part in enumerate(parts)
                                  if part.casefold() == "appdata"), None)
            profile = (str(PureWindowsPath(*parts[:appdata_index])).casefold()
                       if appdata_index is not None and appdata_index > 1 else None)
        else:
            profile = None
        if profile:
            triggered_profiles.add((behavior, profile))
            if behavior in {"파일 다운로드", "생성물/파일 다운로드"}:
                triggered_download_drives.add(source.drive.casefold())
    parsed_lookup: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for index, artifact in enumerate((parsed_artifacts or {}).get("artifacts", [])):
        if isinstance(artifact, dict) and isinstance(artifact.get("source_path"), str):
            parsed_lookup[artifact["source_path"].casefold()] = (index, artifact)
    rows: list[dict[str, str]] = []
    emitted_directories: set[tuple[str, str]] = set()
    for manifest_index, file in enumerate(collection_manifest.get("files", [])):
        if not isinstance(file, dict) or file.get("status") != "ok":
            continue
        original_path = file.get("source_path")
        if not isinstance(original_path, str) or not original_path or not file.get("collected_path"):
            continue
        for definition_id in dict.fromkeys(file.get("definition_ids") or []):
            if definition_id in conditional_ids:
                continue
            if (definition_id in SQLITE_DEFINITION_IDS
                    and original_path.casefold().endswith(("-wal", "-shm"))):
                # The sidecars belong to the main database artifact. The
                # collector and parser retain them, but reports show one row.
                continue
            rule = by_id.get(definition_id)
            if rule is None or (rule.behavior not in ACTION_ID_BY_NAME
                                and rule.behavior != NOTES_BEHAVIOR):
                continue
            if definition_id in {"upload_open_mru", "download_open_mru"}:
                profile = str(PureWindowsPath(original_path).parent).casefold()
                if (rule.behavior, profile) not in triggered_profiles:
                    continue
            elif definition_id == "download_mft":
                if PureWindowsPath(original_path).drive.casefold() not in triggered_download_drives:
                    continue
            source_path = original_path
            if definition_id in DIRECTORY_REPORT_MARKERS:
                marker = DIRECTORY_REPORT_MARKERS[definition_id]
                match_at = source_path.casefold().find(marker)
                if match_at < 0:
                    continue
                end = match_at + len(marker)
                if source_path[end:end + 1] != "\\":
                    continue
                source_path = source_path[:end].replace("\\", "/") + "/"
                directory_identity = (definition_id, source_path.casefold())
                if directory_identity in emitted_directories:
                    continue
                emitted_directories.add(directory_identity)
            parsed_match = parsed_lookup.get(original_path.casefold())
            event_times = (_event_times(parsed_match[1], definition_id)
                           if parsed_match else [])
            if parsed_match:
                parsed_index, parsed_artifact = parsed_match
                evidence = (f"artifacts[{parsed_index}].collection_status="
                            f"{json.dumps(parsed_artifact.get('collection_status'), ensure_ascii=False)}"
                            " (수집 확인; 별도 판정 조건 없음)")
                parsed_records = parsed_artifact.get("records") or []
                if definition_id == "run_global_state" and parsed_records and isinstance(parsed_records[0], dict):
                    first_seen = parsed_records[0].get("desktop-first-seen-at-ms")
                    if first_seen is not None:
                        evidence = (f"artifacts[{parsed_index}].records[0].desktop-first-seen-at-ms="
                                    f"{json.dumps(first_seen, ensure_ascii=False)}")
                elif definition_id == "install_appx":
                    registry_evidence = []
                    for key_index, key in enumerate(parsed_records):
                        if not isinstance(key, dict) or key.get("definition_id") != definition_id:
                            continue
                        for record_index, entry in enumerate(key.get("records") or []):
                            if (not isinstance(entry, dict)
                                    or not PureWindowsPath(str(entry.get("path", ""))).name.casefold().startswith("openai.codex_")):
                                continue
                            if isinstance(entry.get("path"), str):
                                registry_evidence.append(
                                    f"artifacts[{parsed_index}].records[{key_index}].records[{record_index}].path="
                                    + json.dumps(entry["path"], ensure_ascii=False))
                            if entry.get("last_written_iso_from_parser"):
                                registry_evidence.append(
                                    f"artifacts[{parsed_index}].records[{key_index}].records[{record_index}]"
                                    ".last_written_iso_from_parser="
                                    + json.dumps(entry["last_written_iso_from_parser"], ensure_ascii=False))
                    if registry_evidence:
                        evidence = "; ".join(registry_evidence)
            else:
                evidence = (f"_collection_manifest.json files[{manifest_index}].status=ok"
                            " (수집 확인; 별도 판정 조건 없음)")
            if definition_id in NOTES_EVIDENCE:
                evidence = NOTES_EVIDENCE[definition_id]
            elif definition_id == "connector_package":
                root = PureWindowsPath(source_path)
                child_names = set()
                for child in collection_manifest.get("files", []):
                    if child.get("status") != "ok" or definition_id not in child.get("definition_ids", ()):
                        continue
                    try:
                        relative = PureWindowsPath(child["source_path"]).relative_to(root)
                    except (KeyError, ValueError):
                        continue
                    if len(relative.parts) < 2:
                        continue
                    child_names.add(relative.parts[0])
                evidence = "하위 폴더명=[" + ", ".join(sorted(child_names, key=str.casefold)) + "]"
            elif definition_id in {"upload_open_mru", "download_open_mru"} and parsed_match:
                mru_rows = []
                for ki, key in enumerate(parsed_records):
                    if not isinstance(key, dict) or key.get("definition_id") != definition_id:
                        continue
                    for ri, entry in enumerate(key.get("records") or []):
                        for vi, value in enumerate(entry.get("values") or []):
                            detail = value.get("open_save_pidl_mru")
                            if not isinstance(detail, dict):
                                continue
                            fields = []
                            opened_on_path = None
                            for field in ("absolute_path", "opened_on"):
                                if field not in detail:
                                    continue
                                path = (ki, "records", ri, "values", vi, "open_save_pidl_mru", field)
                                fields.append(_record_location(parsed_index, path, parsed_records)
                                              + "=" + json.dumps(detail[field], ensure_ascii=False))
                                source_ref = _source_location_references(parsed_artifact, parsed_index, path)
                                if source_ref and source_ref not in fields:
                                    fields.append(source_ref)
                                if field == "opened_on":
                                    opened_on_path = path
                            if fields:
                                event_times_for_value = (_event_times(parsed_artifact, definition_id,
                                                                      opened_on_path)
                                                         if opened_on_path else [])
                                mru_rows.append(_report_row(
                                    rule, source_path,
                                    min(event_times_for_value) if event_times_for_value else None,
                                    evidence + "; " + "; ".join(fields),
                                    event_key=json.dumps([ki, ri, vi]),
                                ))
                if mru_rows:
                    rows.extend(mru_rows)
                    continue
            rows.append(_report_row(rule, source_path,
                                    min(event_times) if event_times else None, evidence))
    return tuple(rows)


def rows_for_missing_logout(collection_manifest: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Report auth.json absence only for a discovered .codex directory."""
    rule = next(rule for rule in ACTION_RULES if rule.definition_id == "logout_auth")
    files = collection_manifest.get("files", [])
    warnings = collection_manifest.get("warnings", [])
    rows = []
    for profile in collection_manifest.get("user_profiles", []):
        if not isinstance(profile, dict) or not profile.get("artifact_presence", {}).get(".codex"):
            continue
        profile_path = profile.get("path")
        if not isinstance(profile_path, str):
            continue
        auth_path = str(PureWindowsPath(profile_path) / ".codex" / "auth.json")
        if any(isinstance(item, dict) and str(item.get("source_path", "")).casefold() == auth_path.casefold()
               for item in files):
            continue
        if any(isinstance(warning, str) and auth_path.casefold() in warning.casefold()
               and ("cannot " in warning or "access" in warning.casefold()) for warning in warnings):
            continue
        rows.append(_report_row(rule, auth_path, None,
                                f"_collection_manifest.json user_profiles: {profile_path}"
                                " 아래 .codex 확인, auth.json 수집 기록 없음"))
    return tuple(rows)


def _positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _contains_text(value: Any, needle: str) -> bool:
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_contains_text(item, needle) for item in value.values())
    if isinstance(value, list):
        return any(_contains_text(item, needle) for item in value)
    return False


def _contains_key(value: Any, needle: str) -> bool:
    if isinstance(value, dict):
        return needle in value or any(_contains_key(item, needle) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, needle) for item in value)
    return False


def _key_paths(value: Any, needle: str, path: tuple[str | int, ...] = ()):
    if isinstance(value, dict):
        for key, item in value.items():
            current = (*path, key)
            if key == needle:
                yield current
            yield from _key_paths(item, needle, current)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _key_paths(item, needle, (*path, index))


def _text_paths(value: Any, needle: str, path: tuple[str | int, ...] = ()):
    if isinstance(value, str) and needle in value:
        yield path
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _text_paths(item, needle, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _text_paths(item, needle, (*path, index))


def _cache_marker_paths(value: Any, needle: str, path: tuple[str | int, ...]):
    """Find literal marker occurrences in decoded body keys and values."""
    if isinstance(value, str) and needle in value:
        yield path
    elif isinstance(value, dict):
        for key, item in value.items():
            if needle in key:
                yield (*path, key) if item is not None else path
            yield from _cache_marker_paths(item, needle, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _cache_marker_paths(item, needle, (*path, index))


def _nonempty_id(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _cache_json_objects(value: Any, path: tuple[str | int, ...],
                        conversation_id: str | None = None,
                        conversation_path: tuple[str | int, ...] | None = None):
    if isinstance(value, dict):
        own_id = _nonempty_id(value.get("conversation_id"))
        active_id = own_id or conversation_id
        active_path = (*path, "conversation_id") if own_id else conversation_path
        yield path, value, active_id, active_path
        for key, item in value.items():
            yield from _cache_json_objects(item, (*path, key), active_id, active_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _cache_json_objects(item, (*path, index), conversation_id, conversation_path)


def _parsed_tables(artifact: Mapping[str, Any], table_name: str):
    for index, table in enumerate(artifact.get("records", [])):
        if isinstance(table, dict) and table.get("name") == table_name:
            for row_index, row in enumerate(table.get("rows", [])):
                if isinstance(row, dict):
                    yield index, row_index, row


def _normalized_time(value: Any, kind: str) -> datetime | None:
    """Convert only formats whose field semantics are known for this rule."""
    try:
        if kind == "iso" and isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(KST) if parsed.tzinfo else None
        if kind == "registry_utc" and isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=UTC).astimezone(KST) if parsed.tzinfo is None else parsed.astimezone(KST)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            return None
        if kind == "filetime":
            return (WINDOWS_EPOCH + timedelta(microseconds=int(value) // 10)).astimezone(KST)
        if kind == "chrome_us":
            return (WINDOWS_EPOCH + timedelta(microseconds=value)).astimezone(KST)
        if kind == "unix_ms":
            return datetime.fromtimestamp(value / 1000, UTC).astimezone(KST)
        if kind == "unix_s":
            return datetime.fromtimestamp(value, UTC).astimezone(KST)
        if kind == "unix_or_iso":
            unit = "unix_ms" if value >= 10**11 else "unix_s"
            return _normalized_time(value, unit)
    except (OverflowError, OSError, ValueError, TypeError):
        return None
    return None


def _event_times(artifact: Mapping[str, Any], definition_id: str,
                 timestamp_path: tuple[str | int, ...] | None = None) -> list[datetime]:
    records = artifact.get("records") or []
    raw = artifact.get("raw_timestamp") or {}
    candidates: list[tuple[Any, str]] = []
    if definition_id in {"install_prefetch", "run_prefetch"} and timestamp_path is None:
        candidates.extend((value, "filetime") for value in raw.get("last_run_filetime", []))
    elif definition_id == "run_global_state" and records and isinstance(records[0], dict):
        candidates.append((records[0].get("desktop-first-seen-at-ms"), "unix_ms"))
    elif definition_id == "login_auth" and timestamp_path is None and records and isinstance(records[0], dict):
        tokens = records[0].get("tokens") or {}
        for name, claim in (("id_token", "iat"), ("access_token", "iat")):
            token = tokens.get(name) if isinstance(tokens, dict) else None
            claims = token.get("jwt_claims") if isinstance(token, dict) else None
            if isinstance(claims, dict):
                candidates.append((claims.get(claim), "unix_s"))
    elif definition_id == "install_appx" and timestamp_path is None:
        for key in records:
            if isinstance(key, dict) and key.get("definition_id") == "install_appx":
                for record in key.get("records") or []:
                    if (isinstance(record, dict)
                            and PureWindowsPath(str(record.get("path", ""))).name.casefold().startswith("openai.codex_")):
                        candidates.append((record.get("last_written_iso_from_parser"), "registry_utc"))
    if timestamp_path is not None and isinstance(records, list):
        try:
            value = _record_at(records, timestamp_path)
        except (KeyError, IndexError, TypeError, ValueError):
            value = None
        kind = {
            "run_amcache": "registry_utc",
            "install_appx": "registry_utc",
            "login_auth": "unix_s",
            "login_history": "chrome_us", "connector_history": "chrome_us",
            "login_cache": "unix_or_iso", "chat_codex_db": "unix_or_iso",
            "chat_cache": "unix_or_iso", "prompt_input_cache": "unix_or_iso",
            "connector_rollout": "iso",
            "run_userassist": "iso",
            "memory_cache": "unix_or_iso", "upload_cache": "unix_or_iso",
            "download_cache": "unix_or_iso", "temporary_chat_cache": "unix_or_iso",
            "upload_visited_mru": "registry_utc", "download_visited_mru": "registry_utc",
            "upload_open_mru": "registry_utc", "download_open_mru": "registry_utc",
            "connector_work_logs": "unix_s", "connector_thread_history": "unix_ms",
            "connector_log": "log_iso",
        }.get(definition_id)
        if kind:
            if kind == "log_iso" and isinstance(value, str):
                match = re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value)
                value, kind = (match.group() if match else None), "iso"
            if kind == "unix_or_iso" and isinstance(value, str):
                kind = "iso"
            candidates.append((value, kind))
    return [normalized for value, kind in candidates
            if (normalized := _normalized_time(value, kind)) is not None
            and 2020 <= normalized.year <= 2100]


def _format_event_time(value: datetime | None) -> str:
    if value is None:
        return "-"
    formatted = value.strftime("%Y-%m-%d %H:%M:%S")
    if value.microsecond >= 1000:
        formatted += f".{value.microsecond // 1000:03d}"
    return formatted + " KST"


def _detect_preserved_observations(
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

    def add(artifact: Mapping[str, Any], definition_id: str,
            record_path: tuple[str | int, ...], basis: str,
            identifier_kind: str | None = None, identifier: str | None = None,
            timestamp_path: tuple[str | int, ...] | None = None,
            evidence_paths: tuple[tuple[str | int, ...], ...] = (),
            event_key: str | None = None) -> None:
        if definition_id in DETECTABLE_RULE_IDS and definition_id in artifact.get("definition_ids", ()):
            observations.append(ConfirmedObservation(
                definition_id, artifact["collected_path"], record_path, basis,
                identifier_kind, identifier, timestamp_path, evidence_paths,
                event_key=event_key,
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

        raw_cache_markers = {
            marker for record in records
            if isinstance(record, dict) and record.get("record_type") == "cache_file_marker"
            for marker in record.get("matched_markers") or []
        }

        if definitions.intersection({"prompt_edit_cache", "account_delete_cache"}):
            for entry_index, entry in enumerate(records):
                if not isinstance(entry, dict) or entry.get("record_type") == "cache_file_marker":
                    continue
                for body_index, body in enumerate(entry.get("bodies") or []):
                    if not isinstance(body, dict) or not isinstance(body.get("json"), (dict, list)):
                        continue
                    root_path = (entry_index, "bodies", body_index, "json")
                    decoded = body["json"]
                    create_paths = list(_key_paths(decoded, "create_time", root_path))
                    logout_paths = list(_text_paths(decoded, "/auth/logout", root_path))
                    if isinstance(entry.get("key"), str) and "/auth/logout" in entry["key"]:
                        logout_paths.append((entry_index, "key"))
                    for object_path, value, _, _ in _cache_json_objects(decoded, root_path):
                        if ("prompt_edit_cache" in definitions and "prompt_edit_children" not in raw_cache_markers
                                and create_paths and isinstance(value.get("children"), list)
                                and len(value["children"]) >= 2
                                and all(isinstance(child, str) and child.strip() for child in value["children"])):
                            create_path = create_paths[0]
                            try:
                                _record_at(records, create_path)
                            except ValueError:
                                create_path = create_path[:-1]
                            add(artifact, "prompt_edit_cache", create_path,
                                "decoded response contains create_time and multiple children IDs")
                            add(artifact, "prompt_edit_cache", (*object_path, "children"),
                                "decoded response contains create_time and multiple children IDs")
                        if ("account_delete_cache" in definitions and "confirm_delete_account" not in raw_cache_markers
                                and logout_paths and value.get("logout_reason") == "confirm_delete_account"
                                and value.get("logout_source") == "settings.confirm-delete-account-modal"):
                            for match_path in (logout_paths[0], (*object_path, "logout_reason"),
                                               (*object_path, "logout_source")):
                                add(artifact, "account_delete_cache", match_path,
                                    "same decoded response contains logout endpoint and account deletion reason/source")

        if definitions.intersection({"login_codex_db", "switch_codex_db"}):
            hosts: dict[str, tuple[int, int]] = {}
            for table_index, row_index, row in _parsed_tables(artifact, "local_thread_catalog_hosts"):
                host_id = _nonempty_id(row.get("host_id"))
                if host_id is not None:
                    hosts.setdefault(host_id, (table_index, row_index))
            if hosts:
                first_host = next(iter(hosts.values()))
                add(artifact, "login_codex_db",
                    (first_host[0], "rows", first_host[1], "host_id"),
                    "local_thread_catalog_hosts contains a host_id",
                    evidence_paths=tuple((ti, "rows", ri, "host_id") for ti, ri in list(hosts.values())[1:]))
            if len(hosts) >= 2:
                for host_id, (table_index, row_index) in hosts.items():
                    add(artifact, "switch_codex_db",
                        (table_index, "rows", row_index, "host_id"),
                        "local_thread_catalog_hosts contains at least two distinct host_id values",
                        "host_id", host_id)

        if "prompt_input_cache" in definitions:
            for entry_index, entry in enumerate(records):
                if not isinstance(entry, dict) or entry.get("record_type") == "cache_file_marker":
                    continue
                for body_index, body in enumerate(entry.get("bodies") or []):
                    if not isinstance(body, dict) or not isinstance(body.get("json"), (dict, list)):
                        continue
                    root_path = (entry_index, "bodies", body_index, "json")
                    for object_path, value, conversation_id, conversation_path in _cache_json_objects(
                            body["json"], root_path):
                        author = value.get("author")
                        if (not conversation_id or not isinstance(author, dict)
                                or author.get("role") != "user"
                                or value.get("update_time") is None
                                or not isinstance(value.get("content"), dict)):
                            continue
                        time_path = (*object_path, "update_time")
                        # One mapping value identifies one prompt message. Keep
                        # every observation for that message in its own report row.
                        message_key = json.dumps(object_path, ensure_ascii=False,
                                                 separators=(",", ":"))
                        add(artifact, "prompt_input_cache", conversation_path,
                            "conversation_id identifies a conversation with a user prompt",
                            "conversation_id", conversation_id, time_path,
                            event_key=message_key)
                        add(artifact, "prompt_input_cache", (*object_path, "author", "role"),
                            "author.role is user", timestamp_path=time_path,
                            event_key=message_key)
                        add(artifact, "prompt_input_cache", time_path,
                            "user prompt update_time is present", timestamp_path=time_path,
                            event_key=message_key)
                        add(artifact, "prompt_input_cache", (*object_path, "content"),
                            "user prompt content is present", timestamp_path=time_path,
                            event_key=message_key)

        if isinstance(first, dict) and definitions.intersection({"install_prefetch", "run_prefetch"}):
            executable = first.get("executable_filename")
            if isinstance(executable, str):
                name = PureWindowsPath(executable).name.casefold()
                if name == "chatgpt installer.exe":
                    add(artifact, "install_prefetch", (0, "executable_filename"),
                        "pyscca identified the ChatGPT installer executable")
                if name == "chatgpt.exe":
                    add(artifact, "run_prefetch", (0, "executable_filename"),
                        "pyscca identified ChatGPT.exe")

        if "install_appx" in definitions:
            for key_index, key in enumerate(records):
                if not isinstance(key, dict) or key.get("definition_id") != "install_appx":
                    continue
                if key.get("status") not in {"ok", "partial"}:
                    continue
                for record_index, entry in enumerate(key.get("records") or []):
                    if (not isinstance(entry, dict)
                            or not PureWindowsPath(str(entry.get("path", ""))).name.casefold().startswith("openai.codex_")
                            or not isinstance(entry.get("last_written_iso_from_parser"), str)):
                        continue
                    add(artifact, "install_appx", (key_index, "records", record_index, "path"),
                        "AppX PackageState contains an OpenAI.Codex package",
                        timestamp_path=(key_index, "records", record_index, "last_written_iso_from_parser"))
                    add(artifact, "install_appx",
                        (key_index, "records", record_index, "last_written_iso_from_parser"),
                        "AppX package key last write timestamp",
                        timestamp_path=(key_index, "records", record_index, "last_written_iso_from_parser"))

        if "run_global_state" in definitions and isinstance(first, dict):
            value = first.get("desktop-first-seen-at-ms")
            if _positive_number(value):
                add(artifact, "run_global_state", (0, "desktop-first-seen-at-ms"),
                    "global state contains desktop-first-seen-at-ms",
                    timestamp_path=(0, "desktop-first-seen-at-ms"))

        if "run_amcache" in definitions:
            for key_index, key in enumerate(records):
                if not isinstance(key, dict) or key.get("definition_id") != "run_amcache":
                    continue
                for record_index, entry in enumerate(key.get("records") or []):
                    if not isinstance(entry, dict) or not entry.get("last_written_iso_from_parser"):
                        continue
                    for value_index, value in enumerate(entry.get("values") or []):
                        if (isinstance(value, dict) and str(value.get("name", "")).casefold() == "name"
                                and str(value.get("value", "")).casefold() == "chatgpt.exe"):
                            add(artifact, "run_amcache",
                                (key_index, "records", record_index, "values", value_index, "value"),
                                "AmCache InventoryApplicationFile name is ChatGPT.exe",
                                timestamp_path=(key_index, "records", record_index, "last_written_iso_from_parser"))
                            break

        if "login_history" in definitions:
            callback_visits = []
            open_app_visits = []
            for table_index, row_index, row in _parsed_tables(artifact, "urls"):
                url = row.get("url")
                if not isinstance(url, str):
                    continue
                location = (table_index, "rows", row_index)
                if "http://localhost:1455/auth/callback?code=" in url:
                    callback_visits.append(location)
                if "https://chatgpt.com/codex/open-app?source=login&app_brand=chatgpt" in url:
                    open_app_visits.append(location)
            if callback_visits and open_app_visits:
                for location in callback_visits + open_app_visits:
                    add(artifact, "login_history", (*location, "url"),
                        "Chrome History contains both login callback and Codex open-app URLs",
                        timestamp_path=(*location, "last_visit_time"))

        if "chat_global_state" in definitions and isinstance(first, dict):
            for field in ("prompt_history", "client-thread-bindings-v1"):
                for field_path in _key_paths(first, field, (0,)):
                    add(artifact, "chat_global_state", field_path,
                        "global state contains prompt_history or client-thread-bindings-v1")

    return tuple(observations)


DETAILED_RULE_IDS = frozenset({
    "login_auth", "login_cache", "chat_codex_db", "chat_cache", "run_userassist",
    "memory_cache", "upload_cache", "download_cache", "temporary_chat_cache",
    "connector_history", "connector_log", "connector_global_state", "connector_work_logs",
    "connector_rollout", "connector_thread_history", "upload_visited_mru", "download_visited_mru",
})


def _is_plugin_tool_record(value: Any) -> bool:
    """Keep quoted prompts, developer text, and compacted history out of tool evidence."""
    if not isinstance(value, dict):
        return False
    payload = value.get("payload")
    if not isinstance(payload, dict):
        return False
    if value.get("type") == "event_msg" and payload.get("type") == "item_completed":
        item = payload.get("item")
        return isinstance(item, dict) and item.get("type") == "McpToolCall"
    if value.get("type") != "response_item" or payload.get("type") not in {
            "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"}:
        return False
    tool_name = payload.get("name") or payload.get("tool_name")
    return (isinstance(tool_name, str)
            and (tool_name.startswith("mcp__") or tool_name.startswith("tools.mcp")))


def _matching_nested_paths(value: Any, marker: str,
                           path: tuple[str | int, ...] = ()):
    """Find the narrowest JSON key/value containing a verified literal."""
    if isinstance(value, str):
        if marker in value:
            yield path
    elif isinstance(value, dict):
        for key, item in value.items():
            current = (*path, key)
            if marker in str(key):
                yield current
            else:
                yield from _matching_nested_paths(item, marker, current)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _matching_nested_paths(item, marker, (*path, index))


def _encoded_field_matches(value: str, marker: str):
    """An empty path means the entire plain-text field is the matching value."""
    try:
        decoded = json.loads(value)
    except ValueError:
        decoded = value
    return tuple(_matching_nested_paths(decoded, marker))


def _detailed_observations(artifact: Mapping[str, Any]) -> list[ConfirmedObservation]:
    """Conditions with field-level evidence and explicit event identity."""
    records = artifact["records"]
    definitions = set(artifact.get("definition_ids", ()))
    found: list[ConfirmedObservation] = []

    def emit(rule, path, *, evidence=(), time=None, event=None, identifier=None,
             kind=None, nested=None):
        if rule in definitions:
            found.append(ConfirmedObservation(rule, artifact["collected_path"], path,
                         "user-specified parsed field condition", kind, identifier, time,
                         tuple(evidence), event, nested))

    first = records[0] if records else None
    if "login_auth" in definitions and isinstance(first, dict):
        tokens = first.get("tokens")
        if isinstance(tokens, dict) and all(isinstance(tokens.get(n), dict)
                and tokens[n].get("present") is True for n in ("id_token", "access_token")):
            for name in ("id_token", "access_token"):
                base = (0, "tokens", name)
                claims = tokens[name].get("jwt_claims")
                extras = ((*base, "jwt_claims"),) if isinstance(claims, dict) else ()
                if isinstance(claims, dict) and "iat" in claims:
                    extras += ((*base, "jwt_claims", "iat"),)
                emit("login_auth", (*base, "present"), evidence=extras,
                     time=((*base, "jwt_claims", "iat")
                           if isinstance(claims, dict) and "iat" in claims else None))

    for ti, ri, row in _parsed_tables(artifact, "local_thread_catalog"):
        identifier = _nonempty_id(row.get("thread_id"))
        if identifier:
            base = (ti, "rows", ri)
            emit("chat_codex_db", (*base, "thread_id"),
                 evidence=((*base, "display_title"),) if "display_title" in row else (),
                 time=(*base, "source_created_at") if "source_created_at" in row else None,
                 event=identifier, identifier=identifier, kind="thread_id")

    for ti, ri, row in _parsed_tables(artifact, "urls"):
        url = row.get("url")
        if isinstance(url, str) and "https://chatgpt.com/connector_platform_oauth_redirect?state=codex_scheme__oauth" in url:
            emit("connector_history", (ti, "rows", ri, "url"),
                 time=(ti, "rows", ri, "last_visit_time"))

    marker = "@openai-curated-remote)"
    for ti, table in enumerate(records):
        if not isinstance(table, dict):
            continue
        for ri, row in enumerate(table.get("rows") or []):
            if not isinstance(row, dict):
                continue
            base = (ti, "rows", ri)
            if table.get("name") in {"log", "logs"} and isinstance(row.get("feedback_log_body"), str):
                for nested in _encoded_field_matches(row["feedback_log_body"], marker):
                    emit("connector_work_logs", (*base, "feedback_log_body"),
                         time=(*base, "ts"), nested=nested)
            item_json = row.get("item_json")
            if isinstance(item_json, str) and marker in item_json:
                try:
                    history_item = json.loads(item_json)
                except ValueError:
                    history_item = None
                if _is_plugin_tool_record(history_item):
                    for nested in _matching_nested_paths(history_item, marker):
                        emit("connector_thread_history", (*base, "item_json"),
                             time=(*base, "created_at_ms"), nested=nested)

    for i, line in enumerate(records):
        if not isinstance(line, dict):
            continue
        text = line.get("text")
        if isinstance(text, str) and "plugin_install_succeeded" in text:
            emit("connector_log", (i, "text"), time=(i, "text"))
        value = line.get("value")
        if _is_plugin_tool_record(value):
            for nested in _matching_nested_paths(value, marker):
                emit("connector_rollout", (i, "value", *nested),
                     time=(i, "value", "timestamp"))

    if isinstance(first, dict) and "connector_global_state" in definitions:
        state = first.get("electron-persisted-atom-state")
        if isinstance(state, dict):
            for path in _text_paths(state, "openai-curated-remote", (0, "electron-persisted-atom-state")):
                if not any(str(part).casefold().replace("_", "-") in {
                        "prompt-history", "thread-description", "history"} for part in path):
                    emit("connector_global_state", path)
        cache = state.get("mcp-server-tools-cache-v2") if isinstance(state, dict) else None
        catalog = cache.get("catalog") if isinstance(cache, dict) else None
        if isinstance(catalog, list):
            for i, item in enumerate(catalog):
                if not isinstance(item, dict) or "tools" not in item:
                    continue
                base = (0, "electron-persisted-atom-state", "mcp-server-tools-cache-v2", "catalog", i)
                emit("connector_global_state", (*base, "tools"),
                     evidence=tuple(_key_paths(item, "_meta", base)))

    for ki, key in enumerate(records):
        if not isinstance(key, dict) or key.get("definition_id") not in definitions:
            continue
        rule = key["definition_id"]
        for ri, entry in enumerate(key.get("records") or []):
            for vi, value in enumerate(entry.get("values") or []):
                base = (ki, "records", ri, "values", vi)
                if rule == "run_userassist" and str(entry.get("path", "")).casefold().endswith("\\count"):
                    ua = value.get("userassist")
                    name = ua.get("program_name") if isinstance(ua, dict) else codecs.decode(str(value.get("name", "")), "rot_13")
                    if ("chatgpt.exe" in str(name).casefold() and isinstance(ua, dict)
                            and isinstance(ua.get("value_data_offsets"), dict)):
                        extras = ((*base, "userassist", "value_data_offsets"),)
                        if "run_counter" in ua:
                            extras += ((*base, "userassist", "run_counter"),)
                        emit(rule, (*base, "userassist", "program_name"), evidence=extras,
                             time=((*base, "userassist", "last_executed_kst")
                                   if isinstance(ua.get("last_executed_kst"), str) else None))
                if rule in {"upload_visited_mru", "download_visited_mru"}:
                    detail = value.get("last_visited_pidl_mru")
                    if isinstance(detail, dict) and str(detail.get("executable", "")).casefold() == "chatgpt.exe":
                        emit(rule, (*base, "last_visited_pidl_mru", "executable"),
                             evidence=((*base, "last_visited_pidl_mru", "absolute_path"),),
                             time=(*base, "last_visited_pidl_mru", "opened_on"))
                    elif not isinstance(detail, dict):
                        raw = value.get("value")
                        if isinstance(raw, dict) and isinstance(raw.get("base64"), str):
                            try:
                                binary = base64.b64decode(raw["base64"], validate=True)
                            except (ValueError, binascii.Error):
                                continue
                            if b"ChatGPT.exe" in binary or "ChatGPT.exe".encode("utf-16le") in binary:
                                emit(rule, (*base, "value", "base64"))

    conversation_key = "1/0/_dk_https://chatgpt.com https://chatgpt.com https://chatgpt.com/backend-api/conversation/"
    for ei, entry in enumerate(records):
        if not isinstance(entry, dict) or entry.get("record_type") == "cache_file_marker":
            continue
        for bi, body in enumerate(entry.get("bodies") or []):
            if not isinstance(body, dict) or not isinstance(body.get("json"), (dict, list)):
                continue
            root = (ei, "bodies", bi, "json")
            objects = list(_cache_json_objects(body["json"], root))
            for path, obj, cid, cid_path in objects:
                # The login time belongs to account, never an unrelated created_time.
                account = obj.get("account")
                if isinstance(account, dict) and account.get("created_time") is not None:
                    tp = (*path, "account", "created_time")
                    extras = tuple(_key_paths(obj, "account_user_id", path))
                    emit("login_cache", tp, evidence=extras, time=tp,
                         event=json.dumps(account["created_time"], ensure_ascii=False, sort_keys=True))
                if (conversation_key in str(entry.get("key", "")) and _nonempty_id(obj.get("conversation_id"))
                        and isinstance(obj.get("title"), str) and obj.get("create_time") is not None):
                    emit("chat_cache", cid_path, evidence=((*path, "title"), (ei, "key")),
                         time=(*path, "create_time"), event=cid, identifier=cid, kind="conversation_id")
                keys = list(obj)
                if (len(path) >= 2 and path[-2] == "items" and isinstance(path[-1], int)
                        and any(keys[n:n+3] == ["id", "title", "create_time"] for n in range(len(keys)-2))
                        and _nonempty_id(obj.get("id"))):
                    identifier = obj["id"].strip()
                    emit("chat_cache", (*path, "id"), evidence=((*path, "title"),),
                         time=(*path, "create_time"), event=identifier, identifier=identifier, kind="conversation_id")
                if obj.get("title") == "New Chat":
                    emit("temporary_chat_cache", (*path, "title"),
                         evidence=(cid_path,) if cid_path else (), time=(*path, "create_time"))
                # A message's own update_time is the only message action clock.
                is_message = (path[-1:] == ("message",) or "author" in obj)
                if not is_message:
                    continue
                tp = (*path, "update_time")
                for needle in ("memcite", "memory_percentage_used"):
                    for match in _cache_marker_paths(obj, needle, path):
                        emit("memory_cache", match, time=tp)
                metadata = obj.get("metadata")
                if isinstance(metadata, dict) and "attachments" in metadata:
                    emit("upload_cache", (*path, "metadata", "attachments"), time=tp)
                content = obj.get("content")
                if isinstance(content, dict) and list(_text_paths(content.get("parts"), "다운로드]")):
                    emit("download_cache", (*path, "content", "parts"), time=tp)
            # Retain the established memory marker condition even without a message clock.
            for needle in ("memcite", "memory_percentage_used"):
                existing = {o.record_path for o in found if o.definition_id == "memory_cache"}
                for match in _cache_marker_paths(body["json"], needle, root):
                    if match not in existing:
                        emit("memory_cache", match)
    if not any(o.definition_id == "memory_cache" and o.timestamp_path is not None for o in found):
        raw_memory = []
        for i, record in enumerate(records):
            if isinstance(record, dict) and record.get("record_type") == "cache_file_marker":
                for j, marker in enumerate(record.get("matched_markers") or []):
                    if marker in {"memcite", "memory_percentage_used"}:
                        raw_memory.append((i, "matched_markers", j))
        if raw_memory:
            found = [o for o in found if o.definition_id != "memory_cache"]
            for path in raw_memory:
                emit("memory_cache", path)
    return found


def detect_confirmed_observations(parsed_artifacts: Mapping[str, Any]) -> tuple[ConfirmedObservation, ...]:
    observations = list(_detect_preserved_observations(parsed_artifacts))
    for artifact in parsed_artifacts.get("artifacts", []):
        if (isinstance(artifact, dict) and artifact.get("status") in {"success", "partial"}
                and isinstance(artifact.get("records"), list) and artifact.get("collected_path")):
            observations.extend(_detailed_observations(artifact))
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
    if value is None or (isinstance(value, dict) and "omitted" in value):
        raise ValueError("record_path points to an empty or omitted value")
    return value


def _record_location(artifact_index: int, path: tuple[str | int, ...],
                     records: list[Any]) -> str:
    locator = f"artifacts[{artifact_index}].records"
    for part in path:
        if isinstance(part, int):
            locator += f"[{part}]"
        elif part.isidentifier():
            locator += "." + part
        else:
            locator += "[" + json.dumps(part, ensure_ascii=False) + "]"
    return locator


def _nested_json_value(value: str, path: tuple[str | int, ...]) -> Any:
    try:
        decoded = json.loads(value)
    except ValueError:
        decoded = value
    current = decoded
    for part in path:
        current = current[part]
    return current


def _nested_json_locator(path: tuple[str | int, ...]) -> str:
    locator = "$"
    for part in path:
        if isinstance(part, int):
            locator += f"[{part}]"
        elif part.isidentifier():
            locator += "." + part
        else:
            locator += "[" + json.dumps(part, ensure_ascii=False) + "]"
    return locator


def split_evidence_items(evidence: str) -> tuple[str, ...]:
    """Split report clauses without splitting semicolons inside JSON values."""
    parts = []
    start = 0
    quoted = escaped = False
    for index, char in enumerate(evidence):
        if quoted and escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and evidence[index:index + 2] == "; ":
            parts.append(evidence[start:index])
            start = index + 2
    parts.append(evidence[start:])
    return tuple(part for part in parts if part)


def _source_location_references(artifact: Mapping[str, Any], artifact_index: int,
                                path: tuple[str | int, ...]) -> str:
    """Reference stored provenance rather than inventing locators in reports."""
    matches = []
    for index, location in enumerate(artifact.get("source_location") or []):
        prefix = tuple(location.get("record_path", ()))
        if prefix and path[:len(prefix)] == prefix and isinstance(location.get("_source_locator"), str):
            matches.append((len(prefix), index, location))
    if not matches:
        return ""
    longest = max(item[0] for item in matches)
    references = []
    for length, index, location in matches:
        if length == longest:
            references.append(f"artifacts[{artifact_index}].source_location[{index}]._source_locator="
                              + json.dumps(location["_source_locator"], ensure_ascii=False))
    return "; ".join(references)


def _evidence_value(value: Any, definition_id: str,
                    path: tuple[str | int, ...]) -> str:
    leaf = path[-1] if path else None
    if (definition_id == "login_history" and leaf == "url"
            and isinstance(value, str)
            and "http://localhost:1455/auth/callback?code=" in value):
        value = re.sub(r"(?i)([?&](?:code|state)=)[^&#]*", r"\1<redacted>",
                       value.split("#", 1)[0])
    if len(path) >= 2 and path[-2] == "matched_markers" and isinstance(value, str):
        marker_text = {
            "title_then_create_time": '"title" 다음 "create_time"',
            "account_user_id_and_created_time": '"account_user_id" + "created_time"',
            "prompt_edit_children": '"create_time" + "children" ID 2개 이상',
            "title_new_chat": '"title":"New Chat"',
            "confirm_delete_account": '"/auth/logout" + "confirm_delete_account" + "settings.confirm-delete-account-modal"',
        }.get(value, value)
        return json.dumps(marker_text, ensure_ascii=False)
    if leaf == "base64":
        return "ChatGPT.exe (decoded match)"
    if definition_id in DETAILED_RULE_IDS:
        return json.dumps(value, ensure_ascii=False)
    if definition_id == "prompt_edit_cache" and path[-1] == "children" and isinstance(value, list):
        serialized = json.dumps(value, ensure_ascii=False)
        return serialized if len(serialized) <= 512 else serialized[:512] + "…"
    if definition_id == "prompt_input_cache" and leaf == "content":
        serialized = json.dumps(value, ensure_ascii=False)
        return serialized if len(serialized) <= 512 else serialized[:512] + "…"
    if isinstance(value, dict):
        return "keys=" + json.dumps(list(value), ensure_ascii=False)
    if isinstance(value, list):
        return f"list({len(value)})"
    if isinstance(value, str):
        if len(value) > 256:
            return json.dumps(value[:256], ensure_ascii=False) + "…"
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def _report_row(rule: ActionRule, source_path: str, when: datetime | None,
                evidence: str, event_key: str | None = None) -> ReportRow:
    return ReportRow(dict(zip(CSV_COLUMNS, (
        _format_event_time(when), rule.behavior,
        PureWindowsPath(source_path.rstrip("\\/")).name,
        source_path, rule.grade, evidence,
    ))), rule_id=rule.definition_id, event_key=event_key)


def link_confirmed_observations(
    parsed_artifacts: Mapping[str, Any],
    observations: Sequence[ConfirmedObservation],
    rules: Sequence[ActionRule] = ACTION_RULES,
) -> RuleResult:
    """Combine rule/file matches, retaining explicitly requested event identities."""
    by_rule = {rule.definition_id: rule for rule in rules}
    if len(by_rule) != len(rules):
        raise ValueError("duplicate action rule ID")
    by_path: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for artifact_index, artifact in enumerate(parsed_artifacts.get("artifacts", [])):
        path = artifact.get("collected_path")
        if path:
            if path in by_path:
                raise ValueError(f"duplicate collected artifact path: {path}")
            by_path[path] = (artifact_index, artifact)

    matched: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    rejected: list[str] = []
    for observation in observations:
        try:
            rule = by_rule[observation.definition_id]
            artifact_index, artifact = by_path[observation.collected_path]
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
                observed_value = _record_at(records, observation.record_path)
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
                elif str(artifact.get("parser", "")).startswith("ccl_chromium_reader.ccl_chromium_cache"):
                    # A decoded cache entry cannot be assigned to one BlockFile
                    # member, so its provenance is the collected cache directory.
                    source_path = str(PureWindowsPath(source_path).parent)
            if not isinstance(source_path, str) or not source_path:
                raise ValueError("source_path is absent")
            identifier = observation.identifier
            if identifier is not None:
                if (observation.definition_id == "switch_codex_db"
                        and observation.identifier_kind != "host_id") or (
                    observation.definition_id in {"chat_codex_db", "chat_cache"}
                        and observation.identifier_kind not in {"thread_id", "conversation_id"}
                ):
                    raise ValueError("identifier kind does not match action rule")
                if not identifier.strip():
                    raise ValueError("empty action identifier")
                if not isinstance(observed_value, str) or observed_value.strip() != identifier:
                    raise ValueError("identifier does not match the parsed record")
            paths = (observation.record_path, *observation.evidence_paths)
            if observation.timestamp_path is not None:
                paths += (observation.timestamp_path,)
            observed_evidence: list[str] = []
            observed_references: list[str] = []
            for evidence_path in dict.fromkeys(paths):
                try:
                    value = _record_at(records, evidence_path)
                except (IndexError, KeyError, TypeError, ValueError):
                    continue
                location = _record_location(artifact_index, evidence_path, records)
                if evidence_path == observation.record_path and observation.nested_json_path is not None:
                    if not isinstance(value, str):
                        raise ValueError("nested JSON path requires a text field")
                    try:
                        value = _nested_json_value(value, observation.nested_json_path)
                    except (KeyError, IndexError, TypeError) as exc:
                        raise ValueError("nested JSON path is absent") from exc
                    if observation.nested_json_path:
                        location += "[json_key=" + json.dumps(
                            _nested_json_locator(observation.nested_json_path), ensure_ascii=False) + "]"
                evidence = location + "=" + _evidence_value(value, observation.definition_id, evidence_path)
                source_reference = _source_location_references(artifact, artifact_index, evidence_path)
                if evidence not in observed_evidence:
                    observed_evidence.append(evidence)
                for reference in split_evidence_items(source_reference):
                    if reference not in observed_references:
                        observed_references.append(reference)
            observed_times = _event_times(artifact, observation.definition_id,
                                          observation.timestamp_path)
            for event_time in dict.fromkeys(observed_times or [None]):
                time_label = _format_event_time(event_time)
                event_key = (json.dumps([observation.event_key, time_label], ensure_ascii=False)
                             if event_time is not None else observation.event_key)
                identity = (rule.definition_id, source_path.casefold(), event_key)
                bucket = matched.setdefault(identity, {"rule": rule, "path": source_path,
                                                       "evidence": [], "source_references": [],
                                                       "time": event_time, "event_key": event_key})
                for evidence in observed_evidence:
                    if evidence not in bucket["evidence"]:
                        bucket["evidence"].append(evidence)
                for reference in observed_references:
                    if reference not in bucket["source_references"]:
                        bucket["source_references"].append(reference)
                if rule.definition_id in {"install_prefetch", "run_prefetch"}:
                    raw_times = (artifact.get("raw_timestamp") or {}).get("last_run_filetime", [])
                    for index, raw_time in enumerate(raw_times):
                        if _normalized_time(raw_time, "filetime") != event_time:
                            continue
                        evidence = (f"artifacts[{artifact_index}].raw_timestamp.last_run_filetime"
                                    f"[{index}]={json.dumps(raw_time)}")
                        if evidence not in bucket["evidence"]:
                            bucket["evidence"].append(evidence)
                        if records and isinstance(records[0], dict):
                            record_times = records[0].get("last_run_filetime")
                            if isinstance(record_times, list) and index < len(record_times):
                                record_path = (0, "last_run_filetime", index)
                                evidence = (_record_location(artifact_index, record_path, records)
                                            + "=" + json.dumps(record_times[index]))
                                if evidence not in bucket["evidence"]:
                                    bucket["evidence"].append(evidence)
                                source_ref = _source_location_references(artifact, artifact_index, record_path)
                                for reference in split_evidence_items(source_ref):
                                    if reference not in bucket["source_references"]:
                                        bucket["source_references"].append(reference)
        except (KeyError, ValueError) as exc:
            rejected.append(f"{observation.definition_id} @ {observation.collected_path}: {exc}")
    rows = tuple(_report_row(bucket["rule"], bucket["path"], bucket["time"],
                             "; ".join((*bucket["evidence"], *bucket["source_references"])),
                             bucket["event_key"])
                 for bucket in matched.values())
    return RuleResult(rows, tuple(rejected))
