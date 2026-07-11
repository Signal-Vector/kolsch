"""The built-in tools — file and shell tools the platform ships.

Two coexisting sets ship from this module. The legacy set
(`read` / `write` / `edit` / `bash`) consults
[`files.check_path_allowed`][files.check_path_allowed] before
touching the filesystem; `bash` is exempt from the policy in v1's
native sandbox — an honest documented limitation. The canonical
shipping set (`read_file` / `write_file` / `list_files` / `bash_v2`)
adds line-paginated reads, atomic writes via temp + rename, a glob
listing with a `max_entries` cap, and a richer `bash` return shape
with timing, truncation, and overflow persistence.

Each builtin is a *factory* taking the platform root and path policy
because the tool function closes over them. The factory returns the
`@tool`-decorated function; callers register the resulting function
with the Tools subsystem. Agents opt into either set via their
`subsystems.tools.config.builtin` list. The `bash_v2` factory
supersedes legacy `bash` for new agents (same authz action string,
richer return shape); agents using legacy `bash` are unaffected
until they migrate.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import time
from dataclasses import replace as _dc_replace
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
from tools.decorator import get_tool_definition, tool

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from files import PathPolicy
    from tools.decorator import ToolContext


def _detach_tool_definition(
    fn: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Return a per-call wrapper of ``fn`` with a fresh ``_tool_definition``.

    ``make_browse_url`` / ``make_fetch_url`` load the ``@tool``-decorated
    coroutine from a module cached in ``sys.modules`` (the starter-bundle
    file-IS-the-tool pattern). The cached coroutine's ``_tool_definition``
    is a SINGLE shared ``ToolDefinition`` sentinel — mutating it in one
    caller (e.g. ``_workflow_runtime.py``'s ``_stamp_kolsch``) poisons
    every subsequent caller in the same process (a class-of-bug we hit
    in bug-050: a full-suite ``pytest`` run stamps the sentinel via one
    test that invokes ``build_workflow_executor``, then the guard test
    that runs later sees a pre-stamped defn where it expected ``None``).

    This helper wraps ``fn`` in a functools-``wraps``ed thin coroutine
    with its own ``_tool_definition`` attached — a ``dataclass.replace``
    copy of the sentinel's fields. Callers can freely stamp their copy
    without cross-talk. The underlying coroutine body is preserved
    (``functools.wraps`` keeps identity metadata; ``await fn(*args)``
    delegates the actual work), so behavior is byte-identical.
    """
    original_defn = get_tool_definition(fn)

    @functools.wraps(fn)
    async def _wrapper(*args: Any, **kwargs: Any) -> Any:
        return await fn(*args, **kwargs)

    _wrapper._tool_definition = _dc_replace(original_defn)  # type: ignore[attr-defined]
    return _wrapper


# Tunables for the canonical builtins.
_DEFAULT_READ_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB — single-line cap
_DEFAULT_READ_LINE_LIMIT = 2000  # line-based pagination default
_DEFAULT_LIST_MAX_ENTRIES = 500
_DEFAULT_BASH_TIMEOUT_SECONDS = 60
_BASH_OUTPUT_TRUNCATE_BYTES = 64 * 1024  # 64 KiB per stream


# ---------------------------------------------------------------------------
# Argument models — legacy set
# ---------------------------------------------------------------------------


class ReadArgs(BaseModel):
    """Argument shape for the legacy `read` builtin."""

    path: str
    encoding: str = "utf-8"


class WriteArgs(BaseModel):
    """Argument shape for the legacy `write` builtin."""

    path: str
    content: str
    create_parents: bool = True
    encoding: str = "utf-8"


class EditArgs(BaseModel):
    """Argument shape for the legacy `edit` builtin."""

    path: str
    old_str: str
    new_str: str
    occurrence: int = 1


class BashArgs(BaseModel):
    """Argument shape for the legacy `bash` builtin."""

    command: str
    cwd: str | None = None
    timeout_seconds: int = Field(default=60, ge=1)


# ---------------------------------------------------------------------------
# Factories — each returns a @tool-decorated coroutine closing over root + policy
# ---------------------------------------------------------------------------


