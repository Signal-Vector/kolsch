"""Regression tests — ``bug-chat-deny-stops-and-esc-interrupts.md`` Step 3.

Two TCs pin the Esc-during-streaming contract:

- **TC-CHAT-ESC-01** — feeding ``\\x1b`` to the Esc watcher's
  Input abstraction flips the shared CancelToken and stamps
  ``state.last_cancel_reason = "interrupted"``. Same downstream
  path as Ctrl-C; just a different upstream key.
- **TC-CHAT-ESC-02** — when the per-exchange ``try/finally``
  block exits, the watcher is cancelled BEFORE the next
  ``prompt_async`` runs. A subsequent Esc keystroke has no
  effect on the no-longer-active state.

The Esc-detection approach (prompt_toolkit Input via
``create_input()``, attached as a background asyncio task) was
chosen in Step 1's verification writeup and proved via the
throwaway PoC at ``/tmp/esc_poc.py``. These tests use
``create_pipe_input()`` as the production code's ``inp_factory``
seam so they drive the watcher deterministically — no real
terminal needed.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

from prompt_toolkit.input.defaults import create_pipe_input

from cli.chat import _watch_esc_during_streaming
from engine import CancelToken

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_state(cancel: CancelToken | None) -> object:
    """Minimal ``ChatSessionState`` stand-in (see test_chat_deny_stops).

    The Esc watcher reads ``active_cancel`` and writes
    ``last_cancel_reason``."""
    return SimpleNamespace(active_cancel=cancel, last_cancel_reason=None)


# ===========================================================================
# TC-CHAT-ESC-01 — Esc binding flips the cancel token
# ===========================================================================


async def test_tc_chat_esc_01_esc_during_streaming_flips_cancel_token() -> None:
    """TC-CHAT-ESC-01: An Esc keystroke delivered to the chat's input
    abstraction during a streaming exchange flips ``state.active_cancel``
    and stamps ``state.last_cancel_reason = "interrupted"``.

    Drives ``_watch_esc_during_streaming`` against a pipe input
    (the test seam Step 1's PoC validated). The watcher exits as
    soon as Esc is observed — the ``asyncio.wait_for`` bound is
    the test's max latency budget.
    """
    cancel = CancelToken()
    assert cancel.is_cancelled() is False  # pre-state
    state = _stub_state(cancel)

    with create_pipe_input() as pipe_inp:
        watcher = asyncio.create_task(
            _watch_esc_during_streaming(
                state,
                cancel,
                inp_factory=lambda: pipe_inp,
            ),
        )
        # Let the watcher's `attach()` register before we send the
        # byte — without this the byte hits the pipe before
        # there's a callback to consume it.
        await asyncio.sleep(0.05)
        pipe_inp.send_text("\x1b")  # ASCII Esc
        try:
            await asyncio.wait_for(watcher, timeout=2.0)
        except TimeoutError:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            msg = "Esc watcher did not observe \\x1b within 2s"
            raise AssertionError(msg)  # noqa: B904 — re-raising is desired

    # Cancel flipped + reason stamped — same shape Ctrl-C produces.
    # The framework's next iteration poll observes it; the engine
    # emits ``thread.exchange_failed`` with code ``exchange.cancelled``.
    assert cancel.is_cancelled() is True
    assert state.last_cancel_reason == "interrupted"


async def test_tc_chat_esc_01b_non_esc_keys_do_not_flip_cancel() -> None:
    """TC-CHAT-ESC-01b (companion): A non-Esc keystroke (e.g., the
    letter 'a') delivered to the watcher does NOT flip the cancel
    token. The watcher is intentionally narrow — only Esc is a
    cancellation key. The user can keep typing while the agent
    works; their typing is not silently consumed.

    Verified by sending 'a' then 'b' then waiting; the watcher
    keeps waiting until cancelled by the test. If the watcher
    flipped on the 'a' or 'b', the assertions below would catch
    it. Step 2 already pinned the Esc-only branch in the code
    (``if kp.key == Keys.Escape``); this test pins the negative.
    """
    cancel = CancelToken()
    state = _stub_state(cancel)

    with create_pipe_input() as pipe_inp:
        watcher = asyncio.create_task(
            _watch_esc_during_streaming(
                state,
                cancel,
                inp_factory=lambda: pipe_inp,
            ),
        )
        await asyncio.sleep(0.05)  # let attach() register
        pipe_inp.send_text("ab")  # plain letters — not Esc
        # Give the parser a moment to process. No flush_keys timer
        # waiting on these; they're complete keystrokes.
        await asyncio.sleep(0.1)

        # Cancel must still be un-flipped — the watcher only
        # responds to Esc.
        assert cancel.is_cancelled() is False
        assert state.last_cancel_reason is None

        # Tear the watcher down to match production's finally
        # branch.
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


# ===========================================================================
# TC-CHAT-ESC-02 — Listener is torn down with the exchange
# ===========================================================================


async def test_tc_chat_esc_02_listener_stops_when_exchange_ends() -> None:
    """TC-CHAT-ESC-02: When renderer.render completes (success or
    failure), the Esc listener is cancelled. A subsequent Esc has
    no effect on the (no-longer-active) state.

    Simulates the chat input-loop iteration's try/finally:
        try:
            await renderer.render(...)
        finally:
            esc_task.cancel(); await esc_task
            state.active_cancel = None
            state.last_cancel_reason = None

    Then we deliver an Esc byte AFTER the finally and assert
    nothing flips. Without proper teardown, the watcher's
    asyncio reader would still be attached to the pipe input
    AND a stray Esc could mutate state (which would race the
    next exchange).
    """
    cancel = CancelToken()
    state = _stub_state(cancel)

    with create_pipe_input() as pipe_inp:
        watcher = asyncio.create_task(
            _watch_esc_during_streaming(
                state,
                cancel,
                inp_factory=lambda: pipe_inp,
            ),
        )
        # Let the watcher attach before we proceed.
        await asyncio.sleep(0.05)

        # Simulate renderer.render exiting normally — the per-
        # exchange finally cancels the watcher and clears state.
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        active_cancel_before = state.active_cancel
        state.active_cancel = None
        state.last_cancel_reason = None

        # Watcher is done. Now deliver Esc after teardown — must
        # NOT mutate state or raise.
        pipe_inp.send_text("\x1b")
        await asyncio.sleep(0.1)

        # state.active_cancel is None — the finally cleared it.
        assert state.active_cancel is None
        # last_cancel_reason stays None — no stray callback fired.
        assert state.last_cancel_reason is None
        # And the original cancel handle is NOT cancelled by the
        # post-teardown Esc (no callback was attached when the
        # byte arrived).
        assert active_cancel_before is cancel
        assert cancel.is_cancelled() is False
