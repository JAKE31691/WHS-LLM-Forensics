# -*- coding: utf-8 -*-
r"""ActTrace HTML 보고서 — 팀 통일 형식 (2026-10-13).

다른 조 산출물(LLM_Artifact_Report)과 **줄 단위로 맞춰 본다.** 구조 ·
클래스 이름 · 배지 색까지 같게 둔 이유가 그것이다. 모양을 바꾸면 대조가
안 된다.

    .layout
      aside.sidebar     [분석 결과] [판정 규칙]  ← 탭으로 화면을 바꾼다
      main
        [data-view=analysis]
          section.overview   분석 개요
          section.results    행위별 매칭 결과
              ▸ A00 설치                       매칭 N건
                  ▸ 2026-10-05 22:05:19.241 KST  매칭 N건
                      표: 시각|아티팩트|경로|등급|근거
        [data-view=rules]
          section.appendix   판정 규칙

★ 근거 칸은 평문이 아니라 **구조**다. 그래서 한국어 해석을 열을 더 붙이지
  않고 그 안에 넣을 수 있다 —

      [규칙 ID]
      · 내용 (그래서 무슨 일이 있었나)
      · 필드 = 값
      · 시각
      ▸ 원본 근거 보기        <- 펼치면 artifacts[i].records[j] 표기

  표를 훑을 때는 해석이 보이고, 의심스러우면 펼쳐서 원본 위치를 본다.
  5열 그대로이면서 해석을 잃지 않는다.

내용은 `evidence.rows()` 가 만든 것을 그대로 쓴다 — CSV 와 HTML 이 **같은
함수에서 나오므로** 둘이 어긋날 수 없다.

★ 아티팩트 내용은 싣지 않는다. Cache·History 처럼 큰 것은 한 화면에 못
  담는다. 경로를 주고 직접 확인하게 한다.
"""
import io
import os
import re

from . import actions, evidence

