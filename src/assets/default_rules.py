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

    Returns:
        A fresh list of 13 rule mappings, pre-flight allow first and
        the default-deny tail last.
    """
    return [
        # Pre-flight gate — the engine requires agent.execute.<id>
        # to run any exchange. Default to allow; users tighten by
        # adding a more specific deny before this rule.
        agent_execute_preflight_rule(),
        # The canonical builtins — owned by the kolsch bundle.
        {"what": "kolsch/tools/read_file", "pattern": "*", "effect": "allow"},
        {"what": "kolsch/tools/list_files", "pattern": "*", "effect": "allow"},
        {"what": "kolsch/tools/write_file", "pattern": "*", "effect": "prompt"},
        {"what": "kolsch/tools/bash", "pattern": "*", "effect": "prompt"},
        # qol-fetch-url-tool — network access is a fresh capability;
        # conservative default per bash/write_file.
        {"what": "kolsch/tools/fetch_url", "pattern": "*", "effect": "prompt"},
        # qol-browse-url-tool — Tier 2 (opt-in `kolsch[browse]`
        # extra). Same conservative prompt default as fetch_url so the
        # chat surface asks before the agent drives headless Chromium.
        {"what": "kolsch/tools/browse_url", "pattern": "*", "effect": "prompt"},
        # spec-136: `jev_decide` sends workspace data (the `state`) to a
        # third party (OpenRouter / TypeSafe) — same conservative
        # `prompt` default as fetch_url.
        {"what": "kolsch/tools/jev_decide", "pattern": "*", "effect": "prompt"},
        # bug-024: workflow-service tools. `run_workflow` opens a
        # WorkflowCard AND queues a run; conservative `prompt` so the
        # user confirms the run before any nodes fire. `workflow_status`
        # is read-only; safe to `allow`. `cancel_workflow` is user-
        # initiated safety; `allow` so the model can react to an Esc
        # or an explicit "cancel that" instruction without a modal.
        {"what": "kolsch/tools/run_workflow", "pattern": "*", "effect": "prompt"},
        {"what": "kolsch/tools/workflow_status", "pattern": "*", "effect": "allow"},
        {"what": "kolsch/tools/cancel_workflow", "pattern": "*", "effect": "allow"},
        # bug-027: `workflow_inspect` is read-only introspection —
        # the model calls it to discover a workflow's required
        # inputs before `run_workflow`. Safe to `allow`; no side
        # effects, no run started.
        {"what": "kolsch/tools/workflow_inspect", "pattern": "*", "effect": "allow"},
        # Default-deny tail (protected).
        default_deny_tail_rule(),
    ]
