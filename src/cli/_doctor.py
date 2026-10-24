"""``kolsch doctor`` — read-only bundle drift report (qol-089).

Prints five bundle report sections plus an advisory Authorization
section, and exits ``0`` clean / ``1`` drift.
Writes nothing to disk. Complements ``kolsch setup``'s install path
(which records ``.kolsch/bundle-baseline.json``) and precedes qol-093's
``kolsch setup --sync-bundle`` (which will actually repair the drift
this command surfaces).

Report sections:

1. **Missing tool files** — present in the starter-bundle source but
   absent from the workspace's ``bundles/kolsch/`` (e.g. a new tool
   shipped after the workspace was installed).
2. **Modified tool files** — sha256 mismatch vs the recorded baseline.
   Skipped with a one-line note when no baseline exists (older
   workspaces).
3. **Retired-at-source tool files** — present in the workspace but no
   longer in source (e.g. ``fetch_website.py`` after the
   ``fetch_url``/``browse_url`` split).
4. **Retired-tool refs in kolsch-owned YAMLs** — ``bundles/kolsch/agents/*.yaml``
   and ``platform.yaml`` still reference tools the migrator's manifest
   says are retired. Includes the proposed rewrite.
5. **Retired-tool refs in user-owned YAMLs** — the migrator surfaces
   these but never rewrites them. Includes the suggested manual fix.

Plus, only when there is a finding, an **Authorization** section
(bug-152): ``platform.yaml``'s rule set has no default-deny tail, or
has it somewhere other than last. Same text as the runtime bootstrap
WARNING. Advisory — it never changes the exit code — and printed on
the pre-``kolsch setup`` path too.

Then, always, an advisory **Agent write access** section (bug-161):
the paths agent file tools can never write
(``files.PROTECTED_WRITE_PATHS``), plus a warning for each platform
``allow`` rule that lets agents run ``bash`` or write ``bundles/``
without asking — the two routes around that floor. Never changes the
exit code.

qol-089 spec section §Acceptance Criteria (5) and (6) drive the
contract. qol-093 will consume the same detection logic for its
``--sync-bundle`` path.
"""

from __future__ import annotations

import posixpath
import re
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING

from cli._bundle_baseline import (
    read_baseline,
    sha256_file,
)
from cli._migrations.tools import (
    ToolsMigrationResult,
    migrate_tool_refs,
)
from files import PROTECTED_WRITE_PATHS
from kolsch._yamlio import YAMLError
from kolsch._yamlio import load as _yaml_load
from providers.authz import (
    derive_rule_id,
    describe_default_deny_tail_problem,
    inspect_default_deny_tail,
    platform_rules_from_config,
)

if TYPE_CHECKING:
    import argparse
    from pathlib import Path
    from typing import TextIO


# ---------------------------------------------------------------------------
# Source template — same constant the setup module uses; kept in sync via
# `_STARTER_BUNDLE_TEMPLATE_DIR` at the setup module's site.
# ---------------------------------------------------------------------------


def _starter_bundle_dir() -> Path:
    """Locate the packaged starter bundle template.

    Lives beside ``cli/_setup.py``; resolves at call time so tests can
    monkeypatch the module attribute without import-order concerns.
    """
    from cli._setup import _STARTER_BUNDLE_TEMPLATE_DIR  # noqa: PLC0415

    return _STARTER_BUNDLE_TEMPLATE_DIR


# ---------------------------------------------------------------------------
# Public entry point (called from cli.main dispatch)
# ---------------------------------------------------------------------------


