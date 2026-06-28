"""Phase 3.10 Step 3 — auto-mode concurrent worklist tests.

Covers Phase Tests 1, 2 from the build doc:

- **Phase Test 1** — three sibling bash-like nodes (each sleeping
  200ms) under ``concurrency: auto`` complete in < 400ms wall-clock;
  the same workflow under ``concurrency: sequential`` exceeds 550ms.
- **Phase Test 2** — a workflow with no ``concurrency:`` field
  produces byte-identical JSONL to its pre-Phase-3.10 shape (no
  ``concurrency_mode`` / ``peak_in_flight`` keys present).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from assets.catalog import AssetCatalog
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore


class _SleepRunner:
    """Asyncio-sleep runner — returns the configured output after
    a deterministic delay. Used to make the wall-clock gap between
    sequential (sum) and concurrent (max) measurable."""

    def __init__(self, *, delay: float, output: dict[str, Any]) -> None:
        self._delay = delay
        self._output = output

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
        await asyncio.sleep(self._delay)
        return dict(self._output)


def _wf(payload: dict[str, Any]) -> Workflow:
    return Workflow.model_validate(payload)


def _build_three_sibling_workflow(*, concurrency: str) -> Workflow:
    """Three sibling bash-ish nodes (alpha, beta, gamma) feeding into
    a single confluence node (``done``).
    """
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


def _make_registry_with_sleep(delay: float) -> RunnerRegistry:
    registry = RunnerRegistry()
    registry.register(
        "kolsch/nodes/bash",
        _SleepRunner(
            delay=delay,
            output={"stdout": "x\n", "stderr": "", "exit_code": 0},
        ),
    )
    registry.register(
        "kolsch/nodes/noop", _SleepRunner(delay=0.0, output={})
    )
    return registry


def _make_executor(
    tmp_path: Path,
    *,
    workflow: Workflow,
    registry: RunnerRegistry,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/fanout", inputs={})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
    )
    return ex, store, run


# ---------------------------------------------------------------------------
# Phase Test 1 — auto-mode wall-clock speedup
# ---------------------------------------------------------------------------


class TestAutoModeWallClockSpeedup:
    @pytest.mark.asyncio
    async def test_auto_mode_runs_concurrent(self, tmp_path: Path) -> None:
        """Phase Test 1 — three sibling sleep nodes under
        ``concurrency: auto`` finish in roughly one slot's worth of
        time, not three. Confirms the dispatch fork really fires the
        ready nodes concurrently."""
        wf = _build_three_sibling_workflow(concurrency="auto")
        registry = _make_registry_with_sleep(delay=0.2)
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )
        t0 = time.perf_counter()
        status = await ex.run()
        elapsed = time.perf_counter() - t0
        assert status == WorkflowRunStatus.SUCCEEDED
        # Three siblings each sleeping 200ms — concurrent should
        # finish in under 400ms (a generous slack vs. the 200ms
        # ideal); sequential would need 600ms.
        assert elapsed < 0.4, f"auto-mode wall-clock {elapsed:.3f}s ≥ 0.4s"

        # peak_in_flight tracked: with the start node feeding three
        # siblings, all three dispatch in one batch.
        assert ex._peak_in_flight >= 3

        events = list(store.read_events(run.run_id))
        # The terminal event carries peak_in_flight under auto mode.
        completed = next(
            e for e in events if e.get("kind") == "workflow.run.completed"
        )
        assert completed["payload"].get("peak_in_flight") == ex._peak_in_flight
        # The run.started event carries concurrency_mode.
        started = next(
            e for e in events if e.get("kind") == "workflow.run.started"
        )
        assert started["payload"].get("concurrency_mode") == "auto"

    @pytest.mark.asyncio
    async def test_sequential_mode_runs_serially(self, tmp_path: Path) -> None:
        """The SAME workflow under ``concurrency: sequential`` runs
        serially — wall-clock approximates the SUM of the per-node
        sleeps (>= 600ms minus loop overhead). We assert > 550ms with
        slack so a fast machine running the asyncio loop quickly still
        observes a clear sequential signal."""
        wf = _build_three_sibling_workflow(concurrency="sequential")
        registry = _make_registry_with_sleep(delay=0.2)
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )
        t0 = time.perf_counter()
        status = await ex.run()
        elapsed = time.perf_counter() - t0
        assert status == WorkflowRunStatus.SUCCEEDED
        # Three 200ms sleeps in sequence — wall-clock should exceed
        # 550ms (the 50ms slack accounts for loop overhead).
        assert elapsed > 0.55, f"sequential wall-clock {elapsed:.3f}s ≤ 0.55s"

        # Sequential mode does NOT emit peak_in_flight on the run.completed
        # event (byte-identity with pre-Phase-3.10 goldens).
        events = list(store.read_events(run.run_id))
        completed = next(
            e for e in events if e.get("kind") == "workflow.run.completed"
        )
        assert "peak_in_flight" not in completed.get("payload", {})
        started = next(
            e for e in events if e.get("kind") == "workflow.run.started"
        )
        assert "concurrency_mode" not in started.get("payload", {})


# ---------------------------------------------------------------------------
# Phase Test 2 — sequential default unchanged (byte-identity with pre-3.10)
# ---------------------------------------------------------------------------


class TestSequentialDefaultUnchanged:
    @pytest.mark.asyncio
    async def test_workflow_without_concurrency_field_is_sequential(
        self, tmp_path: Path
    ) -> None:
        """A workflow that omits ``concurrency:`` defaults to
        ``sequential`` AND its JSONL payload shape matches the
        pre-Phase-3.10 keys exactly — no ``concurrency_mode`` /
        ``peak_in_flight`` leakage."""
        wf = _wf(
            {
                "nodes": {
                    "greet": {
                        "type": "kolsch/nodes/bash",
                        "command": "echo hi",
                    },
                },
                "graph": {"edges": []},
            }
        )
        registry = RunnerRegistry()
        registry.register(
            "kolsch/nodes/bash",
            _SleepRunner(
                delay=0.0,
                output={"stdout": "hi\n", "stderr": "", "exit_code": 0},
            ),
        )
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED

        events = list(store.read_events(run.run_id))
        # AC 3.9 — sequential JSONL is byte-identical to Phase 3.9.
        # The two new optional fields must NOT appear in the payload.
        started = next(
            e for e in events if e.get("kind") == "workflow.run.started"
        )
        assert "concurrency_mode" not in started["payload"]
        completed = next(
            e for e in events if e.get("kind") == "workflow.run.completed"
        )
        assert "peak_in_flight" not in completed["payload"]

    @pytest.mark.asyncio
    async def test_explicit_sequential_field_also_omits_payload_fields(
        self, tmp_path: Path
    ) -> None:
        """``concurrency: sequential`` (explicit) is byte-identical
        to the default; both omit the auto-only payload fields."""
        wf = _wf(
            {
                "concurrency": "sequential",
                "nodes": {
                    "n": {"type": "kolsch/nodes/noop"},
                },
                "graph": {"edges": []},
            }
        )
        registry = RunnerRegistry()
        registry.register(
            "kolsch/nodes/noop", _SleepRunner(delay=0.0, output={})
        )
        ex, store, run = _make_executor(
            tmp_path, workflow=wf, registry=registry
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.SUCCEEDED

        events = list(store.read_events(run.run_id))
        started = next(
            e for e in events if e.get("kind") == "workflow.run.started"
        )
        assert "concurrency_mode" not in started["payload"]
        completed = next(
            e for e in events if e.get("kind") == "workflow.run.completed"
        )
        assert "peak_in_flight" not in completed["payload"]
