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
| `LLM_Artifact_Report.csv` | `시각`, `사용자 행위`, `아티팩트`, `경로`, `등급`, `근거` 여섯 열의 UTF-8 BOM 보고서. 시각은 확인된 행위 시각의 KST 값이며 근거는 파싱 기록의 위치와 매칭값이다. |
| `LLM_Artifact_Report.html` | CSV와 동일한 행을 행위별 아코디언으로 표시하는 독립 실행형 보고서. |

근거 값 뒤에는 확인 가능한 원본 위치를 표시합니다. JSON은 1부터 시작하는 줄·문자 열 번호, JSONL과 로그는 원본 줄 번호, 캐시 원시 문자열은 0부터 시작하는 파일 바이트 오프셋(10진수·16진수)과 매칭 길이를 기록합니다. 복합 문자열 조건은 각 구성 문자열의 첫 매칭 위치를 표시합니다. SQLite의 테이블·컬럼과 레지스트리의 키·값 이름은 논리 위치로 구분하며, 파서가 물리 위치를 제공하지 않으면 `확인 불가`로 표시합니다. 압축 캐시에서 복원한 응답의 위치를 원본 파일 오프셋으로 추정하지 않습니다.

`Cache_Data`는 ZIP에서 복원한 수집본 디렉터리에 `ccl_chromium_reader.ccl_chromium_cache`의 공식 추출 흐름을 적용합니다. CCL의 `cache_report.csv`와 추출된 응답 본문을 읽어 `parsed_artifacts.json`에 통합하고, 임시 추출 파일은 분석 후 정리합니다. 기존 행위 조건에 필요한 원본 파일 문자열 검사도 수집본에서 수행합니다. 캐시 데이터 파일이 남아 있지만 CCL 인덱스에서 항목을 찾지 못하면 부분 파싱으로 기록합니다.

캐시의 구조 조건은 CCL로 복원한 JSON에서 확인합니다. 업로드는 메시지 `metadata.attachments`, 다운로드는 메시지 `content.parts`의 `다운로드]`를 사용하며, 메모리·업로드·다운로드의 시각은 매칭된 메시지 자신의 `update_time`입니다. 대화 전체의 시각이나 다른 메시지의 시각을 대신 사용하지 않습니다. 기존 원시 문자열 검사는 관측 기록으로 보존하지만 새 구조 조건을 대신하지 않습니다. 메모리의 원시 마커, 프롬프트 수정과 계정 삭제의 기존 복합 마커 조건은 유지합니다.

물리적 파일 하나가 여러 행위·아티팩트 정의에 연결될 수 있습니다. ZIP에는 한 번만 저장하고, CLI의 아티팩트별 `Found`에는 해당하는 각 이름으로 표시합니다. 따라서 `Found` 값의 합이 전체 고유 파일 수보다 클 수 있습니다.

수집 대상은 KAPE Target의 `Path`·`FileMask`·`Recursive` 방식으로 해석합니다. 등록된 파일 경로는 상위 폴더와 파일명 마스크로 분리하고, 등록된 디렉터리 아티팩트는 해당 폴더 아래를 재귀 수집합니다. 사용자 프로필 변수는 E01과 Live에서 발견한 프로필로 확장합니다. KAPE 실행 파일이나 외부 `.tkape` 파일은 필요하지 않습니다.

KapeFiles의 `RegistryHivesSystem.tkape`, `RegistryHivesUser.tkape`, `Amcache.tkape`에 따라 등록된 `SOFTWARE`, `NTUSER.DAT`, `Amcache.hve`와 해당 `*.LOG*` 트랜잭션 파일을 함께 수집합니다. 로그는 ZIP과 manifest에 `companion`으로 보존하며 원본 hive의 증거 행이나 행위 판정으로 계산하지 않습니다. 현재 레지스트리 파서는 트랜잭션 로그를 hive에 재생하지 않으므로, 파싱 결과는 수집 당시 hive 파일의 내용입니다.

