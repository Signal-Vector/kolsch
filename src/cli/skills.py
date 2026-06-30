"""`kolsch skills {list, show, which, new, validate}` CLI surface.

Read-only catalog introspection plus a couple of write commands.
Ops:

- `list` — every catalogued skill across bundles, with a
  `--bundle` filter and a `--json` toggle.
- `show` — frontmatter + body of one SKILL.md.
- `which` — what a given agent has enabled (resolved against the
  catalog so missing refs surface here, not at engine pre-flight).
- `new` — scaffold a new skill directory + SKILL.md.
- `validate` — schema and content checks for the catalogued
  skills.

Mirrors the subparser + dispatcher shape of `cli.permissions`. The
read ops never write, never start a turn, never touch the event
log. The work each read op does is a read on the asset catalog
plus a formatter.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from _yamlio import dump, load
from assets.refs import build_ref
from assets.skill_metadata import parse_skill_frontmatter
from errors import AssetsNotFound, WorkspaceNotInitialized
from platform_runtime import bootstrap_runtime
from platform_runtime.workspace import workspace_is_initialized

_DEFAULT_BUNDLE = "kolsch"
"""Default `--bundle` for `kolsch skills new`.

Matches the starter bundle `kolsch setup` writes (see
`src/cli/_starter_bundle/kolsch/`). There is no separate "user"
bundle convention; `kolsch` is the bundle guaranteed to exist
after first-run setup, so it's the most predictable default.
Users who want a separate bundle for their own skills create it
and pass `--bundle <name>` explicitly — this command never
creates bundles itself."""

_SKILL_NAME_RE = re.compile(r"[a-z][a-z0-9_-]*")
"""The `name` frontmatter field's character class.

