"""Phase 3.5 Step 0 — event kind + emit hook + replay extension.

Covers Step 0 ACs:

- 0.1 — :attr:`WorkflowEventKind.STATE_MUTATED` wire string.
- 0.2 — :class:`WorkflowStateMutatedEvent` /
  :class:`WorkflowStateMutatedPayload` shape + serialisation.
- 0.3 — :meth:`NodeContext.emit_workflow_event` dispatches to the
  bound callback; raises :class:`RuntimeError` when unbound.
- 0.4 — wiring exercised indirectly via Step 1+; the Step 0 unit
  test stubs the callback to confirm the surface contract.
- 0.5 — :func:`replay_into` applies ``set`` / ``delete`` ops in
  JSONL order; silent on absent delete; multi-write last-wins.
"""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from assets.catalog import AssetCatalog
from workflows._replay import replay_into
from workflows.context import NodeContext
from workflows.events import (
    WorkflowEventKind,
    WorkflowStateMutatedEvent,
    WorkflowStateMutatedPayload,
)
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.ids import TraceId, WorkflowRunId
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrk() -> WorkflowRunId:
    return WorkflowRunId(f"wrk_{uuid.uuid4()}")


def _tr() -> TraceId:
    return TraceId(f"tr_{uuid.uuid4()}")


def _make_event(
    *,
    key: str = "x",
    op: str = "set",
    value: Any = 1,
) -> WorkflowStateMutatedEvent:
    return WorkflowStateMutatedEvent(
        workflow_run_id=_wrk(),
        trace_id=_tr(),
        payload=WorkflowStateMutatedPayload(
            key=key,
            op=op,  # type: ignore[arg-type]
            value=value,
        ),
    )


# ---------------------------------------------------------------------------
# AC 0.1 — enum wire string
# ---------------------------------------------------------------------------


class TestEnumWireString:
    def test_state_mutated_value(self) -> None:
        assert WorkflowEventKind.STATE_MUTATED.value == "workflow.state.mutated"


# ---------------------------------------------------------------------------
# AC 0.2 — event class shape
# ---------------------------------------------------------------------------


class TestStateMutatedEventShape:
    def test_construct_and_round_trip(self) -> None:
        ev = _make_event(key="attempts", op="set", value=3)
        serialised = ev.model_dump(mode="json")
        assert serialised["kind"] == "workflow.state.mutated"
        assert serialised["payload"] == {
            "key": "attempts",
            "op": "set",
            "value": 3,
        }
        # JSON round-trip — the on-disk JSONL line is exactly this.
        rehydrated = json.loads(json.dumps(serialised))
        assert rehydrated["payload"]["key"] == "attempts"

    def test_delete_op_value_none(self) -> None:
        ev = _make_event(key="foo", op="delete", value=None)
        assert ev.payload.op == "delete"
        assert ev.payload.value is None

    def test_payload_frozen_rejects_mutation(self) -> None:
        ev = _make_event()
        with pytest.raises(ValidationError):
            ev.payload.key = "y"  # type: ignore[misc]

    def test_payload_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowStateMutatedPayload(
                key="x",
                op="set",
                value=1,
                extra_field="nope",  # type: ignore[call-arg]
            )

    def test_payload_op_rejects_other_strings(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowStateMutatedPayload(
                key="x",
                op="update",  # type: ignore[arg-type]
                value=1,
            )


# ---------------------------------------------------------------------------
# AC 0.3 — NodeContext.emit_workflow_event hook
# ---------------------------------------------------------------------------


class TestEmitWorkflowEventHook:
    def _ctx(
        self,
        *,
        callback: Any = None,
    ) -> NodeContext:
        return NodeContext(
            workflow_run_id="wrk_test",
            invocation_id="inv_test",
            workspace_root=Path(tempfile.mkdtemp(prefix="kolsch-stevt-")),
            log=logging.getLogger("workflows.test"),
            secrets={},
            state={},
            bundle="kolsch",
            emit_fn=lambda *_: None,
            emit_workflow_event_callback=callback,
        )

    async def test_dispatches_to_callback(self) -> None:
        captured: list[Any] = []

        async def _cb(event: Any) -> str:
            captured.append(event)
            return event.event_id

        ctx = self._ctx(callback=_cb)
        ev = _make_event(key="x", op="set", value=42)
        returned = await ctx.emit_workflow_event(ev)
        assert returned == ev.event_id
        assert captured == [ev]

    async def test_raises_when_callback_unbound(self) -> None:
        ctx = self._ctx(callback=None)
        with pytest.raises(RuntimeError, match="emit_workflow_event"):
            await ctx.emit_workflow_event(_make_event())


# ---------------------------------------------------------------------------
# AC 0.5 — replay_into applies state mutations
# ---------------------------------------------------------------------------


def _make_replay_executor(tmp_path: Path) -> IterativeExecutor:
    """A minimal executor for feeding synthetic events into replay_into."""
    wf = Workflow.model_validate({"nodes": {"x": {"type": "kolsch/nodes/noop"}}})
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x")
    return IterativeExecutor(
        workflow=wf,
        run=run,
        store=store,
        registry=RunnerRegistry(),
        catalog=AssetCatalog(),
    )


def _state_event(*, key: str, op: str, value: Any = None) -> dict[str, Any]:
    """Build the on-disk dict shape replay_into walks."""
    return {
        "kind": "workflow.state.mutated",
        "payload": {"key": key, "op": op, "value": value},
    }


class TestReplayAppliesStateMutations:
    def test_set_writes_value(self, tmp_path: Path) -> None:
        ex = _make_replay_executor(tmp_path)
        ex._state = {"x": 1}
        replay_into(ex, [_state_event(key="x", op="set", value=2)])
        assert ex._state["x"] == 2

    def test_delete_pops_existing(self, tmp_path: Path) -> None:
        ex = _make_replay_executor(tmp_path)
        ex._state = {"x": 1}
        replay_into(ex, [_state_event(key="x", op="delete")])
        assert "x" not in ex._state

    def test_delete_silent_on_absent(self, tmp_path: Path) -> None:
        ex = _make_replay_executor(tmp_path)
        ex._state = {}
        # No exception — replay matches DeleteStateRunner's silent semantics.
        replay_into(ex, [_state_event(key="missing", op="delete")])
        assert ex._state == {}

    def test_in_order_last_set_wins(self, tmp_path: Path) -> None:
        ex = _make_replay_executor(tmp_path)
        ex._state = {}
        events = [
            _state_event(key="x", op="set", value=1),
            _state_event(key="x", op="set", value=2),
            _state_event(key="x", op="delete"),
            _state_event(key="x", op="set", value=3),
        ]
        replay_into(ex, events)
        assert ex._state["x"] == 3

    def test_multi_key_independent(self, tmp_path: Path) -> None:
        ex = _make_replay_executor(tmp_path)
        ex._state = {}
        events = [
            _state_event(key="a", op="set", value=1),
            _state_event(key="b", op="set", value=[1, 2, 3]),
            _state_event(key="a", op="delete"),
        ]
        replay_into(ex, events)
        assert ex._state == {"b": [1, 2, 3]}

    def test_unknown_op_ignored(self, tmp_path: Path) -> None:
        # Defensive — a future event kind extension shouldn't poison
        # in-flight resumes (replay's forward-compat promise).
        ex = _make_replay_executor(tmp_path)
        ex._state = {"x": 1}
        replay_into(
            ex,
            [
                {
                    "kind": "workflow.state.mutated",
                    "payload": {"key": "x", "op": "increment", "value": 1},
                }
            ],
        )
        assert ex._state == {"x": 1}
