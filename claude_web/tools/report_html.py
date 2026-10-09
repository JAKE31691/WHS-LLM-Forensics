"""HTML 보고서 — 팀 예시(report.py가 만든 LLM_Artifact_Report.html)와 같은 모양으로 쓴다.

- 분석 개요: 분석 대상 / 입력 형식 / 매칭 행위 / 근거 개수
- 행위별 매칭 결과: 검색창, 모두 펼치기·접기 버튼, 행위 토글(A01~A14) 안에 시각별 토글, 그 안에 표
  표 열: 시각 / 아티팩트 / 경로 / 등급 / 근거 (CSV와 같은 값)
- 처음에는 모두 접혀 있다. 파일 하나로 끝나고 외부 파일을 불러오지 않는다.
- 매칭이 0건인 행위는 팀 표준에 맞춰 '해당 아티팩트 없음'만 쓴다 (규칙 없음·경로 없음·읽기 실패·매칭 없음 구분 없이).
  이유별 상태는 콘솔 출력(main.py)에 그대로 남는다.
- 매칭이 있는 행위는 표 아래에, 행을 만들지 못한 아티팩트의 상태를 적는다:
  Inconclusive (Path not found) / Inconclusive (Path exists but unreadable) / not detected
- 근거에 프롬프트 원문이 들어갈 수 있으므로 모든 값을 이스케이프한다.

CSS와 검색·펼치기 스크립트는 예시 report.py의 REPORT_CSS, REPORT_JS를 그대로 가져왔다.
바꾼 것은 한 줄: 아티팩트 열(2번째)에 최소 너비를 주었다 (IndexedDB 폴더 이름처럼 긴 이름이 한 글자씩 꺾이지 않게).
"""
from collections import OrderedDict
from html import escape

from definitions import ACTIONS, TARGET

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
td:first-child { white-space:nowrap; } td:nth-child(2) { min-width:150px; word-break:break-all; } td:nth-child(3) { font:12px/1.55 Consolas,"Malgun Gothic",monospace; min-width:250px; word-break:break-all; }
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

COLUMNS = ("시각", "아티팩트", "경로", "등급", "근거")

# 행이 없는 아티팩트의 상태 문구 (사용자 지정 세 가지)
STATE_LABELS = (
    ("path_missing", "Inconclusive (Path not found)"),
    ("read_failed", "Inconclusive (Path exists but unreadable)"),
    ("no_match", "not detected"),
)

# 팀 표준: 매칭이 0건인 행위는 이유와 상관없이 이 문구 하나만 쓴다
NO_MATCH_TEXT = "해당 아티팩트 없음"


def _e(s):
    return escape("" if s is None else str(s), quote=True)


def _state_lines(st):
    if st["state"] == "unsupported":
        return ["Inconclusive (No rules defined)"]
    out = []
    for key, label in STATE_LABELS:
        items = st["artifacts"].get(key) or []
        if items:
            out.append("%s: %s" % (label, ", ".join(items)))
    return out


def _cells(r):
    grade_cls = "direct" if r.grade == "직접" else "supporting"
    values = (r.time_text, r.artifact, r.path, None, r.evidence)
    cells = []
    for col, v in zip(COLUMNS, values):
        if col == "등급":
            cells.append('<td><span class="grade %s">%s</span></td>' % (grade_cls, _e(r.grade)))
        else:
            cells.append("<td>%s</td>" % _e(v))
    return "".join(cells)


def write_html(rows, status, info, path):
    """info: source_type("E01"/"Live system"), actions_detected, direct, support"""
    input_format = "E01" if info["source_type"] == "E01" else "live system"
    out = ['<!doctype html>\n<html lang="ko">\n<head>\n',
           '<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
           '<title>LLM_Artifact_Report</title>\n',
           "<style>\n" + REPORT_CSS + "</style>\n</head>\n<body>\n",
           "<h1>LLM_Artifact_Report</h1>\n<main>\n",
           '<section class="overview" aria-label="분석 개요">\n<h2>분석 개요</h2>\n<div class="stats">\n'
           '<div><span>분석 대상</span><strong>%s</strong></div>\n'
           '<div><span>입력 형식</span><strong>%s</strong></div>\n'
           '<div><span>매칭 행위</span><strong>%d개</strong></div>\n'
           '<div><span>근거 개수</span><strong>직접 %d / 보조 %d</strong></div>\n'
           '</div>\n</section>\n' % (_e(TARGET), _e(input_format), info["actions_detected"],
                                     info["direct"], info["support"]),
           '<section class="results"><h2>행위별 매칭 결과</h2>\n'
           '<div class="toolbar"><input id="search" type="search" aria-label="행위명, 아티팩트 또는 경로 검색" '
           'placeholder="행위명, 아티팩트 또는 경로 검색">'
           '<button id="expand" type="button">모두 펼치기</button>'
           '<button id="collapse" type="button">모두 접기</button></div>\n']

    for aid, ko, _ in ACTIONS:
        st = status[aid]
        arows = [r for r in rows if r.action_id == aid]
        out.append('<details class="action">\n<summary><span class="action-id">%s</span> '
                   '<span class="action-name">%s</span><span class="summary-meta">매칭 %d건</span></summary>\n'
                   '<div class="action-body">\n' % (_e(aid), _e(ko), len(arows)))
        groups = OrderedDict()                       # 시각별로 묶는다 (행은 이미 시간순, '-'는 뒤)
        for r in arows:
            groups.setdefault(r.time_text, []).append(r)
        for t, grows in groups.items():
            if t == "-":
                label = '<span class="match-date">시각 없음</span>'
            else:
                date, _, clock = t.partition(" ")
                label = '<span class="match-date">%s</span><span class="match-clock">%s</span>' % (_e(date), _e(clock))
            out.append('<details class="match"><summary><span class="match-time">%s</span>'
                       '<span class="match-meta">매칭 %d건</span></summary>\n'
                       '<div class="match-body table-wrap"><table>\n<thead><tr>%s</tr></thead>\n<tbody>\n'
                       % (label, len(grows), "".join("<th>%s</th>" % c for c in COLUMNS)))
            for r in grows:
                out.append("<tr>%s</tr>\n" % _cells(r))
            out.append("</tbody>\n</table></div>\n</details>\n")
        if not arows:
            out.append('<p class="empty">%s</p>\n' % _e(NO_MATCH_TEXT))
        else:                                   # 매칭이 있는 행위는 행을 못 만든 아티팩트의 상태를 표 아래에 적는다
            for line in _state_lines(st):
                out.append('<p class="empty">%s</p>\n' % _e(line))
        out.append("</div>\n</details>\n")

    out.append("</section>\n</main>\n<script>\n" + REPORT_JS + "</script>\n</body>\n</html>\n")
    with open(path, "w", encoding="utf-8", newline="\n", errors="backslashreplace") as f:
        f.write("".join(out))
