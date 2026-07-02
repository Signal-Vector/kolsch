"""Tool invocation dispatch.

The dispatch order is exact:

1. Validate args against the tool's Pydantic `args_schema`
   ([`ToolInvalidArguments`][errors.ToolInvalidArguments] on failure —
   Diagnostic).
2. Authorize via `authz.allow(principal, tool.action_name)`
   ([`ToolUnauthorized`][errors.ToolUnauthorized] on denial —
   Diagnostic).
3. Resolve declared secrets. `SecretNotFound` is raised by the
   provider; the dispatch lets it propagate — Fatal, surfaced by the
   engine as `thread.exchange_failed` rather than a per-tool
   Diagnostic.
4. Wrap the work in `sandbox.execute(work, policy)`
   (`SandboxPolicyInvalid` on bad policy — Fatal).
5. Run the tool function. Non-`PlatformError` exceptions become
   [`ToolExecutionFailed`][errors.ToolExecutionFailed] (Diagnostic)
   with the original exception preserved on `__cause__` for
   in-process debugging.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as PydanticValidationError

from errors import (
    PlatformError,
    ToolExecutionFailed,
    ToolInvalidArguments,
    ToolUnauthorized,
)
from providers.authz import PermissionLayer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from providers import (
        AuthorizationProvider,
        SandboxProvider,
        SecretsProvider,
    )
    from providers.authz import Rule
    from tools.decorator import ToolContext, ToolDefinition


async def dispatch_tool(
    *,
    defn: ToolDefinition,
    args: dict[str, Any],
    context: ToolContext,
    authz: AuthorizationProvider,
    secrets: SecretsProvider,
    sandbox: SandboxProvider,
    workflow_permissions: list[Rule] | None = None,
    agent_permissions: list[Rule] | None = None,
    bundle_permissions: list[Rule] | None = None,
) -> Any:  # noqa: ANN401 — caller knows the return type for their tool
    """Run a tool through the full dispatch pipeline.

    Walks the validate → authz → secrets → sandbox → execute order
    and returns the tool function's raw return value. The caller
    ([`ToolsSubsystem.invoke_tool`][tools.subsystem.ToolsSubsystem.invoke_tool])
    wraps the return in [`ToolResult`][tools.decorator.ToolResult].

    `workflow_permissions`, `agent_permissions`, and
    `bundle_permissions` are forwarded to `authz.allow` so the
    provider walks the full stack (workflow → agent → bundle →
    platform → default). All three default to `None` so programmatic
    callers (tests, integration harnesses that construct
    `ToolsSubsystem` with their own minimal authz) stay
    byte-identical.

    Args:
        defn: The registered tool's definition.
        args: Raw argument dict from the framework; validated against
            `defn.args_schema` before anything else runs.
        context: The runtime context handed to the tool body.
        authz: Authorization provider — consulted with the composed
            principal and `defn.action_name`.
        secrets: Secrets provider — resolves names listed in
            `defn.secrets_needed`.
        sandbox: Sandbox provider — wraps the tool body.
        workflow_permissions: The workflow's ``policy:`` rules when the
            proximate caller frame is a workflow node. Kolsch's
            ``AuthorizationProvider.allow`` docstring
            (``src/providers/authz.py:813-822``) already specifies
            this layer as FIRST in the tuple; before this parameter
            existed the workflow layer was silently dropped and the
            workflow YAML's ``policy:`` field validated at parse time
            but never enforced at dispatch — see
            ``bug-workflow-policy-not-enforced.md``.
        agent_permissions: The agent's resolved permission rules.
        bundle_permissions: The bundle's resolved permission rules.

    Returns:
        Whatever the tool function returned.

    Raises:
        ToolInvalidArguments: Pydantic validation of `args` failed.
        ToolUnauthorized: Authz denied the call.
        ToolExecutionFailed: The tool body raised a non-`PlatformError`.
    """
    # Step 1 — Pydantic validate args.
    try:
        validated_args = defn.args_schema.model_validate(args)
    except PydanticValidationError as exc:
        raise ToolInvalidArguments(
            tool_name=defn.name,
            validation_errors=[dict(e) for e in exc.errors()],
        ) from exc

    # Step 2 — authorization. The default authz action string is
    # derived by the tool's `action_name`; tools can override via the
    # decorator.
    #
    # Derive the target string from the validated args so the
    # prompt-effect path has something meaningful to show in the
    # modal (and to match against the write-on-[a]-or-[d] suggested
    # pattern). The provider's matcher checks `target` first, then
    # falls back to `action`, so existing rules with action-namespace
    # `what` patterns still fire as before — see
    # RuleBasedAuthorizationProvider.allow.
    target = _extract_target_from_args(validated_args)
    # Assemble the layers tuple in proximate-caller order:
    # workflow → agent → bundle. The provider's docstring
    # (src/providers/authz.py:813-822) specifies workflow FIRST when
    # the proximate caller frame is a workflow node firing the
    # action. Workflow builtin runners populate `workflow_permissions`
    # from `ctx.workflow.policy` so a workflow's own YAML `policy:`
    # rules match at dispatch. When the workflow invoked an agent
    # which is now calling a tool, callers omit `workflow_permissions`
    # (structural escalation prevention — see the docstring).
    layers: tuple[PermissionLayer, ...] = ()
    if workflow_permissions:
        layers = (
            *layers,
            PermissionLayer(name="workflow", rules=tuple(workflow_permissions)),
        )
    if agent_permissions:
        layers = (
            *layers,
            PermissionLayer(name="agent", rules=tuple(agent_permissions)),
        )
    if bundle_permissions:
        layers = (
            *layers,
            PermissionLayer(name="bundle", rules=tuple(bundle_permissions)),
        )
    auth = await authz.allow(
        context.principal,
        defn.action_name,
        target=target,
        layers=layers,
    )
    if not auth.allowed:
        raise ToolUnauthorized(
            tool_name=defn.name,
            action=defn.action_name,
            principal=context.principal,
            matched_rule=(
                auth.matched_rule.model_dump() if auth.matched_rule else None
            ),
        )

    # Step 3 — resolve declared secrets and rebuild the context with
    # them populated. Tools never call secrets.resolve themselves;
    # the platform mediates.
    resolved_secrets = dict(context.secrets)
    for secret_name in defn.secrets_needed:
        # SecretNotFound (Fatal) propagates out of the dispatch. The
        # engine catches it at the exchange boundary and emits
        # `thread.exchange_failed`.
        resolved_secrets[secret_name] = await secrets.resolve(secret_name)
    invocation_context = context.model_copy(
        update={"secrets": resolved_secrets},
    )

    # Step 4 — wrap in the sandbox. Every invocation goes through
    # `sandbox.execute` even when it's the passthrough native
    # variant. Step 5 (the actual run) is inside the work closure.
    #
    # Construct the PermissionsBinding from the same provider +
    # layers the dispatch-boundary check used, then bind it to the
    # ``tools.permissions._BINDING`` ContextVar around the tool
    # function body. The bind/reset pair lands INSIDE ``_work`` so
    # the binding lifetime exactly matches the tool body — sandbox
    # setup logic doesn't call into ``permissions.check`` and
    # shouldn't see the binding leak across calls.
    from tools import permissions as _tool_permissions  # noqa: PLC0415

    binding = _tool_permissions.PermissionsBinding(
        provider=authz,
        agent_permissions=list(agent_permissions or []),
        bundle_permissions=list(bundle_permissions or []),
        principal=context.principal,
        tool=defn.name,
    )

    async def _work() -> Any:  # noqa: ANN401
        token = _tool_permissions.bind(binding)
        try:
            return await defn.func(validated_args, invocation_context)
        finally:
            _tool_permissions.reset(token)

    try:
        return await sandbox.execute(_work, policy={})
    except PlatformError:
        # Already typed — propagate as-is (e.g., FileAccessDenied from
        # a builtin tool's policy check). The engine emits the right
        # event from the typed error.
        raise
    except Exception as exc:
        raise ToolExecutionFailed(
            tool_name=defn.name,
            error_class=type(exc).__name__,
        ) from exc


_TARGET_ARG_PRIORITY = ("path", "command", "url")
"""Argument names dispatch_tool looks at to derive an authz target.

The convention: file tools use ``path``, shell tools use ``command``,
network tools (future) use ``url``. The authz rule's ``what``
matches against the tool's target string — the resolved path for
file tools, the command for ``bash``. First-hit-wins so tools that
declare both a path and a command (none today, but possible later)
pick a stable one.
"""


def _extract_target_from_args(validated_args: object) -> str | None:
    """Return the conventional target string from a tool's args, if any.

    Tools whose declared args lack ``path``/``command``/``url``
    return None — the authz provider's matcher then falls back to
    matching ``what`` against ``action``, so this is safe to call
    for every tool.
    """
    for attr in _TARGET_ARG_PRIORITY:
        value = getattr(validated_args, attr, None)
        if isinstance(value, str):
            return value
    return None


def authz_call(
    *,
    authz: AuthorizationProvider,
) -> Callable[..., Awaitable[Any]]:
    """Return a thin authz invocation seam tests can monkeypatch.

    A reserved no-op pass-through. Exists so tests that want to
    substitute the authz invocation specifically can patch this
    module rather than the provider.
    """

    async def _call(principal: str, action: str) -> Any:  # noqa: ANN401
        return await authz.allow(principal, action)

    return _call
