"""``kolsch setup --sync-bundle`` — repair the drift ``kolsch doctor`` sees (qol-093).

Sits on top of qol-089's shipped foundation:

- :func:`cli._migrations.tools.migrate_tool_refs` — the pure planner
  that returns rewrites for kolsch-owned YAMLs and reports user-owned
  hits (never writes user files).
- :mod:`cli._bundle_baseline` — the sha256 baseline (``.kolsch/bundle-baseline.json``)
  written on install and updated per file by every sync; the oracle for
  "unmodified since last shipped". Optional (bug-146): a workspace
  installed before qol-089 has none, and sync records one.
- :func:`cli._doctor._starter_bundle_dir` — locates the packaged
  source template so tests can monkeypatch it.

Sync classifies every file in ``bundles/kolsch/`` (as
:func:`cli._bundle_baseline.iter_bundle_files` walks it — no
``__pycache__/`` or ``.DS_Store``) into one of four buckets, by
:func:`plan_bundle_files`:

- **unchanged** — present in source + workspace, and byte-identical to
  the source copy, or sha256 matches the baseline entry → **safe to
  overwrite** with the source copy.
- **locally_modified** — present in source + workspace, differs from
  source, and sha256 differs from the baseline entry or has none
  (always the case without a baseline) → **prompt**
  (`[k]eep local / [o]verwrite / [d]iff`); non-interactive path
  defaults to keep-local + records a skip (partial-success exit).
- **added_at_source** — present in source, absent from workspace →
  copy in. Non-interactive: yes.
- **retired_at_source** — present in workspace, absent from source →
  prompt (`[k]eep / [d]elete`); non-interactive default is keep +
  records a skip.

After the file-level pass, applies the migrator's YAML rewrites for
``bundles/kolsch/agents/*.yaml`` and ``platform.yaml`` (if any) and
updates the baseline — on every non-dry-run pass, partial or not — with
an entry for each file sync copied, refreshed, overwrote, rewrote or
found identical to source; a kept-local file keeps its prior entry (or none),
so it is still "modified" next run. **Never writes outside
``bundles/kolsch/**``, ``platform.yaml`` and ``.kolsch/``** — the
kolsch-owned boundary qol-089 established is a hard invariant here.

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
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from cli._bundle_baseline import (
    iter_bundle_files,
    read_baseline,
    sha256_file,
    update_baseline_entries,
)
from cli._migrations.tools import (
    ToolsMigrationResult,
    migrate_tool_refs,
    plan_platform_yaml_rewrite,
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
    # bug-146 — `{bundle-relative path: "sha256:…" | None}` for every
    # file this pass wrote or found identical to source (`None` =
    # deleted). Merged into the baseline after a non-dry-run pass.
    baseline_entries: dict[str, str | None] = field(default_factory=dict)
    baseline_refreshed: bool = False
    dry_run: bool = False
    # Ran under `--yes` / `--no`: the closing line says how to review
    # the kept items.
    non_interactive: bool = False
    # bug-146 — the default-rule pass (cli._rules_upgrade).
    rule_lines: list[str] = field(default_factory=list)
    rule_failures: list[str] = field(default_factory=list)
    rules_offered: bool = False
    # Bundle-relative paths deleted (or, in a dry run, to be deleted)
    # by the retired pass; the migrator must not write them back.
    deleted: set[str] = field(default_factory=set)

    def is_partial(self) -> bool:
        """True when the sync left any drift unresolved."""
        return bool(self.skipped or self.parse_failures or self.rule_failures)


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
        # bug-146 — pre-qol-089 workspaces (and a corrupt baseline)
        # bootstrap: a file identical to source is unchanged, anything
        # else prompts as modified; the pass then records a baseline.
        io.out.write("no baseline recorded — classifying against current source.\n")

    plan = plan_bundle_files(
        source_dir,
        workspace_bundle,
        baseline.files if baseline is not None else None,
    )
    migration = migrate_tool_refs(io.root)
    # Migrator-owned files supersede the file-level modified/unchanged
    # pass — a helper.yaml with a retired ref will register as
    # "modified" (sha256 drift vs baseline), but the migrator's
    # rewrite is the authoritative fix. Strip such paths from the
    # file-level plan so we don't double-handle them.
    plan, pristine_rewrites = _defer_to_migrator(plan, migration)
    report = await _apply(
        io=io,
        source_dir=source_dir,
        workspace_bundle=workspace_bundle,
        plan=plan,
        migration=migration,
        pristine_rewrites=pristine_rewrites,
        baseline_files=baseline.files if baseline is not None else {},
        dry_run=dry_run,
        yes=yes,
        no=no,
    )
    report.non_interactive = yes or no

    # bug-146 — after the file pass (so newly copied tools are in the
    # catalog) and the migrator's platform.yaml rewrite.
    from cli._rules_upgrade import run_default_rules_pass  # noqa: PLC0415

    rules_outcome = await run_default_rules_pass(
        io,
        dry_run=dry_run,
        yes=yes,
        no=no,
        pending_tool_refs=_dry_run_copied_tool_refs(report) if dry_run else (),
    )
    report.rule_lines = rules_outcome.lines
    report.rule_failures = rules_outcome.failures
    report.rules_offered = rules_outcome.offered

    if not dry_run:
        report.baseline_refreshed = update_baseline_entries(
            io.root, report.baseline_entries
        )

    _render_report(report, out=io.out, dry_run=dry_run)
    return 0 if not report.is_partial() else 1


# ---------------------------------------------------------------------------
# Planning — classify every file in source-vs-workspace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BundleFilePlan:
    """Sorted per-bucket relative-path lists — the sync loop's worklist.

    Paths are relative to the bundle directory (e.g. ``tools/bash.py``).
    Built by [`plan_bundle_files`][cli._bundle_sync.plan_bundle_files].

    Attributes:
        added: In source, not in the workspace.
        modified: In both; differs from source, and the workspace hash
            differs from its baseline entry or has none.
        retired: In the workspace, not in source.
        unchanged: In both; identical to source, or the workspace hash
            matches its baseline entry (safe to overwrite from source).
    """

    added: list[str]
    modified: list[str]
    retired: list[str]
    unchanged: list[str]


def plan_bundle_files(
    source_dir: Path,
    workspace_bundle: Path,
    baseline_files: dict[str, str] | None,
) -> BundleFilePlan:
    """Classify every shipped file in source-vs-workspace into four buckets.

    Read-only. A shared file byte-identical to its source copy is always
    ``unchanged``, with or without a baseline entry (bug-146). Otherwise
    it is ``unchanged`` only when its sha256 matches the baseline entry
    (an older shipped version nobody edited), and ``modified`` when the
    entry differs or is missing — always the case when
    ``baseline_files`` is ``None`` (no baseline recorded).

    Args:
        source_dir: The packaged starter-bundle template.
        workspace_bundle: The workspace's ``bundles/kolsch/``.
        baseline_files: The baseline's ``files`` map, or ``None``.

    Returns:
        The [`BundleFilePlan`][cli._bundle_sync.BundleFilePlan].
    """
    source_files = iter_bundle_files(source_dir)
    workspace_files = iter_bundle_files(workspace_bundle)
    recorded_files = baseline_files or {}

    added = sorted(source_files - workspace_files)
    retired = sorted(workspace_files - source_files)
    shared = source_files & workspace_files

    modified: list[str] = []
    unchanged: list[str] = []
    for rel in sorted(shared):
        current = sha256_file(workspace_bundle / rel)
        if current == sha256_file(source_dir / rel):
            unchanged.append(rel)
            continue
        recorded = recorded_files.get(rel)
        if recorded is None or current != recorded:
            # No baseline entry OR sha256 mismatch → treated as
            # locally modified (safer: prompt before touching).
            modified.append(rel)
        else:
            unchanged.append(rel)
    return BundleFilePlan(
        added=added, modified=modified, retired=retired, unchanged=unchanged
    )


def _defer_to_migrator(
    plan: BundleFilePlan, migration: ToolsMigrationResult
) -> tuple[BundleFilePlan, set[str]]:
    """Remove paths the migrator will rewrite from ``modified``/``unchanged``.

    Kolsch-owned YAMLs the migrator handles (``bundles/kolsch/agents/*.yaml``,
    ``platform.yaml``) live in ``migration.yaml_bodies`` keyed by
    workspace-relative path. When a helper agent YAML has a retired
    ref, its sha256 drifts from the baseline *because* of the user's
    or the previous session's rewrite; the migrator is the correct
    fixer, not "overwrite from source" (which would clobber every
    user tweak the file also carries).

    Also returns the deferred paths that were ``unchanged`` (pristine:
    identical to source, or matching their baseline entry). Only those
    get the rewritten file's hash recorded in the baseline; a deferred
    ``modified`` file keeps its prior entry (or none), so a later run
    still asks before touching the user's edit (bug-146).
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
    deferred = BundleFilePlan(
        added=plan.added,
        modified=[r for r in plan.modified if r not in migrator_bundle_paths],
        retired=plan.retired,
        unchanged=[r for r in plan.unchanged if r not in migrator_bundle_paths],
    )
    return deferred, migrator_bundle_paths.intersection(plan.unchanged)


# ---------------------------------------------------------------------------
# Apply pass — walk plan, prompt (or use --yes / --no defaults), write
# ---------------------------------------------------------------------------


async def _apply(
    *,
    io: _SetupIO,
    source_dir: Path,
    workspace_bundle: Path,
    plan: BundleFilePlan,
    migration: ToolsMigrationResult,
    pristine_rewrites: set[str],
    baseline_files: dict[str, str],
    dry_run: bool,
    yes: bool,
    no: bool,
) -> _SyncReport:
    """Walk the plan; write per decisions; return an aggregated report.

    Every file written (copied, refreshed, overwritten, or rewritten by
    the migrator while pristine), or left identical to source, is
    recorded in ``report.baseline_entries`` (deleted files as ``None``).
    """
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

    # 2. Unchanged — overwrite silently to pick up an older shipped
    #    version's changes. (Overwrite of an identical file is a no-op
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
            in_baseline=rel in baseline_files,
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
            recorded=baseline_files.get(rel),
            report=report,
            dry_run=dry_run,
            yes=yes,
            no=no,
        )

    # 5. YAML rewrites from the migrator (only for kolsch-owned files;
    #    user-owned hits are report-only).
    for rel, body in migration.yaml_bodies.items():
        target = io.root / rel
        bundle_rel = rel.removeprefix("bundles/kolsch/")
        if rel.startswith("bundles/kolsch/") and bundle_rel in report.deleted:
            # Deleted by the retired pass above — don't write it back.
            continue
        if rel == "platform.yaml":
            if not dry_run and not _rewrite_platform_yaml_locked(io.root):
                continue
        elif not dry_run:
            _write_atomic(target, body)
            if rel.startswith("bundles/kolsch/") and bundle_rel in pristine_rewrites:
                # Nobody edited it, so the migrator's output is what
                # kolsch shipped. An edited file keeps its old entry.
                report.baseline_entries[bundle_rel] = sha256_file(target)
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
        _copy_atomic(source_dir / rel, target)
        report.baseline_entries[rel] = sha256_file(target)
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
        report.baseline_entries[rel] = sha256_file(dst)
        return
    if not dry_run:
        _copy_atomic(src, dst)
        report.baseline_entries[rel] = sha256_file(dst)
    report.applied.append(
        _SyncOutcome(rel=rel, action="refreshed", kind="unchanged-vs-baseline")
    )


def _resolve_modified_file(
    *,
    io: _SetupIO,
    source_dir: Path,
    workspace_bundle: Path,
    rel: str,
    in_baseline: bool,
    report: _SyncReport,
    dry_run: bool,
    yes: bool,
    no: bool,
) -> None:
    """Handle a locally-modified shipped file — prompt or use safe default.

    ``in_baseline`` picks the prompt wording: without a baseline entry
    sync can't tell a local edit from an older shipped version.
    """
    if no or yes:
        # Safe default in non-interactive mode: keep local. Records
        # the skip so the partial-success signal fires.
        report.skipped.append(
            _SyncOutcome(rel=rel, action="kept-local", kind="modified")
        )
        return
    question = (
        f"[sync] {rel}: locally modified vs baseline."
        if in_baseline
        else f"[sync] {rel}: differs from source (no baseline entry — "
        "a local edit or an older shipped version)."
    )
    while True:
        answer = _prompt_choice(
            io.prompter,
            question=question,
            options=["[k]eep local", "[o]verwrite with source", "[d]iff"],
            default="k",
        )
        if answer == "d":
            _print_diff(io, source_dir / rel, workspace_bundle / rel, rel=rel)
            continue
        break
    if answer == "o":
        if not dry_run:
            _copy_atomic(source_dir / rel, workspace_bundle / rel)
            report.baseline_entries[rel] = sha256_file(workspace_bundle / rel)
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
    recorded: str | None,
    report: _SyncReport,
    dry_run: bool,
    yes: bool,
    no: bool,
) -> None:
    """Handle a retired-at-source file — prompt or use flag defaults.

    "Retired" only means "in the workspace, not in source": it may be a
    file kolsch shipped and later removed, or one the user added under
    ``bundles/kolsch/``. ``--yes`` deletes it only when it provably is
    the former — it has a baseline entry (``recorded``) and its sha256
    still matches it (shipped, never edited; the ses_0e54 repair).
    Anything else — no baseline, no entry, or edited — is kept and
    recorded as a skip (bug-146). ``--no`` always keeps. Interactive
    runs prompt per file; ``[d]`` is the explicit consent to delete.
    """
    pristine_shipped = (
        recorded is not None and sha256_file(workspace_bundle / rel) == recorded
    )
    if yes and not pristine_shipped:
        report.skipped.append(
            _SyncOutcome(
                rel=rel,
                action="kept-retired (not shipped by kolsch, or edited)",
                kind="retired-at-source",
            )
        )
        return
    if yes:
        if not dry_run:
            (workspace_bundle / rel).unlink(missing_ok=True)
            report.baseline_entries[rel] = None
        report.applied.append(
            _SyncOutcome(rel=rel, action="deleted", kind="retired-at-source")
        )
        report.deleted.add(rel)
        return
    if no:
        report.skipped.append(
            _SyncOutcome(rel=rel, action="kept-retired", kind="retired-at-source")
        )
        return
    question = (
        f"[sync] {rel}: retired at source, still in workspace."
        if pristine_shipped
        else f"[sync] {rel}: not in source — not a recorded kolsch file, "
        "or edited since shipped."
    )
    answer = _prompt_choice(
        io.prompter,
        question=question,
        options=["[k]eep local", "[d]elete"],
        default="k",
    )
    if answer == "d":
        if not dry_run:
            (workspace_bundle / rel).unlink(missing_ok=True)
            report.baseline_entries[rel] = None
        report.applied.append(
            _SyncOutcome(rel=rel, action="deleted", kind="retired-at-source")
        )
        report.deleted.add(rel)
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


def _copy_atomic(src: Path, dst: Path) -> None:
    """Copy `src` over `dst` via a temp file + rename (never a truncated `dst`)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.sync-tmp")
    try:
        shutil.copy2(src, tmp)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def _rewrite_platform_yaml_locked(root: Path) -> bool:
    """Apply the migrator's `platform.yaml` rewrite under the rules lock.

    The rewrite edits `authorization.config.rules`, so it takes
    [`platform_yaml_lock`][assets.platform_config.platform_yaml_lock]
    like every other rules writer and re-plans from the file as read
    *inside* the lock: a rule a chat "always" wrote since the first
    read survives (bug-146). Returns False when, on that re-read, there
    is nothing left to rewrite.
    """
    from assets.platform_config import platform_yaml_lock  # noqa: PLC0415
    from cli.permissions import _atomic_write_text  # noqa: PLC0415

    with platform_yaml_lock(root):
        body = plan_platform_yaml_rewrite(root)
        if body is None:
            return False
        _atomic_write_text(root / "platform.yaml", body)
    return True


def _dry_run_copied_tool_refs(report: _SyncReport) -> set[str]:
    """Tool refs a dry run would have copied in (`tools/<name>.py`)."""
    return {
        f"kolsch/tools/{PurePosixPath(o.rel).stem}"
        for o in report.applied
        if o.action == "copied" and o.rel.startswith("tools/") and o.rel.endswith(".py")
    }


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

    if report.rule_lines or report.rule_failures:
        write("Default permission rules:\n")
        for line in report.rule_lines:
            write(f"  {line}\n")
        for line in report.rule_failures:
            write(f"  ✗ {line}\n")
        write("\n")

    if report.baseline_refreshed:
        write("✓ baseline refreshed at .kolsch/bundle-baseline.json\n")

    if (
        not report.applied
        and not report.skipped
        and not report.parse_failures
        and not report.rules_offered
        and not report.rule_failures
    ):
        write("✓ no drift — workspace already in sync with source.\n")

    if report.is_partial():
        unresolved = (
            len(report.skipped) + len(report.parse_failures) + len(report.rule_failures)
        )
        line = f"\n{unresolved} item(s) left unresolved — exit 1 (partial)."
        if report.skipped and report.non_interactive:
            line += (
                " Re-run `kolsch setup --sync-bundle` without --yes/--no "
                "to review each ([d]iff)."
            )
        write(line + "\n")
