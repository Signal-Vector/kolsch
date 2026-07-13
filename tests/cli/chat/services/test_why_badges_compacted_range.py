"""feat-054 Phase 3 — ``/why`` badging for calls inside compacted ranges.

AC #11 — when the referenced ``tool.failed`` (or its correlated
``tool.called``) sits inside an active ``CONTEXT_COMPACTED``
``compacted_event_ids`` list, the deny card renderer appends a
badge naming the compaction event id so the operator knows the live
model may not remember the call.

Uses the same ``_MockEvent`` + ``_MockStoreDirect`` scaffolding as
``tests/cli/chat/services/test_feat041_authz_introspection.py`` — the
badge logic is thread-scanning + payload-reading, no runtime needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cli.chat.services.authz_introspection import (
    find_last_denial_via_store,
    render_denial_card,
)


@dataclass
class _MockEvent:
    """Duck-typed stand-in for ``schemas.Event`` — carries just the
    fields the badge logic reads: ``type``, ``data``, ``event_id``."""

    type: str
    data: dict[str, Any]
    event_id: str = ""


class _MockStoreDirect:
    def __init__(self, events: list[_MockEvent]) -> None:
        self._events = events

    async def read_path_to_leaf(self, thread_id: object) -> Any:
        _ = thread_id
        for event in self._events:
            yield event


@dataclass
class _MockRuntime:
    store: _MockStoreDirect
    root: Path = field(default_factory=Path)


_MATCHED_RULE = {
    "id": "deny-bash-rm",
    "effect": "deny",
    "source_file": "/tmp/platform.yaml",
    "source_line": 42,
    "what": "kolsch/tools/bash",
    "who": "*",
    "pattern": "rm -rf *",
    "reason": "layer=platform (agent-user): matched deny rule",
}


def _policy_event() -> _MockEvent:
    return _MockEvent(
        type="policy.decision.made",
        data={
            "tool_call_id": "tc_1",
            "action": "kolsch/tools/bash",
            "effect": "deny",
        },
        event_id="ev_policy_00000000000000000000000000",
    )


def _tool_called() -> _MockEvent:
    return _MockEvent(
        type="tool.called",
        data={"tool_call_id": "tc_1", "tool_name": "kolsch/tools/bash"},
        event_id="ev_toolcalled_1234567890abcdef00000000",
    )


def _tool_failed() -> _MockEvent:
    return _MockEvent(
        type="tool.failed",
        data={
            "tool_call_id": "tc_1",
            "tool_name": "kolsch/tools/bash",
            "error": {
                "type": "AgentUnauthorized",
                "message": "denied",
                "detail": {
                    "matched_rule": _MATCHED_RULE,
                    "action": "kolsch/tools/bash",
                },
            },
        },
        event_id="ev_toolfailed_00000000000000000000000000",
    )


def _compaction_event(
    *,
    event_id: str,
    covers: list[str],
) -> _MockEvent:
    return _MockEvent(
        type="context.compacted",
        data={
            "compacted_event_ids": covers,
            "summary": {"schema_version": 1},
            "pre_compaction": {},
            "tokens_before": 100,
            "tokens_after": 40,
            "trigger": "auto",
            "preview": False,
        },
        event_id=event_id,
    )


def _revocation_event(target: str) -> _MockEvent:
    return _MockEvent(
        type="context.compaction_revoked",
        data={"compaction_event_id": target, "reason": "user_requested"},
        event_id="ev_revoc_00000000000000000000000000",
    )


@pytest.mark.asyncio
async def test_why_badge_absent_when_no_active_compaction(
    tmp_path: Path,
) -> None:
    """No compaction on the thread → no badge on the card."""
    runtime = _MockRuntime(
        store=_MockStoreDirect(
            [_policy_event(), _tool_called(), _tool_failed()],
        ),
        root=tmp_path,
    )
    data = await find_last_denial_via_store(
        runtime, "th_ignored",
    )
    assert data is not None
    assert data.compaction_badge is None
    assert "compacted range" not in render_denial_card(data)


@pytest.mark.asyncio
async def test_why_badge_when_call_inside_compacted_range(
    tmp_path: Path,
) -> None:
    """Failed tool call sits inside an active compaction → badge set."""
    tc = _tool_called()
    tf = _tool_failed()
    compaction_id = "ev_compabc_00000000000000000000000000"
    compaction = _compaction_event(
        event_id=compaction_id,
        covers=[tc.event_id, tf.event_id],
    )
    runtime = _MockRuntime(
        store=_MockStoreDirect([_policy_event(), tc, tf, compaction]),
        root=tmp_path,
    )
    data = await find_last_denial_via_store(
        runtime, "th_ignored",
    )
    assert data is not None
    assert data.compaction_badge is not None
    assert compaction_id in data.compaction_badge
    rendered = render_denial_card(data)
    assert compaction_id in rendered
    assert "LIVE model may not remember" in rendered


@pytest.mark.asyncio
async def test_why_badge_skipped_when_compaction_revoked(
    tmp_path: Path,
) -> None:
    """A revoked compaction is inactive — no badge even if the call is inside its span."""
    tc = _tool_called()
    tf = _tool_failed()
    compaction_id = "ev_compdef_00000000000000000000000000"
    compaction = _compaction_event(
        event_id=compaction_id,
        covers=[tc.event_id, tf.event_id],
    )
    revocation = _revocation_event(compaction_id)
    runtime = _MockRuntime(
        store=_MockStoreDirect(
            [_policy_event(), tc, tf, compaction, revocation],
        ),
        root=tmp_path,
    )
    data = await find_last_denial_via_store(
        runtime, "th_ignored",
    )
    assert data is not None
    assert data.compaction_badge is None
