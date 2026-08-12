"""spec-131 Step 9 — `PromptsImpl._append_ui_event` hook-observer wiring.

Focused unit coverage (not the full chat-driving integration test —
see `tests/cli/chat/test_approval_hooks.py` for that) confirming:

1. `_append_ui_event` calls `runtime.hook_dispatcher.observe()` exactly
   once per emitted `WIRE_PROMPT_REQUESTED`/`WIRE_PROMPT_ANSWERED`
   event, with the SAME `Event` object that was appended to the store.
   Exercised directly via `_append_ui_event` (rather than only through
   `choice()`) because production `WIRE_PROMPT_ANSWERED` is normally
   appended by `EventAdapter._bridge_widget_resolution` (a widget-layer
   concern outside `PromptsImpl`), not by `resolve_pending` — the one
   `PromptsImpl`-internal path that DOES append `WIRE_PROMPT_ANSWERED`
   itself is `_emit_auto_deny` (bug-114's workflow-timeout auto-deny),
   which requires extra scaffolding unrelated to this step. Driving
   `_append_ui_event` directly for both kinds tests the exact unit
   spec-131 Step 9 changed, without coupling to either of those.
2. `runtime=None` (the default, matching every headless/adapter-less
   test fixture that constructs `PromptsImpl` directly) is a genuine,
   silent no-op — no exception, no observer call, zero behavior change
   from before Step 9 existed.
3. `runtime` wired but `runtime.hook_dispatcher is None` is likewise a
   silent no-op (a `PlatformRuntime` need not always carry a
   dispatcher, even though `bootstrap_runtime` itself always sets one).
4. A raising / hanging observer does not propagate into or block
   `_append_ui_event` — mirrors `engine.engine.execute_stream`'s own
   observer call-site posture (Step 5): timeout-bounded, exceptions
   swallowed and logged.
5. An event kind other than the two documented UI-event kinds (e.g. a
   hypothetical future `_append_ui_event` caller) does NOT reach the
   observer — only `WIRE_PROMPT_REQUESTED`/`WIRE_PROMPT_ANSWERED` do.
6. The full `choice()` round trip (via `resolve_pending`, the real
   adapter-simulated path every other `PromptsImpl` test uses) still
   observes the `.requested` half end-to-end, confirming the wiring
   works through the public API too, not only via the direct
   `_append_ui_event` call.
7. **Ordering regression (post-Step-9 review fix):** `bus.publish()` —
   the call that drives `EventAdapter.handle` to mount the live
   `ApprovalBlock` widget, i.e. the user-visible "a prompt is
   happening" signal — must fire BEFORE the hook-observe call, not
   after. A review pass caught the original Step 9 ordering gating
   the widget-mount signal behind a potentially-slow hook observer
   (measured: `bus.publish()` firing at 3.002s against a hanging
   observer double, instead of near-instantly). This test reproduces
   that exact stress-test shape — a hanging-observer double driven
   through a real `PromptsImpl.choice()` call — and asserts
   `bus.publish` is observed well before the hook-observe timeout
   could possibly have elapsed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from cli.chat.services.prompts_impl import (
    _HOOK_OBSERVE_TIMEOUT_SECONDS,
    PromptsImpl,
)
from schemas.events import EventType
from schemas.wire_kinds import WIRE_PROMPT_ANSWERED, WIRE_PROMPT_REQUESTED
from store import SessionStore, SqliteBackend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from schemas.events import Event
    from tools.services.prompts import PromptSource


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


async def _mk_impl(store: SessionStore, **kwargs: Any) -> PromptsImpl:
    session = await store.create_session("ag_test", name="p")
    threads = await store.list_threads(session.session_id)
    return PromptsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        **kwargs,
    )


async def _ui_events(store: SessionStore, thread_id: Any) -> list[Event]:
    """Every `UI_EVENT` on `thread_id`, excluding `store.create_session`'s
    own `SESSION_OPENED` event (which `_mk_impl`'s session always emits
    and which is irrelevant to these tests)."""
    return [
        ev async for ev in store.read_events(thread_id) if ev.type is EventType.UI_EVENT
    ]


class _SpyDispatcher:
    """Records every `observe()` call — the events it received, in order."""

    def __init__(self) -> None:
        self.observed: list[Event] = []

    async def observe(self, event: Event) -> None:
        self.observed.append(event)


class _RaisingDispatcher:
    """`observe()` always raises — must not propagate."""

    async def observe(self, event: Event) -> None:
        del event
        msg = "boom: hook dispatcher misbehaving on purpose"
        raise RuntimeError(msg)


class _HangingDispatcher:
    """`observe()` never returns — must be bounded by the timeout."""

    async def observe(self, event: Event) -> None:
        del event
        await asyncio.sleep(3600)


class _TimestampingBus:
    """Records the monotonic time of every `publish()` call.

    Used by the ordering-regression test below to confirm
    `bus.publish` fires promptly — well before a hanging hook
    observer's timeout could possibly have elapsed — rather than being
    gated behind it."""

    def __init__(self) -> None:
        self.publish_times: list[float] = []

    async def publish(self, event: Event) -> None:
        del event
        self.publish_times.append(asyncio.get_running_loop().time())


@dataclass
class _FakeRuntime:
    """Minimal stand-in for `PlatformRuntime` — only `hook_dispatcher`
    matters to `PromptsImpl._observe_hook_event`, so this avoids
    constructing a real `PlatformRuntime` (a much heavier composition
    root) for a unit test scoped to this one collaboration."""

    hook_dispatcher: Any = field(default=None)


# ---------------------------------------------------------------------------
# 1. Observer called exactly once per emitted event, correct object.
# ---------------------------------------------------------------------------


async def test_observer_called_once_per_appended_event(store: SessionStore) -> None:
    spy = _SpyDispatcher()
    impl = await _mk_impl(store, runtime=_FakeRuntime(hook_dispatcher=spy))

    await impl._append_ui_event(WIRE_PROMPT_REQUESTED, {"prompt_id": "prm_1"})

    assert len(spy.observed) == 1
    assert spy.observed[0].data["kind"] == WIRE_PROMPT_REQUESTED
    assert spy.observed[0].data["payload"] == {"prompt_id": "prm_1"}

    await impl._append_ui_event(
        WIRE_PROMPT_ANSWERED, {"prompt_id": "prm_1", "response": "y"}
    )

    # One more call, not a re-delivery of the first — two total.
    assert len(spy.observed) == 2
    assert spy.observed[1].data["kind"] == WIRE_PROMPT_ANSWERED
    assert spy.observed[1].data["payload"] == {"prompt_id": "prm_1", "response": "y"}


async def test_observer_receives_the_same_event_appended_to_the_store(
    store: SessionStore,
) -> None:
    spy = _SpyDispatcher()
    impl = await _mk_impl(store, runtime=_FakeRuntime(hook_dispatcher=spy))

    await impl._append_ui_event(WIRE_PROMPT_REQUESTED, {"prompt_id": "prm_2"})

    stored_events = await _ui_events(store, impl._thread_id)
    assert len(stored_events) == 1
    observed = spy.observed[0]
    stored = stored_events[0]
    # `store.read_events` necessarily deserializes a fresh `Event`
    # instance (the store assigns `logical_seq` on its own internal
    # copy, not on the caller's object), so identity/full-data equality
    # can't survive a store round-trip — `event_id` equality plus the
    # caller-supplied `kind`/`payload` matching on both sides is the
    # right proxy for "the observer was handed the very event that was
    # durably appended," which is the property this test cares about.
    assert observed.event_id == stored.event_id
    assert observed.data["kind"] == stored.data["kind"] == WIRE_PROMPT_REQUESTED
    assert observed.data["payload"] == stored.data["payload"] == {"prompt_id": "prm_2"}


async def test_observer_not_called_for_other_ui_event_kinds(
    store: SessionStore,
) -> None:
    """Only the two documented kinds reach the observer — an
    unrelated `_append_ui_event` kind (e.g. `WIRE_PROMPT_CANCELLED`,
    used by `cancel_pending_prompt`) must NOT fire it. spec-131 Step 9
    scoped the fix to `kolsch.prompt.requested`/`.answered` only."""
    spy = _SpyDispatcher()
    impl = await _mk_impl(store, runtime=_FakeRuntime(hook_dispatcher=spy))

    await impl._append_ui_event("kolsch.prompt.cancelled", {"prompt_id": "prm_3"})

    assert spy.observed == []


# ---------------------------------------------------------------------------
# 2. runtime=None (default) is a genuine, silent no-op.
# ---------------------------------------------------------------------------


async def test_runtime_none_is_silent_noop(store: SessionStore) -> None:
    """Default `runtime=None` — matches every pre-Step-9 construction
    site (headless tests, adapter-less fixtures). Must behave exactly
    as it did before this step: no exception, no attribute access
    that could fail, the store append still happens normally."""
    impl = await _mk_impl(store)  # runtime defaults to None
    assert impl._runtime is None

    await impl._append_ui_event(WIRE_PROMPT_REQUESTED, {"prompt_id": "prm_4"})

    stored_events = await _ui_events(store, impl._thread_id)
    assert len(stored_events) == 1
    assert stored_events[0].data["kind"] == WIRE_PROMPT_REQUESTED


async def test_runtime_none_full_choice_round_trip_unaffected(
    store: SessionStore,
) -> None:
    """End-to-end confirmation through the public API: with no
    runtime wired, `choice()` → `resolve_pending()` behaves exactly as
    it did before Step 9 — no new failure mode introduced."""
    impl = await _mk_impl(store)
    source: PromptSource = {"kind": "tool", "id": "tc_1"}

    task = asyncio.create_task(impl.choice("Q?", ["y", "n"], source=source))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if impl.pending_prompt_id is not None:
            break
    else:
        msg = "no pending prompt observed within the polling budget"
        raise AssertionError(msg)
    impl.resolve_pending(str(impl.pending_prompt_id), "y")
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result == "y"


# ---------------------------------------------------------------------------
# 3. runtime wired but hook_dispatcher=None is also a silent no-op.
# ---------------------------------------------------------------------------


async def test_runtime_without_hook_dispatcher_is_silent_noop(
    store: SessionStore,
) -> None:
    impl = await _mk_impl(store, runtime=_FakeRuntime(hook_dispatcher=None))

    await impl._append_ui_event(WIRE_PROMPT_REQUESTED, {"prompt_id": "prm_5"})

    stored_events = await _ui_events(store, impl._thread_id)
    assert len(stored_events) == 1
    assert stored_events[0].data["kind"] == WIRE_PROMPT_REQUESTED


# ---------------------------------------------------------------------------
# 4. A misbehaving observer (raises / hangs) never breaks the caller.
# ---------------------------------------------------------------------------


async def test_raising_observer_does_not_propagate(store: SessionStore) -> None:
    impl = await _mk_impl(
        store, runtime=_FakeRuntime(hook_dispatcher=_RaisingDispatcher())
    )

    # Must not raise despite the dispatcher's observe() always raising.
    await impl._append_ui_event(WIRE_PROMPT_REQUESTED, {"prompt_id": "prm_6"})

    stored_events = await _ui_events(store, impl._thread_id)
    assert len(stored_events) == 1


async def test_hanging_observer_is_bounded_by_timeout(store: SessionStore) -> None:
    impl = await _mk_impl(
        store, runtime=_FakeRuntime(hook_dispatcher=_HangingDispatcher())
    )

    # The hanging observer must not stall `_append_ui_event` beyond
    # roughly `_HOOK_OBSERVE_TIMEOUT_SECONDS` — bound the whole call
    # well above that so a real timeout still passes comfortably, but
    # well below "the hang went unbounded."
    await asyncio.wait_for(
        impl._append_ui_event(WIRE_PROMPT_REQUESTED, {"prompt_id": "prm_7"}),
        timeout=_HOOK_OBSERVE_TIMEOUT_SECONDS * 2 + 5.0,
    )

    stored_events = await _ui_events(store, impl._thread_id)
    assert len(stored_events) == 1


# ---------------------------------------------------------------------------
# 6. Full `choice()` round trip observes the `.requested` half.
# ---------------------------------------------------------------------------


async def test_choice_round_trip_observes_requested(store: SessionStore) -> None:
    spy = _SpyDispatcher()
    impl = await _mk_impl(store, runtime=_FakeRuntime(hook_dispatcher=spy))
    source: PromptSource = {"kind": "tool", "id": "tc_2"}

    task = asyncio.create_task(impl.choice("Q?", ["y", "n"], source=source))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if impl.pending_prompt_id is not None:
            break
    else:
        msg = "no pending prompt observed within the polling budget"
        raise AssertionError(msg)
    impl.resolve_pending(str(impl.pending_prompt_id), "y")
    await asyncio.wait_for(task, timeout=1.0)

    assert len(spy.observed) == 1
    assert spy.observed[0].data["kind"] == WIRE_PROMPT_REQUESTED
    assert spy.observed[0].data["payload"]["prompt"] == "Q?"


# ---------------------------------------------------------------------------
# 7. Ordering regression: bus.publish must not be gated behind a slow
#    hook observer (the widget-mount signal must fire promptly).
# ---------------------------------------------------------------------------


async def test_bus_publish_not_delayed_by_hanging_hook_observer(
    store: SessionStore,
) -> None:
    """Reviewer B's exact stress-test shape: a hanging-observer double,
    driven through a real `PromptsImpl.choice()` call, with a spy bus
    recording publish timestamps.

    Before the ordering fix, `bus.publish()` — which drives
    `EventAdapter.handle`'s `ApprovalBlock` mount, the user-visible
    "a prompt is happening" signal — was awaited AFTER the hook-observe
    call, so a hanging observer delayed it by the full
    `_HOOK_OBSERVE_TIMEOUT_SECONDS` (measured: 3.002s). After the fix,
    `bus.publish` must fire promptly — well before the hook-observe
    timeout could possibly have elapsed — regardless of how slow or
    hung the configured hook observer is.
    """
    bus = _TimestampingBus()
    impl = await _mk_impl(
        store,
        bus=bus,  # structural EventBus double (Protocol, not nominal)
        runtime=_FakeRuntime(hook_dispatcher=_HangingDispatcher()),
    )
    source: PromptSource = {"kind": "tool", "id": "tc_3"}

    loop = asyncio.get_running_loop()
    start = loop.time()
    task = asyncio.create_task(impl.choice("Q?", ["y", "n"], source=source))

    # Poll for the bus publish to land — must happen almost
    # immediately, nowhere near the hook-observer's timeout.
    deadline = start + 1.0
    while loop.time() < deadline:
        if bus.publish_times:
            break
        await asyncio.sleep(0.01)
    else:
        msg = "bus.publish was never observed within 1.0s"
        raise AssertionError(msg)

    publish_elapsed = bus.publish_times[0] - start
    assert publish_elapsed < 1.0, (
        f"bus.publish() took {publish_elapsed:.3f}s — expected near-"
        "instant delivery, decoupled from the hanging hook observer's "
        f"{_HOOK_OBSERVE_TIMEOUT_SECONDS}s timeout"
    )

    # Clean up: resolve the prompt so the task doesn't leak. The
    # `choice()` call itself is still in-flight awaiting the hanging
    # observer's timeout internally (bounded by
    # `_HOOK_OBSERVE_TIMEOUT_SECONDS`) — that's expected and asserted
    # elsewhere (`test_hanging_observer_is_bounded_by_timeout`); this
    # test only cares about the publish ordering, so give the pending
    # prompt a moment to appear, then resolve and let the timeout run
    # its course in the background wait below.
    for _ in range(200):
        await asyncio.sleep(0.01)
        if impl.pending_prompt_id is not None:
            break
    else:
        msg = "no pending prompt observed within the polling budget"
        raise AssertionError(msg)
    impl.resolve_pending(str(impl.pending_prompt_id), "y")
    await asyncio.wait_for(task, timeout=_HOOK_OBSERVE_TIMEOUT_SECONDS + 5.0)
