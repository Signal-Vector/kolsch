"""Card body renderer dispatch — kind → renderer class.

spec-007 §Body renderers. Each entry in
[`KIND_TO_RENDERER`][cli.chat.widgets.card_body.KIND_TO_RENDERER]
maps a card kind string (from
[`CardKindLiteral`][tools.services.card_kinds.CardKindLiteral]) to a
renderer widget class conforming to
[`CardBodyRenderer`][cli.chat.widgets.card_body.CardBodyRenderer].

The map is populated once at import time; the renderer subpackages
are imported lazily inside a helper to avoid any circular imports
against the top-level `cli.chat.widgets` package.

The `CardBodyRenderer` Protocol is intentionally minimal — every
body renderer exposes two callables (`apply_event`, `apply_snapshot`)
that `CardWidget.apply_event` dispatches into. All body renderers
handle payload extraction themselves so `CardWidget` stays kind-
agnostic (rule 2 modularity, spec Design Principles).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CardBodyRenderer(Protocol):
    """Per-kind renderer contract.

    `apply_event` receives the incoming UI-event data (opaque; the
    renderer inspects `.kind` + `.payload` itself). `apply_snapshot`
    receives the replacement content string from a
    `kolsch.card.snapshot` event.

    Both methods must be idempotent-safe: the caller
    (`CardWidget.apply_event`) enforces LRU-based dedupe on
    `event_id` before invoking these; renderers are free to be
    stateful.
    """

    def apply_event(self, event_data: Any) -> None:
        """Handle a card content event (`appended` / `set` / `set_progress`)."""
        ...

    def apply_snapshot(self, content: str) -> None:
        """Replace the current body content with `content`."""
        ...


def _load_renderers() -> dict[str, type]:
    """Import + return the kind → renderer-class dispatch table.

    Lazy so a widget subpackage import can't cycle back through
    this module before the class is defined.
    """
    # Lazy imports (PLC0415) avoid a circular dep against the top-level
    # cli.chat.widgets package — see this function's docstring.
    from cli.chat.widgets.card_body.confirm import ConfirmRenderer  # noqa: PLC0415
    from cli.chat.widgets.card_body.markdown import MarkdownRenderer  # noqa: PLC0415
    from cli.chat.widgets.card_body.progress import ProgressRenderer  # noqa: PLC0415
    from cli.chat.widgets.card_body.workflow import WorkflowRenderer  # noqa: PLC0415

    return {
        "markdown": MarkdownRenderer,
        "progress": ProgressRenderer,
        "confirm": ConfirmRenderer,
        "workflow": WorkflowRenderer,
    }


KIND_TO_RENDERER: dict[str, type] = _load_renderers()
"""Closed-set dispatch. Keys mirror
[`CardKindLiteral`][tools.services.card_kinds.CardKindLiteral];
values are the widget classes mounted inside a `CardWidget`'s body
slot. Bundle-authored kinds are a v1.1 non-goal.
"""

__all__ = ["KIND_TO_RENDERER", "CardBodyRenderer"]
