"""Tool-refs migration for ``kolsch setup --sync-bundle`` (qol-089).

Rewrites retired/renamed tool refs across **kolsch-owned** YAMLs and
reports user-owned refs for manual attention. Follows the pure-function
shape of :mod:`cli._migrations.authz_rules` — the runner in
``cli._setup`` reads / writes files; this module returns the plan.

Boundaries (non-negotiable):

- **Kolsch-owned** — the migrator rewrites these:
    - ``bundles/kolsch/agents/*.yaml`` — tool refs under
      ``subsystems.tools.config.declared[].ref``.
    - ``platform.yaml`` at the workspace root — tool refs under
      ``authorization.config.rules[].what``.
- **User-owned** — everything else. The migrator scans a small set of
  common paths (``bundles/<other>/**/*.yaml``, ``bundles/<other>/**/*.yml``)
  and lists any retired refs it finds; it never modifies these files.

Manifest shape is monotonic — a ref appears the first time it's
renamed / retired and stays there. The manifest key is the *old* ref
(fully-qualified ``kolsch/tools/<name>``); the value is a
:class:`Renamed` or :class:`Retired` marker.

See qol-089's spec for the ses_0e549428 precedent this closes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kolsch._yamlio import YAMLError
from kolsch._yamlio import dump_round_trip as _yaml_dump
from kolsch._yamlio import load_round_trip as _yaml_load

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Manifest primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Renamed:
    """A tool ref that was renamed (or renamed + split into companions).

    ``to`` is the primary replacement — the migrator rewrites the old
    ref to this value. ``also`` names companion refs the split
    introduced (``fetch_website`` split into ``fetch_url`` +
    ``browse_url`` — ``to="fetch_url", also=("browse_url",)``). The
    migrator adds companion refs alongside the rewritten primary.
    ``notes`` is a one-liner shown in the sync report.
    """

    to: str
    also: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class Retired:
    """A tool ref that was removed with no direct replacement.

    ``reason`` is the human-readable rationale shown in the sync
    report. ``replacement`` points at the closest equivalent (if any)
    for operator guidance; the migrator does NOT auto-add it — the
    retirement is a signal that the operator should choose.
    """

    reason: str
    replacement: str | None = None


# The monotonic manifest. Additive-only across releases; entries never
# get removed even after the referenced version is well past.
TOOL_MIGRATIONS: dict[str, Renamed | Retired] = {
    "kolsch/tools/fetch_website": Renamed(
        to="kolsch/tools/fetch_url",
        also=("kolsch/tools/browse_url",),
        notes=(
            "Split into fetch_url (httpx + trafilatura) and browse_url "
            "(Playwright, for JS-heavy pages)."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rewrite:
    """One planned rewrite in a kolsch-owned YAML.

    ``path`` is workspace-relative. ``kind`` is
    ``"agent_declared" | "authz_rule"``. ``added`` names any companion
    refs the migrator inserted alongside the primary rewrite (per
    ``Renamed.also``). ``replacement`` carries the pointer from
    ``Retired(replacement=...)`` so the render layer can surface it
    ("consider: <ref>"); ``None`` for ``Renamed`` rewrites (the target
    is already in ``new_ref``) and for ``Retired`` without a
    replacement pointer.
    """

    path: str
    kind: str
    old_ref: str
    new_ref: str | None
    added: tuple[str, ...] = ()
    replacement: str | None = None


@dataclass(frozen=True)
class ParseFailure:
    """A workspace YAML the migrator couldn't parse.

    Doctor's whole job is diagnosing broken workspaces, so a syntax
    error in a kolsch-owned YAML must surface as a report line rather
    than a stack trace. ``kind`` is ``"agent" | "platform" | "user"``.
    ``reason`` is the parser's short message.
    """

    path: str
    kind: str
    reason: str


@dataclass(frozen=True)
class UserOwnedHit:
    """A retired ref found in a user-owned YAML — reported only, never rewritten."""

    path: str
    old_ref: str
    suggested_new_ref: str | None
    notes: str


@dataclass(frozen=True)
class ToolsMigrationResult:
    """Output of :func:`migrate_tool_refs`.

    ``yaml_bodies`` maps workspace-relative kolsch-owned paths to their
    rewritten YAML text. The caller writes these; the migrator does
    not touch the filesystem.
    """

    rewrites: list[Rewrite] = field(default_factory=list)
    yaml_bodies: dict[str, str] = field(default_factory=dict)
    user_owned_hits: list[UserOwnedHit] = field(default_factory=list)
    parse_failures: list[ParseFailure] = field(default_factory=list)

    def clean(self) -> bool:
        """True when nothing needs writing and no drift/parse-failure was found."""
        return (
            not self.rewrites
            and not self.user_owned_hits
            and not self.parse_failures
        )


# ---------------------------------------------------------------------------
# Migrator
# ---------------------------------------------------------------------------


def migrate_tool_refs(
    workspace_root: Path,
    manifest: dict[str, Renamed | Retired] | None = None,
) -> ToolsMigrationResult:
    """Return a :class:`ToolsMigrationResult` for ``workspace_root``.

    Reads kolsch-owned YAMLs (``bundles/kolsch/agents/*.yaml`` +
    ``platform.yaml``) and user-owned YAMLs (``bundles/<other>/**/*.yaml``);
    plans rewrites for the kolsch-owned set and lists retired refs
    found in the user-owned set. **Never writes.**

    Uses the project's ``ruamel.yaml`` round-trip loader/dumper (via
    :mod:`kolsch._yamlio`) so comments and key order are preserved in
    the rewritten bodies.

    Args:
        workspace_root: Absolute path to the workspace root.
        manifest: Manifest to apply. Defaults to :data:`TOOL_MIGRATIONS`.
    """
    manifest_ = manifest if manifest is not None else TOOL_MIGRATIONS
    result = ToolsMigrationResult()

    _process_helper_agents(workspace_root, manifest_, result)
    _process_platform_yaml(workspace_root, manifest_, result)
    _scan_user_owned(workspace_root, manifest_, result)
    return result


# ---------------------------------------------------------------------------
# Kolsch-owned YAML rewriters
# ---------------------------------------------------------------------------


def _process_helper_agents(
    root: Path,
    manifest: dict[str, Renamed | Retired],
    result: ToolsMigrationResult,
) -> None:
    """Walk ``bundles/kolsch/agents/*.yaml`` and rewrite declared tool refs."""
    agents_dir = root / "bundles" / "kolsch" / "agents"
    if not agents_dir.is_dir():
        return
    for yaml_path in sorted(agents_dir.glob("*.yaml")):
        _rewrite_agent_yaml(root, yaml_path, manifest, result)


def _rewrite_agent_yaml(
    root: Path,
    yaml_path: Path,
    manifest: dict[str, Renamed | Retired],
    result: ToolsMigrationResult,
) -> None:
    """Rewrite one agent yaml's ``subsystems.tools.config.declared`` list.

    Renamed refs → replaced in place, companions from ``also`` inserted
    directly after. Retired refs → removed. Existing rules already
    present in the target list are not duplicated.
    """
    try:
        raw = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return
    rel_for_error = str(yaml_path.relative_to(root))
    try:
        doc = _yaml_load(raw)
    except YAMLError as exc:
        # Doctor's whole reason to exist is diagnosing broken
        # workspaces; a raw traceback here is a UX failure. Record the
        # parse failure and skip so downstream sections still fire.
        result.parse_failures.append(
            ParseFailure(path=rel_for_error, kind="agent", reason=_short_error(exc))
        )
        return
    if not isinstance(doc, dict):
        return
    subsystems = doc.get("subsystems")
    if not isinstance(subsystems, dict):
        return
    tools_sub = subsystems.get("tools")
    if not isinstance(tools_sub, dict):
        return
    config = tools_sub.get("config")
    if not isinstance(config, dict):
        return
    declared = config.get("declared")
    if not isinstance(declared, list):
        return

    rel = str(yaml_path.relative_to(root))
    seen_refs: set[str] = set()
    new_declared: list[Any] = []
    changed = False
    pending_companions: list[str] = []

    for entry in declared:
        if not isinstance(entry, dict):
            new_declared.append(entry)
            continue
        ref = entry.get("ref")
        if not isinstance(ref, str) or ref not in manifest:
            if isinstance(ref, str):
                seen_refs.add(ref)
            new_declared.append(entry)
            continue
        action = manifest[ref]
        if isinstance(action, Retired):
            result.rewrites.append(
                Rewrite(
                    path=rel,
                    kind="agent_declared",
                    old_ref=ref,
                    new_ref=None,
                    replacement=action.replacement,
                )
            )
            changed = True
            continue
        # Renamed. Rewrite in place, queue companions for post-loop
        # insertion (dedup against seen_refs). If action.to was already
        # declared elsewhere, drop this entry entirely.
        changed = True
        added = list(action.also)
        if action.to in seen_refs:
            # Drop the redundant entry — action.to already declared.
            pending_companions.extend(added)
            result.rewrites.append(
                Rewrite(
                    path=rel,
                    kind="agent_declared",
                    old_ref=ref,
                    new_ref=action.to,
                    added=(),
                )
            )
            continue
        entry["ref"] = action.to
        seen_refs.add(action.to)
        new_declared.append(entry)
        pending_companions.extend(added)
        result.rewrites.append(
            Rewrite(
                path=rel,
                kind="agent_declared",
                old_ref=ref,
                new_ref=action.to,
                added=tuple(added),
            )
        )

    for companion in pending_companions:
        if companion in seen_refs:
            continue
        new_declared.append({"ref": companion})
        seen_refs.add(companion)

    if not changed:
        return
    config["declared"] = new_declared
    result.yaml_bodies[rel] = _yaml_dump(doc)


def _process_platform_yaml(
    root: Path,
    manifest: dict[str, Renamed | Retired],
    result: ToolsMigrationResult,
) -> None:
    """Rewrite kolsch/tools/* refs in ``authorization.config.rules``."""
    yaml_path = root / "platform.yaml"
    if not yaml_path.is_file():
        return
    try:
        raw = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return
    try:
        doc = _yaml_load(raw)
    except YAMLError as exc:
        result.parse_failures.append(
            ParseFailure(
                path="platform.yaml",
                kind="platform",
                reason=_short_error(exc),
            )
        )
        return
    if not isinstance(doc, dict):
        return
    authz = doc.get("authorization")
    if not isinstance(authz, dict):
        return
    config = authz.get("config")
    if not isinstance(config, dict):
        return
    rules = config.get("rules")
    if not isinstance(rules, list):
        return

    rel = "platform.yaml"
    seen_whats: set[str] = set()
    new_rules: list[Any] = []
    changed = False
    pending_companions: list[tuple[str, dict[str, Any]]] = []

    for rule in rules:
        if not isinstance(rule, dict):
            new_rules.append(rule)
            continue
        what = rule.get("what")
        if not isinstance(what, str) or what not in manifest:
            if isinstance(what, str):
                seen_whats.add(what)
            new_rules.append(rule)
            continue
        action = manifest[what]
        if isinstance(action, Retired):
            result.rewrites.append(
                Rewrite(
                    path=rel,
                    kind="authz_rule",
                    old_ref=what,
                    new_ref=None,
                    replacement=action.replacement,
                )
            )
            changed = True
            continue
        changed = True
        added = list(action.also)
        # Queue companion rules (cloned from this rule's shape) for
        # post-loop insertion; dedup at insert time against seen_whats.
        for companion in added:
            pending_companions.append((companion, dict(rule)))
        if action.to in seen_whats:
            # Redundant — action.to already covered by another rule.
            result.rewrites.append(
                Rewrite(
                    path=rel,
                    kind="authz_rule",
                    old_ref=what,
                    new_ref=action.to,
                    added=(),
                )
            )
            continue
        rule["what"] = action.to
        seen_whats.add(action.to)
        new_rules.append(rule)
        result.rewrites.append(
            Rewrite(
                path=rel,
                kind="authz_rule",
                old_ref=what,
                new_ref=action.to,
                added=tuple(added),
            )
        )

    for companion_what, template_rule in pending_companions:
        if companion_what in seen_whats:
            continue
        new_rules.append(_companion_authz_rule(template_rule, companion_what))
        seen_whats.add(companion_what)

    if not changed:
        return
    config["rules"] = new_rules
    result.yaml_bodies[rel] = _yaml_dump(doc)


def _companion_authz_rule(original: dict[str, Any], new_what: str) -> dict[str, Any]:
    """Clone ``original`` with ``what=new_what`` — preserves pattern/effect."""
    companion = {k: v for k, v in original.items() if k != "what"}
    companion["what"] = new_what
    # Put `what` first for readability.
    return {"what": new_what, **{k: v for k, v in companion.items() if k != "what"}}


# ---------------------------------------------------------------------------
# User-owned drift scanner (read-only)
# ---------------------------------------------------------------------------


def _scan_user_owned(
    root: Path,
    manifest: dict[str, Renamed | Retired],
    result: ToolsMigrationResult,
) -> None:
    """List retired-ref hits in ``bundles/<other>/**/*.yaml`` — no writes."""
    bundles_dir = root / "bundles"
    if not bundles_dir.is_dir():
        return
    for entry in sorted(bundles_dir.iterdir()):
        if not entry.is_dir() or entry.name == "kolsch":
            continue
        for yaml_path in sorted(entry.rglob("*.yaml")):
            _scan_user_yaml(root, yaml_path, manifest, result)
        for yaml_path in sorted(entry.rglob("*.yml")):
            _scan_user_yaml(root, yaml_path, manifest, result)


def _scan_user_yaml(
    root: Path,
    yaml_path: Path,
    manifest: dict[str, Renamed | Retired],
    result: ToolsMigrationResult,
) -> None:
    """String-scan a user-owned YAML for any retired ref and record hits."""
    try:
        raw = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return
    rel = str(yaml_path.relative_to(root))
    for old_ref, action in manifest.items():
        if old_ref not in raw:
            continue
        suggested: str | None
        if isinstance(action, Renamed):
            suggested = action.to
            notes = action.notes
        else:
            suggested = action.replacement
            notes = action.reason
        result.user_owned_hits.append(
            UserOwnedHit(
                path=rel,
                old_ref=old_ref,
                suggested_new_ref=suggested,
                notes=notes,
            )
        )


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------


def _short_error(exc: Exception) -> str:
    """One-line, operator-facing summary of a YAML parse failure."""
    first_line = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return f"{exc.__class__.__name__}: {first_line}"
