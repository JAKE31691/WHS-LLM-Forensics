from __future__ import annotations

from typing import Any


CSV_FILENAME = 'LLMTrace_행위별_아티팩트_매트릭스.csv'
HTML_FILENAME = 'LLMTrace_팀공유_행위별_아티팩트.html'

ACTION_TITLES = {
    'A00': '설치',
    'A01': 'ChatGPT Web 접속',
    'A02': '로그인',
    'A03': '로그아웃',
    'A04': '계정 전환',
    'A05': '새 대화 생성',
    'A06': '기존 대화 열기',
    'A07': '프롬프트 수정 및 재전송',
    'A08': '메모리 / 개인화',
    'A09': '파일 업로드·메시지 첨부',
    'A10': '생성 파일 다운로드',
    'A11': '대화 제거·계정 삭제 관련 흐름',
    'A12': '임시챗',
    'A13': 'Google Connector 연동·사용',
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
    return f'A{behavior_id[1:]}' if behavior_id.startswith('B') else ''
