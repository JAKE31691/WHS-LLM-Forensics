# Claude Web(Chrome) 도구: 14개 행위 판정 범위 (v2.0.0)

## v1.2.0에서 바뀐 것
- **판정 방식:** 파일 바이트 검색 → 파싱한 레코드의 필드 매칭. LevelDB(Local Storage), IndexedDB는 ccl_chromium_reader로 푼다.
- **규칙:** v1.2.0의 규칙 19개 + 다운로드 판정 1개를 그대로 옮겼다(D7=A, A07·A10 포함). 미지원은 A04, A06, A12.
- **읽는 범위:** 압축된 `.ldb` 안 레코드와 IndexedDB `.blob` 폴더의 값까지 읽는다. v1.2.0에서 "놓칠 수 있다"고 적었던 한계다.
- **사이트 구분:** 쿠키는 도메인이 `claude.ai`(또는 그 하위 도메인)인 것만, History URL 규칙은 호스트가 `claude.ai`인 URL만(`gmailmcp` 규칙 제외, OAuth 주소가 Google 쪽이라서), Local Storage는 `storage_key = https://claude.ai`만 본다. v1.2.0은 사이트를 구분하지 못해 `https://www.google.com/search?q=claude.ai/logout` 같은 URL도 로그아웃으로 잡을 수 있었다.
- **시각 열 추가:** 규칙마다 시각으로 쓸 필드를 정했다(아래 "시각").

## 행위별 규칙과 시험 결과

시험 자료: Default_01(접속만), Default_07(로그인 후 프롬프트 수정), Default_10(실험 10, 데이터 내보내기로 수행).
"검출"은 흔적 발견이지 행위 확정이 아니다. "미검출"은 흔적이 없다는 뜻이지 행위를 안 했다는 뜻이 아니다.

| ID | 행위 | 규칙 | 아티팩트: 조건 | 시각 | 등급 | 시험 결과 |
|---|---|---|---|---|---|---|
| A01 | 사이트 접속 | R-A01-01 | History(claude.ai) `urls.url` ∋ `https://claude.ai/` | last_visit_time | 직접 | 01·07·10 검출 |
| | | R-A01-02 | Cookies(claude.ai) `name` = `anthropic-device-id` | creation_utc | 보조 | 01·07·10 검출 |
| A02 | 로그인 | R-A02-01 | Cookies(claude.ai) `name` = `sessionKey` | creation_utc | 직접 | 07·10 검출, 01 미검출(로그인 안 함) |
| | | R-A02-02 | Cookies(claude.ai) `name` = `lastActiveOrg` | creation_utc | 보조 | 07·10 검출 |
| | | R-A02-03 | History `urls.url` ∋ `https://claude.ai/new` | last_visit_time | 보조 | 07·10 검출. A13의 `new?incognito`도 걸린다 |
| | | R-A02-04 | Local Storage `script_key` = `lastLoginMethod` | 없음 (`-`) | 보조 | 07·10 검출. **07은 `.ldb` 안에 있어 v1.2.0은 못 찾음** |
| A03 | 로그아웃 | R-A03-01 | History(claude.ai) `urls.url` ∋ `claude.ai/logout` | last_visit_time | 직접 | 이번 시험 자료 없음 (v1.2.0에서 Default3 검출) |
| | | R-A03-02 | Local Storage `script_key` ∋ `-logout-at` | 값 (시각이면) | 보조 | 이번 시험 자료 없음 |
| A04 | 다중 계정 | - | - | - | - | 미지원 |
| A05 | 새 대화 생성 | R-A05-01 | History `urls.url` ∋ `https://claude.ai/chat/` | last_visit_time | 보조 | 07·10 검출. 기존 대화를 열어도 남는다 |
| | | R-A05-02 | Local Storage `script_key` ∋ `claude-conversation-ledger` | 없음 | 보조 | 세 폴더 모두 없음 (v1.2.0도 미검출) |
| | | R-A05-03 | IndexedDB `**.parent_message_uuid` = `00000000-0000-4000-8000-000000000000` | 그 메시지의 created_at | 보조 | 10 검출 |
| A06 | 프롬프트 입력 | - | - | - | - | 미지원 |
| A07 | 프롬프트 수정 | R-A07-01 | Local Storage(`antalytics_queue_v1`) `value_json[*].eventName` = `claudeai.message.edit_started` | 그 이벤트의 eventTimestamp | 보조 | 07 검출 2건. **v1.2.0은 1건** (나머지 1건은 `.ldb` 안) |
| A08 | 메모리 기능 사용 | R-A08-01 | IndexedDB `**.name` = `memory_write` | start_timestamp, 없으면 created_at | 직접 | 시험 자료 없음. 아래 1번 참고 |
| A09 | 파일 업로드 | R-A09-01 | IndexedDB `**.file_kind` = `document` | 그 파일의 created_at | 직접 | 10에는 `image`만 있어 미검출(정상) |
| A10 | 파일 다운로드 | R-A10-01 | History `downloads`: tab_url/referrer의 호스트가 `claude.ai`, 또는 `downloads_url_chains.url` ∋ `wiggle/download-file` | start_time | 직접 | **10의 downloads 표가 비어 있어 미검출** (실험 10은 데이터 내보내기) |
| A11 | 대화 삭제 | R-A11-01 | IndexedDB `value.tombstone` = `true` | deletedAt | 직접 | 시험 자료 없음. 구조는 아래 2번으로 확인 |
| A12 | 계정 삭제 | - | - | - | - | 미지원 |
| A13 | 임시 채팅 | R-A13-01 | History `urls.url` ∋ `claude.ai/new?incognito` | last_visit_time | 직접 | 시험 자료 없음 |
| A14 | 플러그인 사용 | R-A14-01 | History `urls.url` ∋ `claude.ai/api/mcp/auth_callback` | last_visit_time | 직접 | 시험 자료 없음 |
| | | R-A14-02 | History `urls.url` ∋ `claude.ai/customize/connectors` | last_visit_time | 보조 | 시험 자료 없음 |
| | | R-A14-03 | History `urls.url` ∋ `gmailmcp` | last_visit_time | 보조 | 시험 자료 없음 |

