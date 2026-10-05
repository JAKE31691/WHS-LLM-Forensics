# -*- coding: utf-8 -*-
"""규칙 엔진 — 관찰을 행위로 바꾼다.

규칙 하나하나가 신뢰도 등급을 스스로 정당화한다. '확정' 을 주려면
아래 셋을 다 만족해야 한다.

    ① 아티팩트와 행위가 1:1        다른 행위로는 이 흔적이 안 생긴다
    ② 재현에서 누락 0             실험 회차에서 빠짐없이 관찰됐다
    ③ 다른 생성 경로가 없다        알려진 대안이 없다

넘을 수 없는 선 두 개
    * 부재 추론은 '추정' 을 넘지 못한다. 없는 것은 여러 이유로 없다 —
      컴팩션 · 미생성 · 수집 누락 · 버전 차이.
    * $MFT 시각만 근거인 추론도 '유력' 을 넘지 못한다. 반복 기록되는
      파일은 최종 시각만 남아서 중간 행위가 통째로 사라진다(R2 결론 04).
"""
import re

from collections import defaultdict

from .schema import (CONFIRMED, PRESUMED, SUSPECTED, UNDECIDED,
                     Inference, iso_to_ms, utc_ms)

# 팀 목록 13개. 13번은 로컬 흔적이 거의 없어 1차에서 뺐으나,
# mcp.servers.listed 가 실측돼 재검토 대상이다.
BEHAVIORS = {
    'B01': ('사이트 접속 · 설치 및 실행', 'Q1'),
    'B02': ('로그인 (인증)', 'Q2'),
    'B03': ('로그아웃', 'Q2'),
    'B04': ('계정 전환', 'Q2'),
    'B05': ('새 대화(세션) 생성', 'Q3'),
    'B06': ('Prompt 입력', 'Q3'),
    'B07': ('Prompt 수정 · 응답 재생성', 'Q3/Q5'),
    'B08': ('메모리 사용 · 관리', 'Q3'),
    'B09': ('파일/이미지 업로드', 'Q4'),
    'B10': ('생성물 다운로드 · Export', 'Q4'),
    'B11': ('대화/계정 데이터 삭제', 'Q5'),
    'B12': ('임시/시크릿 대화 사용', 'Q5'),
    'B13': ('외부 서비스 연동 (connector)', 'Q4'),
}

# 이벤트 이름 -> (행위, 규칙, 등급, 대안가설)
EVENT_MAP = {
    'claudeai.page.new_chat_input_ready': (
        'B05', 'R-NEWCHAT-01', SUSPECTED,
        ['앱을 켜면 새 대화 화면이 기본으로 뜬다 — 실제 입력 없이도 발생한다']),
    'claudeai.conversation.recent.opened': (
        'B06', 'R-REOPEN-01', SUSPECTED,
        ['목록에서 스쳐 지나가며 열렸을 수 있다']),
    'claudeai.desktop.sidebar.profile_menu_opened': (
        'B02', 'R-PROFILE-01', PRESUMED,
        ['프로필 메뉴를 열었을 뿐 계정을 바꾸지 않았을 수 있다']),
    'claudeai.settings.section_viewed': (
        'B02', 'R-SETTINGS-01', PRESUMED, ['설정 열람일 뿐일 수 있다']),
    'mcp.servers.listed': (
        'B13', 'R-CONNECTOR-01', SUSPECTED,
        ['앱이 시작할 때 자동으로 목록을 조회한다 — 사용자 조작이 아닐 수 있다']),
    'claudeai.mcp.bootstrap_stream.completed': (
        'B13', 'R-CONNECTOR-02', PRESUMED,
        ['부팅 시 자동 수행']),
}


def _t(o, *keys):
    for k in keys:
        v = o.ts.get(k)
        if v:
            return v
    return ''


def run(obs, ids, convs=None):
    """-> [Inference]

    convs: 대화 복원 결과(ChatTrace). 있으면 초안 대조에 쓴다.
    """
    out = []
    by_type = defaultdict(list)
    for o in obs:
        by_type[o.fact_type].append(o)

    out += _first_run(by_type, ids)
    out += _prefetch(by_type, ids)
    out += _applog(by_type, ids)
    out += _logout(by_type, ids)
    out += _events(by_type, ids)
    out += _deleted(by_type, ids)
    out += _revisits(by_type, ids)
    out += _drafts(by_type, ids, convs)
    out += _draft_wiped(by_type, ids)
    out += _replies(by_type, ids)
    out += _accounts(by_type, ids)
    out += _org_switch(by_type, ids)
    out += _identity_dirs(by_type, ids)
    out += _session_state(by_type, ids, convs)
    out += _login_transitions(by_type, ids)
    out += _timezone(by_type, ids)
    out += _windows_user(by_type, ids)
    out += _install_order(by_type, ids)
    out += _browser(by_type, ids)
    out += _uploads(by_type, ids)
    out += _temporary(by_type, ids)
    out += _messages(by_type, ids, convs)
    return out


# ── Q1 설치 순서 ($MFT 엔트리 번호) ─────────────────────────────
def _install_order(bt, ids):
    r"""엔트리 번호가 순서를 확정한다 — 시각이 동률이어도.

    우리 분석 6위의 고유 발견이다. NTFS 는 레코드를 번호 순으로
    할당하므로 번호가 작을수록 먼저 만들어졌다.

    ★ 기대 순서가 깨지면 그 자체가 소견이다.
      사용자 컨테이너가 프로그램 본체보다 번호가 작으면 전에 설치했다
      지운 잔재가 있다는 뜻이다.
    """
    out = []
    stages = [o for o in bt.get('install_stage', [])
              if o.fact.get('stage') and o.fact.get('is_dir')]
    if not stages:
        return out
    # 단계별로 가장 작은 엔트리 번호를 대표로
    best = {}
    for o in stages:
        s = o.fact['stage']
        if s not in best or o.fact['entry'] < best[s].fact['entry']:
            best[s] = o
    order = sorted(best.values(), key=lambda o: o.fact['entry'])
    EXPECT = ['패키지 등록', '프로그램 본체', '사용자 컨테이너', '앱 저장소']
    # 참고 단계는 순서 판정에서 뺀다
    order = [o for o in order if o.fact['stage'] in EXPECT] or order
    got = [o.fact['stage'] for o in order]
    expected_here = [s for s in EXPECT if s in got]
    ok = got == expected_here

    times = [_t(o, 'ts_mft_si_created') for o in order
             if _t(o, 'ts_mft_si_created')]
    out.append(Inference(
        ids.next('inf'), 'B01', BEHAVIORS['B01'][0], 'Q1',
        'R-INSTALL-ORDER-01',
        when_from=min(times) if times else '', when_to=max(times) if times else '',
        when_basis='설치 단계 디렉터리의 $MFT 생성 시각',
        supported_by=[o.id for o in order],
        confidence=CONFIRMED if ok else SUSPECTED,
        alternatives=([] if ok else
                      ['기대 순서와 다르다 — 이전 설치의 잔재가 있을 수 있다']),
        basis={'rule': 'R-INSTALL-ORDER-01',
               'why': 'NTFS 는 레코드를 번호 순으로 할당한다. 시각이 같아도 '
                      '번호로 순서가 확정된다',
               'expected': expected_here, 'observed': got,
               'matches_expected': ok,
               'source': '우리 분석 6위 — $MFT 엔트리 번호로 순서 확정'},
        detail={'stages': [{'stage': o.fact['stage'],
                            'entry': o.fact['entry'],
                            'path': o.source_path,
                            'created': _t(o, 'ts_mft_si_created')}
                           for o in order],
                'matches_expected': ok},
        note='설치 %d단계 %s' % (len(order), '순서 정상' if ok else '★순서 이상')))

    for o in bt.get('msix_manifest', []):
        out.append(Inference(
            ids.next('inf'), 'B01', BEHAVIORS['B01'][0], 'Q1',
            'R-MSIX-MANIFEST-01', confidence=CONFIRMED,
            when_basis='AppRepository 매니페스트의 존재',
            supported_by=[o.id],
            basis={'rule': 'R-MSIX-MANIFEST-01',
                   'why_confirmed': '이 파일은 패키지가 등록돼야 생긴다',
                   'source': '우리 분석 6위 — 파일명 자체가 버전'},
            detail={'package': o.fact.get('package'),
                    'version': o.fact.get('version')},
            note='MSIX 등록 · 버전 %s' % o.fact.get('version')))
    return out


# ── Q1/Q4 브라우저 ──────────────────────────────────────────────
def _browser(bt, ids):
    r"""어떻게 여기까지 왔나 — 검색 → 방문 → 다운로드.

    ★ 검색어는 두 곳을 다 봐야 한다(우리 분석 7위).
      기본 검색엔진 검색만 keyword_search_terms 에 구조화되고,
      사이트 내 검색은 URL 쿼리스트링에만 남는다.
    """
    out = []
    visits = [o for o in bt.get('browser_visit', [])]
    if visits:
        times = sorted(_t(x, 'ts_visit') for x in visits if _t(x, 'ts_visit'))
        # 로그인 전 상태를 말하는 제목
        signin = [o for o in visits
                  if 'sign in' in (o.fact.get('title') or '').lower()]
        out.append(Inference(
            ids.next('inf'), 'B01', BEHAVIORS['B01'][0], 'Q1',
            'R-BROWSER-VISIT-01',
            when_from=times[0] if times else '',
            when_to=times[-1] if times else '',
            when_basis='브라우저 방문 기록 %d건' % len(visits),
            supported_by=[o.id for o in visits[:40]], confidence=CONFIRMED,
            alternatives=['방문은 사람이 눌렀을 수도, 리다이렉트였을 수도 있다'],
            basis={'rule': 'R-BROWSER-VISIT-01',
                   'why_confirmed': '방문 기록은 실제로 열어야 생긴다',
                   'visits': len(visits),
                   'signin_pages': len(signin),
                   'source': '우리 분석 7위 — 브라우저 History'},
            detail={'visits': len(visits),
                    'sample': [o.fact.get('url', '')[:110] for o in visits[:8]],
                    'signin_titles': [o.fact.get('title')
                                      for o in signin[:3]]},
            note='AI 서비스 방문 %d건%s'
                 % (len(visits),
                    (' · 로그인 전 화면 %d건' % len(signin)) if signin else '')))

    terms = {}
    for o in bt.get('browser_search', []):
        terms.setdefault((o.fact.get('term') or '').lower(), o)
    for o in visits:
        t = (o.fact.get('search_term_in_url') or '').lower()
        if t:
            terms.setdefault(t, o)
    if terms:
        out.append(Inference(
            ids.next('inf'), 'B01', BEHAVIORS['B01'][0], 'Q1',
            'R-BROWSER-SEARCH-01', confidence=CONFIRMED,
            when_basis='브라우저 검색어 (두 출처를 합침)',
            supported_by=[o.id for o in list(terms.values())[:30]],
            basis={'rule': 'R-BROWSER-SEARCH-01',
                   'why_confirmed': '검색어는 실제로 입력해야 남는다',
                   'note': '기본 검색엔진 검색은 keyword_search_terms, '
                           '사이트 내 검색은 URL 쿼리스트링에만 남는다',
                   'source': '우리 분석 7위'},
            detail={'terms': sorted(terms)[:30]},
            note='검색어 %d종' % len(terms)))

    dls = bt.get('browser_download', [])
    for o in dls:
        out.append(Inference(
            ids.next('inf'), 'B10', BEHAVIORS['B10'][0], 'Q4',
            'R-BROWSER-DOWNLOAD-01', confidence=CONFIRMED,
            when_basis='브라우저 다운로드 기록',
            supported_by=[o.id],
            basis={'rule': 'R-BROWSER-DOWNLOAD-01',
                   'why_confirmed': '다운로드 기록은 실제로 받아야 생긴다',
                   'source': '우리 분석 7위'},
            detail={'target': o.fact.get('target_path'),
                    'source_url': o.fact.get('source_url')},
            note='다운로드 %s' % (o.fact.get('target_path') or '')[-46:]))
    return out


# ── Q4 업로드 ───────────────────────────────────────────────────
def _uploads(bt, ids):
    r"""업로드 레코드에 원본 절대 경로가 남는다 (R1).

    파일 본문은 로컬에 없지만 **어느 파일을 올렸는지와 그 파일이 어디
    있었는지** 가 남는다. 데이터 반출 범위를 따질 때 이게 핵심이다.
    """
    out = []
    ups = bt.get('file_upload', [])
    if not ups:
        return out
    by_path = {}
    for o in ups:
        by_path.setdefault(o.fact.get('local_path') or '', []).append(o)
    for p, lst in sorted(by_path.items()):
        names = sorted({x.fact.get('file_name') for x in lst
                        if x.fact.get('file_name')})
        out.append(Inference(
            ids.next('inf'), 'B09', BEHAVIORS['B09'][0], 'Q4',
            'R-UPLOAD-LOCALPATH-01', confidence=CONFIRMED,
            when_basis='업로드 레코드의 존재 (시각은 레코드 순서로만)',
            supported_by=[x.id for x in lst],
            alternatives=['같은 파일을 여러 번 올리면 레코드가 여럿이다 — '
                          '레코드 수가 곧 업로드 횟수는 아니다'],
            basis={'rule': 'R-UPLOAD-LOCALPATH-01',
                   'why_confirmed': '앱이 업로드할 때만 localPath 를 적는다',
                   'records': len(lst),
                   'source': 'R1 · 업로드가 남긴 원본 경로'},
            detail={'local_path': p, 'file_names': names,
                    'records': len(lst)},
            note='업로드 %s' % (names[0] if names else p[-40:])))
    return out


# ── Q5 임시 대화 ────────────────────────────────────────────────
def _temporary(bt, ids):
    r"""임시 대화는 내용이 안 남아도 식별자는 남는다 (R3).

    그리고 지운 흔적조차 남는다 — 값이 있다가 길이 0 으로 바뀐다.
    LevelDB 는 덮어쓰지 않으므로 그 전후가 둘 다 파일에 있다.
    """
    out = []
    tmp = sorted(bt.get('temporary_conversation', []),
                 key=lambda o: (o.seq if o.seq is not None else 0))
    if not tmp:
        return out
    had = [o for o in tmp if not o.fact.get('cleared')]
    cleared = [o for o in tmp if o.fact.get('cleared')]
    uuids = sorted({u for o in tmp for u in (o.fact.get('uuids') or [])})
    out.append(Inference(
        ids.next('inf'), 'B12', BEHAVIORS['B12'][0], 'Q5',
        'R-TEMPCHAT-01', confidence=CONFIRMED,
        when_basis='임시 대화 식별자 레코드의 존재 (저장소 순서로만)',
        supported_by=[o.id for o in tmp[:30]],
        alternatives=['내용은 남지 않는다 — 무엇을 말했는지는 알 수 없다'],
        basis={'rule': 'R-TEMPCHAT-01',
               'why_confirmed': '이 키는 임시 대화를 써야 생긴다',
               'records': len(tmp), 'cleared_records': len(cleared),
               'source': 'R3 · 임시 대화가 남긴 것'},
        detail={'records': len(tmp), 'uuids': uuids[:8],
                'cleared': len(cleared)},
        note='임시 대화 사용 · 레코드 %d개%s'
             % (len(tmp),
                (' · 지운 흔적 %d개' % len(cleared)) if cleared else '')))
    if had and cleared:
        out.append(Inference(
            ids.next('inf'), 'B12', BEHAVIORS['B12'][0], 'Q5',
            'R-TEMPCHAT-CLEARED-01', confidence=CONFIRMED,
            when_basis='값이 있던 레코드와 길이 0 레코드가 함께 남았다',
            supported_by=[had[-1].id, cleared[-1].id],
            basis={'rule': 'R-TEMPCHAT-CLEARED-01',
                   'why_confirmed': 'LevelDB 는 덮어쓰지 않는다. 지우기 전후가 '
                                    '둘 다 파일에 있다',
                   'source': 'R3 결론 03 · 지운 흔적조차 남는다'},
            detail={'before_seq': had[-1].seq, 'after_seq': cleared[-1].seq},
            note='임시 대화 식별자를 지운 흔적'))
    return out


# ── Q3/Q5 메시지 식별자 (UUIDv7) ────────────────────────────────
def _messages(bt, ids, convs):
    r"""식별자 하나에서 시각이 나온다.

    UUIDv7 상위 48비트가 생성 밀리초다. 내용이 사라져도 '언제 썼나' 는
    남는다. 그리고 수정은 기존 메시지를 갱신하지 않고 새 메시지를 만들어서
    원문과 수정본이 별개 레코드로 공존한다(행위 07).
    """
    out = []
    msgs = [o for o in bt.get('message_identity', []) if _t(o, 'ts_uuid7')]
    if not msgs:
        return out
    by_conv = defaultdict(list)
    for o in msgs:
        by_conv[o.fact.get('conversation') or ''].append(o)
    for cu, lst in sorted(by_conv.items(), key=lambda kv: len(kv[1]), reverse=True):
        lst.sort(key=lambda o: _t(o, 'ts_uuid7'))
        title = lst[0].fact.get('title') or ''
        state = lst[0].fact.get('conv_state') or ''
        out.append(Inference(
            ids.next('inf'), 'B06', BEHAVIORS['B06'][0], 'Q3',
            'R-UUID7-TIME-01',
            when_from=_t(lst[0], 'ts_uuid7'), when_to=_t(lst[-1], 'ts_uuid7'),
            when_basis='메시지 UUIDv7 %d개에서 역산한 처음과 마지막' % len(lst),
            supported_by=[o.id for o in lst[:40]], confidence=CONFIRMED,
            alternatives=['식별자는 메시지 생성 시각이다 — 사용자가 타이핑한 '
                          '시각과는 조금 다르다'],
            basis={'rule': 'R-UUID7-TIME-01',
                   'why_confirmed': 'UUIDv7 은 상위 48비트에 생성 밀리초를 '
                                    '담도록 정해져 있다',
                   'messages': len(lst),
                   'source': '행위 07 · UUIDv7 역산'},
            detail={'conversation': cu, 'title': title,
                    'state': state, 'messages': len(lst)},
            note='%s%s · 메시지 %d개'
                 % (title[:26] or cu[:18], ' [삭제]' if state == 'deleted' else '',
                    len(lst))))
    return out


