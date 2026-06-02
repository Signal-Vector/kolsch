"""``write_file`` — atomic full-overwrite write of a UTF-8 text file.

This file IS the tool. Ships at ``<workspace>/bundles/kolsch/tools/``
when ``kolsch setup`` runs. See ``read_file.py`` for the full
file-IS-the-tool rationale (TOOL-R10 hard rule).

Atomic write: writes to ``<path>.tmp.<pid>`` then ``os.replace`` onto
the destination, so a SIGKILL between the temp write and rename
leaves the prior content intact (TC-TOOL-07). The atomicity matches
Phase 1.1's ``platform.yaml`` discipline.

Spec: docs/process/phase2.0-tools-specs.md §1.2.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from errors import ToolExecutionFailed
from files import assert_writable_destination, check_path_allowed
from tools.decorator import ToolContext, tool


class WriteFileArgs(BaseModel):
    path: str = Field(
        description=(
            "Path where the file will be written. Workspace-relative "
            "(``data/notes.md``) or absolute (must resolve inside "
            "``file_access.allowed_paths``)."
        ),
    )
    content: str = Field(
        description=(
            "Full file contents to write as a UTF-8 string. "
            "FULL OVERWRITE — if the file exists, the new content "
            "completely replaces the old content. This is NOT a "
            "patch / append / partial-edit; pass the entire intended "
            "file body."
        ),
    )
    create_parents: bool = Field(
        default=True,
        description=(
            "Whether to create missing parent directories. Default "
            "true. Set false if you want a missing-parent path to "
            "fail loudly rather than implicitly create directories."
        ),
    )


@tool(
    name="write_file",
    summary="Atomically write a UTF-8 text file to the workspace.",
    description=(
        "Write a UTF-8 text file to the workspace, replacing any "
        "existing content. Use this tool to create new files or "
        "fully replace an existing file's content. Writes are "
        "atomic (temp file + rename) so a crash mid-write can't "
        "leave a half-written file. "
        "\n\n"
        "FULL OVERWRITE semantics — when you call ``write_file``, "
        "pass the ENTIRE intended file content as ``content``, not "
        "a diff or partial edit. If you only want to modify part of "
        "a file, first ``read_file`` to get the current content, "
        "compute the new content yourself, then ``write_file`` the "
        "full new version. "
        "\n\n"
        "DO NOT use this tool to append to a log via ``>>`` or "
        "modify part of a config — read-then-write is the correct "
        "pattern. DO NOT call ``echo > file`` via ``bash`` for "
        "the same reason ``read_file`` is preferred over ``cat``: "
        "this tool enforces the workspace's path policy. "
        "\n\n"
        "Returns ``{path: str, bytes_written: int, created: bool}`` "
        "(``created=true`` when the file didn't previously exist). "
        "Raises ``file.access_denied`` if the path is outside the "
        "allowed scope."
    ),
    args_schema=WriteFileArgs,
)
async def write_file(args: WriteFileArgs, ctx: ToolContext) -> dict[str, Any]:
    """Write ``args.content`` to ``args.path`` atomically.

    :raises file.access_denied: path outside allowed scope.
    """
    if ctx.path_policy is None or ctx.workspace_root is None:
        msg = (
            "write_file requires the platform to populate ctx.path_policy "
            "and ctx.workspace_root."
        )
        raise ToolExecutionFailed(
            tool_name="write_file", error_class="RuntimeError"
        ) from RuntimeError(msg)

    check_path_allowed(args.path, policy=ctx.path_policy, root=ctx.workspace_root)
    assert_writable_destination(args.path, root=ctx.workspace_root)
    target = _resolve(args.path, ctx.workspace_root)
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
        os.replace(temp, target)  # noqa: PTH105 — explicit os call for testability
    except OSError as exc:
        # Best-effort cleanup of the temp file (no-op if creation failed).
        with contextlib.suppress(OSError):
            temp.unlink()
        raise ToolExecutionFailed(
            tool_name="write_file", error_class=type(exc).__name__
        ) from exc
    return {
        "path": str(target),
        "bytes_written": len(encoded),
        "created": not existed,
    }


def _resolve(path: str, root: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate
