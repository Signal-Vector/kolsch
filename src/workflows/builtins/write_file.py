"""`kolsch/nodes/write_file` runner — narrows tool output to `bytes_written`.

Renders BOTH `path` AND `content` (both Jinja-eligible);
dispatches via the in-engine
[`make_write_file`][tools.builtins.make_write_file] factory
(closure over policy + root); narrows the tool's
`{path, bytes_written, created}` return to `{bytes_written}` — the
canonical workflow output contract.

`WriteFileNodeDef.content` is typed `str | bytes` (the parser
accepts both for forward-compat); `bytes` pass through verbatim
without Jinja rendering (strings only). `FileAccessDenied` remap
matches the read-file runner's.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from errors import FileAccessDenied
from schemas.caller_chain import CallerChain
from schemas.identifiers import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext
from workflows.errors import WorkflowFileAccessDenied

if TYPE_CHECKING:
    from providers.authz import AuthorizationProvider
    from providers.sandbox import SandboxProvider
    from providers.secrets import SecretsProvider
    from tools.decorator import ToolDefinition
    from workflows.context import NodeContext
    from workflows.parser import WriteFileNodeDef


__all__ = ["WriteFileRunner"]


_SYNTHETIC_AGENT = AgentId("ag_workflow_executor")


class WriteFileRunner:
    """Runner for `kolsch/nodes/write_file` — captures `{bytes_written}`."""

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
        node_def: WriteFileNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        """Render path + content and dispatch `write_file`.

        Raises:
            WorkflowFileAccessDenied: The rendered path is outside
                the policy-allowed area.
        """
        _ = state
        from tools.dispatch import dispatch_tool  # noqa: PLC0415

        rendered_path = _render_str(ctx, node_def.path, bound_inputs=bound_inputs)
        # Bytes pass through verbatim — Jinja rendering is for strings
        # only. The parser accepts ``str | bytes`` for forward-compat.
        if isinstance(node_def.content, bytes):
            # The underlying tool's args_schema only accepts ``str``;
            # the runner decodes for forward-compat. Writing genuine
            # binary content requires a future binary-write tool path.
            rendered_content: str = node_def.content.decode("utf-8")
        else:
            rendered_content = _render_str(
                ctx, node_def.content, bound_inputs=bound_inputs
            )

        tool_ctx = _build_tool_ctx(ctx)
        # Workflow-scoped `policy:` rules — see bug-workflow-policy-not-enforced.
        workflow_obj = getattr(ctx, "workflow", None)
        workflow_permissions = (
            list(workflow_obj.policy)
            if workflow_obj is not None and getattr(workflow_obj, "policy", None)
            else None
        )
        try:
            result = await dispatch_tool(
                defn=self._defn,
                args={
                    "path": rendered_path,
                    "content": rendered_content,
                },
                context=tool_ctx,
                authz=self._authz,
                secrets=self._secrets,
                sandbox=self._sandbox,
                workflow_permissions=workflow_permissions,
            )
        except FileAccessDenied as exc:
            raise WorkflowFileAccessDenied(
                path=rendered_path,
                node=node_name,
                reason=exc.detail.get("reason"),
                matched_rule=exc.detail.get("matched_rule"),
            ) from exc
        return {"bytes_written": int(result["bytes_written"])}


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
        # Workflow frames are attached in the node-chain step;
        # until then the node's call is unattributed.
        caller_chain=CallerChain.unattributed(),
        workspace_root=ctx.workspace_root,
        path_policy=None,
        tool_call_id=None,
    )
