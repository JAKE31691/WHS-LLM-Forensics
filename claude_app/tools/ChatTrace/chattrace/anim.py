# -*- coding: utf-8 -*-
"""오래 걸리는 단계 동안 보여 줄 CLI 애니메이션.

왜 필요한가 — $MFT 를 뽑고 38만 레코드를 훑는 동안 화면이 멈춘 것처럼
보인다. 로그를 한 줄씩 흘리면 진행 중인 건 알 수 있지만 화면이 지저분해지고
정작 결과가 위로 밀려 올라간다. 그래서 진행 중에는 애니메이션과 상태 한 줄만
보여 주고, 단계가 끝나면 요약을 한 번에 찍는다.

    ──────────────────────────────────────────
      ████████████                              ← 이 친구가 뛴다
      ██  ████  ██
    ████████████████
      ████████████
        ██    ██
    ────────────────
      $MFT 를 뽑는 중  ▓▓▓▓▓▓▓░░░  72%  12.4초
    ──────────────────────────────────────────

까다로운 곳
  * 로그와 그림이 같은 화면을 쓴다. 자물쇠로 묶어 '지우고 → 찍고 → 다시
    그리기' 순서를 강제하지 않으면 그림이 찢어진다.
  * 윈도우 기본 콘솔은 ANSI 가 꺼져 있다. 켜 보고, 안 되면 한 줄짜리로
    강등한다. 애니메이션 때문에 도구가 안 돌면 본말전도다.
  * 출력을 파일로 넘기면(isatty 가 아니면) 아예 끈다. 로그 파일에 커서
    제어 문자가 박히면 나중에 읽을 수가 없다.
"""
from __future__ import print_function

import os
import sys
import threading
import time

# ── 색 ──────────────────────────────────────────────────────────
ESC = '\x1b'
RESET = ESC + '[0m'
DIM = ESC + '[2m'
BOLD = ESC + '[1m'
HIDE = ESC + '[?25l'
SHOW = ESC + '[?25h'
CLEAR_LINE = ESC + '[2K'

ORANGE = ESC + '[38;2;217;119;87m'      # 트루컬러
ORANGE_256 = ESC + '[38;5;209m'         # 256색 대체
GREY = ESC + '[38;5;238m'
WHITE = ESC + '[38;5;250m'
BRIGHT = ESC + '[97m'           # 이름표용 순백


def _up(n):
    return ESC + '[%dA' % n if n > 0 else ''


# ── 그림 ────────────────────────────────────────────────────────
# 픽셀 한 칸 = 글자 두 칸. 글자는 세로로 길쭉해서 가로로 두 배 늘려야
# 정사각형처럼 보인다.
IDLE = [
    '.XXXXXX.',
    '.X.XX.X.',
    'XXXXXXXX',
    '.XXXXXX.',
    '..X..X..',
]

SQUASH = [                    # 웅크림 — 한 줄 낮고 양옆으로 퍼진다
    '.XXXXXX.',
    'XX.XX.XX',
    'XXXXXXXX',
    '.XXXXXX.',
]

STRETCH = [                   # 뻗음 — 한 줄 높고 좁아진다
    '..XXXX..',
    '.XXXXXX.',
    '.X.XX.X.',
    '.XXXXXX.',
    '..X..X..',
]

AIR = [                       # 공중 — 다리를 벌린다
    '.XXXXXX.',
    '.X.XX.X.',
    'XXXXXXXX',
    '.XXXXXX.',
    '.X....X.',
]

# (그림, 띄운 높이). 웅크렸다 뛰고 착지해서 다시 선다.
HOP = (
    [(IDLE, 0)] * 4
    + [(SQUASH, 0), (STRETCH, 0)]
    + [(AIR, 2), (AIR, 3), (AIR, 3), (AIR, 2)]
    + [(STRETCH, 0), (SQUASH, 0)]
    + [(IDLE, 0)] * 3
)

ART_H = 5
MAX_UP = 3
CANVAS_H = ART_H + MAX_UP          # 그림이 차지하는 줄 수

