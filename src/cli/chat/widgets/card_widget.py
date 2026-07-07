"""`CardWidget` — Textual `Collapsible` subclass for a live card.

spec-007 §CardWidget. Mounted per `card_id` by
[`EventAdapter._handle_card_opened`][cli.chat.event_adapter.EventAdapter._handle_card_opened].
Implements the
[`CardMountState`][cli.chat.event_adapter.CardMountState] Protocol
from spec-006 so the adapter's card handlers dispatch through the
Protocol seam — no `isinstance` on the concrete widget, no rewrite
of handler code.

Composition (inside `Collapsible.Contents`):

- `CardHeader` — glyph + title, top row.
- `NoticeSlot` — in-card notice mount point (spec-008).
- Body renderer (one of the four kinds from
  [`KIND_TO_RENDERER`][cli.chat.widgets.card_body.KIND_TO_RENDERER]).
- `CardFooter` — empty by default; renders `✗ error: <msg>` on
  terminal-error close.

State machine drives CSS class + header glyph via a
`reactive("initializing")`. On `success` close the widget auto-
collapses after 1s (matches the `ToolCallBlock` UX). On `error`
close the widget **stays expanded** so the failure line is
readable.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widgets import Collapsible, Static

from cli.chat.widgets.card_body import KIND_TO_RENDERER
from cli.chat.widgets.notice_slot import NoticeSlot
from tools.services.card_kinds import get_kind_spec


# ---------------------------------------------------------------------------
# LRU set — mirrors the `_BoundedLRUSet` from `event_adapter.py`.
# ---------------------------------------------------------------------------
# The widget carries its own applied-`event_id` LRUs so the
# `CardMountState` Protocol methods are self-contained: the adapter
# hands the widget an event_id + payload and the widget dedupes. This
# matches the `_CardMountStub` implementation shape from spec-006.
_CARD_APPEND_LRU_MAXSIZE = 1000
_CARD_SNAPSHOT_LRU_MAXSIZE = 100


class CardHeader(Static):
    """Glyph + title row, top of the card.

    Renders as ``{glyph} {title}`` — set via `set_glyph` /
    `set_title`. The state machine on `CardWidget` calls `set_glyph`
    on every state transition; `set_title` is stable for the card's
    lifetime (`title` is fixed at open).
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("", id=id)
        self._glyph = "⠋"
        self._title = ""
        self._warning_indicator = False

    def set_glyph(self, glyph: str) -> None:
        """Replace the header glyph; re-render."""
        self._glyph = glyph
        self._rerender()

    def set_title(self, title: str) -> None:
        """Replace the header title; re-render."""
        self._title = title
        self._rerender()

    def add_warning_indicator(self) -> None:
        """Turn on the ``⚠`` indicator next to the title.

        Called by
        [`CardWidget.mount_notice`][cli.chat.widgets.card_widget.CardWidget.mount_notice]
        when a ``warning``-kind notice lands in the
        [`NoticeSlot`][cli.chat.widgets.notice_slot.NoticeSlot]. Idempotent
        — a second call keeps the single ``⚠`` glyph.
        """
        self._warning_indicator = True
        self._rerender()

    def _rerender(self) -> None:
        """Redraw the header text with the current glyph + title + warning."""
        suffix = " ⚠" if self._warning_indicator else ""
        self.update(f"{self._glyph} {self._title}{suffix}")


