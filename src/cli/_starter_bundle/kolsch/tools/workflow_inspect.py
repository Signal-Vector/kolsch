"""Inspect a workflow's inputs contract. Butler-facing (bug-027).

Introspection affordance the model can call BEFORE ``run_workflow``
to discover which ``inputs`` keys the target workflow needs.
Bug-027's session 4778 caught the model retrying
``run_workflow(inputs={})`` 11 times against a workflow it had no
schema for; ``workflow_inspect`` gives the model a cheap path to
"which keys go in ``inputs``?" without reading the raw YAML.

Contract:

- Args: ``{ref: str}`` — the 3-seg workflow ref.
- Returns: ``{ref, description, required_inputs, optional_inputs,
  example_call}``.
- Unknown ref: raises ``WrappedError`` with a helpful "workflow not
  found; available refs: ..." message.

bug-028: when per-workflow virtual tools are registered (default),
``example_call`` uses the virtual tool name and typed args shape.
Falls back to bug-027's ``run_workflow(ref=..., inputs={...})``
shape when ``KOLSCH_VIRTUAL_WORKFLOW_TOOLS=0``.

Reuses ``scan_inputs`` from ``src/workflows/inputs_scan.py``
(bug-025's walker, extracted for this bug per AC #13).
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

from errors import WrappedError
from tools.decorator import ToolContext, tool
from tools.services import require_services
from workflows.inputs_scan import scan_inputs
from workflows.tool_schema import virtual_tool_name


class WorkflowInspectArgs(BaseModel):
    """Arguments for the ``workflow_inspect`` tool."""

    ref: str = Field(
        description=(
            "Workflow ref (``bundle/workflows/name``) to inspect. "
            "Example: ``kolsch/workflows/hello``."
        ),
    )


def _virtuals_enabled() -> bool:
    """Mirror ``ToolsSubsystem._virtual_tools_enabled`` — env-only kill switch."""
    raw = os.environ.get("KOLSCH_VIRTUAL_WORKFLOW_TOOLS", "1")
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _build_example_call(
    ref: str,
    required: list[str],
    *,
    prefer_virtual: bool,
) -> str:
    """Return a copy-pasteable invocation string for the model.

    bug-028: prefers the per-workflow virtual tool shape
    (``run_workflow_<bundle>_<name>(url="<value>")``) when
    ``prefer_virtual=True``; falls back to bug-027's
    ``run_workflow(ref="...", inputs={...})`` shape otherwise.
    """
    if prefer_virtual:
        tool_name = virtual_tool_name(ref)
        if not required:
            return f"{tool_name}()"
        pairs = ", ".join(f'{n}="<value from user request>"' for n in required)
        return f"{tool_name}({pairs})"
    if not required:
        return f'run_workflow(ref="{ref}", inputs={{}})'
    pairs = ", ".join(f'"{n}": "<value from user request>"' for n in required)
    return f'run_workflow(ref="{ref}", inputs={{{pairs}}})'


@tool(
    name="workflow_inspect",
    description=(
        "Inspect a workflow's inputs contract before calling "
        "`run_workflow`. Returns `{ref, description, required_inputs, "
        "optional_inputs, required_inputs_including_children, "
        "example_call}` — `required_inputs` are keys you MUST supply "
        "in `run_workflow`'s `inputs` dict for the workflow's OWN "
        "direct references, `optional_inputs` have `{{ inputs.X | "
        "default(...) }}` guards and can be omitted, "
        "`required_inputs_including_children` also folds in "
        "sub-workflow-child requirements that aren't covered by "
        "explicit edge binds (this is the set `example_call` targets), "
        "`example_call` is a ready-to-copy `run_workflow(...)` "
        "invocation. Read-only — safe to call before every "
        "`run_workflow` when you're unsure which inputs the target "
        "workflow needs."
    ),
    args_schema=WorkflowInspectArgs,
    requires=["workflows"],
)
async def workflow_inspect(
    args: WorkflowInspectArgs,
    ctx: ToolContext,
) -> dict[str, object]:
    """Return the workflow's inputs contract + a copy-pasteable example."""
    services = require_services(ctx, ["workflows"])
    assert services.workflows is not None  # narrowed by require_services

    resolver = getattr(services.workflows, "resolve_workflow_yaml", None)
    yaml_source: str | None = None
    if resolver is not None:
        raw = resolver(args.ref)
        if isinstance(raw, str):
            yaml_source = raw

    if yaml_source is None:
        list_refs = getattr(services.workflows, "list_workflow_refs", None)
        available: list[str] = []
        if list_refs is not None:
            try:
                candidate = list_refs()
            except Exception:  # noqa: BLE001 — best-effort probe
                candidate = []
            if isinstance(candidate, list):
                available = [str(r) for r in candidate if isinstance(r, str)]
        if available:
            refs_str = ", ".join(available)
            msg = f"Workflow {args.ref!r} not found. Available refs: {refs_str}."
        else:
            msg = (
                f"Workflow {args.ref!r} not found. "
                "The workspace has no catalogued workflows visible to "
                "this agent."
            )
        raise WrappedError(
            message=msg,
            where="bundles.kolsch.tools.workflow_inspect.resolve",
            detail={"ref": args.ref, "available_refs": available},
        )

    scan = scan_inputs(yaml_source)
    description = _extract_description(yaml_source)

    # qol-097: probe the promoted required set (direct scan + refs
    # coming from sub-workflow children minus edge-bind coverage).
    # Duck-typed via the `resolve_effective_required_inputs` seam so
    # older service instances / mocks that don't expose the method
    # fall through to `required_inputs_including_children ==
    # required_inputs` (backwards-compat). `example_call` is rebuilt
    # from the promoted set so bug-027's copy-pasteable retry
    # template matches the reported requirements.
    promoted_fn = getattr(services.workflows, "resolve_effective_required_inputs", None)
    promoted = promoted_fn(args.ref) if promoted_fn is not None else None
    # Sort the fallback so both branches produce the same ordering for
    # any given ref — the promoted branch already returns sorted.
    required_including_children: list[str] = (
        promoted if isinstance(promoted, list) else sorted(scan.required)
    )

    example = _build_example_call(
        args.ref,
        required_including_children,
        prefer_virtual=_virtuals_enabled(),
    )
    return {
        "ref": args.ref,
        "description": description,
        "required_inputs": list(scan.required),
        "optional_inputs": list(scan.optional),
        "required_inputs_including_children": required_including_children,
        "example_call": example,
    }


def _extract_description(yaml_source: str) -> str:
    """Return the workflow's top-level ``description:`` field, or ``""``."""
    from kolsch._yamlio import load  # noqa: PLC0415

    try:
        payload = load(yaml_source)
    except Exception:  # noqa: BLE001 — best-effort
        return ""
    if not isinstance(payload, dict):
        return ""
    desc = payload.get("description")
    if isinstance(desc, str):
        return desc
    return ""
