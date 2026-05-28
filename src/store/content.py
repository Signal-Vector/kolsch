"""Content sidecar store — spec §2.3 + §I11 (immutability).

Oversized event payloads spill to ``<platform-root>/content/<hash>.bin``.
The hash is the SHA-256 hex digest of the content; the same content
written twice is the same file (write_content is idempotent and a
second write is a no-op).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from errors import StoreWriteFailed
from schemas import ContentRef


def content_path(root: Path, content_hash: str) -> Path:
    """The canonical path for a content entry — spec §2.3."""
    return root / "content" / f"{content_hash}.bin"


def write_content_sync(root: Path, payload: bytes) -> ContentRef:
    """Write ``payload`` to the content store, returning a :class:`ContentRef`.

    Idempotent: if a file already exists at the target path, the
    function returns the existing reference without rewriting. The
    content is immutable per §I11.

    Crash-safety: writes go to a uniquely-named tempfile in the same
    directory, then ``os.rename`` atomically moves it into place. A
    process crash mid-write leaves at most a stray tempfile, never a
    partial content file.
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
