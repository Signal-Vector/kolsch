"""``SlashPopup`` — inline autocomplete panel for the composer.

Renders directly above the composer whenever the user types a
prefix starting with ``/``. Reads its command catalog from
``cli._slash.COMMAND_REGISTRY`` — the same registry the legacy
prompt_toolkit completer used — so the visible commands stay in
lockstep with what's actually registered without a parallel list
to maintain.

Behaviour:

- Empty text or text not starting with ``/`` → widget hides itself
  via CSS class ``.-hidden``.
- Text starting with ``/`` → the widget filters
  ``COMMAND_REGISTRY`` by prefix match against the head token
  (everything up to the first space), and renders one row per
  match:

    ``{name:<12}  {description}``

  A trailing footer line shows the ``usage`` of the highest-
  ranked match — mirroring the legacy popup's "type any model
  ref" style hint. This gives the user two things at once:
  discovery (the list) and parameter guidance (the usage line).

Discovery is the whole point. If the popup ever gets clever
(fuzzy matching, keyboard navigation, tab-completion into the
composer), the follow-up spec is ``qol-slash-textual-completer.md``.

Design principles (from ``process/spec-textual-chat-ui.md``):

1. Framework-aligned — a ``Vertical`` of ``Static`` rows with a
   reactive ``prefix`` attribute. No custom rendering primitives.
2. Modular — one file, its own ``DEFAULT_CSS``, its own Pilot
   test.
3. Do not reinvent — reads from ``COMMAND_REGISTRY``. The registry
   is the source of truth; the popup is a view.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Static

from cli._slash import COMMAND_REGISTRY, SlashCommand


def _head_token(text: str) -> str:
    """Return the leading token — up to the first space."""
    space = text.find(" ")
    return text if space == -1 else text[:space]


def _match_commands(prefix: str) -> list[SlashCommand]:
    """Return commands whose name starts with ``prefix``.

    Sorted alphabetically. Aliases (e.g. ``/quit`` for ``/exit``)
    are surfaced as their own row for full discovery.
    """
    if not prefix.startswith("/"):
        return []
    matches: list[SlashCommand] = []
    for entry in COMMAND_REGISTRY.values():
        names = (entry.name, *entry.aliases)
        for name in names:
            if name.startswith(prefix):
                matches.append(
                    entry
                    if name == entry.name
                    else SlashCommand(
                        name=name,
                        description=f"alias for {entry.name}",
                        usage=entry.usage,
                    )
                )
                break
    return sorted(matches, key=lambda c: c.name)


class SlashPopup(Vertical):
    """Inline slash-command autocomplete panel.

    Mounted above the composer inside the bottom-stack. Toggles its
    own visibility based on the ``prefix`` reactive: whenever
    ``prefix`` starts with ``/`` AND at least one command matches,
    the popup shows the matches; otherwise it hides itself via the
    ``.-hidden`` CSS class.
    """

    DEFAULT_CSS = """
    $tool-fg: #4dd0e1;
    $hint: #6e7681;
    $assistant-fg: #b0c0d5;
    $warning: #ffb454;

    SlashPopup {
        height: auto;
        max-height: 10;
        background: #1a1d24;
        padding: 0 1;
        display: none;
    }
    SlashPopup.-visible {
        display: block;
    }
    SlashPopup > .row {
        color: $assistant-fg;
        height: 1;
    }
    SlashPopup > .row-name {
        color: $tool-fg;
        text-style: bold;
    }
    SlashPopup > .usage {
        color: $warning;
        text-style: italic;
        height: 1;
    }
    SlashPopup > .empty {
        color: $hint;
        text-style: italic dim;
        height: 1;
    }
    """

    # `prefix` drives filtering. Parents set this on every composer
    # text change; the widget refreshes its rows via `watch_prefix`.
    prefix: reactive[str] = reactive("", layout=True)

    ROW_NAME_WIDTH: ClassVar[int] = 12

    def compose(self) -> ComposeResult:  # pragma: no cover - trivial
        # Compose starts empty; watch_prefix populates rows on first
        # meaningful update. Keeping compose empty avoids "flash of
        # empty popup" on mount before the first update.
        yield from ()

    def watch_prefix(self, prefix: str) -> None:
        """Rebuild the visible rows whenever the prefix changes.

        A prefix that doesn't start with ``/`` collapses the popup
        (CSS class removal). A prefix that starts with ``/`` but
        matches nothing renders a single dim italic "no match"
        row so the user knows they're on their own.
        """
        # Wipe existing children — the popup is fully re-rendered
        # each update. The command list is small (~12 items) so this
        # is cheap and keeps the logic obvious.
        for child in list(self.children):
            child.remove()

        if not prefix.startswith("/"):
            self.remove_class("-visible")
            return

        matches = _match_commands(prefix)
        if not matches:
            self.add_class("-visible")
            self.mount(
                Static(
                    f"no slash command matches {prefix!r}",
                    classes="empty",
                )
            )
            return

        self.add_class("-visible")
        for cmd in matches:
            self.mount(
                Static(
                    f"{cmd.name:<{self.ROW_NAME_WIDTH}} {cmd.description}",
                    classes="row",
                )
            )
        # The usage footer targets whichever match is closest —
        # the alphabetically-first hit is a fine proxy while the
        # popup is non-interactive.
        self.mount(Static(matches[0].usage, classes="usage"))
