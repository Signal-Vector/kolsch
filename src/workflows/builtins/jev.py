"""`kolsch/nodes/jev` runner — a typed Jev decision through the tool pipeline.

spec-136 M3. Routes through
[`dispatch_tool`][tools.dispatch.dispatch_tool] against the
`kolsch/tools/jev_decide` tool definition, so the node inherits the
platform's authz + secrets + sandbox machinery and the tool's own
argument validation, retry and error mapping — the runner never talks
to the network itself. Mirrors
[`BrowseUrlRunner`][workflows.builtins.browse_url.BrowseUrlRunner]'s
shape and synthetic identity.

`state` is Jinja-rendered at every depth (strings inside objects and
arrays too) against the same `inputs.*` / `state.*` scope the executor
uses for `binds:`. `questions` and `model` pass through literally.

Captured output: `{answers, model, usage}` plus `value` when
`route_on` is set — the chosen option name (str) for a `choice`
question, `noul >= noul_threshold` (bool) for a `noul` question. The
executor's [`compute_branch_value`][workflows.branching.compute_branch_value]
reads `value` for edge dispatch.

Failures that the author or operator must act on re-raise as
[`WorkflowNodeFailed`][workflows.errors.WorkflowNodeFailed] with the
original as `__cause__` and a `nested_error` (`{type, message,
event_id, detail?, recovery_hint?}`) the executor copies onto the
`workflow.node.failed` payload:

- `tool.jev_below_threshold` — a `fail_below` was breached; `detail`
  carries the failing questions.
- `tool.unauthorized` — authz denied the call.
- `secret.not_found` — `OPENROUTER_API_KEY` is missing (the feat-139
  hiding applies to agent tool lists, not to an explicit workflow
  node); `recovery_hint` is "Run `kolsch connect openrouter`."
- `tool.execution_failed` / `tool.invalid_arguments` — upstream or
  argument errors, with the tool's own `recovery_hint` when it set one.

`ToolTimeout` and `ExchangeCancelled` propagate unchanged, as for
browse_url.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from errors import (
    JevBelowThreshold,
    PlatformError,
    SecretNotFound,
    ToolExecutionFailed,
    ToolInvalidArguments,
    ToolUnauthorized,
)
from schemas.identifiers import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext
from workflows.errors import WorkflowNodeFailed
from workflows.jinja_env import JINJA_ENV

if TYPE_CHECKING:
    from providers.authz import AuthorizationProvider
    from providers.sandbox import SandboxProvider
    from providers.secrets import SecretsProvider
    from tools.decorator import ToolDefinition

__all__ = ["JevRunner"]


_SYNTHETIC_AGENT = AgentId("ag_workflow_executor")
_CONNECT_HINT = "Run `kolsch connect openrouter`."
_WRAPPED_ERRORS: tuple[type[PlatformError], ...] = (
    JevBelowThreshold,
    ToolUnauthorized,
    SecretNotFound,
    ToolExecutionFailed,
    ToolInvalidArguments,
)


class JevRunner:
    """Runner for `kolsch/nodes/jev` — drives the `jev_decide` tool."""

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
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,
    ) -> dict[str, Any]:
        """Render `state`, dispatch `jev_decide`, capture the typed answer."""
        from tools.dispatch import dispatch_tool  # noqa: PLC0415

        scope = {
            "inputs": {
                **dict(getattr(ctx, "workflow_inputs", {}) or {}),
                **dict(bound_inputs),
            },
            "state": dict(state),
        }
        args: dict[str, Any] = {
            "state": _render_deep(node_def.state, scope),
            "questions": node_def.questions,
            "model": node_def.model,
        }
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
        workflow_obj = getattr(ctx, "workflow", None)
        workflow_permissions = (
            list(workflow_obj.policy)
            if workflow_obj is not None and getattr(workflow_obj, "policy", None)
            else None
        )
        try:
            raw = await dispatch_tool(
                defn=self._defn,
                args=args,
                context=tool_ctx,
                authz=self._authz,
                secrets=self._secrets,
                sandbox=self._sandbox,
                workflow_permissions=workflow_permissions,
            )
        except _WRAPPED_ERRORS as exc:
            raise WorkflowNodeFailed(
                node=node_name,
                invocation_id=str(ctx.invocation_id),
                cause=exc,
                nested_error=_nested_error(exc),
            ) from exc

        captured: dict[str, Any] = {
            "answers": raw["answers"],
            "model": raw["model"],
            "usage": raw["usage"],
        }
        route_on = node_def.route_on
        if route_on is not None:
            answer = raw["answers"][route_on]
            if answer.get("type") == "choice":
                captured["value"] = str(answer["choice"])
            else:
                captured["value"] = float(answer["noul"]) >= node_def.noul_threshold
        return captured


def _nested_error(exc: PlatformError) -> dict[str, Any]:
    """The `workflow.node.failed` `nested_error` block for a wrapped error."""
    nested: dict[str, Any] = {
        "type": exc.code,
        "message": str(exc),
        "event_id": None,
    }
    if isinstance(exc, JevBelowThreshold):
        nested["detail"] = {"failures": list(exc.failures)}
    hint = getattr(exc, "recovery_hint", None)
    if isinstance(exc, SecretNotFound):
        hint = _CONNECT_HINT
    if isinstance(hint, str) and hint:
        nested["recovery_hint"] = hint
    return nested


def _render_deep(value: Any, scope: dict[str, Any]) -> Any:
    """Render every Jinja-marked string inside `value`, at any depth.

    Dict keys pass through as literals; non-string scalars are untouched.
    """
    if isinstance(value, str):
        if "{{" not in value and "{%" not in value:
            return value
        return JINJA_ENV.from_string(value).render(**scope)
    if isinstance(value, dict):
        return {k: _render_deep(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_deep(v, scope) for v in value]
    return value
