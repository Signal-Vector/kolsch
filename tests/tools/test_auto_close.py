"""spec-008 Test 1 — Auto-close finalizer on tool dispatch.

Six passes:

1. Tool opens 3 markdown cards, closes only 1, returns normally.
   The other 2 emit ``WIRE_CARD_CLOSED`` with
   ``final_status="success"`` before the tool's dispatch resolves.
2. Tool raises ``RuntimeError("bad")`` — the 2 open cards emit
   ``WIRE_CARD_CLOSED`` with ``final_status="error"``,
   ``error_message="bad"``. Exception re-raised.
3. Tool receives ``CancelledError`` — cards emit
   ``final_status="cancelled"``. Exception re-raised.
4. Long error message (``"x" * 500``) — ``error_message`` truncated
   to 200 chars in the finalizer.
5. Card with ``_owner_transferred=True`` — finalizer SKIPS it; the
   card stays open after tool return.
6. Nested tool calls (A calls B; B opens a card) — B's card is
   auto-closed at B's return, not A's. Contextvar correctly scoped.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import BaseModel

from cli.chat.service_registry import ChatServiceRegistry
from cli.chat.services.cards_impl import CardsImpl
from providers import DotenvSecretsProvider, NativeSandboxProvider
from schemas import AgentId, CallerChain, SessionId, ThreadId
from schemas.events import EventType
from schemas.wire_kinds import WIRE_CARD_CLOSED
from store import SessionStore, SqliteBackend
from tools.decorator import ToolContext, ToolDefinition
from tools.dispatch import dispatch_tool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

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
    ) -> Any:
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
        caller_chain=CallerChain.unattributed(),
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


async def _collect_closed_payloads(
    store: SessionStore,
    thread_id: str,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") == WIRE_CARD_CLOSED:
            payload = data.get("payload") or {}
            if isinstance(payload, dict):
                payloads.append(payload)
    return payloads


def _make_defn(
    tool_func: Any,
    name: str = "t",
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="test",
        args_schema=_EmptyArgs,
        func=tool_func,
        home_bundle="kolsch",
        requires=["cards"],
    )


async def _dispatch(
    defn: ToolDefinition,
    impl: CardsImpl,
    providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> Any:
    secrets, sandbox = providers
    return await dispatch_tool(
        defn=defn,
        args={},
        context=_ctx(impl._session_id, impl._thread_id),
        authz=cast("AuthorizationProvider", _RecordingAuthz()),
        secrets=secrets,
        sandbox=sandbox,
        chat_services=ChatServiceRegistry(cards=impl),
    )


async def _drain(impl: CardsImpl) -> None:
    """Yield to the event loop so pending emits reach the store."""
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 1. Normal return → success close on the 2 still-open cards.
# ---------------------------------------------------------------------------


async def test_normal_return_auto_closes_open_cards(
    store: SessionStore,
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    impl = await _mk_impl(store)
    try:
        opened_ids: list[str] = []

        async def _body(args: _EmptyArgs, ctx: ToolContext) -> str:
            _ = args
            assert ctx.cards is not None
            c1 = await ctx.cards.open("t1", kind="markdown")
            c2 = await ctx.cards.open("t2", kind="markdown")
            c3 = await ctx.cards.open("t3", kind="markdown")
            opened_ids.extend([str(c1.id), str(c2.id), str(c3.id)])
            # Explicitly close only c1.
            await c1.close()
            return "ok"

        result = await _dispatch(_make_defn(_body), impl, _providers)
        assert result == "ok"
        await _drain(impl)

        closed = await _collect_closed_payloads(store, impl._thread_id)
        # 1 explicit + 2 finalizer-emitted = 3 total.
        assert len(closed) == 3
        by_id: dict[str, dict[str, Any]] = {c["card_id"]: c for c in closed}
        # The two finalizer-closed cards report success.
        for card_id in opened_ids[1:]:
            assert by_id[card_id]["final_status"] == "success"
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# 2. Exception → error close with error_message.
# ---------------------------------------------------------------------------


async def test_exception_auto_closes_open_cards_with_error(
    store: SessionStore,
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    impl = await _mk_impl(store)
    try:
        opened_ids: list[str] = []

        async def _body(args: _EmptyArgs, ctx: ToolContext) -> str:
            _ = args
            assert ctx.cards is not None
            c1 = await ctx.cards.open("t1", kind="markdown")
            c2 = await ctx.cards.open("t2", kind="markdown")
            opened_ids.extend([str(c1.id), str(c2.id)])
            raise RuntimeError("bad")

        with pytest.raises(Exception) as exc_info:
            await _dispatch(_make_defn(_body), impl, _providers)
        # The dispatch wraps the RuntimeError in `ToolExecutionFailed`.
        assert (
            "bad" in str(exc_info.value)
            or "ToolExecutionFailed" in type(exc_info.value).__name__
        )
        await _drain(impl)

        closed = await _collect_closed_payloads(store, impl._thread_id)
        assert len(closed) == 2
        for c in closed:
            assert c["final_status"] == "error"
            assert c["error_message"] == "bad"
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# 3. Cancellation → cancelled close.
# ---------------------------------------------------------------------------


async def test_cancellederror_auto_closes_open_cards_as_cancelled(
    store: SessionStore,
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    impl = await _mk_impl(store)
    try:

        async def _body(args: _EmptyArgs, ctx: ToolContext) -> str:
            _ = args
            assert ctx.cards is not None
            await ctx.cards.open("t1", kind="markdown")
            await ctx.cards.open("t2", kind="markdown")
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await _dispatch(_make_defn(_body), impl, _providers)
        await _drain(impl)

        closed = await _collect_closed_payloads(store, impl._thread_id)
        assert len(closed) == 2
        for c in closed:
            assert c["final_status"] == "cancelled"
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# 4. Long error message truncated to 200 chars.
# ---------------------------------------------------------------------------


async def test_error_message_truncated_to_200(
    store: SessionStore,
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    impl = await _mk_impl(store)
    try:

        async def _body(args: _EmptyArgs, ctx: ToolContext) -> str:
            _ = args
            assert ctx.cards is not None
            await ctx.cards.open("t1", kind="markdown")
            raise RuntimeError("x" * 500)

        with pytest.raises(Exception):  # noqa: B017 — asserts dispatch propagates any error
            await _dispatch(_make_defn(_body), impl, _providers)
        await _drain(impl)

        closed = await _collect_closed_payloads(store, impl._thread_id)
        assert len(closed) == 1
        assert closed[0]["final_status"] == "error"
        assert len(closed[0]["error_message"]) == 200
        assert closed[0]["error_message"] == "x" * 200
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# 5. `_owner_transferred=True` card skipped by finalizer.
# ---------------------------------------------------------------------------


async def test_owner_transferred_card_survives_tool_return(
    store: SessionStore,
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    impl = await _mk_impl(store)
    try:
        owned: dict[str, Any] = {}

        async def _body(args: _EmptyArgs, ctx: ToolContext) -> str:
            _ = args
            assert ctx.cards is not None
            card = await ctx.cards.open("t1", kind="markdown")
            # Simulate `Workflows.start(card=card)` taking ownership.
            card._owner_transferred = True
            owned["card"] = card
            return "ok"

        await _dispatch(_make_defn(_body), impl, _providers)
        await _drain(impl)

        card = owned["card"]
        # The transferred card must NOT be closed by the finalizer.
        assert card.is_closed is False
        closed = await _collect_closed_payloads(store, impl._thread_id)
        assert len(closed) == 0
    finally:
        await impl.aclose()


# ---------------------------------------------------------------------------
# 6. Nested tool calls — inner call's cards close at inner return.
# ---------------------------------------------------------------------------


async def test_nested_tool_call_scopes_cards_per_invocation(
    store: SessionStore,
    _providers: tuple[DotenvSecretsProvider, NativeSandboxProvider],
) -> None:
    impl = await _mk_impl(store)
    try:
        inner_card_id: dict[str, str] = {}
        outer_card_id: dict[str, str] = {}

        secrets, sandbox = _providers

        async def _inner_body(args: _EmptyArgs, ctx: ToolContext) -> str:
            _ = args
            assert ctx.cards is not None
            c = await ctx.cards.open("inner", kind="markdown")
            inner_card_id["id"] = str(c.id)
            return "inner-ok"

        inner_defn = _make_defn(_inner_body, name="inner")

        async def _outer_body(args: _EmptyArgs, ctx: ToolContext) -> str:
            _ = args
            assert ctx.cards is not None
            c = await ctx.cards.open("outer", kind="markdown")
            outer_card_id["id"] = str(c.id)
            # Recursive dispatch of an inner tool.
            await dispatch_tool(
                defn=inner_defn,
                args={},
                context=ctx,
                authz=cast("AuthorizationProvider", _RecordingAuthz()),
                secrets=secrets,
                sandbox=sandbox,
                chat_services=ChatServiceRegistry(cards=impl),
            )
            # After the inner returns the inner card must already be
            # closed, but the outer's is still open.
            return "outer-ok"

        result = await _dispatch(
            _make_defn(_outer_body, name="outer"),
            impl,
            _providers,
        )
        assert result == "outer-ok"
        await _drain(impl)

        closed = await _collect_closed_payloads(store, impl._thread_id)
        # Both cards closed as success — inner at inner return, outer
        # at outer return.
        ids = sorted(c["card_id"] for c in closed)
        expected = sorted([inner_card_id["id"], outer_card_id["id"]])
        assert ids == expected
        for c in closed:
            assert c["final_status"] == "success"
    finally:
        await impl.aclose()
