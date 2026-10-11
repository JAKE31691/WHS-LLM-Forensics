from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .schema import Observation
from .raw_io import json_default


def read_observations_jsonl(path: str) -> List[Observation]:
    out: List[Observation] = []
    with open(path, 'r', encoding='utf-8-sig') as f:
        for line in f:
            if not line.strip():
                continue
            out.append(Observation.from_dict(json.loads(line)))
    return out


def write_jsonl(path: str, rows: Iterable[Any]) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        for row in rows:
            d = row.as_dict() if hasattr(row, 'as_dict') else row
            f.write(json.dumps(d, ensure_ascii=False, sort_keys=True, default=json_default) + '\n')


def write_csv(path: str, columns: List[str], rows: Iterable[Dict[str, Any]]) -> None:
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        w.writeheader()
        for row in rows:
            cooked = {}
            for k, v in row.items():
                if isinstance(v, (list, dict)):
                    cooked[k] = json.dumps(v, ensure_ascii=False, sort_keys=True, default=json_default)
                else:
                    cooked[k] = v
            w.writerow(cooked)
