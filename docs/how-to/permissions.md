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
| **`kolsch permissions` CLI** | Scripted edits, batch changes, audit reports. Same locked, insert-only write as the in-chat modal. |
| **Hand-edit `platform.yaml`** | Cross-machine sync, version control, comments. A running chat keeps enforcing the rules it loaded until you run `/reload` (or restart it); new `kolsch` commands read the file fresh. |

All three write the same shape; you can mix and match. Every rule
write — an "always" answer, `/perm`, `kolsch permissions add/remove` —
inserts or removes **one** rule in the file as it is on disk right
now, under a lock (`.platform/platform.yaml.lock`), and keeps your
comments and every other edit. So a chat and `kolsch permissions` in
another terminal can run at the same time without undoing each other.

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
rule landed at in `platform.yaml`:

```
/perm allow: added rule  what='kolsch/tools/bash'  pattern='find *'  effect=allow  (above the catch-all, position 4 in platform.yaml)
```

`/perm` changes the chat's live rules **and** the file. The file write
inserts into (or removes from) the rules as they are on disk now, so
edits made outside this chat survive. If the file can't be written —
it's missing, unparseable, its rules don't load, or another writer
holds the lock — the change still applies to this chat and `/perm`
says so:

```
/perm allow: applied for this chat only — not saved: unparseable: …
```

If you write a duplicate `(what, pattern)`, `/perm` replaces the
prior rule rather than appending a second copy — the output names
the replaced position:

```
/perm allow: replaced previous rule at position 4
/perm allow: added rule  what='kolsch/tools/bash'  pattern='find *'  effect=allow  (above the catch-all, position 4 in platform.yaml)
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
consistently across all their decisions. Approval prompts keep
working after a `/reload`.

`/reload` is the **only** way a running chat starts enforcing rule
edits made on disk — by hand, by `kolsch permissions`, by `kolsch
setup`, or by another chat. Answering **always** or running `/perm`
never undoes those edits (they're kept in the file), but it doesn't
apply them either. When an **always** answer finds that the file's
rules differ from the ones the chat loaded, the chat shows a one-time
notice:

```
platform.yaml's rules changed on disk since this chat loaded them; those changes are not enforced yet. Review them (`kolsch permissions list`), then run `/reload`.
```

Review the file before you `/reload` — it applies every edit in it,
whoever made it.

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
```

