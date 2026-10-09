"""사용자·Chrome 프로필을 찾아 아티팩트를 작업 폴더로 복사하고 ZIP으로 묶는다.

복사본은 원래 경로 구조를 그대로 따른다.
  증거 경로  C:\\Users\\a\\AppData\\Local\\Google\\Chrome\\User Data\\Default\\History
  작업 폴더  <작업 폴더>\\C\\Users\\a\\...\\History
  ZIP 안     C/Users/a/.../History   (ZIP 명세상 구분자는 /)
파서는 원본이 아니라 이 복사본만 읽는다.
"""
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

from definitions import ARTIFACT_DEFS, CHROME_USER_DATA


@dataclass
class Unit:
    """파싱 단위 하나 = parsed_artifacts.json의 artifacts[i] 하나"""
    definition: str
    parser: str
    user: str
    profile: str
    evidence_path: str          # 경로 열에 쓰는 증거 기준 절대 경로 (파일 또는 폴더)
    zip_path: str
    local_path: str             # 작업 폴더 안 복사본 경로
    extra_local_paths: list = field(default_factory=list)
    files: list = field(default_factory=list)       # 수집한 파일 하나하나의 정보
    collection_status: str = "ok"
    collection_note: str = ""
    parse_status: str = "not_parsed"
    parse_note: str = ""
    records: list = field(default_factory=list)

    @property
    def name(self):
        """아티팩트 열 = 경로의 마지막 조각"""
        return PureWindowsPath(self.evidence_path).name


@dataclass
class CollectResult:
    units: list
    per_def: dict               # 정의 이름 -> {"found": n, "extracted": n, "failed": n}
    profiles: list              # [(사용자, 프로필 이름, 프로필 폴더 경로)]
    skipped: list               # [Skip] 읽다가 오류가 나서 들어가 보지 못한 곳
    users: list = field(default_factory=list)    # [(사용자, User Data 경로)] Chrome이 있는 사용자


@dataclass
class Skip:
    """읽기 오류로 건너뛴 곳. definition이 None이면 그 범위의 모든 아티팩트에 해당한다.
    user/profile이 None이면 각각 '모든 사용자'/'그 사용자의 모든 프로필'에 해당한다."""
    path: str
    reason: str
    definition: str = None
    user: str = None
    profile: str = None


def _join(*parts):
    return str(PureWindowsPath(*parts))


def _zip_path(win_path):
    p = PureWindowsPath(win_path)
    return "/".join([p.drive.rstrip(":")] + list(p.parts[1:]))


def _local_path(workdir, win_path):
    p = PureWindowsPath(win_path)
    return os.path.join(workdir, p.drive.rstrip(":"), *p.parts[1:])


def _err(e):
    return "%s: %s" % (type(e).__name__, e)


def find_profiles(source):
    """[(사용자 이름, User Data 경로, [프로필 폴더 이름])], [Skip]

    프로필 폴더 = User Data 아래에서 Preferences 파일이 있는 폴더 (Default, Profile 1, Guest Profile ...)
    읽기 오류는 '없음'으로 치지 않고 Skip으로 남긴다. 프로필은 하나씩 확인해서
    한 프로필의 오류가 같은 사용자의 다른 프로필까지 빼지 않게 한다.
    """
    users_dir = _join(source.drive + "\\", "Users")
    found, skipped = [], []
    try:
        users = source.list_dir(users_dir)
    except OSError as e:
        return [], [Skip(users_dir, _err(e))]
    for name, is_dir in users:
        if not is_dir:
            continue
        user_data = _join(users_dir, name, CHROME_USER_DATA)
        try:
            source.list_dir(_join(users_dir, name))     # 권한이 없으면 여기서 오류가 나서 '건너뜀'으로 남는다
            if not source.is_dir(user_data):
                continue
            entries = source.list_dir(user_data)
        except OSError as e:
            skipped.append(Skip(_join(users_dir, name), _err(e), user=name))
            continue
        profiles = []
        for p, d in entries:
            if not d:
                continue
            try:
                if source.is_file(_join(user_data, p, "Preferences")):
                    profiles.append(p)
            except OSError as e:
                skipped.append(Skip(_join(user_data, p), _err(e), user=name, profile=p))
        found.append((name, user_data, profiles))
    return found, skipped


def _list_files(source, folder):
    """폴더 아래 모든 파일 경로 (하위 폴더 포함)"""
    out = []
    for name, is_dir in source.list_dir(folder):
        p = _join(folder, name)
        if is_dir:
            out.extend(_list_files(source, p))
        else:
            out.append(p)
    return out


def _copy(source, workdir, win_path):
    info = {"path": win_path, "zip_path": _zip_path(win_path)}
    try:
        info.update(source.copy_to(win_path, _local_path(workdir, win_path)))
        info["status"] = "ok"
    except Exception as e:
        info["status"] = "error"
        info["error"] = "%s: %s" % (type(e).__name__, e)
    return info


