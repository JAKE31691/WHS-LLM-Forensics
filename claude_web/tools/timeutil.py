"""시각 값을 UTC datetime으로 바꾸고, 보고서용 KST 문자열로 만든다.

숫자는 크기로 형식을 구분한다 (2000년 ~ 2100년 범위만 시각으로 인정).
  WebKit(1601-01-01 기준 마이크로초)  약 1.26e16 ~ 1.58e16
  Unix 마이크로초                      약 9.46e14 ~ 4.10e15
  Unix 밀리초                          약 9.46e11 ~ 4.10e12
  Unix 초                              약 9.46e8  ~ 4.10e9
범위가 서로 겹치지 않아서 형식을 잘못 고를 일이 없다.

밀리초 표시: 원본에 초 미만 값이 있는 형식이면 .FFF를 붙인다(반올림하지 않고 자름).
원본이 초 단위면 초까지만 쓴다. 없는 정밀도를 만들지 않기 위해서다.
"""
import datetime
import re

UTC = datetime.timezone.utc
KST = datetime.timezone(datetime.timedelta(hours=9))
WEBKIT_EPOCH = datetime.datetime(1601, 1, 1, tzinfo=UTC)
UNIX_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=UTC)

MIN_DT = datetime.datetime(2000, 1, 1, tzinfo=UTC)
MAX_DT = datetime.datetime(2100, 1, 1, tzinfo=UTC)

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")
_DIGITS = re.compile(r"^\d+(\.\d+)?$")


def _in_range(dt):
    return MIN_DT <= dt < MAX_DT


def _from_number(v):
    """숫자 -> (UTC datetime, 초 미만 정밀도 여부) 또는 None"""
    if isinstance(v, bool):
        return None
    try:
        # 정수는 정수로 계산한다 (WebKit 값은 float로 바꾸면 끝자리가 틀어진다)
        x = int(v) if isinstance(v, int) or (isinstance(v, str) and "." not in v) else float(v)
    except (TypeError, ValueError):
        return None
    if x <= 0:
        return None
    # 초 단위 값은 소수 부분이 실제로 있을 때만 초 미만 정밀도가 있다고 본다
    sec_frac = isinstance(x, float) and x != int(x)
    # (마이크로초로 바꾸는 배수, 기준 시각, 초 미만 정밀도)
    candidates = [
        (1, WEBKIT_EPOCH, True),           # WebKit 마이크로초
        (1, UNIX_EPOCH, True),             # Unix 마이크로초
        (1000, UNIX_EPOCH, True),          # Unix 밀리초
        (1000000, UNIX_EPOCH, sec_frac),   # Unix 초
    ]
    for mult, epoch, frac in candidates:
        try:
            dt = epoch + datetime.timedelta(microseconds=x * mult)
        except (OverflowError, ValueError):
            continue
        if _in_range(dt):
            # WebKit 범위와 Unix 마이크로초 범위는 겹치지 않으므로 처음 맞는 것이 답이다
            return dt, frac
    return None


def _from_iso(s):
    if not _ISO.match(s):
        return None
    t = s.replace(" ", "T", 1)
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    # Python 3.10의 fromisoformat은 소수 자릿수가 3/6이 아니면 실패하므로 6자리로 맞춘다
    m = re.match(r"^(.*?\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", t)
    head, frac, tail = m.group(1), m.group(2), m.group(3)
    if re.match(r"^[+-]\d{4}$", tail):                      # +0900 -> +09:00 (Python 3.10용)
        tail = tail[:3] + ":" + tail[3:]
    has_ms = bool(frac) and len(frac) - 1 >= 3                # 원본이 밀리초 이상일 때만 .FFF
    if frac:
        frac = "." + (frac[1:] + "000000")[:6]
    try:
        dt = datetime.datetime.fromisoformat(head + (frac or "") + tail)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None          # 시간대가 없는 문자열은 UTC인지 알 수 없으므로 시각으로 쓰지 않는다
    dt = dt.astimezone(UTC)
    return (dt, has_ms) if _in_range(dt) else None


def parse_time(v):
    """JSON에 들어간 값 하나를 시각으로 해석한다. 시각이 아니면 None.

    반환: (UTC datetime, 초 미만 정밀도 여부)
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return _from_number(v)
    if isinstance(v, str):
        s = v.strip()
        if _DIGITS.match(s):
            return _from_number(s)
        return _from_iso(s)
    return None


def format_kst(dt, has_fraction):
    """UTC datetime -> 'YYYY-MM-DD HH:MM:SS[.FFF] KST'"""
    k = dt.astimezone(KST)
    s = k.strftime("%Y-%m-%d %H:%M:%S")
    if has_fraction:
        s += ".%03d" % (k.microsecond // 1000)
    return s + " KST"


def iso_utc(dt):
    """JSON에 넣을 UTC ISO 문자열 (datetime이 시간대 정보가 없으면 UTC로 본다)"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def iso_from_epoch(seconds):
    """파일 시스템 시각(Unix 초, float) -> UTC ISO 문자열"""
    if seconds is None:
        return None
    try:
        return iso_utc(datetime.datetime.fromtimestamp(float(seconds), UTC))
    except (OverflowError, OSError, ValueError):
        return None
