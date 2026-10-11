from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from llmtrace.core.schema import RawRecord
from .base import register, report_problem
from .helpers import ccl_import, find_dirs, find_leveldb_dir, public_attrs


_RECORD_ATTRS = (
    'storage_key', 'script_key', 'value', 'leveldb_seq_number', 'is_live', 'file'
)


def _append_records(out, db, *, artifact, source_path, ordinal):
    for rec in db.iter_all_records(include_deletions=True):
        ordinal += 1
        row = public_attrs(rec, _RECORD_ATTRS)
        if row.get('file'):
            row['file'] = str(source_path / Path(str(row['file'])).name)
        out.append(RawRecord(
            artifact=artifact,
            dataset='records',
            record=row,
            source_path=str(source_path),
            source_locator=f'record={ordinal}',
            parser='chromium_local_storage',
            ordinal=ordinal,
        ))
    return ordinal


def _readable_leveldb_files(dbdir, level_mod):
    readable = []
    rejected = []
    for source in sorted(dbdir.iterdir(), key=lambda item: item.name.lower()):
        if source.suffix.lower() not in ('.ldb', '.log'):
            continue
        parser_cls = (
            level_mod.LdbFile if source.suffix.lower() == '.ldb'
            else level_mod.LogFile
        )
        parser = None
        try:
            parser = parser_cls(source)
            for _ in parser:
                pass
            readable.append(source)
        except Exception as exc:
            rejected.append((source, exc))
        finally:
            if parser is not None:
                try:
                    parser.close()
                except Exception:
                    pass
    return readable, rejected


def _recover_without_corrupt_files(dbdir, local_mod, level_mod, out, *,
                                   artifact, ordinal, step):
    readable, rejected = _readable_leveldb_files(dbdir, level_mod)
    for source, exc in rejected:
        report_problem(
            step,
            parser='chromium_local_storage',
            artifact=artifact,
            source_path=str(source),
            code='leveldb_file_skipped',
            message=(
                '손상되거나 기록 중이던 LevelDB 파일을 제외하고 나머지 레코드를 '
                f'부분 복구했습니다: {type(exc).__name__}: {exc}'
            ),
        )
    if not readable:
        return ordinal

    with tempfile.TemporaryDirectory(prefix='llmtrace-local-storage-') as temp:
        recovered = Path(temp)
        for source in readable:
            shutil.copy2(source, recovered / source.name)
        for name in ('CURRENT', 'LOG', 'LOG.old', 'MANIFEST-000001'):
            source = dbdir / name
            if source.is_file():
                shutil.copy2(source, recovered / name)
        with local_mod.LocalStoreDb(recovered) as db:
            ordinal = _append_records(
                out, db, artifact=artifact, source_path=dbdir, ordinal=ordinal,
            )
    return ordinal


@register('chromium_local_storage')
def parse_local_storage(input_root, step, profile):
    artifact = step.get('artifact') or 'local_storage'
    out = []
    ordinal = 0
    containers = find_dirs(input_root, ['Local Storage'])
    if not containers:
        return out
    local_mod, _, _, level_mod = ccl_import()
    for container in containers:
        dbdir = find_leveldb_dir(container)
        if dbdir is None:
            continue
        try:
            with local_mod.LocalStoreDb(dbdir) as db:
                ordinal = _append_records(
                    out, db, artifact=artifact, source_path=dbdir,
                    ordinal=ordinal,
                )
        except Exception as exc:
            report_problem(
                step,
                parser='chromium_local_storage',
                artifact=artifact,
                source_path=str(dbdir),
                code='leveldb_partial_recovery',
                message=(
                    'Local Storage 전체 파싱에 실패하여 파일별 부분 복구를 '
                    f'시도했습니다: {type(exc).__name__}: {exc}'
                ),
            )
            ordinal = _recover_without_corrupt_files(
                dbdir, local_mod, level_mod, out, artifact=artifact,
                ordinal=ordinal, step=step,
            )
    return out
