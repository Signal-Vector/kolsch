"""Rich capsule primitives shared across chat surfaces.

Extracted from the (now-deleted) ``cli._renderer`` during the
Foundation phase of ``process/spec-textual-chat-ui.md`` so the small
set of primitives that other rendering surfaces
(``_workflow_capsules.py``, ``_modal.py``) depend on survives the
Textual chat rewrite. These primitives render a "capsule" — a chat
block laid out as a sidebar:

    │ tool · HH:MM:SS                                   [cyan3]
    │ bash(command='ls -la')
    │   running

Each line is prefixed with the bar character (`│` U+2502) in the
source's color. The header carries ``<source> · HH:MM:SS`` (source
label in source color; timestamp in grey50). No closing border;
blank line between capsules.

This module is the canonical home. The new chat surface
(``cli.chat``) uses Textual widgets and does NOT depend on these
primitives; only ``_workflow_capsules.py`` and ``_modal.py`` still
render via capsules today. A follow-up spec can migrate those
surfaces to Textual widgets if desired.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from rich.text import Text

if TYPE_CHECKING:
    from rich.console import Console

_BAR = "│"

_CAPSULE_PALETTE: dict[str, str] = {
    "user": "bright_blue",
    "agent": "grey70",
    "system": "grey42",
    "permission": "dark_orange3",
    "tool": "cyan3",
    "tool-dim": "cyan3 dim",
    "code": "cyan",
    # Workflow-event capsules. The renderer in
    # `cli._workflow_capsules` looks up these keys when emitting
    # capsules from a workflow's JSONL tail. `workflow_muted` is
    # used for branch-not-taken / pruned events; `workflow_paused`
    # for the inline `user_input` prompt; `workflow_failed` for
    # the `caller_message` failure surface.
    "workflow": "cyan",
    "workflow_muted": "bright_black",
    "workflow_paused": "yellow",
    "workflow_failed": "red",
}


def _now_hhmmss() -> str:
    return datetime.now().strftime("%H:%M:%S")  # noqa: DTZ005 — local time is right for UX


def _capsule(
    console: Console,
    *,
    source: str,
    header_extra: str = "",
    body_lines: list[str],
    timestamp: str | None = None,
    color: str | None = None,
) -> None:
    """Print one capsule.

    `source` is the label (`"tool"` / `"permission"` / etc.);
    `color` overrides the palette lookup when set (e.g. the dim
    failed-tool variant). `header_extra` lets callers append
    duration info (`→ HH:MM:SS (1.8s)`). `body_lines` are the
    content lines; each gets the bar prefix.
    """
    style = color if color is not None else _CAPSULE_PALETTE.get(source, "white")
    ts = timestamp if timestamp is not None else _now_hhmmss()

    # Header: bar + label + · + timestamp.
    header = Text()
    header.append(f"{_BAR} ", style=style)
    header.append(source, style=style)
    header.append(f" · {ts}", style="grey50")
    if header_extra:
        header.append(f" {header_extra}", style=style)
    console.print(header)

    # Body: each line gets the bar prefix.
    for line in body_lines:
        body = Text()
        body.append(f"{_BAR} ", style=style)
        body.append(line)
        console.print(body)

    # Blank line separator between capsules.
    console.print()
