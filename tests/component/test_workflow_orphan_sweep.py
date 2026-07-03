"""Phase 3.1 Step 4 — orphan sweep tests.

Covers AC 4.3, 4.4, 4.5:

- a ``started``-without-terminal JSONL whose recorded PID is dead
  reconciles to a synthetic ``workflow.run.failed`` with
  ``reason: "orphaned"``;
- a ``started``-without-terminal JSONL whose PID is alive (this
  process) is NOT reconciled;
- a terminal-event JSONL is never touched;
- a missing PID file counts as dead.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from workflows.orphan_sweep import sweep
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

    from workflows.ids import WorkflowRunId


# A canonical "guaranteed-dead" sentinel. PID 1 (init) is always alive
# on a POSIX system, so we need a PID we can be confident is empty.
# 32-bit MAX-1 is well beyond the typical kernel pid_max; if it ever
# happens to be allocated we re-roll via ``_pick_dead_pid``.
def _pick_dead_pid() -> int:
    """Return a PID whose ``os.kill(pid, 0)`` raises ``ProcessLookupError``.

    Probes a couple of high candidates; the first vacant slot wins.
    Test isolation across CI hosts.
    """
    for candidate in (9999999, 8888888, 7777777):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    msg = "no dead PID candidate available — host pid_max unusually high?"
    raise RuntimeError(msg)


def _seed_started_no_terminal(
    store: WorkflowRunStore,
    *,
    run_id: WorkflowRunId,
    pid: int | None,
) -> None:
    """Create a run directory + JSONL with just ``workflow.run.started``.

    Optionally writes a PID file. Passing ``None`` simulates the
    crash-before-PID-write case (the executor's atomic ``tmp + rename``
    never landed).
    """
    run_dir = store.directory(run_id)
    run_dir.root.mkdir(parents=True, exist_ok=True)
    store.append_event(
        run_id,
        {
            "kind": "workflow.run.started",
            "payload": {"workflow_ref": "demo/workflows/x", "inputs": {}},
        },
    )
    if pid is not None:
        run_dir.pid_path.write_text(str(pid), encoding="utf-8")


# ---------------------------------------------------------------------------
# AC 4.4 — dead PID reconciles.
# ---------------------------------------------------------------------------


def test_dead_pid_reconciles_to_failed(tmp_path: Path) -> None:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    _seed_started_no_terminal(store, run_id=run.run_id, pid=_pick_dead_pid())

    reconciled = sweep(store)

    assert reconciled == [run.run_id]
    events = list(store.read_events(run.run_id))
    kinds = [e["kind"] for e in events]
    assert kinds[-1] == "workflow.run.failed"
    assert events[-1]["payload"]["reason"] == "orphaned"
    # The store's status lookup must now report FAILED via the JSONL
    # tail scan.
    reread = store.read(run.run_id)
    assert reread.status == WorkflowRunStatus.FAILED


def test_missing_pid_file_reconciles_to_failed(tmp_path: Path) -> None:
    """A run whose JSONL has ``workflow.run.started`` but whose ``pid``
    file is missing is the crash-before-PID-write shape. Treat it as
    orphaned — there's no surviving process to attribute it to."""
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    _seed_started_no_terminal(store, run_id=run.run_id, pid=None)

    reconciled = sweep(store)

    assert reconciled == [run.run_id]
    events = list(store.read_events(run.run_id))
    assert events[-1]["kind"] == "workflow.run.failed"
    assert events[-1]["payload"]["reason"] == "orphaned"


# ---------------------------------------------------------------------------
# AC 4.5 — live PID survives.
# ---------------------------------------------------------------------------


def test_live_pid_not_reconciled(tmp_path: Path) -> None:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    _seed_started_no_terminal(store, run_id=run.run_id, pid=os.getpid())

    reconciled = sweep(store)

    assert reconciled == []
    events = list(store.read_events(run.run_id))
    kinds = [e["kind"] for e in events]
    # No synthetic failed event was appended.
    assert "workflow.run.failed" not in kinds


# ---------------------------------------------------------------------------
# Terminal JSONL is never touched.
# ---------------------------------------------------------------------------


def test_terminal_jsonl_skipped(tmp_path: Path) -> None:
    """A run with a terminal ``workflow.run.completed`` event is not
    a candidate for reconciliation, even if its PID is dead (the
    process exited cleanly after writing the terminal event)."""
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    store.append_event(
        run.run_id,
        {"kind": "workflow.run.started", "payload": {}},
    )
    store.append_event(
        run.run_id,
        {"kind": "workflow.run.completed", "payload": {"outputs": {}}},
    )
    store.directory(run.run_id).pid_path.write_text(
        str(_pick_dead_pid()), encoding="utf-8"
    )

    reconciled = sweep(store)

    assert reconciled == []
    events = list(store.read_events(run.run_id))
    # Exactly the two events we seeded — nothing appended.
    assert len(events) == 2


