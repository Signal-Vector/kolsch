"""`introspect_node` — load an authored `@node` module and report its shape.

The workflow-authoring agent calls this tool right after writing a
new authored Python `@node` file so it can iterate on the signature
BEFORE invoking the `validate_workflow` tool (which would also catch
the issue, but with a less direct error chain that runs the entire
run-start gate first).

Delegates to
[`introspect_node_module`][workflows.catalog_node.introspect_node_module]
— the public helper that mirrors the run-start gate's own
introspection logic.

Pure read — never writes, never spawns. The module IS imported (so
the path policy is enforced); the import happens in an isolated
namespace so two consecutive introspections of distinct files with
the same stem don't collide in `sys.modules`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from errors import ToolExecutionFailed
from files import check_path_allowed
from tools.decorator import ToolContext, tool
from workflows.catalog_node import introspect_node_module


class IntrospectNodeArgs(BaseModel):
    """Single argument naming the `@node` Python file."""

    py_path: str = Field(
        description=(
            "Path to the authored ``@node`` Python module. "
            "Workspace-relative (``bundles/my-bundle/nodes/csv_count.py``) "
            "or absolute (must resolve inside "
            "``file_access.allowed_paths``)."
        ),
    )


class NodeIntrospection(BaseModel):
    """The tool's return shape."""

    signature_ok: bool = Field(
        description=(
            "True iff the module imported cleanly AND exposed a "
            "``@node``-decorated ``run`` with a conforming signature "
            "AND a conforming return annotation."
        ),
    )
    label: str | None = Field(
        default=None,
        description=(
            "The ``@node(label=...)`` value when the decorator was "
            "applied (and even when subsequent checks failed). "
            "``None`` when the decorator was missing entirely or no "
            "``label`` was set."
        ),
    )
    inputs_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "JSON-Schema for the ``run`` parameter-1 inputs model. "
            "``None`` when the introspector couldn't reach this field."
        ),
    )
    outputs_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "JSON-Schema for the outputs model embedded in the return "
            "annotation. ``None`` for a ``bool``-only return or when "
            "the introspector couldn't reach this field."
        ),
    )
    errors: list[str] = Field(
        default_factory=list,
        description=(
            "Single-line diagnostic strings. Empty list iff ``signature_ok`` is True."
        ),
    )


# Discovery loads the file via a synthetic module name; combined with
# ``from __future__ import annotations`` Pydantic needs an explicit
# rebuild to resolve the stringified ``dict[str, Any]`` against the
# synthetic module's namespace.
NodeIntrospection.model_rebuild()


@tool(
    name="introspect_node",
    summary="Load an authored @node module and return its signature info.",
    description=(
        "Load an authored Python ``@node`` module by file path, "
        "introspect its ``run`` signature + return annotation, and "
        "return either the resolved schemas or a list of errors. Use "
        "this tool right after writing a new ``@node`` file (and "
        "BEFORE wiring it into a workflow YAML) so signature mistakes "
        "surface immediately instead of cascading through the "
        "workflow validator's catalog-load pass. "
        "\n\n"
        "Returns ``{signature_ok: bool, label: str | None, "
        "inputs_schema: dict | None, outputs_schema: dict | None, "
        "errors: [str]}``. Never raises on a bad module — every "
        "failure surfaces in ``errors`` so the agent's iteration "
        "loop never crashes mid-draft. Missing files, syntax errors, "
        "missing ``@node`` decorator, wrong signature shape, wrong "
        "return type — all surface as ``signature_ok: false`` with "
        "one or more diagnostic strings."
    ),
    args_schema=IntrospectNodeArgs,
)
async def introspect_node(args: IntrospectNodeArgs, ctx: ToolContext) -> dict[str, Any]:
    """Run the introspector and return a `NodeIntrospection` dict."""
    if ctx.path_policy is None or ctx.workspace_root is None:
        msg = (
            "introspect_node requires the platform to populate "
            "ctx.path_policy and ctx.workspace_root."
        )
        raise ToolExecutionFailed(
            tool_name="introspect_node", error_class="RuntimeError"
        ) from RuntimeError(msg)

    check_path_allowed(args.py_path, policy=ctx.path_policy, root=ctx.workspace_root)
    target = _resolve(args.py_path, ctx.workspace_root)
    result = introspect_node_module(target)
    return NodeIntrospection(
        signature_ok=result.signature_ok,
        label=result.label,
        inputs_schema=result.inputs_schema,
        outputs_schema=result.outputs_schema,
        errors=list(result.errors),
    ).model_dump(mode="json")


def _resolve(path: str, root: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate
