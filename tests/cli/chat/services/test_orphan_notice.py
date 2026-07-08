"""feat-015 Test 3 — `WorkflowsImpl.fire_orphan_notice` idempotence.

Three passes:

1. After `sweep_orphans()` reconciled 2 runs, `fire_orphan_notice()`
   fires ONE `Cards.notice` with text containing `"2 workflow"`.
2. Second `fire_orphan_notice()` call fires nothing (idempotent).
3. When `sweep_orphans()` reconciles 0 runs, `fire_orphan_notice()`
   fires nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from cli.chat.services.workflows_impl import WorkflowsImpl
from store import SessionStore, SqliteBackend
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore


@dataclass
class FakeCards:
    """Minimal stub carrying the `.notice(text, *, kind=...)` seam."""

    calls: list[tuple[str, str]] = field(default_factory=list)

    def notice(
        self,
        text: str,
        *,
        kind: str = "info",
        card: Any = None,
    ) -> None:
        _ = card
        self.calls.append((text, kind))


@pytest.fixture
async def session_store(tmp_path: Path) -> AsyncIterator[SessionStore]:
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
    session_store: SessionStore,
    run_store: WorkflowRunStore,
    cards: Any,
) -> WorkflowsImpl:
    session = await session_store.create_session("ag_test", name="w")
    threads = await session_store.list_threads(session.session_id)
    return WorkflowsImpl(
        runtime=None,
        cards=cards,
        prompts=None,
        session_store=session_store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=run_store,
    )


async def test_fire_notice_after_two_orphans_reconciled(
    tmp_path: Path, session_store: SessionStore,
) -> None:
    """AC #5: 2 orphans reconciled → one notice with `"2 workflow"`."""
    run_store = WorkflowRunStore(tmp_path)
    for name in ("a", "b"):
        run = run_store.create(
            workflow_ref=f"demo/workflows/{name}",
            inputs={},
            started_by="in-process",
        )
        run_store.update_status(run.run_id, WorkflowRunStatus.RUNNING)

    cards = FakeCards()
    impl = await _mk_impl(tmp_path, session_store, run_store, cards)
    try:
        n = await impl.sweep_orphans()
        assert n == 2
        impl.fire_orphan_notice()
        assert len(cards.calls) == 1
        text, kind = cards.calls[0]
        assert "2 workflow" in text
        assert kind == "warning"
    finally:
        await impl.aclose()


async def test_fire_notice_idempotent(
    tmp_path: Path, session_store: SessionStore,
) -> None:
    """AC #5: second `fire_orphan_notice()` is a no-op."""
    run_store = WorkflowRunStore(tmp_path)
    run = run_store.create(
        workflow_ref="demo/workflows/a",
        inputs={},
        started_by="in-process",
    )
    run_store.update_status(run.run_id, WorkflowRunStatus.RUNNING)

    cards = FakeCards()
    impl = await _mk_impl(tmp_path, session_store, run_store, cards)
    try:
        assert await impl.sweep_orphans() == 1
        impl.fire_orphan_notice()
        assert len(cards.calls) == 1
        # Second call — must be a no-op.
        impl.fire_orphan_notice()
        assert len(cards.calls) == 1
    finally:
        await impl.aclose()


async def test_fire_notice_no_op_when_no_orphans(
    tmp_path: Path, session_store: SessionStore,
) -> None:
    """AC #5: zero-orphan sweep + fire → no notice fired."""
    run_store = WorkflowRunStore(tmp_path)
    # Terminal in-process run: excluded.
    done = run_store.create(
        workflow_ref="demo/workflows/done",
        inputs={},
        started_by="in-process",
    )
    run_store.update_status(done.run_id, WorkflowRunStatus.SUCCEEDED)

    cards = FakeCards()
    impl = await _mk_impl(tmp_path, session_store, run_store, cards)
    try:
        assert await impl.sweep_orphans() == 0
        impl.fire_orphan_notice()
        assert cards.calls == []
    finally:
        await impl.aclose()
