"""Phase 3.7 Step 1 — WorkflowSubRunner lifecycle tests.

Covers ACs 1.1-1.13 of ``process/phase3.7-composition-build.md``:

- 1.1 / 1.4 / 1.6 — happy-path lifecycle: catalog resolve, parent
  pointer, trace inheritance, terminal harvest, ``child_run_id`` in
  parent's ``workflow.node.completed`` payload.
- 1.2 — cycle detection (direct A→A + indirect A→B→A).
- 1.3 — depth cap at construction (chain of 17 raises
  :class:`WorkflowExtendsDepthExceeded`).
- 1.5 — two new event kinds ordered correctly around the workflow
  node's ``started`` / ``completed``.
- 1.7 — child failure → ``WorkflowNodeFailed(cause=ChildWorkflowFailed)``.
- 1.8 — pause cascade: child's :class:`WorkflowPausedForInput`
  re-raises through the runner; parent pauses per Phase 3.4.
- 1.9 — state isolation between parent and child.
- 1.10 — :func:`compute_terminal_output_dict` projects child run
  state correctly.
- 1.11 — ``register_builtins(executor_factory=)`` wiring registers
  the slot only when the full quartet is supplied.
- 1.12 — coverage of the 10 enumerated scenarios in the Step 1 prompt.

Child workflows live as YAML files under ``tmp_path/bundles/`` so
``load_workflow(Path(entry.path))`` reads them off disk just like the
production code path.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, Any

import pytest

from assets.catalog import AssetCatalog, AssetEntry
from workflows.builtins import (
    ComputeRunner,
    NoopRunner,
    WorkflowSubRunner,
    register_builtins,
)
from workflows.errors import (
    NodeTypeNotImplemented,
    WorkflowPausedForInput,
)
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow, load_workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

    from workflows.context import NodeContext

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _write_workflow(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def _entry(ref: str, path: Path) -> AssetEntry:
    return AssetEntry(
        ref=ref,
        asset_type="workflow",
        bundle=ref.split("/", 1)[0],
        path=str(path),
    )


class _StaticRunner:
    """Trivial runner returning a fixed dict."""

    def __init__(self, output: dict[str, Any]) -> None:
        self._output = dict(output)

    async def run(self, **_: Any) -> dict[str, Any]:
        return dict(self._output)


class _RaisingRunner:
    """Always raises the supplied exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run(self, **_: Any) -> dict[str, Any]:
        raise self._exc


class _PausingRunner:
    """Always raises :class:`WorkflowPausedForInput` — mimics user_input."""

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        _ = (node_def, bound_inputs, state)
        raise WorkflowPausedForInput(
            node_name=node_name,
            invocation_id=str(ctx.invocation_id),
            prompt="prompt-for-tests",
        )


class _StateSetter:
    """Mutates ``state[key] = value`` and returns ``{value: ...}``.

    Used by state-isolation tests — the runner directly writes the
    state dict that the executor handed in, which is the same dict
    flow ``SetStateRunner`` uses in production.
    """

    def __init__(self, *, key: str, value: Any) -> None:
        self._key = key
        self._value = value

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        _ = (node_name, node_def, bound_inputs, ctx)
        state[self._key] = self._value
        return {"value": self._value}


def _build_registry(
    *,
    catalog: AssetCatalog,
    store: WorkflowRunStore,
    extra: dict[str, Any] | None = None,
) -> RunnerRegistry:
    """Build a registry with noop + compute + workflow-sub + any extras.

    Compute is registered so downstream-consumer test scenarios (the
    parent reads the harvested child output via a compute node) work
    without per-test wiring. Tests that don't use compute aren't
    affected.
    """
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/noop", NoopRunner())
    registry.register("kolsch/nodes/compute", ComputeRunner())

    def _factory(**kwargs: Any) -> IterativeExecutor:
        return IterativeExecutor(**kwargs)

    registry.register(
        "kolsch/nodes/workflow",
        WorkflowSubRunner(
            catalog=catalog,
            store=store,
            executor_factory=_factory,
            registry=registry,
        ),
    )
    for type_ref, runner in (extra or {}).items():
        registry.register(type_ref, runner)
    return registry


