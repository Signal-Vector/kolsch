"""Event Bus errors.

Both classes are Diagnostic because the bus is best-effort — losing a
bus message must not fail an exchange. See the
[error model](../../docs/explanation/error-model.md) for the
classification contract.
"""

from errors.base import PlatformError
from errors.level import Level


class BusPublishFailed(PlatformError):
    """Raised when the bus transport rejected or dropped a publish."""

    code = "bus.publish_failed"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        topic: str,
        transport_error: str,
        *,
        where: str = "bus.publish",
    ) -> None:
        super().__init__(
            message=(f"Publish to topic {topic!r} failed: {transport_error}"),
            where=where,
            detail={"topic": topic, "transport_error": transport_error},
        )


class BusUnavailable(PlatformError):
    """Raised when the bus backend is not reachable."""

    code = "bus.unavailable"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        reason: str,
        *,
        where: str = "bus.connect",
    ) -> None:
        super().__init__(
            message=f"Event bus unavailable: {reason}",
            where=where,
            detail={"reason": reason},
        )
