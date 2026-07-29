"""qol-109 — `WorkflowNodeRow.mount_*` sinks cap their text at ~16 K chars.

Tests 1-7, 9 from the spec (Test 8 is the manual live-tracer profile):

- Test 1: ``mount_assistant_message`` caps a 100 K-char body → AssistantProse's
  ``source`` starts with 16 K ``A``s and ends with the exact JSONL-tail marker.
- Test 2: ``mount_tool_call``'s second-phase body update caps a 100 K-char body
  → the mounted ``ToolCallBlock`` body Static holds the capped text.
- Test 3: ``mount_user_message`` flat + content-dict branches both cap. The
  ``small`` key value in the dict branch is byte-identical passthrough.
- Test 4: ``mount_notice`` caps text before the ``NoticeWidget`` constructor.
- Test 5: short body (``"hello world"``) passes through byte-identical — no
  marker, no truncation.
- Test 6: main-chat ``ChatApp.mount_assistant_message`` is NOT capped — a
  100 K-char body mounts with all 100 K chars intact.
- Test 7: ``SessionStore.append_event`` preserves the full 100 K-char content
  on-disk; the row's mounted widget is still capped (cap is UI-only).
- Test 9: full suite green (run separately in CI).

Why char-count assertions and not byte-count: qol-108's ``_cap_with_tail`` is
char-count. Multi-byte UTF-8 does not shift the marker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.notice import NoticeWidget
from cli.chat.widgets.tool_call_block import ToolCallBlock
from cli.chat.widgets.user_chatbox import UserChatbox
from cli.chat.widgets.workflow_node_row import (
    _CAP_MARKER_TEMPLATE,
    WorkflowNodeRow,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


_QOL109_CAP = 16_000


class _Host(App[None]):
    """Minimal host app: a scroll container the row can mount into."""

    CSS = """
    Screen { height: 100vh; }
    #slot { height: 1fr; width: 100%; }
    """

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="slot")


def _expected_marker(original_len: int) -> str:
    """Compute the exact qol-108 marker for an ``original_len`` payload."""
    return _CAP_MARKER_TEMPLATE.format(n_more=original_len - _QOL109_CAP)


# ---------------------------------------------------------------------------
# Test 1: mount_assistant_message caps a 100 K-char body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_assistant_message_capped() -> None:
    """Test 1: 100 K ``A``s → 16 K ``A``s + JSONL-tail marker in ``.source``."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="score_resumes")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()
        # Expand so the queued mount closure runs live (collapsed rows
        # queue the closure without touching Contents; qol-109 caps the
        # text INSIDE that closure).
        row.collapsed = False
        await pilot.pause()

        big = "A" * 100_000
        await row.mount_assistant_message(big)
        await pilot.pause()

        prose_list = list(row.query(AssistantProse))
        assert len(prose_list) == 1, f"expected 1 AssistantProse, got {len(prose_list)}"
        source = prose_list[0].source

        marker = _expected_marker(len(big))
        assert marker == "\n… (84000 more chars; see session JSONL)"
        assert source.startswith("A" * _QOL109_CAP)
        assert source.endswith(marker)
        assert len(source) == _QOL109_CAP + len(marker)


# ---------------------------------------------------------------------------
# Test 2: mount_tool_call second-phase body update caps a 100 K-char body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_tool_call_body_capped() -> None:
    """Test 2: second-phase body update caps the 100 K-char payload.

    The first `mount_tool_call` call creates the section in ``running``
    state with an empty body; the second call (same ``event_id``)
    transitions to ``success`` and populates the body Static. The cap
    lands ONLY on the transition path.
    """
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="browse_url")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()
        row.collapsed = False
        await pilot.pause()

        # Phase 1: running (empty body).
        await row.mount_tool_call(
            "browse_url",
            'url="https://example.com"',
            "",
            event_id="tc-1",
        )
        await pilot.pause()

        # Phase 2: completed with a 100 K-char body.
        big = "B" * 100_000
        await row.mount_tool_call(
            "browse_url",
            'url="https://example.com"',
            big,
            event_id="tc-1",
        )
        await pilot.pause()

        blocks = list(row.query(ToolCallBlock))
        assert len(blocks) == 1, f"expected 1 ToolCallBlock, got {len(blocks)}"
        body_text = str(blocks[0]._body_static.content)

        marker = _expected_marker(len(big))
        assert body_text.startswith("B" * _QOL109_CAP)
        assert body_text.endswith(marker)
        assert len(body_text) == _QOL109_CAP + len(marker)


