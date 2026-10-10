"""Render one shared set of condition-backed and collected-artifact report rows."""

from __future__ import annotations

import csv
import html
import os
import json
import re
from collections import defaultdict
from pathlib import Path, PureWindowsPath
from typing import Mapping, Sequence

from action_rules import (ACTION_ALIASES, ACTION_ID_BY_NAME, ACTION_TAXONOMY,
                          ACTION_RULES, CSV_COLUMNS, NOTES_BEHAVIOR, ReportRow,
                          _report_content, split_evidence_items)
from artifact_definitions import ARTIFACT_DEFINITIONS


ACTION_ORDER = tuple(name for _, name in ACTION_TAXONOMY) + (NOTES_BEHAVIOR,)


# These are the actual rule identifiers used by action_rules.py and the
# artifact inventory. No presentation-only R-Axx identifiers are assigned.
RULE_IDS = {definition.id: definition.id for definition in ARTIFACT_DEFINITIONS}
RULE_BY_ID = {rule.definition_id: rule for rule in ACTION_RULES}

# Match and time columns are documented only where the current detector has a
# corresponding field check. Descriptions in the inventory remain notes.
RULE_FIELDS = {
    "install_prefetch": ("executable_filename", "일치", "CHATGPT INSTALLER.EXE", "last_run_filetime"),
    "install_appx": ("PackageState.path", "접두 일치", "OpenAI.Codex_", "last_written_iso_from_parser"),
    "run_prefetch": ("executable_filename", "일치", "CHATGPT.EXE", "last_run_filetime"),
    "run_userassist": ("userassist.program_name", "포함", "ChatGPT.exe", "last_executed_kst"),
    "run_global_state": ("desktop-first-seen-at-ms", "양수", "-", "desktop-first-seen-at-ms"),
    "run_amcache": ("InventoryApplicationFile.Name", "일치", "ChatGPT.exe", "last_written_iso_from_parser"),
    "login_auth": ("id_token / access_token", "둘 다 조회", "-", "iat"),
    "login_codex_db": ("local_thread_catalog_hosts.host_id", "조회", "-", "-"),
    "login_history": ("urls.url", "두 URL 모두 포함", "localhost 인증 callback / chatgpt.com/codex/open-app", "last_visit_time"),
    "login_cache": ("account.created_time", "조회", "-", "account.created_time"),
    "logout_auth": ("auth.json", "부재", ".codex 존재", "-"),
    "switch_codex_db": ("local_thread_catalog_hosts.host_id", "서로 다른 값 2개 이상", "-", "-"),
    "chat_codex_db": ("local_thread_catalog.thread_id", "조회", "-", "source_created_at"),
    "chat_cache": ("conversation_id / items.id", "대화 응답 필드 조합", "title, create_time", "create_time"),
    "chat_global_state": ("prompt_history / client-thread-bindings-v1", "키 존재", "-", "-"),
    "prompt_input_cache": ("author.role", "일치 및 content 존재", "user", "update_time"),
    "prompt_edit_cache": ("create_time / children", "children ID 2개 이상", "-", "-"),
    "memory_cache": ("message", "문자열 포함", "memcite / memory_percentage_used", "message.update_time"),
    "upload_cache": ("message.metadata.attachments", "키 존재", "-", "message.update_time"),
    "upload_visited_mru": ("executable", "일치", "ChatGPT.exe", "opened_on"),
    "download_visited_mru": ("executable", "일치", "ChatGPT.exe", "opened_on"),
    "download_cache": ("message.content.parts", "문자열 포함", "다운로드]", "message.update_time"),
    "account_delete_cache": ("/auth/logout, logout_reason, logout_source", "같은 응답에서 일치", "confirm_delete_account / settings.confirm-delete-account-modal", "-"),
    "temporary_chat_cache": ("title", "일치", "New Chat", "create_time"),
    "connector_history": ("urls.url", "포함", "connector_platform_oauth_redirect?state=codex_scheme__oauth", "last_visit_time"),
    "connector_log": ("text", "포함", "plugin_install_succeeded", "text 내 날짜"),
    "connector_global_state": ("mcp-server-tools-cache-v2.catalog.tools / 상태 문자열", "키 존재 또는 문자열 포함", "openai-curated-remote", "-"),
    "connector_work_logs": ("log.feedback_log_body", "실제 호출 기록에 포함", "@openai-curated-remote)", "ts"),
    "connector_rollout": ("value", "실제 MCP 도구 호출에 포함", "@openai-curated-remote)", "timestamp"),
    "connector_thread_history": ("item_json", "실제 MCP 도구 호출에 포함", "@openai-curated-remote)", "created_at_ms"),
}
RULE_FILTERS = {
    "install_prefetch": "executable_filename=CHATGPT INSTALLER.EXE",
    "install_appx": "key=PackageState\\<SID>\\OpenAI.Codex_*",
    "run_prefetch": "executable_filename=CHATGPT.EXE",
    "run_userassist": "key=UserAssist\\*\\Count;program_name@=ChatGPT.exe",
    "run_global_state": "desktop-first-seen-at-ms>0",
    "run_amcache": "key=Root\\InventoryApplicationFile;Name=ChatGPT.exe",
    "login_auth": "id_token.present=true;access_token.present=true",
    "login_codex_db": "table=local_thread_catalog_hosts;host_id!=empty",
    "login_history": ("table=urls;url@=http://localhost:1455/auth/callback?code=;"
                      "url@=https://chatgpt.com/codex/open-app?source=login&app_brand=chatgpt"),
    "login_cache": "account.created_time!=null",
    "logout_auth": ".codex=exists;auth.json=absent",
    "switch_codex_db": "table=local_thread_catalog_hosts;distinct(host_id)>=2",
    "chat_codex_db": "table=local_thread_catalog;thread_id!=empty",
    "chat_cache": "conversation API key;conversation_id,title,create_time | items.id,title,create_time",
    "chat_global_state": "prompt_history=exists | client-thread-bindings-v1=exists",
    "prompt_input_cache": "conversation_id!=empty;author.role=user;update_time!=null;content=object",
    "prompt_edit_cache": "create_time=exists;children.count>=2",
    "memory_cache": "message@=memcite | message@=memory_percentage_used",
    "upload_cache": "message.metadata.attachments=exists",
    "upload_open_mru": "profile=LastVisitedPidlMRU/Cache_Data match;absolute_path=exists",
    "upload_visited_mru": "executable=ChatGPT.exe",
    "download_mft": "drive=matched download evidence",
    "download_cache": "message.content.parts@=다운로드]",
    "download_open_mru": "profile=LastVisitedPidlMRU/Cache_Data match;absolute_path=exists",
    "download_visited_mru": "executable=ChatGPT.exe",
    "account_delete_cache": ("/auth/logout;logout_reason=confirm_delete_account;"
                             "logout_source=settings.confirm-delete-account-modal"),
    "temporary_chat_cache": "title=New Chat",
    "connector_history": "table=urls;url@=connector_platform_oauth_redirect?state=codex_scheme__oauth",
    "connector_log": "text@=plugin_install_succeeded",
    "connector_package": "collection_status=ok;directory=openai-curated-remote",
    "connector_global_state": "mcp-server-tools-cache-v2.catalog.tools=exists | state@=openai-curated-remote",
    "connector_work_logs": "table=log|logs;feedback_log_body@=@openai-curated-remote)",
    "connector_rollout": "MCP tool call;value@=@openai-curated-remote)",
    "connector_thread_history": "MCP tool call;item_json@=@openai-curated-remote)",
}
_EVIDENCE_ASSIGNMENT = re.compile(r"^(artifacts\[\d+\].+?)=(.+)$", re.DOTALL)
_TIME_FIELDS = frozenset({"iat", "ts", "timestamp", "time", "create_time", "created_time",
                          "source_created_at", "source_updated_at", "update_time", "last_run_time",
                          "last_visit_time", "last_access_utc", "last_executed_kst",
                          "created_at_ms", "opened_on", "last_write_time"})


