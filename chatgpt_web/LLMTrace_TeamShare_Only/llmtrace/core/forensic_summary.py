from __future__ import annotations

import html
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schema import Observation
from .windows import observation_time


ACCOUNT_FIELDS = (
    'account_id', 'user_id', 'email', 'name', 'first_name', 'phone_number',
    'country', 'region', 'region_code', 'auth_provider', 'last_logged_in_at',
    'workspace_account_ids', 'workspace_names', 'organization_ids',
    'organization_names',
)


def _present(value: Any) -> bool:
    return value not in (None, '', [], {})


def _items(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if _present(item)]
    return [value] if _present(value) else []


def _merge_value(current: Any, incoming: Any) -> Any:
    if not _present(incoming):
        return current
    if not _present(current):
        return incoming
    if isinstance(current, (list, tuple, set)) or isinstance(incoming, (list, tuple, set)):
        merged = []
        for value in [*_items(current), *_items(incoming)]:
            if value not in merged:
                merged.append(value)
        return merged
    return current


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        cooked = str(value).strip()
        if cooked.endswith('Z'):
            cooked = cooked[:-1] + '+00:00'
        parsed = datetime.fromisoformat(cooked)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _earliest(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    a, b = _parse_time(left), _parse_time(right)
    if a is not None and b is not None:
        return left if a <= b else right
    return min(left, right)


def _latest(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    a, b = _parse_time(left), _parse_time(right)
    if a is not None and b is not None:
        return left if a >= b else right
    return max(left, right)


def _text_key(*values: Any) -> str:
    return '|'.join(str(value or '').strip().lower() for value in values)


def _state_label(observation: Observation) -> str:
    fact = observation.fact
    if fact.get('transition_type') == 'removed_from_active_list':
        return '활성 목록 제거'
    if observation.evidence_kind == 'residual' or fact.get('state_role') == 'residual':
        return '잔존'
    if fact.get('membership') is True:
        return '현재 목록'
    return '관찰됨'


def build_forensic_summary(observations: Iterable[Observation]) -> dict[str, Any]:
    """Create deduplicated analyst-facing facts without exposing raw records."""
    rows = list(observations)

    accounts_by_key: dict[str, dict[str, Any]] = {}
    for observation in rows:
        if observation.fact_type not in ('account_observation', 'account_session_observation'):
            continue
        fact = observation.fact
        identity = (
            fact.get('account_id') or fact.get('user_id') or fact.get('email')
            or fact.get('name') or observation.id
        )
        key = str(identity).strip().lower()
        account = accounts_by_key.setdefault(key, {
            **{field: '' for field in ACCOUNT_FIELDS},
            'sources': [], 'observed_at': '', 'state': '잔존',
        })
        for field in ACCOUNT_FIELDS:
            account[field] = _merge_value(account.get(field), fact.get(field))
        account['sources'] = _merge_value(account['sources'], observation.source_artifact)
        account['observed_at'] = _latest(account['observed_at'], observation_time(observation))
        if observation.evidence_kind != 'residual':
            account['state'] = '관찰됨'

    conversation_by_id: dict[str, dict[str, Any]] = {}
    conversation_rank: dict[str, tuple[int, float, int]] = {}

    def ensure_conversation(conversation_id: str) -> dict[str, Any]:
        return conversation_by_id.setdefault(conversation_id, {
            'conversation_id': conversation_id,
            'title': '', 'create_time': '', 'update_time': '',
            'is_archived': '', 'is_temporary_chat': '',
            'is_do_not_remember': '', 'memory_scope': '',
            'user_id': '', 'workspace_account_id': '', 'state': '복원됨',
        })

    for observation in rows:
        fact = observation.fact
        if observation.fact_type not in ('conversation_history_state', 'conversation_snapshot'):
            continue
        conversation_id = str(fact.get('conversation_id') or '').strip()
        if not conversation_id:
            continue
        conversation = ensure_conversation(conversation_id)
        for field in (
            'title', 'create_time', 'update_time', 'is_archived',
            'is_temporary_chat', 'is_do_not_remember', 'memory_scope',
            'user_id', 'workspace_account_id',
        ):
            conversation[field] = _merge_value(conversation.get(field), fact.get(field))
        if observation.fact_type == 'conversation_history_state':
            state = _state_label(observation)
            state_priority = {
                '잔존': 1, '관찰됨': 2, '현재 목록': 3, '활성 목록 제거': 4,
            }.get(state, 0)
            moment = _parse_time(observation_time(observation))
            rank = (
                int(observation.seq) if isinstance(observation.seq, int) else -1,
                moment.timestamp() if moment is not None else -1.0,
                state_priority,
            )
            if rank >= conversation_rank.get(conversation_id, (-1, -1.0, -1)):
                conversation_rank[conversation_id] = rank
                conversation['state'] = state
        elif conversation['state'] == '복원됨':
            conversation['state'] = 'Cache 대화 복원'

    qa_by_key: dict[str, dict[str, Any]] = {}
    prompts_by_key: dict[str, dict[str, Any]] = {}
    for observation in rows:
        fact = observation.fact
        if observation.fact_type == 'conversation_snapshot':
            conversation_id = str(fact.get('conversation_id') or '').strip()
            title = str(fact.get('title') or '').strip()
            for message in fact.get('message_sequence') or []:
                if not isinstance(message, dict):
                    continue
                role = str(message.get('role') or '').strip().lower()
                text = str(message.get('text') or '').strip()
                if role not in ('user', 'assistant') or not text:
                    continue
                message_id = str(message.get('message_id') or '').strip()
                when = str(message.get('create_time') or observation_time(observation) or '')
                key = _text_key(
                    conversation_id, message_id or message.get('content_sha256'), role, text
                )
                qa_by_key.setdefault(key, {
                    'conversation_id': conversation_id, 'title': title,
                    'message_id': message_id, 'role': role, 'text': text, 'when': when,
                })
                if role == 'user':
                    prompt_key = _text_key(
                        'message', conversation_id,
                        message_id or message.get('content_sha256'), text,
                    )
                    prompts_by_key.setdefault(prompt_key, {
                        'kind': '전송 메시지', 'conversation_id': conversation_id,
                        'message_id': message_id, 'text': text, 'when': when,
                        'state': '대화에서 복원',
                    })
        elif observation.fact_type == 'conversation_draft':
            text = str(fact.get('draft_text') or '').strip()
            if not text:
                continue
            conversation_id = str(fact.get('conversation_id') or fact.get('draft_target_id') or '').strip()
            draft_id = str(fact.get('draft_id') or '').strip()
            key = _text_key('draft', conversation_id, draft_id, text)
            prompts_by_key.setdefault(key, {
                'kind': '초안', 'conversation_id': conversation_id,
                'message_id': draft_id, 'text': text,
                'when': observation_time(observation),
                'state': '잔존' if observation.evidence_kind == 'residual' else '현재/전송 미확정',
            })
        elif observation.fact_type == 'conversation_attachment':
            text = str(fact.get('prompt_text') or '').strip()
            if str(fact.get('message_role') or '').lower() != 'user' or not text:
                continue
            conversation_id = str(fact.get('conversation_id') or '').strip()
            message_id = str(fact.get('message_id') or '').strip()
            key = _text_key('message', conversation_id, message_id, text)
            prompts_by_key.setdefault(key, {
                'kind': '첨부 메시지', 'conversation_id': conversation_id,
                'message_id': message_id, 'text': text,
                'when': str(fact.get('message_create_time') or observation_time(observation) or ''),
                'state': '대화에서 복원',
            })

    visited_by_id: dict[str, dict[str, Any]] = {}
    for observation in rows:
        if observation.fact_type != 'conversation_visit':
            continue
        fact = observation.fact
        conversation_id = str(fact.get('conversation_id') or '').strip()
        if not conversation_id or conversation_id.startswith('WEB:'):
            continue
        visit = visited_by_id.setdefault(conversation_id, {
            'conversation_id': conversation_id,
            'title': '', 'visit_count': 0, 'first_visit': '', 'last_visit': '',
            '_visits': set(),
        })
        visit_id = str(fact.get('visit_id') or observation.id)
        if visit_id in visit['_visits']:
            continue
        visit['_visits'].add(visit_id)
        visit['visit_count'] += 1
        visit['title'] = visit['title'] or str(fact.get('title') or '')
        when = observation_time(observation)
        visit['first_visit'] = _earliest(visit['first_visit'], when)
        visit['last_visit'] = _latest(visit['last_visit'], when)

    for conversation_id, visit in visited_by_id.items():
        known = conversation_by_id.get(conversation_id) or {}
        visit['title'] = visit['title'] or str(known.get('title') or '')
        visit.pop('_visits', None)

    for item in qa_by_key.values():
        if not item['title']:
            item['title'] = str(
                (conversation_by_id.get(item['conversation_id']) or {}).get('title') or ''
            )

    message_ids_by_conversation: dict[str, set[str]] = defaultdict(set)
    for key, item in qa_by_key.items():
        if item['role'] == 'user':
            message_ids_by_conversation[item['conversation_id'] or '(대화 ID 없음)'].add(
                item['message_id'] or key
            )
    message_counts = [
        {
            'conversation_id': conversation_id,
            'title': str((conversation_by_id.get(conversation_id) or {}).get('title') or ''),
            'message_count': len(message_ids),
        }
        for conversation_id, message_ids in message_ids_by_conversation.items()
    ]

    accounts = sorted(accounts_by_key.values(), key=lambda row: str(row.get('email') or row.get('account_id') or ''))
    conversations = sorted(
        conversation_by_id.values(),
        key=lambda row: str(row.get('update_time') or row.get('create_time') or row['conversation_id']),
        reverse=True,
    )
    prompts = sorted(prompts_by_key.values(), key=lambda row: str(row.get('when') or ''), reverse=True)
    qa = sorted(
        qa_by_key.values(),
        key=lambda row: (str(row.get('conversation_id') or ''), str(row.get('when') or ''), str(row.get('message_id') or '')),
    )
    visited = sorted(visited_by_id.values(), key=lambda row: str(row.get('last_visit') or ''), reverse=True)
    message_counts.sort(key=lambda row: (-row['message_count'], row['conversation_id']))
    total_user_messages = sum(row['message_count'] for row in message_counts)

    return {
        'accounts': accounts, 'conversations': conversations, 'prompts': prompts,
        'qa': qa, 'visited_conversations': visited,
        'message_counts': message_counts, 'total_user_messages': total_user_messages,
        'counts': {
            'accounts': len(accounts), 'conversations': len(conversations),
            'prompts': len(prompts), 'qa_messages': len(qa),
            'visited_conversations': len(visited), 'user_messages': total_user_messages,
        },
    }


def _esc(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        value = ', '.join(str(item) for item in value)
    if isinstance(value, bool):
        value = '예' if value else '아니오'
    return html.escape(str(value if value not in (None, '') else '-'))


def _short_text(value: Any, limit: int = 2000) -> str:
    text = str(value or '')
    if len(text) > limit:
        text = text[:limit] + f'\n… ({len(text) - limit}자 생략, 원본은 내부 Observation에 보존)'
    return _esc(text)


def _table(headers: list[tuple[str, str]], rows: list[dict[str, Any]], *, text_fields: set[str] | None = None) -> str:
    if not rows:
        return '<p class="empty">확인된 정보가 없습니다.</p>'
    text_fields = text_fields or set()
    parts = ['<div class="table-wrap"><table><thead><tr>']
    parts.extend(f'<th>{_esc(label)}</th>' for _, label in headers)
    parts.append('</tr></thead><tbody>')
    for row in rows:
        parts.append('<tr>')
        for key, _ in headers:
            value = _short_text(row.get(key)) if key in text_fields else _esc(row.get(key))
            klass = ' class="message"' if key in text_fields else ''
            parts.append(f'<td{klass}>{value}</td>')
        parts.append('</tr>')
    parts.append('</tbody></table></div>')
    return ''.join(parts)


def render_forensic_sections(summary: dict[str, Any]) -> str:
    parts: list[str] = ['<div class="forensic-stats">']
    for label, key in (
        ('관찰 계정', 'accounts'), ('대화', 'conversations'),
        ('프롬프트', 'prompts'), ('방문 대화방', 'visited_conversations'),
        ('복원된 사용자 메시지', 'user_messages'),
    ):
        parts.append(f'<div><span>{_esc(label)}</span><strong>{_esc(summary["counts"].get(key, 0))}</strong></div>')
    parts.append('</div>')

    parts.append('<h3>1. 계정 관련 내용</h3>')
    parts.append(_table([
        ('account_id', '계정 ID'), ('user_id', '사용자 ID'), ('email', '이메일'),
        ('name', '이름'), ('phone_number', '전화번호'), ('country', '국가'),
        ('region', '지역'), ('workspace_account_ids', '워크스페이스 ID'),
        ('workspace_names', '워크스페이스'), ('auth_provider', '인증 방식'),
        ('last_logged_in_at', '마지막 로그인'), ('state', '상태'),
    ], summary['accounts']))

    parts.append('<h3>2. 대화목록</h3>')
    parts.append(_table([
        ('conversation_id', 'Conversation ID'), ('title', '제목'),
        ('create_time', '생성 시각'), ('update_time', '수정 시각'),
        ('is_archived', '보관'), ('is_temporary_chat', '임시챗'),
        ('state', '복원 상태'),
    ], summary['conversations']))

    parts.append('<h3>3. 프롬프트 입력</h3>')
    parts.append('<p class="hint">초안은 전송 여부가 확인되지 않은 입력이며, 전송 메시지는 복원된 대화 기준입니다.</p>')
    parts.append(_table([
        ('kind', '구분'), ('conversation_id', 'Conversation ID'),
        ('message_id', 'Message/Draft ID'), ('text', '입력 내용'),
        ('when', '시각'), ('state', '상태'),
    ], summary['prompts'], text_fields={'text'}))

    parts.append('<h3>4. 호출된 대화방의 질문과 응답</h3>')
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in summary['qa']:
        grouped[item.get('conversation_id') or '(대화 ID 없음)'].append(item)
    if not grouped:
        parts.append('<p class="empty">복원된 질문과 응답이 없습니다.</p>')
    else:
        for conversation_id, messages in grouped.items():
            title = next((str(item.get('title') or '') for item in messages if item.get('title')), '')
            parts.append(
                f'<details class="conversation"><summary><strong>{_esc(title or "(제목 없음)")}</strong> · '
                f'{_esc(conversation_id)} · {len(messages)}개 메시지</summary>'
            )
            display_rows = [
                {**item, 'role_label': '질문' if item.get('role') == 'user' else '응답'}
                for item in messages
            ]
            parts.append(_table([
                ('role_label', '구분'), ('message_id', 'Message ID'),
                ('text', '내용'), ('when', '시각'),
            ], display_rows, text_fields={'text'}))
            parts.append('</details>')

    parts.append('<h3>5. 방문했던 대화방</h3>')
    parts.append(_table([
        ('conversation_id', 'Conversation ID'), ('title', '제목'),
        ('visit_count', '방문 횟수'), ('first_visit', '최초 방문'),
        ('last_visit', '최근 방문'),
    ], summary['visited_conversations']))

    parts.append('<h3>6. 유저가 메시지를 보낸 횟수</h3>')
    parts.append(
        '<p class="count-total">복원된 사용자 메시지 총 <strong>'
        f'{_esc(summary["total_user_messages"])}</strong>건</p>'
    )
    parts.append('<p class="hint">초안은 제외하고, 복원된 대화의 고유 사용자 Message ID를 기준으로 집계합니다.</p>')
    parts.append(_table([
        ('conversation_id', 'Conversation ID'), ('title', '제목'),
        ('message_count', '사용자 메시지 수'),
    ], summary['message_counts']))
    return ''.join(parts)


def render_forensic_html(path: str, metadata: dict[str, Any], summary: dict[str, Any]) -> None:
    generated = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    css = r'''
    :root{--ink:#172033;--muted:#64748b;--line:#dbe3ef;--panel:#fff;--accent:#3157d5}
    *{box-sizing:border-box}body{max-width:1440px;margin:0 auto;padding:32px;font:14px/1.55 Inter,"Malgun Gothic",Segoe UI,Arial,sans-serif;color:var(--ink);background:#f5f7fb}
    header{margin:8px 0 26px}h1{font-size:30px;margin:0 0 5px}h2{font-size:20px;margin:0}h3{font-size:18px;margin:30px 0 12px}.sub,.hint{color:var(--muted)}
    main>section{background:var(--panel);padding:24px;margin:16px 0;border:1px solid var(--line);border-radius:14px;box-shadow:0 3px 14px #1e293b0a}
    .forensic-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px}.forensic-stats div{padding:12px;border:1px solid var(--line);border-radius:10px;background:#f8fafc}.forensic-stats span{display:block;color:var(--muted);font-size:12px}.forensic-stats strong{font-size:20px}
    .table-wrap{overflow:auto}table{width:100%;border-collapse:collapse}th,td{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:#f8fafc;position:sticky;top:0}.message{white-space:pre-wrap;min-width:280px;max-width:720px;word-break:break-word}
    details.conversation{border:1px solid var(--line);border-radius:10px;margin:10px 0;background:#fbfdff}details.conversation>summary{cursor:pointer;padding:12px}.empty{padding:14px;border:1px dashed #b9c5d6;border-radius:8px;color:var(--muted)}.count-total{font-size:16px;padding:12px;border-left:4px solid var(--accent);background:#f6f8ff}
    footer{margin:24px 0;color:var(--muted);font-size:12px}@media(max-width:700px){body{padding:14px}main>section{padding:15px}}
    '''
    document = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>LLMTrace 포렌식 유의미 정보</title><style>' + css + '</style></head><body><main>'
        '<header><h1>LLMTrace 포렌식 유의미 정보</h1>'
        '<p class="sub">행위 판정과 분리하여 현재 증거에서 복원된 정보를 중복 제거해 정리했습니다.</p></header>'
        '<section><h2>' + _esc(metadata.get('service') or metadata.get('profile_id') or '분석 결과') + '</h2>'
        + render_forensic_sections(summary)
        + '</section><footer>생성 시각: ' + _esc(generated) + '</footer></main></body></html>'
    )
    Path(path).write_text(document, encoding='utf-8')