# ── 윈도우 사용자 귀속 (BAM · UserAssist) ───────────────────────
def _windows_user(bt, ids):
    r"""Claude 계정이 아니라 '어느 윈도우 사용자가 실행했나' 를 답한다.

    한 PC 를 여럿이 쓰면 둘이 갈린다. 섀도우 AI 사건의 귀속은 후자다.

    ★ MSIX 는 BAM 에 경로가 아니라 패키지 패밀리명으로 적힌다.
      실측: 설치 관리자는 \Device\HarddiskVolume3\Analysis\Claude Setup.exe,
            앱 본체는 Claude_pzs8sxrjxfjjc.
      경로로만 찾으면 앱 본체를 통째로 놓친다.
    """
    out = []
    bysid = defaultdict(list)
    for o in bt.get('bam_last_run', []):
        bysid[o.fact.get('sid') or ''].append(o)
    for sid, lst in sorted(bysid.items()):
        times = sorted(_t(x, 'ts_bam_last_run') for x in lst
                       if _t(x, 'ts_bam_last_run'))
        targets = [x.fact.get('target') for x in lst]
        pkg = [t for t in targets if t and '\\' not in t]
        out.append(Inference(
            ids.next('inf'), 'B01', '윈도우 사용자 귀속', 'Q2',
            'R-BAM-SID-01',
            when_from=times[0] if times else '',
            when_to=times[-1] if times else '',
            when_basis='BAM 이 SID 별로 적은 최종 실행 시각',
            actor=sid, supported_by=[x.id for x in lst],
            confidence=CONFIRMED,
            alternatives=['BAM 은 최종 실행만 남긴다 — 그 이전 실행의 시각은 없다'],
            basis={'rule': 'R-BAM-SID-01',
                   'why_confirmed': 'BAM 은 실제로 실행돼야 기록된다. '
                                    '그리고 SID 별로 나뉘어 있어 사용자가 특정된다',
                   'targets': targets,
                   'package_form': pkg,
                   'note': 'MSIX 는 경로가 아니라 패키지 패밀리명으로 기록된다',
                   'source': '우리 분석 6위 항목 — BAM'},
            detail={'sid': sid, 'targets': targets,
                    'last_runs': times},
            note='SID %s · 대상 %d개' % (sid[-12:], len(lst))))

    for o in bt.get('userassist_run', []):
        out.append(Inference(
            ids.next('inf'), 'B01', '사용자가 직접 실행', 'Q2',
            'R-USERASSIST-01',
            when_from=_t(o, 'ts_ua_last_run'), when_to=_t(o, 'ts_ua_last_run'),
            when_basis='UserAssist 의 마지막 실행 시각',
            actor=o.actor, supported_by=[o.id], confidence=CONFIRMED,
            alternatives=['UserAssist 실행 횟수는 과소 계상된다'],
            basis={'rule': 'R-USERASSIST-01',
                   'why_confirmed': 'UserAssist 는 GUI 로 눌러 실행한 것만 '
                                    '기록한다 — 자동 시작이 아니라는 증거다',
                   'run_count': o.fact.get('run_count'),
                   'source': '우리 분석 6위 항목 — UserAssist'},
            detail={'target': o.fact.get('target'),
                    'run_count': o.fact.get('run_count'),
                    'windows_user': o.fact.get('windows_user')},
            note='%s 직접 실행 %d회'
                 % (o.fact.get('windows_user') or '?', o.fact.get('run_count') or 0)))

    # 키가 아예 없었으면 그것도 결과다
    for o in bt.get('userassist_absent', []):
        out.append(Inference(
            ids.next('inf'), 'B01', '사용자가 직접 실행', 'Q2',
            'R-ABSENCE-01', confidence=UNDECIDED,
            when_basis='UserAssist 키가 이 하이브에 없다',
            supported_by=[o.id],
            alternatives=['키가 생기기 전이다',
                          '트랜잭션 로그(.LOG1/.LOG2) 를 반영해야 보인다',
                          'MSIX 앱은 기록되지 않았을 수 있다'],
            basis={'rule': 'R-ABSENCE-01',
                   'why_undecided': '부재는 여러 이유로 설명된다'},
            detail={'hive': o.source_path},
            note='UserAssist 없음 — 직접 실행 여부 판정 불가'))
    return out


# ── 타임존 역산 ─────────────────────────────────────────────────
def _timezone(bt, ids):
    r"""로그의 로컬 시각과 UTC 증거를 맞대어 기기 타임존을 구한다.

    앱 로그에는 타임존이 안 붙는다. 그래서 지금까지 로그 근거는 전부
    '유력' 에 묶였다 — 시각을 UTC 증거와 나란히 놓을 수 없었기 때문이다.

    그런데 같은 사건이 양쪽에 다 적힌 경우가 있다. 로그아웃이 그렇다.

        Local Storage  epitaxy-context-usage-logout-at   06:33:11.655 UTC
        main.log       Login-state transition (…→true)   15:33:10 로컬

    차이가 정시(15분 단위)에 가까우면 그게 기기 타임존이다. 짝이 여러 개
    나오고 값이 일치하면 우연이 아니다.

    ★ 15분 단위로 맞춘다. 세계의 타임존은 전부 15분 배수다.
    """
    out = []
    utc_anchors = []
    for o in bt.get('logout_at', []):
        t = _t(o, 'ts_record')
        if t:
            utc_anchors.append(('logout', t, o))
    local_anchors = []
    for o in bt.get('app_log_login_transition', []):
        if o.fact.get('logged_out_after') and _t(o, 'ts_log_local'):
            local_anchors.append(('logout', _t(o, 'ts_log_local'), o))
    if not utc_anchors or not local_anchors:
        return out

    QUARTER = 15 * 60 * 1000
    votes = {}
    pairs = []
    for _k, ut, uo in utc_anchors:
        um = iso_to_ms(ut)
        for _k2, lt, lo in local_anchors:
            lm = iso_to_ms(lt)
            if um is None or lm is None:
                continue
            diff = lm - um
            if abs(diff) > 15 * 3600 * 1000:      # ±15시간 넘으면 다른 사건
                continue
            off = int(round(diff / float(QUARTER))) * QUARTER
            # 정시 격자에서 2분 넘게 벗어나면 같은 사건이 아니다
            if abs(diff - off) > 120000:
                continue
            votes[off] = votes.get(off, 0) + 1
            pairs.append((ut, lt, diff, off, uo.id, lo.id))
    if not votes:
        return out
    off, n = max(votes.items(), key=lambda kv: kv[1])
    best = [p for p in pairs if p[3] == off]
    hours = off / 3600000.0
    # 짝이 하나뿐이면 우연일 수 있다. 둘 이상이어야 확정을 준다.
    level = CONFIRMED if n >= 2 else SUSPECTED
    out.append(Inference(
        ids.next('inf'), 'B01', '기기 타임존 판정', 'Q1',
        'R-TZ-DERIVE-01',
        when_from=best[0][0], when_to=best[-1][0],
        when_basis='같은 사건이 UTC 아티팩트와 로컬 로그에 모두 적힌 짝 %d개' % n,
        supported_by=[p[4] for p in best] + [p[5] for p in best],
        confidence=level,
        alternatives=([] if n >= 2 else
                      ['짝이 하나뿐이라 우연일 수 있다']),
        basis={'rule': 'R-TZ-DERIVE-01',
               'offset_ms': off, 'offset_hours': hours,
               'matched_pairs': n,
               'residual_ms': [abs(p[2] - p[3]) for p in best],
               'why': '타임존은 15분 배수다. 격자에 맞고 짝이 여럿이면 우연이 아니다',
               'effect': '앱 로그의 로컬 시각을 UTC 로 옮길 수 있다'},
        detail={'offset_hours': hours,
                'pairs': [{'utc': p[0], 'local': p[1],
                           'diff_ms': p[2]} for p in best]},
        note='UTC%+g시간 (짝 %d개, 오차 %dms 이내)'
             % (hours, n, max(abs(p[2] - p[3]) for p in best))))
    return out


