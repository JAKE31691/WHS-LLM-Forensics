from __future__ import annotations

import shutil
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from llmtrace.core.schema import RawRecord
from .base import register
from .helpers import find_files


def _sidecars(path: Path):
    return [Path(str(path) + x) for x in ('-journal', '-wal', '-shm')]


def _make_writable(path: Path):
    try:
        path.chmod(path.stat().st_mode | stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


@contextmanager
def open_sqlite_snapshot(path: Path):
    """Read an acquired Chromium SQLite DB through a writable temporary copy.

    WAL/rollback-journal recovery, if SQLite needs it, happens only on the
    working copy. The acquired source is never modified.
    """
    with tempfile.TemporaryDirectory(prefix='llmtrace_sqlite_') as td:
        work = Path(td)
        db = work / path.name
        shutil.copy2(path, db)
        copied = [db]
        for src in _sidecars(path):
            if src.is_file():
                dst = work / src.name
                shutil.copy2(src, dst)
                copied.append(dst)
        for p in copied:
            _make_writable(p)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute('PRAGMA busy_timeout=2000')
            conn.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
            conn.execute('PRAGMA query_only=ON')
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()


def _tables(conn) -> list[str]:
    # Read every table visible in sqlite_master; do not pre-filter Chromium data.
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]


def _rows(conn, table: str):
    q = table.replace('"', '""')
    cur = conn.execute(f'SELECT * FROM "{q}"')
    for row in cur:
        yield {k: row[k] for k in row.keys()}


def parse_sqlite_files(input_root: str, *, names: list[str], artifact: str, parser_name: str):
    ordinal = 0
    for path in find_files(input_root, names):
        with open_sqlite_snapshot(path) as conn:
            for table in _tables(conn):
                try:
                    for i, row in enumerate(_rows(conn, table), 1):
                        ordinal += 1
                        yield RawRecord(
                            artifact=artifact,
                            dataset=table,
                            record=row,
                            source_path=str(path),
                            source_locator=f'table={table};row={i}',
                            parser=parser_name,
                            ordinal=ordinal,
                        )
                except sqlite3.DatabaseError:
                    # Keep the parser conservative: a table that SQLite itself
                    # cannot SELECT is skipped rather than replaced with guessed data.
                    continue


def _register_sqlite(name: str, file_names: list[str]):
    @register(name)
    def parser(input_root, step, profile):
        artifact = step.get('artifact') or name.removeprefix('chromium_')
        return list(parse_sqlite_files(
            input_root, names=file_names, artifact=artifact, parser_name=name
        ))
    return parser


chromium_history = _register_sqlite('chromium_history', ['History'])
chromium_cookies = _register_sqlite('chromium_cookies', ['Cookies'])
chromium_dips = _register_sqlite('chromium_dips', ['DIPS'])
chromium_favicons = _register_sqlite('chromium_favicons', ['Favicons'])
