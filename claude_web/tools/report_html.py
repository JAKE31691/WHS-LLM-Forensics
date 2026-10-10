"""HTML 보고서 — PM 예시(LLM_Artifact_Report.html, 사이드바·판정 규칙 탭이 있는 판)와 같은 모양으로 쓴다.

- 왼쪽 사이드바: 분석 대상 이름, '종합분석' / '판정 규칙' 탭
- 종합분석: 분석 개요(분석 대상 / 입력 형식 / 매칭 행위 / 근거 개수),
  검색창과 모두 펼치기·접기, 행위(A01~A14) 토글 → 시각별 토글 → 표(시각 / 아티팩트 / 경로 / 등급 / 근거)
- 근거 칸: 규칙의 아티팩트 이름 배지(History, Cookies ...) → 읽기 쉬운 줄(대표로 맞은 '필드 = 값', 시각)
  → '원본 근거 보기'를 누르면 CSV 근거 열과 같은 원문
- 매칭이 0건인 행위는 PM 예시처럼 '해당 아티팩트 없음'을 쓰고, 뒤에 세부 상태만 붙인다
  (어떤 아티팩트인지는 쓰지 않는다. 수집한 아티팩트는 ZIP에, 규칙은 판정 규칙 탭에 있다):
    해당 아티팩트 없음 (경로 없음)
    해당 아티팩트 없음 (있지만 읽기 실패)
    해당 아티팩트 없음 (읽었지만 매칭 없음)
    해당 아티팩트 없음 (판정 규칙 없음)        <- 규칙이 없는 행위(A04, A06, A12)
  한 행위에 상태가 여럿이면 상태마다 한 줄씩 쓴다.
  매칭이 있는 행위도 못 읽은 아티팩트가 있으면 '일부 아티팩트를 읽지 못함 (있지만 읽기 실패)'를 적는다
  (매칭 건수가 덜 나왔을 수 있으므로).
  SHOW_STATE_DETAIL = False로 바꾸면 PM 예시와 똑같이 '해당 아티팩트 없음'만 쓴다.
- 판정 규칙 탭: rules.csv의 규칙 표 (규칙 ID / 행위 / 아티팩트 / 필터 / 시각 필드 / 비고)
- 처음에는 모두 접혀 있다. 파일 하나로 끝나고 외부 파일을 불러오지 않는다.
- 근거에 프롬프트 원문이 들어갈 수 있으므로 모든 값을 이스케이프한다.

CSS와 스크립트는 PM 예시 HTML의 것을 그대로 가져왔다.
"""
import json
from collections import OrderedDict
from html import escape

from definitions import ACTION_NAME, ACTIONS, TARGET

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

COLUMNS = ("시각", "아티팩트", "경로", "등급", "근거")
RULE_COLUMNS = ("규칙 ID", "행위", "아티팩트", "필터", "시각 필드", "비고")
MAX_VALUE_CHARS = 300

NO_MATCH_TEXT = "해당 아티팩트 없음"      # PM 예시의 0건 문구
SHOW_STATE_DETAIL = True                   # False: PM 예시와 똑같이 NO_MATCH_TEXT만 쓴다

# 행이 없는 아티팩트의 세부 상태 (출력 순서대로)
STATE_LABELS = (
    ("path_missing", "경로 없음"),
    ("read_failed", "있지만 읽기 실패"),
    ("no_match", "읽었지만 매칭 없음"),
)
# rules.csv의 op를 판정 규칙 탭에서 쓰는 기호 (filter 열과 같은 기호)
OP_SYMBOL = {"equals": "=", "contains": "~=", "startswith": "^=", "endswith": "$="}


def _e(s):
    return escape("" if s is None else str(s), quote=True)


