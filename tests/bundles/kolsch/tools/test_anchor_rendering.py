"""feat-018 Test 4 — anchor rendering + scroll.

Three passes:

- ``AssistantProse.append`` with anchor markup mounts a
  :class:`WorkflowAnchorButton` inline in the prose flow.
- Pressing Enter on the focused button triggers
  ``EventAdapter._scroll_to_workflow_card`` on the app's adapter.
- An anchor with a ``run_id`` unknown to the adapter renders the
  button, logs a warning on activation, and does not raise.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from cli.chat.services.workflows_impl import (
    WorkflowRunRecord,
    WorkflowsImpl,
)
from cli.chat.widgets.assistant_prose import (
    ANCHOR_PATTERN,
    AssistantProse,
    WorkflowAnchorButton,
)
from tests.bundles.kolsch.tools.conftest import (  # noqa: F401 — re-exports fixtures
    chat_app,
)
from workflows.ids import WorkflowRunId

if TYPE_CHECKING:
    from cli.chat.app import ChatApp


def test_anchor_pattern_matches_expected_shape() -> None:
    """Regex accepts ``[label](kolsch-anchor://workflow_card/<id>)``."""
    text = "See [▸ release](kolsch-anchor://workflow_card/wrk_1) now."
    matches = ANCHOR_PATTERN.findall(text)
    assert matches == [("▸ release", "wrk_1")]


def test_anchor_pattern_ignores_plain_markdown_links() -> None:
    """Plain markdown links are left alone."""
    text = "See [docs](https://example.com) for details."
    assert ANCHOR_PATTERN.search(text) is None


def test_split_anchors_ordering() -> None:
    """Prose + anchors + prose come out in source order."""
    text = (
        "Before "
        "[▸ a](kolsch-anchor://workflow_card/wrk_a)"
        " middle "
        "[▸ b](kolsch-anchor://workflow_card/wrk_b)"
        " after."
    )
    pieces = AssistantProse._split_anchors(text)
    kinds = [kind for kind, _, _, _ in pieces]
    assert kinds == ["prose", "anchor", "prose", "anchor", "prose"]
    assert pieces[0] == ("prose", "Before ", "", "")
    assert pieces[1] == ("anchor", "", "▸ a", "wrk_a")
    assert pieces[2] == ("prose", " middle ", "", "")
    assert pieces[3] == ("anchor", "", "▸ b", "wrk_b")
    assert pieces[4] == ("prose", " after.", "", "")


@pytest.mark.asyncio
async def test_anchor_button_mounts_inline_in_assistant_prose(
    chat_app: ChatApp,  # noqa: F811 — pytest fixture injection, not a redefinition
) -> None:
    """A message with anchor markup mounts a WorkflowAnchorButton."""
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        await pilot.app.mount_assistant_streaming_widget()
        prose = pilot.app.query(AssistantProse).first()
        await prose.append(
            "Here's the workflow: [▸ my-flow](kolsch-anchor://workflow_card/wrk_run1)",
        )
        await pilot.pause()
        buttons = prose.query(WorkflowAnchorButton)
        assert len(buttons) == 1
        button = buttons.first()
        assert button.run_id == "wrk_run1"


@pytest.mark.asyncio
async def test_anchor_button_activation_scrolls_to_card(
    chat_app: ChatApp,  # noqa: F811 — pytest fixture injection, not a redefinition
) -> None:
    """Enter on the anchor button routes to _scroll_to_workflow_card."""
    async with chat_app.run_test() as pilot:
        await pilot.pause()

        # Pre-populate the adapter's workflow registry + card map with
        # a synthetic run/card pair the anchor can target.
        adapter = pilot.app.event_adapter
        workflows_impl = pilot.app._service_registry.workflows
        assert isinstance(workflows_impl, WorkflowsImpl)

        run_id = WorkflowRunId(f"wrk_{uuid4()}")
        mock_card = MagicMock()
        mock_card.id = f"crd_{uuid4()}"
        workflows_impl._workflow_registry[run_id] = WorkflowRunRecord(
            run_id=run_id,
            ref="kolsch/workflows/anchor",
            phase="running",
            card=mock_card,
            created_at=datetime.now(tz=UTC),
        )
        # Register a widget-like entry in the adapter's card map.
        mock_widget = MagicMock()
        adapter._card_widgets[mock_card.id] = mock_widget

        # Ensure the adapter can see the workflows impl for the lookup.
        adapter.register_workflows_impl(workflows_impl)

        # Mount prose with an anchor pointing at that run_id.
        await pilot.app.mount_assistant_streaming_widget()
        prose = pilot.app.query(AssistantProse).first()
        await prose.append(
            f"Go: [▸ anchor](kolsch-anchor://workflow_card/{run_id})",
        )
        await pilot.pause()

        # Patch the transcript so scroll_to_widget records the call.
        transcript = pilot.app.query_one("#transcript")
        scrolled: list[object] = []

        def _record(widget: object, animate: bool = True) -> None:  # noqa: FBT001, FBT002 — matches scroll_to_widget signature
            _ = animate
            scrolled.append(widget)

        transcript.scroll_to_widget = _record  # type: ignore[method-assign]

        button = prose.query(WorkflowAnchorButton).first()
        button.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert scrolled == [mock_widget]


@pytest.mark.asyncio
async def test_anchor_button_unknown_run_id_logs_and_is_noop(
    chat_app: ChatApp,  # noqa: F811 — pytest fixture injection, not a redefinition
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown run_id: button renders, activation logs warning, no scroll."""
    async with chat_app.run_test() as pilot:
        await pilot.pause()
        await pilot.app.mount_assistant_streaming_widget()
        prose = pilot.app.query(AssistantProse).first()

        stray = f"wrk_{uuid4()}"
        await prose.append(
            f"Text: [▸ stray](kolsch-anchor://workflow_card/{stray})",
        )
        await pilot.pause()

        buttons = prose.query(WorkflowAnchorButton)
        assert len(buttons) == 1

        transcript = pilot.app.query_one("#transcript")
        scrolled: list[object] = []
        transcript.scroll_to_widget = (  # type: ignore[method-assign]
            lambda widget, animate=True: scrolled.append(widget)
        )

        with caplog.at_level(logging.WARNING, logger="cli.chat.event_adapter"):
            buttons.first().focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

        # Nothing scrolled.
        assert scrolled == []
        # A warning was emitted.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(stray in r.getMessage() for r in warnings)


# Silence unused-import warning for AsyncIterator kept for typing symmetry.
_ = AsyncIterator
