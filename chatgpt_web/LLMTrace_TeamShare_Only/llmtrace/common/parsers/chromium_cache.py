from __future__ import annotations

from pathlib import Path

from llmtrace.core.schema import RawRecord
from .base import register
from .helpers import ccl_import, find_dirs, json_plain, public_attrs


_CACHE_META_ATTRS = (
    'request_time', 'response_time', 'http_header_attributes',
    'http_header_declarations', 'other_cache_attributes', 'certs',
)


def _pick_cache_dir(path: Path, cache_mod) -> Path:
    for p in (path / 'Cache_Data', path):
        if p.is_dir():
            try:
                if cache_mod.guess_cache_class(p):
                    return p
            except Exception:
                pass
    return path


def _metadata_plain(meta):
    if meta is None:
        return None
    return public_attrs(meta, _CACHE_META_ATTRS)


@register('chromium_cache')
def parse_cache(input_root, step, profile):
    artifact = step.get('artifact') or 'cache'
    out = []
    ordinal = 0

    # Collection may contain either .../Cache or only .../Cache/Cache_Data.
    candidates = find_dirs(input_root, ['Cache', 'Cache_Data'])
    if not candidates:
        return out
    _, _, cache_mod, _ = ccl_import()
    seen = set()
    roots = []
    for c in candidates:
        d = _pick_cache_dir(c, cache_mod)
        try:
            key = str(d.resolve()).lower()
        except Exception:
            key = str(d).lower()
        if key not in seen:
            seen.add(key)
            roots.append(d)

    for d in roots:
        cls = cache_mod.guess_cache_class(d)
        if cls is None:
            continue
        with cls(d) as cache:
            for raw_key in cache.keys():
                key_text = str(raw_key)
                try:
                    metas = list(cache.get_metadata(raw_key))
                except Exception:
                    metas = []
                try:
                    bodies = list(cache.get_cachefile(raw_key))
                except Exception:
                    bodies = []
                count = max(len(metas), len(bodies), 1)
                for idx in range(count):
                    ordinal += 1
                    # key/metadata/body are the native logical components exposed
                    # by the Chromium cache reader. No URL splitting, previews,
                    # hashes, current/obsolete labels, or behavioral fields are added.
                    row = {
                        'key': key_text,
                        'metadata': _metadata_plain(metas[idx]) if idx < len(metas) else None,
                        'body': bodies[idx] if idx < len(bodies) else None,
                    }
                    out.append(RawRecord(
                        artifact=artifact,
                        dataset='entries',
                        record=row,
                        source_path=str(d),
                        source_locator=f'key={key_text};version={idx}',
                        parser='chromium_cache',
                        ordinal=ordinal,
                    ))
    return out