# ── 로그인 상태 전이 (앱 로그 · 우리 분석 2위 아티팩트) ─────────
def _login_transitions(bt, ids):
    r"""로그 한 줄에 '언제 · 누가 · 무엇을' 이 다 들어 있다.

        Login-state transition (loggedOut: true → false,
            uuid: 7612a620-… → 88c48471-…), clearing oauth cache

    ★ preserving / clearing 이 결정적이다.
        preserving  같은 계정으로 다시 로그인  -> 행위 02
        clearing    계정이 바뀜               -> 행위 04
      쿠키만 보면 둘이 구분되지 않는다. 이 한 단어가 가른다.

    ★ 로그아웃 줄에도 직전 계정 UUID 가 남는다.
      그래서 로그아웃된 기기에서도 마지막 사용 계정이 특정된다.

    시각은 로컬이고 타임존 근거가 파일에 없다. 그래서 '확정' 을 주지
    않는다 — 계정 식별 자체는 확실하지만 시각이 다른 UTC 증거와 어긋날
    수 있기 때문이다.
    """
    out = []
    tr = sorted([o for o in bt.get('app_log_login_transition', [])
                 if _t(o, 'ts_log_local')],
                key=lambda o: _t(o, 'ts_log_local'))
    for o in tr:
        f = o.fact
        t = _t(o, 'ts_log_local')
        u_from, u_to = f.get('uuid_from') or '', f.get('uuid_to') or ''
        cache = f.get('oauth_cache') or ''
        if f.get('logged_out_after'):
            bid, rule = 'B03', 'R-LOGOUT-LOGLINE-01'
            note = '로그아웃 — 직전 계정 %s' % (u_from[:14] or '미상')
            why = '로그아웃 줄에 직전 계정 UUID 가 평문으로 남는다'
            alts = []
        elif u_from and u_to and u_from != u_to:
            bid, rule = 'B04', 'R-ACCTSWITCH-LOGLINE-01'
            note = '계정 전환 %s → %s (%s)' % (u_from[:12], u_to[:12], cache)
            why = ('uuid 가 바뀌고 oauth 캐시를 %s 한다 — 같은 계정 재로그인이 '
                   '아니다' % (cache or 'clearing'))
            alts = []
        elif f.get('same_account') or cache == 'preserving':
            bid, rule = 'B02', 'R-RELOGIN-LOGLINE-01'
            note = '동일 계정 재로그인 %s (%s)' % (u_to[:14], cache)
            why = 'uuid 가 같고 oauth 캐시를 보존한다(preserving)'
            alts = []
        else:
            bid, rule = 'B02', 'R-LOGIN-LOGLINE-01'
            note = '로그인 %s (%s)' % (u_to[:14] or '미상', cache)
            why = '로그아웃 상태에서 계정이 붙었다'
            alts = ['최초 로그인인지 재로그인인지는 이 줄만으로 모른다']
        out.append(Inference(
            ids.next('inf'), bid, BEHAVIORS[bid][0], BEHAVIORS[bid][1],
            rule, when_from=t, when_to=t,
            when_basis='앱 로그의 로그인 상태 전이 줄 (로컬 시각 · 타임존 미상)',
            actor=u_to or u_from, supported_by=[o.id],
            confidence=SUSPECTED,
            alternatives=list(alts) + [
                '로그 시각에 타임존이 없다 — UTC 증거와 직접 비교하면 어긋날 수 있다'],
            basis={'rule': rule, 'why': why,
                   'uuid_from': u_from, 'uuid_to': u_to,
                   'oauth_cache': cache,
                   'tz': 'local(unknown offset)',
                   'source': '우리 분석 2위 아티팩트 — main.log'},
            detail={'uuid_from': u_from, 'uuid_to': u_to,
                    'oauth_cache': cache,
                    'logged_out_after': f.get('logged_out_after')},
            note=note))

    # 앱 로그가 말하는 계정×조직 쌍
    pairs = {}
    for o in bt.get('app_log_account_org', []):
        a, g = o.fact.get('account') or '', o.fact.get('org') or ''
        if a or g:
            pairs.setdefault((a, g), []).append(o)
    for (a, g), lst in sorted(pairs.items()):
        out.append(Inference(
            ids.next('inf'), 'B02', BEHAVIORS['B02'][0], 'Q2',
            'R-ACCTORG-LOGLINE-01',
            when_from=min(_t(x, 'ts_log_local') for x in lst),
            when_to=max(_t(x, 'ts_log_local') for x in lst),
            when_basis='앱 로그가 계정·조직을 함께 적은 줄 %d개' % len(lst),
            actor=a, supported_by=[x.id for x in lst[:20]],
            confidence=SUSPECTED,
            alternatives=['로그 시각에 타임존이 없다'],
            basis={'rule': 'R-ACCTORG-LOGLINE-01', 'lines': len(lst),
                   'source': '우리 분석 2위 아티팩트 — main.log'},
            detail={'account': a, 'org': g, 'lines': len(lst)},
            note='계정 %s / 조직 %s' % (a[:12] or '없음', g[:12] or '없음')))

    # 최초 실행 여부
    fr = [o for o in bt.get('app_log_first_run', []) if o.fact.get('is_first_run')]
    if fr:
        o = sorted(fr, key=lambda x: _t(x, 'ts_log_local'))[0]
        out.append(Inference(
            ids.next('inf'), 'B01', BEHAVIORS['B01'][0], 'Q1',
            'R-FIRSTRUN-LOGLINE-01',
            when_from=_t(o, 'ts_log_local'), when_to=_t(o, 'ts_log_local'),
            when_basis='앱 로그의 is_first_run: true',
            supported_by=[o.id], confidence=SUSPECTED,
            alternatives=['로그 시각에 타임존이 없다'],
            basis={'rule': 'R-FIRSTRUN-LOGLINE-01',
                   'why': '앱이 스스로 최초 실행이라고 적었다',
                   'source': '우리 분석 2위 아티팩트 — main.log'},
            note='앱이 최초 실행이라고 기록'))
    return out


# ── B04 계정/조직 전환 (plan-usage-history) ─────────────────────
def _org_switch(bt, ids):
    """조직 UUID 가 바뀌는 지점이 곧 전환 시각이다.

    457 바이트 평문 JSON 이고 복호화가 필요 없다. 실측에서 표본 6개가
    마커 6개와 1:1 로 대응했다. 값싸고 많이 말한다.
    """
    out = []
    samples = sorted(
        [o for o in bt.get('plan_sample', []) if _t(o, 'ts_sample')],
        key=lambda o: _t(o, 'ts_sample'))
    if not samples:
        return out
    orgs = []
    for o in samples:
        org = o.fact.get('org') or ''
        if not orgs or orgs[-1][0] != org:
            orgs.append((org, o))
    # 조직 목록
    for org in sorted({o.fact.get('org') for o in samples if o.fact.get('org')}):
        lst = [o for o in samples if o.fact.get('org') == org]
        out.append(Inference(
            ids.next('inf'), 'B02', BEHAVIORS['B02'][0], 'Q2',
            'R-ORG-SEEN-01',
            when_from=_t(lst[0], 'ts_sample'), when_to=_t(lst[-1], 'ts_sample'),
            when_basis='이 조직으로 기록된 사용량 표본의 처음과 마지막',
            supported_by=[x.id for x in lst], confidence=CONFIRMED,
            basis={'rule': 'R-ORG-SEEN-01',
                   'why_confirmed': '앱이 사용량을 기록할 때만 조직 UUID 를 '
                                    '적는다. 로그인 없이는 생기지 않는다',
                   'samples': len(lst)},
            detail={'org': org, 'samples': len(lst)},
            note='조직 %s · 표본 %d건' % (org[:18], len(lst))))
    # 전환 지점
    for k in range(1, len(orgs)):
        prev_org, _po = orgs[k - 1]
        org, o = orgs[k]
        prev = [x for x in samples
                if _t(x, 'ts_sample') < _t(o, 'ts_sample')]
        lo = _t(prev[-1], 'ts_sample') if prev else ''
        out.append(Inference(
            ids.next('inf'), 'B04', BEHAVIORS['B04'][0], 'Q2',
            'R-ORG-SWITCH-01', when_from=lo, when_to=_t(o, 'ts_sample'),
            when_basis='직전 표본과 조직이 바뀐 첫 표본 사이',
            supported_by=[prev[-1].id, o.id] if prev else [o.id],
            confidence=CONFIRMED,
            basis={'rule': 'R-ORG-SWITCH-01',
                   'why_confirmed': '같은 기기 기록에서 조직 UUID 가 바뀌려면 '
                                    '계정/조직을 바꾸는 수밖에 없다',
                   'from': prev_org, 'to': org},
            detail={'from_org': prev_org, 'to_org': org},
            note='%s → %s' % (prev_org[:12], org[:12])))
    return out


