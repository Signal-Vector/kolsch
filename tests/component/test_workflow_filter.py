"""Phase 3.6 Step 0 — :class:`FilterRunner` happy-path coverage.

Step 1 (in a follow-up commit) adds edge cases — empty list,
non-list raises, custom ``item_as``, scope shadowing, predicate
raises mid-iteration. This file pins the happy paths Step 0 ships:
default ``item_as`` over a list of primitives, and object filtering
with a custom ``item_as`` over a list of dicts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from assets.catalog import AssetCatalog
from workflows.builtins import FilterRunner
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _registry() -> RunnerRegistry:
    reg = RunnerRegistry()
    reg.register("kolsch/nodes/filter", FilterRunner())
    return reg


def _executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    inputs: dict[str, Any] | None = None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(
        workflow_ref="demo/workflows/filter", inputs=inputs or {}
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
# Happy path — default item_as
# ---------------------------------------------------------------------------


class TestFilterDefaultItemAs:
    async def test_filter_primitives_default_item_as(
        self, tmp_path: Path
    ) -> None:
        # Per-element binding lands at ``inputs.<item_as>`` — the
        # Phase 3.3 ``inputs.*`` namespace is the single Jinja scope
        # for receiver-side per-iteration bindings (master spec §6.4.1).
        wf = _wf(
            {
                "nodes": {
                    "evens": {
                        "type": "kolsch/nodes/filter",
                        "over": "{{ inputs.numbers }}",
                        "condition": "{{ inputs.item > 5 }}",
                    },
                }
            }
        )
        ex, _store, _run = _executor(
            tmp_path,
            workflow=wf,
            inputs={"numbers": [3, 7, 5, 10]},
        )
        await ex.run()
        assert ex._captured["evens"] == {"result": [7, 10]}  # noqa: SLF001


# ---------------------------------------------------------------------------
# Happy path — custom item_as on object lists
# ---------------------------------------------------------------------------


class TestFilterCustomItemAs:
    async def test_filter_objects_custom_item_as(
        self, tmp_path: Path
    ) -> None:
        tickets = [
            {"id": 1, "urgent": True},
            {"id": 2, "urgent": False},
            {"id": 3, "urgent": True},
        ]
        wf = _wf(
            {
                "nodes": {
                    "urgents": {
                        "type": "kolsch/nodes/filter",
                        "over": "{{ inputs.tickets }}",
                        "condition": "{{ inputs.ticket.urgent }}",
                        "item_as": "ticket",
                    },
                }
            }
        )
        ex, _store, _run = _executor(
            tmp_path, workflow=wf, inputs={"tickets": tickets}
        )
        await ex.run()
        captured = ex._captured["urgents"]  # noqa: SLF001
        assert captured == {
            "result": [
                {"id": 1, "urgent": True},
                {"id": 3, "urgent": True},
            ]
        }
