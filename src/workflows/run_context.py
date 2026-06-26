"""Per-async-task workflow run context — Phase 3.1 Step 5.

The authz provider and other sub-systems need to know which workflow
run is currently executing so they can emit ``policy.decision.made`` /
``workflow.tool.called`` events to the right JSONL. The natural
plumbing — passing the run id through every API surface — was rejected
in master spec §11 design rounds as too invasive. Instead, the
executor publishes a :class:`WorkflowRunContext` on a contextvar at
run start and clears it at run end; callers under the same async task
read it without touching their function signatures.

:class:`contextvars.ContextVar` is the right primitive here (not
``threading.local``) — asyncio's task copy semantics mean each child
coroutine sees the parent's snapshot, so the authz emit inside a
``dispatch_tool`` call still observes the executor's run.

The context object is intentionally small: just the bits the authz
emission needs (run id, trace id, store handle, audit level). Adding
more fields here ties new sub-systems to the executor's lifecycle —
add them only when a sub-system needs them.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from workflows.ids import TraceId, WorkflowRunId
    from workflows.run_store import WorkflowRunStore

__all__ = [
    "WorkflowRunContext",
    "current_workflow_run_context",
    "reset_workflow_run_context",
    "set_workflow_run_context",
]


@dataclass(frozen=True, slots=True)
class WorkflowRunContext:
    """The handle a sub-system reads to emit into the active run."""

    run_id: WorkflowRunId
    trace_id: TraceId
    store: WorkflowRunStore


_current_run_context: ContextVar[WorkflowRunContext | None] = ContextVar(
    "workflow_run_context", default=None
)


def current_workflow_run_context() -> WorkflowRunContext | None:
    """Return the active context or ``None`` if no run is in flight.

    Sub-systems that need to emit into the JSONL call this; ``None``
    means "no workflow run active — skip the workflow-side emission".
    """
    return _current_run_context.get()


def set_workflow_run_context(
    context: WorkflowRunContext | None,
) -> object:
    """Publish the context for the current async task.

    Returns the contextvar token; callers MUST pass it to
    :func:`reset_workflow_run_context` in a ``finally`` block so a
    partial-fail run doesn't leak its handle into the next operation
    on the same event loop.
    """
    return _current_run_context.set(context)


def reset_workflow_run_context(token: object) -> None:
    """Roll the contextvar back to its prior value via ``token``.

    ``token`` is whatever :func:`set_workflow_run_context` returned
    on the matching ``set`` call. Pairs structurally with that call
    inside a ``finally`` so nesting + early returns can't desync.
    """
    _current_run_context.reset(token)  # type: ignore[arg-type]
