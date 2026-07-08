"""``WorkflowLayoutManager`` — batched reorderer for workflow cards (spec-016).

Multi-workflow layout policy: two visual groups separated by a
``──── in-progress ────`` divider.

- Terminal group at top, sorted by ``terminal_at`` ASC (oldest completion
  first). Successes and cancelled runs live here; failed runs pinned at
  the boundary until user acknowledges via ``Enter`` (sets
  ``WorkflowRunRecord.acked = True``).
- In-progress group below, sorted by ``started_at`` ASC (trigger order).

Reorders are batched on a 250ms tick. ``WorkflowsImpl`` calls
:meth:`WorkflowLayoutManager.notify_card_state_changed` on every phase
transition; the manager schedules a single ``_flush_reorder`` per tick
regardless of how many notifications land in the window.

Compact-mode policy (spec-016 AC #7-9):

- When ``len(in_progress) > 2``, all in-progress cards flip to compact
  mode (single-line summary; hide node list + footer). The one focused
  card stays full-render.
- Terminal-group cards are ALWAYS compact.
- Any Widget internal-attribute name collisions are avoided — the
  manager does not reuse ``_nodes`` / ``_render`` which are Textual
  Widget internals.

Q1 (from spec ledger): ``VerticalScroll.move_child`` perf accepted for
v1 — repeated calls refresh layout each time; batch via
``transcript.suspend()`` only if perf reports emerge.

Q4 (Coordinator lock): divider mount happens AFTER card reorders — the
divider's DOM index shifts as cards move, so mounting last is the only
stable order.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Callable

from textual.widgets import Static

if TYPE_CHECKING:
    from textual.containers import VerticalScroll

    from cli.chat.services.workflows_impl import WorkflowRunRecord
    from cli.chat.widgets.status_bar import StatusBar


__all__ = [
    "DIVIDER_ID",
    "DIVIDER_TEXT",
    "WorkflowLayoutManager",
    "format_live_workflow_strip",
    "sort_workflow_cards",
]


_LOGGER = logging.getLogger(__name__)


DIVIDER_TEXT = "──── in-progress ────"
"""Divider label mounted between the two groups when both are non-empty."""


DIVIDER_ID = "workflow-divider"
"""DOM id assigned to the divider ``Static`` so tests can query it."""


_REORDER_TICK_SECONDS = 0.250
"""Delay between a ``notify_card_state_changed`` and the flush.

