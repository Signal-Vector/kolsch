"""`kolsch/nodes/bash` runner — dispatches through the tool pipeline.

Routes through
[`dispatch_tool`][tools.dispatch.dispatch_tool] against the
`kolsch/bash` tool definition so the workflow node inherits the
platform's file_access + sandbox + authz machinery with no copying.
Provider-injected at construction; the CLI wire-up resolves the
bash tool from the workspace's `bundles/kolsch/tools/bash.py` via
`importlib.util` and forwards the platform's already-bootstrapped
providers. Jinja rendering of `command:` / `cwd:` / `env:` happens
inside `run` against the same `inputs.*` / `state.*` scope the
executor uses for `binds:`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from errors import ToolUnauthorized
from schemas.identifiers import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext
from workflows.errors import BashNonZeroExit, WorkflowNodeFailed
from workflows.jinja_env import JINJA_ENV

if TYPE_CHECKING:
    from providers.authz import AuthorizationProvider
    from providers.sandbox import SandboxProvider
    from providers.secrets import SecretsProvider
    from tools.decorator import ToolDefinition

__all__ = ["BashRunner"]


# `ToolContext` requires an `agent_id`; the bash tool itself
# ignores it. `ag_` ids are user-named (not UUID-backed), so we
# carry a stable synthetic agent id naming the workflow-executor
# dispatcher. `ses_` and `th_` MUST be UUID4-backed per
# `workflows.ids` — minted fresh per `BashRunner.run` so two
# concurrent workflow bash dispatches never collide on the
# identity surface (future versions may swap these for real
# per-run / per-sub-workflow ids).
_SYNTHETIC_AGENT = AgentId("ag_workflow_executor")


class BashRunner:
    """Runner for `kolsch/nodes/bash` — drives the bash tool dispatch.

    Provider-injected at construction; the constructor stores the
    bash [`ToolDefinition`][tools.decorator.ToolDefinition] along
    with the three platform providers. Each invocation builds a
    synthetic [`ToolContext`][tools.decorator.ToolContext], renders
    the bash node's `command:` / `cwd:` / `env:` against the
    workflow scope, and invokes
    [`dispatch_tool`][tools.dispatch.dispatch_tool] so the existing
    validate → authz → secrets → sandbox → execute pipeline runs
    end-to-end.
    """

    def __init__(
        self,
        *,
        bash_defn: ToolDefinition,
        authz: AuthorizationProvider,
        secrets: SecretsProvider,
        sandbox: SandboxProvider,
    ) -> None:
        self._defn = bash_defn
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
        """Render the node's config and dispatch the bash tool.

        Returns the narrowed `{stdout, stderr, exit_code}` capture.
        A non-zero exit raises `BashNonZeroExit` so the executor's
        branching path can route on the failure when the node has
        outgoing `branch:` edges; tool-side authorization failures
        re-raise as `WorkflowNodeFailed` with the underlying
        `ToolUnauthorized` preserved as `__cause__`.
        """
        from tools.dispatch import dispatch_tool  # noqa: PLC0415

        # Per-node Jinja rendering of the bash config. The executor
        # already rendered the inbound edge binds; this runner now
        # renders the node's own command + cwd + env fields against
        # the same scope (inputs / state).
        #
        # `inputs.*` is the workflow-level inputs mapping overlay'd
        # with this node's edge-bound `bound_inputs` (edge wins on
        # conflict), matching the executor's
        # `_render_jinja(..., inputs_overlay=bound_inputs)`
        # composition so a bash command and an authored Python @node
        # see the SAME `inputs.*` namespace at any given receiver.
        scope = {
            "inputs": {
                **dict(getattr(ctx, "workflow_inputs", {}) or {}),
                **dict(bound_inputs),
            },
            "state": dict(state),
        }
        rendered_command = _render(node_def.command, scope)
        rendered_cwd = _render_optional(getattr(node_def, "cwd", None), scope)
        rendered_env = _render_env(getattr(node_def, "env", None), scope)

        args: dict[str, Any] = {"command": rendered_command}
        if rendered_cwd is not None:
            args["cwd"] = rendered_cwd
        # The bash tool's args_schema doesn't carry an ``env:`` field at
        # v1; rendered env vars are dropped with a clear log. Future
        # work threads the env mapping through the dispatch pipeline.
        _ = rendered_env

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

        # Feed the workflow's own YAML `policy:` rules into dispatch
        # as the outermost layer so workflow-scoped authz rules take
        # effect at run time — see bug-workflow-policy-not-enforced.
        # `ctx.workflow` is stamped by the executor; the `getattr`
        # keeps unit tests that construct bare NodeContexts working.
        workflow_obj = getattr(ctx, "workflow", None)
        workflow_permissions = (
            list(workflow_obj.policy)
            if workflow_obj is not None and getattr(workflow_obj, "policy", None)
            else None
        )

        try:
            result = await dispatch_tool(
                defn=self._defn,
                args=args,
                context=tool_ctx,
                authz=self._authz,
                secrets=self._secrets,
                sandbox=self._sandbox,
                workflow_permissions=workflow_permissions,
            )
        except ToolUnauthorized as exc:
            raise WorkflowNodeFailed(
                node=node_name,
                invocation_id=str(ctx.invocation_id),
                cause=exc,
            ) from exc

        # Bash tool returns a dict with stdout / stderr / exit_code +
        # additional persistence + duration fields. The runner
        # narrows the captured output to the three canonical fields;
        # the persistence / duration / timed_out fields are dropped
        # so downstream nodes see a stable contract. Future versions
        # may surface the extras under a separate namespace.
        captured = {
            "stdout": str(result.get("stdout", "")),
            "stderr": str(result.get("stderr", "")),
            "exit_code": int(result.get("exit_code", 0)),
        }
        # Non-zero exits are NEVER silently swallowed. The runner
        # raises `BashNonZeroExit` carrying the captured dict so the
        # executor can decide whether to (a) surface success of the
        # false branch (when the node has at least one `branch:`
        # outgoing edge) or (b) abort the run via the existing
        # failure path (when the node has no `branch:` edges).
        if captured["exit_code"] != 0:
            raise BashNonZeroExit(node=node_name, captured=captured)
        return captured


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


def _render_env(
    value: dict[str, Any] | None, scope: dict[str, Any]
) -> dict[str, str] | None:
    if value is None:
        return None
    return {
        k: _render(str(v), scope) if isinstance(v, str) else str(v)
        for k, v in value.items()
    }
