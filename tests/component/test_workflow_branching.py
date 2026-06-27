"""Phase 3.3 Steps 1 + 2 — branch value extraction, edge filtering,
pruning cascade, and ``workflow.node.pruned`` event emission tests.

Covers AC 1.1 - 1.8 + AC 2.1 - 2.8 from the Phase 3.3 build doc.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, Any

from assets.catalog import AssetCatalog, AssetEntry
from workflows.branching import compute_branch_value
from workflows.builtins import CatalogNodeRunner
from workflows.errors import BashNonZeroExit
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

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
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/bash", bash)
        # The registry maps type→runner, so register a single noop
        # runner and distinguish via call counts per node via a
        # multiplexing runner.

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
        ex, _, _ = _make_executor(
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
        from workflows._inputs_models import inputs_model_for

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
# AC 2.1 — workflow.node.pruned event emission
# ---------------------------------------------------------------------------


class TestPrunedEvent:
    async def test_immediate_terminal_emits_pruned_event(
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
        pruned = [
            e for e in events if e["kind"] == "workflow.node.pruned"
        ]
        assert len(pruned) == 1
        payload = pruned[0]["payload"]
        assert payload == {
            "node_name": "pruned_target",
            "reason": "branch_not_taken",
            "upstream_source": "check",
            "branch_value": True,
        }
        # Pruned target emits no started/completed framing.
        per_node = [
            (e["payload"].get("node_name"), e["kind"]) for e in events
        ]
        assert ("pruned_target", "workflow.node.started") not in per_node
        assert ("pruned_target", "workflow.node.completed") not in per_node
        # The pruned event chains directly to ``check``'s completion.
        check_completed = next(
            e
            for e in events
            if e["payload"].get("node_name") == "check"
            and e["kind"] == "workflow.node.completed"
        )
        assert pruned[0]["parent_event_id"] == check_completed["event_id"]


# ---------------------------------------------------------------------------
# AC 2.2 — pruning cascade through descendants
# ---------------------------------------------------------------------------


class TestCascade:
    async def test_cascade_marks_descendants_pruned(
        self, tmp_path: Path
    ) -> None:
        """``A → B → C``; A branches false; edge A→B is ``branch:
        true`` (pruned). C is downstream of B only, so the cascade
        marks both B and C pruned and emits two
        ``workflow.node.pruned`` events."""
        wf = _wf(
            {
                "nodes": {
                    "a": {
                        "type": "kolsch/nodes/bash",
                        "command": "false",
                    },
                    "b": {"type": "kolsch/nodes/noop"},
                    "c": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {"from": "a", "to": "b", "branch": True},
                        {"from": "b", "to": "c"},
                    ]
                },
            }
        )
        registry = RunnerRegistry()
        registry.register(
            "kolsch/nodes/bash",
            BashStubRunner(
                {"stdout": "", "stderr": "", "exit_code": 1}
            ),
        )
        recorder = StaticRunner({})
        registry.register("kolsch/nodes/noop", recorder)
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        # Neither downstream fired.
        assert recorder.calls == 0
        events = list(store.read_events(run.run_id))
        pruned = [
            e for e in events if e["kind"] == "workflow.node.pruned"
        ]
        # Both descendants pruned with the same upstream source.
        names = {p["payload"]["node_name"] for p in pruned}
        assert names == {"b", "c"}
        sources = {p["payload"]["upstream_source"] for p in pruned}
        assert sources == {"a"}
        # branch_value=False on every pruned event.
        assert all(
            p["payload"]["branch_value"] is False for p in pruned
        )

    async def test_partial_fan_in_does_not_prune_receiver(
        self, tmp_path: Path
    ) -> None:
        """``A → R, B → R``. A branches in a way that prunes A→R;
        B fires normally. R is NOT pruned; it fires with B's binds
        only — A's pruned bind is absent from ``inputs.*``."""
        wf = _wf(
            {
                "nodes": {
                    "a": {
                        "type": "kolsch/nodes/bash",
                        "command": "true",
                    },
                    "b": {"type": "kolsch/nodes/noop"},
                    "r": {
                        "type": "kolsch/nodes/agent",
                        "agent": "demo/agents/x",
                    },
                },
                "graph": {
                    "edges": [
                        {
                            "from": "a",
                            "to": "r",
                            "branch": False,
                            "binds": {"from_a": "alpha"},
                        },
                        {"from": "a", "to": "b", "branch": True},
                        {
                            "from": "b",
                            "to": "r",
                            "binds": {"from_b": "beta"},
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
        # Use a capturing runner so we can read R's bound_inputs.
        captured_inputs: dict[str, dict[str, Any]] = {}

        class CapturingAgent:
            async def run(
                self,
                *,
                node_name: str,
                node_def: Any,
                bound_inputs: dict[str, Any],
                state: dict[str, Any],
                ctx: Any,
            ) -> dict[str, Any]:
                _ = (node_def, state, ctx)
                captured_inputs[node_name] = dict(bound_inputs)
                return {}

        registry.register("kolsch/nodes/agent", CapturingAgent())
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        # R fired with B's binds only (pruned A→R is skipped).
        assert captured_inputs["r"] == {"from_b": "beta"}
        # R was NOT pruned — no pruned event for it.
        events = list(store.read_events(run.run_id))
        pruned_names = {
            e["payload"]["node_name"]
            for e in events
            if e["kind"] == "workflow.node.pruned"
        }
        assert "r" not in pruned_names


# ---------------------------------------------------------------------------
# AC 2.5 — pruned terminal contributes None to the inferred outputs
# ---------------------------------------------------------------------------


class TestPrunedTerminalOutputs:
    async def test_pruned_terminal_carries_none(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "check": {
                        "type": "kolsch/nodes/bash",
                        "command": "true",
                    },
                    "left_terminal": {"type": "kolsch/nodes/noop"},
                    "right_terminal": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "check",
                            "to": "left_terminal",
                            "branch": True,
                        },
                        {
                            "from": "check",
                            "to": "right_terminal",
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
        registry.register(
            "kolsch/nodes/noop", StaticRunner({"done": True})
        )
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )
        await ex.run()
        events = list(store.read_events(run.run_id))
        run_completed = next(
            e for e in events if e["kind"] == "workflow.run.completed"
        )
        outputs = run_completed["payload"]["outputs"]
        assert outputs["left_terminal"] == {"done": True}
        assert outputs["right_terminal"] is None


# ---------------------------------------------------------------------------
# AC 2.6 — when:-skip cascade structural equivalence
# ---------------------------------------------------------------------------


class TestWhenSkipCascade:
    async def test_when_false_cascades_to_downstream(
        self, tmp_path: Path
    ) -> None:
        """``s`` has ``when: false``; downstream ``d`` has only ``s``
        as predecessor. Phase 3.3 Step 2 unifies the when-skip cascade
        with branch-not-taken cascade — d is structurally pruned and
        does NOT fire. No pruned event is emitted (the when-skip
        source produced no completion event to chain to)."""
        wf = _wf(
            {
                "nodes": {
                    "s": {
                        "type": "kolsch/nodes/noop",
                        "when": "{{ false }}",
                    },
                    "d": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [{"from": "s", "to": "d"}],
                },
            }
        )
        recorder = StaticRunner({})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", recorder)
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        # Neither node fired.
        assert recorder.calls == 0
        events = list(store.read_events(run.run_id))
        per_node = [
            (e["payload"].get("node_name"), e["kind"]) for e in events
        ]
        assert ("s", "workflow.node.started") not in per_node
        assert ("d", "workflow.node.started") not in per_node
        # No pruned event for when-skip cascade (no parent completion).
        assert "workflow.node.pruned" not in [
            e["kind"] for e in events
        ]

    async def test_partial_when_skip_fan_in_still_fires_receiver(
        self, tmp_path: Path
    ) -> None:
        """When ``s`` is when-skipped but ``b`` fires, a receiver with
        inbound from BOTH ``s`` and ``b`` STILL fires using ``b``'s
        binds only — partial-fan-in semantics apply to when-skip
        sources symmetrically with branch-not-taken sources."""
        wf = _wf(
            {
                "nodes": {
                    "s": {
                        "type": "kolsch/nodes/noop",
                        "when": "{{ false }}",
                    },
                    "b": {"type": "kolsch/nodes/noop"},
                    "r": {
                        "type": "kolsch/nodes/agent",
                        "agent": "demo/agents/x",
                    },
                },
                "graph": {
                    "edges": [
                        {
                            "from": "s",
                            "to": "r",
                            "binds": {"from_s": "alpha"},
                        },
                        {
                            "from": "b",
                            "to": "r",
                            "binds": {"from_b": "beta"},
                        },
                    ]
                },
            }
        )
        captured_inputs: dict[str, dict[str, Any]] = {}

        class CapturingAgent:
            async def run(
                self,
                *,
                node_name: str,
                node_def: Any,
                bound_inputs: dict[str, Any],
                state: dict[str, Any],
                ctx: Any,
            ) -> dict[str, Any]:
                _ = (node_def, state, ctx)
                captured_inputs[node_name] = dict(bound_inputs)
                return {}

        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", StaticRunner({}))
        registry.register("kolsch/nodes/agent", CapturingAgent())
        ex, _, _ = _make_executor(tmp_path, workflow=wf, registry=registry)
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        # R fired with b's binds only.
        assert captured_inputs["r"] == {"from_b": "beta"}


# ---------------------------------------------------------------------------
# AC 2.7 — partial-fan-in receiver missing required bind raises strict mismatch
# ---------------------------------------------------------------------------


class TestPartialFanInMissingRequired:
    async def test_pruned_required_key_raises_bind_type_mismatch(
        self, tmp_path: Path
    ) -> None:
        """A receiver whose typed Inputs model REQUIRES a key supplied
        only by a pruned inbound edge fails strict-mode bind
        validation with ``WorkflowBindTypeMismatch`` and aborts the
        run."""
        _write_node_module(
            tmp_path,
            "needs_key",
            """
            from pydantic import BaseModel
            from workflows import node, NodeContext

            class NeedsKeyInputs(BaseModel):
                required: str

            class NeedsKeyOutputs(BaseModel):
                ok: bool

            @node(label="NeedsKey")
            async def run(
                inputs: NeedsKeyInputs, ctx: NodeContext
            ) -> NeedsKeyOutputs:
                return NeedsKeyOutputs(ok=True)
            """,
        )
        catalog = _seed_catalog_with_node(tmp_path, "needs_key")
        # Branch_src → decoy (branch: true, fires) AND
        # branch_src → receiver via TWO arms: pruned-false-arm
        # carries the required bind; fired-true-arm omits it. The
        # receiver fires after partial-fan-in but its bound_inputs
        # are missing ``required`` because the only source of the
        # bind was the pruned arm — strict-mode validation rejects.
        wf = _wf(
            {
                "nodes": {
                    "branch_src": {
                        "type": "kolsch/nodes/bash",
                        "command": "true",
                    },
                    "decoy": {
                        "type": "kolsch/nodes/agent",
                        "agent": "demo/agents/x",
                    },
                    "receiver": {"type": "demo/nodes/needs_key"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "branch_src",
                            "to": "decoy",
                            "branch": True,
                        },
                        {
                            "from": "branch_src",
                            "to": "receiver",
                            "branch": False,
                            "binds": {
                                "required": "{{ inputs.greeting }}"
                            },
                        },
                        {
                            "from": "decoy",
                            "to": "receiver",
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
        registry.register("kolsch/nodes/agent", StaticRunner({}))
        registry.set_catalog_runner(CatalogNodeRunner(catalog=catalog))
        ex, store, run = _make_executor(
            tmp_path,
            workflow=wf,
            registry=registry,
            inputs={"greeting": "should not arrive"},
            catalog=catalog,
        )
        status = await ex.run()
        # The pruned edge supplied ``required`` — receiver fires
        # without it, strict-mode validation rejects.
        assert status == WorkflowRunStatus.FAILED
        events = list(store.read_events(run.run_id))
        failed = next(
            e
            for e in events
            if e["payload"].get("node_name") == "receiver"
            and e["kind"] == "workflow.node.failed"
        )
        assert failed["payload"]["error_class"] == "WorkflowBindTypeMismatch"
