"""spec-005 Test 5 — `UnknownCardKind` message quality.

The v1 kind registry is closed — tool authors can't add kinds until
v1.1. When a caller reaches for a kind that isn't in the set,
`get_kind_spec` needs to hand back an actionable error message so
the author knows both what's available AND how to request a new
kind.
"""

from __future__ import annotations

import pytest

from tools.services import UnknownCardKind, get_kind_spec


def test_message_includes_known_kinds_list() -> None:
    """AC 7 — message includes all four known kinds (sorted)."""
    with pytest.raises(UnknownCardKind) as exc_info:
        get_kind_spec("table")
    msg = str(exc_info.value)
    for kind in ("markdown", "progress", "confirm", "workflow"):
        assert kind in msg


def test_message_includes_issue_template_pointer() -> None:
    """AC 7 — message includes the issue-template URL fragment."""
    with pytest.raises(UnknownCardKind) as exc_info:
        get_kind_spec("timeline")
    msg = str(exc_info.value)
    assert "issues/new" in msg
