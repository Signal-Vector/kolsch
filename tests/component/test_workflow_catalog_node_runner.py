"""Phase 3.1 Step 3 — CatalogNodeRunner + module isolation tests.

Covers ACs 3.1-3.6:

- 3.1 — CatalogNodeRunner dispatches to imported ``@node``-decorated
  ``run`` functions.
- 3.2 — All three return shapes (``BaseModel`` / ``bool`` /
  ``tuple[bool, BaseModel]``) parse into captured outputs.
- 3.3 — Module-isolation snapshot/cleanup runs on every terminal
  state; ``sys.modules`` is unchanged across runs for ``bundles/*``
  entries.
- 3.4 — Third-party packages (``pydantic``) survive cleanup.
- 3.5 — Built-in dispatch (``BashRunner``/``NoopRunner``) still
  works unchanged.
- 3.6 — No regressions.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from assets.catalog import AssetCatalog, AssetEntry
from workflows.builtins import CatalogNodeRunner
from workflows.context import NodeContext
from workflows.errors import (
    NodeTypeNotImplemented,
    WorkflowNodeImportFailed,
    WorkflowNodeNotDecorated,
    WorkflowNodeReturnTypeInvalid,
)
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_node_module(workspace: Path, ref_name: str, body: str) -> Path:
    """Create ``<workspace>/bundles/demo/nodes/<ref_name>.py`` with ``body``
    and return the path."""
    nodes_dir = workspace / "bundles" / "demo" / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    path = nodes_dir / f"{ref_name}.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _seed_catalog_with_node(
    workspace: Path, node_name: str
) -> tuple[AssetCatalog, AssetEntry]:
    """Construct an :class:`AssetCatalog` containing a single
    ``demo/nodes/<node_name>`` entry pointing at the .py the test
    just wrote."""
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
    return catalog, entry


async def _invoke_runner(
    runner: CatalogNodeRunner,
    *,
    type_ref: str,
    bound_inputs: dict[str, Any] | None = None,
    ctx: NodeContext | None = None,
) -> dict[str, Any]:
    if ctx is None:
        ctx = NodeContext.for_test()
    node_def = _StubNodeDef(type_ref=type_ref)
    return await runner.run(
        node_name="n",
        node_def=node_def,
        bound_inputs=bound_inputs or {},
        state={},
        ctx=ctx,
    )


class _StubNodeDef:
    """Minimal :class:`CatalogNodeDef`-ish stub the runner reads
    ``type`` off of."""

    def __init__(self, *, type_ref: str) -> None:
        self.type = type_ref


# ---------------------------------------------------------------------------
# AC 3.1 / 3.2 — return shapes
# ---------------------------------------------------------------------------


class TestReturnShapes:
    async def test_basemodel_return(self, tmp_path: Path) -> None:
        _write_node_module(
            tmp_path,
            "echo",
            """
            from pydantic import BaseModel
            from workflows import node, NodeContext

            class EchoInputs(BaseModel):
                message: str = ""

            class EchoOutputs(BaseModel):
                message: str

            @node(label="Echo")
            async def run(inputs: EchoInputs, ctx: NodeContext) -> EchoOutputs:
                return EchoOutputs(message=inputs.message.upper())
            """,
        )
        catalog, _ = _seed_catalog_with_node(tmp_path, "echo")
        runner = CatalogNodeRunner(catalog=catalog)
        out = await _invoke_runner(
            runner,
            type_ref="demo/nodes/echo",
            bound_inputs={"message": "hi"},
        )
        assert out == {"message": "HI"}

    async def test_bool_return(self, tmp_path: Path) -> None:
        _write_node_module(
            tmp_path,
            "flag",
            """
            from pydantic import BaseModel
            from workflows import node, NodeContext

            class FlagInputs(BaseModel):
                yes: bool = True

            @node(label="Flag")
            async def run(inputs: FlagInputs, ctx: NodeContext) -> bool:
                return inputs.yes
            """,
        )
        catalog, _ = _seed_catalog_with_node(tmp_path, "flag")
        runner = CatalogNodeRunner(catalog=catalog)
        out = await _invoke_runner(
            runner, type_ref="demo/nodes/flag", bound_inputs={"yes": True}
        )
        assert out == {"value": True}

    async def test_tuple_return(self, tmp_path: Path) -> None:
        _write_node_module(
            tmp_path,
            "branchable",
            """
            from pydantic import BaseModel
            from workflows import node, NodeContext

            class Inp(BaseModel):
                pass

            class Outp(BaseModel):
                message: str

            @node(label="Branchable")
            async def run(inputs: Inp, ctx: NodeContext):
                return (True, Outp(message="ok"))
            """,
        )
        catalog, _ = _seed_catalog_with_node(tmp_path, "branchable")
        runner = CatalogNodeRunner(catalog=catalog)
        out = await _invoke_runner(runner, type_ref="demo/nodes/branchable")
        assert out == {"value": True, "message": "ok"}

    async def test_invalid_return_raises(self, tmp_path: Path) -> None:
        _write_node_module(
            tmp_path,
            "bad",
            """
            from pydantic import BaseModel
            from workflows import node, NodeContext

            class Inp(BaseModel):
                pass

            @node(label="Bad")
            async def run(inputs: Inp, ctx: NodeContext):
                return [1, 2, 3]
            """,
        )
        catalog, _ = _seed_catalog_with_node(tmp_path, "bad")
        runner = CatalogNodeRunner(catalog=catalog)
        with pytest.raises(WorkflowNodeReturnTypeInvalid):
            await _invoke_runner(runner, type_ref="demo/nodes/bad")

    async def test_sync_run_supported(self, tmp_path: Path) -> None:
        _write_node_module(
            tmp_path,
            "sync_echo",
            """
            from pydantic import BaseModel
            from workflows import node, NodeContext

            class Inp(BaseModel):
                pass

            class Outp(BaseModel):
                ok: bool

            @node(label="Sync")
            def run(inputs: Inp, ctx: NodeContext) -> Outp:
                return Outp(ok=True)
            """,
        )
        catalog, _ = _seed_catalog_with_node(tmp_path, "sync_echo")
        runner = CatalogNodeRunner(catalog=catalog)
        out = await _invoke_runner(runner, type_ref="demo/nodes/sync_echo")
        assert out == {"ok": True}


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    async def test_unknown_catalog_ref_raises_import_failed(
        self, tmp_path: Path
    ) -> None:
        catalog = AssetCatalog()
        runner = CatalogNodeRunner(catalog=catalog)
        with pytest.raises(WorkflowNodeImportFailed):
            await _invoke_runner(runner, type_ref="acme/nodes/missing")

    async def test_missing_node_decorator_raises(self, tmp_path: Path) -> None:
        _write_node_module(
            tmp_path,
            "undecorated",
            """
            async def run(inputs, ctx):
                return {}
            """,
        )
        catalog, _ = _seed_catalog_with_node(tmp_path, "undecorated")
        runner = CatalogNodeRunner(catalog=catalog)
        with pytest.raises(WorkflowNodeNotDecorated):
            await _invoke_runner(runner, type_ref="demo/nodes/undecorated")

    async def test_builtin_marker_raises_node_type_not_implemented(
        self, tmp_path: Path
    ) -> None:
        catalog = AssetCatalog()
        catalog.assets["kolsch/nodes/bash"] = AssetEntry(
            ref="kolsch/nodes/bash",
            asset_type="node",
            bundle="kolsch",
            path="",
            metadata={"builtin": True},
        )
        runner = CatalogNodeRunner(catalog=catalog)
        with pytest.raises(NodeTypeNotImplemented):
            await _invoke_runner(runner, type_ref="kolsch/nodes/bash")


# ---------------------------------------------------------------------------
# AC 3.3 / 3.4 — module isolation across executor runs
# ---------------------------------------------------------------------------


class TestModuleIsolation:
    async def test_two_runs_leave_sys_modules_unchanged_for_bundles(
        self, tmp_path: Path
    ) -> None:
        # Bootstrap the workspace shape the executor expects.
        (tmp_path / "runs").mkdir()
        _write_node_module(
            tmp_path,
            "echo",
            """
            from pydantic import BaseModel
            from workflows import node, NodeContext

            class Inp(BaseModel):
                pass

            class Outp(BaseModel):
                ok: bool

            @node(label="Echo")
            async def run(inputs: Inp, ctx: NodeContext) -> Outp:
                return Outp(ok=True)
            """,
        )
        catalog, _ = _seed_catalog_with_node(tmp_path, "echo")
        wf = Workflow.model_validate(
            {
                "nodes": {
                    "step": {"type": "demo/nodes/echo"},
                },
                "graph": {"edges": []},
            }
        )

        async def _run_once() -> WorkflowRunStatus:
            store = WorkflowRunStore(tmp_path)
            run = store.create(workflow_ref="demo/workflows/x", inputs={})
            registry = RunnerRegistry()
            registry.set_catalog_runner(CatalogNodeRunner(catalog=catalog))
            ex = IterativeExecutor(
                workflow=wf,
                run=run,
                store=store,
                registry=registry,
                catalog=catalog,
            )
            return await ex.run()

        before = frozenset(sys.modules)
        # Confirm a third-party package is loaded BEFORE we run so the
        # post-run assertion is meaningful.
        assert "pydantic" in before

        assert await _run_once() == WorkflowRunStatus.SUCCEEDED
        # After run 1: no leaked bundle modules from the workspace tree.
        leaked_after_run_1 = [
            name
            for name in sys.modules
            if name not in before and _module_under_workspace(name, tmp_path)
        ]
        assert leaked_after_run_1 == [], (
            f"bundle modules leaked after run 1: {leaked_after_run_1}"
        )

        assert await _run_once() == WorkflowRunStatus.SUCCEEDED
        # After run 2: still clean — third-party packages survive.
        leaked_after_run_2 = [
            name
            for name in sys.modules
            if name not in before and _module_under_workspace(name, tmp_path)
        ]
        assert leaked_after_run_2 == []
        assert "pydantic" in sys.modules

    async def test_cleanup_runs_on_failure_path(self, tmp_path: Path) -> None:
        """Even when a node raises, the ``finally:`` cleanup fires."""
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
        catalog, _ = _seed_catalog_with_node(tmp_path, "boom")
        wf = Workflow.model_validate(
            {
                "nodes": {"step": {"type": "demo/nodes/boom"}},
                "graph": {"edges": []},
            }
        )
        before = frozenset(sys.modules)
        store = WorkflowRunStore(tmp_path)
        run = store.create(workflow_ref="demo/workflows/x", inputs={})
        registry = RunnerRegistry()
        registry.set_catalog_runner(CatalogNodeRunner(catalog=catalog))
        ex = IterativeExecutor(
            workflow=wf,
            run=run,
            store=store,
            registry=registry,
            catalog=catalog,
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED
        # Cleanup still fired — no leaked bundle modules.
        leaked = [
            name
            for name in sys.modules
            if name not in before and _module_under_workspace(name, tmp_path)
        ]
        assert leaked == []


def _module_under_workspace(module_name: str, workspace: Path) -> bool:
    module = sys.modules.get(module_name)
    if module is None:
        return False
    file_attr = getattr(module, "__file__", None)
    if not isinstance(file_attr, str):
        return False
    try:
        Path(file_attr).resolve().relative_to((workspace / "bundles").resolve())
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# AC 3.5 — built-in dispatch still works alongside catalog runner
# ---------------------------------------------------------------------------


class TestRegistryDispatchPrecedence:
    def test_explicit_registration_wins_over_catalog_fallback(self) -> None:
        registry = RunnerRegistry()

        class _Stub:
            async def run(self, **_: Any) -> dict[str, Any]:
                return {}

        explicit = _Stub()
        fallback = _Stub()
        registry.register("kolsch/nodes/noop", explicit)
        registry.set_catalog_runner(fallback)
        assert registry.get("kolsch/nodes/noop", node_name="n") is explicit

    def test_reserved_builtin_still_raises_when_unregistered(self) -> None:
        """Even with a catalog runner installed, reserved built-in slots
        that lack an explicit registration raise — the catalog fallback
        is for AUTHORED Python only."""
        registry = RunnerRegistry()

        class _Stub:
            async def run(self, **_: Any) -> dict[str, Any]:
                return {}

        registry.set_catalog_runner(_Stub())
        with pytest.raises(NodeTypeNotImplemented):
            registry.get("kolsch/nodes/branch", node_name="n")

    def test_authored_ref_routes_to_catalog_fallback(self) -> None:
        registry = RunnerRegistry()

        class _Stub:
            async def run(self, **_: Any) -> dict[str, Any]:
                return {}

        fallback = _Stub()
        registry.set_catalog_runner(fallback)
        assert registry.get("acme/nodes/echo", node_name="n") is fallback
