"""Coverage for :mod:`cli._workflow_chat_runner`.

Two surfaces:

- ``resolve_paused_node_name`` — a pure disk-walk unit exercised
  against a real [`WorkflowRunStore`][workflows.run_store.WorkflowRunStore]
  seeded with a ``waiting_at`` sidecar + JSONL. Every failure branch
  (missing sidecar, empty sidecar, unreadable store, no matching
  ``workflow.node.started``) resolves to ``None`` rather than raising.

- ``execute_workflow_with_renderer`` — the concurrent run+render
  orchestration, driven end-to-end against a real ``bootstrap_runtime``
  workspace: a fresh linear run reaches ``SUCCEEDED`` and streams
  capsules; a ``user_input`` workflow reaches ``WAITING_FOR_INPUT`` and
  renders the inline pause capsule; a resume completes the paused run.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from cli._slash import ChatSessionState
from cli._workflow_chat_runner import (
    execute_workflow_with_renderer,
    resolve_paused_node_name,
)
from platform_runtime import bootstrap_runtime
from workflows.parser import load_workflow
from workflows.run import WorkflowRunStatus
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from platform_runtime import PlatformRuntime
    from workflows.ids import WorkflowRunId
    from workflows.parser import Workflow


# ---------------------------------------------------------------------------
# resolve_paused_node_name — disk-walk unit.
# ---------------------------------------------------------------------------


def _seed_started_event(
    store: WorkflowRunStore,
    run_id: WorkflowRunId,
    *,
    invocation_id: str,
    node_name: str,
) -> None:
    """Append a ``workflow.node.started`` event carrying an invocation."""
    store.append_event(
        run_id,
        {
            "event_id": "ev_seeded00000000",
            "kind": "workflow.node.started",
            "invocation_id": invocation_id,
            "node_name": node_name,
            "payload": {},
        },
    )


def test_resolve_paused_node_name_finds_matching_node(tmp_path: Path) -> None:
    """With a ``waiting_at`` sidecar naming an open invocation and a
    matching ``workflow.node.started`` on the JSONL, the resolver
    returns that node's ``node_name``."""
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    _seed_started_event(store, run.run_id, invocation_id="inv_open42", node_name="ask")
    run_dir = store.directory(run.run_id)
    run_dir.waiting_at_path.write_text("inv_open42", encoding="utf-8")

    assert resolve_paused_node_name(store, run.run_id) == "ask"


def test_resolve_paused_node_name_no_sidecar_returns_none(tmp_path: Path) -> None:
    """No ``waiting_at`` sidecar on disk → ``None`` (the run is not
    paused; the chat renders no pause capsule)."""
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    _seed_started_event(store, run.run_id, invocation_id="inv_x", node_name="ask")

    assert resolve_paused_node_name(store, run.run_id) is None


def test_resolve_paused_node_name_empty_sidecar_returns_none(tmp_path: Path) -> None:
    """A ``waiting_at`` sidecar that is present but blank → ``None``."""
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    run_dir = store.directory(run.run_id)
    run_dir.waiting_at_path.write_text("   \n", encoding="utf-8")

    assert resolve_paused_node_name(store, run.run_id) is None


def test_resolve_paused_node_name_no_matching_event_returns_none(
    tmp_path: Path,
) -> None:
    """The sidecar names an invocation with no matching
    ``workflow.node.started`` on the JSONL → ``None``."""
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    _seed_started_event(store, run.run_id, invocation_id="inv_other", node_name="ask")
    run_dir = store.directory(run.run_id)
    run_dir.waiting_at_path.write_text("inv_open_but_absent", encoding="utf-8")

    assert resolve_paused_node_name(store, run.run_id) is None


def test_resolve_paused_node_name_unreadable_sidecar_returns_none(
    tmp_path: Path,
) -> None:
    """When ``read_text`` on the ``waiting_at`` sidecar raises OSError
    (here: the path is a directory, so the read is ``IsADirectoryError``),
    the resolver swallows it and returns ``None``."""
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    run_dir = store.directory(run.run_id)
    # A directory at the sidecar path exists() as True but read_text()
    # raises OSError — exercises the OSError guard.
    run_dir.waiting_at_path.mkdir(parents=True, exist_ok=True)

    assert resolve_paused_node_name(store, run.run_id) is None


def test_resolve_paused_node_name_store_directory_error_returns_none() -> None:
    """When the store's ``directory`` raises, the resolver swallows it
    and returns ``None`` (defensive against the internally-typed store
    API)."""

    class _BoomStore:
        def directory(self, _run_id: Any) -> Any:
            raise RuntimeError("no such directory")

    assert resolve_paused_node_name(_BoomStore(), "wrk_whatever") is None


