from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from collections import defaultdict
from typing import Callable, Iterable, Optional

from .schema import Observation


@dataclass(frozen=True)
class EvidenceWindow:
    """A candidate evidence range, not a claimed action duration."""

    anchor: Observation
    next_anchor: Optional[Observation] = None

    @property
    def when_from(self) -> str:
        return observation_time(self.anchor)

    @property
    def when_to(self) -> str:
        return observation_time(self.next_anchor)


def observation_time(obs: Optional[Observation]) -> str:
    if obs is None:
        return ''
    data = obs.as_dict()
    preferred = (
        'ts_event', 'ts_visit', 'ts_record', 'ts_created', 'ts_modified',
        'ts_mft_si_created', 'ts_mft_si_modified', 'ts_last_run',
    )
    for key in preferred:
        if data.get(key):
            return str(data[key])
    for key in sorted(data):
        if key.startswith('ts_') and data.get(key):
            return str(data[key])
    return ''


def order_key(obs: Observation):
    # Parse ISO-8601 values before ordering. Lexical comparison is incorrect
    # when equivalent formats use different fractional-second precision, such
    # as ``...18Z`` and ``...18.100Z``.
    raw_time = observation_time(obs)
    if raw_time:
        try:
            cooked = raw_time.strip()
            if cooked.endswith('Z'):
                cooked = cooked[:-1] + '+00:00'
            parsed = datetime.fromisoformat(cooked)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            time_key = (0, parsed.astimezone(timezone.utc))
        except (TypeError, ValueError, OverflowError):
            time_key = (1, raw_time)
    else:
        time_key = (2, '')
    return (
        *time_key,
        obs.seq if obs.seq is not None else 10**18,
        obs.id,
    )


def build_history_windows(
    observations: Iterable[Observation],
    selector: Callable[[Observation], bool],
    scope: Optional[Callable[[Observation], str]] = None,
) -> list[EvidenceWindow]:
    groups = defaultdict(list)
    for obs in observations:
        if obs.source_artifact == 'history' and selector(obs):
            groups[scope(obs) if scope else ''].append(obs)
    windows = []
    for anchors in groups.values():
        anchors.sort(key=order_key)
        windows.extend(
            EvidenceWindow(anchor, anchors[i + 1] if i + 1 < len(anchors) else None)
            for i, anchor in enumerate(anchors)
        )
    return windows


def contains(window: EvidenceWindow, obs: Observation) -> bool:
    key = order_key(obs)
    if key < order_key(window.anchor):
        return False
    if window.next_anchor is not None and key >= order_key(window.next_anchor):
        return False
    return True


def span_seconds(when_from: str, when_to: str) -> Optional[float]:
    if not when_from or not when_to:
        return None

    def parse(value: str) -> datetime:
        cooked = value.strip()
        if cooked.endswith('Z'):
            cooked = cooked[:-1] + '+00:00'
        dt = datetime.fromisoformat(cooked)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    try:
        value = (parse(when_to) - parse(when_from)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None
    return round(value, 6) if value >= 0 else None
