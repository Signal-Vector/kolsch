"""Tests for `platform_runtime.hooks` — spec-131 Steps 2, 3, and 4.

`tests/platform_runtime/` is a new test directory (did not exist before
Step 2) mirroring the new `src/platform_runtime/hooks.py` module.

Matcher coverage (`-k matcher`), per spec-131's Step 2 spec: one case
per documented event type (the six-row Matching table in the spec's
Scope section), a `"*"` wildcard case, and non-matching cases (wrong
event type, wrong subject). Every case runs `match(rule, event)`
directly — no subprocess, no dispatcher, no engine involved.

Fabricated `Event`/`*Data` objects use the real schemas
(`schemas.events.Event`, `schemas.entities.Actor`,
`schemas.identifiers.*`) — not an invented fake shape.

Runner coverage (`-k runner`), per spec-131's Step 3 spec: real
subprocess execution (not mocked — that's the point of these tests)
against `run_hook_command`, covering success, timeout-kills-the-process,
and command-not-found, verifying the "never raises" contract and the
SIGINT-grace-then-SIGKILL escalation actually terminates the process
(not just that the awaiting call returns). No matcher, dispatcher, or
engine code is involved here either — `run_hook_command` is called
directly with a literal `command`/`timeout_seconds`.

Dispatcher coverage (`-k dispatcher`), per spec-131's Step 4 spec:
constructs a real `HookDispatcher` with 2-3 `HookRule`s and feeds it
synthetic events, asserting the expected subprocess launches happen
for matches and not for non-matches; a dedicated non-blocking section
proves `.handle()` returns promptly even when the fired command is
deliberately slow (bounded via `asyncio.wait_for` on `.handle()`
itself); a one-time-warning section covers the `kolsch.prompt.answered`
non-wildcard-matcher construction-time warning. Most dispatcher tests
patch `platform_runtime.hooks.run_hook_command` at the module level
(Step 3's tests already cover real subprocess behavior) — the two
`test_smoke_dispatcher_*` tests at the bottom are the exception: they
run a real, non-mocked subprocess end to end to prove the wiring
actually works, not just the matching logic in isolation. No engine
code is involved anywhere in this file — `HookDispatcher` is fed
fabricated events directly, exactly as spec-131 Step 4 requires (engine
wiring is Steps 5/6).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from platform_runtime.hooks import (
    _BACKGROUND_TASKS,
    _POST_KILL_REAP_TIMEOUT_SECONDS,
    HookDispatcher,
    _reap_after_kill,
    drain_background_hook_tasks,
    match,
    run_hook_command,
)
from schemas.config import HookRule
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId

if TYPE_CHECKING:
    from pathlib import Path

_SESSION_ID = SessionId("ses_11111111-1111-1111-1111-111111111111")
_THREAD_ID = ThreadId("th_22222222-2222-2222-2222-222222222222")
_EVENT_ID = EventId("ev_33333333-3333-3333-3333-333333333333")
_SYSTEM_ACTOR = Actor(kind="system")
_CREATED_AT = datetime(2026, 8, 11, tzinfo=UTC)


def _make_event(
    event_type: EventType,
    data: dict[str, object] | None = None,
) -> Event:
    """Build a real `Event` envelope with fabricated but valid content."""
    return Event(
        event_id=_EVENT_ID,
        parent_event_id=None,
        thread_id=_THREAD_ID,
        session_id=_SESSION_ID,
        type=event_type,
        data=data or {},
        actor=_SYSTEM_ACTOR,
        created_at=_CREATED_AT,
    )


def _ui_event(kind: str, payload: dict[str, object]) -> Event:
    """Build a `UI_EVENT` envelope with the given wire-`kind` + payload."""
    return _make_event(
        EventType.UI_EVENT,
        data={"schema_version": 1, "kind": kind, "payload": payload},
    )


def _rule(event: str, matcher: str = "*") -> HookRule:
    return HookRule(event=event, matcher=matcher, command="true")


# ---------------------------------------------------------------------------
# One case per documented event type (matcher tests)
# ---------------------------------------------------------------------------


class TestMatcherPromptRequested:
    """`kolsch.prompt.requested` — subject is the `action` tool/action name."""

    def test_matches_exact_action(self) -> None:
        event = _ui_event(
            "kolsch.prompt.requested",
            {"prompt_id": "prm_x", "action": "kolsch/bash", "target": "ls -la"},
        )
        rule = _rule("kolsch.prompt.requested", matcher="kolsch/bash")
        assert match(rule, event) is True

    def test_wildcard_matcher_matches_any_action(self) -> None:
        event = _ui_event(
            "kolsch.prompt.requested",
            {"prompt_id": "prm_x", "action": "kolsch/write_file", "target": "x.txt"},
        )
        rule = _rule("kolsch.prompt.requested", matcher="*")
        assert match(rule, event) is True

    def test_non_matching_action(self) -> None:
        event = _ui_event(
            "kolsch.prompt.requested",
            {"prompt_id": "prm_x", "action": "kolsch/bash", "target": "ls -la"},
        )
        rule = _rule("kolsch.prompt.requested", matcher="kolsch/write_file")
        assert match(rule, event) is False


class TestMatcherPromptAnswered:
    """`kolsch.prompt.answered` — same subject rule as `.requested`.

    The real answered-payload shape (see `PromptsImpl._emit_auto_deny`
    and the widget-bridge path in `event_adapter.py`) usually carries
    only `prompt_id` / `response` / an optional `rule` — no `action`.
    Covers both: an event that does carry `action` (forwarded by a
    future/legacy caller) and the common real shape that doesn't.
    """

    def test_matches_when_action_present(self) -> None:
        event = _ui_event(
            "kolsch.prompt.answered",
            {"prompt_id": "prm_x", "response": "y", "action": "kolsch/bash"},
        )
        rule = _rule("kolsch.prompt.answered", matcher="kolsch/bash")
        assert match(rule, event) is True

    def test_wildcard_matches_real_answered_shape_without_action(self) -> None:
        event = _ui_event(
            "kolsch.prompt.answered",
            {
                "prompt_id": "prm_x",
                "response": "n",
                "reason": "approval_window_elapsed",
            },
        )
        rule = _rule("kolsch.prompt.answered", matcher="*")
        assert match(rule, event) is True

    def test_literal_matcher_does_not_match_when_action_absent(self) -> None:
        event = _ui_event(
            "kolsch.prompt.answered",
            {"prompt_id": "prm_x", "response": "y"},
        )
        rule = _rule("kolsch.prompt.answered", matcher="kolsch/bash")
        assert match(rule, event) is False


class TestMatcherThreadExchangeCompleted:
    """`thread.exchange_completed` — subject is always `"*"`."""

    def test_wildcard_matcher_matches(self) -> None:
        event = _make_event(
            EventType.THREAD_EXCHANGE_COMPLETED,
            data={
                "exchange_started_id": "ev_44444444-4444-4444-4444-444444444444",
                "agent_message_id": "ev_55555555-5555-5555-5555-555555555555",
            },
        )
        rule = _rule("thread.exchange_completed", matcher="*")
        assert match(rule, event) is True

    def test_non_wildcard_matcher_does_not_match_wildcard_subject(self) -> None:
        event = _make_event(
            EventType.THREAD_EXCHANGE_COMPLETED,
            data={
                "exchange_started_id": "ev_44444444-4444-4444-4444-444444444444",
                "agent_message_id": "ev_55555555-5555-5555-5555-555555555555",
            },
        )
        rule = _rule("thread.exchange_completed", matcher="kolsch/bash")
        assert match(rule, event) is False


class TestMatcherThreadExchangeFailed:
    """`thread.exchange_failed` — subject is always `"*"`."""

    def test_wildcard_matcher_matches(self) -> None:
        event = _make_event(
            EventType.THREAD_EXCHANGE_FAILED,
            data={
                "exchange_started_id": "ev_44444444-4444-4444-4444-444444444444",
                "error": {"code": "engine.fatal", "message": "boom"},
            },
        )
        rule = _rule("thread.exchange_failed", matcher="*")
        assert match(rule, event) is True

    def test_wrong_event_type_does_not_match(self) -> None:
        event = _make_event(
            EventType.THREAD_EXCHANGE_COMPLETED,
            data={
                "exchange_started_id": "ev_44444444-4444-4444-4444-444444444444",
                "agent_message_id": "ev_55555555-5555-5555-5555-555555555555",
            },
        )
        rule = _rule("thread.exchange_failed", matcher="*")
        assert match(rule, event) is False


class TestMatcherSessionOpened:
    """`session.opened` — subject is always `"*"`."""

    def test_wildcard_matcher_matches(self) -> None:
        event = _make_event(
            EventType.SESSION_OPENED,
            data={
                "session_id": str(_SESSION_ID),
                "initial_agent": {"agent_id": "demo"},
            },
        )
        rule = _rule("session.opened", matcher="*")
        assert match(rule, event) is True

    def test_non_wildcard_matcher_does_not_match_wildcard_subject(self) -> None:
        event = _make_event(
            EventType.SESSION_OPENED,
            data={
                "session_id": str(_SESSION_ID),
                "initial_agent": {"agent_id": "demo"},
            },
        )
        rule = _rule("session.opened", matcher="demo")
        assert match(rule, event) is False


class TestMatcherToolFailed:
    """`tool.failed` — subject is the failing tool's name."""

    def test_matches_exact_tool_name(self) -> None:
        event = _make_event(
            EventType.TOOL_FAILED,
            data={
                "tool_call_id": "call_1",
                "tool_name": "kolsch/bash",
                "error": {"code": "tool.execution_failed", "message": "boom"},
            },
        )
        rule = _rule("tool.failed", matcher="kolsch/bash")
        assert match(rule, event) is True

    def test_wildcard_matcher_matches_any_tool(self) -> None:
        event = _make_event(
            EventType.TOOL_FAILED,
            data={
                "tool_call_id": "call_1",
                "tool_name": "kolsch/write_file",
                "error": {"code": "tool.execution_failed", "message": "boom"},
            },
        )
        rule = _rule("tool.failed", matcher="*")
        assert match(rule, event) is True

    def test_non_matching_tool_name(self) -> None:
        event = _make_event(
            EventType.TOOL_FAILED,
            data={
                "tool_call_id": "call_1",
                "tool_name": "kolsch/bash",
                "error": {"code": "tool.execution_failed", "message": "boom"},
            },
        )
        rule = _rule("tool.failed", matcher="kolsch/write_file")
        assert match(rule, event) is False


