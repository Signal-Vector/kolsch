"""``list_files`` — directory listing with optional recursive glob.

This file IS the tool. See ``read_file.py`` for the
file-IS-the-tool rationale (TOOL-R10 hard rule).

Default glob ``"*"`` is non-recursive; ``"**/*"`` walks recursively.
``max_entries`` caps the returned list. ``include_hidden=False``
skips dotfiles by default (matches conventional shell behaviour).

Spec: docs/process/phase2.0-tools-specs.md §1.3.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from errors import FileNotFound, ToolExecutionFailed
from files import check_path_allowed
from tools.decorator import ToolContext, tool

_DEFAULT_MAX_ENTRIES = 500


class ListFilesArgs(BaseModel):
    path: str = "."
    glob: str = "*"
    max_entries: int = Field(default=_DEFAULT_MAX_ENTRIES, ge=1)
    include_hidden: bool = False


@tool(
    name="list_files",
    description="List entries under a directory (non-recursive by default).",
    args_schema=ListFilesArgs,
)
async def list_files(args: ListFilesArgs, ctx: ToolContext) -> list[dict[str, Any]]:
    """Return ``[{"name", "path", "is_dir", "size_bytes"}, ...]``."""
    if ctx.path_policy is None or ctx.workspace_root is None:
        msg = (
            "list_files requires the platform to populate ctx.path_policy "
            "and ctx.workspace_root."
        )
        raise ToolExecutionFailed(
            tool_name="list_files", error_class="RuntimeError"
        ) from RuntimeError(msg)

    check_path_allowed(args.path, policy=ctx.path_policy, root=ctx.workspace_root)
    target = _resolve(args.path, ctx.workspace_root)
    if not target.exists():
        raise FileNotFound(path=args.path)
    if not target.is_dir():
        raise ToolExecutionFailed(tool_name="list_files", error_class="NotADirectory")

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
                continue
            try:
                is_dir = child.is_dir()
                size = 0 if is_dir else child.stat().st_size
            except OSError:
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
            tool_name="list_files", error_class=type(exc).__name__
        ) from exc
    return entries


def _resolve(path: str, root: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate


def _all_visible(child: Path, root: Path) -> bool:
    """Skip entries below a hidden directory (when include_hidden=False)."""
    try:
        rel = child.relative_to(root)
    except ValueError:
        return True
    return not any(part.startswith(".") for part in rel.parts[:-1])
