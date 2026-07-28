"""spec-017 Test 1 + Test 2 — nested detection + peer semantics.

Test 1 (`test_parent_populated`) verifies the contextvar-based
nested detection:

- Top-level `start()` — record.parent_run_id is None.
- `start()` fired from inside another live run's task —
  record.parent_run_id equals the outer run's id; the reverse
  `_children_of` map records the child on the parent's entry.

Test 2 (`test_peer_no_parent`) documents the peer-vs-nested
ambiguity (spec-017 Q3 lock): a tool that calls `Workflows.start()`
from inside a workflow's node also looks like nested to the
contextvar. A top-level `start()` from outside any workflow
correctly returns None.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from cli.chat.services.workflows_impl import (
    WorkflowsImpl,
    _current_workflow_run_id,
)
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


def _mk_events() -> list[WorkflowEvent]:
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    tr = TraceId(f"tr_{uuid4()}")
    return [
        WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=tr),
        WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=tr),
    ]


async def test_parent_populated(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """Top-level → parent=None; nested-start → parent=outer id.

    The nested branch is exercised by having the mock executor's
    `on_construct` callback call `impl.start()` for the child ref,
    which lands INSIDE the outer run's `_run_one_workflow` frame
    where the contextvar is set. This mirrors how
    `kolsch/nodes/workflow` would invoke a sub-workflow via
    `Workflows.start` from a tool.
    """
    impl = await _mk_impl(tmp_path, store)
    try:
        # Track the child id for a follow-up assert.
        child_id_box: dict[str, WorkflowRunId] = {}

        async def _spawn_child_from_outer() -> None:
            child_id = await impl.start("kolsch/workflows/child", {})
            child_id_box["id"] = child_id

        def _on_outer_construct(_run_dir_name: str) -> None:
            # `on_construct` fires SYNC inside the outer executor's
            # factory-build. Schedule the child start on the running
            # loop so it lands INSIDE the outer run's task context
            # (contextvar is set for the whole `_run_one_workflow`
            # body). Using `create_task` guarantees the child's
            # `start()` runs while the outer executor is still
            # awaiting its own run.
            loop = asyncio.get_running_loop()
            loop.create_task(_spawn_child_from_outer())  # noqa: RUF006 — deliberate fire-and-forget spawn

        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/outer": MockExecutorSpec(
                    events=_mk_events(),
                    per_event_delay=0.15,
                    on_construct=_on_outer_construct,
                ),
                "kolsch/workflows/child": MockExecutorSpec(
                    events=_mk_events(),
                ),
            },
            runs_root=tmp_path / "runs",
        )

        outer_id = await impl.start("kolsch/workflows/outer", {})
        # Top-level: parent must be None immediately after `start()`.
        assert impl._workflow_registry[outer_id].parent_run_id is None

        # Wait for the child to spawn from inside the outer's task.
        for _ in range(60):
            await asyncio.sleep(0.05)
            if "id" in child_id_box:
                break
        assert "id" in child_id_box, "child never spawned from outer"
        child_id = child_id_box["id"]

        # Child's parent_run_id must be the outer's run_id.
        child_rec = impl._workflow_registry[child_id]
        assert child_rec.parent_run_id == outer_id, (
            f"expected parent_run_id={outer_id}, got {child_rec.parent_run_id}"
        )

        # Reverse map must carry the child on the outer's entry.
        assert outer_id in impl._children_of
        assert child_id in impl._children_of[outer_id]

        # Wait for both to run to completion.
        for _ in range(120):
            await asyncio.sleep(0.05)
            phase_outer = impl.status(outer_id).phase
            phase_child = impl.status(child_id).phase
            if phase_outer == "success" and phase_child == "success":
                break
        assert impl.status(outer_id).phase == "success"
        assert impl.status(child_id).phase == "success"
    finally:
        await impl.aclose()


async def test_peer_no_parent(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """Top-level `start()` outside any workflow → parent_run_id=None.

    Complements Test 1's nested-detection pass by proving that a
    plain top-level `start()` fired from outside any workflow's
    frame doesn't accidentally pick up a stale contextvar value.
    Also documents the spec-017 Q3 lock: a peer `start()` fired
    from inside an outer workflow's node's tool WOULD look like
    nested (the contextvar is set from `_run_one_workflow`), and
    that's the documented behaviour.
    """
    impl = await _mk_impl(tmp_path, store)
    try:
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/top": MockExecutorSpec(
                    events=_mk_events(),
                ),
            },
            runs_root=tmp_path / "runs",
        )

        # Baseline: `_current_workflow_run_id` is None outside any
        # `_run_one_workflow` frame.
        assert _current_workflow_run_id.get() is None

        # Top-level start — parent must be None.
        top_id = await impl.start("kolsch/workflows/top", {})
        assert impl._workflow_registry[top_id].parent_run_id is None
        # The reverse map has no entry for a top-level start.
        assert top_id not in impl._children_of

        # Documented peer-vs-nested ambiguity (Q3 lock): a start
        # fired while the contextvar is set (from a hypothetical
        # `_run_one_workflow` context) would look nested. We prove
        # the contextvar semantics directly rather than driving
        # through the executor.
        token = _current_workflow_run_id.set(top_id)
        try:
            # Simulate a peer / nested start from inside an outer
            # run's task — parent_run_id lands as `top_id` because
            # the contextvar is set, regardless of the caller's
            # intent (Q3 lock).
            peer_id = await impl.start("kolsch/workflows/top", {})
            assert impl._workflow_registry[peer_id].parent_run_id == top_id
        finally:
            _current_workflow_run_id.reset(token)

        # Post-reset, a fresh top-level start goes back to
        # parent_run_id=None.
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/top": MockExecutorSpec(
                    events=_mk_events(),
                ),
                "kolsch/workflows/other": MockExecutorSpec(
                    events=_mk_events(),
                ),
            },
            runs_root=tmp_path / "runs",
        )
        other_id = await impl.start("kolsch/workflows/other", {})
        assert impl._workflow_registry[other_id].parent_run_id is None
    finally:
        await impl.aclose()
