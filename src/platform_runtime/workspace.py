"""Workspace resolution — where this `kolsch` invocation operates.

The single point of truth for resolving the workspace root. Two pure
functions, no [`PlatformRuntime`][platform_runtime.runtime.PlatformRuntime]
import, no other module reads `KOLSCH_WORKSPACE` or imports
`platformdirs`.

See [workspace and setup](../../docs/how-to/workspace-and-setup.md)
for the resolution chain rationale and CLI gating behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import platformdirs

KOLSCH_ENV_VAR: Final[str] = "KOLSCH_WORKSPACE"
DEFAULT_DIR_NAME: Final[str] = "kolsch"


def resolve_workspace(cli_root: Path | None = None) -> Path:
    """Return the canonical workspace path for this invocation.

    Resolution order, highest precedence first:

    1. `cli_root` — typically the parsed `--root` flag.
    2. `$KOLSCH_WORKSPACE` — env-var override.
    3. `<platformdirs.user_documents_dir()>/kolsch` — default.

    The returned path is `expanduser().resolve()`-d. The path is NOT
    guaranteed to exist on disk — pair with
    `workspace_is_initialized` to detect uninitialised workspaces.

    Args:
        cli_root: The CLI-supplied root, typically from a `--root`
            flag. When `None`, falls through to the env var and the
            platformdirs default.

    Returns:
        The resolved absolute workspace path.
    """
    if cli_root is not None:
        return cli_root.expanduser().resolve()
    env = os.environ.get(KOLSCH_ENV_VAR)
    if env:
        return Path(env).expanduser().resolve()
    return Path(platformdirs.user_documents_dir(), DEFAULT_DIR_NAME).resolve()


def workspace_is_initialized(root: Path) -> bool:
    """Return `True` iff `root / "platform.yaml"` exists and is a file.

    CLI dispatchers gate non-setup commands on this so an uninitialised
    workspace surfaces a friendly message rather than a cryptic crash
    deeper in the stack.

    Args:
        root: The workspace path, typically from `resolve_workspace`.

    Returns:
        `True` when `root` holds a `platform.yaml` file.
    """
    return (root / "platform.yaml").is_file()
