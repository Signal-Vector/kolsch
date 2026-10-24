"""``.kolsch/bundle-baseline.json`` writer/reader (qol-089, bug-146).

Records the sha256 of every file ``kolsch setup`` ships out of
``src/cli/_starter_bundle/kolsch/`` so ``kolsch doctor`` and
``kolsch setup --sync-bundle`` can tell a shipped file the user never
touched from one they edited.

Two writers:

- [`write_baseline`][cli._bundle_baseline.write_baseline] — the fresh
  install: hashes the whole bundle just copied.
- [`update_baseline_entries`][cli._bundle_baseline.update_baseline_entries]
  — ``--sync-bundle``: merges one entry per file sync wrote or found
  identical to source, even on a partial run, and creates the file on
  a workspace that never had one (bug-146). Files sync left alone keep
  their prior entry, or stay absent.

Which files count is decided by one predicate,
[`is_shipped_bundle_file`][cli._bundle_baseline.is_shipped_bundle_file]
(skips ``__pycache__/`` and ``.DS_Store``), which the baseline writers,
sync and doctor all walk with via
[`iter_bundle_files`][cli._bundle_baseline.iter_bundle_files].

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

# bug-146 — never part of a shipped bundle: bytecode the importer writes
# next to tool files, and macOS Finder metadata.
_IGNORED_DIR_NAMES = frozenset({"__pycache__"})
_IGNORED_FILE_NAMES = frozenset({".DS_Store"})


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


def is_shipped_bundle_file(path: Path, root: Path) -> bool:
    """Return True when ``path`` is a bundle file kolsch ships and tracks.

    ``path`` must lie under ``root`` (the bundle directory). Files under
    a ``__pycache__/`` directory and ``.DS_Store`` files are not: they
    are never in the packaged template, so counting them would show
    them as "retired at source" (and ``--sync-bundle --yes`` would
    delete them).

    Args:
        path: A path under ``root``.
        root: The bundle directory (the template or ``bundles/kolsch/``).

    Returns:
        Whether ``path`` is a regular file that belongs to the bundle.
    """
    if not path.is_file():
        return False
    rel_parts = path.relative_to(root).parts
    if any(part in _IGNORED_DIR_NAMES for part in rel_parts[:-1]):
        return False
    return path.name not in _IGNORED_FILE_NAMES


def iter_bundle_files(root: Path) -> set[str]:
    """Return every shipped bundle file under ``root``, as relative POSIX paths.

    Applies [`is_shipped_bundle_file`][cli._bundle_baseline.is_shipped_bundle_file].
    A missing ``root`` yields an empty set.

    Args:
        root: The bundle directory to walk.

    Returns:
        Paths relative to ``root`` (e.g. ``tools/bash.py``).
    """
    if not root.is_dir():
        return set()
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if is_shipped_bundle_file(path, root)
    }


def write_baseline(
    workspace_root: Path,
    *,
    bundle_dir: Path,
    shipped_at: str | None = None,
) -> BundleBaseline:
    """Compute sha256 for every file in ``bundle_dir`` and write the baseline.

    ``bundle_dir`` is the on-disk copy of ``bundles/kolsch/`` (i.e.
    the destination ``shutil.copytree`` wrote to). Every file
    [`iter_bundle_files`][cli._bundle_baseline.iter_bundle_files] yields is
    hashed; the resulting map is written atomically to
    ``<workspace_root>/.kolsch/bundle-baseline.json``.

    Args:
        workspace_root: Absolute workspace root.
        bundle_dir: Absolute path to the workspace's ``bundles/kolsch/``.
        shipped_at: ISO-8601 timestamp to record. Defaults to
            ``datetime.now(UTC).isoformat()``.

    Returns:
        The parsed :class:`BundleBaseline` just written.
    """
    files = {
        rel: sha256_file(bundle_dir / rel)
        for rel in sorted(iter_bundle_files(bundle_dir))
    }
    return _store(workspace_root, files=files, shipped_at=shipped_at)


def update_baseline_entries(
    workspace_root: Path,
    entries: dict[str, str | None],
) -> bool:
    """Merge per-file entries into the baseline, creating it if absent.

    ``--sync-bundle`` calls this after every non-dry-run pass, partial
    or not, with one entry per file it copied, refreshed or overwrote
    or found identical to source (``sha256:<hex>``), and ``None`` for
    each file it deleted. Paths not in ``entries`` keep their recorded
    hash — or stay unrecorded — so a locally modified file sync kept
    is still "modified" next run.

    Writes (atomically) only when the merge changes the recorded files
    or no readable baseline exists yet; a corrupt baseline is replaced.
    An existing ``shipped_at`` is kept (it records when the baseline
    was first written); ``kolsch_version`` is the version that last
    wrote it.

    Args:
        workspace_root: Absolute workspace root.
        entries: ``{relative bundle path: "sha256:<hex>" | None}``.

    Returns:
        True when the baseline file was written.
    """
    current = read_baseline(workspace_root)
    files = dict(current.files) if current is not None else {}
    for rel, digest in entries.items():
        if digest is None:
            files.pop(rel, None)
        else:
            files[rel] = digest
    if current is not None and files == current.files:
        return False
    _store(
        workspace_root,
        files=dict(sorted(files.items())),
        shipped_at=current.shipped_at if current is not None else None,
    )
    return True


def _store(
    workspace_root: Path,
    *,
    files: dict[str, str],
    shipped_at: str | None,
) -> BundleBaseline:
    """Write ``files`` as the workspace's baseline and return it."""
    baseline = BundleBaseline(
        schema_version=_SCHEMA_VERSION,
        shipped_at=shipped_at or datetime.now(UTC).isoformat(),
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
    baseline" identically — both mean "classify against the current
    source", and the next ``kolsch setup --sync-bundle`` records a
    fresh one.
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
