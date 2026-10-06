# LLMTrace 포렌식 유의미 정보 및 규칙 발견 기록

이 문서는 실험 데이터에서 확인한 사용자 행위 규칙과, 행위를 특정하지 못하더라도 수사·분석에 유의미한 정보를 함께 기록한다. 새 실험을 분석할 때마다 같은 형식으로 항목을 추가한다.

## 기록 원칙

- 사용자 행위와 단순 상태·식별정보는 분리한다.
- 하나의 현재 상태만으로 과거 행위를 만들지 않는다.
- 계정 ID, 사용자 ID, 이메일, 표시 이름, 지역, 대화 ID, 메시지 ID, 파일 ID 등은 행위가 아니어도 Context로 보존한다.
- 주소·전화번호처럼 값이 없거나 불명확한 정보는 추정하지 않는다.
- `region`은 서비스 응답에 기록된 지역값이며 실제 거주지나 행위 장소로 단정하지 않는다.
- Cache의 압축 JSON은 의미 계층에서 해제해 필요한 필드를 추출하되, 원본 바이트는 `_raw`에 그대로 둔다.
- 실험용 BEFORE/AFTER 비교 결과와 단일 이미지에서 가능한 런타임 판정 범위를 구분한다.

## A07 프롬프트 입력 수정 및 재전송

### 실험 자료

- BEFORE 성격 자료: `07-1`
- AFTER 성격 자료: `07-2`
- 두 자료에서 공통으로 확인된 대화 ID: `6aa36059-92dc-83e8-9d73-517cbe4286cc`
- 대화 제목: `비빔밥 만들기_recipe`

### 확인된 행위 특징

`07-1`의 현재 대화 가지에는 기존 A06 사용자 프롬프트와 그 응답이 남아 있다. `07-2`에서는 같은 대화의 공통 메시지 접두부 뒤에서 사용자 메시지 가지가 새 메시지로 교체되고, 그 뒤에 새 Assistant 응답 가지가 연결된다.

- `07-1` 현재 노드: `c9b6bd6a-f733-414c-858f-ca5b9e3083af`
- `07-2` 현재 노드: `2719e499-6843-414c-8991-7c94f4045bb5`
- `07-2` 새 사용자 메시지 ID: `d97068d4-82eb-4bc0-8036-4a1a9f02c577`
- `07-2` 새 프롬프트: `[A07-1_prompt] 비빔밥의 세계적인 입지에 대해서 알려줘.`
- 두 자료의 대화 ID는 같고 현재 노드와 사용자 메시지 가지는 다르다.
- `07-2`에는 동일 대화의 generation status `COMPLETE`가 함께 관찰된다.

따라서 A07의 핵심 근거는 URL에 `include_has_versions=true`가 있다는 사실이나 generation status 자체가 아니다. 같은 대화의 두 복원 가능한 스냅샷을 비교했을 때, 공통 가지 이후의 기존 사용자 메시지가 다른 사용자 메시지로 교체되고 새 응답이 연결되는 구조가 핵심이다.

### 적용한 행위 규칙

규칙 ID는 `R-PROMPT-EDIT-01`, 행위 ID는 `B07`이다.

- 최소 단계: 같은 대화 ID를 가진 이전·이후 스냅샷이 있고, 공통 메시지 접두부 뒤에서 기존 사용자 메시지와 이후 사용자 메시지의 ID 또는 내용 해시가 달라야 한다. 이후 가지에는 Assistant 응답이 있어야 한다. 결과는 `presumed`이다.
- 표준 단계: 최소 단계 조건에 더해 두 스냅샷 사이에서 같은 대화 ID의 generation status `IS_STREAMING` 또는 `COMPLETE`가 관찰되어야 한다. 결과는 `confirmed`이다.
- 확장 단계: 현재 실험에서는 검증하지 않았으므로 `unavailable`이다.
- 제외 조건: 이후 스냅샷이 기존 가지 뒤에 새 사용자/Assistant 턴만 추가한 append-only 구조이면 일반 프롬프트 입력이므로 A07로 판정하지 않는다.
- 단일 스냅샷만 있으면 A07 행위를 출력하지 않는다.

실제 `07-1`과 `07-2` 스냅샷 및 `07-2`의 `COMPLETE` 상태를 같은 실험 비교 범위로 넣어 검증한 결과는 `R-PROMPT-EDIT-STD-01`, `confirmed`이다. 증거 경계는 `2026-09-11T07:22:35.257863`부터 `2026-09-11T08:52:44.852334`까지다. 이 구간은 두 Cache 관찰 시각 사이의 범위이며 사용자가 실제로 1시간 30분 동안 수정했다는 뜻이 아니다.

실험용 두 폴더를 각각 단독 분석하면 각각 한 시점만 존재하므로 `behaviors.csv`에 B07이 생기지 않는 것이 정상이다. 한 증거 세트 안에서 이전 Cache 버전과 이후 Cache 버전이 함께 복원될 때 이 규칙이 런타임으로 동작한다. 실험 비교 자체는 `07-1`과 `07-2`의 스냅샷을 직접 대조해 검증했다.

### Timestamp 선택

- 행위 범위 시작: 이전 conversation snapshot Cache의 `response_time`
- 보강 시각: conversation stream status Cache의 `response_time`
- 행위 범위 종료: 이후 conversation snapshot Cache의 `response_time`
- 대화 JSON 내부 `create_time`과 `update_time`은 대화/메시지 자체의 메타데이터로 보존하지만, Cache가 실제로 관찰된 시각과 구분한다.

### 행위 근거에서 제외한 흔적

`07-2` Local Storage에는 `[A0`, `[A06-1]`, `[A06-1] 오늘 날씨 어때?` 같은 초안 잔존값이 있다. 이 값들의 내장 시각과 내용은 A07이 아니라 이전 A06 입력 과정에 대응하므로 A07 근거로 사용하지 않는다. 빈 `drafts` 배열은 프롬프트 초안이 아니라 `conversation_draft_cleared` 상태로 분리한다.

## A07 자료에서 확인한 포렌식 유의미 정보

아래 값은 `/backend-api/me`와 conversation snapshot의 실제 복원값이다. 계정 응답이 관찰되었다는 사실을 뜻하며, 별도 사용자·장치 귀속 검증 없이 “이 PC의 소유자”로 단정하면 안 된다.

| 구분 | 필드 | 관찰값 | 해석 제한 |
|---|---|---|---|
| 계정 | `account_id` | `user-0dQYyh5dWY4cQ6UrL8ebcyty` | 관찰된 ChatGPT 계정 식별자 |
| 계정 | `email` | `jake31691@kakao.com` | 계정 응답에 포함된 이메일 |
| 계정 | `name` | `황수연` | 계정 표시 이름 |
| 계정 | `country` | `KR` | 서비스 계정/접속 문맥의 국가값 |
| 계정 | `region`, `region_code` | `Daejeon`, `30` | 실제 주소나 거주지로 단정 불가 |
| 계정 | `phone_number` | 값 없음 | 전화번호 미확인 |
| 계정 | `address`, `city`, `postal_code` | 관찰되지 않음 | 상세 주소 미확인 |
| 조직 | `organization_id` | `org-FZBbS9W0ktDNkOpZam9s4bPh` | 서비스 내부 조직 식별자 |
| 조직 | `organization_name` | `user-0dqyyh5dwy4cq6url8ebcyty` | 서비스 내부 조직 이름 |
| 클라이언트 | `client_id` | `app_X8zY6vW2pQ9tR3dE7nK1jL5gH` | 서비스 클라이언트 식별자 |
| 분류 | `email_domain_type` | `social` | 서비스 응답의 도메인 분류값 |
| 분류 | `ads_segment_id` | `7728` | 서비스 응답의 세그먼트 식별값 |
| 대화 | `conversation_id` | `6aa36059-92dc-83e8-9d73-517cbe4286cc` | 두 스냅샷 연결의 핵심 키 |
| 대화 | `title` | `비빔밥 만들기_recipe` | 복원 당시 대화 제목 |
| 대화 | `is_temporary_chat` | `false` | 임시 대화가 아님 |
| 대화 | `is_do_not_remember` | `false` | 응답 필드의 상태값 |
| 메시지 | `message_id`, `request_id`, `turn_id`, `turn_exchange_id` | 다수 복원 | 메시지·요청·턴 상호 연결에 사용 |

## 코드 반영 사항

