"""spec-069 Part B — app-owned default executor teardown.

Covers Acceptance Criteria 5 + 6 of spec-069:

- **AC5** — the app installs/owns the loop's default executor such
  that no non-daemon worker can block interpreter finalization.
  Quitting while a worker is mid-blocking-call exits promptly
  (≤5s).
- **AC6** — durable state integrity across a cancelled/timed-out
  worker or abrupt teardown: session JSONL parses cleanly (the
  store's `recover_partial_final_line` on read), entity DB opens
  without corruption.

The tests do NOT try to observe `Py_Finalize → ThreadHandle_join`
from inside pytest (the process is still alive during the test).
Instead they assert on the *structural* properties that make the
`Py_Finalize` hang impossible:

1. `ChatApp.on_mount` installs a `ThreadPoolExecutor` and stores the
   handle on `self._owned_executor`.
2. `ChatApp.on_unmount` calls `shutdown(wait=False)` on that pool
   AFTER the service `aclose()` chain.
3. A wedged worker does NOT extend `on_unmount`'s wall clock beyond
   a small budget (proof that no join happens).
4. After the wait-free shutdown, the session's JSONL and the entity
   DB still open and parse cleanly (state integrity across abrupt
   teardown).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from bus.in_process import InProcessEventBus
from cli.chat.app import ChatApp
from cli.chat.service_registry import ChatServiceRegistry
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId
from store import SessionStore, SqliteBackend
from store.jsonl import jsonl_path, recover_partial_final_line

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Test scaffolding — a minimal bundle + runtime the ChatApp accepts.
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
        # ChatApp never invokes the engine in these tests — this
        # stub exists only to satisfy the bundle's typing surface.
        # The `yield` after `return` keeps this a proper async
        # generator without emitting anything.
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
    """Build a ChatApp on top of a real store/bus so `on_mount` runs end-to-end."""
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
    """Return a UI_EVENT payload safe to append to the session JSONL."""
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
# Test 4 — wedged worker: teardown completes promptly (no join hang).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wedged_worker_does_not_block_teardown(tmp_path: Path) -> None:
    """AC5 — a worker blocked on a `threading.Event` cannot wedge on_unmount.

    Submits a task to the app-owned executor that blocks on an event the
    test never sets. Verifies that:

    * The pool is installed (`_owned_executor is not None`).
    * `on_unmount` returns within a small budget (5s per AC5 — in
      practice sub-second because `shutdown(wait=False)` does not join).
    * After teardown the pool is marked shut down and the handle on the
      app is cleared.

    Fail mode this catches: any regression to `shutdown(wait=True)` (or
    missing shutdown) would make this test wall-clock hang — which is
    exactly the `Py_Finalize → ThreadHandle_join` shape from spec-069.
    """
    app, _bus, backend, _store = await _bootstrap_app(tmp_path)
    release = threading.Event()
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            pool = app._owned_executor
            assert pool is not None, (
                "on_mount must install an app-owned default executor "
                "(spec-069 Part B). Without it, nothing prevents a "
                "non-daemon worker from blocking Py_Finalize."
            )

            # Submit a task that blocks forever until we release it.
            # We DELIBERATELY never release before teardown — the
            # point is that `shutdown(wait=False)` returns anyway.
            fut = pool.submit(release.wait)  # noqa: F841 — keep alive

        # Exiting `run_test` triggers `on_unmount`. Time it — if
        # `shutdown(wait=True)` slipped in, this would block until we
        # set `release` (i.e. forever in a regression).
        # `run_test` already returned so we just observe post-state.
        assert pool._shutdown, (
            "on_unmount must call `shutdown(...)` on the owned executor"
        )
        assert app._owned_executor is None, (
            "on_unmount should null out the owned-executor handle so a "
            "re-mount of the same App instance installs a fresh pool"
        )
    finally:
        # Release the wedged worker so it exits instead of dangling
        # past the test (belt-and-braces — the daemon-nature of
        # pytest's own thread management usually handles this, but
        # being explicit avoids interpreter shutdown flakes across
        # the wider test session).
        release.set()
        await backend.close()


@pytest.mark.asyncio
async def test_teardown_wall_clock_bounded_with_wedged_worker(
    tmp_path: Path,
) -> None:
    """AC5 (timing) — `on_unmount` returns in <5s even with a wedged worker.

    Measures the wall-clock time from `run_test`'s exit (which triggers
    `on_unmount`) back to test-thread resume. Guards against a future
    regression that swaps `wait=False` for `wait=True` (which would
    make this hang forever, not merely take a couple seconds).
    """
    app, _bus, backend, _store = await _bootstrap_app(tmp_path)
    release = threading.Event()
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            pool = app._owned_executor
            assert pool is not None
            pool.submit(release.wait)
            teardown_start = time.perf_counter()
        elapsed = time.perf_counter() - teardown_start
        # Generous — the spec allows ≤5s. In practice this is a few
        # milliseconds because `shutdown(wait=False)` does not join.
        assert elapsed < 5.0, (
            f"on_unmount took {elapsed:.3f}s with a wedged worker — "
            "spec-069 Part B requires teardown to remain bounded "
            "(shutdown(wait=False), NOT wait=True)"
        )
    finally:
        release.set()
        await backend.close()


# ---------------------------------------------------------------------------
# Test 5 — graceful shutdown: no wedged worker, aclose() chain runs.
# ---------------------------------------------------------------------------


class _StubService:
    """Service double whose `aclose()` records that it ran.

    Slotted into the `ChatServiceRegistry` before mount so
    `on_unmount`'s aclose loop actually iterates something. Verifies
    that the Part-B executor-shutdown addition did NOT regress the
    pre-existing best-effort aclose chain.
    """

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        # Do a tiny bit of `to_thread` work to prove that services
        # scheduling default-executor tasks during teardown still
        # complete BEFORE the executor is shut down. Reads a small
        # amount from a synchronous callable.
        await asyncio.to_thread(lambda: None)
        self.closed = True


@pytest.mark.asyncio
async def test_graceful_shutdown_runs_aclose_and_shuts_executor(
    tmp_path: Path,
) -> None:
    """AC5 (graceful) — idle quit runs `aclose()` chain then shuts pool.

    Verifies (a) the executor is installed at mount, (b) the aclose
    chain still runs, (c) the executor is shut down AFTER `aclose()`
    so services can schedule `to_thread` work during their own
    teardown.
    """
    app, _bus, backend, _store = await _bootstrap_app(tmp_path)
    stub = _StubService()
    # Pre-populate the registry so `on_mount`'s "existing" branch
    # keeps this slot. `session` slot is unused by production wiring
    # — safe to hang the stub off it without colliding.
    app._service_registry = ChatServiceRegistry(session=stub)
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._owned_executor is not None, (
                "on_mount must install the owned executor even on the "
                "graceful path"
            )
        # After `run_test` exits, `on_unmount` has completed.
        assert stub.closed, (
            "on_unmount's existing aclose() chain must still run after "
            "the Part-B executor-shutdown addition"
        )
        assert app._owned_executor is None, (
            "on_unmount must null out the executor handle on the graceful "
            "path too"
        )
    finally:
        await backend.close()


# ---------------------------------------------------------------------------
# Test 6 — state integrity after abrupt teardown.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_integrity_after_wedged_worker_teardown(
    tmp_path: Path,
) -> None:
    """AC6 — session JSONL + entity DB survive a wedged-worker teardown.

    Seeds a session with a handful of events (each `append_event` runs
    on the default executor — i.e. our owned pool), wedges a worker,
    triggers teardown, then re-opens the session and asserts that:

    * The JSONL parses cleanly (relies on `recover_partial_final_line`).
    * The entity DB opens without corruption and enumerates the seeded
      session.

    This exercises the store contract cited in the spec's Technical
    Context: JSONL append is `O_APPEND` + `flock` + `fsync`; sqlite is
    WAL autocommit — both are already crash-safe against an abrupt
    teardown of the worker holding the write.
    """
    app, _bus, backend, store = await _bootstrap_app(tmp_path)
    session_id = app.session.session_id
    thread_id = app.session.thread_id
    release = threading.Event()
    seeded_count = 3
    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            pool = app._owned_executor
            assert pool is not None

            # Seed a few events — these go through the app-owned
            # executor via `asyncio.to_thread(append_event_sync, ...)`.
            for i in range(seeded_count):
                await store.append_event(
                    thread_id, _make_event(session_id, thread_id, f"seed.{i}"),
                )

            # Wedge a worker on the owned pool.
            pool.submit(release.wait)

        # Teardown ran; the app swapped in a fresh default executor
        # so post-teardown `asyncio.to_thread` calls still work
        # (backend init/close, store reads). Now re-open everything
        # and assert state.
        jsonl = jsonl_path(tmp_path, session_id)
        assert jsonl.exists(), "session JSONL should still exist after teardown"

        # Recover any partial trailing line — an idempotent no-op on
        # a well-formed file, but proves the store's crash-safety
        # helper survives whatever the teardown left behind.
        recover_partial_final_line(jsonl)

        # Read all events back through the store's normal read path
        # (which itself runs `recover_partial_final_line` and parses
        # every line as JSON). A corrupt trailing write would raise.
        events = []
        async for ev in store.read_events(thread_id):
            events.append(ev)
        # `create_session` inserts an initial `session.created` event
        # via the same append path; on top of that we appended
        # `seeded_count` events. Expect all of them back.
        assert len(events) >= seeded_count, (
            f"expected ≥{seeded_count} events after teardown, "
            f"got {len(events)} — JSONL may be truncated / corrupted"
        )

        # Entity DB opens cleanly on a fresh backend handle. If a
        # torn worker had left the sqlite file in a bad state, this
        # would raise on connect / on the first read.
        fresh_backend = SqliteBackend(tmp_path / ".platform" / "entities.db")
        await fresh_backend.initialize()
        try:
            session_row = await fresh_backend.get_session(session_id)
            assert session_row is not None, (
                "entity DB should still return the seeded session after "
                "an abrupt teardown"
            )
        finally:
            await fresh_backend.close()
    finally:
        release.set()
        await backend.close()


# ---------------------------------------------------------------------------
# Test 7 — SUBPROCESS: interpreter actually exits promptly with wedged worker.
# ---------------------------------------------------------------------------
#
# The other four tests assert structural properties (pool installed,
# shutdown called, wall clock bounded across `on_unmount`). They do NOT
# and CANNOT observe `Py_Finalize → wait_for_thread_shutdown →
# ThreadHandle_join` from inside pytest — the test process is still
# alive during the test, so the atexit hook has not yet fired.
#
# Empirical finding (Reviewer, 2026-07-14): the daemon-flag defense
# alone leaves an 8+ second hang at interpreter shutdown because
# `concurrent.futures.thread._python_exit` (registered via
# `threading._register_atexit`) iterates every executor's threads in
# `_threads_queues` and joins them UNCONDITIONALLY — the daemon flag
# is checked only later by Py_Finalize, which never runs while
# `_python_exit` is blocked in `t.join()`.
#
# This test launches a subprocess that mirrors the production
# `ChatApp.on_unmount` flow (swap fresh default executor, then
# shutdown our owned pool with `wait=False`) with a wedged worker
# and asserts the subprocess exits inside a strict budget.
#
# Fail mode this catches: reverting the `_threads_queues.pop(t, None)`
# defense in `_DaemonThreadPoolExecutor.shutdown` makes this test
# time out at 15s (empirically verified during the Part-B rework).


def test_wedged_worker_does_not_hang_interpreter_exit() -> None:
    """spec-069 AC5 — interpreter exits promptly with a wedged worker.

    The load-bearing property: a wedged worker on the owned executor
    must NOT block interpreter finalization. Asserts on actual
    subprocess exit time under a strict budget.

    Mirrors the production `ChatApp.on_unmount` flow so the test
    catches regressions to either defense (daemon flag OR
    ``_threads_queues`` cleanup at shutdown) — both are required.
    """
    script = textwrap.dedent(
        """
        import asyncio
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from cli.chat.app import _DaemonThreadPoolExecutor


        async def main() -> None:
            loop = asyncio.get_running_loop()
            pool = _DaemonThreadPoolExecutor(max_workers=2)
            loop.set_default_executor(pool)
            event = threading.Event()  # never set
            loop.run_in_executor(pool, event.wait)
            await asyncio.sleep(0.1)  # ensure the worker actually started

            # Mirror ChatApp.on_unmount: install a fresh default
            # executor BEFORE shutting ours down, so asyncio's own
            # ``shutdown_default_executor`` (called at loop close)
            # does not try to join our wedged worker.
            loop.set_default_executor(
                ThreadPoolExecutor(thread_name_prefix="post-unmount-"),
            )
            pool.shutdown(wait=False, cancel_futures=True)


        asyncio.run(main())
        """
    )

    # 5s is the spec-069 AC5 budget for teardown wall-clock. In
    # practice the subprocess exits in ~1.5s with the fix in place;
    # without it, exit blocks past `timeout` (empirically 15s+ hang).
    timeout_s = 5.0
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            timeout=timeout_s,
            capture_output=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - t0
        pytest.fail(
            f"Interpreter did not exit within {timeout_s:.1f}s "
            f"(elapsed {elapsed:.2f}s) — spec-069 AC5 regression. "
            f"Likely cause: `_DaemonThreadPoolExecutor.shutdown` no "
            f"longer detaches workers from "
            f"`concurrent.futures.thread._threads_queues`, so "
            f"`_python_exit` blocks joining the wedged worker.\n"
            f"stdout: {exc.stdout!r}\nstderr: {exc.stderr!r}",
        )
    elapsed = time.monotonic() - t0
    assert result.returncode == 0, (
        f"Subprocess failed (rc={result.returncode}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # Guard against a slow-but-not-timeout regression too. The AC5
    # budget is 5s; healthy exit is well under 3s locally.
    assert elapsed < timeout_s, (
        f"Interpreter took {elapsed:.2f}s to exit "
        f"(spec-069 AC5 target: <{timeout_s:.0f}s)"
    )
