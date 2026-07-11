"""spec-008 Test 2 — ``_current_tool_open_cards`` contextvar isolation.

Three passes:

1. Two concurrent tool calls (via ``asyncio.gather``). Each opens
   its own card. Each finalizer only closes its own card.
2. Outside a tool call (contextvar is ``None``), ``CardsImpl.open``
   does NOT append to a stale list.
3. ``_current_tool_open_cards.reset(token)`` runs in ``finally``
   even on nested exceptions — the contextvar is None again after
   a failing tool call.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, cast
from uuid import uuid4

import pytest
from pydantic import BaseModel

from cli.chat.service_registry import ChatServiceRegistry
from cli.chat.services.cards_impl import CardsImpl
from providers import DotenvSecretsProvider, NativeSandboxProvider
from schemas import AgentId, SessionId, ThreadId
from schemas.events import EventType
from schemas.wire_kinds import WIRE_CARD_CLOSED
from store import SessionStore, SqliteBackend
from tools.decorator import ToolContext, ToolDefinition
from tools.dispatch import (
    _current_tool_open_cards,
    dispatch_tool,
)

if TYPE_CHECKING:
    from providers.authz import AuthorizationProvider


class _RecordingAuthz:
    async def allow(
        self,
        principal: str,
        action: str,
        *,
        target: str | None = None,
        layers: tuple[Any, ...] = (),
        caller_chain: tuple[str, ...] = (),
        tool_call_id: str | None = None,
    ) -> Any:  # noqa: ANN401
        class _AllowResult:
            allowed = True
            matched_rule = None
            reason = "test-allow"

        return _AllowResult()


class _EmptyArgs(BaseModel):
    pass


def _ctx(session_id: SessionId, thread_id: ThreadId) -> ToolContext:
    return ToolContext(
        agent_id=AgentId("ag_test"),
        session_id=session_id,
        thread_id=thread_id,
        principal="workflow.wrk_test",
        secrets={},
        workspace_root=None,
        path_policy=None,
        tool_call_id=None,
    )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    try:
        yield SessionStore(root=tmp_path, backend=backend)
    finally:
        await backend.close()


@pytest.fixture
def _providers(tmp_path: Path) -> tuple[DotenvSecretsProvider, NativeSandboxProvider]:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return (
        DotenvSecretsProvider(root=tmp_path, config={"path": ".env"}),
        NativeSandboxProvider(),
    )


async def _mk_impl(store: SessionStore) -> CardsImpl:
    session = await store.create_session("ag_test", name="c")
    threads = await store.list_threads(session.session_id)
    return CardsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )


def _make_defn(tool_func: Any, name: str = "t") -> ToolDefinition:  # noqa: ANN401
    return ToolDefinition(
        name=name,
        description="test",
        args_schema=_EmptyArgs,
        func=tool_func,
        home_bundle="kolsch",
        requires=["cards"],
    )


async def _drain(impl: CardsImpl) -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 1. Two concurrent tool calls — each finalizer only closes its own card.
# ---------------------------------------------------------------------------


async def test_concurrent_tools_isolate_open_cards(
    store: SessionStore,
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    impl = await _mk_impl(store)
    try:
        secrets, sandbox = _providers
        gate = asyncio.Event()
        card_a_id: dict[str, str] = {}
        card_b_id: dict[str, str] = {}

        async def _body_a(args: _EmptyArgs, ctx: ToolContext) -> str:
            _ = args
            assert ctx.cards is not None
            c = await ctx.cards.open("a", kind="markdown")
            card_a_id["id"] = str(c.id)
            # Wait so both bodies are live concurrently; each has its
            # own contextvar list.
            await gate.wait()
            return "a-ok"

        async def _body_b(args: _EmptyArgs, ctx: ToolContext) -> str:
            _ = args
            assert ctx.cards is not None
            c = await ctx.cards.open("b", kind="markdown")
            card_b_id["id"] = str(c.id)
            # Release both bodies at once.
            gate.set()
            return "b-ok"

        registry = ChatServiceRegistry(cards=impl)
        ctx = _ctx(impl._session_id, impl._thread_id)
        authz = cast("AuthorizationProvider", _RecordingAuthz())

        res_a, res_b = await asyncio.gather(
            dispatch_tool(
                defn=_make_defn(_body_a, name="a"),
                args={}, context=ctx, authz=authz,
                secrets=secrets, sandbox=sandbox, chat_services=registry,
            ),
            dispatch_tool(
                defn=_make_defn(_body_b, name="b"),
                args={}, context=ctx, authz=authz,
                secrets=secrets, sandbox=sandbox, chat_services=registry,
            ),
        )
        assert res_a == "a-ok"
        assert res_b == "b-ok"
        await _drain(impl)

        # Each tool auto-closed exactly its own card.
        closed: list[dict[str, Any]] = []
        async for ev in store.read_events(impl._thread_id):
            if ev.type != EventType.UI_EVENT:
                continue
            data = ev.data if isinstance(ev.data, dict) else {}
            if data.get("kind") == WIRE_CARD_CLOSED:
                p = data.get("payload") or {}
                if isinstance(p, dict):
                    closed.append(p)
        ids = sorted(c["card_id"] for c in closed)
        expected = sorted([card_a_id["id"], card_b_id["id"]])
        assert ids == expected
        for c in closed:
            assert c["final_status"] == "success"
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# 2. Outside a tool call — CardsImpl.open does not append to stale list.
# ---------------------------------------------------------------------------


async def test_outside_tool_call_no_append(
    store: SessionStore,
) -> None:
    """Contextvar defaults to None → open() must not touch it."""
    impl = await _mk_impl(store)
    try:
        assert _current_tool_open_cards.get() is None
        # Direct open() call — no tool dispatch. Would raise if
        # ``open`` tried to append to a None list.
        card = await impl.open("t", kind="markdown")
        assert card is not None
        assert _current_tool_open_cards.get() is None
        # Because we're outside a tool call, the card is NOT auto-
        # closed. Explicit close required.
        assert card.is_closed is False
        await card.close()
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# 3. Contextvar reset in finally even on exception.
# ---------------------------------------------------------------------------


async def test_contextvar_reset_on_exception(
    store: SessionStore,
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    """After a raising tool call, the contextvar is None again."""
    impl = await _mk_impl(store)
    try:
        async def _body(args: _EmptyArgs, ctx: ToolContext) -> str:
            _ = args
            # Sanity: inside the body, contextvar IS set to a list.
            open_cards = _current_tool_open_cards.get()
            assert open_cards is not None
            assert isinstance(open_cards, list)
            raise RuntimeError("boom")

        with pytest.raises(Exception):
            await dispatch_tool(
                defn=_make_defn(_body),
                args={},
                context=_ctx(impl._session_id, impl._thread_id),
                authz=cast("AuthorizationProvider", _RecordingAuthz()),
                secrets=_providers[0],
                sandbox=_providers[1],
                chat_services=ChatServiceRegistry(cards=impl),
            )

        # After the failing dispatch, the contextvar is back to None
        # (default). The token-based reset in the finalizer's finally
        # ran despite the exception.
        assert _current_tool_open_cards.get() is None
    finally:
        await impl.aclose()
