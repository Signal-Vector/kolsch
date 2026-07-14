"""ApprovalArgsRow — expandable ``Args`` row for the tool-approval block.

qol-047. The tool-approval prompt (`ApprovalBlock`) renders a three-row
context header:

    Tool  bash
    Args  <target string>
    Rule  <matched rule id>

The `Args` row previously used a plain ``Static`` with ``height: 1``, so
any target longer than the visible row silently truncated. Users can't
see the full command they're authorizing — the reported feedback:
"99% of the time I'm approving things while I cannot see the entire
code I'm authorizing."

This widget replaces the plain Static with an auto-height row that:

- Renders `Args  <target>` on a single line when the target fits.
- Detects overflow on first mount + on ``on_resize``.
- When overflowing + collapsed, ends the visible row with ``…▸`` so
  the truncation is legible (not just the last visible char) and the
  chevron affordance is unmissable.
- When expanded, shows `Args  ▾` on the first line and wraps the full
  target on subsequent lines, left-indented 6 columns to align under
  the target text.
- Toggles on Tab (bound at the parent ``ApprovalBlock`` level) or on
  mouse click (this widget's ``on_click`` handler).
- Posts a ``Toggled`` message on every toggle so the parent can update
  its footer hint.

Non-goals (spec-locked out-of-scope):

- No same treatment for ``Rule`` or ``Tool`` rows.
- No syntax highlighting inside the expanded body — matches the dim
  ``.context`` styling used by ``Tool``/``Rule``.
- No persisting expand state across future approval prompts.

Design notes:

- Deliberately does NOT reuse Textual's ``Collapsible``. Its chrome
  (title-bar chevron on the far left, separator below) is wrong for
  a single-row context header. See the spec's Coder role warning.
- All Statics use ``markup=False`` per the bug-040 sweep — tool
  arguments frequently contain ``[...]`` sequences that would blow
  up Rich markup parsing.
- Chevron glyphs are ``▸`` (collapsed) and ``▾`` (expanded); stable
  monospace, no Rich markup dependency.
"""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

# Prefix used by all three context rows (Tool/Args/Rule). The two
# trailing spaces align target text at column 6 — the expanded body
# uses the same indent.
_PREFIX = "Args  "
# Reserve one column for the chevron; a second is reserved for the
# ellipsis character. Overflow detection has to subtract both from
# the available width so the collapsed row leaves room for `…▸`.
_CHEVRON_COLLAPSED = "▸"
_CHEVRON_EXPANDED = "▾"
_ELLIPSIS = "…"


