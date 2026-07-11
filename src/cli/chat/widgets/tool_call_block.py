"""ToolCallBlock — Textual widget for a single tool invocation.

Subclasses Textual's `Collapsible` primitive. Default state is
**collapsed** — the block renders as a compact one-liner
`▶ ● {tool_name}({args})`; press `Enter` on a focused block (or
click the title) to expand and see the body. This reverses the
spec's earlier "expanded by default" call after the first live
smoke test: expanded tool bodies dominate the transcript on
active turns and push assistant prose off-screen, especially in
inline mode. Collapsed-first + expand-on-demand keeps the eye on
the assistant response.

Colours and focus indicator are driven entirely by CSS — no
conditional Python in the render path. The `-failed` CSS class
controls the failure visual (silhouette preserved). A
`recovery_hint` string, if provided, is rendered as an inline
`↳ hint:` line inside the body (visible when expanded).

Spec: process/spec-textual-chat-ui.md → Technical Context →
"Tool block (built on `Collapsible`)".
"""

from __future__ import annotations

from typing import ClassVar

from textual.binding import Binding
from textual.widgets import Collapsible, Static


class ToolCallBlock(Collapsible):
    """One tool call, collapsed by default, expandable with `Enter`.

    Constructor mirrors the spec:

        ToolCallBlock(tool_name, args, body, failed=False,
                      recovery_hint=None)

    Title format: `● {tool_name}({args})`. Body renders as a dim
    mono `Static` (styled via CSS variables `$tool-fg` / `$error`
    / `$hint`). When `failed=True` the widget gains the `-failed`
    CSS class — no runtime colour logic. Collapsed state is
    Textual's default `Collapsible` behaviour; press `Enter` on
    the focused title (Textual's built-in binding) to expand.
    """

    # bug-038: Collapsible defaults ALLOW_SELECT=False; override so
    # users can drag-select tool args + body text to copy. See
    # `Widget.allow_select` at site-packages/textual/widget.py:2786.
    ALLOW_SELECT: ClassVar[bool] = True

    # Fallback variable defaults keep the widget self-sufficient
    # (Design Principles rule 2: modular — a widget must render
    # correctly without theme.tcss loaded). The app-level
    # theme.tcss overrides these variables at runtime. `$error`
    # already ships as a default Textual variable but is redeclared
    # here so the widget carries a coherent palette on its own.
    DEFAULT_CSS = """
    $tool-fg: #4dd0e1;
    $hint: #6e7681;
    ToolCallBlock {
        border: none;
    }
    ToolCallBlock > CollapsibleTitle {
        color: $tool-fg;
    }
    ToolCallBlock.-failed > CollapsibleTitle {
        color: $error;
    }
    ToolCallBlock:focus-within {
        border-left: thick $tool-fg;
    }
    ToolCallBlock.-failed:focus-within {
        border-left: thick $error;
    }
    ToolCallBlock .body {
        color: $hint;
        text-style: dim;
    }
    ToolCallBlock .hint {
        color: $hint;
        text-style: italic;
    }
    """

    BINDINGS = [
        Binding("c", "collapse", "Collapse tool block", show=False),
    ]

    def __init__(
        self,
        tool_name: str,
        args: str,
        body: str,
        failed: bool = False,
        recovery_hint: str | None = None,
    ) -> None:
        self._tool_name = tool_name
        self._args = args
        self._body_text = body
        self._recovery_hint = recovery_hint
        title = f"● {tool_name}({args})"
        # bug-042: stash body Static so post-hoc updates target the
        # right widget. `block.query(Static)` returns CollapsibleTitle
        # first (markup=True by Textual default), which crashes when
        # rendering tool output with `[...]` sequences.
        self._body_static = Static(body, classes="body", markup=False)
        children: list[Static] = [self._body_static]
        if recovery_hint is not None:
            children.append(
                Static(f"↳ hint: {recovery_hint}", classes="hint", markup=False),
            )
        super().__init__(*children, title=title, collapsed=True)
        if failed:
            self.add_class("-failed")

    def update_body(self, text: str) -> None:
        """bug-042: update the body Static directly (markup=False safe)."""
        self._body_static.update(text)

    def action_collapse(self) -> None:
        """Collapse this block. Bound to `c` (see `BINDINGS`)."""
        self.collapsed = True
