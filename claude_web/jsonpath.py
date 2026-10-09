"""parsed_artifacts.json 안의 위치를 문자열로 만들고, 규칙의 필드 패턴으로 값을 찾는다.

경로 문자열 예: artifacts[3].records[2].rows[12].url
  - 배열 번호는 [n] (0부터)
  - 이름이 영문자/숫자/_ 로만 되어 있으면 .name, 아니면 ["name"]

필드 패턴 (rules.csv의 field, time_field 열):
  url                     레코드 바로 아래의 url
  value.tombstone         value 안의 tombstone
  value_json[*].eventName 배열의 모든 원소 안의 eventName  ([*] 또는 * = 아무 키/번호 하나)
  **.file_kind            깊이와 상관없이 이름이 file_kind인 모든 값  (** = 0단계 이상)
"""
import json
import re

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOKEN = re.compile(r"\[(\*|\d+)\]|([^.\[\]]+)")

MAX_VALUE_CHARS = 300   # 근거에 쓰는 값 하나의 최대 길이 (m4)


def path_str(parts):
    """['artifacts', 3, 'records', 2, 'url'] -> 'artifacts[3].records[2].url'"""
    out = ""
    for p in parts:
        if isinstance(p, int):
            out += "[%d]" % p
        elif _IDENT.match(p):
            out += ("." if out else "") + p
        else:
            out += "[%s]" % json.dumps(p, ensure_ascii=False)
    return out


def value_text(v):
    """비교와 표시에 쓰는 문자열 형태. 문자열은 그대로, 나머지는 JSON 표기."""
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def fmt_value(v, needle=None):
    """근거 열에 쓰는 값 표기. 문자열은 따옴표로 감싸고 JSON과 같은 방식으로 이스케이프한다.

    값이 300자를 넘으면 300자만 보여 주고 '…'와 '(총 N자)'를 붙인다.
    N은 원래 값의 글자 수다 (문자열은 따옴표·이스케이프를 빼고, 객체·배열은 JSON 표기 길이).
    찾은 문자열(needle)이 있으면 그 부분이 보이도록 앞뒤를 잘라 낸다.
      예) "https://claude.ai/login?returnTo=…(총 354자)   (잘린 값은 닫는 따옴표 없이 …(총 N자)로 끝난다)
    """
    raw = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    if len(raw) <= MAX_VALUE_CHARS:
        return json.dumps(v, ensure_ascii=False)
    total = len(raw)
    start = 0
    if needle:
        i = raw.find(needle)
        if i > MAX_VALUE_CHARS // 3:
            start = min(i - MAX_VALUE_CHARS // 3, total - MAX_VALUE_CHARS)
    piece = raw[start:start + MAX_VALUE_CHARS]
    head = "…" if start > 0 else ""
    tail = "…" if start + MAX_VALUE_CHARS < total else ""
    body = json.dumps(piece, ensure_ascii=False)[1:-1] if isinstance(v, str) else piece
    quote = '"' if isinstance(v, str) else ""
    return "%s%s%s%s(총 %d자)" % (quote, head, body, tail, total)


def parse_pattern(pattern):
    """'value_json[*].eventName' -> ['value_json', '*', 'eventName']"""
    segs = []
    for idx, name in _TOKEN.findall(pattern):
        if idx:
            segs.append("*" if idx == "*" else int(idx))
        else:
            segs.append(name)
    return segs


def _children(node):
    if isinstance(node, dict):
        return list(node.items())
    if isinstance(node, list):
        return list(enumerate(node))
    return []


def _walk(node, segs, path):
    if not segs:
        yield path, node
        return
    s, rest = segs[0], segs[1:]
    if s == "**":
        yield from _walk(node, rest, path)                 # 0단계
        for k, child in _children(node):                     # 1단계 이상
            yield from _walk(child, segs, path + [k])
    elif s == "*":
        for k, child in _children(node):
            yield from _walk(child, rest, path + [k])
    elif isinstance(node, dict) and s in node:
        yield from _walk(node[s], rest, path + [s])
    elif isinstance(node, list) and isinstance(s, int) and 0 <= s < len(node):
        yield from _walk(node[s], rest, path + [s])


def find(node, pattern):
    """패턴에 맞는 (경로 리스트, 값)을 차례로 돌려준다. 경로는 node 기준."""
    yield from _walk(node, parse_pattern(pattern), [])


def get_at(node, parts):
    for p in parts:
        node = node[p]
    return node