# ---------------------------------------------------------------------------
# No runs/ dir → no-op.
# ---------------------------------------------------------------------------


def test_no_runs_dir_returns_empty(tmp_path: Path) -> None:
    store = WorkflowRunStore(tmp_path)
    reconciled = sweep(store)
    assert reconciled == []


# ---------------------------------------------------------------------------
# Corrupt PID file — treated as dead so the sweep reconciles.
# ---------------------------------------------------------------------------


def test_corrupt_pid_file_reconciles(tmp_path: Path) -> None:
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    _seed_started_no_terminal(store, run_id=run.run_id, pid=None)
    # Write garbage into the pid file.
    store.directory(run.run_id).pid_path.write_text(
        "not-a-pid\n", encoding="utf-8"
    )

    reconciled = sweep(store)

    assert reconciled == [run.run_id]


# ---------------------------------------------------------------------------
# Mixed batch — some reconcile, some don't.
# ---------------------------------------------------------------------------


def test_mixed_batch_reconciles_only_orphans(tmp_path: Path) -> None:
    store = WorkflowRunStore(tmp_path)
    dead_pid = _pick_dead_pid()

    orphan = store.create(workflow_ref="demo/workflows/a", inputs={})
    _seed_started_no_terminal(store, run_id=orphan.run_id, pid=dead_pid)

    live = store.create(workflow_ref="demo/workflows/b", inputs={})
    _seed_started_no_terminal(store, run_id=live.run_id, pid=os.getpid())

    terminal = store.create(workflow_ref="demo/workflows/c", inputs={})
    store.append_event(
        terminal.run_id, {"kind": "workflow.run.started", "payload": {}}
    )
    store.append_event(
        terminal.run_id,
        {"kind": "workflow.run.aborted", "payload": {"reason": "user_cancel"}},
    )

    reconciled = sweep(store)

    assert reconciled == [orphan.run_id]


# ---------------------------------------------------------------------------
# Re-running the sweep on an already-reconciled run is a no-op.
# ---------------------------------------------------------------------------


def test_phase_3_7_child_orphan_uses_orphaned_child_reason(
    tmp_path: Path,
) -> None:
    """Phase 3.7 — a child run (parent pointer is set) whose PID is
    dead reconciles with ``reason="orphaned_child"`` so operators can
    distinguish parent-crash orphans from top-level orphans.
    """
    from schemas.entities import ParentRunPointer
    from workflows.ids import (
        InvocationId,
        WorkflowRunId,
    )

    store = WorkflowRunStore(tmp_path)
    parent_ptr = ParentRunPointer(
        workflow_run_id=WorkflowRunId(
            "wrk_aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        ),
        invocation_id=InvocationId(
            "inv_aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        ),
    )
    child_run = store.create(
        workflow_ref="demo/workflows/child",
        inputs={},
        parent=parent_ptr,
    )
    _seed_started_no_terminal(
        store, run_id=child_run.run_id, pid=_pick_dead_pid()
    )

    reconciled = sweep(store)
    assert reconciled == [child_run.run_id]

    events = list(store.read_events(child_run.run_id))
    assert events[-1]["kind"] == "workflow.run.failed"
    # The differentiating bit — child orphans get a distinct reason.
    assert events[-1]["payload"]["reason"] == "orphaned_child"


def test_top_level_orphan_keeps_orphaned_reason(tmp_path: Path) -> None:
    """Phase 3.7 — top-level runs (no parent pointer) keep the
    pre-existing ``orphaned`` reason; only child runs get the new
    ``orphaned_child`` variant."""
    store = WorkflowRunStore(tmp_path)
    root_run = store.create(workflow_ref="demo/workflows/root", inputs={})
    _seed_started_no_terminal(
        store, run_id=root_run.run_id, pid=_pick_dead_pid()
    )

    reconciled = sweep(store)
    assert reconciled == [root_run.run_id]

    events = list(store.read_events(root_run.run_id))
    assert events[-1]["payload"]["reason"] == "orphaned"


def test_repeat_sweep_is_idempotent(tmp_path: Path) -> None:
    """Once a synthetic ``workflow.run.failed`` lands, the JSONL has a
    terminal event — the next sweep must not append a second one."""
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    _seed_started_no_terminal(store, run_id=run.run_id, pid=_pick_dead_pid())

    first = sweep(store)
    assert first == [run.run_id]

    second = sweep(store)
    assert second == []

    # JSONL contains exactly one terminal event.
    text = store.directory(run.run_id).events_path.read_text(encoding="utf-8")
    failed_lines = [
        line
        for line in text.splitlines()
        if line.strip()
        and json.loads(line).get("kind") == "workflow.run.failed"
    ]
    assert len(failed_lines) == 1
