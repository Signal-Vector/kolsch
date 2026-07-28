"""Read status of a running/completed workflow. Butler-facing.

Demonstrates:

- ``@tool(requires=["workflows"])`` — single-service opt-in.
- ``Workflows.status`` — sync, non-blocking; registry-first with
  cold-read fallback (see ``WorkflowsImpl.status``).
- ``_chat_anchor`` return payload — same shape as ``run_workflow``
  so the butler's prose can re-anchor to an existing card by
  ``run_id`` without holding the ``card_id`` in its own state.

See ``process/feat-018-butler-workflow-tools.md`` for the walkthrough.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from tools.decorator import ToolContext, tool
from tools.services import require_services
from workflows.ids import WorkflowRunId


class WorkflowStatusArgs(BaseModel):
    """Arguments for the ``workflow_status`` tool."""

    run_id: str = Field(
        description="Workflow run id (``wrk_<uuid>``) to look up.",
    )


@tool(
    name="workflow_status",
    description=(
        "Read the current phase, node counts, and last-event timestamp "
        "for a workflow run started via `run_workflow` (or any other "
        "in-session start). Prefer this over probing status through "
        "the `bash` tool (`kolsch workflows status <run_id>`) — this "
        "tool returns a structured dict AND a `_chat_anchor` you can "
        "embed as `[label](kolsch-anchor://workflow_card/<run_id>)` "
        "so the user's prose links back to the live card. Non-"
        "blocking; safe to call while the run is executing.\n\n"
        "The workflow card renders progress INLINE in the transcript "
        "— header, per-node rows, and status footer are all visible "
        "on mount and update live. There is no dropdown, expand-to-"
        "view, or user gesture required to see progress. Do NOT "
        "instruct the user to expand, open, click, or drop down "
        "anything on the card. To draw the user's eye to it, emit "
        "the `_chat_anchor` markdown "
        "`[label](kolsch-anchor://workflow_card/<run_id>)`."
    ),
    args_schema=WorkflowStatusArgs,
    requires=["workflows"],
)
async def workflow_status(
    args: WorkflowStatusArgs,
    ctx: ToolContext,
) -> dict[str, object]:
    """Return the run's phase view + a ``_chat_anchor`` for prose."""
    services = require_services(ctx, ["workflows"])
    assert services.workflows is not None  # noqa: S101 — type-narrowing after require_services
    status = services.workflows.status(WorkflowRunId(args.run_id))
    node_counts: dict[str, int] | None = None
    if status.node_counts is not None:
        node_counts = {
            "total": status.node_counts.total,
            "running": status.node_counts.running,
            "completed": status.node_counts.completed,
            "failed": status.node_counts.failed,
            "pending": status.node_counts.pending,
        }
    return {
        "run_id": str(status.run_id),
        "phase": status.phase,
        "ref": status.ref,
        "node_counts": node_counts,
        "elapsed_seconds": status.elapsed_seconds,
        "last_event_ts": (
            status.last_event_ts.isoformat()
            if status.last_event_ts is not None
            else None
        ),
        # bug-025: terminal-error diagnostics surface here so the model
        # sees a legible failure reason instead of ``phase="error"``
        # with no attribution.
        "error_message": status.error_message,
        "failed_node": status.failed_node,
        # bug-031: terminal outputs from ``workflow.run.completed`` so
        # a late poll (past ``_REGISTRY_EVICTION_SECONDS``) still
        # returns the outputs dict. On a live/in-flight run this is
        # ``None`` — call this tool again once the phase flips.
        "outputs": status.outputs,
        # bug-066: the canonical on-disk identifier. The registry
        # ``run_id`` (above) is the in-memory queue-time id — it may
        # not correlate with anything under ``runs/`` because the
        # executor's ``WorkflowRunStore.create`` mints its own id.
        # ``on_disk_run_id`` is the ``wrk_<uuid>`` that keys
        # ``runs/<on_disk_run_id>/`` (``meta.json`` /
        # ``events.jsonl`` / ``pid`` / ``cancel.flag``) — this is
        # the id operators should ``ls runs/`` or ``kolsch workflows
        # tail`` with. ``None`` while the run is still ``queued`` —
        # poll again after the executor starts.
        "on_disk_run_id": (
            str(status.on_disk_run_id) if status.on_disk_run_id is not None else None
        ),
        "_chat_anchor": {
            "kind": "workflow_card",
            "run_id": str(status.run_id),
            "ref": status.ref,
        },
    }
