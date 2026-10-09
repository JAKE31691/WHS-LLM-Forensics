"""Render one shared set of condition-backed and collected-artifact report rows."""

from __future__ import annotations

import csv
import html
import os
from collections import defaultdict
from pathlib import Path, PureWindowsPath
from typing import Mapping, Sequence

from action_rules import (ACTION_ALIASES, ACTION_ID_BY_NAME, ACTION_TAXONOMY,
                          CSV_COLUMNS, NOTES_BEHAVIOR, ReportRow, split_evidence_items)


ACTION_ORDER = tuple(name for _, name in ACTION_TAXONOMY) + (NOTES_BEHAVIOR,)


# Visual styling adapted from LLMTrace core/team_share.py at commit
# 942f43e3c8b5a36d35955b06be27433a73f9cb76 (visual styles only). Keep report
# content, evidence rules, and behavior order in the existing writer below.
REPORT_CSS = """
:root { color-scheme:light; --ink:#172033; --muted:#64748b; --line:#dce3ed; --panel:#fff; --navy:#17345f; --bg:#f4f7fb; }
* { box-sizing:border-box; }
body { max-width:1900px; margin:0 auto; padding:26px 32px 40px; background:var(--bg); color:var(--ink); font:14px/1.55 Arial,"Malgun Gothic",sans-serif; }
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
summary { display:flex; align-items:center; gap:10px; padding:14px 16px; cursor:pointer; list-style:none; font-weight:700; }
summary::-webkit-details-marker { display:none; }
summary::after { content:"＋"; margin-left:auto; color:var(--muted); font-size:18px; }
details[open]>summary::after { content:"－"; }
details[open]>summary { border-bottom:1px solid var(--line); }
summary:focus-visible { outline:2px solid var(--navy); outline-offset:-3px; }
.action-id { background:var(--navy); color:#fff; border-radius:6px; padding:3px 7px; font-weight:700; }
.summary-meta, .match-meta { color:var(--muted); font-weight:400; }
.action-body { padding:8px 16px 16px; }
.match-time { display:inline-flex; gap:7px; align-items:center; }
.match-date, .match-clock { border:1px solid var(--line); border-radius:6px; background:#fff; padding:2px 7px; }
.match-body { background:#fff; padding:0 12px; }
.grade { display:inline-block; border-radius:999px; padding:3px 9px; font-size:12px; font-weight:700; white-space:nowrap; }
.direct { background:#dff4e9; color:#155e3d; } .supporting { background:#e8f0ff; color:#244b91; }
.table-wrap { overflow:auto; } table { border-collapse:collapse; width:100%; }
th,td { border-bottom:1px solid var(--line); padding:10px; text-align:left; vertical-align:top; }
th { background:#f7f9fc; color:#31415c; } td { overflow-wrap:anywhere; }
td:first-child { white-space:nowrap; } td:nth-child(3) { font:12px/1.55 Consolas,"Malgun Gothic",monospace; min-width:250px; word-break:break-all; }
td:last-child { white-space:pre-wrap; min-width:280px; } tbody tr:last-child td { border-bottom:0; }
.empty { border:1px dashed #b8c3d3; border-radius:9px; padding:15px; color:var(--muted); }
[hidden] { display:none !important; }
@media(max-width:700px) { body { padding:14px; } .results,.overview { padding:14px; } }
"""


REPORT_JS = """
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
    """Validate and combine rule/file/event, then sort known KST times first."""
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
            handle.write("<h1>LLM_Artifact_Report</h1>\n<main>\n")
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
                                     '<div class="match-body table-wrap"><table>\n<thead><tr>'
                                     '<th>시각</th><th>아티팩트</th><th>경로</th><th>등급</th><th>근거</th>'
                                     '</tr></thead>\n<tbody>\n')
                        for row in time_rows:
                            cells = []
                            for column in ("시각", "아티팩트", "경로", "등급", "근거"):
                                value = html.escape(row[column], quote=True)
                                if column == "등급":
                                    grade_class = "direct" if row[column] == "직접" else "supporting"
                                    value = f'<span class="grade {grade_class}">{value}</span>'
                                cells.append("<td>" + value + "</td>")
                            handle.write("<tr>" + "".join(cells) + "</tr>\n")
                        handle.write("</tbody>\n</table></div>\n</details>\n")
                handle.write("</div>\n</details>\n")
            handle.write("</section>\n</main>\n<script>\n" + REPORT_JS
                         + "</script>\n</body>\n</html>\n")
        os.replace(staged, output_path)
    finally:
        staged.unlink(missing_ok=True)
    return len(prepared)
