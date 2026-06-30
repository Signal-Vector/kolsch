"""Context-file resolution and loading for agent prompts.

Two pure-ish functions:
[`resolve_path`][templating.context_files.resolve_path] maps a path
reference to an absolute `pathlib.Path` (accepting `kolsch:<rel>` for
package resources, `workspace:<rel>` as a deprecated form, and plain
`<rel>` workspace-rooted by default), and
[`load_context_file`][templating.context_files.load_context_file]
reads the file at the resolved path subject to a 64 KiB cap and
returns a populated
[`DocItem`][templating.vars.docs.DocItem].

**`kolsch:` resolution.** The wheel ships `docs/` and `AGENTS.md` as
package data under the `kolsch` umbrella; an editable install ships
only the empty `src/kolsch/` package. The resolver probes
`importlib.resources.files` first; if the resource is absent and the
runtime is an editable install, it falls back to the repo root
discovered via the `kolsch` package's `__file__`. Same lookup surface;
same paths land; the dispatch is invisible to agent authors.

**Size cap.** Each context file is capped at 64 KiB. The cap is
enforced via `pathlib.Path.stat` before reading, so a 100 MiB file
doesn't get pulled into memory just to be rejected.
[`ContextFileTooLarge`][errors.ContextFileTooLarge] carries the actual
size and the cap so the message is actionable.
"""

from __future__ import annotations

import importlib.resources
import logging
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from errors import (
    ConfigDeprecatedRefForm,
    ContextFileNotFound,
    ContextFileTooLarge,
)

if TYPE_CHECKING:
    from schemas import ContextFileSpec

from templating.vars.docs import DocItem

_log = logging.getLogger("templating.context_files")


def _log_deprecated_ref(warning: ConfigDeprecatedRefForm) -> None:
    """Default `emit_warning` sink — log the deprecation at WARNING level.

    Surfaces in `kolsch run -v` and pytest log captures so the
    `workspace:` prefix deprecation isn't silent for operators.
    """
    _log.warning("%s", warning)


# 64 KiB cap on a single context file's payload.
DEFAULT_MAX_BYTES = 65_536


# ---------------------------------------------------------------------------
# Path resolution.
# ---------------------------------------------------------------------------


def resolve_path(
    prefixed_path: str,
    workspace_root: Path,
    *,
    emit_warning: Callable[[ConfigDeprecatedRefForm], None] | None = None,
) -> Path:
    """Resolve a `context_files[].path` or `extends:` value to an absolute path.

    Three prefix forms, in order: `kolsch:<rel>` is a kolsch-package
    resource lookup; `workspace:<rel>` is a workspace-root lookup that
    emits a [`ConfigDeprecatedRefForm`][errors.ConfigDeprecatedRefForm]
    warning since the prefix is now implicit; and a plain `<rel>`
    resolves under the workspace root as the canonical form.

    Args:
        prefixed_path: The reference string from the agent YAML.
        workspace_root: The workspace root for plain and `workspace:`
            forms.
        emit_warning: Deprecation-warning sink. Defaults to logging at
            WARNING level via the module logger; tests pass a
            list-append to assert the warning fires.

    Returns:
        The resolved absolute `pathlib.Path`.
    """
    sink = emit_warning if emit_warning is not None else _log_deprecated_ref
    if prefixed_path.startswith("kolsch:"):
        rel = prefixed_path[len("kolsch:") :]
        return _resolve_kolsch_resource(rel)
    if prefixed_path.startswith("workspace:"):
        rel = prefixed_path[len("workspace:") :]
        sink(
            ConfigDeprecatedRefForm(
                deprecated_form=prefixed_path,
                canonical_form=rel,
                location="context_files[].path or extends:",
                where="templating.context_files.resolve_path",
            )
        )
        return (workspace_root / rel).resolve()
    # Plain (unprefixed) path → workspace-root by default.
    return (workspace_root / prefixed_path).resolve()


def _resolve_kolsch_resource(relative: str) -> Path:
    """Resolve `kolsch:<relative>` to an absolute path.

    Works in both wheel and editable-install contexts. Probes
    `importlib.resources.files("kolsch")` first; if the resource is
    absent (editable install where the umbrella is empty), falls back
    to the repo root discovered via the `kolsch` package's
    `__file__.parent`.
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
    """Resolve and read a single context file; return a populated `DocItem`.

    The `summary` fallback (first non-empty line of file content) is
    applied here when `ContextFileSpec.summary` is `None`. Plain paths
    resolve under the workspace root; `workspace:` is accepted but
    emits [`ConfigDeprecatedRefForm`][errors.ConfigDeprecatedRefForm]
    via the module logger.

    Args:
        spec: The `context_files[]` entry.
        workspace_root: Workspace root for path resolution.
        max_bytes: Per-file payload cap.

    Returns:
        A populated [`DocItem`][templating.vars.docs.DocItem].

    Raises:
        ContextFileNotFound: The resolved path does not exist.
        ContextFileTooLarge: The file exceeds `max_bytes`.
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
    """The `ContextFileSpec.summary` fallback — first non-empty line.

    Empty input returns the empty string (no exception).
    """
    for line in content.splitlines():
        if line.strip():
            return line.strip()
    return ""
