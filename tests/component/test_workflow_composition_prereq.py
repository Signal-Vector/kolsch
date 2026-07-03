"""Phase 3.7 Step 0 — composition prereq plumbing tests.

Covers ACs 0.1-0.8 of ``process/phase3.7-composition-build.md``:

- 0.1 ``ParentRunPointer`` — frozen + ``extra="forbid"`` + carries
  ``workflow_run_id`` / ``invocation_id``.
- 0.2 ``WorkflowRun.parent`` — backward-compat default ``None``.
- 0.3 ``WorkflowRunStore.create(parent=)`` — round-trips through
  ``meta.json`` (serialized as a NESTED dict).
- 0.4 Executor ``_call_depth`` / ``_call_chain`` constructor kwargs;
  ``_call_depth > 16`` raises :class:`WorkflowExtendsDepthExceeded`
  AT CONSTRUCTION — not at ``run`` time.
- 0.5 ``NodeContext.derive_for_iteration`` mints a fresh
  ``invocation_id`` and carries iteration metadata.
- 0.6 Two new event kinds (``CALLED_WORKFLOW`` /
  ``CALLED_WORKFLOW_COMPLETED``) serialise + deserialise cleanly.
- 0.7 ``workflow.node.completed`` payload extension — ``child_run_id``
  appears for ``kolsch/nodes/workflow`` nodes ONLY when
  ``ctx.spawned_run_id`` is set; other node types unaffected.
- 0.8 No regressions — pre-Step-0 callers that don't pass the new
  kwargs continue to construct cleanly.

NO ``WorkflowSubRunner`` / ``MapRunner`` work here — those land in
Steps 1 + 2. The tests construct the executor with FAKE runners so
the payload-builder behaviour can be exercised without the Step 1
runtime.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from assets.catalog import AssetCatalog
from schemas.entities import ParentRunPointer
from workflows.context import NodeContext
from workflows.errors import WorkflowExtendsDepthExceeded
from workflows.events import (
    WorkflowEventKind,
    WorkflowNodeCalledWorkflowCompletedEvent,
    WorkflowNodeCalledWorkflowCompletedPayload,
    WorkflowNodeCalledWorkflowEvent,
    WorkflowNodeCalledWorkflowPayload,
)
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.ids import InvocationId, TraceId, WorkflowRunId
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

    from workflows.run import WorkflowRun

_RUN_ID_A = WorkflowRunId("wrk_3b9e7e84-9a1d-4ce6-b6c1-2a4f8a91d101")
_INV_ID_A = InvocationId("inv_2c8c01fa-4b7a-4c0c-b5fb-c19c1c0d3401")


# ---------------------------------------------------------------------------
# AC 0.1 — ParentRunPointer
# ---------------------------------------------------------------------------


class TestParentRunPointer:
    def test_frozen_and_carries_two_ids(self) -> None:
        ptr = ParentRunPointer(
            workflow_run_id=_RUN_ID_A,
            invocation_id=_INV_ID_A,
        )
        assert ptr.workflow_run_id == _RUN_ID_A
        assert ptr.invocation_id == _INV_ID_A
        with pytest.raises((ValidationError, TypeError)):
            ptr.workflow_run_id = WorkflowRunId(  # type: ignore[misc]
                "wrk_99999999-9999-4999-9999-999999999999"
            )

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ParentRunPointer(
                workflow_run_id=_RUN_ID_A,
                invocation_id=_INV_ID_A,
                bogus="nope",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# AC 0.2 / 0.3 — WorkflowRun.parent default + store round-trip
# ---------------------------------------------------------------------------


class TestParentRoundTrip:
    def test_default_parent_is_none(self, tmp_path: Path) -> None:
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/x", inputs={})
        assert run.parent is None
        reread = store.read(run.run_id)
        assert reread.parent is None

    def test_parent_round_trips_as_nested_dict(self, tmp_path: Path) -> None:
        parent_ptr = ParentRunPointer(
            workflow_run_id=_RUN_ID_A, invocation_id=_INV_ID_A
        )
        store = WorkflowRunStore(tmp_path)
        run = store.create(
            workflow_ref="demo/workflows/child",
            inputs={"k": 1},
            parent=parent_ptr,
        )
        # On-disk meta.json carries ``parent`` as a NESTED dict (Step 0
        # AC 0.3 — reviewer pays extra attention to this: NOT a flat
        # string, NOT serialised through a custom encoder).
        meta_path = tmp_path / "runs" / str(run.run_id) / "meta.json"
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        assert isinstance(raw["parent"], dict)
        assert raw["parent"]["workflow_run_id"] == str(_RUN_ID_A)
        assert raw["parent"]["invocation_id"] == str(_INV_ID_A)
        # And ``read`` reconstructs the typed ParentRunPointer.
        reread = store.read(run.run_id)
        assert reread.parent == parent_ptr

    def test_parent_survives_update_status(self, tmp_path: Path) -> None:
        from workflows.run import WorkflowRunStatus

        parent_ptr = ParentRunPointer(
            workflow_run_id=_RUN_ID_A, invocation_id=_INV_ID_A
        )
        store = WorkflowRunStore(tmp_path)
        run = store.create(
            workflow_ref="demo/workflows/child",
            inputs={},
            parent=parent_ptr,
        )
        store.update_status(run.run_id, WorkflowRunStatus.WAITING_FOR_INPUT)
        reread = store.read(run.run_id)
        assert reread.parent == parent_ptr


# ---------------------------------------------------------------------------
# AC 0.4 / 0.8 — Executor nesting kwargs + depth cap
# ---------------------------------------------------------------------------


def _trivial_workflow() -> Workflow:
    return Workflow.model_validate({"nodes": {"only": {"type": "kolsch/nodes/noop"}}})


def _make_run(store: WorkflowRunStore) -> WorkflowRun:
    return store.create(workflow_ref="demo/workflows/x", inputs={})


class TestExecutorNestingKwargs:
    def test_defaults_preserve_phase_3_1_signature(self, tmp_path: Path) -> None:
        # AC 0.8 — pre-Step-0 callers that don't pass the new kwargs
        # MUST construct cleanly. ``_call_depth`` defaults to ``0``;
        # ``_call_chain`` defaults to ``()`` which the executor
        # auto-fills to ``(self._run.workflow_ref,)`` so the Step 1
        # cycle check sees this run's ref in the chain (catches direct
        # A→A on the first would-be call).
        store = WorkflowRunStore(tmp_path)
        run = _make_run(store)
        ex = IterativeExecutor(
            workflow=_trivial_workflow(),
            run=run,
            store=store,
            registry=RunnerRegistry(),
            catalog=AssetCatalog(),
        )
        assert ex._call_depth == 0
        assert ex._call_chain == (run.workflow_ref,)

    def test_depth_at_limit_constructs_cleanly(self, tmp_path: Path) -> None:
        # depth = 16 is the boundary — the executor MUST construct.
        store = WorkflowRunStore(tmp_path)
        run = _make_run(store)
        ex = IterativeExecutor(
            workflow=_trivial_workflow(),
            run=run,
            store=store,
            registry=RunnerRegistry(),
            catalog=AssetCatalog(),
            _call_depth=16,
            _call_chain=("a",) * 16,
        )
        assert ex._call_depth == 16
        assert ex._call_chain == ("a",) * 16

    def test_depth_seventeen_rejected_at_construction(self, tmp_path: Path) -> None:
        # AC 0.4 reviewer note — the check fires at construction, NOT
        # at ``run`` time, so a nested executor that would exceed depth
        # never starts work.
        store = WorkflowRunStore(tmp_path)
        run = _make_run(store)
        with pytest.raises(WorkflowExtendsDepthExceeded) as exc_info:
            IterativeExecutor(
                workflow=_trivial_workflow(),
                run=run,
                store=store,
                registry=RunnerRegistry(),
                catalog=AssetCatalog(),
                _call_depth=17,
            )
        assert exc_info.value.depth == 17
        assert exc_info.value.limit == 16


# ---------------------------------------------------------------------------
# AC 0.5 — NodeContext.derive_for_iteration
# ---------------------------------------------------------------------------


_PARENT_INV = InvocationId("inv_77777777-7777-4777-8777-777777777777")


class TestDeriveForIteration:
    def test_fresh_invocation_id_and_iteration_metadata(self) -> None:
        # NodeContext.for_test accepts free-form string ids — its own
        # field is plain str, not the PrefixedId type — but the new
        # ``parent_invocation_id`` field is typed ``InvocationId | None``
        # so we use a real UUID-backed id there.
        parent_ctx = NodeContext.for_test(invocation_id=str(_PARENT_INV))
        derived = parent_ctx.derive_for_iteration(
            parent_invocation_id=_PARENT_INV,
            iteration_index=0,
            iteration_total=3,
        )
        # Fresh invocation id — non-empty + distinct from the map's.
        assert derived.invocation_id != parent_ctx.invocation_id
        assert derived.invocation_id.startswith("inv_")
        # Iteration metadata stamped.
        assert derived.parent_invocation_id == _PARENT_INV
        assert derived.iteration_index == 0
        assert derived.iteration_total == 3
        # Other fields inherited verbatim.
        assert derived.workflow_run_id == parent_ctx.workflow_run_id
        assert derived.bundle == parent_ctx.bundle
        assert derived.workspace_root == parent_ctx.workspace_root

    def test_each_iteration_gets_distinct_invocation_id(self) -> None:
        parent_ctx = NodeContext.for_test(invocation_id=str(_PARENT_INV))
        a = parent_ctx.derive_for_iteration(
            parent_invocation_id=_PARENT_INV,
            iteration_index=0,
            iteration_total=2,
        )
        b = parent_ctx.derive_for_iteration(
            parent_invocation_id=_PARENT_INV,
            iteration_index=1,
            iteration_total=2,
        )
        assert a.invocation_id != b.invocation_id


# ---------------------------------------------------------------------------
# AC 0.6 — two new event kinds round-trip
# ---------------------------------------------------------------------------


class TestNewEventKinds:
    def test_called_workflow_enum_values(self) -> None:
        assert (
            WorkflowEventKind.CALLED_WORKFLOW.value == "workflow.node.called_workflow"
        )
        assert (
            WorkflowEventKind.CALLED_WORKFLOW_COMPLETED.value
            == "workflow.node.called_workflow_completed"
        )

    def test_called_workflow_event_round_trip(self) -> None:
        evt = WorkflowNodeCalledWorkflowEvent(
            workflow_run_id=_RUN_ID_A,
            trace_id=TraceId("tr_5a7e1ef0-1111-4111-9111-111111111111"),
            parent_event_id=None,
            invocation_id=_INV_ID_A,
            node_name="sub",
            payload=WorkflowNodeCalledWorkflowPayload(
                node_name="sub",
                invocation_id=str(_INV_ID_A),
                child_workflow_run_id="wrk_child",
                child_workflow_ref="demo/workflows/child",
            ),
        )
        dumped = evt.model_dump(mode="json")
        assert dumped["kind"] == "workflow.node.called_workflow"
        assert dumped["payload"]["child_workflow_run_id"] == "wrk_child"
        reloaded = WorkflowNodeCalledWorkflowEvent.model_validate(dumped)
        assert reloaded.payload.child_workflow_ref == "demo/workflows/child"

    def test_called_workflow_completed_event_round_trip(self) -> None:
        evt = WorkflowNodeCalledWorkflowCompletedEvent(
            workflow_run_id=_RUN_ID_A,
            trace_id=TraceId("tr_5a7e1ef0-2222-4222-9222-222222222222"),
            parent_event_id=None,
            invocation_id=_INV_ID_A,
            node_name="sub",
            payload=WorkflowNodeCalledWorkflowCompletedPayload(
                node_name="sub",
                invocation_id=str(_INV_ID_A),
                child_workflow_run_id="wrk_child",
                child_status="waiting_for_input",
            ),
        )
        dumped = evt.model_dump(mode="json")
        assert dumped["kind"] == "workflow.node.called_workflow_completed"
        assert dumped["payload"]["child_status"] == "waiting_for_input"
        reloaded = WorkflowNodeCalledWorkflowCompletedEvent.model_validate(dumped)
        assert reloaded.payload.child_workflow_run_id == "wrk_child"

    def test_called_workflow_payload_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowNodeCalledWorkflowPayload(
                node_name="sub",
                invocation_id="inv_x",
                child_workflow_run_id="wrk_x",
                child_workflow_ref="demo/workflows/x",
                rogue="nope",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# AC 0.7 — workflow.node.completed carries child_run_id for workflow nodes
# ---------------------------------------------------------------------------


class _SpawnedRunStampingRunner:
    """A fake runner that stamps ``ctx.spawned_run_id`` then returns.

    Mimics what :class:`workflows.builtins.workflow_sub.WorkflowSubRunner`
    will do in Step 1: mutate the frozen :class:`NodeContext` via
    :func:`object.__setattr__` (the same escape hatch the agent runner
    uses for ``spawned_session_id``). The payload-builder gate in the
    executor reads the attribute right after the runner returns.
    """

    def __init__(self, *, run_id: WorkflowRunId | None) -> None:
        self._run_id = run_id

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        _ = (node_name, node_def, bound_inputs, state)
        if self._run_id is not None:
            object.__setattr__(ctx, "spawned_run_id", self._run_id)
        return {"ok": True}


class _StaticRunner:
    def __init__(self, output: dict[str, Any]) -> None:
        self._output = output

    async def run(self, **_: Any) -> dict[str, Any]:
        return dict(self._output)


class TestWorkflowNodeCompletedPayload:
    async def test_child_run_id_appears_for_workflow_node(self, tmp_path: Path) -> None:
        # A single-node workflow whose node is ``kolsch/nodes/workflow``;
        # the fake runner stamps ``spawned_run_id`` so the executor's
        # payload builder appends ``child_run_id``.
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "sub": {
                        "type": "kolsch/nodes/workflow",
                        "ref": "demo/workflows/child",
                    },
                },
            }
        )
        child_id = WorkflowRunId("wrk_00000000-0000-4000-8000-000000000001")
        registry = RunnerRegistry()
        registry.register(
            "kolsch/nodes/workflow",
            _SpawnedRunStampingRunner(run_id=child_id),
        )
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/parent", inputs={})
        ex = IterativeExecutor(
            workflow=wf,
            run=run,
            store=store,
            registry=registry,
            catalog=AssetCatalog(),
        )
        await ex.run()
        events = list(store.read_events(run.run_id))
        completed = [e for e in events if e["kind"] == "workflow.node.completed"]
        assert len(completed) == 1
        assert completed[0]["payload"]["child_run_id"] == str(child_id)

    async def test_no_child_run_id_when_runner_did_not_stamp(
        self, tmp_path: Path
    ) -> None:
        # Same workflow-node shape, runner returns WITHOUT stamping.
        # The gate is ``spawned_run_id is not None`` — absent attribute
        # MUST NOT inject the field (would poison forensics).
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "sub": {
                        "type": "kolsch/nodes/workflow",
                        "ref": "demo/workflows/child",
                    },
                },
            }
        )
        registry = RunnerRegistry()
        registry.register(
            "kolsch/nodes/workflow",
            _SpawnedRunStampingRunner(run_id=None),
        )
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/parent", inputs={})
        ex = IterativeExecutor(
            workflow=wf,
            run=run,
            store=store,
            registry=registry,
            catalog=AssetCatalog(),
        )
        await ex.run()
        events = list(store.read_events(run.run_id))
        completed = [e for e in events if e["kind"] == "workflow.node.completed"]
        assert len(completed) == 1
        assert "child_run_id" not in completed[0]["payload"]

    async def test_non_workflow_nodes_never_carry_child_run_id(
        self, tmp_path: Path
    ) -> None:
        # AC 0.7 reviewer note — the payload gate is BOTH ``type ==
        # kolsch/nodes/workflow`` AND ``spawned_run_id is not None``.
        # A future runner that mistakenly stamps the attribute on a
        # non-workflow node MUST NOT pollute the payload.
        wf = Workflow.model_validate({"nodes": {"only": {"type": "kolsch/nodes/noop"}}})

        class _NoopStamper:
            async def run(
                self,
                *,
                node_name: str,
                node_def: Any,
                bound_inputs: dict[str, Any],
                state: dict[str, Any],
                ctx: NodeContext,
            ) -> dict[str, Any]:
                _ = (node_name, node_def, bound_inputs, state)
                object.__setattr__(
                    ctx,
                    "spawned_run_id",
                    WorkflowRunId("wrk_00000000-0000-4000-8000-000000000099"),
                )
                return {}

        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", _NoopStamper())
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/parent", inputs={})
        ex = IterativeExecutor(
            workflow=wf,
            run=run,
            store=store,
            registry=registry,
            catalog=AssetCatalog(),
        )
        await ex.run()
        events = list(store.read_events(run.run_id))
        completed = [e for e in events if e["kind"] == "workflow.node.completed"]
        assert len(completed) == 1
        assert "child_run_id" not in completed[0]["payload"]


# ---------------------------------------------------------------------------
# Sanity — NodeContext from executor carries call_depth / call_chain / workflow
# ---------------------------------------------------------------------------


class TestNodeContextNestingFields:
    async def test_runner_sees_executor_nesting_metadata(self, tmp_path: Path) -> None:
        wf = _trivial_workflow()

        captured: dict[str, Any] = {}

        class _IntrospectingRunner:
            async def run(
                self,
                *,
                node_name: str,
                node_def: Any,
                bound_inputs: dict[str, Any],
                state: dict[str, Any],
                ctx: NodeContext,
            ) -> dict[str, Any]:
                _ = (node_name, node_def, bound_inputs, state)
                captured["call_depth"] = ctx.call_depth
                captured["call_chain"] = ctx.call_chain
                captured["workflow"] = ctx.workflow
                return {}

        store = WorkflowRunStore(tmp_path)
        run = _make_run(store)
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", _IntrospectingRunner())
        ex = IterativeExecutor(
            workflow=wf,
            run=run,
            store=store,
            registry=registry,
            catalog=AssetCatalog(),
            _call_depth=3,
            _call_chain=("a", "b", "c"),
        )
        await ex.run()
        assert captured["call_depth"] == 3
        assert captured["call_chain"] == ("a", "b", "c")
        assert captured["workflow"] is wf
