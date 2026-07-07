"""``NoticeSlot`` — in-card mount point for spec-008 notices.

Sits between
[`CardHeader`][cli.chat.widgets.card_widget.CardHeader] and the body
renderer inside a
[`CardWidget`][cli.chat.widgets.card_widget.CardWidget]. Empty by
default; grows one child per ``add_notice`` call, in mount order.

``last_notice()`` returns the most recently added
[`NoticeWidget`][cli.chat.widgets.notice.NoticeWidget] so
``EventAdapter._handle_card_notice`` can track it under its
``notice_id`` for the update-in-place path (drop-notice count
increment).
"""

from __future__ import annotations

from textual.containers import Vertical

from cli.chat.widgets.notice import NoticeWidget


class NoticeSlot(Vertical):
    """Vertical container holding in-card
    [`NoticeWidget`][cli.chat.widgets.notice.NoticeWidget] children.

    Empty container by default (``height: auto`` collapses to zero
    when no children); grows as ``add_notice`` mounts widgets.
    """

    DEFAULT_CSS = """
    NoticeSlot {
        height: auto;
        padding: 0;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._notices: list[NoticeWidget] = []

    def add_notice(self, text: str, kind: str) -> NoticeWidget:
        """Mount a fresh
        [`NoticeWidget`][cli.chat.widgets.notice.NoticeWidget]
        and return it.

        The widget is appended to the internal list (so
        ``last_notice`` sees it). The mount is fire-and-forget —
        Textual's ``mount`` returns an awaitable, but callers of
        ``add_notice`` are synchronous (the ``CardWidget.mount_notice``
        seam is sync; the widget's own compose life-cycle finalises
        the mount).
        """
        widget = NoticeWidget(text=text, kind=kind)
        self._notices.append(widget)
        # Textual's `mount` returns an `AwaitMount`; we detach — the
        # widget lands on the next tick. Consistent with how
        # ``add_class`` / ``update`` fire from sync widget methods
        # elsewhere in this codebase.
        self.mount(widget)
        return widget

    def last_notice(self) -> NoticeWidget | None:
        """Return the most recent notice widget, or ``None`` if empty."""
        return self._notices[-1] if self._notices else None


__all__ = ["NoticeSlot"]
