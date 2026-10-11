from __future__ import annotations

import dataclasses
import enum
import json
from collections.abc import Mapping
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable


def find_files(root: str | Path, names: Iterable[str]) -> list[Path]:
    """Find files by exact basename beneath an acquired/collected root."""
    base = Path(root)
    wanted = {str(x).lower() for x in names}
    if not base.exists():
        return []
    out = []
    for p in base.rglob('*'):
        try:
            if p.is_file() and p.name.lower() in wanted:
                out.append(p)
        except OSError:
            continue
    return sorted(set(out), key=lambda p: str(p).lower())


def find_dirs(root: str | Path, names: Iterable[str]) -> list[Path]:
    """Find directories by exact basename beneath an acquired/collected root."""
    base = Path(root)
    wanted = {str(x).lower() for x in names}
    if not base.exists():
        return []
    out = []
    for p in base.rglob('*'):
        try:
            if p.is_dir() and p.name.lower() in wanted:
                out.append(p)
        except OSError:
            continue
    return sorted(set(out), key=lambda p: str(p).lower())


def find_leveldb_dir(container: Path) -> Path | None:
    """Return the LevelDB directory stored directly or under ``leveldb``."""
    direct = container / 'leveldb'
    if direct.is_dir():
        return direct
    if (container / 'CURRENT').is_file():
        return container
    return None


def json_plain(value: Any, *, _depth: int = 0) -> Any:
    """Representation-only conversion for parser-library objects.

    No service/surface meaning is introduced. Native JSON scalars and bytes are
    left intact (bytes are encoded losslessly later by raw_io). Datetimes and
    enums are represented as their scalar forms; parser objects are represented
    by their exposed attribute names.
    """
    if _depth > 16:
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool, bytes, bytearray, memoryview)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value if isinstance(value.value, (str, int, float, bool)) else value.name
    if dataclasses.is_dataclass(value):
        return {k: json_plain(v, _depth=_depth + 1) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(k): json_plain(v, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_plain(v, _depth=_depth + 1) for v in value]

    attrs = {}
    try:
        d = vars(value)
    except Exception:
        d = None
    if isinstance(d, dict):
        for k, v in d.items():
            if callable(v):
                continue
            attrs[str(k)] = json_plain(v, _depth=_depth + 1)
    if attrs:
        return attrs
    return repr(value)


def ccl_import():
    try:
        from ccl_chromium_reader import (
            ccl_chromium_localstorage,
            ccl_chromium_sessionstorage,
            ccl_chromium_cache,
        )
        from ccl_chromium_reader.storage_formats import ccl_leveldb
        return (
            ccl_chromium_localstorage,
            ccl_chromium_sessionstorage,
            ccl_chromium_cache,
            ccl_leveldb,
        )
    except Exception as e:
        raise RuntimeError(
            'ccl_chromium_reader is required for Chromium LevelDB/Cache parsing. '
            'Install the same dependency used by semantic_diff_v0.8.9.'
        ) from e


def public_attrs(obj: Any, names: Iterable[str]) -> dict[str, Any]:
    """Copy parser-object attributes using their original exposed names."""
    out = {}
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            continue
        out[name] = json_plain(value)
    # Preserve additional instance fields without overwriting explicit attrs.
    try:
        d = vars(obj)
    except Exception:
        d = {}
    if isinstance(d, dict):
        for k, v in d.items():
            if k not in out and not callable(v):
                out[k] = json_plain(v)
    return out
