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

bug-025 (Rule round-trip): ``choice()`` still returns ``str`` per
the [`Prompts`][tools.services.prompts.Prompts] Protocol, but the
concrete return value is a
[`PromptResponse`][cli.chat.services.prompts_impl.PromptResponse]
(a `str` subclass) that MAY carry an optional ``rule`` dict when the
user picked a pattern-persist option (``a`` / ``d``) via the
inline ``ApprovalBlock`` widget. Every existing ``response == "y"``
comparison keeps working byte-identical because ``PromptResponse``
inherits from ``str``; the callback side type-narrows via
``isinstance(response, PromptResponse)`` and rebuilds the
``(AuthzEffect, Rule)`` tuple so the authz provider's tuple branch
at ``src/providers/authz.py:1106`` fires and persists the rule to
``platform.yaml``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, PromptId, new_prompt_id
from schemas.wire_kinds import WIRE_PROMPT_CANCELLED, WIRE_PROMPT_REQUESTED
from tools.services.exceptions import PromptBusy
from tools.services.prompts import AbstractPrompts, PromptSource


class PromptResponse(str):
    """A `str` subclass carrying an optional persist-rule dict.

    Load-bearing for the Rule persistence round-trip (bug-025 / closes
    bug-022). ``PromptsImpl.choice`` narrows its ``str`` return via
    this shape so the answered response can carry the widget-built
    ``Rule`` back into ``InlineApprovalCallback._translate_response``
    WITHOUT changing the Protocol's ``-> str`` contract.

    - ``key`` mirrors ``str(self)`` — one of ``y`` / ``a`` / ``n`` /
      ``d`` for the approval flow; free text for ``.text()``.
    - ``rule`` is a ``dict`` shape compatible with
      :class:`providers.authz.Rule` when the user picked
      pattern-persist (``a`` / ``d``); ``None`` on the fast paths
      (``y`` / ``n``) and for the ``.text()`` surface.

    Design rationale (open-question resolution): a ``str`` subclass
    is preferred over a dataclass / NamedTuple / pydantic model
    because the tools-facing
    [`Prompts`][tools.services.prompts.Prompts] Protocol declares
    ``-> str``. Changing it to ``PromptResponse`` would ripple into
    dozens of existing tests that assert ``result == "y"`` and into
    author-facing tool code that stores the response as a plain
    string. Sub-classing ``str`` preserves ``__eq__`` /
    ``__hash__`` / iteration / f-string behaviour byte-identical
    while letting the callback layer type-narrow via
    ``isinstance(x, PromptResponse)`` to reach ``.rule``.
    """

    __slots__ = ("rule",)

    rule: dict[str, Any] | None

    def __new__(
        cls,
        key: str,
        *,
        rule: dict[str, Any] | None = None,
    ) -> PromptResponse:
        obj = super().__new__(cls, key)
        obj.rule = rule
        return obj

    @property
    def key(self) -> str:
        """Return the response key as a plain ``str``.

        Convenience accessor for callers that want to be explicit
        about the string half of the payload rather than relying on
        the ``str`` subclass. Equivalent to ``str(self)``.
        """
        return str.__str__(self)


