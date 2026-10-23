r"""Canonical path targets and path patterns for authorization matching.

An authorization rule's `pattern:` glob is matched against a tool
call's *target*. For file tools the target is the `path` argument, and
matching the string the model typed lets a rule be dodged by spelling
the same file differently (`bundles/../platform.yaml` under
`bundles/*`, `/ws/data/secret/k` past a `data/secret/*` deny). This
module reduces both sides to one canonical form so a rule means the
same thing however the path is spelled. See the authorization
contract's "Path targets" section in
[secrets, sandbox and authz](../explanation/secrets-sandbox-authz-contract.md).

Matching is fail-safe in both directions:

- **Permissive (`allow`) rules match strictly** — only the file's
  *real* location, after `..` and symlinks are resolved the same way
  the path policy and the file tools resolve them
  (`files.policy._resolve_against_root`).
- **Restrictive (`deny` / `prompt`) rules match generously** — the
  real location *and* the lexical spelling (`..` and `.` collapsed,
  symlinks not followed), so a deny written against a symlink's own
  path keeps catching it.

The rule's pattern is normalised lexically and never through the
filesystem, so planting a symlink can't widen an allow. Only POSIX
separators are understood: `\` is an ordinary filename character on
macOS and Linux, and Windows is not a supported host.

Pure apart from `Path.resolve` of the target and the root, plus the
cached case-sensitivity probe; `path_pattern_forms` does no I/O.
"""

from __future__ import annotations

import fnmatch
import functools
import os
import posixpath
from dataclasses import dataclass
from pathlib import Path

from files.policy import _resolve_against_root

_GLOB_CHARS = frozenset("*?")


@dataclass(frozen=True)
class PathTarget:
    """One file-tool path argument, reduced to its canonical forms.

    Built by [`normalise_path_target`][files.authz_target.normalise_path_target];
    consumed by
    [`path_pattern_matches`][files.authz_target.path_pattern_matches]
    and by the authorization provider's legacy-rule matcher.

    Attributes:
        raw: The path exactly as the caller passed it.
        display: The canonical form shown in prompts, the approval
            widget and `policy.decision.made`: the workspace-relative
            POSIX path when the resolved file is inside the workspace
            (`"."` for the root itself), otherwise the resolved
            absolute path. A trailing `/` on `raw` is kept.
        strict_forms: The forms `allow` rules match against: the
            resolved relative form (inside the workspace only) and the
            resolved absolute form. `display` is always the first.
        loose_forms: The forms `deny` / `prompt` rules match against:
            `strict_forms` plus the lexical forms (`..` and `.`
            collapsed against the root as given, symlinks not
            followed), relative and absolute. Never includes `raw`
            unless `raw` is already canonical.
        case_insensitive: Whether the workspace filesystem folds case.
            When `True`, `allow` rules also match case-insensitively;
            `deny` / `prompt` rules always do.
        root_given: The workspace root as configured (absolute,
            lexically normalised), or `None` when no root was known.
        root_resolved: The workspace root with symlinks resolved, or
            `None` when no root was known.
    """

    raw: str
    display: str
    strict_forms: tuple[str, ...]
    loose_forms: tuple[str, ...]
    case_insensitive: bool
    root_given: str | None
    root_resolved: str | None

    def forms_for(self, *, restrictive: bool) -> tuple[str, ...]:
        """Return the forms a rule of the given kind matches against.

        Args:
            restrictive: `True` for `deny` / `prompt` rules (loose
                forms), `False` for `allow` rules (strict forms).
        """
        return self.loose_forms if restrictive else self.strict_forms

    def folds_case(self, *, restrictive: bool) -> bool:
        """Return whether a rule of the given kind matches case-insensitively.

        Restrictive rules always fold (over-denying is safe); `allow`
        rules fold only on a case-insensitive workspace filesystem.
        """
        return restrictive or self.case_insensitive


