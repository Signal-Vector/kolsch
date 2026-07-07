"""JSONL append, read, leaf resolution, and partial-line recovery.

Implements the on-disk event-log surface of the
[session/run store contract](../explanation/session-run-store-contract.md).
All filesystem operations are synchronous and small;
[`SessionStore`][store.session_store.SessionStore] wraps them in
`asyncio.to_thread` so the event loop is never blocked on disk I/O.
`flock` itself remains a blocking syscall.
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


# Events whose loss would make the agent "forget what it just did" are
# fsync-critical. Engine-emitted progress events MAY skip fsync for
# performance.
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
    """Return the canonical JSONL path for `session_id` under `root`."""
    return root / "sessions" / f"{session_id}.jsonl"


def lock_path(root: Path, session_id: SessionId) -> Path:
    """Return the sidecar `.lock` path for `session_id` under `root`."""
    return root / "sessions" / f"{session_id}.jsonl.lock"


def _serialise(event: Event) -> bytes:
    """Render an event as a JSONL line — byte-stable across runs.

    The byte representation is load-bearing for two callers:

    - Golden event-log tests snapshot the JSONL output and compare
      against committed files; any change to the byte representation
      invalidates every golden file.
    - The partial-final-line recovery probes the file's last bytes;
      ambiguity here means corrupt detection misfires.

    We therefore do NOT use `Event.model_dump_json` directly, because
    Pydantic v2 emits keys in field-declaration order and a future
    field reorder would silently change every line's bytes. Instead:
    dump to a JSON-ready dict via Pydantic (handles datetime, etc.),
    then re-encode through the stdlib with `sort_keys=True` and
    compact separators. The byte output is insensitive to Pydantic
    field order AND to nested-dict insertion order. Re-reading and
    re-serialising the same Event yields byte-identical output.
    """
    payload = event.model_dump(mode="json")
    # `trace_id` is additive on the envelope. Strip it from the
    # on-disk row when absent so chat / API sessions (which leave it
    # `None`) produce JSONL lines byte-identical to the pre-trace_id
    # shape — the golden snapshots committed before this field landed
    # stay valid. Spawned-from-workflow sessions DO carry it and the
    # key surfaces in their lines verbatim.
    if payload.get("trace_id") is None:
        payload.pop("trace_id", None)
    # spec-002 — `logical_seq` is additive on `_DataModel`. It defaults
    # to 0; populated values arrive from SessionStore in spec-003.
    # Strip the key from `data` when the default is in force so JSONL
    # rows produced by pre-spec-003 emitters remain byte-identical to
    # the pre-spec-002 shape. Non-zero values (once spec-003 lands)
    # continue to surface verbatim.
    data = payload.get("data")
    if isinstance(data, dict) and data.get("logical_seq") == 0:
        data.pop("logical_seq", None)
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _parse_line(raw: str, *, session_id: SessionId, line_number: int) -> Event:
    """Parse one JSONL line into an `Event` or raise `StoreCorruptLog`."""
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
    r"""Return the byte offset of the last `\n` in `path`, or `None`."""
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
    """Read the bytes from `position` to EOF."""
    with path.open("rb") as f:
        f.seek(position)
        return f.read()


def _parses_as_json(payload: bytes) -> bool:
    try:
        json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def _read_last_logical_seq(path: Path) -> int:
    """Return the `logical_seq` on the JSONL's last non-empty line, or 0.

    Called by [`append_event_sync`][store.jsonl.append_event_sync] under
    the per-session `flock` immediately before a new event is stamped
    with `tail + 1`. Reads the last non-empty line's `data.logical_seq`
    field (defaulting to `0` if the key is absent — legacy events
    written before spec-002 didn't carry it), returns 0 on any parse
    failure.

    Coordinator decision #1 for spec-003: the JSONL tail is
    authoritative under `flock`. The `SessionMeta.logical_seq_bookmark`
    field is reserved for a v1.1 perf optimisation but currently
    unused; this helper re-reads the tail on every append.

    Coordinator decision #2 for spec-003: kept inline in `jsonl.py`
    rather than extracted to a separate module. It's a small helper
    with exactly one call site.

    Args:
        path: JSONL file path. Missing / empty files return 0 (the
            next append will stamp `logical_seq=1`).

    Returns:
        The tail's `logical_seq` value, or 0 for empty / missing /
        parse-failing files.
    """
    if not path.exists():
        return 0
    size = path.stat().st_size
    if size == 0:
        return 0
    # Find the byte offset of the last line by locating the newline
    # right before EOF (or the last newline if the file doesn't end in
    # `\n`). `_find_last_newline` returns the offset of the trailing
    # `\n`; the last complete line lives before it.
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        # Walk back to find the newline that terminates the last
        # complete line. If the file ends with `\n`, that's `end - 1`;
        # if it doesn't, the last line is a partial (which
        # `recover_partial_final_line` will have truncated before we
        # get here) so we scan for the previous `\n`.
        pos = end - 1
        # Skip trailing newline if present.
        while pos >= 0:
            f.seek(pos)
            byte = f.read(1)
            if byte != b"\n":
                break
            pos -= 1
        # Now scan back to the previous newline (start of the last
        # line's content). If we hit BOF first, the whole file is one
        # line.
        line_end = pos + 1  # exclusive end of the last line's bytes
        while pos >= 0:
            f.seek(pos)
            byte = f.read(1)
            if byte == b"\n":
                pos += 1
                break
            pos -= 1
        line_start = max(0, pos)
        f.seek(line_start)
        raw = f.read(line_end - line_start)
    if not raw:
        return 0
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0
    data = payload.get("data")
    if not isinstance(data, dict):
        return 0
    seq = data.get("logical_seq", 0)
    if not isinstance(seq, int):
        return 0
    return seq


def recover_partial_final_line(path: Path) -> None:
    """Truncate a partial last line at file open.

    Idempotent — running twice on a well-formed file does nothing the
    second time. Run on every file open before reads or appends so a
    crashed mid-line write does not poison subsequent operations.
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
    """Append `event` to the session's JSONL under a per-session lock.

    Resolves the thread leaf under the lock, stamps the appended
    event's `parent_event_id`, and writes the line with `O_APPEND` so
    the kernel positions the bytes at EOF atomically. fsyncs the file
    descriptor for events in the durability-critical set; lower-stakes
    progress events skip fsync for throughput.

    Args:
        root: Platform root containing the `sessions/` directory.
        session_id: The session whose JSONL receives the event.
        thread_id: The thread whose leaf the append chains onto.
        event: The event to write. `parent_event_id` is filled in by
            this call; callers leave it `None`.

    Returns:
        The appended event with `parent_event_id` populated.

    Raises:
        StoreWriteFailed: The `flock`, write, or fsync failed, or the
            write was short.
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

        # Recover any partial line BEFORE computing leaf.
        recover_partial_final_line(jsonl)

        # Compute leaf under the lock.
        leaf = read_last_event_for_thread(jsonl, thread_id)

        # spec-003: allocate `logical_seq` inside the flock critical
        # section. Read the current JSONL tail (any thread — the seq
        # is per-session, not per-thread) and stamp `tail + 1` on the
        # event's data payload. This is what makes the counter strictly
        # monotonic per session across concurrent appends.
        last_seq = _read_last_logical_seq(jsonl)
        new_seq = last_seq + 1
        # `Event.data` is a plain `dict[str, Any]` on the envelope, so
        # merge the new seq via a fresh dict rather than the pydantic
        # `_DataModel` copy pattern (the sketch in the spec assumed a
        # typed data model; the codebase carries dicts).
        data_dict = dict(event.data) if isinstance(event.data, dict) else {}
        data_dict["logical_seq"] = new_seq

        appended = event.model_copy(
            update={
                "parent_event_id": leaf.event_id if leaf else None,
                "data": data_dict,
            },
        )

        line = _serialise(appended)
        if not jsonl.exists():
            jsonl.parent.mkdir(parents=True, exist_ok=True)

        # Open with O_APPEND so the kernel positions writes at EOF
        # atomically.
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
    """Scan from EOF for the most recent event of `thread_id`.

    Returns `None` when the file does not exist, is empty, or contains
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
    """Yield `(line_text, 1-indexed_line_number)` for every non-empty line."""
    with path.open(encoding="utf-8") as f:
        for index, raw in enumerate(f, start=1):
            stripped = raw.rstrip("\n")
            if not stripped:
                continue
            yield stripped, index


def _session_id_from_path(path: Path) -> SessionId:
    """Derive a `SessionId` from the JSONL file name.

    Used only for surfacing in `StoreCorruptLog` messages — no
    validation since we control the filename.
    """
    from schemas import SessionId as _SessionId  # noqa: PLC0415

    return _SessionId(path.stem)


def read_lines(path: Path) -> Iterator[Event]:
    """Yield every event in `path` in file order.

    Raises [`StoreCorruptLog`][errors.StoreCorruptLog] on any malformed
    mid-file line (with 1-indexed line number). The final partial line,
    if any, is recovered by `recover_partial_final_line`; callers
    should invoke that before reading if they suspect a crash.
    """
    if not path.exists():
        return
    session_id = _session_id_from_path(path)
    for raw, line_number in _iter_lines(path):
        yield _parse_line(raw, session_id=session_id, line_number=line_number)


def read_first_event(path: Path) -> dict[str, Any] | None:
    """Return the first line's JSON payload, or `None` for empty files.

    Used by `reconstruct_from_filesystem` to peek at `session.opened`
    without triggering Event validation on the rest of the file.
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
    """Delete `path` if it exists; ignore ENOENT."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return
        raise
