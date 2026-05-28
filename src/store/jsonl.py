"""JSONL append, read, leaf, and recovery — spec §4 + §5.

All filesystem operations are sync and small. The :class:`SessionStore`
wraps these in :func:`asyncio.to_thread` so the event loop is never
blocked on disk I/O (spec §I10). ``flock`` itself remains a blocking
syscall per the spec's allowance.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from typing import TYPE_CHECKING, Any

from errors import StoreCorruptLog, StoreWriteFailed
from schemas import Event

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from schemas import SessionId, ThreadId


# Per spec §5.2: events whose loss would make the agent "forget what
# it just did" are fsync-critical. Engine-emitted progress events MAY
# skip fsync for performance.
_FSYNC_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "agent.message",
        "tool.result",
        "tool.failed",
        "model.response",
        "config.set",
        "session.opened",
        "thread.exchange_completed",
        "thread.exchange_failed",
    }
)


def jsonl_path(root: Path, session_id: SessionId) -> Path:
    """The canonical JSONL path for a session — spec §3.1."""
    return root / "sessions" / f"{session_id}.jsonl"


def lock_path(root: Path, session_id: SessionId) -> Path:
    """The sidecar `.lock` path for a session — spec §3.1."""
    return root / "sessions" / f"{session_id}.jsonl.lock"


def _serialise(event: Event) -> bytes:
    """Render an event as a JSONL line (one JSON object + ``\\n``)."""
    return event.model_dump_json().encode("utf-8") + b"\n"


def _parse_line(raw: str, *, session_id: SessionId, line_number: int) -> Event:
    """Parse one JSONL line into an :class:`Event` or raise StoreCorruptLog."""
    try:
        payload = json.loads(raw)
        return Event.model_validate(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        # Pydantic raises ValueError-typed validation errors; treat
        # both as corruption (we are at a mid-file line, not the
        # recoverable final partial line).
        raise StoreCorruptLog(
            session_id=str(session_id),
            line_number=line_number,
            parse_error=str(exc),
        ) from exc


def _find_last_newline(path: Path, size: int) -> int | None:
    """Return the byte offset of the last `\\n` in ``path``, or None."""
    chunk_size = 4096
    with path.open("rb") as f:
        offset = max(0, size - chunk_size)
        while True:
            f.seek(offset)
            chunk = f.read(min(chunk_size, size - offset))
            rel = chunk.rfind(b"\n")
            if rel >= 0:
                return offset + rel
            if offset == 0:
                return None
            offset = max(0, offset - chunk_size)


def _tail_after(path: Path, position: int) -> bytes:
    """Read the bytes from ``position`` to EOF."""
    with path.open("rb") as f:
        f.seek(position)
        return f.read()


def _parses_as_json(payload: bytes) -> bool:
    try:
        json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def recover_partial_final_line(path: Path) -> None:
    """Truncate a partial last line at file open — spec §5.3.

    Idempotent: running twice on a well-formed file does nothing the
    second time. Run on every file open before reads/appends.
    """
    if not path.exists():
        return
    size = path.stat().st_size
    if size == 0:
        return

    last_newline = _find_last_newline(path, size)
    if last_newline is None:
        # No newline anywhere → the whole file is one partial line.
        if not _parses_as_json(_tail_after(path, 0)):
            with path.open("ab") as f:
                f.truncate(0)
        return

    tail_start = last_newline + 1
    tail = _tail_after(path, tail_start)
    if not tail:
        return  # File ends with `\n` — well-formed.
    if not _parses_as_json(tail):
        with path.open("ab") as f:
            f.truncate(tail_start)


def append_event_sync(
    *,
    root: Path,
    session_id: SessionId,
    thread_id: ThreadId,
    event: Event,
) -> Event:
    """Lock-serialised, leaf-resolved, atomic-or-recoverable append.

    Returns the appended event with ``parent_event_id`` filled in.
    """
    jsonl = jsonl_path(root, session_id)
    lock = lock_path(root, session_id)
    lock.parent.mkdir(parents=True, exist_ok=True)

    # Acquire the per-session lock. Closing the fd releases.
    with lock.open("ab") as lock_fd:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise StoreWriteFailed(
                session_id=str(session_id),
                reason=f"flock failed: {exc}",
            ) from exc

        # Step §5.3 — recover any partial line BEFORE computing leaf.
        recover_partial_final_line(jsonl)

        # Step §4.1.3 — compute leaf under the lock.
        leaf = read_last_event_for_thread(jsonl, thread_id)
        appended = event.model_copy(
            update={"parent_event_id": leaf.event_id if leaf else None},
        )

        line = _serialise(appended)
        if not jsonl.exists():
            jsonl.parent.mkdir(parents=True, exist_ok=True)

        # Open with O_APPEND so the kernel positions writes at EOF
        # atomically (spec §5.1).
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        fd = os.open(jsonl, flags, 0o644)
        try:
            written = os.write(fd, line)
            if written != len(line):
                # Best-effort: the partial-line recovery on the next
                # open will truncate; raise so the engine knows the
                # append did not complete.
                raise StoreWriteFailed(
                    session_id=str(session_id),
                    reason=f"short write: {written} of {len(line)} bytes",
                )
            if appended.type.value in _FSYNC_EVENT_TYPES:
                os.fsync(fd)
        except OSError as exc:
            raise StoreWriteFailed(
                session_id=str(session_id),
                reason=f"write failed: {exc}",
            ) from exc
        finally:
            os.close(fd)

        return appended


def read_last_event_for_thread(
    path: Path,
    thread_id: ThreadId,
) -> Event | None:
    """Scan back from EOF for the most recent event of ``thread_id``.

    Returns ``None`` if the file does not exist, is empty, or contains
    no events for that thread.
    """
    if not path.exists():
        return None
    last: Event | None = None
    for raw, line_number in _iter_lines(path):
        ev = _parse_line(
            raw,
            session_id=_session_id_from_path(path),
            line_number=line_number,
        )
        if ev.thread_id == thread_id:
            last = ev
    return last


def _iter_lines(path: Path) -> Iterator[tuple[str, int]]:
    """Yield (line_text, 1-indexed_line_number) for every non-empty line."""
    with path.open(encoding="utf-8") as f:
        for index, raw in enumerate(f, start=1):
            stripped = raw.rstrip("\n")
            if not stripped:
                continue
            yield stripped, index


def _session_id_from_path(path: Path) -> SessionId:
    """Derive a SessionId from the JSONL file name.

    Used only for surfacing in :class:`StoreCorruptLog` messages — no
    validation since we control the filename.
    """
    from schemas import SessionId as _SessionId  # noqa: PLC0415

    return _SessionId(path.stem)


def read_lines(path: Path) -> Iterator[Event]:
    """Yield every event in file order — spec §2.3 ``read_events``.

    Raises :class:`StoreCorruptLog` on any malformed mid-file line
    (with 1-indexed line number). The final partial line, if any, is
    recovered by :func:`recover_partial_final_line`; callers should
    invoke that before reading if they suspect a crash.
    """
    if not path.exists():
        return
    session_id = _session_id_from_path(path)
    for raw, line_number in _iter_lines(path):
        yield _parse_line(raw, session_id=session_id, line_number=line_number)


def read_first_event(path: Path) -> dict[str, Any] | None:
    """Return the first line's JSON payload (or ``None`` for empty files).

    Used by reconstruct to peek at ``session.opened`` without
    triggering Event validation on the rest of the file.
    """
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        first = f.readline().strip()
    if not first:
        return None
    try:
        result = json.loads(first)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def safe_unlink(path: Path) -> None:
    """Delete a file if it exists; ignore ENOENT."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return
        raise
