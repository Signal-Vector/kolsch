"""spec-001 Test 2 — service field injection based on `requires`.

Exercises `dispatch_tool(chat_services=...)`. The tool body captures
its `ToolContext` into a shared cell so the test can inspect which
fields the dispatcher populated. Uses the same recording-authz
double + real providers pattern as
`tests/unit/test_dispatch_workflow_permissions.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest
from pydantic import BaseModel

from cli.chat.service_registry import ChatServiceRegistry
from providers import DotenvSecretsProvider, NativeSandboxProvider
from schemas import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext, ToolDefinition
from tools.dispatch import dispatch_tool

if TYPE_CHECKING:
    from pathlib import Path

    from providers.authz import AuthorizationProvider


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


class _EmptyArgs(BaseModel):
    pass


def _make_defn(
    *,
    requires: list[str] | None = None,
    ctx_sink: dict[str, ToolContext],
) -> ToolDefinition:
    async def _capture_tool(args: _EmptyArgs, ctx: ToolContext) -> dict[str, Any]:
        ctx_sink["ctx"] = ctx
        return {"ok": True}

    return ToolDefinition(
        name="capture",
        description="captures its ToolContext into a sink dict for the test",
        args_schema=_EmptyArgs,
        func=_capture_tool,
        home_bundle="kolsch",
        requires=list(requires or []),  # type: ignore[arg-type]
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
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return (
        DotenvSecretsProvider(root=tmp_path, config={"path": ".env"}),
        NativeSandboxProvider(),
    )


@pytest.mark.asyncio
async def test_requires_cards_populates_only_cards(
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    """A tool with `requires=["cards"]` sees `ctx.cards` set and the other three `None`."""
    secrets, sandbox = _providers
    authz = _RecordingAuthz()
    sink: dict[str, ToolContext] = {}
    sentinel = object()
    registry = ChatServiceRegistry(cards=sentinel)

    await dispatch_tool(
        defn=_make_defn(requires=["cards"], ctx_sink=sink),
        args={},
        context=_ctx(),
        authz=cast("AuthorizationProvider", authz),
        secrets=secrets,
        sandbox=sandbox,
        chat_services=registry,
    )

    seen = sink["ctx"]
    assert seen.cards is sentinel
    assert seen.prompts is None
    assert seen.workflows is None
    assert seen.session is None


@pytest.mark.asyncio
async def test_headless_dispatch_leaves_all_service_fields_none(
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    """`chat_services=None` — every service field on the ctx stays `None`."""
    secrets, sandbox = _providers
    authz = _RecordingAuthz()
    sink: dict[str, ToolContext] = {}

    await dispatch_tool(
        defn=_make_defn(requires=["cards"], ctx_sink=sink),
        args={},
        context=_ctx(),
        authz=cast("AuthorizationProvider", authz),
        secrets=secrets,
        sandbox=sandbox,
        chat_services=None,
    )

    seen = sink["ctx"]
    assert seen.cards is None
    assert seen.prompts is None
    assert seen.workflows is None
    assert seen.session is None


@pytest.mark.asyncio
async def test_undeclared_requires_leaves_all_fields_none_even_with_registry(
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    """`requires=[]` + full registry — undeclared caps stay unset."""
    secrets, sandbox = _providers
    authz = _RecordingAuthz()
    sink: dict[str, ToolContext] = {}
    registry = ChatServiceRegistry(
        cards=object(),
        prompts=object(),
        workflows=object(),
        session=object(),
    )

    await dispatch_tool(
        defn=_make_defn(requires=[], ctx_sink=sink),
        args={},
        context=_ctx(),
        authz=cast("AuthorizationProvider", authz),
        secrets=secrets,
        sandbox=sandbox,
        chat_services=registry,
    )

    seen = sink["ctx"]
    assert seen.cards is None
    assert seen.prompts is None
    assert seen.workflows is None
    assert seen.session is None
