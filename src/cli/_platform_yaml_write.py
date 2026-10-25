"""Locked whole-document rewrites of `platform.yaml` (bug-146).

[`mutate_platform_rules`][cli.permissions.mutate_platform_rules] is the
writer for single rule edits. Some commands still re-dump the whole
document to change something else — `defaults.model`,
`file_access.allowed_paths`, `llm_providers`, a whole-list rules
migration. Re-dumping a copy read *before* taking the lock would drop a
rule a concurrent chat "always" (or `kolsch permissions`) wrote in
between, so every such writer goes through
[`rewrite_platform_yaml_locked`][cli._platform_yaml_write.rewrite_platform_yaml_locked]:
it takes [`platform_yaml_lock`][assets.platform_config.platform_yaml_lock],
re-reads the file **inside** it, applies the caller's change to that
fresh copy, and replaces the file atomically.

A trailing, fully-commented block (the fresh-install `hooks:` example)
survives, as `dump_preserving_trailing_comment_block` keeps it; other
comments are lost, as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kolsch._yamlio import dump_preserving_trailing_comment_block, load

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def rewrite_platform_yaml_locked(
    root: Path,
    change: Callable[[dict[str, Any]], bool],
    *,
    before_write: Callable[[Path], object] | None = None,
) -> bool:
    """Apply `change` to a fresh read of `root/platform.yaml`, under the lock.

    Args:
        root: The workspace root.
        change: Mutates the parsed document in place; returns `False`
            when, on this fresh copy, there is nothing to change (then
            nothing is written).
        before_write: Called with the file path just before the write,
            still under the lock (e.g. to take a backup).

    Returns:
        Whether the file was written.

    Raises:
        AuthzRulesPersistFailed: The lock couldn't be taken
            (`lock_timeout` / `write_failed`).
        OSError: The read or the write failed (the file is unchanged).
        TypeError: The file's top level is not a mapping.
    """
    from assets.platform_config import platform_yaml_lock  # noqa: PLC0415
    from cli.permissions import _atomic_write_text  # noqa: PLC0415 — shared writer

    path = root / "platform.yaml"
    with platform_yaml_lock(root):
        previous_text = path.read_text(encoding="utf-8") if path.is_file() else ""
        loaded = load(previous_text) if previous_text else None
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            msg = (
                "platform.yaml top-level must be a mapping; "
                f"got {type(loaded).__name__}"
            )
            raise TypeError(msg)
        if not change(loaded):
            return False
        if before_write is not None:
            before_write(path)
        _atomic_write_text(
            path, dump_preserving_trailing_comment_block(previous_text, loaded)
        )
    return True