def test_resolve_paused_node_name_read_events_error_returns_none(
    tmp_path: Path,
) -> None:
    """A ``waiting_at`` sidecar present, but ``read_events`` raises →
    ``None`` (the JSONL-scan step is wrapped defensively)."""
    real_store = WorkflowRunStore(tmp_path)
    run = real_store.create(workflow_ref="demo/workflows/x", inputs={})
    run_dir = real_store.directory(run.run_id)
    run_dir.waiting_at_path.write_text("inv_open", encoding="utf-8")

    class _ReadBoomStore:
        def __init__(self, inner: WorkflowRunStore) -> None:
            self._inner = inner

        def directory(self, run_id: Any) -> Any:
            return self._inner.directory(run_id)

        def read_events(self, _run_id: Any) -> Any:
            raise RuntimeError("jsonl unreadable")

    assert resolve_paused_node_name(_ReadBoomStore(real_store), run.run_id) is None


# ---------------------------------------------------------------------------
# execute_workflow_with_renderer — full orchestration against a real runtime.
# ---------------------------------------------------------------------------


async def _build_state(workspace: Path) -> tuple[ChatSessionState, PlatformRuntime]:
    """Construct a ChatSessionState bound to a fresh runtime + session.

    Mirrors the setup in the (skipped) Phase 3.9 slash suite so the
    real-runtime path is exercised the same way production wires it.
    """
    runtime = await bootstrap_runtime(root=workspace)
    config = await runtime.build_resolved_config("kolsch/agents/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="chat-runner-test",
    )
    threads = await runtime.store.list_threads(session.session_id)
    state = ChatSessionState(
        runtime=runtime,
        config=config,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )
    return state, runtime


def _resolve_workflow(runtime: PlatformRuntime, ref: str) -> Workflow:
    """Resolve + parse a workflow ref through the runtime's catalog."""
    entry = runtime.asset_management.catalog().lookup(ref, expected_type="workflow")
    assert entry is not None, f"workflow not found in catalog: {ref}"
    return load_workflow(Path(entry.path))


async def test_execute_fresh_run_streams_capsules_and_succeeds(
    chat_workspace: Path,
) -> None:
    """A fresh linear ``hello`` run drives ``executor.run()``, streams
    the running indicator + completion capsules to stdout, and returns
    ``SUCCEEDED``."""
    state, runtime = await _build_state(chat_workspace)
    store = WorkflowRunStore(runtime.root)
    workflow = _resolve_workflow(runtime, "kolsch/workflows/hello")
    run = store.create(
        workflow_ref="kolsch/workflows/hello",
        inputs={"message": "HelloFromRunner"},
    )
    out = io.StringIO()

    status = await execute_workflow_with_renderer(
        state=state,
        stdout=out,
        store=store,
        workflow=workflow,
        run=run,
    )

    assert status == WorkflowRunStatus.SUCCEEDED
    rendered = out.getvalue()
    # The running indicator streamed.
    assert "▸" in rendered
    # The bound input flowed through to the echo node's output.
    assert "HelloFromRunner" in rendered
    # Per-instance descriptions drive the capsule headers.
    assert "echoes the workflow input" in rendered


async def test_execute_paused_run_renders_inline_pause_capsule(
    chat_workspace: Path,
) -> None:
    """A ``hello-paused`` run pauses on its ``ask`` node; the helper
    returns ``WAITING_FOR_INPUT`` and renders the inline pause capsule
    (prompt text + ``→ ask``)."""
    state, runtime = await _build_state(chat_workspace)
    store = WorkflowRunStore(runtime.root)
    workflow = _resolve_workflow(runtime, "kolsch/workflows/hello-paused")
    run = store.create(
        workflow_ref="kolsch/workflows/hello-paused",
        inputs={},
    )
    out = io.StringIO()

    status = await execute_workflow_with_renderer(
        state=state,
        stdout=out,
        store=store,
        workflow=workflow,
        run=run,
    )

    assert status == WorkflowRunStatus.WAITING_FOR_INPUT
    rendered = out.getvalue()
    # The inline pause capsule surfaced the raw prompt + node arrow.
    assert "What would you like to say?" in rendered
    assert "→ ask" in rendered


