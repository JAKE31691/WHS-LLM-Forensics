"""아티팩트 종류 1개 = 파서 1개. 모든 파서는 parse(unit) -> ParseResult 형태다."""
from parsers import cache_parser, indexeddb_parser, json_parser, leveldb_parser, snss_parser, sqlite_parser

PARSERS = {
    "sqlite": sqlite_parser.parse,
    "json": json_parser.parse,
    "localstorage": leveldb_parser.parse_local_storage,
    "sessionstorage": leveldb_parser.parse_session_storage,
    "indexeddb": indexeddb_parser.parse,
    "cache": cache_parser.parse,
    "snss": snss_parser.parse,
}


def parse_unit(unit):
    """unit을 파싱해 records, parse_status, parse_note를 채운다. 예외가 나도 멈추지 않는다."""
    if unit.collection_status == "error":
        unit.parse_status, unit.parse_note = "error", "수집 실패로 파싱하지 않음"
        return
    if not unit.files:
        unit.parse_status, unit.parse_note = "ok", "빈 폴더"
        return
    try:
        res = PARSERS[unit.parser](unit)
    except Exception as e:
        unit.parse_status, unit.parse_note = "error", "%s: %s" % (type(e).__name__, e)
        return
    unit.records, unit.parse_status, unit.parse_note = res.records, res.status, res.note
    if unit.collection_status == "partial" and unit.parse_status == "ok":
        unit.parse_status = "partial"
        unit.parse_note = (unit.parse_note + "; " if unit.parse_note else "") + "일부 파일 수집 실패"
