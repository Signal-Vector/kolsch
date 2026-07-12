"""Phase 3.10 Step 2 — per-run ``sys.modules`` namespace tests.

Covers ACs 2.1 — 2.7 from the build doc:

- :class:`RunModuleNamespace` prefix format + ``wrap_module_name`` /
  ``cleanup_for_run`` surface.
- :class:`CatalogNodeRunner` registers loaded modules under the
  per-run prefixed key (read via the active
  :class:`WorkflowRunContext`).
- :class:`IterativeExecutor.__init__` constructs ``_module_ns``;
  ``run`` / ``resume`` / ``start_from_fork`` ``finally:`` arms call
  ``cleanup_for_run`` alongside the Phase 3.1
  ``cleanup_bundle_imports`` (both fire during the migration window).
- Phase Test 6: two concurrent runs of the same workflow loading
  the same authored node module observe distinct ``sys.modules``
  entries; cleanup at run end removes only this run's
  prefix-tagged entries.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest

from assets.catalog import AssetCatalog, AssetEntry
from workflows.builtins import CatalogNodeRunner
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.module_isolation import RunModuleNamespace
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_node_module(workspace: Path, ref_name: str, body: str) -> Path:
    nodes_dir = workspace / "bundles" / "demo" / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    path = nodes_dir / f"{ref_name}.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _seed_catalog(workspace: Path, node_name: str) -> AssetCatalog:
    catalog = AssetCatalog()
    path = workspace / "bundles" / "demo" / "nodes" / f"{node_name}.py"
    entry = AssetEntry(
        ref=f"demo/nodes/{node_name}",
        asset_type="node",
        bundle="demo",
        path=str(path),
        metadata={},
    )
    catalog.assets[entry.ref] = entry
    return catalog


def _build_executor(
    workspace: Path,
    workflow: Workflow,
    catalog: AssetCatalog,
) -> tuple[IterativeExecutor, WorkflowRunStore]:
    store = WorkflowRunStore(workspace)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    registry = RunnerRegistry()
    registry.set_catalog_runner(CatalogNodeRunner(catalog=catalog))
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=catalog,
    )
    return ex, store


# ---------------------------------------------------------------------------
# AC 2.1 — RunModuleNamespace surface
# ---------------------------------------------------------------------------


class TestRunModuleNamespace:
    def test_prefix_format_matches_spec(self) -> None:
        """Master spec §16.4 Glossary — prefix is
        ``_kolsch_<run-id[:12]>.`` (i.e., ``_kolsch_wrk_<first 8>.``)."""
        ns = RunModuleNamespace(run_id="wrk_abcdef0123456789-deadbeef-feedface")
        assert ns.prefix == "_kolsch_wrk_abcdef01."

    def test_wrap_module_name_prepends_prefix(self) -> None:
        ns = RunModuleNamespace(run_id="wrk_12345678aaaaa")
        wrapped = ns.wrap_module_name("_kolsch_node_demo_nodes_echo")
        assert wrapped == "_kolsch_wrk_12345678._kolsch_node_demo_nodes_echo"

    def test_non_wrk_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            RunModuleNamespace(run_id="ag_12345678aaaaaaaa")

    def test_cleanup_removes_only_prefixed_keys(self) -> None:
        ns = RunModuleNamespace(run_id="wrk_aaaaaaaa1111")
        other_ns = RunModuleNamespace(run_id="wrk_bbbbbbbb2222")
        # Mint two mock "modules" under each prefix plus an
        # unrelated key that must survive.
        my_key = ns.wrap_module_name("mod_x")
        other_key = other_ns.wrap_module_name("mod_x")
        unrelated_key = "unrelated_third_party_pkg_x"
        try:
            sys.modules[my_key] = object()  # type: ignore[assignment]
            sys.modules[other_key] = object()  # type: ignore[assignment]
            sys.modules[unrelated_key] = object()  # type: ignore[assignment]
            removed = ns.cleanup_for_run()
            assert removed == 1
            assert my_key not in sys.modules
            assert other_key in sys.modules
            assert unrelated_key in sys.modules
        finally:
            sys.modules.pop(my_key, None)
            sys.modules.pop(other_key, None)
            sys.modules.pop(unrelated_key, None)

    def test_cleanup_idempotent(self) -> None:
        ns = RunModuleNamespace(run_id="wrk_idemp0001234")
        assert ns.cleanup_for_run() == 0
        assert ns.cleanup_for_run() == 0


# ---------------------------------------------------------------------------
# AC 2.3 — IterativeExecutor.__init__ constructs the namespace
# ---------------------------------------------------------------------------


class TestExecutorNamespaceConstruction:
    def test_executor_init_builds_module_ns(self, tmp_path: Path) -> None:
        (tmp_path / "runs").mkdir()
        wf = Workflow.model_validate({"nodes": {"n": {"type": "kolsch/nodes/noop"}}})
        ex, _ = _build_executor(tmp_path, wf, AssetCatalog())
        assert isinstance(ex._module_ns, RunModuleNamespace)
        # The prefix should embed the run id's short tag.
        assert ex._module_ns.prefix.startswith("_kolsch_wrk_")


# ---------------------------------------------------------------------------
# AC 2.2 / 2.6 — Phase Test 6: two concurrent runs see distinct modules
# ---------------------------------------------------------------------------


def _echo_node_body() -> str:
    return """
        from pydantic import BaseModel
        from workflows import node, NodeContext

        class Inp(BaseModel):
            pass

        class Outp(BaseModel):
            module_id: int
            ok: bool

        @node(label="Echo")
        async def run(inputs: Inp, ctx: NodeContext) -> Outp:
            import sys as _sys
            # Record the id() of this module instance so the test
            # can observe whether two concurrent runs share or
            # diverge here.
            return Outp(module_id=id(_sys.modules[__name__]), ok=True)
        """


def _holding_node_body(barrier_path: str, release_path: str) -> str:
    """Worker that blocks until ``release_path`` exists so two runs
    can park inside the runner concurrently."""
    return f"""
        import time
        from pathlib import Path
        from pydantic import BaseModel
        from workflows import node, NodeContext

        class Inp(BaseModel):
            pass

        class Outp(BaseModel):
            module_id: int

        @node(label="Holding")
        async def run(inputs: Inp, ctx: NodeContext) -> Outp:
            import sys as _sys
            Path({barrier_path!r}).touch()
            for _ in range(200):
                if Path({release_path!r}).exists():
                    break
                time.sleep(0.01)
            return Outp(module_id=id(_sys.modules[__name__]))
        """


class TestConcurrentRunsDistinctModules:
    @pytest.mark.asyncio
    async def test_two_concurrent_runs_observe_distinct_module_instances(
        self, tmp_path: Path
    ) -> None:
        """Phase Test 6 — two concurrent runs each load the same
        authored node module under DIFFERENT
        ``sys.modules`` keys; ``id(module)`` differs across the two
        runs (each got its own fresh import).

        We use two separate workspaces (distinct ``runs/`` roots,
        same node .py) so the executors operate fully independently
        but exercise the SAME module path. Each node returns
        ``id(sys.modules[__name__])`` — the prefix discipline makes
        these distinct.
        """
        ws_a = tmp_path / "ws_a"
        ws_b = tmp_path / "ws_b"
        ws_a.mkdir()
        ws_b.mkdir()
        (ws_a / "runs").mkdir()
        (ws_b / "runs").mkdir()

        # Per-run barrier files coordinate the two runs so both are
        # MID-DISPATCH when the second observes the first's
        # sys.modules entry.
        barrier_a = ws_a / "barrier_a.touch"
        barrier_b = ws_b / "barrier_b.touch"
        release = tmp_path / "release.touch"
        _write_node_module(
            ws_a,
            "holding",
            _holding_node_body(str(barrier_a), str(release)),
        )
        _write_node_module(
            ws_b,
            "holding",
            _holding_node_body(str(barrier_b), str(release)),
        )
        catalog_a = _seed_catalog(ws_a, "holding")
        catalog_b = _seed_catalog(ws_b, "holding")
        wf = Workflow.model_validate(
            {
                "nodes": {"step": {"type": "demo/nodes/holding"}},
                "graph": {"edges": []},
            }
        )

        async def _do_run(ws: Path, catalog: AssetCatalog) -> tuple[str, int]:
            ex, store = _build_executor(ws, wf, catalog)
            status = await ex.run()
            run_id = ex._run.run_id
            events = list(store.read_events(run_id))
            completed = next(
                e for e in events if e.get("kind") == "workflow.node.completed"
            )
            module_id = completed["payload"]["output"]["module_id"]
            return str(status), module_id

        # Schedule both runs concurrently; release them in unison
        # once both are parked inside the node body.
        async def _releaser() -> None:
            for _ in range(200):
                if barrier_a.exists() and barrier_b.exists():
                    release.touch()
                    return
                await asyncio.sleep(0.01)
            release.touch()  # safety; runs will time out anyway

        task_a = asyncio.create_task(_do_run(ws_a, catalog_a))
        task_b = asyncio.create_task(_do_run(ws_b, catalog_b))
        releaser = asyncio.create_task(_releaser())
        try:
            (status_a, mod_a), (status_b, mod_b) = await asyncio.gather(task_a, task_b)
        finally:
            await releaser

        assert status_a.lower().endswith("succeeded")
        assert status_b.lower().endswith("succeeded")
        # The crux: each run loaded its OWN module instance under
        # its OWN prefixed sys.modules key.
        assert mod_a != mod_b


# ---------------------------------------------------------------------------
# AC 2.4 / 2.7 — sequential run still works; cleanup runs
# ---------------------------------------------------------------------------


class TestSequentialCleanup:
    @pytest.mark.asyncio
    async def test_single_run_prefixed_entries_cleared_at_run_end(
        self, tmp_path: Path
    ) -> None:
        """After a sequential run completes, no entries remain under
        the run's prefix in ``sys.modules``."""
        (tmp_path / "runs").mkdir()
        _write_node_module(tmp_path, "echo", _echo_node_body())
        catalog = _seed_catalog(tmp_path, "echo")
        wf = Workflow.model_validate(
            {
                "nodes": {"step": {"type": "demo/nodes/echo"}},
                "graph": {"edges": []},
            }
        )
        ex, _ = _build_executor(tmp_path, wf, catalog)
        prefix = ex._module_ns.prefix
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        # Every prefix-tagged entry has been swept.
        survivors = [k for k in sys.modules if k.startswith(prefix)]
        assert survivors == []

    @pytest.mark.asyncio
    async def test_cleanup_runs_on_failure(self, tmp_path: Path) -> None:
        """Failed runs still trigger ``cleanup_for_run()`` from the
        ``finally:`` arm."""
        (tmp_path / "runs").mkdir()
        _write_node_module(
            tmp_path,
            "boom",
            """
            from pydantic import BaseModel
            from workflows import node, NodeContext

            class Inp(BaseModel):
                pass

            @node(label="Boom")
            async def run(inputs: Inp, ctx: NodeContext) -> bool:
                msg = "node-level failure"
                raise RuntimeError(msg)
            """,
        )
        catalog = _seed_catalog(tmp_path, "boom")
        wf = Workflow.model_validate(
            {
                "nodes": {"step": {"type": "demo/nodes/boom"}},
                "graph": {"edges": []},
            }
        )
        ex, _ = _build_executor(tmp_path, wf, catalog)
        prefix = ex._module_ns.prefix
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED
        survivors = [k for k in sys.modules if k.startswith(prefix)]
        assert survivors == []


# ---------------------------------------------------------------------------
# AC 2.5 — both cleanups fire during the migration window
# ---------------------------------------------------------------------------


class TestBothCleanupsRun:
    def test_phase_3_1_cleanup_call_remains(self) -> None:
        """The Phase 3.1 ``cleanup_bundle_imports`` call site stays
        intact in every executor entry point (initial run / resume /
        fork). AC 2.5 — both cleanups run during the migration
        window."""
        src = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "workflows"
            / "executor.py"
        )
        text = src.read_text(encoding="utf-8")
        # Expect at least three cleanup_bundle_imports() invocation
        # sites (run, resume, start_from_fork).
        assert text.count("cleanup_bundle_imports(") >= 3
        # Expect at least three cleanup_for_run() invocation sites
        # (one per entry point).
        assert text.count("cleanup_for_run()") >= 3
