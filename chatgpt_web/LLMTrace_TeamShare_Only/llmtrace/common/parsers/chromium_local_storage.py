from __future__ import annotations

from pathlib import Path

from llmtrace.core.schema import RawRecord
from .base import register
from .helpers import ccl_import, find_dirs, find_leveldb_dir, public_attrs


_RECORD_ATTRS = (
    'storage_key', 'script_key', 'value', 'leveldb_seq_number', 'is_live', 'file'
)


@register('chromium_local_storage')
def parse_local_storage(input_root, step, profile):
    artifact = step.get('artifact') or 'local_storage'
    out = []
    ordinal = 0
    containers = find_dirs(input_root, ['Local Storage'])
    if not containers:
        return out
    local_mod, _, _, _ = ccl_import()
    for container in containers:
        dbdir = find_leveldb_dir(container)
        if dbdir is None:
            continue
        with local_mod.LocalStoreDb(dbdir) as db:
            for rec in db.iter_all_records(include_deletions=True):
                ordinal += 1
                row = public_attrs(rec, _RECORD_ATTRS)
                out.append(RawRecord(
                    artifact=artifact,
                    dataset='records',
                    record=row,
                    source_path=str(dbdir),
                    source_locator=f'record={ordinal}',
                    parser='chromium_local_storage',
                    ordinal=ordinal,
                ))
    return out
