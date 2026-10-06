from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .extract import DEFAULT_MAX_FILE, extract_case

SUPPORTED_IMAGE_EXTS = {
    '.e01', '.ex01', '.l01', '.lx01', '.raw', '.dd', '.img', '.001', '.vmdk'
}


def normalize_rel(path: str) -> str:
    return path.replace('/', '\\').lstrip('\\')


def discover_collection_files(root: str, patterns: Iterable[str]) -> List[str]:
    """Discover files in an already-collected folder using volume-root globs."""
    found: List[str] = []
    base = Path(root)
    pats = [normalize_rel(p).lower() for p in patterns]
    for dp, _, fns in os.walk(base):
        for fn in fns:
            p = Path(dp) / fn
            rel = normalize_rel(str(p.relative_to(base))).lower()
            if any(fnmatch.fnmatch(rel, pat) for pat in pats):
                found.append(str(p))
    return sorted(found)


def collection_definition(profile) -> Dict[str, Any]:
    if profile.collection is None:
        return {}
    return getattr(profile.collection, 'COLLECTION', {})


def _glob_to_regex(pattern: str) -> str:
    """Convert a Windows volume-root glob to the regex dialect used by extract.py.

    ``*`` matches one path component; ``**`` may cross directory boundaries.
    The returned expression is *not* prefixed with ``^`` because extract.py does
    that for include expressions.
    """
    p = normalize_rel(pattern)
    out: List[str] = []
    i = 0
    while i < len(p):
        ch = p[i]
        if ch == '*':
            if i + 1 < len(p) and p[i + 1] == '*':
                out.append('.*')
                i += 2
            else:
                out.append(r'[^\\]*')
                i += 1
        elif ch == '?':
            out.append(r'[^\\]')
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return ''.join(out)


def _compile_pattern_list(items: Iterable[str], syntax: str) -> List[str]:
    vals = list(items or [])
    if syntax == 'regex':
        return [x.lower() for x in vals]
    if syntax != 'glob':
        raise ValueError(f'Unsupported collection path syntax: {syntax!r}')
    return [_glob_to_regex(x).lower() for x in vals]


def image_collection_spec(profile) -> Dict[str, Any]:
    """Normalize a surface ``COLLECTION['image']`` definition for the extractor."""
    definition = collection_definition(profile)
    image = dict(definition.get('image') or {})
    if not image:
        raise ValueError(
            f'Profile {profile.name!r} has no COLLECTION["image"] definition. '
            'Add volume-root-relative include paths to profiles/<surface>/collection.py.'
        )

    syntax = image.get('syntax', 'glob')
    include = _compile_pattern_list(image.get('include', []), syntax)
    exclude = _compile_pattern_list(image.get('exclude', []), syntax)
    noise = _compile_pattern_list(image.get('noise', []), syntax)
    if not include:
        raise ValueError(f'Profile {profile.name!r} image collection has no include paths')

    return {
        'include_patterns': include,
        'exclude_patterns': exclude,
        'noise_patterns': noise,
        'max_file': int(image.get('max_file_bytes', DEFAULT_MAX_FILE)),
        'collection_meta': {
            'profile': profile.name,
            'syntax': syntax,
            'include': list(image.get('include', [])),
            'exclude': list(image.get('exclude', [])),
            'noise': list(image.get('noise', [])),
        },
    }


def is_disk_image(path: str) -> bool:
    p = Path(path)
    return p.is_file() and p.suffix.lower() in SUPPORTED_IMAGE_EXTS


def extract_image(image_path: str, out_dir: str, profile, *, partition: int | None = None,
                  include_noise: bool = False, log=None, progress=None):
    """Common image adapter used by every surface profile."""
    spec = image_collection_spec(profile)
    return extract_case(
        image_path,
        out_dir,
        partition=partition,
        include_patterns=spec['include_patterns'],
        exclude_patterns=spec['exclude_patterns'],
        noise_patterns=spec['noise_patterns'],
        include_noise=include_noise,
        max_file=spec['max_file'],
        collection_meta=spec['collection_meta'],
        log=log,
        progress=progress,
    )


def prepare_input(input_path: str, profile, work_dir: str, *, partition: int | None = None,
                  include_noise: bool = False, log=None, progress=None) -> Tuple[str, Any | None]:
    """Return a folder parsers can consume; extract disk images when necessary."""
    if not is_disk_image(input_path):
        return input_path, None

    acquired = Path(work_dir) / '_acquired'
    acquired.mkdir(parents=True, exist_ok=True)
    result = extract_image(
        input_path,
        str(acquired),
        profile,
        partition=partition,
        include_noise=include_noise,
        log=log,
        progress=progress,
    )
    return str(acquired), result
