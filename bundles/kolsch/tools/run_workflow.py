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

bug-028 (per-workflow typed virtuals): session 6962 proved even
bug-027's rich prose + retry template isn't enough for Kimi K2.6 —
the model saw the description, called ``workflow_inspect``, read
the error template 13 times, and STILL called ``inputs={}`` byte-
identically. Root cause is structural: ``dict[str, object]``
serialises to ``{additionalProperties: true}`` — no ``properties``,
no ``required``. Fix is per-workflow virtual tools emitted at
tool-list-assembly time with TYPED args models (see
``src/workflows/tool_schema.py`` +
``src/tools/subsystem.py::_register_virtual_workflow_tools``). The
``@tool`` description below now tells the model to prefer the
per-workflow variant when it exists.

bug-031 (blocking + outputs surfacing): pre-bug-031 the tool was
fire-and-forget — it returned ``{run_id, card_id, _chat_anchor}``
at 2ms and exited. Session ``ses_af5980ab-...`` showed this leaves
the model with no way to narrate the outcome: the workflow ran
for 3 min, ``thread.exchange_completed`` fired at second 5, then
a 12 min 59s dead zone before the user asked "what was the
outcome?" — at which point ``workflow_status`` returned
``phase=unknown`` (registry aged out) and the model had to
``bash grep`` the JSONL to reconstruct. The fix converts the tool
to a blocking long-poll at ~500ms until phase is terminal OR
``timeout_seconds`` (default 300) elapses. On terminal the return
carries ``phase`` + ``outputs`` + ``error_message``; on timeout it
returns ``phase="running"`` with a hint to use ``workflow_status``.

See ``process/feat-018-butler-workflow-tools.md`` for the walkthrough.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from errors import WrappedError
from tools.decorator import ToolContext, tool
from tools.services import require_services
from workflows.inputs_scan import extract_required_inputs


_POLL_INTERVAL_SECONDS = 0.5
"""bug-031: how often the blocking tool re-reads ``status()``.

500ms matches the spec. The impl's ``_workflow_registry`` update is
sync — bumping this smaller only trades event-loop time for a shorter
observed tail latency. Bumping it larger widens the "tool_result
arrives after status flipped terminal" gap the user sees on the
transcript.
"""

_DEFAULT_TIMEOUT_SECONDS = 300
"""bug-031: bounded upper limit before the tool falls through.

Session ``ses_af5980ab`` recorded the JD-review workflow at 3 min
23s wall-clock — comfortably inside the 5 min bound. Longer runs
should return ``phase="running"`` so the model narrates the pending
state and can re-check via ``workflow_status``.
"""

_TERMINAL_PHASES = frozenset({"success", "error", "cancelled"})
"""bug-031: services phase vocabulary the poll loop treats as done."""


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
    timeout_seconds: int = Field(
        default=_DEFAULT_TIMEOUT_SECONDS,
        description=(
            "bug-031: upper bound on how long the tool blocks waiting "
            "for terminal. Default 300s. On timeout the tool returns "
            "``phase=\"running\"`` with a note telling the model to "
            "check ``workflow_status`` later."
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
        "status inline.\n\n"
        "This tool BLOCKS until the workflow reaches a terminal phase "
        "(`success` / `error` / `cancelled`) OR the `timeout_seconds` "
        "bound elapses (default 300s). On terminal, returns "
        "`{run_id, card_id, phase, outputs, error_message, "
        "_chat_anchor}` — narrate the `outputs` to the user in prose "
        "after the tool returns. On timeout, returns "
        "`{run_id, card_id, phase: \"running\", _chat_anchor, note}` — "
        "tell the user the workflow is still running and offer to "
        "check `workflow_status(run_id=...)` later.\n\n"
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
    """Open a workflow-kind card + queue the run; block until terminal.

    bug-025: input validation runs BEFORE the card is opened and
    BEFORE the run enters the FIFO queue. A missing required input
    surfaces as ``WrappedError`` (Diagnostic level) so the engine's
    observer turns the failure into a ``TOOL_FAILED`` event with
    ``is_error=true`` in the LLM-facing tool-result payload.

    bug-027: the error message now includes a literal JSON retry
    template so the model can copy the shape and fill in values —
    the "read the text 11 times without figuring out where to put
    the URL" symptom from session 4778.

    bug-031: after ``services.workflows.start(...)`` returns the
    ``run_id``, long-poll ``services.workflows.status(run_id)`` at
    ``_POLL_INTERVAL_SECONDS`` (500ms) until the phase transitions
    to a terminal value or ``args.timeout_seconds`` elapses. On
    terminal return the outputs + error_message alongside the anchor
    so the model narrates the outcome in the same turn. On timeout
    return ``phase="running"`` with a note so the model tells the
    user the workflow is still going and offers a follow-up
    ``workflow_status`` check. Uses ``asyncio.sleep`` per the
    codebase's ``get_running_loop`` convention (D-006).
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

    anchor: dict[str, object] = {
        "kind": "workflow_card",
        "run_id": str(run_id),
        "ref": args.ref,
    }

    # bug-031: long-poll until terminal or timeout. ``status`` is
    # duck-typed on the ``Workflows`` Protocol — headless test fakes
    # that predate bug-031 don't implement it, so guard the probe
    # and fall back to the pre-bug-031 fire-and-forget return (with
    # ``phase="running"`` and a note) so those tests still pass. The
    # duck-typing keeps the Protocol byte-identical.
    status_probe = getattr(services.workflows, "status", None)
    if status_probe is None:
        return {
            "run_id": str(run_id),
            "card_id": str(card.id),
            "phase": "running",
            "_chat_anchor": anchor,
            "note": (
                "Workflow started; this service does not expose "
                "status polling. Use `workflow_status` later if the "
                "service supports it."
            ),
        }

    deadline_polls = max(1, int(args.timeout_seconds / _POLL_INTERVAL_SECONDS))
    for _ in range(deadline_polls):
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        status_view = status_probe(run_id)
        phase = getattr(status_view, "phase", None)
        if isinstance(phase, str) and phase in _TERMINAL_PHASES:
            outputs: dict[str, Any] | None = getattr(
                status_view, "outputs", None,
            )
            error_message = getattr(status_view, "error_message", None)
            return {
                "run_id": str(run_id),
                "card_id": str(card.id),
                "phase": phase,
                "outputs": outputs,
                "error_message": error_message,
                "_chat_anchor": anchor,
            }

    # Timeout — return the running phase so the model can tell the
    # user the workflow's still going. ``workflow_status`` is the
    # follow-up affordance; the note phrasing mirrors the tool's
    # description so the model recognises the pattern.
    return {
        "run_id": str(run_id),
        "card_id": str(card.id),
        "phase": "running",
        "_chat_anchor": anchor,
        "note": (
            f"Workflow still running after {args.timeout_seconds} "
            f"seconds; call workflow_status(run_id={str(run_id)!r}) "
            f"to check later."
        ),
    }
