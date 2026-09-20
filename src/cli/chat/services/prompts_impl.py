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
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, PromptId, new_prompt_id
from schemas.wire_kinds import (
    WIRE_PROMPT_ANSWERED,
    WIRE_PROMPT_CANCELLED,
    WIRE_PROMPT_REQUESTED,
)
from tools.services.exceptions import PromptBusy
from tools.services.prompts import AbstractPrompts, PromptSource

_log = logging.getLogger("cli.chat.services.prompts_impl")

# spec-131 Step 9 — mirrors `engine.engine._OBSERVER_TIMEOUT_SECONDS`
# exactly (same value, same reasoning: generous for the real
# `HookDispatcher.observe()`, which only launches background tasks and
# returns in roughly 0.0001s, while still tightly bounding a hung or
# pathologically slow observer). Kept as its own private constant
# rather than importing the engine's — this call site is structurally
# independent of `execute_stream` (that's the whole reason this fix is
# needed; see this module's `_append_ui_event`), and importing a
# private name across module/layer boundaries would be worse than a
# small, intentional duplication of one float.
_HOOK_OBSERVE_TIMEOUT_SECONDS = 3.0


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
        """Construct the `str` subclass, attaching the optional `rule`."""
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
    from platform_runtime import PlatformRuntime
    from schemas.identifiers import SessionId, ThreadId
    from store.session_store import SessionStore


