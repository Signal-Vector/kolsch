"""feat-054 Phase 2 — the 8 boundary invariants (spec §4).

Parametrized over the invariants: for each, construct a synthetic
event stream where a naive cut would split the atomic group, then
assert :func:`snap_to_safe_boundary` extends the cut past the group.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from providers.compaction import snap_to_safe_boundary
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


def test_invariant_1_tool_called_result_pair_extends_later() -> None:
    events = [
        _mk(EventType.USER_MESSAGE, {}),
        _mk(EventType.TOOL_CALLED, {"tool_call_id": "tc1", "tool_name": "x"}),
        _mk(EventType.TOOL_RESULT, {"tool_call_id": "tc1", "tool_name": "x"}),
        _mk(EventType.AGENT_MESSAGE, {}),
    ]
    # Cut at 2 leaves called in head, result in tail — unsafe. Snap later → 3+.
    assert snap_to_safe_boundary(events, 2, "later") >= 3


def test_invariant_2_assistant_with_tool_calls_and_results() -> None:
    events = [
        _mk(EventType.AGENT_MESSAGE, {"content": {"text": ""}}),
        _mk(EventType.TOOL_CALLED, {"tool_call_id": "tc1", "tool_name": "x"}),
        _mk(EventType.TOOL_CALLED, {"tool_call_id": "tc2", "tool_name": "y"}),
        _mk(EventType.TOOL_RESULT, {"tool_call_id": "tc1", "tool_name": "x"}),
        _mk(EventType.TOOL_RESULT, {"tool_call_id": "tc2", "tool_name": "y"}),
    ]
    assert snap_to_safe_boundary(events, 3, "later") >= 5


def test_invariant_3_streaming_token_sequence() -> None:
    events = [
        _mk(EventType.MODEL_REQUEST, {"call_id": "c1"}),
        _mk(EventType.MODEL_STREAMING_TOKEN, {"call_id": "c1", "text_delta": "a"}),
        _mk(EventType.MODEL_STREAMING_TOKEN, {"call_id": "c1", "text_delta": "b"}),
        _mk(EventType.MODEL_RESPONSE, {"call_id": "c1"}),
    ]
    assert snap_to_safe_boundary(events, 1, "later") >= 4


def test_invariant_4_model_request_response_pair() -> None:
    events = [
        _mk(EventType.MODEL_REQUEST, {"call_id": "c1"}),
        _mk(EventType.MODEL_RESPONSE, {"call_id": "c1"}),
    ]
    assert snap_to_safe_boundary(events, 1, "later") >= 2


def test_invariant_5_exchange_started_completed_pair() -> None:
    started = _mk(EventType.THREAD_EXCHANGE_STARTED, {"trigger_event_id": "ev_x"})
    events = [
        started,
        _mk(EventType.USER_MESSAGE, {}),
        _mk(EventType.AGENT_MESSAGE, {}),
        _mk(
            EventType.THREAD_EXCHANGE_COMPLETED,
            {"exchange_started_id": str(started.event_id), "agent_message_id": "ev_a"},
        ),
    ]
    assert snap_to_safe_boundary(events, 2, "later") >= 4


def test_invariant_6_session_opened_never_compacted() -> None:
    events = [
        _mk(EventType.SESSION_OPENED, {}),
        _mk(EventType.USER_MESSAGE, {}),
    ]
    assert snap_to_safe_boundary(events, 1, "later") == 1
    assert snap_to_safe_boundary(events, 2, "later") == 2


def test_invariant_7_memory_retrieved_next_agent_message() -> None:
    events = [
        _mk(EventType.USER_MESSAGE, {}),
        _mk(EventType.MEMORY_RETRIEVED, {"results": []}),
        _mk(EventType.AGENT_MESSAGE, {}),
    ]
    assert snap_to_safe_boundary(events, 2, "later") >= 3


def test_invariant_8_prior_context_compacted_span_intact() -> None:
    e1 = _mk(EventType.USER_MESSAGE, {})
    e2 = _mk(EventType.AGENT_MESSAGE, {})
    e3 = _mk(
        EventType.CONTEXT_COMPACTED,
        {
            "compacted_event_ids": [str(e1.event_id), str(e2.event_id)],
            "summary": {},
            "tokens_before": 100,
            "tokens_after": 20,
            "pre_compaction": {},
        },
    )
    e4 = _mk(EventType.USER_MESSAGE, {})
    events = [e1, e2, e3, e4]
    # Cut at 1 puts e1 in head, e2 + emit in tail; emit's span straddles → unsafe.
    result = snap_to_safe_boundary(events, 1, "later")
    assert result >= 3


def test_snap_is_idempotent_when_already_safe() -> None:
    events = [
        _mk(EventType.USER_MESSAGE, {}),
        _mk(EventType.AGENT_MESSAGE, {}),
        _mk(EventType.USER_MESSAGE, {}),
    ]
    assert snap_to_safe_boundary(events, 2, "later") == 2


def test_snap_empty_events_returns_input() -> None:
    assert snap_to_safe_boundary([], 0, "later") == 0
