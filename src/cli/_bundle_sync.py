"""``kolsch setup --sync-bundle`` — repair the drift ``kolsch doctor`` sees (qol-093).

Sits on top of qol-089's shipped foundation:

- :func:`cli._migrations.tools.migrate_tool_refs` — the pure planner
  that returns rewrites for kolsch-owned YAMLs and reports user-owned
  hits (never writes user files).
- :mod:`cli._bundle_baseline` — the sha256 baseline (``.kolsch/bundle-baseline.json``)
  written on install; the oracle for "unmodified since last shipped".
- :func:`cli._doctor._starter_bundle_dir` — locates the packaged
  source template so tests can monkeypatch it.

Sync classifies every file in ``bundles/kolsch/`` into one of four
buckets:

- **unchanged** — present in source + workspace, sha256 matches
  baseline → **safe to overwrite** with the source copy.
- **locally_modified** — present in source + workspace, sha256
  differs from baseline (or missing from baseline) → **prompt**
  (`[k]eep local / [o]verwrite / [d]iff`); non-interactive path
  defaults to keep-local + records a skip (partial-success exit).
- **added_at_source** — present in source, absent from workspace →
  copy in. Non-interactive: yes.
- **retired_at_source** — present in workspace, absent from source →
  prompt (`[k]eep / [d]elete`); non-interactive default is keep +
  records a skip.

After the file-level pass, applies the migrator's YAML rewrites for
``bundles/kolsch/agents/*.yaml`` and ``platform.yaml`` (if any) and
refreshes the baseline. **Never writes outside ``bundles/kolsch/**``
+ ``platform.yaml``** — the kolsch-owned boundary qol-089 established
is a hard invariant here.

Exit contract: sync returns ``0`` when the workspace is fully in
sync with source at exit (no skips, all rewrites applied). Any
skipped drift item (locally-modified kept, retired-at-source kept,
parse failure) triggers a non-zero return — the operator needs to
see the partial-success signal.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from difflib import unified_diff
from typing import TYPE_CHECKING

from cli._bundle_baseline import (
    read_baseline,
    sha256_file,
    write_baseline,
)
from cli._migrations.tools import (
    ToolsMigrationResult,
    migrate_tool_refs,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cli._setup import SetupPrompter, _SetupIO


@dataclass(frozen=True)
class _SyncOutcome:
    """One line item recorded during the sync pass."""

    rel: str
    # Human-readable verb for the operator report.
    action: str
    # Bucket the file came from ("modified" / "retired-at-source" /
    # "missing-from-workspace" / "yaml_rewrite").
    kind: str


@dataclass
class _SyncReport:
    """Aggregated sync-pass result — drives the exit code + operator report."""

    applied: list[_SyncOutcome] = field(default_factory=list)
    skipped: list[_SyncOutcome] = field(default_factory=list)
    parse_failures: list[str] = field(default_factory=list)
    user_owned_hits: list[str] = field(default_factory=list)
    baseline_refreshed: bool = False
    dry_run: bool = False

    def is_partial(self) -> bool:
        """True when the sync left any drift unresolved."""
        return bool(self.skipped or self.parse_failures)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def sync_kolsch_bundle(
    io: _SetupIO,
    *,
    dry_run: bool = False,
    yes: bool = False,
    no: bool = False,
) -> int:
    """Run the sync pass; return exit code (0 clean / non-zero on drift skipped).

    ``yes`` and ``no`` are mutually exclusive (the CLI enforces this
    before calling in; here we treat the combination as ``yes``
    winning). Non-interactive default (both False, no TTY) is
    equivalent to ``yes=True`` with the safe defaults: keep locally
    modified, keep retired files. That produces a non-zero exit for
    any drift item the user needs to see.
    """
    from cli._doctor import _starter_bundle_dir  # noqa: PLC0415 — avoid import cycle

    source_dir = _starter_bundle_dir()
    workspace_bundle = io.root / "bundles" / "kolsch"

    if not workspace_bundle.is_dir():
        io.out.write(
            "no kolsch bundle at this workspace — run `kolsch setup` "
            "to install the starter bundle first.\n"
        )
        return 1

    baseline = read_baseline(io.root)
    if baseline is None:
        io.out.write(
            "no `.kolsch/bundle-baseline.json` recorded — cannot classify "
            "shipped files as unchanged-vs-modified. Delete `bundles/kolsch/` "
            "and re-run `kolsch setup` to re-install with a fresh baseline.\n"
        )
        return 1

    plan = _plan(source_dir, workspace_bundle, baseline.files)
    migration = migrate_tool_refs(io.root)
    # Migrator-owned files supersede the file-level modified/unchanged
    # pass — a helper.yaml with a retired ref will register as
    # "modified" (sha256 drift vs baseline), but the migrator's
    # rewrite is the authoritative fix. Strip such paths from the
    # file-level plan so we don't double-handle them.
    plan = _defer_to_migrator(plan, migration)
    report = await _apply(
        io=io,
        source_dir=source_dir,
        workspace_bundle=workspace_bundle,
        plan=plan,
        migration=migration,
        dry_run=dry_run,
        yes=yes,
        no=no,
    )

    if not dry_run and not report.is_partial() and _wrote_anything(report):
        write_baseline(io.root, bundle_dir=workspace_bundle)
        report.baseline_refreshed = True

    _render_report(report, out=io.out, dry_run=dry_run)
    return 0 if not report.is_partial() else 1


# ---------------------------------------------------------------------------
# Planning — classify every file in source-vs-workspace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    """Sorted per-bucket relative-path lists — the sync loop's worklist."""

    added: list[str]  # in source, not in workspace
    modified: list[str]  # in both, sha256 mismatch vs baseline
    retired: list[str]  # in workspace, not in source
    unchanged: list[str]  # in both, sha256 matches baseline (safe overwrite)