def _new_event_id() -> EventId:
    """Build a fresh `EventId` for a synthesised UI event."""
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
        approval_response_seconds: float | None = None,
        runtime: PlatformRuntime | None = None,
    ) -> None:
        self._session_store = session_store
        self._session_id = session_id
        self._thread_id = thread_id
        # spec-131 Step 9 — the same `PlatformRuntime` (and therefore
        # the same `HookDispatcher`, via `runtime.hook_dispatcher`)
        # `ChatApp.on_mount` wires into every other per-session impl.
        # `runtime` itself never changes across a `/new` session switch
        # (only `session_id`/`thread_id` are repointed via
        # `rebind_session` — see that method), so it is captured once
        # here at construction time and never re-threaded. `None` in
        # headless tests / adapter-less fixtures that construct
        # `PromptsImpl` directly without a runtime, exactly like `bus`.
        self._runtime = runtime
        # bug-114 — approval-response window for prompts raised INSIDE a
        # workflow node. `None`/`<= 0` = OFF (unbounded wait, pre-bug-114
        # behavior). When set, an unanswered workflow-scoped approval
        # auto-resolves to a deny after this many seconds via the normal
        # `WIRE_PROMPT_ANSWERED` bridge — so the node fails as a legible
        # denial instead of the compute timeout absorbing the wait and
        # dying with an opaque `NodeTimeoutExceeded`. Sourced from
        # `platform.yaml:workflows.approval_response_seconds`.
        self._approval_response_seconds = approval_response_seconds
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

    def rebind_session(self, session_id: SessionId, thread_id: ThreadId) -> None:
        """feat-117: retarget durable writes to a new session/thread (`/new`).

        The impl caches the ids and appends events with them
        (``append_event(self._thread_id, ...)``); a `/new` session switch
        must repoint them or new-session events would land in the old thread.
        """
        self._session_id = session_id
        self._thread_id = thread_id

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
            # bug-114: bound in-workflow approval prompts by the
            # configured window; unbounded (pre-bug-114) otherwise.
            window = self._approval_window_seconds(source)
            if window is None:
                return await future
            return await self._await_answer_bounded(
                future,
                window=window,
                prompt_id=prompt_id,
                source=source,
            )
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
        timeout: float = 30.0,  # noqa: ASYNC109 — Prompts Protocol contract
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

    @property
    def pending_prompt_id(self) -> PromptId | None:
        """The id of the in-flight prompt, or ``None``.

        bug-128: read-only accessor so the `/new` + workflow-cancel
        wiring can look up whether the single in-flight prompt (the
        impl is serial) belongs to a given workflow run before driving
        `WIRE_PROMPT_CANCELLED`. Returns `None` when no prompt is
        awaiting an answer.
        """
        return self._pending_prompt_id

    async def cancel_pending_prompt(self, *, reason: str = "session_switched") -> bool:
        """feat-127: cancel the in-flight prompt (if any) via the answered bridge.

        Emits ``WIRE_PROMPT_CANCELLED`` for ``_pending_prompt_id`` so the
        adapter's ``_handle_prompt_cancelled`` cancels the awaiting
        ``choice()`` / ``text()`` future — unblocking a butler-turn approval
        parked on a bare ``await`` (a workflow-node approval is handled by
        ``WorkflowsImpl.cancel``) — and flips the mounted ``ApprovalBlock``.
        Same bug-110/bug-128 consume path; the ``_resolved_prompt_ids`` guard
        makes a duplicate emit a no-op. Returns ``True`` when a cancel was
        emitted, ``False`` when no prompt is pending.
        """
        prompt_id = self._pending_prompt_id
        if prompt_id is None:
            return False
        await self._append_ui_event(
            WIRE_PROMPT_CANCELLED,
            {"prompt_id": str(prompt_id), "reason": reason},
        )
        return True

    def resolve_pending(
        self,
        prompt_id: str,
        response: str,
        *,
        rule: dict[str, Any] | None = None,
    ) -> None:
        """Resolve the in-flight future for `prompt_id` with `response`.

        Called by
        `EventAdapter._handle_ui_event`
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
        `EventAdapter._handle_ui_event`
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

    # -- bug-114 approval-response window ------------------------------

    def _approval_window_seconds(self, source: PromptSource) -> float | None:
        """Return the approval-response window for this prompt, or `None`.

        bug-114: only prompts raised INSIDE a workflow node
        (`source["kind"] == "workflow"`) are bounded, and only when a
        positive window is configured. Returns `None` otherwise — an
        unbounded await, the pre-bug-114 behavior (chat approvals never
        auto-deny; the window is OFF by default).
        """
        window = self._approval_response_seconds
        if window is None or window <= 0:
            return None
        if source.get("kind") != "workflow":
            return None
        return float(window)

    async def _await_answer_bounded(
        self,
        future: asyncio.Future[str],
        *,
        window: float,
        prompt_id: PromptId,
        source: PromptSource,
    ) -> str:
        """Await the answer, auto-denying after `window` seconds (bug-114).

        Races the pending future against a `window`-second timer. If the
        operator answers first, returns their response verbatim. If the
        window elapses first, emits a durable
        `WIRE_PROMPT_ANSWERED(response="n", reason="approval_window_elapsed")`
        — riding the normal answered bridge so the mounted `ApprovalBlock`
        flips terminal and the composer unfreezes — then resolves and
        returns the deny.

        Deliberately does NOT cancel the future (a `CancelledError` would
        surface as a turn-cancel, not a deny) and does NOT touch the
        widget's `_resolved_event` (bug-110: that wakes the answered
        bridge and double-writes the durable answer). The single durable
        answer is the emitted event; the direct `resolve_pending` below
        is idempotent with the adapter's own resolve so a headless
        (no-bus) impl still unblocks.
        """
        loop = asyncio.get_running_loop()
        timer: asyncio.Task[None] = loop.create_task(asyncio.sleep(window))
        # `future` (Future[str]) and `timer` (Task[None]) are both
        # Futures — annotate the set so `asyncio.wait`'s type var binds.
        awaitables: set[asyncio.Future[Any]] = {future, timer}
        try:
            await asyncio.wait(
                awaitables,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not timer.done():
                timer.cancel()
        if future.done():
            return future.result()
        # Window elapsed with no answer — auto-deny via the answered
        # bridge, then resolve (idempotent with the adapter's resolve).
        await self._emit_auto_deny(prompt_id, source)
        self.resolve_pending(str(prompt_id), "n")
        return await future

    async def _emit_auto_deny(
        self,
        prompt_id: PromptId,
        source: PromptSource,
    ) -> None:
        """Emit the durable auto-deny `WIRE_PROMPT_ANSWERED` (bug-114).

        `response="n"` deny-once; `reason="approval_window_elapsed"` marks
        it as a window-elapsed auto-deny (vs an interactive `n`) so the
        adapter renders a distinct terminal footer. Rides
        `EventAdapter._handle_prompt_answered`, which flips the widget +
        fires the workflow-scoped `approval_resolved`. On resume the
        event replays as a plain deny (born-resolved).
        """
        payload: dict[str, Any] = {
            "prompt_id": str(prompt_id),
            "response": "n",
            "reason": "approval_window_elapsed",
            "source": dict(source),
        }
        await self._append_ui_event(WIRE_PROMPT_ANSWERED, payload)

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

        spec-131 Step 9 (reordered post-review — see "ordering" note
        below): AFTER the bus publish, forward the SAME `Event` to
        `runtime.hook_dispatcher.observe()` (if a runtime + dispatcher
        are wired) for both `WIRE_PROMPT_REQUESTED` and
        `WIRE_PROMPT_ANSWERED` kinds. This writer bypasses
        `_Exchange._emit`/the queue `AgentEngine.execute_stream` reads
        from entirely (see this module's top-of-file docstring), so
        Step 5's engine-level observer seam never sees these events on
        its own — this call is what closes that gap for the spec's
        headline `kolsch.prompt.requested` use case (and its
        `.answered` counterpart) without routing through the engine at
        all. `None` runtime / `None` `hook_dispatcher` is a silent
        no-op — zero behavior change from before this call existed.
        Matches Step 5's own call-site posture in `execute_stream`
        exactly: bounded by `_HOOK_OBSERVE_TIMEOUT_SECONDS` via
        `asyncio.wait_for` and never allowed to raise into this
        method — a hook dispatcher (or any future observer)
        misbehaving must not break this method.

        Ordering (fixed after a Step 9 review finding — do not reorder
        back): the bus publish MUST happen BEFORE the hook-observe
        call, not after. `bus.publish` is what drives
        `EventAdapter.handle` to mount the live `ApprovalBlock` widget
        for `WIRE_PROMPT_REQUESTED` — the user-visible signal that a
        prompt is happening. With the hook-observe call sequenced
        first (the original Step 9 shape), a slow or hung
        `observer.observe()` delayed that widget mount by up to
        `_HOOK_OBSERVE_TIMEOUT_SECONDS` (measured: 3.002s with a
        hanging observer double) — a real, user-visible latency
        injection into the approval flow, contradicting AC #5 ("never
        delays... the underlying kolsch turn"). Publishing first means
        the widget mount is fully decoupled from hook-observer speed;
        the awaited hook-observe call only affects how long THIS
        METHOD takes to return, which nothing else depends on — the
        approval itself resolves later via the `Future` the user's own
        response resolves (`resolve_pending`), not via this method's
        return. Store-append still comes first, unchanged — durability
        before either side-effect.

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

        # Publish to the bus BEFORE the hook-observe call below — this
        # ordering is load-bearing, not incidental (Step 9 review
        # finding). `bus.publish` is what drives `EventAdapter.handle`
        # to mount the live `ApprovalBlock` widget for
        # `WIRE_PROMPT_REQUESTED` — the user-visible signal that a
        # prompt is happening — and must not be gated behind a
        # potentially-slow hook observer. See the docstring's
        # "Ordering" section for the full reasoning and the measured
        # 3.002s delay this fixes.
        if self._bus is not None:
            with _cl.suppress(Exception):
                await self._bus.publish(event)

        # spec-131 Step 9 — fire-and-forget hook observation for the
        # two documented UI-event kinds, AFTER the bus publish above.
        # See the docstring above for why this call exists at all (the
        # structural bypass of `execute_stream`'s observer seam) and
        # its error-handling posture (mirrors
        # `engine.engine.execute_stream`'s own observer call site
        # exactly: timeout-bounded, exceptions caught and logged, never
        # raised into the caller). A slow/hung observer here only
        # delays this method's own return, not the widget mount above.
        if kind in (WIRE_PROMPT_REQUESTED, WIRE_PROMPT_ANSWERED):
            await self._observe_hook_event(event)

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

    async def _observe_hook_event(self, event: Event) -> None:
        """Forward `event` to `runtime.hook_dispatcher.observe()`, if wired.

        spec-131 Step 9. Silent no-op when `self._runtime` is `None`
        (headless tests / adapter-less fixtures — matches every other
        `self._runtime`-independent code path in this class) or when
        `runtime.hook_dispatcher` is `None` (a `PlatformRuntime`
        constructed without one — `bootstrap_runtime` itself always
        wires a real `HookDispatcher`, even with zero configured
        rules, but this method does not assume that of every possible
        `PlatformRuntime`).

        Error-handling posture mirrors `engine.engine.execute_stream`'s
        own observer call site exactly (spec-131 Step 5): bounded by
        `asyncio.wait_for(..., timeout=_HOOK_OBSERVE_TIMEOUT_SECONDS)`,
        with `TimeoutError` and any other `Exception` caught and
        logged rather than propagated — a hung or misbehaving hook
        dispatcher can never raise into or otherwise break the caller.

        **What is and isn't guaranteed about delay (precise, post-
        review — an earlier version of this docstring overclaimed
        here):** a slow or hung `observer.observe()` CAN still delay
        THIS METHOD's own return by up to
        `_HOOK_OBSERVE_TIMEOUT_SECONDS` — that bound is real, not
        eliminated. What's actually guaranteed is that this delay is
        harmless: `_append_ui_event` (the sole caller) awaits
        `self._bus.publish(event)` — the call that mounts the visible
        `ApprovalBlock` widget — BEFORE calling this method, so the
        user-visible prompt-appearing signal is never gated behind
        this method's latency. Nothing else downstream depends on
        `_append_ui_event`'s own return timing either: the approval
        itself resolves later, asynchronously, via the `Future` the
        user's actual response resolves (`resolve_pending`), fully
        decoupled from when this call happens to finish. In short: a
        slow hook observer can make one internal `await` take longer;
        it cannot delay anything the user perceives.
        """
        runtime = self._runtime
        if runtime is None:
            return
        dispatcher = getattr(runtime, "hook_dispatcher", None)
        if dispatcher is None:
            return
        try:
            await asyncio.wait_for(
                dispatcher.observe(event),
                timeout=_HOOK_OBSERVE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            _log.warning(
                "PromptsImpl._observe_hook_event: hook_dispatcher.observe "
                "exceeded %.1fs for event_id=%s kind=%s — continuing",
                _HOOK_OBSERVE_TIMEOUT_SECONDS,
                event.event_id,
                event.data.get("kind"),
            )
        except Exception:  # noqa: BLE001 — observer is untrusted, mirrors engine.py
            _log.exception(
                "PromptsImpl._observe_hook_event: hook_dispatcher.observe "
                "raised for event_id=%s kind=%s — continuing",
                event.event_id,
                event.data.get("kind"),
            )


__all__ = ["PromptResponse", "PromptsImpl"]