## 바이트 검색을 필드 매칭으로 옮기면서 정한 것

1. **A08 `memory_write`는 tool_use 이름만 본다.**
   - Default_10의 IndexedDB `.blob` 값(`react-query-cache`) 안 GrowthBook 기능 설정에 `tools.memory_write.kind = "write"`가 **키 이름**으로 들어 있다. 메모리를 쓰지 않은 프로필에도 있는 설정이다.
   - claude.ai 스크립트(같은 폴더의 캐시)에서 `memory_write`는 `tool_use` 블록의 `name` 값이다 (`u.type==="tool_use"`, `u.name`, `="memory_write"`).
   - 그래서 키 이름은 제외하고 값이 `memory_write`인 `name` 필드만 본다. 이 경우는 단위 시험(`test_memory_feature_flag_is_not_memory_use`)으로 확인했다.
   - **확인 필요:** `react-query-cache`가 작아 `.blob`으로 빠지지 않은 상태였다면 v1.2.0 바이트 검색도 이 설정을 메모리 사용으로 잡았을 수 있다. 분석 08 실험 폴더로 v2.0.0을 돌려 보면 확인된다.
2. **A11 삭제 표시 구조는 claude.ai 스크립트로 확인했다.**
   - Default_07·10 캐시의 `shared-common-*.js`에 삭제 표시를 판별하는 코드가 있다: `e.tombstone===!0&&typeof e.deletedAt=="number"`.
   - 삭제 표시를 만드는 코드: `{conversationUuid, accountUuid, orgUuid, deletedAt, lastOpenedAt, tombstone:!0}`.
   - 그래서 `value.tombstone = true`이고, 시각은 `deletedAt`(밀리초)이다. v1.2.0 근거(분석11·검증11의 `tombstone`, `deletedAt`)와 같은 내용이다.
3. **A09:** v1.2.0 바이트 `file_kind"\x08document`(V8 직렬화)를 풀어 쓴 것이 `file_kind = "document"`다.
4. **A05:** `00000000-…`은 `childrenByParentUuid` 같은 곳에 키 이름으로도 나온다. 그래서 `parent_message_uuid` 값만 본다.
5. **A07:** 이 문자열은 분석 이벤트 큐(`antalytics_queue_v1`) 값, 즉 JSON 배열 안 이벤트의 `eventName`이다. 이벤트마다 `eventTimestamp`가 있어 시각으로 쓴다.

6. **IndexedDB 예전 기록의 `.blob` 값은 넣지 않는다.** ccl은 `.blob` 파일을 키로만 찾아, 같은 키의 예전 기록에도 최신 blob 내용을 붙인다. Default_10의 `react-query-cache` 예전 기록 3건이 그랬다(예전 blob 파일은 지워져 있음). 그래서 예전 기록은 값을 null로 두고 `value_note`에 이유를 적는다. 이 때문에 A05 IndexedDB 행의 레코드가 5건에서 2건(실제 값)으로 줄었다.

## 시각
- **History:** `urls.last_visit_time`은 그 URL의 **마지막** 방문 시각이다. 첫 방문 시각이 아니다. 방문 하나하나의 시각은 JSON의 `visits` 표에 있다.
- **Cookies:** `creation_utc`, 쿠키가 만들어진 시각이다.
- **Local Storage:** 값 안에 시각이 없으면 `-`로 쓴다. ccl이 계산해 주는 묶음(batch) 시각은 쓰지 않는다. 시험 자료에서 그 시각이 레코드 내용보다 이르게 나왔다.
  - Default_07에서 18:15:19Z 이벤트가 든 레코드의 묶음 시각이 18:14:40Z였다.
  - `lastLoginMethod` 묶음 시각(03:14:40 KST)이 `sessionKey` 생성 시각(03:14:54 KST)보다 이르다.
