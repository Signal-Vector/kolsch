"""Phase 3.10 Step 3 — auto-mode JSONL replay parity (Phase Test 7).

Master spec §16.5.1 — the replay machinery at
:mod:`workflows._replay` rebuilds ``_captured`` / ``_state`` /
``_pruned`` / ``_completed_or_skipped`` from JSONL events
line-by-line. Under concurrent fan-out the JSONL interleaves
multiple invocations' events; the replay's per-invocation
correlation must still rebuild the same final state as a
sequential variant of the same workflow.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from assets.catalog import AssetCatalog
from workflows._replay import replay_into
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path


class _DeterministicRunner:
    """Returns a stable per-node output keyed by ``node_name`` so the
    parallel + sequential variants produce IDENTICAL captured dicts
    even though their JSONL interleaving differs."""

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
        # Slight async-sleep so the parallel variant DOES interleave
        # rather than serialising naturally on a fast machine.
        await asyncio.sleep(0.01)
        return dict(self._outputs.get(node_name, {}))


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _build_fan_out(*, concurrency: str) -> Workflow:
    return _wf(
        {
            "concurrency": concurrency,
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
    runner = _DeterministicRunner(
        {
            "alpha": {"stdout": "alpha\n", "stderr": "", "exit_code": 0},
            "beta": {"stdout": "beta\n", "stderr": "", "exit_code": 0},
            "gamma": {"stdout": "gamma\n", "stderr": "", "exit_code": 0},
        }
    )
    reg.register("kolsch/nodes/bash", runner)
    reg.register("kolsch/nodes/noop", _DeterministicRunner({}))
    return reg


async def _run(
    tmp_path: Path, workflow: Workflow
) -> tuple[WorkflowRunStore, Any, WorkflowRunStatus]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/fanout", inputs={})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=_registry(),
        catalog=AssetCatalog(),
    )
    status = await ex.run()
    return store, run, status


class _ReplayStub:
    """Bare-minimum stand-in for the executor's private fields the
    :func:`replay_into` walker mutates. Mirrors the shape of
    :class:`workflows.executor.IterativeExecutor` to avoid spinning
    a second executor for the replay assertion."""

    def __init__(self) -> None:
        self._captured: dict[str, dict[str, Any]] = {}
        self._completed_or_skipped: set[str] = set()
        self._pruned: set[str] = set()
        self._state: dict[str, Any] = {}
        self._pruned_edges: set[tuple[str, str]] = set()
        self._workflow_node_child_runs: dict[str, str] = {}


@pytest.mark.asyncio
async def test_auto_mode_replay_matches_sequential(tmp_path: Path) -> None:
    """Phase Test 7 — an ``auto``-mode run's JSONL replays into the
    same ``_captured`` / ``_completed_or_skipped`` / ``_state`` as
    a ``sequential``-mode run of the SAME workflow."""
    seq_ws = tmp_path / "seq"
    par_ws = tmp_path / "par"
    seq_ws.mkdir()
    par_ws.mkdir()

    store_seq, run_seq, status_seq = await _run(
        seq_ws, _build_fan_out(concurrency="sequential")
    )
    store_par, run_par, status_par = await _run(
        par_ws, _build_fan_out(concurrency="auto")
    )
    assert status_seq == WorkflowRunStatus.SUCCEEDED
    assert status_par == WorkflowRunStatus.SUCCEEDED

    # Replay both JSONLs into stand-in executors.
    stub_seq = _ReplayStub()
    stub_par = _ReplayStub()
    replay_into(stub_seq, store_seq.read_events(run_seq.run_id))
    replay_into(stub_par, store_par.read_events(run_par.run_id))

    # The captured dicts must match key-by-key (same outputs from
    # the same nodes, regardless of completion order).
    assert stub_seq._captured == stub_par._captured
    assert stub_seq._completed_or_skipped == stub_par._completed_or_skipped
    assert stub_seq._pruned == stub_par._pruned


@pytest.mark.asyncio
async def test_auto_mode_jsonl_has_all_three_completions(
    tmp_path: Path,
) -> None:
    """Even when the JSONL interleaves events for three concurrent
    branches, the replay still observes one completion event per
    branch — the per-invocation correlation is intact."""
    store, run, status = await _run(tmp_path, _build_fan_out(concurrency="auto"))
    assert status == WorkflowRunStatus.SUCCEEDED

    events = list(store.read_events(run.run_id))
    completed_by_node: list[str] = [
        e["node_name"] for e in events if e.get("kind") == "workflow.node.completed"
    ]
    # All five nodes (start, alpha, beta, gamma, done) emit a
    # completion event — order is non-deterministic for the three
    # concurrent siblings but every one must appear.
    assert sorted(completed_by_node) == [
        "alpha",
        "beta",
        "done",
        "gamma",
        "start",
    ]
