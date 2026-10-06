import sys
import os
import csv
import json
import time
import base64
import platform
import socket
import getpass
import html
import hashlib
import sqlite3
import pathlib
import ntpath
import zipfile
import tempfile

VERSION = "v1.3.0"
TARGET_ENV = "Claude Web (Chrome)"

# 팀이 정한 출력 범위 (행위 1~13)
ACTIONS = [
    "사이트 접속", "로그인", "로그아웃", "다른 계정으로 재로그인",
    "새 대화에서 프롬프트 전송", "기존 대화에서 프롬프트 전송", "프롬프트 수정하여 답변 재생성",
    "메모리 기능 사용", "파일 업로드", "파일 다운로드", "개별 대화 삭제",
    "incognito chat 사용", "커넥터 연결",
]

IDB = "IndexedDB/https_claude.ai_0.indexeddb.leveldb"   # claude.ai의 IndexedDB 폴더
LS = "Local Storage/leveldb"                             # 모든 사이트가 같이 쓰는 Local Storage 폴더

# 수집할 아티팩트 정의: (이름, Default 폴더 기준 경로, 파서). 경로가 폴더면 폴더 안 파일 전부
DEFINITIONS = [
    ("History (SQLite)", "History", "SQLite parser"),
    ("Cookies (SQLite)", "Network/Cookies", "SQLite parser"),
    ("Local Storage (LevelDB)", LS, "byte marker scanner"),
    ("IndexedDB (LevelDB)", IDB, "byte marker scanner"),
]

# 규칙: (행위, 아티팩트 이름, Default 폴더 기준 상대 경로, 찾을 문자열, 등급)
# 등급: 직접 = 이 문자열만으로 행위에 귀속 / 보조 = 다른 아티팩트와 함께 봐야 추정 가능
# 근거와 확인 상태는 rules_support.md 참고
RULES = [
    ("사이트 접속", "History (https://claude.ai/)", "History", b"https://claude.ai/", "직접"),
    ("사이트 접속", "Cookies (anthropic-device-id)", "Network/Cookies", b"anthropic-device-id", "보조"),
    ("로그인", "Cookies (sessionKey)", "Network/Cookies", b"sessionKey", "직접"),
    ("로그인", "Cookies (lastActiveOrg)", "Network/Cookies", b"lastActiveOrg", "보조"),
    ("로그인", "History (claude.ai/new)", "History", b"https://claude.ai/new", "보조"),
    ("로그인", "Local Storage (lastLoginMethod)", LS, b"lastLoginMethod", "보조"),
    ("로그아웃", "History (claude.ai/logout)", "History", b"claude.ai/logout", "직접"),
    ("로그아웃", "Local Storage (-logout-at)", LS, b"-logout-at", "보조"),
    ("새 대화에서 프롬프트 전송", "History (claude.ai/chat/)", "History", b"https://claude.ai/chat/", "보조"),
    ("새 대화에서 프롬프트 전송", "Local Storage (claude-conversation-ledger)", LS, b"claude-conversation-ledger", "보조"),
    ("새 대화에서 프롬프트 전송", "IndexedDB (첫 메시지의 parent UUID)", IDB, b"00000000-0000-4000-8000-000000000000", "보조"),
    ("프롬프트 수정하여 답변 재생성", "Local Storage (claudeai.message.edit_started)", LS, b"claudeai.message.edit_started", "보조"),
    ("메모리 기능 사용", "IndexedDB (memory_write)", IDB, b"memory_write", "직접"),
    # V8 직렬화: 키 file_kind 뒤에 문자열 표시(") + 길이(0x08) + document  (09 검증 HxD 캡처와 같은 바이트)
    ("파일 업로드", "IndexedDB (file_kind=document)", IDB, b'file_kind"\x08document', "직접"),
    ("개별 대화 삭제", "IndexedDB (tombstone)", IDB, b"tombstone", "직접"),
    ("incognito chat 사용", "History (claude.ai/new?incognito)", "History", b"claude.ai/new?incognito", "직접"),
    ("커넥터 연결", "History (api/mcp/auth_callback)", "History", b"claude.ai/api/mcp/auth_callback", "직접"),
    ("커넥터 연결", "History (customize/connectors)", "History", b"claude.ai/customize/connectors", "보조"),
    ("커넥터 연결", "History (gmailmcp)", "History", b"gmailmcp", "보조"),
]

