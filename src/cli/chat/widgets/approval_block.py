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

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from cli.chat.widgets.approval_choice_pane import ApprovalChoicePane
from cli.chat.widgets.approval_pattern_edit import ApprovalPatternEdit
from cli.chat.widgets.approval_save_confirm import ApprovalSaveConfirm
from providers.authz import AuthzEffect, Rule

# Type alias for the return contract at ``src/cli/_modal.py:168``.
PromptCallbackReturn = AuthzEffect | tuple[AuthzEffect, Rule]


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
    """

    # Step ids used in the breadcrumb builder.
    _STEP_CHOOSE = 0
    _STEP_PATTERN = 1
    _STEP_SAVE = 2

    def __init__(
        self,
        rule_id: str | None,
        action: str,
        target: str | None,
    ) -> None:
        super().__init__()
        # Preserve the exact strings the callback was fired with so the
        # header reflects reality even when rule_id or target are None
        # (both are optional per the ``PromptCallback`` contract at
        # ``src/cli/_modal.py:244``).
        self._rule_id = rule_id or "(no matching rule)"
        self._action = action
        self._target = target or ""
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

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        self.border_title = "⚠  Tool approval"
        yield Static(self._breadcrumb_text(), classes="breadcrumb", markup=True)
        yield Static(f"Tool  {self._action}", classes="context", markup=False)
        yield Static(f"Args  {self._target}", classes="context", markup=False)
        yield Static(f"Rule  {self._rule_id}", classes="context", markup=False)
        yield Static("", classes="spacer")
        initial = ApprovalChoicePane()
        self._current_pane = initial
        yield initial
        yield Static(
            "Esc → deny once",
            classes="footer-help",
            markup=False,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolved(self) -> PromptCallbackReturn:
        """Await the user's resolution."""
        await self._resolved_event.wait()
        assert self._result is not None  # noqa: S101
        return self._result

    def on_mount(self) -> None:
        """Move focus to the first pane so keybindings capture keys."""
        if self._current_pane is not None:
            self._current_pane.focus()

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
            rule = Rule(
                what=self._action,
                pattern=self._pending_pattern,
                effect=self._pending_effect.value,  # type: ignore[arg-type]
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
        else:
            # `n` / `esc` — fall back to a one-shot effect matching the
            # rule the user was about to save.
            if self._pending_effect is AuthzEffect.ALLOW:
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
            footer.update(footer_help)
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
