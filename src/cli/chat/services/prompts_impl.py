"""``PromptsImpl`` — concrete event-sourced `Prompts` service (spec-004).

Emits `kolsch.prompt.requested` UI events on `choice` / `text` and
awaits a future the
[`EventAdapter`][cli.chat.event_adapter.EventAdapter] resolves when
matching `.answered` / `.cancelled` events dispatch through the
event pipeline. The widget mount itself lives on the adapter's
prompt routing branch — this impl never touches the DOM.

Serial policy (spec-001, enforced here): one in-flight prompt per
impl. A second concurrent `choice` / `text` call raises
[`PromptBusy`][tools.services.exceptions.PromptBusy] SYNCHRONOUSLY
(before any await). Tools that want to queue instead of failing
can call
[`wait_for_slot`][cli.chat.services.prompts_impl.PromptsImpl.wait_for_slot]
first.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, PromptId, new_prompt_id
from schemas.wire_kinds import WIRE_PROMPT_REQUESTED
from tools.services.exceptions import PromptBusy
from tools.services.prompts import AbstractPrompts, PromptSource

if TYPE_CHECKING:
    from bus.event_bus import EventBus
    from schemas.identifiers import SessionId, ThreadId
    from store.session_store import SessionStore


def _new_event_id() -> EventId:
    """Build a fresh `EventId` for a synthesised UI event."""
    from uuid import uuid4

    return EventId(f"ev_{uuid4()}")


class PromptsImpl(AbstractPrompts):
    """Concrete `Prompts` service. See module docstring.

    Constructor deps come from `ChatApp.on_mount`:

    - ``session_store`` — for `append_event` of the emitted
      `kolsch.prompt.requested` UI event.
    - ``session_id`` / ``thread_id`` — folded into the emitted
      event envelope (the store also validates `session_id` against
      the thread record; see `SessionStore.append_event`).

    Once
    [`aclose`][cli.chat.services.prompts_impl.PromptsImpl.aclose]
    fires, subsequent `choice` / `text` calls raise `RuntimeError`;
    an in-flight future is cancelled with
    ``asyncio.CancelledError``.
    """

    def __init__(
        self,
        *,
        session_store: SessionStore,
        session_id: SessionId,
        thread_id: ThreadId,
        bus: "EventBus | None" = None,
    ) -> None:
        self._session_store = session_store
        self._session_id = session_id
        self._thread_id = thread_id
        # bug-020: symmetric with `CardsImpl` / `WorkflowsImpl` — when
        # a runtime bus is injected, `_append_ui_event` publishes each
        # WIRE_PROMPT_* event on `UI_EVENT_TOPIC` so the live
        # `EventAdapter.handle` subscription in `ChatApp.on_mount`
        # dispatches the ApprovalBlock mount in the SAME session
        # rather than only on next-boot replay. `None` in headless
        # tests / adapter-less fixtures.
        self._bus: "EventBus | None" = bus
        self._pending_future: asyncio.Future[str] | None = None
        self._pending_prompt_id: PromptId | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Public surface (Prompts Protocol)
    # ------------------------------------------------------------------

    async def choice(
        self,
        prompt: str,
        options: list[str],
        *,
        source: PromptSource,
    ) -> str:
        """Emit `kolsch.prompt.requested` with `kind="choice"`; await the answer.

        Serial policy: raises
        [`PromptBusy`][tools.services.exceptions.PromptBusy]
        SYNCHRONOUSLY when another prompt is already in flight
        (before any await point, so the caller sees the failure
        deterministically).

        Cancellation:

        - `WIRE_PROMPT_CANCELLED` dispatched through the adapter →
          raises `asyncio.CancelledError` from the awaiting
          `choice()`. Matches the Esc → deny-once semantics of the
          shipped approval UX.
        - `aclose()` while awaiting → same
          `asyncio.CancelledError`.
        """
        self._guard_open_and_free()
        prompt_id = new_prompt_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending_future = future
        self._pending_prompt_id = prompt_id

        payload: dict[str, Any] = {
            "prompt_id": str(prompt_id),
            "kind": "choice",
            "prompt": prompt,
            "options": list(options),
            "source": dict(source),
        }
        try:
            await self._append_ui_event(WIRE_PROMPT_REQUESTED, payload)
            return await future
        finally:
            self._pending_future = None
            self._pending_prompt_id = None

    async def text(
        self,
        prompt: str,
        *,
        placeholder: str = "",
        source: PromptSource,
    ) -> str:
        """Emit `kolsch.prompt.requested` with `kind="text"`; await the answer.

        Same serial + cancellation semantics as `choice`.
        """
        self._guard_open_and_free()
        prompt_id = new_prompt_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending_future = future
        self._pending_prompt_id = prompt_id

        payload: dict[str, Any] = {
            "prompt_id": str(prompt_id),
            "kind": "text",
            "prompt": prompt,
            "placeholder": placeholder,
            "source": dict(source),
        }
        try:
            await self._append_ui_event(WIRE_PROMPT_REQUESTED, payload)
            return await future
        finally:
            self._pending_future = None
            self._pending_prompt_id = None

    async def wait_for_slot(
        self,
        *,
        source: PromptSource | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Block until the current in-flight prompt resolves.

        Cheap for tools that want to queue rather than fail. Returns
        immediately when no prompt is in flight; raises
        [`PromptBusy`][tools.services.exceptions.PromptBusy] after
        ``timeout`` seconds if the pending future has not resolved.

        ``source`` is accepted for signature parity with the
        `Prompts` Protocol; today it's only used for future queue
        arbitration. Timeout defaults to 30s per spec-001.
        """
        _ = source
        pending = self._pending_future
        if pending is None or pending.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(pending), timeout=timeout)
        except asyncio.TimeoutError as exc:
            msg = (
                f"Prompts.wait_for_slot: timed out after {timeout}s waiting for "
                f"prompt {self._pending_prompt_id} to resolve"
            )
            raise PromptBusy(msg) from exc

    async def aclose(self) -> None:
        """Teardown from `ChatApp.on_unmount`. Idempotent.

        Cancels the in-flight future (if any); marks the impl closed.
        Second call is a no-op. Subsequent `choice` / `text` raise
        `RuntimeError` — the impl is dead.
        """
        if self._closed:
            return
        self._closed = True
        pending = self._pending_future
        if pending is not None and not pending.done():
            pending.cancel()

    # ------------------------------------------------------------------
    # Friend-of-class helpers (called by EventAdapter)
    # ------------------------------------------------------------------

    def resolve_pending(self, prompt_id: str, response: str) -> None:
        """Resolve the in-flight future for `prompt_id` with `response`.

        Called by
        [`EventAdapter._handle_ui_event`][cli.chat.event_adapter.EventAdapter._handle_ui_event]
        on `WIRE_PROMPT_ANSWERED`. Idempotent — no-op when the future
        is already done or the prompt_id doesn't match the in-flight
        one (a stale event from a previous prompt).
        """
        if self._pending_prompt_id is None:
            return
        if str(self._pending_prompt_id) != prompt_id:
            return
        pending = self._pending_future
        if pending is None or pending.done():
            return
        pending.set_result(response)

    def cancel_pending(self, prompt_id: str) -> None:
        """Cancel the in-flight future for `prompt_id`.

        Called by
        [`EventAdapter._handle_ui_event`][cli.chat.event_adapter.EventAdapter._handle_ui_event]
        on `WIRE_PROMPT_CANCELLED`. Idempotent — no-op when the
        future is already done or the prompt_id doesn't match.
        Raises `asyncio.CancelledError` from the awaiting `choice()`
        / `text()`. Matches the Esc → deny-once semantics of the
        shipped approval UX.
        """
        if self._pending_prompt_id is None:
            return
        if str(self._pending_prompt_id) != prompt_id:
            return
        pending = self._pending_future
        if pending is None or pending.done():
            return
        pending.cancel()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _guard_open_and_free(self) -> None:
        """Enforce the closed / busy invariants at call-in.

        Raises SYNCHRONOUSLY (before any await) so a second concurrent
        `choice` / `text` observes `PromptBusy` deterministically.
        """
        if self._closed:
            msg = "PromptsImpl is closed"
            raise RuntimeError(msg)
        if self._pending_future is not None:
            msg = (
                f"Another prompt is unresolved: "
                f"prompt_id={self._pending_prompt_id}"
            )
            raise PromptBusy(msg)

    async def _append_ui_event(self, kind: str, payload: dict[str, Any]) -> None:
        """Append a `UI_EVENT` for the given kind + payload.

        Uses `SessionStore.append_event` so the store's logical_seq
        allocation (spec-003) covers this write. Emits with
        `actor=system` — the prompt request is a platform-owned
        surface, not a user or agent utterance.

        bug-020: after the store append, publish the SAME `Event`
        onto the runtime bus (if injected) so the live
        `EventAdapter.handle` subscription reaches the ApprovalBlock
        mount site in the SAME session. Bus failures are swallowed —
        the store append is the durability contract.
        """
        import contextlib as _cl  # noqa: PLC0415

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
        await self._session_store.append_event(self._thread_id, event)
        if self._bus is not None:
            with _cl.suppress(Exception):
                await self._bus.publish(event)


__all__ = ["PromptsImpl"]