# 파일 다운로드 판정용 SQL: claude.ai 탭/리퍼러에서 시작했거나, 다운로드 URL 목록에 wiggle/download-file이 있는 행
DOWNLOAD_SQL = """
SELECT id, target_path, tab_url, referrer FROM downloads
WHERE tab_url LIKE '%claude.ai%'
   OR referrer LIKE '%claude.ai%'
   OR id IN (SELECT id FROM downloads_url_chains WHERE url LIKE '%wiggle/download-file%')
"""

ROW_LIMIT = 10000      # JSON에 넣는 표 하나당 최대 행 수


def find_default_folders(source):
    """입력 종류에 따라 검사할 Chrome Default 폴더 목록을 만든다."""
    if source.lower() == "live":
        users_root = r"C:\Users"
        folders = []
        if os.path.isdir(users_root):
            for user in os.listdir(users_root):
                d = os.path.join(users_root, user, "AppData", "Local",
                                 "Google", "Chrome", "User Data", "Default")
                if os.path.isdir(d):
                    folders.append((user, d))
        return "Live system", folders
    if source.lower().endswith(".e01"):
        return "E01", None          # 아직 미구현
    return "Folder (Default)", [("-", source)]   # 시험용: Default 폴더 직접 입력


def list_files(path):
    """파일이면 그 파일과 SQLite 짝 파일(-journal/-wal/-shm), 폴더면 폴더 안의 모든 파일."""
    if os.path.isfile(path):
        return [path] + [path + s for s in ("-journal", "-wal", "-shm") if os.path.exists(path + s)]
    files = []
    for root, dirs, names in os.walk(path):
        for name in sorted(names):
            files.append(os.path.join(root, name))
    return files


