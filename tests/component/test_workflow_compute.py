"""Phase 3.6 Step 0 — :class:`ComputeRunner` happy-path coverage.

Step 1 ships the broader surface — 8 aggregation recipes
(``sum`` / ``count`` / ``min`` / ``max`` / ``sort`` / ``flatten`` /
``any`` / ``all``) and the NativeEnvironment typed-return regression
suite. This file pins the Step 0 happy paths: Jinja expression
returning a typed int, plus YAML literal pass-through for int and
list values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from assets.catalog import AssetCatalog
from workflows.builtins import ComputeRunner
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _registry() -> RunnerRegistry:
    reg = RunnerRegistry()
    reg.register("kolsch/nodes/compute", ComputeRunner())
    return reg


def _executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    inputs: dict[str, Any] | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(
        workflow_ref="demo/workflows/compute", inputs=inputs or {}
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
# Happy path — Jinja returns typed Python value
# ---------------------------------------------------------------------------


class TestComputeJinja:
    async def test_int_arithmetic_returns_typed_int(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "calc": {
                        "type": "kolsch/nodes/compute",
                        "value": "{{ 1 + 1 }}",
                    },
                }
            }
        )
        ex, _store, _run = _executor(tmp_path, workflow=wf)
        await ex.run()
        captured = ex._captured["calc"]  # noqa: SLF001
        assert captured == {"value": 2}
        # NativeEnvironment preserves the int type — not "2".
        assert isinstance(captured["value"], int)
        assert not isinstance(captured["value"], bool)


# ---------------------------------------------------------------------------
# Happy path — YAML literals pass through verbatim
# ---------------------------------------------------------------------------


class TestComputeLiteral:
    async def test_int_literal_passes_through(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "lit": {
                        "type": "kolsch/nodes/compute",
                        "value": 42,
                    },
                }
            }
        )
        ex, _store, _run = _executor(tmp_path, workflow=wf)
        await ex.run()
        assert ex._captured["lit"] == {"value": 42}  # noqa: SLF001

    async def test_list_literal_passes_through(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "lit": {
                        "type": "kolsch/nodes/compute",
                        "value": [1, 2, 3],
                    },
                }
            }
        )
        ex, _store, _run = _executor(tmp_path, workflow=wf)
        await ex.run()
        assert ex._captured["lit"] == {"value": [1, 2, 3]}  # noqa: SLF001
