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

from pydantic import BaseModel

from errors import ToolExecutionFailed
from files import assert_writable_destination, check_path_allowed
from tools.decorator import ToolContext, tool


class WriteFileArgs(BaseModel):
    path: str
    content: str
    create_parents: bool = True


@tool(
    name="write_file",
    description="Write a UTF-8 text file to the workspace (full overwrite, atomic).",
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