def read_file(path):
    """파일을 바이트로 읽는다. 못 읽으면(잠긴 파일 등) None."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def archive_name(path, default, user):
    """zip 안의 경로: Default 폴더부터 시작 (live에서 사용자가 여럿이면 앞에 사용자 이름)."""
    rel = os.path.relpath(path, os.path.dirname(os.path.normpath(default))).replace("\\", "/")
    return rel if user == "-" else f"{user}/{rel}"


def to_json_value(v):
    """SQLite 값을 JSON에 넣을 수 있게 바꾼다(바이트는 base64)."""
    if isinstance(v, bytes):
        return {"type": "bytes", "base64": base64.b64encode(v).decode("ascii")}
    return v


def open_readonly(db_path):
    """SQLite 파일을 읽기 전용으로 연다(immutable=1: 저널을 건드리지 않아 복사본이 바뀌지 않음)."""
    uri = pathlib.Path(os.path.abspath(db_path)).as_uri() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def parse_sqlite(db_path):
    """SQLite 복사본의 모든 표를 [{name, columns, rows, truncated}] 형태로 돌려준다."""
    con = open_readonly(db_path)
    tables = []
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for name in names:
            cur = con.execute(f'SELECT * FROM "{name}" LIMIT {ROW_LIMIT + 1}')
            rows = [[to_json_value(v) for v in row] for row in cur]
            tables.append({"name": name, "columns": [d[0] for d in cur.description],
                           "rows": rows[:ROW_LIMIT], "truncated": len(rows) > ROW_LIMIT})
    finally:
        con.close()
    return tables


def check_downloads(db_path):
    """History 복사본의 downloads 표에서 claude.ai 다운로드 행을 찾아 (target_path, 예상 확장자) 목록을 돌려준다."""
    con = open_readonly(db_path)
    try:
        rows = con.execute(DOWNLOAD_SQL).fetchall()
    finally:
        con.close()
    found = []
    for _id, target_path, tab_url, referrer in rows:
        ext = ntpath.splitext(ntpath.basename(target_path or ""))[1]   # 윈도우 경로 기준 마지막 확장자
        found.append((target_path, ext or "(확장자 없음)"))
    return found


def main():
    start = time.time()
    if len(sys.argv) != 3:
        print("Usage: python main.py <E01 | live | Default폴더> <저장 경로>")
        return
    source, out_dir = sys.argv[1], sys.argv[2]

    print(f"LLM Artifact Analyzer {VERSION}\nAuthor: jbsquad\nContact: jbsquad000@gmail.com\n")
    print(f'Command line:\n  python {os.path.basename(sys.argv[0])} "{source}" "{out_dir}"\n')
    print("System info:")
    print(f"  Machine name: {socket.gethostname()}")
    print(f"  64-bit: {platform.machine().endswith('64')}")
    print(f"  User: {getpass.getuser()}")
    print(f"  OS: {platform.system()} {platform.release()}\n")

    src_type, folders = find_default_folders(source)
    print(f"Source info:\n  Type: {src_type}\n  Source: {source}\n")
    print(f"Output info:\n  Path: {out_dir}\n")
    print(f"Target:\n  {TARGET_ENV}\n")

    if folders is None:
        print("E01 input is not implemented yet.")
        return
    if not folders:
        print("No Chrome Default folder found. (Live: is Chrome installed?)")
        return
    if src_type == "Live system":
        print("NOTE: Close Chrome before running. Locked files cannot be read.\n")
    os.makedirs(out_dir, exist_ok=True)

    # ---------- 1. 수집: 정의된 아티팩트를 읽어 zip에 원래 구조대로 저장 ----------
    print(f"Loading artifact definitions...\n  Found {len(DEFINITIONS)} artifact definitions.\n")
    print("Artifact collection:")
    zip_path = os.path.join(out_dir, "LLM_Artifacts.zip")
    data = {}             # 원본 경로 -> 읽은 바이트 (분석은 zip에 넣은 것과 같은 바이트로 한다)
    entries = []          # JSON에 들어갈 아티팩트 항목
    found = extracted = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (name, rel, parser) in enumerate(DEFINITIONS, 1):
            count = 0
            for user, default in folders:
                path = os.path.normpath(os.path.join(default, rel))
                if not os.path.exists(path):
                    continue
                members = []
                for f in list_files(path):
                    count += 1
                    b = read_file(f)
                    arc = archive_name(f, default, user)
                    if b is None:
                        members.append({"collected_path": arc, "status": "failed"})
                        continue
                    info = zipfile.ZipInfo(arc, time.localtime(os.path.getmtime(f))[:6])   # 원본 수정 시각 유지
                    info.compress_type = zipfile.ZIP_DEFLATED
                    z.writestr(info, b)
                    data[f] = b
                    extracted += 1
                    members.append({"collected_path": arc, "source_path": f, "size": len(b),
                                    "sha256": hashlib.sha256(b).hexdigest(), "status": "ok"})
                entries.append({"artifact": name, "parser": parser, "user": user, "default": default,
                                "rel": rel, "source_path": path, "members": members})
            found += count
            print(f"  [{i}/{len(DEFINITIONS)}] {name}\n        Found: {count} files\n")
    failed = found - extracted
    print("Artifact extraction completed.")
    print(f"  Artifact definitions : {len(DEFINITIONS)}")
    print(f"  Files found          : {found}")
    print(f"  Files extracted      : {extracted}")
    print(f"  Extraction failures  : {failed}\n")

    # ---------- 2. 분석: 규칙의 문자열 검색 + History 다운로드 판정 ----------
    results = []          # 검출된 행: (행위, 아티팩트, 경로, 등급)
    downloads = []        # (target_path, 예상 확장자)
    parse_errors = 0
    for user, default in folders:
        for action, artifact, rel, pattern, grade in RULES:
            path = os.path.normpath(os.path.join(default, rel))
            for f in (list_files(path) if os.path.exists(path) else []):
                if f in data and pattern in data[f]:
                    results.append((action, artifact, f, grade))

    # SQLite는 수집한 바이트를 임시 폴더에 써서 연다 (원본·zip은 건드리지 않음)
    with tempfile.TemporaryDirectory() as tmp:
        for k, e in enumerate(entries):
            markers = []          # 파일별로 어떤 규칙 문자열이 나왔는지
            for m in e["members"]:
                if m["status"] != "ok":
                    continue
                hit = [p.decode("latin-1") for (_, _, r, p, _) in RULES
                       if r == e["rel"] and p in data[m["source_path"]]]
                if hit:
                    markers.append({"collected_path": m["collected_path"], "matched_markers": hit})
            e["markers"] = markers
            e["records"] = markers if e["parser"] == "byte marker scanner" else []
            e["status"] = "success" if all(m["status"] == "ok" for m in e["members"]) else "partial"
            if e["parser"] != "SQLite parser" or not e["members"] or e["members"][0]["status"] != "ok":
                continue
            work = os.path.join(tmp, str(k))
            os.makedirs(work)
            for m in e["members"]:
                if m["status"] == "ok":
                    with open(os.path.join(work, os.path.basename(m["source_path"])), "wb") as out:
                        out.write(data[m["source_path"]])
            db_copy = os.path.join(work, os.path.basename(e["source_path"]))
            try:
                e["records"] = parse_sqlite(db_copy)
                if e["rel"] == "History":
                    for target_path, ext in check_downloads(db_copy):
                        downloads.append((target_path, ext))
                        results.append(("파일 다운로드", f"History downloads (예상 확장자 {ext})",
                                        e["source_path"], "직접"))   # 팀 명세는 .py
            except sqlite3.Error as err:
                parse_errors += 1
                e["status"] = "failed"
                e["error"] = str(err)

    print("Analyzing user actions...\n")
    supported = set(r[0] for r in RULES) | {"파일 다운로드"}
    for n, action in enumerate(ACTIONS, 1):
        rows = [r for r in results if r[0] == action]
        print(f"  [{n}] {action}")
        if action not in supported:
            print("      Not supported\n")
            continue
        print(f"      Direct evidence     : {sum(1 for r in rows if r[3] == '직접')}")
        print(f"      Supporting evidence: {sum(1 for r in rows if r[3] == '보조')}")
        if action == "파일 다운로드":
            for target_path, ext in downloads:
                print(f"      Expected extension : {ext} ({target_path})")
        print()
    direct = sum(1 for r in results if r[3] == "직접")
    detected = set(r[0] for r in results)
    print("User action analysis completed.")
    print(f"  Actions detected   : {len(detected)}")
    print(f"  Direct evidence    : {direct}")
    print(f"  Supporting evidence: {len(results) - direct}\n")

    # ---------- 3. 출력: CSV, HTML, JSON ----------
    csv_path = os.path.join(out_dir, "LLM_Artifact_Report.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as out:
        writer = csv.writer(out)
        writer.writerow(["행위 분류", "아티팩트", "경로", "아티팩트 등급"])
        writer.writerows(results)
    print(f"Generating CSV report...\n  {csv_path}\n  Status: OK\n")

    html_path = os.path.join(out_dir, "LLM_Artifact_Report.html")
    write_html(html_path, src_type, source, results, supported)
    print(f"Generating HTML report...\n  {html_path}\n  Status: OK\n")

    print(f"Saving collected artifacts...\n  {zip_path}\n  Status: OK\n")

    json_path = os.path.join(out_dir, "parsed_artifacts.json")
    write_json(json_path, src_type, source, entries)
    print(f"Generating parsed JSON...\n  {json_path}\n  Status: OK\n")

    print("Execution summary:")
    print(f"  Target              : {TARGET_ENV}")
    print(f"  Artifact definitions : {len(DEFINITIONS)}")
    print(f"  Files found          : {found}")
    print(f"  Files extracted      : {extracted}")
    print(f"  Actions detected     : {len(detected)}")
    print(f"  Failed               : {failed + parse_errors}")
    print(f"\nTotal execution time: {time.time() - start:.2f} sec")


def write_json(path, src_type, source, entries):
    """팀 공통 스키마(1.1) 형태의 파싱 결과 JSON."""
    artifacts = []
    for e in entries:
        ok = [m for m in e["members"] if m["status"] == "ok"]
        rule_ids = [f"{a} / {n}" for a, n, r, p, g in RULES if r == e["rel"]]
        if e["rel"] == "History":
            rule_ids.append("파일 다운로드 / History downloads")
        artifacts.append({
            "artifact": e["artifact"],
            "artifact_names": [e["artifact"]],
            "definition_ids": rule_ids,
            "user": e["user"],
            "source_path": e["source_path"],
            "collected_path": e["members"][0]["collected_path"] if e["members"] else None,
            "collected_members": [m["collected_path"] for m in ok],
            "collection_status": "ok" if len(ok) == len(e["members"]) else "partial",
            "parser": e["parser"],
            "collection_method": "live_copy" if src_type == "Live system" else "folder_copy",
            "status": e["status"],
            "raw_timestamp": {"source_mtime_ns": os.stat(e["source_path"]).st_mtime_ns},
            "normalized_timestamp": None,
            "records": e["records"],
            "parse_details": {"files": e["members"], "file_marker_matches": e["markers"],
                              "error": e.get("error")},
        })
    doc = {"schema_version": "1.1",
           "source": {"mode": "live" if src_type == "Live system" else "folder", "path": source},
           "collection_manifest": "LLM_Artifacts.zip",
           "artifacts": artifacts}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)


def write_html(path, src_type, source, results, supported):
    """제목 + 행위별 토글(<details>) 안에 아티팩트 표. 팀 공통 HTML 형식."""
    e = html.escape
    style = """
