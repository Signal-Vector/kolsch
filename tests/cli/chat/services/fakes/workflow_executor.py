"""Fake executor factory for spec-012 tests.

Backs the `WorkflowsImpl` `executor_factory` seam. Given a mapping of
`ref → list[WorkflowEvent]`, produces a stub executor whose `run()`
emits each event through the observer (with an optional per-event
delay) and returns the terminal `WorkflowRunStatus`.

The RunDirectory returned is minted under the store's `runs/` root
so `record.run_dir.cancel_flag_path` is a real writable path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from workflows.events import WorkflowEvent, WorkflowEventKind
from workflows.run import RunDirectory, WorkflowRunStatus


@dataclass
class FakeExecutor:
    """Stub executor — emits pre-scripted `WorkflowEvent`s then returns.

    Honours the injected `CancelToken`: when cancel fires mid-run, the
    stub emits a synthesised `RUN_ABORTED` event and returns `ABORTED`.
    """

    events: list[WorkflowEvent]
    observer: Callable[[WorkflowEvent], None]
    cancel_token: Any
    per_event_delay: float = 0.0

    async def run(self) -> WorkflowRunStatus:
        for event in self.events:
            if self.cancel_token is not None and self.cancel_token.is_cancelled():
                # Synthesise an aborted terminal and stop.
                if not any(
                    e.kind == WorkflowEventKind.RUN_ABORTED
                    for e in self.events
                ):
                    from workflows.events import WorkflowRunAbortedEvent

                    aborted = WorkflowRunAbortedEvent(
                        workflow_run_id=event.workflow_run_id,
                        trace_id=event.trace_id,
                        payload={"reason": "user_cancel"},
                    )
                    self.observer(aborted)
                return WorkflowRunStatus.ABORTED
            self.observer(event)
            if self.per_event_delay > 0:
                await asyncio.sleep(self.per_event_delay)

        # Derive terminal status from the last event's kind.
        if self.events:
            last = self.events[-1]
            if last.kind == WorkflowEventKind.RUN_COMPLETED:
                return WorkflowRunStatus.SUCCEEDED
            if last.kind == WorkflowEventKind.RUN_FAILED:
                return WorkflowRunStatus.FAILED
            if last.kind == WorkflowEventKind.RUN_TIMED_OUT:
                return WorkflowRunStatus.TIMED_OUT
            if last.kind == WorkflowEventKind.RUN_ABORTED:
                return WorkflowRunStatus.ABORTED
        return WorkflowRunStatus.SUCCEEDED


@dataclass
class FakeExecutorSpec:
    """Per-ref executor programming.

    `events` is the ordered list of `WorkflowEvent`s the fake emits;
    `per_event_delay` (seconds) sleeps between emits so tests can
    exercise the queued-vs-running FIFO transition. `on_construct` is
    an optional callback invoked with the mint `run_id` when the fake
    executor is built.
    """

    events: list[WorkflowEvent] = field(default_factory=list)
    per_event_delay: float = 0.0
    on_construct: Callable[[str], None] | None = None


def install_fake_executor_factory(
    impl: Any,
    programming: dict[str, FakeExecutorSpec],
    *,
    runs_root: Any,
) -> None:
    """Rebind `impl._executor_factory` to a scripted fake.

    Args:
        impl: The `WorkflowsImpl` under test.
        programming: `ref → FakeExecutorSpec`. A `ref` not present in
            the mapping raises `KeyError` at factory time — tests must
            be explicit about what refs their queue will drive.
        runs_root: The `runs/` root directory the fake mints
            `RunDirectory` roots under. Typically the tmp workspace's
            `runs/` path.
    """
    import uuid as _uuid

    def _factory(
        ref: str,
        inputs: dict[str, Any],
        observer: Callable[[WorkflowEvent], None],
        cancel_token: Any,
    ) -> tuple[FakeExecutor, RunDirectory]:
        _ = inputs
        spec = programming[ref]
        run_dir = RunDirectory(root=runs_root / f"wrk_{_uuid.uuid4()}")
        run_dir.root.mkdir(parents=True, exist_ok=True)
        if spec.on_construct is not None:
            spec.on_construct(str(run_dir.root.name))
        return (
            FakeExecutor(
                events=list(spec.events),
                observer=observer,
                cancel_token=cancel_token,
                per_event_delay=spec.per_event_delay,
            ),
            run_dir,
        )

    impl._executor_factory = _factory
