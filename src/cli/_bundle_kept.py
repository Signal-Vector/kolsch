"""`.kolsch/bundle-kept.json` — shipped files the user chose to keep (bug-146).

When an interactive `kolsch setup --sync-bundle` asks about a file in
`bundles/kolsch/` — one that differs from the shipped version, or one
the source no longer ships — and the user answers `[k]eep`, the answer
is remembered here, keyed by the file's current sha256 and the current
source's sha256 (`None` for a file the source doesn't ship). While both
still match, sync doesn't ask again, and the `kolsch setup` re-run
notice and `kolsch doctor`'s drift count leave the file out (doctor
lists it as "kept local", informational). Change either the file or
the shipped version and it is asked about again.

Deliberately separate from `.kolsch/bundle-baseline.json`: a kept file's
baseline entry is untouched (absent or its prior value), so it is never
mistaken for an unedited shipped file and overwritten. `--yes` / `--no`
never record anything here. Unreadable or malformed reads as empty
("ask again"); writes are atomic.

Schema v1:

.. code-block:: json

    {
      "schema_version": 1,
      "files": {
        "agents/helper.yaml": {
          "file": "sha256:…",
          "source": "sha256:…",
          "at": "2026-10-24T12:00:00+00:00"
        }
      }
    }
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cli._bundle_baseline import _write_atomic

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

KeptKey = tuple[str, str | None]
"""`(file sha256, source sha256 or None)` as `sha256:<hex>` strings."""

_KEPT_REL = ".kolsch/bundle-kept.json"
_SCHEMA_VERSION = 1


def kept_path(workspace_root: Path) -> Path:
    """Return the absolute path to the workspace's kept-files record."""
    return workspace_root / _KEPT_REL


def read_kept(workspace_root: Path) -> dict[str, KeptKey]:
    """Return `{path: (file sha, source sha)}`; `{}` when absent or corrupt."""
    try:
        raw = json.loads(kept_path(workspace_root).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        return {}
    files = raw.get("files")
    if not isinstance(files, dict):
        return {}
    out: dict[str, KeptKey] = {}
    for rel, value in files.items():
        if not isinstance(value, dict) or not isinstance(value.get("file"), str):
            continue
        source = value.get("source")
        out[str(rel)] = (value["file"], source if isinstance(source, str) else None)
    return out


def update_kept(
    workspace_root: Path,
    *,
    keep: Mapping[str, KeptKey],
    forget: Iterable[str] = (),
) -> None:
    """Record `keep` and drop `forget` (atomic; no write when nothing changes)."""
    current = read_kept(workspace_root)
    files = dict(current)
    for rel in forget:
        files.pop(rel, None)
    files.update(keep)
    if files == current:
        return
    stamp = datetime.now(UTC).isoformat()
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "files": {
            rel: {"file": file_sha, "source": source_sha, "at": stamp}
            for rel, (file_sha, source_sha) in sorted(files.items())
        },
    }
    _write_atomic(
        kept_path(workspace_root),
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
