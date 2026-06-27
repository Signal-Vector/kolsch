"""Phase 3.6 Step 0 — :class:`BranchRunner` + ``compute_branch_value``
extension coverage.

Covers Step 0 ACs:

- 0.1 — ``BranchRunner`` renders ``condition:`` (Jinja or literal)
  and returns ``{value: bool}`` with defensive ``bool(...)`` coercion.
- 0.4 — :func:`compute_branch_value` reads ``captured["value"]`` for
  ``kolsch/nodes/branch`` with the ``isinstance(value, bool)`` guard
  (non-bool ints and strings return ``None`` so the executor falls
  back to its un-branched dispatch).
- 0.5 + 0.6 — branch dispatch end-to-end: the matching arm fires,
  the other is pruned via the Phase 3.3 cascade.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from assets.catalog import AssetCatalog
from workflows.branching import compute_branch_value
from workflows.builtins import BranchRunner
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import BranchNodeDef, Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


class _StaticRunner:
    def __init__(self, output: dict[str, Any]) -> None:
        self._output = output

    async def run(self, **_: Any) -> dict[str, Any]:
        return dict(self._output)


def _registry() -> RunnerRegistry:
    reg = RunnerRegistry()
    reg.register("kolsch/nodes/branch", BranchRunner())
    reg.register("kolsch/nodes/noop", _StaticRunner({}))
    return reg


def _executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    inputs: dict[str, Any] | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(
        workflow_ref="demo/workflows/branch", inputs=inputs or {}
    )
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=_registry(),
        catalog=AssetCatalog(),
    )
    return ex, store, run


# ---------------------------------------------------------------------------
# AC 0.1 — runner shape
# ---------------------------------------------------------------------------


class TestBranchRunnerShape:
    async def test_jinja_condition_true(self, tmp_path: Path) -> None:
        wf = _wf(
            {
                "nodes": {
                    "gate": {
                        "type": "kolsch/nodes/branch",
                        "condition": "{{ inputs.x > 5 }}",
                    },
                }
            }
        )
        ex, _store, _run = _executor(
            tmp_path, workflow=wf, inputs={"x": 10}
        )
        await ex.run()
        assert ex._captured["gate"] == {"value": True}  # noqa: SLF001

    async def test_jinja_condition_false(self, tmp_path: Path) -> None:
        wf = _wf(
            {
                "nodes": {
                    "gate": {
                        "type": "kolsch/nodes/branch",
                        "condition": "{{ inputs.x > 5 }}",
                    },
                }
            }
        )
        ex, _store, _run = _executor(
            tmp_path, workflow=wf, inputs={"x": 2}
        )
        await ex.run()
        assert ex._captured["gate"] == {"value": False}  # noqa: SLF001

    async def test_yaml_literal_true(self, tmp_path: Path) -> None:
        wf = _wf(
            {
                "nodes": {
                    "gate": {
                        "type": "kolsch/nodes/branch",
                        "condition": True,
                    },
                }
            }
        )
        ex, _store, _run = _executor(tmp_path, workflow=wf)
        await ex.run()
        assert ex._captured["gate"] == {"value": True}  # noqa: SLF001

    async def test_yaml_literal_false(self, tmp_path: Path) -> None:
        wf = _wf(
            {
                "nodes": {
                    "gate": {
                        "type": "kolsch/nodes/branch",
                        "condition": False,
                    },
                }
            }
        )
        ex, _store, _run = _executor(tmp_path, workflow=wf)
        await ex.run()
        assert ex._captured["gate"] == {"value": False}  # noqa: SLF001

    async def test_truthy_non_bool_coerces(
        self, tmp_path: Path
    ) -> None:
        # Defensive: a Jinja expression returning an int (e.g.
        # ``length``) coerces to a bool via ``bool(...)``. Otherwise
        # ``compute_branch_value``'s ``isinstance(value, bool)`` guard
        # would route to ``None`` and the executor would fan out on
        # every outgoing edge.
        wf = _wf(
            {
                "nodes": {
                    "gate": {
                        "type": "kolsch/nodes/branch",
                        "condition": "{{ inputs.numbers | length }}",
                    },
                }
            }
        )
        ex, _store, _run = _executor(
            tmp_path, workflow=wf, inputs={"numbers": [1, 2]}
        )
        await ex.run()
        captured = ex._captured["gate"]  # noqa: SLF001
        assert captured == {"value": True}
        # The captured value is a real ``bool`` — not an ``int``.
        assert isinstance(captured["value"], bool)


# ---------------------------------------------------------------------------
# AC 0.4 — compute_branch_value extension
# ---------------------------------------------------------------------------


class TestComputeBranchValueExtension:
    def test_true_routes_true(self) -> None:
        node_def = BranchNodeDef(
            type="kolsch/nodes/branch", condition=True
        )
        assert (
            compute_branch_value(node_def, {"value": True}) is True
        )

    def test_false_routes_false(self) -> None:
        node_def = BranchNodeDef(
            type="kolsch/nodes/branch", condition=False
        )
        assert (
            compute_branch_value(node_def, {"value": False}) is False
        )

    def test_int_one_is_not_routed_as_bool(self) -> None:
        # ``isinstance(1, bool)`` is False — the guard rejects ints
        # so a stray non-bool capture doesn't accidentally route.
        node_def = BranchNodeDef(
            type="kolsch/nodes/branch", condition=True
        )
        assert compute_branch_value(node_def, {"value": 1}) is None

    def test_string_value_returns_none(self) -> None:
        node_def = BranchNodeDef(
            type="kolsch/nodes/branch", condition=True
        )
        assert (
            compute_branch_value(node_def, {"value": "true"}) is None
        )

    def test_missing_value_returns_none(self) -> None:
        node_def = BranchNodeDef(
            type="kolsch/nodes/branch", condition=True
        )
        assert compute_branch_value(node_def, {}) is None


# ---------------------------------------------------------------------------
# AC 0.5 + 0.6 — branch dispatch end-to-end
# ---------------------------------------------------------------------------


class TestBranchDispatch:
    async def test_true_arm_fires_false_pruned(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "gate": {
                        "type": "kolsch/nodes/branch",
                        "condition": "{{ inputs.go }}",
                    },
                    "on_true": {"type": "kolsch/nodes/noop"},
                    "on_false": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "gate",
                            "to": "on_true",
                            "branch": True,
                        },
                        {
                            "from": "gate",
                            "to": "on_false",
                            "branch": False,
                        },
                    ]
                },
            }
        )
        ex, store, run = _executor(
            tmp_path, workflow=wf, inputs={"go": True}
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        events = list(store.read_events(run.run_id))
        kinds_for = {
            (e.get("node_name"), e["kind"])
            for e in events
            if e.get("node_name") in {"on_true", "on_false"}
        }
        assert ("on_true", "workflow.node.completed") in kinds_for
        assert ("on_false", "workflow.node.pruned") in kinds_for

    async def test_false_arm_fires_true_pruned(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "gate": {
                        "type": "kolsch/nodes/branch",
                        "condition": "{{ inputs.go }}",
                    },
                    "on_true": {"type": "kolsch/nodes/noop"},
                    "on_false": {"type": "kolsch/nodes/noop"},
                },
                "graph": {
                    "edges": [
                        {
                            "from": "gate",
                            "to": "on_true",
                            "branch": True,
                        },
                        {
                            "from": "gate",
                            "to": "on_false",
                            "branch": False,
                        },
                    ]
                },
            }
        )
        ex, store, run = _executor(
            tmp_path, workflow=wf, inputs={"go": False}
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED
        events = list(store.read_events(run.run_id))
        kinds_for = {
            (e.get("node_name"), e["kind"])
            for e in events
            if e.get("node_name") in {"on_true", "on_false"}
        }
        assert ("on_false", "workflow.node.completed") in kinds_for
        assert ("on_true", "workflow.node.pruned") in kinds_for
