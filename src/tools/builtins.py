"""The four built-in tools — spec §4.

``read`` / ``write`` / ``edit`` consult :func:`files.check_path_allowed`
before touching the filesystem (Fh-I2). ``bash`` is EXEMPT in v1's
native sandbox per spec §4.7 + Fh-I6 — an honest documented
limitation. A future real-sandbox variant closes the gap; for now
users wanting strict path control either omit bash from the agent's
tool list or add a ``deny tool.bash`` authz rule.

Each builtin is a *factory* taking the platform root + path policy
because the tool function closes over them. The factory returns the
``@tool``-decorated function; callers register the resulting function
with the Tools subsystem.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from errors import ToolExecutionFailed
from files import check_path_allowed
from tools.decorator import tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from files import PathPolicy
    from tools.decorator import ToolContext


# ---------------------------------------------------------------------------
# Argument models — spec §4.1-4.4
# ---------------------------------------------------------------------------


class ReadArgs(BaseModel):
    path: str
    encoding: str = "utf-8"


class WriteArgs(BaseModel):
    path: str
    content: str
    create_parents: bool = True
    encoding: str = "utf-8"


class EditArgs(BaseModel):
    path: str
    old_str: str
    new_str: str
    occurrence: int = 1


class BashArgs(BaseModel):
    command: str
    cwd: str | None = None
    timeout_seconds: int = Field(default=60, ge=1)


# ---------------------------------------------------------------------------
# Factories — each returns a @tool-decorated coroutine closing over root + policy
# ---------------------------------------------------------------------------


def make_read(*, policy: PathPolicy, root: Path) -> Callable[..., Awaitable[Any]]:
    @tool(
        name="read",
        description="Read the contents of a file from disk.",
        args_schema=ReadArgs,
    )
    async def read(args: ReadArgs, ctx: ToolContext) -> dict[str, str]:  # noqa: ARG001
        check_path_allowed(args.path, policy=policy, root=root)
        target = _resolve(args.path, root)
        try:
            content = target.read_text(encoding=args.encoding)
        except OSError as exc:
            # Surface as a Diagnostic so the model sees the failure and
            # can react (it isn't the platform's job to make this fatal).
            raise ToolExecutionFailed(
                tool_name="read",
                error_class=type(exc).__name__,
            ) from exc
        return {"content": content}

    return read


def make_write(*, policy: PathPolicy, root: Path) -> Callable[..., Awaitable[Any]]:
    @tool(
        name="write",
        description=("Write text content to a file on disk, overwriting if it exists."),
        args_schema=WriteArgs,
    )
    async def write(args: WriteArgs, ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
        check_path_allowed(args.path, policy=policy, root=root)
        target = _resolve(args.path, root)
        if args.create_parents:
            target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_text(args.content, encoding=args.encoding)
        except OSError as exc:
            raise ToolExecutionFailed(
                tool_name="write",
                error_class=type(exc).__name__,
            ) from exc
        return {"bytes_written": len(args.content), "path": args.path}

    return write


def make_edit(*, policy: PathPolicy, root: Path) -> Callable[..., Awaitable[Any]]:
    @tool(
        name="edit",
        description=("Edit a file by replacing a specific string with new content."),
        args_schema=EditArgs,
    )
    async def edit(args: EditArgs, ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
        check_path_allowed(args.path, policy=policy, root=root)
        target = _resolve(args.path, root)
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionFailed(
                tool_name="edit",
                error_class=type(exc).__name__,
            ) from exc

        if args.occurrence == 0:
            new_text = text.replace(args.old_str, args.new_str)
            replacements = text.count(args.old_str)
        elif args.occurrence == 1:
            count = text.count(args.old_str)
            if count == 0:
                raise ToolExecutionFailed(
                    tool_name="edit",
                    error_class="NoMatchError",
                )
            if count > 1:
                # Spec §4.3 — fail loudly on ambiguity rather than
                # silently picking the first match.
                raise ToolExecutionFailed(
                    tool_name="edit",
                    error_class="MultipleMatchesError",
                )
            new_text = text.replace(args.old_str, args.new_str, 1)
            replacements = 1
        else:
            new_text, replacements = _replace_nth(
                text,
                args.old_str,
                args.new_str,
                args.occurrence,
            )

        try:
            target.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionFailed(
                tool_name="edit",
                error_class=type(exc).__name__,
            ) from exc
        return {"replacements_made": replacements, "path": args.path}

    return edit


def make_bash(*, policy: PathPolicy, root: Path) -> Callable[..., Awaitable[Any]]:  # noqa: ARG001
    """Spec §4.4 + §4.7 + Fh-I6 — bash is EXEMPT from the path policy
    in v1's native sandbox. The `policy` and `root` arguments are
    accepted (and ignored here) so the factory signature matches the
    other builtins; the documented gap is the whole point.
    """

    @tool(
        name="bash",
        description="Execute a shell command and return its output.",
        args_schema=BashArgs,
    )
    async def bash(args: BashArgs, ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
        try:
            proc = await asyncio.create_subprocess_shell(
                args.command,
                cwd=args.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=args.timeout_seconds,
                )
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise ToolExecutionFailed(
                    tool_name="bash",
                    error_class="TimeoutError",
                ) from exc
        except OSError as exc:
            raise ToolExecutionFailed(
                tool_name="bash",
                error_class=type(exc).__name__,
            ) from exc
        return {
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "exit_code": proc.returncode if proc.returncode is not None else -1,
        }

    return bash


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_BUILTIN_FACTORIES: dict[str, Callable[..., Callable[..., Awaitable[Any]]]] = {
    "read": make_read,
    "write": make_write,
    "edit": make_edit,
    "bash": make_bash,
}


def make_builtin(
    name: str,
    *,
    policy: PathPolicy,
    root: Path,
) -> Callable[..., Awaitable[Any]]:
    """Construct one of the four builtins by name — spec §3.1."""
    factory = _BUILTIN_FACTORIES.get(name)
    if factory is None:
        msg = f"unknown builtin tool {name!r}; available: {sorted(_BUILTIN_FACTORIES)}"
        raise ValueError(msg)
    return factory(policy=policy, root=root)


def builtin_names() -> set[str]:
    """The four names spec §4 enumerates."""
    return set(_BUILTIN_FACTORIES)


def _resolve(path: str, root: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate


def _replace_nth(
    text: str,
    old: str,
    new: str,
    n: int,
) -> tuple[str, int]:
    """Replace the Nth (1-indexed) occurrence of ``old`` with ``new``."""
    index = -1
    for _ in range(n):
        index = text.find(old, index + 1)
        if index == -1:
            raise ToolExecutionFailed(
                tool_name="edit",
                error_class="NoMatchError",
            )
    return text[:index] + new + text[index + len(old) :], 1
