"""feat-126 Test 3c — workflow-node approvals auto-approve too (AC #6).

Same live harness as `test_feat126_auto_approve_e2e.py` (real
`ChatApp(auto_approve=True)` + live bus + real store), but the
published `WIRE_PROMPT_REQUESTED` carries a WORKFLOW source
(`source = {"kind": "workflow", "run_id": ..., "node_name": ...}`).

Asserts, with no keypress:

1. Exactly one durable `WIRE_PROMPT_ANSWERED(response="y")` lands —
   the workflow-scoped ask rides the same widget → bridge path.
2. The workflow-scoped `WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED` fires
   with `resolution == "approved"` — exactly what a manual `y` drives
   through `_maybe_emit_approval_resolved`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from bus.in_process import InProcessEventBus
from cli.chat.app import ChatApp
from schemas import Event
from schemas.entities import Actor
from schemas.events import EventType
from schemas.identifiers import EventId
from schemas.wire_kinds import (
    WIRE_PROMPT_ANSWERED,
    WIRE_PROMPT_REQUESTED,
    WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED,
)
from store import SessionStore, SqliteBackend

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator
    from pathlib import Path


class _MockAuthz:
    def register_prompt_callback(
        self,
        callback: Any,
        *,
        on_rule_added: Any = None,
    ) -> None:
        _ = callback, on_rule_added

    def clear_prompt_callback(self) -> None:
        return None


async def _empty_stream(
    *_args: Any,
    **_kwargs: Any,
) -> AsyncGenerator[Any, None]:
    """Yield nothing — the mock engine never runs a real turn."""
    return
    yield  # type: ignore[unreachable]  # pragma: no cover


class _MockEngine:
    execute_stream = staticmethod(_empty_stream)


class _MockConfig:
    name = "TestAgent"
    agent_id = "ag_mock"
    model = None


class _MockBundle:
    def __init__(
        self,
        *,
        runtime: Any,
        config: Any,
        session_id: Any,
        thread_id: Any,
        agent_ref: str,
        root: Path,
        model_label: str,
        is_resume: bool,
    ) -> None:
        self.runtime = runtime
        self.config = config
        self.session_id = session_id
        self.thread_id = thread_id
        self.agent_ref = agent_ref
        self.root = root
        self.model_label = model_label
        self.is_resume = is_resume


@pytest.fixture
async def auto_chat_app(tmp_path: Path) -> AsyncIterator[Any]:
    """Boot a `ChatApp(auto_approve=True)` with live bus + real store."""
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_mock", name="auto_wf")
    threads = await store.list_threads(session.session_id)

    class _R:
        def __init__(self) -> None:
            self.store = store
            self.authz = _MockAuthz()
            self.engine = _MockEngine()
            self.root = tmp_path
            self.bus = InProcessEventBus()

    bundle = _MockBundle(
        runtime=_R(),
        config=_MockConfig(),
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    app = ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
        auto_approve=True,
    )
    try:
        yield app
    finally:
        await backend.close()


async def _events_of_kind(
    store: SessionStore,
    thread_id: Any,
    wire_kind: str,
) -> list[Event]:
    found: list[Event] = []
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") == wire_kind:
            found.append(ev)
    return found


@pytest.mark.asyncio
async def test_workflow_source_prompt_auto_approves(
    auto_chat_app: Any,
) -> None:
    """Workflow ask → self-resolve → one answered("y") + approval_resolved."""
    prompt_id = f"prm_{uuid4().hex[:12]}"
    run_id = f"run_{uuid4().hex[:8]}"

    async with auto_chat_app.run_test() as pilot:
        await pilot.pause()
        bus = pilot.app.session.runtime.bus
        store = pilot.app.session.runtime.store
        thread_id = pilot.app.session.thread_id

        request = Event(
            event_id=EventId(f"ev_{uuid4()}"),
            parent_event_id=None,
            thread_id=thread_id,
            session_id=pilot.app.session.session_id,
            type=EventType.UI_EVENT,
            data={
                "schema_version": 1,
                "kind": WIRE_PROMPT_REQUESTED,
                "payload": {
                    "prompt_id": prompt_id,
                    "kind": "choice",
                    "prompt": "Tool approval — bash mkdir build",
                    "options": ["y", "a", "n", "d"],
                    "source": {
                        "kind": "workflow",
                        "run_id": run_id,
                        "node_name": "build",
                    },
                    "rule_id": "rl_wf_auto",
                    "action": "bash",
                    "target": "mkdir build",
                },
            },
            actor=Actor(kind="system"),
            created_at=datetime.now(UTC),
        )
        await store.append_event(thread_id, request)
        await bus.publish(request)

        # (1) One durable answered "y", no keypress (bounded 2s).
        answered: list[Event] = []
        for _ in range(40):
            await pilot.pause()
            answered = [
                ev
                for ev in await _events_of_kind(
                    store,
                    thread_id,
                    WIRE_PROMPT_ANSWERED,
                )
                if ev.data["payload"].get("prompt_id") == prompt_id
            ]
            if answered:
                break
            await asyncio.sleep(0.05)
        assert answered, "workflow-scoped ask was not auto-approved"
        assert len(answered) == 1
        assert answered[0].data["payload"]["response"] == "y"

        # (2) The workflow-scoped approval_resolved fires with
        # resolution "approved" — the same emit a manual `y` drives.
        resolved: list[Event] = []
        for _ in range(40):
            await pilot.pause()
            resolved = [
                ev
                for ev in await _events_of_kind(
                    store,
                    thread_id,
                    WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED,
                )
                if ev.data["payload"].get("run_id") == run_id
            ]
            if resolved:
                break
            await asyncio.sleep(0.05)
        assert resolved, (
            "workflow approval_resolved never fired — the answered "
            "dispatch did not reach _maybe_emit_approval_resolved."
        )
        assert resolved[0].data["payload"]["resolution"] == "approved"
        assert resolved[0].data["payload"]["node_name"] == "build"
