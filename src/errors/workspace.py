"""Workspace + setup errors — spec §4.14 (Phase 1.2, UX-R6).

Four error classes:

- :class:`WorkspaceNotInitialized` (Fatal) — a non-setup command was
  pointed at a path with no ``platform.yaml``. The CLI intercepts this
  before bootstrap and emits the friendly "run ``kolsch setup``" message
  per Phase 1.2 patch §1.4; the class is still raised by API entry
  points that don't have a friendly-print layer.
- :class:`WorkspacePermissionDenied` (Fatal) — `kolsch setup` step B or
  the runtime bootstrap couldn't create / write a workspace path.
- :class:`SetupAborted` (Recoverable) — informational; user Ctrl-C'd
  out of `kolsch setup`. Never blocks anything; the next setup run
  picks up where the previous one left off.
- :class:`SetupStarterExists` (Recoverable) — step D's target file
  already exists and the user declined overwrite.
"""

from __future__ import annotations

from errors.base import PlatformError
from errors.level import Level


class WorkspaceNotInitialized(PlatformError):
    """The resolved workspace has no ``platform.yaml`` — the path is
    not a Kolsch workspace yet."""

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
    """A workspace path was not writable — surfaced by `kolsch setup`
    step B and by the runtime when writing to ``.platform/`` fails."""

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
    """User Ctrl-C'd out of `kolsch setup`. Informational only — the
    next setup run resumes from the same place."""

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
    """Step D's target starter-agent file already exists and the user
    declined overwrite."""

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