Multiple notifications within one window collapse to a single reorder
pass (spec-016 AC #3).
"""


_TERMINAL_SUCCESS_PHASES = frozenset({"success", "cancelled"})
_IN_PROGRESS_PHASES = frozenset({"queued", "running", "cancelling"})


# ---------------------------------------------------------------------------
# feat-019: status line 2 live-workflow strip.
# ---------------------------------------------------------------------------


_LIVE_PHASES = frozenset({"running", "cancelling"})
"""Phases that count as "live" for the status-line-2 strip.

``queued`` is separately surfaced via the ``⋯ N queued`` suffix; only
records with ``running`` or ``cancelling`` populate the leading
``▸ <ref> N/M`` slot (feat-019 §Scope, "1 workflow" case).
"""


def _has_pending_approval(record: "WorkflowRunRecord") -> bool:
    """Return True when a running record has a pending approval.

    feat-019 uses this to bump the strip's ``⚠ N paused`` counter.
    Reads the optional ``awaiting_approval`` bool the record grows in
    feat-019 (default False for backward compatibility with fixtures
    that build records via positional / minimal kwargs).
    """
    return bool(getattr(record, "awaiting_approval", False))


def format_live_workflow_strip(
    records: list["WorkflowRunRecord"],
) -> str | None:
    """Return the status-line-2 strip string, or ``None`` if idle.

    feat-019 AC #2 — six shapes:

    - Empty / no live records → ``None`` (StatusBar clears the strip).
    - Single ``running`` / ``cancelling`` → ``"▸ <ref> N/M · Ns"``
      (with ``N`` from ``node_counts.completed`` and ``M`` from
      ``node_counts.total``; the ``Ns`` suffix is wall-time since
      ``started_at``, ``created_at`` fallback for still-queued cases
      that leak into the live set).
    - Two or more live → single-workflow head + ``⋯ N queued`` suffix
      (queued count is the ``phase == "queued"`` bucket).
    - Any live + at least one awaiting-approval → append
      ``· ⚠ N paused``.

    Best-effort 78-char target (Coordinator Q2 lock) — no elision
    applied here; Textual's CSS collapse handles narrow terminals.

    Args:
        records: The full :class:`WorkflowRunRecord` list from
            ``WorkflowsImpl._workflow_registry``. Filtering into live /
            queued / paused buckets happens inside this helper so the
            caller passes the raw registry.

    Returns:
        The strip string, or ``None`` when no live workflows exist.
    """
    live = [r for r in records if r.phase in _LIVE_PHASES]
    queued = [r for r in records if r.phase == "queued"]
    paused = [
        r for r in records
        if r.phase in _LIVE_PHASES and _has_pending_approval(r)
    ]
    if not live:
        return None

    head = live[0]
    counts = head.node_counts
    completed = counts.completed
    total = counts.total
    anchor = head.started_at or head.created_at
    try:
        elapsed = int((datetime.now(tz=UTC) - anchor).total_seconds())
    except (TypeError, ValueError):
        elapsed = 0
    elapsed = max(0, elapsed)

    parts = [f"▸ {head.ref} {completed}/{total}"]

    # Compact suffixes — queued count present iff any queued records
    # exist (independent of live cardinality); paused present iff any
    # live records are awaiting approval; elapsed always tacked on
    # after the discrete counts.
    if queued:
        parts.append(f"⋯ {len(queued)} queued")
    parts.append(f"{elapsed}s")
    if paused:
        parts.append(f"⚠ {len(paused)} paused")
    return " · ".join(parts)


def sort_workflow_cards(
    records: list["WorkflowRunRecord"],
) -> list["WorkflowRunRecord"]:
    """Canonical sort per spec-016 AC #2.

    Groups records into three bands and sorts each independently:

    1. Terminal successes + cancelled (and acked failures) — sorted ASC
       by ``terminal_at`` (falling back to ``created_at`` when unset).
    2. Failed pinned (``phase == "error" and not acked``) — sorted ASC
       by ``terminal_at``.
    3. In-progress (``queued`` / ``running`` / ``cancelling``) — sorted
       ASC by ``started_at`` (falling back to ``created_at``).

    Args:
        records: Live and terminal ``WorkflowRunRecord`` instances from
            ``WorkflowsImpl._workflow_registry``.

    Returns:
        A fresh list ordered as: terminal-successes ++ failed-pinned ++
        in-progress. Empty input yields an empty list.
    """
    terminal_success: list["WorkflowRunRecord"] = []
    failed_pinned: list["WorkflowRunRecord"] = []
    in_progress: list["WorkflowRunRecord"] = []

    for record in records:
        phase = record.phase
        if phase in _TERMINAL_SUCCESS_PHASES:
            terminal_success.append(record)
        elif phase == "error":
            if record.acked:
                terminal_success.append(record)
            else:
                failed_pinned.append(record)
        elif phase in _IN_PROGRESS_PHASES:
            in_progress.append(record)
        else:
            # Unknown phase ("unknown" from cold-read); treat as terminal
            # tail so the layout doesn't lose it.
            terminal_success.append(record)

    terminal_success.sort(key=lambda r: r.terminal_at or r.created_at)
    failed_pinned.sort(key=lambda r: r.terminal_at or r.created_at)
    in_progress.sort(key=lambda r: r.started_at or r.created_at)

    return terminal_success + failed_pinned + in_progress


class WorkflowLayoutManager:
    """Batched reorderer for multi-workflow card layout.

    Constructor deps:

    - ``transcript`` — the ``#transcript`` ``VerticalScroll`` container
      cards mount into. The manager calls ``move_child`` to reorder and
      ``mount`` to place the divider.
    - ``get_records`` — a zero-arg callable returning the current list
      of ``WorkflowRunRecord`` values from
      ``WorkflowsImpl._workflow_registry``. Deferred so tests can inject
      a live registry via a lambda without wiring a full impl.
    - ``get_card_widget`` — a callable ``card_id -> CardWidget | None``
      that resolves a widget in the transcript. Wired from
      ``EventAdapter._card_widgets`` at construction time.
    - ``reorder_tick`` — optional override of the batch window; tests
      shrink this to speed up assertions.
    """

    def __init__(
        self,
        *,
        transcript: "VerticalScroll",
        get_records: Callable[[], list["WorkflowRunRecord"]],
        get_card_widget: Callable[[Any], Any | None],
        reorder_tick: float = _REORDER_TICK_SECONDS,
        status_bar: "StatusBar | None" = None,
    ) -> None:
        self._transcript = transcript
        self._get_records = get_records
        self._get_card_widget = get_card_widget
        self._reorder_tick = reorder_tick
        # feat-019: optional StatusBar reference; when wired the
        # manager pushes the live-workflow strip on every flush so the
        # line-2 rendering stays in lockstep with the card layout.
        self._status_bar: "StatusBar | None" = status_bar
        self._divider: Static | None = None
        self._pending_reorder = False
        # Test-facing counter — how many `_flush_reorder` passes have
        # actually run. Test 5 reads this to verify batching.
        self.flush_count = 0

    def register_status_bar(self, status_bar: "StatusBar") -> None:
        """Wire a :class:`StatusBar` handle after construction.

        Called from ``ChatApp.on_mount`` once the widget is composed —
        the manager only exists once the transcript is up, so the
        widget handle is not necessarily known at construction time
        (headless fixtures skip this entirely). Idempotent — a second
        call replaces the reference.
        """
        self._status_bar = status_bar
        # Push whatever state is current so a late-mount status bar
        # picks up in-flight workflows without waiting for the next
        # phase transition.
        self._push_workflow_strip()

    def _push_workflow_strip(self) -> None:
        """Recompute the strip via ``format_live_workflow_strip`` and
        push into the registered :class:`StatusBar`.

        Silent no-op when no status bar is wired (headless fixtures /
        adapter-less paths). Best-effort — any raise from the widget
        seam is swallowed so a bad status-bar can't kill the layout
        drain.
        """
        if self._status_bar is None:
            return
        try:
            records = self._get_records()
            text = format_live_workflow_strip(records)
            self._status_bar.set_workflow_strip(text)
        except Exception:  # noqa: BLE001 — status bar is best-effort
            _LOGGER.exception(
                "WorkflowLayoutManager: status bar push failed",
            )

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def notify_card_state_changed(self, run_id: Any) -> None:
        """Schedule a reorder pass on the next tick.

        Idempotent within one tick window — a second call while a
        reorder is pending is a no-op. Spec-016 AC #3 batching: N
        concurrent phase transitions fire ONE flush.

        ``run_id`` is accepted for future targeting (e.g. "only recompute
        if this specific run changed") but not used today — the manager
        always recomputes the full sort. Kept in the signature so
        ``WorkflowsImpl`` can call with a bare ``run_id``.
        """
        _ = run_id
        if self._pending_reorder:
            return
        self._pending_reorder = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (rare: sync test path without an event
            # loop). Skip; the caller re-fires next transition.
            self._pending_reorder = False
            return
        loop.call_later(
            self._reorder_tick,
            lambda: loop.create_task(self._flush_reorder()),
        )

    async def _flush_reorder(self) -> None:
        """Apply the canonical order to the transcript's children.

        - Recomputes ``sort_workflow_cards(records)``.
        - Flips ``compact_mode`` per spec-016 AC #7-9.
        - Moves each card via ``VerticalScroll.move_child`` to match the
          computed order.
        - Mounts / unmounts the divider AFTER the moves so its own DOM
          index doesn't drift under the reorder.
        """
        self._pending_reorder = False
        self.flush_count += 1
        try:
            records = self._get_records()
            ordered = sort_workflow_cards(records)

            in_progress = [
                r for r in ordered if r.phase in _IN_PROGRESS_PHASES
            ]
            should_compact = len(in_progress) > 2

            # Compact-mode management. Applied BEFORE the DOM shuffle so
            # a mid-move refresh renders the right height each pass.
            for record in ordered:
                self._apply_compact_mode(record, should_compact)

            # DOM move: diff and apply. Q4 lock — divider mount AFTER.
            self._apply_dom_order(ordered)

            # Divider management.
            has_terminal = any(
                r.phase in ("success", "error", "cancelled") for r in ordered
            )
            has_in_progress = len(in_progress) > 0
            await self._sync_divider(ordered, has_terminal, has_in_progress)

            # feat-019: push the current live-workflow strip into the
            # registered StatusBar. Done AFTER the DOM shuffle so the
            # strip lags card state by at most one 250ms tick.
            self._push_workflow_strip()
        except Exception:  # noqa: BLE001 — reorder is best-effort
            _LOGGER.exception(
                "WorkflowLayoutManager: _flush_reorder raised",
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_compact_mode(
        self,
        record: "WorkflowRunRecord",
        should_compact_in_progress: bool,
    ) -> None:
        """Toggle ``renderer.compact_mode`` for one card per policy.

        Terminal cards → always compact.
        In-progress + ``should_compact`` → compact except focused.
        In-progress + not should_compact → full render.

        Also wires the ``_on_error_ack`` callback on the CardWidget so
        pressing ``Enter`` on a failed card flips ``record.acked`` and
        re-fires ``notify_card_state_changed`` — the failed card floats
        from the boundary into the terminal band on the next tick.
        """
        card = record.card
        if card is None:
            return
        widget = self._get_card_widget(card.id)
        if widget is None:
            return
        # spec-016 AC #11: bind ack callback on error-state cards. We
        # rebind on every flush so a replay-only path (where the card
        # exists before the manager's first pass) still gets wired.
        if hasattr(widget, "_on_error_ack") and widget._on_error_ack is None:
            widget._on_error_ack = self._make_ack_callback(record)

        renderer = getattr(widget, "_body", None)
        if renderer is None:
            return
        # Only touch renderers that support the reactive — WorkflowRenderer
        # exposes `compact_mode` as a reactive attribute.
        if not hasattr(renderer, "compact_mode"):
            return
        if record.phase not in _IN_PROGRESS_PHASES:
            renderer.compact_mode = True
            return
        if should_compact_in_progress:
            focused = getattr(widget, "has_focus", False)
            renderer.compact_mode = not bool(focused)
        else:
            renderer.compact_mode = False

    def _make_ack_callback(
        self, record: "WorkflowRunRecord",
    ) -> Callable[[], None]:
        """Build the closure the CardWidget fires on Enter in error state.

        Flips ``record.acked`` and triggers a reorder so the acked
        failure floats up into the terminal band.
        """

        def _ack() -> None:
            record.acked = True
            self.notify_card_state_changed(record.run_id)

        return _ack

    def _apply_dom_order(self, ordered: list["WorkflowRunRecord"]) -> None:
        """Move each card to its computed position via ``move_child``.

        Skips records with no bound card, or cards not tracked in the
        transcript. Reverse-iterating diff — walks from the tail-1
        pair to the head, and for each entry that's currently AFTER
        its intended next-widget anchor, calls
        ``move_child(widget, before=next_widget)``. Reverse order is
        important: after processing ``pairs[i+1]`` (which lands right
        before ``pairs[i+2]``), processing ``pairs[i]`` uses the now-
        settled ``pairs[i+1]`` as its anchor. A forward pass leaves
        cards stuck when an earlier position needs a later widget as
        its anchor.
        """
        pairs: list[Any] = []
        for record in ordered:
            if record.card is None:
                continue
            widget = self._get_card_widget(record.card.id)
            if widget is None:
                continue
            pairs.append(widget)

        # Reverse walk. Skip the last pair (no anchor to its right).
        for i in range(len(pairs) - 2, -1, -1):
            widget = pairs[i]
            next_widget = pairs[i + 1]
            try:
                children = list(self._transcript.children)
                cur = children.index(widget)
                nxt = children.index(next_widget)
            except (ValueError, AttributeError):
                continue
            if cur > nxt:
                with contextlib.suppress(Exception):
                    self._transcript.move_child(widget, before=next_widget)

    async def _sync_divider(
        self,
        ordered: list["WorkflowRunRecord"],
        has_terminal: bool,
        has_in_progress: bool,
    ) -> None:
        """Ensure the divider is mounted iff both groups are non-empty.

        Q4 lock: called AFTER ``_apply_dom_order`` so the divider's own
        DOM index doesn't shift under card moves.
        """
        if has_terminal and has_in_progress:
            if self._divider is None:
                # Mount BEFORE the first in-progress card.
                anchor = self._first_in_progress_widget(ordered)
                divider = Static(DIVIDER_TEXT, id=DIVIDER_ID)
                try:
                    if anchor is not None:
                        await self._transcript.mount(divider, before=anchor)
                    else:
                        await self._transcript.mount(divider)
                    self._divider = divider
                except Exception:  # noqa: BLE001 — headless / stubbed
                    _LOGGER.debug(
                        "WorkflowLayoutManager: divider mount failed",
                        exc_info=True,
                    )
                    return
            else:
                # Divider already mounted — ensure it sits before the
                # first in-progress card.
                self._reposition_divider(ordered)
        else:
            # No dual-group; remove the divider if it exists.
            if self._divider is not None:
                with contextlib.suppress(Exception):
                    self._divider.remove()
                self._divider = None

    def _first_in_progress_widget(
        self, ordered: list["WorkflowRunRecord"],
    ) -> Any | None:
        """Return the widget for the first in-progress record, if any."""
        for record in ordered:
            if record.phase not in _IN_PROGRESS_PHASES:
                continue
            if record.card is None:
                continue
            widget = self._get_card_widget(record.card.id)
            if widget is not None:
                return widget
        return None

    def _reposition_divider(
        self, ordered: list["WorkflowRunRecord"],
    ) -> None:
        """Slot the existing divider before the first in-progress card."""
        if self._divider is None:
            return
        anchor = self._first_in_progress_widget(ordered)
        if anchor is None:
            return
        try:
            children = list(self._transcript.children)
            cur = children.index(self._divider)
            nxt = children.index(anchor)
        except (ValueError, AttributeError):
            return
        if cur >= nxt:
            with contextlib.suppress(Exception):
                self._transcript.move_child(self._divider, before=anchor)
