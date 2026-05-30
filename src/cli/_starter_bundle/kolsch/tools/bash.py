"""``bash`` — run a shell command in the workspace, timed + truncated.

This file IS the tool. See ``read_file.py`` for the
file-IS-the-tool rationale (TOOL-R10 hard rule).

Returns ``{exit_code, stdout, stderr, duration_seconds, timed_out}``.
Streams are truncated to 64 KiB each so a chatty command can't blow
the context budget. The ``cwd`` argument, when supplied, is
policy-checked the same way file tools' ``path`` is.

Bash itself is exempt from the path-access policy in v1's native
sandbox per ``docs/explanation/file-handling.md`` §4.7. The
workspace-only default rule set in ``platform.yaml`` defaults
``tool.bash`` to ``prompt`` so the chat surface (Phase 2.1) gates
each command interactively rather than the path policy trying — and
failing — to constrain the shell.

Spec: docs/process/phase2.0-tools-specs.md §1.4.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel, Field

from errors import ToolExecutionFailed, ToolTimeout
from files import check_path_allowed
from tools.decorator import ToolContext, tool

_DEFAULT_TIMEOUT_SECONDS = 60
_OUTPUT_TRUNCATE_BYTES = 64 * 1024  # 64 KiB per stream


class BashArgs(BaseModel):
    command: str
    cwd: str | None = None
    timeout_seconds: int = Field(default=_DEFAULT_TIMEOUT_SECONDS, ge=1)


@tool(
    name="bash",
    description="Run a shell command in the workspace (timed, truncated).",
    args_schema=BashArgs,
)
async def bash(args: BashArgs, ctx: ToolContext) -> dict[str, Any]:
    """Execute ``args.command`` via the shell.

    :returns: ``{exit_code, stdout, stderr, duration_seconds, timed_out}``.
    :raises tool.timeout: command exceeded ``args.timeout_seconds``.
    :raises file.access_denied: ``args.cwd`` outside allowed scope.
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
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=args.timeout_seconds
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
    return {
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "stdout": _truncate(stdout_b),
        "stderr": _truncate(stderr_b),
        "duration_seconds": round(duration, 3),
        "timed_out": timed_out,
    }


def _truncate(data: bytes) -> str:
    """Decode + truncate captured output to 64 KiB."""
    if len(data) <= _OUTPUT_TRUNCATE_BYTES:
        return data.decode("utf-8", errors="replace")
    head = data[:_OUTPUT_TRUNCATE_BYTES].decode("utf-8", errors="replace")
    return head + f"\n[... truncated {len(data) - _OUTPUT_TRUNCATE_BYTES} bytes]"
