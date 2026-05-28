"""Path-access policy — spec docs/explanation/file-handling.md Part 4.

The path policy gates which filesystem paths the platform's
file-touching code may reach. It is consulted by the builtin tools
(``read``, ``write``, ``edit``) at invocation time and by the engine's
``execute`` boundary when validating ``file_ref`` inputs (Fh-I8).

Two lists per spec §4.1:
- ``allowed_paths`` — prefixes the platform may touch.
- ``denied_paths`` — prefixes the platform MUST NOT touch.

Evaluation order is denies-then-allows-then-default-deny (Fh-I3).
Paths are normalised via :meth:`pathlib.Path.resolve` BEFORE policy
evaluation (Fh-I4) so symlink-based escape is prevented.

Two write-side concerns sit alongside the read-path policy:

- :func:`assert_writable_destination` — refuses any write target
  under ``content/`` regardless of policy (Fh-I5 / Fh-I9 — only the
  engine writes there).
- :func:`validate_file_ref` — sanity-checks the ``file_ref`` string
  per spec §1 (non-empty, no null bytes) before any path policy
  evaluation runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from errors import (
    FileAccessDenied,
    FileAccessInvalidFileRef,
    FileAccessPolicyLoadFailed,
)

if TYPE_CHECKING:
    from typing import Any


_FILE_ACCESS_SECTION = "file_access"


class PathPolicy(BaseModel):
    """The parsed ``file_access`` section of ``platform.yaml``.

    Both lists hold path prefixes — relative to the platform root or
    absolute. Empty lists are valid (everything default-denied).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_paths: list[str] = Field(default_factory=list)
    denied_paths: list[str] = Field(default_factory=list)


