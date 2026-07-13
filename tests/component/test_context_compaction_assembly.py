"""feat-054 Phase 2 — Assembly projection rule for CONTEXT_COMPACTED.

Covers:

- ``ASSEMBLY_VERSION`` bump to ``"2"``.
- Old-format log (no compaction events) projects byte-identically.
- Active compaction: middle span replaced with one system message.
- ``respect_compaction=False``: compaction events treated as no-op.
- Preview compaction: no-op (projected verbatim).
- Revoked compaction (via ``revoked_by`` OR
  ``context.compaction_revoked``): no-op.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from engine.assembly import ASSEMBLY_VERSION, assemble_context
from schemas import Actor, Event, EventType


_TID = f"th_{uuid4()}"
_SID = f"ses_{uuid4()}"


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


def test_assembly_version_is_2() -> None:
    assert ASSEMBLY_VERSION == "2"


def test_old_log_projects_identically_under_both_flags() -> None:
    events = [
        _mk(EventType.USER_MESSAGE, {"content": {"text": "hi"}}),
        _mk(EventType.AGENT_MESSAGE, {"content": {"text": "hello"}}),
        _mk(EventType.USER_MESSAGE, {"content": {"text": "again"}}),
    ]
    with_flag = assemble_context(events, respect_compaction=True)
    without_flag = assemble_context(events, respect_compaction=False)
    assert with_flag.messages == without_flag.messages


def test_active_compaction_replaces_span_with_system_message() -> None:
    e1 = _mk(EventType.USER_MESSAGE, {"content": {"text": "old"}})
    e2 = _mk(EventType.AGENT_MESSAGE, {"content": {"text": "old reply"}})
    e3 = _mk(
        EventType.CONTEXT_COMPACTED,
        {
            "compacted_event_ids": [str(e1.event_id), str(e2.event_id)],
            "summary": {
                "schema_version": 1,
                "summary_text": "prior turn was routine",
                "refs": [],
                "kept_files": [],
                "span": {
                    "first_event_id": str(e1.event_id),
                    "last_event_id": str(e2.event_id),
                    "event_count": 2,
                },
            },
            "tokens_before": 100,
            "tokens_after": 20,
            "pre_compaction": {},
        },
    )
    e4 = _mk(EventType.USER_MESSAGE, {"content": {"text": "new"}})
    projected = assemble_context([e1, e2, e3, e4]).messages
    assert len(projected) == 2
    assert projected[0].role == "system"
    assert "prior turn was routine" in projected[0].content
    assert projected[1].role == "user"


def test_respect_compaction_false_projects_span_verbatim() -> None:
    e1 = _mk(EventType.USER_MESSAGE, {"content": {"text": "old"}})
    e2 = _mk(EventType.AGENT_MESSAGE, {"content": {"text": "old reply"}})
    e3 = _mk(
        EventType.CONTEXT_COMPACTED,
        {
            "compacted_event_ids": [str(e1.event_id), str(e2.event_id)],
            "summary": {
                "summary_text": "should not appear",
                "refs": [],
                "kept_files": [],
                "span": {},
            },
            "tokens_before": 100,
            "tokens_after": 20,
            "pre_compaction": {},
        },
    )
    e4 = _mk(EventType.USER_MESSAGE, {"content": {"text": "new"}})
    projected = assemble_context([e1, e2, e3, e4], respect_compaction=False).messages
    assert len(projected) == 3
    assert all("should not appear" not in m.content for m in projected)


def test_preview_compaction_is_noop() -> None:
    e1 = _mk(EventType.USER_MESSAGE, {"content": {"text": "keep"}})
    e2 = _mk(EventType.AGENT_MESSAGE, {"content": {"text": "keep reply"}})
    e3 = _mk(
        EventType.CONTEXT_COMPACTED,
        {
            "compacted_event_ids": [str(e1.event_id), str(e2.event_id)],
            "summary": {
                "summary_text": "preview-only",
                "refs": [],
                "kept_files": [],
                "span": {},
            },
            "tokens_before": 100,
            "tokens_after": 20,
            "pre_compaction": {},
            "preview": True,
        },
    )
    projected = assemble_context([e1, e2, e3]).messages
    assert len(projected) == 2


def test_revoked_by_field_makes_compaction_noop() -> None:
    e1 = _mk(EventType.USER_MESSAGE, {"content": {"text": "keep"}})
    e2 = _mk(EventType.AGENT_MESSAGE, {"content": {"text": "keep reply"}})
    e3 = _mk(
        EventType.CONTEXT_COMPACTED,
        {
            "compacted_event_ids": [str(e1.event_id), str(e2.event_id)],
            "summary": {
                "summary_text": "revoked",
                "refs": [],
                "kept_files": [],
                "span": {},
            },
            "tokens_before": 100,
            "tokens_after": 20,
            "pre_compaction": {},
            "revoked_by": f"ev_{uuid4()}",
        },
    )
    projected = assemble_context([e1, e2, e3]).messages
    assert len(projected) == 2


def test_compaction_revoked_event_makes_compaction_noop() -> None:
    e1 = _mk(EventType.USER_MESSAGE, {"content": {"text": "keep"}})
    e2 = _mk(EventType.AGENT_MESSAGE, {"content": {"text": "keep reply"}})
    e3 = _mk(
        EventType.CONTEXT_COMPACTED,
        {
            "compacted_event_ids": [str(e1.event_id), str(e2.event_id)],
            "summary": {
                "summary_text": "revoked",
                "refs": [],
                "kept_files": [],
                "span": {},
            },
            "tokens_before": 100,
            "tokens_after": 20,
            "pre_compaction": {},
        },
    )
    e4 = _mk(
        EventType.CONTEXT_COMPACTION_REVOKED,
        {"compaction_event_id": str(e3.event_id), "reason": "user_requested"},
    )
    projected = assemble_context([e1, e2, e3, e4]).messages
    assert len(projected) == 2
