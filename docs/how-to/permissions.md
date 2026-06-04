# Manage Permissions

Recipes for editing the authorization rule list that gates what tools
your agents can call and where they can read or write. For *why* the
rule system works the way it does, read the
[secrets-sandbox-authz contract §3](../explanation/secrets-sandbox-authz-contract.md)
(particularly §3.7 on the `prompt` effect) and the
[Phase 2.0 patch](../process/phase2.0-tools-specs.md). The first-agent
tutorial covers the in-chat modal in
[step 9's sidebar](../tutorials/first-agent.md).

The CLI surface — `kolsch permissions list/add/remove/show` — is the
out-of-chat editor; the in-chat four-choice modal (Phase 2.1) is the
in-chat editor. Both write to `platform.yaml`; both apply within the
same exchange.

## What the defaults give you

`kolsch setup` produces this rule set (workspace path resolved to
absolute):

```yaml
authorization:
  variant: rule_based
  config:
    rules:
      - { id: rl_read_workspace,  who: "tool.read_file",  what: "<ws>/**", effect: allow  }
      - { id: rl_list_workspace,  who: "tool.list_files", what: "<ws>/**", effect: allow  }
      - { id: rl_write_workspace, who: "tool.write_file", what: "<ws>/**", effect: prompt }
      - { id: rl_bash_any,        who: "tool.bash",       what: ".*",      effect: prompt }
      - { id: rl_default_deny,    who: ".*",              what: ".*",      effect: deny   }
```

Plain English: agents can read and list files inside your workspace
silently. They have to ask before writing files or running shell
commands. Anything else is denied. The `rl_default_deny` tail is
*protected* — `kolsch permissions remove rl_default_deny` is
rejected with `authz.protected_rule`.

## Inspect the current rule set

```bash
kolsch permissions list
```

```
rl_read_workspace    allow  who=tool.read_file   what=/Users/jane/Documents/kolsch/**
rl_list_workspace    allow  who=tool.list_files  what=/Users/jane/Documents/kolsch/**
rl_write_workspace   prompt who=tool.write_file  what=/Users/jane/Documents/kolsch/**
rl_bash_any          prompt who=tool.bash        what=.*
rl_default_deny      deny   who=.*               what=.*  (protected)
```

`kolsch permissions show rl_bash_any` prints one rule plus its
position in evaluation order.

## Tighten: forbid `bash` entirely

Drop the `prompt` for `bash` and replace it with an explicit deny
before the (already-denying) tail. Order matters — the new rule
needs to win over `rl_bash_any` (which currently asks), so insert
it before that rule:

```bash
kolsch permissions add \
    --who ".*" --what "tool\.bash" --effect deny \
    --before rl_bash_any
```

Now any `bash` call hits the new deny rule. Confirm:

```bash
kolsch permissions list
```

(You'll also want to remove the agent's `bash` tool entry from its
`tools:` declaration so the model isn't tempted in the first place;
the rule is the safety net, the tool list is the tutorial.)

## Loosen: pre-authorize a specific `bash` pattern

Allow only `rg` (ripgrep) commands; everything else still prompts:

```bash
kolsch permissions add \
    --who ".*" --what "tool\.bash" --effect allow \
    --before rl_bash_any
# Now edit the rule to add the command pattern — `permissions add` accepts
# `--target-pattern` in the in-chat form; out of chat, edit platform.yaml
# directly to set `what` to e.g. `"^rg .*"` for the new rule.
```

The trick is that `what` in this codebase matches the *action* by
default. For `bash`, the command-string match is achieved by writing
the `what` pattern over the action and dispatching the command-text
match through the call-site target.

(If you find yourself wanting to free-form-edit a regex from the CLI
flag, you've hit one of patch Part 8's open items. Hand-editing
`platform.yaml` for unusual patterns is fine; the runtime reloads on
the next call.)

## Audit: what was prompted, what was denied, what was approved live

The event log carries every authorization decision:

```bash
kolsch sessions events ses_<id> --type authz.prompt_requested
kolsch sessions events ses_<id> --type authz.prompt_answered
kolsch sessions events ses_<id> --type authz.prompt_unattended
kolsch sessions events ses_<id> --type authz.denied
```

Each event names the rule id (`rule_id`) so you can correlate against
`kolsch permissions show <id>`. `authz.prompt_unattended` is what
gets emitted by non-chat callers (`kolsch run`, `kolsch server`) when
a rule asked for a prompt but no interactive surface was present —
the call was denied so the process didn't hang.

## Undo a chat-time approval

The in-chat modal's `[a]` and `[d]` write a new rule into
`platform.yaml`. To undo:

```bash
kolsch permissions list   # find the rule id of the one chat added
kolsch permissions remove rl_<id>
```

The change applies on the next authz check — no restart. If you want
to be sure the chat surface re-prompts on the next attempt, remove
the rule and run the same tool again; you'll see the prompt return.

## The deny-list discipline

`rl_default_deny` is the safety net. Removing it disables
default-deny — and the runtime refuses to load a rule set without
it. The CLI raises `authz.protected_rule` when you try; direct
edits to `platform.yaml` removing the tail are rejected at bootstrap.

If you want to *loosen* defaults broadly (the wrong instinct for
v1, but possible), prefer adding a `who: ".*", what: ".*", effect:
allow` rule before the tail rather than removing the tail. The
intent then survives a future tightening of defaults — you can
delete your allow rule and you're back to safe defaults instantly.

## First-use prompts (chat mode)

Adding a new tool to an agent used to be a two-step routine:
declare the tool, then hand-edit `platform.yaml` (or the agent's
`permissions:`) to allow it. Otherwise the very first invocation
hit default-deny and returned a `tool.failed` event mid-exchange
with no path forward.

In chat mode, an unconfigured tool now fires the same
`[y]/[a]/[n]/[d]` modal you already know from `prompt`-effect
rules. The difference: there's no "matched rule" line in the
panel — the modal shows the action + target and waits for your
choice.

- **`[y]`** — allow once; no rule written.
- **`[a]`** — always allow; a new rule with the suggested
  pattern is appended to `platform.yaml`.
- **`[n]`** — deny once; no rule written; next call prompts again.
- **`[d]`** — always deny; a new deny rule lands in `platform.yaml`.

Non-chat callers (`kolsch run`, the server, tests) still hard-deny
when no rule matches — the modal needs an interactive surface,
and `phase2.0-tools-specs.md` §2.2 keeps unattended paths from
hanging.

## Four-level stack

The authz provider walks four layers, deepest first:

```
agent.yaml `permissions:`            ← most specific
   ↑
bundle.yaml `permissions:`           ← NEW (per-bundle)
   ↑
platform.yaml `authorization.rules`  ← workspace default
   ↑
default: prompt-on-first-use (chat) / deny (non-chat)
```

First-match-wins inside each layer; fall through if nothing
matches. A narrower agent rule wins over the wider bundle rule
which wins over the wider platform rule. Concretely — given:

```yaml
# agent: permissions
- { who: ".*", what: "tool\\.bash", effect: allow }

# bundle: permissions
- { who: ".*", what: "tool\\.bash", effect: deny }

# platform.yaml
- { who: ".*", what: "tool\\.bash", effect: prompt }
```

the agent's `allow` rule fires — bundle and platform are never
consulted for that action. Persist the chat modal's `[a]/[d]`
goes to **`platform.yaml`** by default (the widest scope); to
narrow it, hand-edit the rule into `bundle.yaml` or the agent's
own `permissions:` block. The four-level walk ensures the
narrower copy wins over the broader original.

## Bundle permissions

A bundle author who ships a tool can ship the rules that gate it
alongside the tool itself. Drop a `permissions:` block into the
bundle's `bundle.yaml`:

```yaml
# bundles/git-write/bundle.yaml
name: git-write
version: 0.1
description: "Tools that mutate the repository — commit, push, branch."
permissions:
  - { who: ".*", what: "tool\\.commit",      effect: prompt }
  - { who: ".*", what: "tool\\.push",        effect: prompt }
  - { who: ".*", what: "^git-write/branch$", effect: allow  }
```

Any agent whose **home bundle** is `git-write` invokes these
tools through the bundle's rules. The shape is the same
`PermissionRule` as `platform.yaml.authorization.config.rules`
and agent-level `permissions:` (see
[`docs/reference/schemas.md`](../reference/schemas.md) §A.11.4).

A consumer who imports the bundle into their workspace gets the
rules for free — no need to hand-write them in their own
`platform.yaml`. Override one with a narrower rule in
`agent.yaml`'s `permissions:` if the agent needs different
treatment than the bundle author chose.

## Naming tools across bundles (v1 caveat)

Action names today are **single-segment** — `tool.<name>` —
regardless of which bundle ships the tool. Two bundles that both
ship a tool called `bash` share authz rules: a rule with
`what: "tool\\.bash"` matches both bundles' `bash` invocations.

Workaround: name tools uniquely if you want bundle-scoped rules.
A `git-write` bundle shipping `git_commit` rather than `commit`
sidesteps the collision.

Forward-reference: `process/qol-action-namespace-bundle-scoped.md`
(when filed) tracks the migration to bundle-rooted action names
(`tool.<bundle>.<name>`). Until that ships, plan tool names with
the cross-bundle namespace in mind.

## What this does NOT cover

- **Per-agent rule scopes.** v1 rules are platform-wide. Per-agent
  scoping waits for the workflow engine where the discrimination
  becomes useful (Phase 2.0 patch Part 7 item 1).
- **Time-limited rules** ("allow this for 5 minutes"). Deferred per
  patch Part 7 item 2.
- **Pattern-mining suggestions.** The modal proposes one default
  pattern (`^rg .*` from a `rg foo` call); smart suggestions based
  on past calls are a later patch (Part 7 item 3).
- **Real sandboxing for `bash`.** The native sandbox runs `bash`
  in-process; the path-access policy does not constrain it. Real
  isolation (Docker / firejail) is patch Part 7 item 5.

## Where next

- [`docs/explanation/secrets-sandbox-authz-contract.md`](../explanation/secrets-sandbox-authz-contract.md) §3.7 — the `prompt` effect's
  semantics and what each caller does with it.
- [`docs/explanation/error-model.md`](../explanation/error-model.md) §4.16 — the nine codes the permissions
  system surfaces.
- [`docs/explanation/tools-subsystem.md`](../explanation/tools-subsystem.md) §4.5 — the four canonical builtin
  tools and the action names you write rules against.
- [`docs/reference/module-api-reference.md`](../reference/module-api-reference.md) Part 11.8 — the runtime API
  the CLI calls into; useful if you're scripting against the
  Authorization provider directly.
