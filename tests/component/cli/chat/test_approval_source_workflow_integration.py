"""bug-107 Tests 1-4 — `_current_execution_context` → workflow approval source.

The spec-014 workflow-approval downstream (`EventAdapter._handle_prompt_requested`
workflow branch at `event_adapter.py:1917-1975`, `WorkflowsImpl.emit_awaiting_approval` /
`emit_approval_resolved` at `workflows_impl.py:1801-1876`) is fully wired but had
no way to receive a `source.kind="workflow"` payload — the callback that builds
the source (`InlineApprovalCallback._derive_source` at `approval.py:132-177`) was
never handed the ExecutionContext by the tool-dispatch chain
(`providers/authz.py::_resolve_prompt` / `_resolve_no_rule` call the callback
without `execution_context=`). bug-107 wires the contextvar: `_Exchange.run`
publishes `self.context` to `_current_execution_context`; `_derive_source` reads
it as a fallback when its parameter is `None`.

Tests 1-2 exercise the callback → PromptsImpl.choice(source=...) surface with
the contextvar set/unset. Tests 3-4 dispatch the resulting `.requested` /
`.answered` events through the EventAdapter and assert
`emit_awaiting_approval` / `emit_approval_resolved` fire (matching the
`test_workflow_approval_e2e.py` shape, but driving the source from the
contextvar-populated path — the load-bearing new wiring).

File name `_integration` avoids basename collision with the existing
`tests/cli/chat/test_approval_source_workflow.py` (spec-014 direct-helper unit
tests). Named per spec's Test 1 line.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cli.chat.approval import InlineApprovalCallback
from cli.chat.event_adapter import EventAdapter
from cli.chat.services.workflows_impl import WorkflowRunRecord, WorkflowsImpl
from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from engine.context import ExecutionContext
from engine.engine import _current_execution_context
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, ThreadId, new_card_id
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


# ---------------------------------------------------------------------------
# Harness — a minimal ChatApp double + fixtures matching
# `test_workflow_approval_e2e.py`'s shape.
# ---------------------------------------------------------------------------


class _MockApp:
    """Minimal ChatApp double for InlineApprovalCallback + adapter mount hook."""

    def __init__(self) -> None:
        self.mount_calls: list[dict[str, Any]] = []
        self.cleared_calls = 0

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
        self.cleared_calls += 1


class _StubCardBody:
    """Minimal `entry._body` stub — a `WorkflowRenderer` instance."""

    def __init__(self, renderer: WorkflowRenderer) -> None:
        self._body = renderer


def _mk_exec_ctx(**overrides: Any) -> ExecutionContext:
    base: dict[str, Any] = {
        "thread_id": ThreadId("th_00000000-0000-0000-0000-000000000001"),
        "trigger": {"kind": "user_message", "content": {}},
    }
    base.update(overrides)
    return ExecutionContext(**base)


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


async def _mk_workflows_impl(
    tmp_path: Path,
    store: SessionStore,
) -> tuple[WorkflowsImpl, Any, Any]:
    session = await store.create_session("ag_test", name="bug107")
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


# ---------------------------------------------------------------------------
# Test 1 — Workflow-nested approval writes source.kind = "workflow".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_nested_approval_source_kind() -> None:
    """`_current_execution_context` populated with workflow context →
    `PromptsImpl.choice` receives `source.kind='workflow'` with populated
    `run_id` / `node_name`.

    This is the load-bearing bug-107 fix: pre-fix, `_derive_source` always
    saw its `execution_context` parameter as `None` (no caller passed it),
    always emitted `source.kind='tool'`, and every workflow-nested approval
    shipped mis-tagged. Post-fix, `_Exchange.run` publishes `self.context`
    to the contextvar, `_derive_source` reads it as a fallback, and the
    workflow-scoped source shape flows into the emitted event.
    """
    app = _MockApp()
    mock_prompts = AsyncMock()
    mock_prompts.choice = AsyncMock(return_value="y")
    cb = InlineApprovalCallback(app, mock_prompts)

    run_id = WorkflowRunId("wrk_00000000-0000-0000-0000-000000000107")
    exec_ctx = _mk_exec_ctx(
        workflow_run_id=run_id,
        workflow_node_name="write_reqs",
    )

    # Simulate `_Exchange.run`'s set — the callback runs inside this scope.
    token = _current_execution_context.set(exec_ctx)
    try:
        await cb(
            rule_id="rl_write",
            action="write_file",
            target="data/reqs.json",
            caller_chain=("ag_worker",),
            # execution_context intentionally omitted — mirrors the
            # production path in `providers/authz.py::_resolve_prompt`,
            # which does not thread the parameter.
        )
    finally:
        _current_execution_context.reset(token)

    assert mock_prompts.choice.await_count == 1
    source = mock_prompts.choice.await_args.kwargs["source"]
    assert source["kind"] == "workflow"
    assert source["run_id"] == str(run_id)
    assert source["node_name"] == "write_reqs"
    assert source["run_chain"] == []


# ---------------------------------------------------------------------------
# Test 2 — Bare tool approval unchanged (no workflow contextvar → tool source).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_tool_approval_source_kind_unchanged() -> None:
    """No workflow_run_id in the contextvar → `source.kind='tool'`.

    Covers two shapes: (a) a butler-scope exchange whose ExecutionContext
    is set in the contextvar with `workflow_run_id=None`; (b) no
    contextvar set at all (headless test paths). Both must emit the
    tool-scoped source — the pre-bug-107 shape.
    """
    app = _MockApp()
    mock_prompts = AsyncMock()
    mock_prompts.choice = AsyncMock(return_value="y")
    cb = InlineApprovalCallback(app, mock_prompts)

    # Case (a): butler-scope contextvar (workflow_run_id=None).
    butler_ctx = _mk_exec_ctx()  # no workflow_run_id
    token = _current_execution_context.set(butler_ctx)
    try:
        await cb(rule_id=None, action="tool", target=None)
    finally:
        _current_execution_context.reset(token)

    assert mock_prompts.choice.await_count == 1
    source_a = mock_prompts.choice.await_args.kwargs["source"]
    assert source_a["kind"] == "tool"
    # Workflow-only fields must be absent on the tool-scoped shape.
    assert "run_id" not in source_a
    assert "node_name" not in source_a
    assert "run_chain" not in source_a

    # Case (b): no contextvar set — the isolate-contextvar autouse
    # fixture in tests/cli/chat/conftest.py guarantees baseline None,
    # but this file lives under tests/component/cli/chat/ where that
    # fixture isn't in scope. Assert the ambient state up front.
    from engine.engine import current_execution_context

    assert current_execution_context() is None
    await cb(rule_id=None, action="tool", target=None)
    assert mock_prompts.choice.await_count == 2
    source_b = mock_prompts.choice.await_args.kwargs["source"]
    assert source_b["kind"] == "tool"


# ---------------------------------------------------------------------------
# Test 3 — emit_awaiting_approval fires for a workflow-source .requested.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_awaiting_approval_fires_for_workflow_source(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A prompt.requested built via the contextvar-populated callback path
    is dispatched through the adapter and lands a
    `kolsch.workflow.node.awaiting_approval` in the store; the matching
    `WorkflowRunRecord.awaiting_approval` flips to True.

    This closes the loop from bug-107's fix through spec-014's downstream:
    the source is built by `InlineApprovalCallback._derive_source` via the
    contextvar; the adapter's workflow-source branch reads it and fires
    `WorkflowsImpl.emit_awaiting_approval`.
    """
    impl, session_id, thread_id = await _mk_workflows_impl(tmp_path, store)
    app = _MockApp()
    adapter = EventAdapter(app)
    adapter.register_workflows_impl(impl)

    run_id = WorkflowRunId("wrk_00000000-0000-0000-0000-0000000010a3")
    impl._workflow_registry[run_id] = WorkflowRunRecord(
        run_id=run_id,
        ref="kolsch/workflows/test",
        phase="running",
        card=None,
        created_at=datetime.now(tz=UTC),
    )
    # Seed the renderer + card so the adapter's parent_hint has a target.
    renderer = WorkflowRenderer()
    await _run_started_and_node_started(
        renderer,
        run_id=str(run_id),
        node_name="write_reqs",
    )
    adapter._card_widgets[new_card_id()] = _StubCardBody(  # type: ignore[assignment]
        renderer,
    )

    # Build the .requested source via the LIVE callback surface — the
    # test's whole point is that the contextvar populates the branch.
    exec_ctx = _mk_exec_ctx(
        workflow_run_id=run_id,
        workflow_node_name="write_reqs",
    )
    source = InlineApprovalCallback._derive_source(None)
    assert source["kind"] == "tool", "sanity: no contextvar yet → tool"
    token = _current_execution_context.set(exec_ctx)
    try:
        source = InlineApprovalCallback._derive_source(None)
    finally:
        _current_execution_context.reset(token)
    assert source["kind"] == "workflow"

    # Dispatch the workflow-source .requested through the adapter.
    prompt_event = _mk_ui_event(
        session_id=session_id,
        thread_id=thread_id,
        kind=WIRE_PROMPT_REQUESTED,
        payload={
            "prompt_id": "prm_bug107_t3",
            "kind": "choice",
            "prompt": "Approve?",
            "options": ["y", "a", "n", "d"],
            "source": source,
        },
    )
    await adapter.handle(prompt_event)

    # WIRE_WORKFLOW_NODE_AWAITING_APPROVAL landed in the store.
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
    payload = awaiting_ev.data["payload"]
    assert payload["run_id"] == str(run_id)
    assert payload["node_name"] == "write_reqs"

    # feat-019: the record's `awaiting_approval` flag flipped True.
    assert impl._workflow_registry[run_id].awaiting_approval is True


