"""spec-017 Test 4 — `Workflows.cancel(cascade=True)` walks the tree.

Four passes:

1. A→B→C nested chain. `cancel(B, cascade=True)` cascades: A, B, C
   all transition to `cancelling`.
2. Same tree; `cancel(B, cascade=False)` only cancels B; A and C
   are untouched.
3. Terminal runs in the tree are skipped by the cascade (no crash,
   no re-cancel).
4. Unknown run_id + cascade=True returns False (empty tree).

Tests use direct registry population — spawning three linked
`_run_one_workflow` frames via the executor factory is orthogonal
to what the cascade-walk logic here exercises. The lower-level
Test 1 already covers the contextvar-driven populate path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

import pytest

from cli.chat.services.workflows_impl import (
    WorkflowRunRecord,
    WorkflowsImpl,
)
from engine.context import CancelToken
from store import SessionStore, SqliteBackend
from workflows.ids import WorkflowRunId
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


async def _mk_impl(tmp_path: Path, store: SessionStore) -> WorkflowsImpl:
    session = await store.create_session("ag_test", name="w")
    threads = await store.list_threads(session.session_id)
    run_store = WorkflowRunStore(tmp_path)
    return WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=run_store,
    )


def _install_run(
    impl: WorkflowsImpl,
    *,
    ref: str,
    parent_run_id: WorkflowRunId | None = None,
    phase: str = "running",
) -> WorkflowRunId:
    """Register a synthetic live run + its cancel token in the impl.

    Bypasses the FIFO drain — the cascade walker only reads
    `_workflow_registry` + `_children_of` + `_workflow_cancels`, so
    direct population lets the test focus on the walk logic.
    """
    run_id = WorkflowRunId(f"wrk_{uuid4()}")
    record = WorkflowRunRecord(
        run_id=run_id,
        ref=ref,
        phase=phase,  # type: ignore[arg-type]
        card=None,
        created_at=datetime.now(tz=UTC),
        parent_run_id=parent_run_id,
    )
    impl._workflow_registry[run_id] = record
    impl._workflow_cancels[run_id] = CancelToken()
    if parent_run_id is not None:
        impl._children_of.setdefault(parent_run_id, []).append(run_id)
    return run_id


async def test_cascade_true_walks_ancestors_and_descendants(
    tmp_path: Path, store: SessionStore,
) -> None:
    """A→B→C: `cancel(B, cascade=True)` cancels A, B, C."""
    impl = await _mk_impl(tmp_path, store)
    try:
        a = _install_run(impl, ref="kolsch/workflows/a")
        b = _install_run(impl, ref="kolsch/workflows/b", parent_run_id=a)
        c = _install_run(impl, ref="kolsch/workflows/c", parent_run_id=b)

        ok = await impl.cancel(b, cascade=True)
        assert ok is True

        # All three transitioned to `cancelling`.
        assert impl._workflow_registry[a].phase == "cancelling"
        assert impl._workflow_registry[b].phase == "cancelling"
        assert impl._workflow_registry[c].phase == "cancelling"

        # All three tokens fired.
        assert impl._workflow_cancels[a].is_cancelled()
        assert impl._workflow_cancels[b].is_cancelled()
        assert impl._workflow_cancels[c].is_cancelled()
    finally:
        await impl.aclose()


async def test_cascade_false_cancels_only_the_target(
    tmp_path: Path, store: SessionStore,
) -> None:
    """Same tree; `cancel(B, cascade=False)` leaves A and C alone."""
    impl = await _mk_impl(tmp_path, store)
    try:
        a = _install_run(impl, ref="kolsch/workflows/a")
        b = _install_run(impl, ref="kolsch/workflows/b", parent_run_id=a)
        c = _install_run(impl, ref="kolsch/workflows/c", parent_run_id=b)

        ok = await impl.cancel(b, cascade=False)
        assert ok is True

        # Only B transitioned.
        assert impl._workflow_registry[a].phase == "running"
        assert impl._workflow_registry[b].phase == "cancelling"
        assert impl._workflow_registry[c].phase == "running"

        # A and C tokens NOT cancelled.
        assert not impl._workflow_cancels[a].is_cancelled()
        assert impl._workflow_cancels[b].is_cancelled()
        assert not impl._workflow_cancels[c].is_cancelled()
    finally:
        await impl.aclose()


async def test_cascade_skips_terminal_runs(
    tmp_path: Path, store: SessionStore,
) -> None:
    """A→B→C where A already `success`. Cascade over B skips A, hits B+C."""
    impl = await _mk_impl(tmp_path, store)
    try:
        a = _install_run(
            impl, ref="kolsch/workflows/a", phase="success",
        )
        b = _install_run(impl, ref="kolsch/workflows/b", parent_run_id=a)
        c = _install_run(impl, ref="kolsch/workflows/c", parent_run_id=b)

        ok = await impl.cancel(b, cascade=True)
        assert ok is True

        # A stays terminal.
        assert impl._workflow_registry[a].phase == "success"
        # B + C got cascaded.
        assert impl._workflow_registry[b].phase == "cancelling"
        assert impl._workflow_registry[c].phase == "cancelling"
        # A's token was NOT re-cancelled.
        assert not impl._workflow_cancels[a].is_cancelled()
    finally:
        await impl.aclose()


async def test_cascade_unknown_run_id_returns_false(
    tmp_path: Path, store: SessionStore,
) -> None:
    """Unknown run_id + cascade=True → returns False (no work)."""
    impl = await _mk_impl(tmp_path, store)
    try:
        stray = WorkflowRunId(f"wrk_{uuid4()}")
        ok = await impl.cancel(stray, cascade=True)
        assert ok is False
        # Also non-cascade path stays False for unknowns.
        ok_no = await impl.cancel(stray, cascade=False)
        assert ok_no is False
    finally:
        await impl.aclose()


async def test_collect_cancel_tree_returns_full_walk(
    tmp_path: Path, store: SessionStore,
) -> None:
    """`_collect_cancel_tree(B)` on A→B→C returns {A, B, C}.

    Belt-and-braces on AC #6 — validates the helper directly so a
    regression in the walk logic surfaces before the higher-level
    cancel path masks it.
    """
    impl = await _mk_impl(tmp_path, store)
    try:
        a = _install_run(impl, ref="kolsch/workflows/a")
        b = _install_run(impl, ref="kolsch/workflows/b", parent_run_id=a)
        c = _install_run(impl, ref="kolsch/workflows/c", parent_run_id=b)

        collected = set(impl._collect_cancel_tree(b))
        assert collected == {a, b, c}

        # Walking from A also collects all three (A is root of the tree).
        collected_from_a = set(impl._collect_cancel_tree(a))
        assert collected_from_a == {a, b, c}

        # Unknown id → empty list.
        stray = WorkflowRunId(f"wrk_{uuid4()}")
        assert impl._collect_cancel_tree(stray) == []
    finally:
        await impl.aclose()
