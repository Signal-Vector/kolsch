"""Test 12 — streaming event delivery.

Drives the observer's LLM streaming hook with a mock provider that
yields two deltas, then asserts the emitted event log carries two
``MODEL_STREAMING_TOKEN`` events followed by a final
``MODEL_RESPONSE`` with the concatenated text.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from engine.framework import LLMRequest, LLMResponse, Message
from engine.observers import _ObservedLLMHandle
from schemas.events import EventType


class _MockStreamingLLM:
    """Mock LLM that emits two deltas then returns their concatenation."""

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = list(deltas)

    async def stream(
        self,
        request: LLMRequest,
        on_delta: Callable[[str], Any],
    ) -> LLMResponse:
        for chunk in self._deltas:
            await on_delta(chunk)
        return LLMResponse(content="".join(self._deltas))

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        # Fallback if the observer picks the non-streaming path.
        return LLMResponse(content="".join(self._deltas))


class _MockNonStreamingLLM:
    """Baseline: no `stream` method — observer stays on the non-streaming path."""

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(content="Hello there")


@pytest.mark.asyncio
async def test_streaming_llm_emits_delta_events() -> None:
    """Two deltas → two MODEL_STREAMING_TOKEN events + one MODEL_RESPONSE."""
    emitted: list[dict[str, Any]] = []

    async def _emit(*, type_: EventType, data: dict, actor: Any) -> None:
        emitted.append({"type": type_, "data": data})

    handle = _ObservedLLMHandle(
        emit=_emit,
        agent_id="ag_test",
        model_spec={"provider": "mock", "model": "test"},
        config_event_id="ev_00000000-0000-0000-0000-000000000000",
        llm=_MockStreamingLLM(["Hello ", "there"]),
    )
    request = LLMRequest(
        model="mock/test",
        messages=[Message(role="user", content="hi")],
        tools=[],
    )
    response = await handle(request)

    types = [e["type"] for e in emitted]
    assert types == [
        EventType.MODEL_REQUEST,
        EventType.MODEL_STREAMING_TOKEN,
        EventType.MODEL_STREAMING_TOKEN,
        EventType.MODEL_RESPONSE,
    ]
    deltas = [
        e["data"]["text_delta"]
        for e in emitted
        if e["type"] == EventType.MODEL_STREAMING_TOKEN
    ]
    assert deltas == ["Hello ", "there"]
    final = next(e for e in emitted if e["type"] == EventType.MODEL_RESPONSE)
    assert final["data"]["content"]["text"] == "Hello there"
    assert response.content == "Hello there"


@pytest.mark.asyncio
async def test_non_streaming_llm_emits_zero_deltas() -> None:
    """Non-streaming provider path: only MODEL_REQUEST + MODEL_RESPONSE."""
    emitted: list[dict[str, Any]] = []

    async def _emit(*, type_: EventType, data: dict, actor: Any) -> None:
        emitted.append({"type": type_, "data": data})

    handle = _ObservedLLMHandle(
        emit=_emit,
        agent_id="ag_test",
        model_spec={"provider": "mock", "model": "test"},
        config_event_id="ev_00000000-0000-0000-0000-000000000000",
        llm=_MockNonStreamingLLM(),
    )
    request = LLMRequest(
        model="mock/test",
        messages=[Message(role="user", content="hi")],
        tools=[],
    )
    await handle(request)

    types = [e["type"] for e in emitted]
    assert types == [EventType.MODEL_REQUEST, EventType.MODEL_RESPONSE]