- `llmtrace/profiles/chatgpt_web/mappings.py`
  - Brotli·gzip·zlib 압축 Cache JSON 해제
  - `/backend-api/conversations/<id>`를 `conversation_snapshot`으로 정규화
  - 활성 메시지 순서, 메시지·요청·턴 ID, 본문 해시, 현재 노드 추출
  - `/backend-api/me`에서 계정·이름·지역·조직·클라이언트 필드 추출
  - Local Storage 초안 레코드를 실제 초안과 빈 초안 상태로 분리
- `llmtrace/core/engine.py`
  - 다음 스냅샷 선택과 사용자 메시지 가지 교체 비교 추가
- `llmtrace/profiles/chatgpt_web/rules.py`
  - B07 최소/표준 규칙과 계정·대화 Context 추출 확장
- `llmtrace/profiles/chatgpt_web/context.py`
  - 대화 스냅샷과 초안 삭제 상태를 보고서 Context에 추가
- `tests/`
  - 압축 응답, 계정 필드, 초안 분리, A07 양성·음성 사례 테스트 추가

## A08 메모리 기능 실험

### 실험 목적과 결론

A08은 ChatGPT의 메모리 기능을 프롬프트로 요청한 경우와, Personalization 화면에 들어가 사용자가 직접 수정하려 한 경우를 조사한 실험이다. 자료는 `08-1`과 `08-2` 두 시점으로 구성된다.

이 자료에서 확실히 말할 수 있는 것은 두 가지다.

1. 두 시점 모두 사용자가 `Settings > Personalization` 영역에 접근했다.
2. `08-1`과 `08-2` 사이에 Memory/About You 정보의 원본 상태를 나타내는 체크섬이 바뀌었고, 이전에는 오래된 요약 상태였지만 이후에는 원본과 캐시가 일치하는 최신 상태가 되었다.

반대로 이 자료만으로는 변경이 프롬프트 요청 때문에 발생했는지, Personalization 화면에서 직접 수정했기 때문인지, 서비스가 자동으로 요약을 다시 만들었는지를 확정할 수 없다. 변경용 POST 요청이나 대화 본문이 복원되지 않았기 때문이다. 따라서 A08은 `설정 접근`, `메모리 정보 상태 변경`, `정확한 변경 방식`을 서로 다른 판단으로 다룬다.

### 08-1에서 확인된 흔적

- History에서 `2026-09-19T01:18:57.915078Z`에 설정 화면 접근이 확인된다.
- History에서 `2026-09-19T01:19:00.049190Z`에 `/ko-KR/#settings/Personalization` 접근이 확인된다.
- Memory 목록 API 응답은 `2026-09-19T01:19:00.236180`에 관찰되었다.
- Memory/About You 체크섬 응답은 `2026-09-19T01:19:00.832010`에 관찰되었다.
- 이 응답의 `sourceChecksum`은 `23902c12157aab8d40a18a38ce5c74497135975e58e73411e4260df92877f860`이고, `cachedSourceChecksum`은 `6879e9d07e82d8ce0f8166c134f27aaa38878797983560463863d233e7a4d65a`로 서로 다르다.
- `isStale=true`이므로 당시 캐시된 About You 요약은 현재 원본 상태와 일치하지 않았다.
- 캐시된 요약 생성 시각은 `2026-09-18T22:02:06.015550+00:00`이다.
- 수정 화면을 열 수 있다는 capability 응답에는 `make_correction`, `수정하기`, `open_correction_composer`, `correct`가 포함된다. 이것은 수정 기능이 제공되었다는 뜻이지, 실제 수정 실행의 증거는 아니다.

### 08-2에서 확인된 흔적

- History에서 `2026-09-19T12:09:39.477932Z`에 `/ko-KR/#settings/Personalization` 접근이 확인된다.
- History에서 `2026-09-19T12:13:17.900176Z`에 `/ko-KR/#settings/Personalization?section=memory` 접근이 확인된다.
- Memory 목록 API 응답은 `2026-09-19T12:09:40.272141`에 관찰되었다.
- Memory/About You 체크섬 응답은 `2026-09-19T12:09:40.840257`에 관찰되었다.
- 이 응답의 `sourceChecksum`과 `cachedSourceChecksum`은 모두 `e7f9318dc09aa7763b75dd05af9541adbb185c7b01dfbc4762030f314e462dbb`이다.
- `isStale=false`이므로 관찰 당시 캐시된 About You 요약과 원본 상태가 일치했다.
- 새 요약 생성 시각은 `2026-09-19T01:27:23.834018+00:00`이다.
- Local Storage의 conversation-history에는 제목이 `메모리 수정 완료`인 대화가 새로 확인된다. 대화 ID는 `6aade4fc-fad8-83ee-80bb-6d99d7d40094`, 생성 시각은 `2026-09-19T01:27:24.980311Z`, 갱신 시각은 `2026-09-19T01:27:30.292437Z`이다.

새 About You 요약 생성 시각과 `메모리 수정 완료` 대화 생성 시각은 약 1.15초 차이다. 메모리 실험과 관련됐을 가능성을 보강하지만, 제목만으로 대화 본문이나 실제 변경 방식을 증명할 수는 없다.

### 적용한 행위 규칙

설정 접근 규칙의 기본 ID는 `R-MEMORY-MANAGEMENT-OPEN-01`, 행위 ID는 `B08`이다.

- 최소 단계: History에 `#settings/Personalization` 접근만 있으면 `suspected`이다.
- 표준 단계: 같은 증거 범위에서 `section=memory` 접근 또는 Memory 관련 API 응답이 함께 있으면 `confirmed`이다.
- 확장 단계: 현재 검증 자료가 부족하므로 `unavailable`이다.
- 제외 조건: correction action 목록이나 체크섬 응답만 보고 실제 수정·삭제 행위로 판정하지 않는다.

실제 단독 분석 결과 `08-1`과 `08-2` 모두 `R-MEMORY-MANAGEMENT-OPEN-STD-01`, `confirmed`로 판정된다. `08-2`에는 일반 Personalization 접근과 `section=memory` 접근이 각각 History 방문으로 남아 두 개의 접근 구간이 출력된다.

상태 변경 규칙의 기본 ID는 `R-MEMORY-STATE-CHANGE-01`, 행위 ID는 동일하게 `B08`이다.

- 최소 단계: 같은 사용자 자료 범위의 이전·이후 체크섬에서 `sourceChecksum`이 달라지면 Memory/About You 원본 정보의 상태 변경을 `presumed`로 판정한다.
- 표준 단계: 체크섬이 달라졌고, 이후 값에서 `isStale=false`이며 `cachedSourceChecksum == sourceChecksum`이면 상태 변경과 요약 동기화를 `confirmed`로 판정한다.
- 확장 단계: 프롬프트 요청 또는 UI 직접 수정이라는 정확한 변경 방식은 현재 자료로 검증할 수 없어 `unavailable`이다.
- 대안 설명: 프롬프트를 통한 메모리 요청, Personalization 화면에서 직접 수정, 서비스의 자동 요약 재생성을 모두 남긴다.

`08-1`과 `08-2`의 실제 체크섬 관찰값을 하나의 실험 비교 범위로 넣어 검증한 결과는 `R-MEMORY-STATE-CHANGE-STD-01`, `confirmed`이다. 관찰 경계는 `2026-09-19T01:19:00.832010`부터 `2026-09-19T12:09:40.840257`까지다. 이 시간은 두 Cache 응답이 관찰된 범위이며 사용자가 그 시간 동안 계속 수정했다는 뜻이 아니다.

각 폴더를 단독 분석하면 체크섬이 한 개뿐이므로 상태 변경 규칙은 출력되지 않는 것이 정상이다. 설정 접근 행위는 각 폴더에서 따로 출력된다.

### Timestamp 선택

- 설정 접근: History의 `ts_visit`
- 상태 변경의 이전·이후 관찰 경계: 체크섬 Cache 응답의 `response_time`을 정규화한 `ts_record`
- About You 요약 생성 메타데이터: `cachedGeneratedAtIso`를 정규화한 `ts_modified`
- conversation-history의 대화 생성·갱신 시각: 각 항목의 `create_time`, `update_time`

`cachedGeneratedAtIso`는 요약이 생성된 시각이지 사용자가 직접 버튼을 누른 시각이 아니다. 그러므로 행위 대표 timestamp로 단독 사용하지 않고 보조 시각으로 보존한다.

### 메모리 내용과 대화 내용의 해석 제한

Memory 목록 요청에는 `include_memory_entries=false`가 적용되어 있다. 응답의 `memories=[]`는 이 응답에 메모리 항목 본문이 포함되지 않았다는 뜻이며, 계정에 저장된 메모리가 전혀 없었다는 증거가 아니다. 같은 응답에 `memory_num_tokens=107`이 관찰되지만 이것만으로 실제 메모리 문장이나 개수를 복원할 수 없다.

