"""spec-012 Test 4 — `cancel` dual-path.

Four passes:

1. `cancel(run_id)` on a running run: fires the in-process token,
   writes `cancel_flag_path`, returns True.
2. `cancel(unknown_id)` returns False.
3. `cancel(terminal_id)` returns False.
4. Bound card transitions to `cancelling` after `cancel()`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from cli.chat.services.cards_impl import CardsImpl
from cli.chat.services.workflows_impl import WorkflowsImpl
from schemas.events import EventType
from schemas.wire_kinds import WIRE_CARD_STATUS
from store import SessionStore, SqliteBackend
from tests.cli.chat.services.mocks import (
    MockExecutorSpec,
    install_mock_executor_factory,
)
from workflows.events import (
    WorkflowEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunStartedEvent,
)
from workflows.ids import TraceId, WorkflowRunId
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


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


async def _mk_impl(
    tmp_path: Path,
    store: SessionStore,
) -> tuple[WorkflowsImpl, CardsImpl]:
    session = await store.create_session("ag_test", name="w")
    threads = await store.list_threads(session.session_id)
    run_store = WorkflowRunStore(tmp_path)
    cards = CardsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )
    impl = WorkflowsImpl(
        runtime=None,
        cards=cards,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=run_store,
    )
    return impl, cards


def _mk_events() -> list[WorkflowEvent]:
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    tr = TraceId(f"tr_{uuid4()}")
    return [
        WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=tr),
        WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=tr),
    ]


async def test_cancel_running_fires_token_and_writes_flag(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl, cards = await _mk_impl(tmp_path, store)
    try:
        # Use a slow mock so we can catch it mid-run.
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/x": MockExecutorSpec(
                    events=_mk_events(),
                    per_event_delay=0.5,
                ),
            },
            runs_root=tmp_path / "runs",
        )
        rid = await impl.start("kolsch/workflows/x", {})
        # Wait until run_dir populated (post-executor construction).
        for _ in range(40):
            await asyncio.sleep(0.05)
            rec = impl._workflow_registry.get(rid)
            if rec is not None and rec.run_dir is not None:
                break
        rec = impl._workflow_registry[rid]
        assert rec.run_dir is not None
        token = impl._workflow_cancels[rid]

        ok = await impl.cancel(rid)
        assert ok is True
        assert token.is_cancelled()
        assert rec.run_dir.cancel_flag_path.exists()
        assert rec.run_dir.cancel_flag_path.read_text() == "user_cancel"
    finally:
        await impl.aclose()
        await cards.aclose()


async def test_cancel_unknown_returns_false(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl, cards = await _mk_impl(tmp_path, store)
    try:
        stray = WorkflowRunId(f"wrk_{uuid4()}")
        ok = await impl.cancel(stray)
        assert ok is False
    finally:
        await impl.aclose()
        await cards.aclose()


async def test_cancel_terminal_returns_false(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl, cards = await _mk_impl(tmp_path, store)
    try:
        install_mock_executor_factory(
            impl,
            {"kolsch/workflows/x": MockExecutorSpec(events=_mk_events())},
            runs_root=tmp_path / "runs",
        )
        rid = await impl.start("kolsch/workflows/x", {})
        for _ in range(60):
            await asyncio.sleep(0.05)
            if impl.status(rid).phase == "success":
                break
        assert impl.status(rid).phase == "success"
        ok = await impl.cancel(rid)
        assert ok is False
    finally:
        await impl.aclose()
        await cards.aclose()


async def test_cancel_transitions_card_status(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl, cards = await _mk_impl(tmp_path, store)
    try:
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/x": MockExecutorSpec(
                    events=_mk_events(),
                    per_event_delay=0.5,
                ),
            },
            runs_root=tmp_path / "runs",
        )
        card = await cards.open("workflow test", kind="workflow")
        rid = await impl.start(
            "kolsch/workflows/x",
            {},
            card=card,
        )
        # Wait until run_dir populated.
        for _ in range(40):
            await asyncio.sleep(0.05)
            rec = impl._workflow_registry.get(rid)
            if rec is not None and rec.run_dir is not None:
                break
        await impl.cancel(rid)
        # A card.status event with status="cancelling" landed on the store.
        found = False
        async for ev in store.read_events(cards._thread_id):
            if ev.type != EventType.UI_EVENT:
                continue
            data = ev.data if isinstance(ev.data, dict) else {}
            if data.get("kind") != WIRE_CARD_STATUS:
                continue
            payload = data.get("payload") or {}
            if payload.get("status") == "cancelling":
                found = True
                break
        assert found, "expected a card.status=cancelling UI event"
    finally:
        await impl.aclose()
        await cards.aclose()
