"""Hook matcher + subprocess runner for shell-command hooks (spec-131).

This module holds the matching logic (`match`) and the subprocess
execution primitive (`run_hook_command`). No dispatcher, no engine
wiring lives here yet — those are later spec-131 steps (4:
`HookDispatcher`, 5/6: engine injection).

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
source citations. It is a **standalone execution primitive**: nothing
in this module calls it yet (no dispatcher, no matcher-to-runner
wiring) — that lands in Step 4.
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
    finally:
        # Ensure the subprocess is reaped on every path, matching
        # bash.py:194-200 / builtins.py:749-756.
        if proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
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

    try:
        return await proc.communicate()
    except OSError:
        return b"", b""


def _truncate_for_log(data: bytes) -> str:
    """Decode `data` and truncate to `_OUTPUT_TRUNCATE_BYTES` for a log line."""
    text = data[:_OUTPUT_TRUNCATE_BYTES].decode("utf-8", errors="replace")
    overflow = len(data) - _OUTPUT_TRUNCATE_BYTES
    if overflow > 0:
        return f"{text}\n[... truncated {overflow} more bytes]"
    return text
