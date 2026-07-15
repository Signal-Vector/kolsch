"""spec-069 Part A — default per-node execution timeout.

Covers Acceptance Criteria 1-3 of `spec-069-structured-cancellable-
workers-and-timeout-primitive.md`:

- **AC1** — a workflow node with no explicit ``timeout_seconds:`` is
  bounded by a configurable default timeout; on expiry the node
  await is hard-cancelled (via ``asyncio.wait_for``) even if the
  node never polls its cancel token, and a ``workflow.node.failed``
  (timeout) terminal is emitted.
- **AC2** — after a node timeout, the run reaches a terminal
  phase and the executor's ``NodeTimeoutExceeded`` unwinds through
  the existing catch-arm (``executor.py:1493``) as
  ``WorkflowNodeFailedEvent`` + ``WorkflowRunFailedEvent``.
- **AC3** — explicit per-node ``timeout_seconds:`` still overrides
  the default.

The tests exercise the executor directly (``IterativeExecutor``)
rather than going through ``WorkflowsImpl``; the wrapper is the
same in both paths (``_apply_node_modifiers`` in
``src/workflows/executor.py``). A configurable-short default (500ms)
keeps the tests fast without introducing brittleness.

The ``running`` count reconciliation invariant (AC2's ``running == 0``
after finalize) lives in a sibling test focused on
``WorkflowsImpl._finalize_terminal`` — see the ``TestNodeCountReconciles``
class at the tail of this module.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from assets.catalog import AssetCatalog
from cli.chat.services.workflows_impl import (
    WorkflowRunRecord,
    WorkflowRunSpec,
    WorkflowsImpl,
)
from store import SqliteBackend
from store.session_store import SessionStore
from tools.services.workflows import NodeCounts
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.ids import WorkflowRunId
from workflows.parser import Workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures — a stalled runner that NEVER polls its cancel token.
# ---------------------------------------------------------------------------


class _StallForeverRunner:
    """Runner whose ``run`` awaits an event that never fires.

    Simulates the wedged 2-node workflow shape from spec-069's
    Background (defect 1): the agent stream stalls indefinitely,
    the runner does NOT poll ``ctx.cancel_token``, so unwinding
    depends entirely on the executor wrapping the await with a
    hard-cancelling timeout. If the wrapper is absent, the test
    hangs — which is exactly the failure mode we're guarding
    against.
    """

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
        # asyncio.Event.wait() is a plain uncoop-cancel-safe await;
        # `wait_for` will hard-cancel this coroutine on expiry via
        # standard task cancellation. The wrap is the ONLY thing that
        # unwinds this — we deliberately do NOT poll ctx.cancel_token
        # so we prove the timeout doesn't depend on cooperative
        # polling.
        await asyncio.Event().wait()
        return {}


class _FastRunner:
    """Runner that returns immediately with a fixed output."""

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
        return {"ok": True}


def _stall_registry() -> RunnerRegistry:
    reg = RunnerRegistry()
    reg.register("kolsch/nodes/noop", _StallForeverRunner())
    return reg


def _fast_registry() -> RunnerRegistry:
    reg = RunnerRegistry()
    reg.register("kolsch/nodes/noop", _FastRunner())
    return reg


def _single_node_workflow(*, timeout_seconds: int | None = None) -> Workflow:
    """Build a one-node workflow.

    The YAML schema requires ``timeout_seconds`` to be an integer
    (see ``src/workflows/parser.py``), so the override test uses
    integer seconds and picks stall/complete latencies on either
    side of the default rather than sub-second overrides. Any
    override still exercises the same ``_apply_node_modifiers``
    "explicit wins" branch — the value's magnitude only matters
    for keeping the test fast.
    """
    node: dict[str, Any] = {"type": "kolsch/nodes/noop"}
    if timeout_seconds is not None:
        node["timeout_seconds"] = timeout_seconds
    return Workflow.model_validate({"nodes": {"only": node}, "graph": {"edges": []}})


def _make_executor(
    tmp_path: Path,
    workflow: Workflow,
    registry: RunnerRegistry,
    *,
    default_node_timeout_seconds: float | None,
) -> tuple[IterativeExecutor, WorkflowRunStore, Any]:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="tests/workflows/node_default_timeout", inputs={})
    ex = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=store,
        registry=registry,
        catalog=AssetCatalog(),
        default_node_timeout_seconds=default_node_timeout_seconds,
    )
    return ex, store, run


# ---------------------------------------------------------------------------
# AC1 / AC2 — a stalled node unwinds via the default timeout.
# ---------------------------------------------------------------------------


class TestDefaultNodeTimeoutUnwindsStall:
    @pytest.mark.asyncio
    async def test_stalled_node_hits_default_timeout_and_run_fails(
        self, tmp_path: Path
    ) -> None:
        """A stalled node (never polls cancel token) fails within the
        default-timeout window; the run terminates with a
        ``workflow.node.failed`` (``NodeTimeoutExceeded``) followed by
        ``workflow.run.failed``.
        """
        ex, store, run = _make_executor(
            tmp_path,
            _single_node_workflow(),
            _stall_registry(),
            default_node_timeout_seconds=0.5,
        )

        # A generous asyncio.wait_for around the run keeps the test
        # suite from hanging forever if the wrap regresses; the
        # timeout itself (500ms) is enforced by the executor.
        status = await asyncio.wait_for(ex.run(), timeout=5.0)
        assert status == WorkflowRunStatus.FAILED

        events = list(store.read_events(run.run_id))
        failed = [e for e in events if e.get("kind") == "workflow.node.failed"]
        assert len(failed) == 1, (
            f"expected exactly one workflow.node.failed, got {len(failed)}"
        )
        assert failed[0]["node_name"] == "only"
        assert failed[0]["payload"]["error_class"] == "NodeTimeoutExceeded"
        # The generic exception arm at executor.py:1493 chains a
        # run.failed after the node.failed; verify both landed.
        run_failed = [e for e in events if e.get("kind") == "workflow.run.failed"]
        assert len(run_failed) == 1


# ---------------------------------------------------------------------------
# AC3 — explicit per-node timeout still wins over the default.
# ---------------------------------------------------------------------------


class TestExplicitTimeoutOverridesDefault:
    @pytest.mark.asyncio
    async def test_explicit_short_override_wins_when_default_is_long(
        self, tmp_path: Path
    ) -> None:
        """With a 60s default and an explicit 1s override, the
        node fails at ~1s — the explicit value took effect.

        Without the override the node would hang for 60s; we use
        the stall runner + a short explicit override so a bounded
        wait proves the override fires and the default was NOT
        applied.
        """
        ex, store, run = _make_executor(
            tmp_path,
            _single_node_workflow(timeout_seconds=1),
            _stall_registry(),
            default_node_timeout_seconds=60.0,
        )
        status = await asyncio.wait_for(ex.run(), timeout=5.0)
        assert status == WorkflowRunStatus.FAILED
        events = list(store.read_events(run.run_id))
        failed = [e for e in events if e.get("kind") == "workflow.node.failed"]
        assert len(failed) == 1
        assert failed[0]["payload"]["error_class"] == "NodeTimeoutExceeded"
        assert (
            failed[0]["payload"]["message"].startswith("Node 'only' exceeded timeout")
            or "1" in failed[0]["payload"]["message"]
        ), (
            "expected the failure message to reference the explicit 1s "
            "timeout, not the 60s default"
        )

    @pytest.mark.asyncio
    async def test_explicit_long_override_wins_when_default_is_short(
        self, tmp_path: Path
    ) -> None:
        """With a 500ms default and an explicit 5s override, a
        1s sleep node completes — the override kept the node
        alive past the default that would otherwise have killed it.
        """

        class _SleepRunner:
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
                await asyncio.sleep(1.0)
                return {"ok": True}

        reg = RunnerRegistry()
        reg.register("kolsch/nodes/noop", _SleepRunner())
        ex, store, run = _make_executor(
            tmp_path,
            _single_node_workflow(timeout_seconds=5),
            reg,
            default_node_timeout_seconds=0.5,
        )
        status = await asyncio.wait_for(ex.run(), timeout=5.0)
        # No timeout — the override kept us alive.
        assert status == WorkflowRunStatus.SUCCEEDED
        events = list(store.read_events(run.run_id))
        completed = [e for e in events if e.get("kind") == "workflow.node.completed"]
        assert len(completed) == 1


# ---------------------------------------------------------------------------
# AC2 — WorkflowsImpl._finalize_terminal zeros `node_counts.running`.
# ---------------------------------------------------------------------------


async def _mk_impl(
    tmp_path: Path,
) -> tuple[WorkflowsImpl, SessionStore, Any, Any]:
    """Minimal WorkflowsImpl for the reconciliation test.

    No runtime / cards / prompts / bus — those are only used by
    surfaces this test does not exercise. Matches the pattern
    used by other services-level tests in ``tests/cli/chat/services/``.
    """
    (tmp_path / ".platform").mkdir(exist_ok=True)
    backend = SqliteBackend(tmp_path / ".platform" / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_test", name="finalize_test")
    threads = await store.list_threads(session.session_id)
    run_store = WorkflowRunStore(tmp_path)
    impl = WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=run_store,
    )
    return impl, store, backend, session


class TestNodeCountReconciles:
    """Pins the ``running == 0`` invariant on ``_finalize_terminal``.

    spec-069 Part A picked Option B — zero the running count in
    ``_finalize_terminal`` (belt-and-braces) rather than only relying
    on ``_update_node_counts`` decrementing on ``NODE_FAILED`` (which
    the existing code already does). Option B guarantees the invariant
    holds even on construction-failure paths where ``NODE_STARTED``
    bumped ``running`` but no ``NODE_FAILED`` event ever lands.
    """

    @pytest.mark.asyncio
    async def test_finalize_zeros_running_count_on_terminal(
        self, tmp_path: Path
    ) -> None:
        impl, _store, backend, _session = await _mk_impl(tmp_path)
        try:
            # Seed a record whose `node_counts.running` is non-zero
            # (as if NODE_STARTED bumped it but no NODE_FAILED landed —
            # the wedge shape from spec-069's Background).
            run_id = WorkflowRunId(f"wrk_{uuid4()}")
            record = WorkflowRunRecord(
                run_id=run_id,
                ref="tests/workflows/x",
                phase="running",
                card=None,
                created_at=datetime.now(tz=UTC),
                node_counts=NodeCounts(total=2, running=1, completed=1),
            )
            impl._workflow_registry[run_id] = record

            # Build a fake spec + bridge task that closes immediately;
            # _finalize_terminal only needs a completed task to await.
            spec = WorkflowRunSpec(
                run_id=run_id,
                ref="tests/workflows/x",
                inputs={},
                card=None,
            )

            async def _noop() -> None:
                return None

            bridge_task = asyncio.create_task(_noop())
            await asyncio.sleep(0)  # let the noop schedule + complete

            await impl._finalize_terminal(spec, bridge_task)

            # AC2 — after finalize, `running` is 0 and the phase left
            # `"running"` (normalised to "error" per the existing
            # construction-failure branch).
            assert record.node_counts.running == 0
            assert record.phase == "error"
            # Non-running counters are untouched.
            assert record.node_counts.total == 2
            assert record.node_counts.completed == 1
        finally:
            await impl.aclose()
            await backend.close()
