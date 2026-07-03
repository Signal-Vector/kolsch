"""`bash` — run a shell command in the workspace, timed + truncated.

This file IS the tool. See `read_file.py` for the file-IS-the-tool
rationale.

Returns `{exit_code, stdout, stderr, stdout_persisted_to,
stderr_persisted_to, duration_seconds, timed_out}`. Streams are
truncated to 64 KiB each for the inline preview so a chatty command
can't blow the context budget. When a stream OVERFLOWS that budget,
the full bytes are written to
`<workspace>/logs/tool-outputs/<session_id>/<call_id>.<stream>.txt`
and the path is returned in `*_persisted_to`; the model can then
call `read_file` against the persisted path with `offset` / `limit`
to drill in. The `cwd` argument, when supplied, is policy-checked
the same way file tools' `path` is.

Bash itself is exempt from the path-access policy in the native
sandbox — see
[file handling](../../../explanation/file-handling.md). The
workspace-only default rule set in `platform.yaml` defaults
`tool.bash` to `prompt` so the chat surface gates each command
interactively rather than the path policy trying — and failing — to
constrain the shell.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from errors import ExchangeCancelled, ToolExecutionFailed, ToolTimeout
from files import check_path_allowed
from tools.decorator import ToolContext, tool

_SIGINT_GRACE_SECONDS = 0.5
"""Seconds to wait after SIGINT before escalating to SIGKILL.

