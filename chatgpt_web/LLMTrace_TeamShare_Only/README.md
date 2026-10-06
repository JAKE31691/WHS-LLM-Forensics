# LLMTrace Team Share Exporter

LLM 서비스 사용 흔적을 분석하고, 팀 공유용 CSV와 HTML만 생성하는 포렌식 분석 도구입니다. E01 이미지, 수집된 폴더 또는 현재 PC의 라이브 데이터를 입력으로 사용할 수 있습니다.

## 전체 처리 흐름

```text
입력 데이터 준비
  → 브라우저·애플리케이션 아티팩트 파싱
  → 파싱 결과를 Observation으로 정규화
  → 환경별 규칙으로 사용자 행위 매칭
  → 팀 공유용 CSV 및 HTML 생성
```

E01을 입력하면 필요한 파일을 임시 폴더에 추출한 뒤 분석합니다. 파싱 결과와 중간 Observation도 임시 폴더에서 처리하며, 실행이 끝나면 자동으로 삭제합니다.

## 내부 구조

```text
LLMTrace_TeamShare_Only/
├─ chatgpt-web.cmd          # ChatGPT Web 실행 파일
├─ claude-desktop.cmd       # Claude Desktop 실행 파일
├─ llmtrace/
│  ├─ core/                 # 수집, 파싱, 정규화, 규칙 실행, 보고서 생성
│  ├─ profiles/             # 환경별 아티팩트 정의와 행위 규칙
│  │  ├─ chatgpt_web/
│  │  └─ claude_app/
│  └─ common/               # 공통 기능
├─ tests/                   # 동작 검증용 테스트
└─ README.md
```

`core`는 모든 환경에서 공통으로 사용하는 처리 기능입니다. `profiles`에는 ChatGPT Web, Claude Desktop처럼 환경마다 달라지는 수집 대상, 매핑 방법, 행위 규칙이 들어 있습니다.

## 사용법

PowerShell에서 도구 폴더로 이동한 뒤 다음 형식으로 실행합니다.

```powershell
cd "C:\Users\jake3\Documents\Codex\2026-10-02\new-chat\work\source\LLMTrace_TeamShare_Only"
.\<환경명>.cmd "<입력 경로 또는 live>" "<결과 저장 절대 경로>"
```

ChatGPT Web E01 분석 예시:

```powershell
.\chatgpt-web.cmd "C:\Evidence\case01.E01" "C:\Result\case01"
```

현재 PC 분석 예시:

```powershell
.\chatgpt-web.cmd live "C:\Result\live-case"
```

현재 지원하는 실행 파일은 다음과 같습니다.

- `chatgpt-web.cmd`: ChatGPT Web
- `claude-desktop.cmd`: Claude Desktop

`chatgpt-desktop.cmd`와 `claude-web.cmd` 프로필은 아직 구현되지 않았습니다.

## 결과물

지정한 결과 폴더에는 다음 두 파일만 생성됩니다.

```text
LLMTrace_행위별_아티팩트_매트릭스.csv
LLMTrace_팀공유_행위별_아티팩트.html
```

- CSV: 실제로 매칭된 행위 1건당 1행으로 행위, 연결 아티팩트, 경로, 등급을 기록합니다.
- HTML: 분석 개요, 행위별 매칭 결과, 각 매칭에 연결된 아티팩트와 원본 파싱 내용을 화면에서 확인할 수 있습니다.

HTML은 필요한 원본 파싱 데이터를 파일 안에 포함하므로 별도의 보조 폴더가 필요하지 않습니다. 결과 저장 폴더는 비어 있거나 이 도구가 전에 만든 두 결과 파일만 들어 있어야 합니다.

## 주의사항

현재 도구는 BitLocker로 잠긴 물리 디스크 이미지를 직접 해제하지 못합니다. 실험 이미지는 잠금이 해제된 `Logical Drive C:`를 E01로 수집하는 방식을 권장합니다. 분할 이미지인 `.E01`, `.E02` 등이 만들어진 경우 모든 조각을 같은 폴더에 두고 `.E01` 파일을 입력하면 됩니다.
