"""ChatServiceRegistry — the chat CLI's four service impls, wired at startup.

A frozen dataclass whose four fields carry the chat runtime's
concrete service impls (or `None` in headless mode / early in
startup). The tool dispatcher reads this sidecar and injects the
requested handles into a per-tool-call `ToolContext` via
`model_copy(update=...)`.

Fields are typed as `Any | None = None` in this spec because the
concrete impl classes (`CardsImpl`, `PromptsImpl`, `WorkflowsImpl`,
`SessionViewImpl`) don't exist yet — they arrive in spec-004
through spec-012. Once the impls land, the field annotations tighten
in place; no callers change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChatServiceRegistry:
    """The chat CLI's four tool-service impls, injected by the dispatcher.

    All four fields default to `None` so unit tests and headless
    callers can construct an empty registry. In headless mode
    (no `chat_services=` passed to `dispatch_tool`), every
    `ToolContext` service field stays `None` regardless of the
    tool's declared `requires` list.
    """

    cards: Any | None = None
    prompts: Any | None = None
    workflows: Any | None = None
    session: Any | None = None