# ── 계정 식별자 디렉터리 ────────────────────────────────────────
def _identity_dirs(bt, ids):
    r"""식별자는 삭제 층위를 모두 견딘다.

    선행 다섯 회차 전부에서 로그아웃·계정전환·계정삭제를 넘겨 계정 UUID 가
    남았다. 그래서 이 디렉터리의 존재는 '그 계정이 이 기기에서 쓰였다' 의
    확정 증거이고, 반대로 '지금도 로그인 중' 의 증거는 아니다.
    """
    out = []
    pairs = {}
    for o in bt.get('account_org_pair', []):
        a, g = o.fact.get('account') or '', o.fact.get('org') or ''
        pairs.setdefault((a, g), []).append(o)
    for (a, g), lst in sorted(pairs.items()):
        dirs = sorted({x.fact.get('source_dir') for x in lst})
        out.append(Inference(
            ids.next('inf'), 'B02', BEHAVIORS['B02'][0], 'Q2',
            'R-IDDIR-01', confidence=CONFIRMED,
            when_basis='계정 식별자 디렉터리의 존재 (시각은 이 근거로 못 낸다)',
            actor=a, supported_by=[x.id for x in lst],
            alternatives=['식별자는 로그아웃·계정전환·계정삭제를 모두 견딘다 '
                          '— 지금도 로그인 중이라는 뜻은 아니다'],
            basis={'rule': 'R-IDDIR-01',
                   'why_confirmed': '이 경로는 그 계정으로 로그인해야 생긴다',
                   'dirs': dirs},
            detail={'account': a, 'org': g, 'dirs': dirs},
            note='계정 %s / 조직 %s (%s)'
                 % (a[:12], g[:12], '·'.join(d[:16] for d in dirs))))
    if len({a for a, _g in pairs}) > 1:
        accts = sorted({a for a, _g in pairs})
        # 계정·조직 짝마다 식별자 디렉터리 관찰을 근거로 건다.
        ev = [x.id for _k, lst in sorted(pairs.items()) for x in lst[:2]]
        out.append(Inference(
            ids.next('inf'), 'B04', BEHAVIORS['B04'][0], 'Q2',
            'R-MULTIACCT-DIR-01', confidence=CONFIRMED,
            when_basis='서로 다른 계정 UUID 디렉터리가 공존한다',
            supported_by=ev,
            basis={'rule': 'R-MULTIACCT-DIR-01', 'accounts': accts},
            detail={'accounts': accts},
            note='계정 %d개가 이 기기에서 쓰였다' % len(accts)))
    return out


# ── 로그아웃 vs 계정삭제 — 서로의 반증 ──────────────────────────
def _session_state(bt, ids, convs):
    r"""식별자만으로는 로그아웃과 계정삭제가 구분되지 않는다.

    선행 연구 결론 그대로다 — 둘은 '식별자 잔존' 에서 같고 '내용 처리' 에서
    갈린다. 그래서 두 규칙을 함께 발화시키고 서로를 contradicted_by 에
    넣는다. 하나를 임의로 고르지 않는다.

        sessionKey 없음 + 계정UUID 잔존 + 대화 있음  -> 로그아웃
        sessionKey 없음 + 계정UUID 잔존 + 대화 없음  -> 계정삭제
    """
    out = []
    cookies = bt.get('cookie', [])
    if not cookies:
        return out
    live_session = [o for o in cookies
                    if o.fact.get('kind') == 'session'
                    and o.fact.get('state') == 'live']
    dead_session = [o for o in cookies
                    if o.fact.get('kind') == 'session'
                    and o.fact.get('state') == 'deleted']
    has_ident = bool(bt.get('account_org_pair'))
    has_convs = bool(convs)

    if live_session:
        names = sorted({o.fact.get('name') for o in live_session})
        out.append(Inference(
            ids.next('inf'), 'B02', BEHAVIORS['B02'][0], 'Q2',
            'R-SESSION-LIVE-01', confidence=CONFIRMED,
            when_from=min(_t(o, 'ts_cookie_created') for o in live_session) or '',
            when_to=max(_t(o, 'ts_cookie_created') for o in live_session) or '',
            when_basis='세션 쿠키가 만들어진 시각',
            supported_by=[o.id for o in live_session],
            basis={'rule': 'R-SESSION-LIVE-01',
                   'why_confirmed': '세션 쿠키는 로그인해야 생긴다',
                   'cookies': names},
            detail={'cookies': names},
            note='세션 쿠키 %s 잔존 — 수집 시점에 로그인 상태'
                 % ','.join(names)))
        return out

    # 세션 쿠키가 없다 — 로그아웃인가 계정삭제인가
    sup = [o.id for o in dead_session] or [o.id for o in cookies[:5]]
    a_id = ids.next('inf')
    b_id = ids.next('inf')
    common = {
        'sup': sup,
        'basis_common': {'session_cookie_present': False,
                         'identity_dirs_present': has_ident,
                         'conversations_present': has_convs},
    }
    out.append(Inference(
        a_id, 'B03', BEHAVIORS['B03'][0], 'Q2', 'R-LOGOUT-01',
        confidence=SUSPECTED if has_convs else PRESUMED,
        when_basis='세션 쿠키 부재 + 계정 식별자 잔존 + 대화 데이터 존재',
        supported_by=sup, contradicted_by=[b_id],
        alternatives=['계정 삭제여도 식별자는 남는다 — 대화 데이터 존부로만 갈린다'],
        basis=dict(common['basis_common'], rule='R-LOGOUT-01'),
        note='로그아웃 정황 (계정삭제와 경합)'))
    out.append(Inference(
        b_id, 'B11', BEHAVIORS['B11'][0], 'Q5', 'R-ACCTDEL-01',
        confidence=PRESUMED if has_convs else SUSPECTED,
        when_basis='세션 쿠키 부재 + 계정 식별자 잔존 + 대화 데이터 부재',
        supported_by=sup, contradicted_by=[a_id],
        alternatives=['로그아웃이어도 세션 쿠키는 사라진다'],
        basis=dict(common['basis_common'], rule='R-ACCTDEL-01'),
        note='계정삭제 정황 (로그아웃과 경합)'))
    return out


# ── B01 최초 실행 ───────────────────────────────────────────────
def _first_run(bt, ids):
    out = []
    for o in bt.get('app_version_seen', []):
        t = _t(o, 'ts_record')
        if not t:
            continue
        out.append(Inference(
            ids.next('inf'), 'B01', BEHAVIORS['B01'][0], 'Q1',
            'R-FIRSTRUN-01', when_from=t, when_to=t,
            when_basis='앱이 스스로 적은 최초 인지 시각(firstSeenEpochMs)',
            supported_by=[o.id], confidence=CONFIRMED,
            basis={'rule': 'R-FIRSTRUN-01',
                   'why_confirmed': '이 값은 앱이 처음 뜰 때만 쓰인다. '
                                    '다른 행위로는 생성되지 않는다',
                   'observed': [o.id]},
            detail={'app_version': o.fact.get('version')},
            note='앱 버전 %s' % (o.fact.get('version') or '?')))
    return out


# ── B01 실행 · 설치 (Prefetch) ──────────────────────────────────
def _prefetch(bt, ids):
    r"""Prefetch 는 '실행됐다' 의 가장 단단한 증거다.

    다만 '사용자가 눌러서 실행했다' 는 아니다 — 자동 시작·업데이트·
    자식 프로세스로도 생긴다. 그래서 실행 자체는 확정, 사용자 조작
    여부는 대안가설로 남긴다.

    설치 관리자(CLAUDE SETUP.EXE)는 사람이 돌리지 않으면 생기지 않으므로
    한 등급 위로 본다.
    """
    out = []
    sums = bt.get('program_run_summary', [])

    # ① 카운터가 처음부터 센 것인가 — .pf 생성시각이 가장 이른 실행 근처면
    #    처음부터다. 한참 뒤면 그 사이에 .pf 가 지워진 것이고, 횟수는
    #    '전체' 가 아니라 '그 시점 이후' 다.
    born = [_t(o, 'ts_pf_created') for o in sums if _t(o, 'ts_pf_created')]
    first_born = min(born) if born else ''

    # ② 어느 해시가 메인 프로세스인가 — 앱 로그의 기동 횟수와 같은 것.
    #    이걸 못 정하면 9개 해시의 합(123)을 '앱을 123번 켰다' 로 잘못
    #    읽게 된다. 실제로는 Electron 이 역할별로 따로 띄운 것이다.
    n_start = len(bt.get('app_log_app_start', []))
    main_hash = ''
    if n_start:
        for o in sums:
            if o.fact.get('run_count') == n_start:
                main_hash = o.fact.get('hash') or ''
                break

    for o in sums:
        name = (o.fact.get('name') or '').upper()
        setup = 'SETUP' in name
        runs = [x for x in bt.get('program_run', [])
                if x.fact.get('hash') == o.fact.get('hash')]
        times = sorted(_t(x, 'ts_run') for x in runs if _t(x, 'ts_run'))
        if not times:
            continue

        created = _t(o, 'ts_pf_created')
        # 가장 이른 .pf 생성보다 10분 넘게 늦으면 나중에 생긴 것이다
        late = bool(created and first_born and created > first_born
                    and (iso_to_ms(created) or 0)
                    - (iso_to_ms(first_born) or 0) > 600000)
        complete = bool(created) and not late
        is_main = (o.fact.get('hash') == main_hash) and not setup

        if setup:
            rule, level, alts = 'R-INSTALL-PF-01', CONFIRMED, []
            why = '설치 관리자는 사람이 실행하지 않으면 생기지 않는다'
            note = '%s 실행 %d회' % (o.fact.get('name', '?'),
                                  o.fact.get('run_count', 0))
        else:
            rule, level = 'R-RUN-PF-01', CONFIRMED
            alts = ['자동 시작·업데이트·자식 프로세스로도 실행될 수 있다 '
                    '— 사람이 눌렀다는 증거는 아니다']
            why = 'Prefetch 는 그 실행파일이 실제로 돌았을 때만 만들어진다'
            note = '%s%s 실행 %d회 · 시각 %d개' % (
                o.fact.get('name', '?'), ' [메인]' if is_main else '',
                o.fact.get('run_count', 0), len(times))

        miss = max(0, (o.fact.get('run_count') or 0) - len(times))
        if miss:
            alts = list(alts) + [
                '실행 %d회 중 %d회는 시각이 없다 — Prefetch 는 마지막 8회만 '
                '남긴다' % (o.fact.get('run_count'), miss)]
        if not complete:
            alts = list(alts) + [
                '.pf 가 설치 이후에 새로 만들어졌다 — 이 횟수는 전체가 '
                '아니라 %s 이후의 횟수다' % (created or '알 수 없는 시점')]

        out.append(Inference(
            ids.next('inf'), 'B01', BEHAVIORS['B01'][0], 'Q1', rule,
            when_from=times[0], when_to=times[-1],
            when_basis='Prefetch 에 남은 마지막 실행 시각 %d개의 처음과 끝'
                       % len(times),
            supported_by=[o.id] + [x.id for x in runs],
            confidence=level, alternatives=alts,
            basis={'rule': rule, 'why': why,
                   'run_count': o.fact.get('run_count'),
                   'recorded_runs': len(times),
                   'unrecorded_runs': miss,
                   'counter_complete': complete,
                   'counter_scope': ('설치 시점부터의 전체 횟수' if complete
                                     else '%s 이후의 횟수' % (created or '?')),
                   'is_main_process': is_main,
                   'main_process_basis': (
                       '앱 로그의 기동 %d건과 실행 횟수가 일치한다' % n_start
                       if is_main else
                       ('앱 로그 기동 %d건과 일치하지 않는다' % n_start
                        if n_start else '앱 로그가 없어 판별 못 함')),
                   'note': 'Prefetch 는 마지막 8회만 시각을 남긴다. '
                           '횟수는 별도 카운터라 온전하다'},
            detail={'name': o.fact.get('name'),
                    'hash': o.fact.get('hash'),
                    'full_path': o.fact.get('full_path'),
                    'run_count': o.fact.get('run_count'),
                    'unrecorded_runs': miss,
                    'counter_complete': complete,
                    'is_main_process': is_main,
                    'pf_created': created,
                    'run_times': times[::-1]},
            note=note))
    return out


