"""Start a workflow. Butler-facing.

Demonstrates:

- ``@tool(requires=["cards", "workflows"])`` — dual-service opt-in.
- Card ownership transfer via ``Workflows.start(card=...)``: the
  ``WorkflowsImpl.start`` implementation flips
  ``card._owner_transferred=True`` so the tool's auto-close finalizer
  (see ``tools.dispatch._invoke_tool``) skips the card. Terminal
  close is owned by the workflow service.
- ``_chat_anchor`` return payload — the butler's prose can reference
  the mounted workflow card via
  ``[<label>](kolsch-anchor://workflow_card/<run_id>)`` markup;
  ``AssistantProse`` renders those anchors as focusable inline
  buttons that scroll the transcript to the card on Enter / click.

bug-025 (input validation): before enqueueing the run, the tool
scans the workflow YAML for ``{{ inputs.<name> }}`` references and
raises a diagnostic ``WrappedError`` when required inputs are
missing from the ``inputs`` arg. ``| default(...)``-guarded
references are treated as optional — matches the
``kolsch/workflows/hello`` sample's ``inputs.message`` shape.
Fixes the "workflow errors silently 2s later, model retries same
args" symptom from session ses_629b626e.

bug-027 (inputs guidance): the error message now carries a JSON
retry template, and the ``@tool`` description primes the model to
identify required inputs from ``{{ inputs.X }}`` refs in the
workflow YAML. See ``workflow_inspect`` for the introspection
affordance.

bug-028 (per-workflow typed virtuals): a per-workflow typed variant
of this tool is emitted at tool-list-assembly time. The description
below points the model at the virtual when it exists.

See ``process/feat-018-butler-workflow-tools.md`` for the walkthrough.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from errors import WrappedError
from tools.decorator import ToolContext, tool
from tools.services import require_services
from workflows.inputs_scan import extract_required_inputs


class RunWorkflowArgs(BaseModel):
    """Arguments for the ``run_workflow`` tool."""

    ref: str = Field(
        description="Workflow ref (``bundle/workflows/name``) to start.",
    )
    inputs: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "Input dict forwarded to the workflow. Keys MUST match the "
            "workflow's declared ``{{ inputs.<name> }}`` references. "
            'Example: ``{"url": "https://example.com/job"}``.'
        ),
    )


@tool(
    name="run_workflow",
    description=(
        "Start a workflow by ref (`bundle/workflows/name`) and mount a "
        "live workflow card in the chat transcript. Prefer this over "
        "invoking `kolsch workflows run` through the `bash` tool — "
        "`bash` streams raw stdout into a plain tool-call block, while "
        "`run_workflow` opens a `WorkflowCard` widget that renders "
        "per-node progress rows, pause/approval prompts, and terminal "
        "status inline. Returns `{run_id, card_id, _chat_anchor}`; the "
        "anchor lets your prose reference the mounted card via "
        "`[label](kolsch-anchor://workflow_card/<run_id>)`.\n\n"
        "The workflow card renders progress INLINE in the transcript "
        "— header, per-node rows, and status footer are all visible "
        "on mount and update live. There is no dropdown, expand-to-"
        "view, or user gesture required to see progress. Do NOT "
        "instruct the user to expand, open, click, or drop down "
        "anything on the card. To draw the user's eye to it, emit "
        "the `_chat_anchor` markdown "
        "`[label](kolsch-anchor://workflow_card/<run_id>)`.\n\n"
        "When a per-workflow tool like "
        "`run_workflow_<bundle>_workflows_<workflow>` exists in your "
        "tool list, prefer that over calling this generic tool — the "
        "per-workflow variant surfaces each input as a typed argument "
        "so you don't need to construct a JSON `inputs` dict.\n\n"
        "BEFORE CALLING: identify required inputs by reading the "
        "workflow YAML. Every `{{ inputs.X }}` reference is a required "
        "key in `inputs`; a `{{ inputs.X | default(...) }}` reference "
        "is optional. `inputs` MUST be a dict keyed by those names, "
        "with the values sourced from the user's request. If unsure, "
        "call `workflow_inspect(ref=...)` first — it returns the "
        "required/optional input names and an example call.\n\n"
        "Example: run_workflow(ref=\"andre/workflows/job_description_review\", "
        "inputs={\"url\": \"https://example.com/job\"})."
    ),
    args_schema=RunWorkflowArgs,
    requires=["cards", "workflows"],
)
async def run_workflow(
    args: RunWorkflowArgs, ctx: ToolContext,
) -> dict[str, object]:
    """Open a workflow-kind card + queue the run; return ids + anchor.

    bug-025: input validation runs BEFORE the card is opened and
    BEFORE the run enters the FIFO queue.

    bug-027: the error message now includes a JSON retry template so
    the model can copy the shape and fill in values.
    """
    services = require_services(ctx, ["cards", "workflows"])
    assert services.cards is not None  # narrowed by require_services
    assert services.workflows is not None  # narrowed by require_services

    # bug-025: fetch the workflow YAML via the service seam and
    # validate `inputs` against the declared `{{ inputs.X }}`
    # references. Duck-typed to keep the `Workflows` Protocol byte-
    # identical — headless test fakes without this method fall
    # through to the no-validation path (matches pre-bug-025).
    resolver = getattr(services.workflows, "resolve_workflow_yaml", None)
    if resolver is not None:
        yaml_source = resolver(args.ref)
        if isinstance(yaml_source, str):
            required = extract_required_inputs(yaml_source)
            supplied = set(args.inputs.keys())
            missing = sorted(required - supplied)
            if missing:
                # bug-027: emit a JSON retry template as part of the
                # error so the model has a shape to copy, not just a
                # key name to guess about.
                names = ", ".join(f"'{n}'" for n in missing)
                template_pairs = ", ".join(
                    f'"{n}": "<value from user request>"' for n in missing
                )
                template = "{" + template_pairs + "}"
                msg = (
                    f"Workflow {args.ref} requires input(s) {names}. "
                    f"Retry with: inputs={template} "
                    f"(fill each missing key with the value from the "
                    f"user's request). You sent: inputs={sorted(supplied)!r}."
                )
                raise WrappedError(
                    message=msg,
                    where="bundles.kolsch.tools.run_workflow.validate_inputs",
                    detail={
                        "ref": args.ref,
                        "missing_inputs": missing,
                        "supplied_inputs": sorted(supplied),
                    },
                )

    card = await services.cards.open(
        f"Workflow · {args.ref}", kind="workflow",
    )
    run_id = await services.workflows.start(
        args.ref, dict(args.inputs), card=card,
    )
    return {
        "run_id": str(run_id),
        "card_id": str(card.id),
        "_chat_anchor": {
            "kind": "workflow_card",
            "run_id": str(run_id),
            "ref": args.ref,
        },
    }