conversation-history에서 다음 제목은 확인된다.

- `Project Codename Recall`: `6aadc1d6-7e20-83e8-ab04-af2aec21922e`
- `Remember Project Context`: `6aadb057-4860-83ee-a513-40fa79070d71`
- `메모리 수정 완료`: `6aade4fc-fad8-83ee-80bb-6d99d7d40094`

제목은 프롬프트 기반 메모리 실험 정황을 제공하지만 대화 본문이 아니므로, 구체적인 프롬프트 내용이나 메모리 저장 성공을 제목만으로 단정하지 않는다.

## A08 자료에서 확인한 포렌식 유의미 정보

아래 정보는 `/backend-api/me`와 Local Storage의 `accountSwitchSessions`에서 관찰되었다. 서비스 계정 문맥의 값이며, 별도 검증 없이 이 PC의 소유자나 실제 거주지로 단정하면 안 된다.

| 구분 | 필드 | 관찰값 | 해석 제한 |
|---|---|---|---|
| 계정 | `account_id`, `user_id` | `user-nXZRzepT2rAr2j9MXJCjJOTr` | 관찰된 ChatGPT 사용자 식별자 |
| 계정 | `email` | `ghkdtndus000@gmail.com` | 계정 응답 및 세션 상태에 포함된 이메일 |
| 계정 | `name` | `수연` | 계정 표시 이름 |
| 계정 | `phone_number` | `+821071935259` | 계정 응답 및 세션 상태에 포함된 전화번호 |
| 계정 | `country` | `KR` | 서비스 계정/접속 문맥의 국가값 |
| 계정 | `region`, `region_code` | `Gyeonggi-do`, `41` | 실제 주소나 거주지로 단정 불가 |
| 계정 | `address`, `city`, `postal_code` | 관찰되지 않음 | 상세 주소 미확인 |
| 조직 | `organization_id` | `org-iQDUiFGCKsAL4eSbeXny0i5i` | 서비스 내부 조직 식별자 |
| 클라이언트 | `client_id` | `app_X8zY6vW2pQ9tR3dE7nK1jL5gH` | 서비스 클라이언트 식별자 |
| 분류 | `ads_segment_id` | `8273` | 서비스 응답의 세그먼트 식별값 |
| 워크스페이스 | `workspace_account_id` | `48de9c92-ef11-4582-9186-b97bf1779a69` | 세션에 포함된 워크스페이스 계정 식별자 |
| 워크스페이스 | `workspace_name` | `수연` | 워크스페이스 표시 이름 |
| 인증 | `auth_provider` | `openai` | 관찰된 인증 제공자 |
| 인증 | `last_logged_in_at` | `2026-09-18T21:27:58Z` | 서비스가 기록한 마지막 로그인 시각 |
| 인증 | session token | 원본에 존재 | 정규화 observation에는 토큰 원문을 복사하지 않고 `has_session_token=true`만 기록 |

세션 토큰은 포렌식적으로 민감한 인증 비밀값이다. 원본 증거에는 보존되지만 Context·CSV·보고서용 의미 필드에는 원문을 반복 저장하지 않는다.

## A08 코드 반영 사항

- `llmtrace/profiles/chatgpt_web/mappings.py`
  - Memory 목록, About You 체크섬, correction capability를 각각 별도 `fact_type`으로 정규화
  - conversation-history 배열을 대화별 observation으로 분리
  - accountSwitchSessions에서 계정·전화번호·로그인·워크스페이스 정보를 추출하고 토큰 원문은 제외
  - 실험 폴더 안의 History·Cache·Local Storage가 같은 자료 범위로 연결되도록 source scope 정규화
- `llmtrace/core/engine.py`
  - 이전·이후 Memory source checksum 비교 조건 추가
- `llmtrace/profiles/chatgpt_web/rules.py`
  - 설정 접근 B08 최소·표준 규칙 보강
  - Memory/About You 상태 변경 B08 최소·표준 규칙 추가
  - 메모리 상태, 대화 목록, 계정 세션 Context 규칙 추가
- `llmtrace/profiles/chatgpt_web/context.py`
  - Memory / Personalization 및 계정 세션 정보를 보고서 Context에 추가
- `llmtrace/core/context_builder.py`
  - `workspace_account_id`를 엔터티 연결 키로 추가
- `tests/`
  - Memory API 정규화, 계정/대화 분리, source scope, 체크섬 변경 양성·음성 테스트 추가

## A09 파일 업로드 실험

### 실험 자료의 구성

A09 자료는 단일 사후 수집본이다. 전후 폴더가 따로 없으므로 새로 생긴 파일을 폴더 간 차이로 검증할 수는 없다. 대신 ChatGPT 파일 객체, 파일 라이브러리 메타데이터, 파일 본문 Cache, 대화 메시지의 attachment를 서로 구분해 판단한다.

사용자가 기억한 PNG 파일은 실제로 확인된다. 파일명은 `[GPT_WEB_A09-2].png`이다. 다만 같은 수집본에는 `[GPT_WEB_A09-1].pdf`와 `[GPT_WEB_A09-1](1).pdf`를 이용한 PDF 업로드 실험 흔적도 함께 남아 있다.

### PNG에서 확인된 사실

| 필드 | 관찰값 |
|---|---|
| `file_id` | `file_000000002e088211bae661a8372801e1` |
| `library_file_id` | `libfile_0dbda55538388191917d44302afd6030` |
| `library_directory_id` | `libdir_e877101fd45481918c2fd35965e248ba` |
| 파일명 | `[GPT_WEB_A09-2].png` |
| MIME | `image/png` |
| 파일 크기 | `952194` bytes |
| 이미지 크기 | `1448 × 1086` |
| SHA-256 | `dc58626dc0798ae18b3c68cd63a6cff01f5980655adfae7500e8768ff8bfec11` |
| 라이브러리 파일 여부 | `true` |

관찰 흐름은 다음과 같다.

1. `2026-09-19T16:02:49.983773`에 `/backend-api/files/<file_id>/simple` 응답에서 파일 ID, 파일명, MIME, library file ID가 확인된다.
2. `2026-09-19T16:02:50.351066`에 `/backend-api/files/download/<file_id>?download_intent=false` 응답이 확인된다. `download_intent=false`이므로 실제 다운로드 행위가 아니라 미리보기·표시·내용 접근 문맥이다.
3. `2026-09-19T16:02:50.817279`에 `/backend-api/estuary/content?id=<file_id>` 응답에서 952,194바이트의 PNG 본문이 복원된다. PNG signature, IHDR의 가로·세로 값, SHA-256을 검증했다.
4. `2026-09-19T16:03:32.337208`에 같은 파일의 `download_intent=false` 응답이 한 번 더 확인된다.

이 PNG에는 명시적인 `file_upload_time`이 복원되지 않았다. 또한 복원된 conversation snapshot에서 이 PNG의 `file_id`를 가진 사용자 메시지 attachment가 확인되지 않는다. 그러므로 파일이 ChatGPT 쪽에 존재하고 내용이 접근된 사실은 확실하지만, 이 수집 구간에서 업로드 요청이 실행됐거나 프롬프트와 함께 전송됐다고 확정하지 않는다.

실제 판정은 `R-FILE-UPLOAD-MIN-01`, `presumed`이다. 대표 관찰 시각은 파일 객체 응답 시각 `2026-09-19T16:02:49.983773`이다.

### 같은 자료에서 확인된 PDF 업로드·첨부

파일 라이브러리 응답에는 다음 두 PDF가 있다.

- `[GPT_WEB_A09-1].pdf`
  - `file_id`: `file_0000000063448206938add8156620d1e`
  - 크기: `16940` bytes
  - `file_upload_time`: `2026-09-19T13:50:20Z`
  - `origination_message_id`: `d9db0777-eb7f-4d17-bcbb-065bc81ffd17`
- `[GPT_WEB_A09-1](1).pdf`
  - `file_id`: `file_00000000562c8209ab4ac37cc0060852`
  - `library_file_id`: `libfile_a734c18c2a74819186eed13bb989c035`
  - 크기: `16940` bytes
  - `file_upload_time`: `2026-09-19T14:38:49Z`
  - `origination_message_id`: `6e88a7aa-da32-4976-a23d-dc948cf1b65d`

두 PDF에는 서비스가 기록한 `file_upload_time`이 있으므로 각각 `R-FILE-UPLOAD-STD-01`, `confirmed`이다.