행위별 증거 등급은 명시된 정의를 따릅니다. 판정 조건이 있는 정의는 해당 조건을 충족해야 보고서에 기록됩니다. 조건이 없는 정의는 수집에 성공한 파일을 해당 행위의 아티팩트 목록에 기록하며, 이것만으로 사용자 행위가 확정됐다는 뜻은 아닙니다. 파일 부재, 수집 실패, 파싱 실패는 각각 구분해 기록합니다.

원본 E01과 Live 파일은 수정하지 않습니다. ZIP manifest에는 원본 경로, ZIP 내부 경로, 크기, 수집 상태, SHA-256이 기록됩니다. 결과 파일에는 사용자 경로와 대화 관련 정보가 포함될 수 있으므로 공개 저장소에 올리지 마세요.


### 원본 위치와 보고서 스타일

참조: [LLMTrace 고정 커밋](https://github.com/JAKE31691/WHS-LLM-Forensics/tree/942f43e3c8b5a36d35955b06be27433a73f9cb76/chatgpt_web/LLMTrace_TeamShare_Only).
원본 위치 표기는 공통 parser의 논리 위치 방식을 참고하고, HTML은 `core/team_share.py`의 패널·행위 배지·등급 배지 스타일을 적용한다. 웹 분석용 판정 규칙은 가져오지 않는다.

- SQLite `source_locations`: `table=…;row=…`. 행은 1부터 시작하는 SELECT 결과 순번이며 SQLite rowid나 물리 오프셋이 아니다.
- 캐시 `source_locations`: 안전하게 처리된 키와 `export_row=…`. 기존 CCL CSV 내보내기 순번이며 원본 캐시 버전 번호는 제공하지 않는다. 비밀값이 포함될 수 있는 키는 기존 해시/생략 처리를 유지한다.
- JSON의 원본 줄·열, JSONL/로그 줄 번호, 원시 캐시 매칭 바이트 오프셋은 기존 방식으로 유지한다.
- CSV/HTML의 근거에는 논리 위치와 물리 오프셋 확인 여부를 구분해 표시한다. 판정 데이터와 등급은 동일하게 사용한다.
- 로그인의 Chrome Cookies 및 플러그인 사용의 config.toml은 수집·판정 목록에서 제외한다. 공통 파일 형식 파서는 유지한다.


### `source_location` (parsed_artifacts.json 스키마 1.2)

수집 단계에서는 파일 경로를 확인하고, 파일 내부 위치는 파서가 레코드를 읽을 때 확인한다. 각 아티팩트의 `source_location` 배열은 `records`의 내용과 별도로 저장하여 행위 판정에 영향을 주지 않는다.

```json
{
  "record_path": [0, "rows", 33],
  "_source_locator": "table=visits;row=34",
  "kind": "logical",
  "basis": "1-based SELECT order, not SQLite rowid",
  "source_path": "C:\\Users\\example\\History"
}
```

`record_path`는 해당 아티팩트의 `records`를 기준으로 한 JSON 경로이고 배열 인덱스는 0부터 시작한다. CSV 근거에는 실제 저장 위치를 참조한다.

```text
artifacts[0].records[0].rows[33].url="https://chatgpt.com/"; artifacts[0].source_location[0]._source_locator="table=visits;row=34"
```

- `table=…;row=…`: 1부터 시작하는 SELECT 결과 순번(물리 위치·rowid 아님).
- `key=…;export_row=…`: 기존 CCL 내보내기 순번. 내보내기가 제공하지 않는 `version`은 추정하지 않음.
- `line=…;column=…`: 원본 텍스트의 1부터 시작하는 줄·문자 열.
- `offset=…`: 원시 캐시 문자열의 0부터 시작하는 바이트 위치. 복합 조건은 각 구성 문자열의 위치를 모두 저장.
- LevelDB의 `file=…;offset=…`: 파서가 제공한 위치이며 물리 바이트 기준 확인 여부는 `basis`에 표시.
- Registry의 `key=…;value=…`: 레지스트리 키·값의 논리 위치.

알 수 없는 위치에는 `record` 번호나 바이트 오프셋을 생성하지 않는다. 저장할 위치가 없으면 `source_location`은 빈 배열이다. 기존 `source_locations` 필드는 호환용으로 유지하며, 이전 JSON에 대한 보고서 처리도 지원한다. CSV는 기존 여섯 열을 유지한다.


### UserAssist 해석 필드

공통 Registry 파서는 UserAssist `Count` 키의 REG_BINARY 값에 `userassist` 객체를 추가한다. 기존 `name`(ROT13 값 이름)과 `value`(원시 바이너리 보존)는 변경하지 않는다.

- `program_name`: ROT13을 해제한 프로그램명. 알려진 폴더 GUID를 임의의 경로로 치환하지 않는다.
- `run_counter`, `focus_count`, `focus_time_ms`: 기록된 카운터 및 밀리초 값.
- `last_executed_filetime`: 원본 FILETIME 정수.
- `last_executed_utc`, `last_executed_kst`: 시간대가 명시된 변환 시각. 변환값은 마이크로초 정밀도이며 원본 정수에 100ns 정밀도를 보존한다.
- `value_data_offsets`: 레지스트리 **값 데이터 내부**의 바이트 위치. 하이브 파일의 물리 오프셋이 아니다.
- `status`, `timestamp_status`, `error`: 짧은 형식·손상·시각 미기록 여부. 확인할 수 없는 시각은 `null`이다.

형식은 [Registry Explorer UserAssist 플러그인의 Windows 7+ 처리](https://github.com/EricZimmerman/RegistryPlugins/blob/master/RegistryPlugin.UserAssist/UserAssist.cs)를 근거로 한다. 최소 68바이트의 현대 형식만 해석하며, 16바이트 구형 형식은 지원하지 않는다. 기존 `openai.codex_` 이름 판정 및 등급은 변경하지 않는다. Screenshot에 표시된 시각의 시간대는 알 수 없으므로 실제 원시 값을 기준으로 변환한다.


### 나머지 레지스트리 해석 필드

공통 Registry 파서는 기존 키 경로, 원시 값, 키 마지막 기록 시각을 유지하면서 다음 필드를 기록한다.

- AppX PackageState의 OpenAI.Codex_* 키: appx.package_name, appx.sid, appx.key_last_write_iso.
- AmCache InventoryApplicationFile 키: amcache_inventory_application_file.name, key_last_write_iso.
- OpenSavePidlMRU: MRUListEx의 순서, 값별 mru_position, Shell Item의 값 데이터 내부 오프셋·크기·형식 바이트.
- LastVisitedPidlMRU: 위 MRU 정보와 앞부분 UTF-16LE executable, 뒤따르는 Shell Item 구조.

MRU의 원시 REG_BINARY는 기존 value에 남는다. `libfwsi-python`의 `pyfwsi`로 Shell Item을 읽어 `path_components`를 보존하고, 드라이브와 모든 구성 요소가 해석된 경우에만 `absolute_path`를 추가한다. GUID만으로 사용자 폴더 경로를 추측하지 않으며, 미해결 경로는 `path_status=partial`, 디코더 오류는 `path_error`에 기록한다. `opened_on`은 유효한 MRUListEx의 순위 0 항목에만 키 마지막 기록 시각을 연결하며, `opened_on_basis`와 해당 키의 `source_location`을 함께 보존한다. 다른 순위에는 시각을 생성하지 않는다.

구조 확인 근거: [OpenSavePidlMRU](https://github.com/EricZimmerman/RegistryPlugins/blob/master/RegistryPlugin.OpenSavePidlMRU/OpenSavePidlMRU.cs), [LastVisitedPidlMRU](https://github.com/EricZimmerman/RegistryPlugins/blob/master/RegistryPlugin.LastVisitedPidlMRU/LastVisitedPidlMRU.cs). 기존 사용자 행위 판정 규칙과 등급은 그대로 사용한다.


### Chrome History 로그인 규칙

같은 History의 `urls.url` 기록에 `http://localhost:1455/auth/callback?code=`와 `https://chatgpt.com/codex/open-app?source=login&app_brand=chatgpt`가 각각 포함되면 로그인 직접 근거로 기록한다. 방문 제목·순서·시각의 존재는 판정 조건이 아니다. 시각이 있으면 해당 History의 `last_visit_time`을 보고서 시각 계산에 사용한다. 콜백 URL의 `code`·`state` 비밀값은 `parsed_artifacts.json`과 CSV/HTML 근거에서 가리고, 원본은 수집 ZIP에 보존한다.

### 근거와 행 분리

- 근거에는 매칭 필드, 해당 시각 필드의 `artifacts[…].records[…]` 위치와 원본 값, 확인 가능한 `source_location[…]._source_locator`를 함께 표시한다. 시각이 확인되지 않으면 `-`이며, 근거의 원본 시각은 KST 표시값과 별개로 유지한다.
- 실행 Prefetch는 유효한 마지막 실행 시각 중 **가장 최근 값**을 사용한다. 설치 Prefetch는 기존 최초 매칭 시각 선택을 유지한다. UserAssist는 프로그램명·실행 횟수·last executed를 기록한다.
- auth.json은 비밀 토큰을 표시하지 않고 기존의 안전하게 디코딩된 JWT 정보와 각 토큰의 `iat`를 기록한다. 서명을 검증한 결과는 아니다.
- 로그인 Cache_Data는 `account.created_time` 값별로 행을 분리한다. codex-dev.db의 새 대화는 `thread_id`별로 분리하고 `display_title`, `source_created_at`을 근거에 포함한다.
- 새 대화 캐시는 `1/0/_dk_https://chatgpt.com https://chatgpt.com https://chatgpt.com/backend-api/conversation/` 키의 응답에서 같은 객체의 `conversation_id`, `title`, `create_time`을 확인하거나, `items` 항목의 `id`, `title`, `create_time`이 연속으로 나오는 경우를 사용한다. 같은 ID의 중복 응답은 근거를 합치고 다른 ID는 별도 행으로 유지한다.
- 임시 채팅은 `title=New Chat` 조건을 유지하고 `conversation_id`, `create_time`이 있으면 근거·시각에 포함한다.
- 플러그인 History는 `https://chatgpt.com/connector_platform_oauth_redirect?state=codex_scheme__oauth`를 포함한 URL과 `last_visit_time`을 사용한다. 데스크톱 로그는 `plugin_install_succeeded` 행 전체와 그 안의 시간대가 있는 날짜를 사용한다.
- 플러그인 SQLite는 `log`/`logs`의 `feedback_log_body`, thread_history의 `item_json`에 `@openai-curated-remote)`가 포함된 경우를 사용한다. 시각은 각각 Unix 초 `ts`, Unix 밀리초 `created_at_ms`다. thread_history와 rollout에서는 해당 문자열이 **실제 MCP 도구 호출 기록**에 있을 때만 행위로 연결한다. 사용자·개발자 메시지와 요약에 인용된 문자열은 제외한다. 세 아티팩트의 근거에는 매칭 문자열을 담은 가장 안쪽 키 값과 시각 필드만 적는다. JSON 문자열 안의 키는 `json_key=$.…`로 표시한다.
- global state의 플러그인 조건은 기존 MCP catalog의 tools와 그 `_meta`, 또는 `openai-curated-remote`을 포함한 상태 문자열이다. `prompt-history`와 `thread-description`의 인용된 텍스트는 플러그인 사용으로 해석하지 않는다. 플러그인 폴더는 `하위 폴더명=[gmail, google-drive, …]` 형태의 단일 근거로 표시하고 `source_location`은 덧붙이지 않는다.
- UserAssist 실행 조건은 디코딩한 `program_name`에 `ChatGPT.exe`가 포함된 값이다. 근거에는 프로그램명·`value_data_offsets`·실행 횟수·`last_executed_kst`를 표시하고, 보고서 시각은 `last_executed_kst`로 계산한다.
- 같은 CSV 행의 동일한 record 위치와 `source_location`은 한 번만 표시한다. 매칭값과 시각값이 한 원본 줄 또는 SQLite 행에 함께 있어도 원본 위치 참조는 중복하지 않는다.
- CSV 여섯 열과 HTML의 동일 데이터 사용은 유지한다. 개별 행 분리는 내부 식별값으로 관리하고 열을 추가하지 않는다. CLI evidence 개수는 계속 고유 아티팩트 정의 수다. 비고 여섯 아티팩트의 근거는 사용자가 지정한 설명 문구다.
