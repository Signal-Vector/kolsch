"""Phase 3.7 Step 2 — MapRunner tests.

Covers ACs 2.1-2.12 of ``process/phase3.7-composition-build.md``.

The end-to-end tests run via :class:`workflows.executor.IterativeExecutor`
so the iteration events emit through the production callback chain.
Workers are typically :class:`workflows.builtins.compute.ComputeRunner`
(``kolsch/nodes/compute``) because its ``Inputs(BaseModel)`` is
``extra="allow"`` — the map's iteration overlay
(``{**bound_inputs, **rendered_with, item_as: element}``) adds keys
the receiver must accept under strict-mode bind validation. Tests
that target partial failure / strict-mode rejection use other
shapes intentionally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from assets.catalog import AssetCatalog
from workflows.builtins import (
    ComputeRunner,
    FilterRunner,
    MapRunner,
    NoopRunner,
)
from workflows.context import NodeContext
from workflows.errors import WorkflowNodeFailed
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.ids import InvocationId, WorkflowRunId
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _FailOnNthRunner:
    """Succeeds for the first ``n_before_failure`` iterations, then raises.

    Registered against ``kolsch/nodes/compute`` so the map's strict
    bind validation passes (ComputeInputs is ``extra="allow"``).
    """

    def __init__(self, *, n_before_failure: int) -> None:
        self._n = n_before_failure
        self._call_count = 0

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        _ = (node_def, bound_inputs, state, ctx)
        self._call_count += 1
        if self._call_count > self._n:
            msg = f"worker {node_name!r} blew up on call {self._call_count}"
            raise RuntimeError(
                msg
            )
        return {"ok": self._call_count}


def _executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    extras: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    """Build a fresh executor + store with the standard runner set."""
    store = WorkflowRunStore(tmp_path)
    run = store.create(
        workflow_ref="demo/workflows/map_test", inputs=inputs or {}
    )
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/noop", NoopRunner())
    registry.register("kolsch/nodes/compute", ComputeRunner())
    registry.register("kolsch/nodes/filter", FilterRunner())
    registry.register("kolsch/nodes/map", MapRunner(registry=registry))
    for type_ref, runner in (extras or {}).items():
        registry.register(type_ref, runner)
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )
    return ex, store, run


# ---------------------------------------------------------------------------
# AC 2.1 / 2.6 / 2.7 — happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_map_over_ten_elements_collects_results(
        self, tmp_path: Path
    ) -> None:
        # Worker is a compute that echoes the iteration element under
        # ``inputs.item``. The harvested results are 10 ``{value: N}``
        # dicts in order.
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "echo_one": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ inputs.item }}",
                    },
                    "fan_out": {
                        "type": "kolsch/nodes/map",
                        "apply": "echo_one",
                        "over": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                        "item_as": "item",
                    },
                },
                "graph": {"edges": []},
            }
        )
        ex, store, run = _executor(tmp_path, workflow=wf)
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED

        events = list(store.read_events(run.run_id))
        map_completed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["node_name"] == "fan_out"
        )
        assert map_completed["payload"]["output"] == {
            "results": [{"value": i} for i in range(1, 11)]
        }

        # AC 2.7 — every iteration's started/completed pair has the
        # right parent_invocation_id + 0-based iteration_index.
        map_started = next(
            e
            for e in events
            if e["kind"] == "workflow.node.started"
            and e["node_name"] == "fan_out"
        )
        map_inv_id = map_started["invocation_id"]
        iter_starts = [
            e
            for e in events
            if e["kind"] == "workflow.node.started"
            and e["node_name"] == "echo_one"
        ]
        iter_completes = [
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["node_name"] == "echo_one"
        ]
        assert len(iter_starts) == 10
        assert len(iter_completes) == 10
        for index, ev in enumerate(iter_starts):
            assert ev["payload"]["parent_invocation_id"] == map_inv_id
            assert ev["payload"]["iteration_index"] == index
            assert ev["payload"]["iteration_total"] == 10
        for index, ev in enumerate(iter_completes):
            assert ev["payload"]["parent_invocation_id"] == map_inv_id
            assert ev["payload"]["iteration_index"] == index

    async def test_custom_item_as_binds_under_named_key(
        self, tmp_path: Path
    ) -> None:
        # The worker reads ``inputs.ticket`` so we know the map bound
        # the element under its custom ``item_as``.
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "classify": {
                        "type": "kolsch/nodes/compute",
                        "value": "classified:{{ inputs.ticket }}",
                    },
                    "all_tickets": {
                        "type": "kolsch/nodes/map",
                        "apply": "classify",
                        "over": ["server down", "fix login", "add button"],
                        "item_as": "ticket",
                    },
                },
                "graph": {"edges": []},
            }
        )
        ex, store, run = _executor(tmp_path, workflow=wf)
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED

        events = list(store.read_events(run.run_id))
        completed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["node_name"] == "all_tickets"
        )
        assert completed["payload"]["output"] == {
            "results": [
                {"value": "classified:server down"},
                {"value": "classified:fix login"},
                {"value": "classified:add button"},
            ]
        }


# ---------------------------------------------------------------------------
# AC 2.5 — with: rendering
# ---------------------------------------------------------------------------


class TestWithConstants:
    async def test_with_literal_constants_bound_per_iteration(
        self, tmp_path: Path
    ) -> None:
        # Worker echoes ``{prefix}{item}`` so we see both keys in the
        # overlay surfaced in the captured output.
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "echo": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ inputs.prefix }}{{ inputs.item }}",
                    },
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "echo",
                        "over": ["a", "b"],
                        "item_as": "item",
                        "with": {"prefix": "OPS-"},
                    },
                },
                "graph": {"edges": []},
            }
        )
        ex, store, run = _executor(tmp_path, workflow=wf)
        await ex.run()
        events = list(store.read_events(run.run_id))
        completed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["node_name"] == "fan"
        )
        assert completed["payload"]["output"] == {
            "results": [{"value": "OPS-a"}, {"value": "OPS-b"}]
        }

    async def test_with_jinja_renders_against_workflow_inputs(
        self, tmp_path: Path
    ) -> None:
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "echo": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ inputs.prefix }}:{{ inputs.item }}",
                    },
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "echo",
                        "over": ["a", "b"],
                        "item_as": "item",
                        "with": {"prefix": "{{ inputs.tag }}"},
                    },
                },
                "graph": {"edges": []},
            }
        )
        ex, store, run = _executor(
            tmp_path, workflow=wf, inputs={"tag": "ACME"}
        )
        await ex.run()
        events = list(store.read_events(run.run_id))
        completed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["node_name"] == "fan"
        )
        assert completed["payload"]["output"] == {
            "results": [{"value": "ACME:a"}, {"value": "ACME:b"}]
        }

    async def test_with_rendering_fires_once_not_per_iteration(
        self, tmp_path: Path
    ) -> None:
        # ``with: {meta: "{{ inputs.tag | upper }}"}`` evaluates once;
        # every iteration's worker observes the same string. We assert
        # via the worker's iteration ``started`` payload's ``inputs``
        # field — each iteration's overlay has the SAME ``meta``.
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "echo": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ inputs.meta }}",
                    },
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "echo",
                        "over": [1, 2, 3, 4, 5],
                        "item_as": "item",
                        "with": {"meta": "{{ inputs.tag | upper }}"},
                    },
                },
                "graph": {"edges": []},
            }
        )
        ex, store, run = _executor(
            tmp_path, workflow=wf, inputs={"tag": "abc"}
        )
        await ex.run()
        events = list(store.read_events(run.run_id))
        iter_starts = [
            e
            for e in events
            if e["kind"] == "workflow.node.started"
            and e["node_name"] == "echo"
        ]
        metas = {ev["payload"]["inputs"]["meta"] for ev in iter_starts}
        assert metas == {"ABC"}


# ---------------------------------------------------------------------------
# AC 2.3 / 2.4 — over: shapes
# ---------------------------------------------------------------------------


class TestOverShapes:
    async def test_empty_over_returns_empty_results_with_no_iteration_events(
        self, tmp_path: Path
    ) -> None:
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "echo": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ inputs.item }}",
                    },
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "echo",
                        "over": [],
                        "item_as": "item",
                    },
                },
                "graph": {"edges": []},
            }
        )
        ex, store, run = _executor(tmp_path, workflow=wf)
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        events = list(store.read_events(run.run_id))
        # No worker iteration events emitted.
        assert not any(
            e["node_name"] == "echo"
            for e in events
            if e["kind"]
            in {"workflow.node.started", "workflow.node.completed"}
        )
        completed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["node_name"] == "fan"
        )
        assert completed["payload"]["output"] == {"results": []}

    async def test_non_list_over_raises_node_failed(
        self, tmp_path: Path
    ) -> None:
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "echo": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ inputs.item }}",
                    },
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "echo",
                        "over": "{{ inputs.not_a_list }}",
                        "item_as": "item",
                    },
                },
                "graph": {"edges": []},
            }
        )
        ex, store, run = _executor(
            tmp_path, workflow=wf, inputs={"not_a_list": "scalar"}
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED
        events = list(store.read_events(run.run_id))
        failed = next(e for e in events if e["kind"] == "workflow.node.failed")
        assert "must resolve to a list" in failed["payload"]["message"]


# ---------------------------------------------------------------------------
# AC 2.2 — defensive runtime guards via direct MapRunner.run()
# ---------------------------------------------------------------------------


class TestRunnerDefensiveGuards:
    """The Phase 3.0 validator catches ``map.apply: nonexistent`` at
    parse time, so the runner's defensive arm is dead code under the
    normal CLI path. We call :meth:`MapRunner.run` DIRECTLY here to
    verify the defensive raise still surfaces cleanly for catalog
    drift / test-time workflow construction that bypasses the
    validator.
    """

    async def test_undefined_worker_raises_node_failed(self) -> None:
        from workflows.parser import MapNodeDef

        wf = Workflow.model_validate(
            {"nodes": {"echo": {"type": "kolsch/nodes/noop"}}}
        )
        node_def = MapNodeDef.model_validate(
            {
                "type": "kolsch/nodes/map",
                "apply": "i_do_not_exist",
                "over": [1, 2],
                "item_as": "item",
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", NoopRunner())
        runner = MapRunner(registry=registry)
        ctx = _make_ctx(workflow=wf)
        with pytest.raises(WorkflowNodeFailed) as exc_info:
            await runner.run(
                node_name="fan",
                node_def=node_def,
                bound_inputs={},
                state={},
                ctx=ctx,
            )
        assert "i_do_not_exist" in str(exc_info.value)

    async def test_non_list_over_via_runner_direct(self) -> None:
        from workflows.parser import MapNodeDef

        wf = Workflow.model_validate(
            {"nodes": {"echo": {"type": "kolsch/nodes/noop"}}}
        )
        node_def = MapNodeDef.model_validate(
            {
                "type": "kolsch/nodes/map",
                "apply": "echo",
                "over": "not a list",
                "item_as": "item",
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", NoopRunner())
        runner = MapRunner(registry=registry)
        ctx = _make_ctx(workflow=wf)
        with pytest.raises(WorkflowNodeFailed) as exc_info:
            await runner.run(
                node_name="fan",
                node_def=node_def,
                bound_inputs={},
                state={},
                ctx=ctx,
            )
        assert "must resolve to a list" in str(exc_info.value)


def _make_ctx(*, workflow: Workflow) -> NodeContext:
    """NodeContext for direct MapRunner.run() invocation in unit tests."""
    ctx = NodeContext.for_test(
        workflow_run_id=str(WorkflowRunId(f"wrk_{uuid4()}")),
        invocation_id=str(InvocationId(f"inv_{uuid4()}")),
        bundle="demo",
    )
    object.__setattr__(ctx, "workflow", workflow)
    return ctx


# ---------------------------------------------------------------------------
# AC 2.8 — partial-failure semantics
# ---------------------------------------------------------------------------


class TestPartialFailure:
    async def test_worker_failure_aborts_map_no_partial_results(
        self, tmp_path: Path
    ) -> None:
        # 5-element list with a runner that fails on the 3rd call.
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "worker": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ inputs.n }}",
                    },
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "worker",
                        "over": [1, 2, 3, 4, 5],
                        "item_as": "n",
                    },
                },
                "graph": {"edges": []},
            }
        )
        ex, store, run = _executor(
            tmp_path,
            workflow=wf,
            extras={
                "kolsch/nodes/compute": _FailOnNthRunner(n_before_failure=2)
            },
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED

        events = list(store.read_events(run.run_id))
        worker_completed = [
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["node_name"] == "worker"
        ]
        # Only two iterations completed before the third raised.
        assert len(worker_completed) == 2

        # The failing iteration emits its own workflow.node.failed.
        worker_failed = [
            e
            for e in events
            if e["kind"] == "workflow.node.failed"
            and e["node_name"] == "worker"
        ]
        assert len(worker_failed) == 1
        assert worker_failed[0]["payload"]["iteration_index"] == 2

        # The MAP node never completes — only ``started`` + ``failed``.
        assert not any(
            e["node_name"] == "fan"
            for e in events
            if e["kind"] == "workflow.node.completed"
        )
        fan_failed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.failed"
            and e["node_name"] == "fan"
        )
        # The error class on the map node's failed payload is the
        # original worker exception class (Phase 3.1 stamps the raw
        # exception type onto the node.failed payload).
        assert fan_failed["payload"]["error_class"] == "RuntimeError"


# ---------------------------------------------------------------------------
# AC 2.9 — strict-mode bind validation per iteration
# ---------------------------------------------------------------------------


class TestStrictBindValidation:
    async def test_iteration_overlay_validated_against_worker_inputs(
        self, tmp_path: Path
    ) -> None:
        # The ``noop`` runner's ``NoopInputs`` is ``extra="forbid"``;
        # the iteration overlay's ``item`` key is rejected under strict
        # mode → :class:`WorkflowBindTypeMismatch`.
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "strict_worker": {"type": "kolsch/nodes/noop"},
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "strict_worker",
                        "over": ["a"],
                        "item_as": "item",
                    },
                },
                "graph": {"edges": []},
            }
        )
        ex, store, run = _executor(tmp_path, workflow=wf)
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED
        events = list(store.read_events(run.run_id))
        failed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.failed"
            and e["node_name"] == "fan"
        )
        assert (
            failed["payload"]["error_class"]
            == "WorkflowBindTypeMismatch"
        )


# ---------------------------------------------------------------------------
# Worker-only nodes don't fire independently in the worklist
# ---------------------------------------------------------------------------


class TestWorkerOnlyNodeNotFiredIndependently:
    """Phase 3.7 — a node that's only used as ``map.apply:`` (not part
    of the edge graph) MUST NOT fire on its own as a topo-walk root.
    The Phase 3.0 validator already exempts these workers from the
    reachability check; the executor's topology must mirror the
    exemption."""

    async def test_worker_only_node_skipped_from_worklist(
        self, tmp_path: Path
    ) -> None:
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "worker": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ inputs.item }}",
                    },
                    "fan": {
                        "type": "kolsch/nodes/map",
                        "apply": "worker",
                        "over": ["a", "b"],
                        "item_as": "item",
                    },
                },
                "graph": {"edges": []},
            }
        )
        ex, store, run = _executor(tmp_path, workflow=wf)
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        events = list(store.read_events(run.run_id))
        # ``worker`` should appear ONLY as iteration events from the
        # map runner — NOT as a standalone topo-walk fire. The
        # iteration count is 2 ⇒ 2 started + 2 completed; if the
        # worker also fired independently it'd be 3 of each.
        worker_started = [
            e
            for e in events
            if e["kind"] == "workflow.node.started"
            and e["node_name"] == "worker"
        ]
        assert len(worker_started) == 2
        # And every started carries parent_invocation_id (= iteration
        # marker); a standalone fire would not.
        for ev in worker_started:
            assert "parent_invocation_id" in ev["payload"]


# ---------------------------------------------------------------------------
# AC 2.11 — downstream consumer
# ---------------------------------------------------------------------------


class TestDownstreamConsumes:
    async def test_filter_consumes_map_results(
        self, tmp_path: Path
    ) -> None:
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "score_one": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ inputs.n }}",
                    },
                    "all_scores": {
                        "type": "kolsch/nodes/map",
                        "apply": "score_one",
                        "over": [10, 20, 30, 40, 50],
                        "item_as": "n",
                    },
                    "high_scores": {
                        "type": "kolsch/nodes/filter",
                        "over": "{{ nodes.all_scores.results }}",
                        "condition": "{{ inputs.item.value >= 30 }}",
                    },
                },
                "graph": {
                    "edges": [{"from": "all_scores", "to": "high_scores"}]
                },
            }
        )
        ex, store, run = _executor(tmp_path, workflow=wf)
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        events = list(store.read_events(run.run_id))
        filtered = next(
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["node_name"] == "high_scores"
        )
        assert filtered["payload"]["output"] == {
            "result": [{"value": 30}, {"value": 40}, {"value": 50}]
        }
