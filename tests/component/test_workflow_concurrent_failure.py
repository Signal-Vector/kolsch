"""Phase 3.10 Step 3 — failure + cancel mid-fan-out (Phase Tests 9, 10).

Master spec §16.2.2 / §16.6:

- **Phase Test 9** — first failure cancels every other in-flight
  task. The failing node's ``workflow.node.failed`` carries the
  original ``error_class``; sibling cancellations land their own
  ``workflow.node.failed`` with
  ``error_class="asyncio.CancelledError"`` and
  ``reason="sibling_failure"``.
- **Phase Test 10** — ``cancel.flag`` written mid-fan-out aborts
  the run. The executor cancels every in-flight task, emits
  ``workflow.run.aborted``, and sibling cancellations carry
  ``reason="user_cancel"``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from assets.catalog import AssetCatalog
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path


class _SleepRunner:
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


class _FailingRunner:
    """Raises a typed exception after a deterministic delay so the
    concurrent worklist's failure path can observe it."""

    class _SentinelError(RuntimeError):
        pass

    def __init__(self, *, delay: float, message: str) -> None:
        self._delay = delay
        self._message = message

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
        raise self._SentinelError(self._message)


def _wf() -> Workflow:
    return Workflow.model_validate(
        {
            "concurrency": "auto",
            "nodes": {
                "start": {"type": "kolsch/nodes/noop"},
                "fast_failer": {
                    "type": "kolsch/nodes/bash",
                    "command": "echo this-fails",
                },
                "slow_sibling_a": {
                    "type": "kolsch/nodes/bash",
                    "command": "sleep 1",
                },
                "slow_sibling_b": {
                    "type": "kolsch/nodes/bash",
                    "command": "sleep 1",
                },
                "done": {"type": "kolsch/nodes/noop"},
            },
            "graph": {
                "edges": [
                    {"from": "start", "to": "fast_failer"},
                    {"from": "start", "to": "slow_sibling_a"},
                    {"from": "start", "to": "slow_sibling_b"},
                    {"from": "fast_failer", "to": "done"},
                    {"from": "slow_sibling_a", "to": "done"},
                    {"from": "slow_sibling_b", "to": "done"},
                ],
            },
        }
    )


def _failure_registry() -> RunnerRegistry:
    """The fast_failer fails almost immediately; the slow_siblings
    each sleep 1s so the cancellation arm has time to fire."""
    reg = RunnerRegistry()

    class _ByNodeRunner:
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
            if node_name == "fast_failer":
                await asyncio.sleep(0.05)
                raise _FailingRunner._SentinelError("fast_failer raised")
            if node_name.startswith("slow_sibling"):
                await asyncio.sleep(2.0)  # ample time for cancellation
                return {
                    "stdout": "won't reach",
                    "stderr": "",
                    "exit_code": 0,
                }
            return {"stdout": "x", "stderr": "", "exit_code": 0}

    reg.register("kolsch/nodes/bash", _ByNodeRunner())
    reg.register(
        "kolsch/nodes/noop", _SleepRunner(delay=0.0, output={})
    )
    return reg


def _slow_registry() -> RunnerRegistry:
    """Every bash sibling sleeps long enough for the cancel.flag
    test to observe them in flight."""
    reg = RunnerRegistry()
    reg.register(
        "kolsch/nodes/bash",
        _SleepRunner(
            delay=2.0,
            output={"stdout": "x", "stderr": "", "exit_code": 0},
        ),
    )
    reg.register(
        "kolsch/nodes/noop", _SleepRunner(delay=0.0, output={})
    )
    return reg


