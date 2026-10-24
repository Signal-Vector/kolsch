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

## How permissions gate an exchange

Kolsch checks authorization at **two independent surfaces** during an
agent exchange. Rules that gate one surface do not gate the other —
mixing them up is the single most common cause of "unauthorized"
errors that look confusing at first.

### Surface 1 — Agent execution (one check per exchange)

Before an agent's exchange loop even starts, the engine's pre-flight
asks the authz provider: **"is this principal allowed to run this
agent?"** If the answer is deny, no LLM call happens, no tool call
happens — the exchange fails at pre-flight with `agent.unauthorized`
and event kind `thread.exchange_failed`.

The action string this check uses is:

```
agent.execute.<agent_id>
```

For the starter `kolsch/agents/helper` (whose `agent_id: ag_helper`),
that's `agent.execute.ag_helper`. For a personal `acme/agents/butler`
(`agent_id: ag_butler`) it's `agent.execute.ag_butler`. Rule shape
matches on the `what:` field the same way as tool rules:

```yaml
- what: agent.execute.*    # allow any principal to run any agent
  pattern: "*"
  effect: allow
```

The default kolsch rule set ships this catch-all so a fresh workspace
just works. If your workspace was ever hand-edited to narrow it
(e.g. `^agent\.execute\.ag_butler$` to only run butler), any agent
you try to run afterwards — including `kolsch/agents/helper` at
`kolsch setup`'s test step — fails at pre-flight.

Errors from this surface look like:

```
error.type:    agent.unauthorized
error.message: Agent 'ag_helper' unauthorized for action 'agent.execute.ag_helper'
error.where:   engine.preflight.authorize
```

Fix: add an allow rule with `what: agent.execute.*` (broadest) OR a
specific-id rule per agent you want to run.

### Surface 2 — Tool call (one check per invocation)

Every time an agent calls a tool, the dispatcher asks the authz
provider: **"is this agent allowed to call this specific tool with
these arguments?"** If deny, the tool call becomes a `tool.failed`
event; the agent sees it and (usually) course-corrects.

The action string this check uses is the tool's canonical 3-segment
catalog ref:

```
<bundle>/tools/<name>
```

For example `kolsch/tools/bash`, `kolsch/tools/read_file`. Rule shape:

```yaml
- what: kolsch/tools/bash    # which tool
  pattern: "ls *"            # which invocations (glob against target)
  effect: allow
```

`pattern:` matches against the **target** the tool receives — for
`bash` that's the command string; for file tools it's the resolved
path. See §"Patterns: globs, not regex" below.

Errors from this surface look like:

```
error.type:    tool.unauthorized
error.message: tool 'bash' unauthorized for pattern 'rm -rf /'
error.where:   tools.dispatch
```

Fix: add an allow rule with the right `what:` + `pattern:` combo,
or approve via the in-chat modal / `/perm allow`.

### The two surfaces together

A minimal workspace that lets you run the starter agent AND use bash:

```yaml
- what: agent.execute.*     # Surface 1 — let any principal run any agent
  pattern: "*"
  effect: allow
- what: kolsch/tools/bash   # Surface 2 — prompt before every bash invocation
  pattern: "*"
  effect: prompt
- what: "*"                 # default-deny tail (protected)
  pattern: "*"
  effect: deny
```

Rule order matters — evaluation is **first-match-wins** — but the
two surfaces read from the same rule list. The engine picks whichever
rules match `agent.execute.*` at pre-flight, and the dispatcher picks
whichever rules match `<bundle>/tools/<name>` at each tool call. Rules
that don't match are silently skipped.

---

## The rule shape

Every rule is three fields — `what`, `pattern`, `effect`:

```yaml
- what: kolsch/tools/bash      # which tool / system action
  pattern: "find *"      # which invocations of that tool
  effect: allow          # what to do when this matches
```

