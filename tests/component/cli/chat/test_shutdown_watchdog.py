"""bug-067 — shutdown watchdog backstop.

Covers the three watchdog acceptance criteria from the bug-067 spec:

- **AC1 / Test 1** (`test_watchdog_force_exits_on_wedged_teardown`) —
  when a service `aclose()` wedges past the bounded window, the
  watchdog fires its exit hook. Uses a test-spy exit hook (installed
  via `_set_exit_hook`) so the assertion is on the hook's calls
  rather than an actual process exit.
- **AC2 / Test 2** (`test_watchdog_never_fires_on_graceful_shutdown`)
  — an idle quit runs `on_unmount` cleanly and disarms the watchdog
  before it can fire; the spy is never called.
- **AC4 / Test 3** (`test_state_integrity_across_watchdog_exit`) —
  events seeded before a wedge-triggered force-exit are still
  readable from the re-opened session JSONL and the entity DB opens
  without corruption. Mirrors AC6 / Test 6 in
  `test_shutdown_owned_executor.py` for the watchdog path.

The watchdog itself is a `threading.Timer` daemon in production; in
these tests we install a short window (500 ms) via the
`KOLSCH_SHUTDOWN_WATCHDOG_SECONDS` env var so the tests are fast
without giving up realism. The tests are guarded by their own
`asyncio.wait_for` bounds so a regression that fails to disarm
cannot hang the pytest session past the outer budget.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from bus.in_process import InProcessEventBus
from cli.chat import app as chat_app_module
from cli.chat.app import ChatApp, _set_exit_hook
from cli.chat.service_registry import ChatServiceRegistry
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId
from store import SessionStore, SqliteBackend
from store.jsonl import jsonl_path, recover_partial_final_line

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Test scaffolding — mirrors test_shutdown_owned_executor.py so the two
# suites cover the same on_mount/on_unmount surface with matching stubs.
# ---------------------------------------------------------------------------


class _MockAuthz:
    def register_prompt_callback(
        self,
        callback: Any,
        *,
        on_rule_added: Any = None,
    ) -> None:
        _ = (callback, on_rule_added)

    def clear_prompt_callback(self) -> None:
        return None


class _MockEngine:
    async def execute_stream(
        self,
        config: Any,
        ctx: Any,
    ) -> AsyncIterator[Any]:
        _ = (config, ctx)
        return
        yield  # type: ignore[unreachable]  # pragma: no cover


@dataclass
class _MockConfig:
    name: str = "TestAgent"
    agent_id: str = "ag_mock"
    model: Any = None


@dataclass
class _MockBundle:
    runtime: Any
    config: Any
    session_id: Any
    thread_id: Any
    agent_ref: str
    root: Path
    model_label: str
    is_resume: bool


async def _bootstrap_app(
    tmp_path: Path,
) -> tuple[ChatApp, InProcessEventBus, SqliteBackend, SessionStore]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_mock", name="c")
    threads = await store.list_threads(session.session_id)
    bus = InProcessEventBus()

    class _R:
        def __init__(self) -> None:
            self.store = store
            self.authz = _MockAuthz()
            self.engine = _MockEngine()
            self.root = tmp_path
            self.bus = bus

    bundle = _MockBundle(
        runtime=_R(),
        config=_MockConfig(),
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
        is_resume=False,
    )
    app = ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )
    return app, bus, backend, store


def _make_event(session_id: Any, thread_id: Any, kind: str) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.UI_EVENT,
        data={"schema_version": 1, "kind": kind, "payload": {}},
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Test fixtures — spy exit hook + short watchdog window.
# ---------------------------------------------------------------------------


class _ExitHookSpy:
    """Records exit-hook invocations without killing the process.

    `_EXIT_HOOK` defaults to `os._exit` in production; calling that
    from a test would kill pytest. The spy records the exit code
    (and a wall-clock timestamp so tests can bound firing latency).
    """

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.first_call_at: float | None = None
        self.event = threading.Event()

    def __call__(self, code: int) -> None:
        self.calls.append(code)
        if self.first_call_at is None:
            self.first_call_at = time.perf_counter()
        self.event.set()


@pytest.fixture
def exit_hook_spy() -> Iterator[_ExitHookSpy]:
    """Install a spy for the module-level exit hook; restore on teardown."""
    spy = _ExitHookSpy()
    original = chat_app_module._EXIT_HOOK
    _set_exit_hook(spy)
    try:
        yield spy
    finally:
        _set_exit_hook(original)


@pytest.fixture
def short_watchdog_window() -> Iterator[None]:
    """Shrink the watchdog window to 500 ms so tests stay fast.

    Production default is 5s (matches spec-069 AC5's ≤5s target). At
    500 ms the wedge test fires within a second; the graceful test
    still has ample slack (idle `on_unmount` completes in a few ms).
    """
    prev = os.environ.get("KOLSCH_SHUTDOWN_WATCHDOG_SECONDS")
    os.environ["KOLSCH_SHUTDOWN_WATCHDOG_SECONDS"] = "0.5"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("KOLSCH_SHUTDOWN_WATCHDOG_SECONDS", None)
        else:
            os.environ["KOLSCH_SHUTDOWN_WATCHDOG_SECONDS"] = prev


# ---------------------------------------------------------------------------
# Wedging helper — a service whose `aclose()` blocks until a release event.
# ---------------------------------------------------------------------------


class _WedgedService:
    """Service double whose `aclose()` blocks forever until released.

    Simulates the failure mode bug-067 exists to backstop: a
    third-party thread / library call inside a service's teardown
    that we cannot bound (spec-069 Part A/B fix the executor and the
    LLM read; this covers whatever else might wedge).
    """

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.aclose_called = False

    async def aclose(self) -> None:
        self.aclose_called = True
        # Never released in the wedge tests — the point is that the
        # watchdog fires the exit hook even while we're stuck here.
        await self.release.wait()


# ---------------------------------------------------------------------------
# Test 1 — watchdog fires on wedged teardown.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_force_exits_on_wedged_teardown(
    tmp_path: Path,
    exit_hook_spy: _ExitHookSpy,
    short_watchdog_window: None,
) -> None:
    """AC1 — a wedged `aclose()` triggers `_EXIT_HOOK` within the window.

    Directly invokes `on_unmount` in a background task instead of
    exiting `app.run_test()` — the wedge would hang the async-with
    exit itself, which is exactly the failure mode the watchdog is
    supposed to survive. `asyncio.wait_for` protects the test from
    hanging past the outer budget.
    """
    _ = short_watchdog_window  # arms the 500 ms window
    app, _bus, backend, _store = await _bootstrap_app(tmp_path)
    wedge = _WedgedService()
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            # Slot the wedge into the registry so `on_unmount`'s
            # aclose chain iterates it and blocks.
            app._service_registry = ChatServiceRegistry(session=wedge)

            # Kick off `on_unmount` in a task. It will block inside
            # the wedge's `aclose()` — that's the whole point. The
            # watchdog (armed at the top of `on_unmount`) fires
            # ~500 ms later and calls our spy.
            unmount_task = asyncio.create_task(app.on_unmount())

            # Wait for the spy — bounded so a broken watchdog can't
            # hang the test past this point. Budget = window (500ms)
            # + 2s slack for CI thread-scheduling jitter.
            hook_fired = await asyncio.get_running_loop().run_in_executor(
                None,
                exit_hook_spy.event.wait,
                2.5,
            )

            assert hook_fired, (
                "watchdog did not fire within 2.5s of arming "
                "(configured window = 500ms). Regression: either the "
                "watchdog was never armed, disarmed prematurely, or "
                "the exit hook was not invoked."
            )
            assert exit_hook_spy.calls == [1], (
                f"watchdog should call the exit hook exactly once with "
                f"code 1 (abnormal exit); got calls={exit_hook_spy.calls}"
            )
            assert wedge.aclose_called, (
                "wedge's aclose() must have been reached before the "
                "watchdog fired — otherwise the test isn't exercising "
                "the intended scenario"
            )

            # Release the wedge so the awaited task can finish and
            # the async-with can exit cleanly. Otherwise the outer
            # `run_test()` context would itself hang waiting for
            # `on_unmount` to return.
            wedge.release.set()
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(unmount_task, timeout=2.0)
    finally:
        # Belt-and-braces: make sure the wedge is released even if
        # setup raised before we got there.
        wedge.release.set()
        await backend.close()


# ---------------------------------------------------------------------------
# Test 2 — watchdog never fires on graceful shutdown.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_never_fires_on_graceful_shutdown(
    tmp_path: Path,
    exit_hook_spy: _ExitHookSpy,
    short_watchdog_window: None,
) -> None:
    """AC2 — idle quit completes `on_unmount` and disarms the watchdog.

    Uses the SAME 500 ms window as the wedge test — an idle
    `on_unmount` finishes in single-digit milliseconds, well inside
    the window, so the timer's daemon thread never invokes the hook.

    After teardown we hold for a full second — more than the 500 ms
    window — to prove the watchdog's timer is genuinely cancelled
    (not just "hasn't fired yet"). A regression that skipped the
    disarm would surface here as a late-firing spy call.
    """
    _ = short_watchdog_window
    app, _bus, backend, _store = await _bootstrap_app(tmp_path)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
        # `on_unmount` has run at this point. Wait past the window
        # to catch a leaked/undisarmed timer.
        await asyncio.sleep(1.0)
        assert exit_hook_spy.calls == [], (
            f"watchdog fired during a graceful shutdown "
            f"(calls={exit_hook_spy.calls}) — bug-067 AC2 regression. "
            "Either the disarm at the end of `on_unmount` was "
            "skipped, or the disarmed-flag guard in the timer "
            "callback failed."
        )
    finally:
        await backend.close()


# ---------------------------------------------------------------------------
# Test 3 — durable-state integrity across a watchdog-triggered exit.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_integrity_across_watchdog_exit(
    tmp_path: Path,
    exit_hook_spy: _ExitHookSpy,
    short_watchdog_window: None,
) -> None:
    """AC4 — session JSONL + entity DB survive a watchdog-force-exit path.

    Mirrors AC6 in `test_shutdown_owned_executor.py::
    test_state_integrity_after_wedged_worker_teardown` but for the
    watchdog path: seed events through the normal append flow, wedge
    teardown so the watchdog fires, then re-open the session and the
    entity DB from scratch and confirm both parse cleanly.

    This exercises the store contracts the spec's Technical Context
    relies on to justify `os._exit`:

    * JSONL append is `O_APPEND` + `flock` + `fsync` — durable at
      the point each append returns; `recover_partial_final_line`
      handles a torn trailing line if one is present.
    * sqlite is WAL + autocommit — every statement is durable when
      it returns; WAL recovery handles a hard exit cleanly.
    """
    _ = short_watchdog_window
    app, _bus, backend, store = await _bootstrap_app(tmp_path)
    session_id = app.session.session_id
    thread_id = app.session.thread_id
    seeded_count = 3
    wedge = _WedgedService()
    try:
        async with app.run_test() as pilot:
            await pilot.pause()

            # Seed events through the real store append path so
            # they hit JSONL + entity DB the same way production
            # does. These MUST survive the abrupt exit-hook fire.
            for i in range(seeded_count):
                await store.append_event(
                    thread_id,
                    _make_event(session_id, thread_id, f"seed.{i}"),
                )

            # Wedge teardown so the watchdog fires.
            app._service_registry = ChatServiceRegistry(session=wedge)
            unmount_task = asyncio.create_task(app.on_unmount())

            hook_fired = await asyncio.get_running_loop().run_in_executor(
                None,
                exit_hook_spy.event.wait,
                2.5,
            )
            assert hook_fired, "watchdog exit hook did not fire in time"
            assert exit_hook_spy.calls == [1]

            # Release + drain the unmount task so the async-with
            # can exit cleanly (see Test 1 comment).
            wedge.release.set()
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(unmount_task, timeout=2.0)

        # Re-open everything and verify state.
        jsonl = jsonl_path(tmp_path, session_id)
        assert jsonl.exists(), (
            "session JSONL should still exist after watchdog exit path"
        )
        recover_partial_final_line(jsonl)

        events = []
        async for ev in store.read_events(thread_id):
            events.append(ev)
        assert len(events) >= seeded_count, (
            f"expected ≥{seeded_count} events after watchdog exit path, "
            f"got {len(events)} — JSONL may be truncated / corrupted"
        )

        fresh_backend = SqliteBackend(tmp_path / ".platform" / "entities.db")
        await fresh_backend.initialize()
        try:
            session_row = await fresh_backend.get_session(session_id)
            assert session_row is not None, (
                "entity DB should still return the seeded session after "
                "the watchdog exit path"
            )
        finally:
            await fresh_backend.close()
    finally:
        wedge.release.set()
        await backend.close()
