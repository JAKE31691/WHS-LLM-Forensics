"""Read-only collection of registered Windows artifacts into one ZIP archive."""

from __future__ import annotations

import base64
import fnmatch
import glob
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Iterator, Sequence

from artifact_definitions import ArtifactDefinition, expand_path_pattern


CHUNK_SIZE = 1024 * 1024
MAX_REGISTRY_HIVE = 128 * 1024 * 1024
PROFILE_CHECKS: tuple[tuple[str, str], ...] = (
    (".codex", r".codex"),
    ("AppData\\Local\\Packages", r"AppData\Local\Packages"),
    ("Chrome User Data", r"AppData\Local\Google\Chrome\User Data"),
    ("NTUSER.DAT", "NTUSER.DAT"),
    ("UsrClass.dat", r"AppData\Local\Microsoft\Windows\UsrClass.dat"),
)
SYSTEM_PROFILE_NAMES = {
    "public", "default", "default user", "all users", "defaultuser0",
    "systemprofile", "localservice", "networkservice", "wdagutilityaccount",
}
SYSTEM_PROFILE_PREFIXES = ("defaultuser", "dwm-", "umfd-")


class CollectionSetupError(Exception):
    """The source or output cannot be initialized for collection."""


@dataclass(frozen=True)
class FileState:
    size: int
    mtime_ns: int | None


@dataclass(frozen=True)
class Candidate:
    original_path: str
    reference: Any


@dataclass(frozen=True)
class ShadowCopy:
    drive: str
    id: str
    device: str
    create_completed_utc: str


def _run_vss_script(script: str) -> str:
    """Run the Windows VSS WMI interface without passing evidence paths to a shell."""
    command = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    powershell = (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" /
                  "WindowsPowerShell" / "v1.0" / "powershell.exe")
    if not powershell.is_file():
        raise CollectionSetupError("Windows PowerShell is required for live VSS collection")
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
            capture_output=True, text=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError("VSS command timed out") from exc
    if result.returncode:
        raise OSError((result.stderr or result.stdout).strip() or
                      f"VSS command failed with exit code {result.returncode}")
    return result.stdout.strip()


def _create_shadow_copy(drive: str) -> ShadowCopy:
    if not re.fullmatch(r"[A-Za-z]:", drive):
        raise ValueError(f"invalid VSS drive: {drive}")
    drive = drive.upper()
    script = (
        "$ErrorActionPreference='Stop'; "
        "$created=$null; "
        "try { "
        "$class=[wmiclass]'root\\cimv2:Win32_ShadowCopy'; "
        f"$created=$class.Create('{drive}\\','ClientAccessible'); "
        "if($created.ReturnValue -ne 0){throw ('VSS Create returned '+$created.ReturnValue)}; "
        "$copy=Get-WmiObject Win32_ShadowCopy | Where-Object "
        "{$_.ID -eq $created.ShadowID} | Select-Object -First 1; "
        "if(-not $copy){throw 'created VSS snapshot cannot be located'}; "
        "@{id=$copy.ID;device=$copy.DeviceObject} | ConvertTo-Json -Compress "
        "} catch { "
        "if($created -and $created.ShadowID){ "
        "Get-WmiObject Win32_ShadowCopy | Where-Object "
        "{$_.ID -eq $created.ShadowID} | ForEach-Object { $_.Delete() | Out-Null } "
        "}; throw }"
    )
    try:
        details = json.loads(_run_vss_script(script).splitlines()[-1])
        snapshot_id = details["id"]
        device = details["device"].rstrip("\\")
        if not re.fullmatch(r"\{[0-9a-fA-F-]{36}\}", snapshot_id):
            raise ValueError("VSS returned an invalid snapshot ID")
        if not re.fullmatch(r"\\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy\d+", device, re.I):
            raise ValueError("VSS returned an invalid device path")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise CollectionSetupError(f"VSS returned an invalid response for {drive}: {exc}") from exc
    return ShadowCopy(drive, snapshot_id, device, datetime.now(timezone.utc).isoformat())


def _delete_shadow_copy(snapshot: ShadowCopy) -> None:
    if not re.fullmatch(r"\{[0-9a-fA-F-]{36}\}", snapshot.id):
        raise ValueError("invalid VSS snapshot ID")
    script = (
        "$ErrorActionPreference='Stop'; "
        "$copy=Get-WmiObject Win32_ShadowCopy | Where-Object "
        f"{{$_.ID -eq '{snapshot.id}'}} | Select-Object -First 1; "
        "if($copy){$copy.Delete() | Out-Null; "
        "$remaining=Get-WmiObject Win32_ShadowCopy | Where-Object "
        f"{{$_.ID -eq '{snapshot.id}'}} | Select-Object -First 1; "
        "if($remaining){throw 'VSS snapshot still exists after deletion'}}"
    )
    _run_vss_script(script)


