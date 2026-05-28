"""Session/Run Store errors — spec §4.2."""

from errors.base import PlatformError
from errors.level import Level


class SessionNotFound(PlatformError):
    """No session entity record exists for the given id."""

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
    """No thread entity record exists for the given id."""

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
    """Failed to acquire the JSONL append lock within the budget."""

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
    """Filesystem write failed during event append or session create."""

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
    """A JSONL line failed to parse; names the line number (TC-STR-08)."""

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
