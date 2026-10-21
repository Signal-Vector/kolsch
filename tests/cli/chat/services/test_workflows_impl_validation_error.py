"""bug-142 — chat `/workflow` (WorkflowsImpl) on an invalid workflow.

The executor's run-start gate raises the validator's error(s) before
any event. `WorkflowsImpl` must turn that into an `error` phase with a
legible `error_message` (no crash, the FIFO drain keeps serving) — and
for a multi-error `ExceptionGroup`, name the actual errors rather than
"Workflow validation failed (N sub-exceptions)".
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, cast

import pytest

from cli.chat.services.workflows_impl import WorkflowsImpl
from store import SessionStore, SqliteBackend
from tests.cli.chat.services.mocks import (
    MockExecutorSpec,
    install_mock_executor_factory,
)
from workflows.errors import WorkflowNodeNotDefined
from workflows.events import WorkflowRunCompletedEvent, WorkflowRunStartedEvent
from workflows.ids import TraceId, WorkflowRunId
from workflows.run import RunDirectory, WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    (tmp_path / ".platform").mkdir(exist_ok=True)
    backend = SqliteBackend(tmp_path / ".platform" / "entities.db")
    await backend.initialize()
    try:
        yield SessionStore(root=tmp_path, backend=backend)
    finally:
        await backend.close()


def _not_defined(ref: str) -> WorkflowNodeNotDefined:
    return WorkflowNodeNotDefined(
        file="demo/workflows/x", line=0, ref=ref, location=f"edges[0].to={ref}"
    )


class _RejectingExecutor:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run(self) -> WorkflowRunStatus:
        raise self._exc


def _factory(
    tmp_path: Path, exc: BaseException, valid: Callable[..., Any]
) -> Callable[..., Any]:
    """Reject `demo/workflows/x`; delegate any other ref to `valid`."""

    def _make(ref: str, *a: Any, **k: Any) -> tuple[Any, RunDirectory]:
        if ref != "demo/workflows/x":
            return cast("tuple[Any, RunDirectory]", valid(ref, *a, **k))
        run_dir = RunDirectory(root=tmp_path / "runs" / f"wrk_{uuid.uuid4()}")
        run_dir.root.mkdir(parents=True, exist_ok=True)
        return _RejectingExecutor(exc), run_dir

    return _make


async def _wait_phase(impl: WorkflowsImpl, run_id: str, phase: str) -> None:
    for _ in range(500):
        if impl.status(run_id).phase == phase:
            return
        await asyncio.sleep(0.01)
    assert impl.status(run_id).phase == phase


@pytest.mark.parametrize("several", [False, True], ids=["one-error", "error-group"])
async def test_workflows_impl_validation_error_becomes_error_phase(
    tmp_path: Path, store: SessionStore, *, several: bool
) -> None:
    exc: BaseException = (
        ExceptionGroup(
            "Workflow validation failed",
            [_not_defined("ghost"), _not_defined("phantom")],
        )
        if several
        else _not_defined("ghost")
    )
    session = await store.create_session("ag_test", name="w")
    (thread,) = await store.list_threads(session.session_id)
    impl = WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=thread.thread_id,
        run_store=WorkflowRunStore(tmp_path),
    )
    rid = WorkflowRunId(f"wrk_{'0' * 8}-0000-4000-8000-{'0' * 12}")
    trace = TraceId(f"tr_{'1' * 8}-0000-4000-8000-{'1' * 12}")
    install_mock_executor_factory(
        impl,
        {
            "demo/workflows/ok": MockExecutorSpec(
                events=[
                    WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=trace),
                    WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=trace),
                ]
            )
        },
        runs_root=tmp_path / "runs",
    )
    impl._executor_factory = _factory(tmp_path, exc, impl._executor_factory)
    try:
        run_id = await impl.start("demo/workflows/x", {})
        await _wait_phase(impl, run_id, "error")
        message = impl._workflow_registry[run_id].error_message or ""
        assert "'ghost'" in message
        if several:
            assert "'phantom'" in message
            assert "sub-exceptions" not in message
        # The drain survived: a second, valid start runs to success.
        ok_id = await impl.start("demo/workflows/ok", {})
        await _wait_phase(impl, ok_id, "success")
    finally:
        await impl.aclose()


async def test_workflows_impl_real_executor_rejected_run_is_error_phase(
    tmp_path: Path, store: SessionStore
) -> None:
    """The REAL executor's run-start gate (which now discards the
    rejected root run's directory) still yields an `error` phase in the
    chat registry — nothing downstream needs the discarded run dir."""
    from assets.catalog import AssetCatalog
    from workflows.builtins.noop import NoopRunner
    from workflows.executor import IterativeExecutor, RunnerRegistry
    from workflows.parser import Workflow

    run_store = WorkflowRunStore(tmp_path)
    workflows = {
        "demo/workflows/x": Workflow.model_validate(
            {
                "nodes": {"a": {"type": "kolsch/nodes/noop"}},
                "graph": {"edges": [{"from": "a", "to": "ghost"}]},
            }
        ),
        "demo/workflows/ok": Workflow.model_validate(
            {"nodes": {"a": {"type": "kolsch/nodes/noop"}}, "graph": {"edges": []}}
        ),
    }

    def _make(
        ref: str,
        _inputs: dict[str, Any],
        observer: Callable[..., None],
        *_a: Any,
        **_k: Any,
    ) -> tuple[IterativeExecutor, RunDirectory]:
        run = run_store.create(workflow_ref=ref, started_by="in-process")
        registry = RunnerRegistry()
        registry.register("kolsch/nodes/noop", NoopRunner())
        executor = IterativeExecutor(
            workflow=workflows[ref],
            run=run,
            store=run_store,
            registry=registry,
            catalog=AssetCatalog(),
            event_observer=observer,
        )
        return executor, run_store.directory(run.run_id)

    session = await store.create_session("ag_test", name="w")
    (thread,) = await store.list_threads(session.session_id)
    impl = WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=thread.thread_id,
        run_store=run_store,
    )
    impl._executor_factory = _make
    try:
        run_id = await impl.start("demo/workflows/x", {})
        await _wait_phase(impl, run_id, "error")
        assert "'ghost'" in (impl._workflow_registry[run_id].error_message or "")
        assert run_store.list() == []
        # The drain still serves the next start: a valid run succeeds.
        ok_id = await impl.start("demo/workflows/ok", {})
        await _wait_phase(impl, ok_id, "success")
        (only,) = run_store.list()
        assert only.workflow_ref == "demo/workflows/ok"
        assert only.status == WorkflowRunStatus.SUCCEEDED
    finally:
        await impl.aclose()
