from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from .forensic_summary import build_forensic_summary, render_forensic_sections
from .raw_io import json_default
from .schema import ContextRecord, Inference, Observation


# Shared behaviors.csv contract from the integrated specification.
BEHAVIOR_COLUMNS = [
    'id', 'question', 'behavior_id', 'behavior_name', 'confidence',
    'when_from', 'when_to', 'span_sec', 'actor', 'rule_id', 'when_basis',
    'note', 'alternatives', 'supported_by',
]


def _j(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2, default=json_default
    )


def _report_value(value: Any, *, string_limit: int = 4096) -> Any:
    """Bound view-layer values while leaving forensic JSONL outputs untouched."""
    if isinstance(value, dict):
        return {str(k): _report_value(v, string_limit=string_limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        limit = 100
        cooked = [_report_value(v, string_limit=string_limit) for v in value[:limit]]
        if len(value) > limit:
            cooked.append({'_report_omitted_items': len(value) - limit})
        return cooked
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {
            '_report_binary_preview_hex': raw[:128].hex(),
            '_report_binary_length': len(raw),
        }
    if isinstance(value, str) and len(value) > string_limit:
        return value[:string_limit] + f'\n… [report omitted {len(value) - string_limit} characters]'
    return value


def render_html(
    path: str,
    metadata: Dict[str, Any],
    observations: List[Observation],
    inferences: List[Inference],
    contexts: List[ContextRecord],
    sections: List[Dict[str, Any]],
    unresolved: Dict[str, Any] | None = None,
    forensic_summary: Dict[str, Any] | None = None,
):
    """Render existing outputs; never derive a new behavior in the view layer."""

    def esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ''))

    def json_block(value: Any, *, max_chars: int | None = None) -> str:
        rendered = _j(_report_value(value))
        if max_chars is not None and len(rendered) > max_chars:
            omitted = len(rendered) - max_chars
            rendered = rendered[:max_chars] + f'\n… [report omitted {omitted} characters; see _internal/_raw outputs]'
        return f'<pre>{esc(rendered)}</pre>'

    artifacts = Counter(o.source_artifact or '(unknown)' for o in observations)
    state_rows = [
        o for o in observations
        if o.evidence_kind in ('state', 'residual')
    ]

    body: List[str] = []
    body.append('<header><h1>LLMTrace Forensic Report</h1>')
    body.append(
        '<p class="sub">판정 결과, 현재 남은 정보, 원본 파싱 결과를 분리해 표시합니다.</p></header>'
    )
    body.append('<section class="overview"><h2>실행 개요</h2><div class="stats">')
    for label, value in (
        ('Profile', metadata.get('profile_id')),
        ('Service', metadata.get('service')),
        ('Surface', metadata.get('surface')),
        ('Observations', len(observations)),
        ('Behaviors', len(inferences)),
        ('Useful info', len(contexts) + len(state_rows)),
    ):
        body.append(f'<div><span>{esc(label)}</span><strong>{esc(value)}</strong></div>')
    body.append('</div></section>')

    # 1. This is a direct view of the same inferences written to behaviors.csv.
    body.append('<section id="behaviors"><h2>1. 사용자 행위 분석 결과</h2>')
    body.append('<p class="hint">시간 범위는 Evidence Window이며 행위 지속시간을 뜻하지 않습니다.</p>')
    if not inferences:
        body.append('<p class="empty">판정된 사용자 행위가 없습니다.</p>')
    else:
        body.append('<div class="table-wrap"><table><thead><tr>')
        for heading in ('시간 범위', 'Behavior', 'Confidence', 'Matched Rule', '근거'):
            body.append(f'<th>{esc(heading)}</th>')
        body.append('</tr></thead><tbody>')
        for item in inferences:
            detail = {
                'behavior_id': item.behavior_id,
                'rule_id': item.rule_id,
                'matched_rule_level': item.detail.get('matched_rule_level', ''),
                'supported_by': item.supported_by,
                'note': item.note,
                'alternatives': item.alternatives,
                'when_basis': item.when_basis,
                'engine_detail': item.detail,
            }
            body.append('<tr>')
            body.append(
                f'<td>{esc(item.when_from)}<br><span class="muted">'
                f'~ {esc(item.when_to or "open")}</span></td>'
            )
            body.append(
                f'<td><strong>{esc(item.behavior_name)}</strong><br>'
                f'<span class="tag">{esc(item.behavior_id)}</span></td>'
            )
            body.append(f'<td>{esc(item.confidence)}</td><td>{esc(item.rule_id)}</td>')
            body.append('<td><details><summary>상세 근거 보기</summary>')
            body.append(json_block(detail))
            body.append('</details></td></tr>')
        body.append('</tbody></table></div>')
    body.append('</section>')

    # 2. Analyst-facing, deduplicated forensic information. Raw records remain
    # available in the internal JSONL and the separate Observation viewer.
    body.append('<section id="useful"><h2>2. 현재 시스템의 유의미 정보</h2>')
    body.append('<p class="hint">원본 로그를 나열하지 않고, 복원된 계정·대화·메시지 정보를 중복 제거해 표시합니다.</p>')
    body.append(render_forensic_sections(forensic_summary or build_forensic_summary(observations)))
    body.append('</section>')

    # 3. Client-side search/filter only; no semantic inference is added here.
    body.append('<section id="observations"><h2>3. Observation / 원본 파싱 결과</h2>')
    body.append(
        '<div class="filters"><label>검색 <input id="obs-search" type="search" '
        'placeholder="ID, URL, source path, JSON…"></label>'
    )
    body.append('<label>아티팩트 <select id="artifact-filter"><option value="">전체</option>')
    for artifact, count in sorted(artifacts.items()):
        body.append(f'<option value="{esc(artifact)}">{esc(artifact)} ({count})</option>')
    body.append('</select></label><span id="visible-count"></span></div>')
    body.append('<div id="observation-list">')
    for obs in observations:
        obs_dict = obs.as_dict()
        time_value = next(
            (value for key, value in obs_dict.items() if key.startswith('ts_') and value),
            obs.seq if obs.seq is not None else '',
        )
        searchable = ' '.join(
            (obs.id, obs.source_artifact, obs.fact_type, obs.source_path,
             _j(_report_value(obs.fact, string_limit=384)))
        ).lower()
        searchable = searchable[:512]
        artifact = obs.source_artifact or '(unknown)'
        body.append(
            f'<details class="record observation" data-artifact="{esc(artifact)}" '
            f'data-search="{esc(searchable)}"><summary>'
        )
        body.append(
            f'<span class="tag">{esc(obs.source_artifact)}</span> {esc(obs.id)} · '
            f'{esc(obs.fact_type or "parsed record")} '
            f'<span class="muted">{esc(time_value)}</span>'
        )
        body.append('</summary>')
        body.append(json_block({
            'observation': obs_dict,
            'source': {
                'path': obs.source_path,
                'locator': obs.source_locator,
                'parser': obs.parser,
                'status': obs.parser_status,
            },
        }, max_chars=2048))
        body.append('</details>')
    body.append('</div></section>')

    if unresolved:
        body.append('<section id="limitations"><h2>판정 보류 및 한계</h2>')
        for key, value in unresolved.items():
            body.append('<details class="record"><summary>')
            body.append(
                f'{esc(key)} · {esc(value.get("status", "unresolved"))}</summary>'
            )
            body.append(json_block(value))
            body.append('</details>')
        body.append('</section>')

    css = r'''
    :root{color-scheme:light;--ink:#172033;--muted:#64748b;--line:#dbe3ef;--panel:#fff;--accent:#3157d5}
    *{box-sizing:border-box}body{max-width:1440px;margin:0 auto;padding:32px;font:14px/1.5 Inter,Segoe UI,Arial,sans-serif;color:var(--ink);background:#f5f7fb}
    header{margin:8px 0 26px}h1{font-size:30px;margin:0}h2{font-size:20px;margin:0 0 14px}h3{font-size:18px;margin:30px 0 12px}.sub,.hint,.muted{color:var(--muted)}
    section{background:var(--panel);padding:22px;margin:16px 0;border:1px solid var(--line);border-radius:14px;box-shadow:0 3px 14px #1e293b0a}
    .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}.stats div{padding:12px;border:1px solid var(--line);border-radius:10px}.stats span{display:block;color:var(--muted);font-size:12px}.stats strong{font-size:18px}
    .table-wrap{overflow:auto}table{width:100%;border-collapse:collapse}th,td{padding:11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:#f8fafc;position:sticky;top:0}.message{white-space:pre-wrap;min-width:280px;max-width:720px;word-break:break-word}
    details.record,details.observation{border:1px solid var(--line);border-radius:9px;margin:8px 0;background:#fbfdff}summary{cursor:pointer;padding:10px 12px}pre{overflow:auto;margin:0;padding:13px;background:#101827;color:#dbeafe;border-radius:0 0 8px 8px;font:12px/1.5 Consolas,monospace;white-space:pre-wrap;word-break:break-word}
    .tag{display:inline-block;padding:2px 7px;border-radius:999px;background:#e9efff;color:#2445b7;font-size:11px}.filters{display:flex;gap:14px;align-items:end;flex-wrap:wrap;margin-bottom:12px}.filters label{display:grid;gap:4px;color:var(--muted)}input,select{min-width:220px;padding:8px;border:1px solid #b9c5d6;border-radius:7px;background:#fff}.empty{padding:14px;border:1px dashed #b9c5d6;border-radius:8px;color:var(--muted)}
    .forensic-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px}.forensic-stats div{padding:12px;border:1px solid var(--line);border-radius:10px;background:#f8fafc}.forensic-stats span{display:block;color:var(--muted);font-size:12px}.forensic-stats strong{font-size:20px}details.conversation{border:1px solid var(--line);border-radius:10px;margin:10px 0;background:#fbfdff}details.conversation>summary{cursor:pointer;padding:12px}.count-total{font-size:16px;padding:12px;border-left:4px solid var(--accent);background:#f6f8ff}
    @media(max-width:700px){body{padding:14px}section{padding:15px}input,select{min-width:0;width:100%}}
    '''
    script = r'''
    (()=>{const q=document.getElementById('obs-search'),f=document.getElementById('artifact-filter'),n=document.getElementById('visible-count'),rows=[...document.querySelectorAll('.observation')];
    const update=()=>{const needle=(q?.value||'').toLowerCase(),artifact=f?.value||'';let shown=0;rows.forEach(row=>{const ok=(!needle||row.dataset.search.includes(needle))&&(!artifact||row.dataset.artifact===artifact);row.hidden=!ok;if(ok)shown++;});if(n)n.textContent=`${shown} / ${rows.length} records`;};q?.addEventListener('input',update);f?.addEventListener('change',update);update();})();
    '''
    document = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>LLMTrace Report</title><style>' + css + '</style></head><body>'
        + ''.join(body) + '<script>' + script + '</script></body></html>'
    )
    Path(path).write_text(document, encoding='utf-8')
