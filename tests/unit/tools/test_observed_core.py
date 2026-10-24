"""spec-119 Tests 2-3 + AC 8 — the observed tool-invocation core.

Drives ``tools.observed.invoke_observed`` with a synthetic (non-engine)
capture sink and stub subsystems, pinning:

- allowed: emits exactly ``[tool.called, tool.result]`` (copied
  ``arguments``, **normalized** event ``result``, ``actor=agent``) and
  returns ``Ok`` carrying the **raw** result (raw != normalized for a
  non-dict result);
- denied / errored: emits ``tool.failed`` with ``recovery_hint``
  populated and returns the typed ``Denied`` / ``Failed`` outcome —
  never a ``{"error": ...}`` dict (that mapping is the caller's);
- ``asyncio.CancelledError`` from ``invoke_tool`` propagates — the
  core does not convert it to ``Failed`` and emits no terminal;
- AC 8 event-parity: the synthetic caller's emitted sequence is
  identical (kinds, ordering, data, actor) to what the engine's
  ``_ObservedToolHandle`` emits for the same calls, and the wrapper's
  return is exactly the outcome mapped to the legacy dict shape.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from errors import ToolExecutionFailed, ToolUnauthorized
from schemas import Actor, CallerChain, EventType
from tools.decorator import ToolResult
from tools.observed import Denied, Failed, Ok, invoke_observed

_AGENT_ID = "ag_test"
_SESSION_ID = "ses_00000000-0000-0000-0000-000000000001"
_THREAD_ID = "th_00000000-0000-0000-0000-000000000002"
_HINT = "/perm allow kolsch/tools/ping *"


class _StubOk:
    """invoke_tool succeeds with a RAW (non-dict) result."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def invoke_tool(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult(result="pong")


class _StubRaise:
    """invoke_tool raises the seeded exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def invoke_tool(self, **_: Any) -> ToolResult:
        raise self._exc


def _denied_exc() -> ToolUnauthorized:
    return ToolUnauthorized(
        tool_name="ping",
        action="kolsch/tools/ping",
        principal=f"agent.{_AGENT_ID}",
        matched_rule={
            "what": "kolsch/tools/ping",
            "pattern": "*",
            "effect": "deny",
        },
        caller_chain=CallerChain.unattributed(),
    )


def _errored_exc() -> ToolExecutionFailed:
    return ToolExecutionFailed(
        tool_name="ping",
        error_class="BoomClass",
        recovery_hint="run: fix-it now",
    )


class _CaptureSink:
    """The synthetic non-engine caller's own emit sink (AC 8)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


async def _invoke(
    subsystem: Any,
    sink: _CaptureSink,
    *,
    tool_call_id: str | None = "tc_1",
    args: dict[str, Any] | None = None,
) -> Any:
    return await invoke_observed(
        emit=sink,
        agent_id=_AGENT_ID,
        session_id=_SESSION_ID,
        thread_id=_THREAD_ID,
        tools_subsystem=subsystem,
        tool_name="ping",
        args={"message": "ack"} if args is None else args,
        tool_call_id=tool_call_id,
        cancel=None,
        caller_chain=CallerChain.unattributed(),
    )


# ---------------------------------------------------------------------------
# Test 2 — allowed
# ---------------------------------------------------------------------------


async def test_allowed_emits_called_then_result_and_returns_raw_ok() -> None:
    """Allowed call: ``[tool.called, tool.result]``, normalized event
    payload, raw ``Ok`` result, ``actor=agent``."""
    sink = _CaptureSink()
    subsystem = _StubOk()
    caller_args = {"message": "ack"}

    outcome = await _invoke(subsystem, sink, args=caller_args)

    assert [e["type_"] for e in sink.events] == [
        EventType.TOOL_CALLED,
        EventType.TOOL_RESULT,
    ]
    called, result_evt = sink.events
    assert called["data"]["tool_name"] == "ping"
    assert called["data"]["tool_call_id"] == "tc_1"
    assert called["data"]["arguments"] == {"message": "ack"}
    assert called["actor"] == Actor(kind="agent", id=_AGENT_ID)
    assert result_evt["actor"] == Actor(kind="agent", id=_AGENT_ID)

    # The caller's args dict is copied before dispatch — the
    # subsystem receives an equal-but-distinct dict.
    assert subsystem.calls[0]["args"] == caller_args
    assert subsystem.calls[0]["args"] is not caller_args

    # Emit-normalized / return-raw split: the EVENT carries the
    # wrapped dict; the OUTCOME carries the raw string.
    assert result_evt["data"]["result"] == {"value": "pong"}
    assert isinstance(outcome, Ok)
    assert outcome.result == "pong"
    assert outcome.result != result_evt["data"]["result"]

    # The ToolContext threads the same call id + principal.
    context = subsystem.calls[0]["context"]
    assert context.tool_call_id == "tc_1"
    assert context.principal == f"agent.{_AGENT_ID}"


async def test_allowed_mints_identical_tc_fallback_when_id_missing() -> None:
    """``tool_call_id=None`` → the core mints the legacy ``tc_<uuid4>``
    fallback and threads ONE id through event + context."""
    sink = _CaptureSink()
    subsystem = _StubOk()

    await _invoke(subsystem, sink, tool_call_id=None)

    minted = sink.events[0]["data"]["tool_call_id"]
    assert minted.startswith("tc_")
    assert minted != "tc_"
    assert subsystem.calls[0]["context"].tool_call_id == minted
    assert sink.events[1]["data"]["tool_call_id"] == minted


