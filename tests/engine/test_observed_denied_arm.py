"""spec-119 Test 4 — the engine wrapper's denied arm (previously untested).

Pins ``_ObservedToolHandle.__call__`` on ``ToolUnauthorized``:

- returns ``{"error": {..., "recovery_hint": "/perm allow ..."}}`` —
  the security-sensitive Diagnostic mapping the framework forwards to
  the model;
- emits ``tool.failed`` carrying the SAME hint on the event data;
- registers then closes the span (spec-068 registry discipline);

plus that the cancel arm still emits the ``session.interrupted``
terminal (``actor=system``) and re-raises, with the span closed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from engine.framework import ToolCall
from engine.observers import _ObservedToolHandle
from errors import ToolUnauthorized
from schemas import EventType
from tools.decorator import ToolDefinition, ToolResult

_HINT = "/perm allow kolsch/tools/ping *"


class _PingArgs(BaseModel):
    message: str = ""


async def _noop_fn(*_: Any, **__: Any) -> Any:  # pragma: no cover — never invoked
    return None


class _RaisingSubsystem:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def invoke_tool(self, **_: Any) -> ToolResult:
        raise self._exc


class _SpanRecorder:
    """Records register/close calls in order, like the exchange registry."""

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.closed: list[str] = []

    def register_span(self, span_id: str, _factory: Any) -> None:
        self.registered.append(span_id)

    def close_span(self, span_id: str) -> None:
        self.closed.append(span_id)


def _handle(
    subsystem: Any,
    emitted: list[dict[str, Any]],
    spans: _SpanRecorder,
) -> _ObservedToolHandle:
    async def _capture_emit(**kwargs: Any) -> None:
        emitted.append(kwargs)

    return _ObservedToolHandle(
        emit=_capture_emit,
        agent_id="ag_test",
        session_id="ses_00000000-0000-0000-0000-000000000001",
        thread_id="th_00000000-0000-0000-0000-000000000002",
        tools_subsystem=subsystem,
        defn=ToolDefinition(
            name="ping",
            description="d",
            args_schema=_PingArgs,
            func=_noop_fn,
        ),
        register_span=spans.register_span,
        close_span=spans.close_span,
    )


async def test_observed_denied_arm_returns_error_payload_with_hint() -> None:
    """Denied call → ``{"error": {..., "recovery_hint": ...}}`` return,
    ``tool.failed`` event with the same hint, span registered+closed."""
    exc = ToolUnauthorized(
        tool_name="ping",
        action="kolsch/tools/ping",
        principal="agent.ag_test",
        matched_rule={
            "what": "kolsch/tools/ping",
            "pattern": "*",
            "effect": "deny",
        },
    )
    emitted: list[dict[str, Any]] = []
    spans = _SpanRecorder()
    handle = _handle(_RaisingSubsystem(exc), emitted, spans)

    result = await handle(
        ToolCall(name="ping", arguments={"message": "ack"}, tool_call_id="tc_deny")
    )

    # Return mapping — the hint rides INSIDE the error payload.
    assert isinstance(result, dict)
    assert result["error"]["type"] == "tool.unauthorized"
    assert result["error"]["recovery_hint"] == _HINT

    # Event — same hint on the typed data field.
    assert [e["type_"] for e in emitted] == [
        EventType.TOOL_CALLED,
        EventType.TOOL_FAILED,
    ]
    assert emitted[1]["data"]["recovery_hint"] == _HINT
    assert emitted[1]["data"]["error"]["type"] == "tool.unauthorized"
    assert emitted[1]["actor"].kind == "agent"

    # Span discipline — registered after tool.called, closed on exit.
    assert spans.registered == ["tc_deny"]
    assert spans.closed == ["tc_deny"]


async def test_observed_denied_arm_cancel_terminal_still_emits_and_reraises() -> None:
    """The cancel arm stays in the wrapper: ``CancelledError`` from
    ``invoke_tool`` → ``tool.failed(session.interrupted)`` terminal
    (``actor=system``), span closed, cancel re-raised."""
    emitted: list[dict[str, Any]] = []
    spans = _SpanRecorder()
    handle = _handle(_RaisingSubsystem(asyncio.CancelledError()), emitted, spans)

    with pytest.raises(asyncio.CancelledError):
        await handle(
            ToolCall(
                name="ping",
                arguments={"message": "ack"},
                tool_call_id="tc_cancel",
            )
        )

    assert [e["type_"] for e in emitted] == [
        EventType.TOOL_CALLED,
        EventType.TOOL_FAILED,
    ]
    terminal = emitted[1]
    assert terminal["data"]["error"]["type"] == "session.interrupted"
    assert terminal["data"]["tool_call_id"] == "tc_cancel"
    assert terminal["actor"].kind == "system"

    assert spans.registered == ["tc_cancel"]
    assert spans.closed == ["tc_cancel"]
