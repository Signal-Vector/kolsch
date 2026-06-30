"""``kolsch.tools.permissions`` API tests — Phase 2.2 Step 10.

Pins the four contract points from the build plan + the
context-isolation discipline:

1. ``check`` returns the composed effect.
2. ``require`` raises :class:`AuthzDenied` on DENY/PROMPT,
   returns ``None`` on ALLOW.
3. Out-of-context calls raise
   :class:`ToolPermissionsNoContext` (Fatal,
   ``tool.permissions_no_context``).
4. The ContextVar is cleared on BOTH success and exception
   paths.

Plus a smoke for the composition path — when both an agent
rule and a platform rule apply, agent wins (per Phase 2.2 §1.2).
"""

from __future__ import annotations

import pytest

from errors import AuthzDenied, ToolPermissionsNoContext
from providers.authz import AuthzEffect, RuleBasedAuthorizationProvider
from providers.authz import Rule as AuthzRule
from tools import permissions


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def deny_bash_provider() -> RuleBasedAuthorizationProvider:
    return RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "kolsch/tools/bash", "pattern": "*", "effect": "deny"},
            ],
        },
    )


# ---------------------------------------------------------------------------
# 1. check returns composed effect.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_returns_deny_when_platform_denies(
    deny_bash_provider: RuleBasedAuthorizationProvider,
) -> None:
    binding = permissions.PermissionsBinding(
        provider=deny_bash_provider,
        principal="agent.ag_helper",
        tool="bash",
    )
    token = permissions.bind(binding)
    try:
        effect = await permissions.check("agent.ag_helper", "kolsch/tools/bash")
    finally:
        permissions.reset(token)
    assert effect is AuthzEffect.DENY


@pytest.mark.asyncio
async def test_check_returns_allow_when_agent_rule_overrides_platform(
    deny_bash_provider: RuleBasedAuthorizationProvider,
) -> None:
    """Composition path: agent rule wins. Phase 2.2 §1.2 / §3.8.1."""
    binding = permissions.PermissionsBinding(
        provider=deny_bash_provider,
        agent_permissions=[
            AuthzRule(what="kolsch/tools/bash", pattern="*", effect="allow"),
        ],
        principal="agent.ag_helper",
        tool="bash",
    )
    token = permissions.bind(binding)
    try:
        effect = await permissions.check("agent.ag_helper", "kolsch/tools/bash")
    finally:
        permissions.reset(token)
    assert effect is AuthzEffect.ALLOW


# ---------------------------------------------------------------------------
# 2. require raises on deny, returns on allow.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_raises_authz_denied_on_deny(
    deny_bash_provider: RuleBasedAuthorizationProvider,
) -> None:
    binding = permissions.PermissionsBinding(
        provider=deny_bash_provider,
        principal="agent.ag_helper",
        tool="bash",
    )
    token = permissions.bind(binding)
    try:
        with pytest.raises(AuthzDenied) as excinfo:
            await permissions.require("agent.ag_helper", "kolsch/tools/bash")
    finally:
        permissions.reset(token)
    err = excinfo.value
    assert err.code == "authz.denied"
    assert err.detail["tool"] == "bash"


@pytest.mark.asyncio
async def test_require_returns_none_on_allow() -> None:
    """ALLOW returns cleanly — no exception."""
    allow_provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "kolsch/tools/bash", "pattern": "*", "effect": "allow"},
            ],
        },
    )
    binding = permissions.PermissionsBinding(
        provider=allow_provider,
        principal="agent.ag_helper",
        tool="bash",
    )
    token = permissions.bind(binding)
    try:
        result = await permissions.require("agent.ag_helper", "kolsch/tools/bash")
    finally:
        permissions.reset(token)
    assert result is None


# ---------------------------------------------------------------------------
# 3. Out-of-context calls raise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_outside_binding_raises_permissions_no_context() -> None:
    """No binding active → :class:`ToolPermissionsNoContext`
    (Fatal). Catches the test-fixture mistake of importing the
    tool directly."""
    with pytest.raises(ToolPermissionsNoContext) as excinfo:
        await permissions.check("agent.ag_helper", "kolsch/tools/bash")
    assert excinfo.value.code == "tool.permissions_no_context"


@pytest.mark.asyncio
async def test_require_outside_binding_raises_permissions_no_context() -> None:
    """Symmetric to ``check`` — same error envelope."""
    with pytest.raises(ToolPermissionsNoContext):
        await permissions.require("agent.ag_helper", "kolsch/tools/bash")


# ---------------------------------------------------------------------------
# 4. ContextVar isolation — cleared on BOTH success + exception paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_var_cleared_after_clean_exit(
    deny_bash_provider: RuleBasedAuthorizationProvider,
) -> None:
    """After ``reset(token)`` the binding is gone."""
    binding = permissions.PermissionsBinding(
        provider=deny_bash_provider,
        principal="agent.ag_helper",
    )
    assert permissions.current_binding() is None
    token = permissions.bind(binding)
    try:
        assert permissions.current_binding() is binding
    finally:
        permissions.reset(token)
    assert permissions.current_binding() is None


@pytest.mark.asyncio
async def test_context_var_cleared_after_exception_path(
    deny_bash_provider: RuleBasedAuthorizationProvider,
) -> None:
    """Engine's discipline: wrap the tool call in
    ``try / finally``. Even an exception inside the tool body
    must NOT leak the binding."""
    binding = permissions.PermissionsBinding(
        provider=deny_bash_provider,
        principal="agent.ag_helper",
    )
    token = permissions.bind(binding)
    try:
        # Simulate the tool raising mid-execution.
        raise RuntimeError("tool blew up")  # noqa: TRY301
    except RuntimeError:
        pass
    finally:
        permissions.reset(token)
    assert permissions.current_binding() is None


@pytest.mark.asyncio
async def test_nested_bindings_restore_outer_on_reset(
    deny_bash_provider: RuleBasedAuthorizationProvider,
) -> None:
    """``reset(token)`` restores the previous binding, not
    ``None`` — supports composite tools that themselves dispatch
    sub-tools through the engine."""
    outer = permissions.PermissionsBinding(
        provider=deny_bash_provider,
        principal="agent.outer",
    )
    inner = permissions.PermissionsBinding(
        provider=deny_bash_provider,
        principal="agent.inner",
    )
    outer_token = permissions.bind(outer)
    try:
        inner_token = permissions.bind(inner)
        try:
            assert permissions.current_binding() is inner
        finally:
            permissions.reset(inner_token)
        # Outer is restored.
        assert permissions.current_binding() is outer
    finally:
        permissions.reset(outer_token)
    assert permissions.current_binding() is None
