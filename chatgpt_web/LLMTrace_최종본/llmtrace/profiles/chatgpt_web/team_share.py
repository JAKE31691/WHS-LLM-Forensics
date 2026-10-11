from __future__ import annotations

from typing import Any


CSV_FILENAME = 'LLM_Artifact_Report.csv'
HTML_FILENAME = 'LLM_Artifact_Report.html'

ACTION_TITLES = {
    'A00': '설치',
    'A01': '사이트 접속',
    'A02': '로그인',
    'A03': '로그아웃',
    'A04': '다중 계정',
    'A05': '새 대화 생성',
    'A06': '프롬프트 입력',
    'A07': '프롬프트 수정',
    'A08': '메모리 기능 사용',
    'A09': '파일 업로드',
    'A10': '파일 다운로드',
    'A11': '대화 삭제',
    'A12': '계정 삭제',
    'A13': '임시 채팅',
    'A14': '커넥터 사용',
}

ARTIFACT_NAMES = {
    'history': 'Chrome History',
    'cache': 'Chromium Cache',
    'local_storage': 'Chromium Local Storage',
    'session_storage': 'Chromium Session Storage',
    'sessions': 'Chromium Sessions',
    'cookies': 'Chromium Cookies',
    'preferences': 'Chromium Preferences',
    'indexeddb': 'Chromium IndexedDB',
}

# Raw fields used by mappings.py to create each Observation timestamp.  The
# HTML rule appendix reads this table so it can show the real timestamp source
# rather than a generic "Observation timestamp" label.  Fields are listed in
# the same priority/fallback order used by the mapping code.
TIMESTAMP_SOURCE_FIELDS = {
    ('history', 'url_visit'): ('visit_time',),
    ('history', 'conversation_visit'): ('visit_time',),
    ('history', 'browser_download'): ('start_time', 'end_time'),
    ('history', 'download_url_chain'): (),
    ('cache', '*'): (
        'metadata.response_time',
        'metadata.request_time',
    ),
    ('cache', 'conversation_snapshot'): (
        'metadata.response_time',
        'metadata.request_time',
        'create_time',
        'update_time',
    ),
    ('cache', 'conversation_attachment'): (
        'message_create_time',
        'metadata.response_time',
        'metadata.request_time',
        'message_update_time',
    ),
    ('cache', 'library_file'): (
        'file_upload_time',
        'metadata.response_time',
        'metadata.request_time',
        'file_processed_time',
    ),
    ('cache', 'memory_summary_checksum'): (
        'metadata.response_time',
        'metadata.request_time',
        'cachedGeneratedAtIso',
    ),
    ('local_storage', 'kv_record'): ('value.timestamp',),
    ('local_storage', 'account_session_observation'): (
        'account.lastLoggedInAt',
    ),
    ('local_storage', 'conversation_history_state'): (
        'value.timestamp',
        'items[].create_time',
        'items[].update_time',
    ),
    ('local_storage', 'conversation_draft'): (
        'draft.updatedAt',
        'draft.updated_at',
        'draft.timestamp',
        'draft.createdAt',
    ),
    ('session_storage', '*'): (),
    ('sessions', '*'): (),
    ('cookies', 'cookie_record'): (
        'creation_utc',
        'last_update_utc',
        'last_access_utc',
    ),
}

def action_id_for_rule(rule: dict[str, Any]) -> str:
    behavior_id = str(rule.get('behavior_id') or '')
    detail = rule.get('detail') or {}
    explicit_action_id = str(detail.get('action_id') or '')
    if explicit_action_id:
        return explicit_action_id
    base_rule_id = str(detail.get('base_rule_id') or '')
    if behavior_id == 'B11':
        return 'A12' if base_rule_id == 'R-ACCOUNT-DELETE-FLOW-01' else 'A11'
    if behavior_id == 'B12':
        return 'A13'
    if behavior_id == 'B13':
        return 'A14'
    return f'A{behavior_id[1:]}' if behavior_id.startswith('B') else ''
