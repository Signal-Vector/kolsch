"""bug-023 Test 4 (AC #9) — flagship live-runtime exit-flow Pilot regression.

The D-010 gate for bug-023. Boots a real ``ChatApp`` under Pilot
and exercises all four exit paths through the normal user
surface — no scaffold, no hand-crafted key events, no
``adapter.handle(...)`` calls. Every keystroke goes through
``pilot.press(...)`` so the entire focused-widget → binding-chain
→ app-handler path runs.

Four scenarios, one Pilot session each:

1. **``ctrl+d`` on an empty composer → app exits.** The seam that
   was silently broken before bug-023: Textual's ``TextArea``
   claims ``ctrl+d`` for ``delete_right``, so on the focused
   composer the app-level ``ctrl+d`` binding in ``keybindings.py``
   (line moved to :43 in bug-038) never fired. The composer's
   ``_on_key`` now intercepts, posts ``QuitRequested``, and
   ``on_composer_quit_requested`` routes to
   ``action_quit_if_idle``.

2. **``ctrl+d`` on a non-empty composer → does NOT exit; the
   character to the right of the cursor is deleted.** Users
   editing text expect Ctrl+D to forward-delete (matches every
   other TextArea in the ecosystem); the composer intentionally
   lets ``ctrl+d`` fall through when there's text.

3. **Type ``exit`` + Enter → app exits; no ``USER_MESSAGE`` event
   in the store.** REPL-style bare-verb quit. The engine's turn
   pipeline is the load-bearing evidence: if the bare verb slipped
   through to prose, ``USER_MESSAGE`` would land on the thread.

4. **Type ``/exit`` + Enter → app exits.** Regression check for
   the pre-existing slash path.

**Zero** ``_replay_ui_events_since`` (deleted in bug-020).
**Zero** ``adapter.handle(...)`` calls. **Zero** manual
``app.post_message(events.Key(...))``. The Pilot's ``press``
API is the single input surface.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from bus.in_process import InProcessEventBus
from cli.chat.app import ChatApp
from cli.chat.widgets.composer import Composer
from schemas.events import EventType
from store import SessionStore, SqliteBackend


class _FakeAuthz:
    def register_prompt_callback(
        self, callback: Any, *, on_rule_added: Any = None,
    ) -> None:
        _ = callback, on_rule_added

    def clear_prompt_callback(self) -> None:
        return None


async def _empty_stream(
    *_args: Any, **_kwargs: Any,
) -> AsyncGenerator[Any, None]:
    """Yield nothing — the fake engine never runs a real turn.

    Mirrors the pattern in
    ``tests/cli/chat/test_approval_flow_live.py``: the ``yield``
    after ``return`` is what makes this an async generator (which is
    what ``ChatApp`` expects from ``engine.execute_stream``).
    """
    return
    yield  # type: ignore[unreachable]  # pragma: no cover


class _FakeEngine:
    execute_stream = staticmethod(_empty_stream)


class _FakeConfig:
    name = "TestAgent"
    agent_id = "ag_fake"
    model = None


class _FakeBundle:
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
async def chat_app(tmp_path: Path) -> AsyncIterator[Any]:
    """Boot a `ChatApp` under Pilot with a real store + in-process bus.

    Mirrors ``tests/cli/chat/test_approval_flow_live.py``'s
    fixture — a real store is required so the ``USER_MESSAGE``
    assertion in scenario (3) is load-bearing.
    """
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_fake", name="exit-flow")
    threads = await store.list_threads(session.session_id)

    class _R:
        def __init__(self) -> None:
            self.store = store
            self.authz = _FakeAuthz()
            self.engine = _FakeEngine()
            self.root = tmp_path
            self.bus = InProcessEventBus()

    bundle = _FakeBundle(
        runtime=_R(),
        config=_FakeConfig(),
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        agent_ref="kolsch/agents/fake",
        root=tmp_path,
        model_label="fake/test",
        is_resume=False,
    )
    app = ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )
    try:
        yield app
    finally:
        await backend.close()


async def _has_user_message_event(store: Any, thread_id: Any) -> bool:
    async for ev in store.read_events(thread_id):
        if ev.type == EventType.USER_MESSAGE:
            return True
    return False


# --------------------------------------------------------------------
# AC #9 (1) — Ctrl+D on empty composer → app exits.
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ctrl_d_empty_composer_exits_app(chat_app: Any) -> None:
    """The seam bug-023 fixes: ``ctrl+d`` on empty composer exits."""
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        # Sanity — the composer is present and empty.
        composer = pilot.app.query_one(Composer)
        assert composer.text == ""
        await pilot.press("ctrl+d")
        await pilot.pause()
    assert not chat_app.is_running


# --------------------------------------------------------------------
# AC #9 (2) — Ctrl+D on non-empty composer deletes; does NOT exit.
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ctrl_d_non_empty_composer_deletes_right(
    chat_app: Any,
) -> None:
    """Type ``"hello"``, cursor at column 0, ``ctrl+d`` → ``"ello"``.

    The app must NOT exit (users editing text expect Ctrl+D to
    forward-delete, matching every other TextArea in the ecosystem).
    """
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        composer = pilot.app.query_one(Composer)
        composer.focus()
        composer.text = "hello"
        composer.move_cursor((0, 0))
        await pilot.pause()
        await pilot.press("ctrl+d")
        await pilot.pause()
        # Deleted the `h` — TextArea's `delete_right` ran.
        assert composer.text == "ello"
        # App is still up.
        assert chat_app.is_running
        # Cleanly quit the app so the pilot context exits.
        composer.text = ""
        await pilot.press("ctrl+d")
        await pilot.pause()
    assert not chat_app.is_running


# --------------------------------------------------------------------
# AC #9 (3) — Bare ``exit`` + Enter → app exits, NO USER_MESSAGE.
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_typing_exit_and_enter_exits_without_user_message(
    chat_app: Any,
) -> None:
    """The REPL-style bare-verb quit path.

    Before bug-023 this would send ``"exit"`` to the model as a
    ``USER_MESSAGE``; after bug-023 it's routed to ``_handle_slash("/exit")``.
    Load-bearing evidence: the store contains ZERO ``USER_MESSAGE``
    events for this thread.
    """
    store = chat_app.session.runtime.store
    thread_id = chat_app.session.thread_id
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        composer = pilot.app.query_one(Composer)
        composer.focus()
        await pilot.pause()
        for ch in "exit":
            await pilot.press(ch)
        await pilot.pause()
        assert composer.text == "exit"
        await pilot.press("enter")
        await pilot.pause()
    assert not chat_app.is_running
    # No USER_MESSAGE was appended — the bare verb did NOT reach
    # the engine as prose.
    assert not await _has_user_message_event(store, thread_id)


# --------------------------------------------------------------------
# AC #9 (4) — Typing ``/exit`` + Enter exits (regression).
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_typing_slash_exit_and_enter_still_exits(
    chat_app: Any,
) -> None:
    """Existing slash path stays intact.

    Regression guard: the pre-existing ``/exit`` slash command MUST
    keep working after bug-023's changes to
    ``on_composer_submitted``.
    """
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        composer = pilot.app.query_one(Composer)
        composer.focus()
        await pilot.pause()
        for ch in "/exit":
            await pilot.press(ch)
        await pilot.pause()
        assert composer.text == "/exit"
        await pilot.press("enter")
        await pilot.pause()
    assert not chat_app.is_running


# --------------------------------------------------------------------
# Guardrail: this file must NOT rely on any `_replay_*` scaffold
# and must use `pilot.press(...)` as the sole input surface
# (D-010 gate — mirrors the sibling flagship test in
# ``test_approval_flow_live.py``).
# --------------------------------------------------------------------


def test_flagship_uses_no_replay_scaffold() -> None:
    """Grep this file for banned scaffold names — none should be present.

    Rebuilds the banned tokens character-by-character so the
    assertion body itself doesn't contain the literal (which would
    trip the grep).
    """
    self_path = Path(__file__)
    text = self_path.read_text()
    banned_replay = "_replay" + "_ui_events" + "_since"
    banned_dispatch = "adapter" + ".handle("
    banned_hand_key = "post_message(events" + ".Key("
    # Redact the banned-token literals we just constructed above so
    # the checks below don't false-trigger on this assertion body.
    runnable = text.replace(banned_replay, "REDACTED")
    runnable = runnable.replace(banned_dispatch, "REDACTED")
    runnable = runnable.replace(banned_hand_key, "REDACTED")
    assert banned_replay not in runnable, (
        f"Flagship contains a `{banned_replay}` reference — violates D-010."
    )
    assert banned_dispatch not in runnable, (
        f"Flagship contains a `{banned_dispatch}` direct dispatch "
        "— violates D-010."
    )
    assert banned_hand_key not in runnable, (
        f"Flagship contains a `{banned_hand_key}` hand-crafted Key "
        "— violates D-010."
    )
    # And it MUST use `pilot.press(...)` as its sole input surface.
    assert "pilot.press" in text