def _evidence_summary(evidence: str, timestamp: str, fallback: str) -> str:
    """Display one existing matched field and the report time; leave raw evidence intact."""
    for clause in split_evidence_items(evidence):
        match = _EVIDENCE_ASSIGNMENT.match(clause)
        if not match or "._source_locator" in match.group(1):
            continue
        field = match.group(1).rsplit(".", 1)[-1]
        if field in _TIME_FIELDS or field.startswith("_"):
            continue
        raw_value = match.group(2)
        try:
            value = json.loads(raw_value)
        except (ValueError, TypeError):
            value = raw_value
        if isinstance(value, (dict, list)):
            value = fallback
        label = f"{field} = {value}"
        return label + (f" · {timestamp}" if timestamp != "-" else "")
    return fallback + (f" · {timestamp}" if timestamp != "-" else "")


def _summary_markup(evidence: str, timestamp: str, fallback: str) -> str:
    """Apply the reference report's field/value/time typography to safe text."""
    summary = _evidence_summary(evidence, timestamp, fallback)
    time_suffix = f" · {timestamp}" if timestamp != "-" else ""
    label = summary[:-len(time_suffix)] if time_suffix else summary
    field, separator, value = label.partition(" = ")
    if separator:
        readable = ('<span class="rv-field">' + html.escape(field, quote=True)
                    + '</span> = <span class="rv-value">'
                    + html.escape(value, quote=True) + '</span>')
    else:
        readable = '<span class="rv-value">' + html.escape(label, quote=True) + '</span>'
    items = '<li>' + readable + '</li>'
    if time_suffix:
        items += '<li class="rv-time">' + html.escape(timestamp, quote=True) + '</li>'
    return '<ul class="readable">' + items + '</ul>'