- **IndexedDB:** 맞은 값이 든 객체의 `created_at` 등(메시지·파일·이벤트 자체의 시각)이다.
- **표기:** 원본에 초 미만 값이 있는 형식(WebKit 마이크로초, 밀리초, 소수가 있는 ISO)이면 `.FFF`를 붙이고, 반올림하지 않고 자른다.

## v1.2.0과 규칙별 비교 (tests/regression_v120.py)

| 폴더 | 같음 | 다름 |
|---|---|---|
| Default_01 | 20개 전부 | 없음 |
| Default_07 | 19개 | R-A02-04: v1.2.0 미검출 → v2.0.0 검출 (`000005.ldb` 안). R-A07-01은 둘 다 검출이지만 v2.0.0이 1건 더 찾음 (`.ldb`) |
| Default_10 | 20개 전부 | 없음 |

## 그 밖의 확인
- **Python 버전:** 3.14(dissect.target 3.25.2.dev83), 3.13(3.25.1), 3.10에서 CSV의 시각~등급 열이 같다.
- **E01:** 재구성 자료로 시험했다. NTFS 볼륨에 사용자 2명, 프로필 3개(Default_10 / Default_01 / Default_07)를 복사하고 ewfacquire로 E01을 만들었다. 결과는 세 폴더를 따로 돌린 결과의 합과 같았다.
- **근거 경로:** 결과 4개의 근거 key=value 112개가 전부 JSON 안의 실제 값과 일치했다 (`tests/check_evidence_paths.py`, 잘린 값은 표시한 글자 수 `(총 N자)`까지 대조).
- **별도 검수:** 코드를 만들지 않은 검수자가 명세와 대조해 찾은 문제(잠긴 History를 빈 DB로 읽음, 예전 .blob 값, rules.csv 오타 시 중단, 빈 폴더 실패 처리, URL 부분 문자열 오탐, 300자 계산, ISO 정밀도 등)를 고치고 단위 시험(24개)에 넣었다.

## 결과 상태 (v2.0.0 이후 추가)
- 행이 없는 아티팩트는 사용자·프로필마다 `경로 없음` / `있지만 읽기 실패` / `읽었지만 매칭 없음`으로 나눠 적는다. 행위 하나에 섞여도 대표 상태로 묶지 않는다.
- E01을 읽다 오류가 난 경로는 더 이상 "없음"으로 처리하지 않는다. 이전에는 세그먼트가 빠진 E01에서 사용자 하나(whs4user)가 경고 없이 빠졌다.

## 한계
- **시험 자료로 확인하지 못한 규칙:** A03, A05-02, A08, A10, A11, A13, A14. 보관한 실험 폴더로 `tests/run_on_profile.py`를 돌리면 확인할 수 있다.
- **History 시각:** 마지막 방문 시각이다.
- **쿠키 값:** 복호화하지 않는다(m9). 이름과 시각만 본다.
- **live:** Chrome이 켜져 있으면 잠긴 파일은 복사 실패로 남는다.
- **E01 드라이브 문자:** dissect가 인식한 시스템 볼륨(Users 폴더가 있는 볼륨)을 C:로 쓴다.
- **Python 3.14의 E01 입력:** dissect.target 개발판에 의존한다. 정식판을 쓰려면 Python 3.13으로 실행한다.
- **IndexedDB 대화 캐시:** 서버에서 동기화된 대화도 같은 모양으로 남는다. 그래서 A05·A08·A09가 이 PC에서 한 행위인지는 IndexedDB만으로 구분할 수 없다(v1.2.0 범위표의 A05 설명과 같은 한계).
- **경로 열:** LevelDB·IndexedDB는 폴더 단위다. 값이 들어 있던 파일(`.ldb`, `.log`)과 `.blob` 위치는 JSON 레코드의 `file`, `external_value`에 있다.

## 출처
- claude.ai 클라이언트 스크립트: Default_07·10의 `Cache/Cache_Data`에 남은 `assets-proxy.anthropic.com/claude-ai/v2/assets/v1/shared-common-*.js`, `shared-*.js`
- [ccl_chromium_reader](https://github.com/cclgroupltd/ccl_chromium_reader) (MIT)
- [dissect.target](https://github.com/fox-it/dissect.target)
- [V8 value-serializer.cc](https://chromium.googlesource.com/v8/v8/+/refs/heads/main/src/objects/value-serializer.cc)