async def cmd_doctor(args: object, *, out: TextIO, root: Path) -> int:
    """Run the drift report; return 0 clean / 1 drift.

    ``args`` is accepted for parity with the other ``cmd_*`` handlers
    in ``cli.main`` (no CLI options today); ``out`` and ``root`` are
    injected so tests can capture output + point at a tmp workspace.
    """
    _ = args  # no CLI flags in qol-089's scope
    report = _build_report(root)
    if report.not_yet_installed:
        # Pre-`kolsch setup` workspace — no bundle materialized, no
        # baseline. Emit a friendly one-liner instead of dumping the
        # entire source manifest as "missing". Exit 0 — this isn't
        # drift, it's the "you haven't installed yet" case.
        out.write(
            "no kolsch bundle materialized at this workspace — "
            "run `kolsch setup` to install the starter bundle.\n"
        )
        _render_authz_tail(report.authz_tail_note, out=out)
        _render_agent_write_access(report.agent_write_access, out=out)
        return 0
    _render(report, out=out)
    return 1 if report.has_drift else 0


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class _Report:
    """Aggregated drift report for one workspace.

    Kept as a plain class (not a dataclass) so the renderer can update
    an internal ``no_baseline_note`` flag without a mutable-dataclass
    dance; the report is a report, not a business object.
    """

    def __init__(self) -> None:
        self.missing: list[str] = []
        self.modified: list[str] = []
        self.retired_at_source: list[str] = []
        self.migration: ToolsMigrationResult = ToolsMigrationResult()
        self.no_baseline_note: bool = False
        # True when the workspace has no `bundles/kolsch/` at all —
        # the pre-`kolsch setup` case. Rendered as a friendly hint
        # rather than a false-positive drift dump.
        self.not_yet_installed: bool = False
        # bug-152 — default-deny tail finding for `platform.yaml`, or
        # `None`. Advisory: deliberately NOT part of `has_drift`.
        self.authz_tail_note: str | None = None
        # bug-161 — "Agent write access" findings (list) or a
        # "couldn't check" reason (str). Advisory: NOT part of `has_drift`.
        self.agent_write_access: list[str] | str = []

    @property
    def has_drift(self) -> bool:
        """True when any section has entries — drives exit code."""
        return bool(
            self.missing
            or self.modified
            or self.retired_at_source
            or self.migration.rewrites
            or self.migration.user_owned_hits
            or self.migration.parse_failures
        )


def _build_report(workspace_root: Path) -> _Report:
    """Assemble the drift report — pure detection, no writes."""
    report = _Report()
    report.authz_tail_note = _authz_tail_note(workspace_root)
    report.agent_write_access = _agent_write_access(workspace_root)
    source_dir = _starter_bundle_dir()
    workspace_bundle = workspace_root / "bundles" / "kolsch"

    if source_dir.is_dir() and workspace_bundle.is_dir():
        source_files = _relative_files(source_dir)
        workspace_files = _relative_files(workspace_bundle)

        report.missing = sorted(source_files - workspace_files)
        report.retired_at_source = sorted(workspace_files - source_files)

        baseline = read_baseline(workspace_root)
        if baseline is None:
            report.no_baseline_note = True
        else:
            report.modified = _diff_vs_baseline(
                workspace_bundle,
                shared_files=source_files & workspace_files,
                baseline=baseline.files,
            )
    elif source_dir.is_dir() and not workspace_bundle.is_dir():
        # Pre-`kolsch setup` workspace — flagged so the CLI can emit
        # the "run kolsch setup to install" hint rather than the full
        # source manifest as a false-positive drift dump.
        report.not_yet_installed = True
        return report

    # Ref-drift detection: kolsch-owned rewrites + user-owned hits.
    report.migration = migrate_tool_refs(workspace_root)
    return report


def _authz_tail_note(workspace_root: Path) -> str | None:
    """Describe a missing / misplaced default-deny tail, or return `None`.

    Read-only. Normalizes `authorization.config.rules` exactly as the
    runtime does (absent / `null` → `[]`). Returns `None` silently when
    `platform.yaml` is absent, unreadable, unparseable, or not a mapping
    — the migrator's parse-failure section owns reporting those. When
    the document parses but its `authorization` shape can't be read,
    returns a "couldn't check the default-deny tail: <reason>" note
    instead of guessing.
    """
    path = workspace_root / "platform.yaml"
    try:
        data = _yaml_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    reason = _authz_shape_problem(data)
    if reason is not None:
        return f"couldn't check the default-deny tail: {reason}."
    rules = platform_rules_from_config(data["authorization"].get("config", {}))
    if rules is None:  # unreachable after `_authz_shape_problem`; keeps mypy honest
        return None
    return describe_default_deny_tail_problem(inspect_default_deny_tail(rules))


def _yaml_kind(value: object) -> str:
    """Name `value`'s YAML kind (`null`, `string`, `list`, …) for user text."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a mapping"
    return type(value).__name__


def _authz_shape_problem(data: dict[str, object]) -> str | None:
    """Name why `authorization.config.rules` can't be read, or `None`."""
    if "authorization" not in data:
        return "`authorization` section is missing"
    authorization = data["authorization"]
    if not isinstance(authorization, dict):
        return f"`authorization` is {_yaml_kind(authorization)}, not a mapping"
    config = authorization.get("config", {})
    if not isinstance(config, dict):
        return f"`authorization.config` is {_yaml_kind(config)}, not a mapping"
    if platform_rules_from_config(config) is None:
        rules = config.get("rules")
        return f"`authorization.config.rules` is {_yaml_kind(rules)}, not a list"
    return None


