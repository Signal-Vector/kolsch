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

from cli.chat.state_tokens import resolve_state


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
    # spec-078: ToolCallBlock is a `Collapsible` subclass whose base
    # class owns expand/collapse, click handling, and the
    # `CollapsibleTitle` child. Wrapping in a GutterBlock container
    # (`Widget` composition) would require delegating every one of
    # Collapsible's methods + reactive attributes — a large rewrite
    # for a widget whose keybindings (Enter, c) and
    # `Collapsible.collapsed`/`.title` surface are load-bearing in
    # tests + downstream mounts. Rail is rendered via
    # `border-left: outer $color` instead. Textual's `outer` paints
    # `▌` on the left edge — one glyph wider than the spec's target
    # `▎` from the GutterBlock primitive, but visually reads the
    # same "colored left rail per state" the design review calls
    # for. The `● tool` marker in the title is replaced by the
    # canonical state glyph (`▸` running / `✗` failed) per spec-078.
    DEFAULT_CSS = """
    $tool-fg: #4dd0e1;
    $hint: #6e7681;
    ToolCallBlock {
        border: none;
        border-left: outer $tool-fg;
        background: transparent;
        /* spec-078: match the GutterBlock primitive's breather so
         * border-left widgets stack with the same vertical rhythm
         * as widgets that compose the primitive. Without this, tool-
         * call rows butted up against the next assistant response. */
        margin: 0 0 1 0;
    }
    ToolCallBlock.-failed {
        border-left: outer $error;
    }
    ToolCallBlock.-interrupted {
        border-left: outer $hint;
    }
    ToolCallBlock > CollapsibleTitle {
        color: $tool-fg;
    }
    ToolCallBlock.-failed > CollapsibleTitle {
        color: $error;
    }
    ToolCallBlock.-interrupted > CollapsibleTitle {
        color: $hint;
        text-style: dim;
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

    BINDINGS: ClassVar = [
        Binding("c", "collapse", "Collapse tool block", show=False),
    ]

    def __init__(
        self,
        tool_name: str,
        args: str,
        body: str,
        failed: bool = False,  # noqa: FBT001, FBT002 — positional per spec constructor + existing callers
        recovery_hint: str | None = None,
        matched_rule: dict[str, object] | None = None,
    ) -> None:
        self._tool_name = tool_name
        self._args = args
        self._body_text = body
        self._recovery_hint = recovery_hint
        self._matched_rule = matched_rule
        # spec-078: drop the pre-migration `● {tool_name}(...)` marker.
        # Collapsible ships with its own `▶` (collapsed) / `▼`
        # (expanded) chevron on the CollapsibleTitle; adding a running-
        # state `▸` glyph next to it doubled the chevron. State is now
        # signaled by the border-left rail color (`$tool-fg` default /
        # `$error` failed). For failed calls a leading `✗` disambiguates
        # from the collapsible chevron (different glyph family — cross,
        # not arrow — so no collision).
        title = f"✗ {tool_name}({args})" if failed else f"{tool_name}({args})"
        # feat-041 — when the failure was an authz deny (matched_rule
        # non-null), append the 2-line hint to the body Static (spec
        # Mockup 1) so the user sees `denied by rl_.../<file>:<line>`
        # without typing `/why`. Non-deny failures leave the body
        # untouched.
        body_text = body
        if failed and matched_rule:
            from cli.chat.services.authz_introspection import (  # noqa: PLC0415
                format_denial_hint,
            )

            hint = format_denial_hint(matched_rule)
            body_text = f"{body}\n{hint}" if body else hint
        # bug-042: stash body Static so post-hoc updates target the
        # right widget. `block.query(Static)` returns CollapsibleTitle
        # first (markup=True by Textual default), which crashes when
        # rendering tool output with `[...]` sequences.
        self._body_static = Static(body_text, classes="body", markup=False)
        # spec-130 Step 5 (fixup): the hint Static is now ALWAYS
        # created (previously only when `recovery_hint is not None`)
        # so `set_recovery_hint()` has a mounted child to reveal/set
        # later — see that method's docstring for why a post-hoc
        # `mount()` into an already-live `Collapsible` isn't the
        # chosen fix. Starts hidden (`display = False`) and empty when
        # no hint is supplied at construction time; `_apply_recovery_hint`
        # (shared by the constructor and `set_recovery_hint`) fills
        # and reveals it.
        self._hint_static = Static("", classes="hint", markup=False)
        super().__init__(
            self._body_static, self._hint_static, title=title, collapsed=True
        )
        self._apply_recovery_hint(recovery_hint)
        if failed:
            self.add_class("-failed")

    def update_body(self, text: str) -> None:
        """bug-042: update the body Static directly (markup=False safe)."""
        self._body_static.update(text)

    def _apply_recovery_hint(self, recovery_hint: str | None) -> None:
        """Set (or clear) the hint Static's text and visibility.

        Shared by ``__init__`` and ``set_recovery_hint`` so both paths
        render the identical `"↳ hint: {text}"` format and the
        identical show/hide rule (hidden when there's no hint).
        """
        if recovery_hint is None:
            self._hint_static.update("")
            self._hint_static.display = False
            return
        self._hint_static.update(f"↳ hint: {recovery_hint}")
        self._hint_static.display = True

    def set_recovery_hint(self, recovery_hint: str | None) -> None:
        """Reveal/update the hint line on an already-mounted block.

        spec-130 Step 5: the two-phase tool-call fill path
        (``ChatApp.mount_tool_call``) needs to surface a
        ``TOOL_FAILED`` result's ``recovery_hint`` on a block that was
        already mounted running/empty at ``TOOL_CALLED`` time —
        mounting a NEW child ``Static`` into an already-live
        ``Collapsible`` post-hoc is not safe (``Collapsible``'s
        ``Contents`` pane composes its children at mount time; there
        is no supported "add a child after the fact" API). Instead,
        the hint ``Static`` is always created in ``__init__``
        (hidden/empty by default — see ``_apply_recovery_hint``), and
        this method only toggles its text/visibility on that
        already-mounted child, which is safe post-mount. Passing
        ``None`` clears and re-hides it (symmetric with construction).
        """
        self._apply_recovery_hint(recovery_hint)

    def set_failed_state(self, tool_name: str, args: str) -> None:
        """Transition an already-mounted block to the failed visual state.

        spec-130 Step 5: companion to ``update_body``/``set_recovery_hint``
        for the tool-call two-phase fill path. A running block is
        always constructed with ``failed=False`` (its title has no
        ``✗`` prefix — see ``__init__``'s ``title = f"✗ ..." if failed
        else ...``); when the matching result turns out to be a
        ``TOOL_FAILED``, this rewrites the title to match what the
        constructor would have produced for ``failed=True`` — keeping
        title-glyph parity between the single-shot and fill paths —
        and adds the ``-failed`` CSS class (border-left rail + title
        color flip, per ``DEFAULT_CSS``). ``tool_name``/``args`` are
        passed explicitly (rather than read off ``self._tool_name``/
        ``self._args``) so the title always reflects the exact values
        the caller has on hand for this result, matching how the
        single-shot constructor path works.
        """
        self.title = f"✗ {tool_name}({args})"
        self.add_class("-failed")

    def render_interrupted(self) -> None:
        """Flip a still-running block to an interrupted terminal state.

        spec-130 Step 6: companion to ``set_failed_state`` for the
        cancel/turn-supersession sweep (``ChatApp._interrupt_live_registries``).
        A block reaching this method is, by construction, always a
        RUNNING one (mounted empty by ``mount_tool_call_running`` and
        never yet filled — see that method's two-phase docstring) — a
        block that already reached ``TOOL_RESULT``/``TOOL_FAILED`` has
        already been popped out of the registry that feeds this call,
        so this never overwrites real result content.

        Deliberately does NOT reuse ``set_failed_state``/``-failed``:
        an interrupted tool call is a **user cancellation**, not a
        **tool error** — conflating the two would make "the user hit
        Esc" look identical to "the tool actually failed" in the
        transcript, which is misleading after the fact. Instead this
        mirrors ``WorkingPlaceholder.render_terminal("interrupted")``'s
        own glyph/color choice: ``state_tokens.py``'s ``"cancelled"``
        token (``─``, ``$hint``, dim) via the new ``-interrupted`` CSS
        class, keeping the "user-cancelled" visual vocabulary
        consistent across both widget kinds spec-130 introduces.
        """
        glyph = resolve_state("cancelled").glyph
        self.title = f"{glyph} {self._tool_name}({self._args})"
        self.remove_class("-failed")
        self.add_class("-interrupted")
        self.update_body("Interrupted")

    def action_collapse(self) -> None:
        """Collapse this block. Bound to `c` (see `BINDINGS`)."""
        self.collapsed = True
