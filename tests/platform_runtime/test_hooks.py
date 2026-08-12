"""Tests for `platform_runtime.hooks` — spec-131 Steps 2 and 3.

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
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from platform_runtime.hooks import match, run_hook_command
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
