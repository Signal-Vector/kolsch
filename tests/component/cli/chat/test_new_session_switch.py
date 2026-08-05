"""feat-117 — `/new` opens a real fresh session.

Covers the repoint surface (every per-session handle) + the idle guard:

- Unit: the `rebind_session` methods on `PromptsImpl` / `CardsImpl` /
  `WorkflowsImpl` (the durable-writing impls) and `EventAdapter.
  reset_for_session` (retarget ids + wipe every per-session map).
- Integration (real `SessionStore` under `App.run_test()`): `/new` switches
  every handle onto a fresh session; a post-switch durable write lands on the
  NEW thread; the idle guard blocks the switch when a turn or workflow is
  live; and the workflow registry is reset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Static

from bus.in_process import InProcessEventBus
from cli.chat.app import ChatApp
from cli.chat.event_adapter import EventAdapter
from cli.chat.services.cards_impl import CardsImpl
from cli.chat.services.prompts_impl import PromptsImpl
from cli.chat.services.workflows_impl import WorkflowsImpl
from cli.chat.widgets import InfoBar
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId
from store import SessionStore, SqliteBackend
from workflows.ids import WorkflowRunId

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_TRUTHY: bool = True  # `bool`-typed (not `Literal[True]`) to dodge mypy narrowing
_PENDING_WF: str | None = "wrk_00000000-0000-0000-0000-0000000000dd"

_OLD_SES = SessionId("ses_00000000-0000-0000-0000-000000000001")
_OLD_TH = ThreadId("th_00000000-0000-0000-0000-000000000001")
_NEW_SES = SessionId("ses_00000000-0000-0000-0000-000000000002")
_NEW_TH = ThreadId("th_00000000-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# Unit — rebind_session on the three durable-writing impls
# ---------------------------------------------------------------------------


def test_prompts_impl_rebind_session() -> None:
    impl = object.__new__(PromptsImpl)
    impl._session_id = _OLD_SES
    impl._thread_id = _OLD_TH
    impl.rebind_session(_NEW_SES, _NEW_TH)
    assert impl._session_id == _NEW_SES
    assert impl._thread_id == _NEW_TH


def test_cards_impl_rebind_session() -> None:
    impl = object.__new__(CardsImpl)
    impl._session_id = _OLD_SES
    impl._thread_id = _OLD_TH
    impl.rebind_session(_NEW_SES, _NEW_TH)
    assert impl._session_id == _NEW_SES
    assert impl._thread_id == _NEW_TH


def test_workflows_impl_rebind_session_clears_registry() -> None:
    impl = object.__new__(WorkflowsImpl)
    impl._session_id = _OLD_SES
    impl._thread_id = _OLD_TH
    impl._workflow_registry = {
        WorkflowRunId("wrk_00000000-0000-0000-0000-0000000000aa"): cast(
            "Any", object()
        ),
    }
    impl.rebind_session(_NEW_SES, _NEW_TH)
    assert impl._session_id == _NEW_SES
    assert impl._thread_id == _NEW_TH
    assert impl._workflow_registry == {}


# ---------------------------------------------------------------------------
# Unit — EventAdapter.reset_for_session
# ---------------------------------------------------------------------------


def test_event_adapter_reset_for_session_wipes_all_per_session_state() -> None:
    adapter = EventAdapter(
        cast("Any", object()), session_id=_OLD_SES, thread_id=_OLD_TH
    )
    raw = cast("Any", adapter)
    # Populate one representative entry in every per-session container.
    raw._card_widgets["crd"] = object()
    raw._event_widgets[EventId(f"ev_{uuid4()}")] = [object()]
    raw._compaction_widgets["c"] = object()
    raw._notice_widgets["n"] = object()
    raw._live_prompt_blocks["p"] = object()
    raw._live_nodes_by_trace_and_invocation[("t", "i")] = object()
    raw._streaming_widgets["s"] = object()
    raw._streaming_buffers["s"] = "buf"
    raw._tool_call_args["tc"] = {"a": 1}
    raw._suppressed_tool_calls.add("tc")
    raw._workflow_prompt_sources["p"] = ("k", "v")
    raw._applied_notice_ids.add("n")
    raw._applied_prompt_ids.add("p")
    raw._resolved_prompt_ids.add("p")
    raw._remounted_events.add(EventId(f"ev_{uuid4()}"))
    raw._replay_prompt_outcomes["p"] = ("allowed", "f")
    raw._bookmark = EventId(f"ev_{uuid4()}")
    raw._bookmark_dispatch_count = 5
    raw._started = True

    adapter.reset_for_session(_NEW_SES, _NEW_TH)

    assert adapter._session_id == _NEW_SES
    assert adapter._thread_id == _NEW_TH
    assert adapter._bookmark is None
    assert adapter._bookmark_dispatch_count == 0
    assert adapter._started is False
    for container in (
        raw._card_widgets,
        raw._event_widgets,
        raw._compaction_widgets,
        raw._notice_widgets,
        raw._live_prompt_blocks,
        raw._live_nodes_by_trace_and_invocation,
        raw._streaming_widgets,
        raw._streaming_buffers,
        raw._tool_call_args,
        raw._workflow_prompt_sources,
        raw._replay_prompt_outcomes,
        raw._suppressed_tool_calls,
        raw._remounted_events,
    ):
        assert not container
    assert "n" not in adapter._applied_notice_ids
    assert "p" not in adapter._applied_prompt_ids
    assert "p" not in adapter._resolved_prompt_ids


# ---------------------------------------------------------------------------
# Integration — real SessionStore + ChatApp under Pilot
# ---------------------------------------------------------------------------


class _MockAuthz:
    def register_prompt_callback(
        self, callback: Any, *, on_rule_added: Any = None
    ) -> None:
        _ = (callback, on_rule_added)

    def clear_prompt_callback(self) -> None:
        return None


class _MockEngine:
    async def execute_stream(self, config: Any, ctx: Any) -> AsyncIterator[Any]:
        _ = (config, ctx)
        return
        yield  # type: ignore[unreachable]  # pragma: no cover — empty async gen


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


async def _bootstrap_app(tmp_path: Path) -> tuple[ChatApp, SessionStore]:
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
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=False)
    return app, store


@pytest.mark.asyncio
async def test_new_session_switches_every_handle(tmp_path: Path) -> None:
    app, store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        old_ses = app.session.session_id
        old_th = app.session.thread_id

        await app._open_new_session()
        await pilot.pause()

        new_ses = app.session.session_id
        new_th = app.session.thread_id
        assert new_ses != old_ses
        assert new_th != old_th
        assert app.session.is_resume is False

        assert app._chat_state is not None
        assert app._chat_state.session_id == new_ses
        assert app._chat_state.thread_id == new_th
        assert app.event_adapter._session_id == new_ses
        assert app.event_adapter._thread_id == new_th

        reg = app._service_registry
        assert reg is not None
        for impl in (reg.prompts, reg.cards, reg.workflows):
            handle = cast("Any", impl)
            assert handle._session_id == new_ses
            assert handle._thread_id == new_th

        # New session exists in the store; old one still loadable.
        assert await store.get_session(new_ses) is not None
        assert await store.get_session(old_ses) is not None


@pytest.mark.asyncio
async def test_durable_write_after_new_targets_new_thread(tmp_path: Path) -> None:
    app, store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        old_th = app.session.thread_id
        old_events = [e async for e in store.read_path_to_leaf(old_th)]

        await app._open_new_session()
        await pilot.pause()
        new_th = app.session.thread_id

        reg = app._service_registry
        assert reg is not None
        cards = cast("Any", reg.cards)
        # The impl now writes durable events with the NEW thread id (this is
        # the id it passes to `append_event`), not the old one.
        assert cards._thread_id == new_th
        assert cards._thread_id != old_th

        # Appending through that cached thread lands on the new thread only.
        event = Event(
            event_id=EventId(f"ev_{uuid4()}"),
            parent_event_id=None,
            thread_id=cards._thread_id,
            session_id=cards._session_id,
            type=EventType.UI_EVENT,
            data={"schema_version": 1, "kind": "card.notice", "payload": {}},
            actor=Actor(kind="system"),
            created_at=datetime.now(UTC),
        )
        await store.append_event(cards._thread_id, event)

        new_events = [e async for e in store.read_path_to_leaf(new_th)]
        after_old = [e async for e in store.read_path_to_leaf(old_th)]
        assert any(e.event_id == event.event_id for e in new_events)
        assert len(after_old) == len(old_events)  # old thread untouched


@pytest.mark.asyncio
async def test_new_session_proceeds_and_retires_active_turn(tmp_path: Path) -> None:
    """feat-127: `/new` no longer refuses on an active turn — it cancels it."""
    app, _store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        old_ses = app.session.session_id

        class _Tok:
            def cancel(self) -> None:
                # Simulate `_run_exchange`'s finally nulling `active_cancel`
                # once the fired token unwinds the awaiting turn.
                app.active_cancel = None

        app.active_cancel = _Tok()

        await app._open_new_session()
        await pilot.pause()

        assert app.session.session_id != old_ses  # proceeded — not blocked
        assert app.active_cancel is None  # the turn was retired


@pytest.mark.asyncio
async def test_new_session_proceeds_and_retires_live_workflow(tmp_path: Path) -> None:
    """feat-127: a live workflow no longer blocks `/new` — it's retired."""
    app, _store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        old_ses = app.session.session_id
        reg = app._service_registry
        assert reg is not None
        wf = cast("Any", reg.workflows)
        wf._workflow_registry[
            WorkflowRunId("wrk_00000000-0000-0000-0000-0000000000bb")
        ] = SimpleNamespace(phase="running")

        await app._open_new_session()
        await pilot.pause()

        assert app.session.session_id != old_ses  # proceeded — not blocked
        assert wf._workflow_registry == {}  # retired + registry reset


