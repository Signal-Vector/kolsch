"""CompactionBlock — Textual widget for context-compaction lifecycle events.

Subclasses Textual's `Collapsible` primitive. Renders as a compact
one-liner header + expandable body. The block's state can transition
from ``running`` (mounted at start of a compaction attempt) to one of
five terminal states (``success``, ``preview``, ``failure``,
``skipped``, ``revoked``) via ``transition_to_*`` methods.

The state is driven entirely by CSS classes — no conditional Python
in the render path. Silhouette mirrors ``ToolCallBlock``.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from textual.binding import Binding
from textual.widgets import Collapsible, Static

CompactionState = Literal[
    "running",
    "success",
    "preview",
    "failure",
    "skipped",
    "revoked",
]

_STATE_CLASSES: dict[CompactionState, str] = {
    "running": "-running",
    "success": "-success",
    "preview": "-preview",
    "failure": "-failed",
    "skipped": "-skipped",
    "revoked": "-revoked",
}


class CompactionBlock(Collapsible):
    """Context-compaction lifecycle block, collapsed by default.

    Header format: ``<glyph> <status>``. Body renders as a dim mono
    Static with the state-specific details.
    """

    ALLOW_SELECT: ClassVar[bool] = True

    # spec-078: CompactionBlock is a `Collapsible` subclass; same
    # rationale as commits 5/7 — preserve base class, deliver rail
    # via `border-left: outer $color` (`▌`, one glyph wider than the
    # primitive's `▎` target). Rail color = `$tool-fg` for the
    # success/preview/running family (compaction is a tool-family
    # operation), `$error` for failure, `$hint` for skipped/revoked.
    DEFAULT_CSS = """
    $tool-fg: #4dd0e1;
    $hint: #6e7681;
    $error: #ff5555;

    CompactionBlock {
        border: none;
        border-left: outer $tool-fg;
        background: transparent;
    }
    CompactionBlock.-failed  { border-left: outer $error; }
    CompactionBlock.-skipped { border-left: outer $hint; }
    CompactionBlock.-revoked { border-left: outer $hint; }
    CompactionBlock > CollapsibleTitle {
        color: $tool-fg;
    }
    CompactionBlock.-success > CollapsibleTitle {
        color: $tool-fg;
    }
    CompactionBlock.-failed > CollapsibleTitle {
        color: $error;
    }
    CompactionBlock.-skipped > CollapsibleTitle {
        color: $hint;
        text-style: italic;
    }
    CompactionBlock.-revoked > CollapsibleTitle {
        color: $hint;
    }
    CompactionBlock.-preview > CollapsibleTitle {
        color: $tool-fg;
        text-style: italic;
    }
    CompactionBlock .body {
        color: $hint;
        text-style: dim;
    }
    """

    BINDINGS = [
        Binding("c", "collapse", "Collapse compaction block", show=False),
    ]

    def __init__(
        self,
        *,
        state: CompactionState = "running",
        title_text: str | None = None,
        body_text: str = "",
        event_id: str | None = None,
    ) -> None:
        self._state: CompactionState = state
        self._event_id = event_id
        self._body_text = body_text
        title = title_text if title_text is not None else _default_title(state)
        self._body_static = Static(body_text, classes="body", markup=False)
        super().__init__(self._body_static, title=title, collapsed=True)
        self.add_class(_STATE_CLASSES[state])

    @property
    def state(self) -> CompactionState:
        return self._state

    @property
    def body_text(self) -> str:
        return self._body_text

    def _apply(self, state: CompactionState, title: str, body: str) -> None:
        for cls in _STATE_CLASSES.values():
            self.remove_class(cls)
        self.add_class(_STATE_CLASSES[state])
        self._state = state
        self.title = title
        self._body_text = body
        self._body_static.update(body)

    def transition_to_success(
        self,
        *,
        compacted_count: int,
        tokens_before: int,
        tokens_after: int,
        trigger: str = "auto",
        refs_count: int | None = None,
    ) -> None:
        saved = tokens_before - tokens_after
        pct = f"{(saved * 100 // tokens_before)}%" if tokens_before else "?%"
        title = (
            f"✓ Compacted {compacted_count} events "
            f"({tokens_before:,} → {tokens_after:,} tokens · saved {pct})"
        )
        lines = [
            f"trigger: {trigger}",
            f"events compacted: {compacted_count}",
            f"tokens: {tokens_before:,} → {tokens_after:,} (saved {saved:,} · {pct})",
        ]
        if refs_count is not None:
            lines.append(f"summary refs: {refs_count}")
        lines.append("/uncompact to restore · /why-compact for details")
        self._apply("success", title, "\n".join(lines))

    def transition_to_preview(
        self,
        *,
        compacted_count: int,
        tokens_before: int,
        tokens_after: int,
    ) -> None:
        saved = tokens_before - tokens_after
        pct = f"{(saved * 100 // tokens_before)}%" if tokens_before else "?%"
        title = (
            f"◎ Compaction preview — would compact {compacted_count} events "
            f"({tokens_before:,} → {tokens_after:,} tokens · saved {pct}, not applied)"
        )
        body = "\n".join(
            [
                f"would compact: {compacted_count} events",
                f"projected tokens: {tokens_before:,} → {tokens_after:,} "
                f"(saves {saved:,} · {pct})",
                "not applied to live context",
                "/why-compact for the summary output",
            ]
        )
        self._apply("preview", title, body)

    def transition_to_failure(
        self,
        *,
        message: str,
        fallback_tier: str | None = None,
    ) -> None:
        short = message.splitlines()[0] if message else "unknown"
        title = f"✗ Compaction failed: {short}"
        body_lines = [f"error: {message or 'unknown'}"]
        if fallback_tier:
            body_lines.append(f"fallback tier fired: {fallback_tier}")
        self._apply("failure", title, "\n".join(body_lines))

    def transition_to_skipped(self, *, reason: str) -> None:
        title = f"↷ Compaction skipped: {reason}"
        self._apply("skipped", title, f"reason: {reason}")

    def transition_to_revoked(self, *, target_short_id: str) -> None:
        title = f"↶ Compaction {target_short_id} restored"
        self._apply("revoked", title, f"restored compaction: {target_short_id}")

    def update_running_message(self, message: str) -> None:
        """Refresh the running-state title in place (e.g. `Compacting… (retry 1)`)."""
        if self._state != "running":
            return
        self.title = f"● {message}"
        self._body_static.update(message)

    def action_collapse(self) -> None:
        self.collapsed = True


def _default_title(state: CompactionState) -> str:
    return {
        "running": "● Compacting…",
        "success": "✓ Compacted",
        "preview": "◎ Compaction preview",
        "failure": "✗ Compaction failed",
        "skipped": "↷ Compaction skipped",
        "revoked": "↶ Compaction restored",
    }[state]


__all__ = ["CompactionBlock", "CompactionState"]