def _plan(
    source_dir: Path,
    workspace_bundle: Path,
    baseline_files: dict[str, str],
) -> _Plan:
    """Classify every file in ``source-vs-workspace`` into one of four buckets."""
    source_files = _relative_files(source_dir)
    workspace_files = _relative_files(workspace_bundle)

    added = sorted(source_files - workspace_files)
    retired = sorted(workspace_files - source_files)
    shared = source_files & workspace_files

    modified: list[str] = []
    unchanged: list[str] = []
    for rel in sorted(shared):
        recorded = baseline_files.get(rel)
        current = sha256_file(workspace_bundle / rel)
        if recorded is None or current != recorded:
            # No baseline entry OR sha256 mismatch → treated as
            # locally modified (safer: prompt before touching).
            modified.append(rel)
        else:
            unchanged.append(rel)
    return _Plan(added=added, modified=modified, retired=retired, unchanged=unchanged)


def _defer_to_migrator(plan: _Plan, migration: ToolsMigrationResult) -> _Plan:
    """Remove paths the migrator will rewrite from ``modified``/``unchanged``.

    Kolsch-owned YAMLs the migrator handles (``bundles/kolsch/agents/*.yaml``,
    ``platform.yaml``) live in ``migration.yaml_bodies`` keyed by
    workspace-relative path. When a helper agent YAML has a retired
    ref, its sha256 drifts from the baseline *because* of the user's
    or the previous session's rewrite; the migrator is the correct
    fixer, not "overwrite from source" (which would clobber every
    user tweak the file also carries).
    """
    migrator_paths = set(migration.yaml_bodies)
    # Migrator paths are workspace-relative from `io.root`; the plan's
    # entries are relative to `bundles/kolsch/`. Strip the prefix so
    # the sets can compare.
    migrator_bundle_paths = {
        p.removeprefix("bundles/kolsch/")
        for p in migrator_paths
        if p.startswith("bundles/kolsch/")
    }
    return _Plan(
        added=plan.added,
        modified=[r for r in plan.modified if r not in migrator_bundle_paths],
        retired=plan.retired,
        unchanged=[r for r in plan.unchanged if r not in migrator_bundle_paths],
    )


