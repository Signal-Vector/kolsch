"""Pilot-based tests for `ScrollbackCue`.

Spec: `process/spec-textual-chat-ui.md` — `ScrollbackCue lifecycle (locked)`.

The widget is the simplest in the chat package: a `Static` with a
fixed dim rule string. Tests assert:

1. It mounts under an `App.run_test()` harness without error and
   renders the expected cue text.
2. `DEFAULT_CSS` is declared as a class attribute (Design Principle 2
   — modular; each widget owns its own CSS).
3. The declared CSS carries the four properties the spec locks in:
   `height: 1`, `text-align: center`, `color: $hint`, `text-style: dim`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cli.chat.widgets.scrollback_cue import ScrollbackCue

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget


def test_default_css_declared_on_class() -> None:
    """`DEFAULT_CSS` must be a class attribute (modular widget)."""
    assert isinstance(ScrollbackCue.DEFAULT_CSS, str)
    assert ScrollbackCue.DEFAULT_CSS.strip(), "DEFAULT_CSS should be non-empty"


@pytest.mark.parametrize(
    "fragment",
    [
        "height: 1",
        "text-align: center",
        "color: $hint",
        "text-style: dim",
    ],
)
def test_default_css_carries_locked_properties(fragment: str) -> None:
    """Every property locked by the spec must appear in DEFAULT_CSS."""
    assert fragment in ScrollbackCue.DEFAULT_CSS, (
        f"DEFAULT_CSS missing spec-locked fragment: {fragment!r}"
    )


def test_cue_text_matches_spec() -> None:
    """The rendered cue string must match the spec-locked wording."""
    assert ScrollbackCue.CUE_TEXT == "── earlier turns in scrollback ↑ ──"


@pytest.mark.asyncio
async def test_mounts_under_pilot(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Widget mounts under `App.run_test()` and renders the cue text."""
    widget = ScrollbackCue()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        rendered = str(widget.render())
        assert "earlier turns in scrollback" in rendered
        assert widget.styles.height.value == 1


@pytest.mark.asyncio
async def test_widget_is_one_shot_lifecycle(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """No sticky/docked behaviour — the widget participates in normal flow.

    The lifecycle lock in the spec is "one-shot, mounted once, never
    re-mounted, gets baked into terminal scrollback". At the widget
    level this manifests as: no `dock:` in DEFAULT_CSS (which would
    keep it pinned to the viewport). We assert that the widget's CSS
    does not opt into any dock position.
    """
    widget = ScrollbackCue()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        # `dock` should not be set — the widget flows with the transcript
        # and scrolls into scrollback like any other mounted widget.
        assert "dock:" not in ScrollbackCue.DEFAULT_CSS
