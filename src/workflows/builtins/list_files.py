"""``kolsch/nodes/list_files`` runner — Phase 3.5 Step 2.

Master spec §4.2.3. Renders ``path`` and the optional ``pattern``;
dispatches via the in-engine :func:`tools.builtins.make_list_files`
factory; narrows the tool's ``list[{name, path, is_dir, size_bytes}]``
return to ``{entries: list[str]}`` — the canonical workflow output
contract.

Naming-mismatch note: the parser's :attr:`ListFilesNodeDef.pattern`
maps to the underlying tool's ``glob`` argument. The tool natively
honours the filter; the runner forwards verbatim and applies no
secondary :func:`fnmatch.filter` pass — the spec's "runner-side
filter is the safety net" hedge was for cases where the tool ignored
the pattern, which the in-engine ``make_list_files`` does NOT.

Non-recursive at v1 per master spec §4.2.3 — the runner does not
inject ``**`` into the glob; if the author writes ``pattern: "**/*"``
the tool's existing recursive-walk behaviour kicks in (forward-compat).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from errors import FileAccessDenied
from schemas.identifiers import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext
from workflows.errors import WorkflowFileAccessDenied

if TYPE_CHECKING:
    from providers.authz import AuthorizationProvider
    from providers.sandbox import SandboxProvider
    from providers.secrets import SecretsProvider
    from tools.decorator import ToolDefinition
    from workflows.context import NodeContext
    from workflows.parser import ListFilesNodeDef


__all__ = ["ListFilesRunner"]


_SYNTHETIC_AGENT = AgentId("ag_workflow_executor")


class ListFilesRunner:
    """Runtime for ``kolsch/nodes/list_files`` — Phase 3.5 §4.2.3."""

    def __init__(
        self,
        *,
        tool_defn: ToolDefinition,
        authz: AuthorizationProvider,
        secrets: SecretsProvider,
        sandbox: SandboxProvider,
    ) -> None:
        self._defn = tool_defn
        self._authz = authz
        self._secrets = secrets
        self._sandbox = sandbox

    async def run(
        self,
        *,
        node_name: str,
        node_def: ListFilesNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        _ = state
        from tools.dispatch import dispatch_tool  # noqa: PLC0415

        rendered_path = _render_str(
            ctx, node_def.path, bound_inputs=bound_inputs
        )
        args: dict[str, Any] = {"path": rendered_path}
        if node_def.pattern is not None:
            # NodeDef.pattern → tool arg.glob (naming mismatch — see
            # module docstring).
            args["glob"] = _render_str(
                ctx, node_def.pattern, bound_inputs=bound_inputs
            )

        tool_ctx = _build_tool_ctx(ctx)
        try:
            result = await dispatch_tool(
                defn=self._defn,
                args=args,
                context=tool_ctx,
                authz=self._authz,
                secrets=self._secrets,
                sandbox=self._sandbox,
            )
        except FileAccessDenied as exc:
            raise WorkflowFileAccessDenied(
                path=rendered_path,
                node=node_name,
            ) from exc
        # The tool returns a list of metadata dicts; the spec narrows
        # to LEAF names only. Full paths drop — authors get them via
        # the agent-side tool dispatch when they need the structured
        # shape.
        entries: list[str] = [str(item["name"]) for item in result]
        return {"entries": entries}


def _render_str(
    ctx: NodeContext,
    value: str,
    *,
    bound_inputs: dict[str, Any],
) -> str:
    if "{{" not in value:
        return value
    return str(ctx.render_jinja(value, inputs_overlay=bound_inputs))


def _build_tool_ctx(ctx: NodeContext) -> ToolContext:
    return ToolContext(
        agent_id=_SYNTHETIC_AGENT,
        session_id=SessionId(f"ses_{uuid.uuid4()}"),
        thread_id=ThreadId(f"th_{uuid.uuid4()}"),
        principal=f"workflow.{ctx.workflow_run_id}",
        secrets={},
        workspace_root=ctx.workspace_root,
        path_policy=None,
        tool_call_id=None,
    )