# ---------------------------------------------------------------------------
# Test 3: mount_user_message caps flat text + per-key content dict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_user_message_capped() -> None:
    """Test 3: flat branch AND content-dict branch both cap; ``small`` key passthrough."""
    # --- Sub-case A: flat branch -----------------------------------------
    app_a = _Host()
    async with app_a.run_test() as pilot:
        row = WorkflowNodeRow(node_name="score_resumes")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()
        row.collapsed = False
        await pilot.pause()

        big = "C" * 100_000
        await row.mount_user_message(big)
        await pilot.pause()

        boxes = list(row.query(UserChatbox))
        assert len(boxes) == 1, f"expected 1 UserChatbox (flat), got {len(boxes)}"
        text = boxes[0].text
        marker = _expected_marker(len(big))
        assert text.startswith("C" * _QOL109_CAP)
        assert text.endswith(marker)
        assert len(text) == _QOL109_CAP + len(marker)

    # --- Sub-case B: content-dict branch ---------------------------------
    app_b = _Host()
    async with app_b.run_test() as pilot:
        row = WorkflowNodeRow(node_name="score_resumes")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()
        row.collapsed = False
        await pilot.pause()

        big = "D" * 100_000
        await row.mount_user_message(
            text="fallback",
            content={"big": big, "small": "hi"},
        )
        await pilot.pause()

        boxes = list(row.query(UserChatbox))
        assert len(boxes) == 2, f"expected 2 UserChatboxes (dict), got {len(boxes)}"
        # Section titles: iterate top-level `input: <key>` NodeSections
        # via the row's Contents; UserChatboxes live inside those.
        # Robust probe: pair each chatbox by its text length.
        big_box = next(box for box in boxes if len(box.text) > 100)
        small_box = next(box for box in boxes if len(box.text) <= 100)

        marker = _expected_marker(len(big))
        assert big_box.text.startswith("D" * _QOL109_CAP)
        assert big_box.text.endswith(marker)
        assert len(big_box.text) == _QOL109_CAP + len(marker)
        # `small` key passes through unchanged.
        assert small_box.text == "hi"


# ---------------------------------------------------------------------------
# Test 4: mount_notice caps text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_notice_capped() -> None:
    """Test 4: `NoticeWidget` body text is capped at the row sink."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="template_warning")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()
        row.collapsed = False
        await pilot.pause()

        big = "E" * 100_000
        await row.mount_notice(notice_id="n1", text=big, level="warning")
        await pilot.pause()

        notices = list(row.query(NoticeWidget))
        assert len(notices) == 1, f"expected 1 NoticeWidget, got {len(notices)}"
        body = notices[0].text

        marker = _expected_marker(len(big))
        assert body.startswith("E" * _QOL109_CAP)
        assert body.endswith(marker)
        assert len(body) == _QOL109_CAP + len(marker)


# ---------------------------------------------------------------------------
# Test 5: short body passes through byte-identical
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_body_unchanged() -> None:
    """Test 5: short body under the cap → no marker, no truncation."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="tiny")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()
        row.collapsed = False
        await pilot.pause()

        await row.mount_assistant_message("hello world")
        await pilot.pause()

        prose_list = list(row.query(AssistantProse))
        assert len(prose_list) == 1
        assert prose_list[0].source == "hello world"


# ---------------------------------------------------------------------------
# Test 6: main-chat `ChatApp.mount_*` is NOT capped
# ---------------------------------------------------------------------------


class _MockStore:
    async def read_path_to_leaf(self, thread_id: Any) -> AsyncIterator[Any]:
        # empty-async-generator idiom
        if False:
            yield  # type: ignore[unreachable]
        return


