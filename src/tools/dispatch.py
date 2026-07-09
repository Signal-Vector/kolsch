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

import asyncio
import contextlib
import contextvars
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

    from cli.chat.service_registry import ChatServiceRegistry
    from providers import (
        AuthorizationProvider,
        SandboxProvider,
        SecretsProvider,
    )
    from providers.authz import Rule
    from tools.decorator import ToolContext, ToolDefinition
    from tools.services import ServiceName
    from tools.services.cards import Card


# ---------------------------------------------------------------------------
# spec-008 — auto-close finalizer contextvar
# ---------------------------------------------------------------------------
#
# Cards opened via ``Cards.open`` during a tool invocation register
# themselves on the current tool's ``open_cards`` list via this
# ContextVar (``CardsImpl.open`` peeks + appends). The finalizer wraps
# the tool body in ``_invoke_tool`` and closes every non-transferred
# card on the exit path. Outside a tool call the var is ``None`` and
# ``CardsImpl.open`` skips the append (workflow-invoked cards, tests).
_current_tool_open_cards: contextvars.ContextVar[list["Card"] | None] = (
    contextvars.ContextVar("_current_tool_open_cards", default=None)
)


async def _invoke_tool(
    tool_func: Callable[..., Awaitable[Any]],
    validated_args: Any,  # noqa: ANN401
    ctx: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Run the tool body with the spec-008 auto-close finalizer.

    Tracks cards opened during the call via ``_current_tool_open_cards``
    (a ``contextvars.ContextVar`` scoped per-invocation). On the exit
    path, closes each non-transferred, still-open card with a
    ``final_status`` derived from the exit kind:

    - Normal return → ``"success"``.
    - ``asyncio.CancelledError`` → ``"cancelled"``; re-raised.
    - Other exception → ``"error"`` with ``error_message=str(exc)[:200]``;
      re-raised.

    Cards with ``_owner_transferred=True`` are SKIPPED in all three
    exit paths — the workflow service (spec-017) owns their terminal
    transition. The contextvar is reset via ``.reset(token)`` in the
    outer ``finally`` so nested tool calls (tool A → tool B) each get
    their own tracking list without leaking across the boundary.
    """
    open_cards: list[Card] = []
    token = _current_tool_open_cards.set(open_cards)
    try:
        try:
            result = await tool_func(validated_args, ctx)
        except asyncio.CancelledError:
            for card in open_cards:
                if getattr(card, "_owner_transferred", False):
                    continue
                if getattr(card, "is_closed", False):
                    continue
                with contextlib.suppress(Exception):
                    await card.close(final_status="cancelled")
            raise
        except Exception as exc:
            # Truncate the error message HERE (finalizer) — spec AC #4.
            error_message = str(exc)[:200]
            for card in open_cards:
                if getattr(card, "_owner_transferred", False):
                    continue
                if getattr(card, "is_closed", False):
                    continue
                with contextlib.suppress(Exception):
                    await card.close(
                        final_status="error",
                        error_message=error_message,
                    )
            raise
        else:
            # Normal return — close remaining cards as success.
            for card in open_cards:
                if getattr(card, "_owner_transferred", False):
                    continue
                if getattr(card, "is_closed", False):
                    continue
                with contextlib.suppress(Exception):
                    await card.close(final_status="success")
            return result
    finally:
        _current_tool_open_cards.reset(token)


def _populate_service_fields(
    ctx: ToolContext,
    requires: list[ServiceName],
    chat_services: ChatServiceRegistry | None,
) -> ToolContext:
    """Return a new `ToolContext` with the declared service fields populated.

    `ToolContext` is a frozen Pydantic model, so we cannot mutate the
    incoming instance — we call `model_copy(update=...)` to produce a
    fresh frozen instance with the four service fields set from the
    supplied `ChatServiceRegistry`. In headless mode
    (`chat_services=None`), the original context is returned unchanged
    and all four service fields stay `None`.

    Only the fields listed in `requires` are pulled off the registry;
    undeclared caps stay `None` on the resulting context. See spec-001
    §Dispatcher wiring.
    """
    if chat_services is None:
        return ctx
    if not requires:
        return ctx
    updates: dict[str, Any] = {name: getattr(chat_services, name) for name in requires}
    return ctx.model_copy(update=updates)


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
    chat_services: ChatServiceRegistry | None = None,
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
        chat_services: Optional
            [`ChatServiceRegistry`][cli.chat.service_registry.ChatServiceRegistry]
            sidecar the chat runtime passes so tools declaring
            ``requires=[...]`` see the corresponding service handles
            on `ToolContext`. Headless callers (tests, workflow
            runners, one-shot CLI) omit it — every service field
            on the resulting `ToolContext` stays `None` and
            `require_services(ctx, [...])` raises `ToolNotAvailable`
            for any tool that asked for one.

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

    # spec-001 — populate the four capability-scoped service fields on
    # `ToolContext` from the `ChatServiceRegistry` sidecar. Only the
    # services listed in `defn.requires` get populated; undeclared caps
    # stay `None`. In headless mode (`chat_services=None`), the helper
    # returns the context unchanged and all four fields remain `None`.
    invocation_context = _populate_service_fields(
        invocation_context,
        defn.requires,
        chat_services,
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
            # spec-008 — wrap the tool body with the auto-close
            # finalizer. Runs INSIDE the sandbox work closure so the
            # existing `ToolExecutionFailed` translation below still
            # sees any exception the tool raises (the finalizer
            # re-raises), and so the permissions binding stays live
            # throughout the finalizer's card-close writes.
            return await _invoke_tool(
                defn.func,
                validated_args,
                invocation_context,
            )
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


_TARGET_ARG_PRIORITY = ("path", "command", "url", "ref", "run_id")
"""Argument names dispatch_tool looks at to derive an authz target.

The convention: file tools use ``path``, shell tools use ``command``,
network tools use ``url``. bug-025 extends the tuple with ``ref``
(workflow-service tools ``run_workflow`` / ``introspect_node`` /
``validate_workflow``) and ``run_id`` (``cancel_workflow`` /
``workflow_status``) so those tools' approval widgets render a
meaningful ``Args`` line instead of an empty tail.

The authz rule's ``what`` matches against the tool's target string —
the resolved path for file tools, the command for ``bash``, the
workflow ref for ``run_workflow``. First-hit-wins so tools that
declare multiple priority attrs pick a stable one (``path`` beats
``ref`` — the existing convention wins for the tools that had it
first).

_TARGET_ARG_FALLBACK_CHAR_LIMIT is the maximum length of the
last-resort ``model_dump_json(exclude_defaults=True)`` truncation —
any longer args model is elided with a ``…`` suffix so the widget's
single-line ``Args`` row stays legible.
"""

_TARGET_ARG_FALLBACK_CHAR_LIMIT = 80
"""Truncation cap for the ``model_dump_json`` fallback (bug-025 AC #11)."""


def _extract_target_from_args(validated_args: object) -> str | None:
    """Return the conventional target string from a tool's args, if any.

    bug-025 AC #10 + #11:

    1. Priority walk — return the first stringy attr found on
       ``validated_args`` from :data:`_TARGET_ARG_PRIORITY`. Preserves
       backward-compat for tools with ``path``/``command``/``url``
       args.
    2. Fallback — when no priority attr matches, dump the args model
       to JSON (excluding defaults) and truncate to 80 chars with a
       ``…`` suffix if longer. Ensures every tool with non-empty
       args renders SOMETHING in the approval widget's ``Args`` row
       instead of an empty tail. Returns ``None`` only when the args
       model has literally no non-default content (rare — most tools
       declare at least one required field).
    """
    for attr in _TARGET_ARG_PRIORITY:
        value = getattr(validated_args, attr, None)
        if isinstance(value, str):
            return value

    # bug-025 AC #11: fallback to a truncated JSON dump. Uses
    # `exclude_defaults=True` so a tool with all-default args doesn't
    # render `"{}"` — that path returns `None` and the authz
    # matcher's action-namespace fallback kicks in as before.
    dump = getattr(validated_args, "model_dump_json", None)
    if callable(dump):
        try:
            rendered = dump(exclude_defaults=True)
        except Exception:  # noqa: BLE001 — best-effort
            return None
        if isinstance(rendered, str) and rendered and rendered != "{}":
            if len(rendered) > _TARGET_ARG_FALLBACK_CHAR_LIMIT:
                # Trim to N-1 then append the ellipsis for a total of
                # N chars — the widget's single-line row prefers a
                # bounded length.
                return (
                    rendered[: _TARGET_ARG_FALLBACK_CHAR_LIMIT - 1] + "…"
                )
            return rendered
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
