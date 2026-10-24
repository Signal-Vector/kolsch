"""In-tool authz access — `check` and `require`.

Two async functions tool authors call from inside a `@tool` body:
[`check`][tools.permissions.check] returns the composed
[`AuthzEffect`][providers.authz.AuthzEffect] without raising;
[`require`][tools.permissions.require] raises
[`AuthzDenied`][errors.AuthzDenied] on anything other than `ALLOW`.

Both are async because the underlying
`AuthorizationProvider.allow` is async; synchronous bridging from
inside a coroutine is fragile.

**Binding.** The dispatch flow binds a
[`PermissionsBinding`][tools.permissions.PermissionsBinding] to the
module's `_BINDING` `ContextVar` immediately before calling the tool
function and clears it after (on both the success and exception
paths). Tools see the binding only during their own execution; an
out-of-context call raises
[`ToolPermissionsNoContext`][errors.ToolPermissionsNoContext]. The
binding carries the provider, the agent and bundle permission lists,
the principal, and the tool name.

**Why a binding rather than the raw provider.** Carrying the agent's
and bundle's permissions on the binding decouples `check` / `require`
from how the engine threads the resolved config into the dispatcher.
The dispatch flow constructs one binding per tool invocation and
tears it down after — the tool body sees a snapshot scoped exactly
to its own call.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from errors import AuthzDenied, ToolPermissionsNoContext
from providers.authz import AuthzEffect, PermissionLayer

if TYPE_CHECKING:
    from contextvars import Token

    from providers.authz import AuthorizationProvider, Rule
    from schemas import CallerChain


# ---------------------------------------------------------------------------
# Binding type.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PermissionsBinding:
    """The state the dispatcher pins on `_BINDING` while a tool runs.

    Frozen so a tool can't accidentally mutate the engine's authz
    state via the binding it received. Carries `bundle_permissions`
    alongside `agent_permissions` so in-tool
    [`check`][tools.permissions.check] /
    [`require`][tools.permissions.require] walks the same four-level
    stack (agent → bundle → platform → default) as the
    dispatch-boundary check — without the bundle layer the in-tool
    API would diverge silently from the pre-dispatch authz outcome.

    `caller_chain` is the dispatching call's
    [`ToolContext.caller_chain`][tools.decorator.ToolContext], so an
    in-tool check sees the same proximate-first frames — and the same
    `from:`-scoped rule matches — as the dispatch-boundary check.
    Required (keyword-only, no default).
    """

    provider: AuthorizationProvider
    agent_permissions: list[Rule] = field(default_factory=list)
    bundle_permissions: list[Rule] = field(default_factory=list)
    principal: str = "user.local"
    tool: str = "<unknown>"
    caller_chain: CallerChain = field(kw_only=True)


_BINDING: ContextVar[PermissionsBinding | None] = ContextVar(
    "tools.permissions._BINDING",
    default=None,
)


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def _layers_from(binding: PermissionsBinding) -> tuple[PermissionLayer, ...]:
    """Assemble the agent + bundle layer tuple from a binding.

    Mirrors the dispatch-boundary's layer assembly so the in-tool
    `check` / `require` walks the same stack: agent first, then
    bundle (platform rules live on the provider itself; default-deny
    is the tail).
    """
    layers: tuple[PermissionLayer, ...] = ()
    if binding.agent_permissions:
        layers = (
            *layers,
            PermissionLayer(name="agent", rules=tuple(binding.agent_permissions)),
        )
    if binding.bundle_permissions:
        layers = (
            *layers,
            PermissionLayer(name="bundle", rules=tuple(binding.bundle_permissions)),
        )
    return layers


async def check(what: str) -> AuthzEffect:
    """Return the composed [`AuthzEffect`][providers.authz.AuthzEffect].

    The lookup walks the same four-level stack as the
    dispatch-boundary check (agent → bundle → platform →
    default-deny) so in-tool decisions match what the platform would
    have decided pre-dispatch.

    Path targets are not normalised here; dispatch-time authz is the
    path-aware check.

    Args:
        what: The authz action string or target pattern.

    Returns:
        The composed effect — `ALLOW`, `PROMPT`, or `DENY`.

    Raises:
        ToolPermissionsNoContext: Called outside the dispatcher's
            binding — typically a unit test that imported the tool
            directly without setting up an authz fixture.
    """
    binding = _BINDING.get()
    if binding is None:
        raise ToolPermissionsNoContext(tool="<unbound>")
    result = await binding.provider.allow(
        binding.principal,
        what,
        target=what,
        layers=_layers_from(binding),
        caller_chain=binding.caller_chain.frames,
    )
    return result.effect


async def require(what: str) -> None:
    """Raise [`AuthzDenied`][errors.AuthzDenied] on anything other than `ALLOW`.

    The composed result's effect is used directly — `PROMPT` without
    a callback collapses to deny, so `require` treats both `DENY`
    and `PROMPT` (in non-chat contexts) as denial. The raised
    `AuthzDenied` carries the tool name from the binding so error
    envelopes are actionable.

    Unlike the dispatch-boundary check, the in-tool `require` does
    not trigger the no-rule modal — surfacing one mid-execution
    would block the tool's coroutine and disconnect from the user's
    perspective. A `PROMPT` effect here always collapses to deny.

    Path targets are not normalised here; dispatch-time authz is the
    path-aware check.

    Args:
        what: The authz action string or target pattern.

    Raises:
        AuthzDenied: The composed effect was `DENY` or `PROMPT`.
        ToolPermissionsNoContext: Called outside the dispatcher's
            binding.
    """
    binding = _BINDING.get()
    if binding is None:
        raise ToolPermissionsNoContext(tool="<unbound>")
    result = await binding.provider.allow(
        binding.principal,
        what,
        target=what,
        layers=_layers_from(binding),
        caller_chain=binding.caller_chain.frames,
    )
    if result.effect is AuthzEffect.ALLOW:
        return
    rule_id = result.matched_rule.id if result.matched_rule is not None else None
    raise AuthzDenied(
        tool=binding.tool,
        target=what,
        rule_id=rule_id,
    )


# ---------------------------------------------------------------------------
# Dispatcher seam — called by the engine before / after invoking the tool.
# ---------------------------------------------------------------------------


def bind(binding: PermissionsBinding) -> Token[PermissionsBinding | None]:
    """Set `_BINDING` to `binding` and return the reset token.

    The dispatch flow wraps the tool call in
    `token = permissions.bind(...)` / `try ... finally:
    permissions.reset(token)` so the `ContextVar` is restored on
    both the success and exception paths.
    """
    return _BINDING.set(binding)


def reset(token: Token[PermissionsBinding | None]) -> None:
    """Restore the previous `_BINDING` value.

    `token` is what [`bind`][tools.permissions.bind] returned. Always
    call this in a `finally` — typically inside the tool dispatcher.
    """
    _BINDING.reset(token)


def current_binding() -> PermissionsBinding | None:
    """Return the currently-bound `PermissionsBinding`, or `None`.

    For inspection by engine internals and tests; `None` when no
    binding is active.
    """
    return _BINDING.get()
