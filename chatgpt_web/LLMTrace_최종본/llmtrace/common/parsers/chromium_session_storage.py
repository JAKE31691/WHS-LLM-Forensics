from __future__ import annotations

from llmtrace.core.schema import RawRecord
from .base import register
from .helpers import ccl_import, find_dirs, find_leveldb_dir, public_attrs


_RAW_ATTRS = ('user_key', 'value', 'seq', 'state', 'origin_file')


@register('chromium_session_storage')
def parse_session_storage(input_root, step, profile):
    artifact = step.get('artifact') or 'session_storage'
    out = []
    ordinal = 0
    containers = find_dirs(input_root, ['Session Storage'])
    if not containers:
        return out
    *_, level_mod = ccl_import()
    for container in containers:
        dbdir = find_leveldb_dir(container)
        if dbdir is None:
            continue
        db = level_mod.RawLevelDb(dbdir)
        try:
            for rec in db.iterate_records_raw():
                ordinal += 1
                row = public_attrs(rec, _RAW_ATTRS)
                out.append(RawRecord(
                    artifact=artifact,
                    dataset='leveldb_records',
                    record=row,
                    source_path=str(dbdir),
                    source_locator=f'record={ordinal}',
                    parser='chromium_session_storage',
                    ordinal=ordinal,
                ))
        finally:
            db.close()
    return out