Enforced at `skills new` time so the directory name + the
frontmatter `name` agree (the catalog uses the directory name as
the ref segment regardless of frontmatter, so a mismatch would
silently produce a confusing ref)."""

if TYPE_CHECKING:
    from assets import AssetManagement
    from assets.catalog import AssetEntry


# ---------------------------------------------------------------------------
# Argparse wiring — called from cli.main.build_parser
# ---------------------------------------------------------------------------


def add_skills_subparser(
    sub: argparse._SubParsersAction[Any],
) -> argparse.ArgumentParser:
    """Register the `kolsch skills` subparser tree.

    Returns the parent subparser so the dispatcher can stash it for
    `_print_subcommand_help_and_exit` on the missing-op path
    (matching `permissions` / `sessions`).
    """
    skills: argparse.ArgumentParser = sub.add_parser(
        "skills",
        help="Inspect the catalogued skills and what each agent enables.",
    )
    skills_sub = skills.add_subparsers(dest="skills_command", metavar="<op>")

    list_ = skills_sub.add_parser(
        "list",
        help="Print every catalogued skill across bundles.",
    )
    list_.add_argument(
        "--bundle",
        metavar="<name>",
        default=None,
        help="Show only skills from this bundle.",
    )
    list_.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Machine-readable output.",
    )

    show = skills_sub.add_parser(
        "show",
        help="Print one SKILL.md's frontmatter and body.",
    )
    show.add_argument(
        "ref",
        help="Skill ref (e.g. kolsch/literature-review).",
    )

    which = skills_sub.add_parser(
        "which",
        help="Print the skills an agent has enabled.",
    )
    which.add_argument(
        "agent_ref",
        help="Agent ref (e.g. kolsch/agents/helper).",
    )
    which.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Machine-readable output.",
    )

    validate = skills_sub.add_parser(
        "validate",
        help="Lint catalogued skills against the SKILL.md contract.",
    )
    validate.add_argument(
        "ref",
        nargs="?",
        default=None,
        help=(
            "Skill ref to validate (e.g. kolsch/literature-review). "
            "Omit to validate every catalogued skill."
        ),
    )
    validate.add_argument(
        "--all",
        dest="all_skills",
        action="store_true",
        help="Explicit form of the bare invocation (validate every skill).",
    )
    validate.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Machine-readable per-skill result.",
    )

    install = skills_sub.add_parser(
        "install",
        help="Install a skill from a URL (git repo or archive).",
    )
    install.add_argument(
        "url",
        help=(
            "HTTPS URL pointing at a SKILL.md root. GitHub forms accepted: "
            "repo-root (https://github.com/owner/repo) or sub-path "
            "(https://github.com/owner/repo/tree/<branch>/<subdir>). "
            "Direct .tar.gz / .tgz / .zip archive URLs also accepted."
        ),
    )
    install_bundle = install.add_mutually_exclusive_group()
    install_bundle.add_argument(
        "--bundle",
        metavar="<name>",
        default=None,
        help=(
            "Install into an EXISTING bundle (fails if the bundle does "
            "not exist). Omit to auto-create a single-skill bundle."
        ),
    )
    install_bundle.add_argument(
        "--bundle-name",
        dest="bundle_name",
        metavar="<name>",
        default=None,
        help=(
            "Name for the auto-created bundle. Useful for scripted "
            "installs; without this the name is derived from the URL's "
            "last meaningful segment (with a numeric discriminator on "
            "collision)."
        ),
    )
    install.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help=(
            "Skip the trust prompt. Required for non-TTY callers (CI, scripts, server)."
        ),
    )

    remove = skills_sub.add_parser(
        "remove",
        help="Delete a skill's directory; surface dependent agents first.",
    )
    remove.add_argument(
        "ref",
        help="Skill ref to remove (e.g. kolsch/literature-review).",
    )
    remove.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help=(
            "Skip the confirmation prompt(s). Required for non-TTY "
            "callers (CI, scripts, server)."
        ),
    )

    new = skills_sub.add_parser(
        "new",
        help="Scaffold a new SKILL.md skeleton under a bundle.",
    )
    new.add_argument(
        "name",
        help=(
            "Skill name (lowercase, alphanumeric, '-' and '_' allowed; "
            "first char must be a letter). Becomes the skill's directory "
            "name AND the 'name' frontmatter field."
        ),
    )
    new.add_argument(
        "--bundle",
        metavar="<name>",
        default=_DEFAULT_BUNDLE,
        help=(
            f"Bundle to write into. Defaults to {_DEFAULT_BUNDLE!r} (the "
            "starter bundle 'kolsch setup' creates). The bundle directory "
            "must already exist; this command does NOT create bundles."
        ),
    )
    new.add_argument(
        "--description",
        default=None,
        help=(
            "One-line description landed in the SKILL.md frontmatter. "
            "Drives the model's discoverability — make it trigger-rich. "
            "Defaults to a TODO placeholder."
        ),
    )

    return skills


# ---------------------------------------------------------------------------
# Command handlers — called from cli.main._cmd_skills
# ---------------------------------------------------------------------------


async def cmd_skills(
    args: argparse.Namespace,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Dispatch the skills subcommands and return the process exit code.

    `out` / `err` default to the *current* `sys.stdout` /
    `sys.stderr` at call time (not at module import). pytest's
    capsys replaces those streams per test; binding the default at
    call time keeps captured output flowing to the right buffer.
    """
    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr
    if not workspace_is_initialized(args.root):
        raise WorkspaceNotInitialized(path=str(args.root), env_var_set=False)

    runtime = await bootstrap_runtime(root=args.root)

    op = args.skills_command
    if op == "list":
        return _cmd_list(
            runtime.asset_management,
            bundle_filter=args.bundle,
            json_output=args.json_output,
            out=out,
        )
    if op == "show":
        return _cmd_show(runtime.asset_management, args.ref, out=out, err=err)
    if op == "which":
        return _cmd_which(
            runtime.asset_management,
            args.agent_ref,
            json_output=args.json_output,
            out=out,
            err=err,
        )
    if op == "new":
        return _cmd_new(
            args.root,
            args.name,
            bundle=args.bundle,
            description=args.description,
            out=out,
            err=err,
        )
    if op == "validate":
        return _cmd_validate(
            runtime.asset_management,
            ref=args.ref,
            json_output=args.json_output,
            out=out,
            err=err,
        )
    if op == "install":
        return _cmd_install(
            args.root,
            url=args.url,
            bundle=args.bundle,
            bundle_name=args.bundle_name,
            yes=args.yes,
            out=out,
            err=err,
        )
    if op == "remove":
        return _cmd_remove(
            runtime.asset_management,
            ref=args.ref,
            yes=args.yes,
            out=out,
            err=err,
        )
    err.write(
        f"unknown skills op {op!r}; "
        "expected one of list/show/which/new/validate/install/remove\n",
    )
    return 1


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _cmd_list(
    am: AssetManagement,
    *,
    bundle_filter: str | None,
    json_output: bool,
    out: TextIO,
) -> int:
    """``kolsch skills list`` — catalogued skills, optionally filtered."""
    entries = am.list_assets(asset_type="skill")
    if bundle_filter is not None:
        entries = [e for e in entries if e.bundle == bundle_filter]

    if json_output:
        out.write(json.dumps([_skill_row(e) for e in entries], sort_keys=True))
        out.write("\n")
        return 0

    if not entries:
        # The "nothing to list" case prints a friendly line, exit 0 —
        # no skills is a valid state for a freshly-set-up workspace,
        # not an error worth a non-zero exit.
        if bundle_filter is not None:
            out.write(f"(no skills in bundle {bundle_filter!r})\n")
        else:
            out.write("(no skills catalogued)\n")
        return 0

    # Sort by ref for deterministic output.
    entries = sorted(entries, key=lambda e: e.ref)
    # Compute column widths once.
    ref_w = max(len(e.ref) for e in entries)
    name_w = max(len(_meta_str(e, "name", e.ref.rsplit("/", 1)[-1])) for e in entries)
    out.write(f"{'REF':<{ref_w}}  {'NAME':<{name_w}}  FLAGS  DESCRIPTION\n")
    for entry in entries:
        name = _meta_str(entry, "name", entry.ref.rsplit("/", 1)[-1])
        flags = "[hidden]" if _disable_model_invocation(entry) else "       "
        description = _truncate(_meta_str(entry, "description", ""), 72)
        out.write(f"{entry.ref:<{ref_w}}  {name:<{name_w}}  {flags}  {description}\n")
    return 0


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _cmd_show(
    am: AssetManagement,
    ref: str,
    *,
    out: TextIO,
    err: TextIO,
) -> int:
    """``kolsch skills show <ref>`` — frontmatter + body for one SKILL.md.

    Two error paths:

    - Ref doesn't resolve (catalog miss) → friendly stderr + exit 1.
    - Ref resolves but isn't a skill (e.g. an agent or tool ref)
      → friendly stderr naming the actual asset_type + exit 1.

    The body is read from disk on-demand; the catalog stores only
    frontmatter metadata per the spec's "body NOT parsed at catalog
    build" rule.
    """
    try:
        entry = _get_catalog_entry_sync(am, ref)
    except AssetsNotFound:
        err.write(f"error: skill ref {ref!r} not in catalog.\n")
        err.write(
            "(Run `kolsch skills list` to see what's available.)\n",
        )
        return 1
    if entry.asset_type != "skill":
        err.write(f"error: ref {ref!r} is a {entry.asset_type}, not a skill.\n")
        return 1

    # Frontmatter — explicit field order so the output reads in
    # priority order rather than YAML insertion order.
    out.write(f"ref:         {entry.ref}\n")
    out.write(f"path:        {entry.path}\n")
    out.write(f"name:        {_meta_str(entry, 'name', '(unset)')}\n")
    description = _meta_str(entry, "description", "(unset)")
    out.write(f"description: {description}\n")
    license_value = _meta_str(entry, "license", "")
    if license_value:
        out.write(f"license:     {license_value}\n")
    compat = _meta_str(entry, "compatibility", "")
    if compat:
        out.write(f"compatibility: {compat}\n")
    allowed = entry.metadata.get("allowed-tools")
    if allowed is None:
        allowed = entry.metadata.get("allowed_tools")
    if isinstance(allowed, list) and allowed:
        out.write(f"allowed-tools: {', '.join(str(t) for t in allowed)}\n")
    if _disable_model_invocation(entry):
        out.write(
            "disable-model-invocation: true "
            "(hidden from system-prompt listing; force-invoke only)\n"
        )

    body = _read_skill_body(Path(entry.path))
    out.write("\n--- body ---\n")
    out.write(body)
    if not body.endswith("\n"):
        out.write("\n")
    return 0


# ---------------------------------------------------------------------------
# which
# ---------------------------------------------------------------------------


def _cmd_which(
    am: AssetManagement,
    agent_ref: str,
    *,
    json_output: bool,
    out: TextIO,
    err: TextIO,
) -> int:
    """``kolsch skills which <agent_ref>`` — what an agent has enabled.

    Reads the agent YAML *directly* (not via
    :meth:`PlatformRuntime.build_resolved_config`) so unresolvable
    refs surface here rather than being rejected by Layer 3
    validation. The whole point of the verb is "show me what the
    config asks for AND whether the catalog can serve it" — Layer 3
    validation would short-circuit that with a Fatal ConfigInvalid
    on the first orphan ref, which is the opposite of what the user
    wants from a debugging surface.

    The trade-off: ``extends:`` chains aren't walked. Most agent
    YAMLs declare ``subsystems`` directly; agents that inherit skills
    via ``extends:`` are unusual today and can be inspected via the
    parent YAML. Flagged as a follow-up if real cases hit it.
    """
    try:
        entry = _get_catalog_entry_sync(am, agent_ref)
    except AssetsNotFound:
        err.write(f"error: agent ref {agent_ref!r} not in catalog.\n")
        return 1
    if entry.asset_type != "agent":
        err.write(f"error: ref {agent_ref!r} is a {entry.asset_type}, not an agent.\n")
        return 1

    enabled_refs = _read_enabled_skill_refs_from_agent_yaml(Path(entry.path))
    rows = [_which_row(am, ref) for ref in enabled_refs]
    # Exit code computed up-front so the JSON and table paths agree:
    # 2 when ANY enabled ref doesn't resolve, 0 otherwise. The
    # missing case is the actionable one — the next force-invoke
    # against that ref would fail at runtime, so scripts that
    # pre-flight via `which` should be able to detect it.
    exit_code = 2 if any(not r["available"] for r in rows) else 0

    if json_output:
        out.write(
            json.dumps(
                {"agent": agent_ref, "enabled": rows},
                sort_keys=True,
            )
        )
        out.write("\n")
        return exit_code

    if not rows:
        out.write(f"agent {agent_ref}: no skills enabled.\n")
        return 0

    out.write(f"agent {agent_ref}:\n")
    for row in rows:
        marker = "✓" if row["available"] else "✗"
        name = row["name"] or "(missing from catalog)"
        out.write(f"  {marker} {row['ref']:<40}  {name}\n")
    return exit_code


# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------


_SKILL_BODY_TEMPLATE = """\

# {name}

TODO: replace this body with instructions the model can follow when
this skill is loaded. Describe (1) when the skill applies — what kind
of request triggers it — and (2) the steps the model should take. The
description above is what the model sees in the system prompt to
decide whether to load this skill; make it trigger-rich. See
docs/explanation/skills-subsystem.md for the SKILL.md format.
"""

_PLACEHOLDER_DESCRIPTION = (
    "TODO — one-line description of when to use this skill and what it does."
)
"""Default `description` when `--description` is omitted.

The description is the model's discovery signal — a new skill with
this placeholder is intentionally not yet discoverable, which the
TODO marker makes visible. The em-dash (not a colon) keeps the
value unambiguous to YAML's flow-scalar parser; the body template
+ docs steer the user toward making it trigger-rich before
enabling on an agent."""


def _render_skill_md(name: str, description: str) -> str:
    """Render the SKILL.md text for a new skill.

    Serialises the frontmatter via the ``_yamlio`` helper so any
    characters that would otherwise need quoting (colons, quotes,
    newlines) round-trip cleanly through
    :func:`assets.skill_metadata.parse_skill_frontmatter`. The body
    is plain Markdown — no template variables beyond ``name``.
    """
    frontmatter = {
        "name": name,
        "description": description,
        "allowed-tools": [],
        "disable-model-invocation": False,
    }
    yaml_block = dump(frontmatter)
    body = _SKILL_BODY_TEMPLATE.format(name=name)
    return f"---\n{yaml_block}---\n{body}"


def _cmd_new(
    root: Path,
    name: str,
    *,
    bundle: str,
    description: str | None,
    out: TextIO,
    err: TextIO,
) -> int:
    """``kolsch skills new <name>`` — scaffold a SKILL.md skeleton.

    The catalog is built once at runtime bootstrap (Am-I7 — no hot
    reload); a newly-written SKILL.md is NOT visible to the current
    process. The CLI is one-shot, so the next ``kolsch ...``
    invocation picks up the new skill via a fresh scan. The output
    message reminds the user.
    """
    if _SKILL_NAME_RE.fullmatch(name) is None:
        err.write(
            f"error: skill name {name!r} is invalid. "
            "Names must match [a-z][a-z0-9_-]* — lowercase, "
            "alphanumeric with '-' / '_', first char a letter.\n"
        )
        return 1

    bundle_dir = root / "bundles" / bundle
    if not (bundle_dir / "bundle.yaml").is_file():
        err.write(
            f"error: bundle {bundle!r} not found at {bundle_dir}.\n"
            "(Run `kolsch skills list` to see what bundles exist, or "
            "create the bundle directory + bundle.yaml first.)\n"
        )
        return 1

    skill_dir = bundle_dir / "skills" / name
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        err.write(f"error: {skill_md} already exists. Refusing to overwrite.\n")
        err.write(
            f"(View it with `kolsch skills show {bundle}/skills/{name}` "
            "or edit the file directly.)\n"
        )
        return 1

    skill_dir.mkdir(parents=True, exist_ok=True)
    payload = _render_skill_md(
        name=name,
        description=description if description else _PLACEHOLDER_DESCRIPTION,
    )
    skill_md.write_text(payload, encoding="utf-8")

    ref = f"{bundle}/skills/{name}"
    out.write(f"created {skill_md}\n")
    out.write(f"ref: {ref}\n")
    out.write(
        "(The catalog is built at runtime bootstrap; the new skill "
        "will be visible on the next `kolsch ...` invocation. Enable it "
        f"by adding `{{ref: {ref}}}` to an agent's "
        "subsystems.skills.config.enabled list.)\n"
    )
    return 0


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def _cmd_remove(
    am: AssetManagement,
    *,
    ref: str,
    yes: bool,
    out: TextIO,
    err: TextIO,
) -> int:
    """``kolsch skills remove <ref>`` — delete a skill directory.

    Surfaces dependent agents (those whose
    ``subsystems.skills.config.enabled`` lists this ref) BEFORE the
    delete so the user knows what breaks. Does NOT auto-edit agent
    YAMLs — mirrors ``kolsch permissions remove`` (the user is in
    charge of cleanup; silent rewrites of user-authored config
    would be the worse failure mode).

    Two prompts in the worst case: one for the skill delete, one
    extra if removing the skill empties its bundle of all assets.
    ``--yes`` bypasses both.

    The dependent-agent walk reads each agent YAML once (the
    catalog stores only paths for agents, not parsed metadata, so
    this is a re-parse, not a cache miss — see Step 8 escalation
    note). Cost is O(N) on agent count with a single ``_yamlio.load``
    per agent; the cost lives at the right tier for a one-shot CLI
    verb (~ms for a workspace's typical handful of agents).
    """
    try:
        entry = _get_catalog_entry_sync(am, ref)
    except AssetsNotFound:
        err.write(f"error: skill ref {ref!r} not in catalog.\n")
        return 1
    if entry.asset_type != "skill":
        err.write(f"error: ref {ref!r} is a {entry.asset_type}, not a skill.\n")
        return 1

    skill_md_path = Path(entry.path)
    skill_dir = skill_md_path.parent
    if not skill_dir.is_dir():
        err.write(
            f"error: skill directory {skill_dir} does not exist (catalog stale?).\n"
        )
        return 1

    affected = _find_agents_enabling_ref(am, ref)

    out.write(f"\nWill delete: {skill_dir}\n")
    if affected:
        out.write("\nAffected agents (these enable the skill in their config):\n")
        for agent_ref in affected:
            out.write(f"  {agent_ref}\n")
        out.write(
            "\nThese agents will fail to load on next exchange until you "
            "remove or re-target the enabled entry. (`kolsch skills remove` "
            "intentionally does NOT auto-edit agent YAMLs — that's your call.)\n"
        )
    else:
        out.write("\nNo agents in the catalog enable this skill.\n")

    if not _confirm_yes_no(
        out=out,
        err=err,
        question="Delete the skill?",
        yes=yes,
    ):
        out.write("remove cancelled.\n")
        return 1

    import shutil  # noqa: PLC0415

    try:
        shutil.rmtree(skill_dir)
    except OSError as exc:
        err.write(f"error: delete failed: {exc}\n")
        return 1
    out.write(f"deleted {skill_dir}\n")

    # Bundle-cleanup branch: if the skill's removal leaves the
    # bundle empty of every asset type, offer to remove the bundle
    # too. Auto-created single-skill bundles (kolsch skills install
    # without --bundle) are the load-bearing case for this — they
    # have nothing else worth keeping once their one skill is gone.
    bundle_dir = skill_dir.parent.parent  # skills/<name>/ → bundle dir
    if _bundle_has_no_remaining_assets(bundle_dir):
        out.write(
            f"\nBundle {bundle_dir.name!r} is now empty of skills, agents, "
            "tools, and prompts.\n"
        )
        if _confirm_yes_no(
            out=out,
            err=err,
            question="Delete the empty bundle too?",
            yes=yes,
        ):
            try:
                shutil.rmtree(bundle_dir)
            except OSError as exc:
                err.write(f"error: bundle delete failed: {exc}\n")
                return 1
            out.write(f"deleted bundle {bundle_dir}\n")
        else:
            out.write("kept empty bundle.\n")
    return 0