# bug-161 — the refs whose `allow` rules route around the write floor.
_BASH_REFS = ("kolsch/tools/bash", "tool.bash")
_WRITE_FILE_REFS = (
    "kolsch/tools/write_file",
    "kolsch/nodes/write_file",
    "tool.write_file",
)
_GLOB_CHARS = "*?["

# One finding line per class: (subject, consequence). Rule labels are
# grouped into the subject so N near-identical rules give one line.
_BASH_FINDING = (
    "allow `bash` without asking",
    "agents can run shell commands, which bypasses every file protection, "
    "including this one. A `*` in a bash pattern also matches "
    "`; <any command>`.",
)
_BUNDLES_FINDING = (
    "allow `write_file` under `bundles/` without asking",
    "agents can change bundle code and permission YAML (equivalent to running code).",
)
_WILDCARD_FINDING = (
    "allow `write_file` on a glob pattern without asking",
    "rule patterns match the path as the agent wrote it, and `*`, `?` and "
    "`[...]` also match `/` and `.`, so these rules can reach `bundles/` "
    "too (for example through `../`) until authz patterns are matched "
    "against normalized paths.",
)
_ALL_CLEAR = (
    "no rule allows `bash`, and no rule allows `write_file` under "
    "`bundles/` or with a glob pattern (`*`, `?`, `[`), without asking"
)


def _agent_write_access(workspace_root: Path) -> list[str] | str:
    """Return the "Agent write access" findings, or why they can't be computed.

    Read-only. A list (possibly empty) of warning lines when
    `platform.yaml`'s rules can be read; otherwise a short reason
    string for the "couldn't check" line.
    """
    path = workspace_root / "platform.yaml"
    try:
        data = _yaml_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "platform.yaml not found"
    except (OSError, UnicodeDecodeError, YAMLError):
        return "platform.yaml couldn't be read or parsed"
    if not isinstance(data, dict):
        return "platform.yaml is not a mapping"
    reason = _authz_shape_problem(data)
    if reason is not None:
        return reason
    rules = platform_rules_from_config(data["authorization"].get("config", {}))
    if rules is None:  # unreachable after `_authz_shape_problem`; keeps mypy honest
        return "`authorization.config.rules` is not a list"
    return _agent_write_access_findings(rules, workspace_root)


def _agent_write_access_findings(rules: list[object], root: Path) -> list[str]:
    """Flag platform `allow` rules that route around the write floor.

    Pure. Three classes, per bug-161 SC2 / SC7, one line each with the
    matching rule numbers (1-based positions in the raw list, so
    non-mapping entries, which are skipped, don't shift the numbers):

    - **bash** — an `allow` rule whose `what` covers `kolsch/tools/bash`.
    - **bundles** — a `write_file` `allow` rule that reaches
      `bundles/`: its pattern matches `bundles/x` or `<root>/bundles/x`,
      or the literal part before its first glob character (normalized)
      sits under `bundles/` or `<root>/bundles/`. Compared casefolded,
      since the filesystem may be case-insensitive. A legacy rule has
      no target pattern, so a legacy `write_file` allow counts.
    - **glob** — any other `write_file` `allow` whose pattern contains
      a glob character (`*`, `?`, `[`). Patterns match the raw path
      argument and `fnmatch` lets each of these match `/` and `.`, so
      `data/*` reaches `data/../bundles/…` and `?undles/…` reaches
      `bundles/…` — spec F1.

    New-shape rules match `what` with
    `fnmatchcase`, as the provider does; legacy rules (`who` present)
    fullmatch `what` as a regex. Rule order is not considered: an
    `allow` shadowed by an earlier `deny` is still reported (advisory).
    """
    bash: list[str] = []
    bundles: list[str] = []
    wildcard: list[str] = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict) or rule.get("effect") != "allow":
            continue
        what = str(rule.get("what", ""))
        label = f"{index + 1} ({_rule_id(rule)})"
        if "who" in rule:
            if _legacy_what_covers(what, _BASH_REFS):
                bash.append(label)
            if _legacy_what_covers(what, _WRITE_FILE_REFS):
                bundles.append(label)
            continue
        if any(fnmatchcase(ref, what) for ref in _BASH_REFS):
            bash.append(label)
        if not any(fnmatchcase(ref, what) for ref in _WRITE_FILE_REFS):
            continue
        pattern = str(rule.get("pattern", "*"))
        if _pattern_reaches_bundles(pattern, root):
            bundles.append(label)
        elif any(char in pattern for char in _GLOB_CHARS):
            wildcard.append(label)
    findings: list[str] = []
    for labels, (subject, consequence) in (
        (bash, _BASH_FINDING),
        (bundles, _BUNDLES_FINDING),
        (wildcard, _WILDCARD_FINDING),
    ):
        if labels:
            noun = "rule" if len(labels) == 1 else "rules"
            findings.append(f"{noun} {', '.join(labels)} {subject}: {consequence}")
    return findings


