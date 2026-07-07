"""ApprovalSaveConfirm — step 3 of the approval flow.

Renders a rule preview (`authz.allow  tool("pattern")`) and a
``y`` / ``n`` prompt line — no pattern-edit needed because the ``from``
scope isn't a user-editable field in this UI (that decision came out
of the live smoke test: users always want the default, so we just show
it in the preview).

Framework-aligned:

- ``Vertical`` container, ``width: 1fr``.
- ``Static`` preview + ``Static`` prompt row + bindings.
- ``BINDINGS`` fire ``action_choose(key)`` for ``y``/``n``/``escape``.
  Choice is signalled via a ``Chosen(key)`` message — ``y`` = save the
  rule, ``n`` = fall back to allow-once (or deny-once, depending on
  the effect stored on the parent block).

Escape is delivered as ``"esc"`` so the parent can map it to the
"fall back" path — same semantics as ``n``.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static


class ApprovalSaveConfirm(Vertical):
    """Rule preview + y/n prompt. Keypress-driven."""

    DEFAULT_CSS = """
    $assistant-fg: #b0c0d5;
    $tool-fg: #4dd0e1;
    $hint: #6e7681;
    $warning: #ffb454;
    $user-border: #2ea043;
    $error: #ff5555;

    ApprovalSaveConfirm {
        width: 1fr;
        height: auto;
    }
    ApprovalSaveConfirm > .preview-label {
        color: $hint;
        height: 1;
    }
    ApprovalSaveConfirm > .preview {
        color: $tool-fg;
        text-style: bold;
        height: 1;
        margin-left: 2;
    }
    ApprovalSaveConfirm > .preview-allow {
        color: $tool-fg;
    }
    ApprovalSaveConfirm > .preview-deny {
        color: $error;
    }
    ApprovalSaveConfirm > .choices {
        color: $assistant-fg;
        text-style: bold;
        height: 1;
        margin-top: 1;
    }
    ApprovalSaveConfirm > .prompt {
        color: $warning;
        text-style: bold;
        height: 1;
    }
    """

    BINDINGS = [
        Binding("y", "choose('y')", "save rule", show=False),
        Binding("n", "choose('n')", "cancel (fall back)", show=False),
        Binding("escape", "choose('esc')", "cancel (fall back)", show=False),
    ]

    can_focus = True

    class Chosen(Message):
        """Posted when the user picks ``y``/``n`` (or ``Esc``)."""

        def __init__(self, key: str) -> None:
            super().__init__()
            self.key = key

    def __init__(
        self,
        preview: str,
        *,
        deny: bool,
        fallback_label: str,
    ) -> None:
        """
        Args:
            preview: Rendered rule string, e.g. ``authz.allow  bash("ls")``.
            deny: True when the rule to be saved is a ``deny`` — colours
                the preview red instead of cyan. Purely visual.
            fallback_label: Wording for the ``n`` branch — e.g.
                ``allow once`` or ``deny once`` — inlined in the prompt
                so the user sees what pressing ``n`` will do.
        """
        super().__init__()
        self._preview = preview
        self._deny = deny
        self._fallback_label = fallback_label

    def compose(self) -> ComposeResult:
        yield Static("Rule preview:", classes="preview-label", markup=False)
        preview_classes = "preview " + ("preview-deny" if self._deny else "preview-allow")
        yield Static(self._preview, classes=preview_classes, markup=False)
        yield Static(
            f"Save this rule?  [y] save  ·  [n] cancel ({self._fallback_label})",
            classes="choices",
            markup=False,
        )
        yield Static("› ▊", classes="prompt", markup=False)

    def action_choose(self, key: str) -> None:
        """Bindings handler — post the choice to the parent."""
        self.post_message(self.Chosen(key))