def _appendix_rule_fields(definition) -> tuple[str, ...]:
    """Show inventory and implemented detector metadata without inventing tests."""
    filter_text = RULE_FILTERS.get(definition.id, "collection_status=ok")
    time_field = RULE_FIELDS.get(definition.id, ("-", "-", "-", "-"))[3]
    if definition.id in {"upload_open_mru", "download_open_mru"}:
        time_field = "opened_on"
    return filter_text, time_field


# Visual styling adapted from LLMTrace core/team_share.py at commit
# 942f43e3c8b5a36d35955b06be27433a73f9cb76 (visual styles only). Keep report
# content, evidence rules, and behavior order in the existing writer below.
REPORT_CSS = """
:root { color-scheme:light; --ink:#172033; --muted:#64748b; --line:#dce3ed; --panel:#fff; --navy:#17345f; --bg:#f4f7fb; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.55 Arial,"Malgun Gothic",sans-serif; }
main { width:100%; max-width:1900px; margin:0 auto; padding:26px 32px 40px; }
h1 { font-size:28px; margin:0 0 16px; } h2 { font-size:19px; margin:0 0 14px; }
.overview, .results { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:20px; margin:14px 0; box-shadow:0 4px 18px #1720330a; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:10px; }
.stats div { border:1px solid var(--line); border-radius:10px; padding:11px; }
.stats span { display:block; color:var(--muted); font-size:12px; }
.stats strong { font-size:15px; overflow-wrap:anywhere; }
.toolbar { display:flex; gap:9px; flex-wrap:wrap; margin:14px 0; }
.toolbar input { flex:1; min-width:250px; padding:10px 12px; border:1px solid #b8c3d3; border-radius:8px; font:inherit; }
.toolbar button { padding:9px 12px; border:1px solid #9aacbf; background:#fff; border-radius:8px; cursor:pointer; font:inherit; }
details.action { background:#fff; border:1px solid var(--line); border-radius:12px; margin:9px 0; overflow:hidden; }
details.match { background:#fbfcfe; border:1px solid var(--line); border-radius:9px; margin:9px 0; overflow:hidden; }
details.action > summary, details.match > summary { display:flex; align-items:center; gap:10px; padding:14px 16px; cursor:pointer; list-style:none; font-weight:700; }
details.action > summary::-webkit-details-marker, details.match > summary::-webkit-details-marker { display:none; }
details.action > summary::after, details.match > summary::after { content:"＋"; margin-left:auto; color:var(--muted); font-size:18px; }
details.action[open] > summary::after, details.match[open] > summary::after { content:"－"; }
details.action[open] > summary, details.match[open] > summary { border-bottom:1px solid var(--line); }
summary:focus-visible { outline:2px solid var(--navy); outline-offset:-3px; }
.action-id { background:var(--navy); color:#fff; border-radius:6px; padding:3px 7px; font-weight:700; }
.summary-meta, .match-meta { color:var(--muted); font-weight:400; }
.action-body { padding:8px 16px 16px; }
.match-time { display:inline-flex; gap:7px; align-items:center; }
.match-date, .match-clock { border:1px solid var(--line); border-radius:6px; background:#fff; padding:2px 7px; }
.match-body { min-width:0; background:#fff; padding:0 12px; }
.grade { display:inline-block; border-radius:999px; padding:3px 9px; font-size:12px; font-weight:700; white-space:nowrap; }
.direct { background:#dff4e9; color:#155e3d; } .supporting { background:#e8f0ff; color:#244b91; }
.table-wrap { max-width:100%; min-width:0; overflow-x:auto; overflow-y:hidden; scrollbar-gutter:stable; }
table { border-collapse:collapse; width:100%; }
.match-body table { min-width:1420px; table-layout:fixed; }
.match-body th:nth-child(1) { width:250px; }
.match-body th:nth-child(2) { width:250px; }
.match-body th:nth-child(3) { width:300px; }
.match-body th:nth-child(4) { width:100px; }
.match-body th:nth-child(5) { width:520px; }
.appendix table { min-width:1600px; table-layout:fixed; }
.appendix th:nth-child(1), .appendix td:nth-child(1) { width:150px; }
.appendix th:nth-child(2), .appendix td:nth-child(2) { width:145px; }
.appendix th:nth-child(3), .appendix td:nth-child(3) { width:250px; }
.appendix th:nth-child(4), .appendix td:nth-child(4) { width:430px; word-break:break-word; }
.appendix th:nth-child(5), .appendix td:nth-child(5) { width:170px; min-width:170px; white-space:normal; overflow-wrap:anywhere; }
.appendix th:nth-child(6), .appendix td:nth-child(6) { width:350px; word-break:break-word; }
th,td { border-bottom:1px solid var(--line); padding:10px; text-align:left; vertical-align:top; }
th { background:#f7f9fc; color:#31415c; } td { overflow-wrap:anywhere; }
td:first-child { white-space:nowrap; } td:nth-child(3) { font:12px/1.55 Consolas,"Malgun Gothic",monospace; min-width:250px; word-break:break-all; }
td:last-child { white-space:pre-wrap; min-width:280px; } tbody tr:last-child td { border-bottom:0; }
.match-body td:first-child { white-space:normal; overflow-wrap:anywhere; }
.match-body td:nth-child(2) { word-break:break-word; }
.match-body td:nth-child(3) { min-width:300px; }
.match-body td:nth-child(5) { min-width:520px; white-space:normal; word-break:break-word; }
.empty { border:1px dashed #b8c3d3; border-radius:9px; padding:15px; color:var(--muted); }
.layout { display:grid; grid-template-columns:190px minmax(0,1fr); min-height:100vh; }
.layout main, [data-view], .results, details.action, details.match { min-width:0; }
.sidebar { position:sticky; top:0; height:100vh; background:#10294d; color:#fff; padding:22px 12px; }
.sidebar-brand { font-size:20px; font-weight:800; padding:2px 9px 20px; }
.sidebar nav { display:grid; gap:7px; }
.sidebar .nav-tab { width:100%; border:0; border-radius:9px; padding:11px; text-align:left; background:transparent; color:#cad6e8; font:inherit; font-weight:700; cursor:pointer; }
.sidebar .nav-tab:hover { background:#ffffff12; color:#fff; }
.sidebar .nav-tab.active { background:#fff; color:#10294d; }
.rule-id { display:inline-block; font:700 12px/1.4 Consolas,monospace; background:#f1f4f9; border:1px solid var(--line); border-radius:5px; padding:1px 6px; margin:0 4px 4px 0; }
ul.readable { margin:2px 0 6px; padding-left:18px; }
ul.readable li { margin:2px 0; }
.rv-field { color:var(--muted); font-family:Consolas,monospace; font-size:12px; }
.rv-value { font-family:Consolas,monospace; font-size:12px; word-break:break-all; }
.rv-time { color:#244b91; white-space:nowrap; }
details.raw > summary { display:list-item; list-style:disclosure-closed inside; cursor:pointer; color:var(--muted); font-size:12px; padding:2px 0; font-weight:400; }
details.raw[open] > summary { list-style-type:disclosure-open; }
details.raw > summary::after { content:none; }
details.raw[open] > summary { border:0; }
details.raw pre { white-space:pre-wrap; word-break:break-all; font:12px/1.55 Consolas,"Malgun Gothic",monospace; background:#f7f9fc; border:1px solid var(--line); border-radius:6px; padding:8px; margin:4px 0 0; }
.appendix td { font-size:13px; } .appendix td.mono { font:12px Consolas,monospace; }
[hidden] { display:none !important; }
@media(max-width:700px) { main { padding:14px; } .results,.overview { padding:14px; } .layout { display:block; } .sidebar { position:static; height:auto; padding:12px; } .sidebar-brand { padding:2px 9px 10px; } .sidebar nav { grid-template-columns:repeat(2,minmax(0,1fr)); } }
"""


