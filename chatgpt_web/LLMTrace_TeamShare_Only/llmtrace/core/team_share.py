from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

from llmtrace import __version__

from .io import write_csv
from .raw_io import json_default


TEAM_SHARE_COLUMNS = ['행위 분류', '아티팩트', '경로', '아티팩트 등급']
DIRECT_GRADE = '직접'
ATTRIBUTION_GRADE = '귀속'
DEFAULT_CSV_FILENAME = 'LLMTrace_행위별_아티팩트_매트릭스.csv'
DEFAULT_HTML_FILENAME = 'LLMTrace_팀공유_행위별_아티팩트.html'
RAW_DATA_DIRNAME = 'raw_parsing_data'
RAW_INDEX_FILENAME = 'index.js'
RAW_SHARD_SIZE = 100
CSV_MULTI_VALUE_SEPARATOR = ' | '


def _parse_iso_timestamp(value: str) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _representative_time(when_from: str, when_to: str) -> tuple[str, float | None]:
    """Return a second-precision midpoint for display and chronological sorting."""
    start = _parse_iso_timestamp(when_from)
    end = _parse_iso_timestamp(when_to)
    if start is not None and end is not None:
        low, high = sorted((start, end))
        point = low + (high - low) / 2
    else:
        point = start or end
    if point is None:
        return '', None
    display = point.strftime('%Y-%m-%d %H:%M:%S')
    return display, point.timestamp()


def _observation_timestamp(row: Dict[str, Any]) -> str:
    timestamps = row.get('timestamps') or {}
    if isinstance(timestamps, dict):
        preferred = ('ts_event', 'ts_record', 'ts_start', 'ts_end')
        for key in preferred:
            if timestamps.get(key):
                return str(timestamps[key])
        for value in timestamps.values():
            if value:
                return str(value)
    for key, value in row.items():
        if str(key).startswith('ts_') and value:
            return str(value)
    return ''


def _write_raw_parsing_data(profile: Any, out: Path,
                            observations: Iterable[Any]) -> Dict[str, Any]:
    """Write lazy-loadable raw/normalized Observation data for the HTML viewer."""
    data_dir = out / RAW_DATA_DIRNAME
    data_dir.mkdir(parents=True, exist_ok=True)
    for stale in data_dir.glob('shard_*.js'):
        if stale.is_file():
            stale.unlink()

    config = profile.team_share
    index_rows = []
    shard_rows = []
    shard_number = 0

    def flush_shard() -> None:
        nonlocal shard_rows, shard_number
        if not shard_rows:
            return
        payload = json.dumps(
            shard_rows,
            ensure_ascii=False,
            separators=(',', ':'),
            default=json_default,
        )
        shard_path = data_dir / f'shard_{shard_number:04d}.js'
        shard_path.write_text(
            'window.__llmtraceRawShards=window.__llmtraceRawShards||{};'
            f'window.__llmtraceRawShards[{shard_number}]={payload};',
            encoding='utf-8',
        )
        shard_rows = []
        shard_number += 1

    for observation_value in observations:
        row = _row_dict(observation_value)
        offset = len(shard_rows)
        artifact = str(row.get('source_artifact') or '')
        index_rows.append({
            'i': str(row.get('id') or ''),
            'a': artifact,
            'n': _artifact_name(artifact, config),
            't': _observation_timestamp(row),
            'p': str(row.get('parser') or artifact),
            's': str(row.get('source_path') or ''),
            'l': str(row.get('source_locator') or ''),
            'h': shard_number,
            'o': offset,
        })
        shard_rows.append(row.get('parsed_record'))
        if len(shard_rows) >= RAW_SHARD_SIZE:
            flush_shard()
    flush_shard()

    artifacts = sorted({
        (row['a'], row['n']) for row in index_rows
    }, key=lambda item: (item[1], item[0]))
    index_payload = {
        'count': len(index_rows),
        'shardSize': RAW_SHARD_SIZE,
        'shards': shard_number,
        'artifacts': [
            {'value': value, 'label': label} for value, label in artifacts
        ],
        'rows': index_rows,
    }
    (data_dir / RAW_INDEX_FILENAME).write_text(
        'window.__llmtraceRawIndex=' + json.dumps(
            index_payload,
            ensure_ascii=False,
            separators=(',', ':'),
            default=json_default,
        ) + ';',
        encoding='utf-8',
    )
    return {
        'records': len(index_rows),
        'shards': shard_number,
        'directory': str(data_dir),
        'index': str(data_dir / RAW_INDEX_FILENAME),
    }


