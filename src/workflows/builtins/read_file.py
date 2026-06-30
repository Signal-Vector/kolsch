"""`kolsch/nodes/read_file` runner — narrows the tool output to `content`.

Routes through
[`dispatch_tool`][tools.dispatch.dispatch_tool] against a
[`ToolDefinition`][tools.decorator.ToolDefinition] for `read_file`
(built by
[`make_read_file`][tools.builtins.make_read_file] in the CLI
bootstrap — the factory closes over `policy` and `root` so the
tool function checks file access without `ToolContext` needing to
carry the policy, matching the bash runner's wiring).

Narrows the tool's richer return shape (`content` /
`lines_returned` / `total_lines` / `next_offset` / `truncated`) to
`{content}` — the canonical workflow output contract. The runner
threads a large `limit` so the workflow node's
`{{ nodes.<n>.content }}` reflects the entire file rather than
silently truncating at the tool's default 2000-line pagination
cap; pagination as a workflow-author surface is a future QoL.

`FileAccessDenied` raised by the tool's path policy is remapped to
[`WorkflowFileAccessDenied`][workflows.errors.WorkflowFileAccessDenied]
with the rendered path + node name; `__cause__` preserves the
original platform error for in-process debugging.
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
    from workflows.parser import ReadFileNodeDef


__all__ = ["ReadFileRunner"]


# Mirror BashRunner's synthetic identity. ``ag_`` is user-named (no
# UUID requirement); ``ses_`` / ``th_`` MUST be UUID4-backed so two
# concurrent file-IO dispatches never collide on the identity surface.
_SYNTHETIC_AGENT = AgentId("ag_workflow_executor")

# Effective full-file read — keeps the runner from silently
# truncating at the tool's default 2000-line cap. The tool walks
# every line in memory anyway (single-line cap is the real refusal
# point); raising the line limit is cheap.
_FULL_CONTENT_LINE_LIMIT = 10_000_000


class ReadFileRunner:
    """Runner for `kolsch/nodes/read_file` — captures `{content}` only."""

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
        node_def: ReadFileNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        """Render the path and dispatch `read_file`; capture `{content}`.

        Raises:
            WorkflowFileAccessDenied: The rendered path is outside
                the policy-allowed area.
        """
        _ = state
        from tools.dispatch import dispatch_tool  # noqa: PLC0415

        rendered_path = _render(
            ctx, node_def.path, bound_inputs=bound_inputs
        )
        tool_ctx = _build_tool_ctx(ctx)
        try:
            result = await dispatch_tool(
                defn=self._defn,
                args={
                    "path": rendered_path,
                    "limit": _FULL_CONTENT_LINE_LIMIT,
                },
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
        return {"content": result["content"]}


def _render(
    ctx: NodeContext,
    value: str,
    *,
    bound_inputs: dict[str, Any],
) -> str:
    """Render a Jinja-marked string via the executor's renderer.

    Plain strings (no `{{`) pass through verbatim — matches the
    bash runner's pattern so the file-IO runners' config surface
    behaves identically.
    """
    if "{{" not in value:
        return value
    return str(ctx.render_jinja(value, inputs_overlay=bound_inputs))


def _build_tool_ctx(ctx: NodeContext) -> ToolContext:
    """Construct the synthetic `ToolContext` for tool dispatch.

    Mirrors the bash runner's variant: the in-engine `make_*`
    factory closes over `policy` and `root`, so `path_policy` stays
    `None` on the context and the tool function reaches its own
    copy via the closure.
    """
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