두 번째 PDF는 대화 `6aae9eaa-4eac-83e8-9a09-5fb83d0a81f9`, 사용자 메시지 `6e88a7aa-da32-4976-a23d-dc948cf1b65d`에 `source=local` attachment로 직접 연결된다. 메시지 생성 시각은 `2026-09-19T14:39:28.345000Z`이고 프롬프트는 `해당 pdf 내용을 읽어줘.`이다. 이 경우에는 파일이 사용자 메시지에 첨부되어 전송됐음을 `R-FILE-ATTACHMENT-STD-01`, `confirmed`로 판정한다.

### 적용한 행위 규칙

업로드 기록 규칙의 기본 ID는 `R-FILE-UPLOAD-01`, 행위 ID는 `B09`이다.

- 최소 단계: ChatGPT `/files/<id>/simple` 응답이나 파일 라이브러리 항목에서 비어 있지 않은 `file_id`와 파일명이 확인된다. 명시적 업로드 시각이 없으면 `presumed`이다.
- 표준 단계: 같은 파일 객체에 서비스의 `file_upload_time`이 명시되어 있으면 `confirmed`이다.
- 확장 단계: 실제 변경용 업로드 HTTP 요청까지 복원하는 조건은 아직 검증하지 않았으므로 `unavailable`이다.
- 제외·제한 조건: 파일 본문 Cache나 `download_intent=false`만으로 업로드·다운로드 또는 메시지 전송을 확정하지 않는다.

메시지 첨부 규칙의 기본 ID는 `R-FILE-ATTACHMENT-01`, 행위 ID는 `B09`이다.

- 최소 단계: 복원된 사용자 메시지에 `source=local` attachment와 `file_id`가 존재하면 `presumed`이다.
- 표준 단계: conversation ID, message ID, 파일명, MIME이 모두 복원되면 사용자 메시지 파일 첨부를 `confirmed`로 판정한다.
- 확장 단계: 실제 네트워크 업로드 요청과 메시지 attachment를 하나의 요청 흐름으로 직접 연결하는 조건은 아직 `unavailable`이다.

### Timestamp 선택

- 명시적 업로드 기록: 파일 라이브러리의 `file_upload_time`을 `ts_event`로 사용
- 파일 처리 완료 보조 시각: `file_processed_time`을 `ts_modified`로 사용
- 파일 객체·본문 관찰: Cache의 `response_time`을 `ts_record`로 사용
- 메시지 첨부: 사용자 메시지의 `create_time`을 `ts_event`로 사용하고 conversation Cache의 `response_time`은 관찰 시각으로 함께 보존

파일 Cache 응답 시각은 파일이 관찰된 시각이지 곧바로 업로드 실행 시각은 아니다. 반대로 `file_upload_time`은 서비스 메타데이터가 기록한 업로드 시각이므로 업로드 행위 판정에 더 직접적이다.

### 포렌식적으로 유의미한 추가 정보

- 파일 ID, library file ID, library directory ID는 동일 파일을 API·Cache·대화 사이에서 연결하는 핵심 식별자다.
- 파일명, MIME, 크기, 이미지 해상도, SHA-256은 파일 동일성 및 무결성 확인에 사용한다.
- `origination_message_id`는 라이브러리 파일과 최초 메시지를 연결할 수 있다.
- conversation attachment의 request ID, turn ID, turn exchange ID는 파일이 포함된 사용자 턴을 다른 대화 레코드와 연결할 수 있다.
- 이 수집본의 계정 문맥은 A08에서 확인된 계정과 동일하다. 새로운 상세 주소 정보는 확인되지 않았다.
- PNG 원본 바이트는 별도 PNG 파일이 아니라 `_raw/cache/entries.jsonl`의 해당 Cache body에 무손실로 보존된다.
- 수집본에는 과거 파일 라이브러리 항목도 함께 있으므로 B09 결과를 볼 때 실험 파일명과 `file_upload_time`을 기준으로 현재 실험과 과거 흔적을 구분해야 한다.

## A09 코드 반영 사항

- `llmtrace/profiles/chatgpt_web/mappings.py`
  - `/backend-api/files/<id>/simple`을 `file_metadata`로 정규화
  - 파일 라이브러리 응답을 항목별 `library_file` observation으로 분리
  - `/backend-api/files/download/<id>`를 preview/download 의도와 함께 `file_content_reference`로 정규화
  - Estuary 본문에서 파일 크기, SHA-256, PNG/PDF/JPEG/GIF 형식과 PNG 해상도를 추출
  - conversation snapshot의 메시지 attachment를 별도 `conversation_attachment` observation으로 분리
- `llmtrace/profiles/chatgpt_web/rules.py`
  - B09 업로드 기록 최소·표준 규칙 추가
  - B09 사용자 메시지 첨부 최소·표준 규칙 추가
  - 파일 Context 추출 필드 확장
- `llmtrace/core/context_builder.py`
  - file ID, library file/directory ID, turn exchange ID를 보고서의 엔터티 연결 키로 추가
- `tests/`
  - 파일 객체, PNG 본문, 라이브러리 항목, 메시지 attachment 정규화 테스트 추가
  - 업로드 시각 유무 및 메시지 첨부에 따른 최소·표준 판정 테스트 추가

## A10 생성 파일 다운로드 실험

### 결론

A10 자료에서는 ChatGPT가 생성한 파일을 사용자가 내려받은 행위를 `R-DOWNLOAD-EXT-01`, `confirmed`로 판정한다. 단순히 다운로드 URL을 열었다는 정황만 있는 것이 아니다. ChatGPT 다운로드 요청, 첨부 응답, Chrome 다운로드 완료 레코드, 다운로드 URL chain이 식별자로 연결된다.

다운로드된 이름은 `[GPT_WEB_10_retry_filename]`이며 `.py` 확장자는 붙어 있지 않다. 그러나 Cache에서 복원한 본문은 UTF-8 Python 소스다. 첫 줄이 `#!/usr/bin/env python3`이고 Python import와 함수·클래스 정의가 확인되어 `content_type_detected=text/x-python`, `python_source_detected=true`로 분류한다. 분석 과정에서 이 코드를 실행하지 않았다.

### 연결된 증거

| 구분 | 관찰값 |
|---|---|
| conversation ID | `6aaf3022-dfe4-83e8-b28a-d4da9e522a54` |
| message ID | `d7fbe222-8a15-4ec4-beda-517df495ba16` |
| sandbox path | `/mnt/data/[GPT_WEB_10_retry_filename]` |
| file ID | `file_000000000dd082098dd710f86184c571` |
| Chrome download ID | `6` |
| 대상 경로 | `C:\Users\vboxuser\Downloads\[GPT_WEB_10_retry_filename]` |
| 파일 크기 | `26346` bytes |
| SHA-256 | `06e02c5de0ae20f907327a44084e8944f6920ac9809286f4d9795c56bc65f9fd` |
| 내용 판정 | UTF-8 Python source, 633 lines |

관찰 흐름은 다음과 같다.

1. `2026-09-20T01:04:54.900725`에 `/backend-api/conversation/<conversation_id>/interpreter/download` 응답이 관찰된다. Query의 `download_intent=true`, message ID, sandbox path를 정규화했다.
2. `2026-09-20T01:04:55.104217`에 `/backend-api/estuary/content` 응답이 관찰된다. `cd=attachment`이고, file ID와 파일명이 확인된다. 같은 Cache body에서 26,346바이트의 Python 소스를 복원했다.
3. Chrome `downloads` 테이블은 `2026-09-20T01:04:55.129516Z`에 다운로드가 시작되고 `2026-09-20T01:05:14.463640Z`에 레코드가 갱신된 것으로 기록한다. `state=1`, `interrupt_reason=0`, `received_bytes=total_bytes=26346`이므로 완료된 다운로드로 판정한다.
4. Chrome `downloads_url_chains` 테이블의 download ID `6`은 같은 Estuary URL과 file ID를 가리킨다. 이로써 브라우저 다운로드 레코드와 ChatGPT 파일 응답이 직접 연결된다.

### 적용한 행위 규칙

다운로드 규칙의 기본 ID는 `R-DOWNLOAD-01`, 행위 ID는 `B10`이다.

- 최소 단계: `download_intent=true`인 ChatGPT interpreter download 요청만 확인된다. 사용자가 다운로드를 시도했다는 의미이므로 `suspected`이며, 파일 저장 완료를 뜻하지 않는다.
- 표준 단계: 같은 conversation에서 Chrome `downloads` 레코드가 확인되고 `state=1`, `interrupt_reason=0`, `received_bytes=total_bytes`이면 완료 행위를 `confirmed`로 판정한다.
- 확장 단계: 표준 조건에 더해 Estuary attachment의 file ID와 URL chain의 file ID가 같고, Chrome download ID와 URL chain의 download ID도 같으면 `confirmed`로 판정한다.
- 제외 조건: `download_intent=false`는 미리보기·표시·본문 접근 문맥이므로 다운로드 행위로 판정하지 않는다. Cache에 파일 본문이 존재한다는 사실만으로도 다운로드 완료를 확정하지 않는다.

