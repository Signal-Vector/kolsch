"""spec-119 Test 5 — full-envelope equivalence across all three arms.

The parity keystone: for **allowed / denied (`ToolUnauthorized`) /
errored (`PlatformError`)**, running ``_ObservedToolHandle.__call__``
through the REAL engine emit (``_Exchange._emit_void`` — so
``trace_id`` is stamped, the append runs under the per-thread lock,
and the bus publishes) persists the SAME ``Event`` sequence — full
envelope: ``type``, ``data`` (including the store-stamped
``logical_seq``), ``actor``, ``trace_id``, ordering via the
parent-event chain — and returns the same value as before the
refactor.

The ``_EXPECTED`` constants below were captured VERBATIM from the
pre-refactor ``_ObservedToolHandle`` (working tree at spec-119 start,
2026-08-02) by driving the identical harness; this test therefore
asserts the refactored observer against genuine pre-refactor output,
not against itself. This is what guarantees a future bridge's JSONL
can be made indistinguishable — parity is asserted, not assumed.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import BaseModel

from engine import ExecutionContext
from engine.engine import _Exchange
from engine.framework import ToolCall
from engine.observers import _ObservedToolHandle
from errors import ToolExecutionFailed, ToolUnauthorized
from tests.component.test_engine import _build_engine, _config
from tests.fixtures.mock_llm import MockLLM
from tools.decorator import ToolDefinition, ToolResult
from workflows.ids import TraceId

if TYPE_CHECKING:
    from pathlib import Path

_TRACE_ID = "tr_00000000-0000-0000-0000-0000000000ab"


class _PingArgs(BaseModel):
    message: str = ""


async def _noop_fn(*_: Any, **__: Any) -> Any:  # pragma: no cover — never invoked
    return None


class _StubOk:
    async def invoke_tool(self, **_: Any) -> ToolResult:
        return ToolResult(result="pong")


class _StubRaise:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def invoke_tool(self, **_: Any) -> ToolResult:
        raise self._exc


def _stub_for(arm: str) -> Any:
    if arm == "allowed":
        return _StubOk()
    if arm == "denied":
        return _StubRaise(
            ToolUnauthorized(
                tool_name="ping",
                action="kolsch/tools/ping",
                principal="agent.ag_test",
                matched_rule={
                    "what": "kolsch/tools/ping",
                    "pattern": "*",
                    "effect": "deny",
                },
            )
        )
    return _StubRaise(
        ToolExecutionFailed(
            tool_name="ping",
            error_class="BoomClass",
            recovery_hint="run: fix-it now",
        )
    )


# ---------------------------------------------------------------------------
# Pre-refactor capture (2026-08-02) — the expected full envelopes.
# ---------------------------------------------------------------------------

_DENIED_ERROR = {
    "cause": None,
    "detail": {
        "action": "kolsch/tools/ping",
        "matched_rule": {
            "effect": "deny",
            "pattern": "*",
            "what": "kolsch/tools/ping",
        },
        "principal": "agent.ag_test",
        "tool_name": "ping",
    },
    "message": "Authorization denied for tool 'ping'",
    "type": "tool.unauthorized",
    "where": "tools.dispatch.invoke",
}

_ERRORED_ERROR = {
    "cause": None,
    "detail": {"error_class": "BoomClass", "tool_name": "ping"},
    "message": "Tool 'ping' raised BoomClass",
    "type": "tool.execution_failed",
    "where": "tools.dispatch.execute",
}


def _called_event(tool_call_id: str) -> dict[str, Any]:
    return {
        "type": "tool.called",
        "data": {
            "arguments": {"message": "ack"},
            "logical_seq": 2,
            "tool_call_id": tool_call_id,
            "tool_name": "ping",
        },
        "actor_kind": "agent",
        "actor_id": "ag_test",
        "trace_id": _TRACE_ID,
    }


_EXPECTED: dict[str, dict[str, Any]] = {
    "allowed": {
        "all_types": ["session.opened", "tool.called", "tool.result"],
        "tool_events": [
            _called_event("tc_full_env_allowed"),
            {
                "type": "tool.result",
                "data": {
                    "is_error": False,
                    "logical_seq": 3,
                    "result": {"value": "pong"},
                    "tool_call_id": "tc_full_env_allowed",
                    "tool_name": "ping",
                },
                "actor_kind": "agent",
                "actor_id": "ag_test",
                "trace_id": _TRACE_ID,
            },
        ],
        "returned": "pong",
    },
    "denied": {
        "all_types": ["session.opened", "tool.called", "tool.failed"],
        "tool_events": [
            _called_event("tc_full_env_denied"),
            {
                "type": "tool.failed",
                "data": {
                    "error": _DENIED_ERROR,
                    "logical_seq": 3,
                    "recovery_hint": "/perm allow kolsch/tools/ping *",
                    "tool_call_id": "tc_full_env_denied",
                    "tool_name": "ping",
                },
                "actor_kind": "agent",
                "actor_id": "ag_test",
                "trace_id": _TRACE_ID,
            },
        ],
        "returned": {
            "error": {
                **_DENIED_ERROR,
                "recovery_hint": "/perm allow kolsch/tools/ping *",
            }
        },
    },
    "errored": {
        "all_types": ["session.opened", "tool.called", "tool.failed"],
        "tool_events": [
            _called_event("tc_full_env_errored"),
            {
                "type": "tool.failed",
                "data": {
                    "error": _ERRORED_ERROR,
                    "logical_seq": 3,
                    "recovery_hint": "run: fix-it now",
                    "tool_call_id": "tc_full_env_errored",
                    "tool_name": "ping",
                },
                "actor_kind": "agent",
                "actor_id": "ag_test",
                "trace_id": _TRACE_ID,
            },
        ],
        "returned": {"error": {**_ERRORED_ERROR, "recovery_hint": "run: fix-it now"}},
    },
}


@pytest.mark.parametrize("arm", ["allowed", "denied", "errored"])
async def test_observer_full_envelope_equivalence(arm: str, tmp_path: Path) -> None:
    """Persisted full envelope + return value identical to the
    pre-refactor capture, through the real engine ``_emit``."""
    expected = _EXPECTED[arm]
    tool_call_id = str(expected["tool_events"][0]["data"]["tool_call_id"])

    engine, store, _am = await _build_engine(tmp_path, MockLLM(script=[]))
    session = await store.create_session("ag_test", name="cap", trace_id=_TRACE_ID)
    threads = await store.list_threads(session.session_id)
    thread_id = threads[0].thread_id
    ctx = ExecutionContext(
        thread_id=thread_id,
        trigger={"kind": "user_message", "content": "x"},
        trace_id=TraceId(_TRACE_ID),
    )
    exchange = _Exchange(
        engine=engine,
        config=_config(),
        context=ctx,
        inputs={},
        queue=asyncio.Queue(),
    )
    exchange.session_id = str(session.session_id)

    handle = _ObservedToolHandle(
        emit=exchange._emit_void,
        agent_id="ag_test",
        session_id=str(session.session_id),
        thread_id=str(thread_id),
        tools_subsystem=_stub_for(arm),
        defn=ToolDefinition(
            name="ping",
            description="d",
            args_schema=_PingArgs,
            func=_noop_fn,
        ),
        cancel=None,
        register_span=exchange.register_span,
        close_span=exchange.close_span,
    )

    returned = await handle(
        ToolCall(
            name="ping",
            arguments={"message": "ack"},
            tool_call_id=tool_call_id,
        )
    )

    events = [e async for e in store.read_path_to_leaf(thread_id)]

    # Ordering — the persisted path is a strict parent chain.
    assert [e.type.value for e in events] == expected["all_types"]
    for i in range(1, len(events)):
        assert events[i].parent_event_id == events[i - 1].event_id

    # Full envelope: type, data (incl. logical_seq), actor, trace_id.
    actual_tool_events = [
        {
            "type": e.type.value,
            "data": e.data,
            "actor_kind": e.actor.kind,
            "actor_id": e.actor.id,
            "trace_id": e.trace_id,
        }
        for e in events
        if e.type.value.startswith("tool.")
    ]
    assert actual_tool_events == expected["tool_events"]

    # Same framework-visible return value as pre-refactor.
    assert returned == expected["returned"]

    # Span registry drained — no dangling span for the exchange
    # finally to double-terminate.
    assert list(exchange._open_spans.keys()) == []
