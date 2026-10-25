"""Plan which shipped default authz rules an existing workspace is missing (bug-146).

A workspace gets the starter rules
(`STARTER_RULES`) once, at
`kolsch setup`. Rules added to the manifest later never reached it.
This module decides — purely, no I/O, no stdio — which of them
`kolsch setup --sync-bundle` may offer, and `kolsch doctor` reports:

- **proposals** — missing `prompt` / `deny` defaults. They can't
  grant anything without a human per call (`prompt` is a deny for
  every non-chat caller), so an upgrade may add them, with consent.
- **report_only** — missing `allow` defaults. Never written by an
  upgrade (deny → allow is a silent grant); the user adds them with
  `kolsch permissions add`.
- **present** — the user already has a rule (any pattern, effect or
  `from:`) for that target, or a locked rule for it: they tuned it, so
  the default is not offered.
- **unavailable** — a `prompt` / `deny` default for a tool the
  workspace's catalog doesn't have: a rule naming an unknown tool would
  stop the authz provider from loading, so it isn't offered.

A manifest key already in the ledger (`.kolsch/default-rules.json`,
any decision) is skipped entirely, so a default the user removed or
declined is never offered again. The protected default-deny tail is
never planned: a missing tail is reported by
[`describe_default_deny_tail_problem`][providers.authz.describe_default_deny_tail_problem]
(bug-152), not repaired here.

Placement: accepted rules go directly before the first default-deny
tail (found by shape, via
[`inspect_default_deny_tail`][providers.authz.inspect_default_deny_tail]),
else at the end, so they only change the outcome of actions no
existing rule covers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from providers.authz import inspect_default_deny_tail

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from assets.default_rules import StarterRule

_UPGRADE_EFFECTS = frozenset({"prompt", "deny"})


@dataclass(frozen=True, slots=True)
class DefaultRulesPlan:
    """The missing starter rules for one rule list, by kind.

    Attributes:
        proposals: Missing `prompt` / `deny` defaults to offer, in
            manifest order.
        report_only: Missing `allow` defaults (never written).
        present: Keys of defaults the user already has a rule for.
        unavailable: Missing `prompt` / `deny` defaults whose tool is
            not in the catalog (not offered).
        tail_present: The rule list has a default-deny tail.
        insertion_index: Where accepted rules go: the first tail's
            index, else `len(rules)`.
    """

    proposals: tuple[StarterRule, ...]
    report_only: tuple[StarterRule, ...]
    present: tuple[str, ...]
    unavailable: tuple[StarterRule, ...]
    tail_present: bool
    insertion_index: int

    @property
    def actionable(self) -> bool:
        """True when there is at least one rule to offer."""
        return bool(self.proposals)


def plan_default_rule_additions(
    rules: Sequence[object],
    manifest: Sequence[StarterRule],
    ledger: Mapping[str, object],
    *,
    locked_rules: Sequence[object] = (),
    is_known_ref: Callable[[str], bool] | None = None,
) -> DefaultRulesPlan:
    """Plan the missing starter rules for one platform rule list.

    Pure. Never proposes the tail or an `allow`, and never proposes a
    key that is in `ledger`.

    Args:
        rules: The raw `authorization.config.rules` list.
        manifest: The starter-rule manifest (normally `STARTER_RULES`).
        ledger: Decisions already recorded, keyed by manifest key.
        locked_rules: The raw `authorization.config.locked_rules`.
        is_known_ref: Whether a qualified `what` (one containing `/`)
            resolves in the workspace catalog; `None` skips the check.

    Returns:
        The [`DefaultRulesPlan`][cli._migrations.default_rules.DefaultRulesPlan].
    """
    covered = _covered_whats([*rules, *locked_rules])
    proposals: list[StarterRule] = []
    report_only: list[StarterRule] = []
    present: list[str] = []
    unavailable: list[StarterRule] = []
    for entry in manifest:
        if entry.tail or entry.key in ledger:
            continue
        if entry.what in covered:
            present.append(entry.key)
        elif entry.effect not in _UPGRADE_EFFECTS:
            report_only.append(entry)
        elif (
            is_known_ref is not None
            and "/" in entry.what
            and not is_known_ref(entry.what)
        ):
            unavailable.append(entry)
        else:
            proposals.append(entry)
    status = inspect_default_deny_tail(rules)
    return DefaultRulesPlan(
        proposals=tuple(proposals),
        report_only=tuple(report_only),
        present=tuple(present),
        unavailable=tuple(unavailable),
        tail_present=status.index is not None,
        insertion_index=status.index if status.index is not None else len(rules),
    )


def apply_default_rule_additions(
    rules: Sequence[object],
    accepted: Iterable[StarterRule],
    insertion_index: int,
) -> list[Any]:
    """Return `rules` with `accepted` inserted at `insertion_index`.

    Pure preview of what the upgrade writes: every existing entry keeps
    its value and relative order. (The writer itself inserts one rule at
    a time through
    [`mutate_platform_rules`][cli.permissions.mutate_platform_rules],
    which finds the same slot on the disk list.)
    """
    added = [entry.as_rule() for entry in accepted if not entry.tail]
    return [*rules[:insertion_index], *added, *rules[insertion_index:]]


def _covered_whats(entries: Iterable[object]) -> set[str]:
    """`what` values of every new-shape (no `who`) rule mapping."""
    return {
        str(entry["what"])
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("who") is None
        and isinstance(entry.get("what"), str)
    }
