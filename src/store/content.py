"""Content sidecar store — immutable hash-addressed payload spill.

Oversized event payloads spill to `<platform-root>/content/<hash>.bin`.
The hash is the SHA-256 hex digest of the content, so the same payload
written twice resolves to the same file and a second write is a no-op.

The on-disk layout and immutability rule belong to the
[session/run store contract](../explanation/session-run-store-contract.md).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from errors import StoreWriteFailed
from schemas import ContentRef


def content_path(root: Path, content_hash: str) -> Path:
    """Return the canonical path for a content entry under `root`."""
    return root / "content" / f"{content_hash}.bin"


def write_content_sync(root: Path, payload: bytes) -> ContentRef:
    """Write `payload` to the content store and return a `ContentRef`.

    Idempotent — when a file already exists at the target path, return
    the existing reference without rewriting. Content is immutable, so
    the same hash always maps to the same bytes.

    **Crash safety.** Writes go to a uniquely-named tempfile in the
    same directory, then `os.rename` atomically moves it into place.
    A process crash mid-write leaves at most a stray tempfile, never
    a partial content file.

    Args:
        root: The platform root containing the `content/` directory.
        payload: The raw bytes to persist.

    Returns:
        A [`ContentRef`][schemas.entities.ContentRef] naming the
        stored payload.

    Raises:
        StoreWriteFailed: The tempfile write or rename failed.
    """
    digest = hashlib.sha256(payload).hexdigest()
    target = content_path(root, digest)
    if target.exists():
        return ContentRef(
            hash=digest,
            size=len(payload),
            stored_at=f"content/{digest}.bin",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic rename — write to a tempfile in the same directory (same
    # filesystem) so rename is atomic, then move into place.
    try:
        fd, tmp = tempfile.mkstemp(
            prefix=f".{digest}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        tmp_path = Path(tmp)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        tmp_path.rename(target)
    except OSError as exc:
        raise StoreWriteFailed(
            session_id="content",
            reason=f"write_content failed: {exc}",
        ) from exc

    return ContentRef(
        hash=digest,
        size=len(payload),
        stored_at=f"content/{digest}.bin",
    )
