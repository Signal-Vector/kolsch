"""The platform's default authorization rules — the single source.

Two rule sets ship with the platform, and both are built here so their
shared entries (the engine pre-flight allow and the protected
default-deny tail) cannot drift apart:

- [`fail_closed_default_rules`][assets.default_rules.fail_closed_default_rules]
  — what [`default_platform_payload`][assets.platform_config.default_platform_payload]
  writes when `platform.yaml` is auto-generated on a bare root. Agents
  may run; every tool is denied.
- [`workspace_only_default_rules`][assets.default_rules.workspace_only_default_rules]
  — what `kolsch setup` writes for a new workspace: the kolsch
  bundle's builtin tools allowed or gated by `prompt`, then the tail.
  Derived from `STARTER_RULES`,
  the declarative starter-rule manifest that
  `kolsch setup --sync-bundle` also plans upgrades from (bug-146).

The fail-closed set is catalog-independent on purpose:
`load_or_create` runs before the catalog scan, and the authz provider
rejects a `<bundle>/tools/<name>` rule that resolves to no catalogued
tool. Only system-namespace actions (`what` containing `.`) and the
catch-all `*` compile against any catalog, so that is all it contains.

Every function returns fresh dicts and lists; callers may mutate the
result freely. This module imports nothing from `cli` or `providers`
(both import `assets`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

StarterEffect = Literal["allow", "prompt", "deny"]


def default_deny_tail_rule() -> dict[str, str]:
    """Return the protected default-deny tail rule.

    The catch-all `deny` that must end every production rule set. The
    authz provider derives its id as `rl_default_deny` and refuses to
    remove it.

    Returns:
        A fresh `{"what": "*", "pattern": "*", "effect": "deny"}`.
    """
    return {"what": "*", "pattern": "*", "effect": "deny"}


def agent_execute_preflight_rule() -> dict[str, str]:
    """Return the engine pre-flight allow rule.

    The engine checks `agent.execute.<id>` before it runs any exchange,
    so without this allow the default-deny tail would block every
    agent. Users tighten it by adding a more specific deny before it.

    Returns:
        A fresh `{"what": "agent.execute.*", "pattern": "*", "effect": "allow"}`.
    """
    return {"what": "agent.execute.*", "pattern": "*", "effect": "allow"}


def fail_closed_default_rules() -> list[dict[str, str]]:
    """Return the auto-generated `platform.yaml`'s fail-closed rule set.

    Agents may run (the pre-flight allow); every tool is denied by the
    tail, in chat too — the tail matches before the no-rule approval
    prompt, so no modal is shown. `kolsch setup` writes the full
    workspace set instead.

    Returns:
        A fresh `[agent_execute_preflight_rule(), default_deny_tail_rule()]`.
    """
    return [agent_execute_preflight_rule(), default_deny_tail_rule()]


@dataclass(frozen=True, slots=True)
class StarterRule:
    """One entry of the starter-rule manifest.

    Attributes:
        key: Stable id, never reused (e.g. `"jev_decide"`). The
            `.kolsch/default-rules.json` ledger records decisions by it.
        what: The rule's `what`.
        pattern: The rule's `pattern`.
        effect: The rule's `effect`.
        since: The spec / ticket that introduced the rule.
        rationale: One user-facing line on why the default is what it
            is; shown when an upgrade offers the rule.
        tail: True only for the protected default-deny tail.
    """

    key: str
    what: str
    pattern: str
    effect: StarterEffect
    since: str
    rationale: str
    tail: bool = False

    def as_rule(self) -> dict[str, str]:
        """Return the `{what, pattern, effect}` mapping written to `platform.yaml`."""
        return {"what": self.what, "pattern": self.pattern, "effect": self.effect}


def _from_rule(
    key: str,
    rule: dict[str, str],
    effect: StarterEffect,
    *,
    since: str,
    rationale: str,
) -> StarterRule:
    """Wrap one of the shared rule builders above as a manifest entry."""
    if rule["effect"] != effect:  # pragma: no cover — static data
        msg = f"starter rule {key!r}: effect {rule['effect']!r} != {effect!r}"
        raise ValueError(msg)
    return StarterRule(
        key=key,
        what=rule["what"],
        pattern=rule["pattern"],
        effect=effect,
        since=since,
        rationale=rationale,
    )


def _tool(
    name: str, effect: StarterEffect, *, since: str, rationale: str
) -> StarterRule:
    return StarterRule(
        key=name,
        what=f"kolsch/tools/{name}",
        pattern="*",
        effect=effect,
        since=since,
        rationale=rationale,
    )


# The starter-rule manifest — the single source of the rules a new
# workspace gets, in evaluation order. Monotonic, like
# `cli._migrations.tools.TOOL_MIGRATIONS`: entries are only ever added
# (before the tail); a `key` is never removed or reused, because the
# ledger and `--sync-bundle`'s upgrade planner key on it.
STARTER_RULES: tuple[StarterRule, ...] = (
    # Pre-flight gate — the engine requires agent.execute.<id> to run
    # any exchange. Default to allow; users tighten by adding a more
    # specific deny before this rule.
    _from_rule(
        "agent_execute",
        agent_execute_preflight_rule(),
        "allow",
        since="phase-2.0",
        rationale="lets agents run at all (the engine's pre-flight check)",
    ),
    # The canonical builtins — owned by the kolsch bundle.
    _tool("read_file", "allow", since="phase-2.0", rationale="read-only file access"),
    _tool(
        "list_files",
        "allow",
        since="phase-2.0",
        rationale="read-only listing",
    ),
    _tool(
        "write_file",
        "prompt",
        since="phase-2.0",
        rationale="changes files in the workspace",
    ),
    _tool("bash", "prompt", since="phase-2.0", rationale="runs shell commands"),
    # qol-fetch-url-tool — network access is a fresh capability;
    # conservative default per bash/write_file.
    _tool(
        "fetch_url",
        "prompt",
        since="qol-fetch-url-tool",
        rationale="makes network requests",
    ),
    # qol-browse-url-tool — Tier 2 (opt-in `kolsch[browse]` extra).
    # Same conservative prompt default as fetch_url so the chat
    # surface asks before the agent drives headless Chromium.
    _tool(
        "browse_url",
        "prompt",
        since="qol-browse-url-tool",
        rationale="drives a headless browser over the network",
    ),
    # spec-136: `jev_decide` sends workspace data (the `state`) to a
    # third party (OpenRouter / TypeSafe) — same conservative `prompt`
    # default as fetch_url.
    _tool(
        "jev_decide",
        "prompt",
        since="spec-136",
        rationale="sends workspace content to a third party (OpenRouter)",
    ),
    # bug-024: workflow-service tools. `run_workflow` opens a
    # WorkflowCard AND queues a run; conservative `prompt` so the user
    # confirms the run before any nodes fire. `workflow_status` is
    # read-only; safe to `allow`. `cancel_workflow` is user-initiated
    # safety; `allow` so the model can react to an Esc or an explicit
    # "cancel that" instruction without a modal.
    _tool(
        "run_workflow",
        "prompt",
        since="bug-024",
        rationale="starts a workflow run",
    ),
    _tool(
        "workflow_status",
        "allow",
        since="bug-024",
        rationale="read-only workflow run status",
    ),
    _tool(
        "cancel_workflow",
        "allow",
        since="bug-024",
        rationale="stops a run the user asked to stop",
    ),
    # bug-027: `workflow_inspect` is read-only introspection — the
    # model calls it to discover a workflow's required inputs before
    # `run_workflow`. Safe to `allow`; no side effects, no run started.
    _tool(
        "workflow_inspect",
        "allow",
        since="bug-027",
        rationale="read-only workflow introspection",
    ),
    # Default-deny tail (protected). Always last.
    replace(
        _from_rule(
            "default_deny_tail",
            default_deny_tail_rule(),
            "deny",
            since="phase-2.0",
            rationale="denies every action no earlier rule covers",
        ),
        tail=True,
    ),
)


def workspace_only_default_rules() -> list[dict[str, str]]:
    """Return the workspace-only default rules `kolsch setup` writes.

    `what` is the 3-seg catalog ref `<bundle>/tools/<name>` for
    builtin tools, the system action namespace
    (`agent.execute.*`) for the engine pre-flight, and the
    catch-all `*` for the default-deny tail. New workspaces start
    in the 3-seg canonical form so the action matches the rule
    without translation.

    Includes an `agent.execute.*` allow at the top so the engine's
    pre-flight authz check passes — without this the default-deny
    tail would block every exchange.

    The `kolsch/tools/*` refs resolve only when the kolsch bundle is
    in the catalog, so this set is not usable as a bare-root default
    (see [`fail_closed_default_rules`][assets.default_rules.fail_closed_default_rules]).

    Derived from `STARTER_RULES`,
    in manifest order.

    Returns:
        A fresh list of 13 rule mappings, pre-flight allow first and
        the default-deny tail last.
    """
    return [rule.as_rule() for rule in STARTER_RULES]