def _relative_files(root: Path) -> set[str]:
    """Return every non-directory file under ``root``, as relative POSIX paths."""
    if not root.is_dir():
        return set()
    return {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


# ---------------------------------------------------------------------------
# Apply pass — walk plan, prompt (or use --yes / --no defaults), write
# ---------------------------------------------------------------------------


async def _apply(
    *,
    io: _SetupIO,
    source_dir: Path,
    workspace_bundle: Path,
    plan: _Plan,
    migration: ToolsMigrationResult,
    dry_run: bool,
    yes: bool,
    no: bool,
) -> _SyncReport:
    """Walk the plan; write per decisions; return an aggregated report."""
    report = _SyncReport(dry_run=dry_run)

    for failure in migration.parse_failures:
        report.parse_failures.append(f"{failure.path}: {failure.reason}")

    # 1. Added-at-source — safe to copy in.
    for rel in plan.added:
        _copy_source_file(
            io=io,
            source_dir=source_dir,
            workspace_bundle=workspace_bundle,
            rel=rel,
            report=report,
            dry_run=dry_run,
            yes=yes,
            no=no,
        )

    # 2. Unchanged — overwrite silently to pick up any whitespace-only
    #    re-flow in source. (Overwrite of an identical file is a no-op
    #    for content but refreshes the baseline entry.)
    for rel in plan.unchanged:
        _refresh_unchanged_file(
            source_dir=source_dir,
            workspace_bundle=workspace_bundle,
            rel=rel,
            report=report,
            dry_run=dry_run,
        )

    # 3. Modified — prompt (or --yes safe default: keep local).
    for rel in plan.modified:
        _resolve_modified_file(
            io=io,
            source_dir=source_dir,
            workspace_bundle=workspace_bundle,
            rel=rel,
            report=report,
            dry_run=dry_run,
            yes=yes,
            no=no,
        )

    # 4. Retired-at-source — prompt (or --yes safe default: keep).
    for rel in plan.retired:
        _resolve_retired_file(
            io=io,
            workspace_bundle=workspace_bundle,
            rel=rel,
            report=report,
            dry_run=dry_run,
            yes=yes,
            no=no,
        )

    # 5. YAML rewrites from the migrator (only for kolsch-owned files;
    #    user-owned hits are report-only).
    for rel, body in migration.yaml_bodies.items():
        target = io.root / rel
        if not dry_run:
            _write_atomic(target, body)
        report.applied.append(
            _SyncOutcome(rel=rel, action="rewrote", kind="yaml_rewrite")
        )

    # Record user-owned hits — reported, never written.
    for user_hit in migration.user_owned_hits:
        summary = f"{user_hit.path}: `{user_hit.old_ref}`"
        if user_hit.suggested_new_ref:
            summary += f" → suggest `{user_hit.suggested_new_ref}`"
        report.user_owned_hits.append(summary)

    return report


def _copy_source_file(
    *,
    io: _SetupIO,
    source_dir: Path,
    workspace_bundle: Path,
    rel: str,
    report: _SyncReport,
    dry_run: bool,
    yes: bool,
    no: bool,
) -> None:
    """Handle an ``added-at-source`` entry — copy to workspace by default."""
    if no:
        report.skipped.append(
            _SyncOutcome(rel=rel, action="skipped-added", kind="missing-from-workspace")
        )
        return
    if not yes:
        answer = _prompt_choice(
            io.prompter,
            question=f"[sync] {rel}: added at source, not in workspace.",
            options=["[c]opy into workspace", "[k]eep missing"],
            default="c",
        )
        if answer == "k":
            report.skipped.append(
                _SyncOutcome(
                    rel=rel, action="kept-missing", kind="missing-from-workspace"
                )
            )
            return
    if not dry_run:
        target = workspace_bundle / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_dir / rel, target)
    report.applied.append(
        _SyncOutcome(rel=rel, action="copied", kind="missing-from-workspace")
    )


def _refresh_unchanged_file(
    *,
    source_dir: Path,
    workspace_bundle: Path,
    rel: str,
    report: _SyncReport,
    dry_run: bool,
) -> None:
    """Rewrite an unchanged file from source; identical-bytes cases are a no-op.

    We only track this in the report when the source and workspace
    bytes actually differ (whitespace-only source re-flow). Identical
    files add no line to the report to keep it tight.
    """
    src = source_dir / rel
    dst = workspace_bundle / rel
    if src.read_bytes() == dst.read_bytes():
        return
    if not dry_run:
        shutil.copy2(src, dst)
    report.applied.append(
        _SyncOutcome(rel=rel, action="refreshed", kind="unchanged-vs-baseline")
    )


def _resolve_modified_file(
    *,
    io: _SetupIO,
    source_dir: Path,
    workspace_bundle: Path,
    rel: str,
    report: _SyncReport,
    dry_run: bool,
    yes: bool,
    no: bool,
) -> None:
    """Handle a locally-modified shipped file — prompt or use safe default."""
    if no or yes:
        # Safe default in non-interactive mode: keep local. Records
        # the skip so the partial-success signal fires.
        report.skipped.append(
            _SyncOutcome(rel=rel, action="kept-local", kind="modified")
        )
        return
    while True:
        answer = _prompt_choice(
            io.prompter,
            question=f"[sync] {rel}: locally modified vs baseline.",
            options=["[k]eep local", "[o]verwrite with source", "[d]iff"],
            default="k",
        )
        if answer == "d":
            _print_diff(io, source_dir / rel, workspace_bundle / rel, rel=rel)
            continue
        break
    if answer == "o":
        if not dry_run:
            shutil.copy2(source_dir / rel, workspace_bundle / rel)
        report.applied.append(
            _SyncOutcome(rel=rel, action="overwrote", kind="modified")
        )
        return
    report.skipped.append(_SyncOutcome(rel=rel, action="kept-local", kind="modified"))