def _find_agents_enabling_ref(am: AssetManagement, skill_ref: str) -> list[str]:
    """Return the refs of agents whose YAML enables ``skill_ref``.

    Re-parses each agent YAML in catalog order — agents'
    ``AssetEntry.metadata`` is empty by construction
    (``catalog._scan_agents`` doesn't parse YAML at scan time, per
    Am-I10 "body NOT parsed at catalog build"). The per-call cost
    is O(N agents) with a YAML parse each; acceptable for the
    one-shot CLI verb and avoids the larger refactor of caching
    parsed agent metadata in the catalog.
    """
    out: list[str] = []
    for agent_entry in am.list_assets(asset_type="agent"):
        enabled = _read_enabled_skill_refs_from_agent_yaml(Path(agent_entry.path))
        if skill_ref in enabled:
            out.append(agent_entry.ref)
    return sorted(out)


def _bundle_has_no_remaining_assets(bundle_dir: Path) -> bool:
    """True iff every asset subdir under ``bundle_dir`` is empty
    (or absent). Excludes ``bundle.yaml`` itself — that's the
    manifest, not an asset.
    """
    for sub in ("skills", "agents", "tools", "prompts"):
        candidate = bundle_dir / sub
        if not candidate.exists():
            continue
        for child in candidate.iterdir():
            if not child.name.startswith("."):
                return False
    return True


def _confirm_yes_no(
    *,
    out: TextIO,
    err: TextIO,
    question: str,
    yes: bool,
    default: bool = False,
) -> bool:
    """Shared y/n prompt — same TTY/--yes/EOF handling as install.

    ``--yes`` bypasses entirely. Non-TTY callers without ``--yes``
    get a refusal naming the flag as the remedy (silent stdin
    would otherwise hang or, worse, take a stale buffered byte as
    consent).
    """
    if yes:
        return True
    if not sys.stdin.isatty():
        err.write(
            f"\nerror: cannot prompt without a TTY. Re-run with --yes "
            f"to confirm: {question}\n",
        )
        return False
    suffix = " [Y/n]" if default else " [y/N]"
    out.write(f"{question}{suffix} ")
    out.flush()
    try:
        raw = input().strip().lower()
    except EOFError:
        out.write("\n")
        return False
    if not raw:
        return default
    return raw.startswith("y")


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


_SAFE_NAME_RE = re.compile(r"[^a-z0-9_-]+")
_GITHUB_TREE_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?(?:/tree/(?P<branch>[^/]+)"
    r"(?:/(?P<subpath>.+))?)?$"
)
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".zip")


@dataclass(frozen=True)
class _ParsedInstallUrl:
    """The structured view of an install URL.

    ``kind="git"``: fetch via ``git clone`` then optionally ``cd``
    into ``subpath``. ``kind="archive"``: download the file and
    extract it; ``subpath`` is honoured after extraction.

    ``derived_name`` is the URL's "last meaningful segment" — the
    default for both the skill name and the auto-created bundle
    name (sanitised to ``[a-z0-9_-]``).
    """

    kind: str  # "git" | "archive"
    fetch_url: str
    branch: str | None
    subpath: str | None
    derived_name: str


# Type alias for the fetcher seam — tests inject a fake that copies
# from a fixture dir, production uses _real_fetcher.
_Fetcher = Callable[[_ParsedInstallUrl, Path], Path]


def _parse_install_url(url: str) -> _ParsedInstallUrl:
    """Parse the URL into ``_ParsedInstallUrl``.

    GitHub forms recognised by host match; non-GitHub HTTPS hosts
    are accepted as bare git URLs (no sub-path translation). Archive
    URLs are detected by extension. Any URL the parser can't
    confidently classify raises ``ValueError`` with an actionable
    message; the caller surfaces this to the user before any fetch.
    """
    from urllib.parse import urlparse  # noqa: PLC0415

    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        msg = (
            f"unsupported URL scheme {parsed.scheme!r}; only https:// "
            "(or http:// for local test mirrors) is accepted."
        )
        raise ValueError(msg)
    path = parsed.path or "/"

    # Archive shortcut — any host, just check the extension.
    lower = path.lower()
    if any(lower.endswith(suffix) for suffix in _ARCHIVE_SUFFIXES):
        return _ParsedInstallUrl(
            kind="archive",
            fetch_url=url,
            branch=None,
            subpath=None,
            derived_name=_derive_name_from_archive(path),
        )

    # GitHub sub-path form.
    if parsed.netloc.lower() in ("github.com", "www.github.com"):
        match = _GITHUB_TREE_RE.match(path)
        if match is None:
            msg = (
                f"unrecognised GitHub URL shape {url!r}; expected "
                "https://github.com/owner/repo or "
                "https://github.com/owner/repo/tree/<branch>/<subpath>."
            )
            raise ValueError(msg)
        owner = match.group("owner")
        repo = match.group("repo")
        branch = match.group("branch")
        subpath = match.group("subpath")
        derived = subpath.rsplit("/", 1)[-1] if subpath else repo
        return _ParsedInstallUrl(
            kind="git",
            fetch_url=f"https://github.com/{owner}/{repo}.git",
            branch=branch,
            subpath=subpath,
            derived_name=_sanitise_name(derived),
        )

    # Any other HTTPS host: treat as a bare git URL. The user is
    # responsible for ensuring the URL is a clonable git repo with
    # SKILL.md at the root (no sub-path translation for non-GitHub).
    derived = path.rstrip("/").rsplit("/", 1)[-1]
    if derived.endswith(".git"):
        derived = derived[:-4]
    if not derived:
        msg = f"could not derive a skill name from {url!r}"
        raise ValueError(msg)
    return _ParsedInstallUrl(
        kind="git",
        fetch_url=url,
        branch=None,
        subpath=None,
        derived_name=_sanitise_name(derived),
    )


