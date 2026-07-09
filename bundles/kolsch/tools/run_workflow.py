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
missing from the ``inputs`` arg. Fixes the "workflow errors
silently 2s later, model retries same args" symptom from session
ses_629b626e: the model now sees ``is_error=true`` with an
actionable "Workflow X requires input 'url'" message BEFORE the
run enters the FIFO queue.

bug-027 (inputs guidance): bug-025's error text named the missing
key but showed no JSON shape. Session 4778 caught the model
retrying ``inputs={}`` 11 times before giving up because it
couldn't translate "requires input 'url'" into the ``inputs={"url":
"..."}`` shape. This module now emits a copy-pasteable retry
template as part of the error message, and the ``@tool`` description
explicitly primes the model to identify required inputs from
``{{ inputs.X }}`` refs in the workflow YAML. See
``bundles/kolsch/tools/workflow_inspect.py`` for the introspection
affordance.

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
    BEFORE the run enters the FIFO queue. A missing required input
    surfaces as ``WrappedError`` (Diagnostic level) so the engine's
    observer turns the failure into a ``TOOL_FAILED`` event with
    ``is_error=true`` in the LLM-facing tool-result payload.

    bug-027: the error message now includes a literal JSON retry
    template so the model can copy the shape and fill in values —
    the "read the text 11 times without figuring out where to put
    the URL" symptom from session 4778.
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
                # bug-027: build a copy-pasteable JSON retry template
                # so the model has a shape to fill in rather than
                # guessing from a text-only error message. AC #1
                # requires four substrings: ref, missing names,
                # literal `inputs={"<key>": "..."}` template, and the
                # supplied inputs.
                names = ", ".join(f"'{n}'" for n in missing)
                # Build the retry template dict with the first missing
                # key as the example — every missing key maps to the
                # same placeholder so the model can extend the shape
                # for the rest.
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
                # `WrappedError` is a Diagnostic `PlatformError` whose
                # `message` is preserved verbatim through the engine
                # observer at `src/engine/observers.py:284` — the LLM
                # sees the specific "requires input 'url'" text in the
                # `TOOL_FAILED` payload with `is_error=true`. Preferred
                # over `raise ValueError(msg)` (which the dispatch
                # would wrap in `ToolExecutionFailed(error_class=
                # "ValueError")`, hiding the message inside `cause`).
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
