# -*- coding: utf-8 -*-
r"""ActTrace HTML 보고서 — 타임라인 · 행위 박스 · 아티팩트 핀.

설계 (팀 합의, 2026-10-02)

  타임라인 위에 행위(B코드)별 **박스**를 놓고, 그 안에 근거 하나를
  **핀** 하나로 찍는다.

    박스 클릭   그 행위의 종합 해석 — 근거 수 · 등급 분포 · 아티팩트 종류
    핀  클릭    그 근거 하나의 상세 — artifact · type · scope · **경로**

  ★ 아티팩트 내용은 싣지 않는다.
    Cache 나 History 처럼 큰 것은 한 화면에 못 담는다. 대신 경로를 주고
    "직접 확인하라" 로 넘긴다. 경로는 복사 버튼과 file:// 링크를 둔다.
    (브라우저가 탐색기를 직접 띄울 수는 없다 — 보안 정책)

  단일 파일이다. 바깥 CSS·JS 를 안 쓴다 (공통 명세 §5 HTML 규약).
  한국어가 어절 단위로 끊기도록 word-break:keep-all 을 쓴다.
"""
import io
import json
import os

from . import uniform

BEHAV_ORDER = ['B01', 'B02', 'B03', 'B04', 'B05', 'B06', 'B07',
               'B08', 'B09', 'B10', 'B11', 'B12', 'B13']

LEVEL_KO = {'confirmed': '확정', 'suspected': '유력',
            'presumed': '추정', 'undecided': '판정불가'}


