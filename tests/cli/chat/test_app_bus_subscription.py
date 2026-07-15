"""bug-020 Test 3 — `ChatApp.on_mount` bus subscription lifecycle.

Exercises AC #5 (subscribe on mount, unsubscribe on unmount) and the
end-to-end delivery path — a `UI_EVENT` published on the runtime
bus reaches `EventAdapter.handle` in the SAME session (no manual
store drain).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bus.in_process import InProcessEventBus
from cli.chat.app import ChatApp
from cli.chat.services.cards_impl import UI_EVENT_TOPIC
from schemas import Event
from schemas.entities import Actor
from schemas.events import EventType
from schemas.identifiers import EventId


class _MockAuthz:
    def register_prompt_callback(
        self,
        callback: Any,
        *,
        on_rule_added: Any = None,
    ) -> None:
        return None

    def clear_prompt_callback(self) -> None:
        return None


class _MockEngine:
    async def execute_stream(self, config: Any, ctx: Any):
        if False:
            yield  # pragma: no cover


@dataclass
class _MockConfig:
    name: str = "TestAgent"
    agent_id: str = "ag_mock"
    model: Any = None


@dataclass
class _MockBundle:
    runtime: Any
    config: Any
    session_id: Any
    thread_id: Any
    agent_ref: str
    root: Path
    model_label: str
    is_resume: bool


async def _bootstrap_app(tmp_path: Path) -> tuple[ChatApp, InProcessEventBus, Any]:
    """Return a `(ChatApp, bus, backend)` triple ready for `run_test`."""
    from store import SessionStore, SqliteBackend

    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_mock", name="c")
    threads = await store.list_threads(session.session_id)
    bus = InProcessEventBus()

    class _R:
        def __init__(self) -> None:
            self.store = store
            self.authz = _MockAuthz()
            self.engine = _MockEngine()
            self.root = tmp_path
            self.bus = bus

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
        session=bundle,  # type: ignore[arg-type]
        engine=bundle.runtime.engine,
        full_history=False,
    )
    return app, bus, backend


@pytest.mark.asyncio
async def test_on_mount_registers_subscription(tmp_path: Path) -> None:
    """AC #5 — `_ui_event_bus_sub` is populated after mount."""
    app, bus, backend = await _bootstrap_app(tmp_path)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._ui_event_bus_sub is not None
            # And the bus has exactly one subscription registered.
            assert len(bus._subscriptions) == 1
            sub = next(iter(bus._subscriptions.values()))
            assert sub.topic_pattern == UI_EVENT_TOPIC
            # The handler is the adapter's `handle`.
            assert sub.handler == app.event_adapter.handle
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_on_unmount_unsubscribes(tmp_path: Path) -> None:
    """AC #5 + D-008 — unmount clears the subscription; a re-mount does not stack."""
    app, bus, backend = await _bootstrap_app(tmp_path)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(bus._subscriptions) == 1

        # After context exit, `on_unmount` has run.
        assert len(bus._subscriptions) == 0
        assert app._ui_event_bus_sub is None
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_ui_event_publish_reaches_adapter_handle(
    tmp_path: Path,
) -> None:
    """A `UI_EVENT` published on the bus is dispatched to `event_adapter.handle`."""
    app, bus, backend = await _bootstrap_app(tmp_path)
    handled: list[Event] = []

    async def _spy(event: Event) -> None:
        handled.append(event)

    try:
        async with app.run_test() as pilot:
            await pilot.pause()

            # Swap out the subscription's handler with a spy. Easier
            # than replacing the adapter — the sub records the handler
            # by identity so we just re-subscribe.
            assert app._ui_event_bus_sub is not None
            await bus.unsubscribe(app._ui_event_bus_sub)
            app._ui_event_bus_sub = await bus.subscribe(
                UI_EVENT_TOPIC,
                _spy,
            )

            from uuid import uuid4 as _uuid4

            eid = EventId(f"ev_{_uuid4()}")
            event = Event(
                event_id=eid,
                parent_event_id=None,
                thread_id=app.session.thread_id,
                session_id=app.session.session_id,
                type=EventType.UI_EVENT,
                data={"schema_version": 1, "kind": "test.kind", "payload": {}},
                actor=Actor(kind="system"),
                created_at=datetime.now(UTC),
            )
            await bus.publish(event)

            assert handled and handled[0].event_id == eid
    finally:
        await backend.close()
