"""Tool invocation dispatch — Module API Reference §9.1 + spec §6.2.

The dispatch order is EXACT:

1. Validate args against the tool's Pydantic ``args_schema``
   (:class:`ToolInvalidArguments` on failure — Diagnostic).
2. Authorize via ``authz.allow(principal, tool.action_name)``
   (:class:`ToolUnauthorized` on denial — Diagnostic).
3. Resolve declared secrets (:class:`SecretNotFound` is raised by the
   provider; the dispatch lets it propagate — this is a Fatal error
   the engine surfaces as ``thread.exchange_failed`` rather than as
   a per-tool Diagnostic, per Error-Model levels).
4. Wrap the work in ``sandbox.execute(work, policy)``
   (:class:`SandboxPolicyInvalid` on bad policy — Fatal).
5. Run the tool function. Non-PlatformError exceptions become
   :class:`ToolExecutionFailed` (Diagnostic) with the original
   exception preserved on ``__cause__`` for in-process debugging.

Spec conflict (flagged): the prose at spec §6.2 lists authorization
FIRST and validation SECOND. Module API Reference §9.1 — the
arbiter — lists validate FIRST then authz. The implementation
honours the arbiter; documented in the dispatch docstring.
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
    agent_permissions: list[Rule] | None = None,
    bundle_permissions: list[Rule] | None = None,
) -> Any:  # noqa: ANN401 — caller knows the return type for their tool
    """Run a tool through the validate → authz → secrets → sandbox →
    execute pipeline. Returns the tool function's raw return value.

    Caller (:class:`ToolsSubsystem.invoke_tool`) wraps the return in
    :class:`ToolResult`.

    ``agent_permissions`` / ``bundle_permissions`` are forwarded to
    ``authz.allow`` so the provider walks the four-level stack
    (qol-tool-permission-discovery-uniform-with-stacking.md). Both
    default to ``None`` so Phase 1 programmatic callers (tests,
    integration harnesses that construct ToolsSubsystem with their
    own minimal authz) stay byte-identical.
    """
    # Step 1 — Pydantic validate args.
    try:
        validated_args = defn.args_schema.model_validate(args)
    except PydanticValidationError as exc:
        raise ToolInvalidArguments(
            tool_name=defn.name,
            validation_errors=[dict(e) for e in exc.errors()],
        ) from exc

    # Step 2 — authorization. The action name is `tool.<name>` by
    # default per spec T-I1; tools can override via the decorator.
    #
    # Phase 2.1 (24d) — derive the target string from the validated
    # args so the prompt-effect path has something meaningful to
    # show in the modal (and to match against the
    # write-on-[a]-or-[d] suggested pattern). The provider's
    # matcher checks `target` first, then falls back to `action`,
    # so existing rules with action-namespace `what` patterns
    # still fire as before — see RuleBasedAuthorizationProvider.allow.
    target = _extract_target_from_args(validated_args)
    # Phase 3.0 Step 4 (Round 4 item #7) — assemble the layers tuple
    # in proximate-caller order. The current tool-dispatch wrapper
    # only sees agent + bundle layers; the ``workflow`` layer is
    # added by the workflow engine's tool-dispatch wrapper when it
    # ships in Phase 3.1.
    layers: tuple[PermissionLayer, ...] = ()
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
    # them populated. Per T-I3 — tools NEVER call secrets.resolve
    # themselves; the platform mediates.
    resolved_secrets = dict(context.secrets)
    for secret_name in defn.secrets_needed:
        # SecretNotFound (Fatal) propagates out of the dispatch. The
        # engine catches it at the exchange boundary and emits
        # `thread.exchange_failed` per Error-Model §1.3.
        resolved_secrets[secret_name] = await secrets.resolve(secret_name)
    invocation_context = context.model_copy(
        update={"secrets": resolved_secrets},
    )

    # Step 4 — wrap in the sandbox. Per T-I4, every invocation goes
    # through `sandbox.execute` even when it's the passthrough native
    # variant. Step 5 (the actual run) is inside the work closure.
    async def _work() -> Any:  # noqa: ANN401
        return await defn.func(validated_args, invocation_context)

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

Phase 2.0's spec §2.3 says ``what`` "matches the tool's target
string. For file tools the target is the resolved absolute path.
For ``bash`` it's the command string." This list captures the
convention: file tools use ``path``, shell tools use ``command``,
network tools (future) use ``url``. First-hit-wins so tools that
declare both a path and a command (none today, but possible
later) pick a stable one.
"""


def _extract_target_from_args(validated_args: object) -> str | None:
    """Return the conventional target string from a tool's args, if any.

    Tools whose declared args lack ``path``/``command``/``url``
    return None — the authz provider's matcher then falls back to
    matching ``what`` against ``action`` (the Phase-1-compat
    behaviour), so this is safe to call for every tool.
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
    """Placeholder seam — exists so tests that want to substitute the
    authz invocation specifically can monkeypatch this module rather
    than the provider. Currently a no-op pass-through; kept reserved.
    """

    async def _call(principal: str, action: str) -> Any:  # noqa: ANN401
        return await authz.allow(principal, action)

    return _call
