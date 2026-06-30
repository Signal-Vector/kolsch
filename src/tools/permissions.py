"""The ``kolsch.tools.permissions`` module — Phase 2.2 Step 10
(ENV §2).

Two async functions tool authors call from inside a ``@tool``
body:

- :func:`check` — returns the composed
  :class:`providers.authz.AuthzEffect` (``ALLOW`` / ``PROMPT`` /
  ``DENY``) without raising. Use for branching.
- :func:`require` — raises
  :class:`errors.AuthzDenied` (``authz.denied``, Recoverable) on
  ``DENY`` or on ``PROMPT`` when no callback is available. Use
  when the call MUST be allowed.

Both are async per Phase 2.2 §2.3 ("Async by default — matches
the Phase 2.0 dispatcher and Phase 1.1 ``build_llm`` patterns")
because the underlying
:meth:`providers.authz.AuthorizationProvider.allow` is async;
synchronous bridging from inside a coroutine is fragile.

## ContextVar binding

The engine's tool dispatcher binds a :class:`PermissionsBinding`
to the module's :data:`_BINDING` ``ContextVar`` immediately before
calling the tool function and clears it after (in both the
success and exception paths). Tools see the binding only during
their own execution; an out-of-context call raises
:class:`errors.ToolPermissionsNoContext`.

The binding carries:

- The :class:`providers.authz.AuthorizationProvider` instance the
  engine is using.
- The agent's resolved ``permissions:`` list (so the composed
  walk includes the agent layer per Phase 2.2 §1.2 / §3.8.1).
- The bundle's ``permissions:`` list (qol-tool-permissions-binding-unused.md
  Step 2 — same four-level stack as the dispatch-boundary check).
- The principal the tool is running as (for the composed
  ``allow(...)`` call).
- The tool name (for error envelopes).

## Why a Binding rather than the raw provider

Carrying the agent's + bundle's permissions on the binding
decouples :func:`check` / :func:`require` from how the engine
threads the resolved config into the dispatcher. The dispatch
flow (``src/tools/dispatch.py``) constructs one
:class:`PermissionsBinding` per tool invocation and tears it
down after — the tool's body sees a snapshot scoped exactly to
its own call.
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


# ---------------------------------------------------------------------------
# Binding type.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PermissionsBinding:
    """The state the engine's dispatcher pins on the
    :data:`_BINDING` ContextVar while a tool function runs.

    Frozen so a tool can't accidentally mutate the engine's
    authz state via the binding it received.

    qol-tool-permissions-binding-unused.md Step 2 — gains
    ``bundle_permissions`` so in-tool :func:`check`/:func:`require`
    walks the SAME four-level stack as the dispatch-boundary
    check (agent → bundle → platform → default). Without the
    bundle layer the in-tool API would diverge silently from the
    pre-dispatch authz outcome.
    """

    provider: AuthorizationProvider
    agent_permissions: list[Rule] = field(default_factory=list)
    bundle_permissions: list[Rule] = field(default_factory=list)
    principal: str = "user.local"
    tool: str = "<unknown>"


_BINDING: ContextVar[PermissionsBinding | None] = ContextVar(
    "tools.permissions._BINDING",
    default=None,
)


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def _layers_from(binding: PermissionsBinding) -> tuple[PermissionLayer, ...]:
    """Assemble the agent + bundle layer tuple from a binding.

    qol-tool-permissions-binding-unused.md Step 2 — mirrors the
    dispatch-boundary's layer assembly at ``dispatch.py:100-110``
    so the in-tool ``check``/``require`` walks the same stack:
    agent first, then bundle (platform rules are on the provider
    itself; default-deny is the tail).
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


async def check(who: str, what: str) -> AuthzEffect:
    """Return the composed :class:`AuthzEffect` for
    ``(who, what)``.

    ``who`` is the principal pattern (the agent calling the
    tool); ``what`` is the action OR target pattern. The lookup
    walks the same four-level stack as the dispatch-boundary
    check (agent → bundle → platform → default-deny) so in-tool
    decisions match what the platform would have decided.

    Raises :class:`ToolPermissionsNoContext` (Fatal,
    ``tool.permissions_no_context``) when called outside the
    engine's dispatcher binding — typically a unit test that
    imported the tool directly without setting up an authz
    fixture.
    """
    binding = _BINDING.get()
    if binding is None:
        raise ToolPermissionsNoContext(tool="<unbound>")
    _ = who  # accepted for API symmetry; binding.principal is canonical
    result = await binding.provider.allow(
        binding.principal,
        what,
        target=what,
        layers=_layers_from(binding),
    )
    return result.effect


async def require(who: str, what: str) -> None:
    """Raise :class:`AuthzDenied` on anything other than
    :data:`AuthzEffect.ALLOW`.

    The composed result's effect is used directly — PROMPT
    without a callback collapses to deny per Phase 2.2 §2.3, so
    ``require`` treats both ``DENY`` and ``PROMPT`` (in non-chat
    contexts) as denial. The raised :class:`AuthzDenied` carries
    the tool name from the binding so error envelopes are
    actionable.

    Note: the in-tool ``require`` does NOT trigger the no-rule
    modal that the dispatch-boundary check fires (per
    qol-tool-permissions-binding-unused.md Step 1 design pick
    Q3 — modals mid-execution would block the tool's coroutine
    and surface a UX disconnect from the user's perspective).
    A PROMPT-effect result here always collapses to deny.
    """
    binding = _BINDING.get()
    if binding is None:
        raise ToolPermissionsNoContext(tool="<unbound>")
    _ = who
    result = await binding.provider.allow(
        binding.principal,
        what,
        target=what,
        layers=_layers_from(binding),
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
    """Set :data:`_BINDING` to ``binding`` and return the
    :class:`contextvars.Token` the engine uses to reset it.

    The engine's tool dispatcher wraps the tool call in
    ``token = permissions.bind(...)`` / ``try ... finally:
    permissions.reset(token)`` so the ContextVar is restored on
    BOTH the success and exception paths (Step 10 reviewer's
    check 2).
    """
    return _BINDING.set(binding)


def reset(token: Token[PermissionsBinding | None]) -> None:
    """Restore the previous ContextVar value. ``token`` is what
    :func:`bind` returned. Always call this in a ``finally`` —
    typically inside the tool dispatcher."""
    _BINDING.reset(token)


def current_binding() -> PermissionsBinding | None:
    """Return the currently-bound :class:`PermissionsBinding`
    for inspection (engine internals, tests). Returns ``None``
    when no binding is active."""
    return _BINDING.get()
