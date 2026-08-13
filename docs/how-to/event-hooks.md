# Event Hooks

Run a shell command when something happens in a session — the classic
use case is a desktop notification when the agent needs your
permission and you've stepped away from the terminal. Modeled on
[Claude Code's hooks](https://docs.claude.com/en/docs/claude-code/hooks)
(shell-out, `matcher` + `command`), not pi.dev's in-process handler
model — kolsch is a Python CLI with no embedded extension runtime, and
a bash command is what was asked for.

Hooks are **notify-only**: a hook can't block a tool call, change an
authz decision, or otherwise affect what the agent does. For that, see
[Permissions](permissions.md).

---

## Quick start

Add a `hooks:` section to `platform.yaml` (top-level, a sibling of
`authorization:` — not nested under it):

```yaml
hooks:
  - event: "kolsch.prompt.requested"
    command: "terminal-notifier -title kolsch -message 'waiting on your approval'"
```

Then **start (or restart) `kolsch chat`** — see the "Changes require a
restart" note below, this is not optional. Once running, the next
time a tool call hits a `prompt` authz rule, the command runs in the
background. No workspace ships hooks by default; an absent `hooks:`
key means zero hooks fire, at zero overhead.

Swap `terminal-notifier` (macOS) for whatever fits your platform —
`notify-send` (Linux), `say` (macOS text-to-speech), a `curl` to a
webhook, an AppleScript one-liner, anything you'd type at a shell
prompt.

---

## The rule shape

Every entry in `hooks:` has four fields:

```yaml
- event: "kolsch.prompt.requested"   # which event fires this rule
  matcher: "*"                       # optional glob against the event's subject (default "*")
  command: "notify-send 'kolsch needs you'"   # the shell command to run
  timeout_seconds: 10                # optional, default 10, capped at 60
```

- **`event`** — the exact event-type string to match (see the table
  below). Required, no default, no glob — this field is compared for
  exact equality, not fnmatched.
- **`matcher`** — a glob (`fnmatch`, same semantics as
  [permission patterns](permissions.md#patterns-globs-not-regex): `*`
  and `?` only, no regex) checked against a per-event **subject**
  string — see the table in "Which events you can hook" below.
  Defaults to `"*"`, which matches every event of that type.
- **`command`** — the shell command, run via `asyncio.create_subprocess_shell`
  (the same primitive the `bash` tool uses). **This is a static
  string** — kolsch does not inject the triggering event's data into
  it. There's no stdin JSON, no environment variable substitution, no
  `{tool_name}`-style templating (unlike Claude Code's hooks, which
  pass event JSON on stdin). If you need to react differently per
  tool, write separate rules with different `matcher` values instead
  of trying to branch inside one command.
- **`timeout_seconds`** — how long the command may run before kolsch
  kills it. Default 10s, hard-capped at 60s (rules with a higher
  value are rejected at config load, not silently clamped). On
  timeout: SIGINT, a 0.5s grace period, then SIGKILL if still alive —
  the same escalation the `bash` tool uses.

Unknown keys are rejected at config load (`extra: forbid`) so a
typo'd field fails fast instead of being silently ignored.

---

## Changes require a restart — `/reload` will NOT pick up hooks

This is different from the rest of `platform.yaml`. Permission rules,
bundle config, workflow policy, and agent config all pick up edits via
[`/reload`](permissions.md#reload-pick-up-rule-edits-without-restarting)
without leaving the chat session. **`hooks:` is the one section that
does not** — it's read exactly once, at process bootstrap, and
`/reload` deliberately never re-reads or re-applies it, even though it
re-reads the same `platform.yaml` file for everything else.

This is intentional, not an oversight: hooks execute arbitrary shell
commands, and kolsch's own agents can write files (including
`platform.yaml`) by default. If hook config lived-reloaded the same
way permission rules do, a model-driven `write_file` call could append
a malicious `hooks:` entry and have it start firing before you ever
see it. Keeping hooks read-once means an edit to `hooks:` — whichever
process wrote it — only takes effect on the **next full process
restart**, giving you a natural checkpoint to notice the change.

**In practice:** after editing `hooks:` in `platform.yaml`, exit and
restart `kolsch chat` (or start a fresh `kolsch run`) — don't rely on
`/reload`.

---

## Which events you can hook

| `event` | Fires when | What `matcher` matches against |
|---|---|---|
| `kolsch.prompt.requested` | A tool call hits a `prompt` authz rule and the approval modal is about to show — the "notify me before I approve" case. | The tool's 3-segment ref (e.g. `kolsch/tools/bash`), or `*` if the payload didn't carry one. |
| `tool.failed` | Any tool call fails — denied by authz, raised an exception, or otherwise errored. | The failing tool's name. |
| `thread.exchange_completed` | An agent exchange finishes successfully. | Always `*` — no natural subject. |
| `thread.exchange_failed` | An agent exchange fails (pre-flight denial, unhandled error). | Always `*`. |
| `kolsch.prompt.answered` | Narrow, **not recommended** — see the warning below. | Usually `*`; real payloads rarely carry a tool name to match against. |

A rule matching `kolsch.prompt.requested` with a `matcher` narrower
than a real tool ref (e.g. `kolsch/tools/bash`) only fires for that
specific tool's prompts — use this to notify only on the tool calls
you actually care about, e.g. skip notifications for a tool you
always `allow` anyway.

**`kolsch.prompt.answered` is a trap — avoid it in v1.** Setting a
non-`*` `matcher` on this event produces a rule that can *never*
fire (real answered-payloads don't carry the field being matched);
kolsch logs a one-time warning at startup naming the dead rule and
suggesting `kolsch.prompt.requested` instead. Use `matcher: "*"` if
you specifically want a "something was answered" notification —
but note the interactive gap below first.

---

## What doesn't work yet (documented v1 gaps)

Two cases structurally can't fire hooks today — not a bug, a known
limitation:

- **A session opening** (`session.opened`) — the session store writes
  this event directly, bypassing the queue every other hook dispatch
  goes through. There's no "notify me when a new session starts"
  hook yet.
- **Answering an approval by clicking the in-chat modal** — the
  interactive `[y]/[a]/[n]/[d]` widget also writes directly to the
  store, bypassing the same seam. The one `kolsch.prompt.answered`
  case that DOES work is narrow: an in-workflow approval that times
  out and auto-denies (bug-114's path). A human clicking through the
  modal does not currently trigger a hook.

**Hooks only fire in `kolsch chat` today.** Headless `kolsch run`,
`author_workflow`, and the HTTP API never reach a pending-prompt
state at all (`prompt` collapses to a hard `deny` for any non-chat
caller with no registered callback) — this is a pre-existing
limitation of the approval system, not specific to hooks, but it
means `kolsch.prompt.requested` hooks are chat-only in practice.
`tool.failed` and `thread.exchange_completed`/`thread.exchange_failed`
fire correctly across every surface (chat, `run`, sub-agent/workflow
runs, the HTTP API) since those flow through the engine's own event
queue.

---

## Writing safe hook commands

- **The command inherits kolsch's full process environment**,
  unmodified — same posture as the `bash` tool. If your shell profile
  or environment carries secrets, a hook command can see them. Don't
  write a hook that echoes environment variables somewhere you don't
  trust.
- **No sandboxing, no authz check.** A hook command is not a tool
  call — it doesn't go through the permission system at all. Anything
  you'd type at a shell prompt is fair game; be as careful with
  `hooks:` entries as you would be running the command yourself.
- **Keep it fast.** Hooks run fire-and-forget in the background and
  never block the agent, but a slow command still burns the timeout
  window before being killed. A notification call should return in
  well under a second; don't put a long-running job directly in
  `command` — have it kick one off and return instead (`nohup ... &`,
  or a webhook call to a queue).
- **Test the command in a real shell first.** Since kolsch passes no
  event data into it, if `terminal-notifier -message '...'` doesn't
  work when you paste it into a terminal, it won't work as a hook
  either.

---

## Debugging: my hook isn't firing

1. **Check the event/matcher shape.** `event` is an exact string
   match — `kolsch.prompt.requested`, not `prompt.requested` or
   `kolsch/prompt/requested`. `matcher` is a glob against the
   *subject* (see the table above), not against the whole event.
2. **Confirm you're in `kolsch chat`.** `kolsch.prompt.requested`
   hooks can't fire outside it (see "What doesn't work yet" above).
3. **Check kolsch's log output** for lines from the
   `platform_runtime.hooks` logger — a hook whose command exits
   non-zero logs the exit code and a truncated tail of its output;
   a dead `kolsch.prompt.answered` rule logs a warning at startup.
4. **Run the command by hand** in the same shell kolsch runs in, to
   rule out a `$PATH`/permissions issue that has nothing to do with
   hooks at all.

---

## Where next

- [Permissions](permissions.md) — the approval system `kolsch.prompt.requested`
  fires alongside; hooks notify, permission rules decide.
- [Schemas reference](../reference/schemas.md) — the `HookRule` model.
- [Workspace and setup](workspace-and-setup.md) — where `platform.yaml`
  lives and how to reload config without restarting.