def _is_access_error(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32)


@dataclass
class CollectionSummary:
    found: int = 0
    extracted: int = 0
    failures: int = 0
    warnings: list[str] = field(default_factory=list)
    per_definition: dict[str, int] = field(default_factory=dict)
    per_artifact: dict[str, int] = field(default_factory=dict)
    archive_path: Path | None = None


@dataclass(frozen=True)
class UserProfile:
    path: str
    artifact_presence: tuple[tuple[str, bool], ...]


def is_system_profile(name: str) -> bool:
    normalized = name.casefold()
    return normalized in SYSTEM_PROFILE_NAMES or normalized.startswith(SYSTEM_PROFILE_PREFIXES)


def discover_user_profiles(reader: Any) -> tuple[UserProfile, ...]:
    """Apply one profile filter and artifact check to either source reader."""
    profiles: list[UserProfile] = []
    seen: set[str] = set()
    for profile_path in reader.list_user_directories():
        profile = PureWindowsPath(profile_path)
        if is_system_profile(profile.name) or str(profile).casefold() in seen:
            continue
        seen.add(str(profile).casefold())
        presence: list[tuple[str, bool]] = []
        for label, relative in PROFILE_CHECKS:
            artifact_path = str(profile / PureWindowsPath(relative))
            try:
                exists = reader.path_exists(artifact_path)
            except (OSError, IOError, RuntimeError) as exc:
                reader.warnings.append(f"cannot check {artifact_path}: {exc}")
                exists = False
            presence.append((label, exists))
        profiles.append(UserProfile(str(profile), tuple(presence)))
    return tuple(sorted(profiles, key=lambda item: item.path.casefold()))


def zip_path_for_windows_path(original_path: str) -> str:
    """Keep the original tree and replace C: with the top-level C directory."""
    path = PureWindowsPath(original_path)
    if not re.fullmatch(r"[A-Za-z]:", path.drive):
        raise ValueError(f"source has no verified drive letter: {original_path}")
    parts = (path.drive[0].upper(), *path.parts[1:])
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise ValueError(f"unsafe source path: {original_path}")
    return "/".join(parts)


