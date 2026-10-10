# LLMTrace Team-Share Runtime

팀공유용 최종 결과만 생성하는 실행 코드입니다. 테스트, 샘플, 개발 문서와 메인 보고서 생성 코드는 포함하지 않습니다.

## 지원 환경

- `chatgpt-web.cmd`: ChatGPT Web
- `claude-desktop.cmd`: Claude Desktop

## 실행

PowerShell에서 이 폴더로 이동한 뒤 실행합니다.

```powershell
cd "C:\Users\jake3\Documents\Codex\2026-10-02\new-chat\work\source\LLMTrace_TeamShare_Runtime"
.\chatgpt-web.cmd "C:\Evidence\case.E01" "C:\Result\case"
```

라이브 시스템은 E01 경로 대신 `live`를 사용합니다.

```powershell
.\chatgpt-web.cmd live "C:\Result\live-case"
```

## 결과물

지정한 결과 폴더에는 다음 세 파일만 생성됩니다.

```text
LLM_Artifact_Report.csv
LLM_Artifact_Report.html
parsed_artifacts.json
```

- CSV: 행위 매칭 결과와 근거 위치
- HTML: 간단한 분석 개요와 A00~A14 행위별 결과. 미매칭 행위도 `매칭 0건`으로 표시
- JSON: 전체 1차 파싱값과 `artifacts[n].records[n]` 근거 주소

## Python 환경

현재 프로젝트의 공용 Python 환경을 자동으로 찾습니다. 다른 Python을 사용하려면 먼저 환경 변수를 지정합니다.

```powershell
$env:LLMTRACE_PYTHON = "C:\Path\To\python.exe"
```

필요한 Chromium 파서가 없다면 해당 Python 환경에 설치합니다.

```powershell
python -m pip install -r requirements.txt
```

## 코드 구성

```text
_run-environment.cmd       공통 실행기
chatgpt-web.cmd            ChatGPT Web 실행 진입점
claude-desktop.cmd         Claude Desktop 실행 진입점
llmtrace/environment_cli.py
llmtrace/core/             이미지 수집, 파싱, 규칙 적용, 팀공유 결과 생성
llmtrace/common/parsers/   Chromium 공통 파서
llmtrace/profiles/         환경별 수집·정규화·행위 규칙
```
