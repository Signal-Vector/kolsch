"""Regression tests for bug-workflow-policy-not-enforced.

The workflow YAML's ``policy:`` field was validated at parse time
but never applied at dispatch time — users' workflow-scoped rules
silently did nothing. The fix adds a ``workflow_permissions`` kwarg
to ``dispatch_tool`` and threads it into the layered authz check
as the FIRST layer (proximate caller = workflow node), matching the
docstring at ``src/providers/authz.py:813-822``.

These tests pin:

1. ``dispatch_tool`` accepts a ``workflow_permissions`` kwarg.
2. When supplied, the outermost layer sent to ``authz.allow`` has
   ``name="workflow"`` and carries the passed rules.
3. Layer ordering is ``workflow → agent → bundle`` (proximate-first).
4. Omitting the kwarg — the case for agent-invoked or CLI-invoked
   tools — produces byte-identical layer tuples to before the fix
   (so no regression to the agent/bundle path).

Providers: real ``NativeSandboxProvider`` and
``DotenvSecretsProvider`` (matches the pattern in
``tests/component/test_tools.py``). The authz provider is a small
recording double whose only job is to capture the ``layers=`` kwarg
``dispatch_tool`` sends to it — that IS the thing under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from providers import DotenvSecretsProvider, NativeSandboxProvider
from providers.authz import Rule
from schemas import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext, ToolDefinition
from tools.dispatch import dispatch_tool

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Recording authz double — records the ``layers=`` kwarg dispatch sends.
# We use a real object rather than mock/patch because the shape of what
# gets forwarded IS what these tests want to lock in.
# ---------------------------------------------------------------------------


@dataclass
class _RecordingAuthz:
    layers_seen: tuple[Any, ...] = field(default_factory=tuple)

    async def allow(
        self,
        principal: str,
        action: str,
        *,
        target: str | None = None,
        layers: tuple[Any, ...] = (),
        caller_chain: tuple[str, ...] = (),
    ) -> Any:
        self.layers_seen = layers

        class _AllowResult:
            allowed = True
            matched_rule = None
            reason = "test-allow"

        return _AllowResult()


# ---------------------------------------------------------------------------
# Test tool: a trivial no-op so the dispatch pipeline can complete.
# ---------------------------------------------------------------------------


class _EmptyArgs(BaseModel):
    pass


async def _no_op_tool(args: _EmptyArgs, ctx: ToolContext) -> dict[str, Any]:
    return {"ok": True}


def _make_defn() -> ToolDefinition:
    return ToolDefinition(
        name="noop",
        description="a no-op for the dispatch-layers regression test",
        args_schema=_EmptyArgs,
        func=_no_op_tool,
        home_bundle="kolsch",
    )


def _ctx() -> ToolContext:
    return ToolContext(
        agent_id=AgentId("ag_test"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="workflow.wrk_test",
        secrets={},
        workspace_root=None,
        path_policy=None,
        tool_call_id=None,
    )


@pytest.fixture
def _providers(tmp_path: Path) -> tuple[DotenvSecretsProvider, NativeSandboxProvider]:
    """Real providers, per the pattern in tests/component/test_tools.py."""
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return (
        DotenvSecretsProvider(root=tmp_path, config={"path": ".env"}),
        NativeSandboxProvider(),
    )


def _rule(what: str, effect: str = "allow") -> Rule:
    return Rule(what=what, pattern="*", effect=effect)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_forwards_workflow_permissions_as_first_layer(
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    """Workflow layer must be FIRST when workflow_permissions is supplied.

    The provider's docstring (``src/providers/authz.py:813-822``)
    locks the ordering: workflow layer FIRST when the proximate
    caller frame is a workflow node. This regression test pins that.
    """
    secrets, sandbox = _providers
    authz = _RecordingAuthz()
    wf_rule = _rule("kolsch/tools/bash")

    await dispatch_tool(
        defn=_make_defn(),
        args={},
        context=_ctx(),
        authz=authz,
        secrets=secrets,
        sandbox=sandbox,
        workflow_permissions=[wf_rule],
    )

    assert len(authz.layers_seen) == 1
    (layer,) = authz.layers_seen
    assert layer.name == "workflow"
    assert layer.rules == (wf_rule,)


@pytest.mark.asyncio
async def test_dispatch_orders_workflow_agent_bundle_layers(
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    """Proximate-first: workflow → agent → bundle."""
    secrets, sandbox = _providers
    authz = _RecordingAuthz()

    await dispatch_tool(
        defn=_make_defn(),
        args={},
        context=_ctx(),
        authz=authz,
        secrets=secrets,
        sandbox=sandbox,
        workflow_permissions=[_rule("w")],
        agent_permissions=[_rule("a")],
        bundle_permissions=[_rule("b")],
    )

    names = [layer.name for layer in authz.layers_seen]
    assert names == ["workflow", "agent", "bundle"], (
        f"Layer order must be proximate-first (workflow → agent → bundle); got {names}"
    )


@pytest.mark.asyncio
async def test_dispatch_no_workflow_permissions_preserves_prior_shape(
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    """Omitting workflow_permissions must produce the pre-fix layer tuple.

    Guards against a regression on the agent-invocation path — that
    path never passes workflow_permissions and must see the same
    layers tuple it saw before the fix.
    """
    secrets, sandbox = _providers
    authz = _RecordingAuthz()

    await dispatch_tool(
        defn=_make_defn(),
        args={},
        context=_ctx(),
        authz=authz,
        secrets=secrets,
        sandbox=sandbox,
        agent_permissions=[_rule("a")],
        bundle_permissions=[_rule("b")],
    )

    names = [layer.name for layer in authz.layers_seen]
    assert names == ["agent", "bundle"]
    assert not any(layer.name == "workflow" for layer in authz.layers_seen)


@pytest.mark.asyncio
async def test_dispatch_no_layers_at_all_when_all_perms_omitted(
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    """Bare programmatic-test path — no layers at all."""
    secrets, sandbox = _providers
    authz = _RecordingAuthz()

    await dispatch_tool(
        defn=_make_defn(),
        args={},
        context=_ctx(),
        authz=authz,
        secrets=secrets,
        sandbox=sandbox,
    )

    assert authz.layers_seen == ()
