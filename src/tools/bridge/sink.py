"""The bridge's emit sink — queue-less, trace-aware, serialized (spec-120).

The engine's `_emit` (`src/engine/engine.py`, UNTOUCHED by this spec)
does append-under-lock + `trace_id` stamp + live-UI `queue.put_nowait`
+ bus publish. The bridge is a headless caller — there is no chat
surface to feed — so its sink does the same **minus the queue**:

1. take the passed per-thread lock,
2. read the leaf to chain `parent_event_id`,
3. append via `SessionStore.append_event` (which stamps `logical_seq`
   under the store's flock),
4. best-effort bus publish.

`trace_id` is stamped from the bound exchange context. Event parity
with the engine emit is acceptance-tested
(`tests/unit/tools/test_bridge_emit_sink.py`), not assumed.

The store/bus/lock are **passed in** as plain objects — this module
imports `store`/`bus` types only (both cycle-free from `tools/`, see
`tools/context_reference.py:28`) and never `engine`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from schemas import Actor, Event, EventId, EventType, SessionId, ThreadId

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable

    from bus.event_bus import EventBus
    from store import SessionStore

_log = logging.getLogger("tools.bridge")


def make_bridge_emit_sink(
    *,
    store: SessionStore,
    bus: EventBus,
    lock: asyncio.Lock,
    session_id: str,
    thread_id: str,
    trace_id: str | None,
) -> Callable[..., Awaitable[None]]:
    """Build the bridge's `emit(*, type_, data, actor)` closure.

    Mirrors the engine's `_emit` semantics — leaf-read + append under
    the SAME per-thread lock the engine serializes on, then bus
    publish inside the lock — with two deliberate differences: no
    live-UI queue operation, and `trace_id` stamped from the bound
    context rather than an `ExecutionContext`.

    Args:
        store: The live `SessionStore` the exchange writes to.
        bus: The exchange's event bus; publish failures are logged
            and swallowed (best-effort, engine-identical).
        lock: The exchange's per-thread `asyncio.Lock`
            (`engine._lock_for_thread(thread_id)`), passed as a plain
            object so this module never imports `engine`.
        session_id: Bound session id (string form, `ses_<uuid>`).
        thread_id: Bound thread id (string form, `th_<uuid>`).
        trace_id: The bound exchange's trace id, stamped verbatim on
            every emitted envelope; `None` stays `None`.

    Returns:
        An async sink with the engine-emit keyword shape
        `(type_=EventType, data=dict, actor=Actor)`, suitable as
        `invoke_observed(emit=...)`.
    """
    sid = SessionId(session_id)
    tid = ThreadId(thread_id)
    stamped_trace = str(trace_id) if trace_id is not None else None

    async def emit(*, type_: EventType, data: dict[str, Any], actor: Actor) -> None:
        async with lock:
            leaf = await store.get_leaf(tid)
            event = Event(
                event_id=EventId(f"ev_{uuid4()}"),
                parent_event_id=leaf.event_id if leaf is not None else None,
                thread_id=tid,
                session_id=sid,
                type=type_,
                data=data,
                actor=actor,
                created_at=datetime.now(UTC),
                trace_id=stamped_trace,
            )
            appended = await store.append_event(tid, event)
            # Bus publish — best-effort; never raises into the tool
            # call (engine-identical semantics).
            try:
                await bus.publish(appended)
            except Exception as bus_exc:  # noqa: BLE001 — best-effort publish
                _log.warning("bridge sink: bus.publish failed: %s", bus_exc)

    return emit