def _derive_name_from_archive(path: str) -> str:
    """Strip the archive extension + path prefix → sanitised name."""
    last = path.rstrip("/").rsplit("/", 1)[-1]
    for suffix in _ARCHIVE_SUFFIXES:
        if last.lower().endswith(suffix):
            last = last[: -len(suffix)]
            break
    return _sanitise_name(last)


def _sanitise_name(raw: str) -> str:
    """Lowercase + replace non-``[a-z0-9_-]`` with ``-``; trim dashes."""
    cleaned = _SAFE_NAME_RE.sub("-", raw.lower()).strip("-")
    return cleaned or "skill"


def _real_fetcher(parsed: _ParsedInstallUrl, scratch_dir: Path) -> Path:
    """Fetch + extract → return the path to the skill root.

    Git: ``git clone --depth 1`` (with branch when supplied); resolve
    the optional ``subpath`` after clone. Archive: download via
    urllib then extract via tarfile/zipfile; if the archive has a
    single top-level directory, descend into it; honour ``subpath``
    on top of that.

    Subprocess + stdlib only — keeps the install path free of
    additional runtime deps. Network errors and missing-git raise
    ``RuntimeError`` with a friendly message the caller forwards
    to stderr.
    """
    if parsed.kind == "git":
        return _git_fetch(parsed, scratch_dir)
    if parsed.kind == "archive":
        return _archive_fetch(parsed, scratch_dir)
    msg = f"internal: unsupported fetch kind {parsed.kind!r}"
    raise RuntimeError(msg)


def _git_fetch(parsed: _ParsedInstallUrl, scratch_dir: Path) -> Path:
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    if shutil.which("git") is None:
        msg = (
            "git executable not found on PATH; install git or use an "
            "archive URL (.tar.gz / .zip) instead."
        )
        raise RuntimeError(msg)
    checkout_dir = scratch_dir / "checkout"
    cmd = ["git", "clone", "--depth", "1"]
    if parsed.branch:
        cmd.extend(["--branch", parsed.branch])
    cmd.extend([parsed.fetch_url, str(checkout_dir)])
    try:
        subprocess.run(  # noqa: S603 — args list, not shell
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        msg = (
            f"git clone failed (exit {exc.returncode}): "
            f"{(exc.stderr or '').strip() or 'no stderr output'}"
        )
        raise RuntimeError(msg) from exc
    skill_root = checkout_dir
    if parsed.subpath:
        skill_root = checkout_dir / parsed.subpath
    return skill_root


def _archive_fetch(parsed: _ParsedInstallUrl, scratch_dir: Path) -> Path:
    import tarfile  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415
    import zipfile  # noqa: PLC0415

    archive_path = scratch_dir / "archive"
    try:
        urllib.request.urlretrieve(parsed.fetch_url, archive_path)  # noqa: S310 — https validated by caller
    except urllib.error.URLError as exc:
        msg = f"archive download failed: {exc}"
        raise RuntimeError(msg) from exc

    extracted = scratch_dir / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)
    if parsed.fetch_url.lower().endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(extracted)  # noqa: S202 — tmp dir, content validated downstream
    else:
        with tarfile.open(archive_path) as tf:
            tf.extractall(extracted, filter="data")  # noqa: S202 — same

    # Many archives wrap content in a single top-level dir (e.g.
    # github's `repo-<sha>/`). When that's the case, descend into
    # it so the skill root is at extracted/<solo>/(subpath?).
    children = [c for c in extracted.iterdir() if not c.name.startswith(".")]
    skill_root = extracted
    if len(children) == 1 and children[0].is_dir():
        skill_root = children[0]
    if parsed.subpath:
        skill_root = skill_root / parsed.subpath
    return skill_root


def _resolve_install_destination(
    root: Path,
    parsed: _ParsedInstallUrl,
    *,
    bundle: str | None,
    bundle_name: str | None,
) -> tuple[bool, Path, str, str]:
    """Resolve the install destination from the user's flags.

    Returns ``(is_new_bundle, bundle_dir, bundle_name, skill_name)``.

    - ``--bundle <name>`` → ``is_new_bundle=False``; the bundle's
      existing dir is the target. Raises ``ValueError`` if missing.
    - ``--bundle-name <name>`` (or no flag) → ``is_new_bundle=True``;
      the bundle dir does NOT exist yet (the caller creates it
      atomically). When the desired name is taken, a numeric
      discriminator is appended.

    Skill name is always ``parsed.derived_name`` (URL-derived,
    sanitised).
    """
    bundles_root = root / "bundles"
    skill_name = parsed.derived_name

    if bundle is not None:
        bundle_dir = bundles_root / bundle
        if not (bundle_dir / "bundle.yaml").is_file():
            msg = (
                f"bundle {bundle!r} not found at {bundle_dir}. "
                "(Use `kolsch skills list` to see existing bundles, or "
                "omit --bundle to auto-create one.)"
            )
            raise ValueError(msg)
        return False, bundle_dir, bundle, skill_name

    # Auto-create path. Use the explicit --bundle-name if given;
    # otherwise the URL-derived name.
    base = _sanitise_name(bundle_name) if bundle_name else parsed.derived_name
    candidate = base
    suffix = 2
    bundles_root.mkdir(parents=True, exist_ok=True)
    while (bundles_root / candidate).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return True, bundles_root / candidate, candidate, skill_name


def _confirm_install(
    *,
    skill_root: Path,
    parsed_url: _ParsedInstallUrl,
    bundle_name: str,
    is_new_bundle: bool,
    skill_name: str,
    yes: bool,
    out: TextIO,
    err: TextIO,
) -> bool:
    """Show the trust prompt + return True iff the user accepts.

    ``--yes`` bypasses the prompt (but the file list still prints so
    the audit trail is honest). Non-TTY callers without ``--yes`` get
    a clear refusal — no silent hang on stdin.
    """
    out.write(f"\nInstall from: {parsed_url.fetch_url}\n")
    if parsed_url.branch:
        out.write(f"  branch: {parsed_url.branch}\n")
    if parsed_url.subpath:
        out.write(f"  subpath: {parsed_url.subpath}\n")
    destination = f"  → {bundle_name}/skills/{skill_name}" + (
        "  (new bundle)" if is_new_bundle else ""
    )
    out.write(destination + "\n")

    files = sorted(_list_skill_files(skill_root))
    out.write(f"\nFiles ({len(files)}):\n")
    for rel in files:
        out.write(f"  {rel}\n")

    skill_md = skill_root / "SKILL.md"
    metadata = parse_skill_frontmatter(skill_md)
    allowed = metadata.get("allowed-tools") or metadata.get("allowed_tools")
    if isinstance(allowed, list) and allowed:
        out.write(f"\nDeclared allowed-tools: {', '.join(str(t) for t in allowed)}\n")
    else:
        out.write("\nDeclared allowed-tools: (none / informational)\n")

    if yes:
        out.write("\nProceeding (--yes).\n")
        return True

    if not sys.stdin.isatty():
        err.write(
            "\nerror: cannot prompt without a TTY. Re-run with --yes "
            "to install without confirmation.\n"
        )
        return False

    out.write("\nInstall? [y/N]: ")
    out.flush()
    try:
        raw = input().strip().lower()
    except EOFError:
        out.write("\n")
        return False
    return raw.startswith("y")


