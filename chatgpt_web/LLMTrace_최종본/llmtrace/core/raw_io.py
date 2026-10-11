from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .schema import RawRecord


def _safe_name(value: str, fallback: str = 'records') -> str:
    value = (value or fallback).strip()
    value = re.sub(r'[^A-Za-z0-9._-]+', '_', value)
    return value or fallback


def json_default(value: Any):
    """Lossless JSON representation for values JSON cannot natively encode.

    This is representation-level serialization only; no service meaning is
    added. Bytes are encoded in a tagged object so they can be reconstructed.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        import base64
        return {
            '__llmtrace_raw_type__': 'bytes',
            'encoding': 'base64',
            'data': base64.b64encode(bytes(value)).decode('ascii'),
        }
    raise TypeError(f'Object of type {type(value).__name__} is not JSON serializable')


# Kept as an alias for extensions written against the v0.4 parser preview.
_json_default = json_default


def write_raw_records(root: str, records: Iterable[RawRecord]) -> Dict[str, Any]:
    """Write first-stage records without injecting metadata into the records.

    Artifact records are stored under ``_raw/<artifact>/<dataset>.json[l]``.
    Each JSONL line (or JSON value) is exactly ``RawRecord.record`` modulo the
    JSON representation required for non-JSON-native scalar types.

    Provenance is kept separately in ``_raw/manifest.json``.
    """
    out = Path(root)
    out.mkdir(parents=True, exist_ok=True)
    grouped: Dict[Tuple[str, str, str], List[RawRecord]] = defaultdict(list)
    for r in records:
        grouped[(r.artifact, r.dataset, r.output_format or 'jsonl')].append(r)

    manifest: Dict[str, Any] = {'version': 1, 'datasets': []}

    for (artifact, dataset, fmt), rows in sorted(grouped.items()):
        artifact_dir = out / _safe_name(artifact, 'artifact')
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stem = _safe_name(dataset, 'records')
        ext = '.json' if fmt == 'json' else '.jsonl'
        path = artifact_dir / f'{stem}{ext}'

        if fmt == 'json':
            payload = rows[0].raw_value() if len(rows) == 1 else [r.raw_value() for r in rows]
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=json_default)
        else:
            with open(path, 'w', encoding='utf-8') as f:
                for r in rows:
                    f.write(json.dumps(r.raw_value(), ensure_ascii=False, default=json_default) + '\n')

        sources = []
        seen = set()
        for r in rows:
            key = (r.source_path, r.parser)
            if key in seen:
                continue
            seen.add(key)
            sources.append({'source_path': r.source_path, 'parser': r.parser})

        manifest['datasets'].append({
            'artifact': artifact,
            'dataset': dataset,
            'format': fmt,
            'output': str(path.relative_to(out)).replace('\\', '/'),
            'record_count': len(rows),
            'sources': sources,
        })

    with open(out / 'manifest.json', 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest
