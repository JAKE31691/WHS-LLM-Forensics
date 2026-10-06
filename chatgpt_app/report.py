"""Render one shared set of condition-backed and collected-artifact report rows."""

from __future__ import annotations

import csv
import html
import os
from collections import defaultdict
from pathlib import Path, PureWindowsPath
from typing import Mapping, Sequence

from action_rules import CSV_COLUMNS


ACTION_ORDER = (
    "설치", "실행", "로그인", "계정 전환", "새 대화 생성", "프롬프트 입력",
    "프롬프트 수정", "메모리 사용 / 관리", "파일 업로드",
    "생성물 / 파일 다운로드", "대화 삭제", "계정 삭제", "임시 채팅", "외부 서비스 연동",
)

# Ordering aliases only. Row values and accordion titles retain the exact CSV
# behavior when it differs from the requested table-of-contents wording.
ORDER_ALIASES = {
    "로그인(인증)": "로그인",
    "생성물/파일 다운로드": "생성물 / 파일 다운로드",
    "외부 서비스 연동(connector / plugin)": "외부 서비스 연동",
}


# Visual styling adapted from the supplied reference report. Keep report
# content, evidence rules, and behavior order in the existing writer below.
REPORT_CSS = """
:root {
    color-scheme: light;
    --ink: #172033;
    --muted: #64748b;
    --line: #dbe3ef;
    --panel: #fff;
    --accent: #3157d5;
}
* { box-sizing: border-box; }
body {
    max-width: 1440px;
    margin: 0 auto;
    padding: 32px;
    font: 14px/1.5 Inter, "Segoe UI", Arial, sans-serif;
    color: var(--ink);
    background: #f5f7fb;
}
h1 { margin: 8px 0 26px; font-size: 30px; line-height: 1.25; }
main { display: grid; gap: 16px; }
details {
    min-width: 0;
    overflow: hidden;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 14px;
    box-shadow: 0 3px 14px #1e293b0a;
}
summary {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 18px 22px;
    cursor: pointer;
    font-size: 17px;
    font-weight: 600;
    list-style: none;
}
summary::-webkit-details-marker { display: none; }
summary::before {
    content: "›";
    display: inline-block;
    color: var(--accent);
    font-size: 25px;
    font-weight: 400;
    line-height: .7;
    transition: transform .15s ease;
}
details[open] summary { border-bottom: 1px solid var(--line); }
details[open] summary::before { transform: rotate(90deg); }
summary:focus-visible { outline: 2px solid var(--accent); outline-offset: -4px; }
details > p { margin: 0; padding: 20px 22px; color: var(--muted); }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th, td {
    padding: 11px 14px;
    border-bottom: 1px solid var(--line);
    text-align: left;
    vertical-align: top;
}
th { background: #f8fafc; color: var(--muted); font-weight: 600; }
td { overflow-wrap: anywhere; }
td:first-child { font-weight: 600; }
td:nth-child(2) { font-family: Consolas, "Courier New", monospace; font-size: 12px; }
td:last-child { white-space: nowrap; font-weight: 600; }
tbody tr:hover { background: #fbfdff; }
tbody tr:last-child td { border-bottom: 0; }
@media (max-width: 700px) {
    body { padding: 14px; }
    h1 { font-size: 26px; }
    summary { padding: 15px; }
    th, td { padding: 10px; }
}
"""


def prepare_report_rows(rows: Sequence[Mapping[str, str]]) -> tuple[dict[str, str], ...]:
    """Validate and deduplicate once before rendering either report format.

    Duplicate rows are collapsed by all four values, with Windows paths
    compared without regard to case or slash style. The first source path is
    retained unchanged from the collection result.
    """
    prepared: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        if set(row) != set(CSV_COLUMNS):
            raise ValueError("report rows must contain exactly the four fixed columns")
        values = tuple(row[column] for column in CSV_COLUMNS)
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("report row fields must be nonempty text")
        if row["아티팩트 등급"] not in {"직접", "보조"}:
            raise ValueError("report evidence grade must be 직접 or 보조")
        path = PureWindowsPath(row["경로"])
        if not path.is_absolute() or not path.drive:
            raise ValueError("report artifact path must be an absolute source path")
        identity = (values[0], values[1], str(path).casefold(), values[3])
        if identity in seen:
            continue
        seen.add(identity)
        prepared.append(dict(zip(CSV_COLUMNS, values)))
    return tuple(prepared)


def write_csv_report(output_path: Path, rows: Sequence[Mapping[str, str]]) -> int:
    """Write the shared four-column report rows as UTF-8 with BOM."""
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
        grouped[row["행위 분류"]].append(row)

    sections: list[tuple[str, list[Mapping[str, str]]]] = []
    for category in ACTION_ORDER:
        names = [name for name in grouped if ORDER_ALIASES.get(name, name) == category]
        if names:
            sections.extend((name, grouped.pop(name)) for name in names)
        else:
            sections.append((category, []))
    sections.extend((name, records) for name, records in grouped.items())
    return sections


def write_html_report(output_path: Path, rows: Sequence[Mapping[str, str]]) -> int:
    """Render the same report rows with native, initially closed accordions."""
    prepared = prepare_report_rows(rows)
    if output_path.exists():
        raise FileExistsError(f"HTML report already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staged = output_path.with_name(output_path.name + ".partial")
    try:
        with staged.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("<!doctype html>\n<html lang=\"ko\">\n<head>\n")
            handle.write("<meta charset=\"utf-8\">\n"
                         "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
                         "<title>ChatGPT Desktop</title>\n")
            handle.write("<style>\n" + REPORT_CSS + "</style>\n</head>\n<body>\n")
            handle.write("<h1>ChatGPT Desktop</h1>\n<main>\n")
            for behavior, records in _ordered_sections(prepared):
                handle.write("<details>\n<summary>" + html.escape(behavior, quote=True) + "</summary>\n")
                if not records:
                    handle.write("<p>해당 아티팩트 없음</p>\n")
                else:
                    handle.write("<div class=\"table-wrap\"><table>\n<thead><tr><th>아티팩트</th><th>경로</th>"
                                 "<th>아티팩트 등급</th></tr></thead>\n<tbody>\n")
                    for row in records:
                        handle.write("<tr>" + "".join(
                            "<td>" + html.escape(row[column], quote=True) + "</td>"
                            for column in CSV_COLUMNS[1:]
                        ) + "</tr>\n")
                    handle.write("</tbody>\n</table></div>\n")
                handle.write("</details>\n")
            handle.write("</main>\n</body>\n</html>\n")
        os.replace(staged, output_path)
    finally:
        staged.unlink(missing_ok=True)
    return len(prepared)
