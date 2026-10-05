# -*- coding: utf-8 -*-
r"""레지스트리 파서 — "어느 윈도우 계정이 실행했나".

Claude 계정 UUID 는 '누가 로그인했나' 를 말하지만 '어느 윈도우 사용자가
실행했나' 는 말하지 않는다. 한 PC 를 여러 사람이 쓰면 그 둘이 갈린다.
섀도우 AI 사건에서는 후자가 귀속의 핵심이다.

우리 분석 6위 항목을 그대로 구현한다.

    BAM        SYSTEM\ControlSet00N\Services\bam\State\UserSettings\<SID>
               사용자 SID 귀속 최종 실행.
               ★ MSIX 는 경로가 아니라 패키지 패밀리명으로 기록된다
                 (Claude_pzs8sxrjxfjjc). 경로로만 찾으면 못 찾는다.
    UserAssist NTUSER.DAT\…\Explorer\UserAssist\{GUID}\Count
               GUI 로 눌러서 실행한 것만 기록된다 — '사용자가 직접 실행'
               의 증거다. 이름이 ROT13 으로 꼬여 있다.
               ★ 실행 횟수는 과소 계상된다(우리 분석 함정 항목).

시각은 전부 UTC 로 낸다. 원본 regf 파서는 KST(+9)를 더하고 있었는데
그대로 쓰면 다른 UTC 증거와 9시간 어긋난다.
"""
import codecs
import datetime
import os
import struct

from .schema import Observation

_EPOCH = datetime.datetime(1601, 1, 1)


class Hive(object):
    """regf 하이브 최소 리더. 우리 분석의 regf.py 를 정리해 가져왔다."""

    def __init__(self, path):
        with open(path, 'rb') as f:
            self.d = f.read()
        if self.d[:4] != b'regf':
            raise ValueError('regf 하이브가 아니다')
        self.base = 0x1000
        self.root = struct.unpack_from('<I', self.d, 0x24)[0]

    def cell(self, off):
        p = self.base + off
        size = struct.unpack_from('<i', self.d, p)[0]
        return self.d[p + 4:p + abs(size)]

    def nk(self, off):
        c = self.cell(off)
        if c[:2] != b'nk':
            raise ValueError('nk 가 아니다')
        subcnt = struct.unpack_from('<I', c, 0x14)[0]
        sublist = struct.unpack_from('<I', c, 0x1C)[0]
        valcnt = struct.unpack_from('<I', c, 0x24)[0]
        vallist = struct.unpack_from('<I', c, 0x28)[0]
        ts = struct.unpack_from('<Q', c, 0x04)[0]
        nlen = struct.unpack_from('<H', c, 0x48)[0]
        flags = struct.unpack_from('<H', c, 0x02)[0]
        raw = c[0x4C:0x4C + nlen]
        name = (raw.decode('ascii', 'replace') if flags & 0x20
                else raw.decode('utf-16-le', 'replace'))
        return {'name': name, 'subcnt': subcnt, 'sublist': sublist,
                'valcnt': valcnt, 'vallist': vallist, 'ts': ts}

    def subkeys(self, off):
        n = self.nk(off)
        if n['subcnt'] == 0 or n['sublist'] == 0xFFFFFFFF:
            return {}
        return self._list(n['sublist'])

    def _list(self, off):
        c = self.cell(off)
        sig = c[:2]
        out = {}
        cnt = struct.unpack_from('<H', c, 2)[0]
        try:
            if sig in (b'lf', b'lh'):
                for i in range(cnt):
                    o = struct.unpack_from('<I', c, 4 + i * 8)[0]
                    out[self.nk(o)['name'].lower()] = o
            elif sig == b'li':
                for i in range(cnt):
                    o = struct.unpack_from('<I', c, 4 + i * 4)[0]
                    out[self.nk(o)['name'].lower()] = o
            elif sig == b'ri':
                for i in range(cnt):
                    o = struct.unpack_from('<I', c, 4 + i * 4)[0]
                    out.update(self._list(o))
        except (struct.error, ValueError):
            pass
        return out

    def find(self, parts):
        off = self.root
        for part in [p for p in parts if p]:
            subs = self.subkeys(off)
            k = part.lower()
            if k not in subs:
                return None
            off = subs[k]
        return off

    def values(self, off):
        n = self.nk(off)
        out = []
        if n['valcnt'] == 0 or n['vallist'] == 0xFFFFFFFF:
            return out
        lst = self.cell(n['vallist'])
        for i in range(n['valcnt']):
            try:
                vo = struct.unpack_from('<I', lst, i * 4)[0]
                v = self.cell(vo)
            except (struct.error, IndexError):
                continue
            if v[:2] != b'vk':
                continue
            nlen = struct.unpack_from('<H', v, 0x02)[0]
            dsize = struct.unpack_from('<I', v, 0x04)[0]
            doff = struct.unpack_from('<I', v, 0x08)[0]
            dtype = struct.unpack_from('<I', v, 0x0C)[0]
            flags = struct.unpack_from('<H', v, 0x10)[0]
            raw = v[0x14:0x14 + nlen]
            vname = (raw.decode('ascii', 'replace') if flags & 1
                     else raw.decode('utf-16-le', 'replace'))
            if dsize & 0x80000000:
                data = struct.pack('<I', doff)[:dsize & 0x7FFFFFFF]
            else:
                data = self.cell(doff)[:dsize]
            out.append((vname, dtype, data))
        return out