class CardFooter(Static):
    """Empty by default; renders `✗ error: <msg>` on error close."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("", id=id)


class _BoundedLRUSet:
    """Insertion-ordered set with an upper size bound.

    Same shape as `event_adapter._BoundedLRUSet`; duplicated here so
    the widget module has no import-time dependency on the adapter
    module (they're peers in the DAG, not stacked).
    """

    __slots__ = ("_maxsize", "_seen")

    def __init__(self, *, maxsize: int) -> None:
        from collections import OrderedDict

        self._maxsize = maxsize
        self._seen: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, key: str) -> bool:
        return key in self._seen

    def __len__(self) -> int:
        return len(self._seen)

    def add(self, key: str) -> None:
        if key in self._seen:
            self._seen.move_to_end(key)
            return
        self._seen[key] = None
        while len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)


# States driven by `card.status` + `card.closed`. `initializing` is
# the mounted default; `running` fires on first content event.
_TERMINAL_STATES = frozenset({"success", "error", "cancelled"})
_CSS_CLASS_STATES = frozenset(
    {"queued", "paused", "cancelling", "error", "cancelled"},
)


class CardWidget(Collapsible):
    """Per-card widget; implements `CardMountState` Protocol.

    The `apply_append` / `apply_snapshot` / `apply_status` /
    `apply_close` methods are the seam the spec-006 adapter
    handlers dispatch through. Widget internals (header glyph,
    footer text, CSS class, auto-collapse timer) are updated
    inside those methods; the adapter has no direct handle on the
    reactive `state`, only through `apply_status` /
    `apply_close`.

    Auto-collapse on `success` fires 1s after `apply_close` — the
    delay lets the user see the terminal ✓ before the widget folds
    away. `error` close keeps the widget expanded so the error line
    is readable.
    """

    DEFAULT_CSS = """
    $card-fg: #4dd0e1;
    $warning: #ffb454;
    $error: #ff5555;
    $hint: #6e7681;

    CardWidget {
        border: round $card-fg;
        padding: 0 1;
        margin: 0 0 1 0;
        height: auto;
        background: transparent;
    }
    CardWidget.-queued     { border: round $hint; }
    CardWidget.-paused     { border: round $warning; }
    CardWidget.-cancelling { border: round $card-fg; opacity: 0.7; }
    CardWidget.-error      { border: round $error; }
    CardWidget.-cancelled  { border: round $hint; }

    CardWidget CardHeader {
        color: $card-fg;
        text-style: bold;
        height: 1;
    }
    CardWidget.-error     CardHeader { color: $error; }
    CardWidget.-cancelled CardHeader { color: $hint; }
    CardWidget CardFooter {
        color: $hint;
        text-style: italic;
        height: auto;
    }
    """

    BINDINGS = [
        ("enter", "expand", "Expand"),
        ("c", "collapse", "Collapse"),
    ]

    state = reactive("initializing", init=False)
    """Reactive card state; drives CSS class + header glyph via
    `watch_state`. See spec-007 §CardWidget state machine table for
    the eight-state matrix.
    """

    # ------------------------------------------------------------------
    # CardMountState Protocol — public attribute surface
    # ------------------------------------------------------------------
    # These attributes are declared here (rather than only in
    # `__init__`) so mypy / structural-Protocol checks pass without
    # instantiation.

    def __init__(
        self,
        *,
        card_id: Any,
        kind: str,
        title: str = "",
        id: str | None = None,
    ) -> None:
        # Cast for headless tests that pass raw strings.
        self.card_id = card_id
        self.kind = kind
        self._kind_spec = get_kind_spec(kind)
        self.content: str = ""
        self.error_message: str | None = None
        self.applied_appends: _BoundedLRUSet = _BoundedLRUSet(
            maxsize=_CARD_APPEND_LRU_MAXSIZE,
        )
        self.applied_snapshots: _BoundedLRUSet = _BoundedLRUSet(
            maxsize=_CARD_SNAPSHOT_LRU_MAXSIZE,
        )
        # Build the child widgets before super().__init__ so
        # `Collapsible.__init__` can collect them into `_contents_list`.
        card_id_str = str(card_id) if card_id is not None else "unknown"
        self._header = CardHeader(id=f"card-{card_id_str}-header")
        self._notice_slot = NoticeSlot(id=f"card-{card_id_str}-notices")
        renderer_cls = KIND_TO_RENDERER[kind]
        self._body = renderer_cls(id=f"card-{card_id_str}-body")
        self._footer = CardFooter(id=f"card-{card_id_str}-footer")
        # `collapsed=False` — cards open expanded so the user sees
        # streaming content immediately; auto-collapse on success.
        super().__init__(
            title=title,
            collapsed=False,
            id=id,
        )
        # Seed the header with the kind's initial glyph so the
        # widget renders correctly on mount without waiting for a
        # state transition.
        initial_glyph = self._kind_spec.header_glyph_by_status.get(
            "running", "?",
        )
        self._header.set_glyph(initial_glyph)
        self._header.set_title(title)

    def compose(self) -> ComposeResult:
        """Yield the header, notice slot, body renderer, and footer.

        `Collapsible.compose_add_child` collects yielded widgets
        into the parent's `_contents_list` (see
        `.venv/lib/python3.13/site-packages/textual/widgets/_collapsible.py:237`)
        so they end up inside the collapsible's `Contents` container.
        """
        yield self._header
        yield self._notice_slot
        yield self._body
        yield self._footer

    # ------------------------------------------------------------------
    # CardMountState Protocol methods
    # ------------------------------------------------------------------

    def apply_append(self, event_id: str, chunk: str) -> None:
        """Apply an appended chunk; idempotent by `event_id`.

        Buffers into the body renderer via `apply_event` (the renderer
        owns the flush cadence). Also updates `.content` for
        Protocol conformance with the spec-006 stub.
        """
        if event_id in self.applied_appends:
            return
        self.applied_appends.add(event_id)
        self.content += chunk
        self._body.apply_event(_FakeEventData(
            kind="kolsch.card.appended",
            payload={"chunk": chunk},
        ))
        if self.state == "initializing":
            self.state = "running"

    def apply_snapshot(self, event_id: str, content: str) -> None:
        """Apply a snapshot — replace content, clear applied_appends.

        Snapshot supersedes prior appends; the LRU is cleared so a
        post-snapshot non-superseded append still applies cleanly.
        """
        if event_id in self.applied_snapshots:
            return
        self.applied_snapshots.add(event_id)
        self.content = content
        self.applied_appends = _BoundedLRUSet(
            maxsize=_CARD_APPEND_LRU_MAXSIZE,
        )
        self._body.apply_snapshot(content)
        if self.state == "initializing":
            self.state = "running"

    def apply_status(self, status: str) -> None:
        """Last-writer-wins on `state`. Never crosses into terminal."""
        if status in _TERMINAL_STATES:
            # `status` events are informational; terminal transitions
            # only fire through `apply_close`. Ignore accidental
            # terminal writes here.
            return
        self.state = status

    def apply_close(
        self,
        final_status: str,
        error_message: str | None,
    ) -> None:
        """Terminal transition. Idempotent — a terminal card stays put.

        - `success` → schedule auto-collapse after 1s.
        - `error` → footer renders `✗ error: <msg>`; stays expanded.
        - `cancelled` → CSS class + glyph; no footer / no collapse.
        """
        if self.state in _TERMINAL_STATES:
            return
        self.state = final_status
        self.error_message = error_message
        if final_status == "error" and error_message:
            self._footer.update(f"✗ error: {error_message}")
        if final_status == "success":
            # Match ToolCallBlock's collapse cadence. Guard with
            # `is_mounted` — headless tests may exercise `apply_close`
            # before mount (no timer scheduler yet).
            try:
                if self.is_mounted:
                    self.set_timer(1.0, self._collapse_after_success)
            except Exception:  # noqa: BLE001 — timer scheduling is best-effort
                pass

    # ------------------------------------------------------------------
    # Additional `CardWidget`-only surface
    # ------------------------------------------------------------------

    def apply_event(self, data: Any) -> None:
        """Delegate a UI-event data payload to the right Protocol method.

        Kept as a convenience for callers holding raw UIEventData —
        the adapter dispatches through the Protocol methods directly.
        """
        kind = getattr(data, "kind", None) or data.get("kind")
        payload = getattr(data, "payload", None)
        if payload is None:
            payload = data.get("payload") or {}
        event_id = getattr(data, "event_id", None) or payload.get(
            "event_id", "",
        )
        if kind == "kolsch.card.appended":
            chunk = payload.get("chunk", "")
            if isinstance(chunk, str):
                self.apply_append(str(event_id), chunk)
        elif kind == "kolsch.card.snapshot":
            content = payload.get("content", "")
            if isinstance(content, str):
                self.apply_snapshot(str(event_id), content)
        elif kind == "kolsch.card.status":
            status = payload.get("status", "")
            if isinstance(status, str):
                self.apply_status(status)
        elif kind == "kolsch.card.closed":
            raw_final = payload.get("final_status")
            final_status = (
                raw_final if isinstance(raw_final, str) else "success"
            )
            raw_msg = payload.get("error_message")
            error_message = raw_msg if isinstance(raw_msg, str) else None
            self.apply_close(final_status, error_message)
        elif kind in (
            "kolsch.card.set",
            "kolsch.card.set_progress",
        ):
            # Body renderer handles these directly.
            self._body.apply_event(data)
            if self.state == "initializing":
                self.state = "running"
        elif kind == "kolsch.card.notice":
            # In-card notice routing lives on ``mount_notice`` — the
            # adapter's ``_handle_card_notice`` calls it directly with
            # the parsed text + kind. This branch stays a no-op for
            # any raw event a caller happens to pipe through
            # ``apply_event``.
            return

    def mount_notice(self, text: str, kind: str) -> Any:
        """Mount a notice inside the card's
        [`NoticeSlot`][cli.chat.widgets.notice_slot.NoticeSlot].

        Delegates to
        [`NoticeSlot.add_notice`][cli.chat.widgets.notice_slot.NoticeSlot.add_notice].
        Warning-kind notices additionally trip the header's ``⚠``
        indicator via
        [`CardHeader.add_warning_indicator`][cli.chat.widgets.card_widget.CardHeader.add_warning_indicator].
        Returns the mounted widget so callers can hold a handle for
        the update-in-place path (drop-notice count increment).
        """
        widget = self._notice_slot.add_notice(text, kind)
        if kind == "warning":
            self._header.add_warning_indicator()
        return widget

    def watch_state(self, old: str, new: str) -> None:
        """Reactive: apply CSS class + header glyph on state change."""
        _ = old
        for cls in ("-queued", "-paused", "-cancelling", "-error", "-cancelled"):
            self.remove_class(cls)
        if new in _CSS_CLASS_STATES:
            self.add_class(f"-{new}")
        # Update glyph. `running` uses the kind default when the
        # kind-spec map has no explicit entry.
        glyph = self._kind_spec.header_glyph_by_status.get(new)
        if glyph is None:
            # Fallbacks for states outside the kind's own map
            # (e.g. `initializing` / `paused` on a markdown card).
            glyph = _GENERIC_GLYPH_BY_STATE.get(new, "?")
        self._header.set_glyph(glyph)

    def action_expand(self) -> None:
        """`Enter` binding — expand a collapsed card."""
        if self.collapsed:
            self.collapsed = False

    def action_collapse(self) -> None:
        """`c` binding — collapse an expanded card."""
        if not self.collapsed:
            self.collapsed = True

    def _collapse_after_success(self) -> None:
        """Callback for the 1s auto-collapse timer."""
        self.collapsed = True


# Generic fallbacks for state → glyph when the card's kind-spec map
# has no entry (e.g. `paused` on a markdown card).
_GENERIC_GLYPH_BY_STATE: dict[str, str] = {
    "initializing": "⠋",
    "queued": "⋯",
    "paused": "⚠",
    "cancelling": "⧗",
    "success": "✓",
    "error": "✗",
    "cancelled": "─",
}


class _FakeEventData:
    """Lightweight duck-typed payload wrapper for renderer dispatch.

    The body renderers accept anything with `.kind` + `.payload`;
    the widget synthesises this internally so `apply_append` can
    call `body.apply_event` without materialising a full
    `UIEventData` schema instance.
    """

    __slots__ = ("kind", "payload")

    def __init__(self, *, kind: str, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.payload = payload


__all__ = ["CardFooter", "CardHeader", "CardWidget"]
