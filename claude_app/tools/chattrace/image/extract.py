# -*- coding: utf-8 -*-
r"""이미지 -> 수집물. E01 하나만 주면 분석 직전 상태까지 만든다.

    CASE.E01 ─┬─ 파티션 찾기 ─ NTFS 열기 ─ $MFT 추출
              ├─ 경로 복원 ─ Claude 아티팩트 골라내기
              └─ 파일 내용 추출 ──▶ <출력>\C\Users\...\Claude\...
                                   <출력>\C\$MFT
                                   <출력>\manifest.csv

출력 모양을 KAPE 수집물과 똑같이 맞춘다. 그래야 분석 쪽 코드를 한 줄도
고치지 않고 그대로 먹인다. 이미지에서 왔든 현장 수집에서 왔든 뒷단은
같은 것을 본다.

마운트하지 않는다. 마운트는 ACL 을 유지해서 남의 AppData 를 못 읽고,
드라이버 설치와 관리자 권한을 요구하며, 끝나고 떼어내는 것도 일이다.
원시 파싱은 그 셋을 다 건너뛴다.
"""
import datetime
import hashlib
import io
import json
import os
import re
import sys

from .. import mft as mftmod
from .formats import open_image, describe, ImageError
from .partition import read_partitions, pick_windows
from .ntfs import NtfsVolume, NtfsError

# ── 무엇을 가져올 것인가 ────────────────────────────────────────
# 볼륨 뿌리에서부터 맞춘다. 'claude 가 들어간 경로'로 잡으면 이미지 안에
# 들어 있는 남의 수집물(Analysis\T7\kape\C\Users\...)까지 딸려 온다.
CLAUDE_PATTERNS = [
    r'users\\[^\\]+\\appdata\\local\\packages\\claude_[^\\]*\\',
    r'users\\[^\\]+\\appdata\\roaming\\claude\\',
    r'users\\[^\\]+\\appdata\\local\\claude\\',
    r'users\\[^\\]+\\appdata\\local\\anthropic[^\\]*\\',
    r'users\\[^\\]+\\appdata\\roaming\\anthropic[^\\]*\\',
    r'programdata\\packages\\claude_[^\\]*\\',
]

# 앱 본체. 버전·설치시각 확인용이라 기본은 끈다. 수백 MB 다.
APP_PATTERNS = [
    r'program files\\windowsapps\\claude_[^\\]*\\',
]

# 행위 재구성용 시스템 아티팩트. --with-system 일 때만.
SYSTEM_PATTERNS = [
    r'windows\\prefetch\\',
    r'windows\\appcompat\\programs\\',
    r'windows\\system32\\config\\(system|software|sam|security)$',
    r'users\\[^\\]+\\ntuser\.dat$',
    r'users\\[^\\]+\\appdata\\local\\microsoft\\windows\\usrclass\.dat$',
    r'windows\\system32\\winevt\\logs\\',
    r'windows\\system32\\sru\\',
    # 브라우저 기록 — 설치 파일을 어디서 받았는지, 무엇을 검색했는지.
    # 우리 분석 7위: 접근 경로 전체가 복원된다.
    r'users\\[^\\]+\\appdata\\local\\microsoft\\edge\\user data\\[^\\]+\\'
    r'(history|top sites|shortcuts|web data|login data)$',
    r'users\\[^\\]+\\appdata\\local\\google\\chrome\\user data\\[^\\]+\\'
    r'(history|top sites|shortcuts|web data)$',
    r'users\\[^\\]+\\appdata\\roaming\\mozilla\\firefox\\profiles\\[^\\]+\\'
    r'places\.sqlite$',
    # MSIX 매니페스트 — 파일명 자체가 버전이다
    r'programdata\\microsoft\\windows\\apprepository\\',
]

# 대화 흔적이 안 나오는 걸로 실측된 경로. 용량만 크다.
NOISE_PATTERNS = [
    r'\\cache\\cache_data\\',
    r'\\code cache\\',
    r'\\gpucache\\',
    r'\\dawncache\\',
    r'\\component_crx_cache\\',
    r'\\crashpad\\',
]

MAX_FILE = 512 * 1024 * 1024        # 이보다 큰 파일은 건너뛴다


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
        self.files_written = 0
        self.bytes_written = 0
        self.deleted = []
        self.problems = []
        self.skipped = 0
        self.seconds = 0.0

    def as_dict(self):
        return {
            'image': self.image, 'image_info': self.image_info,
            'image_meta': self.image_meta,
            'partitions': [p.as_dict() for p in self.partitions],
            'chosen_partition': self.chosen.as_dict() if self.chosen else None,
            'volume': self.volume_info, 'out_dir': self.out_dir,
            'mft_bytes': self.mft_bytes,
            'files_written': self.files_written,
            'bytes_written': self.bytes_written,
            'deleted_count': len(self.deleted),
            'problem_count': len(self.problems),
            'skipped': self.skipped,
            'seconds': round(self.seconds, 1),
        }


def _long(path):
    r"""윈도우 260자 한계. Claude 경로는 UUID 가 겹쳐 쉽게 넘는다."""
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


def extract_case(image_path, out_dir, partition=None, with_app=False,
                 with_system=False, include_noise=False, exclude=(),
                 log=None, progress=None):
    """E01(또는 dd/vmdk) 에서 분석에 필요한 것을 꺼내 수집물 모양으로 만든다."""
    import time
    t0 = time.time()
    say = log or (lambda *a: None)
    res = ExtractResult()
    res.image = os.path.abspath(image_path)
    res.out_dir = os.path.abspath(out_dir)

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

        pats = _rx(CLAUDE_PATTERNS)
        if with_app:
            pats += _rx(APP_PATTERNS)
        if with_system:
            pats += _rx(SYSTEM_PATTERNS)
        noise = _rx(NOISE_PATTERNS, anchored=False)   # 잡음은 경로 어디든
        excl = [e.lower() for e in exclude]

        def keep(low):
            if any(e in low for e in excl):
                return False
            if not any(p.search(low) for p in pats):
                return False
            if not include_noise and any(p.search(low) for p in noise):
                return False
            return True

        recs = mftmod.scan(res.mft_path, dmap=dmap, path_filter=keep)
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

            if r['size'] > MAX_FILE:
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
        'tool': 'ChatTrace image extractor',
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
