"""Hook matcher + subprocess runner for shell-command hooks (spec-131).

This module holds the matching logic (`match`), the subprocess
execution primitive (`run_hook_command`), and the `HookDispatcher`
that combines them into a fire-and-forget event handler. Engine
wiring (injecting a real `HookDispatcher` into `AgentEngine`) is a
later spec-131 step (5/6).

Matching has two stages, per spec-131's Scope § Matching table:

1. `event.type` must equal `rule.event` — EXCEPT for `EventType.UI_EVENT`,
   where the comparison uses the nested wire-kind string
   (`event.data["kind"]`, e.g. `"kolsch.prompt.requested"`) instead of
   the generic `"ui.event"` envelope type. This lets a `platform.yaml`
   author write `event: "kolsch.prompt.requested"` directly, matching
   the same string CrewAI/Claude-Code-style hook configs would use, one
   level more specific than the raw `EventType` vocabulary.
2. If the event type matches, a per-event-type "subject" string is
   computed (see `_subject_for`) and checked against `rule.matcher` via
   `fnmatch.fnmatchcase` — the same case-sensitive glob primitive
   `RuleBasedAuthorizationProvider._rule_matches` uses internally
   (`src/providers/authz.py:1074-1103`), imported directly rather than
   reusing that private, ACL-shape-coupled helper (see spec-131
   Technical Context).

`run_hook_command` (Step 3) executes a hook's `command` as a subprocess,
mirroring the bash tool's subprocess-safety conventions verbatim —
see the function docstring for the exact conventions reused and their
source citations. It is a **standalone execution primitive**.

`HookDispatcher` (Step 4) combines the two: given a list of `HookRule`s,
its `handle(event)` calls `match()` for each rule and fires
`run_hook_command` as a background task for every match, without
awaiting it — see the class docstring for the fire-and-forget contract.
Nothing in this module wires a `HookDispatcher` to a real event stream
yet (no engine, no `platform.yaml` loading) — that's Steps 5 and 6.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
import signal
from typing import TYPE_CHECKING

from schemas.events import EventType

if TYPE_CHECKING:
    from schemas.config import HookRule
    from schemas.events import Event

_log = logging.getLogger("platform_runtime.hooks")

# --- Subprocess-safety tunables (Step 3) -----------------------------------
#
# Mirrored directly from the bash tool's conventions rather than
# reinvented — see `run_hook_command`'s docstring for exact source
# citations.
#
# `_SIGINT_GRACE_SECONDS` matches `bash.py`'s
# `_SIGINT_GRACE_SECONDS = 0.5` (`src/cli/_starter_bundle/kolsch/tools/
# bash.py:42-51`): give a well-behaved command a brief window to react
# to SIGINT before escalating to SIGKILL.
_SIGINT_GRACE_SECONDS = 0.5

# spec-131 Step 6 review fix — bound on the POST-CANCELLATION cleanup
# (`_reap_after_kill` / `_close_transport_quietly`, below). Real,
# review-caught defect (two independent reviewers, matching empirical
# data): `drain_background_hook_tasks`'s short grace window did not
# actually bound process-exit time, because when it expired,
# `asyncio.run()`'s own shutdown cancellation could hit an UNBOUNDED
# wait deep inside the hook-runner's cleanup path. Two distinct forms
# of the same root cause, both fixed here:
#
# 1. `_kill_with_grace`'s post-kill `proc.communicate()` and
#    `_run_hook_command_inner`'s `finally`-block `proc.wait()` were
#    unbounded. `SIGKILL` only terminates the DIRECT child; if the
#    hook's shell command spawned a grandchild that inherited the
#    stdout/stderr pipes (`cmd &`, `cmd1 && cmd2`, a pipeline) and is
#    still alive, the pipes stay open and the awaited call blocks
#    until THAT process exits — up to the hook's own `timeout_seconds`
#    ceiling (60s max).
# 2. Separately (and more subtly — found via direct empirical
#    tracing, not just reasoning): even when the DIRECT child has
#    already exited on its own (`proc.returncode` already set, no
#    kill needed at all), a `proc.communicate()` call cancelled
#    mid-flight — which is exactly what happens when
#    `drain_background_hook_tasks`'s window expires and
#    `asyncio.run()`'s shutdown cancels the still-suspended hook
#    task — leaves the underlying asyncio subprocess transport/pipe
#    reader in a state that later triggers an unclean
#    `BaseSubprocessTransport.__del__` at garbage-collection time
#    (logged as "Exception ignored in: ...", `RuntimeError: Event
#    loop is closed`) — harmless in production but a real, confirmed
#    leftover from the pipe never being closed synchronously.
#
# `_POST_KILL_REAP_TIMEOUT_SECONDS` bounds case 1; explicit transport
# closing (`_close_transport_quietly`) in `_run_hook_command_inner`'s
# cancellation path fixes case 2. 1.5s is generous for a normal
# OS-level kill+reap (typically sub-millisecond) while still being
# negligible against a CLI command's overall runtime.
_POST_KILL_REAP_TIMEOUT_SECONDS = 1.5

# `_OUTPUT_TRUNCATE_BYTES` is deliberately much smaller than the bash
# tool's `_OUTPUT_TRUNCATE_BYTES = 64 * 1024` (`bash.py:54`,
# `builtins.py:101`). Bash-tool output is fed back to the model as
# part of the tool-call contract, so it needs a real inline-preview
# budget with disk-overflow persistence. Hook output has no such
# consumer — v1 only logs a byte count on non-zero exit for
# diagnostics (see `run_hook_command`) — so there is no reason to
# retain kilobytes of it. 4 KiB is enough to show a meaningful prefix
# of a failing hook's stderr in a log line without holding onto (or
# ever persisting to disk) a large blob for output nobody reads.
_OUTPUT_TRUNCATE_BYTES = 4 * 1024

# Wire-kinds whose "subject" is the tool/action name carried in the
# payload's `action` key (spec-131 Matching table). `kolsch.prompt.requested`
# always carries `action` — it's merged from `InlineApprovalCallback`'s
# `metadata` dict (`src/cli/chat/approval.py:106-110`) into the
# `WIRE_PROMPT_REQUESTED` payload by `PromptsImpl.choice`
# (`src/cli/chat/services/prompts_impl.py:247-264`).
#
# `kolsch.prompt.answered` does NOT always carry `action` — the real
# answered-payload shapes (`PromptsImpl._emit_auto_deny`,
# `src/cli/chat/services/prompts_impl.py:590-596`, and the widget-bridge
# path at `src/cli/chat/event_adapter.py:2341-2360`) only ever include
# `prompt_id`, `response`, and an optional `rule` — never `action`. The
# subject extraction below tolerates this by falling back to `"*"` when
# `action` is absent, which keeps `kolsch.prompt.answered` matchable by
# a wildcard rule and by literal-tool rules on any future/legacy caller
# that does forward `action`.
_ACTION_SUBJECT_KINDS = frozenset(
    {
        "kolsch.prompt.requested",
        "kolsch.prompt.answered",
    }
)

# Wire-kind/event-type values whose subject is always the wildcard —
# there's no natural per-event tool/action to key on (spec-131 Matching
# table).
_WILDCARD_SUBJECT_TYPES = frozenset(
    {
        "thread.exchange_completed",
        "thread.exchange_failed",
        "session.opened",
    }
)

_WILDCARD_SUBJECT = "*"


def match(rule: HookRule, event: Event) -> bool:
    """Return `True` iff `event` fires `rule`.

    Compares `event.type` against `rule.event` — using the nested
    wire-kind string (`event.data["kind"]`) instead of the generic
    `"ui.event"` envelope type when `event.type is EventType.UI_EVENT`.
    When the event type matches, computes the per-event-type subject
    (see `_subject_for`) and checks it against `rule.matcher` via
    `fnmatch.fnmatchcase`.

    Args:
        rule: One `platform.yaml` `hooks:` entry.
        event: A real session event (or a fabricated one in tests)
            to test against `rule`.

    Returns:
        `True` when both the event-type and subject-matcher checks
        pass; `False` otherwise.
    """
    event_type_str = _event_type_string(event)
    if event_type_str != rule.event:
        return False
    subject = _subject_for(event_type_str, event)
    return fnmatch.fnmatchcase(subject, rule.matcher)


def _event_type_string(event: Event) -> str:
    """Return the string to compare against `rule.event`.

    For `EventType.UI_EVENT`, that's the nested wire-kind
    (`event.data["kind"]`) — e.g. `"kolsch.prompt.requested"` — per
    `UIEventData.kind` (`src/schemas/wire_kinds.py`). For every other
    `EventType`, it's the plain `event.type` value (already a `str`
    subclass via `StrEnum`).
    """
    if event.type is EventType.UI_EVENT:
        kind = event.data.get("kind")
        return kind if isinstance(kind, str) else ""
    return str(event.type)


def _subject_for(event_type_str: str, event: Event) -> str:
    """Compute the per-event "subject" matched against `rule.matcher`.

    Per spec-131's Matching table:

    - `kolsch.prompt.requested` / `kolsch.prompt.answered` — the
      tool/action name from the prompt's payload (`data["payload"]["action"]`),
      falling back to `"*"` when absent (see the `_ACTION_SUBJECT_KINDS`
      module comment for why `kolsch.prompt.answered` often lacks it).
    - `thread.exchange_completed` / `thread.exchange_failed` /
      `session.opened` — always `"*"` (no natural subject).
    - `tool.failed` — the failing tool's name (`data["tool_name"]`,
      `ToolFailedData.tool_name`).
    - Anything else (future/undocumented event types reaching this
      function after the type check already passed) — `"*"`, so a
      wildcard-matcher rule still fires generically per spec-131's
      "architecturally generic" design goal.
    """
    if event_type_str in _ACTION_SUBJECT_KINDS:
        payload = event.data.get("payload")
        action = payload.get("action") if isinstance(payload, dict) else None
        return action if isinstance(action, str) else _WILDCARD_SUBJECT
    if event_type_str in _WILDCARD_SUBJECT_TYPES:
        return _WILDCARD_SUBJECT
    if event_type_str == EventType.TOOL_FAILED:
        tool_name = event.data.get("tool_name")
        return tool_name if isinstance(tool_name, str) else _WILDCARD_SUBJECT
    return _WILDCARD_SUBJECT


# ---------------------------------------------------------------------------
# Subprocess execution primitive (Step 3)
# ---------------------------------------------------------------------------


async def run_hook_command(command: str, timeout_seconds: int) -> None:
    """Run `command` as a fire-and-forget hook subprocess. Never raises.

    Reuses the bash tool's subprocess-safety conventions verbatim
    rather than reinventing them — see spec-131's Technical Context
    "Bash-tool subprocess-safety conventions to mirror":

    - **Shell execution**: `asyncio.create_subprocess_shell`, same as
      both `bash.py:156-161`
      (`src/cli/_starter_bundle/kolsch/tools/bash.py`) and
      `builtins.py:711-716` (`make_bash_v2` in `src/tools/builtins.py`).
    - **No `env=` override**: neither bash-tool call site above passes
      `env=` to `create_subprocess_shell` — both inherit the full
      parent process environment unmodified. This function does the
      same, on purpose: a hook author may need `$PATH`/`$HOME` to
      reach `terminal-notifier`/`curl`/etc. This means a hook
      subprocess sees whatever secrets are in kolsch's own process
      env, same as the bash tool already does — an accepted,
      pre-existing posture (see spec-131 Technical Context), not a
      new risk this function introduces.
    - **Timeout + SIGINT-grace-then-SIGKILL escalation**: on timeout,
      sends SIGINT first and waits `_SIGINT_GRACE_SECONDS` (0.5s,
      matching `bash.py:42-51`'s `_SIGINT_GRACE_SECONDS`) for a
      well-behaved command to exit cleanly, then escalates to
      SIGKILL if it's still alive. `bash.py`/`builtins.py` reserve
      SIGINT-then-grace specifically for *cancellation* (Esc) and use
      a bare `proc.kill()` (immediate SIGKILL, no grace) on plain
      *timeout* (`bash.py:179-189`, `builtins.py:728-735`). Hook
      subprocesses have no equivalent "did the user just Esc" signal
      to distinguish, and a hook is explicitly a side-effect the
      engine isn't waiting on — so this function applies the gentler
      grace-then-kill escalation uniformly to its one failure mode
      (timeout), rather than reinventing a second bare-kill path.
    - **Output capture with truncation**: stdout/stderr are captured
      via `proc.communicate()` and truncated to `_OUTPUT_TRUNCATE_BYTES`
      before being included in a log line. Unlike the bash tool, this
      output is never returned to a caller or fed back to the model —
      it exists only for the diagnostic log message on failure — so
      it uses a smaller cap (4 KiB vs. the bash tool's 64 KiB) and no
      disk-overflow persistence. See the module-level
      `_OUTPUT_TRUNCATE_BYTES` comment for the full reasoning; this is
      a deliberately smaller, simpler *replacement* for the bash
      tool's `_persist_or_inline` helper
      (`src/tools/builtins.py:298-341` /
      `bash.py`-equivalent), not a reuse of it — that helper's whole
      purpose is disk persistence for a model-facing contract, which
      doesn't apply here.
    - **Guaranteed reaping**: a `finally` block kills and awaits the
      process if it's somehow still alive when this function returns,
      matching `bash.py:194-200` / `builtins.py:749-756`'s
      "ensure the subprocess is reaped on every path" `finally` block.

    This function is the callee, not the caller, of any future
    dispatcher (Step 4): it does not know about `HookRule`, `match()`,
    or kolsch events at all, and nothing in `platform_runtime.hooks`
    calls it yet. It **must never raise** — every failure mode (bad
    command, non-zero exit, timeout, subprocess spawn failure) is
    caught here and logged via the module logger, because a future
    fire-and-forget dispatcher (Step 4) cannot afford a hook's failure
    to propagate into and break the caller's turn.

    Args:
        command: Shell command string, passed to
            `asyncio.create_subprocess_shell` exactly as given (same
            quoting/pipeline semantics as the bash tool).
        timeout_seconds: Maximum seconds to allow the command to run
            before killing it. Callers are expected to pass
            `HookRule.timeout_seconds` (Step 1), which is itself
            bounded to `_HOOK_TIMEOUT_CEILING_SECONDS` (60s) — this
            function does not re-validate the bound, it just honors
            whatever value it's given.
    """
    try:
        await _run_hook_command_inner(command, timeout_seconds)
    except Exception:  # noqa: BLE001 — top-level fire-and-forget guard; must never raise
        _log.exception("hook command raised unexpectedly: %r", command)


async def _run_hook_command_inner(command: str, timeout_seconds: int) -> None:
    """Do the actual subprocess work `run_hook_command` guards.

    Split out so the outer function's `except Exception` is a single,
    obviously-total backstop rather than being interleaved with the
    specific `TimeoutError` / `OSError` handling below — mirrors the
    bash tool's nested try/except shape
    (`bash.py:155-200`/`builtins.py:710-756`) while keeping the "this
    must never raise" contract enforced in exactly one place.
    """
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        _log.exception("hook command failed to start: %r", command)
        return

    timed_out = False
    try:
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except TimeoutError:
            timed_out = True
            stdout_b, stderr_b = await _kill_with_grace(proc)
        except OSError:
            _log.exception("hook command communicate() failed: %r", command)
            return
        except asyncio.CancelledError:
            # spec-131 Step 6 review fix (case 2 — see
            # `_POST_KILL_REAP_TIMEOUT_SECONDS`'s module comment): this
            # branch fires when `drain_background_hook_tasks`'s grace
            # window expired and `asyncio.run()`'s own shutdown
            # cancelled the still-suspended `communicate()` call. The
            # DIRECT child may already have exited on its own
            # (`proc.returncode` already set — no kill needed at all),
            # but the cancelled `communicate()` leaves the transport's
            # internal pipe-reader state dangling; explicitly closing
            # the transport here (not just relying on `finally`'s
            # kill/reap guard below, which only fires when
            # `proc.returncode is None`) avoids an unclean
            # `BaseSubprocessTransport.__del__` later at GC time.
            # Re-raised afterward — cancellation must still propagate.
            _close_transport_quietly(proc)
            raise
    finally:
        # Ensure the subprocess is reaped on every path, matching
        # bash.py:194-200 / builtins.py:749-756. Bounded via
        # `_reap_after_kill` (spec-131 Step 6 review fix) — a bare
        # `proc.wait()` here can block indefinitely if the hook's
        # shell command spawned a grandchild still holding the
        # stdout/stderr pipes open; see `_POST_KILL_REAP_TIMEOUT_SECONDS`.
        if proc.returncode is None:
            try:
                proc.kill()
                await _reap_after_kill(proc)
            except (ProcessLookupError, OSError):
                pass

    if timed_out:
        _log.warning("hook command timed out after %ss: %r", timeout_seconds, command)
        return

    if proc.returncode != 0:
        _log.warning(
            "hook command exited %s: %r\nstdout: %s\nstderr: %s",
            proc.returncode,
            command,
            _truncate_for_log(stdout_b),
            _truncate_for_log(stderr_b),
        )


async def _kill_with_grace(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """Escalate SIGINT-grace-then-SIGKILL on a timed-out hook subprocess.

    Sends SIGINT and waits `_SIGINT_GRACE_SECONDS` for the process to
    exit on its own; if it's still alive after the grace window,
    escalates to SIGKILL. Mirrors `bash.py`'s cancellation escalation
    (`_nudge_subprocess_toward_exit` + the outer `finally`'s
    `proc.kill()`, `bash.py:279-295`), applied here to the timeout
    path since hook subprocesses have no separate cancellation signal
    (see `run_hook_command`'s docstring).

    Returns whatever partial output had buffered before the kill,
    swallowing `OSError` from `communicate()` the same way
    `bash.py:181-184` / `builtins.py:731-734` do (best-effort partial
    output; a failure here must not escalate past this timeout path).

    **spec-131 Step 6 review fix:** the final `proc.communicate()`
    call (collecting whatever output buffered before the kill) is
    bounded via `_reap_after_kill`, not called bare. A bare
    `communicate()` after `kill()` only guarantees the DIRECT child
    was signaled — if the hook's shell command spawned a grandchild
    that inherited the stdout/stderr pipes (`cmd & `, `cmd1 && cmd2`,
    a pipeline) and is still running, `communicate()` blocks reading
    those pipes until EOF, i.e. until the grandchild itself exits —
    which can be far longer than this function's own timeout-driven
    kill was supposed to bound. See
    `_POST_KILL_REAP_TIMEOUT_SECONDS`'s module-level comment for the
    full reasoning.
    """
    if proc.returncode is None:
        try:
            proc.send_signal(signal.SIGINT)
        except (ProcessLookupError, OSError):
            pass
        else:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_SIGINT_GRACE_SECONDS)

    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()

    return await _reap_after_kill(proc)


async def _reap_after_kill(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """Bounded post-kill reap: collect output, but never wait forever.

    **spec-131 Step 6 review fix (case 1 — see
    `_POST_KILL_REAP_TIMEOUT_SECONDS`'s module comment):** after
    `proc.kill()`, calling `proc.wait()` or `proc.communicate()`
    unbounded is NOT safe against every hook command shape. `SIGKILL`
    only terminates the process this `Process` handle refers to — a
    grandchild the hook's shell command spawned (background `&`, a
    `&&`/`;` chain, a pipeline) is unaffected and may still hold the
    stdout/stderr pipes open. Since `communicate()`/reading from those
    pipes only returns at EOF, the awaited call blocks until that
    unrelated grandchild exits on its own — which could be the hook's
    full `timeout_seconds` (up to the 60s ceiling) or longer,
    defeating both `run_hook_command`'s own timeout-kill contract AND
    `drain_background_hook_tasks`'s short process-exit grace window
    (Step 6). Confirmed via a minimal, kolsch-independent repro:
    `proc.communicate()` called after `proc.kill()` hangs indefinitely
    against a still-alive grandchild holding the pipe, regardless of
    the (already-dead) direct child.

    This function is the fix: it bounds the reap itself with a short
    `asyncio.wait_for(timeout=_POST_KILL_REAP_TIMEOUT_SECONDS)`. If
    even that expires, it gives up on collecting output/confirming a
    clean reap, closes the transport explicitly (see
    `_close_transport_quietly`), and returns `(b"", b"")` anyway —
    leaving the OS-level process technically un-`wait()`-ed is a
    normal, safe Unix pattern for a short-lived CLI process (the
    kernel reaps it once its own grandchildren eventually exit and it
    becomes fully detached, or `init`/the process's session leader
    inherits it); it is NOT a resource leak in any way that matters
    here, and is strictly better than blocking the entire process
    (and therefore the CLI command the user is waiting on) for up to a
    minute.

    Returns:
        Whatever stdout/stderr bytes were collected before either
        `communicate()` returning normally or the bound expiring;
        `(b"", b"")` on any `OSError` or on timeout.
    """
    try:
        return await asyncio.wait_for(
            proc.communicate(), timeout=_POST_KILL_REAP_TIMEOUT_SECONDS
        )
    except TimeoutError:
        _log.warning(
            "hook subprocess reap exceeded %.1fs after kill — a "
            "grandchild process may still be holding stdout/stderr "
            "open; giving up on a clean reap and continuing",
            _POST_KILL_REAP_TIMEOUT_SECONDS,
        )
        _close_transport_quietly(proc)
        return b"", b""
    except OSError:
        return b"", b""


def _close_transport_quietly(proc: asyncio.subprocess.Process) -> None:
    """Explicitly close `proc`'s underlying asyncio transport.

    **spec-131 Step 6 review fix (case 2 — see
    `_POST_KILL_REAP_TIMEOUT_SECONDS`'s module comment):** without
    this, a subprocess whose `communicate()`/reap call was cancelled
    or timed out mid-flight leaves its `Transport`/pipe objects open
    on the Python side. Those objects eventually get
    garbage-collected — possibly after the event loop itself has
    already closed (e.g. at `asyncio.run()`'s own shutdown, which is
    exactly when `drain_background_hook_tasks`'s cancellation path
    hits this) — which asyncio reports as "Exception ignored in:
    BaseSubprocessTransport.__del__" (`RuntimeError: Event loop is
    closed`). Harmless in production (a logged warning; nothing
    breaks; the OS-level process is unaffected either way — this is
    purely about the Python-side wrapper object's cleanup), but a
    strictly cleaner outcome than leaving it to `__del__`, and
    confirmed to matter for at least one downstream consumer (a
    pytest plugin that escalates the ignored exception into a hard
    test-session failure). Calling `.close()` here performs the same
    cleanup `__del__` would, just synchronously and on our own terms
    instead of at an unpredictable GC time against an already-closed
    loop.

    `_transport` is a private, undocumented attribute of
    `asyncio.subprocess.Process` (not in its public type stub, hence
    the `type: ignore`) — reading it to call the one cleanup method it
    exists to provide is a narrow, well-understood use of a private
    API, not a fragile reach into implementation detail we depend on
    for correctness (this call is a best-effort cleanup; every path
    that reaches it already accepts a `(b"", b"")` degraded result
    either way).
    """
    with contextlib.suppress(Exception):
        proc._transport.close()  # type: ignore[attr-defined]


def _truncate_for_log(data: bytes) -> str:
    """Decode `data` and truncate to `_OUTPUT_TRUNCATE_BYTES` for a log line."""
    text = data[:_OUTPUT_TRUNCATE_BYTES].decode("utf-8", errors="replace")
    overflow = len(data) - _OUTPUT_TRUNCATE_BYTES
    if overflow > 0:
        return f"{text}\n[... truncated {overflow} more bytes]"
    return text


# ---------------------------------------------------------------------------
# HookDispatcher (Step 4)
# ---------------------------------------------------------------------------

# The wire-kind `kolsch.prompt.answered` never actually carries an
# `action` field in its real payload shapes (see the
# `_ACTION_SUBJECT_KINDS` module comment above — `PromptsImpl._emit_auto_deny`
# and the widget-bridge path only ever emit `prompt_id`/`response`/`rule`).
# `_subject_for` already tolerates this by falling back to `"*"`, which
# means a rule on this event with any `matcher` other than the literal
# wildcard can structurally never match a real event — a config author
# who writes one almost certainly meant `kolsch.prompt.requested`
# instead, or intended `"*"`. This was flagged as a Step 2 review
# recommendation; `HookDispatcher.__init__` is the right place to warn
# about it once, at construction/startup time, rather than re-deriving
# the same verdict on every event.
_PROMPT_ANSWERED_EVENT = "kolsch.prompt.answered"


class HookDispatcher:
    """Fire-and-forget dispatcher: matches events against rules, runs hits.

    Constructed once with the fully-resolved list of `HookRule`s (Step
    1's config schema — real `platform.yaml` loading is Step 6's job,
    not this class's). `handle()` is the single entry point: it is
    `await`-ed by whatever feeds it events (a spy in this step's tests,
    the engine's observer seam from Step 5 onward), but it never itself
    awaits a hook subprocess to completion — every matching rule's
    command is launched via `asyncio.create_task` and left to run in
    the background, so `handle()` returns as soon as the (fast, in-
    process) matching pass is done, independent of how slow or hung any
    fired command is.

    This class does not track or await the background tasks it creates
    (true fire-and-forget) — a hook's outcome is only ever visible via
    `run_hook_command`'s own logging, never fed back to the caller of
    `handle()`.
    """

    def __init__(self, rules: list[HookRule]) -> None:
        """Store `rules` and emit the one-time prompt.answered warning.

        Args:
            rules: The fully-resolved hook rules to dispatch against,
                in the order they should be evaluated. An empty list
                means `handle()` is a no-op for every event (matches
                the "absent hooks section = zero hooks" convention).
        """
        self._rules = rules
        self._warn_unmatchable_prompt_answered_rules()

    def _warn_unmatchable_prompt_answered_rules(self) -> None:
        """Log once, at construction, about structurally dead rules.

        A rule targeting `kolsch.prompt.answered` with a `matcher`
        other than `"*"` can never match a real event, because that
        event's real payload shapes never carry the `action` field the
        matcher would be compared against (see the module-level
        `_PROMPT_ANSWERED_EVENT` comment). This is config feedback,
        not an error — the rule is still accepted and kept in
        `self._rules` (Step 1 already validated its shape; this isn't
        re-litigating that), it just will never fire in practice.
        """
        for rule in self._rules:
            targets_prompt_answered = rule.event == _PROMPT_ANSWERED_EVENT
            has_non_wildcard_matcher = rule.matcher != _WILDCARD_SUBJECT
            if targets_prompt_answered and has_non_wildcard_matcher:
                _log.warning(
                    "hook rule targets %r with matcher %r, but this event's "
                    "real payload never carries an 'action' field to match "
                    'against — this rule can never fire. Use matcher: "*", '
                    "or did you mean event: %r?",
                    _PROMPT_ANSWERED_EVENT,
                    rule.matcher,
                    "kolsch.prompt.requested",
                )

    async def handle(self, event: Event) -> None:
        """Dispatch `event` to every matching rule's command, then return.

        For each rule in construction order, calls `match(rule, event)`
        and — for every match — launches `run_hook_command` as a
        background `asyncio.create_task`, without awaiting it. This
        method itself only awaits the (synchronous, fast) matching
        pass, never a subprocess, so it returns promptly regardless of
        how long any fired command takes to run (bounded at
        `HookRule.timeout_seconds`, up to the 60s ceiling, on the
        background task's own time — never on this call's).

        Args:
            event: The event to test against every configured rule.

        Note:
            Requires a running event loop — `asyncio.create_task`
            raises `RuntimeError` if called outside one. Not a concern
            for how this class is actually driven (Step 5 onward calls
            it from inside `AgentEngine.execute_stream`, an async
            generator that can only run under a live loop); documented
            here for anyone constructing/calling it standalone.
        """
        for rule in self._rules:
            if match(rule, event):
                task = asyncio.create_task(
                    run_hook_command(rule.command, rule.timeout_seconds)
                )
                # Keep a reference only long enough to suppress
                # "task was never retained" warnings/GC surprises;
                # deliberately not awaited or stored beyond that — see
                # the class docstring's fire-and-forget contract.
                _BACKGROUND_TASKS.add(task)
                task.add_done_callback(_BACKGROUND_TASKS.discard)
                task.add_done_callback(_log_background_task_exception)

    async def observe(self, event: Event) -> None:
        """`EventObserver`-Protocol-shaped alias for `handle` (Step 6).

        `AgentEngine`'s constructor accepts an `observer:
        EventObserver | None` (`src/engine/event_observer.py`, Step
        5), a structural `Protocol` whose one method is
        `async def observe(self, event) -> None`. `HookDispatcher`'s
        own public method is `handle` — named to match Claude Code's
        hook-dispatch vocabulary rather than the engine's observer
        vocabulary, since this class is usable standalone (Steps 2-4's
        tests feed it events directly, with no engine involved). This
        thin alias is what lets a `HookDispatcher` instance be passed
        directly as `AgentEngine(..., observer=dispatcher)` (Step 6's
        real construction sites) without a separate wrapper class —
        it just forwards to `handle`, which already satisfies every
        `EventObserver` contract requirement (never raises, never
        blocks on subprocess completion, never mutates the event).
        """
        await self.handle(event)


def _log_background_task_exception(task: asyncio.Task[None]) -> None:
    """Defensively log any exception that escaped a fired hook task.

    `run_hook_command` (Step 3) is verified to never raise today, so
    this is a non-issue in practice — but relying on that staying true
    forever would make `handle()`'s "never breaks a turn, always
    logged" claim true only incidentally, not structurally. If a
    future edit to Step 3 (or anything else scheduled the same way)
    ever let an exception through, this done-callback is what turns it
    into a real, findable log line via kolsch's own logger instead of
    silently surfacing only through asyncio's default handler ("Task
    exception was never retrieved", printed to stderr at GC time,
    easy to miss and not routed through kolsch's logging at all).

    `CancelledError` is excluded deliberately: a cancelled background
    task is expected, unremarkable behavior (e.g. process shutdown),
    not a hook failure worth logging as one.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.exception("hook background task raised unexpectedly", exc_info=exc)


# Strong references to in-flight background hook tasks. `asyncio` only
# holds a *weak* reference to a task once nothing else references it,
# which can let a still-running task be garbage-collected out from
# under itself (a documented asyncio footgun). Retaining a strong
# reference here — pruned via each task's own done-callback — avoids
# that without turning `HookDispatcher.handle()` into something that
# awaits or otherwise tracks completion; this set is purely about
# object lifetime, not synchronization.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

# spec-131 Step 6 — default grace window for `drain_background_hook_tasks`.
# Deliberately short: this only exists to close the short-lived-process
# race documented on that function, not to make a slow hook command
# block process exit for a long time.
_DRAIN_TIMEOUT_SECONDS = 2.0


async def drain_background_hook_tasks(
    timeout_seconds: float = _DRAIN_TIMEOUT_SECONDS,
) -> None:
    """Best-effort wait for in-flight fire-and-forget hook tasks to finish.

    **Why this exists (spec-131 Step 6 finding, not anticipated by the
    original spec):** `HookDispatcher.handle()` (Step 4) is deliberately
    fire-and-forget — it launches each matching hook's command via
    `asyncio.create_task` and returns without awaiting it, so a slow or
    hung hook command never delays the engine's own event stream. That
    contract is correct and unchanged. But it interacts badly with a
    **short-lived process** like `kolsch run` (`kolsch chat` and the
    HTTP API stay alive for many exchanges, so this race never matters
    for them): if the process's `asyncio.run()` call returns — and
    therefore starts tearing down the event loop — while a hook's
    `run_hook_command` task is still suspended inside
    `proc.communicate()`, cancelling that task during
    `asyncio.run()`'s shutdown sequence does not reliably deliver
    `CancelledError` on this platform (confirmed empirically: a
    minimal, kolsch-independent repro — spawn a subprocess inside an
    unawaited `asyncio.create_task`, return from `main()` immediately —
    hangs the process indefinitely on macOS + CPython 3.13's default
    child-watcher/event-loop-shutdown interaction, 100% reproducible
    across repeated runs). The task is never cancelled, never
    completes, and `asyncio.run()` never returns, so the whole `kolsch
    run` process wedges forever instead of exiting after printing its
    result.

    The fix is NOT inside `run_hook_command` or `HookDispatcher` — both
    behave correctly; cancellation delivery during interpreter/loop
    shutdown is out of kolsch's control. Instead,
    `src/cli/main.py`'s `_run_handler_and_drain_hooks` wraps EVERY CLI
    subcommand at the single dispatch choke point (`main`'s
    `asyncio.run(...)` call) and calls this function once, right
    before returning, to let any hook tasks fired during that command
    ACTUALLY FINISH (not be cancelled) within a short bounded window —
    closing the race by avoiding it, not by fixing cancellation
    semantics this module doesn't control. This covers `kolsch run`
    (where the race was first found) and every other short-lived
    headless command (`kolsch workflows run`/`resume`, etc.) uniformly;
    `kolsch chat` is unaffected in practice (bug-116's
    `force_process_exit` hard-exits before this coroutine would return
    normally, and a long-lived session gives any hook task ample real
    time to finish on its own well before that).

    Uses `asyncio.wait` (not `asyncio.gather`/`wait_for`) specifically
    because `wait` does not cancel outstanding tasks on timeout — it
    just returns the `(done, pending)` split and leaves `pending`
    running. If the grace window expires with a hook task still
    pending, this function simply returns; `main()`'s `finally` then
    returns too, and `asyncio.run()`'s own shutdown sequence cancels
    that still-running task.

    **Second review-caught defect, now fixed at its own root cause
    (`_reap_after_kill`/`_close_transport_quietly`, not here):** that
    shutdown-driven cancellation used to be able to reintroduce an
    unbounded wait of its own — the cancelled task's cleanup path
    (`_run_hook_command_inner`) kills the subprocess (if still alive)
    and then reaps it, and a BARE reap
    (`proc.wait()`/`proc.communicate()`) blocks until the process's
    stdout/stderr pipes hit EOF, which does not happen until every
    process holding the write end exits — including a grandchild the
    hook's shell command spawned (`cmd &`, `cmd1 && cmd2`, a pipeline)
    that SIGKILL to the direct child never touches. That could add up
    to the hook's own `timeout_seconds` (60s ceiling) to process exit,
    regardless of THIS function's `timeout_seconds`.
    `_reap_after_kill` now bounds that reap independently
    (`_POST_KILL_REAP_TIMEOUT_SECONDS`, 1.5s), and
    `_close_transport_quietly` ensures the transport is closed
    cleanly even when the direct child had already exited on its own
    (the cancellation can land there too, with nothing left to kill —
    see that function's docstring). So the total added-to-process-exit
    time this function is responsible for is now genuinely
    `timeout_seconds` (this drain's own wait) plus at most
    `_POST_KILL_REAP_TIMEOUT_SECONDS` (the one bounded reap on the
    cancellation path) — a small, fixed ceiling regardless of how long
    the hook command (or anything it spawns) actually runs, not a
    ceiling that tracks the hook's duration.

    Args:
        timeout_seconds: How long to wait for outstanding hook tasks
            before giving up. Defaults to a short, fixed grace window —
            not user-configurable via `platform.yaml`, since this is
            an internal process-exit safety net, not a documented hook
            behavior.
    """
    pending = {task for task in _BACKGROUND_TASKS if not task.done()}
    if not pending:
        return
    await asyncio.wait(pending, timeout=timeout_seconds)
