"""bug-020 Test 1 — `ToolsSubsystem.set_chat_services` + forwarding.

Exercises AC #1 (setter round-trip), AC #2 (forwarding into
`dispatch_tool`), and the backward-compat path (absent registry —
every service field on the resulting `ToolContext` stays `None`).

The forwarding assertion patches `tools.subsystem.dispatch_tool`
with an async spy so the test asserts against the kwarg the
subsystem sends into the dispatcher — the dispatcher's own service
population is exercised end-to-end by
``tests/tools/test_service_injection.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from cli.chat.service_registry import ChatServiceRegistry
from providers import DotenvSecretsProvider, NativeSandboxProvider
from providers.authz import RuleBasedAuthorizationProvider
from schemas import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext, ToolDefinition
from tools.subsystem import ToolsSubsystem

if TYPE_CHECKING:
    from pathlib import Path


class _EmptyArgs(BaseModel):
    pass


async def _capture_tool(args: _EmptyArgs, ctx: ToolContext) -> dict[str, Any]:
    _ = args, ctx
    return {"ok": True}


def _make_defn() -> ToolDefinition:
    return ToolDefinition(
        name="capture",
        description="records the ToolContext for the test",
        args_schema=_EmptyArgs,
        func=_capture_tool,
        home_bundle="kolsch",
    )


def _ctx() -> ToolContext:
    return ToolContext(
        agent_id=AgentId("ag_test"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="user",
        secrets={},
    )


@pytest.fixture
def _subsystem(tmp_path: Path) -> ToolsSubsystem:
    """Build a `ToolsSubsystem` with real providers rooted at tmp."""
    (tmp_path / ".env").write_text("", encoding="utf-8")
    secrets = DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
    sandbox = NativeSandboxProvider()
    # Empty rules -> default-allow (the empty ruleset falls through
    # to no matching layer; the two-layer default-deny only applies
    # when a rule set is present).
    authz = RuleBasedAuthorizationProvider(
        config={"rules": []},
        catalog=None,  # type: ignore[arg-type]
    )
    return ToolsSubsystem(
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        root=tmp_path,
    )


def test_set_chat_services_stores_registry(
    _subsystem: ToolsSubsystem,
) -> None:
    """AC #1 — the setter round-trips a `ChatServiceRegistry` onto the impl."""
    assert _subsystem._chat_services is None  # default
    sentinel_cards = object()
    registry = ChatServiceRegistry(cards=sentinel_cards)

    _subsystem.set_chat_services(registry)

    assert _subsystem._chat_services is registry
    assert _subsystem._chat_services.cards is sentinel_cards


def test_set_chat_services_none_detaches(
    _subsystem: ToolsSubsystem,
) -> None:
    """Setting `None` after a registry detaches (used by `on_unmount`)."""
    _subsystem.set_chat_services(ChatServiceRegistry())
    assert _subsystem._chat_services is not None
    _subsystem.set_chat_services(None)
    assert _subsystem._chat_services is None


@dataclass
class _DispatchSpy:
    """Async spy — records the kwargs it was called with."""

    kwargs: dict[str, Any] | None = None

    async def __call__(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        # Return a plausible tool-result shape; the subsystem wraps
        # this in a `ToolResult` and we don't care about the value.
        return {"ok": True}


@pytest.mark.asyncio
async def test_invoke_tool_forwards_chat_services(
    _subsystem: ToolsSubsystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #2 — `invoke_tool` forwards the registry as `chat_services=` into `dispatch_tool`."""
    # Register a bare capture tool for the agent id we're about to
    # invoke against.
    agent_id = AgentId("ag_test")
    reg = _subsystem._registry.ensure_agent(agent_id)
    reg.tools["capture"] = _make_defn()

    # Install the sidecar registry.
    sentinel_cards = object()
    chat_registry = ChatServiceRegistry(cards=sentinel_cards)
    _subsystem.set_chat_services(chat_registry)

    # Patch the module-level `dispatch_tool` symbol the subsystem
    # imported at load time.
    spy = _DispatchSpy()
    monkeypatch.setattr("tools.subsystem.dispatch_tool", spy)

    await _subsystem.invoke_tool("capture", {}, _ctx())

    assert spy.kwargs is not None
    assert "chat_services" in spy.kwargs
    assert spy.kwargs["chat_services"] is chat_registry


@pytest.mark.asyncio
async def test_invoke_tool_without_setter_leaves_ctx_services_none(
    _subsystem: ToolsSubsystem,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward-compat — absent registry, `chat_services=None` is forwarded."""
    agent_id = AgentId("ag_test")
    reg = _subsystem._registry.ensure_agent(agent_id)
    reg.tools["capture"] = _make_defn()

    # Deliberately do NOT call set_chat_services; default is None.
    spy = _DispatchSpy()
    monkeypatch.setattr("tools.subsystem.dispatch_tool", spy)

    await _subsystem.invoke_tool("capture", {}, _ctx())

    assert spy.kwargs is not None
    assert spy.kwargs.get("chat_services") is None