# ---------------------------------------------------------------------------
# Cross-cutting: wrong event type entirely
# ---------------------------------------------------------------------------


class TestMatcherWrongEventType:
    def test_rule_for_different_event_type_never_matches(self) -> None:
        event = _make_event(
            EventType.TOOL_FAILED,
            data={
                "tool_call_id": "call_1",
                "tool_name": "kolsch/bash",
                "error": {"code": "tool.execution_failed", "message": "boom"},
            },
        )
        rule = _rule("session.opened", matcher="*")
        assert match(rule, event) is False

    def test_ui_event_kind_mismatch_never_matches(self) -> None:
        event = _ui_event(
            "kolsch.prompt.requested",
            {"prompt_id": "prm_x", "action": "kolsch/bash"},
        )
        rule = _rule("kolsch.prompt.answered", matcher="*")
        assert match(rule, event) is False

    def test_generic_ui_event_type_string_never_matches_wire_kind_rule(self) -> None:
        # A rule literally targeting the generic envelope type
        # ("ui.event") should not match — `_event_type_string` always
        # substitutes the nested wire-kind for UI_EVENT envelopes, by
        # design (an author writes the specific wire-kind, e.g.
        # "kolsch.prompt.requested", not the generic "ui.event").
        event = _ui_event(
            "kolsch.prompt.requested",
            {"prompt_id": "prm_x", "action": "kolsch/bash"},
        )
        rule = _rule("ui.event", matcher="*")
        assert match(rule, event) is False


