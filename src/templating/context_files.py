"""``context_files`` resolution + loading — Phase 2.2 Step 12
(spec §6).

Two pure-ish functions:

- :func:`resolve_path` — maps a prefixed reference
  (``kolsch:<rel>`` or ``workspace:<rel>``) to an absolute
  :class:`pathlib.Path`. Plain references raise
  :class:`errors.ConfigContextFileUnprefixed`.
- :func:`load_context_file` — reads the file at the resolved
  path subject to the 64 KiB cap, returning a fully-populated
  :class:`templating.vars.docs.DocItem`. Raises
  :class:`errors.ContextFileNotFound` /
  :class:`errors.ContextFileTooLarge` as appropriate.

## `kolsch:` resolution — wheel-vs-source-tree dispatch

Per the E-2.2-001 A3 / pyproject.toml inline note, the wheel
ships ``docs/`` and ``AGENTS.md`` as package data under the
``kolsch`` umbrella; an editable install ships only the empty
``src/kolsch/`` package. The resolver probes
:func:`importlib.resources.files` first; if the resource is
absent AND the runtime is an editable install,
:func:`_repo_root_for_kolsch_prefix` falls back to the repo root
discovered via the ``kolsch`` package's ``__file__``. Same lookup
surface; same paths land; the dispatch is invisible to agent
authors.

``workspace:`` resolution is straightforward — ``<root> / <rel>``.

## Size cap (Phase 2.2 §6.4)

Each context file is capped at 64 KiB. The cap is enforced via
:meth:`pathlib.Path.stat` BEFORE reading the file, so a 100 MiB
file doesn't get pulled into memory just to be rejected. The
:class:`errors.ContextFileTooLarge` error carries the actual
size and the cap so the message is actionable.
"""

from __future__ import annotations

import importlib.resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import TYPE_CHECKING

from errors import (
    ConfigContextFileUnprefixed,
    ContextFileNotFound,
    ContextFileTooLarge,
)

if TYPE_CHECKING:
    from schemas import ContextFileSpec

from templating.vars.docs import DocItem

# Phase 2.2 §6.4 — 64 KiB cap on context files.
DEFAULT_MAX_BYTES = 65_536


# ---------------------------------------------------------------------------
# Path resolution.
# ---------------------------------------------------------------------------


def resolve_path(prefixed_path: str, workspace_root: Path) -> Path:
    """Resolve a ``kolsch:<rel>`` or ``workspace:<rel>`` reference
    to an absolute :class:`pathlib.Path`.

    Plain references raise :class:`ConfigContextFileUnprefixed`
    (Fatal) — spec §6.2's no-ambiguity discipline.

    :raises ConfigContextFileUnprefixed: when the prefix is
        missing or unrecognised.
    """
    if prefixed_path.startswith("workspace:"):
        rel = prefixed_path[len("workspace:") :]
        return (workspace_root / rel).resolve()
    if prefixed_path.startswith("kolsch:"):
        rel = prefixed_path[len("kolsch:") :]
        return _resolve_kolsch_resource(rel)
    raise ConfigContextFileUnprefixed(path=prefixed_path)


def _resolve_kolsch_resource(relative: str) -> Path:
    """Resolve ``kolsch:<relative>`` to an absolute path that
    works in both wheel and editable-install contexts.

    Strategy (per E-2.2-001 A3):

    1. Probe :func:`importlib.resources.files("kolsch")` and join
       the relative path. If the result is a :class:`pathlib.Path`
       (it is in editable mode) or a :class:`importlib.abc
       .Traversable` backed by a filesystem entry that EXISTS,
       return it.
    2. Otherwise (editable install where the umbrella is empty),
       fall back to repo root discovered via the ``kolsch``
       package's ``__file__.parent`` ↓ ``..``.
    """
    pkg_root: Traversable = importlib.resources.files("kolsch")
    candidate = pkg_root / relative
    candidate_path = Path(str(candidate))
    if candidate_path.exists():
        return candidate_path.resolve()
    # Editable-install fallback — walk from src/kolsch up two levels
    # to land at the repo root, then descend by the relative path.
    # src/kolsch/__file__ → <repo>/src/kolsch/__init__.py
    # .parent → src/kolsch
    # .parent.parent → src
    # .parent.parent.parent → <repo>
    import kolsch  # noqa: PLC0415

    repo_root = Path(kolsch.__file__).parent.parent.parent
    fallback = (repo_root / relative).resolve()
    return fallback


# ---------------------------------------------------------------------------
# File loading.
# ---------------------------------------------------------------------------


def load_context_file(
    spec: ContextFileSpec,
    workspace_root: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> DocItem:
    """Resolve + read a single context file; return a populated
    :class:`DocItem`.

    Errors:

    - :class:`ConfigContextFileUnprefixed` — missing prefix.
    - :class:`ContextFileNotFound` — resolved path doesn't exist.
    - :class:`ContextFileTooLarge` — file exceeds ``max_bytes``.

    The ``summary`` fallback (first non-empty line of file) is
    applied here when :attr:`ContextFileSpec.summary` is ``None``.
    """
    resolved = resolve_path(spec.path, workspace_root)
    if not resolved.is_file():
        raise ContextFileNotFound(path=spec.path, resolved=str(resolved))
    size_bytes = resolved.stat().st_size
    if size_bytes > max_bytes:
        raise ContextFileTooLarge(
            path=spec.path,
            size_bytes=size_bytes,
            max_bytes=max_bytes,
        )
    content = resolved.read_text(encoding="utf-8")
    summary = spec.summary if spec.summary is not None else _first_non_empty_line(content)
    return DocItem(
        path=spec.path,
        summary=summary,
        primary=spec.primary,
        content=content,
        size_bytes=size_bytes,
    )


def _first_non_empty_line(content: str) -> str:
    """The :class:`ContextFileSpec.summary` fallback — Phase 2.2
    §6.1. Empty input → empty string (no exception)."""
    for line in content.splitlines():
        if line.strip():
            return line.strip()
    return ""
