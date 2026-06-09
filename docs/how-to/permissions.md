# Manage Permissions

Recipes for editing the authorization rule list that gates what tools
your agents can call and what arguments they can pass. For *why* the
rule system works the way it does, read the
[secrets-sandbox-authz contract §3](../explanation/secrets-sandbox-authz-contract.md)
(particularly §3.7 on the `prompt` effect). The first-agent tutorial
covers the in-chat modal in
[step 9's sidebar](../tutorials/first-agent.md).

You have **three** surfaces for editing rules — pick the one that
matches what you're doing:

| Surface | When to use |
|---|---|
| **`/perm` slash commands (in chat)** | Quick adjustments while chatting — write a rule, undo a rule, see the rule list, all without leaving the conversation. |
| **`kolsch permissions` CLI** | Scripted edits, batch changes, audit reports. Same atomic write as the in-chat modal. |
| **Hand-edit `platform.yaml`** | Cross-machine sync, version control, comments. The runtime reloads on the next authz check. |

All three write the same shape; you can mix and match.

---

## The rule shape

Every rule is three fields — `what`, `pattern`, `effect`:

```yaml
- what: kolsch/bash      # which tool / system action
  pattern: "find *"      # which invocations of that tool
  effect: allow          # what to do when this matches
```

- **`what`** is the **fully-qualified tool ref** (`<bundle>/<name>`)
  OR a system action namespace (`agent.execute.*`) OR the catch-all
  `*`. Bare names (`bash` instead of `kolsch/bash`) are rejected at
  startup with the candidates listed; you must commit to a bundle
  even if there's only one.
- **`pattern`** is a **glob** — `*` matches any chars, `?` matches
  one char, everything else is literal. No regex characters: `^`,
  `$`, `[`, `]`, `\` etc. are rejected at config load.
- **`effect`** is `allow`, `deny`, or `prompt`. `prompt` only does
  the right thing in chat — non-chat callers (`kolsch run`, the
  server, tests) collapse `prompt` to `deny` so they never hang on
  stdin.

Evaluation is **first-match-wins** with **default-deny** when
nothing matches. The protected `{ what: "*", pattern: "*", effect: deny }`
catch-all is the safety net; the runtime refuses to remove it.

---

## Tool refs: qualified by bundle

The action the dispatcher passes is `tool.<name>` (e.g.,
`tool.bash`); the rule's `what` is the qualified ref
`<bundle>/<name>` (e.g., `kolsch/bash`). The matcher tries `what`
against both forms so the same rule fires regardless of which one
the catalog produces.

To see what qualified refs are available in your workspace:

```bash
ls bundles/*/tools/*.py
# bundles/kolsch/tools/bash.py       → kolsch/bash
# bundles/kolsch/tools/read_file.py  → kolsch/read_file
# ...
```

Or from inside chat:

```
/tools
```

`/tools` lists each tool the active agent can call alongside the
rule that governs it.

---

## Patterns: globs, not regex

The pattern matches against the **target** the tool receives — for
`bash` that's the command string; for file tools it's the resolved
path; for network tools (future) it's the URL.

| Glob | Matches | Doesn't match |
|---|---|---|
| `*` | anything (including the empty string) | — |
| `find *` | `find /`, `find . -name foo` | `findx`, `grep find` |
| `/src/*` | `/src/main.py`, `/src/lib/x.py` | `/lib/x.py`, `/srcx.py` |
| `?` | any single character | `ab`, `` |

Notably **NOT supported**:

- `^` and `$` — globs have no anchors; the match is always the
  whole string.
- `[abc]` character classes — the schema rejects `[` at load.
- Regex quantifiers (`+`, `{n,m}`) — globs only have `*`/`?`.
- Backslash escapes — characters are always literal.

Trying to write a regex-looking pattern produces
`authz.pattern_invalid` at config load with the bad characters
named. Fix the rule and reload.

---

## Editing rules in chat — `/perm`

Five verbs share one command. All five validate that `<tool>` is
qualified and `<pattern>` is a glob; bad input prints a one-line
error and leaves the rule list untouched.

```
/perm allow  <tool> <pattern>    permit matching invocations
/perm deny   <tool> <pattern>    block matching invocations
/perm prompt <tool> <pattern>    ask each time an invocation matches
/perm remove <tool> <pattern>    drop the new-shape rule with this (tool, pattern)
/perm list                       print every current rule in evaluation order
```

Quote patterns with spaces:

```
/perm allow kolsch/bash "find *"
/perm allow kolsch/bash "rg *"
/perm deny kolsch/bash "rm -rf *"
/perm prompt kolsch/write_file /etc/*
```

After an `allow`/`deny`/`prompt`, the chat prints the position the
rule landed at:

```
/perm allow: added rule  what='kolsch/bash'  pattern='find *'  effect=allow  (above the catch-all, position 4)
```

If you write a duplicate `(what, pattern)`, `/perm` replaces the
prior rule rather than appending a second copy — the output names
the replaced position:

```
/perm allow: replaced previous rule at position 4
/perm allow: added rule  what='kolsch/bash'  pattern='find *'  effect=allow  (above the catch-all, position 4)
```

### Why didn't my `/perm` rule take effect?

`/perm` lands new rules above the default-deny tail. If a **broader
deny or prompt** is already above the tail, that rule still wins
because the matcher is first-match-wins:

```
/perm list
   0  what='agent.execute.*'  pattern='*'         effect=allow
   1  what='kolsch/bash'       pattern='*'         effect=deny     ← broader; fires first
   2  what='kolsch/bash'       pattern='find *'    effect=allow    ← shadowed
   3  what='*'                 pattern='*'         effect=deny     (protected)
```

To fix: either `/perm remove kolsch/bash "*"` to drop the broader
deny, or hand-edit `platform.yaml` to reorder. The slash commands
intentionally don't reorder hand-arranged rules — you get to decide
which intent wins.

---

## Editing rules from the CLI — `kolsch permissions`

The CLI is the scriptable mirror of `/perm`. Use it for batch
changes, audit reports, or anywhere you'd rather not paste into a
chat:

```bash
kolsch permissions list                    # print every rule (legacy entries marked)
kolsch permissions show <rule_id>          # one rule's details
kolsch permissions add --who ".*" --what "kolsch/bash" --effect allow
kolsch permissions remove <rule_id>        # by stable id
```

The CLI surface uses the **legacy** flag names (`--who`, `--what`)
for compatibility with existing scripts; the rule it writes still
ends up in the new shape if you supply the qualified tool ref in
`--what`. The full transition to a `--pattern` flag is a planned
follow-up.

The default-deny tail is **removal-protected**:
`kolsch permissions remove rl_default_deny` raises
`authz.protected_rule`. Direct edits to `platform.yaml` removing
the tail are rejected at bootstrap.

---

## Hand-editing `platform.yaml`

For cross-machine sync (committing rules to your dotfiles repo) or
when you want to leave a YAML comment explaining a rule, edit the
file directly:

```yaml
authorization:
  variant: rule_based
  config:
    rules:
      - { what: agent.execute.*,    pattern: "*",        effect: allow }
      - { what: kolsch/read_file,   pattern: "*",        effect: allow }
      - { what: kolsch/list_files,  pattern: "*",        effect: allow }
      - { what: kolsch/write_file,  pattern: "*",        effect: prompt }
      # Allow read-only bash commands without asking.
      - { what: kolsch/bash,        pattern: "find *",   effect: allow }
      - { what: kolsch/bash,        pattern: "ls *",     effect: allow }
      - { what: kolsch/bash,        pattern: "grep *",   effect: allow }
      # Everything else `bash` triggers the modal.
      - { what: kolsch/bash,        pattern: "*",        effect: prompt }
      # Default-deny tail (protected).
      - { what: "*",                pattern: "*",        effect: deny }
```

Reloads on the next authz check — no restart.

---

## When a tool is denied: the recovery capsule

When the engine denies a tool call, the chat renders a distinct
**permission capsule** (orange sidebar, separate from the dim-cyan
tool-failure capsule):

```
│ permission · 14:23:48                                  [dark_orange3]
│ denied: bash(command='rm -rf /')
│   matched rule:  what='kolsch/bash'  pattern='*'  effect='deny'
│
│ to allow:  /perm allow kolsch/bash "rm *"
```

Three things to read:

1. **The tool ref + abbreviated args** so you know what was asked.
2. **The matched rule** so you can decide whether to undo it,
   tighten it, or leave it alone.
3. **The exact `/perm allow` command** — type it (or edit the
   pattern first) to unblock the model on the next try.

The model also sees the same `/perm` hint in its tool-result
payload (`recovery_hint` on the `tool.failed` event), so it stops
flailing on alternative tools and surfaces the actionable text in
its reply.

> **Note:** the dispatcher wraps every authz denial in a
> `tool.unauthorized` error at the tool boundary (the canonical
> [`AuthzDenied`](../explanation/error-model.md) code is reserved
> for the provider-level decision; the capsule keys off the
> wrapped code because that's what reaches the renderer).

---

## Migrating from the legacy `{id, who, what, effect}` schema

Workspaces created before
`qol-permission-system-human-readable.md` shipped carry the legacy
shape:

```yaml
# legacy
- { id: rl_abc, who: ".*", what: "tool\\.bash", effect: prompt }
```

The schema you want is:

```yaml
# new
- { what: kolsch/bash, pattern: "*", effect: prompt }
```

**`kolsch setup` re-run migrates automatically.** Run it once
against an existing workspace:

```bash
kolsch setup
```

The setup runner:

1. Calls the migrator at `cli/_migrations/authz_rules.py`.
2. Resolves each `tool\.<name>` to `<home_bundle>/<name>` via the
   catalog.
3. Atomically rewrites `platform.yaml` (backup lands under
   `.platform/platform.yaml.bak.<timestamp>`).
4. Emits a one-line `config.deprecated_regex_rule` warning per
   rule the heuristic couldn't convert (custom user regexes with
   character classes, lookahead, etc.).

After migration, `kolsch permissions list` annotates any rules
that stayed in legacy form with
`[legacy regex — please convert]`. The platform still loads them
— migration is one-shot; an unconvertible rule doesn't brick the
workspace — but you should convert by hand or via `/perm` when
convenient.

The heuristic covers every pattern the pre-QoL `_modal.py`
`suggest_pattern` produced, plus the canonical post-Phase-2.0
default rule set. Anything else falls into the "please convert"
bucket.

---

## Inspect the current rule set

```bash
kolsch permissions list
```

```
rl_ec…  allow   what='agent.execute.*'        pattern='*'
rl_0d…  allow   what='kolsch/read_file'       pattern='*'
rl_95…  allow   what='kolsch/list_files'      pattern='*'
rl_d3…  prompt  what='kolsch/write_file'      pattern='*'
rl_45…  allow   what='kolsch/bash'            pattern='find *'
rl_44…  allow   what='kolsch/bash'            pattern='ls *'
rl_4a…  allow   what='kolsch/bash'            pattern='grep *'
rl_c4…  allow   what='kolsch/bash'            pattern='cat *'
rl_99…  prompt  what='kolsch/bash'            pattern='*'
rl_default_deny  deny  what='*'                pattern='*'  (protected)
```

Inside chat:

```
/perm list
```

Same data; same evaluation order.

---

## The four-level stack

The authz provider walks four layers, deepest first:

```
agent.yaml `permissions:`            ← most specific
   ↑
bundle.yaml `permissions:`           ← per-bundle
   ↑
platform.yaml `authorization.rules`  ← workspace default
   ↑
default: prompt-on-first-use (chat) / deny (non-chat)
```

First-match-wins inside each layer; fall through if nothing
matches. A narrower agent rule wins over the wider bundle rule
which wins over the wider platform rule. Both the modal and
`/perm` write to **`platform.yaml`** by default; to narrow,
hand-edit the rule into `bundle.yaml` or the agent's
own `permissions:` block.

---

## Bundle permissions

A bundle author who ships a tool can ship the rules that gate it
alongside the tool:

```yaml
# bundles/git-write/bundle.yaml
name: git-write
version: 0.1
description: "Tools that mutate the repository — commit, push, branch."
permissions:
  - { what: git-write/commit,  pattern: "*",  effect: prompt }
  - { what: git-write/push,    pattern: "*",  effect: prompt }
  - { what: git-write/branch,  pattern: "*",  effect: allow  }
```

Any agent whose **home bundle** is `git-write` invokes these tools
through the bundle's rules. The shape is the same as
`platform.yaml.authorization.config.rules` and agent-level
`permissions:` (see
[`docs/reference/schemas.md`](../reference/schemas.md) §A.11.4).

---

## First-use prompts (chat mode)

Adding a new tool to an agent doesn't require pre-authoring rules
anymore. An unconfigured tool fires the `[y]/[a]/[n]/[d]` modal:

- **`[y]`** — allow once; no rule written.
- **`[a]`** — always allow; a new rule with the suggested glob
  pattern is appended to `platform.yaml`.
- **`[n]`** — deny once; no rule written; next call prompts again.
- **`[d]`** — always deny; a new deny rule lands in `platform.yaml`.

The suggested pattern is a glob: `find /` → `find *`, `/abs/X.py`
→ `/abs/*`. Edit before committing if you want it narrower or
broader.

Non-chat callers (`kolsch run`, the server, tests) still
hard-deny when no rule matches — the modal needs an interactive
surface.

---

## Audit: what was prompted, what was denied

```bash
kolsch sessions events ses_<id> --type tool.failed
kolsch sessions events ses_<id> --type tool.called
```

Each `tool.failed` whose `error.type == tool.unauthorized`
carries a `recovery_hint` field with the exact `/perm allow`
command that would un-block. Pipe through `jq` if you want a
quick summary:

```bash
kolsch sessions events ses_<id> --type tool.failed --json | \
  jq -r 'select(.data.error.type=="tool.unauthorized") | .data.recovery_hint'
```

---

## What this does NOT cover

- **Per-agent rule scopes via `who:`.** v1 evaluates platform
  rules without per-principal scoping; agent / bundle layers
  handle that via the four-level stack.
- **Time-limited rules** ("allow this for 5 minutes"). Deferred.
- **Real sandboxing for `bash`.** The native sandbox runs `bash`
  in-process; the path-access policy does not constrain it. Real
  isolation (Docker / firejail) is a future patch.
- **Shadow detection on `/perm`.** Smart-insertion only collapses
  exact-duplicate `(what, pattern)` rules; broader-rule shadows
  are surfaced only via `/perm list` (see "Why didn't my `/perm`
  rule take effect?" above).

---

## Where next

- [`docs/explanation/secrets-sandbox-authz-contract.md`](../explanation/secrets-sandbox-authz-contract.md) §3.7 — the `prompt` effect's
  semantics and what each caller does with it.
- [`docs/explanation/error-model.md`](../explanation/error-model.md) §4.16 — the codes the
  permissions system surfaces (including `authz.bare_tool_ref`,
  `authz.pattern_invalid`, `config.deprecated_regex_rule`).
- [`docs/explanation/tools-subsystem.md`](../explanation/tools-subsystem.md) §4.5 — the four canonical builtin
  tools and the action names you write rules against.
- [`docs/reference/schemas.md`](../reference/schemas.md) §A.11.4 — the canonical `Rule` /
  `PermissionRule` schema with the legacy-form subsection.
- [`docs/reference/module-api-reference.md`](../reference/module-api-reference.md) Part 11.8 — the runtime API
  the CLI calls into; useful if you're scripting against the
  Authorization provider directly.