class ApprovalArgsRow(Widget):
    """Auto-height row: ``Args  <target>`` with a chevron on overflow.

    Public API:

    - Constructor: ``ApprovalArgsRow(target: str)``.
    - ``expanded`` (property): current expansion state.
    - ``overflowing`` (property): computed overflow, post-mount.
    - ``action_toggle_expand()``: flip expansion, post ``Toggled`` message.
    - ``on_click``: same as ``action_toggle_expand``.
    - ``on_resize``: recompute ``overflowing``.
    - Message ``Toggled(expanded: bool, overflowing: bool)``: fired on
      every toggle (parent updates footer hint).
    """

    DEFAULT_CSS = """
    $assistant-fg: #b0c0d5;

    ApprovalArgsRow {
        color: $assistant-fg;
        height: auto;
        width: 1fr;
    }
    ApprovalArgsRow > .args-line {
        height: auto;
        width: 1fr;
    }
    """

    class Toggled(Message):
        """Posted whenever expansion state flips.

        The parent block listens with
        ``on_approval_args_row_toggled`` and adjusts the footer hint.
        Carries both current ``expanded`` state and the (freshly
        computed) ``overflowing`` state so the parent doesn't have to
        query the widget for it.
        """

        def __init__(self, *, expanded: bool, overflowing: bool) -> None:
            super().__init__()
            self.expanded = expanded
            self.overflowing = overflowing

    def __init__(self, target: str) -> None:
        super().__init__()
        # Full target string — stashed verbatim so re-render after
        # toggle can rebuild either the collapsed or the expanded view
        # from the same source of truth.
        self._target = target
        # Expansion state — flipped by ``action_toggle_expand`` (Tab or click).
        self._expanded = False
        # Overflow flag — computed at first ``on_show`` / ``on_resize``.
        # Fallback default is False; if size is zero at first frame
        # ``_recompute_overflow`` returns without changing this. We
        # deliberately default False (not True) — showing the chevron
        # only when we can prove overflow avoids the "always-visible
        # chevron" ugliness even in the rare zero-size first frame,
        # and Textual will fire an ``on_resize`` as soon as the actual
        # width lands.
        self._overflow = False
        # Cached width the last recompute used — cheap guard against
        # redundant re-renders when ``on_resize`` fires without a
        # meaningful width change.
        self._last_width = -1

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def expanded(self) -> bool:
        """Whether the full target text is currently visible."""
        return self._expanded

    @property
    def overflowing(self) -> bool:
        """Whether the target string overflows the collapsed row width.

        Only meaningful post-mount — when ``size.width`` is populated.
        Before mount / on the zero-size first frame this stays False.
        """
        return self._overflow

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Single Static child holds the whole rendered body. Static's
        # ``update`` is cheap and avoids the mount-tree churn of
        # remove+re-yield. ``markup=False`` is mandatory: target may
        # contain ``[...]`` sequences (bug-040 rule).
        yield Static(
            self._rendered_text(),
            classes="args-line",
            markup=False,
        )

    def _rendered_text(self) -> str:
        """Return the text the child Static should currently display.

        - Not overflowing: ``Args  <target>`` verbatim. Behaviour
          identical to the pre-qol-047 Static.
        - Overflowing + collapsed: truncate target so ``Args  …▸``
          fits on one row.
        - Overflowing + expanded: ``Args  ▾`` first line, then the
          full target wrapped, indented by len(_PREFIX)=6.
        """
        if not self._overflow:
            return f"{_PREFIX}{self._target}"
        if self._expanded:
            # Left-indent lines 2+ to the ``A`` column (6 spaces) so
            # the wrapped body sits under the target start column. A
            # single Static with newlines lets Textual do the width
            # wrapping — we only insert soft-wrapped lines manually
            # so the indent is respected on every visible row.
            indent = " " * len(_PREFIX)
            body = self._wrap_target_body(indent)
            return f"{_PREFIX}{_CHEVRON_EXPANDED}\n{body}"
        # Collapsed + overflowing: truncate target so the row ends
        # with ``…▸`` and everything fits inside ``size.width``.
        # Reserve 2 cells for ``…▸`` after the prefix.
        width = self._last_width
        # Reserve: prefix + ellipsis + chevron.
        reserve = len(_PREFIX) + len(_ELLIPSIS) + len(_CHEVRON_COLLAPSED)
        visible = max(0, width - reserve)
        truncated = self._target[:visible]
        return f"{_PREFIX}{truncated}{_ELLIPSIS}{_CHEVRON_COLLAPSED}"

    def _wrap_target_body(self, indent: str) -> str:
        """Word-wrap ``self._target`` into indented lines.

        Simple char-based wrap — the target may not have whitespace
        (URL-encoded bodies, JSON blobs), so word-wrap would leave
        long unbroken segments overflowing again. Char-wrap fits
        every line into (width - indent).
        """
        width = self._last_width if self._last_width > 0 else len(self._target) + len(indent)
        inner = max(1, width - len(indent))
        if not self._target:
            return indent
        lines: list[str] = []
        i = 0
        while i < len(self._target):
            lines.append(indent + self._target[i : i + inner])
            i += inner
        return "\n".join(lines)

    def _refresh_static(self) -> None:
        """Push the current rendered text into the child Static."""
        try:
            static = self.query_one(".args-line", Static)
        except Exception:  # noqa: BLE001 — during compose the child may not exist yet
            return
        static.update(self._rendered_text())

    # ------------------------------------------------------------------
    # Overflow detection
    # ------------------------------------------------------------------

    def _recompute_overflow(self) -> bool:
        """Recompute ``self._overflow`` from current width + target shape.

        A target counts as overflowing when EITHER (a) its raw length
        exceeds the available row width OR (b) it contains an embedded
        newline. The newline case (qol-048) is chrome consistency —
        Textual renders `\\n`-carrying targets as multi-line blocks
        regardless of terminal width, so a "not overflowing" verdict
        would strip the chevron + `Tab → expand args` footer hint even
        though the row visibly wraps.

        Returns True when the flag changed (so callers can refresh).
        """
        width = self.size.width
        if width <= 0:
            # First-frame zero-size case — leave the flag alone. Textual
            # will fire another ``on_resize`` as soon as the width lands.
            return False
        self._last_width = width
        # Available width for target text: total width minus prefix.
        # We deliberately don't reserve the 2 chevron/ellipsis cells
        # here — the row _overflows_ when the un-chevron-adorned line
        # would exceed width. Reserving those cells would flag borderline
        # short strings as overflowing (chevron appears for text that
        # DOES fit on its own).
        available = max(0, width - len(_PREFIX))
        # qol-048 — treat any embedded newline as overflow so the
        # chevron + Tab hint remain visible on multi-line targets
        # (heredoc bash, formatted JSON). Without this, a short but
        # multi-line target renders across multiple rows with no
        # discoverability affordance — the chrome loses parity with
        # the actual render shape. ``"\n" in`` also catches ``\r\n``
        # because ``\n`` is a substring of ``\r\n``.
        new_flag = len(self._target) > available or "\n" in self._target
        if new_flag != self._overflow:
            self._overflow = new_flag
            return True
        return False

    def on_show(self) -> None:
        """Compute overflow the first time the widget is visible.

        ``on_mount`` may fire before layout has assigned ``size``;
        ``on_show`` fires after the compositor has laid the widget
        out so ``self.size.width`` is populated.
        """
        if self._recompute_overflow():
            self._refresh_static()
            self._notify_overflow_change()

    def on_resize(self, event: events.Resize) -> None:
        """Re-check overflow when the container width changes."""
        del event  # unused — we read self.size directly
        if self._recompute_overflow():
            self._refresh_static()
            self._notify_overflow_change()

    def _notify_overflow_change(self) -> None:
        """Round-2 fix — tell the parent the overflow flag flipped.

        Without this the parent's footer hint gets stuck: on a terminal
        resize from narrow → wide the row's own chevron disappears
        (this widget re-renders), but ``ApprovalBlock`` still shows
        ``Tab → expand args`` because the block only listens for
        ``Toggled`` messages. Reuses the existing ``Toggled`` payload
        + the existing ``on_approval_args_row_toggled`` handler.

        No loop risk: ``_recompute_overflow`` is only called from
        layout callbacks (``on_show`` / ``on_resize``), never from
        the parent's handler. The parent's handler calls back into
        ``_args_footer_suffix`` which only reads state — no re-entry.
        """
        # If the row is currently expanded but no longer overflowing,
        # snap it back to collapsed — an expanded body with no chevron
        # would be dead UI (Tab has nothing to toggle back to). Then
        # the parent's footer suffix goes empty on next paint anyway.
        if not self._overflow and self._expanded:
            self._expanded = False
            self._refresh_static()
        self.post_message(
            self.Toggled(
                expanded=self._expanded,
                overflowing=self._overflow,
            ),
        )

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    # Named ``action_toggle_expand`` rather than ``action_toggle`` —
    # ``DOMNode`` already ships ``action_toggle(attribute_name: str)``
    # for reactive toggles, and overriding it with a zero-arg signature
    # fails mypy strict (Signature-of-supertype error). The public API
    # in the qol-047 spec used ``action_toggle``; we route the block's
    # ``Binding("tab", "toggle_args", ...)`` through
    # ``ApprovalBlock.action_toggle_args`` which forwards to this method,
    # so the naming difference is invisible to callers outside the widget.
    def action_toggle_expand(self) -> None:
        """Flip expansion state, re-render, and notify the parent.

        No-op when the row is not overflowing — Tab on a short target
        must not visually change anything (AC #3 tail).
        """
        if not self._overflow:
            # Still post a Toggled message so the parent can distinguish
            # "user pressed Tab but nothing to do" from "not pressed at
            # all". Keeps the API predictable. Parent's footer-hint
            # update ignores non-overflowing toggles.
            self.post_message(self.Toggled(expanded=False, overflowing=False))
            return
        self._expanded = not self._expanded
        self._refresh_static()
        self.post_message(
            self.Toggled(expanded=self._expanded, overflowing=True),
        )

    def on_click(self, event: events.Click) -> None:
        """Mouse click anywhere on the row toggles expansion.

        Does NOT ``event.stop()`` propagation to focus mechanics —
        AC #4 requires the currently-focused pane keep focus after
        a mouse click. Textual clicks on non-focusable widgets don't
        change focus by default (this widget has no ``can_focus``
        override, so it's not focus-eligible). Safe.
        """
        del event
        self.action_toggle_expand()
