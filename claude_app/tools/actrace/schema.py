# -*- coding: utf-8 -*-
"""ActTrace 데이터 모델 — 관찰과 추론을 두 층으로 나눈다.

    아티팩트 ──파싱──▶ observation ──추론──▶ inference
                (사실)                   (행위)

    "Local Storage 에 d=1 이 적혀 있다"      ← observation. 다툴 여지 없음
    "사용자가 이 대화를 삭제했다"              ← inference. 다툴 여지 있음

섞으면 안 되는 이유. 법정에서 다투는 것은 거의 항상 추론이지 관찰이 아니다.
한 레코드에 섞여 있으면 '이건 본 것이고 저건 생각한 것' 을 분리할 수 없다.
반대 의견이 나왔을 때 관찰은 그대로 두고 추론만 갈아끼울 수 있어야 한다.

시각 규약
  * 필드 이름에 출처와 종류를 박는다. 그냥 'timestamp' 는 금지.
      ts_event      앱이 스스로 적은 이벤트 시각
      ts_record     저장소에 쓰인 시각
      ts_mft_si_*   $MFT $STANDARD_INFORMATION
  * 전부 UTC 로 정규화하고 원본을 tz_source 에 남긴다.
  * 불확실하면 단일 값으로 쓰지 않는다. when_from / when_to / span_sec.
"""
import datetime
import json

SCHEMA_VERSION = '0.1'

# 신뢰도 — 조작적 정의는 rules.py 의 각 규칙이 지킨다
CONFIRMED = 'confirmed'      # 확정. 그 행위 말고는 이 흔적이 생길 길이 없다
SUSPECTED = 'suspected'      # 유력. 다른 원인을 배제하지 못했다
PRESUMED = 'presumed'        # 추정. 간접 증거 하나, 또는 부재 추론
UNDECIDED = 'undecided'      # 판정 불가. 근거 자체가 성립하지 않는다

LEVEL_KO = {CONFIRMED: '확정', SUSPECTED: '유력',
            PRESUMED: '추정', UNDECIDED: '판정불가'}

_EPOCH = datetime.datetime(1970, 1, 1)


def utc_ms(v):
    """Unix 밀리초 -> ISO8601 UTC. 실패하면 빈 문자열."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return ''
    if v <= 0 or v > 4102444800000:          # 2100년 넘으면 값이 이상한 것
        return ''
    dt = _EPOCH + datetime.timedelta(milliseconds=v)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (v % 1000)


def iso_to_ms(s):
    """ISO8601 -> Unix 밀리초. 앱 이벤트가 ISO 문자열로 적는다."""
    if not s:
        return None
    t = s.strip().replace('Z', '+00:00')
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return int((dt - _EPOCH).total_seconds() * 1000)


class Observation(object):
    """아티팩트에서 직접 읽은 사실 하나."""

    __slots__ = ('id', 'source_artifact', 'source_path', 'source_locator',
                 'parser', 'parser_status', 'parser_note',
                 'fact_type', 'fact', 'ts', 'tz_source', 'seq',
                 'actor', 'env_profile', 'confidence', 'confidence_basis')

    def __init__(self, oid, artifact, path, locator, fact_type, fact,
                 parser='', status='ok', note='', ts=None, seq=None,
                 actor='', env_profile='', confidence=CONFIRMED, basis=None,
                 tz_source='UTC'):
        self.id = oid
        self.source_artifact = artifact
        self.source_path = path
        self.source_locator = locator
        self.parser = parser or artifact
        self.parser_status = status
        self.parser_note = note
        self.fact_type = fact_type
        self.fact = fact or {}
        self.ts = ts or {}                  # {'ts_event': '...Z', ...}
        self.tz_source = tz_source
        self.seq = seq                      # 저장소 내부 쓰기 순서
        self.actor = actor
        self.env_profile = env_profile
        # 관찰의 신뢰도는 '제대로 읽었는가' 다. 행위 신뢰도와 다르다.
        self.confidence = confidence
        self.confidence_basis = basis or {}

    def as_dict(self):
        d = {'id': self.id, 'schema_version': SCHEMA_VERSION,
             'source_artifact': self.source_artifact,
             'source_path': self.source_path,
             'source_locator': self.source_locator,
             'parser': self.parser, 'parser_status': self.parser_status,
             'parser_note': self.parser_note,
             'fact_type': self.fact_type, 'fact': self.fact,
             'tz_source': self.tz_source, 'seq': self.seq,
             'actor': self.actor, 'env_profile': self.env_profile,
             'confidence': self.confidence,
             'confidence_basis': self.confidence_basis}
        d.update(self.ts)
        return d


class Inference(object):
    """관찰들로부터 도출한 행위 하나."""

    __slots__ = ('id', 'behavior_id', 'behavior_name', 'question',
                 'when_from', 'when_to', 'when_basis', 'actor',
                 'rule_id', 'supported_by', 'contradicted_by',
                 'alternatives', 'confidence', 'confidence_basis', 'note',
                 'detail')

    def __init__(self, iid, behavior_id, behavior_name, question, rule_id,
                 when_from='', when_to='', when_basis='', actor='',
                 supported_by=None, contradicted_by=None, alternatives=None,
                 confidence=SUSPECTED, basis=None, note='', detail=None):
        self.id = iid
        self.behavior_id = behavior_id
        self.behavior_name = behavior_name
        self.question = question
        self.rule_id = rule_id
        self.when_from = when_from
        self.when_to = when_to
        self.when_basis = when_basis
        self.actor = actor
        # 비어 있어도 필드는 있어야 한다. 있으면 채우게 되고 없으면
        # 생각조차 안 한다.
        self.supported_by = supported_by or []
        self.contradicted_by = contradicted_by or []
        self.alternatives = alternatives or []
        self.confidence = confidence
        self.confidence_basis = basis or {}
        self.note = note
        self.detail = detail or {}

    @property
    def span_sec(self):
        a, b = iso_to_ms(self.when_from), iso_to_ms(self.when_to)
        if a is None or b is None:
            return None
        return round(abs(b - a) / 1000.0, 3)

    def as_dict(self):
        return {
            'id': self.id, 'schema_version': SCHEMA_VERSION,
            'behavior_id': self.behavior_id,
            'behavior_name': self.behavior_name,
            'question': self.question, 'rule_id': self.rule_id,
            'when_from': self.when_from, 'when_to': self.when_to,
            'span_sec': self.span_sec, 'when_basis': self.when_basis,
            'actor': self.actor,
            'supported_by': self.supported_by,
            'contradicted_by': self.contradicted_by,
            'alternatives': self.alternatives,
            'confidence': self.confidence,
            'confidence_basis': self.confidence_basis,
            'note': self.note, 'detail': self.detail,
        }


class Ids(object):
    """레코드 번호 발급기."""

    def __init__(self):
        self.n = {}

    def next(self, prefix):
        self.n[prefix] = self.n.get(prefix, 0) + 1
        return '%s:%04d' % (prefix, self.n[prefix])


def write_jsonl(path, rows):
    import io
    with io.open(path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r.as_dict(), ensure_ascii=False,
                               sort_keys=True) + '\n')
    return len(rows)
