"""Optional labels for the case-specific team-share match export."""

from __future__ import annotations

from typing import Any


CSV_FILENAME = 'artifact_matrix.csv'
HTML_FILENAME = 'team_share.html'

ACTION_TITLES = {}
ARTIFACT_NAMES = {}


def action_id_for_rule(rule: dict[str, Any]) -> str:
    """Map a profile behavior ID to its team-facing action category."""
    behavior_id = str(rule.get('behavior_id') or '')
    return f'A{behavior_id[1:]}' if behavior_id.startswith('B') else ''
