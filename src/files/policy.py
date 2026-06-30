"""Path-access policy plus `file_ref` shape validation.

Implements the policy half of the file-access subsystem per
[file handling](../../docs/explanation/file-handling.md). The policy
gates which filesystem paths the platform's file-touching code may
reach: consulted by the builtin tools (`read`, `write`, `edit`) at
invocation time and by the engine's `execute` boundary when
validating `file_ref` inputs.

Two lists drive the decision — `allowed_paths` (prefixes the platform
may touch) and `denied_paths` (prefixes the platform MUST NOT touch).
Evaluation order is denies-then-allows-then-default-deny; paths are
normalised via `Path.resolve` before policy evaluation so symlink
escape is prevented.

Two write-side guards sit alongside the read-path policy:
[`assert_writable_destination`][files.policy.assert_writable_destination]
refuses any write target under `content/` regardless of policy (the
content sidecar is engine-owned), and
[`validate_file_ref`][files.policy.validate_file_ref] sanity-checks
the `file_ref` string (non-empty, no null bytes) before any path
policy evaluation runs.
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
    """The parsed `file_access` section of `platform.yaml`.

    Both lists hold path prefixes — relative to the platform root or
    absolute. Empty lists are valid; with neither list populated, every
    path falls through to default-deny.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_paths: list[str] = Field(default_factory=list)
    denied_paths: list[str] = Field(default_factory=list)


def load_policy(config_section: dict[str, Any]) -> PathPolicy:
    """Parse the `file_access` config dict into a `PathPolicy`.

    The platform bootstrap calls this once at startup; downstream
    consumers receive the frozen `PathPolicy` and never re-parse.

    Args:
        config_section: The `file_access` section of `platform.yaml`,
            already YAML-decoded into a dict.

    Returns:
        The validated `PathPolicy`.

    Raises:
        FileAccessPolicyLoadFailed: The dict failed Pydantic
            validation (wrong types, unknown fields). Fatal — the
            platform refuses to start with a broken file-access
            policy.
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
    """Make the path absolute and normalise symlinks plus `..` segments.

    Uses `Path.resolve(strict=False)` so non-existent paths are still
    normalisable — important for checking *write* targets that don't
    exist yet.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=False)


def _is_under(child: Path, parent: Path) -> bool:
    """Return `True` when `child` equals `parent` or sits under it."""
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
    """Evaluate the policy and return a `(allowed, reason)` pair.

    The boolean form for callers that want to branch on the outcome
    without catching an exception. Use
    [`check_path_allowed`][files.policy.check_path_allowed] when a
    typed `FileAccessDenied` raise is the right shape.

    Pure: no side effects beyond the symlink resolution implicit in
    `Path.resolve`.

    Args:
        path: The candidate path. Absolute or relative to `root`.
        policy: The loaded `PathPolicy`.
        root: The platform root, used to resolve relative paths.

    Returns:
        `(True, None)` when permitted; `(False, reason)` otherwise.
        The reason is a short human-readable summary suitable for
        diagnostics.
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
    """Evaluate the policy and raise on denial.

    The raise-form companion to
    [`is_path_allowed`][files.policy.is_path_allowed]. Used by the
    builtin tools and by the engine's input validator — both want a
    typed `PlatformError` flowing through rather than a `(bool,
    reason)` tuple they have to unpack.

    Args:
        path: The candidate path. Absolute or relative to `root`.
        policy: The loaded `PathPolicy`.
        root: The platform root, used to resolve relative paths.

    Raises:
        FileAccessDenied: The path is not permitted. The error
            carries `matched_rule` (the structured deny/allow rule
            that fired, or `None` for default-deny) so explainability
            consumers can attribute the denial.
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

    Internal — exposed via the tuple-returning `is_path_allowed` and
    the raising `check_path_allowed`. Carries the rule that fired
    (`None` for default-deny) so callers' error messages can attribute
    the decision.
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
    """Resolve `path` against `root` and apply the policy.

    The structured-decision primitive both
    [`is_path_allowed`][files.policy.is_path_allowed] and
    [`check_path_allowed`][files.policy.check_path_allowed] are built
    on. Denies are evaluated first; a path that matches both a deny
    and an allow rule is denied. Anything that matches neither falls
    through to default-deny.

    Args:
        path: The candidate path. Absolute or relative to `root`.
        policy: The loaded `PathPolicy`.
        root: The platform root, used to resolve relative paths.

    Returns:
        The `_PathDecision` carrying the boolean outcome, a
        diagnostic reason, and the matched rule (or `None` for
        default-deny).
    """
    resolved = _resolve_against_root(path, root)

    # Denies are evaluated FIRST. A path that matches both a deny and
    # an allow rule is denied; the deny pattern fires.
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
# Write-side guard — tools never write to content/
# ---------------------------------------------------------------------------


def assert_writable_destination(path: str, *, root: Path) -> None:
    """Refuse a write target that resolves under `<root>/content/`.

    The `content/` sidecar store is engine-owned: tools writing there
    would violate the engine's sole-writer invariant and the
    hash-naming convention. Tools that need to persist outputs land
    in `data/` instead. This guard runs in addition to the path
    policy — read access to `content/` is still gated by the regular
    policy, only writes are refused here.

    Args:
        path: The candidate write target. Absolute or relative to
            `root`.
        root: The platform root, used to resolve relative paths.

    Raises:
        FileAccessDenied: The path resolves under `<root>/content/`.
    """
    resolved = _resolve_against_root(path, root)
    content_dir = (root / "content").resolve(strict=False)
    if _is_under(resolved, content_dir):
        raise FileAccessDenied(
            path=path,
            reason=("tools may not write to content/; only the engine writes there"),
        )


# ---------------------------------------------------------------------------
# file_ref shape validation
# ---------------------------------------------------------------------------


def validate_file_ref(value: str) -> None:
    """Sanity-check a `file_ref` string before policy evaluation.

    The TypeSpec `file_ref` is a plain `str` at runtime. This helper
    enforces the minimal invariants the path policy can't catch on
    its own: the value must be non-empty and free of embedded null
    bytes (the kernel's path-handling code may otherwise pass them
    through quietly). The policy check via
    [`check_path_allowed`][files.policy.check_path_allowed] is a
    separate, later step.

    Args:
        value: The `file_ref` string to validate.

    Raises:
        FileAccessInvalidFileRef: `value` is empty or contains a
            null byte.
    """
    if not value:
        raise FileAccessInvalidFileRef(value=value, reason="empty file_ref")
    if "\x00" in value:
        raise FileAccessInvalidFileRef(
            value=value,
            reason="file_ref contains null byte",
        )