# 3x5 픽셀 글자. 이름표에만 쓴다.
GLYPH = {
    'A': ['.X.', 'X.X', 'XXX', 'X.X', 'X.X'],
    'B': ['XX.', 'X.X', 'XX.', 'X.X', 'XX.'],
    'C': ['XXX', 'X..', 'X..', 'X..', 'XXX'],
    'D': ['XX.', 'X.X', 'X.X', 'X.X', 'XX.'],
    'E': ['XXX', 'X..', 'XX.', 'X..', 'XXX'],
    'G': ['XXX', 'X..', 'X.X', 'X.X', 'XXX'],
    'H': ['X.X', 'X.X', 'XXX', 'X.X', 'X.X'],
    'I': ['XXX', '.X.', '.X.', '.X.', 'XXX'],
    'J': ['XXX', '..X', '..X', 'X.X', 'XXX'],
    # K 는 꼭짓점이 왼쪽 기둥으로 모여야 K 로 읽힌다. 가운데를 'XX.' 로
    # 두면 오른쪽에 구멍이 뚫린 H 처럼 보인다.
    'K': ['X.X', 'XX.', 'X..', 'XX.', 'X.X'],
    'L': ['X..', 'X..', 'X..', 'X..', 'XXX'],
    'M': ['X.X', 'XXX', 'XXX', 'X.X', 'X.X'],
    'N': ['X.X', 'XXX', 'XXX', 'XXX', 'X.X'],
    'O': ['XXX', 'X.X', 'X.X', 'X.X', 'XXX'],
    'P': ['XXX', 'X.X', 'XXX', 'X..', 'X..'],
    'R': ['XX.', 'X.X', 'XX.', 'X.X', 'X.X'],
    'S': ['XXX', 'X..', 'XXX', '..X', 'XXX'],
    'T': ['XXX', '.X.', '.X.', '.X.', '.X.'],
    'U': ['X.X', 'X.X', 'X.X', 'X.X', 'XXX'],
    'W': ['X.X', 'X.X', 'XXX', 'XXX', 'X.X'],
    'Y': ['X.X', 'X.X', 'XXX', '.X.', '.X.'],
    ' ': ['...', '...', '...', '...', '...'],
}
GLYPH_H = 5


def _tag_rows(text):
    """글자를 픽셀 행 목록으로. 모르는 글자는 건너뛴다."""
    gs = [GLYPH[c] for c in text.upper() if c in GLYPH]
    if not gs:
        return []
    rows = []
    for r in range(GLYPH_H):
        rows.append('.'.join(g[r] for g in gs))     # 글자 사이 한 칸
    return rows


def _tag_lines(text):
    """이름표를 '반칸' 문자로 눌러 담는다. 5픽셀 높이가 3줄이 된다.

    캐릭터는 픽셀 하나를 글자 두 칸으로 그리지만 이름표는 한 칸만 쓴다.
    그래서 같은 모양인데 세로 절반 · 가로 절반으로 작아진다.

        █ = 위아래 둘 다   ▀ = 위만   ▄ = 아래만   (공백) = 없음
    """
    rows = _tag_rows(text)
    if not rows:
        return []
    out = []
    for r in range(0, len(rows), 2):
        top = rows[r]
        bot = rows[r + 1] if r + 1 < len(rows) else '.' * len(top)
        line = []
        for c in range(len(top)):
            t = top[c] == 'X'
            b = bot[c] == 'X' if c < len(bot) else False
            line.append('█' if (t and b) else '▀' if t else '▄' if b else ' ')
        out.append(''.join(line))
    return out
SPIN = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
SPIN_ASCII = '|/-\\'


def _enable_vt():
    """윈도우 콘솔의 ANSI 처리를 켠다. -> 성공 여부

    윈도우 10 이상이면 켤 수 있는데 기본값이 꺼짐이다. Windows Terminal 은
    이미 켜져 있다. 실패해도 그냥 넘어간다 — 한 줄 모드로 간다.
    """
    if os.name != 'nt':
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        for handle in (-11, -12):          # stdout, stderr
            h = k.GetStdHandle(handle)
            mode = ctypes.c_ulong()
            if not k.GetConsoleMode(h, ctypes.byref(mode)):
                continue
            k.SetConsoleMode(h, mode.value | 0x0004)   # VT_PROCESSING
        return True
    except Exception:
        return False


