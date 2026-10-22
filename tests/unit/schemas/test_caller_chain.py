"""Unit tests for `CallerChain` and its required carrier fields — bug-158.

`CallerChain` is the proximate-first tuple of catalog refs a tool
authorization is judged against. The empty chain is only reachable
through `CallerChain.unattributed()`, and the carriers
(`ToolContext`, `PermissionsBinding`) have no default, so a producer
can never silently drop the chain.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest
from pydantic import ValidationError

from providers.authz import AuthzEffect, RuleBasedAuthorizationProvider
from schemas import AgentId, CallerChain, SessionId, ThreadId
from tools.decorator import ToolContext
from tools.permissions import PermissionsBinding

_A = "kolsch/agents/a"
_W = "kolsch/workflows/w"


def test_unattributed_is_empty_and_shared() -> None:
    chain = CallerChain.unattributed()
    assert chain.frames == ()
    assert CallerChain.unattributed() is chain


def test_constructor_rejects_empty_frames() -> None:
    """The only way to an empty chain is `unattributed()`, by name."""
    with pytest.raises(ValueError, match="unattributed"):
        CallerChain(())


@pytest.mark.parametrize("bad", ["", 3, None])
def test_constructor_rejects_non_string_or_empty_frames(bad: object) -> None:
    with pytest.raises(ValueError, match="non-empty strings"):
        CallerChain((_A, bad))


def test_frames_are_lenient_about_shape() -> None:
    """No 3-seg check: legacy / fixture frames are accepted verbatim."""
    assert CallerChain(("test/hello",)).frames == ("test/hello",)


def test_push_prepends_and_returns_new_chain() -> None:
    base = CallerChain((_W,))
    pushed = base.push(_A)
    assert pushed.frames == (_A, _W)
    assert base.frames == (_W,)
    assert CallerChain.unattributed().push(_A).frames == (_A,)


def test_chain_is_frozen_and_hashable() -> None:
    chain = CallerChain((_A,))
    with pytest.raises(FrozenInstanceError):
        chain.frames = ()  # type: ignore[misc]
    assert {chain, CallerChain((_A,))} == {chain}


def _ctx_kwargs() -> dict[str, object]:
    return {
        "agent_id": AgentId("ag_test"),
        "session_id": SessionId(f"ses_{uuid4()}"),
        "thread_id": ThreadId(f"th_{uuid4()}"),
        "principal": "agent.ag_test",
        "secrets": {},
    }


def test_tool_context_requires_caller_chain() -> None:
    with pytest.raises(ValidationError, match="caller_chain"):
        ToolContext(**_ctx_kwargs())


def test_tool_context_rejects_empty_chain_given_as_data() -> None:
    """Building the field from raw data still goes through the
    constructor's empty-chain refusal."""
    with pytest.raises(ValidationError):
        ToolContext(**_ctx_kwargs(), caller_chain={"frames": ()})


def test_tool_context_keeps_the_given_chain() -> None:
    chain = CallerChain((_A, _W))
    ctx = ToolContext(**_ctx_kwargs(), caller_chain=chain)
    assert ctx.caller_chain is chain


def test_permissions_binding_requires_caller_chain() -> None:
    provider = RuleBasedAuthorizationProvider(config={"rules": []})
    with pytest.raises(TypeError, match="caller_chain"):
        PermissionsBinding(provider=provider)  # type: ignore[call-arg]


async def test_scoped_rule_does_not_match_unattributed_chain() -> None:
    """D6: a `from:`-scoped rule never matches the unattributed chain
    (the call falls through to the next rule), and does match a chain
    that carries the frame."""
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": "tool.echo", "pattern": "*", "from": _A, "effect": "deny"},
                {"what": "*", "pattern": "*", "effect": "allow"},
            ]
        }
    )
    unattributed = await provider.allow(
        "agent.ag_a",
        "tool.echo",
        caller_chain=CallerChain.unattributed().frames,
    )
    attributed = await provider.allow(
        "agent.ag_a",
        "tool.echo",
        caller_chain=CallerChain((_A, _W)).frames,
    )
    assert unattributed.effect is AuthzEffect.ALLOW
    assert attributed.effect is AuthzEffect.DENY