def _row_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, 'as_dict'):
        return value.as_dict()
    return {}


def _condition_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ('all_refs', 'any_refs') and isinstance(child, (list, tuple)):
                refs.update(str(item) for item in child)
            else:
                refs.update(_condition_refs(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            refs.update(_condition_refs(child))
    return refs


def _minimum_refs(profile: Any, base_rule_id: str) -> set[str]:
    levels = getattr(profile.rules, 'RULE_LEVELS', {}).get(base_rule_id, {})
    minimum = levels.get('minimum') or {}
    return _condition_refs(minimum.get('when') or {})


def _action_id(inference: Dict[str, Any], config: Any) -> str:
    behavior_id = str(inference.get('behavior_id') or '')
    if config is not None and hasattr(config, 'action_id_for_rule'):
        mapped = config.action_id_for_rule({'behavior_id': behavior_id})
        if mapped:
            return str(mapped)
    if behavior_id.startswith('B'):
        return f'A{behavior_id[1:]}'
    return behavior_id or 'ACTION'


def _action_title(action_id: str, inference: Dict[str, Any], config: Any) -> str:
    configured = getattr(config, 'ACTION_TITLES', {}) if config is not None else {}
    return str(configured.get(action_id) or inference.get('behavior_name') or action_id)


def _artifact_name(artifact: str, config: Any) -> str:
    configured = getattr(config, 'ARTIFACT_NAMES', {}) if config is not None else {}
    return str(configured.get(artifact) or artifact or 'unknown')


def _grade_by_observation(inference: Dict[str, Any], profile: Any) -> Dict[str, str]:
    detail = inference.get('detail') or {}
    refs = detail.get('refs') or {}
    minimum_refs = _minimum_refs(profile, str(detail.get('base_rule_id') or ''))
    default_direct = not minimum_refs
    grades: Dict[str, str] = {}
    for ref_name, ref_value in refs.items():
        if not isinstance(ref_value, dict) or not ref_value.get('observation_id'):
            continue
        observation_id = str(ref_value['observation_id'])
        grade = DIRECT_GRADE if default_direct or ref_name in minimum_refs else ATTRIBUTION_GRADE
        if grades.get(observation_id) != DIRECT_GRADE:
            grades[observation_id] = grade
    for observation_id in inference.get('supported_by') or []:
        grades.setdefault(str(observation_id), ATTRIBUTION_GRADE)
    return grades


def _build_match_actions(profile: Any, observations: Iterable[Any],
                         inferences: Iterable[Any]) -> list[Dict[str, Any]]:
    config = profile.team_share
    observation_map = {
        str(row.get('id')): row
        for row in (_row_dict(item) for item in observations)
        if row.get('id')
    }
    actions: Dict[str, Dict[str, Any]] = {}

    for inference_value in inferences:
        inference = _row_dict(inference_value)
        action_id = _action_id(inference, config)
        action = actions.setdefault(action_id, {
            'id': action_id,
            'title': _action_title(action_id, inference, config),
            'occurrences': 0,
            'matches': [],
            '_rows': {},
        })
        action['occurrences'] += 1
        action_label = f'{action_id} · {action["title"]}'
        match_rows: Dict[tuple[str, str], Dict[str, str]] = {}
        for observation_id, grade in _grade_by_observation(inference, profile).items():
            observation = observation_map.get(observation_id)
            if observation is None:
                continue
            artifact = _artifact_name(str(observation.get('source_artifact') or ''), config)
            source_path = str(observation.get('source_path') or '').strip()
            source_locator = str(observation.get('source_locator') or '').strip()
            path = source_path or source_locator or '(경로 없음)'
            key = (artifact, path)
            row = {
                '행위 분류': action_label,
                '아티팩트': artifact,
                '경로': path,
                '아티팩트 등급': grade,
            }
            match_existing = match_rows.get(key)
            if match_existing is None or (
                match_existing['아티팩트 등급'] == ATTRIBUTION_GRADE
                and grade == DIRECT_GRADE
            ):
                match_rows[key] = row
            existing = action['_rows'].get(key)
            if existing is None or (
                existing['아티팩트 등급'] == ATTRIBUTION_GRADE
                and grade == DIRECT_GRADE
            ):
                action['_rows'][key] = row

        action['matches'].append({
            'id': str(inference.get('id') or ''),
            'when_from': str(inference.get('when_from') or ''),
            'when_to': str(inference.get('when_to') or ''),
            '_input_order': action['occurrences'],
            'rows': sorted(
                match_rows.values(),
                key=lambda row: (
                    0 if row['아티팩트 등급'] == DIRECT_GRADE else 1,
                    row['아티팩트'], row['경로'],
                ),
            ),
        })

    output = []
    for action_id in sorted(actions):
        action = actions[action_id]
        for match in action['matches']:
            display_time, sort_time = _representative_time(
                match['when_from'], match['when_to'],
            )
            match['display_time'] = display_time
            match['_sort_time'] = sort_time
        action['matches'].sort(key=lambda match: (
            match['_sort_time'] is None,
            match['_sort_time'] if match['_sort_time'] is not None else 0,
            match['_input_order'],
        ))
        action['rows'] = sorted(
            action.pop('_rows').values(),
            key=lambda row: (
                0 if row['아티팩트 등급'] == DIRECT_GRADE else 1,
                row['아티팩트'], row['경로'],
            ),
        )
        output.append(action)
    return output


def _build_match_csv_rows(actions: Iterable[Dict[str, Any]]) -> list[Dict[str, str]]:
    """Create one flat, single-line CSV row per inference match.

    The action column is intentionally limited to the action classification.
    Match numbers and timestamps belong to the HTML timeline and made the CSV
    difficult to filter.  When one match uses several artifacts, the related
    artifact/path/grade cells use the same ordered separator instead of embedded
    newlines so even simple CSV viewers keep one physical line per match.
    """
    output = []
    for action in actions:
        action_label = f'{action["id"]} · {action["title"]}'
        for match in action['matches']:
            match_rows = match.get('rows') or []
            output.append({
                '행위 분류': action_label,
                '아티팩트': CSV_MULTI_VALUE_SEPARATOR.join(
                    str(row.get('아티팩트') or '') for row in match_rows
                ),
                '경로': CSV_MULTI_VALUE_SEPARATOR.join(
                    str(row.get('경로') or '') for row in match_rows
                ),
                '아티팩트 등급': CSV_MULTI_VALUE_SEPARATOR.join(
                    str(row.get('아티팩트 등급') or '')
                    for row in match_rows
                ),
            })
    return output


def _render_html(profile: Any, actions: list[Dict[str, Any]],
                  environment: Dict[str, Any], raw_record_count: int) -> str:
    metadata = profile.metadata
    generated = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    blocks = []
    for action in actions:
        match_blocks = []
        for match_index, match in enumerate(action['matches'], start=1):
            rows = []
            for row in match['rows']:
                grade = row['아티팩트 등급']
                grade_class = 'direct' if grade == DIRECT_GRADE else 'attribution'
                rows.append(
                    '<tr>'
                    f'<td>{html.escape(row["아티팩트"])}</td>'
                    f'<td class="path">{html.escape(row["경로"])}</td>'
                    f'<td><span class="grade {grade_class}">'
                    f'{html.escape(grade)}</span></td></tr>'
                )
            table = (
                '<div class="table-wrap"><table><thead><tr>'
                '<th>아티팩트</th><th>경로</th><th>아티팩트 등급</th>'
                '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
                if rows else '<p class="empty">연결된 아티팩트 경로를 찾지 못했습니다.</p>'
            )
            when_from = match.get('when_from') or ''
            when_to = match.get('when_to') or ''
            when = match.get('display_time') or ''
            date_text, _, clock_text = when.partition(' ')
            original_range = ' ~ '.join(
                value for value in (when_from, when_to)
                if value and value != when_from
            )
            if when_from and when_to and when_from != when_to:
                original_range = f'{when_from} ~ {when_to}'
            else:
                original_range = when_from or when_to
            time_label = (
                f'<span class="match-time" title="'
                f'{html.escape("원본 근거 범위: " + original_range, quote=True)}">'
                f'<span class="match-date">{html.escape(date_text)}</span>'
                f'<span class="match-clock">{html.escape(clock_text)}</span></span>'
                if when else ''
            )
            match_search = html.escape(
                ' '.join(
                    [action['id'], action['title'], str(match_index), when]
                    + [
                        f"{row['아티팩트']} {row['경로']} {row['아티팩트 등급']}"
                        for row in match['rows']
                    ]
                )
            ).lower()
            match_blocks.append(
                f'<details class="match" data-search="{match_search}"><summary>'
                f'<strong>매칭 {match_index}</strong>{time_label}'
                f'<span class="match-meta">아티팩트 {len(match["rows"])}건</span>'
                f'</summary><div class="match-body">{table}</div></details>'
            )
        search = html.escape(
            f"{action['id']} {action['title']} "
            + ' '.join(
                f"{row['아티팩트']} {row['경로']} {row['아티팩트 등급']}"
                for match in action['matches'] for row in match['rows']
            )
        ).lower()
        blocks.append(
            f'<details class="action" data-search="{search}"><summary>'
            f'<span class="action-id">{html.escape(action["id"])}</span>'
            f'<strong>{html.escape(action["title"])}</strong>'
            f'<span class="summary-meta">매칭 {action["occurrences"]}건 · '
            f'아티팩트 {len(action["rows"])}건</span></summary>'
            f'<div class="action-body"><div class="match-list">'
            f'{"".join(match_blocks)}</div></div></details>'
        )

    if not blocks:
        blocks.append('<p class="empty">규칙에 매칭된 사용자 행위가 없습니다.</p>')

    target = ' '.join(
        part for part in (
            str(metadata.get('service') or ''),
            {'web': 'Web', 'app': 'Desktop'}.get(
                str(metadata.get('surface') or '').lower(),
                str(metadata.get('surface') or ''),
            ),
        ) if part
    )
    source_type = str(environment.get('source_type') or 'Unknown')
    source = str(environment.get('source') or environment.get('input') or '')
    machine = str(environment.get('machine') or '')
    os_name = str(environment.get('os') or '')

    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLMTrace 매칭 결과</title><style>
:root{{--ink:#172033;--muted:#64748b;--line:#dce3ed;--panel:#fff;--navy:#17345f;--bg:#f4f7fb}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 Arial,"Malgun Gothic",sans-serif}}
.app-shell{{display:grid;grid-template-columns:190px minmax(0,1fr);min-height:100vh}}.sidebar{{position:sticky;top:0;height:100vh;background:#10294d;color:#fff;padding:22px 12px}}.sidebar-brand{{font-size:20px;font-weight:800;padding:2px 9px 20px}}.sidebar nav{{display:grid;gap:7px}}.nav-tab{{width:100%;border:0;border-radius:9px;padding:11px;text-align:left;background:transparent;color:#cad6e8;font-weight:700;cursor:pointer}}.nav-tab:hover{{background:#ffffff12;color:#fff}}.nav-tab.active{{background:#fff;color:#10294d}}.workspace{{min-width:0}}
main{{width:100%;max-width:1900px;margin:0 auto;padding:26px 32px 40px}}header{{margin-bottom:16px}}h1{{font-size:28px;margin:0 0 6px}}h2{{font-size:19px;margin:0 0 14px}}.view{{display:none}}.view.active{{display:block}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;margin:14px 0;box-shadow:0 4px 18px #1720330a}}
.overview{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}}.overview div{{border:1px solid var(--line);border-radius:10px;padding:11px}}.overview span{{display:block;color:var(--muted);font-size:12px}}.overview strong{{font-size:15px;word-break:break-word}}
.legend{{display:flex;gap:8px;flex-wrap:wrap}}.grade{{display:inline-block;border-radius:999px;padding:3px 9px;font-size:12px;font-weight:700;white-space:nowrap}}.direct{{background:#dff4e9;color:#155e3d}}.attribution{{background:#e8f0ff;color:#244b91}}
.toolbar{{display:flex;gap:9px;flex-wrap:wrap;margin:14px 0}}.toolbar input{{flex:1;min-width:250px;padding:10px 12px;border:1px solid #b8c3d3;border-radius:8px}}.toolbar button{{padding:9px 12px;border:1px solid #9aacbf;background:#fff;border-radius:8px;cursor:pointer}}
details.action{{background:#fff;border:1px solid var(--line);border-radius:12px;margin:9px 0;overflow:hidden}}details.action>summary{{display:flex;align-items:center;gap:10px;padding:14px 16px;cursor:pointer;list-style:none}}details.action>summary::-webkit-details-marker{{display:none}}details.action>summary:after{{content:"＋";margin-left:auto;color:var(--muted);font-size:18px}}details.action[open]>summary:after{{content:"－"}}.action-id{{background:var(--navy);color:#fff;border-radius:6px;padding:3px 7px;font-weight:700}}.summary-meta{{color:var(--muted);margin-left:6px}}.action-body{{border-top:1px solid var(--line);padding:16px}}
.match-list{{display:grid;gap:9px}}details.match{{border:1px solid var(--line);border-radius:9px;overflow:hidden;background:#fbfcfe}}details.match>summary{{display:flex;align-items:center;gap:12px;padding:11px 13px;cursor:pointer;list-style:none}}details.match>summary::-webkit-details-marker{{display:none}}details.match>summary:after{{content:"＋";margin-left:auto;color:var(--muted)}}details.match[open]>summary:after{{content:"－"}}.match-time{{display:inline-flex;align-items:center;gap:8px;color:var(--ink);font-size:13px;font-weight:700}}.match-date,.match-clock{{display:inline-block;padding:2px 7px;border:1px solid var(--line);border-radius:6px;background:#fff}}.match-meta{{color:var(--muted);font-size:12px}}.timezone-note{{margin-left:auto;color:var(--muted);font-size:12px;font-weight:700}}.match-body{{border-top:1px solid var(--line);padding:0 12px;background:#fff}}
.raw-controls{{display:grid;grid-template-columns:minmax(240px,1fr) 220px;gap:10px}}.raw-controls input,.raw-controls select{{width:100%;padding:10px 12px;border:1px solid #b8c3d3;border-radius:8px;background:#fff}}.raw-status{{display:flex;justify-content:space-between;gap:10px;align-items:center;margin:14px 1px;color:var(--muted)}}.raw-list{{display:grid;gap:9px}}details.raw-record{{background:#fbfcfe;border:1px solid var(--line);border-radius:10px;overflow:hidden}}details.raw-record>summary{{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:12px 14px;cursor:pointer;list-style:none}}details.raw-record>summary::-webkit-details-marker{{display:none}}details.raw-record>summary:after{{content:"＋";margin-left:auto;color:var(--muted)}}details.raw-record[open]>summary:after{{content:"－"}}.raw-id{{font-weight:800;color:#10294d}}.raw-tag{{padding:2px 7px;border:1px solid var(--line);border-radius:6px;background:#fff;font-size:12px;font-weight:700}}.raw-ts{{color:var(--muted);font-size:12px}}.raw-body{{border-top:1px solid var(--line);padding:14px;background:#fff}}.raw-meta-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;margin-bottom:12px}}.raw-meta-item{{border:1px solid var(--line);border-radius:8px;padding:8px;min-width:0}}.raw-meta-item span{{display:block;color:var(--muted);font-size:11px}}.raw-meta-item strong{{display:block;word-break:break-all;font-size:12px}}.raw-json-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}}.raw-json-grid.single{{grid-template-columns:1fr}}.raw-json-panel{{min-width:0}}.raw-json-panel h3{{font-size:14px;margin:0 0 7px}}.raw-json-panel pre{{margin:0;max-height:520px;overflow:auto;padding:12px;border:1px solid var(--line);border-radius:8px;background:#0f172a;color:#e5edf8;font:12px/1.5 Consolas,"Malgun Gothic",monospace;white-space:pre-wrap;word-break:break-word}}.pagination{{display:flex;justify-content:center;align-items:center;gap:9px;margin-top:15px}}.pagination button{{padding:8px 12px;border:1px solid #9aacbf;background:#fff;border-radius:8px;cursor:pointer}}.pagination button:disabled{{opacity:.45;cursor:default}}.raw-loading{{padding:24px;text-align:center;color:var(--muted)}}
.table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid var(--line);padding:10px;text-align:left;vertical-align:top}}th{{background:#f7f9fc;color:#31415c}}.path{{font-family:Consolas,"Malgun Gothic",monospace;font-size:12px;min-width:420px;word-break:break-all}}.hidden{{display:none}}.empty{{border:1px dashed #b8c3d3;border-radius:9px;padding:15px;color:var(--muted)}}footer{{color:var(--muted);font-size:12px;margin:20px 2px}}
@media(max-width:900px){{.app-shell{{grid-template-columns:1fr}}.sidebar{{position:static;height:auto;padding:12px}}.sidebar-brand{{display:none}}.sidebar nav{{grid-template-columns:repeat(2,1fr)}}.nav-tab{{text-align:center}}main{{padding:18px}}.raw-json-grid{{grid-template-columns:1fr}}}}@media(max-width:700px){{main{{padding:14px}}.summary-meta{{display:none}}.path{{min-width:280px}}.raw-controls{{grid-template-columns:1fr}}}}
</style></head><body><div class="app-shell"><aside class="sidebar"><div class="sidebar-brand">LLMTrace</div><nav>
<button class="nav-tab active" type="button" data-view="summary-view">종합분석</button>
<button class="nav-tab" type="button" data-view="raw-view">원본 파싱 모음</button>
</nav></aside><div class="workspace"><main><section id="summary-view" class="view active"><header><h1>LLMTrace 매칭 결과</h1></header>
<section class="panel"><h2>분석 개요</h2><div class="overview">
<div><span>분석 대상</span><strong>{html.escape(target)}</strong></div>
<div><span>입력 형식</span><strong>{html.escape(source_type)}</strong></div>
<div><span>입력 원본</span><strong>{html.escape(source)}</strong></div>
<div><span>분석 시스템</span><strong>{html.escape(machine)} · {html.escape(os_name)}</strong></div>
<div><span>프로필</span><strong>{html.escape(str(metadata.get('profile_id') or ''))} v{html.escape(str(metadata.get('version') or ''))}</strong></div>
<div><span>도구</span><strong>LLM Artifact Analyzer v{html.escape(__version__)}</strong></div>
<div><span>매칭 행위</span><strong>{len(actions)}개</strong></div>
</div>
<div class="legend"><span class="timezone-note">표시 시간대: UTC</span></div></section>
<section class="panel"><h2>행위별 매칭 결과</h2><div class="toolbar">
<input id="search" type="search" placeholder="행위명, 아티팩트 또는 경로 검색">
<button id="expand" type="button">모두 펼치기</button><button id="collapse" type="button">모두 접기</button></div>
{''.join(blocks)}</section></section>
<section id="raw-view" class="view"><header><h1>원본 파싱 모음</h1></header><section class="panel">
<h2>1차 파싱 원본</h2><div class="overview"><div><span>전체 파싱 레코드</span><strong>{raw_record_count:,}건</strong></div><div><span>표시 내용</span><strong>parsed_record</strong></div></div>
<div class="raw-controls"><input id="raw-search" type="search" placeholder="Observation ID, 아티팩트, 파서, 경로 검색"><select id="raw-artifact"><option value="">전체 아티팩트</option></select></div>
<div class="raw-status"><span id="raw-count">탭을 열면 데이터를 불러옵니다.</span><span>한 페이지 25건</span></div><div id="raw-list" class="raw-list"></div>
<div class="pagination"><button id="raw-prev" type="button" disabled>이전</button><strong id="raw-page">-</strong><button id="raw-next" type="button" disabled>다음</button></div>
</section></section><footer>생성 시각: {html.escape(generated)}</footer>
</main></div></div><script>const cards=[...document.querySelectorAll('details.action')];const matches=[...document.querySelectorAll('details.match')];
document.getElementById('expand').addEventListener('click',()=>[...cards,...matches].filter(x=>!x.classList.contains('hidden')).forEach(x=>x.open=true));
document.getElementById('collapse').addEventListener('click',()=>[...cards,...matches].forEach(x=>x.open=false));
document.getElementById('search').addEventListener('input',e=>{{const q=e.target.value.trim().toLowerCase();cards.forEach(card=>{{const visible=!q||card.dataset.search.includes(q);card.classList.toggle('hidden',!visible);if(visible&&q)card.open=true;card.querySelectorAll('details.match').forEach(match=>{{const matchVisible=!q||match.dataset.search.includes(q);match.classList.toggle('hidden',!matchVisible);if(matchVisible&&q)match.open=true;}});}});}});

const navTabs=[...document.querySelectorAll('.nav-tab')];
navTabs.forEach(tab=>tab.addEventListener('click',()=>{{navTabs.forEach(x=>x.classList.toggle('active',x===tab));document.querySelectorAll('.view').forEach(view=>view.classList.toggle('active',view.id===tab.dataset.view));if(tab.dataset.view==='raw-view')loadRawIndex();}}));
const RAW_BASE='{RAW_DATA_DIRNAME}/';const RAW_PAGE_SIZE=25;let rawIndex=null;let rawFiltered=[];let rawPage=1;let rawRenderToken=0;window.__llmtraceRawShards=window.__llmtraceRawShards||{{}};
function loadScript(src){{return new Promise((resolve,reject)=>{{const script=document.createElement('script');script.src=src;script.onload=()=>resolve();script.onerror=()=>reject(new Error(src));document.head.appendChild(script);}});}}
async function loadRawIndex(){{if(rawIndex)return;if(window.__llmtraceRawIndex){{rawIndex=window.__llmtraceRawIndex;setupRawIndex();return;}}const status=document.getElementById('raw-count');status.textContent='원본 파싱 색인을 불러오는 중...';try{{await loadScript(RAW_BASE+'{RAW_INDEX_FILENAME}');rawIndex=window.__llmtraceRawIndex;setupRawIndex();}}catch(error){{status.textContent='원본 파싱 데이터를 불러오지 못했습니다.';}}}}
function setupRawIndex(){{const select=document.getElementById('raw-artifact');rawIndex.artifacts.forEach(item=>{{const option=document.createElement('option');option.value=item.value;option.textContent=item.label;select.appendChild(option);}});applyRawFilter();}}
async function loadRawShard(number){{if(window.__llmtraceRawShards[number])return window.__llmtraceRawShards[number];await loadScript(RAW_BASE+'shard_'+String(number).padStart(4,'0')+'.js');return window.__llmtraceRawShards[number];}}
function rawSearchText(row){{return [row.i,row.a,row.n,row.t,row.p,row.s,row.l].join(' ').toLowerCase();}}
function applyRawFilter(){{if(!rawIndex)return;const q=document.getElementById('raw-search').value.trim().toLowerCase();const artifact=document.getElementById('raw-artifact').value;rawFiltered=rawIndex.rows.filter(row=>(!artifact||row.a===artifact)&&(!q||rawSearchText(row).includes(q)));rawPage=1;renderRawPage();}}
function createNode(tag,className,text){{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;}}
function metaItem(label,value){{const box=createNode('div','raw-meta-item');box.append(createNode('span','',label),createNode('strong','',value||'-'));return box;}}
function fillRawRecord(details,row){{const shard=window.__llmtraceRawShards[row.h]||[];const parsedRecord=shard[row.o]??null;const body=createNode('div','raw-body');const meta=createNode('div','raw-meta-grid');meta.append(metaItem('파서',row.p),metaItem('원본 경로',row.s),metaItem('원본 위치',row.l),metaItem('시각',row.t));const jsonGrid=createNode('div','raw-json-grid single');const panel=createNode('section','raw-json-panel');panel.append(createNode('h3','','1차 파싱 원본 (parsed_record)'));const pre=createNode('pre','');pre.textContent=JSON.stringify(parsedRecord,null,2);panel.append(pre);jsonGrid.append(panel);body.append(meta,jsonGrid);details.append(body);details.dataset.loaded='1';}}
function buildRawRecord(row){{const details=createNode('details','raw-record');const summary=document.createElement('summary');summary.append(createNode('span','raw-id',row.i||'(ID 없음)'),createNode('span','raw-tag',row.n||row.a||'unknown'));if(row.t)summary.append(createNode('span','raw-ts',row.t));details.append(summary);details.addEventListener('toggle',()=>{{if(details.open&&!details.dataset.loaded)fillRawRecord(details,row);}});return details;}}
async function renderRawPage(){{const token=++rawRenderToken;const list=document.getElementById('raw-list');list.replaceChildren(createNode('div','raw-loading','페이지를 불러오는 중...'));const pages=Math.max(1,Math.ceil(rawFiltered.length/RAW_PAGE_SIZE));if(rawPage>pages)rawPage=pages;const start=(rawPage-1)*RAW_PAGE_SIZE;const rows=rawFiltered.slice(start,start+RAW_PAGE_SIZE);try{{await Promise.all([...new Set(rows.map(row=>row.h))].map(loadRawShard));}}catch(error){{if(token===rawRenderToken)list.replaceChildren(createNode('p','empty','원본 레코드를 불러오지 못했습니다.'));return;}}if(token!==rawRenderToken)return;list.replaceChildren();rows.forEach(row=>list.append(buildRawRecord(row)));if(!rows.length)list.append(createNode('p','empty','조건에 맞는 Observation이 없습니다.'));document.getElementById('raw-count').textContent='전체 '+rawIndex.count.toLocaleString()+'건 중 '+rawFiltered.length.toLocaleString()+'건';document.getElementById('raw-page').textContent=rawPage+' / '+pages;document.getElementById('raw-prev').disabled=rawPage<=1;document.getElementById('raw-next').disabled=rawPage>=pages;}}
document.getElementById('raw-search').addEventListener('input',applyRawFilter);document.getElementById('raw-artifact').addEventListener('change',applyRawFilter);document.getElementById('raw-prev').addEventListener('click',()=>{{if(rawPage>1){{rawPage--;renderRawPage();}}}});document.getElementById('raw-next').addEventListener('click',()=>{{const pages=Math.max(1,Math.ceil(rawFiltered.length/RAW_PAGE_SIZE));if(rawPage<pages){{rawPage++;renderRawPage();}}}});
if(location.hash==='#raw'){{const rawTab=navTabs.find(tab=>tab.dataset.view==='raw-view');if(rawTab)rawTab.click();}}
</script>
</body></html>'''


def export_team_share(profile: Any, out_dir: str,
                      observations: Iterable[Any] = (),
                      inferences: Iterable[Any] = (),
                      environment: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Export a case-specific visualization of matched actions and evidence."""
    observations = list(observations)
    inferences = list(inferences)
    actions = _build_match_actions(profile, observations, inferences)
    artifact_rows = [row for action in actions for row in action['rows']]
    csv_rows = _build_match_csv_rows(actions)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    config = profile.team_share
    csv_name = getattr(config, 'CSV_FILENAME', DEFAULT_CSV_FILENAME)
    html_name = getattr(config, 'HTML_FILENAME', DEFAULT_HTML_FILENAME)
    csv_path = out / csv_name
    html_path = out / html_name
    write_csv(str(csv_path), TEAM_SHARE_COLUMNS, csv_rows)
    raw_data = _write_raw_parsing_data(profile, out, observations)
    html_path.write_text(
        _render_html(
            profile, actions, environment or {}, raw_data['records'],
        ),
        encoding='utf-8',
    )
    return {
        'profile': profile.metadata,
        'mode': 'case_match_export',
        'actions': len(actions),
        'matches': len(inferences),
        'csv_rows': len(csv_rows),
        'artifact_rows': len(artifact_rows),
        'raw_records': raw_data['records'],
        'raw_data': raw_data,
        'csv': str(csv_path),
        'html': str(html_path),
        'output': str(out),
    }
