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

qol-089 spec section §Acceptance Criteria (5) and (6) drive the
contract. qol-093 will consume the same detection logic for its
``--sync-bundle`` path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cli._bundle_baseline import (
    read_baseline,
    sha256_file,
)
from cli._migrations.tools import (
    ToolsMigrationResult,
    migrate_tool_refs,
)
from kolsch._yamlio import YAMLError
from kolsch._yamlio import load as _yaml_load
from providers.authz import (
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
    `platform.yaml` is absent, unreadable, unparseable, or not shaped
    like a platform config — other surfaces own reporting those.
    """
    path = workspace_root / "platform.yaml"
    try:
        data = _yaml_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    authorization = data.get("authorization")
    if not isinstance(authorization, dict):
        return None
    rules = platform_rules_from_config(authorization.get("config", {}))
    if rules is None:
        return None
    return describe_default_deny_tail_problem(inspect_default_deny_tail(rules))


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


def _render_authz_tail(note: str | None, *, out: TextIO) -> None:
    """Print the advisory Authorization section; nothing when `note` is None."""
    if note is None:
        return
    out.write("\nAuthorization (platform.yaml) — advisory, exit code unchanged:\n")
    out.write(f"  ! {note}\n")


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
