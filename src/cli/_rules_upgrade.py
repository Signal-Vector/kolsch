"""Add missing shipped default authz rules to an existing workspace (bug-146).

The runner half of
[`plan_default_rule_additions`][cli._migrations.default_rules.plan_default_rule_additions]:

- [`check_default_rules`][cli._rules_upgrade.check_default_rules] —
  read-only: loads `platform.yaml`, the ledger and the catalog, and
  returns the plan. `kolsch doctor` and `kolsch setup --sync-bundle`
  both call it, so they always agree.
- [`run_default_rules_pass`][cli._rules_upgrade.run_default_rules_pass]
  — `--sync-bundle`'s rule pass: offers the proposals with their
  rationale, inserts the accepted ones one at a time through
  [`mutate_platform_rules`][cli.permissions.mutate_platform_rules]
  (`InsertRule(position="tail")`: directly before the default-deny
  tail, else at the end; locked, comment-preserving, never touches any
  other rule or `locked_rules`), after backing `platform.yaml` up, and
  records each decision in `.kolsch/default-rules.json`.

Modes (owner decisions D2 to D4): `--yes` adds every proposal (they are
`prompt` / `deny` only); `--no` adds none and records nothing, so they
are offered again; `--dry-run` writes nothing; interactive asks once
(`[a]dd all / [c]hoose each / [s]kip`), and a skip is recorded as
`declined`. Missing `allow` defaults and a missing tail are only ever
reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from assets.default_rules import STARTER_RULES
from cli._default_rules_ledger import read_ledger, record_decisions
from cli._migrations.default_rules import plan_default_rule_additions
from errors import PlatformError
from kolsch._yamlio import YAMLError
from kolsch._yamlio import load as _yaml_load
from providers.authz import Rule, platform_rules_from_config

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

    from assets.catalog import AssetCatalog
    from assets.default_rules import StarterRule
    from cli._default_rules_ledger import Decision
    from cli._migrations.default_rules import DefaultRulesPlan
    from cli._setup import _SetupIO


@dataclass(frozen=True, slots=True)
class RulesCheck:
    """Result of [`check_default_rules`][cli._rules_upgrade.check_default_rules].

    Exactly one of `plan` / `skipped` is set.

    Attributes:
        plan: The plan, when the rules could be read.
        skipped: Why the check couldn't run (e.g. `platform.yaml`
            unparseable, catalog build failed).
        catalog: The catalog the plan was checked against (passed on
            to the writer so it validates exactly as the runtime does).
    """

    plan: DefaultRulesPlan | None = None
    skipped: str | None = None
    catalog: AssetCatalog | None = None


@dataclass
class RulesPassOutcome:
    """What the rule pass did — lines for the sync report."""

    lines: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    # True when at least one rule was offered (so the run wasn't a no-op).
    offered: bool = False


async def check_default_rules(
    root: Path, *, pending_tool_refs: Collection[str] = ()
) -> RulesCheck:
    """Plan the missing default rules for the workspace at `root`. Read-only.

    Args:
        root: The workspace root.
        pending_tool_refs: Tool refs a dry run *would* have copied in;
            treated as present in the catalog so the preview matches
            the real run.

    Returns:
        A [`RulesCheck`][cli._rules_upgrade.RulesCheck].
    """
    loaded = _load_rules(root / "platform.yaml")
    if isinstance(loaded, str):
        return RulesCheck(skipped=loaded)
    rules, locked = loaded
    catalog = await _build_catalog(root)
    if isinstance(catalog, str):
        return RulesCheck(skipped=catalog)
    pending = frozenset(pending_tool_refs)

    def known(ref: str) -> bool:
        return ref in pending or catalog.lookup(ref) is not None

    plan = plan_default_rule_additions(
        rules,
        STARTER_RULES,
        read_ledger(root),
        locked_rules=locked,
        is_known_ref=known,
    )
    return RulesCheck(plan=plan, catalog=catalog)


async def run_default_rules_pass(
    io: _SetupIO,
    *,
    dry_run: bool,
    yes: bool,
    no: bool,
    pending_tool_refs: Collection[str] = (),
) -> RulesPassOutcome:
    """Offer and apply the missing `prompt` / `deny` defaults; see module doc."""
    outcome = RulesPassOutcome()
    check = await check_default_rules(io.root, pending_tool_refs=pending_tool_refs)
    if check.plan is None:
        outcome.lines.append(f"? default rules not checked: {check.skipped}")
        return outcome
    plan = check.plan
    decisions: dict[str, Decision] = dict.fromkeys(plan.present, "present")

    accepted: list[StarterRule] = []
    if plan.proposals:
        outcome.offered = True
        accepted = _decide(io, plan, outcome, dry_run=dry_run, yes=yes, no=no)
        if not dry_run and not no:
            for entry in plan.proposals:
                if entry not in accepted:
                    decisions[entry.key] = "declined"
    if accepted and not dry_run:
        decisions.update(_insert(io, accepted, check.catalog, outcome))

    for entry in plan.unavailable:
        outcome.lines.append(
            f"· {entry.what} → {entry.effect}: not offered — the tool isn't "
            "installed in this workspace."
        )
    from cli.permissions import permissions_add_command  # noqa: PLC0415

    for entry in plan.report_only:
        rule = Rule(what=entry.what, pattern=entry.pattern, effect=entry.effect)
        outcome.lines.append(
            f"! missing default `allow` (never added by sync — {entry.rationale}): "
            f"`{permissions_add_command(rule)}`"
        )
    if not dry_run:
        record_decisions(io.root, decisions)
    return outcome


def _decide(
    io: _SetupIO,
    plan: DefaultRulesPlan,
    outcome: RulesPassOutcome,
    *,
    dry_run: bool,
    yes: bool,
    no: bool,
) -> list[StarterRule]:
    """Return the proposals the user accepts (all under `--yes`)."""
    where = (
        "directly before the default-deny tail"
        if plan.tail_present
        else "at the end (this rule list has no default-deny tail — see "
        "`kolsch doctor`)"
    )
    verb = "would add" if dry_run else "add"
    outcome.lines.append(
        f"{len(plan.proposals)} new default rule(s) shipped since this "
        f"workspace was set up ({verb} {where}):"
    )
    for entry in plan.proposals:
        outcome.lines.append(f"  + {_label(entry)}")
    if no:
        outcome.lines.append("  → not added (--no); offered again next run.")
        return []
    if yes or dry_run:
        return list(plan.proposals)
    listing = "\n".join(f"  {_label(entry)}" for entry in plan.proposals)
    answer = io.prompter.choose(
        f"[sync] add these default permission rules?\n{listing}",
        ["[a]dd all", "[c]hoose each", "[s]kip (don't offer again)"],
        "a",
    )
    if answer == "a":
        return list(plan.proposals)
    if answer == "c":
        return [
            entry
            for entry in plan.proposals
            if io.prompter.confirm(f"[sync] add {_label(entry)}?", default=True)
        ]
    outcome.lines.append("  → skipped; recorded as declined (not offered again).")
    return []


def _insert(
    io: _SetupIO,
    accepted: list[StarterRule],
    catalog: AssetCatalog | None,
    outcome: RulesPassOutcome,
) -> dict[str, Decision]:
    """Back up `platform.yaml`, insert each accepted rule; return the decisions."""
    from cli._setup import _backup_platform_yaml  # noqa: PLC0415 — import cycle
    from cli.permissions import InsertRule, mutate_platform_rules  # noqa: PLC0415

    yaml_path = io.root / "platform.yaml"
    decisions: dict[str, Decision] = {}
    try:
        backup = _backup_platform_yaml(yaml_path)
    except OSError as exc:
        outcome.failures.append(f"platform.yaml backup failed; no rule added: {exc}")
        return decisions
    outcome.lines.append(f"  backup: {backup.relative_to(io.root)}")
    for entry in accepted:
        rule = Rule(what=entry.what, pattern=entry.pattern, effect=entry.effect)
        try:
            result = mutate_platform_rules(
                yaml_path, InsertRule(rule, position="tail"), catalog=catalog
            )
        except PlatformError as exc:
            outcome.failures.append(f"could not add {_label(entry)}: {exc}")
            break
        if result.written and result.rule is not None:
            decisions[entry.key] = "added"
            outcome.lines.append(
                f"  ✓ added {result.rule.id}: {entry.what} → {entry.effect} "
                f"(rule {(result.position or 0) + 1})"
            )
        else:
            # Identical rule already in effect (written meanwhile).
            decisions[entry.key] = "present"
    return decisions


def _label(entry: StarterRule) -> str:
    return f"{entry.what} → {entry.effect} ({entry.rationale})"


def _load_rules(yaml_path: Path) -> tuple[list[object], list[object]] | str:
    """Return `(rules, locked_rules)` from `platform.yaml`, or why not."""
    if not yaml_path.is_file():
        return "platform.yaml not found"
    try:
        data: Any = _yaml_load(yaml_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, YAMLError):
        return "platform.yaml couldn't be read or parsed"
    if not isinstance(data, dict):
        return "platform.yaml is not a mapping"
    authorization = data.get("authorization")
    if not isinstance(authorization, dict):
        return "no `authorization` mapping"
    if authorization.get("variant", "rule_based") != "rule_based":
        return "authorization variant is not `rule_based`"
    config = authorization.get("config", {})
    rules = platform_rules_from_config(config)
    if rules is None:
        return "`authorization.config.rules` is not a list"
    locked = config.get("locked_rules") if isinstance(config, dict) else None
    return rules, locked if isinstance(locked, list) else []


async def _build_catalog(root: Path) -> AssetCatalog | str:
    """Build the workspace catalog, or return why it failed."""
    from assets import AssetManagement  # noqa: PLC0415 — heavy import

    am = AssetManagement(root=root)
    try:
        await am.initialize()
        return am.catalog()
    except Exception as exc:  # noqa: BLE001 — advisory: report, never raise
        return f"catalog build failed ({exc})"