def _rule_id(rule: dict[str, object]) -> str:
    """The id `kolsch permissions list` shows for `rule` (explicit or derived)."""
    explicit = rule.get("id")
    if explicit:
        return str(explicit)
    what = str(rule.get("what", ""))
    effect = str(rule.get("effect", ""))
    if "who" in rule:
        return derive_rule_id(str(rule["who"]), what, effect)
    return derive_rule_id(".*", f"{what}|{rule.get('pattern', '*')}", effect)


def _pattern_reaches_bundles(pattern: str, root: Path) -> bool:
    """Return `True` when a `write_file` pattern targets `bundles/` directly."""
    bundles_dirs = ("bundles", str(root / "bundles"))
    probes = ("bundles/x", str(root / "bundles" / "x"))
    folded = pattern.casefold()
    if any(fnmatchcase(p.casefold(), folded) for p in probes):
        return True
    literal = pattern
    for i, char in enumerate(pattern):
        if char in _GLOB_CHARS:
            literal = pattern[:i]
            break
    if not literal:
        return False
    prefix = posixpath.normpath(literal).casefold()
    return any(
        prefix == d.casefold() or prefix.startswith(d.casefold() + "/")
        for d in bundles_dirs
    )


def _legacy_what_covers(what: str, refs: tuple[str, ...]) -> bool:
    """Return `True` when legacy regex `what` fullmatches any of `refs`."""
    try:
        compiled = re.compile(what)
    except re.error:
        return False
    return any(compiled.fullmatch(ref) for ref in refs)


