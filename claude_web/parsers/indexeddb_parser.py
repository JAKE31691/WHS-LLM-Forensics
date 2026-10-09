"""IndexedDB (claude.ai) — ccl_chromium_reader 사용

값이 커서 .blob 폴더로 빠진 레코드도 함께 읽는다. 지우거나 덮어쓴 예전 레코드도 남긴다(m7).

records[k] = 객체 1건
  database, object_store   DB 이름, 객체 저장소 이름
  key                      레코드 키
  value                    V8 직렬화 값을 풀어 둔 것 (객체·배열은 JSON 구조 그대로)
  leveldb_seq              LevelDB 쓰기 순서 번호
  leveldb_state            live = 값 쓰기 기록 / deleted = 삭제 기록
  is_latest                같은 키의 기록 중 가장 마지막인지
  file                     기록이 있던 파일 이름
  external_value           값이 .blob 폴더에 있었으면 그 위치
  value_note               값을 넣지 않은 이유 (아래)

.blob에 있는 값은 같은 키의 최신 기록만 복원할 수 있다. ccl은 blob 파일을 키로만 찾아서
예전 기록에도 최신 blob 내용을 붙여 주는데, 그 값은 예전 기록의 실제 값이 아니다
(예전 blob 파일은 대개 이미 지워져 있다). 그래서 예전 기록의 외부 값은 null로 두고 이유를 적는다.
"""
import os
import pathlib

from parsers.common import ParseResult, to_json


def parse(unit):
    from ccl_chromium_reader import ccl_chromium_indexeddb as idb
    blob_dir = pathlib.Path(unit.extra_local_paths[0]) if unit.extra_local_paths else None
    try:
        wrapper = idb.WrappedIndexDB(pathlib.Path(unit.local_path), blob_dir)
    except Exception as e:
        return ParseResult(status="error", note="%s: %s" % (type(e).__name__, e))

    records, bad = [], []

    def on_bad(key, data):
        bad.append(key)

    try:
        for dbid in wrapper.database_ids:
            db = wrapper[dbid]
            for store_name in db.object_store_names:
                store = db.get_object_store_by_name(store_name)
                try:
                    for rec in store.iterate_records(live_only=False, bad_deserializer_data_handler=on_bad):
                        key = rec.key.value if hasattr(rec.key, "value") else rec.key
                        records.append({
                            "database": db.name,
                            "object_store": store_name,
                            "key": to_json(key),
                            "value": to_json(rec.value),
                            "leveldb_seq": rec.ldb_seq_no,
                            "leveldb_state": "live" if rec.is_live else "deleted",
                            "file": os.path.basename(str(rec.origin_file)) if rec.origin_file else None,
                            "external_value": rec.external_value_path,
                        })
                except Exception as e:
                    bad.append("%s/%s: %s: %s" % (db.name, store_name, type(e).__name__, e))
    except Exception as e:
        return ParseResult(records, "error", "%s: %s" % (type(e).__name__, e))

    records.sort(key=lambda r: (r["database"], r["object_store"], r["leveldb_seq"]))
    last = {}
    for i, r in enumerate(records):
        k = (r["database"], r["object_store"], repr(r["key"]))
        if k not in last or r["leveldb_seq"] > records[last[k]]["leveldb_seq"]:
            last[k] = i
    latest = set(last.values())
    dropped = 0
    for i, r in enumerate(records):
        r["is_latest"] = i in latest
        if r["external_value"] and not r["is_latest"]:
            r["value"] = None
            r["external_value"] = None      # ccl이 준 위치도 최신 blob의 것이라 지운다
            r["value_note"] = "예전 기록의 .blob 값은 복원할 수 없어 넣지 않음 (최신 기록만 복원 가능)"
            dropped += 1

    note = "예전 .blob 값 %d건 제외" % dropped if dropped else ""
    if bad:
        return ParseResult(records, "partial", "; ".join(x for x in ("값을 풀지 못한 레코드 %d건" % len(bad), note) if x))
    return ParseResult(records, "ok", note)
