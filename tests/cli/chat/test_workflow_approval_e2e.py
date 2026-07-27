"""spec-014 Test 6 — End-to-end workflow approval flow.

Four passes wired at the adapter/impl layer (not a full ChatApp
Pilot — see the note in `test_approval_e2e.py`):

1. Mock workflow with a node that fires an approval. The adapter's
   `_handle_prompt_requested` dispatches with `source.kind="workflow"`
   → `WorkflowsImpl.emit_awaiting_approval` fires and the mount hook
   receives `parent_hint`.
2. The `WIRE_WORKFLOW_NODE_AWAITING_APPROVAL` event lands in the
   store and, when dispatched through the adapter, transitions the
   matching `WorkflowNodeRow` to `paused` (via
   `WorkflowRenderer.apply_event`).
3. The user answers "y" (via a `WIRE_PROMPT_ANSWERED` dispatch); the
   adapter fires `emit_approval_resolved(resolution="approved")` →
   the resulting `WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED` event
   transitions the row back to `running`.
4. Both approval-lifecycle events persist to the JSONL — replay-on-
   boot picks up the `paused → running` transition.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from cli.chat.event_adapter import EventAdapter
from cli.chat.services.workflows_impl import WorkflowRunRecord, WorkflowsImpl
from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, new_card_id
from schemas.wire_kinds import (
    WIRE_PROMPT_ANSWERED,
    WIRE_PROMPT_REQUESTED,
    WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED,
    WIRE_WORKFLOW_NODE_AWAITING_APPROVAL,
)
from store import SessionStore, SqliteBackend
from workflows.events import WorkflowEventKind
from workflows.ids import WorkflowRunId
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class _MockApp:
    """Minimal ChatApp double for the adapter's mount hook."""

    def __init__(self) -> None:
        self.mount_calls: list[dict[str, Any]] = []

    async def mount_approval_block(
        self,
        rule_id: Any,
        action: Any,
        target: Any,
        *,
        parent_hint: str | None = None,
    ) -> Any:
        self.mount_calls.append(
            {
                "rule_id": rule_id,
                "action": action,
                "target": target,
                "parent_hint": parent_hint,
            }
        )
        return object()

    def clear_awaiting_approval(self) -> None:
        return None


class _StubCardBody:
    """Minimal `entry._body` stub — a `WorkflowRenderer` instance."""

    def __init__(self, renderer: WorkflowRenderer) -> None:
        self._body = renderer


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
) -> tuple[WorkflowsImpl, Any, Any]:
    session = await store.create_session("ag_test", name="e2e_wf")
    threads = await store.list_threads(session.session_id)
    thread_id = threads[0].thread_id
    run_store = WorkflowRunStore(tmp_path)
    impl = WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
        run_store=run_store,
    )
    return impl, session.session_id, thread_id


