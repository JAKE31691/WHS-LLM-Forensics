# 통합 개발 명세 반영 범위

이 문서는 통합 분석도구 개발 명세의 확정 사항과 v0.4.2 구현을 대응시킨다.

## 반영 완료

- 입력: 수집 폴더 및 E01/RAW/DD/IMG/VMDK 공통 입력 계층
- 1차 파싱: 원본 레코드와 provenance 분리, 원본 수정 금지
- 2차 처리: ChatGPT Web History/Cache/Storage/Cookie 구조화 및 원본 `parsed_record` 보존
- Rule: Minimum/Standard/Extended 정의, 최고 충족 단계 한 건 출력
- Window: History 기반 candidate evidence window와 open-ended 마지막 구간
- 연결: 직접 ID/URL 관계 우선, 시간 근접만으로 동일 행위 확정 금지
- 결과: 공통 14컬럼 `behaviors.csv`
- 오류: 파서 단위 격리, `problems.csv`, 실행 정보 `run.json`
- HTML: Behavior Result / Current Useful Information / Observation Viewer 3영역
- URL 연결: 파서가 만든 `query.<name>`을 Rule과 association이 그대로 사용하며, 예전 Observation JSONL의 최상위 OAuth 별칭은 호환용 fallback으로만 허용
- Context 표시: `context.py`의 분류별 건수와 fact type 구성을 HTML에 표시
- Rule 검증: 중복 evidence ref, 존재하지 않는 association policy/ref, evidence level의 잘못된 ref를 실행 전에 거부
- 팀 공유 부가 출력: 정상 분석의 메인 결과는 유지하고 `<output>/team_share/`에 실제 Inference→Observation 매칭 기반 CSV·HTML을 추가 생성. 등급은 Minimum 핵심 근거인 `직접`과 추가 결합 근거인 `귀속`만 사용

## 의도적으로 확정하지 않은 사항

명세에서 미정으로 남긴 다음 항목은 구현에서도 임의로 고정하지 않는다.

- `fact_type` 제거 여부: 기존 검증 Rule 호환을 위해 선택적 필드로 유지
- 마지막 History가 없을 때의 종료 시각: open-ended 유지
- Current State의 최종 공통 JSON 스키마
- Rule level과 confidence의 자동 매핑
- 같은 window 안에서 서로 다른 행위가 겹칠 때의 일반 병합 정책
- 잔존 데이터가 과거 행위인지 현재 상태인지에 대한 일괄 해석

## 프로필 상태

- `chatgpt_web`: A01~A13 실험 초안 중 구별 가능한 행위 Rule과 증거 수준 연결
- `claude_app`: 원자료 복원과 Viewer 연결, 행위 Rule은 미검증 상태라 생성하지 않음
- `chatgpt_desktop`, `claude_web`: 검증된 수집 경로와 Rule이 제공되지 않아 프로필을 만들지 않음

새 프로필은 `SURFACE_CONTRACT.md`의 계약에 따라 수집 대상, Parser, Rule 필드, 세 단계 증거, 시간 기준, 오탐 조건 및 검증 상태를 정의해야 한다.