if TYPE_CHECKING:
    from collections.abc import Callable

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
        bus: EventBus | None = None,
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
        self._bus: EventBus | None = bus
        self._pending_future: asyncio.Future[str] | None = None
        self._pending_prompt_id: PromptId | None = None
        # spec-068 Part B — bound `close_span` for the current
        # exchange's registry, snapshotted at `choice`/`text` call
        # time (contextvars propagate via task-locals but we hold
        # the callable directly so `resolve_pending` /
        # `cancel_pending` — which run from the adapter's task, not
        # ours — can still find the right registry). `None` when
        # the prompt fired outside an engine exchange (rare: tool
        # call chain always originates from an exchange today).
        self._pending_close_span: Callable[[str], None] | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Public surface (Prompts Protocol)
    # ------------------------------------------------------------------

    # bug-021: reserved payload keys that MUST NOT be clobbered by
    # a caller-supplied `metadata` dict. Keeping this at the class
    # level so `choice` and `text` share a single source of truth
    # and the test that asserts precedence can import it.
    _RESERVED_PAYLOAD_KEYS: frozenset[str] = frozenset(
        {
            "prompt_id",
            "kind",
            "prompt",
            "options",
            "placeholder",
            "source",
        }
    )

    async def choice(
        self,
        prompt: str,
        options: list[str],
        *,
        source: PromptSource,
        metadata: dict[str, Any] | None = None,
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

        bug-021: ``metadata`` (optional) is merged into the emitted
        payload as top-level keys. Reserved keys (see
        ``_RESERVED_PAYLOAD_KEYS``) win — a metadata key with the
        same name is silently dropped. The default ``None`` keeps
        every pre-bug-021 caller shape byte-identical.
        """
        self._guard_open_and_free()
        prompt_id = new_prompt_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending_future = future
        self._pending_prompt_id = prompt_id

        payload: dict[str, Any] = {}
        # bug-021: metadata keys land FIRST so the reserved-key
        # assignment below overwrites any accidental collision.
        # Precedence: reserved keys always win. This is safer than
        # a post-filter loop because the reserved-key contract is
        # expressed by simple dict-assignment order.
        if metadata:
            for key, value in metadata.items():
                if key in self._RESERVED_PAYLOAD_KEYS:
                    continue
                payload[key] = value
        payload["prompt_id"] = str(prompt_id)
        payload["kind"] = "choice"
        payload["prompt"] = prompt
        payload["options"] = list(options)
        payload["source"] = dict(source)
        try:
            await self._append_ui_event(WIRE_PROMPT_REQUESTED, payload)
            return await future
        finally:
            self._pending_future = None
            self._pending_prompt_id = None
            # spec-068 Part B — clear the exchange-registry hook so
            # a follow-on prompt in the SAME exchange starts clean.
            # If the exchange died mid-await (CancelledError bubbling
            # through this finally), the span is DELIBERATELY not
            # closed: the engine's `close_all_open_spans` in the
            # exchange finally will emit the terminal. If the future
            # resolved / was cancelled via a durable widget append,
            # `resolve_pending` / `cancel_pending` already ran
            # `close_span` before this finally fires.
            self._pending_close_span = None

    async def text(
        self,
        prompt: str,
        *,
        placeholder: str = "",
        source: PromptSource,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Emit `kolsch.prompt.requested` with `kind="text"`; await the answer.

        Same serial + cancellation semantics as `choice`.

        bug-021: signature-parity ``metadata`` kwarg. Merge semantics
        match ``choice`` — see the docstring there.
        """
        self._guard_open_and_free()
        prompt_id = new_prompt_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending_future = future
        self._pending_prompt_id = prompt_id

        payload: dict[str, Any] = {}
        if metadata:
            for key, value in metadata.items():
                if key in self._RESERVED_PAYLOAD_KEYS:
                    continue
                payload[key] = value
        payload["prompt_id"] = str(prompt_id)
        payload["kind"] = "text"
        payload["prompt"] = prompt
        payload["placeholder"] = placeholder
        payload["source"] = dict(source)
        try:
            await self._append_ui_event(WIRE_PROMPT_REQUESTED, payload)
            return await future
        finally:
            self._pending_future = None
            self._pending_prompt_id = None
            # spec-068 Part B — clear the exchange-registry hook so
            # a follow-on prompt in the SAME exchange starts clean.
            # If the exchange died mid-await (CancelledError bubbling
            # through this finally), the span is DELIBERATELY not
            # closed: the engine's `close_all_open_spans` in the
            # exchange finally will emit the terminal. If the future
            # resolved / was cancelled via a durable widget append,
            # `resolve_pending` / `cancel_pending` already ran
            # `close_span` before this finally fires.
            self._pending_close_span = None

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
        except TimeoutError as exc:
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

    def resolve_pending(
        self,
        prompt_id: str,
        response: str,
        *,
        rule: dict[str, Any] | None = None,
    ) -> None:
        """Resolve the in-flight future for `prompt_id` with `response`.

        Called by
        [`EventAdapter._handle_ui_event`][cli.chat.event_adapter.EventAdapter._handle_ui_event]
        on `WIRE_PROMPT_ANSWERED`. Idempotent — no-op when the future
        is already done or the prompt_id doesn't match the in-flight
        one (a stale event from a previous prompt).

        bug-025: ``rule`` (default ``None``) carries the persist-rule
        dict the widget built when the user picked ``a`` / ``d``
        (allow-persist / deny-persist). It is wrapped alongside the
        response key into a
        [`PromptResponse`][cli.chat.services.prompts_impl.PromptResponse]
        so the awaiting ``choice()`` / ``text()`` future returns a
        ``str`` (Protocol-compatible) that
        ``InlineApprovalCallback._translate_response`` type-narrows
        back to a ``(AuthzEffect, Rule)`` tuple. ``None`` on the fast
        paths (``y`` / ``n``) is the pre-bug-025 shape byte-identical.
        """
        if self._pending_prompt_id is None:
            return
        if str(self._pending_prompt_id) != prompt_id:
            return
        pending = self._pending_future
        if pending is None or pending.done():
            return
        pending.set_result(PromptResponse(response, rule=rule))
        # spec-068 Part B — the durable terminal (widget-appended
        # `WIRE_PROMPT_ANSWERED`) has already landed on the log, so
        # the exchange's finally-safety-net MUST NOT emit a synthetic
        # `WIRE_PROMPT_CANCELLED(session_interrupted)` on top. Close
        # the span.
        close_span = self._pending_close_span
        if close_span is not None:
            close_span(prompt_id)
        self._pending_close_span = None

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
        # spec-068 Part B — dispatch arrives from
        # `_handle_ui_event` on a `WIRE_PROMPT_CANCELLED` durable
        # append (widget-driven Esc / deny-once). The terminal is
        # already on the log, so close the span to prevent the
        # exchange finally-safety-net from double-emitting.
        close_span = self._pending_close_span
        if close_span is not None:
            close_span(prompt_id)
        self._pending_close_span = None

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
            msg = f"Another prompt is unresolved: prompt_id={self._pending_prompt_id}"
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

        spec-068 Part B: on a `WIRE_PROMPT_REQUESTED` write, plug
        the prompt into the active exchange's span registry (if
        any). The registered terminal-factory produces the exact
        `WIRE_PROMPT_CANCELLED(reason="session_interrupted")`
        UI_EVENT the (now-deleted) renderer synth wrote and the
        engine's resume reconciliation writes — one source of
        truth. The engine's exchange-finally invokes the factory
        via its `_emit` on cancel/quit; `resolve_pending` /
        `cancel_pending` call `close_span` when the widget-appended
        terminal has already landed, so the finally-safety-net
        skips it (no double terminal). See
        `engine.engine.current_span_registry`.
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

        # spec-068 Part B — register the span AFTER the store
        # append so no factory can fire a terminal for a
        # `.requested` the log never mentions.
        if kind == WIRE_PROMPT_REQUESTED:
            from engine.engine import current_span_registry  # noqa: PLC0415

            registry = current_span_registry()
            if registry is not None:
                register_span, close_span = registry
                prompt_id = payload.get("prompt_id")
                if isinstance(prompt_id, str) and prompt_id:
                    span_id = prompt_id

                    def _terminal_factory(
                        _pid: str = span_id,
                    ) -> tuple[EventType, dict[str, Any], Actor]:
                        return (
                            EventType.UI_EVENT,
                            {
                                "schema_version": 1,
                                "kind": WIRE_PROMPT_CANCELLED,
                                "payload": {
                                    "prompt_id": _pid,
                                    "reason": "session_interrupted",
                                },
                            },
                            Actor(kind="system"),
                        )

                    register_span(span_id, _terminal_factory)
                    self._pending_close_span = close_span


__all__ = ["PromptResponse", "PromptsImpl"]
