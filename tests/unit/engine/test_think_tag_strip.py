"""bug-087 — leading `<think>…</think>` strip at the engine LLM handle.

Covers the acceptance criteria from
``process/bug-087-strip-think-reasoning-tags-from-model-responses.md``:

- AC #1 — leading `<think>…</think>` stripped from both the emitted
  ``model.response`` event content and the returned
  ``LLMResponse.content``; verified for both handle sites and, via
  `MinimalFramework.run_exchange`, on the framework return path
  (which is what becomes ``nodes.<agent>.text``).
- AC #2 — clean content passes through byte-identical (the golden
  suite is the wider regression net; this file pins the handle-level
  contract).
- AC #3 — mid-content `<think>` survives.
- AC #4 — truncated leading `<think>` (no closing tag) is handled
  deterministically: whole payload → reasoning, content becomes empty.
- AC #5 — stripped reasoning is captured on
  ``ModelResponseData.reasoning_trace`` (previously always null).
- AC #6 — one shared helper drives both handle sites.

The CrewAIFramework path is not asserted directly because the strip
is at the framework-agnostic LLM-handle seam — the same code path
CrewAI's `_CrewAILLM.call` invokes via `self.llm_handle(request)`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from engine._fallback import FallbackLLMHandle
from engine._think_strip import strip_leading_think
from engine.framework import LLMRequest, LLMResponse, Message
from engine.mock_framework import MinimalFramework
from engine.observers import _ObservedLLMHandle
from schemas import Actor, EventType, ModelSpec

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


# ---------------------------------------------------------------------------
# The helper — fast exhaustive coverage of every branch
# ---------------------------------------------------------------------------


class TestStripLeadingThinkHelper:
    """Pure-function contract for ``strip_leading_think``."""

    def test_leading_block_returns_clean_text_and_reasoning(self) -> None:
        clean, reasoning = strip_leading_think(
            '<think>Let me analyze…</think>\n{"a": 1}'
        )
        assert clean == '{"a": 1}'
        assert reasoning == "Let me analyze…"

    def test_leading_block_with_prefix_whitespace_is_stripped(self) -> None:
        clean, reasoning = strip_leading_think('   \n<think>reason</think>  \n{"a": 1}')
        assert clean == '{"a": 1}'
        assert reasoning == "reason"

    def test_leading_block_multiline_reasoning(self) -> None:
        clean, reasoning = strip_leading_think(
            "<think>\nline 1\nline 2\n</think>\nfinal answer"
        )
        assert clean == "final answer"
        assert reasoning == "\nline 1\nline 2\n"

    def test_clean_content_passes_through_byte_identical_no_reasoning(
        self,
    ) -> None:
        for text in ('{"a": 1}', "just prose", "", "  spaces  ", "no tags here"):
            clean, reasoning = strip_leading_think(text)
            assert clean == text
            assert reasoning is None

    def test_none_input_yields_none_none(self) -> None:
        assert strip_leading_think(None) == (None, None)

    def test_mid_content_think_tag_is_preserved(self) -> None:
        # Legitimate `<think>` after real content — non-leading. Must survive.
        text = "here is code: <think>x</think>"
        clean, reasoning = strip_leading_think(text)
        assert clean == text
        assert reasoning is None

    def test_leading_block_does_not_swallow_second_think_block(self) -> None:
        # Non-greedy match: only the FIRST `</think>` closes the leading block.
        # A second `<think>…</think>` deeper in the content stays put.
        clean, reasoning = strip_leading_think(
            "<think>first</think>\nprelude <think>second</think> tail"
        )
        assert clean == "prelude <think>second</think> tail"
        assert reasoning == "first"

    def test_truncated_leading_think_treated_as_reasoning(self) -> None:
        # No closing `</think>` — deterministic: whole payload is reasoning.
        clean, reasoning = strip_leading_think("<think>partial with no close")
        assert clean == ""
        assert reasoning == "partial with no close"

    def test_truncated_with_leading_whitespace_handled(self) -> None:
        clean, reasoning = strip_leading_think("  \n<think>cut off\nmid-thought")
        assert clean == ""
        assert reasoning == "cut off\nmid-thought"


# ---------------------------------------------------------------------------
# Handle-level fixtures
# ---------------------------------------------------------------------------


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[
            Message(role="system", content="sys"),
            Message(role="user", content="hi"),
        ],
        model="mock/test",
    )


class _StubLLM:
    """Trivial async callable returning a preset ``LLMResponse``."""

    def __init__(self, content: str | None) -> None:
        self._content = content

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        _ = request
        return LLMResponse(content=self._content)


def _emit_capture(
    captured: list[dict[str, Any]],
) -> Callable[..., Awaitable[None]]:
    async def _emit(*, type_: EventType, data: dict[str, Any], actor: Actor) -> None:
        _ = actor
        captured.append({"type": type_, "data": data})

    return _emit


def _observed_handle(
    llm: Any,
    captured: list[dict[str, Any]],
) -> _ObservedLLMHandle:
    return _ObservedLLMHandle(
        emit=_emit_capture(captured),
        agent_id="ag_test",
        model_spec={"provider": "mock", "model": "test"},
        config_event_id="ev_00000000-0000-0000-0000-000000000000",
        llm=llm,
    )


def _fallback_handle(
    llm: Any,
    captured: list[dict[str, Any]],
) -> FallbackLLMHandle:
    spec = ModelSpec(provider="mock", model="test")

    async def _factory(_: ModelSpec) -> Any:
        return llm

    return FallbackLLMHandle(
        emit=_emit_capture(captured),
        agent_id="ag_test",
        primary_spec=spec,
        chain=[],
        llm_factory=_factory,
        config_event_id=f"ev_{uuid4()}",
    )


# ---------------------------------------------------------------------------
# _ObservedLLMHandle — strip on the emitted event AND the returned response
# ---------------------------------------------------------------------------


class TestObservedHandleStrip:
    @pytest.mark.asyncio
    async def test_leading_think_stripped_from_event_and_response(self) -> None:
        captured: list[dict[str, Any]] = []
        handle = _observed_handle(
            _StubLLM('<think>reasoning</think>\n{"a": 1}'), captured
        )
        response = await handle(_request())

        assert response.content == '{"a": 1}'
        final = next(e for e in captured if e["type"] == EventType.MODEL_RESPONSE)
        assert final["data"]["content"] == {"text": '{"a": 1}'}

    @pytest.mark.asyncio
    async def test_reasoning_captured_on_reasoning_trace(self) -> None:
        captured: list[dict[str, Any]] = []
        handle = _observed_handle(
            _StubLLM("<think>my reasoning</think>\nfinal"), captured
        )
        await handle(_request())
        final = next(e for e in captured if e["type"] == EventType.MODEL_RESPONSE)
        assert final["data"]["reasoning_trace"] == {"text": "my reasoning"}

    @pytest.mark.asyncio
    async def test_clean_content_no_op_and_reasoning_trace_null(self) -> None:
        captured: list[dict[str, Any]] = []
        handle = _observed_handle(_StubLLM('{"a": 1}'), captured)
        response = await handle(_request())

        assert response.content == '{"a": 1}'
        final = next(e for e in captured if e["type"] == EventType.MODEL_RESPONSE)
        assert final["data"]["content"] == {"text": '{"a": 1}'}
        # Byte-identical no-op — reasoning_trace stays null exactly like before.
        assert final["data"]["reasoning_trace"] is None

    @pytest.mark.asyncio
    async def test_midcontent_think_preserved(self) -> None:
        captured: list[dict[str, Any]] = []
        payload = "here is code: <think>x</think>"
        handle = _observed_handle(_StubLLM(payload), captured)
        response = await handle(_request())

        assert response.content == payload
        final = next(e for e in captured if e["type"] == EventType.MODEL_RESPONSE)
        assert final["data"]["content"] == {"text": payload}
        assert final["data"]["reasoning_trace"] is None

    @pytest.mark.asyncio
    async def test_truncated_leading_think_deterministic(self) -> None:
        captured: list[dict[str, Any]] = []
        handle = _observed_handle(_StubLLM("<think>cut off mid-thought"), captured)
        response = await handle(_request())

        assert response.content == ""
        final = next(e for e in captured if e["type"] == EventType.MODEL_RESPONSE)
        assert final["data"]["content"] == {"text": ""}
        assert final["data"]["reasoning_trace"] == {
            "text": "cut off mid-thought",
        }


# ---------------------------------------------------------------------------
# FallbackLLMHandle — same contract on the production handle
# ---------------------------------------------------------------------------


class TestFallbackHandleStrip:
    @pytest.mark.asyncio
    async def test_leading_think_stripped_from_event_and_response(self) -> None:
        captured: list[dict[str, Any]] = []
        handle = _fallback_handle(
            _StubLLM('<think>reasoning</think>\n{"a": 1}'), captured
        )
        response = await handle(_request())

        assert response.content == '{"a": 1}'
        final = next(e for e in captured if e["type"] == EventType.MODEL_RESPONSE)
        assert final["data"]["content"] == {"text": '{"a": 1}'}
        assert final["data"]["reasoning_trace"] == {"text": "reasoning"}

    @pytest.mark.asyncio
    async def test_clean_content_reasoning_trace_null(self) -> None:
        captured: list[dict[str, Any]] = []
        handle = _fallback_handle(_StubLLM('{"a": 1}'), captured)
        response = await handle(_request())

        assert response.content == '{"a": 1}'
        final = next(e for e in captured if e["type"] == EventType.MODEL_RESPONSE)
        assert final["data"]["content"] == {"text": '{"a": 1}'}
        assert final["data"]["reasoning_trace"] is None

    @pytest.mark.asyncio
    async def test_midcontent_think_preserved(self) -> None:
        captured: list[dict[str, Any]] = []
        payload = "here is code: <think>x</think>"
        handle = _fallback_handle(_StubLLM(payload), captured)
        response = await handle(_request())

        assert response.content == payload
        final = next(e for e in captured if e["type"] == EventType.MODEL_RESPONSE)
        assert final["data"]["content"] == {"text": payload}
        assert final["data"]["reasoning_trace"] is None

    @pytest.mark.asyncio
    async def test_truncated_leading_think_deterministic(self) -> None:
        captured: list[dict[str, Any]] = []
        handle = _fallback_handle(_StubLLM("<think>partial with no close"), captured)
        response = await handle(_request())

        assert response.content == ""
        final = next(e for e in captured if e["type"] == EventType.MODEL_RESPONSE)
        assert final["data"]["content"] == {"text": ""}
        assert final["data"]["reasoning_trace"] == {
            "text": "partial with no close",
        }


# ---------------------------------------------------------------------------
# End-to-end via MinimalFramework — the strip flows through to `agent.message`
# ---------------------------------------------------------------------------


class TestMinimalFrameworkPropagation:
    """`MinimalFramework.run_exchange` returns ``response.content or ""``.

    That return value is what becomes the ``agent.message`` node text.
    So a stripped ``LLMResponse.content`` at the handle guarantees the
    node text is clean under MinimalFramework — which is what the
    autoply synthesizer and any compute node consume.
    """

    @pytest.mark.asyncio
    async def test_run_exchange_returns_stripped_content(self) -> None:
        captured: list[dict[str, Any]] = []
        handle = _fallback_handle(
            _StubLLM('<think>reasoning</think>\n{"a": 1}'), captured
        )
        framework = MinimalFramework()
        result = await framework.run_exchange(
            system_prompt="sys",
            user_message="hi",
            history=(),
            llm=handle,
            tools=(),
        )
        assert result == '{"a": 1}'

    @pytest.mark.asyncio
    async def test_run_exchange_no_op_on_clean_content(self) -> None:
        captured: list[dict[str, Any]] = []
        handle = _fallback_handle(_StubLLM("Hello there."), captured)
        framework = MinimalFramework()
        result = await framework.run_exchange(
            system_prompt="sys",
            user_message="hi",
            history=(),
            llm=handle,
            tools=(),
        )
        assert result == "Hello there."
