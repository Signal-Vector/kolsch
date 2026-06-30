"""Memory provider errors.

See the [error model](../../docs/explanation/error-model.md) for the
Diagnostic / Recoverable / Fatal classification each class lives under.
"""

from errors.base import PlatformError
from errors.level import Level


class MemoryWriteDisabled(PlatformError):
    """Raised when `write_tag` is not configured for the bound context."""

    code = "memory.write_disabled"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        agent_id: str,
        reason: str,
        *,
        where: str = "memory.context.write",
    ) -> None:
        super().__init__(
            message=(f"Memory writes disabled for agent {agent_id!r}: {reason}"),
            where=where,
            detail={"agent_id": agent_id, "reason": reason},
        )


class MemoryItemNotFound(PlatformError):
    """Raised when no memory item with the given id exists in the bound session."""

    code = "memory.item_not_found"
    level = Level.DIAGNOSTIC

    def __init__(
        self,
        item_id: str,
        session_id: str,
        *,
        where: str = "memory.context.delete",
    ) -> None:
        super().__init__(
            message=(f"Memory item {item_id!r} not found in session {session_id!r}"),
            where=where,
            detail={"item_id": item_id, "session_id": session_id},
        )


class MemorySessionUnbound(PlatformError):
    """Raised when a memory operation ran against a session that was never bound."""

    code = "memory.session_unbound"
    level = Level.FATAL

    def __init__(
        self,
        session_id: str,
        *,
        where: str = "memory.context.operation",
    ) -> None:
        super().__init__(
            message=f"Memory context for session {session_id!r} is unbound",
            where=where,
            detail={"session_id": session_id},
        )


class MemoryCompactionFailed(PlatformError):
    """Raised when the compaction model call failed.

    Recoverable — the engine may retry compaction against another
    model in the fallback chain.
    """

    code = "memory.compaction_failed"
    level = Level.RECOVERABLE

    def __init__(
        self,
        events_count: int,
        target_tokens: int,
        cause: str,
        *,
        where: str = "memory.context.compact",
    ) -> None:
        super().__init__(
            message=(
                f"Compaction of {events_count} event(s) to {target_tokens} "
                f"tokens failed: {cause}"
            ),
            where=where,
            detail={
                "events_count": events_count,
                "target_tokens": target_tokens,
                "cause": cause,
            },
        )


class MemoryInvalidTag(PlatformError):
    """Raised when `write_tag` contained a wildcard or violated tag rules."""

    code = "memory.invalid_tag"
    level = Level.FATAL

    def __init__(
        self,
        tag: str,
        reason: str,
        *,
        where: str = "memory.validate",
    ) -> None:
        super().__init__(
            message=f"Invalid memory tag {tag!r}: {reason}",
            where=where,
            detail={"tag": tag, "reason": reason},
        )
