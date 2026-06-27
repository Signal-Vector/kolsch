"""Workflow run persistence — Phase 3.1 Step 0.

The :class:`WorkflowRunStore` is the per-workspace API for the
``runs/<wrk_id>/`` directories. It mints fresh run IDs, writes the
meta sidecar, and exposes the JSONL append + read + tail surfaces
the executor (Step 1+) consumes.

Per the master spec §8.4 sidecar layout:

::

    runs/<wrk_id>/
    ├── meta.json         # serialised WorkflowRun snapshot (this module)
    ├── events.jsonl      # append-only event log (this module)
    ├── pid               # executor PID (Step 4)
    └── cancel.flag       # operator sentinel (Phase 3.0 Step 6 + Step 4)

This module owns ``meta.json`` + ``events.jsonl`` and the read /
write surfaces over them. ``pid`` + ``cancel.flag`` are owned by
the executor and CLI; this module only exposes their PATHS via
:class:`RunDirectory`.

NO executor logic here — pure data + filesystem plumbing.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from workflows.ids import TraceId, WorkflowRunId
from workflows.run import RunDirectory, WorkflowRun, WorkflowRunStatus

__all__ = ["WorkflowRunStore"]


# Polling interval for :meth:`WorkflowRunStore.tail`. 100ms is the
# spec-stated default — small enough to feel live in ``--follow``,
# large enough to keep CPU below noise.
_TAIL_POLL_SECONDS = 0.1


class WorkflowRunStore:
    """Per-workspace API for ``runs/<wrk_id>/`` directories.

    Construction is cheap (just records the workspace root); the
    ``runs/`` directory is created lazily on the first :meth:`create`.

    All methods operate against the on-disk filesystem — no in-memory
    caching. The executor reads + writes through this store; the
    orphan sweep (Step 4) walks :meth:`list` to identify
    ``status=running`` runs whose PIDs are dead.
    """

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._runs_root = workspace_root / "runs"

    @property
    def runs_root(self) -> Path:
        return self._runs_root

    # ------------------------------------------------------------------
    # create / read
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        workflow_ref: str,
        inputs: dict[str, Any] | None = None,
        trace_id: TraceId | None = None,
    ) -> WorkflowRun:
        """Mint a fresh run + write its meta sidecar.

        ``trace_id`` defaults to a freshly-minted ``tr_<uuid>`` for
        root entry points; callers nested inside an existing trace
        (sub-workflow, fork) pass the parent's trace id verbatim so
        the chain-spanning ID flows through (master spec §11.3).

        Returns the materialised :class:`WorkflowRun`. The JSONL is
        NOT touched here — :class:`workflows.executor.IterativeExecutor`
        emits ``workflow.run.started`` as the first event when it
        begins the run (Step 1).
        """
        run_id = WorkflowRunId(f"wrk_{uuid.uuid4()}")
        resolved_trace_id = trace_id if trace_id is not None else TraceId(
            f"tr_{uuid.uuid4()}"
        )
        run = WorkflowRun(
            run_id=run_id,
            trace_id=resolved_trace_id,
            workflow_ref=workflow_ref,
            status=WorkflowRunStatus.PENDING,
            inputs=dict(inputs) if inputs else {},
            created_at=datetime.now(UTC),
        )
        run_dir = self._directory(run_id)
        run_dir.root.mkdir(parents=True, exist_ok=True)
        self._write_meta(run_dir, run)
        return run

    def read(self, run_id: WorkflowRunId) -> WorkflowRun:
        """Reconstruct the :class:`WorkflowRun` for an existing run.

        Reads the ``meta.json`` sidecar for the unchanging fields,
        then walks the JSONL tail for the live status / reason. If
        the JSONL has no terminal event, status remains whatever the
        meta sidecar recorded (which Step 1+ will set to ``running``
        at start time).
        """
        run_dir = self._directory(run_id)
        if not run_dir.meta_path.exists():
            msg = f"workflow run not found: {run_id}"
            raise FileNotFoundError(msg)
        meta = json.loads(run_dir.meta_path.read_text(encoding="utf-8"))
        run = WorkflowRun.model_validate(meta)
        # Walk the JSONL tail for the most recent ``workflow.run.*``
        # event; if one of the terminal kinds is present, lift the
        # status from there.
        tail_status, tail_reason = _scan_jsonl_for_terminal_status(
            run_dir.events_path
        )
        if tail_status is not None:
            run = run.model_copy(
                update={
                    "status": tail_status,
                    "reason": tail_reason if tail_reason is not None else run.reason,
                }
            )
        return run

    def update_status(
        self, run_id: WorkflowRunId, status: WorkflowRunStatus
    ) -> None:
        """Phase 3.4 — atomically update the run's ``meta.json`` status.

        The pause handler (Step 0) writes ``waiting_for_input`` so
        :meth:`read` returns the paused status (the JSONL has no
        terminal event yet, so the tail scan falls through to the
        meta sidecar's status). The resume handler (Step 1) will
        flip it back to ``running``. The four terminal statuses
        flow through the JSONL terminal events and don't need this
        path — the tail scan in :meth:`read` overrides any meta
        status when a terminal event is present.

        Atomic via ``tmp + rename`` so an interrupted write never
        leaves a corrupt sidecar.
        """
        run_dir = self._directory(run_id)
        if not run_dir.meta_path.exists():
            msg = f"workflow run not found: {run_id}"
            raise FileNotFoundError(msg)
        meta = json.loads(run_dir.meta_path.read_text(encoding="utf-8"))
        meta["status"] = str(status)
        run = WorkflowRun.model_validate(meta)
        self._write_meta(run_dir, run)

    # ------------------------------------------------------------------
    # event append / read
    # ------------------------------------------------------------------

    def append_event(
        self, run_id: WorkflowRunId, event: dict[str, Any]
    ) -> None:
        """Append-only JSONL write with a trailing newline.

        The caller is responsible for the event's shape (kind +
        common fields per master spec §11.3); Step 5 wires the
        Pydantic envelopes in. At Step 0 the contract is just:
        valid JSON + one line per event + atomic-enough for the
        sequential-executor regime.
        """
        run_dir = self._directory(run_id)
        run_dir.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, separators=(",", ":"), default=str)
        with run_dir.events_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")

    def read_events(
        self, run_id: WorkflowRunId
    ) -> Iterator[dict[str, Any]]:
        """Yield one parsed dict per JSONL line.

        Returns an empty iterator when the run exists but has no
        events yet (a pending run). Raises :class:`FileNotFoundError`
        when the run directory itself is missing.
        """
        run_dir = self._directory(run_id)
        if not run_dir.root.exists():
            msg = f"workflow run not found: {run_id}"
            raise FileNotFoundError(msg)
        if not run_dir.events_path.exists():
            return
        with run_dir.events_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                yield json.loads(stripped)

    def tail(
        self,
        run_id: WorkflowRunId,
        *,
        poll_seconds: float = _TAIL_POLL_SECONDS,
    ) -> Iterator[dict[str, Any]]:
        """Tail the JSONL like ``tail -f``.

        Polls every ``poll_seconds`` (default 100ms) until the run
        reaches a terminal status (see
        :meth:`WorkflowRunStatus.terminal_set`). Yields events in
        append order; terminates after yielding the terminal
        ``workflow.run.*`` event.

        Used by the CLI ``--follow`` flag (Step 5).
        """
        run_dir = self._directory(run_id)
        offset = 0
        terminal_kinds = _terminal_event_kinds()
        while True:
            if run_dir.events_path.exists():
                with run_dir.events_path.open("r", encoding="utf-8") as fh:
                    fh.seek(offset)
                    for line in fh:
                        stripped = line.strip()
                        if not stripped:
                            continue
                        event = json.loads(stripped)
                        yield event
                        if event.get("kind") in terminal_kinds:
                            return
                    offset = fh.tell()
            time.sleep(poll_seconds)

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    def list(self) -> list[WorkflowRun]:
        """Enumerate runs under ``runs/``, newest first.

        Each row is reconstructed via :meth:`read` so the returned
        status reflects any terminal event that landed in the JSONL.
        Sort order is ``created_at`` descending; ties broken by id.
        """
        if not self._runs_root.exists():
            return []
        rows: list[WorkflowRun] = []
        for child in self._runs_root.iterdir():
            if not child.is_dir():
                continue
            meta = child / "meta.json"
            if not meta.exists():
                continue
            try:
                run = self.read(WorkflowRunId(child.name))
            except (FileNotFoundError, ValueError):
                continue
            rows.append(run)
        rows.sort(key=lambda r: (r.created_at, r.run_id), reverse=True)
        return rows

    # ------------------------------------------------------------------
    # directory accessor
    # ------------------------------------------------------------------

    def directory(self, run_id: WorkflowRunId) -> RunDirectory:
        """Return the :class:`RunDirectory` for a given run id.

        Public so the executor (Step 4) can read the PID + cancel
        flag paths through the same value object the store uses.
        """
        return self._directory(run_id)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _directory(self, run_id: WorkflowRunId) -> RunDirectory:
        return RunDirectory(root=self._runs_root / str(run_id))

    def _write_meta(
        self, run_dir: RunDirectory, run: WorkflowRun
    ) -> None:
        tmp = run_dir.meta_path.with_suffix(".json.tmp")
        tmp.write_text(
            run.model_dump_json(indent=2), encoding="utf-8"
        )
        tmp.replace(run_dir.meta_path)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


_TERMINAL_KIND_TO_STATUS: dict[str, WorkflowRunStatus] = {
    "workflow.run.completed": WorkflowRunStatus.SUCCEEDED,
    "workflow.run.failed": WorkflowRunStatus.FAILED,
    "workflow.run.timed_out": WorkflowRunStatus.TIMED_OUT,
    "workflow.run.aborted": WorkflowRunStatus.ABORTED,
}


def _terminal_event_kinds() -> frozenset[str]:
    return frozenset(_TERMINAL_KIND_TO_STATUS)


def _scan_jsonl_for_terminal_status(
    events_path: Path,
) -> tuple[WorkflowRunStatus | None, str | None]:
    """Read the JSONL bottom-up and return the first terminal status.

    Returns ``(None, None)`` when the file is missing OR contains no
    terminal ``workflow.run.*`` event yet.
    """
    if not events_path.exists():
        return (None, None)
    try:
        text = events_path.read_text(encoding="utf-8")
    except OSError:
        return (None, None)
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        kind = event.get("kind")
        if kind in _TERMINAL_KIND_TO_STATUS:
            reason = event.get("payload", {}).get("reason")
            return (_TERMINAL_KIND_TO_STATUS[kind], reason)
    return (None, None)