def _run_parent(
    tmp_path: Path,
    *,
    parent_yaml: str,
    parent_ref: str,
    children: dict[str, str],
    extra_runners: dict[str, Any] | None = None,
    extra_entries: dict[str, AssetEntry] | None = None,
    parent_inputs: dict[str, Any] | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any, AssetCatalog]:
    """End-to-end setup: write YAML files, build catalog + store,
    construct the parent executor, return it for the caller to ``run``.

    ``children`` is ``{child_ref: yaml_content}``; their YAMLs land at
    ``tmp_path/bundles/<bundle>/workflows/<name>.yaml`` and the
    catalog gets one :class:`AssetEntry` per ref.
    """
    parent_path = tmp_path / "bundles" / "demo" / "workflows" / "parent.yaml"
    _write_workflow(parent_path, parent_yaml)
    assets: dict[str, AssetEntry] = {parent_ref: _entry(parent_ref, parent_path)}
    for ref, content in children.items():
        bundle, _kind, name = ref.split("/")
        child_path = tmp_path / "bundles" / bundle / "workflows" / f"{name}.yaml"
        _write_workflow(child_path, content)
        assets[ref] = _entry(ref, child_path)
    if extra_entries:
        assets.update(extra_entries)
    catalog = AssetCatalog(assets=assets)
    store = WorkflowRunStore(tmp_path)
    parent_run = store.create(
        workflow_ref=parent_ref,
        inputs=parent_inputs or {},
    )
    parent_workflow = load_workflow(parent_path)
    registry = _build_registry(catalog=catalog, store=store, extra=extra_runners)
    executor = IterativeExecutor(
        workflow=parent_workflow,
        run=parent_run,
        store=store,
        registry=registry,
        catalog=catalog,
    )
    return executor, store, parent_run, catalog


# ---------------------------------------------------------------------------
# AC 1.1 / 1.4 / 1.5 / 1.6 / 1.10 — happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_parent_harvests_child_terminal_output(self, tmp_path: Path) -> None:
        parent_yaml = """
        nodes:
          sub:
            type: kolsch/nodes/workflow
            ref: demo/workflows/child
        """
        child_yaml = """
        nodes:
          only:
            type: kolsch/nodes/noop
        graph:
          edges: []
        """
        ex, store, parent_run, _ = _run_parent(
            tmp_path,
            parent_yaml=parent_yaml,
            parent_ref="demo/workflows/parent",
            children={"demo/workflows/child": child_yaml},
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED

        events = list(store.read_events(parent_run.run_id))
        kinds = [e["kind"] for e in events]
        # Parent emits: run.started → node.started(sub) →
        # called_workflow → called_workflow_completed →
        # node.completed(sub) → run.completed.
        assert kinds == [
            "workflow.run.started",
            "workflow.node.started",
            "workflow.node.called_workflow",
            "workflow.node.called_workflow_completed",
            "workflow.node.completed",
            "workflow.run.completed",
        ]

        sub_completed = next(
            e for e in events if e["kind"] == "workflow.node.completed"
        )
        # AC 1.6 — child_run_id on parent's node.completed payload.
        assert sub_completed["payload"]["child_run_id"].startswith("wrk_")
        child_run_id = sub_completed["payload"]["child_run_id"]
        called = next(e for e in events if e["kind"] == "workflow.node.called_workflow")
        assert called["payload"]["child_workflow_run_id"] == child_run_id
        assert called["payload"]["child_workflow_ref"] == "demo/workflows/child"

    async def test_terminal_output_flows_to_downstream_compute(
        self, tmp_path: Path
    ) -> None:
        # Parent's downstream node consumes the child's terminal output
        # via a Jinja-rendered bind. Compute carries ``extra="allow"``
        # on its Inputs so the receiver accepts our ``echo`` key; the
        # rendered value lands as the captured ``{value: ...}`` of the
        # compute node itself, then the workflow's terminal-output
        # builder surfaces it.
        parent_yaml = """
        nodes:
          sub:
            type: kolsch/nodes/workflow
            ref: demo/workflows/child
          consume:
            type: kolsch/nodes/compute
            value: "{{ nodes.sub.only.value }}"
        graph:
          edges:
            - {from: sub, to: consume}
        """
        child_yaml = """
        nodes:
          only:
            type: kolsch/nodes/noop
        graph:
          edges: []
        """
        # Override the noop runner so the child's only-node returns a
        # captured output we can read downstream.
        ex, store, parent_run, _ = _run_parent(
            tmp_path,
            parent_yaml=parent_yaml,
            parent_ref="demo/workflows/parent",
            children={"demo/workflows/child": child_yaml},
            extra_runners={"kolsch/nodes/noop": _StaticRunner({"value": "v"})},
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        # Verify the harvested value flowed through to ``consume``.
        events = list(store.read_events(parent_run.run_id))
        consume_completed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.completed" and e["node_name"] == "consume"
        )
        assert consume_completed["payload"]["output"] == {"value": "v"}


# ---------------------------------------------------------------------------
# AC 1.4 — trace_id inheritance + WorkflowRun.parent
# ---------------------------------------------------------------------------


class TestParentPointerAndTraceId:
    async def test_child_inherits_trace_id_and_records_parent(
        self, tmp_path: Path
    ) -> None:
        parent_yaml = """
        nodes:
          sub:
            type: kolsch/nodes/workflow
            ref: demo/workflows/child
        """
        child_yaml = """
        nodes:
          only:
            type: kolsch/nodes/noop
        graph:
          edges: []
        """
        ex, store, parent_run, _ = _run_parent(
            tmp_path,
            parent_yaml=parent_yaml,
            parent_ref="demo/workflows/parent",
            children={"demo/workflows/child": child_yaml},
        )
        await ex.run()

        parent_events = list(store.read_events(parent_run.run_id))
        completed = next(
            e for e in parent_events if e["kind"] == "workflow.node.completed"
        )
        child_run_id = completed["payload"]["child_run_id"]

        from workflows.ids import WorkflowRunId

        child_run = store.read(WorkflowRunId(child_run_id))
        # Trace id inheritance — child's run carries the parent's id.
        assert child_run.trace_id == parent_run.trace_id
        # WorkflowRun.parent records the immediate spawning frame.
        assert child_run.parent is not None
        assert child_run.parent.workflow_run_id == parent_run.run_id
        # Cross-JSONL: every child event shares the same trace_id.
        child_events = list(store.read_events(WorkflowRunId(child_run_id)))
        assert child_events  # child actually ran
        assert all(e["trace_id"] == str(parent_run.trace_id) for e in child_events)


# ---------------------------------------------------------------------------
# AC 1.2 — cycle detection (direct + indirect)
# ---------------------------------------------------------------------------


class TestCycleDetection:
    async def test_direct_self_call_rejected(self, tmp_path: Path) -> None:
        # A workflow whose only node tries to call itself. The Phase 3.7
        # executor auto-include of the run's own ref in ``call_chain``
        # catches this on the first would-be call.
        parent_yaml = """
        nodes:
          loop:
            type: kolsch/nodes/workflow
            ref: demo/workflows/parent
        """
        path = tmp_path / "bundles" / "demo" / "workflows" / "parent.yaml"
        _write_workflow(path, parent_yaml)
        catalog = AssetCatalog(
            assets={"demo/workflows/parent": _entry("demo/workflows/parent", path)},
        )
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/parent")
        registry = _build_registry(catalog=catalog, store=store)
        executor = IterativeExecutor(
            workflow=load_workflow(path),
            run=run,
            store=store,
            registry=registry,
            catalog=catalog,
        )
        status = await executor.run()
        assert status == WorkflowRunStatus.FAILED

        events = list(store.read_events(run.run_id))
        failed = next(e for e in events if e["kind"] == "workflow.run.failed")
        # The runner raises WorkflowCallCycle; executor wraps into
        # workflow.node.failed → workflow.run.failed. The reason
        # mentions the offending node.
        assert "loop" in failed["payload"]["reason"]
        node_failed = next(e for e in events if e["kind"] == "workflow.node.failed")
        assert "WorkflowCallCycle" in node_failed["payload"]["error_class"]

    async def test_indirect_cycle_rejected(self, tmp_path: Path) -> None:
        # A → B → A. The chain seen at B's runner is ("A", "B"); B's
        # call to A is caught immediately. Cycle = ("A", "B", "A").
        parent_yaml = """
        nodes:
          call_b:
            type: kolsch/nodes/workflow
            ref: demo/workflows/b
        """
        b_yaml = """
        nodes:
          call_a:
            type: kolsch/nodes/workflow
            ref: demo/workflows/parent
        """
        ex, store, parent_run, _ = _run_parent(
            tmp_path,
            parent_yaml=parent_yaml,
            parent_ref="demo/workflows/parent",
            children={"demo/workflows/b": b_yaml},
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED

        # Walk the child JSONL too — B's node.failed should name the
        # cycle. Find the child run id via the called_workflow event.
        events = list(store.read_events(parent_run.run_id))
        called = next(e for e in events if e["kind"] == "workflow.node.called_workflow")
        from workflows.ids import WorkflowRunId

        b_run_id = WorkflowRunId(called["payload"]["child_workflow_run_id"])
        b_events = list(store.read_events(b_run_id))
        b_failed = next(e for e in b_events if e["kind"] == "workflow.node.failed")
        assert "WorkflowCallCycle" in b_failed["payload"]["error_class"]


# ---------------------------------------------------------------------------
# AC 1.3 — depth cap
# ---------------------------------------------------------------------------


class TestDepthCap:
    async def test_chain_of_seventeen_rejected(self, tmp_path: Path) -> None:
        # Workflows w0 → w1 → ... → w17 (each calls the next via a
        # workflow: node). The 17th nested executor's constructor
        # check raises WorkflowExtendsDepthExceeded.
        depth = 17
        catalog_assets: dict[str, AssetEntry] = {}
        for i in range(depth + 1):
            ref = f"demo/workflows/w{i}"
            if i < depth:
                content = textwrap.dedent(
                    f"""
                    nodes:
                      next:
                        type: kolsch/nodes/workflow
                        ref: demo/workflows/w{i + 1}
                    """
                ).lstrip()
            else:
                content = textwrap.dedent(
                    """
                    nodes:
                      done:
                        type: kolsch/nodes/noop
                    graph:
                      edges: []
                    """
                ).lstrip()
            path = tmp_path / "bundles" / "demo" / "workflows" / f"w{i}.yaml"
            _write_workflow(path, content)
            catalog_assets[ref] = _entry(ref, path)
        catalog = AssetCatalog(assets=catalog_assets)
        store = WorkflowRunStore(tmp_path)
        root_run = store.create(workflow_ref="demo/workflows/w0")
        registry = _build_registry(catalog=catalog, store=store)
        executor = IterativeExecutor(
            workflow=load_workflow(
                tmp_path / "bundles" / "demo" / "workflows" / "w0.yaml"
            ),
            run=root_run,
            store=store,
            registry=registry,
            catalog=catalog,
        )
        status = await executor.run()
        assert status == WorkflowRunStatus.FAILED

        # Walk the deepest child whose runner raised the depth error.
        # Easiest assertion: the deepest workflow.run.failed reason
        # mentions ``WorkflowExtendsDepthExceeded``. Walk from root.
        from workflows.ids import WorkflowRunId

        current_run_id = root_run.run_id
        last_failed_class = ""
        for _ in range(depth + 1):
            events = list(store.read_events(current_run_id))
            node_failed = [e for e in events if e["kind"] == "workflow.node.failed"]
            if not node_failed:
                break
            last_failed_class = node_failed[-1]["payload"]["error_class"]
            called = [e for e in events if e["kind"] == "workflow.node.called_workflow"]
            if not called:
                break
            current_run_id = WorkflowRunId(
                called[-1]["payload"]["child_workflow_run_id"]
            )
        assert last_failed_class == "WorkflowExtendsDepthExceeded"


# ---------------------------------------------------------------------------
# AC 1.7 — child failure propagates
# ---------------------------------------------------------------------------


class TestChildFailurePropagates:
    async def test_failed_child_wraps_in_child_workflow_failed(
        self, tmp_path: Path
    ) -> None:
        parent_yaml = """
        nodes:
          sub:
            type: kolsch/nodes/workflow
            ref: demo/workflows/child
        """
        child_yaml = """
        nodes:
          boom:
            type: kolsch/nodes/noop
        graph:
          edges: []
        """
        # Override the child's noop runner with one that always raises.
        ex, store, parent_run, _ = _run_parent(
            tmp_path,
            parent_yaml=parent_yaml,
            parent_ref="demo/workflows/parent",
            children={"demo/workflows/child": child_yaml},
            extra_runners={"kolsch/nodes/noop": _RaisingRunner(RuntimeError("kaboom"))},
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED

        events = list(store.read_events(parent_run.run_id))
        node_failed = next(e for e in events if e["kind"] == "workflow.node.failed")
        # The parent's runner wraps in WorkflowNodeFailed with
        # ChildWorkflowFailed inside. ``error_class`` is the OUTER
        # error class (the executor's failure path stamps that).
        assert node_failed["payload"]["error_class"] == "WorkflowNodeFailed"
        # And the message names the failed child run.
        assert "child workflow run" in node_failed["payload"]["message"]
        # The completed marker still emits with child_status=failed.
        cw_completed = next(
            e for e in events if e["kind"] == "workflow.node.called_workflow_completed"
        )
        assert cw_completed["payload"]["child_status"] == "failed"


# ---------------------------------------------------------------------------
# AC 1.8 — pause cascade
# ---------------------------------------------------------------------------


class TestPauseCascade:
    async def test_child_pause_pauses_parent_too(self, tmp_path: Path) -> None:
        parent_yaml = """
        nodes:
          sub:
            type: kolsch/nodes/workflow
            ref: demo/workflows/child
        """
        # Child has a single ``user_input`` node — we register a
        # pausing mock against the slot.
        child_yaml = """
        nodes:
          wait:
            type: kolsch/nodes/user_input
            prompt: "pause"
        graph:
          edges: []
        """
        ex, store, parent_run, _ = _run_parent(
            tmp_path,
            parent_yaml=parent_yaml,
            parent_ref="demo/workflows/parent",
            children={"demo/workflows/child": child_yaml},
            extra_runners={"kolsch/nodes/user_input": _PausingRunner()},
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.WAITING_FOR_INPUT

        # Parent sidecar carries the WORKFLOW-NODE invocation id (the
        # parent's outer pause is at the ``sub`` node). Child sidecar
        # carries the inner user_input invocation id.
        parent_run_dir = store.directory(parent_run.run_id)
        assert parent_run_dir.waiting_at_path.exists()
        parent_waiting_inv = parent_run_dir.waiting_at_path.read_text(
            encoding="utf-8"
        ).strip()

        events = list(store.read_events(parent_run.run_id))
        sub_started = next(
            e
            for e in events
            if e["kind"] == "workflow.node.started" and e["node_name"] == "sub"
        )
        assert sub_started["invocation_id"] == parent_waiting_inv

        # The parent emitted called_workflow_completed with
        # child_status=waiting_for_input BEFORE the re-raise.
        cw_completed = next(
            e for e in events if e["kind"] == "workflow.node.called_workflow_completed"
        )
        assert cw_completed["payload"]["child_status"] == "waiting_for_input"

        # Child sidecar exists and points at the inner pause's invocation id.
        from workflows.ids import WorkflowRunId

        child_run_id = WorkflowRunId(cw_completed["payload"]["child_workflow_run_id"])
        child_run_dir = store.directory(child_run_id)
        assert child_run_dir.waiting_at_path.exists()
        child_run = store.read(child_run_id)
        assert child_run.status == WorkflowRunStatus.WAITING_FOR_INPUT


# ---------------------------------------------------------------------------
# AC 1.9 — state isolation
# ---------------------------------------------------------------------------


class TestStateIsolation:
    async def test_child_state_does_not_leak_into_parent(self, tmp_path: Path) -> None:
        # Parent has two nodes: ``set_x_1`` (sets state.x=1) → ``sub``
        # (calls child) → ``check`` (asserts state.x is still 1).
        # Child has one node ``set_x_99`` (sets state.x=99).
        parent_yaml = """
        nodes:
          set_x_1:
            type: kolsch/nodes/noop
          sub:
            type: kolsch/nodes/workflow
            ref: demo/workflows/child
          check:
            type: kolsch/nodes/noop
        graph:
          edges:
            - {from: set_x_1, to: sub}
            - {from: sub, to: check}
        """
        child_yaml = """
        nodes:
          set_x_99:
            type: kolsch/nodes/noop
        graph:
          edges: []
        """
        # Capture state at parent's ``check`` runtime.
        check_state: dict[str, Any] = {}

        class _ParentCheckRunner:
            async def run(
                self,
                *,
                node_name: str,
                node_def: Any,
                bound_inputs: dict[str, Any],
                state: dict[str, Any],
                ctx: NodeContext,
            ) -> dict[str, Any]:
                _ = (node_name, node_def, bound_inputs, ctx)
                check_state.update(state)
                return {}

        # The parent's ``set_x_1`` and child's ``set_x_99`` both share
        # the noop type — we need different runners for each, but the
        # registry maps by ``type:`` ref. Easiest: use a single runner
        # that branches on node_name (parent vs child).
        class _NoopWithStateSet:
            async def run(
                self,
                *,
                node_name: str,
                node_def: Any,
                bound_inputs: dict[str, Any],
                state: dict[str, Any],
                ctx: NodeContext,
            ) -> dict[str, Any]:
                _ = (node_def, bound_inputs, ctx)
                if node_name == "set_x_1":
                    state["x"] = 1
                elif node_name == "set_x_99":
                    state["x"] = 99
                elif node_name == "check":
                    check_state.update(state)
                return {}

        ex, _store, _parent_run, _ = _run_parent(
            tmp_path,
            parent_yaml=parent_yaml,
            parent_ref="demo/workflows/parent",
            children={"demo/workflows/child": child_yaml},
            extra_runners={"kolsch/nodes/noop": _NoopWithStateSet()},
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        # Parent's state.x is still 1 — the child's set_x_99 was on
        # the child's isolated state dict.
        assert check_state == {"x": 1}


# ---------------------------------------------------------------------------
# 3-level nesting end-to-end
# ---------------------------------------------------------------------------


class TestThreeLevelNesting:
    async def test_grandparent_parent_child_all_share_trace_id(
        self, tmp_path: Path
    ) -> None:
        grandparent_yaml = """
        nodes:
          to_parent:
            type: kolsch/nodes/workflow
            ref: demo/workflows/parent
        """
        parent_yaml = """
        nodes:
          to_child:
            type: kolsch/nodes/workflow
            ref: demo/workflows/child
        """
        child_yaml = """
        nodes:
          only:
            type: kolsch/nodes/noop
        graph:
          edges: []
        """
        gp_path = tmp_path / "bundles" / "demo" / "workflows" / "grandparent.yaml"
        p_path = tmp_path / "bundles" / "demo" / "workflows" / "parent.yaml"
        c_path = tmp_path / "bundles" / "demo" / "workflows" / "child.yaml"
        _write_workflow(gp_path, grandparent_yaml)
        _write_workflow(p_path, parent_yaml)
        _write_workflow(c_path, child_yaml)
        catalog = AssetCatalog(
            assets={
                "demo/workflows/grandparent": _entry(
                    "demo/workflows/grandparent", gp_path
                ),
                "demo/workflows/parent": _entry("demo/workflows/parent", p_path),
                "demo/workflows/child": _entry("demo/workflows/child", c_path),
            }
        )
        store = WorkflowRunStore(tmp_path)
        gp_run = store.create(workflow_ref="demo/workflows/grandparent")
        registry = _build_registry(catalog=catalog, store=store)
        executor = IterativeExecutor(
            workflow=load_workflow(gp_path),
            run=gp_run,
            store=store,
            registry=registry,
            catalog=catalog,
        )
        status = await executor.run()
        assert status == WorkflowRunStatus.SUCCEEDED

        from workflows.ids import WorkflowRunId

        gp_events = list(store.read_events(gp_run.run_id))
        p_run_id = WorkflowRunId(
            next(e for e in gp_events if e["kind"] == "workflow.node.called_workflow")[
                "payload"
            ]["child_workflow_run_id"]
        )
        p_events = list(store.read_events(p_run_id))
        c_run_id = WorkflowRunId(
            next(e for e in p_events if e["kind"] == "workflow.node.called_workflow")[
                "payload"
            ]["child_workflow_run_id"]
        )
        c_events = list(store.read_events(c_run_id))
        # All three JSONLs share the same trace_id.
        trace = str(gp_run.trace_id)
        assert all(e["trace_id"] == trace for e in gp_events)
        assert all(e["trace_id"] == trace for e in p_events)
        assert all(e["trace_id"] == trace for e in c_events)
        # And parent-pointer chain reconstructs cleanly.
        p_run = store.read(p_run_id)
        c_run = store.read(c_run_id)
        assert p_run.parent is not None
        assert p_run.parent.workflow_run_id == gp_run.run_id
        assert c_run.parent is not None
        assert c_run.parent.workflow_run_id == p_run_id


# ---------------------------------------------------------------------------
# AC 1.11 — register_builtins gating
# ---------------------------------------------------------------------------


class TestRegisterBuiltinsGating:
    """Verify the registry's slot gating for ``kolsch/nodes/workflow``.

    Step 1's ``register_builtins`` extension adds the
    ``workflow_store`` + ``executor_factory`` kwargs. The slot must
    register ONLY when BOTH are supplied (mirrors the agent runner's
    triple-kwarg gate at Phase 3.2). The direct registry-construction
    paths below avoid the full provider stack — the gating logic is
    purely about the kwarg presence checks, not the wired runtime.
    """

    def test_workflow_slot_not_registered_when_factory_omitted(
        self, tmp_path: Path
    ) -> None:
        catalog = AssetCatalog()
        store = WorkflowRunStore(tmp_path)
        registry = RunnerRegistry()
        # Stub the conditional fields directly — we're testing the
        # WorkflowSubRunner-specific gating, not the agent / file-IO
        # gates. The bash / authz / secrets / sandbox triple is
        # required by the function signature but irrelevant here.
        _stub_register_builtins_without_factory(
            registry=registry,
            catalog=catalog,
            store=store,
            tmp_path=tmp_path,
        )
        with pytest.raises(NodeTypeNotImplemented) as exc_info:
            registry.get("kolsch/nodes/workflow", node_name="x")
        assert exc_info.value.phase == "3.7"

    def test_workflow_slot_registered_when_factory_supplied(
        self, tmp_path: Path
    ) -> None:
        catalog = AssetCatalog()
        store = WorkflowRunStore(tmp_path)
        registry = RunnerRegistry()

        def _factory(**kwargs: Any) -> IterativeExecutor:
            return IterativeExecutor(**kwargs)

        _stub_register_builtins_without_factory(
            registry=registry,
            catalog=catalog,
            store=store,
            tmp_path=tmp_path,
            workflow_store=store,
            executor_factory=_factory,
        )
        runner = registry.get("kolsch/nodes/workflow", node_name="x")
        assert isinstance(runner, WorkflowSubRunner)


def _stub_register_builtins_without_factory(
    *,
    registry: RunnerRegistry,
    catalog: AssetCatalog,
    store: WorkflowRunStore,
    tmp_path: Path,
    workflow_store: WorkflowRunStore | None = None,
    executor_factory: Any = None,
) -> None:
    """Test helper — call ``register_builtins`` with minimal stub
    providers so the workflow-sub gating arm is the only meaningful
    decision.

    The bash + authz + secrets + sandbox triple is required by the
    function signature but irrelevant to these gating tests; we
    construct the cheapest valid stand-ins. ``catalog`` is supplied
    so the workflow-sub slot's catalog gate passes.
    """
    _ = store  # parent-test fixture; passed for symmetry
    from files import PathPolicy
    from providers.authz import (
        RuleBasedAuthorizationProvider,
    )
    from providers.sandbox import NativeSandboxProvider
    from providers.secrets import DotenvSecretsProvider
    from tools.builtins import make_bash
    from tools.decorator import get_tool_definition

    policy = PathPolicy(allowed_paths=["."], denied_paths=[])
    bash_fn = make_bash(policy=policy, root=tmp_path)
    register_builtins(
        registry,
        bash_defn=get_tool_definition(bash_fn),
        authz=RuleBasedAuthorizationProvider(
            config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]},
        ),
        secrets=DotenvSecretsProvider(root=tmp_path, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(),
        catalog=catalog,
        workflow_store=workflow_store,
        executor_factory=executor_factory,
    )


# ---------------------------------------------------------------------------
# AC 1.10 — compute_terminal_output_dict
# ---------------------------------------------------------------------------


class TestComputeTerminalOutputDict:
    def test_terminals_only_with_pruned_as_none(self) -> None:
        from workflows._terminal import compute_terminal_output_dict

        wf = Workflow.model_validate(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "b": {"type": "kolsch/nodes/noop"},
                    "c": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "a", "to": "b"},
                        {"from": "a", "to": "c"},
                    ]
                },
            }
        )
        # ``a`` has outgoing edges; ``b`` + ``c`` are terminals.
        out = compute_terminal_output_dict(
            wf,
            captured={"a": {"x": 1}, "b": {"y": 2}, "c": {"z": 3}},
            pruned=set(),
        )
        assert out == {"b": {"y": 2}, "c": {"z": 3}}

    def test_pruned_terminal_resolves_to_none(self) -> None:
        from workflows._terminal import compute_terminal_output_dict

        wf = Workflow.model_validate(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "b": {"type": "kolsch/nodes/noop"},
                },
                "graph": {"edges": [{"from": "a", "to": "b"}]},
            }
        )
        out = compute_terminal_output_dict(
            wf,
            captured={"a": {"x": 1}},  # b never fired
            pruned={"b"},
        )
        assert out == {"b": None}


# ---------------------------------------------------------------------------
# Unknown-ref guard
# ---------------------------------------------------------------------------


class TestUnknownChildRef:
    async def test_unknown_ref_raises_node_failed(self, tmp_path: Path) -> None:
        parent_yaml = """
        nodes:
          sub:
            type: kolsch/nodes/workflow
            ref: demo/workflows/missing
        """
        path = tmp_path / "bundles" / "demo" / "workflows" / "parent.yaml"
        _write_workflow(path, parent_yaml)
        catalog = AssetCatalog(
            assets={
                "demo/workflows/parent": _entry("demo/workflows/parent", path),
            }
        )
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/parent")
        registry = _build_registry(catalog=catalog, store=store)
        executor = IterativeExecutor(
            workflow=load_workflow(path),
            run=run,
            store=store,
            registry=registry,
            catalog=catalog,
        )
        status = await executor.run()
        assert status == WorkflowRunStatus.FAILED
        events = list(store.read_events(run.run_id))
        failed = next(e for e in events if e["kind"] == "workflow.node.failed")
        assert "missing" in failed["payload"]["message"]
