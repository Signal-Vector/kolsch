"""bug-148 Test 3 — CrewAI console output is silenced at the adapter seam.

A real `crewai.llms.base_llm.BaseLLM` subclass emits the incident's
event sequence through the real `crewai_event_bus` (one
`LLMCallStartedEvent`, then two `LLMCallFailedEvent`s, as
`OpenAICompletion` does on a 400) and raises. Called directly in a
worker thread, CrewAI's singleton listener prints `❌ LLM Error` panels
and a `[CrewAIEventsBus] … empty scope stack` warning (the control).
Called through `make_llm_callable`, nothing reaches stdout/stderr, the
exception propagates unchanged, and the suppression flag does not leak
into the caller.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest
from crewai.events.event_bus import crewai_event_bus
from crewai.events.event_listener import EventListener
from crewai.events.utils.console_formatter import should_suppress_console_output
from crewai.llms.base_llm import BaseLLM, llm_call_context

from engine._crewai_adapter import make_llm_callable
from engine.framework import LLMRequest, Message

if TYPE_CHECKING:
    from crewai import LLM

_ERROR = "OpenAI API call failed: Error code: 400 - stub 400"


class _StubFailure(RuntimeError):
    pass


class _FailingLLM(BaseLLM):
    """Emits started + two failed events (like `OpenAICompletion`) and raises."""

    def call(  # type: ignore[override]
        self, messages: Any, tools: Any = None, **kwargs: Any
    ) -> Any:
        with llm_call_context():
            self._emit_call_started_event(messages=messages)
            self._emit_call_failed_event(error=_ERROR)
            self._emit_call_failed_event(error=_ERROR)
        raise _StubFailure(_ERROR)


def _noise(text: str) -> list[str]:
    return [marker for marker in ("LLM Error", "CrewAIEventsBus") if marker in text]


@pytest.fixture(autouse=True)
def _listener(monkeypatch: pytest.MonkeyPatch) -> None:
    # The singleton listener that owns the panel-printing ConsoleFormatter
    # (constructing it is idempotent). Its default is `verbose=True`, as
    # in a fresh `kolsch run`; an earlier Crew kickoff in the same test
    # process can flip it, so pin it.
    monkeypatch.setattr(EventListener().formatter, "verbose", True)


def _request() -> LLMRequest:
    return LLMRequest(messages=[Message(role="user", content="hi")], model="stub")


async def test_crewai_console_quiet_control_direct_call_prints_noise(
    capfd: pytest.CaptureFixture[str],
) -> None:
    # Proves the test can see the noise at all.
    llm = _FailingLLM(model="stub")
    with pytest.raises(_StubFailure):
        await asyncio.to_thread(llm.call, [{"role": "user", "content": "hi"}])
    crewai_event_bus.flush()

    captured = capfd.readouterr()
    assert _noise(captured.out + captured.err) == ["LLM Error", "CrewAIEventsBus"]


@pytest.mark.parametrize("timeout_env", ["0", "30"], ids=["no-timeout", "timeout"])
async def test_crewai_console_quiet_through_adapter(
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    timeout_env: str,
) -> None:
    # "0" takes the unbounded `to_thread` branch, "30" the `wait_for` one.
    monkeypatch.setenv("KOLSCH_LLM_CALL_TIMEOUT_SECONDS", timeout_env)
    call = make_llm_callable(cast("LLM", _FailingLLM(model="stub")))

    with pytest.raises(_StubFailure, match="stub 400"):
        await call(_request())
    crewai_event_bus.flush()

    captured = capfd.readouterr()
    assert _noise(captured.out + captured.err) == []
    assert captured.out == ""
    # Restored in the calling context: nothing outside the call is muted.
    assert should_suppress_console_output() is False
