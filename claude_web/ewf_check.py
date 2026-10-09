"""E01(EWF) 세그먼트 확인과 해시 검증.

이미지를 열기 전에 세그먼트 파일(.E01, .E02 ...)을 모두 찾고, 빠진 것이 없는지 EWF 구조로 확인한다.
  - 세그먼트 파일마다 머리글에 세그먼트 번호(1, 2, 3 ...)가 있다 → 번호가 1부터 빠짐없이 이어져야 한다
  - 세그먼트 파일은 마지막 섹션이 next(다음 세그먼트가 더 있음) 또는 done(마지막 세그먼트)이다
    → 번호가 가장 큰 세그먼트가 done으로 끝나야 하고, 나머지는 next로 끝나야 한다
E01에 저장된 해시(hash 섹션의 MD5, digest 섹션의 MD5·SHA1)를 읽고,
요청하면(--verify-hash) 이미지 전체를 읽어 계산한 해시와 비교한다.

EWF 구조 (EnCase 1~6 형식, E01)
  파일 머리글 13바이트: 서명 8 (EVF 09 0D 0A FF 00) + 1 + 세그먼트 번호(uint16) + 2
  섹션 설명자 76바이트: 종류 16 + 다음 섹션 위치(uint64) + 크기(uint64) + 40 + 체크섬 4
  hash 섹션 데이터: MD5 16바이트 ...  /  digest 섹션 데이터: MD5 16 + SHA1 20 ...
"""
import hashlib
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

SIGNATURE = b"EVF\x09\x0d\x0a\xff\x00"
FILE_HEADER = 13
SECTION_DESC = 76
READ_CHUNK = 4 * 1024 * 1024


@dataclass
class Segment:
    path: Path
    number: int = 0
    size: int = 0
    last_section: str = ""
    error: str = ""

    @property
    def name(self):
        return self.path.name


@dataclass
class EwfInfo:
    segments: list = field(default_factory=list)
    stored_md5: str = ""
    stored_sha1: str = ""
    problems: list = field(default_factory=list)     # 빠진 세그먼트 등 (있으면 분석하지 않음)

    def segment_line(self):
        return "%d (%s)" % (len(self.segments), ", ".join(s.name for s in self.segments))

    def to_json(self):
        return {
            "segments": [{"name": s.name, "number": s.number, "size": s.size, "last_section": s.last_section}
                         for s in self.segments],
            "stored_hash": {"md5": self.stored_md5 or None, "sha1": self.stored_sha1 or None},
        }


def find_segments(path):
    """dissect와 같은 규칙으로 세그먼트 파일을 찾는다 (같은 폴더, 같은 이름, 확장자 E01~E99, EAA ...).
    dissect가 실제로 여는 목록과 같아야 하므로 dissect의 함수를 먼저 쓴다."""
    try:
        from dissect.evidence.ewf import find_files
        return [Path(p) for p in find_files(path)]
    except ImportError:
        p = Path(path).resolve()
        pat = re.compile(re.escape(p.stem) + r"\.[E-Ze-z][0-9A-Za-z]{2}$")
        return sorted(x for x in p.parent.iterdir() if pat.match(x.name))


def segment_file_name(stem, number):
    """세그먼트 번호 -> 파일 이름 (1~99는 .E01~.E99, 그 뒤는 .EAA부터)"""
    if 1 <= number <= 99:
        return "%s.E%02d" % (stem, number)
    n = number - 100
    if 0 <= n < 26 * 26:
        return "%s.E%s%s" % (stem, chr(65 + n // 26), chr(65 + n % 26))
    return "%s (%d번째 세그먼트)" % (stem, number)


def _read_segment(path, info):
    seg = Segment(path=path)
    try:
        seg.size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(FILE_HEADER)
            if len(head) < FILE_HEADER or head[:8] != SIGNATURE:
                seg.error = "EWF(E01) 세그먼트 서명이 아님"
                return seg
            seg.number = struct.unpack("<H", head[9:11])[0]
            offset = FILE_HEADER
            for _ in range(100000):                     # 손상된 파일에서 무한 반복하지 않게
                f.seek(offset)
                desc = f.read(SECTION_DESC)
                if len(desc) < SECTION_DESC:
                    seg.error = "섹션 목록이 중간에 끊김 (offset %d)" % offset
                    break
                kind = desc[:16].rstrip(b"\x00").decode("ascii", "replace")
                nxt = struct.unpack("<Q", desc[16:24])[0]
                if kind in ("hash", "digest"):
                    f.seek(offset + SECTION_DESC)
                    data = f.read(36)
                    md5 = data[:16]
                    if any(md5) and not info.stored_md5:
                        info.stored_md5 = md5.hex()
                    if kind == "digest" and any(data[16:36]):
                        info.stored_sha1 = data[16:36].hex()
                if kind in ("next", "done") or nxt == offset or nxt == 0:
                    seg.last_section = kind
                    break
                offset = nxt
    except OSError as e:
        seg.error = "%s: %s" % (type(e).__name__, e)
    return seg


def inspect_ewf(path):
    """세그먼트를 찾아 번호·끝 섹션을 확인하고 저장된 해시를 읽는다."""
    info = EwfInfo()
    paths = find_segments(path)
    if not paths:
        info.problems.append("세그먼트 파일을 찾지 못했습니다: %s" % path)
        return info
    info.segments = [_read_segment(p, info) for p in paths]
    stem = Path(path).stem

    for s in info.segments:
        if s.error:
            info.problems.append("%s: %s" % (s.name, s.error))
    good = [s for s in info.segments if not s.error]
    if not good:
        return info
    numbers = [s.number for s in good]
    dup = sorted({n for n in numbers if numbers.count(n) > 1})
    if dup:
        info.problems.append("세그먼트 번호가 겹칩니다: %s" % ", ".join(map(str, dup)))
    last = max(good, key=lambda s: s.number)
    missing = [n for n in range(1, last.number + 1) if n not in numbers]
    if last.last_section == "next":                    # 마지막으로 찾은 세그먼트 뒤에 더 있어야 함
        missing.append(last.number + 1)
    if missing:
        info.problems.append("세그먼트가 빠졌습니다: %s" % ", ".join(
            "%s (%d번)" % (segment_file_name(stem, n), n) for n in missing))
    early_done = [s.name for s in good if s is not last and s.last_section == "done"]
    if early_done:
        info.problems.append("마지막이 아닌 세그먼트가 done으로 끝납니다 (다른 이미지의 세그먼트일 수 있음): %s"
                             % ", ".join(early_done))
    return info


def verify(info, progress=None):
    """이미지 전체를 읽어 저장된 해시와 같은 종류의 해시를 계산한다 -> {"md5": hex, "sha1": hex}

    progress(읽은 바이트, 전체 바이트)는 콘솔 진행률 출력용."""
    from dissect.evidence.ewf import EWF
    ewf = EWF([s.path for s in info.segments])
    stream = ewf.open()
    total = ewf.size
    hashers = {}
    if info.stored_md5:
        hashers["md5"] = hashlib.md5()
    if info.stored_sha1:
        hashers["sha1"] = hashlib.sha1()
    done = 0
    stream.seek(0)
    while done < total:
        buf = stream.read(min(READ_CHUNK, total - done))
        if not buf:
            break
        for h in hashers.values():
            h.update(buf)
        done += len(buf)
        if progress:
            progress(done, total)
    if done != total:
        raise OSError("이미지를 끝까지 읽지 못했습니다 (%d / %d 바이트)" % (done, total))
    return {k: h.hexdigest() for k, h in hashers.items()}
