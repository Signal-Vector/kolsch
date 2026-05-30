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
