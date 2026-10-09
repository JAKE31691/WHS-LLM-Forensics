# LLM Artifact Analyzer v2.0.0

Windows 11 + Chrome에서 Claude(claude.ai)를 쓴 사용자 행위를 아티팩트로 확인하는 도구.

## 실행

```
py -m pip install -r requirements.txt          (처음 한 번, E01 입력과 캐시 본문 해제에 필요)
python main.py "C:\Evidence\case01.E01" "C:\Result"
python main.py "C:\Evidence\case01.E01" "C:\Result" --verify-hash     (E01 해시 검증까지)
python main.py live "C:\Result"
```

- Python 3.10 이상. Windows Python 3.14 기준으로 만들었고 3.13, 3.10에서도 같은 결과를 확인했다.
- 결과 저장 경로는 절대 경로여야 한다. 없으면 만든다.
- live: Chrome을 끄고 실행한다(켜져 있으면 Cookies, LevelDB의 LOCK 같은 잠긴 파일은 복사 실패로 남는다).
  다른 사용자 폴더까지 보려면 관리자 권한으로 실행한다.
- E01: 마운트하지 않고 dissect.target으로 NTFS를 직접 읽는다. Users 폴더가 있는 시스템 볼륨을 C:로 본다.
- 종료 코드: 0 정상 / 1 일부 실패(복사·파싱 실패, 읽다가 오류가 나 건너뛴 폴더, 해시 검증을 요청했지만 E01에 저장된 해시가 없음) /
  2 입력 또는 rules.csv를 읽을 수 없음, E01 세그먼트가 빠짐, 해시 불일치

### E01 세그먼트와 해시 (ewf_check.py)

- 이미지를 열기 전에 같은 폴더에서 같은 이름의 세그먼트(.E01, .E02 …)를 모두 찾아 콘솔 `Source info`에 목록을 출력한다.
- 빠진 세그먼트가 있는지 EWF 구조로 확인한다. 세그먼트마다 머리글의 세그먼트 번호가 1부터 빠짐없이 이어져야 하고,
  번호가 가장 큰 세그먼트는 마지막 섹션이 `done`, 나머지는 `next`여야 한다.
  어긋나면 빠진 파일 이름(예: `case01.E02 (2번)`)을 알려 주고 분석하지 않는다(종료 코드 2).
- E01에 저장된 해시(`hash` 섹션의 MD5, `digest` 섹션의 MD5·SHA1)를 항상 출력한다.
- `--verify-hash`를 붙이면 이미지 전체를 읽어 같은 해시를 계산해 비교한다. 다르면 분석하지 않는다(종료 코드 2).
  저장된 해시가 없으면 검증할 수 없다고 출력하고 분석은 계속한다(종료 코드 1).
- 세그먼트 목록, 저장된 해시, 검증 결과는 `parsed_artifacts.json`의 `source.ewf`에도 남는다.

## 출력물 (결과 폴더)

| 파일 | 내용 |
|---|---|
| `LLM_Artifacts.zip` | 수집한 아티팩트 원본 복사본. 원래 경로 구조 그대로(`C/Users/...`) |
| `parsed_artifacts.json` | 파싱한 내용 전부. 파일마다 크기·SHA-256·원본 시각(생성/수정/접근)과 복사 결과도 들어 있다 |
| `LLM_Artifact_Report.csv` | 보고서: 시각 \| 사용자 행위 \| 아티팩트 \| 경로 \| 등급 \| 근거 (UTF-8 BOM) |
| `LLM_Artifact_Report.html` | 같은 내용을 팀 예시 형식으로 보여 주는 파일 하나짜리 HTML: 분석 개요(분석 대상, 입력 형식, 매칭 행위, 근거 개수), 검색창과 모두 펼치기·접기, 행위(A01~A14) → 시각별 토글 → 표 |

### 행이 없는 아티팩트의 상태

규칙이 보는 아티팩트 중 행을 만들지 못한 것은 사용자·프로필마다 셋 중 하나로 콘솔과 HTML에 적는다.
행위 하나에 상태가 섞여도 대표 상태 하나로 묶지 않고 아티팩트별로 모두 적는다.

| 상태 (HTML / 콘솔) | 뜻 |
|---|---|
| 경로 없음 / Path not found | 그 프로필에 아티팩트 경로가 없다 (예: claude.ai를 쓰지 않아 IndexedDB 폴더가 없음) |
| 있지만 읽기 실패 / Read failed | 경로는 있는데 복사·파싱에 실패했거나 일부만 읽었다. 폴더를 탐색하다 읽기 오류가 나 들어가 보지 못한 곳도 여기에 넣는다 |
| 읽었지만 매칭 없음 / Read, no match | 읽었지만 규칙에 맞는 레코드가 없다 |

규칙이 없는 행위(A04, A06, A12)는 `미지원 (판정 규칙 없음)`으로 나온다.

### CSV 형식

- 한 행 = 규칙 1개가 아티팩트 1개에서 맞은 것.
- 시각: `YYYY-MM-DD HH:MM:SS KST` 또는 `...SS.FFF KST`(원본에 초 미만 값이 있을 때만, 반올림 없이 자름) 또는 `-`.
  맞은 레코드가 여럿이면 가장 이른 시각.
