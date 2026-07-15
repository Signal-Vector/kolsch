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

import time
from typing import Any

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widgets import Collapsible, Static

from cli.chat.widgets.card_body import KIND_TO_RENDERER
from cli.chat.widgets.notice_slot import NoticeSlot
from tools.services.card_kinds import get_kind_spec


# feat-019: hardcoded threshold — the ``x`` force-kill binding only
# fires when a card has been in ``cancelling`` state for more than this
# many seconds. Coordinator Q3 lock: no env-var override in v1.
_FORCE_KILL_THRESHOLD_SECONDS = 5.0


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
        # bug-040: ``set_title`` receives executor-supplied
        # ``payload["title"]`` from ``kolsch.card.opened`` wire events,
        # and ``_rerender`` pipes it through ``self.update(...)``.
        # Disable Textual markup so bracket content in a card title
        # can't trip ``MarkupError``.
        super().__init__("", id=id, markup=False)
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
        # bug-040: ``CardWidget._apply_close_error`` writes
        # ``f"✗ error: {error_message}"`` here where ``error_message``
        # is executor-sourced. Disable Textual markup so bracket
        # content in the error message can't trip ``MarkupError``.
        super().__init__("", id=id, markup=False)


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
    $tool-fg: #4dd0e1;
    $warning: #ffb454;
    $error: #ff5555;
    $hint: #6e7681;

    CardWidget {
        border: round $tool-fg;
        padding: 0 1;
        margin: 0 0 1 0;
        height: auto;
        background: transparent;
    }
    CardWidget.-queued     { border: round $hint; }
    CardWidget.-paused     { border: round $warning; }
    CardWidget.-cancelling { border: round $tool-fg; opacity: 0.7; }
    CardWidget.-error      { border: round $error; }
    CardWidget.-cancelled  { border: round $hint; }

    CardWidget CardHeader {
        color: $tool-fg;
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
        ("enter", "expand_or_ack", "Expand or acknowledge"),
        ("c", "collapse", "Collapse"),
        # feat-019: force-kill escalation. Guarded inside
        # ``action_force_kill`` — only fires when the card is focused,
        # in ``cancelling`` state, AND has been there for more than
        # ``_FORCE_KILL_THRESHOLD_SECONDS``.
        ("x", "force_kill", "Force-kill (when cancelling >5s)"),
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
        # spec-016: optional callback fired when Enter is pressed on a
        # card in ``error`` state. Set by ``WorkflowsImpl`` at card-bind
        # time on workflow-kind cards; None for every other kind. The
        # callback receives no args — it looks up the run record via
        # its own closure and flips ``record.acked = True`` plus
        # triggers a layout reorder.
        self._on_error_ack: Any = None
        # feat-019: monotonic timestamp recording when the card most
        # recently entered its current ``state``. Used by
        # :meth:`_time_in_state` to gate the ``x`` force-kill binding
        # (>5s in ``cancelling``). Updated inside ``watch_state`` on
        # every transition. Seeded here to the mount time so the
        # initial ``initializing`` state has a well-defined entry
        # timestamp — safer than ``None`` for the arithmetic path.
        self._state_entered_at: float = time.monotonic()
        # feat-019: optional wiring for the ``x`` force-kill escalation.
        # ``_workflows_impl`` is the concrete WorkflowsImpl handle
        # (or a stub) that ``action_force_kill`` calls to compute the
        # cascade tree size and construct ``ForceKillConfirm``.
        # ``_run_id`` is the run this card is bound to. Both left
        # unset on non-workflow cards; ``action_force_kill`` no-ops
        # cleanly when they're absent.
        self._workflows_impl: Any = None
        self._run_id: Any = None
        # feat-019: session store handle passed through to
        # ``ForceKillConfirm`` so the 500ms fallback can synthesise
        # the ``workflow.run.aborted`` event. Kept as ``Any`` so the
        # widget stays decoupled from concrete store implementations.
        self._session_store: Any = None
        self._thread_id: Any = None
        self._session_id: Any = None
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
        self._body.apply_event(_MockEventData(
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
            # Match ToolCallBlock's collapse cadence for concise
            # kinds (text / progress / confirm). Guard with
            # `is_mounted` — headless tests may exercise `apply_close`
            # before mount (no timer scheduler yet).
            #
            # bug-035: workflow cards STAY expanded on success. The
            # user's mental model is "the workflow finished, show me
            # what it did" — auto-collapsing hides the whole node
            # timeline behind a title that says only "✓". For other
            # card kinds the timeline is trivial (one line of text /
            # a bar) and auto-collapse keeps the transcript tidy.
            if self.kind != "workflow":
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
        """Reactive: apply CSS class + header glyph on state change.

        feat-019: also records the monotonic entry timestamp for the
        new state so :meth:`_time_in_state` can gate the ``x``
        force-kill binding on ``cancelling`` residency.
        """
        _ = old
        # feat-019: reset the state-entry clock on every transition so
        # ``_time_in_state`` returns the residency in the *current*
        # state (not since mount).
        self._state_entered_at = time.monotonic()
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

    def action_expand_or_ack(self) -> None:
        """`Enter` binding — ack an error card, else expand.

        spec-016 AC #11: when the card is in ``error`` state (e.g. a
        failed workflow pinned at the boundary between terminal and
        in-progress groups), pressing ``Enter`` fires the
        ``_on_error_ack`` callback (set at card-bind time by
        ``WorkflowsImpl``). The callback flips
        ``WorkflowRunRecord.acked = True`` and schedules a reorder so
        the card moves into the terminal band.

        For every other state (or a card without an ack callback),
        the binding behaves like the legacy ``action_expand`` — expand
        a collapsed card.
        """
        if self.state == "error" and self._on_error_ack is not None:
            try:
                self._on_error_ack()
            except Exception:  # noqa: BLE001 — ack callback best-effort
                pass
            return
        if self.collapsed:
            self.collapsed = False

    # Retained as a compatibility alias — tests written against the
    # spec-007 binding name still call `action_expand`.
    def action_expand(self) -> None:
        """`Enter` binding — expand a collapsed card (compat alias)."""
        if self.collapsed:
            self.collapsed = False

    def action_collapse(self) -> None:
        """`c` binding — collapse an expanded card."""
        if not self.collapsed:
            self.collapsed = True

    def _collapse_after_success(self) -> None:
        """Callback for the 1s auto-collapse timer."""
        self.collapsed = True

    # ------------------------------------------------------------------
    # feat-019: force-kill escalation
    # ------------------------------------------------------------------

    def _time_in_state(self, target_state: str) -> float:
        """Return seconds since the card entered ``target_state``.

        When the card's current ``state`` doesn't match ``target_state``
        the result is ``0.0`` — the caller should read this as "not
        applicable" rather than "just entered". Uses ``time.monotonic``
        so wall-clock skew doesn't affect the threshold.

        feat-019 AC #8: consumed by :meth:`action_force_kill` to gate
        the ``x`` binding on ``cancelling`` residency.
        """
        if self.state != target_state:
            return 0.0
        return time.monotonic() - self._state_entered_at

    def bind_workflow(
        self,
        *,
        workflows_impl: Any,
        run_id: Any,
        session_store: Any = None,
        thread_id: Any = None,
        session_id: Any = None,
    ) -> None:
        """Wire the workflow context needed by the ``x`` force-kill.

        Called by ``ChatApp`` / ``EventAdapter`` after mounting a
        workflow-kind card so ``action_force_kill`` can compute the
        cascade tree size + construct :class:`ForceKillConfirm` with
        the right cancel target. Idempotent; a second call replaces
        the reference.
        """
        self._workflows_impl = workflows_impl
        self._run_id = run_id
        self._session_store = session_store
        self._thread_id = thread_id
        self._session_id = session_id

    def action_force_kill(self) -> None:
        """``x`` binding — mount :class:`ForceKillConfirm` when guarded.

        Guards (feat-019 AC #8):

        - Card must be in ``cancelling`` state.
        - Time-in-state must exceed ``_FORCE_KILL_THRESHOLD_SECONDS``
          (Coordinator Q3 lock: 5s hardcoded).
        - Widget must have a bound :class:`WorkflowsImpl` handle +
          run id (via :meth:`bind_workflow`).

        Any guard failure is a silent no-op — the binding stays
        registered globally but only fires when the escalation makes
        sense.
        """
        if self.state != "cancelling":
            return
        if self._time_in_state("cancelling") < _FORCE_KILL_THRESHOLD_SECONDS:
            return
        if self._workflows_impl is None or self._run_id is None:
            return

        # Compute the cascade tree size — total minus the target run
        # itself gives the "N related" count for the modal question
        # (feat-019 AC #9).
        collect = getattr(self._workflows_impl, "_collect_cancel_tree", None)
        if collect is not None:
            try:
                tree = collect(self._run_id)
                n_related = max(0, len(tree) - 1)
            except Exception:  # noqa: BLE001 — cascade tree best-effort
                n_related = 0
        else:
            n_related = 0

        # Lazy import — the widget module shouldn't import
        # ForceKillConfirm at module import time so tests that mount
        # CardWidget in isolation don't drag in the modal.
        from cli.chat.widgets.force_kill_confirm import (  # noqa: PLC0415
            ForceKillConfirm,
        )

        modal = ForceKillConfirm(
            run_id=self._run_id,
            n_related=n_related,
            workflows_impl=self._workflows_impl,
            session_store=self._session_store,
            thread_id=self._thread_id,
            session_id=self._session_id,
        )
        try:
            if self.app is not None:
                self.app.mount(modal)
        except Exception:  # noqa: BLE001 — mount is best-effort
            pass


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


class _MockEventData:
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
