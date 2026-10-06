"""User-provided ChatGPT Desktop inventory; descriptions are not parser-verified."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Literal, Sequence


Grade = Literal["직접", "보조"]


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
LOCAL_STORAGE = DESKTOP_PROFILE + r"\Local Storage\leveldb"
SESSION_STORAGE = DESKTOP_PROFILE + r"\Session Storage"
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
    _d("install_appx", "설치", "AppX", (SYSTEM + r"\System32\config\SOFTWARE",), "registry_hive", "직접", "AppX 설치 완료 시간 관련 기록", r"Microsoft\Windows\CurrentVersion\Appx\AppxAllUserStore\S-1-5-21-*\OpenAI.Codex_*"),

    _d("run_prefetch", "실행", "Prefetch", (PREFETCH_FILES,), "prefetch", "직접", "모든 실행 시간·횟수와 백그라운드 활성 기록", prefetch_executable="CHATGPT.EXE"),
    _d("run_userassist", "실행", "UserAssist", (NTUSER,), "registry_key", "직접", "GUI를 통한 실행 시각", r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"),
    _d("run_global_state", "실행", ".codex-global-state.json", (GLOBAL_STATE,), "json", "보조", "앱 최초 실행 시각 관련 상태"),
    _d("run_amcache", "실행", "AmCache InventoryApplicationFile", (SYSTEM + r"\AppCompat\Programs\Amcache.hve",), "registry_hive", "보조", "마지막 실행 시간"),
    _d("run_appcompat", "실행", "AppCompatCache", (SYSTEM + r"\System32\config\SYSTEM",), "registry_key", "보조", "Prefetch와 비슷한 기록 및 최초 실행 시간", r"ControlSet*\Control\Session Manager\AppCompatCache"),

    _d("login_auth", "로그인(인증)", "auth.json", (AUTH,), "json", "직접", "사용자·계정 정보와 인증 요청·완료 및 토큰 발급 흐름"),
    _d("login_codex_db", "로그인(인증)", "codex-dev.db", (CODEX_DB,), "sqlite", "보조", "로그인 계정의 로컬 상태와 계정 ID"),
    _d("login_cookies", "로그인(인증)", "Chrome Cookies", (CHROME_COOKIES,), "chromium_cookies", "보조", "인증 세션과 ChatGPT 접속·상태 동기화 정황"),
    _d("login_history", "로그인(인증)", "Chrome History", (CHROME_HISTORY,), "chromium_history", "직접", "URL을 통한 로그인·인증 과정"),
    _d("login_cache", "로그인(인증)", "Cache_Data", (CACHE_DATA,), "chromium_cache", "보조", "user- 문자열이 남은 캐시 파일"),

    _d("switch_codex_db", "계정 전환", "codex-dev.db", (CODEX_DB,), "sqlite", "직접", "데스크톱 내부 계정과 계정별 대화 데이터의 연결"),
    _d("switch_cookies", "계정 전환", "Chrome Cookies", (CHROME_COOKIES,), "chromium_cookies", "직접", "계정 전환에 수반되는 인증 세션 기록"),
    _d("switch_history", "계정 전환", "Chrome History", (CHROME_HISTORY,), "chromium_history", "직접", "계정 전환에 수반되는 로그인·인증 URL 기록"),

    _d("chat_codex_db", "새 대화 생성", "codex-dev.db", (CODEX_DB,), "sqlite", "직접", "local_thread_catalog의 thread ID와 생성 시각"),
    _d("chat_desktop_log", "새 대화 생성", "codex-desktop-*.log", DESKTOP_LOGS, "log", "보조", "thread ID와 대화 세션 생성·서버 동기화 기록"),
    _d("chat_cache", "새 대화 생성", "Cache_Data", (CACHE_DATA,), "chromium_cache", "보조", "사용자 프롬프트·서버 응답 등이 남을 수 있는 캐시"),
    _d("chat_global_state", "새 대화 생성", ".codex-global-state.json", (GLOBAL_STATE,), "json", "보조", "일부 conversation ID와 프롬프트 내용의 연결"),

    _d("prompt_edit_cache", "프롬프트 수정", "Cache_Data", (CACHE_DATA,), "chromium_cache", "보조", "프롬프트 수정 관련 캐시 기록"),

    _d("memory_cache", "메모리 사용 / 관리", "Cache_Data", (CACHE_DATA,), "chromium_cache", "보조", "메모리 사용·관리 관련 캐시 기록"),

    _d("upload_cache", "파일 업로드", "Cache_Data", (CACHE_DATA,), "chromium_cache", "보조", "업로드 파일명·MIME 유형·내용·시각·처리 과정 관련 기록"),
    _d("upload_open_mru", "파일 업로드", "OpenSavePidlMRU", (NTUSER,), "registry_key", "직접", "파일 선택 대화상자의 파일명·경로", MRU_OPEN_KEY),
    _d("upload_visited_mru", "파일 업로드", "LastVisitedPidlMRU", (NTUSER,), "registry_key", "직접", "파일 선택 대화상자를 사용한 앱과 마지막 방문 위치", MRU_VISITED_KEY),

    _d("download_mft", "생성물/파일 다운로드", "$MFT", (MFT,), "ntfs_metadata", "보조", "로컬 파일 저장 경로·생성 시각"),
    _d("download_cache", "생성물/파일 다운로드", "Cache_Data", (CACHE_DATA,), "chromium_cache", "보조", "파일명·내용·시각 등 파일 정보와 메타데이터·처리 과정"),
    _d("download_open_mru", "생성물/파일 다운로드", "OpenSavePidlMRU", (NTUSER,), "registry_key", "직접", "저장 대화상자의 파일명·경로", MRU_OPEN_KEY),
    _d("download_visited_mru", "생성물/파일 다운로드", "LastVisitedPidlMRU", (NTUSER,), "registry_key", "직접", "저장 대화상자를 사용한 앱과 마지막 방문 위치", MRU_VISITED_KEY),

    _d("deleted_global_state", "대화 삭제", ".codex-global-state.json", (GLOBAL_STATE,), "json", "보조", "prompt-history·thread-description의 대화 일부 잔존"),
    _d("deleted_cache", "대화 삭제", "Cache_Data", (CACHE_DATA,), "chromium_cache", "보조", "삭제된 대화 기록이 캐시에 남은 정황"),
    _d("deleted_account_global_state", "계정 삭제", ".codex-global-state.json", (GLOBAL_STATE,), "json", "보조", "계정 정보 잔존"),
    _d("deleted_auth", "계정 삭제", "auth.json", (AUTH,), "json", "보조", "계정 정보 잔존"),
    _d("conversation_delete_cache", "대화 삭제", "Cache_Data", (CACHE_DATA,), "chromium_cache", "보조", "활성 대화 목록 상태"),
    _d("conversation_delete_local_storage", "대화 삭제", "Local Storage", (LOCAL_STORAGE,), "leveldb", "보조", "대화 삭제 관련 수집 대상"),
    _d("conversation_delete_session_storage", "대화 삭제", "Session Storage", (SESSION_STORAGE,), "session_storage", "보조", "대화 삭제 관련 수집 대상"),
    _d("conversation_delete_cookies", "대화 삭제", "Cookies", (DESKTOP_PROFILE + r"\Network\Cookies", CHROME_COOKIES), "chromium_cookies", "보조", "대화 삭제 관련 수집 대상"),
    _d("account_delete_cache", "계정 삭제", "Cache_Data", (CACHE_DATA,), "chromium_cache", "보조", "계정 삭제에 따른 로그아웃 사유 관련 기록"),
    _d("account_delete_local_storage", "계정 삭제", "Local Storage", (LOCAL_STORAGE,), "leveldb", "보조", "계정 전환 세션과 모델·고정·기록 캐시 정리 상태"),
    _d("account_delete_session_storage", "계정 삭제", "Session Storage", (SESSION_STORAGE,), "session_storage", "보조", "계정 삭제 확인 절차 진입 관련 상태"),
    _d("account_delete_cookies", "계정 삭제", "Cookies", (DESKTOP_PROFILE + r"\Network\Cookies", CHROME_COOKIES), "chromium_cookies", "보조", "인증 세션 종료·로그인 상태 제거 관련 기록"),

    _d("temporary_chat_cache", "임시 채팅", "Cache_Data", (CACHE_DATA,), "chromium_cache", "보조", "New Chat 제목과 사용자 프롬프트·AI 응답 잔존"),

    _d("connector_history", "외부 서비스 연동(connector / plugin)", "Chrome History", (CHROME_HISTORY,), "chromium_history", "보조", "OAuth 링크 이후 ChatGPT plugin 사이트 접속 기록"),
    _d("connector_log", "외부 서비스 연동(connector / plugin)", "codex-desktop-*.log", DESKTOP_LOGS, "log", "보조", "OAuth·플러그인 설치 시각"),
    _d("connector_config", "외부 서비스 연동(connector / plugin)", "config.toml", (CODEX + r"\config.toml",), "toml", "보조", "플러그인 권한·활성화 설정"),
    _d("connector_package", "외부 서비스 연동(connector / plugin)", "openai-curated-remote 파일", (CODEX + r"\plugins\cache\openai-curated-remote",), "directory", "보조", "설치 플러그인 종류·설치 시각·최근 사용 시각·버전 관련 파일"),
    _d("connector_global_state", "외부 서비스 연동(connector / plugin)", ".codex-global-state.json", (GLOBAL_STATE,), "json", "직접", "tools에 기록된 플러그인 설치·사용 종류와 관련 대화 ID"),

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
