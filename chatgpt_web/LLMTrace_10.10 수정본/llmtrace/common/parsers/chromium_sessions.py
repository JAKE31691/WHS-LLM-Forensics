from __future__ import annotations

import struct
from pathlib import Path

from llmtrace.core.schema import RawRecord
from .base import register, report_problem
from .helpers import find_dirs


def iter_snss_raw(path: Path):
    """Yield native SNSS command records without browser/service interpretation."""
    data = path.read_bytes()
    if len(data) < 8 or data[:4] != b'SNSS':
        raise ValueError(f'Invalid SNSS magic: {path}')
    version = struct.unpack_from('<I', data, 4)[0]
    if version not in (1, 3):
        raise ValueError(f'Unsupported SNSS version {version}: {path}')

    off = 8
    while off + 2 <= len(data):
        record_offset = off
        length = struct.unpack_from('<H', data, off)[0]
        off += 2
        if length == 0:
            break
        if off + length > len(data):
            raise ValueError(
                f'Truncated SNSS command at offset {record_offset}: '
                f'need {length} bytes, have {len(data)-off}'
            )
        record = data[off:off + length]
        off += length
        if not record:
            continue
        yield record_offset, version, record[0], record[1:]


@register('chromium_sessions')
def parse_sessions(input_root, step, profile):
    artifact = step.get('artifact') or 'sessions'
    out = []
    ordinal = 0
    for folder in find_dirs(input_root, ['Sessions']):
        for path in sorted(folder.iterdir(), key=lambda p: p.name):
            if not path.is_file() or not path.name.startswith(('Session_', 'Tabs_')):
                continue
            dataset = 'session_commands' if path.name.startswith('Session_') else 'tabs_commands'
            try:
                commands = iter_snss_raw(path)
                for offset, version, command_id, payload in commands:
                    ordinal += 1
                    out.append(RawRecord(
                        artifact=artifact,
                        dataset=dataset,
                        record={
                            'version': version,
                            'command_id': command_id,
                            'payload': payload,
                        },
                        source_path=str(path),
                        source_locator=f'offset={offset}',
                        parser='chromium_sessions',
                        ordinal=ordinal,
                    ))
            except (OSError, ValueError, struct.error) as exc:
                report_problem(
                    step, parser='chromium_sessions', artifact=artifact,
                    source_path=str(path),
                    message=f'{type(exc).__name__}: {exc}',
                )
                continue
    return out