# ---------------------------------------------------------------------------
# Test 4 — emit_approval_resolved fires on .answered.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_approval_resolved_fires_on_answer(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """After the .requested → adapter → awaiting_approval loop, a
    matching .answered fires `emit_approval_resolved` with
    `resolution='approved'`; the record's `awaiting_approval` flag
    clears back to False.
    """
    impl, session_id, thread_id = await _mk_workflows_impl(tmp_path, store)
    app = _MockApp()
    adapter = EventAdapter(app)
    adapter.register_workflows_impl(impl)

    run_id = WorkflowRunId("wrk_00000000-0000-0000-0000-0000000010a4")
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
        node_name="write_merge_script",
    )
    adapter._card_widgets[new_card_id()] = _StubCardBody(  # type: ignore[assignment]
        renderer,
    )

    exec_ctx = _mk_exec_ctx(
        workflow_run_id=run_id,
        workflow_node_name="write_merge_script",
    )
    token = _current_execution_context.set(exec_ctx)
    try:
        source = InlineApprovalCallback._derive_source(None)
    finally:
        _current_execution_context.reset(token)
    assert source["kind"] == "workflow"

    await adapter.handle(
        _mk_ui_event(
            session_id=session_id,
            thread_id=thread_id,
            kind=WIRE_PROMPT_REQUESTED,
            payload={
                "prompt_id": "prm_bug107_t4",
                "kind": "choice",
                "prompt": "Approve?",
                "options": ["y", "a", "n", "d"],
                "source": source,
            },
        )
    )
    # Sanity: paused flag set by the .requested branch.
    assert impl._workflow_registry[run_id].awaiting_approval is True

    # Now .answered → adapter fires emit_approval_resolved.
    await adapter.handle(
        _mk_ui_event(
            session_id=session_id,
            thread_id=thread_id,
            kind=WIRE_PROMPT_ANSWERED,
            payload={"prompt_id": "prm_bug107_t4", "response": "y"},
        )
    )

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
    assert payload["node_name"] == "write_merge_script"
    assert payload["resolution"] == "approved"

    # feat-019: the flag cleared back to False.
    assert impl._workflow_registry[run_id].awaiting_approval is False
