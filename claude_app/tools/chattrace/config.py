# -*- coding: utf-8 -*-
"""분석 대상 경로 지도 — R1~R5 에서 실측한 값."""
import glob
import os

# MSIX 패키지 가상화 때문에 앱 로그는 AppData\Roaming\Claude 로 기록하지만
# 디스크($MFT)상의 실제 위치는 아래다. 이 불일치 자체가 보고서에 쓸 소견이다.
APP_ROOT_TMPL = (r'{local}\Packages\Claude_pzs8sxrjxfjjc'
                 r'\LocalCache\Roaming\Claude')

# (키, 상대경로, 종류, 설명)
ARTIFACTS = [
    ('indexeddb',   r'IndexedDB',                 'leveldb-parent',
     '대화 본문·첨부 메타. 복원 1순위'),
    ('local',       r'Local Storage\leveldb',     'leveldb',
     '설정·계정 식별자·기능 플래그'),
    ('session',     r'Session Storage',           'leveldb',
     '탐색 파라미터. 계정 삭제 후에도 잔존 확인(행위 14)'),
    ('cookies',     r'Network\Cookies',           'sqlite',
     '인증 쿠키. 삭제해도 파일 크기 불변 → 프리블록 확인'),
    ('cache',       r'Cache\Cache_Data',          'blob',
     'HTTP 응답 본문. 서버 응답 원문이 남는 경로'),
    ('codecache',   r'Code Cache\js',             'blob',
     'JS 코드 캐시. 문자열 상수 잔재'),
    ('mainlog',     r'claude-local\logs',         'text',
     '앱 동작 타임라인'),
    ('prefs',       r'Preferences',               'json',
     '프로필 설정'),
    ('localstate',  r'Local State',               'json',
     '암호화 키 래퍼(os_crypt) 포함'),
]


def default_app_root():
    local = os.environ.get('LOCALAPPDATA', r'C:\Users\Public\AppData\Local')
    return APP_ROOT_TMPL.format(local=local)


def resolve(root):
    """루트 아래에서 실제로 존재하는 아티팩트만 골라낸다.

    IndexedDB 는 오리진 폴더명이 환경마다 달라(file__0 / https_claude.ai_0)
    하드코딩하지 않고 glob 으로 찾는다.
    """
    found = []
    for key, rel, kind, desc in ARTIFACTS:
        full = os.path.join(root, rel)
        if kind == 'leveldb-parent':
            for d in sorted(glob.glob(os.path.join(full, '*.leveldb'))):
                found.append({'key': key, 'path': d, 'kind': 'leveldb',
                              'desc': desc, 'origin': os.path.basename(d)})
            continue
        if os.path.exists(full):
            found.append({'key': key, 'path': full, 'kind': kind, 'desc': desc})
    return found
