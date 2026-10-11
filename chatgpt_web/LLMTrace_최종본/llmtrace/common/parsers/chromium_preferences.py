from __future__ import annotations

import json
from pathlib import Path

from llmtrace.core.schema import RawRecord
from .base import register
from .helpers import find_files


@register('chromium_preferences')
def parse_preferences(input_root, step, profile):
    artifact = step.get('artifact') or 'preferences'
    out = []
    for i, path in enumerate(find_files(input_root, ['Preferences']), 1):
        with path.open('r', encoding='utf-8-sig') as f:
            obj = json.load(f)
        out.append(RawRecord(
            artifact=artifact,
            dataset='preferences',
            record=obj,
            source_path=str(path),
            source_locator='$',
            parser='chromium_preferences',
            ordinal=i,
            output_format='json',
        ))
    return out
