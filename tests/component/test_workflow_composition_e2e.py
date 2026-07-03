"""Phase 3.7 e2e — the 8 Phase-Level Integration Tests.

Cross-cutting tests covering the eight scenarios listed in
``process/phase3.7-composition-spec.md`` under "Phase-level
Integration Tests". Each test mirrors a section there; redundancy
with the per-step unit tests is deliberate (the goal is a single
file where the phase-level acceptance is provable end-to-end).

The headline new ground here is Phase Test 4: the operator-driven
resume cascade. A parent's child pauses; both go ``WAITING_FOR_INPUT``;
the operator resumes the leaf, then the parent — the parent re-checks
its child's status, harvests the child's terminal output, and runs
to ``SUCCEEDED``.

All test workflows live on disk under ``tmp_path/bundles/`` so the
production ``load_workflow`` + catalog path is exercised exactly as
the CLI would. Workers are typically ``kolsch/nodes/compute`` because
its receiver-side ``Inputs(BaseModel)`` is ``extra="allow"``; tests
that specifically exercise strict-mode rejection use ``noop``
intentionally.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

from assets.catalog import AssetCatalog, AssetEntry
from workflows.builtins import (
    ComputeRunner,
    MapRunner,
    NoopRunner,
    WorkflowSubRunner,
)
from workflows.builtins.user_input import UserInputRunner
from workflows.errors import (
    WorkflowExtendsDepthExceeded,
)
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.ids import WorkflowRunId
from workflows.parser import load_workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

# ---------------------------------------------------------------------------
# Fixture helpers — write YAML to disk, build catalog, build registry.
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


def _build_registry(
    *,
    catalog: AssetCatalog,
    store: WorkflowRunStore,
    extras: dict[str, Any] | None = None,
) -> RunnerRegistry:
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/noop", NoopRunner())
    registry.register("kolsch/nodes/compute", ComputeRunner())
    registry.register("kolsch/nodes/user_input", UserInputRunner())
    registry.register("kolsch/nodes/map", MapRunner(registry=registry))

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
    for type_ref, runner in (extras or {}).items():
        registry.register(type_ref, runner)
    return registry


def _run_with_yaml_catalog(
    tmp_path: Path,
    *,
    entry_ref: str,
    workflows: dict[str, str],
    inputs: dict[str, Any] | None = None,
    extras: dict[str, Any] | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any, AssetCatalog]:
    """Materialise the workflow YAMLs under ``tmp_path/bundles/``,
    build a catalog + store, return a constructed parent executor."""
    assets: dict[str, AssetEntry] = {}
    for ref, content in workflows.items():
        bundle, _kind, name = ref.split("/")
        path = (
            tmp_path / "bundles" / bundle / "workflows" / f"{name}.yaml"
        )
        _write_workflow(path, content)
        assets[ref] = _entry(ref, path)
    catalog = AssetCatalog(assets=assets)
    store = WorkflowRunStore(tmp_path)
    entry_run = store.create(workflow_ref=entry_ref, inputs=inputs or {})
    entry_path = Path(assets[entry_ref].path)
    entry_workflow = load_workflow(entry_path)
    registry = _build_registry(
        catalog=catalog, store=store, extras=extras
    )
    executor = IterativeExecutor(
        workflow=entry_workflow,
        run=entry_run,
        store=store,
        registry=registry,
        catalog=catalog,
    )
    return executor, store, entry_run, catalog


# ---------------------------------------------------------------------------
# Phase Test 1: 3-level nesting end-to-end
# ---------------------------------------------------------------------------


class TestPhase1ThreeLevelNesting:
    async def test_three_level_nesting(self, tmp_path: Path) -> None:
        # great-grandparent → grandparent → parent → child (4 levels;
        # the spec calls this "3-level nesting" because there are 3
        # workflow-node hops from the root).
        ggp_yaml = """
        nodes:
          to_gp:
            type: kolsch/nodes/workflow
            ref: demo/workflows/gp
        """
        gp_yaml = """
        nodes:
          to_p:
            type: kolsch/nodes/workflow
            ref: demo/workflows/p
        """
        p_yaml = """
        nodes:
          to_c:
            type: kolsch/nodes/workflow
            ref: demo/workflows/c
        """
        c_yaml = """
        nodes:
          done:
            type: kolsch/nodes/compute
            value: "leaf-output"
        graph:
          edges: []
        """
        ex, store, root_run, _ = _run_with_yaml_catalog(
            tmp_path,
            entry_ref="demo/workflows/ggp",
            workflows={
                "demo/workflows/ggp": ggp_yaml,
                "demo/workflows/gp": gp_yaml,
                "demo/workflows/p": p_yaml,
                "demo/workflows/c": c_yaml,
            },
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED

        # Walk the trace from the great-grandparent down through all
        # four runs; assert each child's parent pointer is correct and
        # every JSONL shares the trace_id.
        trace = str(root_run.trace_id)
        events = list(store.read_events(root_run.run_id))
        assert all(e["trace_id"] == trace for e in events)
        # Follow called_workflow events to the leaf.
        current_run_id = root_run.run_id
        depth = 0
        while True:
            called = [
                e
                for e in store.read_events(current_run_id)
                if e["kind"] == "workflow.node.called_workflow"
            ]
            if not called:
                break
            depth += 1
            child_id = WorkflowRunId(
                called[0]["payload"]["child_workflow_run_id"]
            )
            child = store.read(child_id)
            assert child.trace_id == root_run.trace_id
            assert child.parent is not None
            assert child.parent.workflow_run_id == current_run_id
            current_run_id = child_id
        assert depth == 3  # ggp→gp, gp→p, p→c


# ---------------------------------------------------------------------------
# Phase Test 2: Cycle rejected
# ---------------------------------------------------------------------------


class TestPhase2CycleRejected:
    async def test_self_call_cycle_rejected(self, tmp_path: Path) -> None:
        a_yaml = """
        nodes:
          loop:
            type: kolsch/nodes/workflow
            ref: demo/workflows/a
        """
        ex, store, run, _ = _run_with_yaml_catalog(
            tmp_path,
            entry_ref="demo/workflows/a",
            workflows={"demo/workflows/a": a_yaml},
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED
        events = list(store.read_events(run.run_id))
        node_failed = next(
            e for e in events if e["kind"] == "workflow.node.failed"
        )
        assert (
            node_failed["payload"]["error_class"] == "WorkflowCallCycle"
        )


# ---------------------------------------------------------------------------
# Phase Test 3: Depth cap
# ---------------------------------------------------------------------------


class TestPhase3DepthSeventeenRejected:
    async def test_depth_seventeen_rejected(self, tmp_path: Path) -> None:
        depth = 17
        workflows: dict[str, str] = {}
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
                        type: kolsch/nodes/compute
                        value: "leaf"
                    graph:
                      edges: []
                    """
                ).lstrip()
            workflows[ref] = content
        ex, store, root_run, _ = _run_with_yaml_catalog(
            tmp_path,
            entry_ref="demo/workflows/w0",
            workflows=workflows,
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED

        # The deepest workflow.node.failed reason mentions
        # WorkflowExtendsDepthExceeded.
        current_run_id = root_run.run_id
        last_class = ""
        while True:
            events = list(store.read_events(current_run_id))
            node_failed = [
                e for e in events if e["kind"] == "workflow.node.failed"
            ]
            if not node_failed:
                break
            last_class = node_failed[-1]["payload"]["error_class"]
            called = [
                e
                for e in events
                if e["kind"] == "workflow.node.called_workflow"
            ]
            if not called:
                break
            current_run_id = WorkflowRunId(
                called[-1]["payload"]["child_workflow_run_id"]
            )
        assert last_class == WorkflowExtendsDepthExceeded.__name__


# ---------------------------------------------------------------------------
# Phase Test 4: Nested pause + resume cascade  ★ NEW GROUND ★
# ---------------------------------------------------------------------------


class TestPhase4NestedPauseResume:
    async def test_nested_pause_resume_cascade(
        self, tmp_path: Path
    ) -> None:
        # Parent: ``sub`` node → ``wrap`` node.
        # Child: a ``user_input`` node that pauses, then a downstream
        # compute that echoes the response.
        parent_yaml = """
        nodes:
          sub:
            type: kolsch/nodes/workflow
            ref: demo/workflows/child
          wrap:
            type: kolsch/nodes/compute
            value: "wrapped: {{ nodes.sub.echo.value }}"
        graph:
          edges:
            - from: sub
              to: wrap
        """
        child_yaml = """
        nodes:
          ask:
            type: kolsch/nodes/user_input
            prompt: "say hi"
          echo:
            type: kolsch/nodes/compute
            value: "{{ inputs.message }}"
        graph:
          edges:
            - from: ask
              to: echo
              binds:
                message: "{{ nodes.ask.response }}"
        """
        ex, store, parent_run, catalog = _run_with_yaml_catalog(
            tmp_path,
            entry_ref="demo/workflows/parent",
            workflows={
                "demo/workflows/parent": parent_yaml,
                "demo/workflows/child": child_yaml,
            },
        )
        # 1. Run parent → both parent + child pause.
        status = await ex.run()
        assert status == WorkflowRunStatus.WAITING_FOR_INPUT

        # Find the child run id via the parent's JSONL.
        parent_events = list(store.read_events(parent_run.run_id))
        called = next(
            e
            for e in parent_events
            if e["kind"] == "workflow.node.called_workflow"
        )
        leaf_run_id = WorkflowRunId(
            called["payload"]["child_workflow_run_id"]
        )
        leaf_run = store.read(leaf_run_id)
        assert leaf_run.status == WorkflowRunStatus.WAITING_FOR_INPUT

        # 2. Resume the leaf with a response. Build a fresh leaf
        # executor (mirrors the per-process CLI invocation).
        leaf_workflow = load_workflow(
            Path(catalog.lookup(leaf_run.workflow_ref).path)
        )
        leaf_registry = _build_registry(catalog=catalog, store=store)
        leaf_ex = IterativeExecutor(
            workflow=leaf_workflow,
            run=leaf_run,
            store=store,
            registry=leaf_registry,
            catalog=catalog,
        )
        leaf_status = await leaf_ex.resume(response={"response": "hi"})
        assert leaf_status == WorkflowRunStatus.SUCCEEDED

        # Parent's status is now stale — child says SUCCEEDED but
        # parent meta still says WAITING_FOR_INPUT. Operator runs the
        # second resume.
        parent_reread = store.read(parent_run.run_id)
        assert parent_reread.status == WorkflowRunStatus.WAITING_FOR_INPUT

        # 3. Resume the parent. No --input needed (the spec's
        # operator-driven cascade contract); the runner reads the
        # child's status off disk and harvests.
        parent_workflow = load_workflow(
            Path(catalog.lookup(parent_reread.workflow_ref).path)
        )
        parent_registry = _build_registry(catalog=catalog, store=store)
        parent_ex = IterativeExecutor(
            workflow=parent_workflow,
            run=parent_reread,
            store=store,
            registry=parent_registry,
            catalog=catalog,
        )
        parent_status = await parent_ex.resume(response={})
        assert parent_status == WorkflowRunStatus.SUCCEEDED

        # 4. Verify the harvested output flowed through to the parent's
        # wrap node.
        events_after = list(store.read_events(parent_run.run_id))
        wrap_completed = next(
            e
            for e in events_after
            if e["kind"] == "workflow.node.completed"
            and e["node_name"] == "wrap"
        )
        assert wrap_completed["payload"]["output"] == {
            "value": "wrapped: hi"
        }

        # 5. The sub node's completion event carries the child_run_id.
        sub_completed = next(
            e
            for e in events_after
            if e["kind"] == "workflow.node.completed"
            and e["node_name"] == "sub"
        )
        assert sub_completed["payload"]["child_run_id"] == str(leaf_run_id)
        # 6. The parent's JSONL records the post-resume
        # called_workflow_completed with child_status=succeeded.
        cw_completed_events = [
            e
            for e in events_after
            if e["kind"]
            == "workflow.node.called_workflow_completed"
        ]
        # Two events: the pre-pause "waiting_for_input" + the
        # post-resume "succeeded".
        assert len(cw_completed_events) == 2
        assert (
            cw_completed_events[0]["payload"]["child_status"]
            == "waiting_for_input"
        )
        assert (
            cw_completed_events[1]["payload"]["child_status"]
            == "succeeded"
        )

    async def test_resuming_parent_before_leaf_surfaces_clear_error(
        self, tmp_path: Path
    ) -> None:
        # Same setup as above; this time try to resume the PARENT
        # while the leaf is still paused. The runner's resume cascade
        # detects WAITING_FOR_INPUT on the child and raises a
        # WorkflowNodeFailed naming the leaf run id.
        parent_yaml = """
        nodes:
          sub:
            type: kolsch/nodes/workflow
            ref: demo/workflows/child
        """
        child_yaml = """
        nodes:
          ask:
            type: kolsch/nodes/user_input
            prompt: "say hi"
        graph:
          edges: []
        """
        ex, store, parent_run, catalog = _run_with_yaml_catalog(
            tmp_path,
            entry_ref="demo/workflows/parent",
            workflows={
                "demo/workflows/parent": parent_yaml,
                "demo/workflows/child": child_yaml,
            },
        )
        await ex.run()
        # Leaf still paused — try resuming the parent.
        parent_reread = store.read(parent_run.run_id)
        parent_workflow = load_workflow(
            Path(catalog.lookup(parent_reread.workflow_ref).path)
        )
        parent_registry = _build_registry(catalog=catalog, store=store)
        parent_ex = IterativeExecutor(
            workflow=parent_workflow,
            run=parent_reread,
            store=store,
            registry=parent_registry,
            catalog=catalog,
        )
        status = await parent_ex.resume(response={})
        assert status == WorkflowRunStatus.FAILED
        events = list(store.read_events(parent_run.run_id))
        failed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.failed"
            and e["node_name"] == "sub"
        )
        assert "still paused" in failed["payload"]["message"]


# ---------------------------------------------------------------------------
# Phase Test 5: Map happy path
# ---------------------------------------------------------------------------


class TestPhase5MapHappyPath:
    async def test_map_over_ten_elements(self, tmp_path: Path) -> None:
        wf_yaml = """
        nodes:
          echo_one:
            type: kolsch/nodes/compute
            value: "{{ inputs.item }}"
          fan_out:
            type: kolsch/nodes/map
            apply: echo_one
            over: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
            item_as: item
        graph:
          edges: []
        """
        ex, store, run, _ = _run_with_yaml_catalog(
            tmp_path,
            entry_ref="demo/workflows/m",
            workflows={"demo/workflows/m": wf_yaml},
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        events = list(store.read_events(run.run_id))
        completed = next(
            e
            for e in events
            if e["kind"] == "workflow.node.completed"
            and e["node_name"] == "fan_out"
        )
        assert completed["payload"]["output"] == {
            "results": [{"value": i} for i in range(1, 11)]
        }


# ---------------------------------------------------------------------------
# Phase Test 6: Map non-list raises
# ---------------------------------------------------------------------------


class TestPhase6MapNonListRaises:
    async def test_non_list_over_aborts_run(self, tmp_path: Path) -> None:
        wf_yaml = """
        nodes:
          echo:
            type: kolsch/nodes/compute
            value: "{{ inputs.item }}"
          fan:
            type: kolsch/nodes/map
            apply: echo
            over: "{{ inputs.scalar }}"
            item_as: item
        graph:
          edges: []
        """
        ex, _store, _run, _ = _run_with_yaml_catalog(
            tmp_path,
            entry_ref="demo/workflows/m",
            workflows={"demo/workflows/m": wf_yaml},
            inputs={"scalar": "abc"},
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED


# ---------------------------------------------------------------------------
# Phase Test 7: State isolation
# ---------------------------------------------------------------------------


class TestPhase7StateIsolation:
    async def test_parent_child_state_isolated(
        self, tmp_path: Path
    ) -> None:
        # Captured-state probe via a custom runner that records the
        # state dict it sees at fire time.
        parent_yaml = """
        nodes:
          set_parent:
            type: kolsch/nodes/noop
          sub:
            type: kolsch/nodes/workflow
            ref: demo/workflows/child
          check:
            type: kolsch/nodes/noop
        graph:
          edges:
            - from: set_parent
              to: sub
            - from: sub
              to: check
        """
        child_yaml = """
        nodes:
          set_child:
            type: kolsch/nodes/noop
        graph:
          edges: []
        """
        captured: dict[str, Any] = {}

        class _StateProbe:
            async def run(
                self,
                *,
                node_name: str,
                node_def: Any,
                bound_inputs: dict[str, Any],
                state: dict[str, Any],
                ctx: Any,
            ) -> dict[str, Any]:
                _ = (node_def, bound_inputs, ctx)
                if node_name == "set_parent":
                    state["x"] = "parent_value"
                elif node_name == "set_child":
                    state["x"] = "child_value"
                elif node_name == "check":
                    captured.update(state)
                return {}

        ex, _, _, _ = _run_with_yaml_catalog(
            tmp_path,
            entry_ref="demo/workflows/parent",
            workflows={
                "demo/workflows/parent": parent_yaml,
                "demo/workflows/child": child_yaml,
            },
            extras={"kolsch/nodes/noop": _StateProbe()},
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        # Parent's state.x is still "parent_value" — the child's
        # state mutation didn't leak.
        assert captured == {"x": "parent_value"}


# ---------------------------------------------------------------------------
# Phase Test 8: No regressions
# ---------------------------------------------------------------------------


class TestPhase8NoRegressions:
    async def test_non_composite_workflow_unchanged(
        self, tmp_path: Path
    ) -> None:
        # A flat 2-node compute pipeline that uses NONE of Phase 3.7's
        # new node types. Must still run end-to-end with the Phase 3.7
        # executor topology + register_builtins extensions.
        wf_yaml = """
        nodes:
          first:
            type: kolsch/nodes/compute
            value: "alpha"
          second:
            type: kolsch/nodes/compute
            value: "{{ nodes.first.value }}-omega"
        graph:
          edges:
            - from: first
              to: second
        """
        ex, store, run, _ = _run_with_yaml_catalog(
            tmp_path,
            entry_ref="demo/workflows/flat",
            workflows={"demo/workflows/flat": wf_yaml},
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        events = list(store.read_events(run.run_id))
        # No composition-specific event kinds emitted.
        kinds = {e["kind"] for e in events}
        assert "workflow.node.called_workflow" not in kinds
        assert "workflow.node.called_workflow_completed" not in kinds
        # And the run.completed payload carries the standard outputs
        # dict keyed by terminal nodes.
        completed = next(
            e for e in events if e["kind"] == "workflow.run.completed"
        )
        assert completed["payload"]["outputs"] == {
            "second": {"value": "alpha-omega"}
        }