def _mk_ui_event(
    *,
    session_id: Any,
    thread_id: Any,
    kind: str,
    payload: dict[str, Any],
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.UI_EVENT,
        data={
            "schema_version": 1,
            "kind": kind,
            "payload": payload,
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


async def _run_started_and_node_started(
    renderer: WorkflowRenderer,
    *,
    run_id: str,
    node_name: str,
) -> None:
    ts = datetime.now(tz=UTC).isoformat()
    renderer.apply_event(
        {
            "kind": "kolsch.workflow.progress",
            "payload": {
                "run_id": run_id,
                "workflow_ref": "kolsch/workflows/test",
                "kind": WorkflowEventKind.RUN_STARTED.value,
                "event_id": "ev_run_started",
                "ts": ts,
                "trace_id": "tr_root",
                "payload": {},
            },
        }
    )
    renderer.apply_event(
        {
            "kind": "kolsch.workflow.progress",
            "payload": {
                "run_id": run_id,
                "workflow_ref": "kolsch/workflows/test",
                "kind": WorkflowEventKind.NODE_STARTED.value,
                "event_id": "ev_node_started",
                "ts": ts,
                "trace_id": "tr_root",
                "payload": {},
                "node_name": node_name,
            },
        }
    )


@pytest.mark.asyncio
async def test_e2e_workflow_approval_paused(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """Prompt.requested with workflow source → row paused + parent_hint."""
    impl, session_id, thread_id = await _mk_impl(tmp_path, store)
    app = _MockApp()
    adapter = EventAdapter(app)  # type: ignore[arg-type]
    adapter.register_workflows_impl(impl)

    # Seed the impl's registry so the adapter's parent_hint derives
    # `workflow <ref>` (not the bare run_id).
    run_id = WorkflowRunId("wrk_00000000-0000-0000-0000-0000000000e2")
    impl._workflow_registry[run_id] = WorkflowRunRecord(
        run_id=run_id,
        ref="kolsch/workflows/test",
        phase="running",
        card=None,
        created_at=datetime.now(tz=UTC),
    )

    # Wire a live WorkflowRenderer so the awaiting_approval event
    # dispatch has somewhere to land.
    renderer = WorkflowRenderer()
    await _run_started_and_node_started(
        renderer,
        run_id=str(run_id),
        node_name="build",
    )
    entry = _StubCardBody(renderer)
    # Adapter fans out via `_card_widgets.values()` — inject via a
    # scratch card id.
    adapter._card_widgets[new_card_id()] = entry  # type: ignore[assignment]

    # Fire the workflow-scoped prompt.requested event.
    prompt_event = _mk_ui_event(
        session_id=session_id,
        thread_id=thread_id,
        kind=WIRE_PROMPT_REQUESTED,
        payload={
            "prompt_id": "prm_wf1",
            "kind": "choice",
            "prompt": "Approve?",
            "options": ["y", "a", "n", "d"],
            "source": {
                "kind": "workflow",
                "id": str(run_id),
                "run_id": str(run_id),
                "node_name": "build",
                "run_chain": [],
            },
        },
    )
    await adapter.handle(prompt_event)

    # ApprovalBlock mounted with parent_hint.
    assert len(app.mount_calls) == 1
    # spec-017 extends the breadcrumb to append `· node <name>` when
    # the innermost source carries a node_name; the spec-014 shape
    # (bare `workflow <ref>`) is preserved only when node_name is
    # empty. The Q3-locked breadcrumb builder walks the registry's
    # `parent_run_id` chain to construct the full lineage; a
    # single-level run collapses to `workflow <ref> · node <name>`.
    assert (
        app.mount_calls[0]["parent_hint"]
        == "workflow kolsch/workflows/test · node build"
    )

    # WIRE_WORKFLOW_NODE_AWAITING_APPROVAL was appended.
    async def _find_awaiting() -> Event | None:
        deadline = asyncio.get_running_loop().time() + 1.0
        while asyncio.get_running_loop().time() < deadline:
            async for ev in store.read_events(thread_id):
                if ev.type != EventType.UI_EVENT:
                    continue
                data = ev.data if isinstance(ev.data, dict) else {}
                if data.get("kind") == WIRE_WORKFLOW_NODE_AWAITING_APPROVAL:
                    return ev
            await asyncio.sleep(0.01)
        return None

    awaiting_ev = await _find_awaiting()
    assert awaiting_ev is not None

    # Dispatch it through the adapter — renderer transitions the row.
    await adapter.handle(awaiting_ev)
    assert renderer._node_rows[("build", "")].state == "paused"


@pytest.mark.asyncio
async def test_e2e_workflow_approval_resolved_transitions_back(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """User presses `y` → resolved event fires → row transitions to running."""
    impl, session_id, thread_id = await _mk_impl(tmp_path, store)
    app = _MockApp()
    adapter = EventAdapter(app)  # type: ignore[arg-type]
    adapter.register_workflows_impl(impl)

    run_id = WorkflowRunId("wrk_00000000-0000-0000-0000-0000000000e3")
    impl._workflow_registry[run_id] = WorkflowRunRecord(
        run_id=run_id,
        ref="kolsch/workflows/test",
        phase="running",
        card=None,
        created_at=datetime.now(tz=UTC),
    )

    renderer = WorkflowRenderer()
    await _run_started_and_node_started(
        renderer,
        run_id=str(run_id),
        node_name="build",
    )
    adapter._card_widgets[new_card_id()] = _StubCardBody(
        renderer,
    )  # type: ignore[assignment]

    # 1. Prompt.requested with workflow source → adapter emits
    #    awaiting_approval; tracks prompt_id → (run_id, node_name).
    prompt_event = _mk_ui_event(
        session_id=session_id,
        thread_id=thread_id,
        kind=WIRE_PROMPT_REQUESTED,
        payload={
            "prompt_id": "prm_wf2",
            "kind": "choice",
            "prompt": "Approve?",
            "options": ["y", "a", "n", "d"],
            "source": {
                "kind": "workflow",
                "id": str(run_id),
                "run_id": str(run_id),
                "node_name": "build",
                "run_chain": [],
            },
        },
    )
    await adapter.handle(prompt_event)

    # 2. Prompt.answered → adapter fires emit_approval_resolved.
    answered_event = _mk_ui_event(
        session_id=session_id,
        thread_id=thread_id,
        kind=WIRE_PROMPT_ANSWERED,
        payload={"prompt_id": "prm_wf2", "response": "y"},
    )
    await adapter.handle(answered_event)

    # 3. Poll for the WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED event —
    #    the emit is fire-and-forget so give it a beat to land.
    async def _find_resolved() -> Event | None:
        deadline = asyncio.get_running_loop().time() + 1.5
        while asyncio.get_running_loop().time() < deadline:
            async for ev in store.read_events(thread_id):
                if ev.type != EventType.UI_EVENT:
                    continue
                data = ev.data if isinstance(ev.data, dict) else {}
                if data.get("kind") == WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED:
                    return ev
            await asyncio.sleep(0.02)
        return None

    resolved_ev = await _find_resolved()
    assert resolved_ev is not None
    payload = resolved_ev.data["payload"]
    assert payload["run_id"] == str(run_id)
    assert payload["node_name"] == "build"
    assert payload["resolution"] == "approved"

    # 4. Dispatch back through the adapter — row transitions to running.
    # First simulate the paused transition (the awaiting_approval event
    # from step 1 is also in the store; dispatch it so the row is paused).
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") == WIRE_WORKFLOW_NODE_AWAITING_APPROVAL:
            await adapter.handle(ev)
            break
    assert renderer._node_rows[("build", "")].state == "paused"

    await adapter.handle(resolved_ev)
    assert renderer._node_rows[("build", "")].state == "running"


@pytest.mark.asyncio
async def test_e2e_workflow_approval_denied_still_transitions_running(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A `n` (deny) resolution also transitions the row back to running.

    The resolution verb reflects the user's answer, but the node row
    always returns to `running` — the executor's follow-up NODE_*
    events drive any subsequent terminal transition (e.g. NODE_FAILED
    when the tool's denial cascades).
    """
    impl, session_id, thread_id = await _mk_impl(tmp_path, store)
    app = _MockApp()
    adapter = EventAdapter(app)  # type: ignore[arg-type]
    adapter.register_workflows_impl(impl)

    run_id = WorkflowRunId("wrk_00000000-0000-0000-0000-0000000000e4")
    impl._workflow_registry[run_id] = WorkflowRunRecord(
        run_id=run_id,
        ref="kolsch/workflows/test",
        phase="running",
        card=None,
        created_at=datetime.now(tz=UTC),
    )
    renderer = WorkflowRenderer()
    await _run_started_and_node_started(
        renderer,
        run_id=str(run_id),
        node_name="build",
    )
    adapter._card_widgets[new_card_id()] = _StubCardBody(
        renderer,
    )  # type: ignore[assignment]

    await adapter.handle(
        _mk_ui_event(
            session_id=session_id,
            thread_id=thread_id,
            kind=WIRE_PROMPT_REQUESTED,
            payload={
                "prompt_id": "prm_wf3",
                "kind": "choice",
                "prompt": "Approve?",
                "options": ["y", "a", "n", "d"],
                "source": {
                    "kind": "workflow",
                    "id": str(run_id),
                    "run_id": str(run_id),
                    "node_name": "build",
                    "run_chain": [],
                },
            },
        )
    )
    await adapter.handle(
        _mk_ui_event(
            session_id=session_id,
            thread_id=thread_id,
            kind=WIRE_PROMPT_ANSWERED,
            payload={"prompt_id": "prm_wf3", "response": "n"},
        )
    )

    # Poll for the resolved event.
    async def _find_resolved() -> Event | None:
        deadline = asyncio.get_running_loop().time() + 1.5
        while asyncio.get_running_loop().time() < deadline:
            async for ev in store.read_events(thread_id):
                if ev.type != EventType.UI_EVENT:
                    continue
                data = ev.data if isinstance(ev.data, dict) else {}
                if data.get("kind") == WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED:
                    return ev
            await asyncio.sleep(0.02)
        return None

    resolved_ev = await _find_resolved()
    assert resolved_ev is not None
    assert resolved_ev.data["payload"]["resolution"] == "denied"


def test_e2e_no_get_event_loop_in_modified_files() -> None:
    """AC #13 — no runtime `get_event_loop` call in modified files.

    Uses an AST walker (not a raw text grep) so docstrings and
    comments that legitimately mention `get_event_loop` (as
    documentation of the anti-pattern) don't trigger a false
    positive. The invariant is: no Python attribute-access node
    reads `.get_event_loop` — that's the only shape a runtime call
    to `asyncio.get_event_loop()` can take.
    """
    import ast

    modified = [
        "src/cli/chat/services/workflows_impl.py",
        "src/cli/chat/services/prompts_impl.py",
        "src/cli/chat/event_adapter.py",
        "src/cli/chat/approval.py",
        "src/cli/chat/widgets/approval_block.py",
        "src/cli/chat/widgets/card_body/workflow.py",
    ]
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[3]
    for rel in modified:
        p = repo_root / rel
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "get_event_loop", (
                    f"{rel} calls `.get_event_loop` — spec-014 AC #13"
                )
