"""도구 정보, 사용자 행위 목록, 아티팩트 정의(무엇을 어디서 수집할지).

판정 규칙은 여기 두지 않고 rules.csv에 둔다.
"""
from dataclasses import dataclass, field

TOOL_NAME = "LLM Artifact Analyzer"
VERSION = "v2.0.0"
AUTHOR = "jbsquad"
CONTACT = "jbsquad000@gmail.com"
TARGET = "Claude Web (Chrome)"

# 출력 파일 이름
CSV_NAME = "LLM_Artifact_Report.csv"
HTML_NAME = "LLM_Artifact_Report.html"
JSON_NAME = "parsed_artifacts.json"
ZIP_NAME = "LLM_Artifacts.zip"

# 사용자 행위: (ID, 한국어 이름, 콘솔용 영어 이름)
ACTIONS = [
    ("A01", "사이트 접속", "Site Access"),
    ("A02", "로그인", "Login"),
    ("A03", "로그아웃", "Logout"),
    ("A04", "다중 계정", "Multiple Accounts"),
    ("A05", "새 대화 생성", "New Chat"),
    ("A06", "프롬프트 입력", "Prompt Input"),
    ("A07", "프롬프트 수정", "Prompt Edit"),
    ("A08", "메모리 기능 사용", "Memory"),
    ("A09", "파일 업로드", "File Upload"),
    ("A10", "파일 다운로드", "File Download"),
    ("A11", "대화 삭제", "Chat Deletion"),
    ("A12", "계정 삭제", "Account Deletion"),
    ("A13", "임시 채팅", "Temporary Chat"),
    ("A14", "플러그인 사용", "Plugin Use"),
]
ACTION_NAME = {a[0]: a[1] for a in ACTIONS}

# 사용자 폴더 기준 Chrome 사용자 데이터 위치
CHROME_USER_DATA = r"AppData\Local\Google\Chrome\User Data"


@dataclass
class ArtifactDef:
    """아티팩트 한 종류.

    name     : 콘솔·규칙에서 부르는 이름 (rules.csv의 artifact 열과 같아야 함)
    base     : "profile" = 프로필 폴더(Default, Profile 1 ...) 기준
               "user_data" = User Data 폴더 기준 (Local State처럼 프로필 밖에 있는 것)
    rel      : base 기준 상대 경로 (Windows 구분자)
    kind     : "file"   = 파일 하나가 아티팩트 하나
               "folder" = 폴더 전체가 아티팩트 하나 (LevelDB처럼 여러 파일을 함께 읽는 것)
               "files"  = 폴더 안 파일 하나하나가 각각 아티팩트 하나
    parser   : parsers 패키지의 파서 이름
    extras   : 같이 수집할 파일/폴더 (base 기준). file이면 -journal 같은 짝 파일,
               folder면 IndexedDB의 .blob 폴더처럼 파서가 함께 읽는 폴더
    """
    name: str
    base: str
    rel: str
    kind: str
    parser: str
    extras: list = field(default_factory=list)


ARTIFACT_DEFS = [
    ArtifactDef("History", "profile", "History", "file", "sqlite",
                ["History-journal", "History-wal"]),
    ArtifactDef("Cookies", "profile", r"Network\Cookies", "file", "sqlite",
                [r"Network\Cookies-journal", r"Network\Cookies-wal"]),
    ArtifactDef("Web Data", "profile", "Web Data", "file", "sqlite",
                ["Web Data-journal", "Web Data-wal"]),
    ArtifactDef("Preferences", "profile", "Preferences", "file", "json"),
    ArtifactDef("Local State", "user_data", "Local State", "file", "json"),
    ArtifactDef("Local Storage", "profile", r"Local Storage\leveldb", "folder", "localstorage"),
    ArtifactDef("Session Storage", "profile", "Session Storage", "folder", "sessionstorage"),
    ArtifactDef("IndexedDB", "profile", r"IndexedDB\https_claude.ai_0.indexeddb.leveldb", "folder", "indexeddb",
                [r"IndexedDB\https_claude.ai_0.indexeddb.blob"]),
    ArtifactDef("Sessions", "profile", "Sessions", "files", "snss"),
    ArtifactDef("Cache", "profile", r"Cache\Cache_Data", "folder", "cache"),
]