REPORT_JS = """
const views = Array.from(document.querySelectorAll('[data-view]'));
for (const button of document.querySelectorAll('[data-view-button]')) {
  button.addEventListener('click', () => {
    const selected = button.dataset.viewButton;
    for (const view of views) view.hidden = view.dataset.view !== selected;
    for (const nav of document.querySelectorAll('[data-view-button]')) {
      nav.classList.toggle('active', nav === button);
      nav.setAttribute('aria-selected', nav === button ? 'true' : 'false');
    }
  });
}
const search = document.getElementById('search');
const actions = Array.from(document.querySelectorAll('details.action'));
function filterActions() {
  const query = search.value.trim().toLocaleLowerCase();
  for (const action of actions) {
    const actionMatches = action.querySelector('.action-name').textContent.toLocaleLowerCase().includes(query);
    let visibleRows = 0;
    for (const match of action.querySelectorAll('details.match')) {
      let matchRows = 0;
      for (const row of match.querySelectorAll('tbody tr')) {
        const cells = row.cells;
        const matches = !query || actionMatches || [cells[1], cells[2]].some(cell => cell.textContent.toLocaleLowerCase().includes(query));
        row.hidden = !matches;
        if (matches) matchRows++;
      }
      match.hidden = matchRows === 0;
      visibleRows += matchRows;
    }
    action.hidden = !!query && !actionMatches && visibleRows === 0;
  }
}
search.addEventListener('input', filterActions);
document.getElementById('expand').addEventListener('click', () => {
  for (const action of actions) if (!action.hidden) {
    action.open = true;
    for (const match of action.querySelectorAll('details.match')) if (!match.hidden) match.open = true;
  }
});
document.getElementById('collapse').addEventListener('click', () => {
  for (const action of actions) {
    action.open = false;
    for (const match of action.querySelectorAll('details.match')) match.open = false;
  }
});
"""