def _esc(s):
    return (str(s or '').replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def _pins(infs, obs_by_id):
    """추론 하나 = 핀 하나. 근거 관찰에서 경로를 끌어온다."""
    out = []
    for i in infs:
        d = i.as_dict() if hasattr(i, 'as_dict') else dict(i)
        ev = []
        for oid in (d.get('supported_by') or [])[:12]:
            o = obs_by_id.get(oid)
            if not o:
                continue
            ev.append({
                'id': oid,
                'artifact': o.source_artifact,
                'type': o.fact_type,
                'path': o.source_path,
                'locator': o.source_locator,
                'status': o.parser_status,
            })
        out.append({
            'id': d.get('id'),
            'b': d.get('behavior_id'),
            'name': d.get('behavior_name'),
            'q': d.get('question'),
            'level': d.get('confidence'),
            'from': d.get('when_from') or '',
            'to': d.get('when_to') or '',
            'span': d.get('span_sec'),
            'rule': d.get('rule_id'),
            'basis': d.get('when_basis') or '',
            'note': d.get('note') or '',
            'alts': d.get('alternatives') or [],
            'arts': sorted({e['artifact'] for e in ev}),
            'ev': ev,
        })
    return out


_CSS = """
:root{--bg:#faf9f7;--fg:#1a1a1a;--dim:#6b6b6b;--line:#e0ddd8;--card:#fff;
--c1:#2563eb;--c2:#7c3aed;--c3:#ca8a04;--c4:#9ca3af;--hi:#fef3c7}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#1a1917;--fg:#eceae6;--dim:#9a958d;--line:#33312e;--card:#242220;
--c1:#60a5fa;--c2:#a78bfa;--c3:#fbbf24;--c4:#6b7280;--hi:#422006}}
:root[data-theme=dark]{--bg:#1a1917;--fg:#eceae6;--dim:#9a958d;--line:#33312e;
--card:#242220;--c1:#60a5fa;--c2:#a78bfa;--c3:#fbbf24;--c4:#6b7280;--hi:#422006}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"Segoe UI","Malgun Gothic",sans-serif;
word-break:keep-all;overflow-wrap:anywhere}
.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 80px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--dim);font-size:13px;margin-bottom:18px}
.meta{display:grid;grid-template-columns:auto 1fr;gap:2px 14px;
font-size:12px;color:var(--dim);margin-bottom:22px}
.meta b{font-weight:600;color:var(--fg)}
.mono{font-family:ui-monospace,Consolas,"D2Coding",monospace;font-size:12px}
h2.sec{font-size:14px;margin:26px 0 10px;padding-bottom:6px;
border-bottom:1px solid var(--line);color:var(--dim);font-weight:600;
letter-spacing:.02em}
.g{display:inline-block;font-size:11px;font-weight:700;padding:1px 7px;
border-radius:3px;white-space:nowrap}
.gd{background:var(--c1);color:#fff}
.gs{background:transparent;color:var(--dim);border:1px solid var(--line)}
.legend{font-size:12px;color:var(--dim);margin:-8px 0 4px}
.legend .g{margin-right:5px}
.legend div{margin:3px 0}
.pathcell{font-family:ui-monospace,Consolas,monospace;font-size:11.5px}
.axis{position:relative;height:26px;margin:0 0 6px;border-bottom:1px solid var(--line)}
.axis span{position:absolute;top:6px;font-size:11px;color:var(--dim);
transform:translateX(-50%);white-space:nowrap}
.box{background:var(--card);border:1px solid var(--line);border-radius:8px;
margin-bottom:10px;overflow:hidden}
.box>summary{list-style:none;cursor:pointer;padding:10px 14px;display:flex;
gap:10px;align-items:center;flex-wrap:wrap}
.box>summary::-webkit-details-marker{display:none}
.box>summary:hover{background:var(--hi)}
.code{font-weight:700;font-size:12px;padding:2px 7px;border-radius:4px;
background:var(--c1);color:#fff;flex:none}
.bname{font-weight:600}
.cnt{color:var(--dim);font-size:12px;margin-left:auto;flex:none}
.track{position:relative;height:30px;margin:2px 14px 10px;
border-top:1px dashed var(--line)}
.pin{position:absolute;top:6px;width:11px;height:11px;border-radius:50%;
border:2px solid var(--card);cursor:pointer;transform:translateX(-50%)}
.pin:hover{outline:2px solid var(--fg)}
.lv-confirmed{background:var(--c1)}.lv-suspected{background:var(--c2)}
.lv-presumed{background:var(--c3)}.lv-undecided{background:var(--c4)}
.inner{padding:0 14px 14px}
.sum{font-size:13px;color:var(--dim);margin:0 0 10px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);
vertical-align:top}
th{color:var(--dim);font-weight:600;white-space:nowrap}
tr.row{cursor:pointer}tr.row:hover{background:var(--hi)}
.tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:3px;
border:1px solid var(--line);color:var(--dim);margin-right:4px}
#panel{position:fixed;right:0;top:0;bottom:0;width:min(440px,92vw);
background:var(--card);border-left:1px solid var(--line);padding:18px;
overflow:auto;transform:translateX(100%);transition:transform .18s;z-index:9}
#panel.on{transform:none;box-shadow:-8px 0 24px rgba(0,0,0,.14)}
#panel h2{font-size:15px;margin:0 0 2px}
#panel .x{position:absolute;right:12px;top:10px;cursor:pointer;
color:var(--dim);font-size:20px;line-height:1;border:0;background:none}
.kv{display:grid;grid-template-columns:72px 1fr;gap:3px 10px;font-size:12.5px;
margin:10px 0}
.kv b{color:var(--dim);font-weight:600}
.path{background:var(--bg);border:1px solid var(--line);border-radius:6px;
padding:8px;margin:6px 0;font-family:ui-monospace,Consolas,monospace;
font-size:11.5px}
.btn{font:inherit;font-size:11.5px;padding:3px 9px;border:1px solid var(--line);
border-radius:5px;background:var(--bg);color:var(--fg);cursor:pointer;
margin:4px 6px 0 0}
.btn:hover{background:var(--hi)}
.alt{font-size:12px;color:var(--dim);border-left:2px solid var(--c3);
padding-left:8px;margin:6px 0}
.note{font-size:12px;color:var(--dim);margin-top:16px;padding-top:10px;
border-top:1px solid var(--line)}
@media(max-width:640px){.wrap{padding:16px}#panel{width:100%}}
"""

_JS = """
var DATA=%s;
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,
function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function copy(t,el){
  var ok=function(){var o=el.textContent;el.textContent='복사됨';
    setTimeout(function(){el.textContent=o},1200)};
  try{navigator.clipboard.writeText(t).then(ok,function(){fb(t,ok)})}
  catch(e){fb(t,ok)}}
function fb(t,ok){try{var a=document.createElement('textarea');a.value=t;
  document.body.appendChild(a);a.select();document.execCommand('copy');
  document.body.removeChild(a);ok()}catch(e){}}
function panel(html){var p=document.getElementById('panel');
  document.getElementById('pbody').innerHTML=html;p.classList.add('on')}
function closeP(){document.getElementById('panel').classList.remove('on')}
function showPin(id){
  var p=null;for(var i=0;i<DATA.pins.length;i++){if(DATA.pins[i].id===id){p=DATA.pins[i];break}}
  if(!p)return;
  var h='<h2>'+esc(p.name)+'</h2><div class="sub mono">'+esc(p.id)+' · '+esc(p.rule)+'</div>';
  h+='<div class="kv"><b>등급</b><span>'+esc(p.levelKo)+'</span>';
  h+='<b>시각</b><span class="mono">'+esc(p.from||'—');
  if(p.to&&p.to!==p.from)h+=' ~ '+esc(p.to);
  if(p.span)h+=' ('+esc(p.span)+'초)';
  h+='</span><b>근거</b><span>'+esc(p.basis||'—')+'</span></div>';
  if(p.note)h+='<p>'+esc(p.note)+'</p>';
  (p.alts||[]).forEach(function(a){h+='<div class="alt">배제 못 한 설명 — '+esc(a)+'</div>'});
  h+='<div class="note"><b>아티팩트 '+p.ev.length+'건</b> — 내용은 싣지 않는다. 경로에서 직접 확인한다.</div>';
  p.ev.forEach(function(e){
    h+='<div class="path"><div><span class="tag">'+esc(e.artifact)+'</span>';
    h+='<span class="tag">'+esc(e.type)+'</span>';
    if(e.status&&e.status!=='ok')h+='<span class="tag">'+esc(e.status)+'</span>';
    h+='</div><div style="margin-top:6px">'+esc(e.path||'—')+'</div>';
    if(e.locator)h+='<div style="color:var(--dim);margin-top:3px">'+esc(e.locator)+'</div>';
    var full=(DATA.root?DATA.root+'\\\\':'')+(e.path||'');
    h+='<button class="btn" onclick="copy('+JSON.stringify(full).replace(/"/g,'&quot;')+',this)">경로 복사</button>';
    h+='<a class="btn" href="file:///'+esc(full.replace(/\\\\/g,'/'))+'" target="_blank">열기</a>';
    h+='<span class="tag mono">'+esc(e.id)+'</span></div>'});
  panel(h)}
function showBox(b){
  var ps=DATA.pins.filter(function(p){return p.b===b});
  if(!ps.length)return;
  var lv={},ar={},ts=[];
  ps.forEach(function(p){lv[p.levelKo]=(lv[p.levelKo]||0)+1;
    (p.arts||[]).forEach(function(a){ar[a]=(ar[a]||0)+1});
    if(p.from)ts.push(p.from)});
  ts.sort();
  var h='<h2>'+esc(b)+' '+esc((DATA.bnames||{})[b]||ps[0].name)+'</h2>';
  h+='<div class="sub">근거 '+ps.length+'건</div>';
  h+='<div class="kv"><b>등급</b><span>'+Object.keys(lv).map(function(k){
    return esc(k)+' '+lv[k]}).join(' · ')+'</span>';
  h+='<b>구간</b><span class="mono">'+(ts.length?esc(ts[0])+' ~ '+esc(ts[ts.length-1]):'—')+'</span>';
  h+='<b>아티팩트</b><span>'+Object.keys(ar).map(function(k){
    return esc(k)+'×'+ar[k]}).join(', ')+'</span></div>';
  var n=Object.keys(ar).length;
  h+='<p>서로 다른 아티팩트 '+n+'종이 같은 행위를 가리킨다.'+
     (n>1?' 한 출처가 틀려도 나머지가 남는다.':' 출처가 하나뿐이라 교차검증이 안 된다.')+'</p>';
  h+='<div class="note">핀을 누르면 근거 하나하나의 경로가 나온다.</div>';
  panel(h)}
document.addEventListener('keydown',function(e){if(e.key==='Escape')closeP()});
"""


def write_html(outdir, obs, infs, meta, urows=None):
    """ActTrace 보고서를 단일 HTML 파일로 낸다.

    urows 는 report.csv 와 **같은** 통일 4열 행이다. 같은 입력을 쓰므로
    표와 CSV 가 어긋날 수 없다. 안 주면 여기서 만든다 (단독 호출 대비).
    """
    if urows is None:
        urows = uniform.rows(obs, infs, BEHAV_ORDER)
    # 토글 제목은 **표준 행위 이름**으로 단다. 규칙이 추론마다 붙인 이름
    # ('사용자가 직접 실행' 처럼)을 제목에 쓰면 CSV 의 '행위 분류' 와
    # 글자가 달라져 두 표가 같은 것인지 알 수 없게 된다.
    try:
        BNAMES = uniform._names()
    except Exception:
        BNAMES = {}
    by_art = {}
    for r in urows:
        by_art.setdefault(r['b'], []).append(r)
    obs_by_id = {o.id: o for o in obs}
    pins = _pins(infs, obs_by_id)
    for p in pins:
        p['levelKo'] = LEVEL_KO.get(p['level'], p['level'])

    # 시간축 — 시각이 있는 핀만으로 범위를 잡는다
    ts = sorted(p['from'] for p in pins if p['from'])
    t0, t1 = (ts[0], ts[-1]) if ts else ('', '')

    def pos(v):
        """ISO 시각 -> 0~100%. 못 재면 None."""
        if not v or not t0 or t0 == t1:
            return None
        try:
            a, b, c = t0[:19], t1[:19], v[:19]
            import datetime as dt
            f = '%Y-%m-%dT%H:%M:%S'
            A = dt.datetime.strptime(a, f)
            B = dt.datetime.strptime(b, f)
            C = dt.datetime.strptime(c, f)
            span = (B - A).total_seconds()
            if span <= 0:
                return None
            return max(0.0, min(100.0, 100.0 * (C - A).total_seconds() / span))
        except Exception:
            return None

    by_b = {}
    for p in pins:
        by_b.setdefault(p['b'], []).append(p)

    H = []
    a = H.append
    a('<!doctype html><html lang="ko"><head><meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width,initial-scale=1">')
    a('<title>ActTrace 행위 재구성</title><style>%s</style></head><body>' % _CSS)
    a('<div class="wrap">')
    a('<h1>ActTrace — 행위 재구성</h1>')
    a('<div class="sub">타임라인 위 상자가 행위, 점이 근거 하나다. '
      '점을 누르면 그 근거가 나온 <b>파일 경로</b>가 나온다.</div>')

    a('<h2 class="sec">개요</h2>')
    a('<div class="meta">')
    for k, v in (('입력', meta.get('input')), ('지문', meta.get('input_sha256')),
                 ('환경', meta.get('env_profile')),
                 ('도구', '%s %s (명세 v%s)' % (meta.get('tool'),
                                               meta.get('tool_version'),
                                               meta.get('spec_version'))),
                 ('실행', '%s ~ %s' % (meta.get('started_at'),
                                       meta.get('finished_at'))),
                 ('결과', '관찰 %s · 추론 %s · 문제 %s'
                  % (meta.get('observations'), meta.get('inferences'),
                     meta.get('problems')))):
        a('<b>%s</b><span class="mono">%s</span>' % (_esc(k), _esc(v)))
    a('</div>')

    # 등급의 뜻을 보고서 안에 적는다. 표만 보고 '직접' 을 제 느낌대로
    # 읽으면 통일한 의미가 없다.
    a('<div class="legend">')
    a('<div><span class="g gd">%s</span>%s</div>'
      % (_esc(uniform.DIRECT), _esc(uniform.GRADE_DEF[uniform.DIRECT])))
    a('<div><span class="g gs">%s</span>%s</div>'
      % (_esc(uniform.SUPPORT), _esc(uniform.GRADE_DEF[uniform.SUPPORT])))
    a('</div>')

    a('<h2 class="sec">행위</h2>')

    if t0:
        a('<div class="axis">')
        for frac, lab in ((0, t0[11:19]), (50, '·'), (100, t1[11:19])):
            a('<span style="left:%d%%">%s</span>' % (frac, _esc(lab)))
        a('</div>')

    for b in BEHAV_ORDER:
        ps = by_b.get(b)
        if not ps:
            continue
        ps.sort(key=lambda x: (x['from'] or '', x['id']))
        lv = {}
        for p in ps:
            lv[p['levelKo']] = lv.get(p['levelKo'], 0) + 1
        arts = sorted({x for p in ps for x in p['arts']})
        a('<details class="box"%s>' % (' open' if len(ps) <= 6 else ''))
        a('<summary onclick="showBox(\'%s\')">' % _esc(b))
        a('<span class="code">%s</span>' % _esc(b))
        a('<span class="bname">%s</span>'
          % _esc(BNAMES.get(b) or ps[0]['name']))
        gr = by_art.get(b) or []
        nd = sum(1 for r in gr if r['아티팩트 등급'] == uniform.DIRECT)
        ns = len(gr) - nd
        a('<span class="cnt">%s %d · %s %d · 근거 %d</span>'
          % (_esc(uniform.DIRECT), nd, _esc(uniform.SUPPORT), ns, len(ps)))
        a('</summary>')

        a('<div class="track">')
        for p in ps:
            x = pos(p['from'])
            if x is None:
                continue
            a('<span class="pin lv-%s" style="left:%.2f%%" title="%s"'
              ' onclick="showPin(\'%s\')"></span>'
              % (_esc(p['level']), x,
                 _esc((p['from'][11:19] + ' ' + (p['note'] or ''))[:60]),
                 _esc(p['id'])))
        a('</div>')

        a('<div class="inner">')
        # ── 팀 합의 표 : 아티팩트 | 경로 | 아티팩트 등급 ──
        #   report.csv 의 같은 행위 부분과 한 줄씩 그대로 맞는다.
        a('<div class="scroll"><table>')
        a('<tr><th>아티팩트</th><th>경로</th><th>아티팩트 등급</th></tr>')
        for r in gr:
            cls = 'gd' if r['아티팩트 등급'] == uniform.DIRECT else 'gs'
            a('<tr><td>%s</td><td class="pathcell">%s</td>'
              '<td><span class="g %s">%s</span></td></tr>'
              % (_esc(r['아티팩트']), _esc(r['경로'] or '—'), cls,
                 _esc(r['아티팩트 등급'])))
        if not gr:
            a('<tr><td colspan="3">근거 관찰에 경로가 붙지 않았다</td></tr>')
        a('</table></div>')

        # ── 아래는 ActTrace 고유 — 시각 · 판정 등급 · 근거 수 ──
        a('<p class="sum">근거 %d건 · 아티팩트 %d종 — %s</p>'
          % (len(ps), len(arts), _esc(', '.join(arts))))
        a('<div class="scroll"><table><tr><th>시각</th><th>등급</th>'
          '<th>출처</th><th>내용</th><th>근거</th></tr>')
        for p in ps:
            a('<tr class="row" onclick="showPin(\'%s\')">' % _esc(p['id']))
            a('<td class="mono">%s</td>' % _esc(p['from'][11:23] or '—'))
            a('<td>%s</td>' % _esc(p['levelKo']))
            a('<td>%s</td>' % _esc(' · '.join(p['arts']) or '—'))
            a('<td>%s</td>' % _esc(p['note'] or p['basis'] or ''))
            a('<td class="mono">%d건</td>' % len(p['ev']))
            a('</tr>')
        a('</table></div></div></details>')

    a('<div class="note">아티팩트 내용은 이 보고서에 싣지 않는다. '
      'Cache 나 History 처럼 큰 것은 한 화면에 담기지 않으므로, '
      '경로를 주고 직접 확인하게 한다. 판단 근거의 원본은 '
      '<span class="mono">observations.jsonl</span> 에 있다.</div>')
    a('</div>')
    a('<aside id="panel"><button class="x" onclick="closeP()">&times;</button>'
      '<div id="pbody"></div></aside>')

    payload = {'pins': pins, 'root': meta.get('input') or '',
               'bnames': BNAMES}
    a('<script>%s</script>' % (_JS % json.dumps(payload, ensure_ascii=False)))
    a('</body></html>')

    p = os.path.join(outdir, 'report.html')
    with io.open(p, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(H))
    return p
