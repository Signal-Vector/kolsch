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

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tools.decorator import ToolContext

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
        effect = await permissions.check("kolsch/tools/bash")
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
        effect = await permissions.check("kolsch/tools/bash")
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
            await permissions.require("kolsch/tools/bash")
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
        await permissions.require(
            "kolsch/tools/bash"
        )  # returns None; success = no raise
    finally:
        permissions.reset(token)


# ---------------------------------------------------------------------------
# 3. Out-of-context calls raise.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_outside_binding_raises_permissions_no_context() -> None:
    """No binding active → :class:`ToolPermissionsNoContext`
    (Fatal). Catches the test-fixture mistake of importing the
    tool directly."""
    with pytest.raises(ToolPermissionsNoContext) as excinfo:
        await permissions.check("kolsch/tools/bash")
    assert excinfo.value.code == "tool.permissions_no_context"


@pytest.mark.asyncio
async def test_require_outside_binding_raises_permissions_no_context() -> None:
    """Symmetric to ``check`` — same error envelope."""
    with pytest.raises(ToolPermissionsNoContext):
        await permissions.require("kolsch/tools/bash")


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
        raise RuntimeError("tool blew up")
    except RuntimeError:
        pass
    finally:
        permissions.reset(token)
    assert permissions.current_binding() is None


# ---------------------------------------------------------------------------
# qol-tool-permissions-binding-unused.md Step 2 — dispatch wires the binding
# ---------------------------------------------------------------------------


class _StubSecrets:
    """Minimal SecretsProvider stub for dispatch tests with no
    ``secrets_needed`` — ``resolve``/``exists`` are unreachable."""

    async def resolve(self, name: str) -> str:  # pragma: no cover
        msg = f"unexpected resolve({name!r})"
        raise AssertionError(msg)

    async def exists(self, name: str) -> bool:  # pragma: no cover
        return False

    async def list_names(self) -> list[str]:  # pragma: no cover
        return []


class _PassthroughSandbox:
    """Minimal SandboxProvider stub — runs the work closure directly."""

    async def execute(
        self,
        work: object,
        policy: dict[str, Any],
    ) -> object:
        _ = policy
        return await work()  # type: ignore[operator]


def _ctx_for(name: str) -> ToolContext:
    """Build a minimal :class:`ToolContext` for the dispatcher.

    Session / thread IDs require uuid bodies; agent_id accepts any
    alphanumeric body (see :mod:`schemas.identifiers`).
    """
    import uuid

    from schemas import AgentId, SessionId, ThreadId
    from tools.decorator import ToolContext

    u = str(uuid.uuid4())
    return ToolContext(
        agent_id=AgentId(f"ag_{name}"),
        session_id=SessionId(f"ses_{u}"),
        thread_id=ThreadId(f"th_{u}"),
        principal=f"agent.{name}",
        secrets={},
    )


@pytest.mark.asyncio
async def test_dispatcher_binds_permissions_around_tool_function() -> None:
    """When ``dispatch_tool`` invokes a tool whose body calls
    ``permissions.check(...)``, the binding is populated for the
    duration of the body and cleared after.

    Regression test for the gap qol-tool-permissions-binding-unused
    fixed: before this QoL the binding was NEVER set, so every
    in-body ``check`` raised ``ToolPermissionsNoContext``.
    """
    from pydantic import BaseModel

    from tools.decorator import ToolDefinition
    from tools.dispatch import dispatch_tool

    class _Args(BaseModel):
        path: str

    captured: dict[str, Any] = {}

    async def _body(_args: _Args, _ctx: ToolContext) -> str:
        captured["pre_effect"] = await permissions.check("workspace.git.write")
        captured["pre_binding"] = permissions.current_binding()
        return "ok"

    defn = ToolDefinition(
        name="probe",
        description="probe",
        args_schema=_Args,
        func=_body,
        home_bundle="kolsch",
    )
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {
                    "what": "workspace.git.write",
                    "pattern": "*",
                    "effect": "allow",
                },
                {"what": "*", "pattern": "*", "effect": "allow"},
            ],
        },
    )
    result = await dispatch_tool(
        defn=defn,
        args={"path": "/tmp/x"},
        context=_ctx_for("probe"),
        authz=provider,
        secrets=_StubSecrets(),
        sandbox=_PassthroughSandbox(),
    )
    assert result == "ok"
    assert captured["pre_effect"] is AuthzEffect.ALLOW
    assert isinstance(captured["pre_binding"], permissions.PermissionsBinding)
    # After dispatch returns, the binding is cleared again.
    assert permissions.current_binding() is None


@pytest.mark.asyncio
async def test_dispatcher_binding_includes_bundle_layer() -> None:
    """The binding carries ``bundle_permissions`` so in-tool
    ``require`` participates in the same four-level stack the
    dispatch-boundary check uses. A bundle-layer allow rule
    satisfies an in-tool ``require`` for an action no other layer
    covers."""
    from pydantic import BaseModel

    from tools.decorator import ToolDefinition
    from tools.dispatch import dispatch_tool

    class _Args(BaseModel):
        path: str

    inner_ok = False

    async def _body(_args: _Args, _ctx: ToolContext) -> str:
        nonlocal inner_ok
        await permissions.require("workspace.git.commit")
        inner_ok = True
        return "ok"

    defn = ToolDefinition(
        name="probe",
        description="probe",
        args_schema=_Args,
        func=_body,
        home_bundle="kolsch",
    )
    # Platform: allow the tool-action (so dispatch passes) but
    # deny the in-tool action explicitly. The bundle's allow rule
    # MUST win to satisfy the in-body `require`. If the bundle
    # layer weren't being threaded through, the platform deny would
    # surface here and the test would raise AuthzDenied.
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {
                    "what": "kolsch/tools/probe",
                    "pattern": "*",
                    "effect": "allow",
                },
                {
                    "what": "workspace.git.commit",
                    "pattern": "*",
                    "effect": "deny",
                },
                {"what": "*", "pattern": "*", "effect": "deny"},
            ],
        },
    )
    bundle_rules = [
        AuthzRule(what="workspace.git.commit", pattern="*", effect="allow"),
    ]
    result = await dispatch_tool(
        defn=defn,
        args={"path": "/tmp/x"},
        context=_ctx_for("probe"),
        authz=provider,
        secrets=_StubSecrets(),
        sandbox=_PassthroughSandbox(),
        bundle_permissions=bundle_rules,
    )
    assert result == "ok"
    assert inner_ok


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
