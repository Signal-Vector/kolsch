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
    args: CancelWorkflowArgs,
    ctx: ToolContext,
) -> dict[str, object]:
    """Fire the cancel; return `{cancelled, run_id, cascade}`.

    bug-025: when the target run is already terminal, the return
    payload also carries ``phase`` and ``reason`` so the model sees
    a structured no-op instead of a silent ``cancelled=false``.
    ``reason`` mirrors :attr:`WorkflowRunStatusView.error_message`
    when available (the failure's diagnostic); falls back to
    ``"already terminal"``. Fixes the "cancel returns false; model
    keeps retrying" symptom from session ses_629b626e.
    """
    services = require_services(ctx, ["workflows"])
    assert services.workflows is not None  # narrowed by require_services
    run_id = WorkflowRunId(args.run_id)
    fired = await services.workflows.cancel(run_id, cascade=args.cascade)
    result: dict[str, object] = {
        "cancelled": bool(fired),
        "run_id": args.run_id,
        "cascade": args.cascade,
    }
    if not fired:
        # bug-025: enrich the no-op response with the terminal reason.
        # `status()` is sync + non-blocking; on a fully unknown run it
        # returns `phase="unknown"` and both diagnostic fields `None`.
        try:
            view = services.workflows.status(run_id)
        except Exception:  # noqa: BLE001 — best-effort enrichment
            return result
        if view.phase in ("success", "error", "cancelled", "unknown"):
            result["phase"] = view.phase
            result["reason"] = view.error_message or "already terminal"
            if view.failed_node is not None:
                result["failed_node"] = view.failed_node
    return result