- **`what`** is the **3-segment catalog ref** (`<bundle>/tools/<name>`)
  OR a system action namespace (`agent.execute.*`) OR the catch-all
  `*`. Bare names (`bash` instead of `kolsch/tools/bash`) are rejected at
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
catch-all (id `rl_default_deny`) is the safety net; `kolsch setup`
writes it last and `kolsch permissions remove` refuses to remove it.
A hand-edited rule list without it still loads — see
[If your rule list has no default-deny tail](#if-your-rule-list-has-no-default-deny-tail).

---

## Tool refs: one grammar everywhere

The runtime action the dispatcher passes to the authz check, the
`what` field on a rule, and the `subsystems.tools.config.declared`
ref in an agent YAML all use the same 3-segment catalog ref
(`<bundle>/tools/<name>`, e.g. `kolsch/tools/bash`). The matcher
fnmatches `what` against the action directly — no translation,
no bridge map. Two bundles can ship a tool with the same name
(`kolsch/tools/bash` vs `acme/tools/bash`) and authz rules
target them independently.

To see what tools are available in your workspace:

```bash
ls bundles/*/tools/*.py
# bundles/kolsch/tools/bash.py       → kolsch/tools/bash
# bundles/kolsch/tools/read_file.py  → kolsch/tools/read_file
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
/perm allow  <tool> <pattern> [from:<ref>]    permit matching invocations
/perm deny   <tool> <pattern> [from:<ref>]    block matching invocations
/perm prompt <tool> <pattern> [from:<ref>]    ask each time an invocation matches
/perm remove <tool> <pattern> [from:<ref>]    drop the new-shape rule with this (tool, pattern, from)
/perm list                                    print every current rule in evaluation order
```

Quote patterns with spaces:

```
/perm allow kolsch/tools/bash "find *"
/perm allow kolsch/tools/bash "rg *"
/perm deny kolsch/tools/bash "rm -rf *"
/perm prompt kolsch/tools/write_file /etc/*
```

Add `from:<ref>` to scope a rule to a specific agent or workflow
frame (see
["Scoping rules to a specific agent or workflow"](#scoping-rules-to-a-specific-agent-or-workflow)
below):

```
/perm allow kolsch/tools/bash "rg *" from:kolsch/agents/triage
/perm prompt kolsch/tools/bash "*" from:kolsch/workflows/security
```

After an `allow`/`deny`/`prompt`, the chat prints the position the
rule landed at:

```
/perm allow: added rule  what='kolsch/tools/bash'  pattern='find *'  effect=allow  (above the catch-all, position 4)
```

If you write a duplicate `(what, pattern)`, `/perm` replaces the
prior rule rather than appending a second copy — the output names
the replaced position:

```
/perm allow: replaced previous rule at position 4
/perm allow: added rule  what='kolsch/tools/bash'  pattern='find *'  effect=allow  (above the catch-all, position 4)
```

### Why didn't my `/perm` rule take effect?

`/perm` lands new rules above the default-deny tail. If a **broader
deny or prompt** is already above the tail, that rule still wins
because the matcher is first-match-wins:

```
/perm list
   0  what='agent.execute.*'  pattern='*'         effect=allow
   1  what='kolsch/tools/bash'       pattern='*'         effect=deny     ← broader; fires first
   2  what='kolsch/tools/bash'       pattern='find *'    effect=allow    ← shadowed
   3  what='*'                 pattern='*'         effect=deny     (protected)
```

To fix: either `/perm remove kolsch/tools/bash "*"` to drop the broader
deny, or hand-edit `platform.yaml` to reorder. The slash commands
intentionally don't reorder hand-arranged rules — you get to decide
which intent wins.

Two other reasons a `/perm` rule doesn't fire:

- **A locked rule matches.** `/perm list` prints locked rules first.
  They run before every layer and `/perm` can't override or remove
  them — see [Locked rules](#locked-rules).
- **A workflow, agent or bundle layer decides first.** Layer rules are
  consulted before `platform.yaml`'s `rules` — see
  [How the layers stack](#how-the-layers-stack).

---

## Debugging denials in chat — `/why` and `/reload`

Two companion slash commands help you iterate on rules without
restarting the chat.

### `/why` — inspect a denial

When a tool call was denied and you want to see the exact rule
that fired:

```
/why                    # deny card for the most recent denial in this thread
/why <tool_call_id>     # deny card for a specific tool call
```

The card names the matched rule, the layer it came from
(`platform` / `bundle` / `workflow` / `agent`), the source
`file:line` where it's declared, and the caller / target / action
strings the check ran against. Use this to answer "which rule?"
without hunting through YAMLs.

If no denial is found in the current thread, `/why` prints a
notice suggesting `/why <tool_call_id>` if you meant a specific
call.

### `/reload` — pick up rule edits without restarting

After hand-editing any of the 4 rule sources — `platform.yaml`,
`bundles/*/bundle.yaml`, workflow `policy:` blocks, or agent
`permissions:` fields — run:

```
/reload
```

The command atomically:

1. Rescans the filesystem to discover new / deleted YAMLs since
   the chat started (so `bundles/new-bundle/bundle.yaml` created
   mid-session is picked up).
2. Re-parses every YAML in all 4 layers.
3. If `platform.yaml` is missing (deleted mid-session): aborts and
   keeps the old authz provider live. `/reload` never re-creates the
   file — restore it, or run `kolsch setup`, then `/reload` again.
4. If ANY parse fails: aborts, keeps the old authz provider live,
   prints the offending `file:line`. Enforcement never enters a
   half-loaded state.
5. Otherwise: swaps in the fresh provider and prints a summary
   like `reloaded 17 rules across 4 layers [+1 bundle, -1 agent]`.

Next authz decision uses the new rules. In-flight tool calls
straddling a `/reload` are race-safe — they see the old provider
consistently across all their decisions. A workflow run already in
progress keeps the rules it started with; new runs and chat turns
pick them up.

Common `/why` → edit → `/reload` loop:

```
> run some tool                # denied
> /why                         # see which rule blocked, and where
> (edit platform.yaml)         # adjust the rule
> /reload                      # pick up the edit
> run the tool again           # allowed
```

---

## Editing rules from the CLI — `kolsch permissions`

The CLI is the scriptable mirror of `/perm`. Use it for batch
changes, audit reports, or anywhere you'd rather not paste into a
chat:

```bash
kolsch permissions list                    # print every rule (legacy entries marked)
kolsch permissions show <rule_id>          # one rule's details
kolsch permissions add --what "kolsch/tools/bash" --pattern "*" --effect allow
kolsch permissions add --what "kolsch/tools/bash" --pattern "find *" \
    --from "kolsch/agents/triage" --effect allow
kolsch permissions remove <rule_id>        # by stable id
kolsch permissions add --locked --what "kolsch/tools/bash" --effect deny
```

`--what` is the qualified tool ref (or `agent.execute.*` /
`*`), `--pattern` is the glob matched against the target (defaults
to `*`), and `--effect` is one of `allow`/`deny`/`prompt`. The
optional `--from <ref>` scopes the rule to invocations whose
caller-chain contains a matching 3-segment agent/workflow ref (see
["Scoping rules to a specific agent or workflow"](#scoping-rules-to-a-specific-agent-or-workflow)
below).

The default-deny tail is **removal-protected**:
`kolsch permissions remove rl_default_deny` exits 1 with
`authz.protected_rule` and leaves `platform.yaml` untouched. A
hand edit that removes or moves the tail is **not** refused — the
rule set loads with a warning (see
[If your rule list has no default-deny tail](#if-your-rule-list-has-no-default-deny-tail)).

`--locked` adds to `authorization.config.locked_rules` instead — a deny
no layer can override (see [Locked rules](#locked-rules)).

`kolsch permissions add` / `remove` rewrite `platform.yaml`: only a
comment block at the very end of the file is preserved; comments
anywhere else are lost.

---

## Scoping rules to a specific agent or workflow

By default, a rule fires for every caller — every agent, every
workflow. That's usually fine for platform-wide defaults, but
sometimes you want a narrower scope: "let `triage` run `bash rg *`,
but no other agent." The optional `from:` field on a rule is the
knob.

```yaml
# Only fires when the caller-chain contains `kolsch/agents/triage`.
- { what: kolsch/tools/bash, pattern: "rg *", from: "kolsch/agents/triage", effect: allow }

# Fires for any agent in the `security` workflow.
- { what: kolsch/tools/bash, pattern: "*", from: "kolsch/workflows/security", effect: prompt }

# Broad — no `from:`, fires regardless of caller.
- { what: kolsch/tools/bash, pattern: "ls *", effect: allow }
```

Three rules to remember:

1. **`from:` is a 3-segment glob.** Shape must be
   `<bundle>/<agents|workflows>/<name>`; `<name>` can be a literal
   or a glob (`kolsch/agents/*`). Anything else raises a parse-time
   error.
2. **Caller-chain is proximate-first.** For a workflow `W`
   invoking agent `A` invoking `tool.bash`, the chain is
   `(kolsch/agents/A, kolsch/workflows/W)`. A rule with
   `from: kolsch/workflows/W` fires because the workflow appears in
   the chain (any frame matches — the check is transitive).
3. **Empty chain fails closed.** A rule with `from:` set will NOT
   fire for engine-bootstrap invocations that never entered an
   agent/workflow frame.

The chat modal auto-populates `from:` from the proximate frame
when you press `[a]` or `[d]` — so approving a tool during agent
`X`'s run scopes the rule to `X` by default. Type `*` at the
`from: [<default>]` prompt to broaden.

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
      - { what: kolsch/tools/read_file,   pattern: "*",        effect: allow }
      - { what: kolsch/tools/list_files,  pattern: "*",        effect: allow }
      - { what: kolsch/tools/write_file,  pattern: "*",        effect: prompt }
      # Allow read-only bash commands without asking.
      - { what: kolsch/tools/bash,        pattern: "find *",   effect: allow }
      - { what: kolsch/tools/bash,        pattern: "ls *",     effect: allow }
      - { what: kolsch/tools/bash,        pattern: "grep *",   effect: allow }
      # Everything else `bash` triggers the modal.
      - { what: kolsch/tools/bash,        pattern: "*",        effect: prompt }
      # Ask before fetching a URL from the open web — the light
      # `fetch_url` (httpx + trafilatura) or the heavier
      # `browse_url` (opt-in `kolsch[browse]` extra, Playwright + Chromium).
      - { what: kolsch/tools/fetch_url,   pattern: "*",        effect: prompt }
      - { what: kolsch/tools/browse_url,  pattern: "*",        effect: prompt }
      # Default-deny tail (protected).
      - { what: "*",                pattern: "*",        effect: deny }
```

Reloads on the next authz check — no restart.

### An auto-generated `platform.yaml`

When something boots the platform on a root with no `platform.yaml`
(the programmatic `bootstrap_runtime` API, for example — the CLI
refuses to run there and points you at `kolsch setup`), kolsch writes
a default file and logs a WARNING. Its rules are fail-closed:

```yaml
      - { what: agent.execute.*,  pattern: "*",  effect: allow }
      - { what: "*",              pattern: "*",  effect: deny }
```

Agents may run; every tool is denied, in chat too — the tail matches
before the approval prompt, so no modal appears. Run `kolsch setup`
for the full workspace rule set, or add rules with `kolsch permissions
add` (they go before the tail). The file starts with a comment header
explaining this; the first `kolsch permissions` rewrite drops that
header, since only a trailing comment block is preserved.

### If your rule list has no default-deny tail

Every command that starts the runtime (`kolsch run`, `chat`,
`sessions`, `server`, `workflows`, `mcp serve`, …) checks the
platform rule list once per process. When the
`{ what: "*", pattern: "*", effect: deny }` tail is missing, or is
not the last rule, it logs one warning on stderr and **loads the
rules anyway** — nothing is refused and no rule is changed:

```
WARNING platform_runtime: /path/to/platform.yaml: the authorization rule set has no default-deny tail (...). Effect: ... Fix: ...
```

`kolsch doctor` reports the same finding in an **Authorization**
section. It is advisory: doctor's exit code doesn't change.

What it means today:

- **Tail missing.** An action no rule covers asks for approval in
  `kolsch chat` — and is **allowed without asking** under
  `kolsch chat --auto-approve` — and is denied everywhere else
  (`kolsch run`, the server, headless workflows). An empty, absent,
  or `null` `rules:` list counts as missing.
- **Tail present (for comparison).** An action no rule covers is
  denied outright everywhere — in chat too, with no approval
  prompt, and `--auto-approve` doesn't change that.
- **Tail missing, with a catch-all allow** (e.g.
  `{ who: ".*", what: ".*", effect: allow }`). That rule already
  allows every action no earlier rule denies — remove or narrow it
  before adding the tail.
- **Tail not last.** Every rule after it can never match.

How to fix:

- Missing: `kolsch permissions add --what '*' --pattern '*' --effect deny`
  (appends the tail as `rl_default_deny`), then `kolsch doctor` to
  re-check. This rewrites `platform.yaml` (comments other than a
  trailing block are lost) — hand-add the rule instead if you want
  to keep them.
- Not last: move the tail rule to the end of
  `authorization.config.rules` by hand. `kolsch permissions add`
  would insert a new rule *before* the existing tail, not move it.

Notes:

- Only the two exact tail shapes are recognized — the rule above,
  and the legacy `{ who: ".*", what: ".*", effect: deny }`. An
  equivalent catch-all deny written differently (`pattern: "**"`,
  `what: "^.*$"`, `who: ".+"`) still hard-denies uncovered actions,
  but is reported as "no default-deny tail". That errs on the safe
  side; use the canonical shape to clear the warning.
- A catch-all deny scoped with `from:` is not a tail — it only
  covers that caller. This applies to canonical-shape rules; legacy
  `{who, what, effect}` rules currently ignore `from:` altogether.
- `/reload` in chat does not re-check. A tail removed mid-session
  is reported by `kolsch doctor` or the next command that starts
  the runtime.

---

## When a tool is denied: the recovery capsule

When the engine denies a tool call, the chat renders a distinct
**permission capsule** (orange sidebar, separate from the dim-cyan
tool-failure capsule):

```
│ permission · 14:23:48                                  [dark_orange3]
│ denied: bash(command='rm -rf /')
│   matched rule:  what='kolsch/tools/bash'  pattern='*'  effect='deny'
│
│ to allow:  /perm allow kolsch/tools/bash "rm *"
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
- { what: kolsch/tools/bash, pattern: "*", effect: prompt }
```

**`kolsch setup` re-run migrates automatically.** Run it once
against an existing workspace:

```bash
kolsch setup
```

The setup runner:

1. Calls the migrator at `cli/_migrations/authz_rules.py`.
2. Resolves each `tool\.<name>` to `<home_bundle>/tools/<name>`
   via the catalog (3-segment canonical form).
3. Auto-upgrades the deprecated 2-segment shape
   (`<bundle>/<name>`, e.g. `kolsch/bash`) to 3-segment
   (`<bundle>/tools/<name>`) when the catalog confirms the
   bundle owns a tool of that name.
4. Atomically rewrites `platform.yaml` (backup lands under
   `.platform/platform.yaml.bak.<timestamp>`).
5. Emits a one-line `config.deprecated_regex_rule` warning per
   legacy regex rule the heuristic couldn't convert, and a
   `config.deprecated_rule_form` warning per 2-segment rule it
   surfaced.

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
rl_ec…  allow   what='agent.execute.*'              pattern='*'
rl_0d…  allow   what='kolsch/tools/read_file'       pattern='*'
rl_95…  allow   what='kolsch/tools/list_files'      pattern='*'
rl_d3…  prompt  what='kolsch/tools/write_file'      pattern='*'
rl_45…  allow   what='kolsch/tools/bash'            pattern='find *'
rl_44…  allow   what='kolsch/tools/bash'            pattern='ls *'
rl_4a…  allow   what='kolsch/tools/bash'            pattern='grep *'
rl_c4…  allow   what='kolsch/tools/bash'            pattern='cat *'
rl_99…  prompt  what='kolsch/tools/bash'            pattern='*'
rl_fu…  prompt  what='kolsch/tools/fetch_url'       pattern='*'
rl_bu…  prompt  what='kolsch/tools/browse_url'      pattern='*'
rl_default_deny  deny  what='*'                     pattern='*'  (protected)
```

Inside chat:

```
/perm list
```

Same data; same evaluation order.

---

## How the layers stack

Every authorization check walks these stages in order. The **first
match at any stage ends the walk**:

```
platform.yaml `authorization.config.locked_rules`   ← locked: deny-only, nothing below can override
   ↓
workflow `policy:`                                  ← only when a workflow builtin node fires the action
   ↓
agent.yaml `permissions:`                           ← the acting agent
   ↓
bundle.yaml `permissions:`                          ← the acting agent's home bundle
   ↓
platform.yaml `authorization.config.rules`          ← workspace rules (ends with the default-deny tail)
   ↓
no rule matched: prompt (chat) / deny (everywhere else)
```

Within each stage, rules are first-match-wins top to bottom; if
nothing in a stage matches, the walk falls through to the next one.

**Layers can widen past the platform. This is intended.** A workflow,
agent or bundle rule is consulted *before* `platform.yaml`'s `rules`,
so a layer `allow` wins over a platform `deny` for the same action —
including an explicit one, not just the default-deny tail. Layers can
equally narrow (a layer `deny` beats a platform `allow`) or shift a
`prompt` to `allow`. The layering is precedence, not enforced
narrowing.

One structural protection always holds: an **embedded agent never
inherits its workflow's `policy:`**. When a workflow runs an `agent`
node, that agent's tool calls are checked against the agent and bundle
layers only, so a workflow can't hand its own permissions to the
agents it calls.

The in-tool check (`tools.permissions.check` / `require`, used by tool
authors) walks locked rules, then the agent and bundle layers, then the
platform rules and default deny — the same stack minus the workflow
layer.

Both the modal and `/perm` write to **`platform.yaml`**'s `rules`.
To narrow for one agent or bundle, hand-edit the rule into its
`permissions:` block.

### Who can widen what

A layer's rules are only as trustworthy as whoever can write its file:

| Layer | File | Who can write it | What it reaches |
|---|---|---|---|
| Locked rules | `platform.yaml` → `authorization.config.locked_rules` | The operator, `kolsch permissions add --locked`, anyone who can edit `platform.yaml` | Every action, before every layer (deny only) |
| Workflow `policy:` | `bundles/<b>/workflows/*.yaml` | Whoever can write `bundles/` | Builtin nodes of that workflow |
| Agent `permissions:` | `bundles/<b>/agents/*.yaml` | Whoever can write `bundles/` | Every tool call that agent makes |
| Bundle `permissions:` | `bundles/<b>/bundle.yaml` | Whoever can write `bundles/` | Every agent whose home bundle it is |
| Platform rules | `platform.yaml` → `authorization.config.rules` | The operator, `kolsch permissions`, `/perm`, the approval modal | Whatever no locked rule or layer decided |

!!! warning "Write access to `platform.yaml` and `bundles/` is the real boundary"
    "Whoever can write `bundles/`" includes an **agent** whose
    `write_file` rules reach that directory: it can author a workflow
    whose `policy:` allows `bash`, and run it. An agent that can write
    `platform.yaml` — or run `bash`, and so `kolsch permissions remove
    <id>` — can delete a locked rule. No rule in this file defends
    against that. Keep `write_file` (and `bash`) away from
    `platform.yaml` and `bundles/` if you rely on locked rules or on
    the platform list to contain your agents.

---

## Locked rules

A **locked rule** is the one control no layer can override. Use it
for "never, whatever any workflow, agent or bundle says":

```yaml
# platform.yaml
authorization:
  variant: rule_based
  config:
    locked_rules:
      - { what: kolsch/tools/bash, pattern: "*", effect: deny }
    rules:
      - { what: agent.execute.*, pattern: "*", effect: allow }
      # ...
      - { what: "*", pattern: "*", effect: deny }
```

Locked rules live in their own list and are evaluated **before every
layer**, first-match-wins. A matching locked rule always denies.

The shape is deliberately narrow, and anything else fails at load
(every `kolsch` command and `/reload` refuse the file, naming
`locked_rules[<i>]` and the file (with the line where known), and
the fix):

- **Deny only.** `allow` and `prompt` are refused: a locked `allow`
  would stop layers from narrowing, and a locked `prompt` would
  persist "always" answers into the `rules` list it pre-empts.
- **`{what, pattern, effect}` only.** No legacy `who`, no `id` (ids are
  derived as `rl_locked_<hash>`), and no `from:` — the tool path
  doesn't pass a caller chain yet, so a `from:`-scoped lock would never
  fire.
- **`what` is a full three-segment ref** (`kolsch/tools/bash`, not
  `kolsch/bash` — the short form would load and never match) or a
  system action like `agent.execute.<ref>`.
- **Not the catch-all.** `{what: "*", pattern: "*"}` would deny
  everything; keep it as the default-deny tail at the end of `rules`.
- No duplicates.

Manage them from the CLI:

```bash
kolsch permissions add --locked --what kolsch/tools/bash --effect deny
kolsch permissions list            # locked section first, when there are any
kolsch permissions show rl_locked_<hash>
kolsch permissions remove rl_locked_<hash>
```

`--locked` requires `--effect deny` and refuses `--from`, `--before`,
`--after` and `--first`. Locked rules are not removal-protected — they
are yours. `/perm` and the approval modal never add, change or remove
them. After a hand edit, `/reload` picks them up. A workflow run
already in progress keeps the rules it started with; new runs and
chat turns pick them up.

**Lock whole tools.** Prefer `pattern: "*"`. Pattern locks are weaker
than they look:

- A pattern is a glob over the target *string* the tool receives, not a
  sandbox. A locked `bash` deny on `rm *` is bypassed by `bash -c 'rm
  …'`, `env rm …` and similar.
- Path patterns match the path as written. A locked `write_file` deny
  on `/etc/*` does not catch `/tmp/../etc/passwd` or a relative path.

**What locked rules cannot do:**

- Govern Python that runs without an authorization check — authored
  `@node` compute nodes and the bodies of tool files under `bundles/`.
  Anyone who can write those files can run code directly.
- Protect themselves. See the warning in
  [Who can widen what](#who-can-widen-what): they are only as strong
  as write access to `platform.yaml`.
- Prevent a lock-out. A locked deny on `agent.execute.*`, or on a tool
  your chat agent needs, locks you out of chat too. Fix it from the
  CLI (`kolsch permissions remove rl_locked_<hash>`) or by editing
  `platform.yaml`.

**How a locked denial looks in chat.** The failed tool block's hint
names the rule and says it's locked:

```
denied by rl_locked_9d58f5ce (deny) at ~/kolsch/platform.yaml:52
locked rule — workflow/agent/bundle allows can't override it; edit authorization.config.locked_rules in platform.yaml, then /reload
```

`/why` shows `layer locked`, `/tools` marks a whole-tool lock as
`(deny, locked)`, and `/perm list` lists locked rules first. Outside
chat, the `rl_locked_` id appears as `rule_id` in the run's
`policy.decision.made` events and in `tool.failed`'s `matched_rule`.

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
  - { what: git-write/tools/commit,  pattern: "*",  effect: prompt }
  - { what: git-write/tools/push,    pattern: "*",  effect: prompt }
  - { what: git-write/tools/branch,  pattern: "*",  effect: allow  }
```

Any agent whose **home bundle** is `git-write` invokes these tools
through the bundle's rules. The shape is the same as
`platform.yaml.authorization.config.rules` and agent-level
`permissions:` — see the `Rule` model in the
[Schemas reference](../reference/schemas.md).

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

`kolsch sessions events ses_<id>` prints every event in the session
as one JSON line, so filter with `jq` — `.type` is the event kind:

```bash
kolsch sessions events ses_<id> | jq -c 'select(.type=="tool.failed")'
kolsch sessions events ses_<id> | jq -c 'select(.type=="tool.called")'
```

Each `tool.failed` whose `.data.error.type == "tool.unauthorized"`
carries a `.data.recovery_hint` field with the exact `/perm allow`
command that would un-block. Pull just the hints:

```bash
kolsch sessions events ses_<id> \
  | jq -r 'select(.type=="tool.failed" and .data.error.type=="tool.unauthorized") | .data.recovery_hint'
```

---

## What this does NOT cover

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
- [Schemas reference](../reference/schemas.md) — the `Rule` /
  `PermissionRule` model, generated from source.
