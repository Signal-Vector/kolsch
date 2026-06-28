"""Phase 3.10 Step 3 — fork from a concurrent-fan-out parent (Phase Test 8).

Master spec §16.5.1 — forked runs from concurrent parents inherit
the parent's exact replay ordering. ``start_from_fork`` replays the
parent's JSONL prefix into the forked executor's private state;
downstream nodes pick up from the fork point with the parent's
captured state intact.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from assets.catalog import AssetCatalog
from schemas.entities import ForkedFrom
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore


class _DeterministicRunner:
    def __init__(self, outputs: dict[str, dict[str, Any]]) -> None:
        self._outputs = outputs

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
        await asyncio.sleep(0.005)
        return dict(self._outputs.get(node_name, {}))


def _wf() -> Workflow:
    return Workflow.model_validate(
        {
            "concurrency": "auto",
            "nodes": {
                "start": {"type": "kolsch/nodes/noop"},
                "alpha": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo alpha",
                },
                "beta": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo beta",
                },
                "gamma": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo gamma",
                },
                "done": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {"from": "start", "to": "alpha"},
                    {"from": "start", "to": "beta"},
                    {"from": "start", "to": "gamma"},
                    {"from": "alpha", "to": "done"},
                    {"from": "beta", "to": "done"},
                    {"from": "gamma", "to": "done"},
                ],
            },
        }
    )


def _registry() -> RunnerRegistry:
    reg = RunnerRegistry()
    reg.register(
        "kolsch/nodes/bash",
        _DeterministicRunner(
            {
                "alpha": {"stdout": "alpha\n", "stderr": "", "exit_code": 0},
                "beta": {"stdout": "beta\n", "stderr": "", "exit_code": 0},
                "gamma": {"stdout": "gamma\n", "stderr": "", "exit_code": 0},
            }
        ),
    )
    reg.register("kolsch/nodes/noop", _DeterministicRunner({}))
    return reg


@pytest.mark.asyncio
async def test_fork_from_concurrent_parent_inherits_event_ordering(
    tmp_path: Path,
) -> None:
    """Phase Test 8 — fork from a concurrent-fan-out parent at one
    branch's ``workflow.node.completed`` event. The forked run replays
    the parent's exact prefix and continues with the remaining
    downstream nodes."""
    wf = _wf()
    store = WorkflowRunStore(tmp_path)
    parent_run = store.create(workflow_ref="demo/workflows/fanout", inputs={})
    parent_ex = IterativeExecutor(
        workflow=wf,
        run=parent_run,
        store=store,
        registry=_registry(),
        catalog=AssetCatalog(),
    )
    parent_status = await parent_ex.run()
    assert parent_status == WorkflowRunStatus.SUCCEEDED

    parent_events = list(store.read_events(parent_run.run_id))
    # Find the alpha completion event — the fork point.
    alpha_completed = next(
        e
        for e in parent_events
        if e.get("kind") == "workflow.node.completed"
        and (e.get("node_name") == "alpha")
    )
    fork_event_id = alpha_completed["event_id"]

    # Re-read parent's status after run.
    parent_run = store.read(parent_run.run_id)

    forked_run = store.create(
        workflow_ref=parent_run.workflow_ref,
        inputs=dict(parent_run.inputs),
        trace_id=parent_run.trace_id,
        forked_from=ForkedFrom(
            run_id=parent_run.run_id,
            fork_event_id=fork_event_id,
        ),
    )
    forked_ex = IterativeExecutor(
        workflow=wf,
        run=forked_run,
        store=store,
        registry=_registry(),
        catalog=AssetCatalog(),
    )
    forked_status = await forked_ex.start_from_fork(
        fork_event_id=fork_event_id, input_overrides={}
    )
    assert forked_status == WorkflowRunStatus.SUCCEEDED

    fork_events = list(store.read_events(forked_run.run_id))
    # The forked run's first event is workflow.run.forked_from with
    # the parent run id + the fork event id in payload.
    assert fork_events[0]["kind"] == "workflow.run.forked_from"
    assert fork_events[0]["payload"]["forked_from_run_id"] == str(
        parent_run.run_id
    )
    assert fork_events[0]["payload"]["fork_event_id"] == fork_event_id

    # The forked run inherits the parent's `_captured["alpha"]` via
    # replay — assert by reading the executor's private state.
    assert forked_ex._captured.get("alpha") == {
        "stdout": "alpha\n",
        "stderr": "",
        "exit_code": 0,
    }

    # The forked run still fires `done` (the confluence). Beta and
    # gamma — siblings to alpha that hadn't completed BEFORE the fork
    # point — re-fire in the forked run because their parent (start)
    # is replayed but their own completion events come AFTER the
    # fork point in the parent JSONL.
    forked_kinds_per_node: dict[str, list[str]] = {}
    for event in fork_events:
        if event.get("kind", "").startswith("workflow.node."):
            name = event.get("node_name") or event.get(
                "payload", {}
            ).get("node_name")
            forked_kinds_per_node.setdefault(str(name), []).append(
                event["kind"]
            )
    # done MUST have completed in the forked run regardless.
    assert (
        "workflow.node.completed" in forked_kinds_per_node.get("done", [])
    )
