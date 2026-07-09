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
raises a diagnostic ``ToolExecutionFailed`` when required inputs
are missing from the ``inputs`` arg. Fixes the "workflow errors
silently 2s later, model retries same args" symptom from session
ses_629b626e: the model now sees ``is_error=true`` with an
actionable "Workflow X requires input 'url'" message BEFORE the
run enters the FIFO queue.

See ``process/feat-018-butler-workflow-tools.md`` for the walkthrough.
"""

from __future__ import annotations

from typing import Any  # noqa: F401 — used in helper's recursive type hint

from pydantic import BaseModel, Field

from errors import WrappedError
from tools.decorator import ToolContext, tool
from tools.services import require_services


class RunWorkflowArgs(BaseModel):
    """Arguments for the ``run_workflow`` tool."""

    ref: str = Field(
        description="Workflow ref (``bundle/workflows/name``) to start.",
    )
    inputs: dict[str, object] = Field(
        default_factory=dict,
        description="Input dict forwarded to the workflow.",
    )


def _extract_required_inputs(yaml_source: str) -> set[str]:
    """Return the top-level ``inputs.<name>`` refs without a ``default(...)``.

    Walks the parsed workflow YAML for every string field, parses
    each string as a Jinja template, and collects the first-level
    attribute name off every ``{{ inputs.<name> }}`` chain — but
    ONLY when the reference isn't wrapped in a ``| default(...)``
    filter or an equivalent guard. A workflow that references
    ``{{ inputs.message | default('hi') }}`` treats ``message`` as
    optional (the shipped `kolsch/workflows/hello` sample follows
    exactly this pattern), so the validator MUST NOT flag it as
    missing when the caller supplies ``inputs={}``.

    Returns an empty set when parsing fails or the workflow has no
    inputs references (best-effort probe — bad YAML surfaces later
    via the parser, not here).

    Note: bare ``{{ inputs }}`` (whole-namespace reference) is
    ignored — there's no per-key requirement to name.
    """
    # Local imports keep the module import graph light.
    from jinja2 import Environment  # noqa: PLC0415
    from jinja2.exceptions import TemplateError  # noqa: PLC0415
    from jinja2.nodes import (  # noqa: PLC0415
        Filter,
        Getattr,
        Getitem,
        Name,
    )

    from _yamlio import load  # noqa: PLC0415

    try:
        payload = load(yaml_source)
    except Exception:  # noqa: BLE001 — best-effort
        return set()
    if not isinstance(payload, dict):
        return set()

    env = Environment()  # noqa: S701 — parse-only, no rendering
    required: set[str] = set()

    def _extract_key(node: Any) -> str | None:  # noqa: ANN401
        """Return the top-level ``inputs.<name>`` key from a Getattr/Getitem chain."""
        # Getattr(node=Name('inputs'), attr='foo') → 'foo'.
        if isinstance(node, Getattr) and isinstance(node.node, Name) and node.node.name == "inputs":
            return str(node.attr)
        # Getitem(node=Name('inputs'), arg=Const('foo')) → 'foo'.
        if isinstance(node, Getitem) and isinstance(node.node, Name) and node.node.name == "inputs":
            arg = node.arg
            # jinja2.nodes.Const.value is the literal.
            val = getattr(arg, "value", None)
            if isinstance(val, str) and val:
                return val
        return None

    def _walk_string(s: str) -> None:
        try:
            ast = env.parse(s)
        except TemplateError:
            return
        # Collect the top-level `inputs.<name>` chain nodes that live
        # under a `default(...)` filter — those are optional.
        guarded: set[int] = set()
        for filt in ast.find_all(Filter):
            # A `default` filter guards its subject `node`.
            if getattr(filt, "name", None) == "default":
                # The subject may be a nested Getattr/Getitem chain —
                # walk down to find the immediate `inputs.<name>` root.
                _collect_guarded_ids(filt.node, guarded)
        # Now collect every `inputs.<name>` reference; skip guarded ones.
        for chain in ast.find_all((Getattr, Getitem)):
            key = _extract_key(chain)
            if key is None:
                continue
            if id(chain) in guarded:
                continue
            required.add(key)

    def _collect_guarded_ids(node: Any, out: set[int]) -> None:  # noqa: ANN401
        """Mark this node id (and descendant inputs chains) as guarded."""
        out.add(id(node))
        # Recurse into common containers so `default(inputs.foo)` and
        # `inputs.foo | default(...) | trim` still mark `inputs.foo`.
        for child in getattr(node, "iter_child_nodes", lambda: [])():
            _collect_guarded_ids(child, out)

    def _walk(node: Any) -> None:  # noqa: ANN401
        if isinstance(node, str):
            _walk_string(node)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(payload)
    return required


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
        "`[label](kolsch-anchor://workflow_card/<run_id>)`."
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
    surfaces as ``ToolExecutionFailed`` (Diagnostic level) so the
    engine's observer turns the failure into a ``TOOL_FAILED``
    event with ``is_error=true`` in the LLM-facing tool-result
    payload. The model sees an actionable "requires input 'url'"
    message and can retry with proper inputs — or explain to the
    user that the workflow needs an argument it didn't get.
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
            required = _extract_required_inputs(yaml_source)
            supplied = set(args.inputs.keys())
            missing = sorted(required - supplied)
            if missing:
                names = ", ".join(f"'{n}'" for n in missing)
                msg = (
                    f"Workflow {args.ref} requires input {names}; "
                    f"got inputs={sorted(supplied)!r}"
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
