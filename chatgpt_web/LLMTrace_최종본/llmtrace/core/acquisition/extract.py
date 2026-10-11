# -*- coding: utf-8 -*-
r"""Disk-image -> collected-folder extractor.

The low-level EWF/RAW/VMDK, partition, NTFS, WOF and MFT code is shared by
all LLM surfaces.  Surface-specific paths are supplied by the selected profile
collection definition; this module never hard-codes ChatGPT/Claude paths.

Output preserves volume-root-relative paths under ``<out>/C/...`` so the
downstream parsers see the same layout whether input came from an image or a
pre-collected folder.
"""
import datetime
import hashlib
import io
import json
import os
import re
import sys

from . import mft as mftmod
from .formats import open_image, describe, ImageError
from .partition import read_partitions, pick_windows
from .ntfs import NtfsVolume, NtfsError

# Surface-specific collection paths are injected at runtime.
# Patterns passed to this module are regular expressions anchored at volume root.
DEFAULT_MAX_FILE = 512 * 1024 * 1024

# Generic low-value locations that a profile may opt to exclude. They are NOT
# applied automatically; collection.py decides whether to use them.
DEFAULT_NOISE_PATTERNS = [
    r'\\code cache\\',
    r'\\gpucache\\',
    r'\\dawncache\\',
    r'\\component_crx_cache\\',
    r'\\crashpad\\',
]

MAX_FILE = DEFAULT_MAX_FILE


def _rx(pats, anchored=True):
    r"""경로 패턴을 컴파일한다.

    anchored=True 면 볼륨 뿌리에 고정한다. 안 하면 이미지 안에 들어 있는
    다른 수집본(Analysis\T1\kape\C\Users\...)까지 걸려서 같은 아티팩트를
    여러 벌 세게 된다. 실측 1,626개 중 1,095개가 그런 중복이었고,
    그 탓에 대화가 38개가 아니라 139개로 보였다.
    """
    return [re.compile(('^' if anchored else '') + p) for p in pats]


class ExtractResult(object):
    def __init__(self):
        self.image = ''
        self.image_info = {}
        self.image_meta = {}
        self.partitions = []
        self.chosen = None
        self.volume_info = {}
        self.out_dir = ''
        self.mft_path = ''
        self.mft_bytes = 0
        self.files_found = 0
        self.files_written = 0
        self.bytes_written = 0
        self.deleted = []
        self.problems = []
        self.skipped = 0
        self.seconds = 0.0
        self.collection = {}

    def as_dict(self):
        return {
            'image': self.image, 'image_info': self.image_info,
            'image_meta': self.image_meta,
            'partitions': [p.as_dict() for p in self.partitions],
            'chosen_partition': self.chosen.as_dict() if self.chosen else None,
            'volume': self.volume_info, 'out_dir': self.out_dir,
            'mft_bytes': self.mft_bytes,
            'files_found': self.files_found,
            'files_written': self.files_written,
            'bytes_written': self.bytes_written,
            'deleted_count': len(self.deleted),
            'problem_count': len(self.problems),
            'skipped': self.skipped,
            'seconds': round(self.seconds, 1),
            'collection': self.collection,
        }


def _long(path):
    r"""윈도우 260자 경로 한계를 우회한다."""
    p = os.path.abspath(path)
    if os.name == 'nt' and len(p) > 240 and not p.startswith('\\\\?\\'):
        return '\\\\?\\' + p
    return p


def _safe_rel(path):
    """볼륨 안 경로를 출력 폴더 아래 상대경로로. 위로 올라가는 건 막는다."""
    parts = []
    for seg in path.replace('/', '\\').split('\\'):
        if not seg or seg in ('.', '..'):
            continue
        parts.append(seg)
    return os.path.join(*parts) if parts else ''


def _pick_volume(img, parts, want_index, log):
    """어느 파티션을 볼지 정한다.

    자동 선택은 '가장 큰 NTFS' 다. 복구 파티션도 NTFS 라 첫 NTFS 로
    고르면 틀린다. 고른 뒤에 \\Windows 나 \\Users 가 있는지 확인해서
    아니면 다음 후보로 넘어간다.
    """
    if want_index:
        for p in parts:
            if p.index == want_index:
                if p.fs != 'NTFS':
                    raise ImageError('파티션 %d 는 NTFS 가 아니다 (%s)'
                                     % (want_index, p.fs or '알 수 없음'))
                return p, NtfsVolume(img, p.offset)
        raise ImageError('파티션 %d 가 없다' % want_index)

    ntfs = sorted([p for p in parts if p.fs == 'NTFS'],
                  key=lambda p: p.size, reverse=True)
    if not ntfs:
        # '없다' 로 끝내면 수사관이 원인을 못 찾는다. 무엇이 있었는지와
        # 다음에 뭘 해야 하는지까지 말한다.
        seen = [p for p in parts if p.fs]
        if any(p.fs == 'BitLocker' for p in parts):
            raise ImageError(
                'BitLocker 로 암호화된 볼륨이다. 이 이미지는 암호화된 채로 '
                '떠졌다.\n'
                '        복구키나 암호로 먼저 풀어야 하고, 대상 PC 에서 다시 '
                '뜬다면\n'
                '        Physical Drive 가 아니라 Logical Drive 로 이미징한다.')
        if any(p.fs == 'ReFS' for p in parts):
            raise ImageError('ReFS 볼륨이다. 아직 지원하지 않는다.')
        raise ImageError(
            'NTFS 파티션이 없다. 확인된 파일시스템: %s\n'
            '        파티션 목록은 --list-partitions 로 볼 수 있다.'
            % (', '.join('%d=%s' % (p.index, p.fs) for p in seen) or '없음'))
    last = None
    for p in ntfs:
        try:
            vol = NtfsVolume(img, p.offset)
        except NtfsError as e:
            last = e
            continue
        if _has_windows(vol):
            return p, vol
        if log:
            log('   파티션 %d 는 윈도우 설치본이 아니다 — 건너뛴다' % p.index)
        last = None
    # \Windows 가 없어도 가장 큰 NTFS 는 돌려준다. 볼륨만 뜬 이미지일 수 있다.
    p = ntfs[0]
    return p, NtfsVolume(img, p.offset)