class LiveReader:
    """Read one VSS point-in-time view, with raw NTFS fallback for locked files."""

    def __init__(self, output: Path, system_drive: str | None = None, *, use_vss: bool = True):
        self.source_mode = "live"
        self.source_path = "live"
        self.system_drive = system_drive or os.environ.get("SystemDrive", "C:")
        self.output = output.resolve()
        self.warnings: list[str] = []
        self.snapshots: dict[str, ShadowCopy] = {}
        self._raw_volumes: dict[str, Any] = {}
        self._raw_files: dict[str, Any] = {}
        self.profiles = discover_user_profiles(self)
        self.user_profiles = tuple(profile.path for profile in self.profiles)
        if use_vss:
            drives = {self.system_drive.upper()}
            drives.update(PureWindowsPath(path).drive.upper() for path in self.user_profiles)
            try:
                for drive in sorted(drives):
                    self.snapshots[drive] = _create_shadow_copy(drive)
            except (OSError, ValueError, CollectionSetupError) as exc:
                self.close()
                raise CollectionSetupError(
                    f"live VSS snapshot creation failed: {exc}; run an elevated Windows terminal"
                ) from exc

    def _source_path(self, original_path: str) -> str:
        path = PureWindowsPath(original_path)
        snapshot = self.snapshots.get(path.drive.upper())
        if snapshot is None:
            return original_path
        return snapshot.device + "\\" + "\\".join(path.parts[1:])

    def _original_path(self, source_path: str, drive: str) -> str:
        snapshot = self.snapshots.get(drive.upper())
        if snapshot is None:
            return source_path
        prefix = snapshot.device + "\\"
        if not source_path.casefold().startswith(prefix.casefold()):
            raise ValueError(f"VSS path is outside snapshot {snapshot.id}: {source_path}")
        return drive.upper() + "\\" + source_path[len(prefix):]

    def _raw_file(self, candidate: Candidate) -> Any:
        key = _candidate_key(candidate)
        if key in self._raw_files:
            return self._raw_files[key]
        path = PureWindowsPath(candidate.original_path)
        drive = path.drive.upper()
        if not re.fullmatch(r"[A-Z]:", drive):
            raise OSError(f"raw read requires a verified drive: {candidate.original_path}")
        if drive not in self._raw_volumes:
            try:
                import pytsk3  # type: ignore[import-not-found]
                device = self.snapshots[drive].device if drive in self.snapshots else "\\\\.\\" + drive
                image = pytsk3.Img_Info(device)
                filesystem = pytsk3.FS_Info(image, offset=0)
                self._raw_volumes[drive] = (image, filesystem)
            except (ImportError, OSError, RuntimeError) as exc:
                raise OSError(f"raw NTFS volume cannot be opened for {drive}: {exc}") from exc
        _, filesystem = self._raw_volumes[drive]
        internal = "/" + "/".join(path.parts[1:])
        try:
            raw_file = filesystem.open(path=internal)
        except (OSError, RuntimeError) as exc:
            raise OSError(f"raw NTFS file cannot be opened: {candidate.original_path}: {exc}") from exc
        self._raw_files[key] = raw_file
        return raw_file

    def enable_raw(self, candidate: Candidate) -> None:
        self._raw_file(candidate)

    def collection_method(self, candidate: Candidate) -> str:
        kind = "vss" if PureWindowsPath(candidate.original_path).drive.upper() in self.snapshots else "live"
        return kind + ("_raw_ntfs" if _candidate_key(candidate) in self._raw_files else "_regular")

    def list_user_directories(self) -> tuple[str, ...]:
        users_root = Path(self.system_drive + r"\Users")
        profiles: list[str] = []
        try:
            for path in users_root.iterdir():
                is_junction = getattr(path, "is_junction", lambda: False)()
                if path.is_dir() and not path.is_symlink() and not is_junction:
                    profiles.append(str(path))
        except OSError as exc:
            self.warnings.append(f"cannot enumerate user profiles at {users_root}: {exc}")
        current = os.environ.get("USERPROFILE")
        if current and Path(current).is_dir() and current.casefold() not in {p.casefold() for p in profiles}:
            profiles.append(current)
        return tuple(profiles)

    def path_exists(self, original_path: str) -> bool:
        return Path(original_path).exists()

    def _excluded(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
            return resolved == self.output or self.output in resolved.parents
        except OSError:
            return False

    def find(self, pattern: str) -> Iterator[Candidate]:
        # Python's glob/stat can silently treat a GLOBALROOT VSS device path
        # as absent. Discover names through the live directory tree, then read
        # every candidate's content and metadata from the matching snapshot.
        # A live name absent from the snapshot becomes a collection failure.
        if glob.has_magic(pattern):
            matches = glob.iglob(pattern)
        else:
            # glob.iglob silently drops an exact path when stat is denied.
            # Keep it as a candidate so the manifest records collection_failed
            # with Access Denied instead of claiming the artifact is absent.
            try:
                Path(pattern).stat()
            except FileNotFoundError:
                return
            except PermissionError:
                yield Candidate(pattern, Path(self._source_path(pattern)))
                return
            except OSError as exc:
                self.warnings.append(f"cannot inspect {pattern}: {exc}")
                return
            matches = (pattern,)
        for found in matches:
            path = Path(found)
            original = str(path)
            if self._excluded(Path(original)):
                continue
            if path.is_symlink():
                self.warnings.append(f"skipped symlink outside direct artifact data: {path}")
                continue
            if path.is_dir():
                def on_error(exc: OSError) -> None:
                    self.warnings.append(f"cannot traverse {path}: {exc}")

                for root, directories, files in os.walk(path, followlinks=False, onerror=on_error):
                    directories[:] = [d for d in directories if not self._excluded(Path(root) / d)]
                    for name in files:
                        child = Path(root) / name
                        original_child = str(child)
                        if child.is_symlink():
                            self.warnings.append(f"skipped symlink outside direct artifact data: {child}")
                        elif not self._excluded(Path(original_child)):
                            yield Candidate(original_child, Path(self._source_path(original_child)))
            else:
                # A matching but unreadable file is a collection failure,
                # not an absent artifact.
                yield Candidate(original, Path(self._source_path(original)))

    def stat(self, candidate: Candidate) -> FileState:
        if _candidate_key(candidate) in self._raw_files:
            meta = self._raw_file(candidate).info.meta
            return FileState(int(meta.size), int(meta.mtime) * 1_000_000_000 if meta.mtime else None)
        try:
            state = Path(candidate.reference).stat()
            return FileState(state.st_size, state.st_mtime_ns)
        except OSError as exc:
            if not _is_access_error(exc):
                raise
            try:
                self.enable_raw(candidate)
                return self.stat(candidate)
            except OSError as raw_exc:
                raise OSError(f"regular stat failed: {exc}; raw NTFS retry failed: {raw_exc}") from raw_exc

    def chunks(self, candidate: Candidate) -> Iterator[bytes]:
        if _candidate_key(candidate) in self._raw_files:
            raw_file = self._raw_file(candidate)
            size = int(raw_file.info.meta.size)
            for offset in range(0, size, CHUNK_SIZE):
                chunk = raw_file.read_random(offset, min(CHUNK_SIZE, size - offset))
                if not chunk:
                    raise OSError(f"unexpected end of raw NTFS file: {candidate.original_path}")
                yield chunk
            return
        with Path(candidate.reference).open("rb") as source:
            while chunk := source.read(CHUNK_SIZE):
                yield chunk

    def close(self) -> None:
        for image, _ in self._raw_volumes.values():
            try:
                image.close()
            except (OSError, RuntimeError) as exc:
                self.warnings.append(f"cannot close raw NTFS volume: {exc}")
        self._raw_volumes.clear()
        self._raw_files.clear()
        for drive, snapshot in list(self.snapshots.items()):
            try:
                _delete_shadow_copy(snapshot)
            except (OSError, ValueError) as exc:
                self.warnings.append(f"cannot delete VSS snapshot {snapshot.id}: {exc}")
            else:
                del self.snapshots[drive]


@dataclass
class _ImageVolume:
    fs: Any
    offset: int
    start_sector: int
    drive: str | None = None


class E01Reader:
    """Read an EWF image through pyewf and pytsk3, with verified drive mapping."""

    def __init__(self, source: str):
        self.source_mode = "E01"
        self.source_path = source
        try:
            import pyewf  # type: ignore[import-not-found]
            import pytsk3  # type: ignore[import-not-found]
            from Registry import Registry  # type: ignore[import-not-found]
        except ImportError as exc:
            raise CollectionSetupError(
                "E01 collection requires libewf-python, pytsk3, and python-registry "
                "(use a supported 64-bit Python environment)"
            ) from exc

        self.pytsk3 = pytsk3
        self.Registry = Registry
        self.warnings: list[str] = []
        segments = pyewf.glob(source)
        if not segments:
            raise CollectionSetupError(f"no E01 segments found for {source}")

        class EwfImage(pytsk3.Img_Info):
            def __init__(self, names: Sequence[str]):
                self.handle = pyewf.handle()
                self.handle.open(names)
                super().__init__(url="", type=pytsk3.TSK_IMG_TYPE_EXTERNAL)

            def read(self, offset: int, size: int) -> bytes:
                self.handle.seek(offset)
                return self.handle.read(size)

            def get_size(self) -> int:
                return self.handle.get_media_size()

            def close(self) -> None:
                self.handle.close()

        try:
            self.image = EwfImage(segments)
            self.volumes, self.sector_size = self._open_filesystems()
            self._map_drives()
        except Exception:
            if hasattr(self, "image"):
                self.image.close()
            raise

        self.system_drive = self._system_drive()
        self.profiles = discover_user_profiles(self)
        self.user_profiles = tuple(profile.path for profile in self.profiles)

    def _open_filesystems(self) -> tuple[list[_ImageVolume], int]:
        volumes: list[_ImageVolume] = []
        sector_size = 512
        try:
            layout = self.pytsk3.Volume_Info(self.image)
            sector_size = int(layout.info.block_size)
            for partition in layout:
                if not (partition.flags & self.pytsk3.TSK_VS_PART_FLAG_ALLOC):
                    continue
                offset = int(partition.start) * sector_size
                try:
                    fs = self.pytsk3.FS_Info(self.image, offset=offset)
                    volumes.append(_ImageVolume(fs, offset, int(partition.start)))
                except (OSError, IOError, RuntimeError):
                    continue
        except (OSError, IOError, RuntimeError):
            pass
        if not volumes:
            try:
                volumes.append(_ImageVolume(self.pytsk3.FS_Info(self.image, offset=0), 0, 0))
            except (OSError, IOError, RuntimeError) as exc:
                raise CollectionSetupError("E01 contains no readable file system") from exc
        return volumes, sector_size

    def _open_file(self, volume: _ImageVolume, internal_path: str) -> Any:
        return volume.fs.open(path=internal_path)

    def _resolve_internal_path(self, volume: _ImageVolume, internal_path: str) -> str | None:
        current: tuple[str, ...] = ()
        for wanted in (part for part in internal_path.split("/") if part):
            parent = "/" + "/".join(current)
            actual = next(
                (name for name, _ in self._entries(volume, parent) if name.casefold() == wanted.casefold()),
                None,
            )
            if actual is None:
                return None
            current = (*current, actual)
        return "/" + "/".join(current)

    def _read_hive(self, volume: _ImageVolume, internal_path: str) -> Any | None:
        try:
            resolved = self._resolve_internal_path(volume, internal_path)
            if resolved is None:
                return None
            source = self._open_file(volume, resolved)
            size = int(source.info.meta.size)
            if size <= 0 or size > MAX_REGISTRY_HIVE:
                return None
            data = bytearray()
            for offset in range(0, size, CHUNK_SIZE):
                data.extend(source.read_random(offset, min(CHUNK_SIZE, size - offset)))
            if len(data) != size:
                return None
            return self.Registry.Registry(io.BytesIO(data))
        except Exception:
            # A damaged hive must not stop collection of other image volumes.
            return None

    def _gpt_guids(self) -> dict[int, bytes]:
        header = self.image.read(self.sector_size, self.sector_size)
        if header[:8] != b"EFI PART":
            return {}
        entry_lba = struct.unpack_from("<Q", header, 72)[0]
        count = struct.unpack_from("<I", header, 80)[0]
        entry_size = struct.unpack_from("<I", header, 84)[0]
        if not (0 < count <= 4096 and 128 <= entry_size <= 1024):
            return {}
        table = self.image.read(entry_lba * self.sector_size, count * entry_size)
        result: dict[int, bytes] = {}
        for index in range(count):
            entry = table[index * entry_size:(index + 1) * entry_size]
            if len(entry) < 40 or entry[:16] == b"\x00" * 16:
                continue
            result[struct.unpack_from("<Q", entry, 32)[0]] = entry[16:32]
        return result

    def _map_drives(self) -> None:
        gpt_guids = self._gpt_guids()
        mbr_signature = self.image.read(440, 4)
        mapped: dict[str, _ImageVolume] = {}
        for system_volume in self.volumes:
            hive = self._read_hive(system_volume, "/Windows/System32/config/SYSTEM")
            if hive is None:
                continue
            try:
                values = hive.open("MountedDevices").values()
            except Exception as exc:
                self.warnings.append(f"cannot read MountedDevices in image: {exc}")
                continue
            for value in values:
                match = re.fullmatch(r"\\DosDevices\\([A-Za-z]):", value.name(), re.I)
                if not match:
                    continue
                data = value.value()
                if not isinstance(data, bytes):
                    continue
                drive = match.group(1).upper() + ":"
                for volume in self.volumes:
                    mbr_match = (
                        len(data) == 12 and data[:4] == mbr_signature
                        and int.from_bytes(data[4:12], "little") == volume.offset
                    )
                    gpt_match = (
                        len(data) == 24 and data[:8] == b"DMIO:ID:"
                        and gpt_guids.get(volume.start_sector) == data[8:24]
                    )
                    if mbr_match or gpt_match:
                        if drive in mapped and mapped[drive] is not volume:
                            self.warnings.append(f"ambiguous E01 drive mapping for {drive}")
                            mapped.pop(drive, None)
                        elif volume.drive is None:
                            volume.drive = drive
                            mapped[drive] = volume

        # An image can lack a usable MountedDevices entry. The Windows volume
        # can still identify its own drive through an explicit SystemRoot value.
        for volume in self.volumes:
            if volume.drive is not None:
                continue
            hive = self._read_hive(volume, "/Windows/System32/config/SOFTWARE")
            if hive is not None:
                try:
                    value = hive.open(r"Microsoft\Windows NT\CurrentVersion").value("SystemRoot").value()
                    match = re.match(r"^([A-Za-z]):\\Windows(?:\\|$)", value, re.I)
                    if match:
                        volume.drive = match.group(1).upper() + ":"
                except Exception:
                    pass

        drive_groups: dict[str, list[_ImageVolume]] = {}
        for volume in self.volumes:
            if volume.drive:
                drive_groups.setdefault(volume.drive, []).append(volume)
        for drive, group in drive_groups.items():
            if len(group) > 1:
                self.warnings.append(f"ambiguous E01 drive mapping for {drive}; volumes skipped")
                for volume in group:
                    volume.drive = None

        for volume in self.volumes:
            if volume.drive is None:
                self.warnings.append(
                    f"E01 volume at byte offset {volume.offset} has no verified drive letter; skipped"
                )

    def _system_drive(self) -> str:
        for volume in self.volumes:
            if volume.drive and self._read_hive(volume, "/Windows/System32/config/SYSTEM"):
                return volume.drive
        return next((v.drive for v in self.volumes if v.drive), "C:")

    def _entries(self, volume: _ImageVolume, internal_dir: str) -> Iterator[tuple[str, bool]]:
        try:
            directory = volume.fs.open_dir(path=internal_dir)
            for entry in directory:
                if entry.info.name is None or entry.info.meta is None:
                    continue
                raw = entry.info.name.name
                name = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                if name in (".", ".."):
                    continue
                is_dir = entry.info.meta.type == self.pytsk3.TSK_FS_META_TYPE_DIR
                yield name, is_dir
        except (OSError, IOError, RuntimeError) as exc:
            self.warnings.append(f"cannot list E01 directory {internal_dir}: {exc}")

    def list_user_directories(self) -> tuple[str, ...]:
        profiles: list[str] = []
        for volume in self.volumes:
            if volume.drive:
                for name, is_dir in self._entries(volume, "/Users"):
                    if is_dir:
                        profiles.append(volume.drive + "\\Users\\" + name)
        return tuple(profiles)

    def path_exists(self, original_path: str) -> bool:
        path = PureWindowsPath(original_path)
        if not re.fullmatch(r"[A-Za-z]:", path.drive):
            return False
        for volume in self.volumes:
            if volume.drive and volume.drive.casefold() == path.drive.casefold():
                internal = self._resolve_internal_path(volume, "/" + "/".join(path.parts[1:]))
                if internal is not None:
                    try:
                        self._open_file(volume, internal)
                        return True
                    except (OSError, IOError, RuntimeError):
                        try:
                            volume.fs.open_dir(path=internal)
                            return True
                        except (OSError, IOError, RuntimeError):
                            continue
        return False

    def _walk(self, volume: _ImageVolume, parts: Sequence[str], current: tuple[str, ...] = ()) -> Iterator[Candidate]:
        if not parts:
            internal = "/" + "/".join(current)
            try:
                item = self._open_file(volume, internal)
                if item.info.meta.type == self.pytsk3.TSK_FS_META_TYPE_DIR:
                    yield from self._walk_directory(volume, current)
                else:
                    yield Candidate(volume.drive + "\\" + "\\".join(current), (volume, internal))
            except (OSError, IOError, RuntimeError):
                return
            return

        internal_dir = "/" + "/".join(current)
        wanted = parts[0].casefold()
        for name, is_dir in self._entries(volume, internal_dir):
            if not fnmatch.fnmatchcase(name.casefold(), wanted):
                continue
            if len(parts) > 1 and not is_dir:
                continue
            yield from self._walk(volume, parts[1:], (*current, name))

    def _walk_directory(self, volume: _ImageVolume, current: tuple[str, ...]) -> Iterator[Candidate]:
        if len(current) > 64:
            self.warnings.append(f"E01 directory nesting limit reached: {'/'.join(current)}")
            return
        internal_dir = "/" + "/".join(current)
        for name, is_dir in self._entries(volume, internal_dir):
            child = (*current, name)
            if is_dir:
                yield from self._walk_directory(volume, child)
            else:
                yield Candidate(volume.drive + "\\" + "\\".join(child), (volume, "/" + "/".join(child)))

    def find(self, pattern: str) -> Iterator[Candidate]:
        path = PureWindowsPath(pattern)
        if not re.fullmatch(r"[A-Za-z]:", path.drive):
            return
        for volume in self.volumes:
            if volume.drive and volume.drive.casefold() == path.drive.casefold():
                yield from self._walk(volume, path.parts[1:])

    def stat(self, candidate: Candidate) -> FileState:
        volume, internal = candidate.reference
        meta = self._open_file(volume, internal).info.meta
        return FileState(int(meta.size), int(meta.mtime) * 1_000_000_000 if meta.mtime else None)

    def chunks(self, candidate: Candidate) -> Iterator[bytes]:
        volume, internal = candidate.reference
        source = self._open_file(volume, internal)
        size = int(source.info.meta.size)
        for offset in range(0, size, CHUNK_SIZE):
            chunk = source.read_random(offset, min(CHUNK_SIZE, size - offset))
            if not chunk:
                raise OSError(f"unexpected end of E01 file: {candidate.original_path}")
            yield chunk

    def close(self) -> None:
        self.image.close()


def _candidate_key(candidate: Candidate) -> str:
    return candidate.original_path.casefold()


def _prefetch_executable(reader: LiveReader | E01Reader, candidate: Candidate) -> str:
    """Identify a Prefetch process with libSCCA before admitting it to the archive."""
    try:
        import pyscca  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OSError("Prefetch process identification requires libscca-python") from exc

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pf", delete=False) as temporary:
            temporary_path = Path(temporary.name)

            def copy_candidate() -> None:
                temporary.seek(0)
                temporary.truncate(0)
                for chunk in reader.chunks(candidate):
                    temporary.write(chunk)

            try:
                copy_candidate()
            except OSError as exc:
                if not isinstance(reader, LiveReader) or not _is_access_error(exc):
                    raise
                reader.enable_raw(candidate)
                copy_candidate()

        prefetch = pyscca.open(str(temporary_path))
        try:
            executable = prefetch.executable_filename
        finally:
            prefetch.close()
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError("Prefetch has no readable process filename")
        return PureWindowsPath(executable).name.casefold()
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def collect_artifacts(
    reader: LiveReader | E01Reader,
    output: Path,
    definitions: Sequence[ArtifactDefinition],
) -> CollectionSummary:
    """Create one archive; preserve original-to-archive mappings in its manifest."""
    archive_path = output / "collected_artifacts.zip"
    if archive_path.exists():
        raise CollectionSetupError(f"output archive already exists: {archive_path}")

    summary = CollectionSummary(archive_path=archive_path)
    definitions_by_id = {definition.id: definition for definition in definitions}
    candidates: dict[str, tuple[Candidate, set[str]]] = {}
    pattern_owners: dict[str, tuple[str, set[str]]] = {}
    matched_by_definition: dict[str, set[str]] = {item.id: set() for item in definitions}
    for definition in definitions:
        for template in definition.expected_paths:
            patterns = expand_path_pattern(template, reader.user_profiles, reader.system_drive)
            for pattern in patterns:
                key = pattern.casefold()
                if key in pattern_owners:
                    pattern_owners[key][1].add(definition.id)
                else:
                    pattern_owners[key] = (pattern, {definition.id})

    for pattern, owners in pattern_owners.values():
        try:
            found_here = list(reader.find(pattern))
        except (OSError, IOError, ValueError) as exc:
            summary.warnings.append(f"cannot search {pattern}: {exc}")
            continue
        for candidate in found_here:
            prefetch_owners = {
                item_id for item_id in owners
                if definitions_by_id[item_id].prefetch_executable
            }
            selected_owners = set(owners)
            if prefetch_owners:
                try:
                    executable = _prefetch_executable(reader, candidate)
                except (OSError, IOError, ValueError, RuntimeError) as exc:
                    summary.warnings.append(
                        f"cannot identify Prefetch process in {candidate.original_path}: {exc}"
                    )
                    selected_owners.difference_update(prefetch_owners)
                else:
                    selected_owners = {
                        item_id for item_id in selected_owners
                        if item_id not in prefetch_owners or
                        definitions_by_id[item_id].prefetch_executable.casefold() == executable
                    }
            if not selected_owners:
                continue
            key = _candidate_key(candidate)
            for definition_id in selected_owners:
                matched_by_definition[definition_id].add(key)
            if key in candidates:
                candidates[key][1].update(selected_owners)
            else:
                candidates[key] = (candidate, selected_owners)

    warned_definitions: set[tuple[tuple[str, ...], str | None]] = set()
    for definition in definitions:
        summary.per_definition[definition.id] = len(matched_by_definition[definition.id])
        warning_key = (definition.expected_paths, definition.prefetch_executable)
        if not matched_by_definition[definition.id] and warning_key not in warned_definitions:
            filter_note = (f"; Process EXE: {definition.prefetch_executable}"
                           if definition.prefetch_executable else "")
            summary.warnings.append(
                f"artifact not found: {definition.artifact} "
                f"({', '.join(definition.expected_paths)}{filter_note})"
            )
            warned_definitions.add(warning_key)

    summary.found = len(candidates)
    for _, definition_ids in candidates.values():
        # The archive stores each physical file once. Display it under every
        # artifact label whose definition matched the file (for example, one
        # NTUSER.DAT contains UserAssist and both ComDlg32 MRU keys).
        for artifact in {definitions_by_id[item_id].artifact for item_id in definition_ids}:
            summary.per_artifact[artifact] = summary.per_artifact.get(artifact, 0) + 1
    summary.warnings.extend(reader.warnings)
    output.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        raise CollectionSetupError(f"output archive already exists: {archive_path}")
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "source": {
            "mode": reader.source_mode, "path": reader.source_path,
            **({"snapshots": [snapshot.__dict__ for snapshot in reader.snapshots.values()]}
               if isinstance(reader, LiveReader) else {}),
        },
        "user_profiles": [
            {"path": profile.path, "artifact_presence": dict(profile.artifact_presence)}
            for profile in getattr(reader, "profiles", ())
        ],
        "files": [],
        "warnings": summary.warnings,
    }
    staged_archive = archive_path.with_name(archive_path.name + ".partial")
    try:
        with zipfile.ZipFile(staged_archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for candidate, definition_ids in candidates.values():
                record: dict[str, Any] = {
                    "source_path": candidate.original_path,
                    "collected_path": None,
                    "definition_ids": sorted(definition_ids),
                    "status": "failed",
                    "status_before": "not_checked",
                    "status_after": "not_checked",
                    "size_before": None,
                    "size_after": None,
                    "mtime_ns_before": None,
                    "mtime_ns_after": None,
                    "collected_size": None,
                    "sha256": None,
                    "collection_method": None,
                }
                manifest["files"].append(record)
                try:
                    member = zip_path_for_windows_path(candidate.original_path)
                    before = reader.stat(candidate)
                    record["size_before"] = before.size
                    record["mtime_ns_before"] = before.mtime_ns
                    record["status_before"] = "ok"
                    with tempfile.SpooledTemporaryFile(max_size=8 * CHUNK_SIZE) as staged_file:
                        def stage_source() -> tuple[int, Any]:
                            staged_file.seek(0)
                            staged_file.truncate(0)
                            digest = hashlib.sha256()
                            copied = 0
                            for chunk in reader.chunks(candidate):
                                staged_file.write(chunk)
                                digest.update(chunk)
                                copied += len(chunk)
                            return copied, digest

                        try:
                            copied, digest = stage_source()
                        except OSError as exc:
                            if not isinstance(reader, LiveReader) or not _is_access_error(exc):
                                raise
                            try:
                                reader.enable_raw(candidate)
                                before = reader.stat(candidate)
                                record["size_before"] = before.size
                                record["mtime_ns_before"] = before.mtime_ns
                                copied, digest = stage_source()
                            except OSError as raw_exc:
                                raise OSError(
                                    f"regular read failed: {exc}; raw NTFS retry failed: {raw_exc}"
                                ) from raw_exc
                        after = reader.stat(candidate)
                        record["size_after"] = after.size
                        record["mtime_ns_after"] = after.mtime_ns
                        record["status_after"] = "ok"
                        if copied != before.size or copied != after.size or before.mtime_ns != after.mtime_ns:
                            record["status_after"] = "changed"
                            raise OSError("source size or modification state changed during collection")
                        staged_file.seek(0)
                        with archive.open(member, "w", force_zip64=True) as destination:
                            while chunk := staged_file.read(CHUNK_SIZE):
                                destination.write(chunk)
                    stored = archive.getinfo(member)
                    if stored.file_size != copied:
                        raise OSError("archive member size does not match collected file size")
                    record.update({
                        "collected_path": member,
                        "collected_size": copied,
                        "sha256": digest.hexdigest(),
                        "status": "ok",
                    })
                    record["collection_method"] = (
                        reader.collection_method(candidate) if isinstance(reader, LiveReader) else "e01"
                    )
                    summary.extracted += 1
                except (OSError, IOError, ValueError, RuntimeError) as exc:
                    if isinstance(reader, LiveReader):
                        record["collection_method"] = reader.collection_method(candidate)
                    record["error"] = str(exc)
                    summary.failures += 1
                    summary.warnings.append(f"cannot collect {candidate.original_path}: {exc}")
            manifest["warnings"] = summary.warnings
            archive.writestr("_collection_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        os.replace(staged_archive, archive_path)
    except Exception:
        staged_archive.unlink(missing_ok=True)
        raise
    return summary
