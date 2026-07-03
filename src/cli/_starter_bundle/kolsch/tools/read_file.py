"""`read_file` — read a UTF-8 text file by line range.

This file IS the tool. It ships at `<workspace>/bundles/kolsch/tools/`
when `kolsch setup` runs and is discovered + registered by the Tools
subsystem's standard bundle-tool path (importlib + `@tool` attribute
scan). Edit, copy, or swap freely.

There is no hidden builtin registry — this file is the source of
truth for what `read_file` does. Future contributors adding a fifth
tool follow the same shape: a Pydantic args model, an
`@tool`-decorated coroutine, the typed-error raises this file
demonstrates. See the
[tools subsystem](../../../explanation/tools-subsystem.md)
for the discovery + dispatch contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from errors import (
    FileAccessDenied,
    FileLineTooLong,
    FileNotFound,
    ToolExecutionFailed,
)
from files import check_path_allowed
from tools.decorator import ToolContext, tool

_DEFAULT_LINE_LIMIT = 2000
_DEFAULT_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB — single-line cap


class ReadFileArgs(BaseModel):
    """Argument schema for the `read_file` tool."""

    path: str = Field(
        description=(
            "Path to the file to read. Workspace-relative "
            "(``bundles/kolsch/agents/helper.yaml``) or absolute "
            "(``/abs/path/to/file.txt``). Absolute paths must "
            "resolve inside ``file_access.allowed_paths``."
        ),
    )
    offset: int = Field(
        default=0,
        ge=0,
        description=(
            "Line offset (0-indexed). Use ``next_offset`` from a prior "
            "call to continue paginating through a large file."
        ),
    )
    limit: int = Field(
        default=_DEFAULT_LINE_LIMIT,
        ge=1,
        description=(
            "Maximum number of lines to return. Default 2000. "
            "Files longer than ``offset + limit`` soft-truncate; the "
            "response carries ``next_offset`` so a follow-up call "
            "continues from where this one stopped."
        ),
    )
    max_bytes: int = Field(
        default=_DEFAULT_MAX_BYTES,
        ge=1,
        description=(
            "Cap on any single line's UTF-8 byte length. Lines longer "
            "than this raise ``file.line_too_long`` (Recoverable). "
            "Default 1 MiB — guards against binary files masquerading "
            "as text. Whole-file size has no cap; large files paginate "
            "via ``offset`` / ``limit`` instead."
        ),
    )


@tool(
    name="read_file",
    summary="Read a UTF-8 text file by line range.",
    description=(
        "Read a UTF-8 text file by line range. Returns the requested "
        "lines (default: first 2000) with ``cat -n`` style line numbers, "
        "plus pagination metadata. Use this for ANY file inspection — "
        "code, config, logs, YAML. "
        "\n\n"
        "Examples:\n"
        '  read_file({path: "src/main.py"})\n'
        "    — first 2000 lines, numbered.\n"
        '  read_file({path: "src/main.py", offset: 50, limit: 100})\n'
        "    — lines 51 through 150 (offset is 0-indexed).\n"
        '  read_file({path: "logs/tool-outputs/.../bash.stdout.txt"})\n'
        "    — read a persisted bash output (see the bash tool). "
        "\n\n"
        "DO NOT use ``bash`` with ``cat``, ``head``, ``tail``, "
        "``less``, or ``wc -l`` to inspect files. Those bypass the "
        "workspace path policy AND dump unbounded content into "
        "context. ``read_file`` enforces the policy and returns "
        "structured pagination so you can drill in without bloat. "
        "\n\n"
        "For directories use ``list_files``. For binary files "
        "(images, archives) the UTF-8 decode will produce garbage — "
        "don't. "
        "\n\n"
        "Returns ``{content, lines_returned, total_lines, "
        "next_offset, truncated}``. When ``truncated`` is true, pass "
        "``offset: next_offset`` to continue. Raises "
        "``file.line_too_long`` if any single line exceeds "
        "``max_bytes`` (default 1 MiB — guards against binary "
        "masquerading as text). Raises ``file.not_found`` / "
        "``file.access_denied`` for the usual reasons."
    ),
    args_schema=ReadFileArgs,
)
async def read_file(args: ReadFileArgs, ctx: ToolContext) -> dict[str, Any]:
    r"""Read `args.path` and return a paginated, numbered view.

    Returns:
        `{content, lines_returned, total_lines, next_offset,
        truncated}`. `content` is the requested slice rendered as
        `"<N>\t<line>\n"` rows.

    Raises:
        FileAccessDenied: Path resolves outside the allowed scope.
        FileLineTooLong: A single line exceeds `args.max_bytes`.
        FileNotFound: Path does not exist.
    """
    if ctx.path_policy is None or ctx.workspace_root is None:
        msg = (
            "read_file requires the platform to populate ctx.path_policy "
            "and ctx.workspace_root; the running ToolsSubsystem appears "
            "to have been constructed without them. See "
            "docs/explanation/tools-subsystem.md."
        )
        raise ToolExecutionFailed(
            tool_name="read_file", error_class="RuntimeError"
        ) from RuntimeError(msg)

    check_path_allowed(args.path, policy=ctx.path_policy, root=ctx.workspace_root)
    target: Path = _resolve(args.path, ctx.workspace_root)
    try:
        raw = target.read_bytes()  # noqa: ASYNC240 — sync IO is intentional
    except FileNotFoundError as exc:
        raise FileNotFound(path=args.path) from exc
    except OSError as exc:
        raise FileAccessDenied(
            path=args.path,
            reason=f"read failed: {type(exc).__name__}: {exc}",
        ) from exc

    return _paginate(
        raw,
        path=args.path,
        offset=args.offset,
        limit=args.limit,
        max_bytes=args.max_bytes,
    )


def _paginate(
    raw: bytes,
    *,
    path: str,
    offset: int,
    limit: int,
    max_bytes: int,
) -> dict[str, Any]:
    """Decode → split → enforce single-line cap → slice → format."""
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total_lines = len(lines)

    # Single-line byte cap — guards binary masquerading as text. The
    # check uses the encoded byte length so a wide Unicode codepoint
    # doesn't cause a false negative against the byte budget.
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
    # cat -n always trails with a newline; matches the expected
    # ergonomic so consumers can split cleanly on '\n'.
    content = rendered + "\n" if rendered else ""

    return {
        "content": content,
        "lines_returned": len(selected),
        "total_lines": total_lines,
        "next_offset": next_offset,
        "truncated": truncated,
    }


def _resolve(path: str, root: Path) -> Path:
    """Resolve a workspace-relative path against the workspace root."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate
