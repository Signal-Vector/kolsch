"""The EventObserver Protocol — the engine's event-stream seam.

Mirrors [`Framework`][engine.framework.Framework]: the engine depends
on this Protocol, never on a concrete observer implementation. Where
`Framework` lets the engine delegate the agentic work to a swappable
driver, `EventObserver` lets the engine notify a swappable listener
about every event it emits, without knowing anything about what the
listener does with it (shell out to a hook command, log, forward to a
UI — the engine doesn't know or care).

spec-131 Step 5 — introduced so a `HookDispatcher` (built and injected
in Step 6, living in `src/platform_runtime/hooks.py`) can observe
every event `AgentEngine.execute_stream` yields, from a single choke
point inside the engine, covering every current and future caller
(chat, headless run, sub-agent/workflow runs, the HTTP API) without
per-call-site wiring. `src/engine/observers.py` was evaluated first
and does not fit: its `_ObservedLLMHandle` / `_ObservedToolHandle`
wrappers are scoped to the `Framework` Protocol's `LLMHandle` /
`ToolHandle` seams (in-band observation of individual LLM/tool calls,
engine-internal, not part of the public surface) — not the full
yielded-`Event` stream a hook system needs to see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from schemas import Event


class EventObserver(Protocol):
    """Notified once per event `AgentEngine.execute_stream` yields.

    `observe` MUST NOT raise, and MUST return promptly, in a
    well-behaved implementation — but the engine does not trust either
    guarantee: the call site in `execute_stream` catches and logs any
    raised exception, AND bounds the call with a timeout so a hung or
    pathologically slow `observe` cannot stall the primary
    event-delivery path indefinitely (defense in depth; see the call
    site's comment for both). `observe` MUST also propagate
    ``asyncio.CancelledError`` rather than catching and swallowing it —
    the timeout is enforced via ``asyncio.wait_for``, which cancels a
    still-running `observe` on timeout but cooperatively waits for that
    cancellation to actually be *honored*; an implementation that
    catches `CancelledError` and keeps running defeats the bound
    entirely (verified: this reintroduces the exact indefinite-hang
    risk the timeout exists to close). `HookDispatcher` (Step 4) never
    does this — it only launches a background `asyncio.create_task`
    and returns — but this is a real hazard for any future
    `EventObserver` implementation to be aware of.

    `observe` receives the exact `Event` instance the caller also
    receives — **this is a documented contract, not a structural
    guarantee**: `Event` (`schemas.events.Event`) is a regular,
    mutable Pydantic model (`ConfigDict(frozen=False, ...)`), and the
    SAME object reference is handed to both the observer and the
    caller's `yield`. An `observe` implementation that mutates the
    event in place WOULD be visible to the caller — nothing in this
    seam copies or freezes the event to prevent that. Implementations
    must treat the event as read-only. The engine does not defensively
    copy or freeze events before this call because the only production
    observer this spec builds (`HookDispatcher`, Step 4) never
    mutates events; adding copy-on-observe overhead to every event on
    every exchange to guard against a violation that doesn't exist in
    practice was judged not worth the per-event cost.
    """

    async def observe(self, event: Event) -> None:
        """Called once per yielded event, in yield order.

        Must not raise, and should return promptly — the caller
        enforces both a timeout and exception-swallowing around this
        call as a backstop, but a well-behaved implementation should
        not rely on either.
        """
