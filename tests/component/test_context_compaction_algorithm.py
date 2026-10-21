"""feat-054 Phase 2 — token estimator + compact() end-to-end.

Covers the token estimator and the `compact()` end-to-end path with
a mocked summariser LLM. The boundary-invariant tests live in
:mod:`tests.component.test_context_compaction_boundary_invariants`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from providers.compaction import (
    ReferenceSummaryCompactionProvider,
    estimate_tokens_for_messages,
)
from schemas import Actor, Event, EventType


def _eid() -> str:
    return f"ev_{uuid4()}"


def _thread_id() -> str:
    return f"th_{uuid4()}"


def _session_id() -> str:
    return f"ses_{uuid4()}"


_TID = _thread_id()
_SID = _session_id()


def _make_event(type_: EventType, data: dict[str, Any]) -> Event:
    return Event(
        event_id=_eid(),
        parent_event_id=None,
        thread_id=_TID,
        session_id=_SID,
        type=type_,
        data=data,
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# estimate_tokens_for_messages
# ---------------------------------------------------------------------------


def test_estimate_tokens_empty_list_is_zero() -> None:
    assert estimate_tokens_for_messages([], None) == 0


def test_estimate_tokens_single_message_is_positive() -> None:
    tokens = estimate_tokens_for_messages(
        [{"role": "user", "content": "Hello, world!"}],
        None,
    )
    assert tokens > 4
    assert tokens < 50


def test_estimate_tokens_scales_with_content() -> None:
    small = estimate_tokens_for_messages([{"role": "user", "content": "short"}], None)
    large = estimate_tokens_for_messages(
        [{"role": "user", "content": "short " * 200}], None
    )
    assert large > small * 10


def test_estimate_tokens_includes_tool_schemas() -> None:
    no_tools = estimate_tokens_for_messages([{"role": "user", "content": "hi"}], None)
    with_tools = estimate_tokens_for_messages(
        [{"role": "user", "content": "hi"}],
        [
            {
                "name": "search",
                "description": "search the web for a query",
                "schema": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                },
            }
        ],
    )
    assert with_tools > no_tools


# ---------------------------------------------------------------------------
# compact() end-to-end
# ---------------------------------------------------------------------------


class _MockLLM:
    """Async callable that returns a fixed JSON summary."""

    def __init__(self, body: str) -> None:
        self._body = body
        self.calls: list[Any] = []

    async def __call__(self, request: Any) -> Any:
        from engine import LLMResponse

        self.calls.append(request)
        return LLMResponse(content=self._body, tool_calls=None, usage={}, raw={})


@pytest.mark.asyncio
async def test_compact_empty_events_returns_noop() -> None:
    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=None,
        config={},
    )
    outcome = await provider.compact(
        llm_context=None,
        events=[],
        keep_head=0,
        keep_tail=1000,
        model_context_length=100_000,
        thread_id=_TID,
        config={},
    )
    assert outcome.compacted_event_ids == []


@pytest.mark.asyncio
async def test_compact_end_to_end_invokes_summariser() -> None:
    events = [
        _make_event(EventType.SESSION_OPENED, {}),
        _make_event(EventType.USER_MESSAGE, {"content": {"text": "hi"}}),
        _make_event(EventType.AGENT_MESSAGE, {"content": {"text": "hello"}}),
        _make_event(EventType.USER_MESSAGE, {"content": {"text": "again"}}),
        _make_event(EventType.AGENT_MESSAGE, {"content": {"text": "sure"}}),
        _make_event(EventType.USER_MESSAGE, {"content": {"text": "keep this"}}),
    ]
    ref_id = str(events[1].event_id)
    body = (
        '{"schema_version": 1, "summary_text": "user asked and agent answered", '
        '"refs": [{"event_id": "'
        + ref_id
        + '", "note": "user opened"}], "kept_files": [], '
        '"span": {"first_event_id": "'
        + ref_id
        + '", "last_event_id": "'
        + ref_id
        + '", "event_count": 1}}'
    )
    llm = _MockLLM(body)

    async def factory(_spec: Any, *, context: Any = None) -> Any:
        return llm

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=factory,
        config={},
    )
    outcome = await provider.compact(
        llm_context=None,
        events=events,
        keep_head=0,
        keep_tail=5,
        model_context_length=1000,
        thread_id=_TID,
        config={},
    )
    assert outcome.compacted_event_ids
    assert len(llm.calls) == 1
    assert outcome.summary["schema_version"] == 1
    assert "summary_text" in outcome.summary
    assert "refs" in outcome.summary
    assert "span" in outcome.summary
    assert "compactor" in outcome.summary


@pytest.mark.asyncio
async def test_compact_caps_refs_at_max_refs() -> None:
    events = [
        _make_event(EventType.USER_MESSAGE, {"content": {"text": f"{i}"}})
        for i in range(30)
    ]
    fake_ids = [_eid() for _ in range(60)]
    refs_json = ",".join(f'{{"event_id": "{eid}", "note": "n"}}' for eid in fake_ids)
    body = (
        '{"schema_version": 1, "summary_text": "s", "refs": ['
        + refs_json
        + '], "kept_files": [], '
        '"span": {"first_event_id": "'
        + fake_ids[0]
        + '", "last_event_id": "'
        + fake_ids[-1]
        + '", "event_count": 60}}'
    )
    llm = _MockLLM(body)

    async def factory(_spec: Any, *, context: Any = None) -> Any:
        return llm

    provider = ReferenceSummaryCompactionProvider(
        llm_for_model=factory,
        config={},
    )
    outcome = await provider.compact(
        llm_context=None,
        events=events,
        keep_head=0,
        keep_tail=1,
        model_context_length=100,
        thread_id=_TID,
        config={"max_refs": 10},
    )
    refs = outcome.summary["refs"]
    assert len(refs) == 10
    assert "event_id_range" in refs[-1]
