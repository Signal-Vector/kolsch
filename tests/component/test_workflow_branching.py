"""Phase 3.3 Step 1 — branch value extraction + edge filtering tests.

Covers AC 1.1 - 1.8 from the Phase 3.3 build doc:

- 1.1 — ``compute_branch_value`` correct for bash, bool-@node,
  tuple-@node; ``None`` for non-branchable.
- 1.2 — Edge filtering reads ``edge.branch`` and fires only matching
  arms; non-``branch:`` edges fire regardless.
- 1.3 — Bash non-zero is SUCCESS when at least one outgoing edge
  has ``branch is not None``; FAILURE otherwise.
- 1.4 — ``workflow.node.completed`` payload carries ``branch_value``
  for branchable sources; absent for non-branchable.
- 1.5 — ``inputs_model_for`` returns the authored Python @node's
  introspected model via :class:`CatalogNodeRunner.inputs_model_for`.
- 1.6 — Authored Python capture shape matches
  ``{"value": bool}`` / ``{"value": bool, **model_dump()}``.
- 1.7 — All scenarios above.
- 1.8 — No ``workflow.node.pruned`` event yet (Step 2 owns it);
  pruned edges are silently skipped (single-level only).

Test fixtures use IMMEDIATELY-TERMINAL branches (the pruned target
has no downstream) so Step 1's cascade gap isn't observable — the
master spec's cascade ships in Step 2.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

from assets.catalog import AssetCatalog, AssetEntry
from workflows.branching import compute_branch_value
from workflows.builtins import CatalogNodeRunner
from workflows.errors import BashNonZeroExit
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _make_executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    registry: RunnerRegistry,
    inputs: dict[str, Any] | None = None,
    catalog: AssetCatalog | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path / "store")
    run = store.create(workflow_ref="demo/workflows/x", inputs=inputs or {})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=catalog or AssetCatalog(),
    )
    return ex, store, run


class StaticRunner:
    """Returns a fixed output. Tracks invocation count for AC 1.2 /
    1.3 assertions."""

    def __init__(self, output: dict[str, Any]) -> None:
        self._output = output
        self.calls = 0

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
        self.calls += 1
        return dict(self._output)


class BashStubRunner:
    """Mirrors :class:`workflows.builtins.bash.BashRunner`'s
    non-zero-exit raise without standing up the tool-dispatch
    pipeline. Returns the configured ``captured`` dict; raises
    :class:`BashNonZeroExit` when ``exit_code != 0``."""

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured
        self.calls = 0

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,
    ) -> dict[str, Any]:
        _ = (node_def, bound_inputs, state, ctx)
        self.calls += 1
        captured = dict(self._captured)
        if captured.get("exit_code", 0) != 0:
            raise BashNonZeroExit(node=node_name, captured=captured)
        return captured


def _write_node_module(workspace: Path, ref_name: str, body: str) -> Path:
    nodes_dir = workspace / "bundles" / "demo" / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    path = nodes_dir / f"{ref_name}.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _seed_catalog_with_node(
    workspace: Path, node_name: str
) -> AssetCatalog:
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


class _BashStubNodeDef:
    type = "kolsch/nodes/bash"


class _NoopStubNodeDef:
    type = "kolsch/nodes/noop"


class _CatalogStubNodeDef:
    def __init__(self, type_ref: str) -> None:
        self.type = type_ref


# ---------------------------------------------------------------------------
# AC 1.1 — compute_branch_value
# ---------------------------------------------------------------------------


class TestComputeBranchValue:
    def test_bash_zero_exit_is_true(self) -> None:
        assert (
            compute_branch_value(
                _BashStubNodeDef(),
                {"exit_code": 0, "stdout": "", "stderr": ""},
            )
            is True
        )

    def test_bash_nonzero_exit_is_false(self) -> None:
        assert (
            compute_branch_value(
                _BashStubNodeDef(),
                {"exit_code": 1, "stdout": "", "stderr": ""},
            )
            is False
        )

    def test_authored_bool_return_routes_on_value_key(self) -> None:
        assert (
            compute_branch_value(
                _CatalogStubNodeDef("demo/nodes/flag"), {"value": True}
            )
            is True
        )
        assert (
            compute_branch_value(
                _CatalogStubNodeDef("demo/nodes/flag"), {"value": False}
            )
            is False
        )

    def test_authored_tuple_return_routes_on_value_key(self) -> None:
        # ``CatalogNodeRunner._coerce_return`` stores the tuple's bool
        # at the ``value`` key alongside the BaseModel's fields.
        assert (
            compute_branch_value(
                _CatalogStubNodeDef("demo/nodes/classify"),
                {"value": True, "category": "urgent", "score": 0.95},
            )
            is True
        )

    def test_non_branchable_basemodel_return_is_none(self) -> None:
        # An authored @node returning a plain BaseModel produces a
        # dict with the model's fields BUT no ``value`` key — not
        # branchable.
        assert (
            compute_branch_value(
                _CatalogStubNodeDef("demo/nodes/echo"),
                {"message": "ok"},
            )
            is None
        )

    def test_noop_is_never_branchable(self) -> None:
        assert compute_branch_value(_NoopStubNodeDef(), {}) is None

    def test_truthy_int_is_not_branchable(self) -> None:
        # Defensive: ``bool`` is a subclass of ``int`` in Python, but
        # the executor MUST NOT route on truthy/falsy ints.
        assert (
            compute_branch_value(
                _CatalogStubNodeDef("demo/nodes/x"), {"value": 1}
            )
            is None
        )


# ---------------------------------------------------------------------------
# AC 1.2 / 1.3 — bash edge filtering + success-vs-failure semantics
# ---------------------------------------------------------------------------


class TestBashBranchDispatch:
    async def test_bash_zero_exit_fires_true_arm(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "check": {
                        "type": "kolsch/nodes/bash",
                        "command": "true",
                    },
                    "on_success": {"type": "kolsch/nodes/noop"},
                    "on_failure": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "check",
                            "to": "on_success",
                            "branch": True,
                        },
                        {
                            "from": "check",
                            "to": "on_failure",
                            "branch": False,
                        },
                    ]
                },
            }
        )
        bash = BashStubRunner(
            {"stdout": "ok", "stderr": "", "exit_code": 0}
        )
        on_success = StaticRunner({})
        on_failure = StaticRunner({})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/bash", bash)
        registry.register("kolsch/nodes/noop", on_success)
        # The registry maps type→runner, so register a single noop
        # runner and distinguish via call counts on the SHARED instance
        # — but we want separate trackers per node. Use a multiplexing
        # runner instead.

        class MuxedNoopRunner:
            def __init__(self) -> None:
                self.calls: dict[str, int] = {}

            async def run(
                self,
                *,
                node_name: str,
                node_def: Any,
                bound_inputs: dict[str, Any],
                state: dict[str, Any],
                ctx: Any,
            ) -> dict[str, Any]:
                _ = (node_def, bound_inputs, state, ctx)
                self.calls[node_name] = self.calls.get(node_name, 0) + 1
                return {}

        noop = MuxedNoopRunner()
        registry.register("kolsch/nodes/noop", noop)
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        # True-arm fired; false-arm was pruned.
        assert noop.calls.get("on_success") == 1
        assert "on_failure" not in noop.calls

        events = list(store.read_events(run.run_id))
        # Pruned node emits NO started/completed in Step 1.
        per_node = [
            (e["payload"].get("node_name"), e["kind"]) for e in events
        ]
        assert ("on_failure", "workflow.node.started") not in per_node
        assert ("on_failure", "workflow.node.completed") not in per_node

    async def test_bash_nonzero_exit_fires_false_arm(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "check": {
                        "type": "kolsch/nodes/bash",
                        "command": "false",
                    },
                    "on_success": {"type": "kolsch/nodes/noop"},
                    "on_failure": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "check",
                            "to": "on_success",
                            "branch": True,
                        },
                        {
                            "from": "check",
                            "to": "on_failure",
                            "branch": False,
                        },
                    ]
                },
            }
        )
        bash = BashStubRunner(
            {"stdout": "", "stderr": "err", "exit_code": 1}
        )

        class MuxedNoopRunner:
            def __init__(self) -> None:
                self.calls: dict[str, int] = {}

            async def run(
                self,
                *,
                node_name: str,
                node_def: Any,
                bound_inputs: dict[str, Any],
                state: dict[str, Any],
                ctx: Any,
            ) -> dict[str, Any]:
                _ = (node_def, bound_inputs, state, ctx)
                self.calls[node_name] = self.calls.get(node_name, 0) + 1
                return {}

        noop = MuxedNoopRunner()
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/bash", bash)
        registry.register("kolsch/nodes/noop", noop)
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )
        status = await ex.run()
        # The non-zero is success-of-the-false-arm; the run completes.
        assert status == WorkflowRunStatus.SUCCEEDED
        assert noop.calls.get("on_failure") == 1
        assert "on_success" not in noop.calls

    async def test_bash_nonzero_without_branch_edges_aborts(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "check": {
                        "type": "kolsch/nodes/bash",
                        "command": "false",
                    },
                    "after": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [{"from": "check", "to": "after"}],
                },
            }
        )
        bash = BashStubRunner(
            {"stdout": "", "stderr": "fatal", "exit_code": 2}
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/bash", bash)
        registry.register("kolsch/nodes/noop", StaticRunner({}))
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED
        events = list(store.read_events(run.run_id))
        kinds = [(e["payload"].get("node_name"), e["kind"]) for e in events]
        assert ("check", "workflow.node.failed") in kinds
        assert ("after", "workflow.node.started") not in kinds
        # The wrapped exception class is the bash sentinel.
        failed = next(
            e
            for e in events
            if e["payload"].get("node_name") == "check"
            and e["kind"] == "workflow.node.failed"
        )
        assert failed["payload"]["error_class"] == "BashNonZeroExit"


# ---------------------------------------------------------------------------
# AC 1.2 — authored Python branching
# ---------------------------------------------------------------------------


class TestAuthoredPythonBranchDispatch:
    async def test_bool_returning_node_routes(self, tmp_path: Path) -> None:
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
        catalog = _seed_catalog_with_node(tmp_path, "flag")
        wf = _wf(
            {
                "nodes": {
                    "branchable": {"type": "demo/nodes/flag"},
                    "on_true": {"type": "kolsch/nodes/noop"},
                    "on_false": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "branchable",
                            "to": "on_true",
                            "branch": True,
                        },
                        {
                            "from": "branchable",
                            "to": "on_false",
                            "branch": False,
                        },
                    ]
                },
            }
        )

        class MuxedNoopRunner:
            def __init__(self) -> None:
                self.calls: dict[str, int] = {}

            async def run(
                self,
                *,
                node_name: str,
                node_def: Any,
                bound_inputs: dict[str, Any],
                state: dict[str, Any],
                ctx: Any,
            ) -> dict[str, Any]:
                _ = (node_def, bound_inputs, state, ctx)
                self.calls[node_name] = self.calls.get(node_name, 0) + 1
                return {}

        noop = MuxedNoopRunner()
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", noop)
        registry.set_catalog_runner(CatalogNodeRunner(catalog=catalog))
        ex, _, _ = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            catalog=catalog,
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        assert noop.calls.get("on_true") == 1
        assert "on_false" not in noop.calls

    async def test_tuple_returning_node_routes(self, tmp_path: Path) -> None:
        _write_node_module(
            tmp_path,
            "classify",
            """
            from pydantic import BaseModel
            from workflows import node, NodeContext

            class Inp(BaseModel):
                text: str

            class Outp(BaseModel):
                category: str
                score: float

            @node(label="Classify")
            async def run(inputs: Inp, ctx: NodeContext) -> tuple[bool, Outp]:
                urgent = "urgent" in inputs.text.lower()
                return urgent, Outp(
                    category="urgent" if urgent else "normal",
                    score=0.95 if urgent else 0.1,
                )
            """,
        )
        catalog = _seed_catalog_with_node(tmp_path, "classify")
        # The classify node's typed Inputs model requires ``text``,
        # so a ``seed`` noop fans into branchable with a bound ``text``
        # key sourced from workflow-level inputs.
        wf = _wf(
            {
                "nodes": {
                    "seed": {"type": "kolsch/nodes/noop"},
                    "branchable": {"type": "demo/nodes/classify"},
                    "urgent_path": {"type": "kolsch/nodes/noop"},
                    "normal_path": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "seed",
                            "to": "branchable",
                            "binds": {"text": "{{ inputs.text }}"},
                        },
                        {
                            "from": "branchable",
                            "to": "urgent_path",
                            "branch": True,
                        },
                        {
                            "from": "branchable",
                            "to": "normal_path",
                            "branch": False,
                        },
                    ]
                },
            }
        )

        class MuxedNoopRunner:
            def __init__(self) -> None:
                self.calls: dict[str, int] = {}

            async def run(
                self,
                *,
                node_name: str,
                node_def: Any,
                bound_inputs: dict[str, Any],
                state: dict[str, Any],
                ctx: Any,
            ) -> dict[str, Any]:
                _ = (node_def, bound_inputs, state, ctx)
                self.calls[node_name] = self.calls.get(node_name, 0) + 1
                return {}

        noop = MuxedNoopRunner()
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", noop)
        registry.set_catalog_runner(CatalogNodeRunner(catalog=catalog))
        ex, store, run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            inputs={"text": "URGENT — server down"},
            catalog=catalog,
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        assert noop.calls.get("urgent_path") == 1
        assert "normal_path" not in noop.calls
        # The branchable's captured output preserves the BaseModel
        # fields alongside the routing bool.
        events = list(store.read_events(run.run_id))
        completed = next(
            e
            for e in events
            if e["payload"].get("node_name") == "branchable"
            and e["kind"] == "workflow.node.completed"
        )
        assert completed["payload"]["output"] == {
            "value": True,
            "category": "urgent",
            "score": 0.95,
        }


# ---------------------------------------------------------------------------
# AC 1.4 — branch_value in completed payload
# ---------------------------------------------------------------------------


class TestBranchValuePayload:
    async def test_branchable_bash_completed_carries_branch_value(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "check": {
                        "type": "kolsch/nodes/bash",
                        "command": "true",
                    },
                    "on_true": {"type": "kolsch/nodes/noop"},
                    "on_false": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "check", "to": "on_true", "branch": True},
                        {
                            "from": "check",
                            "to": "on_false",
                            "branch": False,
                        },
                    ]
                },
            }
        )
        registry = RunnerRegistry()
        registry.register(
            "kolsch/nodes/bash",
            BashStubRunner(
                {"stdout": "ok", "stderr": "", "exit_code": 0}
            ),
        )
        registry.register("kolsch/nodes/noop", StaticRunner({}))
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )
        await ex.run()
        events = list(store.read_events(run.run_id))
        completed = next(
            e
            for e in events
            if e["payload"].get("node_name") == "check"
            and e["kind"] == "workflow.node.completed"
        )
        assert completed["payload"]["branch_value"] is True

    async def test_non_branchable_noop_completed_omits_branch_value(
        self, tmp_path: Path
    ) -> None:
        wf = _wf({"nodes": {"n": {"type": "kolsch/nodes/noop"}}})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", StaticRunner({}))
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )
        await ex.run()
        events = list(store.read_events(run.run_id))
        completed = next(
            e
            for e in events
            if e["payload"].get("node_name") == "n"
            and e["kind"] == "workflow.node.completed"
        )
        assert "branch_value" not in completed["payload"]


# ---------------------------------------------------------------------------
# AC 1.5 — _inputs_models authored Python wiring
# ---------------------------------------------------------------------------


class TestInputsModelForAuthoredPython:
    def test_catalog_runner_introspects_inputs_model(
        self, tmp_path: Path
    ) -> None:
        _write_node_module(
            tmp_path,
            "echo",
            """
            from pydantic import BaseModel
            from workflows import node, NodeContext

            class EchoInputs(BaseModel):
                message: str

            class EchoOutputs(BaseModel):
                message: str

            @node(label="Echo")
            async def run(inputs: EchoInputs, ctx: NodeContext) -> EchoOutputs:
                return EchoOutputs(message=inputs.message.upper())
            """,
        )
        catalog = _seed_catalog_with_node(tmp_path, "echo")
        runner = CatalogNodeRunner(catalog=catalog)
        model = runner.inputs_model_for("demo/nodes/echo")
        assert model is not None
        # The introspected model retains the declared field.
        assert "message" in model.model_fields

    def test_inputs_model_for_threads_catalog_runner(
        self, tmp_path: Path
    ) -> None:
        from workflows._inputs_models import inputs_model_for  # noqa: PLC0415

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
        catalog = _seed_catalog_with_node(tmp_path, "flag")
        runner = CatalogNodeRunner(catalog=catalog)

        node_def = _CatalogStubNodeDef("demo/nodes/flag")
        # Without the catalog runner: None (Step 0 fallback).
        assert inputs_model_for(node_def) is None
        # With the catalog runner: introspected model.
        model = inputs_model_for(node_def, catalog_runner=runner)
        assert model is not None
        assert "yes" in model.model_fields


# ---------------------------------------------------------------------------
# AC 1.8 — no pruned event yet
# ---------------------------------------------------------------------------


class TestNoPrunedEventYet:
    async def test_pruned_target_emits_no_started_or_completed(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "check": {
                        "type": "kolsch/nodes/bash",
                        "command": "true",
                    },
                    "pruned_target": {"type": "kolsch/nodes/noop"},
                    "fired_target": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "check",
                            "to": "fired_target",
                            "branch": True,
                        },
                        {
                            "from": "check",
                            "to": "pruned_target",
                            "branch": False,
                        },
                    ]
                },
            }
        )
        registry = RunnerRegistry()
        registry.register(
            "kolsch/nodes/bash",
            BashStubRunner(
                {"stdout": "", "stderr": "", "exit_code": 0}
            ),
        )
        registry.register("kolsch/nodes/noop", StaticRunner({}))
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )
        await ex.run()
        events = list(store.read_events(run.run_id))
        kinds = [e["kind"] for e in events]
        # No workflow.node.pruned event (Step 2 ships it).
        assert "workflow.node.pruned" not in kinds