def _has_windows(vol, limit=50000):
    r"""루트 바로 아래에 \Windows 와 \Users 가 있는지 본다.

    실측한 볼륨에서 Windows 는 3748번, Users 는 3691번 레코드였다. 앞쪽
    몇만 개만 봐도 갈린다. 인덱스를 제대로 파고드는 게 정석이지만, 여기서
    필요한 건 '이 파티션이 윈도우 설치본인가' 하나라 이걸로 충분하다.
    """
    want = {'windows', 'users'}
    hit = set()
    for entry in range(5, min(limit, vol.mft_record_count)):
        raw = vol.record_raw(entry)          # parse_record 가 픽스업을 한다
        if raw is None:
            continue
        r = mftmod.parse_record(raw, entry)
        if r and r['is_dir'] and r['parent'] == mftmod.ROOT_ENTRY:
            nm = r['name'].lower()
            if nm in want:
                hit.add(nm)
                if len(hit) == len(want):
                    return True
    return bool(hit)


def extract_case(image_path, out_dir, partition=None, include_patterns=(),
                 exclude_patterns=(), noise_patterns=(), include_noise=False,
                 max_file=DEFAULT_MAX_FILE, collection_meta=None,
                 log=None, progress=None):
    """Extract profile-selected files from E01/RAW/VMDK into a collected layout.

    ``include_patterns`` and the other pattern arguments are regular-expression
    strings matched against lower-case NTFS volume-root-relative paths. Include
    expressions are anchored at the volume root. Exclude/noise expressions may
    match anywhere.
    """
    import time
    t0 = time.time()
    say = log or (lambda *a: None)
    res = ExtractResult()
    res.image = os.path.abspath(image_path)
    res.out_dir = os.path.abspath(out_dir)
    res.collection = dict(collection_meta or {})

    img = open_image(image_path)
    try:
        res.image_info = img.info()
        res.image_meta = img.metadata()
        say('   %s' % describe(img))
        if res.image_info.get('truncated_chunks'):
            res.problems.append({
                'kind': 'image_truncated',
                'detail': '청크 %s개가 표에 없다. 세그먼트 파일이 빠졌을 수 있다.'
                          % format(res.image_info['truncated_chunks'], ',')})
        for k, v in sorted(res.image_meta.items()):
            say('   %s: %s' % (k, v))

        parts = read_partitions(img)
        res.partitions = parts
        say('   파티션 %d개' % len(parts))
        for p in parts:
            say('     %d  %-12s %-6s %8.1f MB  %s'
                % (p.index, p.type_name, p.fs or '-',
                   p.size / 1048576.0, p.label or ''))

        chosen, vol = _pick_volume(img, parts, partition, say)
        res.chosen = chosen
        res.volume_info = vol.info()
        say('   -> 파티션 %d 를 본다 (%.1f GB · 클러스터 %s B · 레코드 %s개)'
            % (chosen.index, chosen.size / 1073741824.0,
               format(vol.cluster_size, ','),
               format(vol.mft_record_count, ',')))

        # ── $MFT ────────────────────────────────────────────
        vol_root = os.path.join(res.out_dir, 'C')
        os.makedirs(_long(vol_root), exist_ok=True)
        res.mft_path = os.path.join(vol_root, '$MFT')
        say('   $MFT 추출 중...')
        res.mft_bytes = vol.dump_mft(_long(res.mft_path), progress)
        say('   $MFT %s B' % format(res.mft_bytes, ','))

        # ── 경로 복원 ───────────────────────────────────────
        say('   경로 복원 중...')
        dmap = mftmod.build_dir_map(res.mft_path)
        say('   디렉터리 %s개' % format(len(dmap), ','))

        if not include_patterns:
            raise ImageError('collection profile has no include_patterns')
        pats = _rx([p.lower() for p in include_patterns])
        excludes = _rx([p.lower() for p in exclude_patterns], anchored=False)
        noise = _rx([p.lower() for p in noise_patterns], anchored=False)

        def keep(low):
            if excludes and any(p.search(low) for p in excludes):
                return False
            if not any(p.search(low) for p in pats):
                return False
            if not include_noise and noise and any(p.search(low) for p in noise):
                return False
            return True

        recs = mftmod.scan(res.mft_path, dmap=dmap, path_filter=keep)
        res.files_found = sum(
            1 for record in recs
            if not record['is_dir'] and _safe_rel(record['path'])
        )
        say('   대상 %s개' % format(len(recs), ','))

        # ── 내용 추출 ───────────────────────────────────────
        man = []
        n = 0
        for r in recs:
            n += 1
            if progress and n % 200 == 0:
                progress(n, len(recs))
            if r['is_dir']:
                continue
            rel = _safe_rel(r['path'])
            if not rel:
                continue
            dst = os.path.join(vol_root, rel)
            row = {
                'path': r['path'], 'entry': r['entry'], 'seq': r['seq'],
                'in_use': r['in_use'],
                'path_status': r.get('path_status', ''),
                'size_mft': r['size'],
                'si_modified': mftmod.filetime_str(r['si_modified']),
                'si_created': mftmod.filetime_str(r['si_created']),
                'si_mft_modified': mftmod.filetime_str(r['si_mft_modified']),
                'fn_modified': mftmod.filetime_str(r['fn_modified']),
                'status': '', 'note': '', 'compression': '',
                'size_read': 0, 'sha256': '',
            }
            if not r['in_use']:
                res.deleted.append(row)

            if r['size'] > max_file:
                row['status'] = 'too_big'
                row['note'] = '%s B — 건너뜀' % format(r['size'], ',')
                res.skipped += 1
                man.append(row)
                continue

            try:
                fd = vol.file_content(r['entry'])
            except Exception as e:
                row['status'] = 'error'
                row['note'] = '%s: %s' % (type(e).__name__, e)
                res.problems.append({'kind': 'read_error', 'path': r['path'],
                                     'detail': row['note']})
                man.append(row)
                continue

            row['status'] = fd.status
            row['note'] = fd.note
            row['compression'] = fd.compression
            if fd.data is None:
                # 내용을 못 가져온 것은 빈 파일로 만들지 않는다. 있는 척이
                # 되어 버린다. 목록에만 남기고 사유를 적는다.
                if fd.status != 'no_data':
                    res.problems.append({'kind': fd.status, 'path': r['path'],
                                         'detail': fd.note})
                man.append(row)
                continue

            try:
                os.makedirs(_long(os.path.dirname(dst)), exist_ok=True)
                with open(_long(dst), 'wb') as f:
                    f.write(fd.data)
            except OSError as e:
                row['status'] = 'write_error'
                row['note'] = str(e)
                res.problems.append({'kind': 'write_error', 'path': r['path'],
                                     'detail': str(e)})
                man.append(row)
                continue

            row['size_read'] = len(fd.data)
            row['sha256'] = hashlib.sha256(fd.data).hexdigest()
            res.files_written += 1
            res.bytes_written += len(fd.data)
            if fd.status != 'ok':
                res.problems.append({'kind': fd.status, 'path': r['path'],
                                     'detail': fd.note})
            man.append(row)

        _write_manifest(res.out_dir, man)
        _write_deleted(res.out_dir, res.deleted)
        res.seconds = time.time() - t0
        _write_custody(res, img)
        return res
    finally:
        img.close()


