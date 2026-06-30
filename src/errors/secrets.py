"""Secrets provider errors.

Both classes are Fatal — a missing or unreadable secret means a tool
that needs it cannot be set up and the agent cannot run. See the
[error model](../../docs/explanation/error-model.md) for the
classification contract.
"""

from errors.base import PlatformError
from errors.level import Level


class SecretNotFound(PlatformError):
    """Raised when the named secret is not available."""

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
    """Raised when the secrets source could not be read."""

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