def prepare_report_rows(rows: Sequence[Mapping[str, str]]) -> tuple[dict[str, str], ...]:
    """Validate and combine six-column rule/file/event rows, then sort by KST."""
    combined: dict[tuple[str, str, str | None], ReportRow] = {}
    for row in rows:
        if set(row) != set(CSV_COLUMNS):
            raise ValueError("report rows must contain exactly the six fixed columns")
        values = tuple(row[column] for column in CSV_COLUMNS)
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("report row fields must be nonempty text")
        if row["등급"] not in {"직접", "보조"}:
            raise ValueError("report evidence grade must be 직접 or 보조")
        path = PureWindowsPath(row["경로"])
        if not path.is_absolute() or not path.drive:
            raise ValueError("report artifact path must be an absolute source path")
        rule_id = row.rule_id if isinstance(row, ReportRow) else None
        event_key = row.event_key if isinstance(row, ReportRow) else None
        normalized = ReportRow(dict(zip(CSV_COLUMNS, values)), rule_id=rule_id, event_key=event_key)
        normalized["사용자 행위"] = ACTION_ALIASES.get(normalized["사용자 행위"], normalized["사용자 행위"])
        identity = (rule_id or normalized["사용자 행위"] + "\x00" + normalized["아티팩트"]
                    + "\x00" + normalized["등급"],
                    str(path).casefold(), event_key)
        prior = combined.get(identity)
        if prior is None:
            combined[identity] = normalized
            continue
        if normalized["시각"] != "-" and (prior["시각"] == "-" or normalized["시각"] < prior["시각"]):
            prior["시각"] = normalized["시각"]
        evidence_items = list(split_evidence_items(prior["근거"]))
        for evidence_item in split_evidence_items(normalized["근거"]):
            if evidence_item not in evidence_items:
                evidence_items.append(evidence_item)
        prior["근거"] = "; ".join(evidence_items)

    def sort_key(row: ReportRow):
        action_rank = int(ACTION_ID_BY_NAME[row["사용자 행위"]][1:]) if row["사용자 행위"] in ACTION_ID_BY_NAME else 15
        return (row["시각"] == "-", row["시각"] if row["시각"] != "-" else "",
                action_rank, row["경로"].casefold(), row.rule_id or "")

    return tuple(sorted(combined.values(), key=sort_key))


