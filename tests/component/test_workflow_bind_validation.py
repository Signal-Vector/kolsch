"""Phase 3.3 Step 0 — strict-mode bind validation tests.

Covers AC 0.3 + 0.5 + 0.6 from the Phase 3.3 build doc:

- ``inputs_model_for(node_def).model_validate(bound_inputs, strict=True)``
  fires AFTER ``_render_bound_inputs`` and BEFORE the runner is
  invoked.
- Mismatch raises :class:`workflows.errors.WorkflowBindTypeMismatch`
  with ``file`` / ``line`` / ``node`` / ``bind`` / ``expected_type`` /
  ``actual_type`` populated.
- The exception aborts the run via the Phase 3.1 failure path
  (``workflow.node.failed`` then ``workflow.run.failed``).
- For built-in models with ``extra="forbid"`` (e.g.
  :class:`workflows.parser.NoopInputs`) an unknown bind key is the
  observable mismatch case at the Step 0 surface — typed-field
  mismatches arrive in Step 1 with the authored-Python introspection
  wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from assets.catalog import AssetCatalog
from workflows.errors import WorkflowBindTypeMismatch
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path


class RecordingRunner:
    """Records every call; tests assert call-count to prove validation
    gated the dispatch."""

    def __init__(self, output: dict[str, Any]) -> None:
        self._output = output
        self.calls: list[str] = []

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
        self.calls.append(node_name)
        return dict(self._output)


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _make_executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    registry: RunnerRegistry,
    inputs: dict[str, Any] | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs=inputs or {})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )
    return ex, store, run


class TestExtraKeyRejected:
    async def test_noop_receiver_unknown_bind_key_raises(self, tmp_path: Path) -> None:
        """``NoopInputs`` has ``extra='forbid'`` and no required
        fields. An edge binding an unknown key violates the strict
        contract — the executor surfaces it via
        ``WorkflowBindTypeMismatch`` and the run fails."""
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "b": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "a",
                            "to": "b",
                            "binds": {"surprise": "literal"},
                        }
                    ]
                },
            }
        )
        recorder = RecordingRunner({})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", recorder)
        ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED

        events = list(store.read_events(run.run_id))
        kinds = [(e["payload"].get("node_name"), e["kind"]) for e in events]
        # ``a`` ran cleanly; ``b`` failed via the bind-validation gate.
        assert ("a", "workflow.node.completed") in kinds
        assert ("b", "workflow.node.failed") in kinds
        assert kinds[-1] == (None, "workflow.run.failed")

        # The recorded ``error_class`` proves the typed exception
        # bubbled through the Phase 3.1 failure path.
        failed = next(
            e
            for e in events
            if e["payload"].get("node_name") == "b"
            and e["kind"] == "workflow.node.failed"
        )
        assert failed["payload"]["error_class"] == "WorkflowBindTypeMismatch"

    async def test_runner_not_invoked_when_binds_fail_strict(
        self, tmp_path: Path
    ) -> None:
        """Validation is the GATE — when the strict check fails for
        node ``b``, the registry's runner for ``b`` is NEVER invoked.
        The recorder confirms only the predecessor ``a`` was called."""
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "b": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "a",
                            "to": "b",
                            "binds": {"bad": "literal"},
                        }
                    ]
                },
            }
        )
        recorder = RecordingRunner({})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", recorder)
        ex, _, _ = _make_executor(tmp_path, workflow=wf, registry=registry)
        await ex.run()
        # ``a`` ran; ``b`` was gated by validation.
        assert recorder.calls == ["a"]


class TestExceptionFields:
    async def test_bind_type_mismatch_carries_node_and_bind(
        self, tmp_path: Path
    ) -> None:
        """The raised exception carries the offending receiver's name
        and the first failing bind key (AC 0.3 fields populated). We
        bypass the worklist's catch-and-emit shape by calling the
        private validator directly so the typed exception surfaces."""
        wf = _wf(
            {
                "nodes": {
                    "a": {"type": "kolsch/nodes/noop"},
                    "target": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "a",
                            "to": "target",
                            "binds": {"stranger": 42},
                        }
                    ]
                },
            }
        )
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", RecordingRunner({}))
        ex, _, _ = _make_executor(tmp_path, workflow=wf, registry=registry)
        target_def = ex._workflow.nodes["target"]
        with pytest.raises(WorkflowBindTypeMismatch) as exc_info:
            ex._validate_bound_inputs_strict(
                node_name="target",
                node_def=target_def,
                bound_inputs={"stranger": 42},
            )
        err = exc_info.value
        assert err.node == "target"
        assert err.bind == "stranger"
        # Pydantic v2 raises ``extra_forbidden`` for unknown keys
        # under strict mode; the exact tag string is part of the
        # exception's actionable surface (Step 0 build doc §6.4.4).
        assert "extra" in err.expected_type
        # The offending value was an int — the recorded actual type
        # is the Python type name of the bound value.
        assert err.actual_type == "int"

    async def test_authored_python_node_skips_step0_validation(
        self, tmp_path: Path
    ) -> None:
        """Authored Python @nodes return ``None`` from
        ``inputs_model_for`` at Step 0 — Phase 3.3 Step 1 wires the
        catalog-runner introspection. The strict-mode hook is a no-op
        for them now."""
        wf = _wf(
            {
                "nodes": {
                    "demo": {"type": "demo/nodes/custom"},
                }
            }
        )
        registry = RunnerRegistry()
        # Even though we don't register a runner, the strict-mode
        # check must run + skip CLEANLY for authored Python (a
        # subsequent registry.get() call would still raise
        # NodeTypeNotImplemented — but that's downstream of the
        # validator).
        from workflows._inputs_models import (
            inputs_model_for,
        )

        node_def = wf.nodes["demo"]
        assert inputs_model_for(node_def) is None

        # Direct call to the validator: no raise.
        ex, _, _ = _make_executor(tmp_path, workflow=wf, registry=registry)
        ex._validate_bound_inputs_strict(
            node_name="demo",
            node_def=node_def,
            bound_inputs={"anything": "goes"},
        )


class TestEmptyBindsAcceptedWhereModelAllowsIt:
    async def test_noop_no_inbound_binds_passes_strict(self, tmp_path: Path) -> None:
        """``NoopInputs`` has ``extra='forbid'`` but NO required
        fields; an empty ``bound_inputs`` dict satisfies it under
        strict mode (Step 0 build doc §6.4.4 edge case)."""
        wf = _wf({"nodes": {"a": {"type": "kolsch/nodes/noop"}}})
        recorder = RecordingRunner({})
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", recorder)
        ex, _, _ = _make_executor(tmp_path, workflow=wf, registry=registry)
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        assert recorder.calls == ["a"]