# ── 앱 로그 ─────────────────────────────────────────────────────
_LOG_MAP = {
    'app_log_app_start': ('B01', 'R-APPSTART-LOG-01', 'Q1', '앱 기동'),
    'app_log_page_load': ('B01', 'R-PAGELOAD-LOG-01', 'Q1', 'claude.ai 로드'),
    'app_log_account': ('B02', 'R-ACCTLOG-01', 'Q2', '계정 정보 수신'),
    'app_log_session_load': ('B02', 'R-SESSLOAD-LOG-01', 'Q2', '세션 적재'),
    'app_log_oauth_fail': ('B13', 'R-OAUTHFAIL-LOG-01', 'Q4', 'OAuth 실패'),
    'app_log_mcp_config': ('B13', 'R-MCPCONF-LOG-01', 'Q4', 'MCP 설정 읽음'),
}


def _applog(bt, ids):
    """앱 로그는 시각이 로컬이고 타임존 근거가 파일에 없다.

    그래서 '확정' 을 줄 수 없다. 사건 타임라인에 다른 UTC 증거와
    나란히 놓으면 몇 시간 어긋날 수 있기 때문이다.
    """
    out = []
    for ft, (bid, rule, q, label) in _LOG_MAP.items():
        lst = bt.get(ft, [])
        if not lst:
            continue
        times = sorted(_t(x, 'ts_log_local') for x in lst
                       if _t(x, 'ts_log_local'))
        if not times:
            continue
        out.append(Inference(
            ids.next('inf'), bid, BEHAVIORS[bid][0], q, rule,
            when_from=times[0], when_to=times[-1],
            when_basis='앱 로그 %d줄 (로컬 시각 · 타임존 미상)' % len(lst),
            supported_by=[x.id for x in lst[:40]], confidence=SUSPECTED,
            alternatives=['로그 시각에 타임존이 없다 — 다른 UTC 증거와 '
                          '직접 비교하면 어긋날 수 있다'],
            basis={'rule': rule, 'lines': len(lst),
                   'tz': 'local(unknown offset)',
                   'why_not_confirmed': '타임존 근거가 이 파일에 없다'},
            detail={'label': label, 'count': len(lst),
                    'samples': [x.fact.get('message', '')[:90]
                                for x in lst[:5]]},
            note='%s %d건' % (label, len(lst))))
    return out


# ── B03 로그아웃 ────────────────────────────────────────────────
def _logout(bt, ids):
    out = []
    for o in bt.get('logout_at', []):
        t = _t(o, 'ts_record')
        if not t:
            continue
        out.append(Inference(
            ids.next('inf'), 'B03', BEHAVIORS['B03'][0], 'Q2',
            'R-LOGOUT-01', when_from=t, when_to=t,
            when_basis='앱이 로그아웃 시각을 직접 기록한 값',
            supported_by=[o.id], confidence=CONFIRMED,
            basis={'rule': 'R-LOGOUT-01',
                   'why_confirmed': '키 이름이 곧 행위다. 로그아웃 외에 '
                                    '이 키가 쓰이는 경로가 없다',
                   'observed': [o.id]}))
    return out


# ── 이벤트 기반 ─────────────────────────────────────────────────
def _events(bt, ids):
    out = []
    groups = defaultdict(list)
    for o in bt.get('app_event', []):
        name = o.fact.get('event') or ''
        if name in EVENT_MAP:
            groups[name].append(o)
    for name, lst in groups.items():
        bid, rule, level, alts = EVENT_MAP[name]
        lst.sort(key=lambda x: _t(x, 'ts_event'))
        times = [_t(x, 'ts_event') for x in lst if _t(x, 'ts_event')]
        if not times:
            continue
        out.append(Inference(
            ids.next('inf'), bid, BEHAVIORS[bid][0], BEHAVIORS[bid][1],
            rule, when_from=times[0], when_to=times[-1],
            when_basis='앱 이벤트 %s %d건' % (name, len(lst)),
            actor=lst[0].actor,
            supported_by=[x.id for x in lst], confidence=level,
            alternatives=alts,
            basis={'rule': rule, 'event': name, 'hits': len(lst),
                   'alternatives_open': alts},
            detail={'event': name, 'count': len(lst), 'times': times[:20]}))
    return out


# ── B11 삭제 ────────────────────────────────────────────────────
def _deleted(bt, ids):
    r"""장부의 d=1 로 삭제를 잡고, 스냅샷 차이로 시각을 좁힌다.

    ★ 없는 것은 근거가 아니다. d 플래그가 없다고 '안 지웠다' 가 아니다.
      실측에서 한국어 대화는 실제로 삭제됐는데 장부 스냅샷에는 플래그가
      없었다 — 장부가 갱신되기 전 스냅샷이었기 때문이다.
    """
    out = []
    snaps = sorted(bt.get('ledger_snapshot', []),
                   key=lambda o: (o.seq if o.seq is not None else 0))
    # 어느 스냅샷에서 처음 d=1 이 되었나
    first_seen = {}
    prev_time = {}
    for s in snaps:
        for u in s.fact.get('deleted') or []:
            if u not in first_seen:
                first_seen[u] = s
    # 대화별 마지막 '삭제 아님' 스냅샷 시각 = 하한
    for s in snaps:
        for u in s.fact.get('uuids') or []:
            if u not in (s.fact.get('deleted') or []):
                prev_time[u] = s

    entries = defaultdict(list)
    for o in bt.get('ledger_entry', []):
        entries[o.fact.get('conversation')].append(o)

    for u, s in first_seen.items():
        lo = prev_time.get(u)
        ent = [e for e in entries.get(u, []) if e.fact.get('deleted_flag')]
        t_hi = ''
        for e in ent:
            t_hi = _t(e, 'ts_record') or t_hi
        t_lo = ''
        if lo is not None:
            for e in entries.get(u, []):
                if not e.fact.get('deleted_flag'):
                    t_lo = _t(e, 'ts_record') or t_lo
        sup = [s.id] + [e.id for e in ent]
        out.append(Inference(
            ids.next('inf'), 'B11', BEHAVIORS['B11'][0], 'Q5',
            'R-DELETE-LEDGER-01',
            when_from=t_lo or t_hi, when_to=t_hi,
            when_basis='장부에 d=1 이 나타난 스냅샷' +
                       (' · 직전 스냅샷을 하한으로' if t_lo else ''),
            supported_by=sup, confidence=CONFIRMED,
            basis={'rule': 'R-DELETE-LEDGER-01',
                   'why_confirmed': '앱이 삭제 플래그를 직접 적는다. '
                                    '다른 행위로 d=1 이 생기지 않는다',
                   'observed': sup},
            detail={'conversation': u},
            note='대화 %s' % u))

    # 부재는 근거가 아니다 — 판정 불가로 남긴다
    all_u = set()
    for s in snaps:
        all_u.update(s.fact.get('uuids') or [])
    for u in sorted(all_u - set(first_seen)):
        out.append(Inference(
            ids.next('inf'), 'B11', BEHAVIORS['B11'][0], 'Q5',
            'R-ABSENCE-01', confidence=UNDECIDED,
            when_basis='장부에 d 플래그가 없다',
            alternatives=['아직 안 지웠다',
                          '지웠지만 장부가 갱신되기 전 스냅샷만 남았다'],
            basis={'rule': 'R-ABSENCE-01',
                   'why_undecided': '플래그 부재는 두 가지로 설명된다. '
                                    '구분할 근거가 이 아티팩트에 없다'},
            detail={'conversation': u},
            note='대화 %s — 삭제 여부 판정 불가' % u))
    return out


