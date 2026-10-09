"""SQLite (History, Cookies, Web Data)

records[t] = 테이블 하나 = {"table", "row_count", "rows": [...]}
rows[k]    = 행 하나 (열 이름 -> 값). 시각 열은 원래 값(WebKit 마이크로초 등) 그대로 둔다.
"""
import pathlib
import sqlite3

from parsers.common import ParseResult, to_json

ROW_LIMIT = 300000      # 표 하나에서 JSON에 넣는 최대 행 수 (넘으면 partial)
# rules.csv가 보는 표는 자르지 않는다 (잘리면 그 뒤의 행은 판정에서 빠지기 때문)
FULL_TABLES = {"urls", "downloads", "downloads_url_chains", "cookies"}


def parse(unit):
    try:
        # mode=rw: 파일이 없으면 새로 만들지 않고 오류를 낸다 (빈 DB를 정상으로 읽는 일을 막음)
        con = sqlite3.connect(pathlib.Path(unit.local_path).as_uri() + "?mode=rw", uri=True)
    except sqlite3.Error as e:
        return ParseResult(status="error", note="%s: %s" % (type(e).__name__, e))
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    records, notes, status = [], [], "ok"
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for t in tables:
            try:
                cur = con.execute('SELECT * FROM "%s"' % t.replace('"', '""'))
                cols = [d[0] for d in cur.description]
                rows = []
                for row in cur:
                    if len(rows) >= ROW_LIMIT and t not in FULL_TABLES:
                        status = "partial"
                        notes.append("%s: %d행까지만 저장" % (t, ROW_LIMIT))
                        break
                    rows.append({c: to_json(v) for c, v in zip(cols, row)})
                records.append({"table": t, "row_count": len(rows), "rows": rows})
            except sqlite3.Error as e:
                status = "partial"
                notes.append("%s: %s" % (t, e))
                records.append({"table": t, "row_count": 0, "rows": [], "error": str(e)})
    except sqlite3.Error as e:
        return ParseResult(records, "error", "%s: %s" % (type(e).__name__, e))
    finally:
        con.close()
    return ParseResult(records, status, "; ".join(notes))