실제 A10 결과는 네 종류의 증거가 모두 연결되어 확장 단계다. 적용된 observation은 `obs:4343`, `obs:0704`, `obs:0278`, `obs:0287`이다.

### Timestamp 선택

- 다운로드 의도 관찰: interpreter download Cache의 `response_time`을 `ts_record`로 사용
- 브라우저 다운로드 시작: History `downloads.start_time`을 정규화한 `ts_event` 사용
- 다운로드 완료 상태 갱신: History `downloads.end_time`을 정규화한 `ts_modified` 사용
- 파일 본문 관찰: Estuary Cache의 `response_time`을 `ts_record`로 사용
- URL chain: 원본 테이블에 독립 timestamp가 없어 download ID 연결 증거로만 사용

행위의 대표 시작 시각은 `2026-09-20T01:04:54.900725`이다. 완료 시각을 가장 잘 나타내는 값은 Chrome download의 `ts_modified=2026-09-20T01:05:14.463640Z`이다. Estuary 응답 시각은 본문이 관찰된 시각이지 로컬 파일 쓰기가 끝난 시각은 아니다.

### 원본 파일 보존과 해석 제한

제공된 ZIP은 Chrome 프로필 선별본으로 `Downloads` 디렉터리 자체를 포함하지 않는다. 따라서 대상 경로에 존재했던 독립 파일을 ZIP 내부에서 직접 확인한 것은 아니다. 다만 동일한 26,346바이트는 결과 폴더의 `_raw/cache/entries.jsonl`에 Cache body로 무손실 보존되고, Chrome History는 로컬 대상 경로와 완료 상태를 별도로 기록한다.

파일명이 `.py`로 끝나지 않으므로 확장자만으로 Python 파일이라고 판정하면 안 된다. 본문 signature와 구문 특징을 함께 사용한다. 반대로 Python 내용이 복원됐다는 사실은 파일 내용과 동일성을 뒷받침하지만, 다운로드 완료 판정 자체는 Chrome `downloads` 상태 필드에 근거한다.

### 포렌식적으로 유의미한 추가 정보

- conversation ID, message ID, file ID, download ID는 대화·생성 파일·브라우저 저장 기록을 연결하는 핵심 식별자다.
- sandbox path는 ChatGPT 실행 환경에서 만들어진 원래 파일 위치를 보여준다.
- 대상 경로의 `vboxuser`는 Windows 사용자 프로필 이름을 드러내지만, 별도 검증 없이 실제 사람 이름이나 장비 소유자로 단정하지 않는다.
- A10의 계정 ID, 이메일, 전화번호, 국가·지역 값은 A08에서 기록한 계정 문맥과 같다. 새로운 상세 주소·도시·우편번호는 확인되지 않았다.
- SHA-256, 파일 크기, Python 내용 판정은 Cache 본문과 향후 확보될 로컬 파일의 동일성 검증에 사용할 수 있다.

## A10 코드 반영 사항

- `llmtrace/profiles/chatgpt_web/mappings.py`
  - 다운로드 요청 Query에서 message ID, sandbox path, download intent를 정규화
  - Estuary와 URL chain의 `fn`, `cd`, file ID를 파일명·attachment 문맥으로 정규화
  - Chrome download에서 conversation ID, 대상 파일명, 완료 조건을 계산
  - UTF-8 텍스트와 Python 소스를 보수적으로 식별하고 줄 수를 기록
- `llmtrace/core/engine.py`
  - 이전 milestone의 식별자와 같은 observation만 선택하는 `first_same_ref` 선택 방식 추가
  - 소수점 자릿수가 다른 ISO timestamp도 실제 datetime으로 비교하도록 순서 판정 보완
- `llmtrace/profiles/chatgpt_web/rules.py`
  - B10 최소·표준·확장 단계와 완료 조건을 명시
  - conversation ID, file ID, download ID를 이용해 네 종류의 증거를 연결
  - 복원 파일 Context에 filename, SHA-256, Python 판정, 줄 수, raw dataset을 포함
- `tests/`
  - 다운로드 Query·History·URL chain·Python body 정규화 테스트 추가
  - 의도만 있는 최소 단계와 완료·식별자 연결이 있는 확장 단계 테스트 추가

## A11 대화 활성 목록 제거 실험

### 결론

A11 자료는 계정 삭제 실험이 아니다. 대화 `인사 대화`가 동일 계정·워크스페이스의 활성 대화 목록에서 사라진 상태 전이를 확인했다. 따라서 `ChatGPT 대화 활성 목록 제거`를 `R-CONVERSATION-ACTIVE-REMOVAL-STD-01`, `confirmed`로 판정한다.

다만 이 자료만으로 서버에서 대화가 영구 삭제됐다고 확정하지 않는다. 직접적인 DELETE 요청·성공 응답이나 `is_archived=true` 목록은 복원되지 않았다. 현재 안전한 결론은 “활성 목록 제거 확정, 대화 삭제 추정”이며, archive 또는 다른 활성 목록 제외 상태가 대안이다.

같은 수집본에는 일반 로그아웃과 다른 계정 로그인 흔적도 있다. 로그아웃 페이지의 명시적 값은 `logout_reason=logout_confirm_modal`, `logout_source=modal.logout-confirm-modal`이다. 계정 삭제 흐름의 값인 `confirm_delete_account`, `settings.confirm-delete-account-modal`은 없다. 따라서 로그아웃·클라이언트 정리 흔적을 계정 삭제로 해석하지 않는다.

### 연결된 증거

| 구분 | 관찰값 |
|---|---|
| 제거된 conversation ID | `6aaf4952-1c70-83e8-b5ac-bc9f496f9d4f` |
| 대화 제목 | `인사 대화` |
| 생성 시각 | `2026-09-20T02:47:46.841455Z` |
| 마지막 대화 갱신 시각 | `2026-09-20T02:50:15.417170Z` |
| 마지막 존재 스냅샷 | LevelDB seq `835`, `2026-09-20T03:36:56.732000Z`, 활성 목록 4개 |
| 최초 부재 스냅샷 | LevelDB seq `861`, `2026-09-20T05:36:34.123000Z`, 활성 목록 3개 |
| 후속 부재 확인 | LevelDB seq `885`, `2026-09-20T05:39:54.562000Z`, 활성 목록 3개 |
| 마지막 존재 상태 | `is_archived=false` |

관찰 흐름은 다음과 같다.

1. seq `804`와 `835`의 동일한 conversation-history 키에는 `인사 대화`를 포함한 대화 4개가 있다.
2. seq `861`에서는 다른 세 대화가 그대로 남고 `인사 대화`만 사라진다.
3. seq `885`에서도 같은 세 대화만 남는다. 일시적인 한 번의 파싱 누락보다는 지속된 활성 목록 제거에 부합한다.
4. 같은 conversation ID, user ID, workspace account ID가 전후 상태에서 연결되므로 다른 계정이나 다른 브라우저 프로필의 목록을 잘못 비교한 것이 아니다.

### 적용한 행위 규칙

활성 목록 제거 규칙의 기본 ID는 `R-CONVERSATION-ACTIVE-REMOVAL-01`, 행위 ID는 `B11`이다.

- 최소 단계: 같은 conversation ID가 이전 활성 목록에는 있고 다음 목록에는 없는 연속 상태가 복원되면 `presumed`이다.
- 표준 단계: 최소 조건에 더해 user ID와 workspace account ID가 전후에 모두 같으면 활성 목록 제거를 `confirmed`로 판정한다.
- 확장 단계: 직접적인 삭제 요청과 성공 결과까지 연결하는 조건은 아직 검증하지 않았으므로 `unavailable`이다.
- 해석 제한: 표준 단계도 “활성 목록에서 사라짐”을 확정할 뿐 “영구 삭제 완료”를 확정하지 않는다. archive나 다른 제외 상태를 함께 표시한다.

실제 A11 결과는 표준 단계다. 계정 삭제 규칙 `R-ACCOUNT-DELETE-FLOW-01`은 한 건도 생성되지 않았다.

### Timestamp 선택

- 이전 상태 시각: 대화가 마지막으로 포함된 Local Storage snapshot의 내부 `timestamp`를 정규화한 `ts_record`
- 다음 상태 시각: 대화가 처음 빠진 다음 snapshot의 내부 `timestamp`를 정규화한 `ts_record`
- 대화 생성·갱신 시각: 목록 항목의 `create_time`, `update_time`을 각각 `ts_created`, `ts_modified`로 보존