# ── B06 재방문 ──────────────────────────────────────────────────
def _revisits(bt, ids):
    out = []
    best = {}
    for o in bt.get('conversation_revisit', []):
        u = o.fact.get('conversation')
        t = _t(o, 'ts_record')
        if not u or not t:
            continue
        if u not in best or t > _t(best[u], 'ts_record'):
            best[u] = o
    for u, o in sorted(best.items(), key=lambda x: _t(x[1], 'ts_record')):
        # 관찰은 '열었다' 이고 행위 목록의 6번은 '프롬프트 입력' 이다.
        # 열기만 하고 안 쳤을 수 있으므로 확정을 줄 수 없다. 관찰 자체는
        # 확정이지만 행위로의 도약에서 한 등급 내려간다.
        out.append(Inference(
            ids.next('inf'), 'B06', BEHAVIORS['B06'][0], 'Q3',
            'R-REVISIT-01', when_from=_t(o, 'ts_record'),
            when_to=_t(o, 'ts_record'),
            when_basis='기기별 재방문 기록의 마지막 열람 시각',
            actor=o.actor, supported_by=[o.id], confidence=SUSPECTED,
            alternatives=['대화를 열기만 하고 입력하지 않았을 수 있다'],
            basis={'rule': 'R-REVISIT-01',
                   'confirmed_part': '이 대화를 연 것은 확정이다 — '
                                     '앱이 열 때만 이 시각을 갱신한다',
                   'suspected_part': '입력까지 했는지는 이 아티팩트가 말하지 않는다',
                   'observed': [o.id]},
            detail={'conversation': u},
            note='대화를 열었다 %s' % u[:18]))
    return out


# ── 전송하지 않은 입력 ──────────────────────────────────────────
#: 세션 정리와 초안 비움을 같은 사건으로 볼 시간 창. 로그아웃 기록이
#  먼저 찍히고 화면 정리가 뒤따르므로 앞쪽은 좁게, 뒤쪽은 넓게 잡는다.
WIPE_NEAR_BEFORE_MS = 5 * 1000
WIPE_NEAR_AFTER_MS = 180 * 1000


def _draft_wiped(bt, ids):
    """입력창을 비운 시각. 다만 '누가' 비웠는지는 따로 가려야 한다.

    `chat-draft-write-signal` 이 "쳤다" 를 말한다면 이쪽은 "비워졌다" 를
    말한다. 앱이 직접 적은 값이라 시점 자체는 다툴 여지가 없다.

    ★ B01 실측에서 귀속을 고쳤다(2026-09-22).
      처음에는 이것을 B07(프롬프트 수정)에 붙였는데, 시연 데이터를 마커
      로그와 맞춰보니 어긋났다.

          15:20:35 비움  ←  15:20:08 로그아웃   +27초
          15:33:11 비움  ←  15:32:48 계정전환   +23초
          15:49:42 프롬프트 수정                 29분 떨어짐

      사용자가 감추려고 지운 게 아니라 **앱이 세션을 정리하며 비운 것**이다.
      그래서 로그아웃 기록이 가까이 있으면 그쪽(B03)에 붙이고, 없을 때만
      사용자 행위(B07) 로 두되 등급을 낮춘다. 근거가 가리키는 곳에
      붙이지 않으면 시각이 맞아도 결론이 틀린다.
    """
    out = []
    anchors = []
    for a in bt.get('logout_at', []):
        at = _t(a, 'ts_record')
        am = iso_to_ms(at) if at else None
        if am is not None:
            anchors.append((am, a))

    for o in bt.get('draft_wiped_at', []):
        t = _t(o, 'ts_record')
        if not t:
            continue
        tm = iso_to_ms(t)
        near = None
        if tm is not None:
            for am, a in anchors:
                d = tm - am
                if -WIPE_NEAR_BEFORE_MS <= d <= WIPE_NEAR_AFTER_MS:
                    if near is None or abs(d) < abs(near[0]):
                        near = (d, a)

        if near is not None:
            d, a = near
            out.append(Inference(
                ids.next('inf'), 'B03', BEHAVIORS['B03'][0], 'Q2',
                'R-DRAFT-WIPED-01', when_from=t, when_to=t,
                when_basis='앱이 입력창을 비운 시각을 직접 기록한 값',
                supported_by=[o.id, a.id], confidence=SUSPECTED,
                alternatives=[
                    '우연히 같은 시각대에 사용자가 직접 비웠을 수 있다',
                ],
                basis={'rule': 'R-DRAFT-WIPED-01',
                       'key': 'chat-draft-wipe-completed-at',
                       'gap_sec': round(d / 1000.0, 1),
                       'why': '로그아웃 기록과 %.1f초 차로 붙는다. 세션을 '
                              '정리하며 화면을 비운 것으로 본다' % (d / 1000.0)},
                detail={'raw': o.fact.get('raw')},
                note='세션 정리 과정에서 입력창이 비워졌다'))
            continue

        out.append(Inference(
            ids.next('inf'), 'B07', BEHAVIORS['B07'][0], 'Q5',
            'R-DRAFT-WIPED-01', when_from=t, when_to=t,
            when_basis='앱이 입력창을 비운 시각을 직접 기록한 값',
            supported_by=[o.id], confidence=PRESUMED,
            alternatives=[
                '전송해서 비워졌을 수 있다 — 비움 자체는 삭제를 뜻하지 않는다',
                '창을 옮기거나 대화를 바꿔도 비워질 수 있다',
                '수집물에 로그아웃 기록이 없어 세션 정리를 배제하지 못했다',
            ],
            basis={'rule': 'R-DRAFT-WIPED-01',
                   'key': 'chat-draft-wipe-completed-at',
                   'why': '가까운 로그아웃 기록이 없다. 사용자가 비웠을 '
                          '가능성이 남지만 단정할 근거는 없다'},
            detail={'raw': o.fact.get('raw')},
            note='입력창을 비운 시각 — 원인 미상'))
    return out


def _drafts(bt, ids, convs):
    """초안에만 있고 대화 어디에도 없으면 '치다 만 입력' 이다."""
    out = []
    bodies = []
    for c in (convs or []):
        # ★ uuid 없는 '미연결' 뭉치는 대조에서 뺀다.
        #   초안 자체가 조각으로 잡혀 그 뭉치에 들어가기 때문에, 포함하면
        #   초안이 초안과 일치해서 '전송됐다' 는 반대 결론이 나온다.
        #   실측에서 KOR-5520 이 정확히 그렇게 뒤집혔다.
        if not (c.get('uuid') or '').strip():
            continue
        for m in (c.get('messages') or []):
            t = (m.get('text') or '').strip()
            if t:
                bodies.append(t)
    joined = '\n'.join(bodies)

    for o in bt.get('composer_draft', []):
        txt = (o.fact.get('text') or '').strip()
        if not txt:
            continue
        head = txt[:60]
        sent = head in joined
        if not joined:
            # ★ 대조할 대화가 한 글자도 없다.
            #   이때 'head in joined' 는 언제나 False 라, 그대로 두면
            #   비교를 못 한 것을 '비교했더니 없었다' 로 적게 된다.
            #   근거 부재는 결론이 아니다 — 판정 불가로 남긴다.
            level, rule = UNDECIDED, 'R-DRAFT-NOCMP-01'
            note = '전송 여부 판정 불가 — 대조할 대화가 복원되지 않았다'
            alts = ['전송됐는데 대화가 복원되지 않았을 수 있다',
                    '실제로 전송되지 않았을 수 있다']
        elif sent:
            level, rule = PRESUMED, 'R-DRAFT-SENT-01'
            note = '전송된 것으로 보인다 — 같은 문구가 대화에 있다'
            alts = ['초안 자체가 조각으로 잡혀 대화처럼 보였을 수 있다']
        else:
            level, rule = SUSPECTED, 'R-DRAFT-UNSENT-01'
            note = '전송되지 않은 입력 — 대화 어디에도 같은 문구가 없다'
            alts = ['대화가 이미 컴팩션으로 사라져 대조가 불가능했을 수 있다']
        out.append(Inference(
            ids.next('inf'), 'B07', BEHAVIORS['B07'][0], 'Q5', rule,
            when_from=_t(o, 'ts_record'), when_to=_t(o, 'ts_record'),
            when_basis='초안이 마지막으로 갱신된 시각',
            actor=o.actor, supported_by=[o.id], confidence=level,
            alternatives=alts,
            basis={'rule': rule, 'matched_in_conversations': sent,
                   'compared_chars': len(joined)},
            detail={'text': txt[:300], 'chars': o.fact.get('chars'),
                    'attachments': o.fact.get('attachments')},
            note=note))
    return out



