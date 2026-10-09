"""JSON 파일 (Preferences, Local State)

records[0] = 파일 전체 내용
"""
import json

from parsers.common import ParseResult


def parse(unit):
    try:
        with open(unit.local_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return ParseResult(status="error", note="%s: %s" % (type(e).__name__, e))
    return ParseResult([data])
