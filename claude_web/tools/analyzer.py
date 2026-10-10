"""rules.csv의 규칙을 parsed_artifacts.json 레코드에 적용해 보고서 행을 만든다.

CSV 한 행 = 규칙 1개가 아티팩트 1개에서 맞은 것 (D6)
  시각 : 맞은 레코드들의 시각 중 가장 이른 것 (KST)
  근거 : 레코드 단위로, 레코드 안에서 맞은 key=value와 그 시각 값을 같이 쓴다
         ① 시각 열 값을 만든 레코드를 맨 앞, 나머지는 JSON 순서
         ② 최대 3개 레코드, 나머지는 "외 N건"

rules.csv 열
  filter      레코드를 고르는 조건. ';'로 여러 개. table=urls 는 SQLite 표 이름
              연산자: = (같음)  ^= (로 시작)  $= (로 끝남)  ~= (포함)
                      @= (호스트가 같거나 그 하위 도메인. URL이면 호스트를 꺼내고, .claude.ai 같은 쿠키 도메인도 됨)
  field       값을 찾을 위치 (jsonpath.py의 패턴 설명 참고)
  op          equals / contains / startswith / endswith / custom
  time_field  시각으로 쓸 키 이름. '|'로 여러 개면 앞의 것부터.
              맞은 값과 같은 단계에서 먼저 찾고, 없으면 바깥 단계로 올라가며 찾는다.
"""
import csv
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from definitions import ACTIONS, ACTION_NAME, ARTIFACT_DEFS
from jsonpath import find, fmt_value, get_at, path_str, value_text
from timeutil import format_kst, parse_time

MAX_RECORDS = 3
CELL_LIMIT = 32000          # Excel 셀 최대 길이(32,767자)보다 조금 작게

_FILTER = re.compile(r"^\s*([^=^$~@]+?)\s*(\^=|\$=|~=|@=|=)(.*)$")
OPS = ("equals", "contains", "startswith", "endswith", "custom")
GRADES = ("직접", "보조")


class RuleError(Exception):
    """rules.csv에 고칠 곳이 있을 때. 메시지에 문제가 된 행을 모두 적는다."""


@dataclass
class Rule:
    rule_id: str
    action_id: str
    artifact: str
    filters: list
    table: str
    field: str
    op: str
    value: str
    time_fields: list
    grade: str
    note: str


@dataclass
class Row:
    time: object            # UTC datetime 또는 None
    has_fraction: bool
    action_id: str
    artifact: str
    path: str
    grade: str
    evidence: str
    rule_id: str = ""
    records: int = 0
    parts: list = None      # 근거를 key=value 단위로 나눈 것
    unit_index: int = -1    # 이 행을 만든 아티팩트의 parsed_artifacts.json 번호
    rule_artifact: str = "" # 규칙의 아티팩트 이름 (History, Cookies, Local Storage, IndexedDB ...)
    key_field: str = ""     # HTML의 읽기 쉬운 줄: 시각 열을 만든 레코드에서 규칙에 맞은 대표 필드 이름
    key_value: object = None  #                    그 값 (시각 값이 아니라 규칙에 맞은 값)
    key_needle: str = None    #                    contains 규칙이면 찾은 문자열 (긴 값을 자를 때 보이게)

    @property
    def action_name(self):
        return ACTION_NAME[self.action_id]

    @property
    def time_text(self):
        return format_kst(self.time, self.has_fraction) if self.time else "-"


