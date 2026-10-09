"""Local Storage, Session Storage (LevelDB) — ccl_chromium_reader 사용

압축된 .ldb 파일까지 읽고, 덮어쓰거나 지운 예전 레코드도 남긴다(m7).

Local Storage records[k] = 키-값 1건
  storage_key    사이트 (예: https://claude.ai)
  script_key     키 이름
  value          값 (문자열 그대로)
  value_json     값이 JSON 문자열이면 풀어 둔 것 (규칙이 값 안의 필드를 볼 수 있게)
  leveldb_seq    LevelDB 쓰기 순서 번호
  leveldb_state  live = 값 쓰기 기록 / deleted = 삭제 기록
  is_latest      같은 키의 기록 중 가장 마지막인지
  file           기록이 있던 파일 이름
"""
import json
import os
import pathlib

from parsers.common import ParseResult, to_json


def _maybe_json(s):
    if not isinstance(s, str) or not s[:1] in ("{", "["):
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def _mark_latest(records, key_fields):
    last = {}
    for i, r in enumerate(records):
        k = tuple(r[f] for f in key_fields)
        if k not in last or r["leveldb_seq"] > records[last[k]]["leveldb_seq"]:
            last[k] = i
    latest = set(last.values())
    for i, r in enumerate(records):
        r["is_latest"] = i in latest


def parse_local_storage(unit):
    from ccl_chromium_reader import ccl_chromium_localstorage as ls
    try:
        db = ls.LocalStoreDb(pathlib.Path(unit.local_path))
    except Exception as e:
        return ParseResult(status="error", note="%s: %s" % (type(e).__name__, e))
    try:
        # ccl은 META 기록으로 묶음(batch) 시각도 계산해 주지만 넣지 않는다.
        # 시험 자료에서 그 시각이 레코드 내용(이벤트 시각)보다 이르게 나와 기록 시각으로 믿을 수 없었다.
        records = []
        for rec in db.iter_all_records(include_deletions=True):
            r = {
                "storage_key": rec.storage_key,
                "script_key": to_json(rec.script_key),
                "value": to_json(rec.value),
            }
            parsed = _maybe_json(rec.value)
            if parsed is not None:
                r["value_json"] = parsed
            r.update({
                "leveldb_seq": rec.leveldb_seq_number,
                "leveldb_state": "live" if rec.is_live else "deleted",
                "file": os.path.basename(str(rec.file)) if rec.file else None,
            })
            records.append(r)
    except Exception as e:
        return ParseResult(status="error", note="%s: %s" % (type(e).__name__, e))
    finally:
        db.close()
    records.sort(key=lambda r: (r["storage_key"], r["leveldb_seq"]))
    _mark_latest(records, ("storage_key", "script_key"))
    return ParseResult(records)


def parse_session_storage(unit):
    from ccl_chromium_reader import ccl_chromium_sessionstorage as ss
    try:
        db = ss.SessionStoreDb(pathlib.Path(unit.local_path))
    except Exception as e:
        return ParseResult(status="error", note="%s: %s" % (type(e).__name__, e))
    try:
        records = []
        for rec in db.iter_all_records(include_deletions=True, include_orphans=True):
            r = {"host": rec.host, "key": to_json(rec.key), "value": to_json(rec.value)}
            parsed = _maybe_json(rec.value)
            if parsed is not None:
                r["value_json"] = parsed
            r.update({
                "leveldb_seq": rec.leveldb_sequence_number,
                "leveldb_state": "deleted" if rec.is_deleted else "live",
                "file": os.path.basename(str(rec.file)) if rec.file else None,
            })
            records.append(r)
    except Exception as e:
        return ParseResult(status="error", note="%s: %s" % (type(e).__name__, e))
    finally:
        db.close()
    records.sort(key=lambda r: (r["host"] or "", r["leveldb_seq"]))
    _mark_latest(records, ("host", "key"))
    return ParseResult(records)