def _list_skill_files(skill_root: Path) -> list[str]:
    """Walk ``skill_root`` and return relative POSIX-style paths.

    Includes ``SKILL.md`` and everything else (scripts/, references/,
    assets/) so the trust prompt's file count matches what's about
    to land on disk. ``.git`` and similar VCS metadata are excluded
    because git-cloned skills carry the parent repo's hidden tree
    that we don't want to copy through anyway.
    """
    out: list[str] = []
    for path in skill_root.rglob("*"):
        if any(part.startswith(".") for part in path.relative_to(skill_root).parts):
            continue
        if path.is_file():
            out.append(str(path.relative_to(skill_root)))
    return out


def _cmd_install(  # noqa: PLR0913 — install owns the full URL → destination flow; bundling further hurts readability
    root: Path,
    *,
    url: str,
    bundle: str | None,
    bundle_name: str | None,
    yes: bool,
    out: TextIO,
    err: TextIO,
    fetcher: _Fetcher | None = None,
) -> int:
    """``kolsch skills install <url>`` — install a skill from a URL.

    ``fetcher`` is the test-injection seam: tests pass a fake that
    copies from a local fixture dir; production passes ``None`` and
    gets :func:`_real_fetcher` (git clone or archive download). The
    rest of the flow is identical either way — strict-root SKILL.md
    validation, trust prompt, atomic move into place.
    """
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    try:
        parsed = _parse_install_url(url)
    except ValueError as exc:
        err.write(f"error: {exc}\n")
        return 1

    # Resolve destination BEFORE fetch — fast-fail for a typo'd
    # --bundle saves the user a clone they'll throw away.
    try:
        is_new_bundle, bundle_dir, bundle_final, skill_name = (
            _resolve_install_destination(
                root,
                parsed,
                bundle=bundle,
                bundle_name=bundle_name,
            )
        )
    except ValueError as exc:
        err.write(f"error: {exc}\n")
        return 1

    # Refuse if the skill dir already exists under an EXISTING bundle.
    # (For auto-create, _resolve_install_destination already discriminates
    # against the bundle name itself.)
    target_skill_dir = bundle_dir / "skills" / skill_name
    if not is_new_bundle and target_skill_dir.exists():
        err.write(
            f"error: {target_skill_dir} already exists. Refusing to overwrite.\n",
        )
        return 1

    active_fetcher = fetcher if fetcher is not None else _real_fetcher

    # All write-side operations live INSIDE this tmp-dir block so
    # any failure during validation rolls back cleanly. Atomic move
    # into place is the LAST step.
    with tempfile.TemporaryDirectory(prefix="kolsch-skill-install-") as scratch:
        scratch_dir = Path(scratch)
        try:
            skill_root = active_fetcher(parsed, scratch_dir)
        except (RuntimeError, OSError) as exc:
            err.write(f"error: fetch failed: {exc}\n")
            return 1

        # Strict-root contract — SKILL.md MUST be at the resolved
        # directory's root, not nested. Multi-skill repos require
        # an explicit subpath in the URL (decision locked, Phase
        # 2.4 Part 0).
        if not skill_root.is_dir():
            err.write(f"error: fetched path {skill_root} is not a directory.\n")
            return 1
        skill_md = skill_root / "SKILL.md"
        if not skill_md.is_file():
            err.write(
                "error: this URL does not point at a SKILL.md root. "
                "Multi-skill repos require an explicit sub-path "
                "(e.g. github.com/org/repo/tree/main/skills/<name>).\n"
            )
            return 1
        metadata = parse_skill_frontmatter(skill_md)
        if not metadata or not isinstance(metadata.get("name"), str):
            err.write(
                "error: SKILL.md frontmatter is missing or malformed "
                "(at minimum, `name` must be a string).\n"
            )
            return 1

        if not _confirm_install(
            skill_root=skill_root,
            parsed_url=parsed,
            bundle_name=bundle_final,
            is_new_bundle=is_new_bundle,
            skill_name=skill_name,
            yes=yes,
            out=out,
            err=err,
        ):
            out.write("install cancelled.\n")
            return 1

        # Atomic move. For auto-create, stage the whole new bundle
        # inside the scratch dir first so a partial failure can't
        # leave the workspace's bundles/ tree half-built.
        try:
            if is_new_bundle:
                _install_into_new_bundle(
                    skill_root=skill_root,
                    final_bundle_dir=bundle_dir,
                    final_bundle_name=bundle_final,
                    skill_name=skill_name,
                    scratch=scratch_dir,
                )
            else:
                target_skill_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(skill_root, target_skill_dir)
        except OSError as exc:
            err.write(f"error: install copy failed: {exc}\n")
            return 1

    ref = f"{bundle_final}/skills/{skill_name}"
    out.write(f"\ninstalled {ref}\n")
    out.write(
        f"(Run `kolsch skills show {ref}` to inspect, or "
        f"`kolsch skills which <agent>` to see which agents enable it.)\n"
    )

    # Post-install validation. Re-bootstrap so the fresh catalog
    # entry is visible. A failure here is informational — the
    # files are already on disk; the user can fix and re-run
    # validate.
    import asyncio  # noqa: PLC0415

    from assets import AssetManagement  # noqa: PLC0415

    async def _validate_installed() -> int:
        am = AssetManagement(root=root)
        await am.initialize()
        return _cmd_validate(
            am,
            ref=ref,
            json_output=False,
            out=out,
            err=err,
        )

    out.write("\n")
    asyncio.run(_validate_installed())
    return 0


def _install_into_new_bundle(
    *,
    skill_root: Path,
    final_bundle_dir: Path,
    final_bundle_name: str,
    skill_name: str,
    scratch: Path,
) -> None:
    """Stage a fresh single-skill bundle under ``scratch`` then move
    it into place with one rename so a half-built tree never appears
    under ``bundles/``.
    """
    import shutil  # noqa: PLC0415

    staged_bundle = scratch / "staged_bundle"
    staged_bundle.mkdir(parents=True, exist_ok=True)
    bundle_yaml = staged_bundle / "bundle.yaml"
    bundle_yaml.write_text(
        dump(
            {
                "name": final_bundle_name,
                "version": "0.1.0",
                "description": (
                    f"Auto-created by `kolsch skills install` to hold "
                    f"the {skill_name} skill."
                ),
            },
        ),
        encoding="utf-8",
    )
    skills_dir = staged_bundle / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(skill_root, skills_dir / skill_name)

    # One rename — atomic on POSIX when source + dest live on the
    # same filesystem (the workspace tmp + bundles/ are siblings
    # under the workspace root, so this holds for typical usage).
    final_bundle_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged_bundle), str(final_bundle_dir))


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


