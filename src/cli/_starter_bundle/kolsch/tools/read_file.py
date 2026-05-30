"""``read_file`` — read a UTF-8 text file from the workspace.

This file IS the tool. It ships at ``<workspace>/bundles/kolsch/tools/``
when ``kolsch setup`` runs and is discovered + registered by the Tools
subsystem's standard bundle-tool path (importlib + ``@tool`` attribute
scan). Edit, copy, or swap freely.

Per AGENTS.md hard rule (TOOL-R10): there is no hidden builtin
registry — this file is the source of truth for what ``read_file``
does. Future contributors adding a fifth tool follow the same shape:
a Pydantic args model, an ``@tool``-decorated coroutine, the
typed-error raises this file demonstrates.

Spec: docs/process/phase2.0-tools-specs.md §1.1.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from errors import (
    FileAccessDenied,
    FileNotFound,
    FileTooLarge,
    ToolExecutionFailed,
)
from files import check_path_allowed
from tools.decorator import ToolContext, tool

_DEFAULT_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB context-budget guard


class ReadFileArgs(BaseModel):
    path: str
    max_bytes: int = Field(default=_DEFAULT_MAX_BYTES, ge=1)


@tool(
    name="read_file",
    description="Read a UTF-8 text file from the workspace.",
    args_schema=ReadFileArgs,
)
async def read_file(args: ReadFileArgs, ctx: ToolContext) -> str:
    """Read the file at ``args.path`` and return its content.

    :param args: ``path`` (workspace-relative or absolute), and
        ``max_bytes`` (default 1 MiB) — files larger than this are
        rejected before being opened. Protects context budgets.
    :param ctx: tool invocation context. The platform populates
        ``ctx.workspace_root`` and ``ctx.path_policy`` so this tool
        can enforce the workspace-only access policy without holding
        a closure over them.
    :returns: file content as a UTF-8 string.
    :raises file.access_denied: path resolves outside
        ``file_access.allowed_paths``.
    :raises file.too_large: file size exceeds ``args.max_bytes``.
    :raises file.not_found: path does not exist.
    """
    if ctx.path_policy is None or ctx.workspace_root is None:
        msg = (
            "read_file requires the platform to populate ctx.path_policy "
            "and ctx.workspace_root; the running ToolsSubsystem appears "
            "to have been constructed without them. See "
            "docs/explanation/tools-subsystem.md §4.5."
        )
        raise ToolExecutionFailed(
            tool_name="read_file", error_class="RuntimeError"
        ) from RuntimeError(msg)

    check_path_allowed(args.path, policy=ctx.path_policy, root=ctx.workspace_root)
    target: Path = _resolve(args.path, ctx.workspace_root)
    try:
        size = target.stat().st_size  # noqa: ASYNC240 — sync IO is intentional
    except FileNotFoundError as exc:
        raise FileNotFound(path=args.path) from exc
    except OSError as exc:
        raise FileAccessDenied(
            path=args.path,
            reason=f"stat failed: {type(exc).__name__}: {exc}",
        ) from exc
    if size > args.max_bytes:
        raise FileTooLarge(path=args.path, size_bytes=size, max_bytes=args.max_bytes)
    try:
        content: str = target.read_text(encoding="utf-8")  # noqa: ASYNC240
    except FileNotFoundError as exc:
        raise FileNotFound(path=args.path) from exc
    except OSError as exc:
        raise ToolExecutionFailed(
            tool_name="read_file", error_class=type(exc).__name__
        ) from exc
    return content


def _resolve(path: str, root: Path) -> Path:
    """Resolve a workspace-relative path against the workspace root."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate
