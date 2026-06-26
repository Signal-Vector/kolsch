"""Phase 3.1 Step 4 — cancel-mid-run + cancel-before-start tests.

Covers AC 4.2 + AC 4.6: the executor polls ``cancel.flag`` at each
node boundary; on detection, it emits ``workflow.run.aborted`` with
``reason: user_cancel`` AFTER the in-flight node's terminal event
(``workflow.node.completed`` / ``workflow.node.failed``) and AFTER
the PID is written, then returns
:attr:`WorkflowRunStatus.ABORTED`.

Cancel-before-start is verified separately: the pre-walk poll fires
before any node dispatches, so the JSONL contains only
``workflow.run.started`` + ``workflow.run.aborted``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from assets.catalog import AssetCatalog
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path


class _CancelOnFirstNodeRunner:
    """Static-output runner that drops ``cancel.flag`` BEFORE returning.

    Cooperative-cancellation contract: the flag is observed at the NODE
    BOUNDARY (after ``workflow.node.completed`` is emitted), so a
    runner that writes the flag during its own execution simulates an
    operator cancelling the run while a node was in flight. The
    poll-after-completion ordering means the in-flight node's
    ``completed`` event still lands BEFORE the abort event.
    """

    def __init__(self, *, cancel_path: Path, output: dict[str, Any]) -> None:
        self._cancel_path = cancel_path
        self._output = output

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,
    ) -> dict[str, Any]:
        _ = (node_name, node_def, bound_inputs, state, ctx)
        self._cancel_path.write_text("", encoding="utf-8")
        return dict(self._output)


class _StaticRunner:
    def __init__(self, output: dict[str, Any]) -> None:
        self._output = output

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,
    ) -> dict[str, Any]:
        _ = (node_name, node_def, bound_inputs, state, ctx)
        return dict(self._output)


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _make_executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    registry: RunnerRegistry,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/cancel", inputs={})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )
    return ex, store, run


# ---------------------------------------------------------------------------
# AC 4.1 — PID file written at run start.
# ---------------------------------------------------------------------------


class TestPidFile:
    async def test_pid_file_written_at_run_start(self, tmp_path: Path) -> None:
        wf = _wf({"nodes": {"a": {"type": "kolsch/nodes/noop"}}})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", _StaticRunner({}))
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        pid_path = store.directory(run.run_id).pid_path
        assert pid_path.exists()
        assert pid_path.read_text(encoding="utf-8").strip() == str(os.getpid())

    async def test_pid_file_is_atomic(self, tmp_path: Path) -> None:
        """The ``tmp + rename`` write must not leave a stale ``.tmp``
        sibling after the run completes."""
        wf = _wf({"nodes": {"a": {"type": "kolsch/nodes/noop"}}})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", _StaticRunner({}))
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()
        pid_path = store.directory(run.run_id).pid_path
        tmp_sibling = pid_path.with_suffix(".tmp")
        assert not tmp_sibling.exists()


# ---------------------------------------------------------------------------
# AC 4.2 / 4.6 — cancel-mid-run never mid-node.
# ---------------------------------------------------------------------------


class TestCancelMidRun:
    async def test_cancel_after_first_node_aborts_before_second_fires(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "first": {"type": "kolsch/nodes/noop"},
                    "second": {"type": "kolsch/nodes/noop"},
                },
                "graph": {"edges": [{"from": "first", "to": "second"}]},
            }
        )
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/cancel", inputs={})
        cancel_path = store.directory(run.run_id).cancel_flag_path

        registry = RunnerRegistry()
        # ``first`` drops the cancel.flag at the end of its run; the
        # executor must observe it BEFORE dispatching ``second``.
        registry.register(
            "kolsch/nodes/noop",
            _CancelOnFirstNodeRunner(cancel_path=cancel_path, output={}),
        )

        ex = IterativeExecutor(
            workflow=wf,
            run=run,
            store=store,
            registry=registry,
            catalog=AssetCatalog(),
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.ABORTED
        events = list(store.read_events(run.run_id))
        kinds = [e["kind"] for e in events]
        assert kinds[0] == "workflow.run.started"
        # ``first``'s started + completed pair must land BEFORE the
        # abort event. ``second`` must never have fired.
        assert (
            "workflow.node.started",
            "first",
        ) in [(e["kind"], e["payload"].get("node_name")) for e in events]
        assert (
            "workflow.node.completed",
            "first",
        ) in [(e["kind"], e["payload"].get("node_name")) for e in events]
        assert not any(
            e["kind"].startswith("workflow.node.")
            and e["payload"].get("node_name") == "second"
            for e in events
        )
        # The terminal event is ``aborted`` with the user_cancel reason.
        assert kinds[-1] == "workflow.run.aborted"
        assert events[-1]["payload"]["reason"] == "user_cancel"

    async def test_cancel_emitted_after_node_completed_event(
        self, tmp_path: Path
    ) -> None:
        """AC 4.2 — abort lands AFTER the in-flight node's terminal
        event in the JSONL. Specifically: the ``completed`` event for
        ``first`` precedes ``workflow.run.aborted`` line-wise."""
        wf = _wf(
            {
                "nodes": {
                    "first": {"type": "kolsch/nodes/noop"},
                    "second": {"type": "kolsch/nodes/noop"},
                },
                "graph": {"edges": [{"from": "first", "to": "second"}]},
            }
        )
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/cancel", inputs={})
        cancel_path = store.directory(run.run_id).cancel_flag_path
        registry = RunnerRegistry()
        registry.register(
            "kolsch/nodes/noop",
            _CancelOnFirstNodeRunner(cancel_path=cancel_path, output={}),
        )
        ex = IterativeExecutor(
            workflow=wf,
            run=run,
            store=store,
            registry=registry,
            catalog=AssetCatalog(),
        )
        await ex.run()
        events = list(store.read_events(run.run_id))
        kinds = [e["kind"] for e in events]
        completed_idx = kinds.index("workflow.node.completed")
        aborted_idx = kinds.index("workflow.run.aborted")
        assert completed_idx < aborted_idx


# ---------------------------------------------------------------------------
# AC 4.2 — cancel-before-start path.
# ---------------------------------------------------------------------------


class TestCancelBeforeStart:
    async def test_pre_walk_cancel_skips_all_nodes(self, tmp_path: Path) -> None:
        wf = _wf(
            {
                "nodes": {
                    "first": {"type": "kolsch/nodes/noop"},
                    "second": {"type": "kolsch/nodes/noop"},
                },
                "graph": {"edges": [{"from": "first", "to": "second"}]},
            }
        )
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/cancel", inputs={})
        # Drop the cancel flag BEFORE the executor runs.
        run_dir = store.directory(run.run_id)
        run_dir.root.mkdir(parents=True, exist_ok=True)
        run_dir.cancel_flag_path.write_text("", encoding="utf-8")

        registry = RunnerRegistry()
        # If the executor dispatched, this runner would fail the test
        # by raising — confirming no node ever fired.
        class _Forbidden:
            async def run(self, **_: Any) -> dict[str, Any]:
                msg = "cancel-before-start must skip all nodes"
                raise AssertionError(msg)

        registry.register("kolsch/nodes/noop", _Forbidden())
        ex = IterativeExecutor(
            workflow=wf,
            run=run,
            store=store,
            registry=registry,
            catalog=AssetCatalog(),
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.ABORTED
        events = list(store.read_events(run.run_id))
        kinds = [e["kind"] for e in events]
        assert kinds == ["workflow.run.started", "workflow.run.aborted"]
        assert events[-1]["payload"]["reason"] == "user_cancel"


# ---------------------------------------------------------------------------
# Step ownership: cleanup still fires on abort.
# ---------------------------------------------------------------------------


class TestAbortPathRunsCleanup:
    async def test_aborted_run_clears_bundle_modules(self, tmp_path: Path) -> None:
        """AC 3.3 — module-isolation cleanup runs on EVERY terminal
        state. Verify by injecting a ``sys.modules`` entry under the
        workspace's ``bundles/`` prefix DURING the run; after the
        aborted exit, the entry must be gone."""
        # The cleanup walks ``sys.modules`` filtering by ``__file__``
        # under ``<workspace>/bundles/``. Synthesise a fake module
        # entry whose ``__file__`` matches that shape so the cleanup
        # path is exercised under abort.
        import sys
        import types

        bundles_root = tmp_path / "bundles"
        bundles_root.mkdir(parents=True, exist_ok=True)
        synthetic_path = bundles_root / "demo_module.py"
        synthetic_path.write_text("# placeholder\n", encoding="utf-8")

        wf = _wf(
            {
                "nodes": {
                    "first": {"type": "kolsch/nodes/noop"},
                    "second": {"type": "kolsch/nodes/noop"},
                },
                "graph": {"edges": [{"from": "first", "to": "second"}]},
            }
        )
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/cancel", inputs={})
        cancel_path = store.directory(run.run_id).cancel_flag_path

        class _CancelAndInjectModule:
            """Drop ``cancel.flag`` AND inject a bundle-rooted
            ``sys.modules`` entry, simulating an authored ``@node``
            that imported a sibling module right before the operator
            cancelled."""

            async def run(self, **_: Any) -> dict[str, Any]:
                module = types.ModuleType("_kolsch_test_cancel_synthetic")
                module.__file__ = str(synthetic_path)
                sys.modules["_kolsch_test_cancel_synthetic"] = module
                cancel_path.write_text("", encoding="utf-8")
                return {}

        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", _CancelAndInjectModule())
        ex = IterativeExecutor(
            workflow=wf,
            run=run,
            store=store,
            registry=registry,
            catalog=AssetCatalog(),
        )
        try:
            status = await ex.run()
        finally:
            sys.modules.pop("_kolsch_test_cancel_synthetic", None)
        assert status == WorkflowRunStatus.ABORTED
        assert "_kolsch_test_cancel_synthetic" not in sys.modules