def _resolve_retired_file(
    *,
    io: _SetupIO,
    workspace_bundle: Path,
    rel: str,
    report: _SyncReport,
    dry_run: bool,
    yes: bool,
    no: bool,
) -> None:
    """Handle a retired-at-source file — prompt or use flag defaults.

    Unlike modified files (which carry user edits), retired-at-source
    files are shipped platform files the source has since removed —
    the user has no reason to keep them. So ``--yes`` DELETES them
    (aligns with the ses_0e54 repair story); ``--no`` KEEPS them
    (strict, never touches). Interactive prompts per file.
    """
    if yes:
        if not dry_run:
            (workspace_bundle / rel).unlink(missing_ok=True)
        report.applied.append(
            _SyncOutcome(rel=rel, action="deleted", kind="retired-at-source")
        )
        return
    if no:
        report.skipped.append(
            _SyncOutcome(rel=rel, action="kept-retired", kind="retired-at-source")
        )
        return
    answer = _prompt_choice(
        io.prompter,
        question=f"[sync] {rel}: retired at source, still in workspace.",
        options=["[k]eep local", "[d]elete"],
        default="k",
    )
    if answer == "d":
        if not dry_run:
            (workspace_bundle / rel).unlink(missing_ok=True)
        report.applied.append(
            _SyncOutcome(rel=rel, action="deleted", kind="retired-at-source")
        )
        return
    report.skipped.append(
        _SyncOutcome(rel=rel, action="kept-retired", kind="retired-at-source")
    )


# ---------------------------------------------------------------------------
# Prompt + diff helpers
# ---------------------------------------------------------------------------


def _prompt_choice(
    prompter: SetupPrompter,
    *,
    question: str,
    options: list[str],
    default: str,
) -> str:
    """Wrap ``SetupPrompter.choose`` — returns the single-letter answer."""
    return prompter.choose(question, options, default)


def _print_diff(io: _SetupIO, source: Path, workspace: Path, *, rel: str) -> None:
    """Print a unified diff of workspace-vs-source for one file, then re-prompt."""
    src_text = source.read_text(encoding="utf-8", errors="replace").splitlines(
        keepends=True
    )
    ws_text = workspace.read_text(encoding="utf-8", errors="replace").splitlines(
        keepends=True
    )
    for line in unified_diff(ws_text, src_text, fromfile=f"a/{rel}", tofile=f"b/{rel}"):
        io.out.write(line)
    io.out.write("\n")


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + rename)."""
    import os  # noqa: PLC0415

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


def _wrote_anything(report: _SyncReport) -> bool:
    """True when the sync applied any change (used to gate baseline refresh)."""
    return bool(report.applied)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_report(report: _SyncReport, *, out: object, dry_run: bool) -> None:
    """Print an operator-facing summary of the sync pass.

    ``out`` is a TextIO — untyped here to keep the imports light.
    """
    write = out.write  # type: ignore[attr-defined]
    header = "Sync report (dry-run — no writes)" if dry_run else "Sync report"
    write(f"\n{header}\n")
    write("=" * len(header) + "\n\n")

    if report.applied:
        verb_map = {False: "Applied", True: "Would apply"}
        write(f"{verb_map[dry_run]} ({len(report.applied)}):\n")
        for outcome in report.applied:
            write(f"  ✓ {outcome.action}: {outcome.rel}\n")
        write("\n")

    if report.skipped:
        write(f"Skipped ({len(report.skipped)}):\n")
        for outcome in report.skipped:
            write(f"  · {outcome.action}: {outcome.rel}\n")
        write("\n")

    if report.parse_failures:
        write("Parse failures (fix + rerun):\n")
        for line in report.parse_failures:
            write(f"  ✗ {line}\n")
        write("\n")

    if report.user_owned_hits:
        write("User-owned refs (manual fix — kolsch does not rewrite user bundles):\n")
        for line in report.user_owned_hits:
            write(f"  ! {line}\n")
        write("\n")

    if report.baseline_refreshed:
        write("✓ baseline refreshed at .kolsch/bundle-baseline.json\n")

    if not report.applied and not report.skipped and not report.parse_failures:
        write("✓ no drift — workspace already in sync with source.\n")
