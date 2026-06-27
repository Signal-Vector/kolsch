"""Phase 3.6 Step 0 — :class:`FilterRunner` happy-path coverage.

Step 1 adds edge cases — empty list, non-list raises (run aborts),
scope shadowing, predicate raises mid-iteration. The happy paths
Step 0 ships sit at the top: default ``item_as`` over a list of
primitives, and object filtering with a custom ``item_as`` over a
list of dicts.
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


# ---------------------------------------------------------------------------
# Step 1 edge cases
# ---------------------------------------------------------------------------


class TestFilterEdgeCases:
    async def test_filter_empty_list_returns_empty(
        self, tmp_path: Path
    ) -> None:
        wf = _wf(
            {
                "nodes": {
                    "f": {
                        "type": "kolsch/nodes/filter",
                        "over": "{{ inputs.empty }}",
                        "condition": "{{ inputs.item > 5 }}",
                    },
                }
            }
        )
        ex, _store, _run = _executor(
            tmp_path, workflow=wf, inputs={"empty": []}
        )
        await ex.run()
        assert ex._captured["f"] == {"result": []}  # noqa: SLF001

    async def test_filter_non_list_raises(
        self, tmp_path: Path
    ) -> None:
        from workflows.run import (  # noqa: PLC0415
            WorkflowRunStatus,
        )

        wf = _wf(
            {
                "nodes": {
                    "f": {
                        "type": "kolsch/nodes/filter",
                        "over": "{{ inputs.scalar }}",
                        "condition": "{{ inputs.item > 5 }}",
                    },
                }
            }
        )
        ex, store, run = _executor(
            tmp_path,
            workflow=wf,
            inputs={"scalar": "not a list"},
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED
        events = list(store.read_events(run.run_id))
        failed = [
            e for e in events if e["kind"] == "workflow.node.failed"
        ]
        assert len(failed) == 1
        assert failed[0]["payload"]["error_class"] == "TypeError"
        assert events[-1]["kind"] == "workflow.run.failed"

    async def test_filter_item_as_shadows_bound_input(
        self, tmp_path: Path
    ) -> None:
        # The bind delivers ``item: "fallback"`` to the filter; the
        # per-element overlay MUST win on the same key so the
        # predicate sees the element, not the bound default.
        wf = _wf(
            {
                "nodes": {
                    "src": {"type": "kolsch/nodes/noop"},
                    "f": {
                        "type": "kolsch/nodes/filter",
                        "over": "{{ inputs.numbers }}",
                        "condition": "{{ inputs.item > 5 }}",
                    },
                },
                "graph": {
                    "edges": [
                        {
                            "from": "src",
                            "to": "f",
                            "binds": {
                                # ``item`` would otherwise mask the
                                # per-element binding for every
                                # iteration.
                                "item": "fallback",
                            },
                        }
                    ],
                },
            }
        )
        from workflows.builtins import NoopRunner  # noqa: PLC0415

        registry = _registry()
        registry.register("kolsch/nodes/noop", NoopRunner())
        store = WorkflowRunStore(tmp_path)
        run = store.create(
            workflow_ref="demo/workflows/shadow",
            inputs={"numbers": [3, 7, 5, 10]},
        )
        ex = IterativeExecutor(
            workflow=wf,
            run=run,
            store=store,
            registry=registry,
            catalog=AssetCatalog(),
        )
        await ex.run()
        assert ex._captured["f"] == {"result": [7, 10]}  # noqa: SLF001

    async def test_filter_predicate_raises_aborts_run(
        self, tmp_path: Path
    ) -> None:
        # Predicate fails on second element — ``10 / 0`` raises
        # ZeroDivisionError → executor's failure path → run aborts.
        # The first element's truthy result is DISCARDED; no partial
        # capture leaks through.
        from workflows.run import (  # noqa: PLC0415
            WorkflowRunStatus,
        )

        wf = _wf(
            {
                "nodes": {
                    "f": {
                        "type": "kolsch/nodes/filter",
                        "over": "{{ inputs.numbers }}",
                        "condition": "{{ (10 / inputs.item) > 0 }}",
                    },
                }
            }
        )
        ex, store, run = _executor(
            tmp_path,
            workflow=wf,
            inputs={"numbers": [2, 0, 5]},  # second triggers div-by-zero
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED
        # No captured output for the filter node — partial state
        # doesn't leak through despite the first element matching.
        assert "f" not in ex._captured  # noqa: SLF001
        events = list(store.read_events(run.run_id))
        assert events[-1]["kind"] == "workflow.run.failed"
