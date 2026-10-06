# Surface Profile Contract v0.4

## 처리 경계

```text
collection.py
   ↓
공통/전용 1차 Parser
   ↓
RawRecord                 # 원본 레코드 보존
   ↓
mappings.py / process()   # 여기서 처음 표면 의미 가공
   ↓
Observation
   ↓
rules.py
   ↓
Inference
```

## collection.py

E01/RAW/VMDK에서 무엇을 추출할지와 어떤 1차 parser를 실행할지를 정의합니다.

```python
COLLECTION = {
    'image': {
        'syntax': 'glob',
        'include': [r'Users\*\AppData\...\History'],
        'exclude': [],
        'noise': [],
    }
}

PARSER_PLAN = [
    {'parser': 'chromium_history', 'artifact': 'history'},
]
```

## 1차 Parser 계약

공통 parser 예:

```python
from llmtrace.common.parsers.base import register
from llmtrace.core.schema import RawRecord

@register('chromium_history')
def parse_history(input_root, step, profile):
    # SQLite 구조를 읽고 원래 table row를 복원한다.
    yield RawRecord(
        artifact='history',
        dataset='urls',
        record={
            'id': 45,
            'url': 'https://chatgpt.com/c/abc',
            'title': 'ChatGPT',
            'visit_count': 3,
            'typed_count': 1,
            'last_visit_time': 13433509595528772,
            'hidden': 0,
        },
        source_path='.../History',
        source_locator='urls rowid=45',
        parser='chromium_history',
    )
```

`RawRecord.record`에는 원본 아티팩트에 존재하지 않는 의미 필드를 추가하면 안 됩니다.

### 1차에서 가능

- SQLite/LevelDB/Cache 구조 해석
- 압축 해제
- 레코드 경계 복원
- key/value 및 원래 column 복원
- 원래 숫자/문자열/바이너리 값 보존

### 1차에서 금지

- `fact_type` 부여
- `evidence_kind` 부여
- 서비스 의미 해석
- URL에서 conversation ID를 새 필드로 생성
- 사용자 행위 판정
- 서로 다른 레코드를 LLM 의미로 결합

`source_path`, `source_locator`, `parser` 등은 내부 wrapper의 provenance이며 raw row 자체에 쓰이지 않습니다. `_raw/manifest.json`으로 별도 관리합니다.

## 1차 산출물

기본 레코드형 데이터는 다음처럼 저장합니다.

```text
_raw/<artifact>/<dataset>.jsonl
```

각 JSONL 라인에는 `RawRecord.record`만 기록합니다.

단일 JSON 구조는 parser가 `output_format='json'`으로 지정할 수 있습니다.

## 2차 mappings.py 계약

2차에서 처음 표면별 의미를 붙입니다.

단순한 경우:

```python
RAW_MAPPINGS = [
    {
        'artifact': 'history',
        'dataset': 'urls',
        'when': {'url': {'contains': 'chatgpt.com/c/'}},
        'fact_type': 'conversation_url',
        'evidence_kind': 'event',
        'fact': {
            'url': 'url',
            'title': 'title',
        },
    }
]
```

복잡한 경우:

```python
def process(records, ids, profile):
    # URL 구조화, ID 추출, 상태/잔존 판별 등
    # Observation을 생성한다.
    ...
```

## rules.py

Observation만 입력으로 받아 행위를 판정합니다. RawRecord를 직접 해석하지 않는 것을 기본 원칙으로 합니다.

행위별 증거 수준은 `RULE_LEVELS`에 정의합니다.

```python
RULE_LEVELS = {
    'R-LOGOUT-01': {
        'minimum': {
            'rule_id': 'R-LOGOUT-MIN-01',
            'when': {'all_refs': ['logout_history']},
        },
        'standard': {
            'rule_id': 'R-LOGOUT-STD-01',
            'when': {
                'all_refs': ['logout_history'],
                'any_refs': ['return_home', 'logout_marker'],
            },
        },
        'extended': {
            'rule_id': 'R-LOGOUT-EXT-01',
            'when': {'all_refs': ['logout_history', 'signout_state']},
        },
    },
}
```

여러 단계가 동시에 만족되면 공통 엔진은 `Extended > Standard > Minimum` 순으로 가장 높은 단계 한 건만 출력합니다. 단계와 confidence는 별도 값이며 서로 자동 변환하지 않습니다.

History 기반 window를 쓰는 프로필은 다음 정책을 선언합니다.

```python
HISTORY_WINDOW_POLICY = {
    'enabled': True,
    'mode': 'next_matching_history',
    'last_window': 'open',
}
```

이 window는 증거 후보 범위이며 행위 지속시간이 아닙니다.

## context.py

HTML에 보여줄 상태/잔존/연결정보를 Observation 기준으로 정의합니다. 공통 보고서는 각 분류의 Observation 건수와 `fact_type` 구성을 표시하고, 원문 확인은 Observation Viewer와 `_internal/observations.jsonl`을 사용합니다. 향후 필요하면 `_raw/` 원자료 drill-down 연결을 추가할 수 있습니다.

## team_share.py

선택 구성입니다. 공통 exporter가 사건별 Inference를 `supported_by` Observation에 다시 연결해 팀공유 CSV·HTML로 내보낼 때 사용할 표시명만 정의합니다.

- `ACTION_TITLES`: 공유 화면에 표시할 A번호와 행위명
- `ARTIFACT_NAMES`: Observation의 `source_artifact`를 사람이 읽기 쉬운 이름으로 표시
- `action_id_for_rule()`: Rule의 `behavior_id`를 공유용 A번호에 연결

공통 exporter는 `RULE_LEVELS.minimum`에 포함된 evidence ref를 `직접`, 그 밖에 실제로 결합된 evidence ref를 `귀속`으로 표시합니다. CSV에는 `행위 분류 | 아티팩트 | 경로 | 아티팩트 등급` 네 컬럼만 쓰며, 정적 Rule 조건이나 미매칭 아티팩트 경로는 출력하지 않습니다.

## 오류 계약

공통 파서 하나가 실패해도 다음 파서를 계속 실행합니다. 복구 가능한 오류는 `problems.csv`에 기록하고 `run.json` 상태를 `completed_with_problems`로 표시합니다. 원본 파서가 복원한 값은 `Observation.parsed_record`에도 보존하여 Viewer에서 출처를 추적할 수 있게 합니다.