행위 시각은 `2026-09-20T03:36:56.732000Z` 이후, `2026-09-20T05:36:34.123000Z` 이전의 범위로만 말할 수 있다. 이는 마지막 존재와 최초 부재 사이의 증거 경계이며 정확한 삭제 버튼 클릭 시각이나 처리 시간은 아니다. 대화 자체의 `update_time`도 제거 시각으로 사용하지 않는다.

### 계정 삭제와 구분한 근거

- `/auth/logout` Cache HTML에서 일반 로그아웃 확인 모달 값이 직접 복원됐다.
- Local Storage `nextauth.message`에는 `event=session`, `data.trigger=signout`이 있다.
- 로그아웃 후 일부 계정별 Cache 키가 tombstone 상태로 남고 다른 계정 로그인 흐름이 이어진다. 이는 로그아웃·계정 전환 후 클라이언트 정리로 해석할 수 있지만 계정 삭제 성공의 증거는 아니다.
- 계정 삭제 전용 logout reason/source, 삭제용 Session Storage 키, 서버 측 삭제 성공 결과는 확인되지 않았다.

이에 따라 기존 `account_delete_cleanup` Context 이름은 일반적인 `signout_cleanup`으로 바꿨다. 일반 로그아웃·계정 전환·계정 삭제 흐름 모두 유사한 클라이언트 정리를 남길 수 있기 때문이다.

### 포렌식적으로 유의미한 추가 정보

- 제거된 대화가 속한 user ID는 `user-0dQYyh5dWY4cQ6UrL8ebcyty`, workspace account ID는 `3ad265d1-c035-44b0-9043-f6cadb5b42e8`이다.
- account switch session과 로그인 힌트에서 이메일 `jake31691@kakao.com`, 표시 이름 `황수연`이 복원됐다. 이 값은 계정 식별 정보이며 별도 검증 없이 실제 장비 소유자나 행위자의 신원으로 단정하지 않는다.
- 같은 활성 목록에 남은 제목은 `비빔밥 만들기_recipe`, `인터넷 작동 원리 설명`, `다음주 날씨 안내`다. 대화 제목은 내용·관심사를 드러낼 수 있는 포렌식 정보다.
- tombstone 키에는 과거 계정/워크스페이스 조합 `user-DjHloHx8W4rvCQ2ll7rXh1Ej / 2cab065c-a0d0-448c-b9f1-c187922ba107`과 `user-nXZRzepT2rAr2j9MXJCjJOTr / 48de9c92-ef11-4582-9186-b97bf1779a69`도 남아 있다. 현재 A11 대화의 소유 계정으로 연결하지 않고 과거 계정 사용 정황으로만 보존한다.
- 이 수집본에서 새로 확인된 전화번호·상세 주소·도시·우편번호는 없다.
- 세션 토큰 원문은 분석 결과나 이 문서에 복사하지 않는다.

## A11 코드 반영 사항

- `llmtrace/profiles/chatgpt_web/mappings.py`
  - 압축된 logout HTML에서 logout reason/source/correlation ID를 안전하게 추출
  - 같은 conversation-history 키의 연속 LevelDB snapshot을 비교
  - 이전에는 존재하고 다음에는 사라진 conversation을 명시적인 음성 membership observation으로 생성
  - 마지막 존재와 최초 부재 snapshot을 행위 시간 범위로 선택
- `llmtrace/profiles/chatgpt_web/rules.py`
  - B11 활성 목록 제거 최소·표준·확장 단계를 추가
  - 계정·워크스페이스·conversation ID가 같은 전후 상태만 연결
  - 삭제 완료를 단정하지 않고 archive 등 대안을 결과에 포함
  - 일반 logout marker와 account-delete marker를 분리하고 cleanup Context를 일반화
- `tests/`
  - Brotli logout HTML의 일반 로그아웃 marker 추출 테스트 추가
  - 연속 목록 snapshot에서 제거 상태를 생성하는 테스트 추가
  - 활성 목록 제거 판정과 일반 로그아웃의 계정 삭제 오탐 방지 테스트 추가

## A12 Temporary Chat 실험

### 결론

A12 자료에서는 임시챗 화면만 연 것이 아니라 실제 임시 대화에서 생성 요청까지 수행한 사실이 확인된다. 적용 결과는 `R-TEMPCHAT-USE-STD-01`, `confirmed`이다.

핵심은 `temporary-chat=true` 진입 기록 하나가 아니다. 같은 흐름에서 `history_off_<conversation_id>` 쿠키가 생성되고, 동일 conversation ID의 `IS_STREAMING` 상태가 확인된다. `conv_key_<conversation_id>`와 route-data Cache도 함께 존재한다.

### 연결된 증거

| 구분 | 관찰값 |
|---|---|
| 임시챗 진입 URL | `https://chatgpt.com/ko-KR/?temporary-chat=true` |
| 진입 시각 | `2026-09-20T05:39:18.109502Z` |
| route-data 관찰 | `/ko-KR.data?temporary-chat=true`, `2026-09-20T05:39:18.445467` UTC |
| 임시 conversation ID | `6aaf719d-4238-83e8-8c96-f8790b87e494` |
| history-off 쿠키 | `history_off_6aaf719d-4238-83e8-8c96-f8790b87e494` |
| history-off 생성 시각 | `2026-09-20T05:39:48.327004Z` |
| conversation-key 쿠키 | `conv_key_6aaf719d-4238-83e8-8c96-f8790b87e494` |
| stream 상태 | `IS_STREAMING` |
| stream 응답 관찰 시각 | `2026-09-20T05:39:49.237474` UTC |
| 후속 textdocs 응답 | `2026-09-20T05:39:49.628360` UTC |

관찰 흐름은 다음과 같다.

1. History에 `temporary-chat=true` URL 방문이 기록된다. 경로의 `ko-KR`은 언어 설정일 뿐이므로 규칙은 locale을 고정하지 않고 query 값을 사용한다.
2. 약 30초 뒤 `history_off_<id>`와 `conv_key_<id>` 쿠키가 거의 동시에 생성된다.
3. 약 0.9초 뒤 같은 ID의 `/backend-api/conversation/<id>/stream_status`가 `IS_STREAMING`을 반환한다. 따라서 단순 모드 진입이 아니라 실제 생성이 시작됐다.
4. 같은 ID의 `textdocs` 응답도 뒤따른다.
5. 이 conversation ID는 Chrome History의 `/c/<id>` 방문, Local Storage conversation-history 목록, 후속 활성 대화 목록에서 확인되지 않는다. 이는 임시챗의 비저장 성격과 일치하지만 음성 증거이므로 행위 판정의 필수 조건으로 사용하지 않는다.

### 적용한 행위 규칙

임시챗 규칙의 행위 ID는 `B12`이며, 모드 진입과 실제 사용을 분리한다.

- 모드 진입: History의 ChatGPT URL에서 `temporary-chat=true`가 확인되면 `R-TEMPCHAT-OPEN-MIN-01`이다. route-data Cache까지 있으면 진입 표준 단계다.
- 실제 사용 최소 단계: 임시챗 진입 후 `history_off_<id>` 쿠키와 같은 ID의 `IS_STREAMING`이 연결되면 `R-TEMPCHAT-USE-MIN-01`, `confirmed`이다.
- 실제 사용 표준 단계: 최소 조건에 더해 `temporary-chat=true` route-data Cache와 같은 ID의 `conv_key_<id>`가 있으면 `R-TEMPCHAT-USE-STD-01`, `confirmed`이다.
- 실제 사용 확장 단계: conversation snapshot에서 `is_temporary_chat=true`와 `is_do_not_remember=true`를 직접 확인하는 단계는 이번 자료에서 복원되지 않아 `unavailable`이다.
- 중복 억제: 실제 사용이 성립하면 같은 흐름의 단순 모드 진입 결과는 별도 B12 행으로 중복 출력하지 않는다.

`history_off`는 임시챗을 구분하는 핵심 표식이다. `conv_key`는 일반 대화에서도 사용될 수 있으므로 단독으로 임시챗을 판정하지 않는다. 두 쿠키와 stream은 반드시 동일 conversation ID여야 한다.

### Timestamp 선택

- 임시챗 진입: History `visit_time`을 정규화한 `ts_visit`
- route-data 확인: Cache `response_time`을 `ts_record`로 사용
- 임시 대화 ID 생성 문맥: `history_off` 쿠키의 `creation_utc`를 `ts_created`로 사용
- 실제 생성 확인: 같은 ID의 stream-status Cache `response_time`을 `ts_record`로 사용

