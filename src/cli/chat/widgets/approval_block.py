"""ApprovalBlock — Option-C inline approval flow.

Framework-aligned three-step state machine. The block is a bordered
``Vertical`` container that swaps a single "current pane" child as the
user progresses:

  step 1  choose      ApprovalChoicePane        y/a/n/d/esc
  step 2  pattern     ApprovalPatternEdit       edit rule pattern
  step 3  save        ApprovalSaveConfirm       preview + y/n

Header shows a breadcrumb (``● choose ─ ○ pattern ─ ○ save``) with
filled dots for completed steps and hollow dots for pending. Context
lines (``Tool``, ``Args``, ``Rule``) sit above the pane and stay fixed
across step transitions. A footer hint tells the user which keys
advance / back out.

Design decision (LOCKED after user feedback): the ``from`` scope is
**not** user-editable. It defaults to the empty string (which the
authz provider treats as "any caller"); users always want this in
practice. The old ``ApprovalFromEdit`` pane is no longer used by this
container — the widget file stays in the repo for now so the standalone
tests don't have to be deleted, but no code path mounts it.

Each pane posts a small custom message when it's done; this container
listens using Textual's canonical ``on_<snake_widget>_<snake_message>``
naming convention and drives the state machine:

- ``on_approval_choice_pane_chosen``:
    * ``y`` → resolve as allow-once (fast path)
    * ``n`` / ``esc`` → resolve as deny-once (fast path)
    * ``a`` / ``d`` → advance to pattern edit; remember effect
- ``on_approval_pattern_edit_confirmed``: advance to save-confirm.
- ``on_approval_pattern_edit_cancelled``: back to the choice pane.
- ``on_approval_save_confirm_chosen``:
    * ``y`` → build the ``Rule`` and resolve with ``(effect, rule)``.
    * ``n`` / ``esc`` → resolve as the "fallback" effect (allow-once
      when the user was building an allow rule; deny-once for deny).

Resolution mounts a frozen footer ``Static`` and sets an internal
:class:`asyncio.Event`. Callers await :meth:`ApprovalBlock.resolved` to
get the ``PromptCallbackReturn`` tuple back — see
``src/cli/_modal.py:168`` for the contract shape.

The block **remains mounted** in the transcript after resolution
(same "mount and freeze" lifecycle as ``UserChatbox`` /
``ToolCallBlock``).
"""

from __future__ import annotations

import asyncio
import contextlib

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from cli.chat.widgets.approval_args_row import ApprovalArgsRow
from cli.chat.widgets.approval_choice_pane import ApprovalChoicePane
from cli.chat.widgets.approval_pattern_edit import ApprovalPatternEdit
from cli.chat.widgets.approval_save_confirm import ApprovalSaveConfirm
from providers.authz import AuthzEffect, Rule

# Type alias for the return contract at ``src/cli/_modal.py:168``.
PromptCallbackReturn = AuthzEffect | tuple[AuthzEffect, Rule]


def response_key_from(result: PromptCallbackReturn) -> str:
    """Map an :data:`PromptCallbackReturn` back to a choice-pane key.

    bug-021 AC #8. The widget owns the vocabulary — this helper is
    the inverse of :func:`cli.chat.approval._translate_response`:

    - :attr:`AuthzEffect.ALLOW` → ``"y"``
    - :attr:`AuthzEffect.DENY` → ``"n"``
    - ``(AuthzEffect.ALLOW, Rule)`` → ``"a"``
    - ``(AuthzEffect.DENY, Rule)`` → ``"d"``

    bug-025 (closes bug-022): the tuple case now round-trips FULLY.
    The adapter bridge at
    :meth:`cli.chat.event_adapter.EventAdapter._bridge_widget_resolution`
    serialises the Rule dict alongside this key into the
    ``WIRE_PROMPT_ANSWERED`` payload, and
    :func:`cli.chat.approval._translate_response` rebuilds the
    ``(effect, Rule)`` tuple so
    :meth:`providers.authz.RuleBasedAuthorizationProvider._resolve_prompt`
    persists the rule to ``platform.yaml``.
    """
    if isinstance(result, tuple):
        effect, _rule = result
        return "a" if effect is AuthzEffect.ALLOW else "d"
    return "y" if result is AuthzEffect.ALLOW else "n"


