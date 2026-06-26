"""Engine-startup orphan sweep — Phase 3.1 Step 4.

A workflow run that emitted ``workflow.run.started`` but never wrote a
terminal ``workflow.run.*`` event is, by definition, either still
running OR was killed mid-flight (process crash, ``kill -9``, host
reboot). The sweep reconciles the second case so the run index always
reflects truth:

- enumerate ``runs/<wrk_id>/`` directories;
- if the JSONL contains ``workflow.run.started`` but no terminal
  ``workflow.run.*`` event AND the recorded PID is missing or dead,
  append a synthetic ``workflow.run.failed`` event with
  ``{reason: "orphaned"}``.

The check is :func:`os.kill` with signal ``0`` — POSIX-portable
liveness probe that doesn't actually send a signal. A
:class:`ProcessLookupError` means the PID slot is empty; a
:class:`PermissionError` means the PID exists but belongs to another
user (still alive — don't reconcile).

The sweep is wired into ``cli.main.main`` so every ``workflows`` /
``nodes`` invocation reconciles before dispatching.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from workflows.ids import WorkflowRunId

if TYPE_CHECKING:
    from workflows.run_store import WorkflowRunStore

__all__ = ["sweep"]


_TERMINAL_KINDS = frozenset(
    {
        "workflow.run.completed",
        "workflow.run.failed",
        "workflow.run.timed_out",
        "workflow.run.aborted",
    }
)
_STARTED_KIND = "workflow.run.started"


def sweep(store: WorkflowRunStore) -> list[WorkflowRunId]:
    """Reconcile orphaned runs in ``store.runs_root``.

    Returns the list of run ids whose JSONLs were appended to. An
    empty list means the run index was already coherent — every
    ``started``-without-terminal run has a live PID.
    """
    runs_root = store.runs_root
    if not runs_root.exists():
        return []

    reconciled: list[WorkflowRunId] = []
    for child in runs_root.iterdir():
        if not child.is_dir():
            continue
        run_id = WorkflowRunId(child.name)
        run_dir = store.directory(run_id)
        if not run_dir.events_path.exists():
            # No JSONL means the run never started writing — meta.json
            # may exist (created by ``store.create``) but the executor
            # hasn't reached its first ``append_event``. Skip; the next
            # sweep iteration will catch it once the JSONL is born.
            continue
        if not _is_started_without_terminal(run_dir.events_path):
            continue
        if _pid_alive(run_dir.pid_path):
            continue
        # Orphan. Append the synthetic terminal event so the JSONL
        # tail scan + run index reconcile to ``failed`` on the next
        # ``store.read`` / ``store.list``.
        store.append_event(
            run_id,
            {
                "kind": "workflow.run.failed",
                "payload": {"reason": "orphaned"},
            },
        )
        reconciled.append(run_id)
    return reconciled


def _is_started_without_terminal(events_path: Path) -> bool:
    """Return True iff the JSONL has ``workflow.run.started`` but no
    terminal ``workflow.run.*`` kind. Tolerates corrupt lines (skips
    them) — a partial last-line write must not derail the sweep.
    """
    has_started = False
    try:
        with events_path.open(encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                kind = event.get("kind")
                if kind == _STARTED_KIND:
                    has_started = True
                elif kind in _TERMINAL_KINDS:
                    return False
    except OSError:
        return False
    return has_started


def _pid_alive(pid_path: Path) -> bool:
    """Liveness probe via ``os.kill(pid, 0)``.

    A missing PID file means the executor never reached the atomic
    PID write — treat as dead, since a crashed run that never wrote
    its PID is the canonical orphan shape.
    """
    try:
        with pid_path.open(encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return False
    try:
        pid = int(raw)
    except ValueError:
        # Corrupt PID file → can't probe, treat as dead so the sweep
        # reconciles. The alternative (treat as alive) would leave the
        # JSONL hanging forever.
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Another user owns the PID slot — process exists, so we
        # don't reconcile. Cross-user run hijacking is out of scope
        # for the single-user workspace model.
        return True
    return True