행위 결과 범위는 진입 시각 `2026-09-20T05:39:18.109502Z`부터 stream 응답 관찰 시각 `2026-09-20T05:39:49.237474` UTC까지다. 이 31.127972초는 화면 진입부터 생성 상태 확인까지의 증거 범위이며, 전체 답변 생성 시간은 아니다.

### 복원되지 않은 내용과 해석 제한

- 임시 conversation의 전체 snapshot이 없어서 사용자 프롬프트 문장, assistant 답변, 대화 제목은 복원되지 않았다.
- `is_temporary_chat`와 `is_do_not_remember` 필드도 이 conversation에 대해서는 직접 확인하지 못했다.
- `IS_STREAMING`은 생성 실행을 확인하지만 답변이 정상 완료됐다는 뜻은 아니다. 완료 이벤트는 별도로 복원되지 않았다.
- History와 활성 목록에 conversation ID가 없다는 사실은 중요한 보조 정황이지만, 수집 범위 밖 기록까지 존재하지 않는다고 단정할 수는 없다.

### 포렌식적으로 유의미한 추가 정보

- 임시챗도 `history_off`와 `conv_key` 쿠키에 conversation ID를 남기므로 일반 대화 목록에 없어도 대화 단위를 식별할 수 있다.
- 동일 ID가 Cache의 stream-status와 textdocs 경로에도 남아 있어 쿠키와 네트워크 흔적을 연결할 수 있다.
- 계정 문맥은 A11과 동일한 user ID `user-0dQYyh5dWY4cQ6UrL8ebcyty`, workspace account ID `3ad265d1-c035-44b0-9043-f6cadb5b42e8`, 이메일 `jake31691@kakao.com`, 표시 이름 `황수연`이다. 별도 검증 없이 실제 행위자 신원으로 단정하지 않는다.
- 이번 자료에서 새로운 전화번호·상세 주소·도시·우편번호는 확인되지 않았다.

## A12 코드 반영 사항

- `llmtrace/profiles/chatgpt_web/rules.py`
  - 임시챗 모드 진입과 실제 사용 조건을 분리
  - history-off 쿠키와 stream-status를 conversation ID로 강제 연결
  - route-data와 conv-key를 표준 단계 보강 증거로 사용
  - 실제 사용이 확인되면 단순 진입 결과를 억제
  - stream-status를 종료 anchor로 사용해 열린 시간 범위 대신 실제 증거 범위를 출력
- `llmtrace/core/windows.py`
  - 소수점 정밀도가 다른 ISO timestamp를 문자열이 아닌 datetime으로 비교하도록 수정
- `tests/test_rules.py`
  - 다른 conversation의 stream을 잘못 연결하지 않는지 검증
  - 표준 단계 판정, 단순 진입 억제, 시작·종료 시각을 검증

## A13 Google Connector 연동 및 Drive/Gmail 사용 실험

### 입력 자료와 결론

입력 ZIP은 `Default 내부 선별 (11).zip`이고 SHA-256은 `C0F44D9B2E49092CD1294E2D4836D02A0B097F00F2684C14CE875A6B8927C390`이다. 내부의 `13-1`은 연동 과정, `13-2`는 연동 후 프롬프트 사용 과정으로 나누어 분석했다.

A13-1에서는 Google OAuth 시작과 ChatGPT Connector 콜백의 `state`가 정확히 일치한다. 콜백에는 authorization `code`가 있고 후속 connector backend 응답도 있다. 따라서 Google Connector OAuth 연동 흐름을 `R-GOOGLE-CONNECTOR-LINK-STD-01`, `confirmed`로 판정한다. 이는 Google OAuth bundle 연동을 뜻하며 Gmail, Drive, Calendar 등 각 서비스를 모두 실제 사용했다는 뜻은 아니다.

A13-2에서는 Google Drive 문서 검색과 피드백 파일 업로드를 요청하는 draft, 새 Drive 대화 방문, 같은 conversation ID의 `IS_STREAMING`, 같은 대화에서 렌더링된 `drive.google.com/file/d/<id>` 링크가 연결된다. 따라서 “Google Drive Connector 작업 결과에 Drive 파일 링크가 생성됨”을 `R-GOOGLE-DRIVE-CONNECTOR-RESULT-STD-01`, `confirmed`로 판정한다.

다만 URL safety check는 ChatGPT 응답에 외부 링크가 표시됐다는 흔적이지 Google 측 업로드 성공 영수증이 아니다. 이 자료만으로 그 링크가 새로 업로드한 피드백 파일인지, 검색·읽기로 찾은 기존 Drive 파일인지까지 확정하지 않는다. 사용자가 실제 플러그인 동작을 후속 확인했다는 실험 설명은 ground truth로 참고하지만, 자동 판정은 수집본 안의 증거만 사용한다.

Gmail은 `이메일 확인 답장 전송`이라는 대화 제목과 conversation ID가 복원됐다. 그러나 사용자 프롬프트, Gmail tool request/result, 메시지 ID, 전송 성공 결과는 복원되지 않았다. 따라서 Gmail 사용 의도·대화 정황만 Context로 남기고 읽기·답장 작성·실제 전송 중 어느 행위가 완료됐는지는 확정하지 않는다.

### A13-1 연동 증거

| 구분 | 관찰값 |
|---|---|
| OAuth 시작 | `accounts.google.com/o/oauth2/v2/auth` |
| 시작 시각 | `2026-09-20T08:07:38.763190Z` |
| redirect URI | `https://chatgpt.com/connector_platform_oauth_redirect` |
| OAuth state | `oauth_s_6aaf946323d08191930d4d195c513be4` |
| Connector 콜백 | `chatgpt.com/connector_platform_oauth_redirect` |
| 콜백 방문 시각 | `2026-09-20T08:08:24.334592Z` |
| 콜백 code | 존재함. 원문은 보고서에서 증거로 보존하되 문서에는 복사하지 않음 |
| 후속 backend | `/backend-api/aip/connectors/.../actions` 등 |
| 규칙 결과 | `R-GOOGLE-CONNECTOR-LINK-STD-01`, `confirmed` |

요청 scope에는 Gmail modify, Drive 전체·읽기·파일, Calendar, Contacts, Docs, Sheets, Slides 계열 권한이 포함된다. Scope는 승인 요청 범위를 보여 줄 뿐 각 서비스 사용 사실을 증명하지 않는다. `/actions` 응답은 Gmail의 가능한 작업 목록과 `Write` capability를 보여 주지만 특정 메일에 대한 실행 요청이나 성공 결과가 아니다.

Google 로그인 화면의 History 제목에서 `황수연`이 복원됐다. 이는 OAuth UI에 표시된 계정 이름 후보이므로 법적 신원, 장비 소유자 또는 실제 행위자로 단정하지 않는다.

### A13-2 Drive 결과 연결 증거

| 구분 | 관찰값 |
|---|---|
| draft 시각 | `2026-09-21T09:45:08.272Z` |
| draft 표식 | `[GPT_WEB_13-search]`, `[GPT_WEB_13-make-uproad]` |
| conversation ID | `6ab0fcaa-11a8-83e8-9a8f-a65ee9bf3bfe` |
| 대화 제목 | `드라이브 문서 피드백 작성` |
| 대화 방문 시각 | `2026-09-21T09:45:28.968368Z` |
| 생성 상태 | 같은 conversation ID의 `IS_STREAMING` |
| stream 응답 시각 | `2026-09-21T09:45:29.458309` UTC |
| 결과 링크 확인 시각 | `2026-09-21T09:46:03.309169` UTC |
| 결과 host | `drive.google.com` |
| 외부 Drive file ID | `1e3xQp-xdzVFlc_YQo87Vo6Ex_ckk5PPj` |
| 규칙 결과 | `R-GOOGLE-DRIVE-CONNECTOR-RESULT-STD-01`, `confirmed` |

draft만으로 실행을 판정하지 않는다. draft는 전송되지 않은 입력도 남길 수 있다. A13-2의 표준 단계는 draft 뒤에 실제 대화 방문과 생성 stream이 발생하고, 두 Cache 흔적이 동일 conversation ID로 연결되며, 그 대화의 결과 링크가 Drive file URL이어야 한다.

별도로 `드라이브 확인 및 업로드` 대화(`6ab0fb38-8f5c-83ee-94af-3673761cc3a0`)도 활성 목록과 History에서 확인된다. 이 대화에는 stream 정황이 있지만 현재 규칙이 요구하는 draft와 Drive 결과 링크의 완전한 연결이 없어 별도 confirmed 행위를 만들지 않는다.

