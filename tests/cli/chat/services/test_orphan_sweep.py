"""feat-015 Test 2 — `WorkflowsImpl.sweep_orphans` reconciliation.

Five passes:

1. Fixture: 3 runs on disk — one subprocess non-terminal, one
   in-process running (non-terminal), one in-process succeeded
   (terminal).
2. Call `sweep_orphans()`. Assert returns 1 — only the in-process
   running run is reconciled.
3. The in-process running run's JSONL now has an appended
   `workflow.run.aborted` event with `reason="chat-quit"`.
4. Meta status updated to `ABORTED`.
5. Subprocess run + in-process success run are UNTOUCHED (no new
   JSONL events, meta status preserved).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

import pytest

from cli.chat.services.workflows_impl import WorkflowsImpl
from store import SessionStore, SqliteBackend
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore


@pytest.fixture
async def session_store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    try:
        yield SessionStore(root=tmp_path, backend=backend)
    finally:
        await backend.close()


async def _mk_impl(
    tmp_path: Path,
    session_store: SessionStore,
    run_store: WorkflowRunStore,
) -> WorkflowsImpl:
    session = await session_store.create_session("ag_test", name="w")
    threads = await session_store.list_threads(session.session_id)
    return WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=session_store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=run_store,
    )


async def test_sweep_reconciles_only_in_process_non_terminal(
    tmp_path: Path, session_store: SessionStore,
) -> None:
    """AC #4 / AC #7 / AC #8: sweep only touches in-process orphans."""
    run_store = WorkflowRunStore(tmp_path)

    # Run 1: subprocess-hosted, non-terminal (PENDING).
    subproc_run = run_store.create(
        workflow_ref="demo/workflows/sub",
        inputs={},
        started_by="subprocess",
    )
    # Run 2: in-process, non-terminal — the ORPHAN.
    inproc_orphan = run_store.create(
        workflow_ref="demo/workflows/orphan",
        inputs={},
        started_by="in-process",
    )
    # Run 3: in-process, terminal (already succeeded).
    inproc_success = run_store.create(
        workflow_ref="demo/workflows/done",
        inputs={},
        started_by="in-process",
    )
    run_store.update_status(inproc_success.run_id, WorkflowRunStatus.SUCCEEDED)

    impl = await _mk_impl(tmp_path, session_store, run_store)
    try:
        # AC #4: sweep returns 1 (only the in-process orphan).
        reconciled = await impl.sweep_orphans()
        assert reconciled == 1

        # AC #4: orphan JSONL has an appended `workflow.run.aborted`.
        orphan_dir = run_store.directory(inproc_orphan.run_id)
        assert orphan_dir.events_path.exists()
        lines = [
            line for line in
            orphan_dir.events_path.read_text().splitlines()
            if line.strip()
        ]
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["kind"] == "workflow.run.aborted"
        assert event["payload"]["reason"] == "chat-quit"
        assert "terminal_at" in event["payload"]

        # AC #4: meta status flipped to ABORTED.
        orphan_meta = json.loads(orphan_dir.meta_path.read_text())
        assert orphan_meta["status"] == WorkflowRunStatus.ABORTED.value

        # AC #7: subprocess run untouched.
        sub_dir = run_store.directory(subproc_run.run_id)
        assert not sub_dir.events_path.exists()
        sub_meta = json.loads(sub_dir.meta_path.read_text())
        assert sub_meta["status"] == WorkflowRunStatus.PENDING.value

        # AC #8: in-process success run untouched.
        done_dir = run_store.directory(inproc_success.run_id)
        assert not done_dir.events_path.exists()
        done_meta = json.loads(done_dir.meta_path.read_text())
        assert done_meta["status"] == WorkflowRunStatus.SUCCEEDED.value
    finally:
        await impl.aclose()


