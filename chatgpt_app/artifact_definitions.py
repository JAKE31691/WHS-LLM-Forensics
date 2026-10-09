"""User-provided ChatGPT Desktop inventory; descriptions are not parser-verified."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Literal, Sequence


Grade = Literal["직접", "보조"]

# KapeFiles Targets/Windows/{RegistryHivesSystem,RegistryHivesUser,Amcache}.tkape:
# collect these transaction logs alongside the registered hive files.
REGISTRY_LOG_MASKS = {
    "SOFTWARE": "SOFTWARE.LOG*",
    "NTUSER.DAT": "NTUSER.DAT.LOG*",
    "AMCACHE.HVE": "Amcache.hve.LOG*",
}


@dataclass(frozen=True)
class CollectionTarget:
    """KAPE-style location, filename mask, and recursion for one source path."""

    path: str
    file_mask: str
    recursive: bool = False

    @property
    def search_pattern(self) -> str:
        return self.path if self.recursive else str(PureWindowsPath(self.path) / self.file_mask)


@dataclass(frozen=True)
class ArtifactDefinition:
    id: str
    behavior: str
    artifact: str
    expected_paths: tuple[str, ...]
    artifact_type: str
    grade: Grade
    description: str
    registry_key: str | None = None
    prefetch_executable: str | None = None

    def collection_targets(
        self, user_profiles: Sequence[str], system_drive: str = "C:"
    ) -> tuple[CollectionTarget, ...]:
        """Resolve this definition to narrow target searches on Live or E01."""
        targets: list[CollectionTarget] = []
        for template in self.expected_paths:
            for expanded in expand_path_pattern(template, user_profiles, system_drive):
                if self.artifact_type == "directory" or expanded.endswith("\\"):
                    target = CollectionTarget(expanded.rstrip("\\"), "*", True)
                else:
                    file_path = PureWindowsPath(expanded)
                    target = CollectionTarget(str(file_path.parent), file_path.name)
                if target not in targets:
                    targets.append(target)
        return tuple(targets)

    def companion_targets(
        self, user_profiles: Sequence[str], system_drive: str = "C:"
    ) -> tuple[CollectionTarget, ...]:
        """Registry transaction logs paired with registered hives in KAPE targets."""
        if self.artifact_type not in {"registry_hive", "registry_key"}:
            return ()
        return tuple(CollectionTarget(target.path, REGISTRY_LOG_MASKS[target.file_mask.upper()])
                     for target in self.collection_targets(user_profiles, system_drive)
                     if not target.recursive and target.file_mask.upper() in REGISTRY_LOG_MASKS)


def _d(
    id: str,
    behavior: str,
    artifact: str,
    paths: tuple[str, ...],
    artifact_type: str,
    grade: Grade,
    description: str,
    registry_key: str | None = None,
    prefetch_executable: str | None = None,
) -> ArtifactDefinition:
    # SQLite sidecars are part of the registered database artifact. Register
    # them explicitly so the collector never reaches beyond definition paths.
    if artifact_type in {"sqlite", "chromium_history", "chromium_cookies"}:
        paths = tuple(dict.fromkeys(
            path for base in paths for path in (base, base + "-wal", base + "-shm")
        ))
    return ArtifactDefinition(id, behavior, artifact, paths, artifact_type, grade, description,
                              registry_key, prefetch_executable)


SYSTEM = r"%SYSTEMDRIVE%\Windows"
CODEX = r"%USERPROFILE%\.codex"
PACKAGE = (
    r"%LOCALAPPDATA%\Packages\OpenAI.Codex_2p2nqsd0c76g0"
    r"\LocalCache"
)
DESKTOP_LOGS = (
    PACKAGE + r"\Local\Codex\Logs\codex-desktop-*.log",
    PACKAGE + r"\Local\Codex\Logs\*\*\*\codex-desktop-*.log",
)
DESKTOP_PROFILE = PACKAGE + r"\Roaming\Codex\web\Codex\Default"
CACHE_DATA = DESKTOP_PROFILE + r"\Cache\Cache_Data"
CHROME_CACHE = r"C:\Users\*\AppData\Local\Google\Chrome\User Data\*\Cache\Cache_Data"
CHROME_HISTORY = r"C:\Users\*\AppData\Local\Google\Chrome\User Data\*\History"
CHROME_COOKIES = r"C:\Users\*\AppData\Local\Google\Chrome\User Data\*\Network\Cookies"
CODEX_DB = CODEX + r"\sqlite\codex-dev.db"
GLOBAL_STATE = CODEX + r"\.codex-global-state.json"
AUTH = CODEX + r"\auth.json"
NTUSER = r"%USERPROFILE%\NTUSER.DAT"
MFT = r"%SYSTEMDRIVE%\$MFT"
# The Prefetch filename contains the executable name and a path hash. Narrow
# discovery to ChatGPT candidates, then verify the process name with libSCCA.
PREFETCH_FILES = SYSTEM + r"\Prefetch\*CHATGPT*.pf"
MRU_OPEN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\ComDlg32\OpenSavePidlMRU"
MRU_VISITED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\ComDlg32\LastVisitedPidlMRU"


# One definition represents one user-provided behavior/artifact/grade combination.
# Expected paths are discovery patterns, not claims that a file or event exists.
ARTIFACT_DEFINITIONS: tuple[ArtifactDefinition, ...] = (
    _d("install_prefetch", "설치", "Prefetch (ChatGPT installer.exe)", (PREFETCH_FILES,), "prefetch", "직접", "ChatGPT 설치 프로그램 실행 흔적", prefetch_executable="CHATGPT INSTALLER.EXE"),
    _d("install_appx", "설치", "AppX", (SYSTEM + r"\System32\config\SOFTWARE",), "registry_hive", "직접", "PackageState의 마지막 기록 시각", r"Microsoft\Windows\CurrentVersion\Appx\PackageState\S-1-5-21-*\OpenAI.Codex_*"),

    _d("run_prefetch", "실행", "Prefetch", (PREFETCH_FILES,), "prefetch", "직접", "모든 실행 시간·횟수와 백그라운드 활성 기록", prefetch_executable="CHATGPT.EXE"),
    _d("run_userassist", "실행", "UserAssist", (NTUSER,), "registry_key", "직접", "program_name에 ChatGPT.exe가 포함된 경우 실행 횟수·값 데이터 오프셋·last_executed_kst", r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"),
    _d("run_global_state", "실행", ".codex-global-state.json", (GLOBAL_STATE,), "json", "보조", "앱 최초 실행 시각 관련 상태"),
    _d("run_amcache", "실행", "AmCache InventoryApplicationFile", (SYSTEM + r"\AppCompat\Programs\Amcache.hve",), "registry_hive", "보조", "마지막 실행 시간", r"Root\InventoryApplicationFile"),

    _d("login_auth", "로그인", "auth.json", (AUTH,), "json", "직접", "id_token과 access_token이 모두 조회되는 경우"),
    _d("login_codex_db", "로그인", "codex-dev.db", (CODEX_DB,), "sqlite", "보조", "local_thread_catalog_hosts의 host_id가 조회되는 경우"),
    _d("login_history", "로그인", "Chrome History", (CHROME_HISTORY,), "chromium_history", "직접", "localhost 인증 콜백과 ChatGPT Codex open-app 로그인 URL이 모두 포함된 방문 기록"),
    _d("login_cache", "로그인", "Cache_Data", (CACHE_DATA,), "chromium_cache", "보조", "account.created_time별 로그인 기록"),

    _d("logout_auth", "로그아웃", "auth.json", (AUTH,), "json", "보조", ".codex는 있으나 auth.json이 없는 상태"),

    _d("switch_codex_db", "다중 계정", "codex-dev.db", (CODEX_DB,), "sqlite", "직접", "local_thread_catalog_hosts에 서로 다른 host_id가 2개 이상이면 각 host_id를 근거에 표시"),

    _d("chat_codex_db", "새 대화 생성", "codex-dev.db", (CODEX_DB,), "sqlite", "직접", "local_thread_catalog의 thread_id별 행"),
    _d("chat_cache", "새 대화 생성", "Cache_Data", (CACHE_DATA,), "chromium_cache", "직접", "conversation API 캐시의 title·conversation_id·create_time 또는 items의 연속 id·title·create_time; ID별 행"),
    _d("chat_global_state", "새 대화 생성", ".codex-global-state.json", (GLOBAL_STATE,), "json", "보조", "prompt_history 또는 client-thread-bindings-v1 포함"),

    _d("prompt_input_cache", "프롬프트 입력", "Cache_Data", (CACHE_DATA,), "chromium_cache", "직접", "conversation_id별 author.role=user, update_time, content 기록"),

    _d("prompt_edit_cache", "프롬프트 수정", "Cache_Data", (CACHE_DATA,), "chromium_cache", "직접", "create_time과 복수의 children ID"),

    _d("memory_cache", "메모리 기능 사용", "Cache_Data", (CACHE_DATA,), "chromium_cache", "직접", "memcite 또는 memory_percentage_used"),

    _d("upload_cache", "파일 업로드", "Cache_Data", (CACHE_DATA,), "chromium_cache", "직접", "message.metadata.attachments와 message.update_time"),
    _d("upload_open_mru", "파일 업로드", "OpenSavePidlMRU", (NTUSER,), "registry_key", "보조", "파일 선택 대화상자의 파일명·경로", MRU_OPEN_KEY),
    _d("upload_visited_mru", "파일 업로드", "LastVisitedPidlMRU", (NTUSER,), "registry_key", "보조", "ChatGPT.exe의 마지막 방문 위치", MRU_VISITED_KEY),

    _d("download_mft", "파일 다운로드", "$MFT", (MFT,), "ntfs_metadata", "보조", "로컬 파일 저장 경로·생성 시각"),
    _d("download_cache", "파일 다운로드", "Cache_Data", (CACHE_DATA,), "chromium_cache", "보조", "파일명·내용·시각 등 파일 정보와 메타데이터·처리 과정"),
    _d("download_open_mru", "파일 다운로드", "OpenSavePidlMRU", (NTUSER,), "registry_key", "보조", "저장 대화상자의 파일명·경로", MRU_OPEN_KEY),
    _d("download_visited_mru", "파일 다운로드", "LastVisitedPidlMRU", (NTUSER,), "registry_key", "보조", "ChatGPT.exe의 마지막 저장 위치", MRU_VISITED_KEY),

    _d("account_delete_cache", "계정 삭제", "Chrome Cache", (CHROME_CACHE,), "chromium_cache", "직접", "auth/logout의 계정 삭제 사유와 출처"),

    _d("temporary_chat_cache", "임시 채팅", "Cache_Data", (CACHE_DATA,), "chromium_cache", "보조", "New Chat 제목과 사용자 프롬프트·AI 응답 잔존"),

    _d("connector_history", "플러그인 사용", "Chrome History", (CHROME_HISTORY,), "chromium_history", "보조", "connector_platform_oauth_redirect?state=codex_scheme__oauth URL과 last_visit_time"),
    _d("connector_log", "플러그인 사용", "codex-desktop-*.log", DESKTOP_LOGS, "log", "보조", "OAuth·플러그인 설치 시각"),
    _d("connector_package", "플러그인 사용", "openai-curated-remote", (CODEX + r"\plugins\cache\openai-curated-remote",), "directory", "보조", "설치 플러그인 종류·설치 시각·최근 사용 시각·버전 관련 파일"),
    _d("connector_global_state", "플러그인 사용", ".codex-global-state.json", (GLOBAL_STATE,), "json", "직접", "tools에 기록된 플러그인 설치·사용 종류와 관련 대화 ID"),
    _d("connector_work_logs", "플러그인 사용", "logs_2.sqlite", (CODEX + r"\logs_2.sqlite", CODEX + r"\sqlite\logs_2.sqlite"), "sqlite", "직접", "log/logs.feedback_log_body의 @openai-curated-remote)와 ts"),
    _d("connector_rollout", "플러그인 사용", "rollout-*.jsonl", (CODEX + r"\sessions\*\*\*\rollout-*.jsonl",), "jsonl", "직접", "실제 MCP 도구 호출의 @openai-curated-remote) 매칭 키 값과 timestamp"),
    _d("connector_thread_history", "플러그인 사용", "thread_history_1.sqlite", (CODEX + r"\thread_history_1.sqlite",), "sqlite", "직접", "item_json의 실제 MCP 도구 호출에서 @openai-curated-remote) 매칭 키 값과 created_at_ms"),

    _d("work_logs", "비고 - Codex/ ChatGPT Work 관련", "logs_2.sqlite", (CODEX + r"\logs_2.sqlite", CODEX + r"\sqlite\logs_2.sqlite"), "sqlite", "보조", "실행 로그의 thread ID·cwd·도구·프롬프트 등"),
    _d("work_session_index", "비고 - Codex/ ChatGPT Work 관련", "session_index.jsonl", (CODEX + r"\session_index.jsonl",), "jsonl", "보조", "thread ID·대화 제목·수정 시각 색인"),
    _d("work_state", "비고 - Codex/ ChatGPT Work 관련", "state_5.sqlite", (CODEX + r"\state_5.sqlite", CODEX + r"\sqlite\state_5.sqlite"), "sqlite", "보조", "thread·session 메타데이터와 모델·환경·rollout 경로"),
    _d("work_rollout", "비고 - Codex/ ChatGPT Work 관련", "rollout-날짜-thread_id.jsonl", (CODEX + r"\sessions\*\*\*\rollout-*.jsonl",), "jsonl", "직접", "thread의 사용자·에이전트 메시지와 대화·작업 기록"),
    _d("work_thread_history", "비고 - Codex/ ChatGPT Work 관련", "thread_history_1.sqlite", (CODEX + r"\thread_history_1.sqlite",), "sqlite", "직접", "rollout에서 추출한 thread와 대화·작업 기록"),
    _d("work_memories", "비고 - Codex/ ChatGPT Work 관련", "memories 파일", (CODEX + r"\memories",), "directory", "직접", "raw memory와 memory summary 등 저장된 기록"),
)


def expand_path_pattern(
    pattern: str,
    user_profiles: Sequence[str],
    system_drive: str = "C:",
) -> tuple[str, ...]:
    """Expand user tokens without accessing a live system or E01 image.

    Both source readers can pass their discovered Windows profile paths here.
    Other wildcards, such as Profile * and rollout-*, remain discovery patterns.
    """
    drive = system_drive.rstrip("\\")
    if not re.fullmatch(r"[A-Za-z]:", drive):
        raise ValueError("system_drive must be a Windows drive letter, such as C:")
    base = pattern.replace("%SYSTEMDRIVE%", drive)
    needs_profile = any(token in base for token in ("%USERPROFILE%", "%LOCALAPPDATA%", "<사용자명>"))
    needs_profile = needs_profile or bool(re.match(r"^[A-Za-z]:\\Users\\\*\\", base, re.I))
    if not needs_profile:
        return (base,)

    expanded: list[str] = []
    for profile_text in user_profiles:
        profile = str(PureWindowsPath(profile_text))
        name = PureWindowsPath(profile).name
        path = base.replace("%USERPROFILE%", profile)
        path = path.replace("%LOCALAPPDATA%", profile + r"\AppData\Local")
        path = re.sub(r"^[A-Za-z]:\\Users\\<사용자명>(?=\\|$)", lambda _: profile, path, flags=re.I)
        path = path.replace("<사용자명>", name)
        path = re.sub(r"^[A-Za-z]:\\Users\\\*(?=\\|$)", lambda _: profile, path, flags=re.I)
        if path not in expanded:
            expanded.append(path)
    return tuple(expanded)


def validate_definitions() -> None:
    ids: set[str] = set()
    for definition in ARTIFACT_DEFINITIONS:
        if definition.id in ids:
            raise ValueError(f"duplicate artifact definition ID: {definition.id}")
        ids.add(definition.id)
        if not all((definition.behavior, definition.artifact, definition.artifact_type, definition.description)):
            raise ValueError(f"incomplete artifact definition: {definition.id}")
        if not definition.expected_paths:
            raise ValueError(f"missing expected path: {definition.id}")
        if definition.grade not in ("직접", "보조"):
            raise ValueError(f"invalid grade: {definition.id}")
        if definition.prefetch_executable and definition.artifact_type != "prefetch":
            raise ValueError(f"Prefetch executable filter on non-Prefetch artifact: {definition.id}")
