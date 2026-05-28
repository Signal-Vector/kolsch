"""Secrets provider errors — spec §4.7.

Secrets are Fatal because a missing/unreadable secret means a tool
that needs it cannot be set up; the agent can't run.
"""

from errors.base import PlatformError
from errors.level import Level


class SecretNotFound(PlatformError):
    """The named secret is not available (TC-SEC-03)."""

    code = "secret.not_found"
    level = Level.FATAL

    def __init__(
        self,
        name: str,
        *,
        where: str = "secrets.dotenv.resolve",
    ) -> None:
        super().__init__(
            message=f"Secret {name!r} is not available",
            where=where,
            detail={"name": name},
        )


class SecretReadFailed(PlatformError):
    """The secrets source could not be read (e.g., .env file unreadable)."""

    code = "secret.read_failed"
    level = Level.FATAL

    def __init__(
        self,
        path: str,
        os_error: str,
        *,
        where: str = "secrets.dotenv.load",
    ) -> None:
        super().__init__(
            message=f"Failed to read secrets from {path!r}: {os_error}",
            where=where,
            detail={"path": path, "os_error": os_error},
        )