def make_read(*, policy: PathPolicy, root: Path) -> Callable[..., Awaitable[Any]]:
    """Build the legacy `read` builtin — whole-file text read.

    The factory closes the returned `@tool` function over `policy`
    and `root` so the body can gate paths without holding extra
    state.
    """

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
    """Build the legacy `write` builtin — overwrite a file with text.

    Path policy gates reads; an additional write-destination check
    refuses writes into the engine-only `content/` store even when
    the policy allows reads there.
    """

    @tool(
        name="write",
        description=("Write text content to a file on disk, overwriting if it exists."),
        args_schema=WriteArgs,
    )
    async def write(args: WriteArgs, ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
        # Path policy first.
        check_path_allowed(args.path, policy=policy, root=root)
        # Refuse writes into the engine-only content/ store, even
        # when policy would otherwise allow reads there. Two-step
        # check: policy gates reads; this gates writes.
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
    """Build the legacy `edit` builtin — substring replacement in a file.

    Edit reads and writes, so both the path policy and the
    write-destination check fire. `occurrence` controls which match
    is replaced; matching multiple sites without `occurrence=0`
    fails loudly rather than silently picking the first.
    """

    @tool(
        name="edit",
        description=("Edit a file by replacing a specific string with new content."),
        args_schema=EditArgs,
    )
    async def edit(args: EditArgs, ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
        # Edit reads AND writes, so it needs both gates. The
        # write-side refusal of content/ matters even when the path
        # policy allows reads under content/.
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
                # Fail loudly on ambiguity rather than silently
                # picking the first match.
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


def make_fetch_url(
    *,
    policy: PathPolicy,  # noqa: ARG001 — kept for signature symmetry across make_* factories
    root: Path,  # noqa: ARG001
) -> Callable[..., Awaitable[Any]]:
    """Factory for the fetch_url tool — qol-fetch-url-tool Step 2.

    Loads the ``@tool``-decorated coroutine defined in
    ``src/cli/_starter_bundle/kolsch/tools/fetch_url.py`` via
    ``importlib`` (the starter-bundle tree is a filesystem template,
    not an importable package — no ``__init__.py``) so this factory
    stays a thin wrapper. The starter-bundle file is the source of
    truth (TOOL-R10, file-IS-the-tool); ``make_fetch_url`` exists so
    the workflow-node companion (a follow-up QoL) can consume the
    same implementation via the ``make_*`` pattern that ``make_bash``
    / ``make_read_file`` use.

    ``policy`` and ``root`` are accepted for factory-signature symmetry
    but not used: ``fetch_url`` reaches the network, not the filesystem,
    and reads ``workspace_root`` / ``session_id`` off ``ToolContext`` at
    invocation time (via the dispatch flow) rather than closing over them.
    """
    import importlib.util  # noqa: PLC0415 — deferred; avoids cost when factory unused
    import sys  # noqa: PLC0415 — deferred

    # The template lives under src/cli/_starter_bundle/kolsch/tools/ —
    # this file (src/tools/builtins.py) is siblings-of-siblings with it.
    tool_path = (
        Path(__file__).resolve().parent.parent
        / "cli"
        / "_starter_bundle"
        / "kolsch"
        / "tools"
        / "fetch_url.py"
    )
    module_name = "_kolsch_starter_bundle_fetch_url"
    cached = sys.modules.get(module_name)
    if cached is None:
        spec = importlib.util.spec_from_file_location(module_name, tool_path)
        if spec is None or spec.loader is None:
            msg = f"could not load fetch_url tool template from {tool_path}"
            raise RuntimeError(msg)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    else:
        module = cached
    fetch_url: Callable[..., Awaitable[Any]] = module.fetch_url
    # bug-050: detach a fresh `_tool_definition` per call so callers
    # that stamp `home_bundle` don't poison the module-cached sentinel
    # for every subsequent caller in the process.
    return _detach_tool_definition(fetch_url)


def make_browse_url(
    *,
    policy: PathPolicy,  # noqa: ARG001 — kept for signature symmetry across make_* factories
    root: Path,  # noqa: ARG001
) -> Callable[..., Awaitable[Any]]:
    """Factory for the browse_url tool — qol-browse-url-tool Step 2.

    Loads the ``@tool``-decorated coroutine defined in
    ``src/cli/_starter_bundle/kolsch/tools/browse_url.py`` via
    ``importlib`` (the starter-bundle tree is a filesystem template,
    not an importable package — no ``__init__.py``) so this factory
    stays a thin wrapper. The starter-bundle file is the source of
    truth (TOOL-R10, file-IS-the-tool); ``make_browse_url`` exists so
    the workflow-node companion (a follow-up QoL) can consume the
    same implementation via the ``make_*`` pattern that ``make_bash``
    / ``make_read_file`` use.

    ``policy`` and ``root`` are accepted for factory-signature symmetry
    but not used: ``browse_url`` reaches the network, not the filesystem,
    and reads ``workspace_root`` / ``session_id`` off ``ToolContext`` at
    invocation time (via the dispatch flow) rather than closing over them.
    """
    import importlib.util  # noqa: PLC0415 — deferred; avoids cost when factory unused
    import sys  # noqa: PLC0415 — deferred

    tool_path = (
        Path(__file__).resolve().parent.parent
        / "cli"
        / "_starter_bundle"
        / "kolsch"
        / "tools"
        / "browse_url.py"
    )
    module_name = "_kolsch_starter_bundle_browse_url"
    cached = sys.modules.get(module_name)
    if cached is None:
        spec = importlib.util.spec_from_file_location(module_name, tool_path)
        if spec is None or spec.loader is None:
            msg = f"could not load browse_url tool template from {tool_path}"
            raise RuntimeError(msg)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    else:
        module = cached
    browse_url: Callable[..., Awaitable[Any]] = module.browse_url
    # bug-050: detach a fresh `_tool_definition` per call so callers
    # that stamp `home_bundle` don't poison the module-cached sentinel
    # for every subsequent caller in the process.
    return _detach_tool_definition(browse_url)


def make_bash(*, policy: PathPolicy, root: Path) -> Callable[..., Awaitable[Any]]:  # noqa: ARG001
    """Build the legacy `bash` builtin — shell tool exempt from path policy.

    `bash` is exempt from the path policy in v1's native sandbox
    (an honest documented limitation). The `policy` and `root`
    arguments are accepted and ignored so the factory signature
    matches the other builtins.
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
# Canonical set — read_file / write_file / list_files / bash_v2
# ---------------------------------------------------------------------------


class ReadFileArgs(BaseModel):
    """Argument shape for the canonical `read_file` builtin."""

    path: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=_DEFAULT_READ_LINE_LIMIT, ge=1)
    max_bytes: int = Field(default=_DEFAULT_READ_MAX_BYTES, ge=1)


class WriteFileArgs(BaseModel):
    """Argument shape for the canonical `write_file` builtin."""

    path: str
    content: str
    create_parents: bool = True


class ListFilesArgs(BaseModel):
    """Argument shape for the canonical `list_files` builtin."""

    path: str = "."
    glob: str = "*"
    max_entries: int = Field(default=_DEFAULT_LIST_MAX_ENTRIES, ge=1)
    include_hidden: bool = False


class BashV2Args(BaseModel):
    """Argument shape for the canonical `bash_v2` builtin."""

    command: str
    cwd: str | None = None
    timeout_seconds: int = Field(default=_DEFAULT_BASH_TIMEOUT_SECONDS, ge=1)


def make_read_file(*, policy: PathPolicy, root: Path) -> Callable[..., Awaitable[Any]]:
    """Build the canonical `read_file` builtin — line-based read with soft truncation.

    Args are `path` plus `offset` (0-indexed line), `limit` (max
    lines, default 2000), and `max_bytes` (single-line cap, default
    1 MiB). Return shape is `{content, lines_returned, total_lines,
    next_offset, truncated}`. Whole-file size is not capped — only
    a single line exceeding `max_bytes` trips
    [`FileLineTooLong`][errors.FileLineTooLong] (the binary-as-text
    guard). Output is `cat -n` numbered so the model can reference
    line numbers in responses and follow-up calls.
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
    """Shared implementation between the builtin and the bundle copy.

    Returns the canonical `{content, lines_returned, total_lines,
    next_offset, truncated}` dict. The single-line byte cap is the
    only hard refusal — everything else soft-truncates.
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
    """Build the canonical `write_file` builtin — atomic full-overwrite write.

    Writes to `<path>.tmp.<pid>` then `os.replace` onto the
    destination so a SIGKILL between the temp write and the rename
    leaves the prior content intact. Matches the atomicity
    discipline used for `platform.yaml` writes elsewhere.
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
    """Build the canonical `list_files` builtin — directory listing with optional glob.

    Default glob is `"*"` (one-level); `"**/*"` walks recursively.
    `max_entries` caps the returned list to prevent runaway context
    spend on huge trees. `include_hidden` defaults to `False` to
    skip dotfiles, mirroring conventional shell behaviour.
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
    """Build the canonical `bash_v2` builtin — timed, truncated, cwd-policed bash.

    Same shell semantics as legacy `bash` with a richer return:
    `{exit_code, stdout, stderr, stdout_persisted_to,
    stderr_persisted_to, duration_seconds, timed_out}`. The legacy
    `bash` (`make_bash` above) stays in place for backward
    compatibility; agents opting into the canonical set register
    `bash_v2`.

    Streams have a 64 KiB inline preview budget. When a stream
    exceeds that budget, the full bytes are written to
    `<root>/logs/tool-outputs/<session_id>/<call_id>.<stream>.txt`
    and the path is returned in `*_persisted_to`. The model then
    calls `read_file` on the path with offset/limit to drill in —
    no more lost tail when a chatty command exceeds the inline
    budget.

    The `cwd` argument, when supplied, is policy-checked the same
    way file tools' `path` is — the workspace-only default rule set
    means the model can't redirect commands to dangerous directories
    via `cwd` alone.
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
                # catalog code; the partial output is captured for
                # replay analysis.
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
    # Legacy set — kept for backward compatibility with workspaces
    # and tests authored before the canonical set landed.
    "read": make_read,
    "write": make_write,
    "edit": make_edit,
    "bash": make_bash,
    # Canonical set. bash_v2 ships under a distinct name alongside
    # the legacy bash so existing agents using the simpler return
    # shape continue to work; new agents opt into the richer shape
    # by listing `bash_v2`. The v2 suffix is an implementation-level
    # distinguisher — agents see the tool by whichever name they
    # declare.
    "read_file": make_read_file,
    "write_file": make_write_file,
    "list_files": make_list_files,
    "bash_v2": make_bash_v2,
}


def _all_visible(child: Path, root: Path) -> bool:
    """Return False when any path segment between `root` and `child` is hidden.

    A segment starting with `"."` counts as hidden. Used to skip
    dotfile descendants during recursive glob without making the
    caller specify a more restrictive pattern.
    """
    try:
        rel = child.relative_to(root)
    except ValueError:
        return True
    return not any(part.startswith(".") for part in rel.parts[:-1])


def _truncate_stream(data: bytes) -> str:
    """Decode and truncate a captured stdout/stderr stream to 64 KiB.

    The truncation marker is human-readable so the model knows it
    lost data; bytes-level truncation happens before decode to
    avoid splitting a multi-byte sequence in the middle (we accept
    the cost of falling back to `errors='replace'` if the truncate
    boundary lands inside a codepoint).

    Retained for the legacy `bash` factory; the canonical `bash_v2`
    uses `_persist_or_inline_stream` instead so the full tail is
    available via `read_file`.
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
    """Decode and return inline preview, persisting on overflow.

    Mirrors the helper in the bundle's `bash.py` so the two tool
    surfaces (builtin and bundle) behave identically — when a stream
    exceeds 64 KiB, the full bytes go to
    `<root>/logs/tool-outputs/<session_id>/<call_id>.<stream>.txt`
    and the path is returned for the model to `read_file` on. The
    `logs/` prefix is in the default `file_access.allowed_paths` so
    the subsequent `read_file` passes the policy gate. Returns
    `(inline_text, persisted_path_str_or_None)`.
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
        suffix = f"\n[... truncated {overflow} bytes (persistence failed)]"
        return preview + suffix, None

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
    """Construct a builtin tool by name.

    Args:
        name: One of the names returned by
            [`builtin_names`][tools.builtins.builtin_names].
        policy: Path policy the file-touching builtins consult.
        root: The platform root the builtins resolve paths against.

    Returns:
        The `@tool`-decorated coroutine for the named builtin.

    Raises:
        ValueError: `name` isn't a known builtin.
    """
    factory = _BUILTIN_FACTORIES.get(name)
    if factory is None:
        msg = f"unknown builtin tool {name!r}; available: {sorted(_BUILTIN_FACTORIES)}"
        raise ValueError(msg)
    return factory(policy=policy, root=root)


def builtin_names() -> set[str]:
    """Return the set of names `make_builtin` recognises."""
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
    """Replace the Nth (1-indexed) occurrence of `old` with `new`."""
    index = -1
    for _ in range(n):
        index = text.find(old, index + 1)
        if index == -1:
            raise ToolExecutionFailed(
                tool_name="edit",
                error_class="NoMatchError",
            )
    return text[:index] + new + text[index + len(old) :], 1