def filetime(b, off=0):
    """FILETIME -> ISO8601 UTC. ★ 타임존을 더하지 않는다."""
    if len(b) < off + 8:
        return ''
    v = struct.unpack_from('<Q', b, off)[0]
    if not v:
        return ''
    try:
        dt = _EPOCH + datetime.timedelta(microseconds=v // 10)
    except (OverflowError, OSError):
        return ''
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % ((v // 10000) % 1000)


CLAUDE_HINTS = ('claude', 'anthropic', 'pzs8sxrjxfjjc')


def _is_claude(s):
    low = (s or '').lower()
    return any(h in low for h in CLAUDE_HINTS)


def _find_file(root, name):
    out = []
    for dp, dn, fns in os.walk(root):
        for f in fns:
            if f.lower() == name.lower():
                out.append(os.path.join(dp, f))
    return out


# ── BAM ─────────────────────────────────────────────────────────
def _bam(root, ids, say, env):
    obs = []
    for p in _find_file(root, 'SYSTEM'):
        try:
            h = Hive(p)
        except (OSError, ValueError):
            continue
        rel = os.path.relpath(p, root)
        found = 0
        for cs in ('controlset001', 'controlset002', 'currentcontrolset'):
            base = h.find([cs, 'services', 'bam', 'state', 'usersettings'])
            if base is None:
                continue
            for sid, off in sorted(h.subkeys(base).items()):
                for vname, _dtype, data in h.values(off):
                    if not _is_claude(vname):
                        continue
                    found += 1
                    obs.append(Observation(
                        ids.next('obs'), 'bam', rel,
                        '%s\\%s' % (cs, sid), 'bam_last_run',
                        {'sid': sid, 'target': vname,
                         'is_package': '\\' not in vname,
                         'controlset': cs},
                        ts={'ts_bam_last_run': filetime(data)},
                        actor=sid, env_profile=env,
                        note='MSIX 는 경로가 아니라 패키지 패밀리명으로 기록된다'))
            if found:
                break
        if found:
            say('   BAM %d건 (SYSTEM)' % found)
    return obs


# ── UserAssist ──────────────────────────────────────────────────
def _userassist(root, ids, say, env):
    obs = []
    total = 0
    for p in _find_file(root, 'NTUSER.DAT'):
        try:
            h = Hive(p)
        except (OSError, ValueError):
            continue
        rel = os.path.relpath(p, root)
        base = h.find(['software', 'microsoft', 'windows', 'currentversion',
                       'explorer', 'userassist'])
        if base is None:
            # 없는 것도 결과다. 조용히 0이 되면 '직접 실행 안 했다' 로
            # 잘못 읽힌다.
            obs.append(Observation(
                ids.next('obs'), 'userassist', rel, 'UserAssist',
                'userassist_absent', {'hive': rel},
                status='partial',
                note='UserAssist 키가 이 하이브에 없다. 트랜잭션 로그 미반영일 수 있다',
                env_profile=env))
            continue
        # 사용자 이름은 경로에서 얻는다 (…\Users\<이름>\NTUSER.DAT)
        parts = rel.replace('/', '\\').split('\\')
        winuser = ''
        for k, seg in enumerate(parts):
            if seg.lower() == 'users' and k + 1 < len(parts):
                winuser = parts[k + 1]
                break
        for guid, goff in sorted(h.subkeys(base).items()):
            coff = h.subkeys(goff).get('count')
            if coff is None:
                continue
            for vname, _dtype, data in h.values(coff):
                try:
                    name = codecs.decode(vname, 'rot_13')
                except Exception:
                    name = vname
                if not _is_claude(name):
                    continue
                total += 1
                # Win7+ Count 값: 0x04 실행횟수, 0x3C 포커스시간(ms),
                #                 0x3C+0x1C 마지막 실행 FILETIME(0x3C)
                runs = focus = 0
                last = ''
                if len(data) >= 72:
                    runs = struct.unpack_from('<I', data, 4)[0]
                    focus = struct.unpack_from('<I', data, 12)[0]
                    last = filetime(data, 60)
                obs.append(Observation(
                    ids.next('obs'), 'userassist', rel, guid,
                    'userassist_run',
                    {'target': name, 'raw_name': vname,
                     'run_count': runs, 'focus_ms': focus,
                     'windows_user': winuser},
                    ts={'ts_ua_last_run': last},
                    actor=winuser, env_profile=env,
                    note='GUI 로 실행한 것만 기록된다. 횟수는 과소 계상된다'))
    if total:
        say('   UserAssist %d건' % total)
    return obs


def parse(root, ids, log=None, env_profile=''):
    say = log or (lambda *a: None)
    obs = []
    obs += _bam(root, ids, say, env_profile)
    obs += _userassist(root, ids, say, env_profile)
    return obs
