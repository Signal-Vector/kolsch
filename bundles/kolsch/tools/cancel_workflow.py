"""Cancel a running workflow. Butler-facing.

Demonstrates:

- ``@tool(requires=["workflows"])`` — single-service opt-in.
- ``Workflows.cancel(run_id, cascade=...)`` — the ``cascade=True``
  variant walks ancestors + descendants (spec-017).

See ``process/feat-018-butler-workflow-tools.md`` for the walkthrough.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from tools.decorator import ToolContext, tool
from tools.services import require_services
from workflows.ids import WorkflowRunId


class CancelWorkflowArgs(BaseModel):
    """Arguments for the ``cancel_workflow`` tool."""

    run_id: str = Field(
        description="Workflow run id (``wrk_<uuid>``) to cancel.",
    )
    cascade: bool = Field(
        default=False,
        description="Cancel run + all its ancestors + descendants.",
    )


@tool(
    name="cancel_workflow",
    description=(
        "Cancel an in-flight workflow run by id. Optional "
        "`cascade=True` cancels the run's whole ancestor/descendant "
        "tree (spec-017). Prefer this over `bash kolsch workflows "
        "cancel <run_id>` — this tool cooperates with the live "
        "`WorkflowsImpl` service so the `WorkflowCard` in the chat "
        "transcript transitions to `cancelling`/`cancelled` in place, "
        "instead of leaving the card in a stale state while a "
        "subprocess signals the executor. Returns `{cancelled, "
        "run_id, cascade}`."
    ),
    args_schema=CancelWorkflowArgs,
    requires=["workflows"],
)
async def cancel_workflow(
    args: CancelWorkflowArgs, ctx: ToolContext,
) -> dict[str, object]:
    """Fire the cancel; return `{cancelled, run_id, cascade}`."""
    services = require_services(ctx, ["workflows"])
    assert services.workflows is not None  # narrowed by require_services
    fired = await services.workflows.cancel(
        WorkflowRunId(args.run_id), cascade=args.cascade,
    )
    return {
        "cancelled": bool(fired),
        "run_id": args.run_id,
        "cascade": args.cascade,
    }
