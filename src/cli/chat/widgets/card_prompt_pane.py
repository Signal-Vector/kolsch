# ruff: noqa: RUF002 — docstring documents the actual cursor glyphs it renders
"""`CardPromptPane` — inline prompt pane widget skeleton.

spec-007 §CardPromptPane. Three-`Static` skeleton (question row +
options row + cursor row) mounted inside a card's body when a
workflow approval fires against a source card. Wiring against
[`PromptsImpl`][cli.chat.services.prompts_impl.PromptsImpl] lands
with **spec-014** (workflow approval integration); this spec ships
the widget class alone so downstream code can construct + mount
it without a second spec landing first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import Vertical
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult


class CardPromptPane(Vertical):
    """Question + options + cursor line.

    Layout::

        {question}
        [opt1]  [opt2]  [opt3]
        › ▊

    Widget-only in this spec — no key bindings, no message routing.
    Spec-014 wires the pane into `PromptsImpl` so `choice()` awaits
    resolve here.
    """

    DEFAULT_CSS = """
    CardPromptPane {
        height: auto;
        padding: 0;
    }
    CardPromptPane > Static {
        height: 1;
    }
    """

    def __init__(
        self,
        *,
        question: str,
        options: list[str],
        id: str | None = None,  # noqa: A002 — matches Textual Widget's `id` kwarg
    ) -> None:
        super().__init__(id=id)
        self._question = question
        self._options = list(options)

    def compose(self) -> ComposeResult:
        """Yield the question row, the options row, and the cursor line."""
        # bug-040: `_question` is a constructor arg wired from
        # `PromptsImpl` (spec-014). Bracket content in the prompt
        # question would otherwise trip `MarkupError` — disable
        # markup for parity with the options row below.
        yield Static(
            self._question,
            classes="prompt-question",
            markup=False,
        )
        # `markup=False` — brackets like `[y]` would otherwise parse
        # as Textual markup tags (`[y]`, `[/n]`, etc.) and get
        # stripped from the render. Plain-text rendering keeps the
        # options row readable.
        yield Static(
            "  ".join(f"[{o}]" for o in self._options),
            classes="prompt-options",
            markup=False,
        )
        yield Static("› ▊", classes="prompt-cursor")  # noqa: RUF001 — intentional cursor glyph (visual input affordance)


__all__ = ["CardPromptPane"]
