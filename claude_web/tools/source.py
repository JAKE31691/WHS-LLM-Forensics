"""입력(E01 또는 live)을 판별하고, 그 안의 파일을 읽는다.

수집 단계 이후는 입력 종류와 상관없이 같은 코드를 쓴다.
그래서 여기서는 두 입력이 같은 함수(list_dir, is_dir, is_file, copy_to)를 갖도록 맞춘다.
경로는 항상 증거 기준 Windows 절대 경로 문자열(예: C:\\Users\\a\\NTUSER.DAT)로 주고받는다.
"""
import getpass
import hashlib
import os
import platform
import sys
from pathlib import PureWindowsPath

from timeutil import iso_from_epoch

CHUNK = 1024 * 1024


class SourceError(Exception):
    """입력 자체를 열 수 없을 때 (종료 코드 2). details는 오류 앞에 같이 출력할 줄들."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or []


def _split(win_path):
    """'C:\\Users\\a' -> ['Users', 'a'] (드라이브 문자 뒤 부분)"""
    return list(PureWindowsPath(win_path).parts[1:])


def _copy_stream(src, dest_path):
    """열린 파일을 dest_path로 복사하면서 SHA-256을 계산한다."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    h = hashlib.sha256()
    size = 0
    with open(dest_path, "wb") as out:
        while True:
            buf = src.read(CHUNK)
            if not buf:
                break
            h.update(buf)
            out.write(buf)
            size += len(buf)
    return size, h.hexdigest()


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _stat_times(st, ctime_is_creation=False):
    """stat 결과에서 원본 파일 시각(UTC ISO)을 꺼낸다. 없는 값은 None.
    Windows의 Python 3.11 이하는 st_birthtime이 없고 st_ctime이 생성 시각이다."""
    created = getattr(st, "st_birthtime", None)
    if created is None and ctime_is_creation:
        created = getattr(st, "st_ctime", None)
    return {
        "created": iso_from_epoch(created),
        "modified": iso_from_epoch(getattr(st, "st_mtime", None)),
        "accessed": iso_from_epoch(getattr(st, "st_atime", None)),
    }


class LiveSource:
    """지금 실행 중인 Windows의 파일을 그대로 읽는다.

    root는 시험용이다. 실제 실행에서는 시스템 드라이브(C:\\)를 쓴다.
    """
    kind = "Live system"

    def __init__(self, root=None):
        self.drive = (os.environ.get("SystemDrive") or "C:").upper()
        self.root = root if root is not None else self.drive + "\\"
        if not os.path.isdir(self.root):
            raise SourceError("시스템 드라이브를 찾을 수 없습니다: %s" % self.root)
        self.display = "live"

    def _local(self, win_path):
        return os.path.join(self.root, *_split(win_path))

    def list_dir(self, win_path):
        """[(이름, 폴더 여부)] — 바로가기 폴더(junction, symlink)는 건너뛴다."""
        out = []
        with os.scandir(self._local(win_path)) as it:
            for e in it:
                try:
                    attrs = getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0)
                except OSError:
                    attrs = 0
                # symlink, junction(예: C:\Users\Default User) 모두 reparse point다
                if e.is_symlink() or attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
                out.append((e.name, e.is_dir(follow_symlinks=False)))
        return sorted(out, key=lambda x: x[0].lower())

    def is_dir(self, win_path):
        return os.path.isdir(self._local(win_path))

    def is_file(self, win_path):
        return os.path.isfile(self._local(win_path))

    def copy_to(self, win_path, dest_path):
        """파일을 복사하고 {size, sha256, times}를 돌려준다. 실패하면 OSError."""
        local = self._local(win_path)
        st = os.stat(local)
        with open(local, "rb") as f:
            size, digest = _copy_stream(f, dest_path)
        return {"size": size, "sha256": digest, "times": _stat_times(st, sys.platform == "win32")}

    def evidence_info(self):
        return {"hostname": platform.node(), "os": system_info()["os"]}

    def close(self):
        pass


