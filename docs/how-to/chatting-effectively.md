# Chatting Effectively with `kolsch chat`

Short recipes for getting the most out of the streaming REPL.
For *why* chat works the way it does (event-stream consumer,
four-choice approval modal, slash commands), read the
[Phase 2.1 patch](../process/phase2.1-chat-specs.md). For an
introduction, see [step 10 of the first-agent
tutorial](../tutorials/first-agent.md#10-chat-with-your-agent).

The TL;DR: `kolsch chat` is the day-2 command. It streams the
same engine you've been running one-shot through `kolsch run`,
turning the event log into a conversation in your terminal.

## Starting a chat

```bash
kolsch chat                          # uses the configured starter agent
kolsch chat kolsch/helper     # explicit agent ref
kolsch chat --model haiku            # session-scoped model override
kolsch chat --session ses_abc123     # resume a prior conversation
```

The agent's `model:` field stays the same on disk — `--model`
overrides for this session only.

## Working with long conversations

Two pressures pull in opposite directions: keep enough history
that the agent has context, but don't blow the model's context
window on the dozenth turn.

### Watch the model's reply quality, not the byte count

The Phase 1.3 memory provider (`recent_buffer`) keeps the last
~20 turns by default. You'll feel the seam when the agent stops
referring back to something you said many turns ago. When that
happens:

```
/clear
```

`/clear` wipes the visible scrollback but **not** the session
log — `kolsch sessions events <id>` still has every event.
This is the right move when the agent is generating fine but
the screen is just cluttered. Same session continues; same
memory budget applies.

### Start fresh when you change topic

```
/new
```

`/new` opens a **new session** under the same agent and model.
Memory resets; the old session is preserved on disk (you can
resume it later with `--session <old_id>`). Different from
`/clear` — `/clear` keeps the same session, `/new` starts
another.

Rule of thumb: switching from "help me debug this Python" to
"draft a release note" → `/new`. Asking the same agent the
same kind of question on a new file → just keep going.

### Resume a conversation later

Grab the session id:

```bash
kolsch sessions list
```

```
SESSION_ID                                STARTED           LAST  NAME
ses_30d67125-35ca-4cdc-aeac-959fafd4c02c  2026-05-30 11:05  4s    chat:kolsch/helper
ses_11f5475c-afb2-4487-9510-b3ceae629d28  2026-05-29 09:41  19h   chat:kolsch/helper
```

Then:

```bash
kolsch chat kolsch/helper --session ses_30d67125-...
```

The prior conversation replays into your scrollback (the
renderer is a pure consumer of the event log — same JSONL,
same rendering, modulo timestamps) and you continue from where
it left off. Closing chat doesn't "save"; everything was already
on disk.

## Switching models mid-session

```
/model haiku
/model openrouter/openai/gpt-4o
/model {provider: anthropic, model: claude-opus-4-7}
```

Any `ModelSpec` input shape works — alias, compact string, or
the canonical object. The next exchange uses the new model;
prior turns are unchanged (they happened on the previous model
and the session log records that).

A `session.model_overridden` event lands in the JSONL so replay
shows where you switched.

### When to switch

- **Faster iteration on cheap models.** Drafting a regex you'll
  keep refining — `/model haiku` (or whatever your cheap alias
  is); switch back to `opus` when you need depth.
- **Tool-use density.** If the agent is calling tools a lot and
  the cheap model is bouncing off, `/model opus` for the next
  exchange. The tool's own work is unchanged; the agent's
  *decisions* about which tool and what args benefit from the
  smarter model.
- **A model is misbehaving.** Transient 5xx, weird hallucinations,
  prompt-injection paranoia — `/model <other_alias>`, try the
  exchange again. The fallback chain handles transient blips
  automatically (see
  [`configure-fallbacks.md`](configure-fallbacks.md)); manual
  switching is for "this model is wrong for this kind of
  question."

### What `/model` doesn't do

- It doesn't persist. Closing chat reverts to the agent's
  declared `model:`. To make the switch stick, edit the agent
  YAML.
- It doesn't reset history. Memory and message history carry
  over to the new model; the new model sees the conversation
  exactly as you do.
- It doesn't fire during streaming. If you `/model` while
  tokens are arriving from the old model, the switch applies to
  the next exchange — wait for the current one to finish first.

## Approving bash patterns wisely

When the agent wants to call `bash`, the default workspace rule
set hits a `prompt`-effect rule and the four-choice modal pops:

```
│ permission · 14:23:44                              [dark_orange3]
│ bash(command='rg "build_llm" src/')  —  matched what='kolsch/tools/bash' pattern='*' effect='prompt'
│
│ [y] once    [a] always allow this
│ [n] deny once    [d] always deny
│
│ ▍
```

- `[y]` — runs this exact call. Next call prompts again.
- `[n]` — refuses this call. Agent sees the deny in its tool
  result and can react (try a different approach or give up).
- `[a]` — runs the call AND writes a rule with the chat's
  suggested pattern. Future matching calls run silently.
- `[d]` — refuses the call AND writes a deny rule. Future
  matching calls don't even prompt.

The chat's pattern suggestor pre-fills the `[a]`/`[d]` rule's
`pattern` from the call. For `bash`, the default is the first
whitespace-bounded token of the command plus `*`:

- `rg foo src/` → suggests `rg *`
- `ls -la` → suggests `ls *`
- `cat README.md` → suggests `cat *`

(Patterns are **globs**, not regex — `*` means "any chars," `?`
means "one char," everything else is literal.)

You can edit the pattern before committing. **Take a second to
think about it.** A too-broad pattern (`*` allows everything for
that tool) is the same shape as a missing rule. A too-narrow
pattern (the literal command) defeats the point — you'll just
re-prompt on the next variation.

### Rules of thumb

- **Read-only commands** (`rg`, `ls`, `cat`, `find`, `grep`) →
  `[a]` with the default `<cmd> *` is fine. The risk surface
  is bounded; the agent benefits from not interrupting.
- **Anything that writes** (`rm`, `mv`, `chmod`, `git
  commit`/`push`, `sed -i`) → think before `[a]`. A narrower
  pattern (`git commit -m *`) is safer than `git *`. Often
  `[y]` (allow once) is the right call.
- **Network calls** (`curl`, `wget`, `ssh`, `nc`) → almost
  always `[y]` not `[a]`. The target URL or host matters more
  than the command name.
- **The pattern is a glob** — `*` is greedy on any chars,
  including spaces. `git *` matches `git commit -m foo` and
  `git push -f`; if that's too loose, pick the verb (`git
  commit *`).

If you regret an `[a]`, undo it with `/perm`:

```
/perm list                              # find the (what, pattern) pair
/perm remove kolsch/tools/bash "rg *"         # back to prompting
```

(Or edit `platform.yaml`; the change applies on the next authz
check.) For the full permissions story see
[`permissions.md`](permissions.md).

### What the modal isn't

- **It isn't a replacement for code review.** If the agent is
  about to do something irreversible, `[n]` is cheap. You can
  always `[y]` after eyeballing the command.
- **It isn't durable trust.** Rules you wrote when you were
  paying attention apply later when you aren't. Audit
  occasionally with `/perm list`.

## Editing permissions inside chat: `/perm`

When a tool is denied OR you just want to pre-authorize
something, type the command instead of leaving the chat:

```
/perm allow  kolsch/tools/bash "rg *"             # pre-authorize ripgrep
/perm deny   kolsch/tools/bash "rm -rf *"         # block the dangerous shape
/perm prompt kolsch/tools/write_file /etc/*       # always ask for system writes
/perm remove kolsch/tools/bash "rg *"             # undo a prior allow
/perm list                                   # print every current rule
```

Two things make this useful mid-conversation:

1. **The denied capsule prints the exact command to type.** When
   a tool fails with `tool.unauthorized`, the chat surfaces a
   permission capsule that names the matched rule and the
   `/perm allow` command that would unblock the next attempt.
   Copy, edit if you want a narrower pattern, hit enter.
2. **The model sees the same hint.** The chat's `tool.failed`
   event carries the `/perm` command in its `recovery_hint`
   field; the agent surfaces it in its reply so you don't have
   to scroll to find the panel.

Three things to know:

- **`<tool>` must be qualified.** `bash` alone is rejected with
  a friendly error naming the qualified form (e.g.,
  `kolsch/tools/bash`). Use `/tools` to list refs the agent can call.
- **`<pattern>` is a glob.** `^find .*$` is rejected at the
  slash level — same character set the YAML rejects at load.
- **Conflicts: `/perm allow` replaces an exact prior `(what,
  pattern)`** rule instead of duplicating. Broader prior denies
  still shadow your new allow (use `/perm list` to spot it).

The slash command writes to the same `platform.yaml` the modal
does; everything you change persists across chat exits.

## Cancelling and recovering

- **Ctrl-C during streaming** — cancels the current exchange.
  The agent block ends in `[cancelled]` styling; the input
  prompt returns. Useful when the agent's gone off-track or
  you noticed a typo in your prompt halfway through the
  response.
- **Ctrl-C while idle** — does nothing (a hint suggests `/exit`
  if you wanted to leave).
- **Ctrl-D** — clean exit (same as `/exit`). The session
  closes; everything is on disk.
- **The chat crashed** — `kolsch chat <agent> --session
  <prior_id>` replays the session into a new chat. The JSONL
  is intact; resume is the recovery path.

## Telling chat from `kolsch run`

| Want | Use |
|---|---|
| One question, scripted | `kolsch run <agent> --input content="..."` |
| Conversation, tool calls visible | `kolsch chat <agent>` |
| Same agent, JSON output for piping | `kolsch run <agent>` (chat is human-formatted) |
| Resume a prior chat | `kolsch chat <agent> --session <id>` |
| Resume a prior run | not the same — runs are one-shot; sessions are the durable record |

Both write to the same session log. `kolsch run` and `kolsch
chat` against the same agent ref are interchangeable in terms of
what lands on disk — the difference is the interaction surface,
not the recording.

## What this how-to does NOT cover

- **Writing custom slash commands.** Phase 2.1 ships eight; new
  ones are demand-driven (the patch's Part 0 design decision).
- **Mid-stream fallback** ("if `opus` fails halfway through, try
  `haiku`"). Out of scope per
  [`configure-fallbacks.md`](configure-fallbacks.md); the chat
  honors whatever the engine does.
- **Multi-agent chat** (two agents in one session). Phase 2+
  workflow concern, not a 2.1 surface.
- **Image rendering in agent messages.** Phase 2.1 is plain
  text + Rich panels; image-capable terminals (iTerm2, Kitty)
  may get them in a future polish phase.

## Where next

- [`docs/process/phase2.1-chat-specs.md`](../process/phase2.1-chat-specs.md) — the full chat contract: event-to-render mapping,
  modal mechanics, exit codes, every slash command.
- [`docs/how-to/permissions.md`](permissions.md) — editing the
  rule list that drives the approval modal.
- [`docs/how-to/configure-fallbacks.md`](configure-fallbacks.md) — automatic recovery from transient model failures.
- [`docs/how-to/find-models.md`](find-models.md) — discovering
  what to put in `/model <ref>`.
- [`docs/tutorials/first-agent.md`](../tutorials/first-agent.md) — step 10 introduces chat with a worked example.