# Error codes used by both the CLI op and the setup-time hook
# (cli._setup._maybe_warn_invalid_skills). Stable strings so JSON
# consumers + log scrapers can match on them.
_ERR_FILE_MISSING = "file-missing"
_ERR_FRONTMATTER_UNPARSEABLE = "frontmatter-unparseable"
_ERR_MISSING_NAME = "missing-name"
_ERR_MISSING_DESCRIPTION = "missing-description"
_ERR_ALLOWED_TOOLS_NOT_LIST = "allowed-tools-not-list"
_ERR_DISABLE_FLAG_NOT_BOOL = "disable-model-invocation-not-bool"
_ERR_LICENSE_NOT_STRING = "license-not-string"
_ERR_COMPATIBILITY_NOT_STRING = "compatibility-not-string"
_ERR_BODY_EMPTY = "body-empty"
_ERR_MISSING_REF_PREFIX = "missing-ref:"

_BACKTICKED_PATH_RE = re.compile(r"`([^`\s]+/[^`\s]+)`")
"""Match backticked tokens containing a slash — heuristic for paths.

Catches ``\\`scripts/extract.py\\``` and ``\\`./references/spec.md\\``` in
skill body markdown. Bare ``\\`run_thing\\``` (no slash) doesn't match
— a single word might be a function name, a variable, or a path
without a directory. The slash requirement keeps false positives
low; the cost is missing references to top-level files in the
skill dir, which the body convention already discourages."""


def validate_skill_entry(entry: AssetEntry) -> list[str]:
    """Return a list of error codes (empty when the skill is well-formed).

    Pure function — no I/O beyond the SKILL.md read. Used by both
    ``kolsch skills validate`` (CLI op) and the setup-time warning
    hook in :mod:`cli._setup`. Sharing the primitive keeps the
    two surfaces from drifting in what they consider valid.
    """
    errors: list[str] = []
    path = Path(entry.path)
    if not path.is_file():
        # No file → can't validate anything else; short-circuit so
        # downstream checks don't crash trying to read a missing
        # file. The catalog held a path that no longer exists.
        return [_ERR_FILE_MISSING]

    metadata = entry.metadata
    if not isinstance(metadata, dict) or not metadata:
        errors.append(_ERR_FRONTMATTER_UNPARSEABLE)
        # Continue — we still check the body even if frontmatter is
        # gone, so the user sees every issue at once.

    name = metadata.get("name") if isinstance(metadata, dict) else None
    if not isinstance(name, str) or not name:
        errors.append(_ERR_MISSING_NAME)
    description = metadata.get("description") if isinstance(metadata, dict) else None
    if not isinstance(description, str) or not description:
        errors.append(_ERR_MISSING_DESCRIPTION)

    if isinstance(metadata, dict):
        allowed = metadata.get("allowed-tools")
        if allowed is None:
            allowed = metadata.get("allowed_tools")
        if allowed is not None and not isinstance(allowed, list):
            errors.append(_ERR_ALLOWED_TOOLS_NOT_LIST)

        dmi = metadata.get("disable_model_invocation")
        if dmi is not None and not isinstance(dmi, bool):
            errors.append(_ERR_DISABLE_FLAG_NOT_BOOL)

        license_value = metadata.get("license")
        if license_value is not None and not isinstance(license_value, str):
            errors.append(_ERR_LICENSE_NOT_STRING)
        compat = metadata.get("compatibility")
        if compat is not None and not isinstance(compat, str):
            errors.append(_ERR_COMPATIBILITY_NOT_STRING)

    body = _read_skill_body(path)
    if not body.strip():
        errors.append(_ERR_BODY_EMPTY)

    skill_dir = path.parent
    for rel in _extract_skill_relative_refs(body):
        candidate = (skill_dir / rel).resolve()
        # Confine the check to paths under the skill's own dir so
        # backticked external refs (URLs without scheme, fragments
        # of code like ``utils/foo()``) don't fire false positives.
        try:
            candidate.relative_to(skill_dir.resolve())
        except ValueError:
            continue
        if not candidate.exists():
            errors.append(f"{_ERR_MISSING_REF_PREFIX}{rel}")
    return errors


def _extract_skill_relative_refs(body: str) -> list[str]:
    """Pull skill-dir-relative path candidates out of a body string.

    Heuristic per Step 6: anything backticked that contains a slash
    and doesn't look like an absolute path or URL is treated as a
    relative reference. The caller scopes the check to paths that
    resolve under the skill's directory.
    """
    refs: list[str] = []
    for match in _BACKTICKED_PATH_RE.findall(body):
        if match.startswith(("/", "http://", "https://")):
            continue
        # Strip a leading "./" so the path normalises cleanly.
        if match.startswith("./"):
            match = match[2:]
        refs.append(match)
    return refs


_ERROR_DESCRIPTIONS: dict[str, str] = {
    _ERR_FILE_MISSING: "SKILL.md not found at catalogued path",
    _ERR_FRONTMATTER_UNPARSEABLE: "frontmatter is empty or unparseable",
    _ERR_MISSING_NAME: "frontmatter missing required `name` field",
    _ERR_MISSING_DESCRIPTION: "frontmatter missing required `description` field",
    _ERR_ALLOWED_TOOLS_NOT_LIST: "`allowed-tools` must be a list",
    _ERR_DISABLE_FLAG_NOT_BOOL: "`disable-model-invocation` must be a bool",
    _ERR_LICENSE_NOT_STRING: "`license` must be a string",
    _ERR_COMPATIBILITY_NOT_STRING: "`compatibility` must be a string",
    _ERR_BODY_EMPTY: "body is empty (only frontmatter present)",
}


def _humanise_error(code: str) -> str:
    """Map an error code to a human-readable reason for the table view."""
    if code.startswith(_ERR_MISSING_REF_PREFIX):
        ref = code[len(_ERR_MISSING_REF_PREFIX) :]
        return f"backticked ref `{ref}` not found under skill dir"
    return _ERROR_DESCRIPTIONS.get(code, code)