_CSS = """
:root { color-scheme:light; --ink:#172033; --muted:#64748b; --line:#dce3ed;
--panel:#fff; --navy:#17345f; --bg:#f4f7fb; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
font:14px/1.55 Arial,"Malgun Gothic",sans-serif; word-break:keep-all;
overflow-wrap:break-word; }
h1 { font-size:28px; margin:0 0 16px; }
h2 { font-size:19px; margin:0 0 14px; }
.layout { display:grid; grid-template-columns:190px minmax(0,1fr);
min-height:100vh; }
.layout main { min-width:0; padding:26px 32px 40px; }
.layout main, [data-view], .results, .overview, .appendix,
details.action, details.match { min-width:0; }
.sidebar { position:sticky; top:0; height:100vh; background:#10294d;
color:#fff; padding:22px 12px; }
.sidebar-brand { font-size:20px; font-weight:800; padding:2px 9px 20px; }
.sidebar nav { display:grid; gap:7px; }
.sidebar .nav-tab { width:100%; border:0; border-radius:9px; padding:11px;
text-align:left; background:transparent; color:#cad6e8; font:inherit;
font-weight:600; cursor:pointer; }
.sidebar .nav-tab:hover { background:#ffffff12; color:#fff; }
.sidebar .nav-tab.active { background:#fff; color:#10294d; }
.sidebar-note { color:#8fa4c4; font-size:11.5px; line-height:1.5;
padding:16px 9px 0; }
.overview, .results, .appendix { background:var(--panel);
border:1px solid var(--line); border-radius:14px; padding:20px; margin:14px 0;
box-shadow:0 4px 18px #1720330a; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
gap:10px; }
.stats div { border:1px solid var(--line); border-radius:10px; padding:11px; }
.stats span { display:block; color:var(--muted); font-size:12px; }
.stats strong { font-size:15px; overflow-wrap:anywhere; }
.stats.sub strong { font:12px/1.5 Consolas,"Malgun Gothic",monospace; }
.toolbar { display:flex; gap:9px; flex-wrap:wrap; margin:14px 0; }
.toolbar input { flex:1; min-width:250px; padding:10px 12px;
border:1px solid #b8c3d3; border-radius:8px; font:inherit; }
.toolbar button { padding:9px 12px; border:1px solid #9aacbf; background:#fff;
border-radius:8px; cursor:pointer; font:inherit; }
details.action { background:#fff; border:1px solid var(--line);
border-radius:12px; margin:9px 0; overflow:hidden; }
details.match { background:#fbfcfe; border:1px solid var(--line);
border-radius:9px; margin:9px 0; overflow:hidden; }
summary { display:flex; align-items:center; gap:10px; padding:14px 16px;
cursor:pointer; list-style:none; font-weight:700; }
summary::-webkit-details-marker { display:none; }
details.action>summary::after, details.match>summary::after { content:"\\FF0B";
margin-left:auto; color:var(--muted); font-size:18px; }
details.action[open]>summary::after,
details.match[open]>summary::after { content:"\\FF0D"; }
details.action[open]>summary, details.match[open]>summary {
border-bottom:1px solid var(--line); }
summary:focus-visible { outline:2px solid var(--navy); outline-offset:-3px; }
.action-id { background:var(--navy); color:#fff; border-radius:6px;
padding:3px 7px; font-weight:700; }
.summary-meta, .match-meta { color:var(--muted); font-weight:400; }
.action-body { padding:8px 16px 16px; }
.match-time { display:inline-flex; gap:7px; align-items:center; }
.match-date, .match-clock { border:1px solid var(--line); border-radius:6px;
background:#fff; padding:2px 7px; }
.match-body { background:#fff; padding:0 12px; }
.grade { display:inline-block; border-radius:999px; padding:3px 9px;
font-size:12px; font-weight:700; white-space:nowrap; }
.direct { background:#dff4e9; color:#155e3d; }
.supporting { background:#e8f0ff; color:#244b91; }
.table-wrap { overflow:auto; }
table { border-collapse:collapse; width:100%; }
th,td { border-bottom:1px solid var(--line); padding:10px; text-align:left;
vertical-align:top; }
th { background:#f7f9fc; color:#31415c; }
td { overflow-wrap:anywhere; }
td:first-child { white-space:nowrap; }
td:nth-child(3) { font:12px/1.55 Consolas,"Malgun Gothic",monospace;
min-width:250px; word-break:break-all; }
.match-body th:nth-child(5) { width:520px; }
.match-body td:nth-child(5) { min-width:520px; white-space:normal;
word-break:break-word; }
tbody tr:last-child td { border-bottom:0; }
.rule-id { display:inline-block; font:700 12px/1.4 Consolas,monospace;
background:#f1f4f9; border:1px solid var(--line); border-radius:6px;
padding:2px 7px; color:#31415c; }
ul.readable { margin:2px 0 6px; padding-left:18px; }
ul.readable li { margin:2px 0; }
.rv-note { font-weight:600; }
.rv-field { color:var(--muted); font-family:Consolas,monospace; font-size:12px; }
.rv-value { font-family:Consolas,monospace; font-size:12px;
word-break:break-all; }
.rv-time { color:#244b91; white-space:nowrap; }
details.raw > summary { display:list-item; list-style:disclosure-closed inside;
cursor:pointer; color:var(--muted); font-size:12px; padding:2px 0;
font-weight:400; }
details.raw[open] > summary { list-style-type:disclosure-open; }
details.raw > summary::after { content:none; }
details.raw pre { white-space:pre-wrap; word-break:break-all;
font:12px/1.55 Consolas,"Malgun Gothic",monospace; background:#f7f9fc;
border:1px solid var(--line); border-radius:7px; padding:9px; margin:5px 0 0; }
.appendix table { min-width:1200px; table-layout:fixed; }
.appendix td { font-size:13px; }
.appendix td.mono { font:12px Consolas,monospace; }
.empty { border:1px dashed #b8c3d3; border-radius:9px; padding:15px;
color:var(--muted); }
.note { color:var(--muted); font-size:12.5px; margin-top:14px; }
[hidden] { display:none !important; }
@media(max-width:860px) {
.layout { display:block; }
.layout main { padding:14px; }
.sidebar { position:static; height:auto; padding:12px; }
.sidebar-brand { padding:2px 9px 10px; }
.sidebar nav { grid-template-columns:repeat(2,minmax(0,1fr)); }
.results,.overview,.appendix { padding:14px; } }
"""

_JS = """
const views = Array.from(document.querySelectorAll('[data-view]'));
for (const button of document.querySelectorAll('[data-view-button]')) {
  button.addEventListener('click', () => {
    const sel = button.dataset.viewButton;
    for (const v of views) v.hidden = v.dataset.view !== sel;
    for (const nav of document.querySelectorAll('[data-view-button]')) {
      nav.classList.toggle('active', nav === button);
      nav.setAttribute('aria-selected', nav === button ? 'true' : 'false');
    }
  });
}
const search = document.getElementById('search');
const actions = Array.from(document.querySelectorAll('details.action'));
function filterActions() {
  const q = search.value.trim().toLocaleLowerCase();
  for (const action of actions) {
    const hitName = action.querySelector('.action-name')
      .textContent.toLocaleLowerCase().includes(q);
    let visible = 0;
    for (const match of action.querySelectorAll('details.match')) {
      let rows = 0;
      for (const row of match.querySelectorAll('tbody tr')) {
        const hit = !q || hitName ||
          row.textContent.toLocaleLowerCase().includes(q);
        row.hidden = !hit;
        if (hit) rows++;
      }
      match.hidden = rows === 0;
      visible += rows;
    }
    action.hidden = visible === 0;
  }
}
search.addEventListener('input', filterActions);
function setAll(open) {
  for (const d of document.querySelectorAll('details.action, details.match'))
    d.open = open;
}
document.getElementById('expand').addEventListener('click', () => setAll(true));
document.getElementById('collapse').addEventListener('click',
  () => setAll(false));
"""


