# -*- coding: utf-8 -*-
"""결과 출력 — JSON / CSV / HTML + 무결성 대장."""
import csv
import datetime
import hashlib
import io
import json
import os

SCHEMA = 'claude-desktop-artifact-report/1.0'


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def custody(paths):
    """입력 파일 무결성 대장. 논문 재현성의 최소 조건."""
    rows = []
    for p in paths:
        try:
            st = os.stat(p)
            rows.append({
                'path': p,
                'size': st.st_size,
                'sha256': sha256(p),
                'mtime_utc': datetime.datetime.utcfromtimestamp(
                    st.st_mtime).strftime('%Y-%m-%dT%H:%M:%SZ'),
            })
        except OSError as e:
            rows.append({'path': p, 'size': None, 'sha256': None,
                         'error': str(e)})
    return rows


def _flat(v, limit=2000):
    if v is None:
        return ''
    if isinstance(v, (bytes, bytearray)):
        return v[:limit].hex()
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)[:limit]
    return str(v)[:limit]


def write_json(obj, path):
    with io.open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=_flat)
    return path


def write_csv(rows, path, fields=None):
    if not rows:
        rows = []
    if fields is None:
        seen = []
        for r in rows:
            for k in r:
                if k not in seen:
                    seen.append(k)
        fields = seen
    # utf-8-sig: 엑셀에서 한글이 깨지지 않게
    with io.open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow({k: _flat(r.get(k)) for k in fields})
    return path


_HTML = u"""<!doctype html><html lang="ko"><meta charset="utf-8">
<title>Claude Desktop 아티팩트 분석</title>
<style>
 body{font-family:"Malgun Gothic","Noto Sans KR",sans-serif;margin:32px;
      color:#1d2b36;word-break:keep-all;line-height:1.6}
 h1{font-size:20px;border-bottom:2px solid #1F3348;padding-bottom:8px}
 h2{font-size:15px;margin-top:28px;color:#1F3348}
 .wrap{overflow-x:auto}
 table{border-collapse:collapse;font-size:12px;min-width:640px}
 th{background:#1F3348;color:#fff;padding:6px 10px;text-align:left;
    white-space:nowrap}
 td{border-bottom:1px solid #dde3e9;padding:5px 10px;vertical-align:top}
 td.mono{font-family:Consolas,monospace;word-break:break-all}
 .tag{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px}
 .survived{background:#1F3348;color:#fff}
 .recovered{background:#EAF0F6;color:#1F3348}
 .meta{color:#667;font-size:12px}
</style>
<h1>Claude Desktop 아티팩트 분석 결과</h1>
<p class="meta">생성 %(now)s · 스키마 %(schema)s</p>
%(body)s
</html>"""


def write_html(sections, path):
    parts = []
    for title, rows, fields in sections:
        parts.append(u'<h2>%s <span class="meta">(%d건)</span></h2>' % (title, len(rows)))
        if not rows:
            parts.append(u'<p class="meta">해당 없음</p>')
            continue
        parts.append(u'<div class="wrap"><table><tr>' +
                     u''.join(u'<th>%s</th>' % f for f in fields) + u'</tr>')
        for r in rows[:400]:
            tds = []
            for f in fields:
                v = _flat(r.get(f), 300)
                if f == 'status' and v in ('SURVIVED', 'RECOVERED'):
                    v = u'<span class="tag %s">%s</span>' % (v.lower(), v)
                    tds.append(u'<td>%s</td>' % v)
                else:
                    cls = ' class="mono"' if f in ('offset', 'sha256', 'key') else ''
                    tds.append(u'<td%s>%s</td>' % (cls, _esc(v)))
            parts.append(u'<tr>' + u''.join(tds) + u'</tr>')
        parts.append(u'</table></div>')
        if len(rows) > 400:
            parts.append(u'<p class="meta">※ 상위 400건만 표시 — 전체는 CSV/JSON 참조</p>')
    html = _HTML % {'now': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
                    'schema': SCHEMA, 'body': u'\n'.join(parts)}
    with io.open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    return path


def _esc(s):
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
