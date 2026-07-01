"""`kolsch/nodes/browse_url` runner — dispatches through the tool pipeline.

Routes through
[`dispatch_tool`][tools.dispatch.dispatch_tool] against the
`kolsch/tools/browse_url` tool definition so the workflow node inherits
the platform's authz + secrets + sandbox machinery with no copying.
Mirrors [`BashRunner`][workflows.builtins.bash.BashRunner]'s shape and
uses [`ReadFileRunner`][workflows.builtins.read_file.ReadFileRunner]'s
`tool_defn=` kwarg convention. Jinja rendering of `url`, `format`,
`wait_selector`, and the per-value strings inside `headers` / `cookies`
happens inside `run` against the same `inputs.*` / `state.*` scope the
executor uses for `binds:`; header/cookie **keys pass through as
literals** so a Jinja-generated string cannot smuggle unintended header
names past review.

The runner narrows the browse_url tool's nine-field return shape to the
six canonical workflow fields `{url, final_url, status, content,
content_persisted_to, truncated}`. `format`, `content_length`, and
`duration_seconds` are dropped — authors who need bytes-counting compute
from `content`; timing is a workflow-executor concern, not a payload one.

`ToolUnauthorized` from dispatch re-raises as
[`WorkflowNodeFailed`][workflows.errors.WorkflowNodeFailed] preserving
the original error as `__cause__`; `ToolTimeout` and `ExchangeCancelled`
propagate so the executor's existing handling turns them into
`workflow.node.failed` events.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from errors import ToolUnauthorized
from schemas.identifiers import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext
from workflows.errors import WorkflowNodeFailed
from workflows.jinja_env import JINJA_ENV

if TYPE_CHECKING:
    from providers.authz import AuthorizationProvider
    from providers.sandbox import SandboxProvider
    from providers.secrets import SecretsProvider
    from tools.decorator import ToolDefinition

__all__ = ["BrowseUrlRunner"]


# Mirror BashRunner's synthetic identity. `ag_` is user-named (no UUID
# requirement); `ses_` / `th_` MUST be UUID4-backed per `workflows.ids`
# — minted fresh per `BrowseUrlRunner.run` so two concurrent workflow
# dispatches never collide on the identity surface.
_SYNTHETIC_AGENT = AgentId("ag_workflow_executor")


class BrowseUrlRunner:
    """Runner for `kolsch/nodes/browse_url` — drives the browse_url tool.

    Provider-injected at construction; the constructor stores the
    browse_url [`ToolDefinition`][tools.decorator.ToolDefinition] along
    with the three platform providers. Each invocation builds a
    synthetic [`ToolContext`][tools.decorator.ToolContext], renders the
    node's `url` / `format` / `wait_selector` / per-value `headers` /
    per-value `cookies` against the workflow scope, and invokes
    [`dispatch_tool`][tools.dispatch.dispatch_tool] so the existing
    validate → authz → secrets → sandbox → execute pipeline runs
    end-to-end.
    """

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
        node_def: Any,  # noqa: ANN401 — BrowseUrlNodeDef
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,  # noqa: ANN401 — NodeContext
    ) -> dict[str, Any]:
        """Render the node's config and dispatch the browse_url tool.

        Returns the narrowed six-field capture. Tool-side authorization
        failures re-raise as `WorkflowNodeFailed` with the underlying
        `ToolUnauthorized` preserved as `__cause__`; `ToolTimeout` and
        `ExchangeCancelled` propagate unchanged.
        """
        from tools.dispatch import dispatch_tool  # noqa: PLC0415

        # Mirror bash.py's scope composition: workflow-level inputs
        # overlay'd with this node's edge-bound `bound_inputs` (edge
        # wins on conflict), so a browse_url node and an authored
        # Python @node see the SAME `inputs.*` namespace at any given
        # receiver.
        scope = {
            "inputs": {
                **dict(getattr(ctx, "workflow_inputs", {}) or {}),
                **dict(bound_inputs),
            },
            "state": dict(state),
        }
        rendered_url = _render(node_def.url, scope)
        rendered_format = _render(node_def.format, scope)
        rendered_selector = _render_optional(
            node_def.wait_selector, scope
        )
        rendered_headers = _render_mapping(
            node_def.headers, scope
        )
        rendered_cookies = _render_mapping(
            node_def.cookies, scope
        )

        args: dict[str, Any] = {
            "url": rendered_url,
            "format": rendered_format,
            "request_timeout_seconds": node_def.request_timeout_seconds,
        }
        if rendered_selector is not None:
            args["wait_selector"] = rendered_selector
        if rendered_headers is not None:
            args["headers"] = rendered_headers
        if rendered_cookies is not None:
            args["cookies"] = rendered_cookies

        tool_ctx = ToolContext(
            agent_id=_SYNTHETIC_AGENT,
            session_id=SessionId(f"ses_{uuid.uuid4()}"),
            thread_id=ThreadId(f"th_{uuid.uuid4()}"),
            principal=f"workflow.{ctx.workflow_run_id}",
            secrets={},
            workspace_root=ctx.workspace_root,
            path_policy=None,
            tool_call_id=None,
        )

        try:
            raw = await dispatch_tool(
                defn=self._defn,
                args=args,
                context=tool_ctx,
                authz=self._authz,
                secrets=self._secrets,
                sandbox=self._sandbox,
            )
        except ToolUnauthorized as exc:
            raise WorkflowNodeFailed(
                node=node_name,
                invocation_id=str(ctx.invocation_id),
                cause=exc,
            ) from exc

        # Return-shape narrowing. The tool returns nine fields
        # `{url, final_url, status, content, content_persisted_to,
        # content_length, format, duration_seconds, truncated}`; the
        # node captures the six canonical ones — the shape downstream
        # workflow authors bind against via `{{ nodes.<n>.<field> }}`.
        # `format`, `content_length`, `duration_seconds` are dropped
        # (`content_length` is derivable from `content`, timing is an
        # executor concern). Keeping BOTH `url` and `final_url` lets
        # authors detect redirects; keeping `truncated` disambiguates
        # `content_persisted_to is None` (small page vs persistence
        # failed).
        return {
            "url": str(raw.get("url", "")),
            "final_url": str(raw.get("final_url", raw.get("url", ""))),
            "status": int(raw.get("status", 0)),
            "content": str(raw.get("content", "")),
            "content_persisted_to": raw.get("content_persisted_to"),
            "truncated": bool(raw.get("truncated", False)),
        }


def _render(value: str, scope: dict[str, Any]) -> str:
    """Render a Jinja-marked string; pass plain strings through verbatim."""
    if "{{" not in value:
        return value
    return JINJA_ENV.from_string(value).render(**scope)


def _render_optional(
    value: str | None, scope: dict[str, Any]
) -> str | None:
    if value is None:
        return None
    return _render(value, scope)


def _render_mapping(
    value: dict[str, str] | None, scope: dict[str, Any]
) -> dict[str, str] | None:
    """Render each VALUE of a header/cookie mapping; keys pass through.

    Keys are not rendered by design — allowing dynamic header/cookie
    names would let a Jinja-generated string smuggle unintended
    headers past review. The spec calls this out explicitly.
    """
    if value is None:
        return None
    return {k: _render(str(v), scope) for k, v in value.items()}