# ---------------------------------------------------------------------------
# Test 3 — denied / errored / cancel
# ---------------------------------------------------------------------------


async def test_denied_emits_tool_failed_and_returns_typed_outcome() -> None:
    """``ToolUnauthorized`` → ``tool.failed`` with the derived hint in
    the EVENT, and a ``Denied`` outcome carrying error + hint — not a
    ``{"error": ...}`` dict."""
    sink = _CaptureSink()

    outcome = await _invoke(_StubRaise(_denied_exc()), sink)

    assert [e["type_"] for e in sink.events] == [
        EventType.TOOL_CALLED,
        EventType.TOOL_FAILED,
    ]
    failed = sink.events[1]
    assert failed["data"]["recovery_hint"] == _HINT
    assert failed["data"]["error"]["type"] == "tool.unauthorized"
    assert failed["actor"] == Actor(kind="agent", id=_AGENT_ID)

    # Typed outcome — NOT the caller's `{"error": ...}` dict shape.
    assert isinstance(outcome, Denied)
    assert outcome.error.type == "tool.unauthorized"
    assert outcome.recovery_hint == _HINT


async def test_errored_emits_tool_failed_and_returns_failed_with_hint() -> None:
    """A generic ``PlatformError`` carrying ``.recovery_hint`` →
    ``tool.failed`` + ``Failed(error, recovery_hint)`` (qol-090: the
    hint rides on ``Failed`` too, not only ``Denied``)."""
    sink = _CaptureSink()

    outcome = await _invoke(_StubRaise(_errored_exc()), sink)

    assert [e["type_"] for e in sink.events] == [
        EventType.TOOL_CALLED,
        EventType.TOOL_FAILED,
    ]
    failed = sink.events[1]
    assert failed["data"]["recovery_hint"] == "run: fix-it now"
    assert failed["data"]["error"]["type"] == "tool.execution_failed"

    # Typed outcome — NOT the caller's `{"error": ...}` dict shape.
    assert isinstance(outcome, Failed)
    assert outcome.error.type == "tool.execution_failed"
    assert outcome.recovery_hint == "run: fix-it now"


async def test_errored_without_hint_carries_none() -> None:
    """An exception class that doesn't opt in → ``Failed`` with
    ``recovery_hint=None`` and a ``None`` event field."""
    sink = _CaptureSink()
    exc = ToolExecutionFailed(tool_name="ping", error_class="SomeClass")

    outcome = await _invoke(_StubRaise(exc), sink)

    assert sink.events[1]["data"]["recovery_hint"] is None
    assert isinstance(outcome, Failed)
    assert outcome.recovery_hint is None


async def test_denied_cancelled_error_propagates_without_terminal() -> None:
    """``asyncio.CancelledError`` from ``invoke_tool`` propagates —
    no ``Failed`` conversion, no ``tool.failed``/``session.interrupted``
    emit (the engine wrapper owns the cancel terminal)."""
    sink = _CaptureSink()

    with pytest.raises(asyncio.CancelledError):
        await _invoke(_StubRaise(asyncio.CancelledError()), sink)

    assert [e["type_"] for e in sink.events] == [EventType.TOOL_CALLED]


# ---------------------------------------------------------------------------
# AC 8 — core reusability / event parity with the engine wrapper
# ---------------------------------------------------------------------------


class _EmptyOrPingArgs(BaseModel):
    message: str = ""


async def _noop_fn(*_: Any, **__: Any) -> Any:  # pragma: no cover — never invoked
    return None


def _make_subsystem(arm: str) -> Any:
    """A fresh stub per caller so core and wrapper never share state."""
    if arm == "allowed":
        return _StubOk()
    if arm == "denied":
        return _StubRaise(_denied_exc())
    return _StubRaise(_errored_exc())


@pytest.mark.parametrize("arm", ["allowed", "denied", "errored"])
async def test_core_event_parity_with_engine_wrapper(arm: str) -> None:
    """A synthetic non-engine caller driving the core emits the
    IDENTICAL event sequence (kinds, ordering, data, actor) as the
    engine's ``_ObservedToolHandle`` for the same call, and the
    wrapper's return is exactly the typed outcome mapped to the
    legacy dict shape."""
    from engine.observers import _ObservedToolHandle
    from tools.decorator import ToolDefinition

    core_sink = _CaptureSink()
    outcome = await _invoke(_make_subsystem(arm), core_sink)

    wrapper_sink = _CaptureSink()
    handle = _ObservedToolHandle(
        emit=wrapper_sink,
        agent_id=_AGENT_ID,
        session_id=_SESSION_ID,
        thread_id=_THREAD_ID,
        tools_subsystem=_make_subsystem(arm),
        defn=ToolDefinition(
            name="ping",
            description="d",
            args_schema=_EmptyOrPingArgs,
            func=_noop_fn,
        ),
        caller_chain=CallerChain.unattributed(),
    )
    from engine.framework import ToolCall

    returned = await handle(
        ToolCall(name="ping", arguments={"message": "ack"}, tool_call_id="tc_1")
    )

    # Identical emitted sequence — full kwargs (type_, data, actor).
    assert core_sink.events == wrapper_sink.events

    # The wrapper's return is the outcome under the legacy mapping.
    if isinstance(outcome, Ok):
        assert returned == outcome.result
    else:
        expected = outcome.error.model_dump(mode="json")
        if outcome.recovery_hint is not None:
            expected["recovery_hint"] = outcome.recovery_hint
        assert returned == {"error": expected}
