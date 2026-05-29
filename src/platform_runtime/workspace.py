"""Workspace resolution — Phase 1.2 (UX-R7, Module API Reference §11.6).

The single point of truth for "where does this `kolsch` invocation
operate?". Two pure functions; no PlatformRuntime import; no other
module reads :data:`KOLSCH_ENV_VAR` or imports ``platformdirs``.
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

    Resolution order (highest precedence first):

    1. ``cli_root`` — typically the parsed ``--root`` flag.
    2. ``$KOLSCH_WORKSPACE`` — env-var override.
    3. ``<platformdirs.user_documents_dir()>/kolsch`` — default.

    Returned path is ``.expanduser().resolve()``-d. NOT guaranteed
    to exist — pair with :func:`workspace_is_initialized` to detect
    uninitialised workspaces.
    """
    if cli_root is not None:
        return cli_root.expanduser().resolve()
    env = os.environ.get(KOLSCH_ENV_VAR)
    if env:
        return Path(env).expanduser().resolve()
    return Path(platformdirs.user_documents_dir(), DEFAULT_DIR_NAME).resolve()


def workspace_is_initialized(root: Path) -> bool:
    """``True`` iff ``root / "platform.yaml"`` exists and is a file."""
    return (root / "platform.yaml").is_file()