class ApprovalBlock(Vertical):
    """Bordered three-step approval flow.

    Public API:

    - Constructor: ``ApprovalBlock(rule_id, action, target)``.
    - ``resolved()``: async, returns the ``PromptCallbackReturn`` tuple.
    """

    DEFAULT_CSS = """
    $hint: #6e7681;
    $error: #ff5555;
    $assistant-fg: #b0c0d5;
    $user-border: #2ea043;
    $warning: #ffb454;
    $tool-fg: #4dd0e1;

    ApprovalBlock {
        height: auto;
        width: 1fr;
        border: round $warning;
        padding: 0 1;
        margin: 0 0 1 0;
    }

    ApprovalBlock > .breadcrumb {
        color: $hint;
        height: 1;
    }
    ApprovalBlock > .breadcrumb .-done {
        color: $tool-fg;
        text-style: bold;
    }
    ApprovalBlock > .breadcrumb .-current {
        color: $warning;
        text-style: bold;
    }

    ApprovalBlock > .context {
        color: $assistant-fg;
        height: 1;
    }
    ApprovalBlock > .context-label {
        color: $hint;
    }

    ApprovalBlock > .spacer {
        height: 1;
    }

    ApprovalBlock > .footer-help {
        color: $hint;
        text-style: italic;
        height: 1;
        margin-top: 1;
    }
    ApprovalBlock > .resolved {
        height: 1;
        margin-top: 1;
        text-style: bold;
    }
    ApprovalBlock > .resolved.-allowed {
        color: $tool-fg;
    }
    ApprovalBlock > .resolved.-denied {
        color: $error;
    }
    ApprovalBlock > .parent-hint {
        color: $hint;
        padding: 0 0 0 1;
        height: 1;
    }
    """

    # qol-047: block-level Tab binding toggles the expandable Args row.
    # Fires from any focused descendant (choice/pattern/save panes) —
    # none of those panes bind Tab, and Input doesn't either, so this
    # binding is reached via Textual's bubble-up-to-ancestors mechanism
    # before Screen's default ``app.focus_next`` binding wins.
    BINDINGS = [
        Binding("tab", "toggle_args", "expand/collapse Args", show=False),
    ]

    # Step ids used in the breadcrumb builder.
    _STEP_CHOOSE = 0
    _STEP_PATTERN = 1
    _STEP_SAVE = 2

    def __init__(
        self,
        rule_id: str | None,
        action: str,
        target: str | None,
        *,
        parent_hint: str | None = None,
        terminal_state: tuple[bool | None, str] | None = None,
    ) -> None:
        super().__init__()
        # Preserve the exact strings the callback was fired with so the
        # header reflects reality even when rule_id or target are None
        # (both are optional per the ``PromptCallback`` contract at
        # ``src/cli/_modal.py:244``).
        self._rule_id = rule_id or "(no matching rule)"
        self._action = action
        self._target = target or ""
        # spec-014: when non-None, `compose` yields a leading
        # `Static(" ↳", classes="parent-hint")` BEFORE the border box
        # so the workflow-scoped approval visually connects back to
        # the source workflow card via the `↳` gutter connector.
        self._parent_hint = parent_hint
        # Current step index; drives the breadcrumb rendering.
        self._step: int = self._STEP_CHOOSE
        # The current pane child. Kept as an attribute so message
        # handlers can call ``.remove()`` on it directly rather than
        # querying (queries can race with mount-in-flight).
        self._current_pane: Widget | None = None
        # Set when the user picks ``a`` or ``d`` — the effect the
        # eventual rule will carry.
        self._pending_effect: AuthzEffect | None = None
        # Set by the pattern-edit step; consumed at save-confirm to
        # construct the final ``Rule``.
        self._pending_pattern: str | None = None
        # The resolution the ``resolved()`` waiter returns.
        self._result: PromptCallbackReturn | None = None
        # ``asyncio.Event`` the PromptCallback implementation awaits via
        # ``resolved()``. One-shot — set once, never cleared.
        self._resolved_event: asyncio.Event = asyncio.Event()
        # bug-056: when non-None, this block was constructed to render a
        # historical (resolved / interrupted) outcome on session resume.
        # `compose` skips the interactive choice pane; `on_mount` skips
        # the pane `.focus()`; `_resolved_event` is NEVER set so the
        # adapter's `_bridge_widget_resolution` (if it were ever spawned
        # for such a block, which it isn't) would park forever and not
        # append a duplicate `WIRE_PROMPT_ANSWERED`. The tuple is
        # ``(allowed, footer)`` — matching the interactive path's
        # `_resolve` footer/class choice.
        self._terminal_state: tuple[bool | None, str] | None = terminal_state

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        # spec-014: `parent_hint` renders as `↳` above the border box
        # so the workflow-scoped approval visually anchors to the
        # workflow card that spawned it. When `None` (the default —
        # standard tool-scoped approvals) no hint row is emitted.
        if self._parent_hint is not None:
            yield Static(" ↳", classes="parent-hint", markup=False)
        self.border_title = "⚠  Tool approval"
        yield Static(self._breadcrumb_text(), classes="breadcrumb", markup=True)
        yield Static(f"Tool  {self._action}", classes="context", markup=False)
        # qol-047: swap the previously-truncating Static for a small
        # custom widget that detects overflow and offers a chevron +
        # Tab-to-expand affordance. See ``approval_args_row.py``.
        yield ApprovalArgsRow(self._target)
        yield Static(f"Rule  {self._rule_id}", classes="context", markup=False)
        yield Static("", classes="spacer")
        # bug-056: a terminal-state block was constructed for a historical
        # prompt on session resume. Skip the interactive choice pane +
        # footer-help and yield the frozen resolved row directly, so the
        # widget never surfaces an active keyboard capture and never
        # sets `_resolved_event`.
        if self._terminal_state is not None:
            allowed, footer = self._terminal_state
            yield Static(footer, classes=self._resolved_classes(allowed), markup=False)
            return
        initial = ApprovalChoicePane()
        self._current_pane = initial
        yield initial
        yield Static(
            self._choice_footer_hint(),
            classes="footer-help",
            markup=False,
        )

    @staticmethod
    def _resolved_classes(allowed: bool | None) -> str:
        """Pick the CSS classes for a resolved-row Static.

        Matches the interactive path's `_resolve` mapping — ``True``
        renders as ``resolved -allowed`` (tool-cyan), ``False`` as
        ``resolved -denied`` (error-red). bug-056: ``None`` (dangling /
        interrupted) reuses the ``-denied`` styling so the row reads
        the same visual weight as a denied resolution — consistent with
        the widget's resolved-row styling (Coordinator note).
        """
        if allowed is True:
            return "resolved -allowed"
        return "resolved -denied"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolved(self) -> PromptCallbackReturn:
        """Await the user's resolution."""
        await self._resolved_event.wait()
        assert self._result is not None  # noqa: S101
        return self._result

    @property
    def is_resolved(self) -> bool:
        """True if this block is no longer awaiting user input.

        bug-056 composer-gate recompute: the interactive path marks
        resolution by setting `_resolved_event` (via `_resolve`); the
        replay-terminal path marks it by being constructed with a
        terminal state (`compose` yields the frozen row directly, no
        interactive pane is ever mounted). `ChatApp.recompute_awaiting_approval`
        walks mounted blocks and freezes the composer only when at least
        one is still awaiting input.
        """
        if self._terminal_state is not None:
            return True
        return self._resolved_event.is_set()

    def on_mount(self) -> None:
        """Move focus to the first pane so keybindings capture keys."""
        # bug-056: a terminal-state block has no interactive pane to
        # focus (compose skipped it) — skip the focus + footer refresh
        # entirely so the resumed transcript doesn't steal focus from
        # the composer (AC #9).
        if self._terminal_state is not None:
            return
        if self._current_pane is not None:
            self._current_pane.focus()
        # qol-047: the args row's overflow flag is only meaningful once
        # layout has assigned it a width. Schedule a footer refresh on
        # the next render frame so the initial "Tab → expand args"
        # suffix appears when the target is already too long to fit.
        self.call_after_refresh(self._refresh_footer_help)

    async def render_terminal(
        self,
        *,
        allowed: bool | None,
        footer: str,
    ) -> None:
        """Reconfigure this block as a frozen historical row.

        bug-056: called on session resume for prompts whose replayed
        slice contains a matching `.answered` / `.cancelled`, or that
        are dangling (no resolution in-slice). Removes the interactive
        choice pane + footer-help hint if they were mounted, and mounts
        the same frozen resolved-row Static the interactive path fires
        from `_resolve` — but **does not** set `_resolved_event`.
        Anything parked on `resolved()` (the adapter bridge) therefore
        never wakes, so no duplicate `WIRE_PROMPT_ANSWERED` can be
        appended to the durable log (AC #4).

        Primary construction path is via the `terminal_state=` kwarg on
        `__init__`; this instance method exists as a symmetric transform
        for callers that already hold a live block reference.
        """
        if self._current_pane is not None:
            with contextlib.suppress(Exception):
                await self._current_pane.remove()
            self._current_pane = None
        with contextlib.suppress(Exception):
            await self.query_one(".footer-help", Static).remove()
        with contextlib.suppress(Exception):
            await self.mount(
                Static(
                    footer,
                    classes=self._resolved_classes(allowed),
                    markup=False,
                ),
            )
        self._terminal_state = (allowed, footer)

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    async def on_approval_choice_pane_chosen(
        self,
        event: ApprovalChoicePane.Chosen,
    ) -> None:
        event.stop()
        key = event.key
        if key == "y":
            await self._resolve(
                AuthzEffect.ALLOW,
                footer="✓ Allowed once",
                allowed=True,
            )
        elif key in ("n", "esc"):
            await self._resolve(
                AuthzEffect.DENY,
                footer="✗ Denied once",
                allowed=False,
            )
        elif key in ("a", "d"):
            self._pending_effect = (
                AuthzEffect.ALLOW if key == "a" else AuthzEffect.DENY
            )
            self._step = self._STEP_PATTERN
            self._refresh_breadcrumb()
            await self._swap_pane(
                ApprovalPatternEdit(self._suggested_pattern()),
                footer_help="Enter → next  ·  Esc → back to choice",
            )

    async def on_approval_pattern_edit_confirmed(
        self,
        event: ApprovalPatternEdit.Confirmed,
    ) -> None:
        event.stop()
        self._pending_pattern = event.pattern
        self._step = self._STEP_SAVE
        self._refresh_breadcrumb()
        preview = self._preview_text()
        fallback = (
            "allow once"
            if self._pending_effect is AuthzEffect.ALLOW
            else "deny once"
        )
        await self._swap_pane(
            ApprovalSaveConfirm(
                preview,
                deny=self._pending_effect is AuthzEffect.DENY,
                fallback_label=fallback,
            ),
            footer_help="Enter → save  ·  Esc → cancel (fall back)",
        )

    async def on_approval_pattern_edit_cancelled(
        self,
        event: ApprovalPatternEdit.Cancelled,
    ) -> None:
        event.stop()
        self._pending_effect = None
        self._pending_pattern = None
        self._step = self._STEP_CHOOSE
        self._refresh_breadcrumb()
        await self._swap_pane(
            ApprovalChoicePane(),
            footer_help="Esc → deny once",
        )

    async def on_approval_save_confirm_chosen(
        self,
        event: ApprovalSaveConfirm.Chosen,
    ) -> None:
        event.stop()
        key = event.key
        if key == "y":
            # Build the Rule with the fixed empty ``from`` scope. Authz
            # treats an empty ``from`` as "any caller" — which is the
            # default users want (per the live-smoke feedback).
            if self._pending_effect is None or self._pending_pattern is None:
                return
            # bug-025 (closes bug-022): the Rule now round-trips
            # through the WIRE_PROMPT_ANSWERED payload — the adapter
            # bridge at `event_adapter.py::_bridge_widget_resolution`
            # serialises `rule.model_dump()` into the wire event's
            # payload when this branch fires, and
            # `InlineApprovalCallback._translate_response` rebuilds
            # the tuple so `src/providers/authz.py:1106` persists to
            # `platform.yaml`.
            rule = Rule(
                what=self._action,
                pattern=self._pending_pattern,
                effect=self._pending_effect.value,
            )
            if self._pending_effect is AuthzEffect.ALLOW:
                footer = "✓ Rule saved (allow)"
                allowed = True
            else:
                footer = "✗ Rule saved (deny)"
                allowed = False
            await self._resolve(
                (self._pending_effect, rule),
                footer=footer,
                allowed=allowed,
            )
        # `n` / `esc` — fall back to a one-shot effect matching the
        # rule the user was about to save.
        elif self._pending_effect is AuthzEffect.ALLOW:
            await self._resolve(
                AuthzEffect.ALLOW,
                footer="✓ Allowed once",
                allowed=True,
            )
        else:
            await self._resolve(
                AuthzEffect.DENY,
                footer="✗ Denied once",
                allowed=False,
            )

    # ------------------------------------------------------------------
    # qol-047 — Args-row expansion
    # ------------------------------------------------------------------

    def action_toggle_args(self) -> None:
        """Tab binding — forward to the args-row widget's own toggle.

        Wrapping the call this way keeps the pane-owned bindings
        (y/a/n/d/esc, Enter/Esc) untouched — Tab lives on the block,
        not on any pane.
        """
        try:
            args_row = self.query_one(ApprovalArgsRow)
        except Exception:  # noqa: BLE001 — during resolve the row may already be gone
            return
        args_row.action_toggle_expand()

    def on_approval_args_row_toggled(
        self,
        event: ApprovalArgsRow.Toggled,
    ) -> None:
        """Update the footer hint when the args-row expansion flips.

        Stops the message here — no ancestor cares about it.
        """
        event.stop()
        self._refresh_footer_help()

    def _refresh_footer_help(self) -> None:
        """Recompute + push the footer help line for the current pane.

        Uses the widget class of the current pane to pick the base
        wording, then appends the qol-047 args-row suffix when the row
        is overflowing.
        """
        try:
            footer = self.query_one(".footer-help", Static)
        except Exception:  # noqa: BLE001 — footer removed post-resolve
            return
        pane = self._current_pane
        if isinstance(pane, ApprovalPatternEdit):
            base = "Enter → next  ·  Esc → back to choice"
        elif isinstance(pane, ApprovalSaveConfirm):
            base = "Enter → save  ·  Esc → cancel (fall back)"
        else:
            base = "Esc → deny once"
        footer.update(base + self._args_footer_suffix())

    def _choice_footer_hint(self) -> str:
        """Initial footer hint for the choice pane, including args suffix.

        Called from ``compose`` before the args row has been laid out,
        so the suffix is always empty at this point — the real hint
        arrives via ``call_after_refresh(_refresh_footer_help)`` in
        ``on_mount``. Kept as a helper for symmetry with the other
        footer-string sites.
        """
        return "Esc → deny once" + self._args_footer_suffix()

    def _args_footer_suffix(self) -> str:
        """`` · Tab → expand args`` / ``· collapse args`` / empty.

        AC #5. Only appears when the args row is actually overflowing;
        wording follows current expansion state.
        """
        try:
            args_row = self.query_one(ApprovalArgsRow)
        except Exception:  # noqa: BLE001
            return ""
        if not args_row.overflowing:
            return ""
        if args_row.expanded:
            return "  ·  Tab → collapse args"
        return "  ·  Tab → expand args"

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _breadcrumb_text(self) -> str:
        """Render the top breadcrumb with `●`/`○` for done/pending.

        Uses Rich markup for per-step styling — the ``breadcrumb``
        class is set on the outer Static with ``markup=True``.
        """
        parts: list[str] = []
        labels = [(self._STEP_CHOOSE, "choose"), (self._STEP_PATTERN, "pattern"), (self._STEP_SAVE, "save")]
        for i, (step_id, label) in enumerate(labels):
            if step_id < self._step:
                dot = "[b bright_cyan]●[/]"
                text = f"[b bright_cyan]{label}[/]"
            elif step_id == self._step:
                dot = "[b yellow]○[/]"
                text = f"[b yellow]{label}[/]"
            else:
                dot = "[dim]○[/]"
                text = f"[dim]{label}[/]"
            parts.append(f"{dot} {text}")
            if i < len(labels) - 1:
                parts.append("[dim]─[/]")
        return " ".join(parts)

    def _refresh_breadcrumb(self) -> None:
        """Re-render the breadcrumb Static after a step transition."""
        try:
            crumb = self.query_one(".breadcrumb", Static)
        except Exception:  # noqa: BLE001 — during compose the widget may not exist yet
            return
        crumb.update(self._breadcrumb_text())

    async def _swap_pane(self, new_pane: Widget, *, footer_help: str) -> None:
        """Replace the current pane and update the footer hint."""
        if self._current_pane is not None:
            await self._current_pane.remove()
        self._current_pane = new_pane
        # Mount the new pane BEFORE the footer-help Static so the pane
        # stays sandwiched between context + footer. Textual mount()
        # accepts a `before=` selector for exactly this.
        try:
            footer = self.query_one(".footer-help", Static)
            await self.mount(new_pane, before=footer)
        except Exception:  # noqa: BLE001 — footer may not exist during resolve
            await self.mount(new_pane)
        try:
            footer = self.query_one(".footer-help", Static)
            # qol-047: append the args-expand hint suffix when the row
            # overflows. ``footer_help`` remains the source of truth for
            # the pane-specific wording; the suffix rides along.
            footer.update(footer_help + self._args_footer_suffix())
        except Exception:  # noqa: BLE001
            pass
        # Focus the new pane so its bindings + Input see keystrokes.
        new_pane.focus()

    async def _resolve(
        self,
        result: PromptCallbackReturn,
        *,
        footer: str,
        allowed: bool,
    ) -> None:
        """Record the resolution, mount the frozen footer, wake waiters."""
        if self._current_pane is not None:
            await self._current_pane.remove()
            self._current_pane = None
        # Remove the footer hint — it's replaced by the resolution row.
        try:
            await self.query_one(".footer-help", Static).remove()
        except Exception:  # noqa: BLE001
            pass
        classes = "resolved -allowed" if allowed else "resolved -denied"
        await self.mount(Static(footer, classes=classes, markup=False))
        self._result = result
        self._resolved_event.set()

    def _suggested_pattern(self) -> str:
        """Initial value for the pattern edit — the target verbatim."""
        return self._target or "*"

    def _preview_text(self) -> str:
        """Rule preview line: `authz.<effect>  <what>("<pattern>")`."""
        effect_word = (
            "allow" if self._pending_effect is AuthzEffect.ALLOW else "deny"
        )
        pattern = self._pending_pattern or ""
        return f'authz.{effect_word}  {self._action}("{pattern}")'