def normalise_path_target(
    raw: str,
    *,
    root: Path | None,
    case_insensitive: bool | None = None,
) -> PathTarget:
    r"""Canonicalise a file tool's `path` argument for rule matching.

    With a `root`, the path is resolved exactly as the path policy and
    the file tools resolve it — joined to `root` when relative, then
    `Path.resolve(strict=False)` (follows symlinks and `..`; no `~`
    expansion). A resolved path inside the workspace gets a relative
    form plus an absolute form; one outside gets only the absolute
    form. The lexical forms (`posixpath`-normalised against the root as
    given, no symlink following) are added to the loose set.

    With `root=None` (tests and programmatic callers only), the path
    is normalised lexically: `bundles/../platform.yaml` becomes
    `platform.yaml`, a leading `..` stays relative (so it never
    matches a `dir/*` pattern), and `case_insensitive` is `False`.

    `\` is an ordinary filename character, never a separator.

    Args:
        raw: The `path` argument as passed to the tool.
        root: The workspace root, or `None` for lexical-only mode.
        case_insensitive: Inject the case flag; `None` probes the
            workspace volume via
            [`is_case_insensitive_fs`][files.authz_target.is_case_insensitive_fs].

    Returns:
        The [`PathTarget`][files.authz_target.PathTarget].

    Raises:
        ValueError: The path can't be normalised (an embedded NUL, or
            resolution failed with `OSError` / a symlink loop). The
            caller must fail closed.
    """
    if "\x00" in raw:
        msg = f"path contains an embedded NUL: {raw!r}"
        raise ValueError(msg)
    trailing = raw.endswith("/")

    if root is None:
        lexical = _lexical_normpath(raw)
        forms = (_with_trailing(lexical, trailing=trailing),)
        return PathTarget(
            raw=raw,
            display=forms[0],
            strict_forms=forms,
            loose_forms=forms,
            case_insensitive=False if case_insensitive is None else case_insensitive,
            root_given=None,
            root_resolved=None,
        )

    try:
        root_resolved = root.resolve()
        resolved = _resolve_against_root(raw, root)
    except (OSError, RuntimeError) as exc:
        msg = f"path cannot be resolved: {raw!r}: {exc}"
        raise ValueError(msg) from exc
    # Lexical on purpose: the root "as given" must not follow symlinks.
    root_given = _lexical_normpath(os.path.abspath(os.fspath(root)))  # noqa: PTH100
    root_r = root_resolved.as_posix()

    strict = _root_relative_forms(
        resolved.as_posix(), roots=(root_r,), trailing=trailing
    )
    lexical_abs = _lexical_normpath(posixpath.join(root_given, raw))
    lexical_forms = _root_relative_forms(
        lexical_abs, roots=(root_given, root_r), trailing=trailing
    )
    if case_insensitive is None:
        case_insensitive = is_case_insensitive_fs(root_resolved)
    return PathTarget(
        raw=raw,
        display=strict[0],
        strict_forms=strict,
        loose_forms=_dedupe(strict + lexical_forms),
        case_insensitive=case_insensitive,
        root_given=root_given,
        root_resolved=root_r,
    )


@functools.lru_cache(maxsize=1024)
def path_pattern_forms(
    pattern: str,
    *,
    root_given: str | None,
    root_resolved: str | None,
) -> tuple[str, ...]:
    """Normalise a rule's path pattern into the forms it is matched as.

    Lexical only — no filesystem access of any kind, so a symlink can
    never widen a pattern (`ln -s / data` must not turn `data/*` into
    `/*`). The stored pattern, `list_rules()` output and rule ids are
    unaffected; this runs at match time and is cached.

    Steps:

    1. `*` is returned as-is.
    2. Repeated `/` and `.` segments collapse, a leading `./` is
       stripped and a trailing `/` is kept. A `..` segment is resolved
       only when the segment before it is literal (no `*` / `?`); a
       `..` after a glob segment is left in place and can never match
       a canonical target, so as an allow it fails safe and as a deny
       it is inert.
    3. A relative pattern that still starts with `..` is anchored to
       the workspace root (`../shared/*` becomes `<parent>/shared/*`),
       in both spellings of the root. Without a root it stays
       relative.
    4. An absolute pattern under either spelling of the root (as
       given, or symlink-resolved) is also emitted in the other
       spelling, so `/tmp/ws/data/*` matches when the root resolves to
       `/private/tmp/ws`.

    Args:
        pattern: The rule's `pattern:` glob, verbatim.
        root_given: The root as configured (`PathTarget.root_given`).
        root_resolved: The resolved root (`PathTarget.root_resolved`).

    Returns:
        The pattern forms, in a stable order with no duplicates.
    """
    if pattern == "*":
        return ("*",)
    cleaned = _clean_pattern(pattern)
    if root_given is None or root_resolved is None:
        return (cleaned,)
    roots = _dedupe((root_given, root_resolved))
    if not cleaned.startswith("/"):
        if cleaned != ".." and not cleaned.startswith("../"):
            return (cleaned,)
        return _dedupe(tuple(_clean_pattern(f"{r}/{cleaned}") for r in roots))
    forms = [cleaned]
    for this in roots:
        rest = _strip_root(cleaned, this)
        if rest is None:
            continue
        forms.extend(other + rest for other in roots if other != this)
    return _dedupe(tuple(forms))


