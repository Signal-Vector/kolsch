"""Workflow run persistence — the on-disk store for `runs/<wrk_id>/`.

[`WorkflowRunStore`][workflows.run_store.WorkflowRunStore] is the
per-workspace API for the `runs/<wrk_id>/` directories. It mints fresh
run IDs, writes the meta sidecar, and exposes the JSONL append /
read / tail surfaces the executor consumes.

Sidecar layout:

```text
runs/<wrk_id>/
├── meta.json         # serialised WorkflowRun snapshot (this module)
├── events.jsonl      # append-only event log (this module)
├── pid               # executor PID (executor-owned)
└── cancel.flag       # operator sentinel (CLI-owned)
```

This module owns `meta.json` and `events.jsonl` and the read / write
surfaces over them. `pid` and `cancel.flag` are owned by the executor
and CLI; this module only exposes their PATHS via
[`RunDirectory`][workflows.run.RunDirectory].

NO executor logic here — pure data and filesystem plumbing.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from schemas.entities import ForkedFrom, ParentRunPointer
from workflows.ids import TraceId, WorkflowRunId
from workflows.run import RunDirectory, WorkflowRun, WorkflowRunStatus

__all__ = ["WorkflowRunStore"]


# Polling interval for `WorkflowRunStore.tail`. 100ms is the default
# — small enough to feel live in `--follow`, large enough to keep CPU
# below noise.
_TAIL_POLL_SECONDS = 0.1


class WorkflowRunStore:
    """Per-workspace API for `runs/<wrk_id>/` directories.

    Construction is cheap (just records the workspace root); the
    `runs/` directory is created lazily on the first `create`.

    All methods operate against the on-disk filesystem — no in-memory
    caching. The executor reads and writes through this store; the
    orphan sweep walks `list` to identify `status=running` runs whose
    PIDs are dead.
    """

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._runs_root = workspace_root / "runs"

    @property
    def runs_root(self) -> Path:
        """The `runs/` directory under the workspace root."""
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
        parent: ParentRunPointer | None = None,
        forked_from: ForkedFrom | None = None,
        started_by: Literal["subprocess", "in-process"] = "subprocess",
    ) -> WorkflowRun:
        """Mint a fresh run and write its meta sidecar.

        `trace_id` defaults to a freshly-minted `tr_<uuid>` for root
        entry points; callers nested inside an existing trace
        (sub-workflow, fork) pass the parent's trace id verbatim so
        the chain-spanning ID flows through.

        `parent` is the parent-run pointer when this run is being
        spawned by a `kolsch/nodes/workflow` node in a parent run.
        Root entry points pass `None`. Persisted in `meta.json` as a
        nested dict and round-tripped on `read`.

        `forked_from` is the parent-run pointer when this run is
        being created by `kolsch workflows fork`. The two
        parent-pointer kwargs are INDEPENDENT — a forked sub-workflow
        run can carry both. Persisted in `meta.json` as a nested dict
        alongside `parent`.

        The JSONL is NOT touched here — the executor emits
        `workflow.run.started` as the first event when it begins the
        run.

        Args:
            workflow_ref: The 3-seg ref of the workflow being run.
            inputs: Optional input dict — defaults to empty.
            trace_id: Optional caller-supplied trace id; defaults to
                a freshly-minted one.
            parent: Optional sub-workflow parent pointer.
            forked_from: Optional fork parent pointer.
            started_by: feat-015 — `"in-process"` for runs started by
                the chat's `WorkflowsImpl`; `"subprocess"` (default)
                for runs launched via `kolsch workflows run` and
                similar CLI entry points. The chat's boot-time orphan
                sweep only reconciles `"in-process"` runs.

        Returns:
            The materialised
            [`WorkflowRun`][workflows.run.WorkflowRun].
        """
        run_id = WorkflowRunId(f"wrk_{uuid.uuid4()}")
        resolved_trace_id = (
            trace_id if trace_id is not None else TraceId(f"tr_{uuid.uuid4()}")
        )
        run = WorkflowRun(
            run_id=run_id,
            trace_id=resolved_trace_id,
            workflow_ref=workflow_ref,
            status=WorkflowRunStatus.PENDING,
            inputs=dict(inputs) if inputs else {},
            created_at=datetime.now(UTC),
            parent=parent,
            forked_from=forked_from,
            run_started_by=started_by,
        )
        run_dir = self._directory(run_id)
        run_dir.root.mkdir(parents=True, exist_ok=True)
        self._write_meta(run_dir, run)
        return run

    def read(self, run_id: WorkflowRunId) -> WorkflowRun:
        """Reconstruct the run record for an existing run.

        Reads the `meta.json` sidecar for the unchanging fields, then
        walks the JSONL tail for the live status / reason. If the
        JSONL has no terminal event, status remains whatever the
        meta sidecar recorded (the executor sets it to `running` at
        start time).

        Args:
            run_id: The run to read.

        Returns:
            The reconstructed run.

        Raises:
            FileNotFoundError: No `meta.json` exists for `run_id`.
        """
        run_dir = self._directory(run_id)
        if not run_dir.meta_path.exists():
            msg = f"workflow run not found: {run_id}"
            raise FileNotFoundError(msg)
        meta = json.loads(run_dir.meta_path.read_text(encoding="utf-8"))
        run = WorkflowRun.model_validate(meta)
        # Walk the JSONL tail for the most recent `workflow.run.*`
        # event; if one of the terminal kinds is present, lift the
        # status from there.
        tail_status, tail_reason = _scan_jsonl_for_terminal_status(run_dir.events_path)
        if tail_status is not None:
            run = run.model_copy(
                update={
                    "status": tail_status,
                    "reason": tail_reason if tail_reason is not None else run.reason,
                }
            )
        return run

    def update_status(self, run_id: WorkflowRunId, status: WorkflowRunStatus) -> None:
        """Atomically update the run's `meta.json` status.

        The pause handler writes `waiting_for_input` so `read`
        returns the paused status (the JSONL has no terminal event
        yet, so the tail scan falls through to the meta sidecar's
        status). The resume handler flips it back to `running`. The
        four terminal statuses flow through the JSONL terminal events
        and don't need this path — the tail scan in `read` overrides
        any meta status when a terminal event is present.

        Atomic via `tmp + rename` so an interrupted write never leaves
        a corrupt sidecar.

        Args:
            run_id: The run to update.
            status: The new status to record.

        Raises:
            FileNotFoundError: No `meta.json` exists for `run_id`.
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

    def append_event(self, run_id: WorkflowRunId, event: dict[str, Any]) -> None:
        """Append a single event to the run's JSONL with a trailing newline.

        The caller is responsible for the event's shape (kind + common
        fields). The contract is: valid JSON, one line per event,
        atomic-enough for the sequential-executor regime.

        Args:
            run_id: The run whose JSONL to append to.
            event: The event payload.
        """
        run_dir = self._directory(run_id)
        run_dir.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, separators=(",", ":"), default=str)
        with run_dir.events_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")

    def read_events(self, run_id: WorkflowRunId) -> Iterator[dict[str, Any]]:
        """Yield one parsed dict per JSONL line.

        Args:
            run_id: The run to read.

        Yields:
            One parsed event dict per JSONL line.

        Raises:
            FileNotFoundError: The run directory itself is missing.
                When the run exists but has no events yet (a pending
                run), the iterator is simply empty.
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
        stop: Callable[[], bool] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Tail the JSONL like `tail -f`.

        Polls every `poll_seconds` (default 100ms) until the run
        reaches a terminal status (see
        [`WorkflowRunStatus.terminal_set`][workflows.run.WorkflowRunStatus.terminal_set]).
        Yields events in append order; terminates after yielding the
        terminal `workflow.run.*` event. Used by the CLI's `--follow`
        flag.

        `stop` (bug-142) is checked after each drain of the file: once
        it returns `True` the tail yields whatever is left and returns,
        even without a terminal event. The `--follow` caller passes
        "the executor has finished" so a run that ends WITHOUT writing
        a terminal event (e.g. rejected by the validator before its
        first event) can't leave the tail polling forever.

        Args:
            run_id: The run to tail.
            poll_seconds: Poll interval between file checks.
            stop: Optional predicate; `True` ends the tail after a
                final drain.

        Yields:
            One event dict per appended JSONL line, in append order.
        """
        run_dir = self._directory(run_id)
        offset = 0
        terminal_kinds = _terminal_event_kinds()
        while True:
            # Sample `stop` BEFORE draining so every line written before
            # the producer finished is still yielded below.
            stopping = stop is not None and stop()
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
            if stopping:
                return
            time.sleep(poll_seconds)

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    def list(self) -> list[WorkflowRun]:
        """Enumerate runs under `runs/`, newest first.

        Each row is reconstructed via `read` so the returned status
        reflects any terminal event that landed in the JSONL. Sort
        order is `created_at` descending; ties broken by id.

        Returns:
            All runs in the store, newest first.
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

    def list_all_meta(self) -> Iterator[WorkflowRun]:
        """Yield one `WorkflowRun` per meta sidecar under `runs/`.

        Cheap enumeration — reads `meta.json` only (no JSONL walk).
        Used by feat-015's in-process orphan sweep to identify runs
        whose `run_started_by="in-process"` and whose meta status is
        still non-terminal after a chat quit. Skips directories that
        lack a `meta.json` or whose meta fails to parse; skipping is
        deliberate — a partial run without a meta is not a candidate
        for orphan reconciliation.

        Yields:
            One `WorkflowRun` per readable meta sidecar. Order is
            filesystem-iteration order (unsorted).
        """
        if not self._runs_root.exists():
            return
        for child in self._runs_root.iterdir():
            if not child.is_dir():
                continue
            meta_path = child / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                yield WorkflowRun.model_validate(meta)
            except (OSError, json.JSONDecodeError, ValueError):
                # Corrupt or unreadable meta — skip. The sweep must
                # not derail on one bad run.
                continue

    def discard_unstarted(self, run_id: WorkflowRunId) -> None:
        """Remove a minted run that never started (bug-142).

        Used when a fork is rejected before its first event (e.g. the
        reloaded workflow fails validation): the forked run's
        directory holds only `meta.json`, so leaving it would surface a
        phantom `pending` run in `kolsch workflows list`. `meta.json` is
        unlinked first — a single atomic step after which no listing
        (`list` / `list_all_meta`) treats the directory as a run — and
        the directory is then removed.

        Raises:
            ValueError: The run has events — it started, and discarding
                it would destroy history.
        """
        run_dir = self._directory(run_id)
        if run_dir.events_path.exists() and run_dir.events_path.stat().st_size > 0:
            msg = f"refusing to discard run {run_id}: it has events"
            raise ValueError(msg)
        (run_dir.root / "meta.json").unlink(missing_ok=True)
        shutil.rmtree(run_dir.root, ignore_errors=True)

    # ------------------------------------------------------------------
    # directory accessor
    # ------------------------------------------------------------------

    def directory(self, run_id: WorkflowRunId) -> RunDirectory:
        """Return the [`RunDirectory`][workflows.run.RunDirectory] for a given run id.

        Public so the executor can read the PID and cancel-flag paths
        through the same value object the store uses.
        """
        return self._directory(run_id)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _directory(self, run_id: WorkflowRunId) -> RunDirectory:
        return RunDirectory(root=self._runs_root / str(run_id))

    def _write_meta(self, run_dir: RunDirectory, run: WorkflowRun) -> None:
        tmp = run_dir.meta_path.with_suffix(".json.tmp")
        tmp.write_text(run.model_dump_json(indent=2), encoding="utf-8")
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

    Returns `(None, None)` when the file is missing OR contains no
    terminal `workflow.run.*` event yet.
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
