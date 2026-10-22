"""Path-access policy plus `file_ref` shape validation.

Implements the policy half of the file-access subsystem per
[file handling](../explanation/file-handling.md). The policy
gates which filesystem paths the platform's file-touching code may
reach: consulted by the builtin tools (`read`, `write`, `edit`) at
invocation time and by the engine's `execute` boundary when
validating `file_ref` inputs.

Two lists drive the decision — `allowed_paths` (prefixes the platform
may touch) and `denied_paths` (prefixes the platform MUST NOT touch).
Evaluation order is denies-then-allows-then-default-deny; paths are
normalised via `Path.resolve` before policy evaluation so symlink
escape is prevented.

Deny matching (`denied_paths`, the `content/` guard and the protected
write floor) is case-insensitive: both sides are casefolded, so
`.ENV` cannot slip past a `.env` deny on a case-insensitive
filesystem such as default macOS APFS. Allow matching stays
case-sensitive, so case never *grants* access.

Two write-side guards sit alongside the read-path policy:
[`assert_writable_destination`][files.policy.assert_writable_destination]
refuses any write target under `content/` (the content sidecar is
engine-owned) or under one of the
[`PROTECTED_WRITE_PATHS`][files.policy.PROTECTED_WRITE_PATHS]
(kolsch's own configuration and state), regardless of policy and of
authorization. [`validate_file_ref`][files.policy.validate_file_ref]
sanity-checks the `file_ref` string (non-empty, no null bytes) before
any path policy evaluation runs.
"""

from __future__ import annotations

import unicodedata
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

PROTECTED_WRITE_PATHS: tuple[str, ...] = (
    "platform.yaml",
    ".kolsch/",
    ".env",
    ".platform/",
)
"""Workspace paths no agent file tool may ever write (bug-161).

Each entry is relative to the workspace root; a write target is
refused when its resolved path equals the entry or sits under it,
compared casefolded.
[`assert_writable_destination`][files.policy.assert_writable_destination]
enforces it, so it holds under every authorization outcome — an
allow-all rule set, a `bundles/*` allow (which also matches
`bundles/../platform.yaml`), or `kolsch chat --auto-approve`.

- `platform.yaml` — authorization rules, `file_access`, provider
  topology. An agent that could write it could grant itself anything.
- `.kolsch/` — setup's bundle baseline and rule ledgers.
- `.env` — secrets and endpoint-redirect environment variables.
- `.platform/` — the session store database.

Not configurable, by design: there is no config key or environment
variable to turn it off, so an agent cannot disable it by editing a
file it can reach. kolsch's own writers (`kolsch permissions`, the
approval modal's "always allow", `kolsch connect`, `kolsch setup`)
are Python and never go through this guard. The floor does NOT
cover `bash` (unconstrained under the native sandbox) or
`bundles/**` (writing bundle code or permission YAML is equivalent
to code execution).
"""


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


def _fold(part: str) -> str:
    """Canonical caseless form of one path component (NFD + casefold)."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", part).casefold())


def _is_under_casefold(child: Path, parent: Path) -> bool:
    """Case-insensitive [`_is_under`][files.policy._is_under] for deny checks.

    Both paths must already be resolved. Compares components
    element-wise after casefolding, so `/ws/.ENV` is under `/ws/.env`
    on every platform. On a case-sensitive filesystem this over-matches
    (fail-safe: it only ever widens a deny); on a case-insensitive one
    (default macOS APFS) it matches what the filesystem actually opens.
    """
    child_parts = child.parts
    parent_parts = parent.parts
    if len(child_parts) < len(parent_parts):
        return False
    return all(
        _fold(c) == _fold(p) for c, p in zip(child_parts, parent_parts, strict=False)
    )


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
    # an allow rule is denied; the deny pattern fires. Deny matching
    # is casefolded (bug-161) so `.ENV` can't slip past `.env` on a
    # case-insensitive filesystem; allow matching below stays
    # case-sensitive so case never grants access.
    for denied in policy.denied_paths:
        denied_abs = _resolve_against_root(denied, root)
        if _is_under_casefold(resolved, denied_abs):
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
# Write-side guard — tools never write to content/ or the protected floor
# ---------------------------------------------------------------------------


_WRITE_GUARD_WHERE = "files.policy.assert_writable_destination"


def _same_existing_file(a: Path, b: Path) -> bool:
    """Return `True` when both paths exist and are the same inode.

    Catches what a path comparison can't: a pre-existing hard link to
    a protected file, and any filesystem-specific aliasing beyond
    casefolding. Missing paths (the common write-a-new-file case)
    are simply not the same file.
    """
    try:
        return a.samefile(b)
    except OSError:
        return False


def assert_writable_destination(path: str, *, root: Path) -> None:
    """Refuse a write target under `content/` or a protected path.

    Every write-capable file tool calls this from its body, after
    authorization and after
    [`check_path_allowed`][files.policy.check_path_allowed], so it
    holds whatever the authz rules, `file_access` lists or
    `--auto-approve` say. Two refusals:

    - `<root>/content/` — the sidecar store is engine-owned: tools
      writing there would violate the engine's sole-writer invariant
      and the hash-naming convention. Tools that need to persist
      outputs land in `data/` instead. Read access to `content/` is
      still gated by the regular policy; only writes are refused here.
    - [`PROTECTED_WRITE_PATHS`][files.policy.PROTECTED_WRITE_PATHS]
      (bug-161) — `platform.yaml`, `.kolsch/`, `.env`, `.platform/`.
      Agents must not change kolsch's own configuration or state;
      people change it with `kolsch permissions`, the approval modal,
      `kolsch setup`, or an editor. Reads are not affected.

    Matching is on the *resolved* target (symlinks and `..` followed,
    so `bundles/../platform.yaml` and a `data/link -> ../platform.yaml`
    symlink are both caught) and casefolded (so `PLATFORM.YAML` is
    caught on every platform). An existing target that is the same
    file as a protected path (a hard link) is refused too.

    Args:
        path: The candidate write target. Absolute or relative to
            `root`.
        root: The platform root, used to resolve relative paths.

    Raises:
        FileAccessDenied: The path resolves under `<root>/content/`
            or a protected path. A floor refusal carries
            `matched_rule={"pattern": <entry>, "type":
            "protected_write_floor"}`.
    """
    resolved = _resolve_against_root(path, root)
    content_dir = _resolve_against_root("content", root)
    if _is_under_casefold(resolved, content_dir):
        raise FileAccessDenied(
            path=path,
            reason=("tools may not write to content/; only the engine writes there"),
        )
    for entry in PROTECTED_WRITE_PATHS:
        protected = _resolve_against_root(entry, root)
        if _is_under_casefold(resolved, protected) or _same_existing_file(
            resolved, protected
        ):
            raise FileAccessDenied(
                path=path,
                reason=(
                    f"{entry} is protected: agents cannot change kolsch "
                    "configuration or state. Change permissions with "
                    "`kolsch permissions` or the approval modal; edit other "
                    "settings by hand or with `kolsch setup`."
                ),
                matched_rule={"pattern": entry, "type": "protected_write_floor"},
                where=_WRITE_GUARD_WHERE,
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