# ---------------------------------------------------------------------------
# Parametrized sweep: wildcard always matches, garbage never does
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "data"),
    [
        (
            EventType.SESSION_OPENED,
            {"session_id": str(_SESSION_ID), "initial_agent": {"agent_id": "demo"}},
        ),
        (
            EventType.THREAD_EXCHANGE_COMPLETED,
            {
                "exchange_started_id": "ev_44444444-4444-4444-4444-444444444444",
                "agent_message_id": "ev_55555555-5555-5555-5555-555555555555",
            },
        ),
        (
            EventType.TOOL_FAILED,
            {
                "tool_call_id": "call_1",
                "tool_name": "kolsch/bash",
                "error": {"code": "tool.execution_failed", "message": "boom"},
            },
        ),
    ],
)
def test_matcher_wildcard_always_matches(
    event_type: EventType, data: dict[str, object]
) -> None:
    event = _make_event(event_type, data=data)
    rule = _rule(str(event_type), matcher="*")
    assert match(rule, event) is True


def test_matcher_non_matching_subject_with_specific_glob() -> None:
    event = _make_event(
        EventType.TOOL_FAILED,
        data={
            "tool_call_id": "call_1",
            "tool_name": "kolsch/read_file",
            "error": {"code": "tool.execution_failed", "message": "boom"},
        },
    )
    rule = _rule("tool.failed", matcher="kolsch/bash*")
    assert match(rule, event) is False


def test_matcher_glob_prefix_matches_tool_name() -> None:
    event = _make_event(
        EventType.TOOL_FAILED,
        data={
            "tool_call_id": "call_1",
            "tool_name": "kolsch/bash",
            "error": {"code": "tool.execution_failed", "message": "boom"},
        },
    )
    rule = _rule("tool.failed", matcher="kolsch/*")
    assert match(rule, event) is True


# ---------------------------------------------------------------------------
# Runner tests (Step 3) — `-k runner`
#
# Real subprocess execution throughout (not mocked): this is exactly the
# behavior spec-131 Step 3 needs verified — shell invocation, timeout/kill
# escalation, and the "never raises" contract are all subprocess-level
# concerns that a mock would beg the question on.
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Return whether a POSIX process id is still alive.

    `os.kill(pid, 0)` sends no signal — it only performs the
    existence/permission check the kernel does before delivering a
    real signal, so this is a safe, side-effect-free liveness probe.
    `ProcessLookupError` means the pid is gone; any other outcome
    (success, or `PermissionError` for a pid we don't own — not
    expected here since we spawned it) means it's still around.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    else:
        return True


async def test_runner_success_runs_command(tmp_path: Path) -> None:
    """A working command actually executes — file appears after the call."""
    marker = tmp_path / f"kolsch-hook-test-{uuid.uuid4().hex}"
    assert not marker.exists()
    try:
        await run_hook_command(f"touch {marker}", timeout_seconds=5)
        assert marker.exists()
    finally:
        marker.unlink(missing_ok=True)


async def test_runner_timeout_kills_process_and_returns_promptly(
    tmp_path: Path,
) -> None:
    """A hanging command is killed within the grace+kill budget, not awaited to completion.

    Verifies two things `-k runner`'s spec explicitly calls out:
    1. `run_hook_command` returns within a couple of seconds for a
       1s timeout — not anywhere close to the full duration of the
       hanging command (`sleep 100`) — proving it doesn't block the
       caller.
    2. The underlying OS process is actually terminated (not just
       that the awaiting call returned) — verified by having the
       shell write its own PID to a file, then probing that PID with
       `os.kill(pid, 0)` after the call returns and asserting it's
       gone.
    """
    pid_file = tmp_path / f"kolsch-hook-pid-{uuid.uuid4().hex}"
    # `$$` in `sh -c` is the shell's own pid; `exec sleep 100` replaces
    # the shell process image with `sleep` (same pid) so the pid we
    # capture is the one that actually needs to die, not a parent
    # shell that would otherwise be left orphaned by a plain `kill()`
    # of a *different* pid than the one doing the sleeping.
    command = f"echo $$ > {pid_file}; exec sleep 100"

    started = time.monotonic()
    await run_hook_command(command, timeout_seconds=1)
    elapsed = time.monotonic() - started

    # Grace window is 0.5s on top of the 1s timeout; a few seconds of
    # slack comfortably distinguishes "killed promptly" from
    # "accidentally waited for the full 100s sleep."
    assert elapsed < 10, (
        f"run_hook_command took {elapsed:.1f}s — expected well under 10s"
    )

    assert pid_file.exists(), "hook command never started"
    pid = int(pid_file.read_text().strip())
    still_alive = await _wait_until_dead(pid, timeout_seconds=5)
    assert not still_alive, f"pid {pid} is still alive after timeout+kill"


