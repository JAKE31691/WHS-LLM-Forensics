from __future__ import annotations

from llmtrace.core.schema import RawRecord
from .base import register
from .helpers import ccl_import, find_dirs, public_attrs


_RAW_ATTRS = ('user_key', 'value', 'seq', 'state', 'origin_file')


@register('chromium_indexeddb')
def parse_indexeddb(input_root, step, profile):
    """Recover raw LevelDB records beneath Chromium IndexedDB containers.

    Object-store interpretation remains a surface-processing concern.  This
    parser intentionally exposes only the physical LevelDB record fields.
    """
    artifact = step.get('artifact') or 'indexeddb'
    containers = find_dirs(input_root, ['IndexedDB'])
    dbdirs = []
    for container in containers:
        if (container / 'CURRENT').is_file():
            dbdirs.append(container)
        for current in container.rglob('CURRENT'):
            if current.is_file():
                dbdirs.append(current.parent)
    if not dbdirs:
        return []

    *_, level_mod = ccl_import()
    out = []
    ordinal = 0
    seen = set()
    for dbdir in sorted(dbdirs, key=lambda p: str(p).lower()):
        key = str(dbdir.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        db = level_mod.RawLevelDb(dbdir)
        try:
            for record in db.iterate_records_raw():
                ordinal += 1
                out.append(RawRecord(
                    artifact=artifact,
                    dataset='leveldb_records',
                    record=public_attrs(record, _RAW_ATTRS),
                    source_path=str(dbdir),
                    source_locator=f'record={ordinal}',
                    parser='chromium_indexeddb',
                    ordinal=ordinal,
                ))
        finally:
            db.close()
    return out