def path_pattern_matches(
    pattern: str,
    target: PathTarget,
    *,
    restrictive: bool,
) -> bool:
    """Return whether a new-shape rule pattern matches a path target.

    The glob semantics are `fnmatch.fnmatchcase` (`*` matches `/`) over
    every pattern form from
    [`path_pattern_forms`][files.authz_target.path_pattern_forms] and
    every target form picked by the rule's effect: an `allow` rule
    (`restrictive=False`) sees only the strict forms; a `deny` /
    `prompt` rule (`restrictive=True`) sees the loose forms. Case is
    folded on both sides for restrictive rules always, and for `allow`
    rules only when the workspace filesystem is case-insensitive.

    Args:
        pattern: The rule's `pattern:` glob, verbatim.
        target: The canonical path target.
        restrictive: `True` for `deny` / `prompt` rules.
    """
    pattern_forms = path_pattern_forms(
        pattern,
        root_given=target.root_given,
        root_resolved=target.root_resolved,
    )
    target_forms = target.forms_for(restrictive=restrictive)
    if target.folds_case(restrictive=restrictive):
        pattern_forms = tuple(p.casefold() for p in pattern_forms)
        target_forms = tuple(t.casefold() for t in target_forms)
    return any(
        fnmatch.fnmatchcase(form, p) for p in pattern_forms for form in target_forms
    )


@functools.lru_cache(maxsize=64)
def is_case_insensitive_fs(root: Path) -> bool:
    """Probe whether the filesystem holding `root` folds case.

    Swaps the case of the nearest component of the resolved root that
    has letters and checks `Path.samefile` against the root. Any
    error (including a component with no letters anywhere) reports
    `False` — the case-sensitive answer, under which `allow` rules
    match exactly. Cached per root; callers pass the resolved root.

    The answer for the workspace volume is applied to every path
    target, including absolute paths on other volumes.
    """
    try:
        resolved = root.resolve()
        parts = resolved.parts
        for index in range(len(parts) - 1, 0, -1):
            swapped = parts[index].swapcase()
            if swapped == parts[index]:
                continue
            probe = Path(*parts[:index], swapped, *parts[index + 1 :])
            return resolved.samefile(probe)
    except (OSError, ValueError, RuntimeError):
        return False
    return False


# ---------------------------------------------------------------------------
# Lexical helpers
# ---------------------------------------------------------------------------


def _lexical_normpath(path: str) -> str:
    """`posixpath.normpath`, also collapsing POSIX's leading `//`."""
    normed = posixpath.normpath(path)
    if normed.startswith("//"):
        normed = "/" + normed.lstrip("/")
    return normed


def _with_trailing(form: str, *, trailing: bool) -> str:
    """Re-append the caller's trailing `/` (never to `.` or `/`)."""
    if trailing and form != "." and not form.endswith("/"):
        return form + "/"
    return form


def _strip_root(path: str, root: str) -> str | None:
    """Return the part of absolute `path` after `root` (`""` or `/…`).

    `None` when `path` is not `root` or under it. A plain string
    prefix test; `root` and `path` are both lexically normalised.
    """
    if path == root:
        return ""
    if root == "/":
        return path if path.startswith("/") else None
    if path.startswith(root + "/"):
        return path[len(root) :]
    return None


def _root_relative_forms(
    absolute: str,
    *,
    roots: tuple[str, ...],
    trailing: bool,
) -> tuple[str, ...]:
    """Return `(relative, absolute)` when under a root, else `(absolute,)`."""
    for root in roots:
        rest = _strip_root(absolute, root)
        if rest is None:
            continue
        if rest == "":
            return _dedupe((".", absolute))
        relative = rest.lstrip("/")
        return (
            _with_trailing(relative, trailing=trailing),
            _with_trailing(absolute, trailing=trailing),
        )
    return (_with_trailing(absolute, trailing=trailing),)


def _clean_pattern(pattern: str) -> str:
    """Collapse `//`, `.` and literal-preceded `..` segments in a glob."""
    absolute = pattern.startswith("/")
    trailing = pattern.endswith("/")
    stack: list[str] = []
    for segment in pattern.split("/"):
        if segment in ("", "."):
            continue
        if segment != "..":
            stack.append(segment)
            continue
        if not stack:
            if not absolute:
                stack.append(segment)
            continue
        previous = stack[-1]
        if previous == ".." or _GLOB_CHARS.intersection(previous):
            stack.append(segment)
        else:
            stack.pop()
    body = "/".join(stack)
    if absolute:
        return _with_trailing("/" + body, trailing=trailing)
    return _with_trailing(body or ".", trailing=trailing)


def _dedupe(items: tuple[str, ...]) -> tuple[str, ...]:
    """Drop repeats, keeping the first occurrence's position."""
    return tuple(dict.fromkeys(items))