async def _wait_until_dead(pid: int, *, timeout_seconds: float) -> bool:
    """Poll `pid` until it exits or `timeout_seconds` elapses.

    SIGKILL delivery/reaping can lag `run_hook_command`'s return by a
    beat under load, so this gives the OS a short, bounded grace
    window rather than asserting liveness immediately.

    Returns whether `pid` is still alive when the wait ends (`True` =
    still alive / timed out, `False` = confirmed dead).
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return False
        await asyncio.sleep(0.1)
    return _pid_alive(pid)


async def test_runner_timeout_with_grandchild_holding_pipe_still_returns_promptly(
    tmp_path: Path,
) -> None:
    """spec-131 Step 6 review fix regression test.

    Two independent reviewers caught: a hook command that spawns a
    detached grandchild inheriting the stdout/stderr pipes (here:
    `(sleep 30 &)` — a background job in a subshell, so the DIRECT
    child exits almost immediately but the grandchild `sleep 30`
    keeps the pipes open) used to make `run_hook_command` block for
    up to the grandchild's lifetime after the timeout-kill fired,
    because `_kill_with_grace`'s final `proc.communicate()` read the
    pipes to EOF — which only happens once every holder of the write
    end (including the grandchild SIGKILL never touched) exits.
    `_reap_after_kill` now bounds that read via
    `_POST_KILL_REAP_TIMEOUT_SECONDS` (1.5s) instead. This test's
    `timeout_seconds=1` command doesn't even need to hit the SIGINT
    grace path — the outer `wait_for(proc.communicate(), timeout=1)`
    itself already blocks on the same held-open pipe, so this also
    exercises `_run_hook_command_inner`'s own `finally`-block reap
    (`proc.kill()` -> `_reap_after_kill`), the second call site the
    reviewers flagged.
    """
    # 4s: long enough to prove the reap bound (1.5s) actually applies —
    # without the fix this test would take ~4s (tracking the
    # grandchild's lifetime), not the ~1-3s the fix produces — while
    # short enough that the grandchild has always exited by the time
    # the interpreter/event loop tears down at test-session end. A
    # much longer sleep (e.g. 30s) reproduces the same defect just as
    # well but leaks a live background process across the whole test
    # session, which trips pytest's `unraisableexception` plugin via a
    # `BaseSubprocessTransport.__del__` warning at a LATER, unrelated
    # point in the run — a real hazard for CI, not just cosmetic.
    command = "(sleep 4 &)"

    started = time.monotonic()
    await run_hook_command(command, timeout_seconds=1)
    elapsed = time.monotonic() - started

    # Without the fix this would track the grandchild's ~4s lifetime.
    # With the fix: ~1s (outer timeout) + a beat for the SIGINT-grace
    # escalation + at most _POST_KILL_REAP_TIMEOUT_SECONDS (1.5s) for
    # the bounded reap — comfortably under 3.5s, well under the
    # grandchild's full lifetime.
    assert elapsed < 3.5, (
        f"run_hook_command took {elapsed:.1f}s with a grandchild holding "
        "the pipe open — expected well under 3.5s (was previously "
        "unbounded, tracking the grandchild's own lifetime)"
    )


async def test_reap_after_kill_bounds_communicate_against_held_open_pipe() -> None:
    """Unit-level version of the same fix, isolated to `_reap_after_kill`
    itself (not the full `run_hook_command` timeout ladder): a process
    whose direct child exits immediately but leaves a grandchild
    holding the stdout pipe open must still have its reap bounded.

    Uses a 4s grandchild sleep (not a much longer one) so it always
    exits before the test session ends — see the sibling test's
    comment for why a long-lived leaked background process is a real
    CI hazard (a `BaseSubprocessTransport.__del__` warning pytest's
    `unraisableexception` plugin escalates), not just cosmetic.
    """
    proc = await asyncio.create_subprocess_shell(
        "(sleep 4 &)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # Let the direct child actually exit (it should, almost
    # immediately — only the backgrounded grandchild lingers).
    await proc.wait()
    assert proc.returncode is not None

    started = time.monotonic()
    stdout_b, stderr_b = await _reap_after_kill(proc)
    elapsed = time.monotonic() - started

    assert elapsed < _POST_KILL_REAP_TIMEOUT_SECONDS + 2.0, (
        f"_reap_after_kill took {elapsed:.1f}s against a grandchild "
        f"holding the pipe open — expected close to "
        f"_POST_KILL_REAP_TIMEOUT_SECONDS ({_POST_KILL_REAP_TIMEOUT_SECONDS}s)"
    )
    # Timed out on the pipe read — no output was ever collected.
    assert stdout_b == b""
    assert stderr_b == b""


async def test_runner_nonexistent_command_does_not_raise() -> None:
    """A genuinely bad command is caught internally — no exception propagates.

    A command name that will not exist on PATH; the shell reports
    "command not found" and exits non-zero — `run_hook_command` must
    swallow this (log it) rather than let it surface. Simply
    completing this `await` without raising is the assertion.
    """
    unique = uuid.uuid4().hex
    await run_hook_command(
        f"kolsch-hook-test-nonexistent-command-{unique}", timeout_seconds=5
    )


async def test_runner_success_does_not_raise() -> None:
    """A trivially successful command also completes without raising."""
    await run_hook_command("true", timeout_seconds=5)


# ---------------------------------------------------------------------------
# HookDispatcher tests (Step 4) — `-k dispatcher`
#
# Most cases here spy/mock `run_hook_command` at the module level
# (`platform_runtime.hooks.run_hook_command`, patched via
# `monkeypatch.setattr` so `HookDispatcher.handle`'s own
# `asyncio.create_task(run_hook_command(...))` call picks up the
# replacement — the module-level name, not a re-import, is what
# `handle()` actually calls) rather than requiring a real subprocess
# round-trip per test; Step 3's runner tests already cover the real
# subprocess behavior. The one exception is the smoke test at the
# bottom, which deliberately runs a real (mocked-nothing) subprocess to
# prove end-to-end wiring at this layer.
# ---------------------------------------------------------------------------


def _spy_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Patch `run_hook_command` with a fast, non-blocking recording spy.

    Returns the list the spy appends `(command, timeout_seconds)` to.
    The spy itself does no real subprocess work and returns
    immediately, isolating these tests from Step 3's subprocess
    behavior entirely.
    """
    calls: list[tuple[str, int]] = []

    async def _spy(command: str, timeout_seconds: int) -> None:
        calls.append((command, timeout_seconds))

    monkeypatch.setattr("platform_runtime.hooks.run_hook_command", _spy)
    return calls


