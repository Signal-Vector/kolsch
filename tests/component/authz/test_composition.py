"""Permission composition — Phase 2.2 Step 9 (ENV §3.8 / §1.2).

Pins the three composition paths the spec mandates:

1. **Agent rule allow + platform rule deny → ALLOW** (agent wins).
2. **Agent rule absent + platform rule deny → DENY** (platform
   fallback).
3. **Both absent → DENY** (default-deny tail).

Plus a back-compat check: ``agent_permissions=None`` (the
default — every existing caller) produces identical behaviour
to the pre-Phase-2.2 baseline.

The ``who_chain`` detail on the result's reason names which
layer matched.
"""

from __future__ import annotations

import pytest

from providers.authz import (
    AuthzEffect,
    RuleBasedAuthorizationProvider,
)
from providers.authz import Rule as AuthzRule


@pytest.fixture
def deny_bash_provider() -> RuleBasedAuthorizationProvider:
    """Platform configured with a single ``deny tool.bash`` rule."""
    return RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"who": ".*", "what": r"tool\.bash", "effect": "deny"},
            ],
        },
    )


@pytest.fixture
def empty_provider() -> RuleBasedAuthorizationProvider:
    """Platform with no rules (only the implicit default-deny
    tail)."""
    return RuleBasedAuthorizationProvider(config={"rules": []})


# ---------------------------------------------------------------------------
# Three composition paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_allow_wins_over_platform_deny(
    deny_bash_provider: RuleBasedAuthorizationProvider,
) -> None:
    """Spec §1.2 / §3.8.1: agent permissions evaluate FIRST.
    Agent's ``allow`` for the same target overrides the
    platform's ``deny``."""
    from providers.authz import PermissionLayer

    agent_rules = (AuthzRule(who=".*", what=r"tool\.bash", effect="allow"),)
    result = await deny_bash_provider.allow(
        "agent.ag_helper",
        "tool.bash",
        layers=(PermissionLayer(name="agent", rules=agent_rules),),
    )
    assert result.allowed is True
    assert result.effect is AuthzEffect.ALLOW
    assert "who_chain=agent" in result.reason


@pytest.mark.asyncio
async def test_platform_deny_applies_when_agent_silent(
    deny_bash_provider: RuleBasedAuthorizationProvider,
) -> None:
    """No agent rules → platform's deny rule fires. The reason
    detail names the platform layer."""
    result = await deny_bash_provider.allow(
        "agent.ag_helper",
        "tool.bash",
    )
    assert result.allowed is False
    assert result.effect is AuthzEffect.DENY
    assert "who_chain=platform" in result.reason


@pytest.mark.asyncio
async def test_default_deny_when_both_layers_absent(
    empty_provider: RuleBasedAuthorizationProvider,
) -> None:
    """No agent rules + no platform rules → default-deny tail
    fires. The reason detail names ``who_chain=default`` so
    debugging can identify the tail."""
    result = await empty_provider.allow(
        "agent.ag_helper",
        "tool.bash",
    )
    assert result.allowed is False
    assert result.effect is AuthzEffect.DENY
    assert "who_chain=default" in result.reason


# ---------------------------------------------------------------------------
# Back-compat — None / empty list reproduces the pre-Phase-2.2 surface.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_permissions_none_matches_pre_phase_2_2_behaviour(
    deny_bash_provider: RuleBasedAuthorizationProvider,
) -> None:
    """Phase 3.0 Step 4 surface — ``layers=()`` (the default) produces
    IDENTICAL behaviour to an explicitly-empty layer tuple."""
    result_default = await deny_bash_provider.allow(
        "agent.ag_helper",
        "tool.bash",
    )
    result_explicit = await deny_bash_provider.allow(
        "agent.ag_helper",
        "tool.bash",
        layers=(),
    )
    assert result_explicit.allowed == result_default.allowed
    assert result_explicit.effect == result_default.effect
    assert result_explicit.matched_rule == result_default.matched_rule


@pytest.mark.asyncio
async def test_empty_agent_permissions_list_matches_none(
    deny_bash_provider: RuleBasedAuthorizationProvider,
) -> None:
    """An empty-rules layer is indistinguishable from omitting the
    layer entirely — an empty rule list contributes nothing to the
    composed walk."""
    from providers.authz import PermissionLayer

    result = await deny_bash_provider.allow(
        "agent.ag_helper",
        "tool.bash",
        layers=(PermissionLayer(name="agent", rules=()),),
    )
    assert result.allowed is False
    assert "who_chain=platform" in result.reason


# ---------------------------------------------------------------------------
# Agent's first-match-wins is independent of platform's first-match-wins.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_first_match_wins(
    deny_bash_provider: RuleBasedAuthorizationProvider,
) -> None:
    """Within the agent layer, first-match-wins per A-I2. A more
    specific allow EARLIER in the list overrides a deny later."""
    from providers.authz import PermissionLayer

    agent_rules = (
        AuthzRule(who=".*", what=r"tool\.bash", effect="allow"),
        AuthzRule(who=".*", what=r"tool\..*", effect="deny"),
    )
    result = await deny_bash_provider.allow(
        "agent.ag_helper",
        "tool.bash",
        layers=(PermissionLayer(name="agent", rules=agent_rules),),
    )
    assert result.allowed is True
    assert "who_chain=agent" in result.reason