# ── 산출물 ──────────────────────────────────────────────────────
MANIFEST_COLS = ['path', 'entry', 'seq', 'in_use', 'path_status',
                 'size_mft', 'size_read', 'status', 'compression', 'note',
                 'sha256', 'si_created', 'si_modified', 'si_mft_modified',
                 'fn_modified']


def _csv(path, cols, rows):
    import csv
    with io.open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _write_manifest(out_dir, rows):
    _csv(os.path.join(out_dir, 'manifest.csv'), MANIFEST_COLS, rows)


def _write_deleted(out_dir, rows):
    if not rows:
        return
    _csv(os.path.join(out_dir, 'deleted_files.csv'), MANIFEST_COLS, rows)


def _write_custody(res, img):
    """증거 연계. 이미지 자체는 해시하지 않는다 — 60 GB 를 다시 읽는 건
    분석 때 할 일이 아니고, 이미징 도구가 이미 검증한 값이 있다."""
    doc = {
        'tool': 'LLMTrace common image extractor',
        'when': datetime.datetime.now().isoformat(timespec='seconds'),
        'image': res.image,
        'image_segments': getattr(img, 'paths', [res.image]),
        'image_info': res.image_info,
        'image_metadata': res.image_meta,
        'partitions': [p.as_dict() for p in res.partitions],
        'chosen_partition': res.chosen.as_dict() if res.chosen else None,
        'volume': res.volume_info,
        'result': res.as_dict(),
        'problems': res.problems[:500],
        'note': '원본 이미지는 읽기만 했다. 쓰기 경로 없음.',
    }
    with io.open(os.path.join(res.out_dir, 'extract_custody.json'),
                 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
