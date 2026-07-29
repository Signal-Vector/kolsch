"""bug-106 — the wedged sub-workflow-node-timeout scenario, replayed.

Session ``ses_de1d4e6c-fac3-44fd-950f-cac1de829ed2`` wedged when two
sub-workflow nodes hit their per-node ``NodeTimeoutExceeded`` (360s)
while awaiting inline approvals. The ``asyncio.wait_for`` timeout
cascaded a cancel through the exchange, the drain in
``_Exchange.close_all_open_spans`` re-raised on the first
``await self._emit(...)``, and every subsequent span's terminal
was dropped: two approvals never got
``WIRE_PROMPT_CANCELLED``, the composer stayed frozen, and two
workflow cards never finalized.

This test replays the shape at the exact layer the bug lives in —
a real ``PromptsImpl`` inside a real engine exchange, driven by an
``asyncio.wait_for(..., timeout=0.1)``. It's the spec's Test 5
fallback path: the full sub-workflow + agent-runner + node-timeout
harness would take much longer to assemble than a component-level
test justifies when the fix is in the drain method — the drain is
the same code whether the ambient cancel comes from
``asyncio.wait_for`` on a sub-workflow node or any other cascading
cancel path. Before bug-106's fix this test hangs / fails; after
the fix the terminal ``WIRE_PROMPT_CANCELLED(reason="session_
interrupted")`` lands on the JSONL as spec-068 Part B promised.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from bus.in_process import InProcessEventBus
from cli.chat.services.prompts_impl import PromptsImpl
from engine.engine import _current_exchange_span_registry, _Exchange
from schemas import EventType
from schemas.wire_kinds import (
    WIRE_PROMPT_CANCELLED,
    WIRE_PROMPT_REQUESTED,
)
from store import SqliteBackend
from store.session_store import SessionStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures — real session store + bus + PromptsImpl.
# ---------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    try:
        yield SessionStore(root=tmp_path, backend=backend)
    finally:
        await backend.close()


def _minimal_exchange(
    *,
    store: SessionStore,
    bus: InProcessEventBus,
    thread_id: str,
    session_id: str,
) -> _Exchange:
    """Build the smallest ``_Exchange`` whose ``_emit`` + drain work.

    ``close_all_open_spans`` calls ``self._emit``, which touches
    ``self._lock``, ``self.engine._store``, ``self.engine._bus``,
    ``self.session_id``, ``self.context.thread_id``,
    ``self.context.trace_id``. Every other exchange field is
    irrelevant to this test.

    We skip ``_Exchange.__init__`` and only set the fields the drain
    path touches — cheaper than standing up the full ``AgentEngine``
    with all its provider deps.
    """

    from types import SimpleNamespace

    fake_context = SimpleNamespace(thread_id=thread_id, trace_id=None)
    fake_engine = SimpleNamespace(_store=store, _bus=bus)

    ex = _Exchange.__new__(_Exchange)
    ex.engine = fake_engine  # type: ignore[assignment]
    ex.context = fake_context  # type: ignore[assignment]
    ex.session_id = session_id
    ex._lock = asyncio.Lock()
    ex._open_spans = OrderedDict()
    # No queue is needed by the drain — but ``_emit`` calls
    # ``queue.put_nowait``.
    ex.queue = asyncio.Queue()
    return ex


# ---------------------------------------------------------------------------
# The regression — the wedged shape resolves under the fix.
# ---------------------------------------------------------------------------


async def test_prompt_cancelled_emits_on_wait_for_timeout(
    store: SessionStore,
) -> None:
    """A ``PromptsImpl.choice`` awaited inside an exchange task,
    wrapped by ``asyncio.wait_for(..., timeout=0.1)``, produces
    exactly one ``WIRE_PROMPT_CANCELLED(reason="session_interrupted")``
    on the thread's JSONL after the timeout fires.

    Simulates the wedged session's shape: the sub-workflow's node
    timeout hard-cancels the runner while it's awaiting an inline
    approval; the engine's exchange finally is expected to drain the
    registered ``WIRE_PROMPT_REQUESTED`` span and emit the paired
    ``WIRE_PROMPT_CANCELLED`` terminal. Before bug-106's fix the
    drain aborted mid-loop and the terminal never landed.
    """
    session = await store.create_session("ag_test", name="bug_106")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id

    bus = InProcessEventBus()
    ex = _minimal_exchange(
        store=store,
        bus=bus,
        thread_id=str(thread_id),
        session_id=str(session.session_id),
    )
    prompts = PromptsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
        bus=bus,
    )

    async def _runner() -> None:
        # Publish the exchange's ``(register_span, close_span)``
        # bound pair on the contextvar so ``PromptsImpl._append_ui_
        # event`` (which reads ``current_span_registry()``) plugs the
        # prompt into the exchange's registry. Same shape as
        # ``_Exchange.run`` at ``engine.py:1347``.
        token = _current_exchange_span_registry.set(
            (ex.register_span, ex.close_span),
        )
        try:
            try:
                # This future never resolves — the widget-side
                # ``resolve_pending`` / ``cancel_pending`` would
                # normally run from the adapter's task, but we're
                # simulating a wedge where the timeout hits BEFORE
                # any response lands.
                await prompts.choice(
                    "Approve write?",
                    ["y", "n"],
                    source={
                        "id": "tst",
                        "kind": "tool",
                        "tool_call_id": f"tc_{uuid4()}",
                    },
                )
            finally:
                await ex.close_all_open_spans()
        finally:
            _current_exchange_span_registry.reset(token)

    with pytest.raises((asyncio.TimeoutError, asyncio.CancelledError)):
        await asyncio.wait_for(_runner(), timeout=0.1)

    # Give any in-flight shielded emits a chance to complete their
    # store append. ``asyncio.shield`` in the drain keeps the emit
    # coroutine alive across the ambient cancel, but scheduling is
    # cooperative — one yield is enough for the shielded task to
    # observe completion.
    await asyncio.sleep(0.05)

    events = [e async for e in store.read_path_to_leaf(thread_id)]
    ui_events = [e for e in events if e.type == EventType.UI_EVENT]

    # Exactly one WIRE_PROMPT_REQUESTED (from PromptsImpl) + exactly
    # one WIRE_PROMPT_CANCELLED (from the drain) for the same
    # prompt_id. The wedge without the fix produces the request
    # with no matching cancel.
    kinds = [e.data.get("kind") for e in ui_events]
    requested = [e for e in ui_events if e.data.get("kind") == WIRE_PROMPT_REQUESTED]
    cancelled = [e for e in ui_events if e.data.get("kind") == WIRE_PROMPT_CANCELLED]
    assert len(requested) == 1, f"expected one .requested; got kinds={kinds!r}"
    assert len(cancelled) == 1, (
        f"expected one .cancelled from the drain; got kinds={kinds!r}. "
        "before bug-106's fix the drain aborted mid-loop and no "
        "cancel ever landed on the JSONL — this is the wedge shape."
    )

    # The terminal payload is the shape ``PromptsImpl``'s registered
    # factory declared (see ``prompts_impl.py:505-519``).
    payload = cancelled[0].data.get("payload") or {}
    assert payload.get("reason") == "session_interrupted"
    assert payload.get("prompt_id") == requested[0].data["payload"]["prompt_id"]