class Animator(object):
    """단계 하나를 감싼다. 진행 중에는 그림, 끝나면 요약 한 줄."""

    def __init__(self, stream=None, enabled=None, fps=12, tag=''):
        self.stream = stream or sys.stderr
        self.fps = fps
        # 이름표. 실제 도구에서는 비워 둔다 — 화면을 더 먹는다.
        # 캐릭터 머리 위에 붙어서 같이 뛴다.
        self.tag_lines = _tag_lines(tag) if tag else []
        self.canvas_h = CANVAS_H + len(self.tag_lines)
        self._lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self._drawn = 0                 # 지금 화면에 그려 둔 줄 수
        self._frame = 0
        self._t0 = 0.0
        self._title = ''
        self._note = ''
        self._done = 0
        self._total = 0

        if enabled is None:
            enabled = self._detect()
        self.enabled = enabled
        self.rich = False
        self.color = ''
        if self.enabled:
            self.rich = _enable_vt()
            self.color = ORANGE if self._truecolor() else ORANGE_256

    # ── 환경 판정 ───────────────────────────────────────────
    def _detect(self):
        if os.environ.get('CHATTRACE_NO_ANIM'):
            return False
        if os.environ.get('NO_COLOR'):
            return False
        try:
            if not self.stream.isatty():
                return False               # 파일로 넘어가면 절대 안 쓴다
        except Exception:
            return False
        if os.environ.get('TERM') == 'dumb':
            return False
        return True

    @staticmethod
    def _truecolor():
        ct = os.environ.get('COLORTERM', '').lower()
        if 'truecolor' in ct or '24bit' in ct:
            return True
        return bool(os.environ.get('WT_SESSION'))    # Windows Terminal

    def _width(self):
        try:
            return max(40, min(120, os.get_terminal_size().columns))
        except Exception:
            return 80

    # ── 그리기 ──────────────────────────────────────────────
    def _render_art(self, frame_idx):
        """캔버스 높이만큼의 (줄, 흰색여부) 목록을 만든다.

        이름표는 캐릭터 머리 위에 붙어 함께 뛴다. 고정해 두면 뛸 때
        글자를 뚫고 지나가서 겹친다.
        """
        art, up = HOP[frame_idx % len(HOP)]
        tag = self.tag_lines
        pad_top = self.canvas_h - len(tag) - len(art) - up
        lines = [('', False)] * max(0, pad_top)

        # 이름표를 캐릭터 폭(글자 16칸) 한가운데로
        if tag:
            off = max(0, (16 - len(tag[0])) // 2)
            for t in tag:
                lines.append((' ' * off + t, True))
        for row in art:
            lines.append((''.join('██' if c == 'X' else '  ' for c in row), False))
        lines += [('', False)] * up

        # 그림자 — 높이 뜰수록 작아진다
        shade = {0: 'XXXXXXXX', 2: '.XXXXXX.', 3: '..XXXX..'}.get(up, 'XXXXXXXX')
        shadow = ''.join('▄▄' if c == 'X' else '  ' for c in shade)
        return lines[:self.canvas_h], shadow

    def _bar(self, width):
        """진행 막대. 총량을 모르면 아무것도 내지 않는다."""
        if not self._total:
            return ''
        pct = min(1.0, float(self._done) / self._total)
        n = max(10, min(28, width - 44))
        fill = int(round(pct * n))
        return '%s%s%s%s %3d%%' % ('▓' * fill, DIM, '░' * (n - fill), RESET,
                                   int(pct * 100))

    def _compose(self):
        w = self._width()
        pad = '  '
        lines, shadow = self._render_art(self._frame)
        out = []
        for l, white in lines:
            if not l.strip():
                out.append('')
            else:
                out.append(pad + (BRIGHT + BOLD if white else self.color) + l + RESET)
        out.append(pad + GREY + shadow + RESET)

        el = time.time() - self._t0
        spin = SPIN[self._frame % len(SPIN)] if self.rich else \
            SPIN_ASCII[self._frame % len(SPIN_ASCII)]
        status = '%s%s%s %s%s%s' % (self.color, spin, RESET, BOLD, self._title, RESET)
        bar = self._bar(w)
        if bar:
            status += '  ' + bar
        if self._note:
            status += '  ' + DIM + self._note + RESET
        status += '  ' + DIM + '%.1f초' % el + RESET
        out.append(pad + status)
        return out

    def _erase(self):
        if self._drawn:
            self.stream.write(_up(self._drawn))
            for _ in range(self._drawn):
                self.stream.write(CLEAR_LINE + '\n')
            self.stream.write(_up(self._drawn))
            self._drawn = 0

    def _paint(self):
        if not self.rich:
            # 한 줄 모드 — 캐리지리턴만 쓴다. 어디서나 된다.
            el = time.time() - self._t0
            spin = SPIN_ASCII[self._frame % len(SPIN_ASCII)]
            pct = ''
            if self._total:
                pct = ' %3d%%' % int(100.0 * self._done / self._total)
            msg = '  %s %s%s  %.1f초' % (spin, self._title, pct, el)
            self.stream.write('\r' + msg[:self._width() - 1] + ' ' * 6)
            self.stream.flush()
            return
        self._erase()
        lines = self._compose()
        for l in lines:
            self.stream.write(CLEAR_LINE + l + '\n')
        # 이름표 유무에 따라 줄 수가 달라진다. 고정값을 쓰면 지울 때
        # 어긋나서 잔상이 남는다.
        self._drawn = len(lines)
        self.stream.flush()

    # ── 수명 ────────────────────────────────────────────────
    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                if self._stop.is_set():
                    break
                self._frame += 1
                try:
                    self._paint()
                except Exception:
                    return                   # 그림 때문에 도구가 죽으면 안 된다
            self._stop.wait(1.0 / self.fps)

    def start(self, title, total=0):
        if not self.enabled:
            return self
        with self._lock:
            self._title = title
            self._total = total
            self._done = 0
            self._note = ''
            self._frame = 0
            self._t0 = time.time()
            self._stop.clear()
            if self.rich:
                self.stream.write(HIDE)
            self._thread = threading.Thread(target=self._loop)
            self._thread.daemon = True
            self._thread.start()
        return self

    def set_tag(self, text):
        """이름표를 바꾼다. 빈 문자열이면 뗀다."""
        with self._lock:
            self.tag_lines = _tag_lines(text) if text else []
            self.canvas_h = CANVAS_H + len(self.tag_lines)

    def update(self, done=None, total=None, note=None):
        if not self.enabled:
            return
        with self._lock:
            if done is not None:
                self._done = done
            if total is not None:
                self._total = total
            if note is not None:
                self._note = note

    def log(self, text):
        """애니메이션을 지우고 한 줄 찍은 뒤 다시 그린다."""
        if not self.enabled or self._thread is None:
            print(text, file=self.stream)
            return
        with self._lock:
            if self.rich:
                self._erase()
            else:
                self.stream.write('\r' + ' ' * (self._width() - 1) + '\r')
            self.stream.write(text + '\n')
            self.stream.flush()

    def stop(self, summary=None):
        if not self.enabled:
            if summary:
                print(summary, file=self.stream)
            return
        with self._lock:
            self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=1.0)
        with self._lock:
            self._thread = None
            if self.rich:
                self._erase()
                self.stream.write(SHOW)
            else:
                self.stream.write('\r' + ' ' * (self._width() - 1) + '\r')
            if summary:
                self.stream.write(summary + '\n')
            self.stream.flush()

    # ── with 구문 ───────────────────────────────────────────
    def stage(self, title, total=0):
        return _Stage(self, title, total)