def load_rules(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except UnicodeDecodeError:                      # Excel이 ANSI(cp949)로 저장한 경우
        with open(path, encoding="cp949") as f:
            rows = list(csv.DictReader(f))
    rules, problems = [], []
    action_ids = {a[0] for a in ACTIONS}
    artifact_names = {a.name for a in ARTIFACT_DEFS}
    for line_no, r in enumerate(rows, 2):
        if not (r.get("rule_id") or "").strip():
            continue
        where = "rules.csv %d행 (%s)" % (line_no, r["rule_id"].strip())
        filters, table = [], ""
        for part in (r.get("filter") or "").split(";"):
            if not part.strip():
                continue
            m = _FILTER.match(part)
            if not m:
                problems.append("%s: filter를 해석할 수 없음: %r" % (where, part))
                continue
            key, op, val = m.group(1).strip(), m.group(2), m.group(3).strip()
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                problems.append("%s: filter의 필드 이름이나 연산자가 잘못됨: %r" % (where, part))
                continue
            if key == "table" and op == "=":
                table = val
            else:
                filters.append((key, op, val))
        op = (r.get("op") or "").strip()
        checks = [
            ((r.get("action_id") or "").strip() in action_ids, "action_id가 A01~A14가 아님"),
            ((r.get("artifact") or "").strip() in artifact_names, "artifact가 정의에 없음 (%s)" % ", ".join(sorted(artifact_names))),
            (op in OPS, "op는 %s 중 하나" % "/".join(OPS)),
            ((r.get("grade") or "").strip() in GRADES, "grade는 직접/보조"),
            (op != "custom" or (r.get("value") or "").strip() in CUSTOM, "custom 함수 이름이 analyzer.py에 없음"),
            (op == "custom" or (r.get("field") or "").strip(), "field가 비어 있음"),
        ]
        bad = [msg for ok, msg in checks if not ok]
        if bad:
            problems.append("%s: %s" % (where, "; ".join(bad)))
            continue
        rules.append(Rule(
            rule_id=r["rule_id"].strip(), action_id=r["action_id"].strip(), artifact=r["artifact"].strip(),
            filters=filters, table=table, field=(r.get("field") or "").strip(), op=r["op"].strip(),
            value=r.get("value") or "", time_fields=[t.strip() for t in (r.get("time_field") or "").split("|") if t.strip()],
            grade=r["grade"].strip(), note=r.get("note") or ""))
    if problems:
        raise RuleError("\n".join(problems))
    return rules


# ---------------------------------------------------------------- 비교

def _compare(v, op, want):
    s = value_text(v)
    if op == "equals":
        return s == want
    if op == "contains":
        return want in s
    if op == "startswith":
        return s.startswith(want)
    if op == "endswith":
        return s.endswith(want)
    raise ValueError("알 수 없는 op: %s" % op)


_FILTER_OPS = {"=": "equals", "^=": "startswith", "$=": "endswith", "~=": "contains"}


def host_matches(value, want):
    """value(URL 또는 '.claude.ai' 같은 도메인)의 호스트가 want이거나 그 하위 도메인인지"""
    s = str(value or "").strip()
    host = (urlsplit(s).hostname or "") if "://" in s else s.lstrip(".")
    host = host.lower()
    want = want.lower()
    return host == want or host.endswith("." + want)


def _passes(rec, filters):
    for key, op, val in filters:
        if not isinstance(rec, dict) or key not in rec or rec[key] is None:
            return False
        if op == "@=":
            if not host_matches(rec[key], val):
                return False
        elif not _compare(rec[key], _FILTER_OPS[op], val):
            return False
    return True


def _nearest_time(rec, path, names):
    """맞은 값과 같은 단계부터 바깥으로 올라가며 시각 키를 찾는다 -> (경로, 값, (dt, frac))"""
    for level in range(len(path) - 1, -1, -1):
        node = get_at(rec, path[:level])
        if not isinstance(node, dict):
            continue
        for name in names:
            if name in node:
                t = parse_time(node[name])
                if t:
                    return path[:level] + [name], node[name], t
    return None


# ---------------------------------------------------------------- 레코드 단위 매칭 결과

@dataclass
class Hit:
    """레코드 하나에서 맞은 것"""
    prefix: list                                  # 레코드의 JSON 경로 (artifacts[i].records[j]...)
    pairs: list = field(default_factory=list)     # [(경로 문자열, 값, 강조 문자열)]
    time: tuple = None                            # (dt, frac) 중 가장 이른 것
    primary: tuple = None                         # (필드 이름, 값, 강조 문자열) 대표로 보여 줄 맞은 값

    def add(self, parts, value, needle=None):
        key = path_str(self.prefix + parts)
        if all(p[0] != key for p in self.pairs):
            self.pairs.append((key, value, needle))

    def set_primary(self, parts, value, needle=None):
        """규칙에 맞은 값 중 처음 것을 대표로 둔다. 필드 이름은 경로의 마지막 이름(배열 번호 제외)."""
        if self.primary is None:
            names = [p for p in parts if isinstance(p, str)]
            self.primary = (names[-1] if names else path_str(parts), value, needle)

    def add_time(self, t):
        if t and (self.time is None or t[0] < self.time[0]):
            self.time = t

    def texts(self):
        return ["%s=%s" % (k, fmt_value(v, n)) for k, v, n in self.pairs]


def _iter_records(idx, unit, table):
    if unit.parser == "sqlite":
        for t_idx, t in enumerate(unit.records):
            if table and t.get("table") != table:
                continue
            for r_idx, row in enumerate(t.get("rows", [])):
                yield ["artifacts", idx, "records", t_idx, "rows", r_idx], row
    else:
        for r_idx, rec in enumerate(unit.records):
            yield ["artifacts", idx, "records", r_idx], rec


def _match_rule(rule, idx, unit):
    hits = []
    needle = rule.value if rule.op == "contains" else None
    for prefix, rec in _iter_records(idx, unit, rule.table):
        if not _passes(rec, rule.filters):
            continue
        hit = Hit(prefix)
        for path, val in find(rec, rule.field):
            if not _compare(val, rule.op, rule.value):
                continue
            hit.add(path, val, needle)
            hit.set_primary(path, val, needle)
            tm = _nearest_time(rec, path, rule.time_fields)
            if tm:
                hit.add(tm[0], tm[1])
                hit.add_time(tm[2])
        if hit.pairs:
            hits.append(hit)
    return hits


# ---------------------------------------------------------------- 표로 못 쓰는 규칙

def _table(unit, name):
    for t_idx, t in enumerate(unit.records):
        if t.get("table") == name:
            return t_idx, t.get("rows", [])
    return None, []


def downloads_from_claude(rule, idx, unit):
    """A10: claude.ai 탭/리퍼러에서 시작했거나, 다운로드 URL 목록에 wiggle/download-file이 있는 다운로드"""
    d_idx, downloads = _table(unit, "downloads")
    c_idx, chains = _table(unit, "downloads_url_chains")
    chain_rows = {}
    for k, row in enumerate(chains):
        if "wiggle/download-file" in str(row.get("url") or ""):
            chain_rows.setdefault(row.get("id"), []).append(k)
    hits = []
    for k, row in enumerate(downloads):
        prefix = ["artifacts", idx, "records", d_idx, "rows", k]
        hit = Hit(prefix)
        for col in ("tab_url", "referrer"):
            if host_matches(row.get(col), "claude.ai"):
                hit.add([col], row[col], "claude.ai")
        via_chain = chain_rows.get(row.get("id"), [])
        if not hit.pairs and not via_chain:
            continue
        hit.add(["target_path"], row.get("target_path"))
        hit.set_primary(["target_path"], row.get("target_path"))      # 내려받은 파일 경로를 대표로
        tm = _nearest_time(row, ["target_path"], rule.time_fields)
        if tm:
            hit.add(tm[0], tm[1])
            hit.add_time(tm[2])
        hits.append(hit)
        for ck in via_chain:
            ch = Hit(["artifacts", idx, "records", c_idx, "rows", ck])
            ch.add(["url"], chains[ck]["url"], "wiggle/download-file")
            ch.set_primary(["url"], chains[ck]["url"], "wiggle/download-file")
            hits.append(ch)
    return hits


CUSTOM = {"downloads_from_claude": downloads_from_claude}


# ---------------------------------------------------------------- 행 만들기

def _make_row(rule, unit, hits):
    hits = sorted(hits, key=lambda h: h.prefix)                       # JSON 순서
    timed = [h for h in hits if h.time]
    first = min(timed, key=lambda h: h.time[0]) if timed else None   # 동률이면 JSON 순서상 앞의 것
    ordered = ([first] if first else []) + [h for h in hits if h is not first]
    parts = [t for h in ordered[:MAX_RECORDS] for t in h.texts()]
    if len(ordered) > MAX_RECORDS:
        parts.append("외 %d건" % (len(ordered) - MAX_RECORDS))
    text = "; ".join(parts)
    if len(text) > CELL_LIMIT:
        text = text[:CELL_LIMIT] + "…(셀 길이 제한으로 생략)"
        parts = [text]
    key = ordered[0].primary or ("", None, None)                       # 시각 열을 만든(맨 앞) 레코드의 대표 값
    return Row(time=first.time[0] if first else None, has_fraction=first.time[1] if first else False,
               action_id=rule.action_id, artifact=unit.name, path=unit.evidence_path,
               grade=rule.grade, evidence=text, rule_id=rule.rule_id, records=len(hits), parts=parts,
               rule_artifact=rule.artifact, key_field=key[0], key_value=key[1], key_needle=key[2])


# ---------------------------------------------------------------- 행위별 상태

STATE_KEYS = ("path_missing", "read_failed", "no_match")
NOTE_LIMIT = 150


def _short(text):
    text = " ".join(str(text or "").split())
    return text if len(text) <= NOTE_LIMIT else text[:NOTE_LIMIT] + "…"


def _scopes(adef, units, collection):
    """이 아티팩트를 찾아본 범위: [(사용자, 프로필 이름)] (Local State처럼 프로필 밖이면 프로필 이름은 "")"""
    if collection is not None:
        if adef.base == "user_data":
            return [(u, "") for u, _ in collection.users]
        return [(u, p) for u, p, _ in collection.profiles]
    seen = []                                           # 시험용: 수집 결과 없이 units만 받은 경우
    for u in units:
        key = (u.user, u.profile if adef.base == "profile" else "")
        if key not in seen:
            seen.append(key)
    return seen


def _label(name, user, profile):
    return "%s (%s)" % (name, "%s\\%s" % (user, profile) if profile else user)


def artifact_states(names, units, matched_units, collection=None):
    """규칙이 보는 아티팩트를 사용자·프로필마다 세 가지 중 하나로 나눈다.

      path_missing : 경로 없음          (그 프로필에 아티팩트 경로가 없음)
      read_failed  : 있지만 읽기 실패   (복사·파싱 실패, 일부만 읽음, 읽다가 오류가 나 들어가 보지 못한 곳)
      no_match     : 읽었지만 매칭 없음
    matched_units에 든 아티팩트(이 행위의 행을 만든 것)는 표에 나오므로 넣지 않는다.
    단, 일부만 읽은 아티팩트는 행이 있어도 read_failed에 넣는다 (못 읽은 부분이 있으므로).
    """
    out = {k: [] for k in STATE_KEYS}
    defs = {a.name: a for a in ARTIFACT_DEFS}
    for name in names:
        scopes = _scopes(defs[name], units, collection)
        if not scopes:
            out["path_missing"].append("%s (Chrome 프로필 없음)" % name)
        for user, profile in scopes:
            us = [u for u in units if u.definition == name and u.user == user
                  and (u.profile == profile or defs[name].base == "user_data")]
            label = _label(name, user, profile)
            if not us:
                out["path_missing"].append(label)
                continue
            for u in us:
                if u.collection_status == "error":
                    out["read_failed"].append("%s 수집 실패: %s" % (label, _short(u.collection_note)))
                elif u.parse_status not in ("ok", "partial"):
                    out["read_failed"].append("%s 파싱 실패: %s" % (label, _short(u.parse_note)))
                elif u.parse_status == "partial":
                    out["read_failed"].append("%s 일부만 읽음: %s" % (label, _short(u.parse_note)))
                elif id(u) not in matched_units:
                    out["no_match"].append(label)
    if collection is not None:
        for sk in collection.skipped:
            if sk.definition is None or sk.definition in names:
                line = "%s 탐색 실패: %s" % (sk.path, _short(sk.reason))
                if line not in out["read_failed"]:
                    out["read_failed"].append(line)
    for k in STATE_KEYS:                                 # 같은 아티팩트가 여러 번 나오지 않게
        out[k] = list(dict.fromkeys(out[k]))
    return out


def analyze(units, rules, collection=None):
    """-> (rows, status)

    status[행위 ID] = {"state": detected(행 있음) / none(행 없음) / unsupported(규칙 없음),
                       "direct": n, "support": n,
                       "artifacts": {"path_missing": [...], "read_failed": [...], "no_match": [...]}}
    행이 없는 행위도 대표 상태 하나로 묶지 않고 아티팩트별 상태만 보여 준다.
    collection은 collector.CollectResult (프로필 목록과 건너뛴 곳을 알기 위함). 시험에서는 생략 가능.
    """
    rows = []
    for rule in rules:
        for idx, unit in enumerate(units):
            if unit.definition != rule.artifact or unit.parse_status not in ("ok", "partial"):
                continue
            if rule.op == "custom":
                hits = CUSTOM[rule.value](rule, idx, unit)
            else:
                hits = _match_rule(rule, idx, unit)
            if hits:
                row = _make_row(rule, unit, hits)
                row.unit_index = idx
                rows.append(row)

    order = {a[0]: i for i, a in enumerate(ACTIONS)}
    timed = sorted([r for r in rows if r.time], key=lambda r: (r.time, order[r.action_id], r.artifact.lower(), r.path.lower()))
    untimed = sorted([r for r in rows if not r.time], key=lambda r: (order[r.action_id], r.artifact.lower(), r.path.lower()))
    rows = timed + untimed

    status = {}
    for aid, _, _ in ACTIONS:
        arules = [r for r in rules if r.action_id == aid]
        arows = [r for r in rows if r.action_id == aid]
        st = {"direct": sum(1 for r in arows if r.grade == "직접"),
              "support": sum(1 for r in arows if r.grade == "보조"),
              "artifacts": {k: [] for k in STATE_KEYS}}
        if not arules:
            st["state"] = "unsupported"
        else:
            st["state"] = "detected" if arows else "none"
            matched = {id(units[r.unit_index]) for r in arows}
            names = list(dict.fromkeys(r.artifact for r in arules))
            st["artifacts"] = artifact_states(names, units, matched, collection)
        status[aid] = st
    return rows, status
