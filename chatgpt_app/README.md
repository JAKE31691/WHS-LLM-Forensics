# ChatGPT Desktop LLM Artifact Analyzer

Windows 11의 ChatGPT Desktop 아티팩트를 수집·파싱하고 사용자 행위별 CSV·HTML 보고서를 생성하는 Python CLI 도구입니다.

## 설치

Python 3.12 64비트와 Git이 필요합니다. 프로젝트 루트에서 실행합니다.

```cmd
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 실행

CLI 인자는 `source`와 절대 경로인 `output` 두 개입니다. `source`에는 E01 첫 세그먼트 또는 정확히 `live`를 입력합니다. 분할 E01의 다른 세그먼트는 같은 폴더에 둡니다. 결과 파일이 이미 있는 출력 폴더는 사용할 수 없습니다.

```cmd
chatgpt-app.cmd "C:\Evidence\case01.E01" "C:\Result\case01"
chatgpt-app.cmd live "C:\Result\live01"
```

Live 수집은 관리자 권한의 Windows 터미널에서 실행합니다. 프로그램은 VSS 스냅샷을 만들고 파일을 읽은 뒤 스냅샷 삭제를 시도합니다. E01은 읽기 전용으로 열며, 드라이브 문자를 확인할 수 없는 볼륨에는 임의의 문자를 지정하지 않습니다.

## 결과 파일

| 파일 | 내용 |
| --- | --- |
| `collected_artifacts.zip` | 수집본과 `_collection_manifest.json`. 원본 경로 `C:\Users\...`는 ZIP 안에서 `C/Users/...`로 보존됩니다. |
| `parsed_artifacts.json` | 파서별 기록, 수집·파싱 상태, 원본·수집 경로. 인증 토큰과 쿠키 값은 표시하지 않으며 일부 파서는 출력 기록 수를 제한합니다. 원본 바이트는 ZIP에 남습니다. |
| `LLM_Artifact_Report.csv` | `행위 분류`, `아티팩트`, `경로`, `아티팩트 등급` 네 열의 UTF-8 BOM 보고서. |
| `LLM_Artifact_Report.html` | CSV와 동일한 행을 행위별 아코디언으로 표시하는 독립 실행형 보고서. |

물리적 파일 하나가 여러 행위·아티팩트 정의에 연결될 수 있습니다. ZIP에는 한 번만 저장하고, CLI의 아티팩트별 `Found`에는 해당하는 각 이름으로 표시합니다. 따라서 `Found` 값의 합이 전체 고유 파일 수보다 클 수 있습니다.

행위별 증거 등급은 명시된 정의를 따릅니다. 판정 조건이 있는 정의는 해당 조건을 충족해야 보고서에 기록됩니다. 조건이 없는 정의는 수집에 성공한 파일을 해당 행위의 아티팩트 목록에 기록하며, 이것만으로 사용자 행위가 확정됐다는 뜻은 아닙니다. 파일 부재, 수집 실패, 파싱 실패는 각각 구분해 기록합니다.

원본 E01과 Live 파일은 수정하지 않습니다. ZIP manifest에는 원본 경로, ZIP 내부 경로, 크기, 수집 상태, SHA-256이 기록됩니다. 결과 파일에는 사용자 경로와 대화 관련 정보가 포함될 수 있으므로 공개 저장소에 올리지 마세요.