class E01Source:
    """E01 이미지를 마운트하지 않고 dissect.target으로 NTFS를 직접 읽는다.

    열기 전에 세그먼트(.E01, .E02 ...)를 모두 찾아 빠진 것이 없는지 확인한다 (ewf_check.py).
    빠진 세그먼트가 있으면 열지 않는다. 그대로 열면 빠진 부분에 있는 파일이
    '없음'처럼 보여 결과가 틀리기 때문이다.
    """
    kind = "E01"

    def __init__(self, path):
        from ewf_check import inspect_ewf
        if not os.path.isfile(path):
            raise SourceError("E01 파일이 없습니다: %s" % path)
        self.display = path
        self.ewf = inspect_ewf(path)
        seg_line = ["Segments: " + self.ewf.segment_line()] if self.ewf.segments else []
        if self.ewf.problems:
            raise SourceError("E01 세그먼트에 문제가 있어 분석하지 않습니다. 모든 세그먼트를 .E01과 같은 폴더에 "
                              "같은 이름으로 두고 다시 실행하세요.", seg_line + self.ewf.problems)
        try:
            from dissect.target import Target
        except ImportError:
            raise SourceError("E01을 읽으려면 dissect.target이 필요합니다: pip install dissect.target", seg_line)
        try:
            self.target = Target.open(path)
        except Exception as e:
            raise SourceError("E01을 열 수 없습니다: %s: %s" % (type(e).__name__, e))
        self.drive = "C:"
        self.root = self._find_system_root()

    def _find_system_root(self):
        """Users 폴더가 있는 볼륨을 C:로 본다.

        dissect가 Windows를 인식하면 시스템 볼륨(sysvol)을 쓰고,
        인식하지 못하면 파일 시스템을 하나씩 열어 Users 폴더가 있는 것을 고른다.
        """
        t = self.target
        try:
            root = t.fs.path("sysvol")
            if root.joinpath("Users").is_dir():
                return root
        except Exception:
            pass
        for fs in getattr(t, "filesystems", []):
            try:
                root = fs.path("/")
                if root.joinpath("Users").is_dir():
                    return root
            except Exception:
                continue
        raise SourceError("E01 안에서 Users 폴더가 있는 볼륨을 찾지 못했습니다")

    def _path(self, win_path):
        p = self.root
        for part in _split(win_path):
            p = p.joinpath(part)
        return p

    def list_dir(self, win_path):
        # dissect의 오류(FilesystemError 등)는 OSError가 아니므로 OSError로 바꿔 수집이 멈추지 않게 한다
        try:
            children = list(self._path(win_path).iterdir())
        except Exception as e:
            raise OSError("%s: %s" % (type(e).__name__, e))
        out = []
        for child in children:
            try:
                if child.is_symlink():
                    continue
                out.append((child.name, child.is_dir()))
            except Exception as e:
                raise OSError("%s: %s (%s)" % (type(e).__name__, e, child.name))
        return sorted(out, key=lambda x: x[0].lower())

    # dissect는 없는 경로에 예외 없이 False를 돌려준다. 그래서 예외는 '없음'이 아니라 '읽기 오류'다.
    # 읽기 오류를 False(없음)로 바꾸면 그 아래 아티팩트가 경고 없이 빠지므로 OSError로 올린다.
    def is_dir(self, win_path):
        try:
            return self._path(win_path).is_dir()
        except OSError:
            raise
        except Exception as e:
            raise OSError("%s: %s" % (type(e).__name__, e))

    def is_file(self, win_path):
        try:
            return self._path(win_path).is_file()
        except OSError:
            raise
        except Exception as e:
            raise OSError("%s: %s" % (type(e).__name__, e))

    def copy_to(self, win_path, dest_path):
        try:
            p = self._path(win_path)
            st = p.stat()
            with p.open("rb") as f:
                size, digest = _copy_stream(f, dest_path)
        except OSError:
            raise
        except Exception as e:
            raise OSError("%s: %s" % (type(e).__name__, e))
        return {"size": size, "sha256": digest, "times": _stat_times(st)}

    def evidence_info(self):
        info = {"hostname": None, "os": None}
        for key, attr in (("hostname", "hostname"), ("os", "version")):
            try:
                info[key] = getattr(self.target, attr)
            except Exception:
                pass
        return info

    def close(self):
        try:
            self.target.close()
        except Exception:
            pass


def open_source(arg):
    """CLI 첫 번째 인자로 입력 종류를 판별한다."""
    if arg.lower() == "live":
        return LiveSource()
    if arg.lower().endswith((".e01", ".ex01")):
        return E01Source(arg)
    raise SourceError("입력은 E01 파일 경로 또는 live 이어야 합니다: %s" % arg)


def system_info():
    """도구를 실행하는 PC의 정보 (m8: System info는 실행 PC 기준)"""
    if sys.platform == "win32":
        build = sys.getwindowsversion().build
        os_name = "Windows 11" if build >= 22000 else "Windows %s" % platform.release()
    else:
        os_name = "%s %s" % (platform.system(), platform.release())
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    return {
        "machine": platform.node(),
        "is_64bit": platform.machine().lower() in ("amd64", "x86_64", "arm64", "aarch64"),
        "user": user,
        "os": os_name,
    }

