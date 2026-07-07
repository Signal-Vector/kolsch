"""``CardsImpl`` + ``CardImpl`` — concrete event-sourced Cards service (spec-006).

Emits ``kolsch.card.*`` UI events onto the session store; a bounded
two-lane bus queue backs the streaming path. Critical events
(``opened``, ``closed``, ``snapshot``, ``notice``) use blocking put
so their delivery is guaranteed; streaming events (``appended``,
``set``, ``set_progress``, ``status``) use ``put_nowait`` with a drop
counter so a saturated bus never blocks the tool.

Snapshot coalescing collapses high-frequency ``card.appended`` streams
into periodic ``card.snapshot`` events so ``--resume`` replay stays
efficient. Snapshots fire on N appends (default 50), T ms since the
last append (default 2000), or on close if any appends since the
last snapshot exist.

The concrete widget mount is handled by
[`EventAdapter`][cli.chat.event_adapter.EventAdapter]; this impl
never touches the DOM. See ``AGENTS.md:187-207`` — CardsImpl emits
UI_EVENTs; adapter dispatches; widgets render.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import CardId, EventId, new_card_id
from schemas.wire_kinds import (
    WIRE_CARD_APPENDED,
    WIRE_CARD_CLOSED,
    WIRE_CARD_NOTICE,
    WIRE_CARD_OPENED,
    WIRE_CARD_SET,
    WIRE_CARD_SET_PROGRESS,
    WIRE_CARD_SNAPSHOT,
    WIRE_CARD_STATUS,
)
from tools.services.card_kinds import CardKindLiteral, CardKindSpec, get_kind_spec
from tools.services.cards import (
    AbstractCard,
    AbstractCards,
    Card,
    CardFinalStatus,
    CardStatus,
)
from tools.services.exceptions import CardClosed

if TYPE_CHECKING:
    from types import TracebackType

    from schemas.identifiers import SessionId, ThreadId
    from store.session_store import SessionStore


_LOGGER = logging.getLogger(__name__)


_STREAM_QUEUE_MAXSIZE = 256
"""Bounded queue size for the streaming lane. Spec-006 AC #10 / two-lane
drop policy. Critical events bypass this via blocking `put`; streaming
events increment `_dropped_events` on `QueueFull`."""

_DEFAULT_SNAPSHOT_N = 50
_DEFAULT_SNAPSHOT_MS = 2000


def _new_event_id() -> EventId:
    """Build a fresh `EventId` for a synthesised UI event."""
    return EventId(f"ev_{uuid4()}")


class CardsImpl(AbstractCards):
    """Concrete Cards service.

    Constructor deps come from `ChatApp.on_mount`:

    - ``session_store`` — for `append_event` of every emitted
      ``kolsch.card.*`` UI event.
    - ``session_id`` / ``thread_id`` — folded into the emitted
      event envelope (the store also validates `session_id` against
      the thread record; see `SessionStore.append_event`).

    A single drain worker task consumes queued events and re-publishes
    them onto the in-process bus for `EventAdapter` dispatch. The
    worker is lazy-started on the first `open()` call and cancelled
    on `aclose()`.

    Once [`aclose`][cli.chat.services.cards_impl.CardsImpl.aclose]
    fires, subsequent `open()` calls raise `RuntimeError`.
    """

    def __init__(
        self,
        *,
        session_store: SessionStore,
        session_id: SessionId,
        thread_id: ThreadId,
    ) -> None:
        self._session_store = session_store
        self._session_id = session_id
        self._thread_id = thread_id
        self._closed = False
        self._live_cards: dict[CardId, CardImpl] = {}
        # Two-lane bounded queue. Critical events bypass drop via
        # blocking put; streaming events use put_nowait + drop counter.
        self._stream_queue: asyncio.Queue[Event] = asyncio.Queue(
            maxsize=_STREAM_QUEUE_MAXSIZE,
        )
        self._drain_task: asyncio.Task[None] | None = None
        self._dropped_events: int = 0
        # Track spawned `notice` fire-and-forget tasks so `aclose` can
        # wait for them / cancel them; without this a task started
        # right before shutdown would leak.
        self._pending_tasks: set[asyncio.Task[EventId]] = set()

    # ------------------------------------------------------------------
    # Public surface (Cards Protocol)
    # ------------------------------------------------------------------

    async def open(
        self,
        title: str,
        kind: CardKindLiteral = "markdown",
    ) -> Card:
        """Mint a fresh CardId, emit `kolsch.card.opened`, return a handle.

        Validates `kind` via `get_kind_spec` — raises
        [`UnknownCardKind`][tools.services.exceptions.UnknownCardKind]
        for an unregistered kind. The `opened` event is CRITICAL lane
        (blocking put) so the adapter never misses the mount trigger.
        """
        if self._closed:
            msg = "CardsImpl is closed"
            raise RuntimeError(msg)
        # Validate kind (raises UnknownCardKind on miss).
        spec = get_kind_spec(kind)
        card_id = new_card_id()
        card = CardImpl(
            id=card_id,
            kind=kind,
            title=title,
            kind_spec=spec,
            impl=self,
        )
        self._live_cards[card_id] = card
        # Lazy-start the drain worker on the first open. Do this
        # BEFORE the critical emit so a saturated queue doesn't
        # deadlock the first-ever call. In practice the queue starts
        # empty; the ordering matters only for edge tests.
        self._ensure_drain_task()
        await self._emit_ui_event(
            WIRE_CARD_OPENED,
            {
                "card_id": str(card_id),
                "title": title,
                "kind": kind,
            },
            critical=True,
        )
        return card

    def notice(self, text: str, *, kind: str = "info") -> None:
        """Fire-and-forget emit of `kolsch.card.notice` on the critical lane.

        The Cards Protocol declares `notice` as synchronous — tools call
        it without `await` so the notice fire path can't deadlock a
        long-running tool. We spawn a task that emits the UI event on
        the critical lane; failures are logged and swallowed.
        """
        if self._closed:
            msg = "CardsImpl is closed"
            raise RuntimeError(msg)
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self._emit_ui_event(
                WIRE_CARD_NOTICE,
                {
                    "notice_id": f"ntc_{uuid4()}",
                    "text": text,
                    "kind": kind,
                },
                critical=True,
            ),
        )
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def aclose(self) -> None:
        """Cancel every pending snapshot timer + drain worker. Idempotent.

        Second call is a no-op. Post-aclose, `open()` raises
        `RuntimeError`. Live cards' snapshot timers are cancelled
        (so no post-close snapshots fire); the drain task is cancelled
        and awaited.
        """
        if self._closed:
            return
        self._closed = True
        # Cancel snapshot timers on all live cards (does NOT close the
        # cards — that's the caller's responsibility). Prevents
        # post-aclose snapshot events from being scheduled.
        for card in list(self._live_cards.values()):
            card._cancel_snapshot_timer()
        # Cancel any pending fire-and-forget notice tasks.
        for task in list(self._pending_tasks):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        # Cancel the drain worker.
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain_task

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_drain_task(self) -> None:
        """Start the drain worker lazily; safe to call repeatedly."""
        if self._drain_task is not None and not self._drain_task.done():
            return
        loop = asyncio.get_running_loop()
        self._drain_task = loop.create_task(self._drain_stream_queue())

    async def _emit_ui_event(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        critical: bool,
    ) -> EventId:
        """Append a UI_EVENT to the store; enqueue for bus dispatch.

        `critical=True` uses blocking `await queue.put(evt)` so
        delivery is guaranteed; `critical=False` uses `put_nowait`
        with a drop-counter increment on `QueueFull`.

        Returns the appended event's `event_id` — CardImpl tracks these
        to populate `superseded_event_ids` on snapshot emission.
        """
        event = Event(
            event_id=_new_event_id(),
            parent_event_id=None,
            thread_id=self._thread_id,
            session_id=self._session_id,
            type=EventType.UI_EVENT,
            data={
                "schema_version": 1,
                "kind": kind,
                "payload": payload,
            },
            actor=Actor(kind="system"),
            created_at=datetime.now(UTC),
        )
        # Durable write first (spec-003 replay-on-boot contract:
        # append-then-publish so the bus never carries an event that
        # isn't in the log).
        await self._session_store.append_event(self._thread_id, event)
        # Then publish onto the streaming queue.
        if critical:
            await self._stream_queue.put(event)
        else:
            try:
                self._stream_queue.put_nowait(event)
            except asyncio.QueueFull:
                self._dropped_events += 1
                # spec-008 concern: rate-limited drop notice. For now
                # we just count.
        return event.event_id

    async def _drain_stream_queue(self) -> None:
        """Drain queued events onto the in-process bus.

        The bus publish API is deliberately loose — the current
        chat runtime (spec-003) uses append-then-publish, and the
        adapter picks up events via replay + live subscription. This
        drain worker is the seam where the in-process bus lives.

        Today the drain is a no-op sink (events are already durable in
        the store; the adapter's replay path picks them up on
        subscription). A future spec (spec-008+ / the bus wiring)
        will wire this to an actual pub/sub channel. For now the
        worker exists so the queue can be back-pressured / dropped
        against, and so `aclose` has a task to cancel.
        """
        while True:
            event = await self._stream_queue.get()
            try:
                await self._publish_to_bus(event)
            except Exception:  # noqa: BLE001 — drain is best-effort
                _LOGGER.exception(
                    "CardsImpl: drain publish raised for event %r",
                    event.event_id,
                )
            finally:
                self._stream_queue.task_done()

    async def _publish_to_bus(self, event: Event) -> None:
        """Publish `event` to the in-process bus.

        No-op stub in this spec — see `_drain_stream_queue` docstring.
        The append-then-publish contract from spec-003 means the
        adapter's live subscription (when wired) will observe the
        event via its own replay mechanism; the drain worker exists
        as the queue back-pressure surface.
        """
        _ = event
        return None


class CardImpl(AbstractCard):
    """Concrete Card handle. See module docstring for the whole shape."""

    def __init__(
        self,
        *,
        id: CardId,
        kind: str,
        title: str,
        kind_spec: CardKindSpec,
        impl: CardsImpl,
    ) -> None:
        self.id = id
        self.kind = kind
        self.title = title
        self._kind_spec = kind_spec
        self._impl = impl
        self._closed = False
        # r3 default — workflow-service (spec-017) writes True to
        # transfer ownership; the ABC does not enforce immutability.
        self._owner_transferred = False
        # Snapshot coalescing state.
        self._append_counter = 0
        self._appends_since_snapshot: list[str] = []
        self._pending_event_ids: list[str] = []
        self._snapshot_timer: asyncio.TimerHandle | None = None
        self._SNAPSHOT_N = self._read_env_int(
            "KOLSCH_CARD_SNAPSHOT_N", _DEFAULT_SNAPSHOT_N,
        )
        self._SNAPSHOT_MS = self._read_env_int(
            "KOLSCH_CARD_SNAPSHOT_MS", _DEFAULT_SNAPSHOT_MS,
        )

    @staticmethod
    def _read_env_int(name: str, default: int) -> int:
        """Parse an env var as int with a fallback default.

        A malformed env value (non-int) reverts to the default rather
        than crashing the tool — the env vars are operator knobs, not
        contract-carrying config.
        """
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    # ------------------------------------------------------------------
    # Public surface (Card Protocol)
    # ------------------------------------------------------------------

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def append(self, chunk: str) -> None:
        """Emit `kolsch.card.appended` (streaming lane)."""
        self._gate("append")
        self._append_counter += 1
        self._appends_since_snapshot.append(chunk)
        event_id = await self._impl._emit_ui_event(
            WIRE_CARD_APPENDED,
            {"card_id": str(self.id), "chunk": chunk},
            critical=False,
        )
        self._pending_event_ids.append(str(event_id))
        # Snapshot policy: fire every N appends; between snapshots the
        # T-ms timer covers slow streams.
        if self._append_counter % self._SNAPSHOT_N == 0:
            await self._emit_snapshot()
        else:
            self._schedule_snapshot()

    async def set(self, content: str) -> None:
        """Replace all content — emit `kolsch.card.set` (streaming lane)."""
        self._gate("set")
        await self._impl._emit_ui_event(
            WIRE_CARD_SET,
            {"card_id": str(self.id), "content": content},
            critical=False,
        )

    async def set_progress(
        self,
        current: int,
        total: int,
        *,
        message: str = "",
    ) -> None:
        """Emit `kolsch.card.set_progress` (streaming lane)."""
        self._gate("set_progress")
        await self._impl._emit_ui_event(
            WIRE_CARD_SET_PROGRESS,
            {
                "card_id": str(self.id),
                "current": current,
                "total": total,
                "message": message,
            },
            critical=False,
        )

    async def set_status(self, status: CardStatus) -> None:
        """Emit `kolsch.card.status` (streaming lane)."""
        self._gate("set_status")
        await self._impl._emit_ui_event(
            WIRE_CARD_STATUS,
            {"card_id": str(self.id), "status": status},
            critical=False,
        )

    async def set_title(self, title: str) -> None:
        """Update the card title — emit `kolsch.card.status` (streaming lane).

        The wire kind is `WIRE_CARD_STATUS` per the technical-context
        sketch; the payload's `title` field distinguishes the mutation.
        No dedicated `set_title` wire kind exists in v1.
        """
        self._gate("set_title")
        self.title = title
        await self._impl._emit_ui_event(
            WIRE_CARD_STATUS,
            {"card_id": str(self.id), "title": title},
            critical=False,
        )

    async def close(
        self,
        *,
        collapse: bool = True,
        final_status: CardFinalStatus | None = None,
        error_message: str | None = None,
    ) -> None:
        """Emit `kolsch.card.closed` (critical lane). Idempotent."""
        if self._closed:
            return  # idempotent — second call silently no-ops
        self._closed = True
        # Stop the snapshot timer BEFORE emitting the final snapshot
        # so a pending scheduled callback doesn't race the close.
        self._cancel_snapshot_timer()
        # Flush any un-superseded appends as a final snapshot.
        if self._appends_since_snapshot:
            await self._emit_snapshot()
        # CRITICAL lane — the adapter must observe the close to
        # transition state.
        await self._impl._emit_ui_event(
            WIRE_CARD_CLOSED,
            {
                "card_id": str(self.id),
                "final_status": final_status,
                "collapse": collapse,
                "error_message": error_message,
            },
            critical=True,
        )
        # Drop the card from the live-map so aclose doesn't try to
        # cancel its (already cancelled) timer again.
        self._impl._live_cards.pop(self.id, None)

    async def __aenter__(self) -> Card:
        """Enter the card's async-context scope.

        Raises `CardClosed` if the card is already closed — matches
        the Protocol's r3 footgun guard.
        """
        if self._closed:
            msg = f"Card {self.id} is closed"
            raise CardClosed(msg)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Auto-close with the appropriate terminal status.

        - Normal exit → `final_status="success"`.
        - `CancelledError` exit → `final_status="cancelled"`.
        - Other exception → `final_status="error"` +
          `error_message=str(exc_val)[:200]`.

        Never suppresses the exception (returns `None`, not `True`).
        """
        _ = tb
        if self._closed:
            return
        if exc_type is None:
            await self.close(final_status="success")
        elif issubclass(exc_type, asyncio.CancelledError):
            await self.close(final_status="cancelled")
        else:
            await self.close(
                final_status="error",
                error_message=str(exc_val)[:200],
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _gate(self, op: str) -> None:
        """Validate operation against `_kind_spec.allowed_ops`.

        Raises `CardClosed` SYNCHRONOUSLY on a closed card; raises
        `TypeError` SYNCHRONOUSLY on an op that isn't in this kind's
        allowed set. Both raise BEFORE any await point so callers see
        the failure deterministically and no event fires.
        """
        if self._closed:
            msg = f"Card {self.id} is closed"
            raise CardClosed(msg)
        if op not in self._kind_spec.allowed_ops:
            msg = (
                f"Operation {op!r} not allowed on {self.kind!r} card. "
                f"Allowed: {sorted(self._kind_spec.allowed_ops)}"
            )
            raise TypeError(msg)

    def _schedule_snapshot(self) -> None:
        """Arm the T-ms coalescing timer.

        Cancels any existing timer first so a burst of appends re-arms
        without piling up TimerHandles. The scheduled callback checks
        `_closed` before emitting so a post-close snapshot never fires.
        """
        if self._closed:
            return
        if self._snapshot_timer is not None:
            self._snapshot_timer.cancel()
        loop = asyncio.get_running_loop()
        # Note: `get_running_loop` NOT `get_event_loop` — spec AC #17.
        self._snapshot_timer = loop.call_later(
            self._SNAPSHOT_MS / 1000.0,
            self._on_snapshot_timer_fired,
        )

    def _on_snapshot_timer_fired(self) -> None:
        """TimerHandle callback — schedule a snapshot flush task.

        Runs in the loop's callback context (sync). Check `_closed`
        first so a timer that fired between close-cancel and this
        callback's actual dispatch is a silent no-op.
        """
        if self._closed:
            return
        task = asyncio.create_task(self._flush_snapshot())
        # Detach — the task self-cleans; we do not track it because
        # aclose cancels the timer, which prevents scheduling in the
        # first place.
        _ = task

    def _cancel_snapshot_timer(self) -> None:
        """Cancel + drop the snapshot timer, if any."""
        if self._snapshot_timer is not None:
            self._snapshot_timer.cancel()
            self._snapshot_timer = None

    async def _flush_snapshot(self) -> None:
        """T-ms tick reached — emit a snapshot if the card is still open."""
        if self._closed:
            return
        if not self._appends_since_snapshot:
            return
        await self._emit_snapshot()

    async def _emit_snapshot(self) -> None:
        """Emit `kolsch.card.snapshot` (critical lane); reset buffers."""
        content = "".join(self._appends_since_snapshot)
        superseded_ids = list(self._pending_event_ids)
        await self._impl._emit_ui_event(
            WIRE_CARD_SNAPSHOT,
            {
                "card_id": str(self.id),
                "content": content,
                "superseded_event_ids": superseded_ids,
            },
            critical=True,
        )
        self._appends_since_snapshot = []
        self._pending_event_ids = []
        # After a snapshot, drop the timer — the next append will
        # re-schedule fresh.
        self._cancel_snapshot_timer()


__all__ = ["CardImpl", "CardsImpl"]
