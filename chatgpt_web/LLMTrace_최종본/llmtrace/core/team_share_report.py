from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable


DIRECT_GRADE = '직접'
SUPPORTING_GRADE = '보조'
KST = timezone(timedelta(hours=9), name='KST')


def _target_name(metadata: Dict[str, Any]) -> str:
    return ' '.join(
        part for part in (
            str(metadata.get('service') or ''),
            {'web': 'Web', 'app': 'Desktop'}.get(
                str(metadata.get('surface') or '').lower(),
                str(metadata.get('surface') or ''),
            ),
        ) if part
    )


def _action_catalog(profile: Any,
                    actions: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    action_map = {str(action.get('id') or ''): action for action in actions}
    configured = getattr(profile.team_share, 'ACTION_TITLES', {}) or {}
    return [
        action_map.get(action_id, {
            'id': action_id,
            'title': str(configured.get(action_id) or action_id),
            'occurrences': 0,
            'matches': [],
            'rows': [],
        })
        for action_id in sorted(set(configured) | set(action_map))
    ]


def _plain_evidence_value(value: str) -> str:
    text = str(value or '').strip()
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text
    if isinstance(decoded, str):
        return decoded
    if decoded is None:
        return 'null'
    if isinstance(decoded, (dict, list)):
        return json.dumps(decoded, ensure_ascii=False, separators=(',', ':'))
    return str(decoded)


def _readable_evidence(evidence: str, limit: int = 3) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for part in re.split(r';\s*|\s+\|\s+', str(evidence or '')):
        field, marker, value = part.partition('=')
        if not marker:
            continue
        field = field.strip()
        if field.endswith('._source_locator'):
            continue
        field = re.sub(
            r'^artifacts\[\d+\]\.(?:records\[\d+\]\.|raw_timestamp\.|source_location\[\d+\]\.)',
            '',
            field,
        )
        if not field:
            continue
        items.append((field, _plain_evidence_value(value)))
        if len(items) >= limit:
            break
    return items


def _evidence_cell(row: Dict[str, Any], rule_id: str) -> str:
    raw = str(row.get('근거') or '-')
    readable = _readable_evidence(raw)
    list_items = [
        '<li><span class="rv-field">'
        f'{html.escape(field)}</span> = <span class="rv-value">'
        f'{html.escape(value)}</span></li>'
        for field, value in readable
    ]
    when = str(row.get('시각') or '-')
    if when != '-':
        list_items.append(f'<li class="rv-time">{html.escape(when)}</li>')
    if not list_items:
        list_items.append('<li><span class="rv-value">원본 근거 참조</span></li>')
    return (
        f'<span class="rule-id">{html.escape(rule_id or "-")}</span>'
        f'<ul class="readable">{"".join(list_items)}</ul>'
        '<details class="raw"><summary>원본 근거 보기</summary>'
        f'<pre>{html.escape(raw)}</pre></details>'
    )


def _artifact_table(match_rows: Iterable[Dict[str, Any]], rule_id: str) -> str:
    rows = []
    for row in match_rows:
        grade = str(row.get('등급') or SUPPORTING_GRADE)
        grade_class = 'direct' if grade == DIRECT_GRADE else 'supporting'
        rows.append(
            '<tr>'
            f'<td>{html.escape(str(row.get("시각") or "-"))}</td>'
            f'<td>{html.escape(str(row.get("아티팩트") or "-"))}</td>'
            f'<td>{html.escape(str(row.get("경로") or "-"))}</td>'
            f'<td><span class="grade {grade_class}">{html.escape(grade)}</span></td>'
            f'<td>{_evidence_cell(row, rule_id)}</td>'
            '</tr>'
        )
    if not rows:
        return '<p class="empty">연결된 아티팩트 경로를 찾지 못했습니다.</p>'
    return (
        '<div class="table-wrap"><table><thead><tr>'
        '<th>시각</th><th>아티팩트</th><th>경로</th><th>등급</th><th>근거</th>'
        '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
    )


def _condition_text(match: Any) -> str:
    if not isinstance(match, dict):
        return '-'
    conditions = []
    for field, requirement in match.items():
        if isinstance(requirement, dict):
            for operator, value in requirement.items():
                rendered = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
                conditions.append(f'{field}.{operator}={rendered}')
        else:
            rendered = json.dumps(requirement, ensure_ascii=False, separators=(',', ':'))
            conditions.append(f'{field}={rendered}')
    return '; '.join(conditions) or '-'


def _rule_nodes(rule: Dict[str, Any]) -> list[Dict[str, Any]]:
    nodes = []
    for key in ('start', 'end', 'match'):
        value = rule.get(key)
        if isinstance(value, dict):
            nodes.append(value)
    for key in ('milestones', 'match_any'):
        value = rule.get(key)
        if isinstance(value, list):
            nodes.extend(item for item in value if isinstance(item, dict))
    return nodes


def _timestamp_field_text(profile: Any,
                          nodes: Iterable[Dict[str, Any]]) -> str:
    configured = (
        getattr(profile.team_share, 'TIMESTAMP_SOURCE_FIELDS', {}) or {}
    )
    fields: list[str] = []
    for node in nodes:
        artifact = str(node.get('artifact') or '')
        fact_types = []
        if node.get('fact_type'):
            fact_types.append(str(node['fact_type']))
        if isinstance(node.get('fact_type_in'), (list, tuple)):
            fact_types.extend(str(value) for value in node['fact_type_in'])
        if not fact_types:
            fact_types.append('*')
        for fact_type in fact_types:
            sources = configured.get(
                (artifact, fact_type),
                configured.get((artifact, '*'), ()),
            )
            for source in sources or ():
                source = str(source)
                if source and source not in fields:
                    fields.append(source)
    return ' / '.join(fields) or '-'


def _rule_action(profile: Any, rule: Dict[str, Any]) -> tuple[str, str]:
    config = profile.team_share
    detail = {
        'base_rule_id': str(rule.get('rule_id') or ''),
        **(rule.get('output_extra') or {}),
    }
    inference = {'behavior_id': rule.get('behavior_id'), 'detail': detail}
    mapper = getattr(config, 'action_id_for_rule', None)
    action_id = str(mapper(inference) if callable(mapper) else '')
    if not action_id and str(rule.get('behavior_id') or '').startswith('B'):
        action_id = 'A' + str(rule['behavior_id'])[1:]
    titles = getattr(config, 'ACTION_TITLES', {}) or {}
    return action_id, str(titles.get(action_id) or rule.get('behavior_name') or action_id)


def _graded_rule_ids(rules_module: Any,
                     rule: Dict[str, Any]) -> list[tuple[str, str]]:
    base_id = str(rule.get('rule_id') or '')
    if not base_id or base_id.startswith('S-'):
        return [(base_id or '-', '보조')]
    levels = (getattr(rules_module, 'RULE_LEVELS', {}) or {}).get(base_id, {})
    output = []
    labels = {'minimum': '최소', 'standard': '중간', 'maximum': '최대'}
    for level in ('minimum', 'standard', 'maximum'):
        specification = levels.get(level)
        if not isinstance(specification, dict):
            continue
        rule_id = str(specification.get('rule_id') or '')
        if rule_id and specification.get('status') != 'unavailable':
            output.append((rule_id, labels[level]))
    return output or [(base_id, '직접')]


def _rule_appendix(profile: Any) -> str:
    rules_module = profile.rules
    rules = list(getattr(rules_module, 'INFERENCE_RULES', ()) or ())
    rules.extend(getattr(rules_module, 'SUPPORTING_INFERENCE_RULES', ()) or ())
    rows = []
    artifact_names = getattr(profile.team_share, 'ARTIFACT_NAMES', {}) or {}
    for rule in rules:
        nodes = _rule_nodes(rule)
        artifacts = []
        filters = []
        for node in nodes:
            artifact = str(node.get('artifact') or '')
            if artifact and artifact not in artifacts:
                artifacts.append(artifact)
            ref = str(node.get('ref') or node.get('fact_type') or artifact or 'evidence')
            filters.append(f'{ref}: {_condition_text(node.get("match"))}')
        action_id, action_title = _rule_action(profile, rule)
        note = str(
            rule.get('candidate_reason')
            or next(iter(rule.get('notes') or ()), '')
            or '-'
        )
        timestamp_fields = _timestamp_field_text(profile, nodes)
        for rule_id, level in _graded_rule_ids(rules_module, rule):
            rows.append(
                '<tr>'
                f'<td class="mono">{html.escape(rule_id)}</td>'
                f'<td>{html.escape((action_id + " " + action_title).strip())}</td>'
                f'<td>{html.escape(", ".join(artifact_names.get(x, x) for x in artifacts) or "-")}</td>'
                f'<td class="mono">{html.escape(" | ".join(filters) or "-")}</td>'
                f'<td class="mono">{html.escape(timestamp_fields)}</td>'
                f'<td>{html.escape(level + " 근거 · " + note)}</td>'
                '</tr>'
            )
    if not rows:
        rows.append('<tr><td colspan="6">표시할 판정 규칙이 없습니다.</td></tr>')
    return (
        '<section class="results appendix"><h2>부록: 판정 규칙</h2>'
        '<div class="table-wrap"><table><thead><tr>'
        '<th>규칙 ID</th><th>행위</th><th>아티팩트</th><th>필터</th>'
        '<th>시각 필드</th><th>비고</th></tr></thead><tbody>'
        + ''.join(rows) + '</tbody></table></div></section>'
    )


_STYLE = r'''
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
.match-body th:nth-child(1) { width:250px; } .match-body th:nth-child(2) { width:250px; }
.match-body th:nth-child(3) { width:300px; } .match-body th:nth-child(4) { width:100px; }
.match-body th:nth-child(5) { width:520px; }
.appendix table { min-width:1600px; table-layout:fixed; }
.appendix th:nth-child(1), .appendix td:nth-child(1) { width:170px; }
.appendix th:nth-child(2), .appendix td:nth-child(2) { width:160px; }
.appendix th:nth-child(3), .appendix td:nth-child(3) { width:260px; }
.appendix th:nth-child(4), .appendix td:nth-child(4) { width:500px; word-break:break-word; }
.appendix th:nth-child(5), .appendix td:nth-child(5) { width:190px; }
.appendix th:nth-child(6), .appendix td:nth-child(6) { width:360px; word-break:break-word; }
th,td { border-bottom:1px solid var(--line); padding:10px; text-align:left; vertical-align:top; }
th { background:#f7f9fc; color:#31415c; } td { overflow-wrap:anywhere; }
.match-body td:nth-child(3) { font:12px/1.55 Consolas,"Malgun Gothic",monospace; word-break:break-all; }
.rule-id { display:inline-block; font:700 12px/1.4 Consolas,monospace; background:#f1f4f9; border:1px solid var(--line); border-radius:5px; padding:1px 6px; margin:0 4px 4px 0; }
ul.readable { margin:2px 0 6px; padding-left:18px; } ul.readable li { margin:2px 0; }
.rv-field { color:var(--muted); font-family:Consolas,monospace; font-size:12px; }
.rv-value { font-family:Consolas,monospace; font-size:12px; word-break:break-all; }
.rv-time { color:#244b91; white-space:nowrap; }
details.raw > summary { display:list-item; list-style:disclosure-closed inside; cursor:pointer; color:var(--muted); font-size:12px; padding:2px 0; font-weight:400; }
details.raw[open] > summary { list-style-type:disclosure-open; border:0; }
details.raw > summary::after { content:none; }
details.raw pre { white-space:pre-wrap; word-break:break-all; font:12px/1.55 Consolas,"Malgun Gothic",monospace; background:#f7f9fc; border:1px solid var(--line); border-radius:6px; padding:8px; margin:4px 0 0; }
.appendix td { font-size:13px; } .appendix td.mono { font:12px Consolas,monospace; }
.empty { border:1px dashed #b8c3d3; border-radius:9px; padding:15px; color:var(--muted); }
.layout { display:grid; grid-template-columns:190px minmax(0,1fr); min-height:100vh; }
.layout main, [data-view], .results, details.action, details.match { min-width:0; }
.sidebar { position:sticky; top:0; height:100vh; background:#10294d; color:#fff; padding:22px 12px; }
.sidebar-brand { font-size:20px; font-weight:800; padding:2px 9px 20px; }
.sidebar nav { display:grid; gap:7px; }
.sidebar .nav-tab { width:100%; border:0; border-radius:9px; padding:11px; text-align:left; background:transparent; color:#cad6e8; font:inherit; font-weight:700; cursor:pointer; }
.sidebar .nav-tab:hover { background:#ffffff12; color:#fff; }
.sidebar .nav-tab.active { background:#fff; color:#10294d; }
[hidden] { display:none !important; }
@media(max-width:700px) { main { padding:14px; } .results,.overview { padding:14px; } .layout { display:block; } .sidebar { position:static; height:auto; padding:12px; } .sidebar-brand { padding:2px 9px 10px; } .sidebar nav { grid-template-columns:repeat(2,minmax(0,1fr)); } }
'''


_SCRIPT = r'''
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
'''


def render_reference_html(profile: Any, actions: list[Dict[str, Any]],
                          environment: Dict[str, Any]) -> str:
    metadata = profile.metadata
    target = _target_name(metadata)
    catalog = _action_catalog(profile, actions)
    blocks = []
    for action in catalog:
        match_blocks = []
        for match in action.get('matches') or []:
            when = str(match.get('display_time') or '')
            date_text, _, clock_text = when.partition(' ')
            time_label = (
                '<span class="match-time">'
                f'<span class="match-date">{html.escape(date_text)}</span>'
                f'<span class="match-clock">{html.escape(clock_text)}</span></span>'
                if when and when != '-'
                else '<span class="match-time">시각 미확인</span>'
            )
            rows = match.get('rows') or []
            match_blocks.append(
                '<details class="match"><summary>'
                f'{time_label}<span class="match-meta">매칭 {len(rows)}건</span>'
                f'</summary><div class="match-body">{_artifact_table(rows, str(match.get("rule_id") or "-"))}'
                '</div></details>'
            )
        body = ''.join(match_blocks) or '<p class="empty">매칭된 근거가 없습니다.</p>'
        blocks.append(
            '<details class="action"><summary>'
            f'<span class="action-id">{html.escape(str(action.get("id") or ""))}</span> '
            f'<span class="action-name">{html.escape(str(action.get("title") or ""))}</span>'
            f'<span class="summary-meta">매칭 {int(action.get("occurrences") or 0)}건</span>'
            f'</summary><div class="action-body">{body}</div></details>'
        )
    if not blocks:
        blocks.append('<p class="empty">규칙에 매칭된 사용자 행위가 없습니다.</p>')

    evidence_rows = [
        row for action in actions for match in action.get('matches') or ()
        for row in match.get('rows') or ()
    ]
    direct_count = sum(1 for row in evidence_rows if row.get('등급') == DIRECT_GRADE)
    support_count = sum(1 for row in evidence_rows if row.get('등급') == SUPPORTING_GRADE)
    matched_actions = sum(1 for action in actions if action.get('occurrences'))
    source_type = str(environment.get('source_type') or 'Unknown')
    return ''.join([
        '<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">\n',
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n',
        '<title>LLM_Artifact_Report</title><style>\n', _STYLE, '</style></head><body>\n',
        '<div class="layout"><aside class="sidebar"><div class="sidebar-brand">',
        html.escape(target),
        '</div><nav aria-label="보고서 메뉴">',
        '<button type="button" class="nav-tab active" data-view-button="analysis" aria-selected="true">종합분석</button>',
        '<button type="button" class="nav-tab" data-view-button="rules" aria-selected="false">판정 규칙</button>',
        '</nav></aside><main><h1>LLM_Artifact_Report</h1><div data-view="analysis">',
        '<section class="overview" aria-label="분석 개요"><h2>분석 개요</h2><div class="stats">',
        f'<div><span>분석 대상</span><strong>{html.escape(target)}</strong></div>',
        f'<div><span>입력 형식</span><strong>{html.escape(source_type)}</strong></div>',
        f'<div><span>매칭 행위</span><strong>{matched_actions}개</strong></div>',
        f'<div><span>근거 개수</span><strong>직접 {direct_count} / 보조 {support_count}</strong></div>',
        '</div></section><section class="results"><h2>행위별 매칭 결과</h2>',
        '<div class="toolbar"><input id="search" type="search" aria-label="행위명, 아티팩트 또는 경로 검색" placeholder="행위명, 아티팩트 또는 경로 검색">',
        '<button id="expand" type="button">모두 펼치기</button><button id="collapse" type="button">모두 접기</button></div>',
        ''.join(blocks), '</section></div><div data-view="rules" hidden>',
        _rule_appendix(profile), '</div></main></div><script>\n', _SCRIPT,
        '</script>\n</body></html>',
    ])