- 아티팩트: 경로의 마지막 조각(파일 또는 폴더). 경로: 드라이브 문자부터 쓴 증거 기준 절대 경로.
- 등급: 직접(이 아티팩트만으로 행위에 귀속) / 보조(다른 아티팩트와 함께 봐야 추정 가능).
- 근거: `parsed_artifacts.json` 안의 위치=값. 대괄호 숫자는 0부터 세는 배열 번호.
  레코드(`rows[k]` / `records[k]`) 단위로, 맞은 key=value와 시각 값을 같이 쓴다.
  시각 열 값을 만든 레코드가 맨 앞, 나머지는 JSON 순서. 최대 3개 레코드, 나머지는 `외 N건`.
  값 하나가 300자를 넘으면 찾은 문자열이 보이도록 잘라 `…(총 N자)`를 붙인다 (N은 원래 값의 글자 수).
- 정렬: 시각 있는 행을 시간순으로 먼저, `-` 행은 행위 ID → 아티팩트 이름 순.

## 구성

```
main.py              단계 순서와 콘솔 출력
definitions.py       도구 정보, 행위 목록(A01~A14), 아티팩트 정의 10종
rules.csv            판정 규칙 (엑셀로 열어 고칠 수 있음)
source.py            입력 판별, E01/live에서 파일 읽기
ewf_check.py         E01 세그먼트 확인, 저장된 해시 읽기, 해시 검증
collector.py         사용자·프로필 탐색, 복사, SHA-256, ZIP
parsers/             아티팩트 종류 1개 = 파서 1개
  sqlite_parser.py     History, Cookies, Web Data
  json_parser.py       Preferences, Local State
  leveldb_parser.py    Local Storage, Session Storage
  indexeddb_parser.py  IndexedDB (claude.ai, .blob 포함)
  cache_parser.py      Cache_Data (본문은 claude.ai 응답만)
  snss_parser.py       Sessions
analyzer.py          규칙 적용, CSV 행 만들기, 행위별 상태
timeutil.py          시각 변환 (WebKit/Unix/ISO -> KST)
jsonpath.py          근거 경로 문자열, 규칙의 필드 패턴
report_csv.py, report_html.py
third_party/         ccl_chromium_reader, ccl_simplesnappy (MIT, 버전과 수정 사항은 VERSIONS.txt)
tests/               시험용 스크립트 (도구 실행에는 필요 없음)
rules_support.md     행위별 판정 범위와 근거, 시험 결과
```

수집 → ZIP → 파싱 순서다. ZIP은 파싱 전에 만들어 손대지 않은 복사본을 남긴다. 파서는 원본이 아니라 작업 폴더의 복사본만 읽고, 작업 폴더는 끝나면 지운다.

## 규칙 추가 (rules.csv)

| 열 | 뜻 |
|---|---|
| rule_id | `R-A02-01` 형식 |
| action_id | A01~A14 |
| artifact | definitions.py의 아티팩트 이름 (History, Cookies, Local Storage, IndexedDB ...) |
| filter | 레코드 고르는 조건, `;`로 여러 개. `table=urls`는 SQLite 표. 연산자 `=` `^=`(시작) `$=`(끝) `~=`(포함) `@=`(호스트가 같거나 하위 도메인: `url@=claude.ai`, `host_key@=claude.ai`) |
| field | 값을 찾을 위치. `url`, `value.tombstone`, `value_json[*].eventName`(배열 원소마다), `**.file_kind`(깊이 상관없이) |
| op | `equals` / `contains` / `startswith` / `endswith` / `custom`(analyzer.py의 함수 이름을 value에) |
| value | 찾을 값. true/false/숫자는 JSON 표기로 비교 |
| time_field | 시각으로 쓸 키 이름, `|`로 여러 개. 맞은 값과 같은 단계에서 찾고 없으면 바깥으로 올라간다 |
| grade | 직접 / 보조 |
| note | 설명 (출력에는 안 나옴) |

규칙이 하나도 없는 행위는 콘솔에 `Not supported`, HTML에 `미지원 (판정 규칙 없음)`으로 나온다.
rules.csv에 오타(없는 op, 행위 ID, 아티팩트 이름, filter 문법 오류 등)가 있으면 수집을 시작하기 전에 문제 행을 모두 보여 주고 종료 코드 2로 끝난다.

## 시험 (tests/)

```
python -m unittest discover -s tests -v                       시각·경로·규칙·수집·E01 세그먼트·보고서 형식 (30개)
python tests/run_on_profile.py <Default 폴더> <결과 폴더>       보관한 프로필 폴더 하나로 전체 실행
python tests/regression_v120.py <Default 폴더> ...              v1.2.0(바이트 검색)과 규칙별 비교
python tests/check_evidence_paths.py <결과 폴더>                근거 경로가 JSON의 값을 실제로 가리키는지
```

`run_on_profile.py`는 임시 폴더를 C:\ 로 보는 live 입력으로 돌린다. 정식 입력(E01, live)에는 영향이 없다.