def _plain_value(v, needle=None):
    """읽기 쉬운 줄의 값: 문자열은 따옴표 없이, 300자가 넘으면 찾은 부분이 보이게 잘라 '…(총 N자)'"""
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    if len(s) <= MAX_VALUE_CHARS:
        return s
    start = 0
    if needle and needle in s:
        i = s.find(needle)
        if i > MAX_VALUE_CHARS // 3:
            start = min(i - MAX_VALUE_CHARS // 3, len(s) - MAX_VALUE_CHARS)
    head = "…" if start else ""
    tail = "…" if start + MAX_VALUE_CHARS < len(s) else ""
    return "%s%s%s(총 %d자)" % (head, s[start:start + MAX_VALUE_CHARS], tail, len(s))


def _state_lines(st):
    """행위 토글 안 회색 상자에 쓰는 줄들"""
    if not SHOW_STATE_DETAIL:
        return [NO_MATCH_TEXT] if st["state"] != "detected" else []
    if st["state"] == "unsupported":
        return ["%s (판정 규칙 없음)" % NO_MATCH_TEXT]
    a = st["artifacts"]
    if st["state"] == "detected":
        failed = a.get("read_failed") or []
        return ["일부 아티팩트를 읽지 못함 (있지만 읽기 실패)"] if failed else []
    return ["%s (%s)" % (NO_MATCH_TEXT, label) for key, label in STATE_LABELS if a.get(key)]


def _evidence_cell(r):
    out = ['<span class="rule-id">%s</span><ul class="readable">' % _e(r.rule_artifact or r.artifact)]
    if r.key_field:
        out.append('<li><span class="rv-field">%s</span> = <span class="rv-value">%s</span></li>'
                   % (_e(r.key_field), _e(_plain_value(r.key_value, r.key_needle))))
    if r.time_text != "-":
        out.append('<li class="rv-time">%s</li>' % _e(r.time_text))
    out.append('</ul><details class="raw"><summary>원본 근거 보기</summary><pre>%s</pre></details>' % _e(r.evidence))
    return "".join(out)


def _row_html(r):
    grade_cls = "direct" if r.grade == "직접" else "supporting"
    return ("<tr><td>%s</td><td>%s</td><td>%s</td><td><span class=\"grade %s\">%s</span></td><td>%s</td></tr>\n"
            % (_e(r.time_text), _e(r.artifact), _e(r.path), grade_cls, _e(r.grade), _evidence_cell(r)))


def rule_condition(rule):
    """판정 규칙 탭의 필터 칸: filter 열과 field·op·value를 ';'로 이어 한 줄로
    예) table=urls;url@=claude.ai;url~=https://claude.ai/"""
    parts = ["table=%s" % rule.table] if rule.table else []
    parts += ["%s%s%s" % (k, op, v) for k, op, v in rule.filters]
    if rule.op == "custom":
        parts.append("custom:%s" % rule.value)
    else:
        parts.append("%s%s%s" % (rule.field, OP_SYMBOL.get(rule.op, rule.op), rule.value))
    return ";".join(parts)


def _rules_table(rules):
    out = ['<div data-view="rules" hidden><section class="results appendix"><h2>부록: 판정 규칙</h2>'
           '<div class="table-wrap"><table><thead><tr>%s</tr></thead><tbody>'
           % "".join("<th>%s</th>" % c for c in RULE_COLUMNS)]
    for rule in rules:
        out.append('<tr><td class="mono">%s</td><td>%s</td><td>%s</td><td class="mono">%s</td>'
                   '<td class="mono">%s</td><td>%s</td></tr>\n'
                   % (_e(rule.rule_id), _e("%s %s" % (rule.action_id, ACTION_NAME.get(rule.action_id, ""))),
                      _e(rule.artifact), _e(rule_condition(rule)),
                      _e("|".join(rule.time_fields) or "-"), _e(rule.note)))
    out.append("</tbody></table></div></section></div>\n")
    return "".join(out)


def write_html(rows, status, info, path, rules=()):
    """info: source_type("E01"/"Live system"), actions_detected, direct, support / rules: analyzer.load_rules 결과"""
    input_format = "E01" if info["source_type"] == "E01" else "live system"
    out = ['<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
           '<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>LLM_Artifact_Report</title>\n',
           "<style>\n" + REPORT_CSS + "</style>\n</head>\n<body>\n",
           '<div class="layout"><aside class="sidebar"><div class="sidebar-brand">%s</div>'
           '<nav aria-label="보고서 메뉴">'
           '<button type="button" class="nav-tab active" data-view-button="analysis" aria-selected="true">종합분석</button>'
           '<button type="button" class="nav-tab" data-view-button="rules" aria-selected="false">판정 규칙</button>'
           '</nav></aside><main><h1>LLM_Artifact_Report</h1><div data-view="analysis">\n' % _e(TARGET),
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
                       '<div class="match-body"><div class="table-wrap"><table>\n<thead><tr>%s</tr></thead>\n<tbody>\n'
                       % (label, len(grows), "".join("<th>%s</th>" % c for c in COLUMNS)))
            out += [_row_html(r) for r in grows]
            out.append("</tbody>\n</table></div></div>\n</details>\n")
        lines = _state_lines(st)
        if lines:
            out.append('<p class="empty">%s</p>\n' % "<br>".join(_e(x) for x in lines))
        out.append("</div>\n</details>\n")

    out.append("</section></div>\n")
    out.append(_rules_table(rules))
    out.append("</main></div>\n<script>\n" + REPORT_JS + "</script>\n</body>\n</html>\n")
    with open(path, "w", encoding="utf-8", newline="\n", errors="backslashreplace") as f:
        f.write("".join(out))
