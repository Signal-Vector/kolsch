"""Workspace and setup errors.

Two Fatal classes that block bootstrap and two Recoverable classes
that flag setup-time conditions a human can resolve. See the
[error model](../explanation/error-model.md) for the
classification contract.

- `WorkspaceNotInitialized` (Fatal) — a non-setup command was pointed
  at a path with no `platform.yaml`. The CLI intercepts this before
  bootstrap and emits a friendly "run `kolsch setup`" message; the
  class is still raised by API entry points that don't have a
  friendly-print layer.
- `WorkspacePermissionDenied` (Fatal) — `kolsch setup` or the runtime
  bootstrap couldn't create or write a workspace path.
- `SetupAborted` (Recoverable) — the user Ctrl-C'd out of
  `kolsch setup`. Informational; the next setup run picks up where
  the previous one left off.
- `SetupStarterExists` (Recoverable) — the starter-agent file already
  exists and the user declined overwrite.
"""

from __future__ import annotations

from errors.base import PlatformError
from errors.level import Level


class WorkspaceNotInitialized(PlatformError):
    """Raised when the resolved workspace has no `platform.yaml`.

    The path exists but isn't a Kolsch workspace yet; the friendly
    remediation is to run `kolsch setup` against it.
    """

    code = "workspace.not_initialized"
    level = Level.FATAL

    def __init__(
        self,
        path: str,
        *,
        env_var_set: bool,
        where: str = "platform_runtime.workspace",
    ) -> None:
        super().__init__(
            message=(
                f"No Kolsch workspace at {path}. Run `kolsch setup` to create one."
            ),
            where=where,
            detail={"path": path, "env_var_set": env_var_set},
        )


class WorkspacePermissionDenied(PlatformError):
    """Raised when a workspace path was not writable.

    Surfaced by `kolsch setup` and by the runtime when writing to
    `.platform/` fails.
    """

    code = "workspace.permission_denied"
    level = Level.FATAL

    def __init__(
        self,
        path: str,
        operation: str,
        *,
        where: str = "cli.setup",
    ) -> None:
        super().__init__(
            message=(
                f"Permission denied while {operation} {path}. "
                f"Check filesystem permissions."
            ),
            where=where,
            detail={"path": path, "operation": operation},
        )


class SetupAborted(PlatformError):
    """Raised when the user Ctrl-C'd out of `kolsch setup`.

    Informational — the next setup run resumes from the same place.
    """

    code = "setup.aborted"
    level = Level.RECOVERABLE

    def __init__(
        self,
        step: str,
        reason: str,
        *,
        where: str = "cli.setup",
    ) -> None:
        super().__init__(
            message=f"Setup aborted at step {step}: {reason}",
            where=where,
            detail={"step": step, "reason": reason},
        )


class SetupStarterExists(PlatformError):
    """Raised when the starter-agent file exists and the user declined overwrite."""

    code = "setup.starter_exists"
    level = Level.RECOVERABLE

    def __init__(
        self,
        path: str,
        *,
        where: str = "cli.setup.step_d",
    ) -> None:
        super().__init__(
            message=(
                f"Starter agent file {path} already exists; declined to overwrite."
            ),
            where=where,
            detail={"path": path},
        )
