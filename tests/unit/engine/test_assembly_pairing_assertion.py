"""bug-064 — post-projection pairing backstop tests.

Exercises the defensive check added at the end of
``engine.assembly._project_events``: every assistant ``tool_calls``
id must be followed by a matching ``role="tool"`` message, and every
``role="tool"`` message must have a preceding ``tool_calls`` id.

The check has two modes:

* **Test mode** (this module): raise ``AssertionError`` on violation.
* **Production**: log once, drop the offending in-memory message(s).

These tests deliberately seed malformed event lists directly into
the projector to exercise both directions — they do NOT run a full
exchange, so spec-068's engine-side reconciliation is NOT in the
picture. That isolation is intentional per the coder brief.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from engine.assembly import _project_events, assemble_context
from schemas import Actor, Event, EventId, EventType, ThreadId

_TID = ThreadId(f"th_{uuid4()}")
_SID = f"ses_{uuid4()}"


def _mk(type_: EventType, data: dict[str, Any]) -> Event:
    """Build a synthetic Event for projector tests."""
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=_TID,
        session_id=_SID,
        type=type_,
        data=data,
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Test 1 — orphan call is caught
# ---------------------------------------------------------------------------


def test_orphan_call_is_caught() -> None:
    """An assistant tool_calls with no following tool message asserts.

    Path: user "go" → assistant tool_call(t1) → user "hi" (no result).
    The projector's post-projection check must fire in test mode.
    """
    events = [
        _mk(EventType.USER_MESSAGE, {"content": {"text": "go"}}),
        _mk(
            EventType.TOOL_CALLED,
            {
                "tool_call_id": "t1",
                "tool_name": "noop",
                "arguments": {},
            },
        ),
        _mk(EventType.USER_MESSAGE, {"content": {"text": "hi"}}),
    ]

    with pytest.raises(AssertionError, match="bug-064"):
        _project_events(events)


def test_orphan_call_prod_drops_in_memory() -> None:
    """Production degrade: the orphan call is dropped in-memory only."""
    events = [
        _mk(EventType.USER_MESSAGE, {"content": {"text": "go"}}),
        _mk(
            EventType.TOOL_CALLED,
            {
                "tool_call_id": "t1",
                "tool_name": "noop",
                "arguments": {},
            },
        ),
        _mk(EventType.USER_MESSAGE, {"content": {"text": "hi"}}),
    ]

    with patch("engine.assembly._in_test_mode", return_value=False):
        messages = _project_events(events)

    # No message may carry an unpaired tool_calls entry for t1.
    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            ids = {str(tc.get("id", "")) for tc in msg.tool_calls}
            assert "t1" not in ids
    # The two user messages survive intact.
    user_texts = [m.content for m in messages if m.role == "user"]
    assert user_texts == ["go", "hi"]


# ---------------------------------------------------------------------------
# Test 2 — orphan result (compaction) is caught
# ---------------------------------------------------------------------------


def test_orphan_result_from_compaction_is_caught() -> None:
    """Compaction that drops tool.called but keeps tool.result asserts.

    Simulates the reverse orphan direction: a compaction span
    swallows the ``tool.called`` event but leaves its matching
    ``tool.result`` untouched. The projector sees a bare
    ``role="tool"`` with no preceding assistant ``tool_calls`` id.
    """
    called_ev = _mk(
        EventType.TOOL_CALLED,
        {
            "tool_call_id": "t7",
            "tool_name": "noop",
            "arguments": {},
        },
    )
    result_ev = _mk(
        EventType.TOOL_RESULT,
        {
            "tool_call_id": "t7",
            "tool_name": "noop",
            "result": {"ok": True},
        },
    )
    # Active compaction event that swallows the CALLED but NOT the
    # RESULT — the pathological configuration that produces an
    # orphan result at projection time.
    compaction_ev = _mk(
        EventType.CONTEXT_COMPACTED,
        {
            "preview": False,
            "compacted_event_ids": [str(called_ev.event_id)],
            "summary": {
                "compacted_event_ids": [str(called_ev.event_id)],
                "summary_text": "prior tool activity summarized",
            },
        },
    )
    events = [
        _mk(EventType.USER_MESSAGE, {"content": {"text": "go"}}),
        called_ev,
        result_ev,
        compaction_ev,
        _mk(EventType.USER_MESSAGE, {"content": {"text": "next"}}),
    ]

    with pytest.raises(AssertionError, match="bug-064"):
        _project_events(events)


def test_orphan_result_prod_drops_tool_msg() -> None:
    """Production degrade: the bare tool message is dropped."""
    called_ev = _mk(
        EventType.TOOL_CALLED,
        {
            "tool_call_id": "t7",
            "tool_name": "noop",
            "arguments": {},
        },
    )
    result_ev = _mk(
        EventType.TOOL_RESULT,
        {
            "tool_call_id": "t7",
            "tool_name": "noop",
            "result": {"ok": True},
        },
    )
    compaction_ev = _mk(
        EventType.CONTEXT_COMPACTED,
        {
            "preview": False,
            "compacted_event_ids": [str(called_ev.event_id)],
            "summary": {
                "compacted_event_ids": [str(called_ev.event_id)],
                "summary_text": "prior tool activity summarized",
            },
        },
    )
    events = [
        _mk(EventType.USER_MESSAGE, {"content": {"text": "go"}}),
        called_ev,
        result_ev,
        compaction_ev,
        _mk(EventType.USER_MESSAGE, {"content": {"text": "next"}}),
    ]

    with patch("engine.assembly._in_test_mode", return_value=False):
        messages = _project_events(events)

    # No orphan tool message survives.
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert tool_msgs == []
    # Both user messages and the compaction system message survive.
    user_texts = [m.content for m in messages if m.role == "user"]
    assert user_texts == ["go", "next"]
    assert any(m.role == "system" for m in messages)


# ---------------------------------------------------------------------------
# Test 3 — partially-resolved multi-call turn (per-id check)
# ---------------------------------------------------------------------------


def test_multi_call_partial_resolution_drops_only_dangling() -> None:
    """Assistant tool_calls=[a,b], only ``a`` resolved → ``b`` dropped.

    The fold at assembly.py:261-265 merges TOOL_CALLED events after
    an assistant message into a single ``tool_calls[]``. The pairing
    invariant is per-id, so a partially-resolved multi-call turn
    must drop just the dangling id in prod (and assert in tests).
    """
    events = [
        _mk(EventType.USER_MESSAGE, {"content": {"text": "go"}}),
        _mk(EventType.AGENT_MESSAGE, {"content": {"text": "sure"}}),
        _mk(
            EventType.TOOL_CALLED,
            {
                "tool_call_id": "a",
                "tool_name": "noop",
                "arguments": {},
            },
        ),
        _mk(
            EventType.TOOL_CALLED,
            {
                "tool_call_id": "b",
                "tool_name": "noop",
                "arguments": {},
            },
        ),
        _mk(
            EventType.TOOL_RESULT,
            {
                "tool_call_id": "a",
                "tool_name": "noop",
                "result": {"ok": True},
            },
        ),
        # No result for ``b`` — dangling.
        _mk(EventType.USER_MESSAGE, {"content": {"text": "next"}}),
    ]

    # Test mode: asserts.
    with pytest.raises(AssertionError, match="bug-064"):
        _project_events(events)

    # Prod mode: only ``b`` is dropped; ``a`` and its result survive.
    with patch("engine.assembly._in_test_mode", return_value=False):
        messages = _project_events(events)

    assistants = [m for m in messages if m.role == "assistant" and m.tool_calls]
    assert len(assistants) == 1
    surviving_ids = {str(tc.get("id", "")) for tc in assistants[0].tool_calls or []}
    assert surviving_ids == {"a"}
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "a"


# ---------------------------------------------------------------------------
# Test 4 — determinism: well-formed input is byte-identical
# ---------------------------------------------------------------------------


def test_determinism_well_formed_path_byte_identical() -> None:
    """The check is a no-op on a well-formed path.

    We can't literally "toggle the check off" without adding a
    knob (which the spec forbids), so this test verifies the two
    behavioural guarantees the determinism criterion actually cares
    about:

    * The check returns the projected list unchanged (same instances
      in the same order) when the pairing invariant already holds.
    * The output round-trips through ``assemble_context`` byte-for-
      byte across independent calls (no hidden mutation, no
      log-only side effect that alters output on re-run).
    """
    events = [
        _mk(EventType.USER_MESSAGE, {"content": {"text": "go"}}),
        _mk(EventType.AGENT_MESSAGE, {"content": {"text": "sure"}}),
        _mk(
            EventType.TOOL_CALLED,
            {
                "tool_call_id": "t1",
                "tool_name": "noop",
                "arguments": {"n": 1},
            },
        ),
        _mk(
            EventType.TOOL_RESULT,
            {
                "tool_call_id": "t1",
                "tool_name": "noop",
                "result": {"ok": True, "n": 1},
            },
        ),
        _mk(EventType.AGENT_MESSAGE, {"content": {"text": "done"}}),
    ]

    a = _project_events(events)
    b = _project_events(events)
    # Byte-identical across independent calls.
    assert [m.model_dump() for m in a] == [m.model_dump() for m in b]

    # And identical via the public assemble_context entrypoint too.
    ctx1 = assemble_context(events)
    ctx2 = assemble_context(events)
    assert ctx1.model_dump() == ctx2.model_dump()

    # No orphan tool_calls or bare tool messages in the output.
    for i, msg in enumerate(a):
        if msg.role == "assistant" and msg.tool_calls:
            ids = {str(tc.get("id", "")) for tc in msg.tool_calls}
            # Next messages should cover every id.
            j = i + 1
            covered: set[str] = set()
            while j < len(a) and a[j].role == "tool":
                covered.add(a[j].tool_call_id or "")
                j += 1
            assert ids <= covered, f"orphan call ids: {ids - covered}"


# ---------------------------------------------------------------------------
# Test 5 — adjacency-gap: existence-check passes, adjacency-check fails
# ---------------------------------------------------------------------------


def test_adjacency_gap_user_between_call_and_result_is_caught() -> None:
    """The wedge shape ``[assistant(tool_calls=[t1]), user, tool(t1)]``.

    This is the exact shape produced by the pre-ordering-fix bug:
    spec-068 reconciliation appended the tool terminal AFTER the
    trigger's ``user.message`` was already committed, and the
    projection ended up with a user message wedged between the
    assistant's tool_call and its matching tool response.

    Under the old existence-based check this passed (t1 had a
    matching tool message SOMEWHERE later). Under strict adjacency
    the same projection fails — the user message broke the run — and
    ``_history_before_last_user`` would then have sliced off the
    tool response, leaving the assistant's dangling tool_call to
    provoke the OpenAI 400 that started the incident.

    In test mode: raises. In prod mode: the offending orphan-call id
    is stripped and the wandering tool message is dropped, matching
    the drop policy tests 1-4 exercise.
    """
    events = [
        _mk(EventType.USER_MESSAGE, {"content": {"text": "go"}}),
        # No AGENT_MESSAGE, so TOOL_CALLED synthesizes a
        # tool-call-only assistant turn per the fold at
        # assembly.py:290-299.
        _mk(
            EventType.TOOL_CALLED,
            {
                "tool_call_id": "t1",
                "tool_name": "noop",
                "arguments": {},
            },
        ),
        # A user message lands between the call and its result — the
        # wedge shape. Existence-check passes here (t1 is answered
        # later), adjacency-check fires.
        _mk(EventType.USER_MESSAGE, {"content": {"text": "hi"}}),
        _mk(
            EventType.TOOL_RESULT,
            {
                "tool_call_id": "t1",
                "tool_name": "noop",
                "result": {"ok": True},
            },
        ),
    ]

    # Test mode: asserts.
    with pytest.raises(AssertionError, match="bug-064"):
        _project_events(events)

    # Prod mode: drop the orphan-call id (assistant becomes empty →
    # whole message dropped) and drop the wandering tool message.
    with patch("engine.assembly._in_test_mode", return_value=False):
        messages = _project_events(events)

    # No assistant with tool_calls survives.
    assistants_with_calls = [
        m for m in messages if m.role == "assistant" and m.tool_calls
    ]
    assert assistants_with_calls == []
    # No tool message survives.
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert tool_msgs == []
    # Both user messages survive intact.
    user_texts = [m.content for m in messages if m.role == "user"]
    assert user_texts == ["go", "hi"]
