"""Cache (Cache_Data) — ccl_chromium_reader 사용

records[k] = 캐시 항목 1건
  url, key            요청 URL, 원래 캐시 키
  request_time        요청 시각 (UTC)
  response_time       응답 시각 (UTC)
  status_line         HTTP 상태 줄
  headers             응답 헤더 {이름: [값...]}
  bodies              복원한 응답 본문 목록 (같은 키의 예전 버전이 있으면 여러 개)
                      claude.ai 응답만 본문을 넣는다(m6). 나머지는 size만 남긴다.
    bodies[n].json    JSON 응답이면 풀어 둔 것
    bodies[n].text    그 밖의 텍스트 응답
"""
import gzip
import json
import pathlib
import zlib
from urllib.parse import urlsplit

from parsers.common import ParseResult, to_json
from timeutil import iso_utc

BODY_HOSTS = ("claude.ai",)          # 본문을 넣는 호스트 (하위 도메인 포함)
MAX_TEXT_CHARS = 2000000


def _decoders():
    """content-encoding별 해제 함수. 없는 라이브러리는 None."""
    d = {"gzip": gzip.decompress, "deflate": zlib.decompress, "": lambda b: b, "identity": lambda b: b}
    try:
        import brotli
        d["br"] = brotli.decompress
    except ImportError:
        d["br"] = None
    try:
        from compression import zstd           # Python 3.14+
        d["zstd"] = zstd.decompress
    except ImportError:
        try:
            import zstandard
            d["zstd"] = lambda b: zstandard.ZstdDecompressor().decompressobj().decompress(b)
        except ImportError:
            d["zstd"] = None
    return d


def _want_body(url):
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in BODY_HOSTS)


def _body(data, meta, decoders, notes):
    enc = ((meta.get_attribute("content-encoding") or [""])[0] if meta else "").lower().strip()
    ctype = ((meta.get_attribute("content-type") or [""])[0] if meta else "").lower()
    b = {"size": len(data), "content_encoding": enc, "content_type": ctype}
    fn = decoders.get(enc)
    if fn is None:
        b["note"] = "해제할 수 없는 인코딩: %s" % (enc or "?")
        notes.add(b["note"])
        return b
    try:
        raw = fn(data)
    except Exception as e:
        b["note"] = "본문 해제 실패: %s" % type(e).__name__
        return b
    text = raw.decode("utf-8", "replace")
    if "json" in ctype:
        try:
            b["json"] = json.loads(text)
            return b
        except ValueError:
            pass
    if ctype.startswith("text/") or "json" in ctype or "javascript" in ctype:
        b["text"] = text[:MAX_TEXT_CHARS]
    else:
        b["note"] = "텍스트가 아닌 본문"
    return b


def parse(unit):
    from ccl_chromium_reader import ccl_chromium_cache as cc
    try:
        cls = cc.guess_cache_class(pathlib.Path(unit.local_path))
        if cls is None:
            return ParseResult(status="error", note="캐시 형식을 알 수 없음")
        cache = cls(pathlib.Path(unit.local_path))
    except Exception as e:
        return ParseResult(status="error", note="%s: %s" % (type(e).__name__, e))
    decoders = _decoders()
    records, notes, failed = [], set(), 0
    try:
        for key in sorted(cache.keys()):
            try:
                url = cc.CacheKey(key).url
                metas = cache.get_metadata(key)
                datas = cache.get_cachefile(key)
                m0 = next((m for m in metas if m), None)
                r = {"url": url, "key": key,
                     "request_time": iso_utc(m0.request_time) if m0 and m0.request_time else None,
                     "response_time": iso_utc(m0.response_time) if m0 and m0.response_time else None,
                     "status_line": None, "headers": {}}
                if m0:
                    decl = sorted(m0.http_header_declarations)
                    r["status_line"] = decl[0] if decl else None
                    headers = {}
                    for k, v in m0.http_header_attributes:
                        headers.setdefault(k, []).append(v)
                    r["headers"] = to_json(headers)
                bodies = []
                for i, data in enumerate(datas):
                    if data is None:
                        continue
                    meta = metas[i] if i < len(metas) else m0
                    if _want_body(url):
                        bodies.append(_body(data, meta, decoders, notes))
                    else:
                        bodies.append({"size": len(data), "note": "본문 생략 (claude.ai 외)"})
                r["bodies"] = bodies
                records.append(r)
            except Exception as e:
                failed += 1
                notes.add("항목 읽기 실패: %s" % type(e).__name__)
    finally:
        try:
            cache.close()
        except Exception:
            pass
    status = "partial" if failed or any(n.startswith("해제할 수 없는") for n in notes) else "ok"
    return ParseResult(records, status, "; ".join(sorted(notes)))