async def test_sweep_reconciles_running_and_waiting_for_input(
    tmp_path: Path, session_store: SessionStore,
) -> None:
    """AC #4: RUNNING and WAITING_FOR_INPUT statuses both reconcile."""
    run_store = WorkflowRunStore(tmp_path)

    running_orphan = run_store.create(
        workflow_ref="demo/workflows/running",
        inputs={},
        started_by="in-process",
    )
    run_store.update_status(running_orphan.run_id, WorkflowRunStatus.RUNNING)

    waiting_orphan = run_store.create(
        workflow_ref="demo/workflows/waiting",
        inputs={},
        started_by="in-process",
    )
    run_store.update_status(
        waiting_orphan.run_id, WorkflowRunStatus.WAITING_FOR_INPUT,
    )

    impl = await _mk_impl(tmp_path, session_store, run_store)
    try:
        reconciled = await impl.sweep_orphans()
        assert reconciled == 2

        for run_id in (running_orphan.run_id, waiting_orphan.run_id):
            meta = json.loads(
                run_store.directory(run_id).meta_path.read_text(),
            )
            assert meta["status"] == WorkflowRunStatus.ABORTED.value
    finally:
        await impl.aclose()


async def test_sweep_zero_when_no_orphans(
    tmp_path: Path, session_store: SessionStore,
) -> None:
    """AC #4 baseline: no orphans → returns 0, no JSONL writes."""
    run_store = WorkflowRunStore(tmp_path)

    # One subprocess run (excluded by started_by) + one terminal
    # in-process run (excluded by status).
    run_store.create(
        workflow_ref="demo/workflows/sub",
        inputs={},
        started_by="subprocess",
    )
    done = run_store.create(
        workflow_ref="demo/workflows/done",
        inputs={},
        started_by="in-process",
    )
    run_store.update_status(done.run_id, WorkflowRunStatus.SUCCEEDED)

    impl = await _mk_impl(tmp_path, session_store, run_store)
    try:
        assert await impl.sweep_orphans() == 0
        # Neither run should have gained a JSONL.
        for run_id in (done.run_id,):
            assert not run_store.directory(run_id).events_path.exists()
    finally:
        await impl.aclose()


async def test_sweep_terminal_statuses_untouched(
    tmp_path: Path, session_store: SessionStore,
) -> None:
    """AC #8: every terminal status (SUCCEEDED/FAILED/TIMED_OUT/ABORTED) skipped."""
    run_store = WorkflowRunStore(tmp_path)

    terminals = [
        WorkflowRunStatus.SUCCEEDED,
        WorkflowRunStatus.FAILED,
        WorkflowRunStatus.TIMED_OUT,
        WorkflowRunStatus.ABORTED,
    ]
    created = []
    for status in terminals:
        run = run_store.create(
            workflow_ref=f"demo/workflows/{status.value}",
            inputs={},
            started_by="in-process",
        )
        run_store.update_status(run.run_id, status)
        created.append((run, status))

    impl = await _mk_impl(tmp_path, session_store, run_store)
    try:
        assert await impl.sweep_orphans() == 0
        for run, original_status in created:
            meta = json.loads(
                run_store.directory(run.run_id).meta_path.read_text(),
            )
            assert meta["status"] == original_status.value
            assert not run_store.directory(run.run_id).events_path.exists()
    finally:
        await impl.aclose()


async def test_sweep_appended_event_replayable(
    tmp_path: Path, session_store: SessionStore,
) -> None:
    """AC #4: appended aborted event round-trips through `read_events`.

    Ensures the synthetic event survives JSONL round-trip (no bad
    serialisation) — the renderer picks it up via replay just like a
    live abort.
    """
    run_store = WorkflowRunStore(tmp_path)
    orphan = run_store.create(
        workflow_ref="demo/workflows/orphan",
        inputs={},
        started_by="in-process",
    )
    run_store.update_status(orphan.run_id, WorkflowRunStatus.RUNNING)

    impl = await _mk_impl(tmp_path, session_store, run_store)
    try:
        assert await impl.sweep_orphans() == 1

        events = list(run_store.read_events(orphan.run_id))
        assert len(events) == 1
        assert events[0]["kind"] == "workflow.run.aborted"
        assert events[0]["payload"]["reason"] == "chat-quit"

        # `WorkflowRunStore.read` reconciles the run to ABORTED via
        # the JSONL tail scan.
        reconstructed = run_store.read(orphan.run_id)
        assert reconstructed.status == WorkflowRunStatus.ABORTED
    finally:
        await impl.aclose()