`--what` is the qualified tool ref (or `agent.execute.*` /
`*`), `--pattern` is the glob matched against the target (defaults
to `*`), and `--effect` is one of `allow`/`deny`/`prompt`. The
optional `--from <ref>` scopes the rule to invocations whose
caller-chain contains a matching 3-segment agent/workflow ref (see
["Scoping rules to a specific agent or workflow"](#scoping-rules-to-a-specific-agent-or-workflow)
below).

`add` and `remove` edit the file as it is on disk (under the same
lock the chat uses) and print the rule's position. An `add` of a rule
that's already in effect prints `already present at position N;
nothing written`. Any failure exits 1 with an `error:` line and leaves
the file untouched.

The default-deny tail is **removal-protected**:
`kolsch permissions remove rl_default_deny` exits 1 with
`authz.protected_rule` and leaves `platform.yaml` untouched. A
hand edit that removes or moves the tail is **not** refused — the
rule set loads with a warning (see
[If your rule list has no default-deny tail](#if-your-rule-list-has-no-default-deny-tail)).

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

!!! warning "Behaviour change (bug-158): `from:` rules on tools are now enforced"
    Before bug-158 the caller chain never reached tool authorization,
    so **every `from:`-scoped tool rule was silently inert** (and
    `agent.execute.*` rules scoped with `from:` only matched inside
    workflows). After upgrading:

    - a scoped **deny** now blocks the agent or workflow it names —
      calls that used to fall through to a broader allow fail with
      `tool.unauthorized`;
    - a scoped **allow** now grants — calls that used to prompt or be
      denied now run without asking;
    - a scoped **prompt** now prompts (and collapses to deny outside
      chat);
    - `from:`-scoped `agent.execute.*` rules now also match in
      `kolsch chat` / `kolsch run` / the API (chain `(A,)`), and inside
      workflows they see the workflow frames too.

    Review your scoped rules with `kolsch permissions list` (look for
    `from=`) and the `permissions:` blocks in agent / bundle YAML and
    workflow `policy:`.

Four rules to remember:

1. **`from:` is a 3-segment glob.** Shape must be
   `<bundle>/<agents|workflows>/<name>`; `<name>` can be a literal
   or a glob (`kolsch/agents/*`). Anything else raises a parse-time
   error.
2. **The caller chain is proximate-first, and any frame matches.**
   The chain lists the frames that led to the call, closest first:
   the calling agent, then the workflow it runs in, then any
   enclosing workflows out to the root. A rule with
   `from: kolsch/workflows/W` fires for every call whose chain
   contains `W` — W's own tool nodes and the agents W runs alike.
3. **An empty chain never matches.** A call with no agent or
   workflow frame (only an agent that isn't backed by a catalog
   entry and has no callers) is *unattributed*: a scoped allow does
   not grant it and a scoped deny does not block it.
4. **Layers still win over `from:`.** Scoping narrows *which calls* a
   rule applies to; it does not move the rule up the
   [four-level stack](#the-four-level-stack). A matching rule in the
   agent's own `permissions:` (or its bundle's) is decided before any
   platform rule, scoped or not. The starter `helper.yaml`, for
   example, carries `{what: kolsch/tools/bash, effect: prompt}`, so a
   platform-level `{what: kolsch/tools/bash, from: kolsch/agents/helper,
   effect: deny}` never fires for `helper` — remove or edit the agent
   rule (or put the scoped deny in the agent's YAML).

### What the chain is at each call site

The engine and the workflow executor build the chain from their own
state — the agent's catalog ref, the workflow run's call chain. It is
never taken from model output, tool arguments, or the MCP wire.

| Call site | Caller chain |
|---|---|
| Agent `A` in `kolsch chat` / `kolsch run` / the API — tool calls, the forced skill read, and the `agent.execute.*` check | `(A,)` |
| `claude_code` agent `A` calling a kolsch tool through the MCP bridge | the same as `A`'s exchange: `(A,)`, or `(A, W, …)` inside a workflow |
| Builtin tool node (`bash`, `read_file`, `write_file`, `list_files`, `browse_url`, `jev`) in workflow `W` | `(W,)` |
| Builtin tool node in child workflow `C` called by parent `P` | `(C, P)` |
| Agent node `A` in `W` — its tool calls and its `agent.execute.*` check | `(A, W)` |
| Agent node `A` in `C`, under `P` | `(A, C, P)` |
| In-tool `permissions.check` / `require` | the chain of the tool call that is running |

A child run resumed or forked on its own (`kolsch workflows resume` /
`fork`) is rebuilt with its parent frames from the run's stored
parent pointers, so it keeps `(C, P)` — or is refused if one of those
runs is gone (see below). The chain a denial was judged
against is recorded in the session log as
`tool.unauthorized` → `detail.caller_chain`, and in a workflow run's
JSONL as `policy.decision.made` → `payload.caller_chain`.

### Known limits

- **A workflow started by an agent doesn't carry the agent's frame.**
  When agent `A` launches workflow `W` with the `run_workflow` tool,
  `W`'s nodes see `(W,)`, not `(W, A)`. To stop an agent launching
  workflows, scope the launch itself:
  `{what: kolsch/tools/run_workflow, pattern: "*", from: kolsch/agents/A, effect: deny}`.
- **A child run whose parent run was deleted can't be resumed or
  forked.** The parent frames can't be proven, so `kolsch workflows
  resume|fork` (and the chat equivalents) refuse it with
  `WorkflowLineageMissing`, naming the child and the missing run,
  before anything runs. Keep parent runs while their children may
  still be resumed.
- **Claude Code's own tools are outside kolsch.** A `claude_code`
  agent's calls to *kolsch* tools go through the MCP bridge and are
  authorized here, with the agent's chain. Claude Code's built-in
  tools (its own Bash, Read, Edit, …) never reach kolsch: they follow
  the user's Claude Code permissions (`~/.claude/settings.json`), not
  these rules.
- **Two agents can't share an `agent_id`.** The chain's agent frame is
  resolved from the agent's id, so building a second agent with an id
  already in use raises `config.duplicate_agent_id`. This includes an
  `extends:` child that inherits its parent's explicit `agent_id:` —
  give the child its own `agent_id:` (or drop the parent's to use
  derived ids).

### Where scoped rules come from

The live chat's approval prompt saves **unscoped** rules — "always
allow" / "always deny" apply to every caller. To scope a rule, use
`kolsch permissions add --from <ref>`, `/perm <verb> <tool> <pattern>
from:<ref>`, or write `from:` into `platform.yaml`, an agent's or
bundle's `permissions:`, or a workflow's `policy:` by hand.

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

New `kolsch` commands pick the edit up immediately. A chat that is
already open keeps enforcing the rules it loaded: run `/reload` in it
(or restart it) to apply the edit. The chat never undoes a hand edit —
its own rule writes insert into the file as it is on disk.

---

## If you answered "always" or used `/perm` before bug-162 was fixed

Before this fix, answering **always** (`[a]`/`[d]`) or using `/perm`
in a chat rewrote `platform.yaml`'s rules from the copy the chat
loaded at startup. Any rule change made on disk while that chat was
open — `kolsch permissions`, a hand edit, `kolsch setup`, an agent's
write — was undone, including a re-added default-deny tail and added
`deny` rules.

To check your workspace:

1. Review `kolsch permissions list`: the last rule should be the
   default-deny tail `what='*' pattern='*' effect=deny`, and every
   `deny` you added should still be there. (`kolsch doctor` reports a
   missing or misplaced tail once bug-152 lands.)
2. List the **always** answers you gave in a session:

   ```bash
   kolsch sessions events ses_<id> | jq -c 'select(.type=="ui.event" and .data.kind=="kolsch.prompt.answered" and (.data.payload.response=="a" or .data.payload.response=="d")) | [.created_at, .data.payload.rule.what, .data.payload.rule.pattern, .data.payload.rule.effect]'
   ```

3. `kolsch setup` migration backups are in
   `.platform/platform.yaml.bak.*`.

Re-add anything that was lost with `kolsch permissions add`.

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
  pattern is saved to `platform.yaml`.
- **`[n]`** — deny once; no rule written; next call prompts again.
- **`[d]`** — always deny; a new deny rule is saved to `platform.yaml`.

An **always** rule is inserted into the file as it is on disk now:
immediately before the `prompt` rule that asked (when that rule is in
`platform.yaml`), otherwise before the default-deny tail, otherwise at
the end. Comments and every other edit in the file are kept. If the
rule is already in effect, nothing is written. If the file can't be
written, the rule still applies for the rest of this chat and a notice
gives the `kolsch permissions add …` command that saves it later.

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

## What agents can never change (and what they still can)

Permission rules live in files. If an agent can write those files, it
can change its own permissions. kolsch draws the line like this.

### Never writable by agent file tools

File tools can never write these paths, whatever your rules say:

- `platform.yaml` (authorization rules, `file_access`, providers)
- `.kolsch/` (setup's baseline and ledgers)
- `.env` (secrets)
- `.platform/` (the session store)

This holds under an allow-all rule set, under a workflow `policy:` or
agent `permissions:` allow, and under `kolsch chat --auto-approve`. It
also holds for spelling tricks: `bundles/../platform.yaml`,
`PLATFORM.YAML`, an absolute path, and a symlink that points at
`platform.yaml` are all refused. The agent sees a `file_access.denied`
error saying the path is protected. Reading `platform.yaml` still works.

To change these files yourself, use `kolsch permissions`, the approval
modal's "always allow", `/perm`, `kolsch connect` or `kolsch setup`, or
edit them in an editor. There is no setting that lets agents write them.
See [File Handling §4.8](../explanation/file-handling.md#48-the-protected-write-floor).

### Still reachable, so decide deliberately

- **`bash` can do anything.** The shell isn't path-checked and the
  native sandbox doesn't isolate it. An `allow` rule for
  `kolsch/tools/bash` lets an agent rewrite `platform.yaml` with a
  shell command, so keep `bash` on `prompt`.
- **Writing `bundles/**` is the same as running code.** An agent that
  can write there can add a tool or `@node` file that runs inside
  kolsch, edit a starter tool such as `bundles/kolsch/tools/write_file.py`,
  or widen an agent's `permissions:`, a bundle's `permissions:` or a
  workflow's `policy:`. A widened `policy:` applies on the workflow's
  next run, without `/reload`. The author-workflow agent needs to write
  `bundles/` to do its job, so this isn't blocked; keep `write_file` on
  `prompt` so you see each write.
- **`--auto-approve` turns every `prompt` into an allow.** With it,
  `prompt` rules on `bash` or `write_file` no longer stop anything; only
  the protected paths above stay out of reach.
- **A `dir/*` pattern also matches `dir/../anything`.** Rule patterns
  match the path exactly as the agent wrote it, before `..` is resolved.
  So `{what: kolsch/tools/write_file, pattern: "data/*", effect: allow}`
  also allows `data/../bundles/x/agents/y.yaml`. Prefer `prompt` for
  `write_file` over a narrow-looking `allow`.
- **Claude Code's own tools.** kolsch starts Claude Code with no tools
  (text-only agents) or with `--allowedTools` listing only the kolsch
  tools it bridges, which go through kolsch rules and this floor. But
  Claude Code also loads your user settings (`~/.claude/settings.json`),
  and any built-in tool those settings allow (`Write`, `Edit`, `Bash`)
  is governed by Claude Code, not by kolsch rules or this floor.

### Check your workspace

`kolsch doctor` prints an **Agent write access** section. It lists the
protected paths, then checks the `allow` rules in `platform.yaml` for
three things:

- an `allow` for `kolsch/tools/bash` (including `*` and
  `kolsch/tools/*`);
- an `allow` for `write_file` whose pattern points into `bundles/`:
  `bundles/*`, `bundles/<name>/*`, a literal path under `bundles/`, the
  same under the absolute workspace path, or `*`;
- any other `allow` for `write_file` whose pattern uses a glob
  character (`*`, `?` or `[`), such as `data/*`. Patterns match the
  path as written, and each glob character also matches `/` and `.`,
  so these reach `bundles/` too: `data/*` matches
  `data/../bundles/…`, and `?undles/*` matches `bundles/…`.

Matching rules are grouped into one line per kind, by position and the
id `kolsch permissions list` shows. `prompt` and `deny` rules,
literal paths outside `bundles/` (no glob characters), and rules for
other tools are not flagged. Doctor doesn't consider rule order, so an `allow` that an
earlier `deny` shadows is still reported. Agent, workflow and bundle
`permissions:` / `policy:` layers aren't checked. The section is
advisory: doctor's exit code doesn't change.

```text
Agent write access — advisory, exit code unchanged:
  agent file tools can never write: platform.yaml, .kolsch/, .env, .platform/ (whatever the rules allow)
  ! rules 1 (rl_4f188373), 2 (rl_d125ce8f) allow `write_file` under `bundles/` without asking: agents can change bundle code and permission YAML (equivalent to running code).
  ! rule 3 (rl_cff8db4b) allow `write_file` on a glob pattern without asking: rule patterns match the path as the agent wrote it, and `*`, `?` and `[...]` also match `/` and `.`, so these rules can reach `bundles/` too (for example through `../`) until authz patterns are matched against normalized paths.
  see these rules (by id) with `kolsch permissions list`.
  note: `kolsch chat --auto-approve` turns every `prompt` rule into an allow.
```

With none of those rules, the section ends with:

```text
  ✓ no rule allows `bash`, and no rule allows `write_file` under `bundles/` or with a glob pattern (`*`, `?`, `[`), without asking
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