def _cmd_validate(
    am: AssetManagement,
    *,
    ref: str | None,
    json_output: bool,
    out: TextIO,
    err: TextIO,
) -> int:
    """``kolsch skills validate [<ref>]`` — lint catalogued skills.

    Exit 0 if all checked skills pass; exit 1 if any fail. The
    ``--all`` flag is sugar for the bare invocation (no ref); they
    behave the same. A specific ``ref`` that isn't in the catalog
    fails with a friendly stderr (different from a validation
    failure — the catalog miss is a usage error).
    """
    if ref is not None:
        try:
            entry = _get_catalog_entry_sync(am, ref)
        except AssetsNotFound:
            err.write(f"error: skill ref {ref!r} not in catalog.\n")
            return 1
        if entry.asset_type != "skill":
            err.write(f"error: ref {ref!r} is a {entry.asset_type}, not a skill.\n")
            return 1
        entries = [entry]
    else:
        entries = sorted(
            am.list_assets(asset_type="skill"),
            key=lambda e: e.ref,
        )

    results: list[dict[str, Any]] = [
        {"ref": e.ref, "ok": not (errs := validate_skill_entry(e)), "errors": errs}
        for e in entries
    ]

    if json_output:
        out.write(json.dumps(results, sort_keys=True))
        out.write("\n")
        return 1 if any(not r["ok"] for r in results) else 0

    if not results:
        out.write("(no skills to validate)\n")
        return 0

    for row in results:
        if row["ok"]:
            out.write(f"[OK]   {row['ref']}\n")
        else:
            humanised = ", ".join(_humanise_error(c) for c in row["errors"])
            out.write(f"[FAIL] {row['ref']}: {humanised}\n")

    return 1 if any(not r["ok"] for r in results) else 0


def _read_enabled_skill_refs_from_agent_yaml(path: Path) -> list[str]:
    """Pull ``subsystems.skills.config.enabled[*].ref`` out of an agent YAML.

    Direct parse — does not walk ``extends:`` chains. Used by
    ``which`` so orphan refs (refs in the YAML that the catalog
    can't resolve) surface as ✗ rows instead of being short-circuited
    by Layer 3 validation. Malformed entries are silently skipped;
    the rest of the CLI is the right place to surface ill-formed
    YAML (``kolsch run`` against this agent fails loudly via the
    validator).
    """
    from _yamlio import YAMLError  # noqa: PLC0415

    try:
        payload = load(path.read_text(encoding="utf-8")) or {}
    except (YAMLError, OSError):
        return []
    if not isinstance(payload, dict):
        return []
    subsystems = payload.get("subsystems") or {}
    if not isinstance(subsystems, dict):
        return []
    skills_sub = subsystems.get("skills") or {}
    if not isinstance(skills_sub, dict):
        return []
    body = skills_sub.get("config") or {}
    if not isinstance(body, dict):
        return []
    raw = body.get("enabled") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            ref = entry.get("ref")
            if isinstance(ref, str) and ref:
                out.append(ref)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_catalog_entry_sync(am: AssetManagement, ref: str) -> AssetEntry:
    """Sync get_asset — the catalog is in-memory after initialize().

    `AssetManagement.get_asset` is async only because the API
    leaves room for network-backed catalogs. Today it does no I/O;
    the CLI is sync at this point so a direct catalog lookup keeps
    the call site simple.

    Accepts any of the three catalog-ref input forms:

    - 3-segment `<bundle>/<asset_type>/<name>` (legacy passthrough)
    - 2-segment `<bundle>/<name>` (canonical) — looks across all
      asset types since the sync helper has no field context
    - 1-segment `<name>` shorthand — same fallback scan

    Raises [`AssetsNotFound`][errors.AssetsNotFound] on a miss.
    """
    # `_catalog` access is intentional — the public list/get APIs
    # would work equally well; this avoids the async hop. All
    # catalog reads below route through `AssetCatalog.lookup` so
    # this resolver stays disciplined alongside the rest of the
    # codebase; only the 1-seg fallback iterates entries (no ref
    # to look up — it's a whole-catalog scan by trailing name).
    catalog = am._catalog  # noqa: SLF001 — read-only catalog access
    if catalog is None:
        msg = "AssetManagement not initialized"
        raise RuntimeError(msg)
    # Direct lookup — handles legacy 3-segment refs that match a
    # catalog key verbatim.
    entry = catalog.lookup(ref)
    if entry is not None:
        return entry
    # Fallback for 2-segment <bundle>/<name> or 1-segment <name>.
    # Scan all asset types; the CLI surface knows which command
    # routed here so type-mismatch is caught at the caller's check
    # against ``entry.asset_type`` (e.g., `which` rejects
    # non-skill matches via the existing check at _which_row).
    parts = ref.split("/")
    if len(parts) == 2:
        bundle, name = parts
        for asset_type in ("tool", "skill", "agent", "prompt"):
            candidate = catalog.lookup(build_ref(bundle, asset_type, name))
            if candidate is not None:
                return candidate
    elif len(parts) == 1:
        name = parts[0]
        matches = [
            e for e in catalog.assets.values() if e.ref.rsplit("/", 1)[-1] == name
        ]
        if len(matches) == 1:
            return matches[0]
    raise AssetsNotFound(ref=ref)


def _which_row(am: AssetManagement, ref: str) -> dict[str, Any]:
    """One row for the ``which`` table.

    Resolves ``ref`` against the catalog; ``available`` is False
    when the ref is in the agent's config but not in the catalog,
    or when it resolves to a non-skill asset (which would also
    fail at force-invoke).
    """
    try:
        entry = _get_catalog_entry_sync(am, ref)
    except AssetsNotFound:
        return {"ref": ref, "name": None, "available": False}
    if entry.asset_type != "skill":
        return {"ref": ref, "name": None, "available": False}
    return {
        "ref": ref,
        "name": _meta_str(entry, "name", ref.rsplit("/", 1)[-1]),
        "available": True,
    }


def _skill_row(entry: AssetEntry) -> dict[str, Any]:
    """JSON-shape for one ``list`` row."""
    return {
        "ref": entry.ref,
        "bundle": entry.bundle,
        "name": _meta_str(entry, "name", entry.ref.rsplit("/", 1)[-1]),
        "description": _meta_str(entry, "description", ""),
        "disable_model_invocation": _disable_model_invocation(entry),
    }


def _meta_str(entry: AssetEntry, key: str, default: str) -> str:
    value = entry.metadata.get(key)
    return value if isinstance(value, str) else default


def _disable_model_invocation(entry: AssetEntry) -> bool:
    return bool(entry.metadata.get("disable_model_invocation", False))


def _truncate(text: str, limit: int) -> str:
    """Single-line truncation with ellipsis for table display."""
    flattened = " ".join(text.split())
    if len(flattened) <= limit:
        return flattened
    return flattened[: limit - 1] + "…"


def _read_skill_body(skill_md: Path) -> str:
    """Return everything after the YAML frontmatter block.

    The SKILL.md format is ``---\\nYAML\\n---\\nbody``. When the
    delimiter pair is missing or malformed (rare; the catalog
    accepted the file so the path exists), return the whole file
    so the user still sees the content.
    """
    text = skill_md.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            # Body starts on the line AFTER the closing delimiter.
            body_lines = lines[idx + 1 :]
            # Drop one leading blank line per the convention in the
            # spec's worked example — keeps output tight without
            # losing structural blank lines deeper in the body.
            if body_lines and body_lines[0].strip() == "":
                body_lines = body_lines[1:]
            return "".join(body_lines)
    return text