def write_csv_report(output_path: Path, rows: Sequence[Mapping[str, str]]) -> int:
    """Write the shared six-column report rows as UTF-8 with BOM."""
    prepared = prepare_report_rows(rows)
    if output_path.exists():
        raise FileExistsError(f"CSV report already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staged = output_path.with_name(output_path.name + ".partial")
    try:
        with staged.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="raise")
            writer.writeheader()
            writer.writerows(prepared)
        os.replace(staged, output_path)
    finally:
        staged.unlink(missing_ok=True)
    return len(prepared)


def _ordered_sections(rows: Sequence[Mapping[str, str]]) -> list[tuple[str, list[Mapping[str, str]]]]:
    grouped: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["사용자 행위"]].append(row)

    sections: list[tuple[str, list[Mapping[str, str]]]] = []
    for category in ACTION_ORDER:
        sections.append((category, grouped.pop(category, [])))
    sections.extend((name, records) for name, records in grouped.items())
    return sections


def write_html_report(
    output_path: Path, rows: Sequence[Mapping[str, str]], *,
    source_type: str, actions_detected: int,
    direct_evidence_count: int | None = None,
    supporting_evidence_count: int | None = None,
) -> int:
    """Render CSV rows in initially closed action and timestamp accordions."""
    prepared = prepare_report_rows(rows)
    input_format = {"E01": "E01", "Live": "live system"}.get(source_type)
    if input_format is None:
        raise ValueError(f"unsupported source type for HTML report: {source_type}")
    if type(actions_detected) is not int or actions_detected < 0:
        raise ValueError("actions_detected must be a nonnegative integer")
    evidence_counts = {"직접": set(), "보조": set()}
    for row in prepared:
        if row["사용자 행위"] in ACTION_ID_BY_NAME:
            evidence_counts[row["등급"]].add((row["사용자 행위"],
                getattr(row, "rule_id", None) or row["아티팩트"]))
    for count in (direct_evidence_count, supporting_evidence_count):
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError("evidence counts must be nonnegative integers")
    direct = len(evidence_counts["직접"]) if direct_evidence_count is None else direct_evidence_count
    supporting = (len(evidence_counts["보조"]) if supporting_evidence_count is None
                  else supporting_evidence_count)
    if output_path.exists():
        raise FileExistsError(f"HTML report already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staged = output_path.with_name(output_path.name + ".partial")
    try:
        with staged.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("<!doctype html>\n<html lang=\"ko\">\n<head>\n")
            handle.write("<meta charset=\"utf-8\">\n"
                         "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
                         "<title>LLM_Artifact_Report</title>\n")
            handle.write("<style>\n" + REPORT_CSS + "</style>\n</head>\n<body>\n")
            handle.write('<div class="layout"><aside class="sidebar">'
                         '<div class="sidebar-brand">ChatGPT Desktop</div>'
                         '<nav aria-label="보고서 메뉴">'
                         '<button type="button" class="nav-tab active" data-view-button="analysis" aria-selected="true">종합분석</button>'
                         '<button type="button" class="nav-tab" data-view-button="rules" aria-selected="false">판정 규칙</button>'
                         '</nav></aside><main><h1>LLM_Artifact_Report</h1><div data-view="analysis">\n')
            handle.write(
                '<section class="overview" aria-label="분석 개요">\n'
                '<h2>분석 개요</h2>\n<div class="stats">\n'
                '<div><span>분석 대상</span><strong>ChatGPT Desktop</strong></div>\n'
                '<div><span>입력 형식</span><strong>' + input_format + '</strong></div>\n'
                '<div><span>매칭 행위</span><strong>' + str(actions_detected) + '개</strong></div>\n'
                '<div><span>근거 개수</span><strong>직접 ' + str(direct)
                + ' / 보조 ' + str(supporting) + '</strong></div>\n'
                '</div>\n</section>\n'
            )
            handle.write('<section class="results"><h2>행위별 매칭 결과</h2>\n'
                         '<div class="toolbar"><input id="search" type="search" '
                         'aria-label="행위명, 아티팩트 또는 경로 검색" '
                         'placeholder="행위명, 아티팩트 또는 경로 검색">'
                         '<button id="expand" type="button">모두 펼치기</button>'
                         '<button id="collapse" type="button">모두 접기</button></div>\n')
            for behavior, records in _ordered_sections(prepared):
                action_id = ACTION_ID_BY_NAME.get(behavior)
                badge = ('<span class="action-id">' + action_id + "</span> " if action_id else "")
                handle.write('<details class="action">\n<summary>' + badge
                             + '<span class="action-name">' + html.escape(behavior, quote=True)
                             + '</span><span class="summary-meta">매칭 ' + str(len(records))
                             + '건</span></summary>\n<div class="action-body">\n')
                if not records:
                    handle.write('<p class="empty">해당 아티팩트 없음</p>\n')
                else:
                    by_time: dict[str, list[Mapping[str, str]]] = defaultdict(list)
                    for row in records:
                        by_time[row["시각"]].append(row)
                    for timestamp, time_rows in by_time.items():
                        if timestamp == "-":
                            time_label = '<span class="match-date">시각 없음</span>'
                        else:
                            date, _, clock = timestamp.partition(" ")
                            time_label = ('<span class="match-date">'
                                          + html.escape(date, quote=True) + '</span>'
                                          '<span class="match-clock">'
                                          + html.escape(clock, quote=True) + '</span>')
                        handle.write('<details class="match"><summary><span class="match-time">'
                                     + time_label + '</span><span class="match-meta">매칭 '
                                     + str(len(time_rows)) + '건</span></summary>\n'
                                     '<div class="match-body"><div class="table-wrap"><table>\n<thead><tr>'
                                     '<th>시각</th><th>아티팩트</th><th>경로</th><th>등급</th><th>근거</th>'
                                     '</tr></thead>\n<tbody>\n')
                        for row in time_rows:
                            cells = []
                            for column in ("시각", "아티팩트", "경로", "등급", "근거"):
                                value = html.escape(row[column], quote=True)
                                if column == "등급":
                                    grade_class = "direct" if row[column] == "직접" else "supporting"
                                    value = f'<span class="grade {grade_class}">{value}</span>'
                                elif column == "근거":
                                    internal_id = getattr(row, "rule_id", None)
                                    rule_id = RULE_IDS.get(internal_id, "-")
                                    rule = RULE_BY_ID.get(internal_id)
                                    fallback = (_report_content(rule, row["근거"]) if rule else "근거 기록 확인")
                                    value = ('<span class="rule-id">' + html.escape(rule_id, quote=True)
                                             + '</span>'
                                             + _summary_markup(row["근거"], row["시각"], fallback)
                                             + '<details class="raw"><summary>원본 근거 보기</summary><pre>'
                                             + value + '</pre></details>')
                                cells.append("<td>" + value + "</td>")
                            handle.write("<tr>" + "".join(cells) + "</tr>\n")
                        handle.write("</tbody>\n</table></div></div>\n</details>\n")
                handle.write("</div>\n</details>\n")
            handle.write('</section></div>\n<div data-view="rules" hidden>'
                         '<section class="results appendix"><h2>부록: 판정 규칙</h2>'
                         '<div class="table-wrap"><table><thead><tr>'
                         '<th>규칙 ID</th><th>행위</th><th>아티팩트</th><th>필터</th>'
                         '<th>시각 필드</th><th>비고</th>'
                         '</tr></thead><tbody>')
            for definition in ARTIFACT_DEFINITIONS:
                behavior = ACTION_ALIASES.get(definition.behavior, definition.behavior)
                action = ACTION_ID_BY_NAME.get(behavior, "비고") + " " + behavior
                filter_text, time_field = _appendix_rule_fields(definition)
                fields = (definition.id, action, definition.artifact, filter_text,
                          time_field, definition.description)
                handle.write('<tr>' + ''.join('<td' + (' class="mono"' if index in {0, 3, 4} else '')
                                               + '>' + html.escape(value, quote=True) + '</td>'
                                               for index, value in enumerate(fields)) + '</tr>\n')
            handle.write('</tbody></table></div></section></div>\n</main></div>\n<script>\n' + REPORT_JS
                         + "</script>\n</body>\n</html>\n")
        os.replace(staged, output_path)
    finally:
        staged.unlink(missing_ok=True)
    return len(prepared)
