"""ApprovalChoicePane — step 1 of the approval flow.

The four-choice row + a visible input prompt line ``› ▊``. Rendered as
a two-line ``Vertical`` so both a choice grid and the "waiting for
input" cursor are always visible — the earlier one-line ``Horizontal``
clipped to `[y]` on narrow terminals because it used ``width: auto`` on
a container that then couldn't fit all four choices.

Key handling stays keybinding-driven (single keypress selects) — the
``› ▊`` line is a *visual affordance* saying "I'm waiting for you",
not a real ``Input``. This keeps single-key flow while giving the eye
somewhere to land.

Framework-aligned:

- ``Vertical`` container, ``width: 1fr``, height sizes to content.
- ``Static`` children for the choice grid + prompt line.
- ``BINDINGS`` fire ``action_choose(key)`` for ``y``/``a``/``n``/``d``/
  ``escape``. Choice is signalled via a custom ``Chosen`` message; the
  pane itself does not resolve the approval.

Escape is delivered as ``key="esc"`` so the parent can map it to
deny-once (per ``_modal.py:152`` "Esc abandoned the rule write"
semantics).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult


class ApprovalChoicePane(Vertical):
    """Four-choice grid + prompt-cursor line. Keypress-driven."""

    # Fallback variable values so the widget renders correctly in
    # isolation (e.g. under Pilot tests) without theme.tcss loaded.
    # When theme.tcss IS loaded at the app level its definitions win.
    DEFAULT_CSS = """
    $assistant-fg: #b0c0d5;
    $user-border: #2ea043;
    $warning: #ffb454;
    $hint: #6e7681;

    ApprovalChoicePane {
        width: 1fr;
        height: auto;
    }
    ApprovalChoicePane > .choices {
        color: $assistant-fg;
        text-style: bold;
        height: 1;
    }
    ApprovalChoicePane > .prompt {
        color: $warning;
        text-style: bold;
        height: 1;
        margin-top: 1;
    }
    ApprovalChoicePane:focus > .prompt {
        color: $user-border;
    }
    """

    BINDINGS = [
        Binding("y", "choose('y')", "allow once", show=False),
        Binding("a", "choose('a')", "allow + rule", show=False),
        Binding("n", "choose('n')", "deny once", show=False),
        Binding("d", "choose('d')", "deny + rule", show=False),
        Binding("escape", "choose('esc')", "cancel", show=False),
    ]

    can_focus = True

    class Chosen(Message):
        """Posted when the user picks one of the four keys (or Esc)."""

        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    def compose(self) -> ComposeResult:
        yield Static(
            "[y] allow once   [a] allow + rule   [n] deny once   [d] deny + rule",
            classes="choices",
            markup=False,
        )
        # Visible cursor affordance. The ``▊`` character is a stable
        # width in monospace fonts and reads as "waiting for input"
        # without pretending to be a real Input widget.
        yield Static("› ▊", classes="prompt", markup=False)

    def action_choose(self, key: str) -> None:
        """Bindings handler — post the choice to the parent."""
        self.post_message(self.Chosen(key))