UUID7_RX = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}'
                      r'-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', re.I)


# ── B06/B07 서버 응답 — 본문은 없어도 '받았다' 는 남는다 ──────────
def _replies(bt, ids):
    """답변 본문이 로컬에 안 남는 버전에서도 응답 사실을 복원한다.

    ★ 2026-10-02 실측 (앱 2.9939.2)
      대화 본문(react-query-cache)은 사라졌지만 Local Storage 의
      이벤트 큐에 응답 관련 기록이 남는다. 본문은 못 가져와도
      '언제 보냈고 몇 초 만에 첫 글자가 왔고 언제 끝났나' 는 나온다.

      sent → perceived_ttft → first_text → perceived_ttlt
    """
    out = []
    ev = []
    for o in bt.get('app_event', []):
        name = (o.fact or {}).get('event') or ''
        if not name.startswith('claudeai.message.'):
            continue
        t = _t(o, 'ts_event')
        if not t:
            continue
        pr = (o.fact or {}).get('props') or {}
        ev.append({'o': o, 'kind': name.rsplit('.', 1)[-1], 't': t,
                   'conv': pr.get('conversation_uuid') or '',
                   'msg': pr.get('message_uuid') or '',
                   'sender': pr.get('sender') or ''})
    if not ev:
        return out
    ev.sort(key=lambda x: x['t'])

    # 1) 질문 → 응답 한 묶음
    #    sent 뒤에 오는 ttft/first_text/ttlt 를 같은 대화에서 찾는다.
    sents = [e for e in ev if e['kind'] == 'sent']
    for s_ in sents:
        later = [e for e in ev
                 if e['t'] > s_['t'] and e['conv'] == s_['conv']
                 and e['kind'] in ('perceived_ttft', 'first_text',
                                   'perceived_ttlt')]
        if not later:
            continue
        ttft = next((e for e in later if e['kind'] == 'perceived_ttft'), None)
        last = later[-1]
        a, b = iso_to_ms(s_['t']), iso_to_ms(last['t'])
        span = round(abs(b - a) / 1000.0, 3) if (a and b) else None
        alts = ['이벤트 큐는 앱이 스스로 적은 것이다 — '
                '응답 본문이 실제로 무엇이었는지는 알 수 없다']
        note = '응답 수신'
        if ttft:
            ta = iso_to_ms(ttft['t'])
            if a and ta:
                note += ' — 첫 글자까지 %.1f초' % ((ta - a) / 1000.0)
        if span:
            note += ' · 전체 %.1f초' % span
        out.append(Inference(
            ids.next('inf'), 'B06', BEHAVIORS['B06'][0], 'Q3',
            'R-REPLY-TIMING-01',
            when_from=s_['t'], when_to=last['t'],
            when_basis='앱이 적은 전송·수신 이벤트 시각',
            actor=s_['o'].actor,
            supported_by=[e['o'].id for e in ([s_] + later)[:8]],
            confidence=CONFIRMED, alternatives=alts,
            basis={'rule': 'R-REPLY-TIMING-01',
                   'conversation': s_['conv'],
                   'stages': [e['kind'] for e in later]},
            detail={'conversation': s_['conv'],
                    'stages': [e['kind'] for e in later]},
            note=note))

    # 2) 답변을 복사했다 — sender 로 가른다
    for e in ev:
        if e['kind'] not in ('copied', 'selection_copied'):
            continue
        if e['sender'] != 'assistant':
            continue
        how = ('일부를 드래그해' if e['kind'] == 'selection_copied' else '통째로')
        out.append(Inference(
            ids.next('inf'), 'B10', BEHAVIORS['B10'][0], 'Q4',
            'R-REPLY-COPIED-01',
            when_from=e['t'], when_to=e['t'],
            when_basis='앱이 적은 복사 이벤트 시각',
            actor=e['o'].actor, supported_by=[e['o'].id],
            confidence=CONFIRMED,
            alternatives=['복사했다는 기록일 뿐, 어디에 붙여 넣었는지는 모른다'],
            basis={'rule': 'R-REPLY-COPIED-01', 'how': e['kind'],
                   'message': e['msg']},
            detail={'conversation': e['conv'], 'message': e['msg']},
            note='답변을 %s 복사했다' % how))

    # 3) 답변 메시지 UUIDv7 -> 생성 시각
    #    본문이 사라져도 식별자만 있으면 '언제' 가 나온다.
    seen = set()
    for e in ev:
        u = (e['msg'] or '').strip()
        if not u or u in seen:
            continue
        if not UUID7_RX.match(u):
            continue
        seen.add(u)
        ms = int(u.replace('-', '')[:12], 16)
        out.append(Inference(
            ids.next('inf'), 'B06', BEHAVIORS['B06'][0], 'Q3',
            'R-REPLY-UUID7-01',
            when_from=utc_ms(ms), when_to=utc_ms(ms),
            when_basis='메시지 UUIDv7 상위 48비트 = 생성 밀리초',
            actor=e['o'].actor, supported_by=[e['o'].id],
            confidence=CONFIRMED,
            alternatives=['식별자 생성 시각이다 — 전송·수신 시각과 '
                          '밀리초 단위로 어긋날 수 있다'],
            basis={'rule': 'R-REPLY-UUID7-01', 'uuid': u,
                   'sender': e['sender']},
            detail={'message': u, 'sender': e['sender'],
                    'conversation': e['conv']},
            note='%s 메시지 식별자에서 역산' % (e['sender'] or '발신자미상')))

    # 4) 답변 누적 개수 — replies 카운터
    for o in bt.get('reply_count', []):
        n = (o.fact or {}).get('count')
        if not n:
            continue
        out.append(Inference(
            ids.next('inf'), 'B06', BEHAVIORS['B06'][0], 'Q3',
            'R-REPLY-COUNT-01',
            when_from=_t(o, 'ts_record'), when_to=_t(o, 'ts_record'),
            when_basis='카운터가 갱신된 시각',
            actor=o.actor, supported_by=[o.id], confidence=CONFIRMED,
            alternatives=['누적값이다 — 이 시점까지의 합이지 '
                          '이 순간 받은 수가 아니다'],
            basis={'rule': 'R-REPLY-COUNT-01', 'count': n},
            detail={'count': n},
            note='답변 누적 %s건' % n))
    return out


# ── B02/B04 계정 ────────────────────────────────────────────────
def _accounts(bt, ids):
    out = []
    seen = defaultdict(list)
    for o in bt.get('app_event', []):
        if o.actor:
            seen[o.actor].append(o)
    for acct, lst in seen.items():
        times = sorted(_t(x, 'ts_event') for x in lst if _t(x, 'ts_event'))
        if not times:
            continue
        out.append(Inference(
            ids.next('inf'), 'B02', BEHAVIORS['B02'][0], 'Q2',
            'R-ACCOUNT-SEEN-01', when_from=times[0], when_to=times[-1],
            when_basis='이 계정으로 기록된 앱 이벤트의 처음과 마지막',
            actor=acct, supported_by=[x.id for x in lst[:40]],
            confidence=CONFIRMED,
            basis={'rule': 'R-ACCOUNT-SEEN-01',
                   'why_confirmed': '이벤트마다 계정 UUID 가 붙는다. '
                                    '로그인 없이는 기록되지 않는다',
                   'events': len(lst)},
            detail={'account': acct, 'events': len(lst),
                    'org': (lst[0].fact.get('org') or '')},
            note='이벤트 %d건' % len(lst)))

    if len(seen) > 1:
        # 계정마다 처음·마지막 이벤트를 근거로 건다. 집계 판정이라고
        # supported_by 를 비워 두면 confirmed 를 받칠 게 아무것도 없다.
        ev = []
        for lst in seen.values():
            srt = sorted(lst, key=lambda x: _t(x, 'ts_event') or '')
            ev.append(srt[0].id)
            if len(srt) > 1:
                ev.append(srt[-1].id)
        out.append(Inference(
            ids.next('inf'), 'B04', BEHAVIORS['B04'][0], 'Q2',
            'R-ACCOUNT-SWITCH-01', confidence=CONFIRMED,
            when_basis='서로 다른 계정 UUID 가 같은 기기 기록에 공존한다',
            supported_by=ev,
            basis={'rule': 'R-ACCOUNT-SWITCH-01', 'accounts': list(seen)},
            detail={'accounts': list(seen)},
            note='계정 %d개가 이 기기에서 쓰였다' % len(seen)))
    return out