def _make_unit(adef, user, profile, main_path, workdir):
    return Unit(definition=adef.name, parser=adef.parser, user=user, profile=profile,
                evidence_path=main_path, zip_path=_zip_path(main_path),
                local_path=_local_path(workdir, main_path))


def _finish(unit, main_is_first):
    """main_is_first: 파일 하나짜리 아티팩트면 files[0]이 주 파일이다.
    주 파일을 못 읽으면 짝 파일(-journal 등)을 읽었어도 수집 실패로 본다."""
    failed = [f for f in unit.files if f["status"] != "ok"]
    if not unit.files:
        unit.collection_status, unit.collection_note = "ok", "빈 폴더"
    elif main_is_first and unit.files[0]["status"] != "ok":
        unit.collection_status = "error"
        unit.collection_note = unit.files[0]["error"]
    elif len(failed) == len(unit.files):
        unit.collection_status = "error"
        unit.collection_note = failed[0]["error"]
    elif failed:
        unit.collection_status = "partial"
        unit.collection_note = "파일 %d개 중 %d개 복사 실패" % (len(unit.files), len(failed))


def _is_file_or_error(source, path):
    """짝 파일(-journal 등) 확인용: 읽기 오류가 나면 일단 있다고 보고 복사를 시도해 오류를 기록한다."""
    try:
        return source.is_file(path)
    except OSError:
        return True


def collect(source, workdir, on_definition=None):
    """정의 순서대로 수집한다. on_definition(번호, 정의, 개수 정보)는 콘솔 출력용."""
    found, skipped = find_profiles(source)
    units, per_def, profiles = [], {}, []
    for user, user_data, profs in found:
        for p in profs:
            profiles.append((user, p, _join(user_data, p)))

    for i, adef in enumerate(ARTIFACT_DEFS, 1):
        counts = {"found": 0, "extracted": 0, "failed": 0}
        # 이 정의를 찾아볼 위치: (사용자, 프로필 이름, 기준 폴더)
        bases = []
        for user, user_data, profs in found:
            if adef.base == "user_data":
                bases.append((user, "", user_data))
            else:
                bases.extend((user, p, _join(user_data, p)) for p in profs)

        for user, profile, base in bases:
            main = _join(base, adef.rel)
            try:
                if adef.kind == "file":
                    if not source.is_file(main):
                        continue
                    u = _make_unit(adef, user, profile, main, workdir)
                    paths = [main] + [_join(base, x) for x in adef.extras if _is_file_or_error(source, _join(base, x))]
                    u.files = [_copy(source, workdir, p) for p in paths]
                    new_units = [u]
                elif adef.kind == "folder":
                    if not source.is_dir(main):
                        continue
                    u = _make_unit(adef, user, profile, main, workdir)
                    paths = _list_files(source, main)
                    for x in adef.extras:
                        extra = _join(base, x)
                        if source.is_dir(extra):
                            u.extra_local_paths.append(_local_path(workdir, extra))
                            paths += _list_files(source, extra)
                    u.files = [_copy(source, workdir, p) for p in paths]
                    new_units = [u]
                else:  # "files": 폴더 안 파일 하나하나가 아티팩트
                    if not source.is_dir(main):
                        continue
                    new_units = []
                    for name, is_dir in source.list_dir(main):
                        if is_dir:
                            continue
                        p = _join(main, name)
                        u = _make_unit(adef, user, profile, p, workdir)
                        u.files = [_copy(source, workdir, p)]
                        new_units.append(u)
            except OSError as e:
                skipped.append(Skip(main, _err(e), adef.name, user, profile))
                continue
            for u in new_units:
                _finish(u, adef.kind != "folder")
                counts["found"] += len(u.files)
                counts["failed"] += sum(1 for f in u.files if f["status"] != "ok")
                counts["extracted"] += sum(1 for f in u.files if f["status"] == "ok")
                units.append(u)
        per_def[adef.name] = counts
        if on_definition:
            on_definition(i, adef, counts)

    # JSON의 artifacts 순서를 경로 기준으로 고정한다 (같은 입력 -> 같은 근거 경로)
    units.sort(key=lambda u: u.evidence_path.lower())
    return CollectResult(units, per_def, profiles, skipped, [(u, ud) for u, ud, _ in found])


def make_zip(units, workdir, zip_path):
    """복사에 성공한 파일을 원래 경로 구조로 ZIP에 넣는다."""
    entries = sorted({f["zip_path"] for u in units for f in u.files if f["status"] == "ok"})
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for arc in entries:
            local = os.path.join(workdir, *arc.split("/"))
            z.write(local, arc)
    return len(entries)
