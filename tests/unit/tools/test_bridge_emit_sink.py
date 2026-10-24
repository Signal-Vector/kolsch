"""spec-120 Test 5 — bridge emit-sink parity (queue-less, trace-aware).

The audit keystone: driving ``invoke_observed`` with
``make_bridge_emit_sink(...)`` persists ``tool.*`` events whose data +
envelope — ``type``, ``data`` (including the store-stamped
``logical_seq``), ``actor``, ``trace_id``, and ordering via the
store-assigned ``parent_event_id`` chain — EQUAL an in-engine call
through the real ``_Exchange._emit_void`` for all three arms
(allowed / denied / errored), while:

- performing **no** live-UI queue operation (the engine's queue gets
  the events; the bridge sink structurally has no queue — asserted by
  inspecting the closure), and
- stamping ``trace_id`` from the bound context.

Mirrors ``tests/engine/test_observer_full_envelope_equivalence.py``:
the engine arm here IS the real emit path (per-thread lock, store
flock ``logical_seq`` allocation, bus publish), so any divergence in
the bridge sink's JSONL is a test failure, not an emergent surprise.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from tests.component.test_engine import _build_engine, _config
from tests.fixtures.mock_llm import MockLLM

from bus import InProcessEventBus
from engine import ExecutionContext
from engine.engine import _Exchange
from errors import ToolExecutionFailed, ToolUnauthorized
from schemas import CallerChain
from store import SessionStore, SqliteBackend
from tools import invoke_observed
from tools.bridge import make_bridge_emit_sink
from tools.decorator import ToolResult
from workflows.ids import TraceId

if TYPE_CHECKING:
    from pathlib import Path

    from schemas import Event

_TRACE = "tr_00000000-0000-0000-0000-0000000000ab"
_AGENT_ID = "ag_test"
_TOOL_CALL_ID = "tc_parity"


# ---------------------------------------------------------------------------
# Stubs — same shapes as tests/unit/tools/test_observed_core.py.
# ---------------------------------------------------------------------------


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
                principal=f"agent.{_AGENT_ID}",
                matched_rule={
                    "what": "kolsch/tools/ping",
                    "pattern": "*",
                    "effect": "deny",
                },
                caller_chain=CallerChain.unattributed(),
            )
        )
    return _StubRaise(
        ToolExecutionFailed(
            tool_name="ping",
            error_class="BoomClass",
            recovery_hint="run: fix-it now",
        )
    )


async def _invoke(emit: Any, subsystem: Any, *, session_id: str, thread_id: str) -> Any:
    return await invoke_observed(
        emit=emit,
        agent_id=_AGENT_ID,
        session_id=session_id,
        thread_id=thread_id,
        tools_subsystem=subsystem,
        tool_name="ping",
        args={"message": "ack"},
        tool_call_id=_TOOL_CALL_ID,
        cancel=None,
        caller_chain=CallerChain.unattributed(),
    )


def _tool_envelopes(events: list[Event]) -> list[dict[str, Any]]:
    """Project the comparison surface: full envelope minus the ids.

    ``event_id`` is a fresh uuid per emit and ``created_at`` is a
    wall-clock stamp — everything else (type, data INCLUDING the
    store-stamped ``logical_seq``, actor, trace) must match exactly.
    """
    return [
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


@pytest.mark.parametrize("arm", ["allowed", "denied", "errored"])
async def test_bridge_sink_parity_with_engine_emit(arm: str, tmp_path: Path) -> None:
    """Persisted ``tool.*`` events through the bridge sink equal the
    real engine emit's — same data, actor, ordering, ``trace_id``,
    ``parent_event_id`` chaining, and ``logical_seq`` — minus the
    live-UI queue, which only the engine touches."""
    # ---- Engine arm: the REAL `_Exchange._emit_void`. -----------------
    engine_root = tmp_path / "engine"
    engine_root.mkdir()
    engine, engine_store, _am = await _build_engine(engine_root, MockLLM(script=[]))
    e_session = await engine_store.create_session(
        _AGENT_ID, name="parity", trace_id=_TRACE
    )
    e_thread = (await engine_store.list_threads(e_session.session_id))[0].thread_id
    queue: asyncio.Queue[Any] = asyncio.Queue()
    exchange = _Exchange(
        engine=engine,
        config=_config(),
        context=ExecutionContext(
            thread_id=e_thread,
            trigger={"kind": "user_message", "content": "x"},
            trace_id=TraceId(_TRACE),
        ),
        inputs={},
        queue=queue,
    )
    exchange.session_id = str(e_session.session_id)

    engine_outcome = await _invoke(
        exchange._emit_void,
        _stub_for(arm),
        session_id=str(e_session.session_id),
        thread_id=str(e_thread),
    )
    engine_events = [e async for e in engine_store.read_path_to_leaf(e_thread)]

    # ---- Bridge arm: `make_bridge_emit_sink` — NO engine anywhere. ----
    bridge_root = tmp_path / "bridge"
    bridge_root.mkdir()
    backend = SqliteBackend(bridge_root / ".platform" / "entities.db")
    await backend.initialize()
    bridge_store = SessionStore(root=bridge_root, backend=backend)
    b_session = await bridge_store.create_session(
        _AGENT_ID, name="parity", trace_id=_TRACE
    )
    b_thread = (await bridge_store.list_threads(b_session.session_id))[0].thread_id

    bus = InProcessEventBus()
    published: list[Event] = []

    async def _record(event: Event) -> None:
        published.append(event)

    await bus.subscribe("tool.*", _record)

    sink = make_bridge_emit_sink(
        store=bridge_store,
        bus=bus,
        lock=asyncio.Lock(),
        session_id=str(b_session.session_id),
        thread_id=str(b_thread),
        trace_id=_TRACE,
    )
    bridge_outcome = await _invoke(
        sink,
        _stub_for(arm),
        session_id=str(b_session.session_id),
        thread_id=str(b_thread),
    )
    bridge_events = [e async for e in bridge_store.read_path_to_leaf(b_thread)]

    # ---- Parity: full tool.* envelope, byte-equal. --------------------
    assert _tool_envelopes(bridge_events) == _tool_envelopes(engine_events)
    assert len(_tool_envelopes(bridge_events)) == 2

    # Whole-log ordering identical (session.opened, tool.called, then
    # the arm's terminal) and the persisted path is a strict
    # store-assigned parent chain in BOTH stores.
    assert [e.type.value for e in bridge_events] == [
        e.type.value for e in engine_events
    ]
    for events in (engine_events, bridge_events):
        for i in range(1, len(events)):
            assert events[i].parent_event_id == events[i - 1].event_id

    # `logical_seq` is store-assigned under the flock — cannot diverge
    # when the sink appends via `store.append_event`; pin it anyway.
    assert [
        e.data["logical_seq"] for e in bridge_events if e.type.value.startswith("tool.")
    ] == [2, 3]

    # trace_id stamped from the bound context on every bridge event.
    assert all(e.trace_id == _TRACE for e in bridge_events)

    # ---- The ONE intended difference: the live-UI queue. --------------
    # The engine emit queued both tool events for its chat surface;
    # the bridge sink performed no queue operation — structurally it
    # closes over store/bus/lock/ids only, never a queue.
    assert queue.qsize() == 2
    closure = sink.__closure__ or ()
    assert all(not isinstance(cell.cell_contents, asyncio.Queue) for cell in closure)

    # Bus publish is engine-identical (best-effort, same events).
    assert [e.type.value for e in published] == [
        e["type"] for e in _tool_envelopes(bridge_events)
    ]

    # Same typed outcome from the core either way.
    assert type(bridge_outcome) is type(engine_outcome)
