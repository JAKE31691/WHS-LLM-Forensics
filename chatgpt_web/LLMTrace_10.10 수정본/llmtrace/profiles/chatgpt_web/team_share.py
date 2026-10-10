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
