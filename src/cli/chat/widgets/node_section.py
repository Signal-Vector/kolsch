"""`NodeSection` — a titled collapsible section inside a `WorkflowNodeRow`.

bug-037: wraps ONE content widget (UserChatbox / AssistantProse /
ToolCallBlock / Static) with a titled ``Collapsible`` frame so
the row body reads as a per-type index the user drills into.

State-aware title decoration (canonical vocabulary per qol-084):
- ``running`` → suffix ``…``
- ``failed`` → prefix ``✗ `` + ``-failed`` CSS class
- ``success`` (default) → plain title
"""

from __future__ import annotations

from typing import ClassVar

from textual.widgets import Collapsible

_RUNNING_SUFFIX = "…"
_FAILED_PREFIX = "✗ "


class NodeSection(Collapsible):
    """One titled collapsible slot inside a workflow-row body."""

    # bug-038: Collapsible defaults ALLOW_SELECT=False; override so
    # users can drag-select text inside the section's contents
    # (input / response / tool / output blocks). See
    # `Widget.allow_select` at site-packages/textual/widget.py:2786.
    ALLOW_SELECT: ClassVar[bool] = True

    DEFAULT_CSS = """
    $error: #ff5555;

    NodeSection {
        padding: 0;
        margin: 0;
        border-top: none;
    }
    NodeSection.-failed > CollapsibleTitle {
        color: $error;
    }
    """

    def __init__(
        self,
        title: str,
        *,
        state: str = "success",
        collapsed: bool = True,
        classes: str | None = None,
    ) -> None:
        self._base_title = title
        self._state = state
        decorated = _decorate_title(title, state)
        cls_final = _append_class(classes, state)
        super().__init__(
            title=decorated,
            collapsed=collapsed,
            classes=cls_final,
        )

    def set_state(self, state: str) -> None:
        """Update the section's state; re-decorate title + class."""
        if state == self._state:
            return
        prev = self._state
        self._state = state
        self.title = _decorate_title(self._base_title, state)
        if prev == "failed":
            self.remove_class("-failed")
        if state == "failed":
            self.add_class("-failed")


def _decorate_title(base: str, state: str) -> str:
    if state == "running":
        return f"{base}{_RUNNING_SUFFIX}"
    if state == "failed":
        return f"{_FAILED_PREFIX}{base}"
    return base


def _append_class(classes: str | None, state: str) -> str | None:
    base = "node-section"
    parts = [base]
    if classes:
        parts.append(classes)
    if state == "failed":
        parts.append("-failed")
    return " ".join(parts)


__all__ = ["NodeSection"]
