"""bug-129 — answering an approval must NOT scroll-jump the transcript.

Root cause: the composer is disabled while an approval is pending, so
removing the FOCUSED pane let Textual's `Screen._reset_focus` hand
focus to the nearest previous focusable transcript widget (e.g. a
`[▸ …]` `WorkflowAnchorButton`) and scroll-CENTER it — jumping the
view mid-conversation and releasing the follow-the-tail anchor.

The fix is a focus-handoff policy: a guarded `screen.set_focus(None)`
release before each of `ApprovalBlock`'s three pane-removal sites, plus
successor selection in `ChatApp.recompute_awaiting_approval` (next
unresolved approval pane, else the composer). These tests assert the
view NEVER moves (no scroll calls exist to re-park it — the anchor must
be engaged-and-untouched, not merely at-bottom):

1. Single-approval resolutions (`y`/`n`/Esc) stay at the bottom;
   composer ends enabled + focused; the view still follows the tail.
2. The pattern-edit flow (`a`→Enter→`y` and the inner-Input leg
   `a`→Esc→`y`) is jump-free at every transition.
3. Stacked approvals hand focus to the next unresolved pane (fixes the
   pre-existing keyboard-dead bug with an interposed focusable).
4. `render_terminal` (live-cancel shape): no jump, `_resolved_event`
   never set, and deliberately-parked focus is NOT stolen.
5. feat-126 auto-approve: no jump and the composer regains focus.
6. Esc-deny still resolves `✗ Denied once` with no exchange-cancel
   fall-through (the bug-110-era CRITICAL contract).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from textual.containers import VerticalScroll

from bus.in_process import InProcessEventBus
from cli.chat.app import ChatApp
from cli.chat.widgets.approval_block import ApprovalBlock
from cli.chat.widgets.assistant_prose import AssistantProse, WorkflowAnchorButton
from cli.chat.widgets.composer import Composer
from schemas import Event
from schemas.entities import Actor
from schemas.events import EventType
from schemas.identifiers import EventId
from schemas.wire_kinds import WIRE_PROMPT_ANSWERED, WIRE_PROMPT_REQUESTED
from store import SessionStore, SqliteBackend

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path

    from textual.pilot import Pilot


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
async def make_chat_app(
    tmp_path: Path,
) -> AsyncGenerator[Callable[..., ChatApp], None]:
    """Factory: boot a `ChatApp` with live bus + real store.

    Returns a callable taking `auto_approve` so the feat-126 case can
    share the harness. The sqlite backend is closed at teardown.
    """
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_mock", name="bug129")
    threads = await store.list_threads(session.session_id)

    class _R:
        def __init__(self) -> None:
            self.store = store
            self.authz = _MockAuthz()
            self.engine = _MockEngine()
            self.root = tmp_path
            self.bus = InProcessEventBus()

    def _make(*, auto_approve: bool = False) -> ChatApp:
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
        return ChatApp(
            session=bundle,
            engine=bundle.runtime.engine,
            full_history=False,
            auto_approve=auto_approve,
        )

    try:
        yield _make
    finally:
        await backend.close()


_LONG_MD = "\n\n".join(f"paragraph {i} " + "lorem ipsum " * 10 for i in range(30))


async def _setup_transcript(pilot: Pilot[Any]) -> WorkflowAnchorButton:
    """Mount tall prose + a focusable anchor + more tall prose.

    Mirrors the diagnosis repro: the jump requires a focusable
    mid-transcript widget for `_reset_focus` to land on.
    """
    transcript = pilot.app.query_one("#transcript", VerticalScroll)
    await transcript.mount(AssistantProse(f"# reply 0\n\n{_LONG_MD}"))
    anchor_btn = WorkflowAnchorButton(label="wf/example", run_id="run_x")
    await transcript.mount(anchor_btn)
    for n in (1, 2):
        await transcript.mount(AssistantProse(f"# reply {n}\n\n{_LONG_MD}"))
    await pilot.pause()
    await pilot.pause()
    return anchor_btn


async def _publish_approval(
    pilot: Pilot[Any],
    prompt_id: str,
    *,
    allow_resolved: bool = False,
) -> ApprovalBlock:
    """Publish a live `WIRE_PROMPT_REQUESTED` and await the block mount.

    `allow_resolved=True` accepts an already-resolved block — needed
    for the feat-126 auto-approve case, which self-resolves within a
    frame of mounting (an unresolved snapshot may never be observed).
    """
    app = pilot.app
    ev = Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=app.session.thread_id,
        session_id=app.session.session_id,
        type=EventType.UI_EVENT,
        data={
            "schema_version": 1,
            "kind": WIRE_PROMPT_REQUESTED,
            "payload": {
                "prompt_id": prompt_id,
                "kind": "choice",
                "prompt": "Tool approval — bash ls",
                "options": ["y", "a", "n", "d"],
                "source": {"kind": "tool", "id": "t1"},
                "rule_id": "rl_129",
                "action": "bash",
                "target": "ls",
            },
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )
    await app.session.runtime.store.append_event(app.session.thread_id, ev)
    await app.session.runtime.bus.publish(ev)
    block: ApprovalBlock | None = None
    for _ in range(40):
        await pilot.pause()
        fresh = [
            b
            for b in pilot.app.query(ApprovalBlock)
            if allow_resolved or not b.is_resolved
        ]
        if fresh:
            block = fresh[-1]
            break
        await asyncio.sleep(0.05)
    assert block is not None, "ApprovalBlock did not mount"
    await pilot.pause()
    return block


async def _settle(pilot: Pilot[Any], *, seconds: float = 0.35) -> None:
    """Pump frames long enough for any deferred scroll_to_center to land.

    The buggy jump is a `call_later`-deferred scroll — asserting too
    early would pass vacuously.
    """
    steps = max(1, int(seconds / 0.05))
    for _ in range(steps):
        await pilot.pause()
        await asyncio.sleep(0.05)


def _assert_bottom_anchored(pilot: Pilot[Any]) -> None:
    """At the bottom AND the anchor was never touched (not re-engaged)."""
    transcript = pilot.app.query_one("#transcript", VerticalScroll)
    assert transcript.scroll_y == transcript.max_scroll_y, (
        f"jumped: scroll_y={transcript.scroll_y} max={transcript.max_scroll_y}"
    )
    assert transcript._anchored is True
    assert transcript._anchor_released is False, (
        "anchor was released — a scroll fired somewhere"
    )


def _composer(pilot: Pilot[Any]) -> Composer:
    return pilot.app.query_one(Composer)


async def _answered_responses(
    store: SessionStore,
    thread_id: Any,
    prompt_id: str,
) -> list[str]:
    found: list[str] = []
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") != WIRE_PROMPT_ANSWERED:
            continue
        payload = data.get("payload")
        if isinstance(payload, dict) and payload.get("prompt_id") == prompt_id:
            found.append(str(payload.get("response")))
    return found


# --------------------------------------------------------------------
# Test 1 — single-approval resolutions stay at the bottom (AC #1/#2/#9)
# --------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["y", "n", "escape"])
async def test_resolution_stays_at_bottom_and_focuses_composer(
    make_chat_app: Callable[..., ChatApp],
    key: str,
) -> None:
    app = make_chat_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _setup_transcript(pilot)
        await _publish_approval(pilot, f"prm_{uuid4().hex[:8]}")
        _assert_bottom_anchored(pilot)

        await pilot.press(key)
        await _settle(pilot)

        _assert_bottom_anchored(pilot)
        composer = _composer(pilot)
        assert composer.disabled is False
        assert pilot.app.focused is composer, (
            f"focused={pilot.app.focused!r} — successor handoff missing"
        )
        # Typed characters land in the composer with no mouse/Tab.
        await pilot.press("x")
        await pilot.pause()
        assert composer.text == "x"

        # AC #9: the view still FOLLOWS the tail (anchor engaged, not
        # merely at-bottom by coincidence).
        transcript = pilot.app.query_one("#transcript", VerticalScroll)
        await transcript.mount(AssistantProse(f"# tail\n\n{_LONG_MD}"))
        await _settle(pilot, seconds=0.2)
        _assert_bottom_anchored(pilot)


# --------------------------------------------------------------------
# Test 2 — pattern-edit flow jump-free, inner-Input covered (AC #4)
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pattern_flow_is_jump_free(
    make_chat_app: Callable[..., ChatApp],
) -> None:
    """`a` → Enter → `y`: every `_swap_pane` transition stays at bottom."""
    app = make_chat_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _setup_transcript(pilot)
        await _publish_approval(pilot, f"prm_{uuid4().hex[:8]}")

        await pilot.press("a")  # → pattern edit (removes choice pane)
        await _settle(pilot)
        _assert_bottom_anchored(pilot)

        await pilot.press("enter")  # → save confirm (removes Input pane)
        await _settle(pilot)
        _assert_bottom_anchored(pilot)

        await pilot.press("y")  # save → resolve
        await _settle(pilot)
        _assert_bottom_anchored(pilot)
        assert pilot.app.focused is _composer(pilot)


@pytest.mark.asyncio
async def test_pattern_escape_leg_is_jump_free(
    make_chat_app: Callable[..., ChatApp],
) -> None:
    """`a` → Esc → `y`: the focused INNER Input leg (descendant guard)."""
    app = make_chat_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _setup_transcript(pilot)
        await _publish_approval(pilot, f"prm_{uuid4().hex[:8]}")

        await pilot.press("a")
        await _settle(pilot)
        _assert_bottom_anchored(pilot)

        # Esc inside the pattern editor: focus is on the pane's inner
        # Input, not the pane — a `focused is pane` guard misses this.
        await pilot.press("escape")
        await _settle(pilot)
        _assert_bottom_anchored(pilot)

        await pilot.press("y")
        await _settle(pilot)
        _assert_bottom_anchored(pilot)
        assert pilot.app.focused is _composer(pilot)


# --------------------------------------------------------------------
# Test 3 — stacked approvals hand focus to the next pane (AC #3)
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stacked_approvals_hand_focus_to_next_pane(
    make_chat_app: Callable[..., ChatApp],
) -> None:
    app = make_chat_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _setup_transcript(pilot)
        store = pilot.app.session.runtime.store
        thread_id = pilot.app.session.thread_id

        prm_a = f"prm_{uuid4().hex[:8]}"
        prm_b = f"prm_{uuid4().hex[:8]}"
        block_a = await _publish_approval(pilot, prm_a)
        # Interposed focusable BETWEEN the blocks — the shape in which
        # the pre-fix `_reset_focus` fallback made block A
        # keyboard-dead today.
        transcript = pilot.app.query_one("#transcript", VerticalScroll)
        await transcript.mount(
            WorkflowAnchorButton(label="wf/between", run_id="run_b"),
        )
        block_b = await _publish_approval(pilot, prm_b)
        assert block_a is not block_b

        # Focus sits on the LAST-mounted block's pane; `y` resolves B.
        await pilot.press("y")
        await _settle(pilot)
        _assert_bottom_anchored(pilot)
        assert bool(block_b.is_resolved) is True
        # `bool(...)` widens so mypy doesn't narrow to Literal[False]
        # and mark the post-second-`y` asserts unreachable (the
        # spec-111-era trap).
        assert bool(block_a.is_resolved) is False
        # The successor is block A's pane — immediately answerable.
        assert pilot.app.focused is block_a._current_pane, (
            f"focused={pilot.app.focused!r} — next-pane handoff missing"
        )
        assert _composer(pilot).disabled is True

        await pilot.press("y")
        await _settle(pilot)
        _assert_bottom_anchored(pilot)
        assert block_a.is_resolved is True
        composer = _composer(pilot)
        assert composer.disabled is False
        assert pilot.app.focused is composer

        # Exactly one durable answer per prompt (bug-110 hazard).
        assert await _answered_responses(store, thread_id, prm_a) == ["y"]
        assert await _answered_responses(store, thread_id, prm_b) == ["y"]


# --------------------------------------------------------------------
# Test 4 — render_terminal: no jump, no steal, no _resolved_event (AC #5)
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_terminal_focused_pane_no_jump(
    make_chat_app: Callable[..., ChatApp],
) -> None:
    app = make_chat_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _setup_transcript(pilot)
        block = await _publish_approval(pilot, f"prm_{uuid4().hex[:8]}")
        _assert_bottom_anchored(pilot)

        # Live-cancel shape: flip terminal + the cancelled-path
        # recompute (mirrors `_handle_prompt_cancelled`).
        await block.render_terminal(allowed=None, footer="⊘ Interrupted")
        pilot.app.recompute_awaiting_approval()
        await _settle(pilot)

        _assert_bottom_anchored(pilot)
        # bug-110 contract: the waiter channel is never woken.
        assert block._resolved_event.is_set() is False
        composer = _composer(pilot)
        assert composer.disabled is False
        assert pilot.app.focused is composer


@pytest.mark.asyncio
async def test_render_terminal_does_not_steal_parked_focus(
    make_chat_app: Callable[..., ChatApp],
) -> None:
    app = make_chat_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        anchor_btn = await _setup_transcript(pilot)
        block = await _publish_approval(pilot, f"prm_{uuid4().hex[:8]}")

        # User deliberately parks focus on a transcript widget (the
        # parking scroll is user-initiated — not asserted against).
        anchor_btn.focus()
        await _settle(pilot, seconds=0.2)
        assert pilot.app.focused is anchor_btn
        transcript = pilot.app.query_one("#transcript", VerticalScroll)
        parked_scroll_y = transcript.scroll_y

        await block.render_terminal(allowed=None, footer="⊘ Interrupted")
        pilot.app.recompute_awaiting_approval()
        await _settle(pilot)

        # Focus NOT stolen; the view did not move further.
        assert pilot.app.focused is anchor_btn
        assert transcript.scroll_y == parked_scroll_y


# --------------------------------------------------------------------
# Test 5 — feat-126 auto-approve regains the composer (AC #6)
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_approve_regains_composer_focus(
    make_chat_app: Callable[..., ChatApp],
) -> None:
    app = make_chat_app(auto_approve=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _setup_transcript(pilot)
        composer = _composer(pilot)
        composer.focus()
        await pilot.pause()
        assert pilot.app.focused is composer

        prm = f"prm_{uuid4().hex[:8]}"
        block = await _publish_approval(pilot, prm, allow_resolved=True)
        # Wait for the self-resolve (no keypress anywhere).
        for _ in range(40):
            await pilot.pause()
            if block.is_resolved:
                break
            await asyncio.sleep(0.05)
        assert block.is_resolved is True
        await _settle(pilot)

        _assert_bottom_anchored(pilot)
        # Pre-fix: the composer-disable blur left focus None forever.
        assert composer.disabled is False
        assert pilot.app.focused is composer


# --------------------------------------------------------------------
# Test 6 — Esc-deny CRITICAL contract regression (AC #7)
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escape_deny_no_exchange_cancel_fallthrough(
    make_chat_app: Callable[..., ChatApp],
) -> None:
    app = make_chat_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _setup_transcript(pilot)
        store = pilot.app.session.runtime.store
        thread_id = pilot.app.session.thread_id
        prm = f"prm_{uuid4().hex[:8]}"
        block = await _publish_approval(pilot, prm)

        await pilot.press("escape")
        await _settle(pilot)

        footer = block.body_children[-1]
        assert "✗ Denied once" in str(footer.render())
        assert await _answered_responses(store, thread_id, prm) == ["n"]
        # No `[exchange failed — …]` error notice anywhere: Esc resolved
        # the approval, it did not fall through to a butler-turn cancel.
        from textual.widgets import Static

        error_texts = [
            str(w.render()) for w in pilot.app.query(Static) if w.has_class("error")
        ]
        assert not error_texts, f"unexpected error notices: {error_texts}"
        _assert_bottom_anchored(pilot)
