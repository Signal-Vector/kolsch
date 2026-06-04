"""The built-in tools — Phase 1 spec §4 + Phase 2.0 patch §1.

Phase 1 set (``read`` / ``write`` / ``edit`` / ``bash``): consult
:func:`files.check_path_allowed` before touching the filesystem
(Fh-I2). ``bash`` is EXEMPT in v1's native sandbox per spec §4.7 +
Fh-I6 — an honest documented limitation. A future real-sandbox
variant closes the gap; for now users wanting strict path control
either omit bash from the agent's tool list or add a
``deny tool.bash`` authz rule.

Phase 2.0 set (``read_file`` / ``write_file`` / ``list_files`` /
``bash``): the canonical shipping builtins per Phase 2.0 patch §1.
``read_file`` pre-checks ``max_bytes`` via ``stat`` before opening
(context-budget guard); ``write_file`` is atomic via temp + rename;
``list_files`` exposes a glob with a ``max_entries`` cap;
``bash`` returns ``{exit_code, stdout, stderr, duration_seconds,
timed_out}`` with stdout/stderr truncated to 64 KiB each. The Phase
2.0 builtins raise the Error Model §4.16 codes (``file.access_denied``,
``file.too_large``, ``file.not_found``, ``tool.timeout``).

Each builtin is a *factory* taking the platform root + path policy
because the tool function closes over them. The factory returns the
``@tool``-decorated function; callers register the resulting function
with the Tools subsystem. The Phase 1 and Phase 2.0 sets coexist;
agents opt into either via their ``subsystems.tools.config.builtin``
list. Phase 2.0's `bash` factory supersedes Phase 1's — same action
name, more comprehensive return shape — but agents using the legacy
Phase 1 `bash` are unaffected until they migrate.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel, Field

from errors import (
    FileAccessDenied,
    FileLineTooLong,
    FileNotFound,
    ToolExecutionFailed,
    ToolTimeout,
)
from files import assert_writable_destination, check_path_allowed
from tools.decorator import tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from files import PathPolicy
    from tools.decorator import ToolContext


# Phase 2.0 tunables (patch §1.1, §1.4).
_DEFAULT_READ_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB — NEW MEANING: single-line cap
_DEFAULT_READ_LINE_LIMIT = 2000  # QoL: line-based pagination default
_DEFAULT_LIST_MAX_ENTRIES = 500
_DEFAULT_BASH_TIMEOUT_SECONDS = 60
_BASH_OUTPUT_TRUNCATE_BYTES = 64 * 1024  # 64 KiB per stream


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
        # Fh-I2 — path policy first.
        check_path_allowed(args.path, policy=policy, root=root)
        # Fh-I5 + Fh-I9 — refuse writes into the engine-only
        # content/ store, even when policy would otherwise allow
        # reads there. Two-step check: policy gates reads; this
        # gates writes.
        assert_writable_destination(args.path, root=root)
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
        # Fh-I2 + Fh-I5 — edit reads AND writes, so it needs both
        # gates. The write-side refusal of content/ matters even when
        # the path policy allows reads under content/.
        check_path_allowed(args.path, policy=policy, root=root)
        assert_writable_destination(args.path, root=root)
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
# Phase 2.0 (patch §1) — read_file / write_file / list_files / bash_v2
# ---------------------------------------------------------------------------


class ReadFileArgs(BaseModel):
    path: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=_DEFAULT_READ_LINE_LIMIT, ge=1)
    max_bytes: int = Field(default=_DEFAULT_READ_MAX_BYTES, ge=1)


class WriteFileArgs(BaseModel):
    path: str
    content: str
    create_parents: bool = True


class ListFilesArgs(BaseModel):
    path: str = "."
    glob: str = "*"
    max_entries: int = Field(default=_DEFAULT_LIST_MAX_ENTRIES, ge=1)
    include_hidden: bool = False


class BashV2Args(BaseModel):
    command: str
    cwd: str | None = None
    timeout_seconds: int = Field(default=_DEFAULT_BASH_TIMEOUT_SECONDS, ge=1)


def make_read_file(*, policy: PathPolicy, root: Path) -> Callable[..., Awaitable[Any]]:
    """Phase 2.0 §1.1 + QoL §9 — line-based read with soft truncation.

    Replaces the pre-QoL whole-file-bytes shape. New shape:

    - Args: ``path`` + ``offset`` (0-indexed line) + ``limit`` (max
      lines, default 2000) + ``max_bytes`` (single-line cap, default
      1 MiB).
    - Return: ``{content, lines_returned, total_lines, next_offset,
      truncated}`` — see ``process/qol-tool-output-truncation-and-
      persistence.md`` §9.5.
    - Whole-file size is no longer capped (``FileTooLarge`` is NOT
      raised here); only a single line exceeding ``max_bytes`` trips
      :class:`FileLineTooLong` (the binary-as-text guard).
    - Output is ``cat -n`` numbered so the model can reference line
      numbers in responses + follow-up calls.
    """

    @tool(
        name="read_file",
        description="Read a UTF-8 text file by line range (paginated).",
        args_schema=ReadFileArgs,
    )
    async def read_file(args: ReadFileArgs, ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
        check_path_allowed(args.path, policy=policy, root=root)
        target = _resolve(args.path, root)
        try:
            raw = target.read_bytes()
        except FileNotFoundError as exc:
            raise FileNotFound(path=args.path) from exc
        except OSError as exc:
            raise FileAccessDenied(
                path=args.path,
                reason=f"read failed: {type(exc).__name__}: {exc}",
            ) from exc
        return _paginate_read_file(
            raw,
            path=args.path,
            offset=args.offset,
            limit=args.limit,
            max_bytes=args.max_bytes,
        )

    return read_file


def _paginate_read_file(
    raw: bytes,
    *,
    path: str,
    offset: int,
    limit: int,
    max_bytes: int,
) -> dict[str, Any]:
    """Shared implementation between the builtin and (conceptually)
    the bundle copy. Returns the QoL §9.5 dict shape.

    Single-line byte cap is the only hard refusal — everything else
    soft-truncates.
    """
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total_lines = len(lines)

    for i, line in enumerate(lines):
        line_bytes = len(line.encode("utf-8"))
        if line_bytes > max_bytes:
            raise FileLineTooLong(
                path=path,
                line_number=i,
                line_byte_length=line_bytes,
                max_bytes=max_bytes,
            )

    slice_end = min(offset + limit, total_lines)
    selected = lines[offset:slice_end]
    truncated = slice_end < total_lines
    next_offset = slice_end if truncated else None
    width = max(1, len(str(total_lines)))
    rendered = "\n".join(
        f"{i + 1:>{width}}\t{line}" for i, line in enumerate(selected, start=offset)
    )
    content = rendered + "\n" if rendered else ""
    return {
        "content": content,
        "lines_returned": len(selected),
        "total_lines": total_lines,
        "next_offset": next_offset,
        "truncated": truncated,
    }


def make_write_file(*, policy: PathPolicy, root: Path) -> Callable[..., Awaitable[Any]]:
    """Phase 2.0 §1.2 — atomic full-overwrite write.

    Writes to ``<path>.tmp.<pid>`` then ``os.replace`` onto the
    destination so a SIGKILL between the temp write and the rename
    leaves the prior content intact (TC-TOOL-07). The atomicity
    discipline matches Phase 1.1's ``platform.yaml`` writes.
    """

    @tool(
        name="write_file",
        description="Write a UTF-8 text file to the workspace.",
        args_schema=WriteFileArgs,
    )
    async def write_file(args: WriteFileArgs, ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
        check_path_allowed(args.path, policy=policy, root=root)
        assert_writable_destination(args.path, root=root)
        target = _resolve(args.path, root)
        existed = target.exists()
        if args.create_parents:
            target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
        try:
            encoded = args.content.encode("utf-8")
            with temp.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)  # noqa: PTH105 (explicit os call for testability)
        except OSError as exc:
            # Best-effort cleanup of the temp file. If the temp itself
            # didn't get created (mkdir failed earlier) this is a
            # no-op.
            with contextlib.suppress(OSError):
                temp.unlink()
            raise ToolExecutionFailed(
                tool_name="write_file",
                error_class=type(exc).__name__,
            ) from exc
        return {
            "path": str(target),
            "bytes_written": len(encoded),
            "created": not existed,
        }

    return write_file


def make_list_files(*, policy: PathPolicy, root: Path) -> Callable[..., Awaitable[Any]]:
    """Phase 2.0 §1.3 — directory listing with optional recursive glob.

    Default glob is ``"*"`` (one-level); ``"**/*"`` walks recursively.
    ``max_entries`` caps the returned list to prevent runaway context
    spend on huge trees. ``include_hidden`` defaults to False to skip
    dotfiles (the conventional shell behaviour).
    """

    @tool(
        name="list_files",
        description="List entries under a directory (non-recursive by default).",
        args_schema=ListFilesArgs,
    )
    async def list_files(args: ListFilesArgs, ctx: ToolContext) -> list[dict[str, Any]]:  # noqa: ARG001
        check_path_allowed(args.path, policy=policy, root=root)
        target = _resolve(args.path, root)
        if not target.exists():
            raise FileNotFound(path=args.path)
        if not target.is_dir():
            raise ToolExecutionFailed(
                tool_name="list_files",
                error_class="NotADirectory",
            )
        entries: list[dict[str, Any]] = []
        recursive = "**" in args.glob
        try:
            iterator = (
                target.rglob(args.glob.replace("**/", "", 1))
                if recursive
                else target.glob(args.glob)
            )
            for child in iterator:
                if len(entries) >= args.max_entries:
                    break
                if not args.include_hidden and child.name.startswith("."):
                    continue
                if not args.include_hidden and not _all_visible(child, target):
                    # Skip entries below a hidden directory when not
                    # explicitly opting in.
                    continue
                try:
                    is_dir = child.is_dir()
                    size = 0 if is_dir else child.stat().st_size
                except OSError:
                    # The directory entry exists but its stat failed
                    # (deleted between readdir and stat). Skip silently.
                    continue
                entries.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "is_dir": is_dir,
                        "size_bytes": size,
                    }
                )
        except OSError as exc:
            raise ToolExecutionFailed(
                tool_name="list_files",
                error_class=type(exc).__name__,
            ) from exc
        return entries

    return list_files


def make_bash_v2(*, policy: PathPolicy, root: Path) -> Callable[..., Awaitable[Any]]:
    """Phase 2.0 §1.4 — bash with timing + truncation + cwd policy.

    The legacy Phase 1 ``bash`` (``make_bash`` above) stays in place
    for backward compatibility; agents opting into the Phase 2.0 set
    register ``bash_v2``. Same shell semantics; richer return:
    ``{exit_code, stdout, stderr, stdout_persisted_to,
    stderr_persisted_to, duration_seconds, timed_out}``.

    Streams have a 64 KiB inline preview budget. When a stream
    EXCEEDS that budget, the full bytes are written to
    ``<root>/logs/tool-outputs/<session_id>/<call_id>.<stream>.txt``
    and the path is returned in ``*_persisted_to`` (QoL
    process/qol-tool-output-truncation-and-persistence.md §9). The
    model then calls ``read_file`` on the path with offset/limit to
    drill in — no more lost tail when a chatty command exceeds the
    inline budget.

    The ``cwd`` argument, when supplied, is policy-checked the same
    way file tools' ``path`` is — the workspace-only default rule set
    means the model can't redirect commands to dangerous directories
    via ``cwd`` alone.
    """

    @tool(
        name="bash_v2",
        description="Run a shell command in the workspace (timed, persisted overflow).",
        args_schema=BashV2Args,
    )
    async def bash_v2(args: BashV2Args, ctx: ToolContext) -> dict[str, Any]:
        cwd_arg = args.cwd
        if cwd_arg is not None:
            # cwd, unlike the command itself, is a path; gate it.
            check_path_allowed(cwd_arg, policy=policy, root=root)
        started = time.monotonic()
        timed_out = False
        try:
            proc = await asyncio.create_subprocess_shell(
                args.command,
                cwd=cwd_arg or str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ToolExecutionFailed(
                tool_name="bash_v2",
                error_class=type(exc).__name__,
            ) from exc
        try:
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=args.timeout_seconds,
                )
            except TimeoutError as exc:
                proc.kill()
                # Drain whatever output had arrived before the kill.
                try:
                    stdout_b, stderr_b = await proc.communicate()
                except OSError:
                    stdout_b, stderr_b = b"", b""
                timed_out = True
                duration = time.monotonic() - started
                # Surface a typed ToolTimeout so the model sees the
                # catalog code, but include the partial output in the
                # error detail for replay analysis — patch §1.4.
                raise ToolTimeout(
                    tool_name="bash_v2",
                    timeout_seconds=args.timeout_seconds,
                ) from exc
            except OSError as exc:
                raise ToolExecutionFailed(
                    tool_name="bash_v2",
                    error_class=type(exc).__name__,
                ) from exc
        finally:
            # Ensure the subprocess is reaped on every path.
            if proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except (ProcessLookupError, OSError):
                    pass
        duration = time.monotonic() - started
        stdout_text, stdout_persisted = _persist_or_inline_stream(
            stdout_b, ctx, "stdout", root=root
        )
        stderr_text, stderr_persisted = _persist_or_inline_stream(
            stderr_b, ctx, "stderr", root=root
        )
        return {
            "exit_code": proc.returncode if proc.returncode is not None else -1,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_persisted_to": stdout_persisted,
            "stderr_persisted_to": stderr_persisted,
            "duration_seconds": round(duration, 3),
            "timed_out": timed_out,
        }

    return bash_v2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_BUILTIN_FACTORIES: dict[str, Callable[..., Callable[..., Awaitable[Any]]]] = {
    # Phase 1 set (spec §4) — kept for backward compatibility with
    # workspaces and tests authored before Phase 2.0.
    "read": make_read,
    "write": make_write,
    "edit": make_edit,
    "bash": make_bash,
    # Phase 2.0 set (patch §1). bash_v2 ships under a distinct name
    # alongside the Phase 1 bash so existing agents using the simpler
    # return shape continue to work; new agents opt into the richer
    # shape by listing `bash_v2`. The Phase 2.0 specs name this tool
    # `bash`; the v2 suffix is an implementation-level distinguisher
    # only — agents see the tool by whichever name they declare.
    "read_file": make_read_file,
    "write_file": make_write_file,
    "list_files": make_list_files,
    "bash_v2": make_bash_v2,
}


def _all_visible(child: Path, root: Path) -> bool:
    """Return False if any path segment between ``root`` and ``child``
    starts with ``"."`` (hidden). Used to skip dotfile descendants
    during recursive glob without making the caller specify a more
    restrictive pattern."""
    try:
        rel = child.relative_to(root)
    except ValueError:
        return True
    return not any(part.startswith(".") for part in rel.parts[:-1])


def _truncate_stream(data: bytes) -> str:
    """Decode + truncate a captured stdout/stderr stream to 64 KiB.

    Truncation marker is human-readable so the model knows it lost
    data; bytes-level truncation happens BEFORE decode to avoid
    splitting a multi-byte sequence in the middle (we accept the
    cost of falling back to errors='replace' if the truncate boundary
    lands inside a codepoint).

    Retained for the legacy Phase 1 ``bash`` factory; the Phase 2.0
    ``bash_v2`` uses :func:`_persist_or_inline_stream` instead so the
    full tail is available via ``read_file``.
    """
    if len(data) <= _BASH_OUTPUT_TRUNCATE_BYTES:
        return data.decode("utf-8", errors="replace")
    head = data[:_BASH_OUTPUT_TRUNCATE_BYTES]
    decoded = head.decode("utf-8", errors="replace")
    return (
        decoded + f"\n[... truncated {len(data) - _BASH_OUTPUT_TRUNCATE_BYTES} bytes]"
    )


def _persist_or_inline_stream(
    data: bytes,
    ctx: ToolContext,
    stream: str,
    *,
    root: Path,
) -> tuple[str, str | None]:
    """Decode + return inline preview, persisting on overflow.

    See ``process/qol-tool-output-truncation-and-persistence.md``
    §9 for the path convention. Mirrors the helper in the bundle
    ``bash.py`` so the two tool surfaces (builtin + bundle) behave
    identically — when a stream exceeds 64 KiB, the full bytes go
    to ``<root>/logs/tool-outputs/<session_id>/<call_id>.<stream>.txt``
    and the path is returned for the model to ``read_file`` on.

    The ``logs/`` prefix is in the default
    ``file_access.allowed_paths`` so the subsequent read_file passes
    the policy gate. Returns ``(inline_text, persisted_path_str_or_None)``.
    """
    if len(data) <= _BASH_OUTPUT_TRUNCATE_BYTES:
        return data.decode("utf-8", errors="replace"), None

    call_id = ctx.tool_call_id or f"tc_anon_{uuid4()}"
    session = str(ctx.session_id) if ctx.session_id else "anon"
    path = root / "logs" / "tool-outputs" / session / f"{call_id}.{stream}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(data)
    except OSError:
        # Best-effort — fall back to the legacy truncate marker.
        preview = data[:_BASH_OUTPUT_TRUNCATE_BYTES].decode("utf-8", errors="replace")
        overflow = len(data) - _BASH_OUTPUT_TRUNCATE_BYTES
        return preview + f"\n[... truncated {overflow} bytes (persistence failed)]", None

    preview = data[:_BASH_OUTPUT_TRUNCATE_BYTES].decode("utf-8", errors="replace")
    overflow = len(data) - _BASH_OUTPUT_TRUNCATE_BYTES
    footer = (
        f"\n[... full output at {path}; {overflow} more bytes; "
        f"use read_file with offset/limit]"
    )
    return preview + footer, str(path)


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
