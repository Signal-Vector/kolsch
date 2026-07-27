"""bug-096 T1/T2 — RUN_STARTED carries the topological node manifest.

The executor emits a ``nodes`` field on the ``workflow.run.started``
domain payload so the chat UI can pre-mount one placeholder row per
top-level node (see
``WorkflowRenderer._mount_placeholder_rows``). The manifest is a list
of ``{name, type, predecessors}`` in topological order, predecessors
the full reachability closure sorted alphabetically for byte-stable
JSONL, map-worker-only templates excluded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from assets.catalog import AssetCatalog
from workflows.builtins import ComputeRunner, MapRunner, NoopRunner
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path


class _StaticRunner:
    def __init__(self, output: dict[str, Any] | None = None) -> None:
        self._output = output or {}

    async def run(self, **_: Any) -> dict[str, Any]:
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
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )
    return ex, store, run


def _run_started_nodes(store: WorkflowRunStore, run_id: Any) -> list[dict[str, Any]]:
    events = list(store.read_events(run_id))
    started = next(e for e in events if e["kind"] == "workflow.run.started")
    return started["payload"]["nodes"]


async def test_run_started_emits_topological_node_manifest(tmp_path: Path) -> None:
    """T1: a 5-node linear chain a→b→c→d→e emits the full closure."""
    wf = _wf(
        {
            "nodes": {
                "a": {"type": "kolsch/nodes/noop"},
                "b": {"type": "kolsch/nodes/noop"},
                "c": {"type": "kolsch/nodes/noop"},
                "d": {"type": "kolsch/nodes/noop"},
                "e": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {"from": "a", "to": "b"},
                    {"from": "b", "to": "c"},
                    {"from": "c", "to": "d"},
                    {"from": "d", "to": "e"},
                ]
            },
        }
    )
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/noop", _StaticRunner({}))
    ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
    status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED

    nodes = _run_started_nodes(store, run.run_id)
    assert nodes == [
        {"name": "a", "type": "kolsch/nodes/noop", "predecessors": []},
        {"name": "b", "type": "kolsch/nodes/noop", "predecessors": ["a"]},
        {"name": "c", "type": "kolsch/nodes/noop", "predecessors": ["a", "b"]},
        {"name": "d", "type": "kolsch/nodes/noop", "predecessors": ["a", "b", "c"]},
        {
            "name": "e",
            "type": "kolsch/nodes/noop",
            "predecessors": ["a", "b", "c", "d"],
        },
    ]


async def test_run_started_omits_map_worker_only_nodes(tmp_path: Path) -> None:
    """T2: the map node appears in the manifest; its worker does NOT."""
    wf = _wf(
        {
            "nodes": {
                "echo_one": {
                    "type": "kolsch/nodes/compute",
                    "value": "{{ inputs.item }}",
                },
                "fan_out": {
                    "type": "kolsch/nodes/map",
                    "apply": "echo_one",
                    "over": [1, 2, 3],
                    "item_as": "item",
                },
            },
            "graph": {"edges": []},
        }
    )
    registry = RunnerRegistry()
    registry.register("kolsch/nodes/noop", NoopRunner())
    registry.register("kolsch/nodes/compute", ComputeRunner())
    registry.register("kolsch/nodes/map", MapRunner(registry=registry))
    ex, store, run = _make_executor(tmp_path, workflow=wf, registry=registry)
    status = await ex.run()
    assert status == WorkflowRunStatus.SUCCEEDED

    nodes = _run_started_nodes(store, run.run_id)
    names = [entry["name"] for entry in nodes]
    assert "fan_out" in names
    assert "echo_one" not in names
    fan_out = next(entry for entry in nodes if entry["name"] == "fan_out")
    assert fan_out["type"] == "kolsch/nodes/map"