async def test_execute_resume_completes_paused_run(
    chat_workspace: Path,
) -> None:
    """After a pause, calling the helper again with ``resume_response``
    drives ``executor.resume(...)`` and the run completes; the resumed
    response surfaces in the downstream echo node's output."""
    state, runtime = await _build_state(chat_workspace)
    store = WorkflowRunStore(runtime.root)
    workflow = _resolve_workflow(runtime, "kolsch/workflows/hello-paused")
    run = store.create(
        workflow_ref="kolsch/workflows/hello-paused",
        inputs={},
    )

    # First: run to the pause.
    pause_status = await execute_workflow_with_renderer(
        state=state,
        stdout=io.StringIO(),
        store=store,
        workflow=workflow,
        run=run,
    )
    assert pause_status == WorkflowRunStatus.WAITING_FOR_INPUT

    # Re-read the run so it reflects the on-disk ``waiting_for_input``
    # status the pause handler wrote (mirrors the chat resume path,
    # which reconstructs the run via ``store.read`` before resuming).
    paused_run = store.read(run.run_id)

    # Then: resume with the operator's response.
    out = io.StringIO()
    resume_status = await execute_workflow_with_renderer(
        state=state,
        stdout=out,
        store=store,
        workflow=workflow,
        run=paused_run,
        resume_response={"response": "world"},
    )

    assert resume_status == WorkflowRunStatus.SUCCEEDED
    assert "world" in out.getvalue()


async def test_execute_fork_reroots_run_from_event(
    chat_workspace: Path,
) -> None:
    """The ``fork_event_id`` mode drives ``executor.start_from_fork``:
    a forked ``hello`` run re-rooted at the ``ready`` node's completion
    event re-runs the downstream ``greet`` node, streams its capsule,
    and completes — and the forked run's JSONL opens with a
    ``workflow.run.forked_from`` event (not a fresh ``run.started``)."""
    from schemas.entities import ForkedFrom

    state, runtime = await _build_state(chat_workspace)
    store = WorkflowRunStore(runtime.root)
    workflow = _resolve_workflow(runtime, "kolsch/workflows/hello")

    # Parent run first — needed so the fork has an event to re-root at.
    parent = store.create(
        workflow_ref="kolsch/workflows/hello",
        inputs={"message": "parent-msg"},
    )
    parent_status = await execute_workflow_with_renderer(
        state=state,
        stdout=io.StringIO(),
        store=store,
        workflow=workflow,
        run=parent,
    )
    assert parent_status == WorkflowRunStatus.SUCCEEDED

    # Find the ``ready`` node's completion event id in the parent JSONL.
    ready_event_id: str | None = None
    for event in store.read_events(parent.run_id):
        if (
            event.get("kind") == "workflow.node.completed"
            and event.get("node_name") == "ready"
        ):
            ready_event_id = str(event.get("event_id"))
            break
    assert ready_event_id is not None

    # Mint the forked run carrying the parent pointer (the caller owns
    # this; the helper only drives ``start_from_fork``).
    forked = store.create(
        workflow_ref="kolsch/workflows/hello",
        trace_id=parent.trace_id,
        forked_from=ForkedFrom(
            run_id=parent.run_id,
            fork_event_id=ready_event_id,
        ),
    )
    out = io.StringIO()

    fork_status = await execute_workflow_with_renderer(
        state=state,
        stdout=out,
        store=store,
        workflow=workflow,
        run=forked,
        fork_event_id=ready_event_id,
        input_overrides={"message": "forked-msg"},
    )

    assert fork_status == WorkflowRunStatus.SUCCEEDED
    # The fork re-ran the downstream ``greet`` node and streamed its
    # capsule (per-instance description drives the header).
    assert "echoes the workflow input" in out.getvalue()
    # The forked run's JSONL opens with a fork marker, not a fresh
    # ``workflow.run.started`` — proving ``start_from_fork`` drove it.
    fork_events = list(store.read_events(forked.run_id))
    assert fork_events[0]["kind"] == "workflow.run.forked_from"
    assert fork_events[0]["payload"]["fork_event_id"] == ready_event_id


async def test_execute_paused_run_without_workflow_still_returns_status(
    chat_workspace: Path,
) -> None:
    """Even when the pause-capsule node lookup would miss (the resolver
    returns ``None`` if the sidecar path yields no match), the helper
    still returns the terminal status without raising — the capsule is
    a best-effort no-op on lookup failure."""
    state, runtime = await _build_state(chat_workspace)
    store = WorkflowRunStore(runtime.root)
    workflow = _resolve_workflow(runtime, "kolsch/workflows/hello")
    run = store.create(
        workflow_ref="kolsch/workflows/hello",
        inputs={"message": "ok"},
    )
    out = io.StringIO()

    status = await execute_workflow_with_renderer(
        state=state,
        stdout=out,
        store=store,
        workflow=workflow,
        run=run,
    )
    # Non-pausing workflow: status is terminal, no pause capsule path.
    assert status == WorkflowRunStatus.SUCCEEDED
    assert "→ ask" not in out.getvalue()


# The pytest plugin resolves markers by name; component tests here run
# against a real bootstrapped workspace (session-scoped fixture).
pytestmark = pytest.mark.component