class _Stage(object):
    def __init__(self, anim, title, total):
        self.a = anim
        self.title = title
        self.total = total
        self.summary = None
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.time()
        self.a.start(self.title, self.total)
        return self

    def __exit__(self, et, ev, tb):
        # 예외로 빠져나가도 커서는 반드시 되돌린다.
        self.a.stop(None if et else self.summary)
        return False

    def update(self, done=None, total=None, note=None):
        self.a.update(done, total, note)

    def finish(self, summary):
        self.summary = summary

    @property
    def elapsed(self):
        return time.time() - self.t0


# 도구 전체에서 하나만 쓴다. 이름표는 반칸 문자로 눌러 담아 세 줄만
# 쓰므로 진행 화면이 크게 길어지지 않는다. --tag 로 바꾸거나 끌 수 있다.
UI = Animator(tag='CK')


def _demo(tag='CK'):
    """ChatTrace --demo-anim — 실제 터미널에서 눈으로 확인한다.

    이름표는 데모에만 붙인다. 실제 도구에 넣으면 진행 화면이 다섯 줄
    더 길어져서, 오래 걸리는 단계마다 화면을 많이 먹는다.
    """
    a = Animator(enabled=True, tag=tag)
    print()
    print('  ChatTrace 애니메이션 시험')
    print('  모드: %s%s' % ('여러 줄 (ANSI)' if a.rich else '한 줄 (강등)',
                          ('  ·  이름표 %s' % tag) if a.tag_lines else ''))
    print()
    with a.stage('$MFT 를 뽑는 중', total=382208) as st:
        for i in range(0, 382209, 9000):
            st.update(done=i, note='레코드 %s' % format(i, ','))
            time.sleep(0.05)
        st.finish('  $MFT 391,380,992 B · %.1f초' % st.elapsed)
    with a.stage('경로를 복원하는 중') as st:
        for i in range(60):
            if i == 30:
                a.log('  ' + DIM + '디렉터리 138,080개 수집' + RESET)
            st.update(note='디렉터리 %s개' % format(i * 2300, ','))
            time.sleep(0.05)
        st.finish('  대상 2,374개 · %.1f초' % st.elapsed)
    print()
    print('  끝. 커서가 돌아왔으면 정상이다.')
    print()


if __name__ == '__main__':
    _demo()
