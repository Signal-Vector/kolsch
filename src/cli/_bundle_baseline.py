"""``.kolsch/bundle-baseline.json`` writer/reader (qol-089).

Records the sha256 of every file ``kolsch setup`` ships out of
``src/cli/_starter_bundle/kolsch/`` so a later ``kolsch doctor`` (and,
in qol-093, ``kolsch setup --sync-bundle``) can classify each shipped
file as ``unchanged`` / ``locally_modified`` / ``retired-at-source`` /
``added-at-source``.

Schema v1:

.. code-block:: json

    {
      "schema_version": 1,
      "shipped_at": "2026-07-25T18:33:07Z",
      "kolsch_version": "0.0.0",
      "files": {
        "tools/fetch_url.py": "sha256:abc...",
        "agents/helper.yaml": "sha256:def..."
      }
    }

Paths are workspace-relative under ``bundles/kolsch/`` (matching the
``_STARTER_BUNDLE_TEMPLATE_DIR`` layout so a source-vs-baseline diff
can key on the same string).

Write is atomic (temp file + fsync + rename) so a mid-write crash
never leaves a corrupt baseline that would confuse the next
``kolsch doctor`` run.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata as _metadata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


_BASELINE_REL = ".kolsch/bundle-baseline.json"
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class BundleBaseline:
    """Parsed ``.kolsch/bundle-baseline.json``.

    ``files`` maps relative bundle paths (e.g. ``tools/fetch_url.py``,
    ``agents/helper.yaml``) to ``sha256:<hex>`` strings. Callers use
    it as the "unmodified since last shipped" oracle.
    """

    schema_version: int
    shipped_at: str
    kolsch_version: str
    files: dict[str, str]


def baseline_path(workspace_root: Path) -> Path:
    """Return the absolute path to the workspace's baseline JSON."""
    return workspace_root / _BASELINE_REL


def baseline_exists(workspace_root: Path) -> bool:
    """True when the workspace has a recorded baseline."""
    return baseline_path(workspace_root).is_file()


def write_baseline(
    workspace_root: Path,
    *,
    bundle_dir: Path,
    shipped_at: str | None = None,
) -> BundleBaseline:
    """Compute sha256 for every file in ``bundle_dir`` and write the baseline.

    ``bundle_dir`` is the on-disk copy of ``bundles/kolsch/`` (i.e.
    the destination ``shutil.copytree`` wrote to). Every non-directory
    file under it is hashed; the resulting map is written atomically
    to ``<workspace_root>/.kolsch/bundle-baseline.json``.

    Args:
        workspace_root: Absolute workspace root.
        bundle_dir: Absolute path to the workspace's ``bundles/kolsch/``.
        shipped_at: ISO-8601 timestamp to record. Defaults to
            ``datetime.now(UTC).isoformat()``.

    Returns:
        The parsed :class:`BundleBaseline` just written.
    """
    files: dict[str, str] = {}
    if bundle_dir.is_dir():
        for path in sorted(bundle_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(bundle_dir))
            files[rel] = f"sha256:{_sha256_file(path)}"
    stamp = shipped_at or datetime.now(UTC).isoformat()
    baseline = BundleBaseline(
        schema_version=_SCHEMA_VERSION,
        shipped_at=stamp,
        kolsch_version=_current_kolsch_version(),
        files=files,
    )
    _write_atomic(
        baseline_path(workspace_root),
        json.dumps(
            {
                "schema_version": baseline.schema_version,
                "shipped_at": baseline.shipped_at,
                "kolsch_version": baseline.kolsch_version,
                "files": baseline.files,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return baseline


def read_baseline(workspace_root: Path) -> BundleBaseline | None:
    """Return the parsed baseline, or ``None`` if the file is absent/malformed.

    Malformed shapes (wrong schema version, non-dict payload, etc.)
    return ``None`` so callers can treat "no baseline" and "corrupt
    baseline" identically — both surface as "run `kolsch setup` to
    record".
    """
    path = baseline_path(workspace_root)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("schema_version") != _SCHEMA_VERSION:
        return None
    files = raw.get("files")
    if not isinstance(files, dict):
        return None
    return BundleBaseline(
        schema_version=_SCHEMA_VERSION,
        shipped_at=str(raw.get("shipped_at", "")),
        kolsch_version=str(raw.get("kolsch_version", "")),
        files={str(k): str(v) for k, v in files.items()},
    )


def sha256_file(path: Path) -> str:
    """Return ``sha256:<hex>`` for ``path`` — the baseline's on-disk format."""
    return f"sha256:{_sha256_file(path)}"


def _sha256_file(path: Path) -> str:
    """Return the plain hex sha256 of ``path``'s contents."""
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _current_kolsch_version() -> str:
    """Best-effort kolsch package version; falls back to ``"unknown"``."""
    try:
        return _metadata.version("kolsch")
    except _metadata.PackageNotFoundError:
        return "unknown"


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + fsync + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