@pytest.mark.asyncio
async def test_new_session_resets_workflow_registry(tmp_path: Path) -> None:
    app, _store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        reg = app._service_registry
        assert reg is not None
        wf = cast("Any", reg.workflows)
        # A TERMINAL record (not live) — the idle guard passes, switch proceeds.
        wf._workflow_registry[
            WorkflowRunId("wrk_00000000-0000-0000-0000-0000000000cc")
        ] = SimpleNamespace(phase="success")

        await app._open_new_session()
        await pilot.pause()

        assert wf._workflow_registry == {}


@pytest.mark.asyncio
async def test_new_session_clears_per_turn_flags(tmp_path: Path) -> None:
    """reviewer MAJOR: the new session starts with a clean per-turn slate.

    Without clearing these, `/skill X` then `/new` would silently force-invoke
    X on the first message of the new session, and a paused-workflow /
    awaited-input flag would leak across the boundary.
    """
    app, _store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._chat_state is not None
        # Pre-switch values are assigned from field-typed module constants so
        # mypy narrows the attrs to their declared types (not literals) and
        # doesn't read the post-switch `is None`/`is False` asserts as
        # unreachable.
        app._chat_state.pending_force_skill.append("deploy")
        app._chat_state.pending_workflow_run_id = _PENDING_WF
        app._chat_state.expecting_user_input_response = _TRUTHY

        await app._open_new_session()
        await pilot.pause()

        assert app._chat_state.pending_force_skill == []
        assert app._chat_state.pending_workflow_run_id is None
        assert app._chat_state.expecting_user_input_response is False