:root { color-scheme: light; --ink: #172033; --muted: #64748b; --line: #dbe3ef; --panel: #fff; --accent: #3157d5; }
* { box-sizing: border-box; }
body { max-width: 1440px; margin: 0 auto; padding: 32px; font: 14px/1.5 Inter, "Segoe UI", Arial, sans-serif; color: var(--ink); background: #f5f7fb; }
h1 { margin: 8px 0 26px; font-size: 30px; line-height: 1.25; }
main { display: grid; gap: 16px; }
details { min-width: 0; overflow: hidden; background: var(--panel); border: 1px solid var(--line); border-radius: 14px; box-shadow: 0 3px 14px #1e293b0a; }
summary { display: flex; align-items: center; gap: 12px; padding: 18px 22px; cursor: pointer; font-size: 17px; font-weight: 600; list-style: none; }
summary::-webkit-details-marker { display: none; }
summary::before { content: "\\203A"; display: inline-block; color: var(--accent); font-size: 25px; font-weight: 400; line-height: .7; transition: transform .15s ease; }
details[open] summary { border-bottom: 1px solid var(--line); }
details[open] summary::before { transform: rotate(90deg); }
details > p { margin: 0; padding: 20px 22px; color: var(--muted); }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 11px 14px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { background: #f8fafc; color: var(--muted); font-weight: 600; }
td { overflow-wrap: anywhere; }
td:first-child { font-weight: 600; }
td:nth-child(2) { font-family: Consolas, "Courier New", monospace; font-size: 12px; }
td:last-child { white-space: nowrap; font-weight: 600; }
tbody tr:last-child td { border-bottom: 0; }
"""
    parts = ["<!doctype html>", "<html lang=\"ko\">", "<head>", "<meta charset=\"utf-8\">",
             "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
             f"<title>{e(TARGET_ENV)}</title>", f"<style>{style}</style>", "</head>", "<body>",
             f"<h1>{e(TARGET_ENV)}</h1>", "<main>"]
    for action in ACTIONS:
        rows = [r for r in results if r[0] == action]
        parts.append(f"<details>\n<summary>{e(action)}</summary>")
        if action not in supported:
            parts.append("<p>미지원 (사유: rules_support.md)</p>")
        elif not rows:
            parts.append("<p>해당 아티팩트 없음</p>")
        else:
            parts.append("<div class=\"table-wrap\"><table>")
            parts.append("<thead><tr><th>아티팩트</th><th>경로</th><th>아티팩트 등급</th></tr></thead>\n<tbody>")
            for a, artifact, p, grade in rows:
                parts.append(f"<tr><td>{e(artifact)}</td><td>{e(p)}</td><td>{e(grade)}</td></tr>")
            parts.append("</tbody>\n</table></div>")
        parts.append("</details>")
    parts.append("<details>\n<summary>비고</summary>")
    parts.append(f"<p>입력 종류: {e(src_type)}<br>입력: {e(source)}<br>"
                 "검출 = 흔적 발견(행위 확정 아님). 해당 아티팩트 없음 = 흔적 없음(행위 안 함 아님).</p>")
    parts.append("</details>\n</main>\n</body>\n</html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


if __name__ == "__main__":
    main()
