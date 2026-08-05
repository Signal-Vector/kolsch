"""feat-126 Test 3 — end-to-end auto-approve, real `ChatApp`, NO keypress.

Modeled on `test_approval_flow_live.py` (the bug-021 flagship): boots
a real `ChatApp` under Pilot with `auto_approve=True`, publishes a
`WIRE_PROMPT_REQUESTED` through the LIVE bus, and asserts — with no
key ever sent — that:

1. The mounted `ApprovalBlock` resolves itself (footer
   `✓ Auto-approved (once)`).
2. Exactly ONE durable `WIRE_PROMPT_ANSWERED(response="y")` lands in
   the store, via the existing `_bridge_widget_resolution` path
   (bug-110 hazard: a duplicate write would double-resolve).
3. A real `InlineApprovalCallback` → `PromptsImpl.choice()` chain
   returns `AuthzEffect.ALLOW` (the future RESOLVES — bug-114 hazard:
   never a cancel), so the tool proceeds.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from bus.in_process import InProcessEventBus
from cli.chat.app import ChatApp
from cli.chat.approval import InlineApprovalCallback
from cli.chat.widgets.approval_block import ApprovalBlock
from providers.authz import AuthzEffect
from schemas import Event
from schemas.entities import Actor
from schemas.events import EventType
from schemas.identifiers import EventId
from schemas.wire_kinds import WIRE_PROMPT_ANSWERED, WIRE_PROMPT_REQUESTED
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
    session = await store.create_session("ag_mock", name="auto_approve")
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


def _mk_requested_event(
    *,
    session_id: Any,
    thread_id: Any,
    prompt_id: str,
    rule_id: str,
    action: str,
    target: str,
    source: dict[str, Any] | None = None,
) -> Event:
    """Build a `WIRE_PROMPT_REQUESTED` mirroring `PromptsImpl.choice`."""
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.UI_EVENT,
        data={
            "schema_version": 1,
            "kind": WIRE_PROMPT_REQUESTED,
            "payload": {
                "prompt_id": prompt_id,
                "kind": "choice",
                "prompt": f"Tool approval — {action} {target} (rule: {rule_id})",
                "options": ["y", "a", "n", "d"],
                "source": source or {"kind": "tool", "id": "t1"},
                "rule_id": rule_id,
                "action": action,
                "target": target,
            },
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


async def _answered_events(
    store: SessionStore,
    thread_id: Any,
    prompt_id: str,
) -> list[Event]:
    """ALL `WIRE_PROMPT_ANSWERED` events for `prompt_id` (dup detection)."""
    found: list[Event] = []
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") != WIRE_PROMPT_ANSWERED:
            continue
        payload = data.get("payload")
        if isinstance(payload, dict) and payload.get("prompt_id") == prompt_id:
            found.append(ev)
    return found


@pytest.mark.asyncio
async def test_auto_approve_single_durable_answer_no_keypress(
    auto_chat_app: Any,
) -> None:
    """Live bus → mount → self-resolve → exactly ONE answered("y")."""
    prompt_id = f"prm_{uuid4().hex[:12]}"

    async with auto_chat_app.run_test() as pilot:
        await pilot.pause()
        bus = pilot.app.session.runtime.bus
        store = pilot.app.session.runtime.store
        thread_id = pilot.app.session.thread_id

        request = _mk_requested_event(
            session_id=pilot.app.session.session_id,
            thread_id=thread_id,
            prompt_id=prompt_id,
            rule_id="rl_auto_probe",
            action="bash",
            target="ls -la /tmp",
        )
        await store.append_event(thread_id, request)
        await bus.publish(request)

        # NO pilot.press anywhere in this test. Wait (bounded 2s) for
        # the auto-resolved answer to land durably.
        answered: list[Event] = []
        for _ in range(40):
            await pilot.pause()
            answered = await _answered_events(store, thread_id, prompt_id)
            if answered:
                break
            await asyncio.sleep(0.05)
        assert answered, (
            "WIRE_PROMPT_ANSWERED never landed with no keypress — the "
            "auto-approve self-resolve → bridge path is not wired."
        )
        assert answered[0].data["payload"]["response"] == "y"

        # bug-110 hazard: pump extra frames, then re-count — a late
        # duplicate write from a second emit site would show up here.
        for _ in range(10):
            await pilot.pause()
            await asyncio.sleep(0.02)
        answered = await _answered_events(store, thread_id, prompt_id)
        assert len(answered) == 1, (
            f"expected exactly one durable answer, got {len(answered)}"
        )

        # The card is still visible, flipped to the auto footer.
        blocks = list(pilot.app.query(ApprovalBlock))
        assert len(blocks) == 1
        assert blocks[0].is_resolved is True
        footer = blocks[0].body_children[-1]
        assert "✓ Auto-approved (once)" in str(footer.render())


@pytest.mark.asyncio
async def test_callback_returns_allow_without_keypress(
    auto_chat_app: Any,
) -> None:
    """Full chain: callback → PromptsImpl → widget self-resolve → ALLOW.

    The future must RESOLVE (bug-114 hazard: a cancel would surface
    as a turn-cancel). A bare `AuthzEffect.ALLOW` return also proves
    allow-once — no `Rule` travelled back, so the authz persistence
    hook (`_on_rule_added`) has nothing to persist.
    """
    async with auto_chat_app.run_test() as pilot:
        await pilot.pause()

        prompts = pilot.app._service_registry.prompts
        assert prompts is not None
        cb = InlineApprovalCallback(pilot.app, prompts)

        cb_task = asyncio.create_task(
            cb(
                rule_id="rl_auto_cb",
                action="bash",
                target="echo hi",
            ),
        )

        result: Any = None
        for _ in range(80):
            await pilot.pause()
            if cb_task.done():
                result = cb_task.result()
                break
            await asyncio.sleep(0.05)
        assert cb_task.done(), (
            "callback never returned — the auto-approve resolution did "
            "not reach PromptsImpl.resolve_pending."
        )
        assert result is AuthzEffect.ALLOW
        assert not isinstance(result, tuple)
