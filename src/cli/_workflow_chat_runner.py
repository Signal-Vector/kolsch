"""Chat-side workflow execution helpers.

Shared by the `/workflow` slash handler and the `user_input`
chat-resume path. Both surfaces need:

1. A wired
   [`IterativeExecutor`][workflows.executor.IterativeExecutor] rooted
   at the chat's runtime.
2. A
   [`WorkflowCapsuleRenderer`][cli._workflow_capsules.WorkflowCapsuleRenderer]
   writing to the chat's stdout.
3. A concurrent run loop: executor task + tail-rendering renderer +
   a `done_event` so a `WAITING_FOR_INPUT` pause (no terminal
   `workflow.run.*` event) doesn't hang the tail.
4. Post-pause rendering of the inline prompt capsule.

The caller owns:

- Catalog lookup + `WorkflowRun.create` (`/workflow` handler).
- Reading the existing run + setting/clearing
  `ChatSessionState.expecting_user_input_response` (chat resume path).

This module is intentionally small — the chat-specific orchestration
lives here; the chat input loop and slash dispatcher stay focused on
their own concerns.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from rich.console import Console

from cli._workflow_capsules import WorkflowCapsuleRenderer
from cli._workflow_runtime import build_workflow_executor
from workflows.errors import WorkflowError
from workflows.parser import load_workflow

if TYPE_CHECKING:
    from pathlib import Path
    from typing import TextIO

    from cli._slash import ChatSessionState
    from platform_runtime import PlatformRuntime
    from workflows.parser import Workflow
    from workflows.run import WorkflowRunStatus
    from workflows.run_store import WorkflowRunStore


__all__ = [
    "execute_workflow_with_renderer",
    "resolve_paused_node_name",
]


async def execute_workflow_with_renderer(
    *,
    state: ChatSessionState,
    stdout: TextIO,
    store: WorkflowRunStore,
    workflow: Workflow,
    run: Any,
    resume_response: dict[str, Any] | None = None,
    fork_event_id: str | None = None,
    input_overrides: dict[str, Any] | None = None,
) -> WorkflowRunStatus:
    """Run, resume, or fork a workflow with the chat capsule renderer.

    Mode is determined by which kwargs are set (caller picks exactly
    one):

    - Neither `resume_response` nor `fork_event_id` → fresh start:
      `executor.run()`.
    - `resume_response` set → `executor.resume(response=...)`.
    - `fork_event_id` set → `executor.start_from_fork(
      fork_event_id=..., input_overrides=...)`; the caller is
      responsible for minting the forked `WorkflowRun` with a
      `forked_from` pointer before invoking this helper.

    Returns the executor's terminal status (or `WAITING_FOR_INPUT` if
    a downstream `user_input` pauses). The caller updates
    `ChatSessionState.expecting_user_input_response` based on the
    returned status — this helper only renders the pause capsule; it
    does not arm the flag.
    """
    runtime: PlatformRuntime = state.runtime
    executor = build_workflow_executor(
        root=runtime.root,
        runtime=runtime,
        workflow=workflow,
        run=run,
        store=store,
    )
    console = Console(file=stdout, force_terminal=False, highlight=False)
    catalog = runtime.asset_management.catalog()

    def _load_child_workflow(child_run_id: str) -> Workflow | None:
        from workflows.ids import WorkflowRunId  # noqa: PLC0415

        try:
            child_run = store.read(WorkflowRunId(child_run_id))
        except (FileNotFoundError, ValueError):
            return None
        child_entry = catalog.lookup(child_run.workflow_ref, expected_type="workflow")
        if child_entry is None:
            return None
        from pathlib import Path  # noqa: PLC0415

        try:
            return load_workflow(Path(child_entry.path))
        except WorkflowError:
            return None

    renderer = WorkflowCapsuleRenderer(
        console=console,
        workflow=workflow,
        store=store,
        child_workflow_loader=_load_child_workflow,
    )

    done_event = asyncio.Event()
    if fork_event_id is not None:
        executor_coro = executor.start_from_fork(
            fork_event_id=fork_event_id,
            input_overrides=input_overrides,
        )
    elif resume_response is not None:
        executor_coro = executor.resume(response=resume_response)
    else:
        executor_coro = executor.run()
    executor_task = asyncio.create_task(executor_coro)

    async def _await_executor() -> WorkflowRunStatus:
        try:
            return await executor_task
        finally:
            done_event.set()

    exec_wrapper = asyncio.create_task(_await_executor())
    try:
        await renderer.render_run(run.run_id, done_event=done_event)
    finally:
        status = await exec_wrapper

    # Render the inline pause capsule when the run is paused for
    # input. The capsule's node lookup walks the JSONL + ``waiting_at``
    # sidecar; it is a no-op if the lookup fails.
    from workflows.run import WorkflowRunStatus  # noqa: PLC0415

    if status == WorkflowRunStatus.WAITING_FOR_INPUT:
        node_name = resolve_paused_node_name(store, run.run_id)
        if node_name is not None:
            renderer.render_pause_capsule(node_name)
    return status


def resolve_paused_node_name(
    store: WorkflowRunStore,
    run_id: Any,
) -> str | None:
    """Resolve the paused `user_input` node's `node_name` from disk.

    Reads the `waiting_at` sidecar (the executor's pause handler
    writes the open invocation_id there); then scans the JSONL for
    the matching `workflow.node.started` event and returns its
    `node_name`. Returns `None` when any step of the lookup fails —
    the chat handler renders no pause capsule in that case rather
    than crashing.
    """
    try:
        run_dir = store.directory(run_id)
    except Exception:  # noqa: BLE001 — store API is internally-typed; defensive
        return None
    waiting_at: Path | None = getattr(run_dir, "waiting_at_path", None)
    if waiting_at is None or not waiting_at.exists():
        return None
    try:
        invocation_id = waiting_at.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not invocation_id:
        return None
    try:
        events = list(store.read_events(run_id))
    except Exception:  # noqa: BLE001 — store API is internally-typed; defensive
        return None
    for event in events:
        if (
            event.get("kind") == "workflow.node.started"
            and str(event.get("invocation_id") or "") == invocation_id
        ):
            return str(event.get("node_name") or "") or None
    return None
