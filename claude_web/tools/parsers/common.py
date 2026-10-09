"""파서가 공통으로 쓰는 것: 파싱 결과를 JSON에 넣을 수 있는 값으로 바꾼다."""
import datetime
import enum
import math

from timeutil import iso_utc

MAX_HEX_BYTES = 256     # 바이너리 값은 앞 256바이트만 16진수로 남긴다
MAX_DEPTH = 200


class ParseResult:
    """파서 하나의 결과. status: ok / partial / error"""

    def __init__(self, records=None, status="ok", note=""):
        self.records = records if records is not None else []
        self.status = status
        self.note = note


def bytes_text(b):
    b = bytes(b)
    s = "hex:" + b[:MAX_HEX_BYTES].hex()
    if len(b) > MAX_HEX_BYTES:
        s += "…(총 %d바이트)" % len(b)
    return s


def to_json(v, _depth=0, _seen=None):
    """파이썬 값 -> JSON 값.

    dict 키는 문자열로, bytes는 'hex:...'로, datetime은 UTC ISO 문자열로 바꾼다.
    ccl의 V8 특수 값(undefined 등)은 null 또는 문자열 표기로 바꾼다.
    """
    if _depth > MAX_DEPTH:
        return "<너무 깊은 구조>"
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes_text(v)
    if isinstance(v, datetime.datetime):
        return iso_utc(v)
    if isinstance(v, (datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, enum.Enum):
        return v.name
    if _seen is None:
        _seen = set()
    if isinstance(v, dict) or hasattr(v, "items") and callable(v.items):
        if id(v) in _seen:
            return "<순환 참조>"
        _seen.add(id(v))
        out = {str(k): to_json(x, _depth + 1, _seen) for k, x in v.items()}
        _seen.discard(id(v))
        return out
    if isinstance(v, (list, tuple, set, frozenset)):
        if id(v) in _seen:
            return "<순환 참조>"
        _seen.add(id(v))
        items = sorted(v, key=repr) if isinstance(v, (set, frozenset)) else v
        out = [to_json(x, _depth + 1, _seen) for x in items]
        _seen.discard(id(v))
        return out
    name = type(v).__name__
    if name in ("Undefined", "_Undefined", "Null", "_Null"):
        return None
    return "<%s> %s" % (name, v)