When Esc flips during a bash invocation, the tool sends SIGINT first
(so the subprocess can flush buffers / run cleanup) and waits this
long for it to exit. If still alive after the grace, it sends
SIGKILL. 500ms balances "well-behaved CLIs get a chance to clean
up" against "Esc-during-runaway bash visibly stops the chat within
a heartbeat."
"""

_DEFAULT_TIMEOUT_SECONDS = 60
_OUTPUT_TRUNCATE_BYTES = 64 * 1024  # 64 KiB per stream — preview budget


class BashArgs(BaseModel):
    """Argument schema for the `bash` tool."""

    command: str = Field(
        description=(
            "Bash command to execute. Single string passed to the "
            "shell, so quoting and pipelines work as you'd expect "
            "in a terminal. Examples: 'ls -la', 'git status', "
            'rg "build_llm" src/, npm test.'
        ),
    )
    cwd: str | None = Field(
        default=None,
        description=(
            "Working directory for the command. Workspace-relative "
            "or absolute (must resolve inside an allowed path). "
            "When omitted, runs in the workspace root."
        ),
    )
    timeout_seconds: int = Field(
        default=_DEFAULT_TIMEOUT_SECONDS,
        ge=1,
        description=(
            "Maximum seconds before the command is killed and "
            "tool.timeout is raised. Default 60. Increase for "
            "long-running builds; decrease for quick probes."
        ),
    )


@tool(
    name="bash",
    summary="Run a shell command in the workspace.",
    description=(
        "Execute a shell command in the workspace and return its "
        "combined stdout, stderr, and exit code. Use this tool any "
        "time the user asks you to RUN a command, EXECUTE something, "
        "check the system state, run tests, run a build, query git, "
        "or invoke an installed CLI (npm, uv, docker, etc.). When "
        "the user says 'run X', 'execute X', or quotes a literal "
        "shell command, this is the tool to call — do not narrate "
        "or hallucinate the output. "
        "\n\n"
        "DO NOT use this tool for file operations: use ``read_file`` "
        "to read, ``write_file`` to write, ``list_files`` to list a "
        "directory. Using ``cat`` / ``echo > file`` / ``ls`` via "
        "bash bypasses the platform's path-policy enforcement and "
        "loses the structured-return contract the file tools provide. "
        "\n\n"
        "File inspection (``cat``, ``head``, ``tail``, ``less``, "
        "``wc -l``) MUST go through ``read_file``, not bash. The bash "
        "tool returns file contents if you cat them, but the workspace "
        "path policy is bypassed and the output is truncated at 64 KiB "
        "without pagination. Use ``read_file`` with offset/limit instead. "
        "\n\n"
        "When stdout or stderr exceeds 64 KiB, the full output is "
        "written to a workspace tmp path (returned as "
        "``stdout_persisted_to`` / ``stderr_persisted_to``). Call "
        "``read_file`` on that path to drill in — the inline preview "
        "is the first 64 KiB only. "
        "\n\n"
        "Returns ``{exit_code: int, stdout: str, stderr: str, "
        "stdout_persisted_to: str | None, stderr_persisted_to: str | "
        "None, duration_seconds: float, timed_out: bool}``. When a "
        "stream exceeds 64 KiB, the inline string holds the first "
        "64 KiB plus a footer naming the persisted path; the "
        "corresponding ``*_persisted_to`` field carries that path. "
        "Commands exceeding ``timeout_seconds`` are killed and raise "
        "``tool.timeout``."
    ),
    args_schema=BashArgs,
)
async def bash(args: BashArgs, ctx: ToolContext) -> dict[str, Any]:
    """Execute `args.command` via the shell.

    Returns:
        `{exit_code, stdout, stderr, stdout_persisted_to,
        stderr_persisted_to, duration_seconds, timed_out}`.

    Raises:
        ToolTimeout: Command exceeded `args.timeout_seconds`.
        FileAccessDenied: `args.cwd` outside allowed scope.
    """
    cwd = args.cwd
    if cwd is not None:
        if ctx.path_policy is None or ctx.workspace_root is None:
            msg = (
                "bash with cwd requires the platform to populate "
                "ctx.path_policy and ctx.workspace_root."
            )
            raise ToolExecutionFailed(
                tool_name="bash", error_class="RuntimeError"
            ) from RuntimeError(msg)
        check_path_allowed(cwd, policy=ctx.path_policy, root=ctx.workspace_root)

    workdir = cwd or (str(ctx.workspace_root) if ctx.workspace_root else None)
    started = time.monotonic()
    timed_out = False
    try:
        proc = await asyncio.create_subprocess_shell(
            args.command,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ToolExecutionFailed(
            tool_name="bash", error_class=type(exc).__name__
        ) from exc
    try:
        try:
            # Race the subprocess communicate() against the exchange's
            # CancelToken so Esc during a long-running command
            # interrupts within ~one event-loop tick of the flip
            # (instead of waiting for the command to finish or hit
            # its timeout). The subprocess receives SIGINT then
            # escalates to SIGKILL after a 500ms grace; the tool
            # re-raises ExchangeCancelled (Fatal) so the engine
            # surfaces it as ``exchange.cancelled``.
            stdout_b, stderr_b = await _communicate_with_cancel(
                proc, ctx, timeout_seconds=args.timeout_seconds
            )
        except TimeoutError as exc:
            proc.kill()
            try:
                stdout_b, stderr_b = await proc.communicate()
            except OSError:
                stdout_b, stderr_b = b"", b""
            timed_out = True
            _ = stdout_b, stderr_b
            raise ToolTimeout(
                tool_name="bash", timeout_seconds=args.timeout_seconds
            ) from exc
        except OSError as exc:
            raise ToolExecutionFailed(
                tool_name="bash", error_class=type(exc).__name__
            ) from exc
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
    duration = time.monotonic() - started

    stdout_text, stdout_persisted = _persist_or_inline(stdout_b, ctx, "stdout")
    stderr_text, stderr_persisted = _persist_or_inline(stderr_b, ctx, "stderr")

    return {
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_persisted_to": stdout_persisted,
        "stderr_persisted_to": stderr_persisted,
        "duration_seconds": round(duration, 3),
        "timed_out": timed_out,
    }


async def _communicate_with_cancel(
    proc: asyncio.subprocess.Process,
    ctx: ToolContext,
    *,
    timeout_seconds: int,
) -> tuple[bytes, bytes]:
    """Race `proc.communicate()` against cancellation and timeout.

    When `ctx.cancellation` is None (programmatic test paths), falls
    through to the plain `asyncio.wait_for(proc.communicate(),
    timeout=...)` shape so existing callers see no behaviour change.

    When `ctx.cancellation` is set, races three completions
    concurrently:

    - `proc.communicate()` — the natural completion.
    - `ctx.cancellation.wait_cancelled()` — Esc was pressed.
    - `asyncio.sleep(timeout_seconds)` — the per-call timeout
      budget.

    On cancellation: sends SIGINT to the subprocess, waits up to
    `_SIGINT_GRACE_SECONDS` for it to exit, escalates to SIGKILL if
    still alive, then raises `ExchangeCancelled`. On timeout:
    re-raises `TimeoutError` so the outer handler emits `ToolTimeout`.
    """
    if ctx.cancellation is None:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)

    communicate_task = asyncio.ensure_future(proc.communicate())
    cancel_task = asyncio.ensure_future(ctx.cancellation.wait_cancelled())
    timeout_task = asyncio.ensure_future(asyncio.sleep(timeout_seconds))
    try:
        done, pending = await asyncio.wait(
            {communicate_task, cancel_task, timeout_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        for task in (communicate_task, cancel_task, timeout_task):
            task.cancel()
        raise

    # Clean up the losers so they don't leak.
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    if communicate_task in done:
        return communicate_task.result()

    if timeout_task in done:
        raise TimeoutError

    # Cancellation won — interrupt the subprocess and raise.
    _nudge_subprocess_toward_exit(proc)
    raise ExchangeCancelled(
        cancelled_at_event_id="",
        where="kolsch.tools.bash._communicate_with_cancel",
    )


def _nudge_subprocess_toward_exit(proc: asyncio.subprocess.Process) -> None:
    """Send SIGINT to the subprocess — best-effort, synchronous.

    SIGKILL escalation lives in the outer `finally` block of `bash`
    (lines tagged with `proc.kill()`). Splitting the two stages this
    way lets the cancellation handler raise immediately without
    awaiting the SIGINT grace window: a well-behaved CLI gets the
    chance to handle the signal cleanly while the outer sweep
    guarantees the subprocess is dead by the time the bash tool
    returns.
    """
    if proc.returncode is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
    except (ProcessLookupError, OSError):
        return


def _persist_or_inline(
    data: bytes, ctx: ToolContext, stream: str
) -> tuple[str, str | None]:
    """Decode and return an inline preview, persisting overflow to disk.

    Returns `(inline_text, persisted_path_str_or_None)`:

    - When `len(data) <= 64 KiB`: `(decoded_text, None)`.
    - When `len(data) > 64 KiB`: writes `data` to
      `<workspace>/logs/tool-outputs/<session_id>/<call_id>.<stream>.txt`
      and returns `(preview_64KiB + footer, str(path))`. The footer
      names the persisted path, byte-overflow size, and the
      `read_file` handoff.

    When the workspace root or session is unavailable (e.g., a test
    constructing a bare `ToolContext`), the full stream still needs a
    recovery path, so the function falls back to a
    `<cwd>/.tool-outputs/` tmp dir. Practically never hits in real
    engine use because the dispatcher populates both.
    """
    if len(data) <= _OUTPUT_TRUNCATE_BYTES:
        return data.decode("utf-8", errors="replace"), None

    path = _persisted_output_path(ctx, stream)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(data)
    except OSError:
        # Persistence is best-effort — if the filesystem refuses
        # we fall back to the old behaviour (truncate + report
        # overflow byte count) so the model still gets a useful
        # signal rather than a tool failure.
        preview = data[:_OUTPUT_TRUNCATE_BYTES].decode("utf-8", errors="replace")
        overflow = len(data) - _OUTPUT_TRUNCATE_BYTES
        footer = f"\n[... truncated {overflow} bytes (persistence failed)]"
        return preview + footer, None

    preview = data[:_OUTPUT_TRUNCATE_BYTES].decode("utf-8", errors="replace")
    overflow = len(data) - _OUTPUT_TRUNCATE_BYTES
    footer = (
        f"\n[... full output at {path}; {overflow} more bytes; "
        f"use read_file with offset/limit]"
    )
    return preview + footer, str(path)


def _persisted_output_path(ctx: ToolContext, stream: str) -> Path:
    """Return the workspace path where the full stream will land.

    Layout:
    `<workspace>/logs/tool-outputs/<session_id>/<call_id>.<stream>.txt`

    The `logs/` prefix is in the default `file_access.allowed_paths`
    so a subsequent `read_file` against the returned path passes the
    policy gate. `.platform/` was the obvious choice but is DENIED by
    default, defeating the handoff.

    When `ctx.tool_call_id` is missing (out-of-engine invocations), a
    stable `tc_anon_<uuid4>` is synthesized so concurrent calls don't
    collide. When `ctx.workspace_root` is missing, the path resolves
    under the current working directory's `.tool-outputs/` so the
    persisted file is at least addressable — bare-context tests still
    exercise the persistence shape.
    """
    call_id = ctx.tool_call_id or f"tc_anon_{uuid4()}"
    session = str(ctx.session_id) if ctx.session_id else "anon"
    if ctx.workspace_root is not None:
        return (
            Path(ctx.workspace_root)
            / "logs"
            / "tool-outputs"
            / session
            / f"{call_id}.{stream}.txt"
        )
    return Path(".tool-outputs") / session / f"{call_id}.{stream}.txt"