@pytest.mark.asyncio
async def test_new_session_proceeds_and_discards_queued_workflow(
    tmp_path: Path,
) -> None:
    """feat-127: a QUEUED workflow no longer blocks `/new` — it's discarded.

    Pre-feat-127 a queued run blocked the switch (it would promote + be lost).
    Now `/new` retires (discards) it and proceeds; the registry is reset.
    """
    app, _store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        old_ses = app.session.session_id
        reg = app._service_registry
        assert reg is not None
        wf = cast("Any", reg.workflows)
        wf._workflow_registry[
            WorkflowRunId("wrk_00000000-0000-0000-0000-0000000000ee")
        ] = SimpleNamespace(phase="queued")

        await app._open_new_session()
        await pilot.pause()

        assert app.session.session_id != old_ses  # proceeded — not blocked
        assert wf._workflow_registry == {}  # queued run discarded + reset


@pytest.mark.asyncio
async def test_new_session_store_error_is_reported_not_raised(tmp_path: Path) -> None:
    """reviewer MINOR: a store failure degrades to a notice, not an app crash."""
    app, _store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        old_ses = app.session.session_id

        async def _boom(*args: Any, **kwargs: Any) -> Any:
            _ = (args, kwargs)
            raise RuntimeError("db down")

        app.session.runtime.store.create_session = _boom  # type: ignore[method-assign]

        await app._open_new_session()  # must NOT raise
        await pilot.pause()

        assert app.session.session_id == old_ses  # unchanged


@pytest.mark.asyncio
async def test_new_session_clears_transcript_infobar_and_adapter(
    tmp_path: Path,
) -> None:
    """reviewer coverage: transcript wiped, info bar updated, adapter maps wiped.

    Asserts the user-visible + adapter-state effects of the switch end-to-end
    (the unit test covers `reset_for_session` in isolation; this proves it is
    actually wired into `_open_new_session`).
    """
    app, _store = await _bootstrap_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)
        await transcript.mount(Static("SENTINEL", id="feat117-sentinel"))
        cast("Any", app.event_adapter)._card_widgets["crd"] = object()
        await pilot.pause()
        assert app.query("#feat117-sentinel")

        await app._open_new_session()
        await pilot.pause()

        new_ses = app.session.session_id
        # Transcript wiped — the pre-switch sentinel is gone.
        assert not app.query("#feat117-sentinel")
        # Info bar reflects the new session id.
        assert app.query_one(InfoBar).session == str(new_ses)[:8]
        # Adapter per-session state wiped end-to-end (not just in the unit test).
        assert not cast("Any", app.event_adapter)._card_widgets
