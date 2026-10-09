"""Sessions (Session_*, Tabs_*) — ccl_chromium_reader 사용

records[k] = 탭 이동 기록 1건 (SNSS 명령 중 탭 이동만)
  offset, command, session_id, index, url, title, timestamp(UTC), transition,
  referrer_url, original_request_url, http_status
"""
import os

from parsers.common import ParseResult, to_json
from timeutil import iso_utc


def parse(unit):
    from ccl_chromium_reader import ccl_chromium_snss2 as snss
    name = os.path.basename(unit.local_path)
    if name.startswith("Session_"):
        ftype = snss.SnssFileType.Session
    elif name.startswith("Tabs_"):
        ftype = snss.SnssFileType.Tab
    else:
        return ParseResult(status="ok", note="SNSS 파일이 아님 (수집만 함)")
    records, other = [], 0
    try:
        with open(unit.local_path, "rb") as f:
            for cmd in snss.SnssFile(ftype, f).iter_session_commands():
                if not isinstance(cmd, snss.NavigationEntry):
                    other += 1
                    continue
                records.append({
                    "offset": cmd.offset,
                    "command": cmd.id_type.name,
                    "session_id": cmd.session_id,
                    "index": cmd.index,
                    "url": cmd.url,
                    "title": cmd.title,
                    "timestamp": iso_utc(cmd.timestamp) if cmd.timestamp else None,
                    "transition": str(cmd.transition_type) if cmd.transition_type is not None else None,
                    "referrer_url": cmd.referrer_url,
                    "original_request_url": cmd.original_request_url,
                    "http_status": to_json(cmd.http_status),
                })
    except Exception as e:
        status = "partial" if records else "error"
        return ParseResult(records, status, "%s: %s" % (type(e).__name__, e))
    return ParseResult(records, "ok", "탭 이동 외 명령 %d건은 넣지 않음" % other if other else "")
