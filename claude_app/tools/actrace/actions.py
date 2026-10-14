# -*- coding: utf-8 -*-
r"""행위 코드 A00~A14 — 팀 합의 (2026-10-09).

기존 B01~B13 을 A00~A14 로 다시 가른다. 단순 이름 바꾸기가 아니다.
**둘이 쪼개진다.**

    B01 사이트 접속 · 설치 및 실행  ->  A00 설치 + A01 실행|사이트 접속
    B11 대화/계정 데이터 삭제       ->  A11 대화 삭제 + A12 계정 삭제

쪼개지는 쪽은 B 코드만 보고는 못 가른다. **규칙 단위로 붙인다** — 어느
규칙이 어느 행위를 가리키는지는 그 규칙이 무엇을 보는지로 정해진다.
예를 들어 설치 순서($MFT 엔트리)는 설치고, 실행 횟수(Prefetch)는 실행이다.

환경에 따라 이름이 갈리는 것 둘.

    A01  app  -> '실행'        web -> '사이트 접속'
    A14  app  -> '플러그인 사용' web -> '커넥터 사용'

★ 매핑에 없는 규칙은 **버리지 않는다.** B 코드의 기본 A 코드로 보내고
  `unmapped` 로 표시해 눈에 띄게 한다. 조용히 사라지면 행위가 통째로
  빠진 줄도 모른다.
"""

#: A 코드 -> (app 이름, web 이름). 같으면 한 벌만 쓴다.
ACTIONS = [
    ('A00', '설치', None),
    ('A01', '실행', '사이트 접속'),
    ('A02', '로그인', '로그인'),
    ('A03', '로그아웃', '로그아웃'),
    ('A04', '다중 계정', '다중 계정'),
    ('A05', '새 대화 생성', '새 대화 생성'),
    ('A06', '프롬프트 입력', '프롬프트 입력'),
    ('A07', '프롬프트 수정', '프롬프트 수정'),
    ('A08', '메모리 기능 사용', '메모리 기능 사용'),
    ('A09', '파일 업로드', '파일 업로드'),
    ('A10', '파일 다운로드', '파일 다운로드'),
    ('A11', '대화 삭제', '대화 삭제'),
    ('A12', '계정 삭제', '계정 삭제'),
    ('A13', '임시 채팅', '임시 채팅'),
    ('A14', '플러그인 사용', '커넥터 사용'),
]

ORDER = [a for a, _, _ in ACTIONS]

#: B 코드의 기본 행선지. 쪼개지는 둘은 규칙으로 덮어쓴다.
_BY_B = {
    'B01': 'A01', 'B02': 'A02', 'B03': 'A03', 'B04': 'A04',
    'B05': 'A05', 'B06': 'A06', 'B07': 'A07', 'B08': 'A08',
    'B09': 'A09', 'B10': 'A10', 'B11': 'A11', 'B12': 'A13',
    'B13': 'A14',
}

#: 규칙 단위 지정. B 코드보다 우선한다.
_BY_RULE = {
    # ── B01 이 둘로 쪼개진다 ──────────────────────────────
    # 설치 — 패키지가 디스크에 놓인 흔적
    'R-INSTALL-ORDER-01': 'A00',   # $MFT 엔트리 번호로 본 설치 4단계
    'R-INSTALL-PF-01': 'A00',      # 설치 프로그램 Prefetch
    'R-MSIX-MANIFEST-01': 'A00',   # MSIX 매니페스트
    # 실행 / 사이트 접속 — 그 밖의 B01 전부
    #   R-FIRSTRUN-01 R-FIRSTRUN-LOGLINE-01 R-RUN-PF-01 R-USERASSIST-01
    #   R-BAM-SID-01 R-BROWSER-VISIT-01 R-BROWSER-SEARCH-01
    #   R-APPSTART-LOG-01 R-PAGELOAD-LOG-01 R-TZ-DERIVE-01 R-ABSENCE-01

    # ── B11 이 둘로 쪼개진다 ──────────────────────────────
    'R-ACCTDEL-01': 'A12',         # 계정을 지워도 남는 것
    # 대화 삭제 — R-DELETE-LEDGER-01, R-ABSENCE-01
}


def code_of(behavior_id, rule_id):
    """(B 코드, 규칙) -> A 코드. 모르면 None."""
    a = _BY_RULE.get(rule_id)
    if a:
        return a
    return _BY_B.get(behavior_id)


def name_of(code, env_profile=''):
    """A 코드 -> 행위명. 환경에 따라 갈리는 둘을 가른다."""
    web = 'web' in (env_profile or '').lower()
    for c, app_name, web_name in ACTIONS:
        if c == code:
            if web and web_name:
                return web_name
            return app_name
    return code


def label(behavior_id, rule_id, env_profile=''):
    """표에 그대로 넣을 'A01 실행' 꼴. 모르는 규칙은 표시를 남긴다."""
    a = code_of(behavior_id, rule_id)
    if not a:
        return '%s (미매핑)' % (behavior_id or '?')
    return '%s %s' % (a, name_of(a, env_profile))
