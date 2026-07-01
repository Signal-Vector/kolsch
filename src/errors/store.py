"""Session/Run Store errors.

All five classes are Fatal — store failures break the persistence
contract every other subsystem depends on. See the
[error model](../explanation/error-model.md) for the
classification contract.
"""

from errors.base import PlatformError
from errors.level import Level


class SessionNotFound(PlatformError):
    """Raised when no session entity record exists for the given id."""

    code = "session.not_found"
    level = Level.FATAL

    def __init__(
        self,
        session_id: str,
        *,
        where: str = "store.session.get",
    ) -> None:
        super().__init__(
            message=f"Session {session_id!r} not found",
            where=where,
            detail={"session_id": session_id},
        )


class ThreadNotFound(PlatformError):
    """Raised when no thread entity record exists for the given id."""

    code = "thread.not_found"
    level = Level.FATAL

    def __init__(
        self,
        thread_id: str,
        *,
        where: str = "store.thread.get",
    ) -> None:
        super().__init__(
            message=f"Thread {thread_id!r} not found",
            where=where,
            detail={"thread_id": thread_id},
        )


class StoreLockTimeout(PlatformError):
    """Raised when the JSONL append lock could not be acquired in budget."""

    code = "store.lock_timeout"
    level = Level.FATAL

    def __init__(
        self,
        session_id: str,
        timeout_seconds: float,
        *,
        where: str = "store.events.append",
    ) -> None:
        super().__init__(
            message=(
                f"Lock timeout after {timeout_seconds}s appending to "
                f"session {session_id!r}"
            ),
            where=where,
            detail={
                "session_id": session_id,
                "timeout_seconds": timeout_seconds,
            },
        )


class StoreWriteFailed(PlatformError):
    """Raised when a filesystem write failed during event append or session create."""

    code = "store.write_failed"
    level = Level.FATAL

    def __init__(
        self,
        session_id: str,
        reason: str,
        *,
        where: str = "store.events.append",
    ) -> None:
        super().__init__(
            message=(f"Write failed for session {session_id!r}: {reason}"),
            where=where,
            detail={"session_id": session_id, "reason": reason},
        )


class StoreCorruptLog(PlatformError):
    """Raised when a JSONL line failed to parse.

    The envelope's `detail.line_number` names the offending line so
    callers can point a human at the corruption without re-scanning
    the file.
    """

    code = "store.corrupt_log"
    level = Level.FATAL

    def __init__(
        self,
        session_id: str,
        line_number: int,
        parse_error: str,
        *,
        where: str = "store.events.read",
    ) -> None:
        super().__init__(
            message=(
                f"Corrupt log line {line_number} in session "
                f"{session_id!r}: {parse_error}"
            ),
            where=where,
            detail={
                "session_id": session_id,
                "line_number": line_number,
                "parse_error": parse_error,
            },
        )
