"""feat-054 Phase 2 — context.reference_lookup builtin.

Covers hard-scope behaviour: an event_id cited in an active
compaction's refs is fetchable; anything else raises
`ToolExecutionFailed`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from errors import ToolExecutionFailed
from schemas import Actor, CallerChain, Event, EventType
from tools.context_reference import ReferenceLookupArgs, make_reference_lookup

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_TID = f"th_{uuid4()}"
_SID = f"ses_{uuid4()}"
_AGENT_ID = f"ag_{uuid4()}"


def _mk(type_: EventType, data: dict[str, Any]) -> Event:
    return Event(
        event_id=f"ev_{uuid4()}",
        parent_event_id=None,
        thread_id=_TID,
        session_id=_SID,
        type=type_,
        data=data,
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


class _FakeStore:
    def __init__(self, events: list[Event]) -> None:
        self._events = events

    async def read_path_to_leaf(
        self,
        thread_id: Any,
    ) -> AsyncIterator[Event]:
        for e in self._events:
            yield e


def _ctx() -> Any:
    from tools.decorator import ToolContext

    return ToolContext(
        agent_id=_AGENT_ID,
        session_id=_SID,
        thread_id=_TID,
        principal="user.local",
        secrets={},
        caller_chain=CallerChain.unattributed(),
    )


@pytest.mark.asyncio
async def test_reference_lookup_returns_scoped_event() -> None:
    target = _mk(EventType.USER_MESSAGE, {"content": {"text": "original"}})
    compaction = _mk(
        EventType.CONTEXT_COMPACTED,
        {
            "compacted_event_ids": [str(target.event_id)],
            "summary": {
                "summary_text": "s",
                "refs": [{"event_id": str(target.event_id), "note": "n"}],
                "kept_files": [],
                "span": {},
            },
            "tokens_before": 10,
            "tokens_after": 5,
            "pre_compaction": {},
        },
    )
    tool = make_reference_lookup(session_store=_FakeStore([target, compaction]))
    result = await tool(ReferenceLookupArgs(event_id=str(target.event_id)), _ctx())
    assert result["event_id"] == str(target.event_id)
    assert result["type"] == EventType.USER_MESSAGE.value


@pytest.mark.asyncio
async def test_reference_lookup_refuses_out_of_scope_event() -> None:
    other = _mk(EventType.USER_MESSAGE, {"content": {"text": "other"}})
    scoped = _mk(EventType.AGENT_MESSAGE, {"content": {"text": "scoped"}})
    compaction = _mk(
        EventType.CONTEXT_COMPACTED,
        {
            "compacted_event_ids": [str(scoped.event_id)],
            "summary": {
                "summary_text": "s",
                "refs": [{"event_id": str(scoped.event_id), "note": "n"}],
                "kept_files": [],
                "span": {},
            },
            "tokens_before": 10,
            "tokens_after": 5,
            "pre_compaction": {},
        },
    )
    tool = make_reference_lookup(session_store=_FakeStore([other, scoped, compaction]))
    with pytest.raises(ToolExecutionFailed):
        await tool(ReferenceLookupArgs(event_id=str(other.event_id)), _ctx())


@pytest.mark.asyncio
async def test_reference_lookup_refuses_revoked_scope() -> None:
    target = _mk(EventType.USER_MESSAGE, {"content": {"text": "a"}})
    compaction = _mk(
        EventType.CONTEXT_COMPACTED,
        {
            "compacted_event_ids": [str(target.event_id)],
            "summary": {
                "summary_text": "s",
                "refs": [{"event_id": str(target.event_id), "note": "n"}],
                "kept_files": [],
                "span": {},
            },
            "tokens_before": 10,
            "tokens_after": 5,
            "pre_compaction": {},
        },
    )
    revoke = _mk(
        EventType.CONTEXT_COMPACTION_REVOKED,
        {"compaction_event_id": str(compaction.event_id), "reason": "user_requested"},
    )
    tool = make_reference_lookup(session_store=_FakeStore([target, compaction, revoke]))
    with pytest.raises(ToolExecutionFailed):
        await tool(ReferenceLookupArgs(event_id=str(target.event_id)), _ctx())