def _make_executor(
    tmp_path: Path, workflow: Workflow, registry: RunnerRegistry
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
# Phase Test 9 — first-failure-cancels-siblings
# ---------------------------------------------------------------------------


class TestFirstFailureCancelsSiblings:
    @pytest.mark.asyncio
    async def test_failing_node_carries_original_error_class(
        self, tmp_path: Path
    ) -> None:
        ex, store, run = _make_executor(
            tmp_path, _wf(), _failure_registry()
        )
        status = await ex.run()
        assert status == WorkflowRunStatus.FAILED

        events = list(store.read_events(run.run_id))
        # The first failed event should be for the fast_failer with
        # its ORIGINAL error_class (not asyncio.CancelledError).
        failed_events = [
            e for e in events if e.get("kind") == "workflow.node.failed"
        ]
        fast_failed = next(
            e for e in failed_events if e["node_name"] == "fast_failer"
        )
        assert fast_failed["payload"]["error_class"] == "_SentinelError"
        # Sibling cancellations land their own workflow.node.failed
        # with error_class="asyncio.CancelledError" + reason="sibling_failure".
        sibling_a_failed = next(
            e for e in failed_events if e["node_name"] == "slow_sibling_a"
        )
        sibling_b_failed = next(
            e for e in failed_events if e["node_name"] == "slow_sibling_b"
        )
        for sibling in (sibling_a_failed, sibling_b_failed):
            assert (
                sibling["payload"]["error_class"]
                == "asyncio.CancelledError"
            )
            assert sibling["payload"]["reason"] == "sibling_failure"

        # The run.failed event chains after every node.failed and
        # names fast_failer as the cause.
        run_failed = next(
            e for e in events if e.get("kind") == "workflow.run.failed"
        )
        assert "fast_failer" in run_failed["payload"]["reason"]

    @pytest.mark.asyncio
    async def test_run_terminates_quickly_after_first_failure(
        self, tmp_path: Path
    ) -> None:
        """Without sibling cancellation, the slow_siblings would each
        sleep 2s. With cancellation, the run terminates as soon as
        the fast_failer raises."""
        import time as _time

        ex, _, _ = _make_executor(
            tmp_path, _wf(), _failure_registry()
        )
        t0 = _time.perf_counter()
        status = await ex.run()
        elapsed = _time.perf_counter() - t0
        assert status == WorkflowRunStatus.FAILED
        # The failing node sleeps 0.05s; siblings sleep 2s each. With
        # cancellation working the elapsed time stays well under
        # 1.0s — generous slack accommodates cancel-arm overhead.
        assert elapsed < 1.0, (
            f"sibling cancellation didn't fire — elapsed {elapsed:.3f}s"
        )


# ---------------------------------------------------------------------------
# Phase Test 10 — cancel.flag mid-fan-out
# ---------------------------------------------------------------------------


class TestCancelFlagMidFanOut:
    @pytest.mark.asyncio
    async def test_cancel_flag_aborts_in_flight_batch(
        self, tmp_path: Path
    ) -> None:
        """Operator writes ``cancel.flag`` mid-fan-out. The executor
        cancels every in-flight task and emits ``workflow.run.aborted``;
        sibling cancellations land with ``reason="user_cancel"``."""
        ex, store, run = _make_executor(
            tmp_path, _wf(), _slow_registry()
        )
        # Touch the cancel.flag shortly after kicking off the run so
        # the dispatch loop observes it AT THE TOP of an iteration.
        run_dir = store.directory(run.run_id)

        async def _touch_flag() -> None:
            # Wait long enough for the first dispatch batch to land.
            await asyncio.sleep(0.1)
            run_dir.root.mkdir(parents=True, exist_ok=True)
            run_dir.cancel_flag_path.touch()

        flag_task = asyncio.create_task(_touch_flag())
        status = await ex.run()
        await flag_task

        assert status == WorkflowRunStatus.ABORTED
        events = list(store.read_events(run.run_id))
        # Confirm workflow.run.aborted lands.
        aborted = [
            e for e in events if e.get("kind") == "workflow.run.aborted"
        ]
        assert len(aborted) == 1
        # Sibling cancellations: each in-flight sibling emits
        # workflow.node.failed with reason="user_cancel".
        failed_events = [
            e for e in events if e.get("kind") == "workflow.node.failed"
        ]
        assert failed_events, "no sibling failed events emitted"
        for ev in failed_events:
            assert (
                ev["payload"]["error_class"] == "asyncio.CancelledError"
            )
            assert ev["payload"]["reason"] == "user_cancel"