async def _drain_background_tasks() -> None:
    """Yield control so fire-and-forget tasks created via `create_task` run.

    `HookDispatcher.handle()` schedules background tasks but does not
    await them; a bare `asyncio.sleep(0)` gives the event loop enough
    of a turn for an already-scheduled, fast (spy) task to actually
    execute before the test asserts on its side effect.
    """
    await asyncio.sleep(0)


class TestHookDispatcherMatchingAndFiring:
    """Construct with 2-3 rules, feed synthetic events, assert fires/no-fires."""

    def _dispatcher(self) -> HookDispatcher:
        rules = [
            HookRule(
                event="kolsch.prompt.requested",
                matcher="kolsch/bash",
                command="notify-bash-requested",
            ),
            HookRule(
                event="tool.failed",
                matcher="*",
                command="notify-any-tool-failed",
            ),
            HookRule(
                event="session.opened",
                matcher="*",
                command="notify-session-opened",
            ),
        ]
        return HookDispatcher(rules)

    async def test_matching_event_fires_the_right_rule(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _spy_calls(monkeypatch)
        dispatcher = self._dispatcher()
        event = _ui_event(
            "kolsch.prompt.requested",
            {"prompt_id": "prm_x", "action": "kolsch/bash", "target": "ls -la"},
        )

        await dispatcher.handle(event)
        await _drain_background_tasks()

        assert calls == [("notify-bash-requested", 10)]

    async def test_non_matching_event_fires_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _spy_calls(monkeypatch)
        dispatcher = self._dispatcher()
        event = _ui_event(
            "kolsch.prompt.requested",
            {"prompt_id": "prm_x", "action": "kolsch/write_file", "target": "x.txt"},
        )

        await dispatcher.handle(event)
        await _drain_background_tasks()

        assert calls == []

    async def test_unrelated_event_type_fires_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _spy_calls(monkeypatch)
        dispatcher = self._dispatcher()
        event = _make_event(
            EventType.THREAD_EXCHANGE_COMPLETED,
            data={
                "exchange_started_id": "ev_44444444-4444-4444-4444-444444444444",
                "agent_message_id": "ev_55555555-5555-5555-5555-555555555555",
            },
        )

        await dispatcher.handle(event)
        await _drain_background_tasks()

        assert calls == []

    async def test_wildcard_rule_fires_for_matching_event_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _spy_calls(monkeypatch)
        dispatcher = self._dispatcher()
        event = _make_event(
            EventType.TOOL_FAILED,
            data={
                "tool_call_id": "call_1",
                "tool_name": "kolsch/anything",
                "error": {"code": "tool.execution_failed", "message": "boom"},
            },
        )

        await dispatcher.handle(event)
        await _drain_background_tasks()

        assert calls == [("notify-any-tool-failed", 10)]

    async def test_multiple_matching_rules_all_fire(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _spy_calls(monkeypatch)
        rules = [
            HookRule(event="session.opened", matcher="*", command="cmd-a"),
            HookRule(event="session.opened", matcher="*", command="cmd-b"),
        ]
        dispatcher = HookDispatcher(rules)
        event = _make_event(
            EventType.SESSION_OPENED,
            data={
                "session_id": str(_SESSION_ID),
                "initial_agent": {"agent_id": "demo"},
            },
        )

        await dispatcher.handle(event)
        await _drain_background_tasks()

        assert sorted(calls) == [("cmd-a", 10), ("cmd-b", 10)]

    async def test_empty_rule_list_fires_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _spy_calls(monkeypatch)
        dispatcher = HookDispatcher([])
        event = _make_event(
            EventType.SESSION_OPENED,
            data={
                "session_id": str(_SESSION_ID),
                "initial_agent": {"agent_id": "demo"},
            },
        )

        await dispatcher.handle(event)
        await _drain_background_tasks()

        assert calls == []


class TestHookDispatcherNonBlocking:
    """`.handle()` must return promptly even when the fired command is slow.

    Bounds the assertion with `asyncio.wait_for` on `.handle()` itself
    (not on the underlying command) — if `handle()` ever regressed to
    awaiting the runner inline, this test would time out and fail,
    rather than just running slow.
    """

    async def test_handle_returns_promptly_despite_slow_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_spy(command: str, timeout_seconds: int) -> None:
            started.set()
            # Would hang forever if `handle()` ever awaited this call
            # inline — the test's own timeout below is what actually
            # catches a regression, this `release` gate just makes the
            # background task's lifetime deterministic within the test.
            await release.wait()

        monkeypatch.setattr("platform_runtime.hooks.run_hook_command", _slow_spy)
        dispatcher = HookDispatcher(
            [HookRule(event="session.opened", matcher="*", command="slow-command")]
        )
        event = _make_event(
            EventType.SESSION_OPENED,
            data={
                "session_id": str(_SESSION_ID),
                "initial_agent": {"agent_id": "demo"},
            },
        )

        started_at = time.monotonic()
        await asyncio.wait_for(dispatcher.handle(event), timeout=2)
        elapsed = time.monotonic() - started_at

        assert elapsed < 1, (
            f"handle() took {elapsed:.3f}s — it must not await the fired command"
        )

        # Let the background task actually start (proves the command
        # really was launched, not silently dropped), then release it
        # so it doesn't leak past the test.
        await asyncio.wait_for(started.wait(), timeout=2)
        release.set()

    async def test_handle_returns_promptly_with_multiple_slow_matches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = asyncio.Event()
        call_count = 0

        async def _slow_spy(command: str, timeout_seconds: int) -> None:
            nonlocal call_count
            call_count += 1
            await release.wait()

        monkeypatch.setattr("platform_runtime.hooks.run_hook_command", _slow_spy)
        dispatcher = HookDispatcher(
            [
                HookRule(event="session.opened", matcher="*", command="slow-a"),
                HookRule(event="session.opened", matcher="*", command="slow-b"),
            ]
        )
        event = _make_event(
            EventType.SESSION_OPENED,
            data={
                "session_id": str(_SESSION_ID),
                "initial_agent": {"agent_id": "demo"},
            },
        )

        started_at = time.monotonic()
        await asyncio.wait_for(dispatcher.handle(event), timeout=2)
        elapsed = time.monotonic() - started_at

        assert elapsed < 1

        release.set()
        # Drain so both background tasks actually complete before the
        # test ends (avoids "coroutine was never awaited"-style noise
        # across test boundaries).
        for _ in range(10):
            if call_count == 2:
                break
            await asyncio.sleep(0.05)
        assert call_count == 2


class TestHookDispatcherBackgroundTaskExceptionLogging:
    """Defensive logging if a fired background task ever raises.

    `run_hook_command` (Step 3) is verified to never raise today, so
    this is purely a structural backstop: if a future edit ever let an
    exception through, it must surface via kolsch's own logger, not
    only via asyncio's default "Task exception was never retrieved"
    handler at GC time.
    """

    async def test_exception_from_fired_task_is_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def _raising_spy(command: str, timeout_seconds: int) -> None:
            raise RuntimeError("boom from a hypothetical future Step 3 bug")

        monkeypatch.setattr("platform_runtime.hooks.run_hook_command", _raising_spy)
        dispatcher = HookDispatcher(
            [HookRule(event="session.opened", matcher="*", command="whatever")]
        )
        event = _make_event(
            EventType.SESSION_OPENED,
            data={
                "session_id": str(_SESSION_ID),
                "initial_agent": {"agent_id": "demo"},
            },
        )

        with caplog.at_level("ERROR", logger="platform_runtime.hooks"):
            await dispatcher.handle(event)
            # The task raises asynchronously; give the done-callback a
            # turn to run and log before asserting on caplog.
            for _ in range(20):
                if caplog.records:
                    break
                await asyncio.sleep(0.05)

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelname == "ERROR"
        assert "hook background task raised unexpectedly" in record.getMessage()
        assert record.exc_info is not None
        assert isinstance(record.exc_info[1], RuntimeError)

    async def test_cancelled_task_is_not_logged_as_an_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        release = asyncio.Event()

        async def _hanging_spy(command: str, timeout_seconds: int) -> None:
            await release.wait()

        monkeypatch.setattr("platform_runtime.hooks.run_hook_command", _hanging_spy)
        dispatcher = HookDispatcher(
            [HookRule(event="session.opened", matcher="*", command="whatever")]
        )
        event = _make_event(
            EventType.SESSION_OPENED,
            data={
                "session_id": str(_SESSION_ID),
                "initial_agent": {"agent_id": "demo"},
            },
        )

        with caplog.at_level("ERROR", logger="platform_runtime.hooks"):
            await dispatcher.handle(event)
            await asyncio.sleep(0)  # let the task actually start

            assert len(_BACKGROUND_TASKS) == 1
            (task,) = tuple(_BACKGROUND_TASKS)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert caplog.records == []


class TestHookDispatcherPromptAnsweredWarning:
    """One-time construction-time warning for a structurally dead rule.

    `kolsch.prompt.answered`'s real payload never carries `action`
    (see the `_ACTION_SUBJECT_KINDS` module comment in
    `platform_runtime.hooks`), so a non-wildcard matcher on that event
    can never fire. `HookDispatcher.__init__` logs a warning once, at
    construction, when it sees this — not per-event.
    """

    def test_warns_once_at_construction_for_non_wildcard_matcher(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="platform_runtime.hooks"):
            HookDispatcher(
                [
                    HookRule(
                        event="kolsch.prompt.answered",
                        matcher="kolsch/bash",
                        command="notify-answered",
                    )
                ]
            )

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "kolsch.prompt.answered" in warnings[0].getMessage()

    async def test_does_not_warn_again_per_event(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _spy_calls(monkeypatch)
        with caplog.at_level("WARNING", logger="platform_runtime.hooks"):
            dispatcher = HookDispatcher(
                [
                    HookRule(
                        event="kolsch.prompt.answered",
                        matcher="kolsch/bash",
                        command="notify-answered",
                    )
                ]
            )
            assert len(caplog.records) == 1

            event = _ui_event(
                "kolsch.prompt.answered",
                {"prompt_id": "prm_x", "response": "y"},
            )
            for _ in range(3):
                await dispatcher.handle(event)
                await _drain_background_tasks()

        # Still exactly one warning — construction-time only, never
        # re-emitted by `handle()` no matter how many events flow
        # through (and this rule never actually fires, by design).
        assert len(caplog.records) == 1

    def test_no_warning_for_wildcard_matcher(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="platform_runtime.hooks"):
            HookDispatcher(
                [
                    HookRule(
                        event="kolsch.prompt.answered",
                        matcher="*",
                        command="notify-answered",
                    )
                ]
            )

        assert len(caplog.records) == 0

    def test_no_warning_for_prompt_requested_non_wildcard(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # `kolsch.prompt.requested` DOES carry `action` in its real
        # payload — a non-wildcard matcher there is legitimate and
        # must not trigger this warning.
        with caplog.at_level("WARNING", logger="platform_runtime.hooks"):
            HookDispatcher(
                [
                    HookRule(
                        event="kolsch.prompt.requested",
                        matcher="kolsch/bash",
                        command="notify-requested",
                    )
                ]
            )

        assert len(caplog.records) == 0

    def test_no_warning_for_unrelated_event(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="platform_runtime.hooks"):
            HookDispatcher(
                [HookRule(event="tool.failed", matcher="kolsch/bash", command="notify")]
            )

        assert len(caplog.records) == 0


# ---------------------------------------------------------------------------
# HookDispatcher.observe — the EventObserver-Protocol-shaped alias
# (Step 6). `AgentEngine`'s `observer:` param expects a structural
# `async def observe(self, event) -> None`; `HookDispatcher`'s own
# public method is `handle` (Step 4's naming, matching Claude Code's
# hook vocabulary). `observe` is a thin forwarding alias added at Step
# 6 so a `HookDispatcher` instance can be passed directly as
# `AgentEngine(..., observer=dispatcher)` at every real construction
# site (`src/platform_runtime/runtime.py:bootstrap_runtime`).
# ---------------------------------------------------------------------------


class TestHookDispatcherObserveAlias:
    async def test_observe_forwards_to_handle_for_matching_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _spy_calls(monkeypatch)
        dispatcher = HookDispatcher(
            [_rule("kolsch.prompt.requested", matcher="kolsch/bash")]
        )
        event = _ui_event(
            "kolsch.prompt.requested",
            {"prompt_id": "prm_x", "action": "kolsch/bash"},
        )

        await dispatcher.observe(event)
        await _drain_background_tasks()

        assert len(calls) == 1

    async def test_observe_no_op_for_non_matching_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _spy_calls(monkeypatch)
        dispatcher = HookDispatcher(
            [_rule("kolsch.prompt.requested", matcher="kolsch/bash")]
        )
        event = _ui_event(
            "kolsch.prompt.requested",
            {"prompt_id": "prm_x", "action": "kolsch/write_file"},
        )

        await dispatcher.observe(event)
        await _drain_background_tasks()

        assert calls == []

    async def test_observe_returns_promptly_for_slow_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same fire-and-forget contract as `handle()` — `observe()`
        must not block on a slow fired command, since it's `observe()`
        (not `handle()`) that `AgentEngine.execute_stream` actually
        awaits per event (Step 5)."""

        async def _slow_spy(command: str, timeout_seconds: int) -> None:
            await asyncio.sleep(10)

        monkeypatch.setattr("platform_runtime.hooks.run_hook_command", _slow_spy)
        dispatcher = HookDispatcher([_rule("tool.failed")])
        event = _make_event(EventType.TOOL_FAILED, {"tool_name": "kolsch/bash"})

        await asyncio.wait_for(dispatcher.observe(event), timeout=1.0)

    def test_dispatcher_satisfies_event_observer_protocol_shape(self) -> None:
        """Structural check: `HookDispatcher` has the one method the
        `EventObserver` Protocol (`src/engine/event_observer.py`)
        requires — `async def observe(self, event) -> None` — so it is
        usable directly as `AgentEngine(observer=dispatcher)` without a
        separate wrapper class. `EventObserver` is a plain (not
        `@runtime_checkable`) `Protocol`, so this checks the method's
        presence directly rather than via `isinstance`, which would
        raise `TypeError` against a non-runtime-checkable Protocol."""
        dispatcher = HookDispatcher([])
        assert hasattr(dispatcher, "observe")
        assert asyncio.iscoroutinefunction(dispatcher.observe)


# ---------------------------------------------------------------------------
# drain_background_hook_tasks (Step 6) — the short-lived-process fix.
#
# Real root cause (verified via a minimal, kolsch-independent repro,
# not just inferred): a fire-and-forget `HookDispatcher.handle()` task
# still suspended inside `proc.communicate()` when a short-lived
# process's top-level coroutine returns does NOT reliably get
# `CancelledError` delivered during `asyncio.run()`'s shutdown
# sequence on macOS + CPython 3.13 — the process hangs forever instead
# of exiting. `drain_background_hook_tasks` closes the race by
# best-effort AWAITING (never cancelling) in-flight hook tasks for a
# short bounded window before the caller returns. `kolsch run` and
# `kolsch workflows run`/`resume` (any short-lived CLI command) need
# this; `kolsch chat`/the HTTP API don't (long-lived processes, ample
# real time for a hook task to finish on its own).
# ---------------------------------------------------------------------------


class TestDrainBackgroundHookTasks:
    async def test_drain_waits_for_in_flight_task_to_finish(self) -> None:
        """A task that finishes well within the grace window is awaited
        to completion, not abandoned."""
        finished = False

        async def _slow_but_finishes() -> None:
            nonlocal finished
            await asyncio.sleep(0.05)
            finished = True

        task = asyncio.create_task(_slow_but_finishes())
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

        await drain_background_hook_tasks(timeout_seconds=2.0)

        assert finished
        assert task.done()

    async def test_drain_does_not_cancel_on_timeout(self) -> None:
        """A task that outlives the grace window is left running, not
        cancelled — the whole point is to avoid the cancellation path
        that hangs; timing out just means this function gives up
        waiting and returns, same outcome as if it didn't exist."""

        async def _never_finishes_in_time() -> None:
            await asyncio.sleep(10)

        task = asyncio.create_task(_never_finishes_in_time())
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

        await drain_background_hook_tasks(timeout_seconds=0.05)

        assert not task.done()
        assert not task.cancelled()

        # Cleanup: this test's own task must not leak into later tests.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def test_drain_no_op_when_nothing_pending(self) -> None:
        """No in-flight tasks — returns immediately, no error."""
        assert not _BACKGROUND_TASKS
        await drain_background_hook_tasks(timeout_seconds=2.0)  # must not hang/raise

    async def test_drain_ignores_already_done_tasks(self) -> None:
        """A task already finished before drain runs doesn't block
        or error the wait — only genuinely pending tasks are waited
        on."""

        async def _instant() -> None:
            return None

        task = asyncio.create_task(_instant())
        await task  # already done by the time drain runs
        _BACKGROUND_TASKS.add(task)

        await drain_background_hook_tasks(timeout_seconds=2.0)

        _BACKGROUND_TASKS.discard(task)

    async def test_drain_real_end_to_end_via_dispatcher(self, tmp_path: Path) -> None:
        """The actual scenario `_run_handler_and_drain_hooks` (Step 6,
        `src/cli/main.py`) exists to fix: a real `HookDispatcher.handle()`
        call launches a real subprocess task, the caller does NOT await
        `handle()`'s background work, and `drain_background_hook_tasks`
        is what lets that subprocess actually finish before the process
        would otherwise exit."""
        marker = tmp_path / f"kolsch-drain-e2e-{uuid.uuid4().hex}"
        assert not marker.exists()

        dispatcher = HookDispatcher(
            [
                HookRule(
                    event="tool.failed", command=f"touch {marker}", timeout_seconds=5
                )
            ]
        )
        event = _make_event(EventType.TOOL_FAILED, {"tool_name": "kolsch/bash"})

        await dispatcher.handle(event)  # fire-and-forget, per its contract
        assert not marker.exists()  # not yet — handle() didn't wait

        await drain_background_hook_tasks(timeout_seconds=5.0)

        assert marker.exists()


# ---------------------------------------------------------------------------
# Smoke test — real (non-mocked) end-to-end dispatch (Step 4)
# ---------------------------------------------------------------------------


async def test_smoke_dispatcher_fires_real_subprocess_for_matching_event(
    tmp_path: Path,
) -> None:
    """Construct a real `HookDispatcher`, feed real events, no mocking at all.

    Proves end-to-end wiring at this layer: `handle()` -> `match()` ->
    `run_hook_command()` -> an actual subprocess that leaves a file on
    disk. A background task is inherently async relative to `handle()`
    returning, so this polls briefly for the file rather than asserting
    immediately after `await dispatcher.handle(...)` returns.
    """
    marker = tmp_path / f"kolsch-dispatcher-smoketest-{uuid.uuid4().hex}"
    assert not marker.exists()

    dispatcher = HookDispatcher(
        [
            HookRule(
                event="kolsch.prompt.requested",
                matcher="*",
                command=f"touch {marker}",
                timeout_seconds=5,
            )
        ]
    )

    try:
        matching_event = _ui_event(
            "kolsch.prompt.requested",
            {"prompt_id": "prm_x", "action": "kolsch/bash", "target": "ls -la"},
        )
        await dispatcher.handle(matching_event)

        appeared = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if marker.exists():
                appeared = True
                break
            await asyncio.sleep(0.05)
        assert appeared, "expected the hook's touch command to create the marker file"

        # Non-matching event: different action, must not fire again
        # (nothing new to observe here beyond "no crash, no second
        # marker expected" since the marker already exists from above —
        # covered properly by the isolated non-matching case below).
    finally:
        marker.unlink(missing_ok=True)


async def test_smoke_dispatcher_does_not_fire_real_subprocess_for_non_matching_event(
    tmp_path: Path,
) -> None:
    """Same real dispatcher, a non-matching event: confirm no file appears."""
    marker = tmp_path / f"kolsch-dispatcher-smoketest-{uuid.uuid4().hex}"
    assert not marker.exists()

    dispatcher = HookDispatcher(
        [
            HookRule(
                event="kolsch.prompt.requested",
                matcher="kolsch/bash",
                command=f"touch {marker}",
                timeout_seconds=5,
            )
        ]
    )

    try:
        non_matching_event = _ui_event(
            "kolsch.prompt.requested",
            {"prompt_id": "prm_x", "action": "kolsch/write_file", "target": "x.txt"},
        )
        await dispatcher.handle(non_matching_event)

        # Give any (incorrectly fired) background task a real chance to
        # run before asserting absence.
        await asyncio.sleep(0.5)
        assert not marker.exists(), (
            "non-matching event must not have fired the hook subprocess"
        )
    finally:
        marker.unlink(missing_ok=True)