def load_policy(config_section: dict[str, Any]) -> PathPolicy:
    """Build a :class:`PathPolicy` from the ``file_access`` config dict.

    Malformed shapes (wrong types, unknown fields) raise
    :class:`FileAccessPolicyLoadFailed` (Fatal — the platform refuses
    to start with a broken file-access policy).
    """
    try:
        return PathPolicy.model_validate(config_section)
    except ValidationError as exc:
        raise FileAccessPolicyLoadFailed(
            config_section=_FILE_ACCESS_SECTION,
            error=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Path resolution + policy evaluation
# ---------------------------------------------------------------------------


def _resolve_against_root(path: str | Path, root: Path) -> Path:
    """Make the path absolute (relative to ``root`` if needed) and
    normalise symlinks + ``..`` segments per spec §4.2 step 1.

    Uses ``Path.resolve(strict=False)`` so non-existent paths are
    still normalisable — important for checking *write* targets that
    don't exist yet.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=False)


def _is_under(child: Path, parent: Path) -> bool:
    """Return True when ``child`` equals ``parent`` or sits under it."""
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def is_path_allowed(
    path: str,
    *,
    policy: PathPolicy,
    root: Path,
) -> tuple[bool, str | None]:
    """Spec §4.2 / Module API Reference signature.

    Returns ``(True, None)`` when the path is permitted, or
    ``(False, reason)`` otherwise. The ``reason`` string is a short
    human-readable summary suitable for diagnostics; consumers that
    need the structured rule + resolved path call
    :func:`check_path_allowed` (or :func:`evaluate_path`) instead.

    Pure: no side effects beyond the symlink resolution implicit in
    :meth:`pathlib.Path.resolve`.
    """
    decision = evaluate_path(path, policy=policy, root=root)
    if decision.allowed:
        return (True, None)
    return (False, decision.reason)


def check_path_allowed(
    path: str,
    *,
    policy: PathPolicy,
    root: Path,
) -> None:
    """Raise :class:`FileAccessDenied` if the path is not allowed.

    Convenience for callers (the builtin tools, the engine input
    validator) that want the typed-PlatformError raise rather than
    the ``(bool, reason)`` tuple. The raised error carries
    ``matched_rule`` (the structured deny/allow rule that fired, or
    ``None`` for default-deny) per spec §4.11 + Error-Model A-I5
    explainability.
    """
    decision = evaluate_path(path, policy=policy, root=root)
    if decision.allowed:
        return
    raise FileAccessDenied(
        path=path,
        reason=decision.reason,
        matched_rule=decision.matched_rule,
    )


# ---------------------------------------------------------------------------
# Structured evaluation — populates `matched_rule` for explainability
# ---------------------------------------------------------------------------


class _PathDecision(BaseModel):
    """The structured outcome of one policy evaluation.

    Internal — exposed via the tuple-returning :func:`is_path_allowed`
    and the raising :func:`check_path_allowed`. Carries the structured
    rule that fired (or ``None`` for default-deny) and the resolved
    absolute path so callers' error messages can be precise.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    reason: str
    matched_rule: dict[str, str] | None = None


def evaluate_path(
    path: str,
    *,
    policy: PathPolicy,
    root: Path,
) -> _PathDecision:
    resolved = _resolve_against_root(path, root)

    # Fh-I3 — denies are evaluated FIRST. A path that matches both a
    # deny and an allow rule is denied; the deny pattern fires.
    for denied in policy.denied_paths:
        denied_abs = _resolve_against_root(denied, root)
        if _is_under(resolved, denied_abs):
            return _PathDecision(
                allowed=False,
                reason=f"denied by rule {denied!r}",
                matched_rule={"pattern": denied, "type": "denied_paths"},
            )

    for allowed in policy.allowed_paths:
        allowed_abs = _resolve_against_root(allowed, root)
        if _is_under(resolved, allowed_abs):
            return _PathDecision(
                allowed=True,
                reason=f"allowed by rule {allowed!r}",
                matched_rule={"pattern": allowed, "type": "allowed_paths"},
            )

    return _PathDecision(
        allowed=False,
        reason="default deny",
        matched_rule=None,
    )


# ---------------------------------------------------------------------------
# Write-side guard — Fh-I5: tools never write to content/
# ---------------------------------------------------------------------------


def assert_writable_destination(path: str, *, root: Path) -> None:
    """Refuse a write target that resolves under ``<root>/content/``.

    Spec Fh-I5 + Fh-I9 — the content/ sidecar store is engine-owned.
    Tools that write persistent outputs land in ``data/``; tools that
    target the content store would invariably violate the engine's
    sole-writer invariant and the hash-naming convention. This guard
    refuses regardless of the path policy (the default policy
    actually *allows* reads under ``content/``).

    Note: read access to ``content/`` is still gated by the regular
    path policy — only writes are guarded here.
    """
    resolved = _resolve_against_root(path, root)
    content_dir = (root / "content").resolve(strict=False)
    if _is_under(resolved, content_dir):
        raise FileAccessDenied(
            path=path,
            reason=(
                "tools may not write to content/ (Fh-I5); only the engine writes there"
            ),
        )


# ---------------------------------------------------------------------------
# file_ref validation — spec §1
# ---------------------------------------------------------------------------


def validate_file_ref(value: str) -> None:
    """Sanity-check a ``file_ref`` string per spec §1.

    The TypeSpec value ``file_ref`` is a plain str at runtime
    (Fh-I1). This helper enforces the minimal invariants that path
    policy can't catch alone:

    - non-empty
    - no embedded null bytes (the kernel's path-handling code may
      pass them through quietly otherwise)

    Raises :class:`errors.FileAccessInvalidFileRef` — a typed
    PlatformError per the handbook hard rule "no non-PlatformError
    escapes a public method." The policy check
    (``check_path_allowed``) is a separate, later step.
    """
    if not value:
        raise FileAccessInvalidFileRef(value=value, reason="empty file_ref")
    if "\x00" in value:
        raise FileAccessInvalidFileRef(
            value=value,
            reason="file_ref contains null byte",
        )