class _MockAuthz:
    def register_prompt_callback(
        self, callback: Any, *, on_rule_added: Any = None
    ) -> None:
        return None

    def clear_prompt_callback(self) -> None:
        return None


class _MockEngine:
    async def execute_stream(self, config: Any, ctx: Any) -> AsyncIterator[Any]:
        if False:
            yield  # type: ignore[unreachable]
        return


class _MockRuntime:
    def __init__(self) -> None:
        self.store = _MockStore()
        self.authz = _MockAuthz()
        self.engine = _MockEngine()


@dataclass
class _MockConfig:
    name: str = "TestAgent"
    agent_id: str = "ag_mock"
    model: Any = None


@dataclass
class _MockBundle:
    runtime: Any
    config: Any
    session_id: str
    thread_id: str
    agent_ref: str
    root: Path
    model_label: str
    is_resume: bool


@pytest.mark.asyncio
async def test_main_chat_mount_body_unchanged(tmp_path: Path) -> None:
    """Test 6: `ChatApp.mount_assistant_message` retains the full 100 K chars."""
    from cli.chat.app import ChatApp

    bundle = _MockBundle(
        runtime=_MockRuntime(),
        config=_MockConfig(),
        session_id="ses_test",
        thread_id="th_test",
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    app = ChatApp(session=bundle, engine=bundle.runtime.engine, full_history=False)
    async with app.run_test() as pilot:
        await pilot.pause()

        big = "F" * 100_000
        await app.mount_assistant_message(big)
        await pilot.pause()

        # Main-chat transcript mounts an `AssistantProse` and appends
        # `text` via `.append(text)`. The full 100 K chars must land
        # in the widget's `.source` — no cap, no marker.
        prose = app._last_assistant_widget
        assert prose is not None
        source = prose.source
        assert len(source) == 100_000, (
            f"main-chat cap leak: expected 100_000 chars, got {len(source)}"
        )
        assert source == big
        marker_head = "\n… ("
        assert marker_head not in source

        await pilot.press("ctrl+d")
        await pilot.pause()


# ---------------------------------------------------------------------------
# Test 7: JSONL preserves full payload; row still caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jsonl_full_payload_preserved(tmp_path: Path) -> None:
    """Test 7: on-disk JSONL keeps all 100 K chars; row widget is capped."""
    from schemas.entities import Actor
    from schemas.events import Event, EventType
    from schemas.identifiers import EventId
    from store import SessionStore, SqliteBackend

    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()

    try:
        store = SessionStore(root=tmp_path, backend=backend)
        session = await store.create_session("ag_mock")
        threads = await store.list_threads(session.session_id)
        assert threads, "session must have at least one thread"
        thread_id = threads[0].thread_id

        big = "G" * 100_000
        event = Event(
            event_id=EventId(f"ev_{uuid4()}"),
            thread_id=thread_id,
            session_id=session.session_id,
            type=EventType.AGENT_MESSAGE,
            data={"content": {"type": "text", "text": big}},
            actor=Actor(kind="agent"),
            created_at=datetime.now(tz=UTC),
        )
        await store.append_event(thread_id, event)

        # Assert on-disk JSONL still carries all 100 K chars byte-for-byte.
        from store.jsonl import jsonl_path

        path = jsonl_path(tmp_path, session.session_id)
        raw = path.read_text()
        assert raw.count("G") >= 100_000, (
            f"expected on-disk JSONL to preserve 100_000 G's, got {raw.count('G')}"
        )
    finally:
        await backend.close()

    # Now drive the row mount with the same big content and confirm
    # the widget body is capped.
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="score_resumes")
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()
        row.collapsed = False
        await pilot.pause()

        await row.mount_assistant_message(big)
        await pilot.pause()

        prose_list = list(row.query(AssistantProse))
        assert len(prose_list) == 1
        source = prose_list[0].source
        marker = _expected_marker(len(big))
        assert source.startswith("G" * _QOL109_CAP)
        assert source.endswith(marker)
        assert len(source) == _QOL109_CAP + len(marker)