def _relative_files(root: Path) -> set[str]:
    """Return every non-directory file under ``root``, as relative POSIX paths."""
    if not root.is_dir():
        return set()
    return {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def _diff_vs_baseline(
    bundle_dir: Path,
    *,
    shared_files: set[str],
    baseline: dict[str, str],
) -> list[str]:
    """List shared files whose current sha256 differs from the baseline."""
    modified: list[str] = []
    for rel in sorted(shared_files):
        recorded = baseline.get(rel)
        if recorded is None:
            # File exists in source + workspace but wasn't in the
            # baseline snapshot (e.g. baseline predates this file's
            # addition). Treated as "modified" so the operator sees
            # it and can decide.
            modified.append(rel)
            continue
        current = sha256_file(bundle_dir / rel)
        if current != recorded:
            modified.append(rel)
    return modified


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(report: _Report, *, out: TextIO) -> None:
    """Print the five report sections; write nothing on a clean workspace."""
    if not report.has_drift and not report.no_baseline_note:
        out.write("✓ no drift detected — bundle in sync with source.\n")
        _render_authz_tail(report.authz_tail_note, out=out)
        _render_agent_write_access(report.agent_write_access, out=out)
        return

    out.write("Bundle drift report\n")
    out.write("===================\n\n")

    _render_missing(report.missing, out=out)
    _render_modified(report.modified, no_baseline=report.no_baseline_note, out=out)
    _render_retired_at_source(report.retired_at_source, out=out)
    _render_parse_failures(report.migration, out=out)
    _render_kolsch_owned_rewrites(report.migration, out=out)
    _render_user_owned_hits(report.migration, out=out)

    if report.has_drift:
        out.write(
            "\nEdit the listed files by hand to repair. Automated "
            "`kolsch setup --sync-bundle` support is planned in qol-093 "
            "(not yet shipped).\n"
        )
    _render_authz_tail(report.authz_tail_note, out=out)
    _render_agent_write_access(report.agent_write_access, out=out)


def _render_authz_tail(note: str | None, *, out: TextIO) -> None:
    """Print the advisory Authorization section; nothing when `note` is None."""
    if note is None:
        return
    out.write("\nAuthorization (platform.yaml) — advisory, exit code unchanged:\n")
    out.write(f"  ! {note}\n")


def _render_agent_write_access(findings: list[str] | str, *, out: TextIO) -> None:
    """Print the advisory "Agent write access" section (always)."""
    out.write("\nAgent write access — advisory, exit code unchanged:\n")
    protected = ", ".join(PROTECTED_WRITE_PATHS)
    out.write(
        f"  agent file tools can never write: {protected} (whatever the rules allow)\n"
    )
    if isinstance(findings, str):
        out.write(f"  ? couldn't check agent write access: {findings}.\n")
        return
    if not findings:
        out.write(f"  ✓ {_ALL_CLEAR}\n")
    for line in findings:
        out.write(f"  ! {line}\n")
    if findings:
        out.write("  see these rules (by id) with `kolsch permissions list`.\n")
    out.write(
        "  note: `kolsch chat --auto-approve` turns every `prompt` rule "
        "into an allow.\n"
    )


def _render_missing(missing: list[str], *, out: TextIO) -> None:
    out.write("Missing (present in source, absent from workspace):\n")
    if not missing:
        out.write("  ✓ none\n\n")
        return
    for rel in missing:
        out.write(f"  + {rel}\n")
    out.write("\n")


def _render_modified(
    modified: list[str],
    *,
    no_baseline: bool,
    out: TextIO,
) -> None:
    out.write("Modified vs baseline (sha256 mismatch):\n")
    if no_baseline:
        out.write(
            "  · baseline not recorded — run `kolsch setup` to record "
            "the current shipped file set.\n\n"
        )
        return
    if not modified:
        out.write("  ✓ none\n\n")
        return
    for rel in modified:
        out.write(f"  ~ {rel}\n")
    out.write("\n")


def _render_retired_at_source(retired: list[str], *, out: TextIO) -> None:
    out.write("Retired at source (present in workspace, no longer shipped):\n")
    if not retired:
        out.write("  ✓ none\n\n")
        return
    for rel in retired:
        out.write(f"  - {rel}\n")
    out.write("\n")


def _render_parse_failures(migration: ToolsMigrationResult, *, out: TextIO) -> None:
    """Print kolsch-owned YAMLs the migrator couldn't parse.

    Rendered only when the section has entries — doctor's whole reason
    to exist is diagnosing broken workspaces, so a broken YAML must
    surface as an actionable line rather than a stack trace. See
    qol-089 Reviewer #2 finding #1.
    """
    if not migration.parse_failures:
        return
    out.write("Kolsch-owned YAMLs the migrator couldn't parse (fix + rerun):\n")
    for failure in migration.parse_failures:
        out.write(f"  ✗ {failure.path}: {failure.reason}\n")
    out.write("\n")


def _render_kolsch_owned_rewrites(
    migration: ToolsMigrationResult, *, out: TextIO
) -> None:
    out.write("Retired-tool refs in kolsch-owned YAMLs (proposed rewrite):\n")
    if not migration.rewrites:
        out.write("  ✓ none\n\n")
        return
    for rewrite in migration.rewrites:
        if rewrite.new_ref is None:
            # Retired action — surface the manifest's `replacement`
            # pointer when it's set so the operator sees the intended
            # migration target, not just "gone".
            hint = (
                f" (consider: `{rewrite.replacement}`)" if rewrite.replacement else ""
            )
            out.write(f"  - {rewrite.path}: remove `{rewrite.old_ref}`{hint}\n")
        else:
            out.write(f"  ~ {rewrite.path}: `{rewrite.old_ref}` → `{rewrite.new_ref}`")
            if rewrite.added:
                out.write(f" (+ {', '.join(rewrite.added)})")
            out.write("\n")
    out.write("\n")


def _render_user_owned_hits(migration: ToolsMigrationResult, *, out: TextIO) -> None:
    out.write(
        "Retired-tool refs in user-owned YAMLs (manual fix — kolsch will "
        "not rewrite these):\n"
    )
    if not migration.user_owned_hits:
        out.write("  ✓ none\n\n")
        return
    for hit in migration.user_owned_hits:
        target = hit.suggested_new_ref or "(no direct replacement)"
        out.write(f"  ! {hit.path}: `{hit.old_ref}` → suggest `{target}`\n")
        if hit.notes:
            out.write(f"      {hit.notes}\n")
    out.write("\n")


# ---------------------------------------------------------------------------
# argparse subparser registration — called from cli.main._build_parser
# ---------------------------------------------------------------------------


def add_doctor_subparser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``kolsch doctor`` subcommand.

    Read-only; no flags in qol-089's scope. qol-093 may add
    ``--verbose`` / ``--json`` variants later without touching this
    module's core detection path.
    """
    sub.add_parser(
        "doctor",
        help=(
            "Report bundle-drift: added/modified/retired tool files "
            "and retired-tool refs across kolsch-owned + user-owned YAMLs."
        ),
    )
