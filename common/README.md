# ActTrace 통합 도구

`act_trace.cmd <E01 경로 또는 live> <결과 절대 경로>`로 실행합니다.
입력의 아티팩트를 확인해 `chatgpt_app`, `chatgpt_web`, `claude_app`,
`claude_web` 중 흔적이 발견된 환경만 분석합니다. 환경 선택 인자는 없습니다.

```cmd
cd common
act_trace.cmd "C:\Evidence\case01.E01" "C:\Result\case01"
act_trace.cmd live "C:\Result\live01"
```

Live는 **관리자 권한으로 실행한 Windows 터미널**이 필요합니다.
VSS를 만들지 못하면 오류로 종료합니다. 잠긴 파일은 기존 ChatGPT Desktop
수집기의 일반 읽기 → 접근 거부 시 원시 NTFS 읽기 순서를 사용합니다.
E01은 libewf/TSK로 읽기 전용으로 열며, 확인된 원본 드라이브만 사용합니다.

## 설치

저장소 전체를 내려받아 네 환경 폴더와 `common`을 함께 유지하세요.
Python 3.12 64비트가 필요합니다. 기존의 호환 가상환경을 사용할 수도 있습니다.

```cmd
cd common
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install --only-binary=zstd -r requirements.txt
```

실행기는 `common\.venv`, 저장소 루트의 `.venv`, `py -3.12` 순으로 Python을
찾습니다. 분석 시 외부 프로그램의 CLI 설치는 필요하지 않습니다.
필요한 포렌식 라이브러리가 없으면 해당 수집/파싱 오류를 출력합니다.

## 기존 도구와의 연결

기존 네 환경의 코드 파일을 수정하거나 복제하지 않습니다.
`common/worker.py`에서 다음 코드를 별도 프로세스로 호출하여 같은 이름의
Python 모듈(`main`, `parsers` 등)이 서로 충돌하지 않게 합니다.

| 환경 | 호출하는 기존 코드 | 결과 파일 |
|---|---|---|
| chatgpt_app | `chatgpt_app`의 파서·판정 규칙·보고 함수 | `collected_artifacts.zip`, `parsed_artifacts.json`, `LLM_Artifact_Report.csv`, `LLM_Artifact_Report.html` |
| chatgpt_web | `chatgpt_web/LLMTrace_최종본`의 `chatgpt_web` 프로필 | `parsed_artifacts.json`, `LLM_Artifact_Report.csv`, `LLM_Artifact_Report.html` |
| claude_app | `claude_app/tools/actrace` | `collected_artifacts.zip`, `parsed_artifacts.json`, `LLM_Artifact_Report.csv`, `LLM_Artifact_Report.html` |
| claude_web | `claude_web/tools` | `LLM_Artifacts.zip`, `parsed_artifacts.json`, `LLM_Artifact_Report.csv`, `LLM_Artifact_Report.html` |

ChatGPT Web의 최종본은 원래 결과 파일 3개를 생성하며, Claude Web의 ZIP 이름은
원래 `LLM_Artifacts.zip`입니다. 두 웹 환경은 그 계약을 유지합니다.
Claude Desktop만 요청에 따라 ChatGPT Desktop의 결과 파일 종류·이름으로
맞춥니다. ActTrace가 내부적으로 생성하는 부가 CSV/JSONL은 최종 폴더에
내보내지 않습니다. ActTrace가 import하는 `chattrace`의 저수준 라이브러리는
의존성으로 사용하되 ChatTrace 대화 복원 프로그램은 실행하지 않습니다.

ChatGPT Web과 Claude Desktop만 발견되었다면 다음과 같이 저장됩니다.

```text
C:\Result\case01\
├─ chatgpt_web\
│  ├─ parsed_artifacts.json
│  ├─ LLM_Artifact_Report.csv
│  └─ LLM_Artifact_Report.html
└─ claude_app\
   ├─ collected_artifacts.zip
   ├─ parsed_artifacts.json
   ├─ LLM_Artifact_Report.csv
   └─ LLM_Artifact_Report.html
```

## 환경 탐지와 증거 보존

- 원본은 수집 단계에서만 읽습니다. 파서에는 별도 수집본만 전달합니다.
- 동일 파일은 공유 수집 단계에서 한 번 읽고, 환경별 작업본을 별도로 만듭니다.
  VSS의 데이터베이스 및 WAL/SHM, 캐시 구성 파일을 함께 수집합니다.
- 수집 전후 크기·수정 시각을 검사하고 SHA-256과 원본/수집 경로를 기록합니다.
  ZIP 경로는 `C/Users/...` 형식입니다. 확인되지 않은 드라이브는 추정하지 않습니다.
- ChatGPT Desktop은 등록된 `.codex`/OpenAI.Codex 앱 파일, 실행 파일이 확인된
  Prefetch, 공유 레지스트리의 앱 식별 문자열을 탐지 대상으로 사용합니다.
- Claude Desktop은 Claude 앱 데이터·패키지 매니페스트·Prefetch 경로 또는
  공유 레지스트리의 Claude 식별 문자열을 확인합니다.
- 웹 환경은 Chrome 사용자 데이터의 History URL/Cookies 호스트 등 서비스
  필드, 캐시·스토리지의 서비스 도메인 흔적을 확인합니다. ChatGPT는
  `chatgpt.com`/`chat.openai.com`, Claude는 `claude.ai`입니다.
  Chrome/History 파일 존재만으로 웹 환경을 선택하지 않습니다.
- 이 탐지는 분석기 선택 기준입니다. 실제 사용자 행위는 기존 각 도구의
  판정 규칙으로만 분석합니다. 캐시의 도메인 흔적은 사용자 방문의 확정 증거가 아닙니다.
- 읽기 실패는 부재와 구분해 CLI와 탐지된 환경의 JSON `integration`에 기록합니다.
  탐지할 수 없는 환경에는 정상 보고서를 임의로 만들지 않습니다.
  한 환경의 실행 실패는 다음 환경의 분석을 중단시키지 않습니다.
- 공개 결과 JSON/CSV/HTML의 인증 토큰·쿠키 비밀값은 출력 단계에서 생략합니다.
  분석기의 판정 입력과 ZIP의 원본 바이트는 변경하지 않습니다.

결과는 새 출력 폴더를 사용하세요. 동일 환경의 결과 폴더가 이미 있으면 덮어쓰지
않고 입력 오류로 종료합니다. 작업 중 수집 ZIP과 환경별 작업본을 만들기 때문에
최종 산출물보다 많은 임시 저장 공간이 필요합니다. 완료 후 임시 폴더는 정리됩니다.

## 검증

```cmd
.venv\Scripts\python.exe -m pip install pytest
.venv\Scripts\python.exe -m pytest tests -q
```

테스트는 가공한 Chrome SQLite, 앱 파일 및 가짜 읽기 전용 E01/Live reader를
사용합니다. 환경 선택/제외, 비슷한 가짜 도메인 제외, 실패 후 계속 진행,
드라이브 경로·해시·ZIP 보존, 네 분석기의 실제 결과 파일 생성을 검사합니다.
실제 Windows VSS와 실제 E01의 전체 실행은 별도 증거 이미지에서 검증해야 합니다.
성공은 종료 코드 0, 수집·탐지·분석 오류는 1, 잘못된 CLI 입력은 2입니다.