def _esc(s):
    return (str(s if s is not None else '').replace('&', '&amp;')
            .replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;'))


_PAIR = re.compile(r'^([A-Za-z_][\w.\[\]]*)=(.*)$')


def _readable(r):
    r"""근거를 읽을 수 있는 목록으로 편다.

    CSV 의 `근거` 는 `artifacts[0].records[0].name="x"; @loc; 외 3건` 꼴로
    한 줄이다. 표에서는 그걸 그대로 보여 주면 읽히지 않으므로 앞 몇 개를
    `필드 = 값` 으로 끊어 낸다. 전체는 '원본 근거 보기' 안에 그대로 둔다.
    """
    out = []
    for chunk in (r.get('근거') or '').split('; '):
        chunk = chunk.strip()
        if not chunk or chunk.startswith('@') or chunk.startswith('외 '):
            continue
        m = _PAIR.match(chunk)
        if not m:
            continue
        field, val = m.group(1), m.group(2).strip()
        # 인덱스 접두사는 떼고 필드만 보여 준다 — 전체 경로는 원본 쪽에
        field = field.rsplit('.', 1)[-1]
        out.append((field, val.strip('"')))
        if len(out) >= 3:
            break
    return out


def write_html(outdir, obs, infs, meta, erows=None):
    """팀 통일 형식 HTML. 내용은 CSV 와 같은 행에서 나온다."""
    env = meta.get('env_profile') or ''
    if erows is None:
        erows = evidence.rows(obs, infs, env)

    by_act = {}
    for r in erows:
        by_act.setdefault((r['code'], r['사용자 행위']), []).append(r)

    def akey(k):
        try:
            return (actions.ORDER.index(k[0]), k[0])
        except ValueError:
            return (len(actions.ORDER), k[0])

    nd = sum(1 for r in erows if r['등급'] == evidence.DIRECT)
    title = 'LLM_Artifact_Report'

    H = []
    a = H.append
    a('<!doctype html>')
    a('<html lang="ko">')
    a('<head>')
    a('<meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width, initial-scale=1">')
    a('<title>%s</title>' % _esc(title))
    a('<style>%s</style>' % _CSS)
    a('</head>')
    a('<body>')
    a('<div class="layout">')

    # ── 사이드바 ─────────────────────────────────────────────
    a('<aside class="sidebar">')
    a('<div class="sidebar-brand">ActTrace</div>')
    a('<nav>')
    a('<button type="button" class="nav-tab active" data-view-button="analysis"'
      ' aria-selected="true">분석 결과</button>')
    a('<button type="button" class="nav-tab" data-view-button="rules"'
      ' aria-selected="false">판정 규칙</button>')
    a('</nav>')
    a('</aside>')

    a('<main>')
    a('<h1>%s</h1>' % _esc(title))

    # ── 분석 결과 ────────────────────────────────────────────
    a('<div data-view="analysis">')
    a('<section class="overview" aria-label="분석 개요">')
    a('<h2>분석 개요</h2>')
    a('<div class="stats">')
    for lab, val in (
            ('분석 대상', 'Claude Desktop'),
            ('입력 형식', (meta.get('input_kind') or '-')),
            ('매칭 행위', '%d개' % len(by_act)),
            ('근거 개수', '직접 %d / 보조 %d' % (nd, len(erows) - nd)),
    ):
        a('<div><span>%s</span><strong>%s</strong></div>'
          % (_esc(lab), _esc(val)))
    a('</div>')
    a('</section>')

    a('<section class="results">')
    a('<h2>행위별 매칭 결과</h2>')
    a('<div class="toolbar">'
      '<input id="search" type="search" aria-label="행위명, 아티팩트 또는 경로 검색"'
      ' placeholder="행위명, 아티팩트 또는 경로 검색">'
      '<button id="expand" type="button">모두 펼치기</button>'
      '<button id="collapse" type="button">모두 접기</button></div>')

    # ★ A00~A14 를 **전부** 찍는다. 매칭이 없는 행위도 '매칭 0건' 으로
    #   남긴다 — 0건은 결과지 부재가 아니다. 목록에서 빠지면 '안 했다' 와
    #   '못 찾았다' 가 구분되지 않는다.
    by_code = {}
    for (c, n), rs in by_act.items():
        by_code[c] = (n, rs)
    for code in actions.ORDER:
        name, rs = by_code.get(code, (actions.name_of(code, env), []))
        a('<details class="action">')
        a('<summary><span class="action-id">%s</span> '
          '<span class="action-name">%s</span>'
          '<span class="summary-meta">매칭 %d건</span></summary>'
          % (_esc(code), _esc(name), len(rs)))
        a('<div class="action-body">')
        if not rs:
            a('<p class="empty">해당 아티팩트 없음</p>')
            a('</div></details>')
            continue

        groups = {}
        for r in rs:
            groups.setdefault(r['시각'], []).append(r)
        for t in sorted(groups, key=lambda x: (x == '-', x)):
            g = groups[t]
            if t == '-':
                date, clock = '시각 없음', ''
            else:
                part = t.split(' ', 1)
                date = part[0]
                clock = part[1] if len(part) > 1 else ''
            a('<details class="match"><summary><span class="match-time">'
              '<span class="match-date">%s</span>%s</span>'
              '<span class="match-meta">매칭 %d건</span></summary>'
              % (_esc(date),
                 ('<span class="match-clock">%s</span>' % _esc(clock))
                 if clock else '', len(g)))
            a('<div class="match-body table-wrap"><table>')
            a('<thead><tr><th>시각</th><th>아티팩트</th><th>경로</th>'
              '<th>등급</th><th>근거</th></tr></thead>')
            a('<tbody>')
            for r in g:
                cls = ('direct' if r['등급'] == evidence.DIRECT
                       else 'supporting')
                ev = ['<span class="rule-id">%s</span>'
                      % _esc(r.get('규칙') or '-')]
                ev.append('<ul class="readable">')
                note = r.get('내용') or ''
                if note and note != '-':
                    ev.append('<li class="rv-note">%s</li>' % _esc(note))
                for field, val in _readable(r):
                    ev.append('<li><span class="rv-field">%s</span> = '
                              '<span class="rv-value">%s</span></li>'
                              % (_esc(field), _esc(val)))
                if r['시각'] != '-':
                    ev.append('<li class="rv-time">%s</li>' % _esc(r['시각']))
                ev.append('</ul>')
                ev.append('<details class="raw"><summary>원본 근거 보기'
                          '</summary><pre>%s</pre></details>'
                          % _esc(r.get('근거') or '-'))
                a('<tr><td>%s</td><td>%s</td><td>%s</td>'
                  '<td><span class="grade %s">%s</span></td><td>%s</td></tr>'
                  % (_esc(r['시각']), _esc(r['아티팩트']), _esc(r['경로']),
                     cls, _esc(r['등급']), ''.join(ev)))
            a('</tbody></table></div></details>')
        a('</div></details>')

    a('</section>')
    a('</div>')

    # ── 판정 규칙 ────────────────────────────────────────────
    a('<div data-view="rules" hidden>')
    a('<section class="appendix">')
    a('<h2>부록: 판정 규칙</h2>')
    a('<div class="table-wrap"><table>')
    a('<thead><tr><th>규칙 ID</th><th>행위</th><th>아티팩트</th>'
      '<th>필터</th><th>시각 필드</th><th>비고</th></tr></thead><tbody>')
    seen = {}
    for r in erows:
        k = (r.get('규칙') or '', r['code'], r['사용자 행위'])
        g = seen.setdefault(k, {'arts': set(), 'n': 0, 'grade': set(),
                                'note': r.get('내용') or '', 'fields': []})
        g['arts'].add(r['아티팩트'])
        g['grade'].add(r['등급'])
        g['n'] += 1
        for f, v in _readable(r):
            s = '%s=%s' % (f, v)
            if s not in g['fields']:
                g['fields'].append(s)
    for k in sorted(seen, key=lambda x: (akey((x[1], x[2])), x[0])):
        g = seen[k]
        arts = ', '.join(sorted(g['arts'])[:3])
        if len(g['arts']) > 3:
            arts += ' 외 %d' % (len(g['arts']) - 3)
        a('<tr><td class="mono">%s</td><td>%s %s</td><td>%s</td>'
          '<td class="mono">%s</td><td>%s</td><td>%s</td></tr>'
          % (_esc(k[0] or '-'), _esc(k[1]), _esc(k[2]), _esc(arts),
             _esc('; '.join(g['fields'][:3]) or '-'),
             _esc(' · '.join(sorted(g['grade'])) + ' / %d건' % g['n']),
             _esc(g['note'][:110])))
    a('</tbody></table></div>')
    a('</section>')
    a('</div>')

    a('</main>')
    a('</div>')
    a('<script>%s</script>' % _JS)
    a('</body></html>')

    p = os.path.join(outdir, 'LLM_Artifact_Report.html')
    with io.open(p, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(H))
    return p