### Gmail 정황과 제한

| 구분 | 관찰값 |
|---|---|
| conversation ID | `6ab09ce5-9a6c-83ee-8298-d0df0fdc6c53` |
| 제목 | `이메일 확인 답장 전송` |
| 생성 시각 | `2026-09-21T02:56:38.633177Z` |
| 갱신 시각 | `2026-09-21T02:57:35.710315Z` |
| 판정 | `CTX-GMAIL-CONVERSATION-01` Context만 생성 |

제목은 이메일 관련 작업 대화가 있었다는 정황이다. 제목만으로 Gmail connector가 실제로 메일을 읽었거나 답장을 보냈다고 판단하지 않는다. 이 판정을 높이려면 같은 conversation ID의 사용자 메시지와 Gmail tool call, 대상 메시지·스레드 ID, provider result 또는 전송 성공 결과가 추가로 필요하다.

### 적용한 행위 규칙

A13의 행위 ID는 `B13`이고 연동과 사용 결과를 별도 규칙으로 판정한다.

- 연동 최소 단계: Google OAuth 시작과 connector callback이 있고 두 URL의 `query.state`가 같으면 `R-GOOGLE-CONNECTOR-LINK-MIN-01`이다.
- 연동 표준 단계: 최소 조건에 더해 후속 connector backend 응답이 있으면 `R-GOOGLE-CONNECTOR-LINK-STD-01`, `confirmed`이다.
- 연동 확장 단계: provider 측 grant/audit record까지 연결하는 조건은 아직 검증하지 않아 `unavailable`이다.
- Drive 결과 최소 단계: Drive 검색·업로드 의도의 draft, Drive 제목의 대화 방문, 같은 conversation ID의 Drive 파일 결과 링크가 연결되면 `R-GOOGLE-DRIVE-CONNECTOR-RESULT-MIN-01`, `presumed`이다.
- Drive 결과 표준 단계: 최소 조건에 같은 conversation ID의 `IS_STREAMING`을 추가하면 `R-GOOGLE-DRIVE-CONNECTOR-RESULT-STD-01`, `confirmed`이다.
- Drive 결과 확장 단계: provider tool request/result 또는 Google audit record로 정확한 검색·읽기·생성·업로드 sub-action을 확인하는 단계는 `unavailable`이다.
- 제외 조건: 단순 plugin mode, connector descriptor, `/actions` capability 목록, conversation 제목, draft 키워드는 각각 단독 행위 증거로 사용하지 않는다.

### Timestamp 선택

- OAuth 시작과 콜백은 Chrome History `visit_time`을 정규화한 `ts_visit`을 사용한다.
- Connector backend, stream-status, URL safety check는 Cache `response_time`을 `ts_record`로 사용한다.
- Drive 입력 의도는 draft 내부 millisecond timestamp를 `ts_modified`로 사용한다.
- 대화 제목의 `create_time`과 `update_time`은 각각 `ts_created`, `ts_modified`로 보존한다. Local Storage snapshot 관찰 시각과 대화 자체 갱신 시각을 혼동하지 않는다.

연동 결과의 증거 범위는 `2026-09-20T08:07:38.763190Z`부터 `2026-09-20T08:08:24.334592Z`까지다. Drive 결과의 증거 범위는 draft 시각 `2026-09-21T09:45:08.272Z`부터 Drive 링크 확인 시각 `2026-09-21T09:46:03.309169` UTC까지다. 두 범위 모두 UI 전체 체류 시간이나 provider 작업 소요 시간을 뜻하지 않는다.

### 포렌식적으로 유의미한 추가 정보

- ChatGPT user ID는 `user-nXZRzepT2rAr2j9MXJCjJOTr`, workspace account ID는 `48de9c92-ef11-4582-9186-b97bf1779a69`, organization ID는 `org-iQDUiFGCKsAL4eSbeXny0i5i`다.
- 계정 문맥에서 이메일 `ghkdtndus000@gmail.com`, 표시 이름 `수연`, 전화번호 `+821071935259`, 국가 `KR`, 지역 `Gyeonggi-do`, 지역 코드 `41`이 복원됐다.
- Google OAuth UI 표시 이름 후보는 `황수연`이다. ChatGPT의 표시 이름과 유사하지만 별도 검증 없이 동일 인물, 실제 사용자 또는 장비 소유자로 단정하지 않는다.
- Drive 결과의 conversation ID와 외부 file ID는 대화와 외부 파일을 연결하는 핵심 식별자다.
- OAuth authorization code와 세션 토큰 같은 인증 비밀 원문은 요약 문서에 복사하지 않는다.
- 이번 자료에서 상세 도로명 주소, 우편번호 또는 추가 도시 정보는 확인되지 않았다.

### A13 코드 반영 사항

- `llmtrace/profiles/chatgpt_web/mappings.py`
  - `/backend-api/conversation/<id>/url_safe`를 `url_safety_check`로 정규화
  - conversation ID, target URL·host·path, Drive file ID를 분리 저장
- `llmtrace/profiles/chatgpt_web/rules.py`
  - 기존 OAuth 규칙이 존재하지 않는 `oauth_state` 별칭을 비교하던 오류를 수정하고 실제 `query.state`를 비교
  - 연동 규칙의 종료 시각을 connector callback으로 선택
  - Drive draft·대화·stream·result link를 연결하는 최소·표준 규칙 추가
  - OAuth scope, Google UI 표시 이름 후보, Gmail 대화 정황 Context 추가
  - 특정 provider sub-action은 직접 증거가 없으면 unresolved로 유지
- `tests/`
  - URL safety check 정규화 테스트 추가
  - 동일/불일치 OAuth state 테스트 추가
  - Drive 표준 판정과 draft 단독 오탐 방지 테스트 추가
  - A13 반영 시점 전체 49개 테스트 통과

## A01~A13 누적 규칙 정합성 점검

A13 반영 뒤 공통 엔진, 실제 2차 Observation 구조, 샘플, HTML 보고 계층을 다시 대조했다. 이 점검에서 A02 로그인 규칙이 실제 URL 파서가 생성하는 중첩 `query` 필드가 아니라 샘플에만 있던 최상위 편의 필드 `oauth_state`, `auth_session_logging_id`를 우선 사용한다는 문제가 확인됐다. 실제 History 자료에서는 올바른 로그인 흐름이 누락될 수 있으므로 다음과 같이 정리했다.

- 로그인 시작·authorize·callback의 session/state 연결은 각각 `query.auth_session_logging_id`, `query.state`를 사용한다.
- 기존에 만들어 둔 Observation JSONL 호환을 위해 최상위 별칭은 association policy의 후순위 fallback으로만 남긴다.
- 샘플 JSONL에서 실제 파서가 만들지 않는 최상위 별칭을 제거했다.
- 동일 nested state는 로그인으로 연결되고, 다른 state는 연결되지 않는 회귀 테스트를 추가했다.
- 규칙 시작·milestone·end의 ref, association policy, Minimum/Standard/Extended 단계의 ref가 서로 맞는지 `validate_ruleset_shape()`가 실행 전에 검사하도록 보강했다.
- `context.py`에 A13 외부 Connector 결과 링크 분류를 추가하고, 공통 HTML의 Current Useful Information에서 분류별 Observation 건수와 fact type을 보이도록 연결했다.

점검 후 전체 51개 자동 테스트가 통과했다. A13 원자료 두 건도 같은 최종 출력 폴더에 다시 분석했고 모두 `problems=0`이었다. A13-1은 67개 행위 중 B13 연동 1건, A13-2는 66개 행위 중 B13 Drive 결과 1건으로 기존 핵심 판정이 유지됐다. 동시에 실제 nested query를 쓰는 과거 로그인 흐름 B02가 각 자료에서 2건씩 정상 복원되어, 이 수정이 단순 샘플 대응이 아니라 실제 누락을 해소했음도 확인했다. 이 정합성 수정은 증거 없이 새 행위를 추가하거나 A13의 provider 세부 행위를 과장하는 변경은 아니다.

## 다음 실험 기록 형식

이후 실험을 분석할 때 다음 항목을 이 문서에 추가한다.

1. 실험 전·후 자료와 의도한 행위
2. 새로 생긴 원본 레코드와 사라진 레코드
3. 반복 관찰 가능한 행위 고유 특징
4. 계정·사용자·대화·메시지·파일·위치성 정보
5. `fact_type`과 대표 timestamp 선택
6. 최소/표준/확장 규칙과 제외 조건
7. 단정할 수 없는 사항과 대안 설명
8. 코드·Context·테스트 반영 내역
