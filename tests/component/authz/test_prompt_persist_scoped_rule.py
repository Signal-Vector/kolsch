"""Component test — a prompt callback can persist an agent-scoped rule (bug-158).

The "scoped always-allow" capability at the provider seam: a registered
prompt callback receives the real caller chain of a `dispatch_tool`
call, persists `Rule(from_=caller_chain[0], effect=allow)`, and that
rule then allows the same agent's next call with no prompt while a
different agent still reaches the callback. (The live chat widget
deliberately saves unscoped rules; see
`tests/cli/chat/test_approval_persist_and_reprompt.py`.)
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from errors import ToolUnauthorized
from providers.authz import AuthzEffect, Rule, RuleBasedAuthorizationProvider
from schemas import AgentId, CallerChain, SessionId, ThreadId
from tools.decorator import ToolContext, ToolDefinition
from tools.dispatch import dispatch_tool

_A = "kolsch/agents/a"
_B = "kolsch/agents/b"
_ACTION = "kolsch/tools/probe"


class _Args(BaseModel):
    path: str


async def _body(_args: _Args, _ctx: ToolContext) -> str:
    return "ok"


_DEFN = ToolDefinition(
    name="probe",
    description="probe",
    args_schema=_Args,
    func=_body,
    home_bundle="kolsch",
)


class _Secrets:
    async def resolve(self, name: str) -> str:
        return f"secret-{name}"


class _Sandbox:
    async def execute(self, work: Any, policy: dict[str, Any]) -> Any:
        _ = policy
        return await work()


class _ScopingCallback:
    """Allows the first prompt and persists a rule scoped to its caller;
    denies (single-call) every later prompt."""

    def __init__(self) -> None:
        self.chains: list[tuple[str, ...]] = []

    async def __call__(
        self,
        *,
        rule_id: str | None,
        action: str,
        target: str | None,
        caller_chain: tuple[str, ...] = (),
        execution_context: Any = None,
    ) -> AuthzEffect | tuple[AuthzEffect, Rule]:
        _ = rule_id, target, execution_context
        self.chains.append(caller_chain)
        if len(self.chains) == 1:
            return (
                AuthzEffect.ALLOW,
                Rule(what=action, pattern="*", from_=caller_chain[0], effect="allow"),
            )
        return AuthzEffect.DENY


async def _dispatch(provider: RuleBasedAuthorizationProvider, agent_ref: str) -> Any:
    return await dispatch_tool(
        defn=_DEFN,
        args={"path": "/tmp/x"},
        context=ToolContext(
            agent_id=AgentId("ag_probe"),
            session_id=SessionId(f"ses_{uuid4()}"),
            thread_id=ThreadId(f"th_{uuid4()}"),
            principal="agent.ag_probe",
            secrets={},
            caller_chain=CallerChain((agent_ref,)),
        ),
        authz=provider,
        secrets=_Secrets(),
        sandbox=_Sandbox(),
    )


async def test_persisted_scoped_rule_allows_only_that_agent() -> None:
    provider = RuleBasedAuthorizationProvider(
        config={
            "rules": [
                {"what": _ACTION, "pattern": "*", "effect": "prompt"},
                {"what": "*", "pattern": "*", "effect": "deny"},
            ]
        }
    )
    callback = _ScopingCallback()
    persisted: list[list[Rule]] = []
    provider.register_prompt_callback(callback, on_rule_added=persisted.append)

    # A's first call prompts; the callback sees A's real chain and
    # persists an A-scoped allow.
    assert await _dispatch(provider, _A) == "ok"
    assert callback.chains == [(_A,)]
    (rules,) = persisted
    scoped = [r for r in rules if r.from_ == _A]
    assert [(r.what, r.effect) for r in scoped] == [(_ACTION, "allow")]

    # A's next call matches the persisted rule — no prompt.
    assert await _dispatch(provider, _A) == "ok"
    assert callback.chains == [(_A,)]

    # B is outside the scope: it reaches the callback, which denies.
    with pytest.raises(ToolUnauthorized) as excinfo:
        await _dispatch(provider, _B)
    assert callback.chains == [(_A,), (_B,)]
    assert excinfo.value.detail["caller_chain"] == [_B]
