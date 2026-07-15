"""spec-014 Test 2 — `WorkflowsImpl.emit_awaiting_approval` durable.

Three passes:

1. `emit_awaiting_approval(run_id, node_name)` appends a
   `WIRE_WORKFLOW_NODE_AWAITING_APPROVAL` UI event to the session
   store with the matching payload.
2. `emit_approval_resolved(run_id, node_name, resolution)` appends
   a `WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED` UI event with the
   matching payload.
3. Both emits use the critical lane semantics — durability is via
   `SessionStore.append_event` so the event round-trips through the
   store's read path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from cli.chat.services.workflows_impl import WorkflowsImpl
from schemas.events import EventType
from schemas.wire_kinds import (
    WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED,
    WIRE_WORKFLOW_NODE_AWAITING_APPROVAL,
)
from store import SessionStore, SqliteBackend
from workflows.run_store import WorkflowRunStore


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
) -> tuple[WorkflowsImpl, str]:
    session = await store.create_session("ag_test", name="w")
    threads = await store.list_threads(session.session_id)
    run_store = WorkflowRunStore(tmp_path)
    impl = WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=run_store,
    )
    return impl, threads[0].thread_id


async def _read_ui_events(
    store: SessionStore,
    thread_id: str,
    kind: str,
) -> list[dict]:
    """Collect every UI_EVENT of `kind` from the store's read path."""
    hits: list[dict] = []
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") == kind:
            payload = data.get("payload")
            if isinstance(payload, dict):
                hits.append(payload)
    return hits


@pytest.mark.asyncio
async def test_emit_awaiting_approval_appends_ui_event(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """`emit_awaiting_approval` appends WIRE_WORKFLOW_NODE_AWAITING_APPROVAL."""
    impl, thread_id = await _mk_impl(tmp_path, store)
    await impl.emit_awaiting_approval(
        run_id="r1",
        node_name="n1",
    )
    hits = await _read_ui_events(
        store,
        thread_id,
        WIRE_WORKFLOW_NODE_AWAITING_APPROVAL,
    )
    assert len(hits) == 1
    payload = hits[0]
    assert payload["run_id"] == "r1"
    assert payload["node_name"] == "n1"


@pytest.mark.asyncio
async def test_emit_approval_resolved_appends_ui_event(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """`emit_approval_resolved` appends WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED."""
    impl, thread_id = await _mk_impl(tmp_path, store)
    await impl.emit_approval_resolved(
        run_id="r1",
        node_name="n1",
        resolution="approved",
    )
    hits = await _read_ui_events(
        store,
        thread_id,
        WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED,
    )
    assert len(hits) == 1
    payload = hits[0]
    assert payload["run_id"] == "r1"
    assert payload["node_name"] == "n1"
    assert payload["resolution"] == "approved"


@pytest.mark.asyncio
async def test_emit_approval_resolved_resolutions_variants(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """`resolution` accepts approved/denied/cancelled — all persist verbatim."""
    impl, thread_id = await _mk_impl(tmp_path, store)
    for resolution in ("approved", "denied", "cancelled"):
        await impl.emit_approval_resolved(
            run_id="r1",
            node_name=f"n_{resolution}",
            resolution=resolution,
        )
    hits = await _read_ui_events(
        store,
        thread_id,
        WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED,
    )
    assert len(hits) == 3
    assert {h["resolution"] for h in hits} == {
        "approved",
        "denied",
        "cancelled",
    }


@pytest.mark.asyncio
async def test_emit_awaiting_approval_uses_critical_lane(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """Critical lane = durable append; event has non-zero logical_seq."""
    impl, thread_id = await _mk_impl(tmp_path, store)
    await impl.emit_awaiting_approval(
        run_id="r1",
        node_name="n1",
    )
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") != WIRE_WORKFLOW_NODE_AWAITING_APPROVAL:
            continue
        # spec-003 stamps logical_seq inside the flock critical
        # section; non-zero after append.
        seq = data.get("logical_seq")
        assert isinstance(seq, int)
        assert seq > 0
        return
    raise AssertionError("no WIRE_WORKFLOW_NODE_AWAITING_APPROVAL event found")
