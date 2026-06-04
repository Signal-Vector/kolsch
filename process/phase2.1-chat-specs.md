# Phase 2.1 Patch — `kolsch chat` (Interactive REPL)

**Draft 0.1**

> The second patch of Phase 2 (interactive agent experience). Phase
> 2.0 ships the four builtin tools, the `prompt` authz effect, and
> the in-chat callback hook — but no UI consumes them. Phase 2.1
> ships the UI: a streaming REPL that turns the engine's
> `execute_stream` event sequence into a Claude-Code-style
> conversation. Token-by-token rendering, collapsible tool-call
> blocks, the four-choice approval modal that finally calls
> 2.0's callback, slash commands, and resumable sessions backed
> by the existing event log.
>
> This patch specifies: (1) the `kolsch chat` command contract,
> (2) the prompt_toolkit + Rich layout (the deliberate MVP — a
> Textual polish pass is a later phase), (3) the event-to-render
> mapping from the engine's stream events to terminal blocks,
> (4) the approval modal that consumes the Phase 2.0 callback,
> (5) the slash-command surface, and (6) the cancellation /
> resume / error semantics.

---

## Part 0 — Why This Patch Exists

After Phase 2.0, the platform has tools and a permission discipline
ready to be used. The way to use them today is `kolsch run agent
--input content="..."` — one-shot, no follow-up, no live feedback.
That's a script harness, not a chat. Phase 1.2's setup-banner
already advertises `kolsch chat` as the natural next step; the
command doesn't exist yet.

Missing today:
- A way to *talk* to an agent — turns, history, continuation.
- A way to see streaming tokens land instead of waiting for a JSON
  blob.
- A consumer for the Phase 2.0 prompt-callback hook. Without it,
  `prompt`-effect rules always resolve to deny (the safe non-chat
  behavior), which means users can't actually `write_file` or
  `bash` without editing YAML.

This patch fills the gap. Headline design decisions:

1. **prompt_toolkit + Rich for the MVP.** Ergonomic input
   (history nav, multi-line, vi/emacs mode) from prompt_toolkit;
   styled rendering (panels, markdown, syntax highlighting) from
   Rich. The full Textual TUI polish is a deliberate later phase.
   This pass ships the interaction model; the polish ships the
   feel.
2. **The chat is a *consumer* of the engine, not a fork.** Every
   visible block in the chat corresponds to an event the engine
   emitted. The chat never invents state or fakes events. If a
   crash or disconnect happens, `kolsch sessions events <id>`
   reconstructs the exact same conversation from the JSONL —
   chat output is a derived view, not a parallel record.
3. **One callback consumed, no second prompt mechanism.** The
   Phase 2.0 callback hook is the only path the chat uses to
   intercept `prompt`-effect calls. No new APIs in Authz; no
   chat-specific authz dispatch.
4. **Slash commands stay small.** Six commands at launch
   (`/exit`, `/help`, `/clear`, `/model`, `/tools`, `/session`).
   New commands are paid for by demand, not anticipation.

---

## Part 1 — The `kolsch chat` Command

### 1.1 Contract

```bash
kolsch chat [<agent_ref>] [--session <SES_ID>] [--model <MODEL_REF>]
            [--root <PATH>] [-v]
```

- `<agent_ref>` — bundle-qualified path (`kolsch/helper`).
  If omitted, defaults to the configured starter agent
  (`platform.yaml`'s `defaults.starter_agent`, falling back to
  `kolsch/helper`).
- `--session <SES_ID>` — resume an existing session. Without
  this flag, chat creates a fresh session.
- `--model <MODEL_REF>` — for this session only, override the
  agent's declared `model:`. Accepts any ModelSpec input shape
  (object, compact string, alias). The override is recorded as
  a session-scoped attribute, NOT a YAML edit. Closing chat
  doesn't persist the override; the agent file is untouched.
- `--root` and `-v` — same semantics as every other subcommand.

### 1.2 Startup flow

1. Resolve the workspace via Phase 1.2's `resolve_workspace`.
2. Bootstrap the runtime (the existing Phase 1.x bootstrap).
3. Resolve `<agent_ref>` and load the agent config via Phase
   1.1's `build_resolved_config`. If `--model` is set, override
   `config.model` after resolution.
4. If `--session`, fetch the session entity from the store;
   otherwise create a new one named `chat:<agent_ref>`.
5. Print the title banner (agent name, model, session id) and
   enter the input loop.

### 1.3 Exit codes

| Code | Meaning |
|---|---|
| 0 | User exited cleanly (`/exit`, EOF, Ctrl-D). |
| 1 | Invalid invocation (missing agent, bad session id, bad model ref). |
| 2 | Engine reported an exchange-level Fatal that chat couldn't recover from. The chat reports it and exits. |
| 3 | Workspace not initialized (per Phase 1.2 §1.4). |

Distinct from `kolsch run`'s codes — chat doesn't surface model
Fatals as exit 2 unless they exhaust the session (i.e., the
agent can no longer be talked to). A single failed exchange
within a chat doesn't end the chat.

---

## Part 2 — The REPL Layout

ASCII sketch of the rendered terminal during a typical session:

```
┌──────────────────────────────────────────────────────────────────────┐
│  kolsch chat • helper • opus • ses_abc123                            │  ← title bar
├──────────────────────────────────────────────────────────────────────┤
│  You: read README.md and tell me what this project is                │
│                                                                      │
│  ▼ read_file(path="README.md")                                       │  ← tool call (collapsed-collapsible)
│    Kolsch is a local-first, definition-orchestrated multi-agent...   │
│    [3.2 KiB read]                                                    │
│                                                                      │
│  Helper: This project is Kolsch — a local-first platform for         │  ← streamed tokens land here
│  defining and running AI agents from YAML. The README explains       │
│  that...                                                             │
│                                                                      │
│  You: ▍                                                              │  ← input prompt (cursor here)
└──────────────────────────────────────────────────────────────────────┘
  /help for slash commands • Ctrl-C to cancel current reply
```

### 2.1 Sections

- **Title bar (top):** agent name • current model • session id.
  Updates when `/model` switches models or `/session` switches
  sessions.
- **Scrollback (middle):** conversation history. Renders newest
  exchange at the bottom; older exchanges scroll up as the user
  continues. **Terminal-native scrolling and text selection
  must work** — see §2.4 for the architectural commitment that
  makes this true.
- **Input prompt (bottom):** multi-line via prompt_toolkit.
  Submit with Enter; **Shift+Enter inserts a newline**. Cursor
  navigation per §2.5.
- **Footer hint (bottom-most line):** one-liner with the most
  useful affordances for the current state ("Ctrl-C cancels"
  during streaming, "/help • Ctrl-D to exit" while idle, or the
  active slash-command's hint per §5.4).

### 2.2 Block types

| Block | Trigger | Style |
|---|---|---|
| User message | `user.message` event | Plain text after "You: " prefix. |
| Agent message | `agent.message` event (final) OR streamed `model.response` tokens (live) | Plain text after "<AgentName>: " prefix. Markdown rendering via Rich for code blocks, lists, tables. |
| Tool call | `tool.requested` event | Rich panel with header `▶/▼ tool_name(arg=val, ...)`; collapsed by default once result lands, expandable via `/expand <n>` (later phase). |
| Tool result | `tool.completed` event | Inlined inside the tool-call block. Truncated to 20 lines by default; "[N more lines, /show <n> for full]" suffix. |
| Tool error | `tool.failed` event | Same block shape as result but red border. Error code + message. |
| Approval modal | Phase 2.0 callback fires | Modal overlay; see §4. |
| System notice | slash-command output, errors | Yellow text, no prefix. |
| Streaming indicator | between `model.request` and first token | Spinner + "Thinking..." in the agent block's spot. |

### 2.3 Rendering discipline

- **Streaming tokens append, never re-render the whole block.**
  Rich's `Live` context manages the in-progress agent message
  block during streaming; once `agent.message` lands, the block
  becomes static and Live exits. **Live MUST scope to the
  current block only** — re-rendering older blocks would break
  scrollback and text selection (§2.4).
- **Tool-call panels never extend past 20 lines without
  collapsing.** Long results truncate with "[N more lines,
  /show <n> for full]" footers. The full content stays in the
  JSONL.
- **Markdown rendering is on by default for agent messages.**
  Code blocks get syntax highlighting (Rich's pygments
  integration). The user can disable with `/markdown off`
  (later-phase polish; not in MVP).
- **No fancy redraws.** Rich handles streaming via Live context
  within a single scrollback append region; prompt_toolkit
  handles the input prompt at the bottom. No full-screen TUI;
  the terminal still scrolls naturally.

### 2.4 Scrolling and text selection (architectural commitment)

The chat MUST stay in prompt_toolkit's **bottom-toolbar mode**
(equivalent to its non-`full_screen` PromptSession idiom). It
MUST NOT enter alternate-screen mode, MUST NOT use prompt_toolkit's
`Application.run(full_screen=True)`, MUST NOT take over the
terminal with `screen.alternate_screen()` or equivalent.

Why this matters:

- **Native scrolling.** Terminal scrollback (mouse wheel,
  trackpad scroll, Page Up/Down, ⌘+↑/↓) only works on output
  written via normal stdout. Full-screen mode and alternate-
  screen mode replace the scrollback buffer; native scroll
  becomes no-op.
- **Native text selection.** Users select text by click-drag
  (or shift+click). This works on the terminal emulator's
  buffered output. Apps that capture the terminal (full-screen
  TUIs) break selection because mouse events go to the app
  instead of the terminal. iTerm2, Terminal.app, gnome-terminal,
  Kitty, Alacritty all break selection inside a full-screen
  TUI.
- **Copy/paste.** Same reason — terminal-native ⌘+C / Ctrl+
  Shift+C operates on the selection. Full-screen apps either
  intercept those keys or render their own selection layer
  (Textual does the latter; prompt_toolkit's bottom-mode
  doesn't need to).

What this constrains:

- Rich `Live` is permitted but ONLY around the in-progress
  agent block. The completed exchanges above must be plain
  Console.print output — text in the terminal's scrollback,
  selectable, scrollable.
- The title bar (§2.1) is a one-line header printed at the
  start of the chat AND on `/clear`. It is NOT a pinned region
  — it scrolls away with the conversation. Users who want to
  see the title scroll up. This is the deliberate trade-off:
  pinning the title would require alternate-screen or a custom
  Rich Layout, breaking scroll.
- The footer hint line (§2.1) IS pinned to the bottom — that's
  what prompt_toolkit's `bottom_toolbar` gives us, and it stays
  with the input prompt, not the scrollback. So selection of
  scrollback text doesn't accidentally select the footer.
- The Phase 2.4 polish phase (Textual) will likely break this
  property in exchange for richer layout. That's an explicit
  trade-off recorded in this patch's Part 10. 2.1 prioritizes
  scroll + selection; 2.4 prioritizes layout. Users get to
  choose by phase.

### 2.5 Input editing and cursor behavior

prompt_toolkit handles input editing natively. Required
key bindings (all standard prompt_toolkit defaults except where
noted):

| Action | Key |
|---|---|
| **Submit** | Enter |
| **Newline (multi-line)** | **Shift+Enter** (primary); Alt+Enter (fallback for terminals that can't distinguish Shift+Enter) |
| Cursor left/right by character | ← / → |
| Cursor by word | Alt+← / Alt+→ (macOS terminal default) OR Ctrl+← / Ctrl+→ |
| Cursor to start/end of line | Home / End (or Ctrl+A / Ctrl+E in emacs mode) |
| Cursor to start/end of input | Ctrl+Home / Ctrl+End |
| Delete character | Backspace, Delete |
| Delete word backward | Ctrl+W |
| Delete to end of line | Ctrl+K |
| Clear input | Ctrl+U |
| Up/Down in prompt history | ↑ / ↓ (at start/end of input only — otherwise navigates within multi-line text) |
| Reverse history search | Ctrl+R |
| Cancel current input | Esc (clears input without submitting) |
| Exit chat | Ctrl+D (EOF), or `/exit` |

**Shift+Enter is the primary newline key**, but several
terminals don't send a distinguishable Shift+Enter sequence by
default — macOS Terminal.app and stock Linux gnome-terminal
both send plain Enter for Shift+Enter unless the user configures
otherwise. To handle this without the user feeling broken:

1. Primary: bind Shift+Enter to insert newline (works in iTerm2
   with default settings, kitty, Alacritty).
2. Fallback: Alt+Enter (Option+Enter on macOS) ALSO inserts
   newline — works universally because Alt+Enter is a distinct
   key sequence on every terminal.
3. The `/help` output names both. The footer hint mentions
   "Shift+Enter (or Alt+Enter) for new line" when input is
   non-empty.

No backslash-continuation. Users who learned `\` + Enter from
shell prompts get nothing special — Enter submits, full stop.
(Reason: a stray backslash in shell-command pastes shouldn't
swallow the next line silently.)

---

## Part 3 — Event-to-Render Mapping

The chat is a pure consumer of `runtime.engine.execute_stream`.
For each event, the renderer does one thing:

| Event type | Render action |
|---|---|
| `thread.exchange_started` | Begin a new exchange render boundary; no UI yet. |
| `user.message` | Print the "You: <content>" block. (The chat already printed the user's input; this event confirms it — if the engine echoes, dedupe.) |
| `model.request` | Show the "Thinking..." spinner in the agent block's position. |
| `model.response.tokens` (streaming) | Replace the spinner with the agent name prefix and append tokens as they arrive. |
| `model.response` (final, non-streaming) | Replace spinner with final content; render markdown. |
| `tool.requested` | If authz returned `allow` directly → print the tool-call block in "▶ running" state. If authz returned `prompt` → halt rendering, show the approval modal (§4), resume after user choice. |
| `tool.completed` | Fill in the tool-call block with the result; collapse to summary. |
| `tool.failed` | Render the block in red; show error code + message. |
| `agent.message` | If the agent message text differs from streamed tokens (post-process), update the block to the final content. Otherwise no-op. |
| `thread.exchange_completed` | Unlock input; restore footer hint. |
| `thread.exchange_failed` | Render a system notice with the failure cause; unlock input. Chat continues. |
| `model.fallback` (Phase 1.3) | Render a small italic notice ("Falling back to <model> because <reason>"). |
| `authz.prompt_requested` | (Diagnostic only — the modal already showed.) |
| `authz.prompt_answered` | (Diagnostic only — the modal's outcome is already applied.) |

The renderer's only state is the current exchange's block list;
everything else is read from events. If a crash happens mid-
exchange and the user resumes the session, the renderer reads
the JSONL and reproduces the conversation up to the failure.

---

## Part 4 — The Approval Modal

The chat is the canonical consumer of the Phase 2.0 callback.
Per Phase 2.0 §4.2, the callback fires when a tool call's authz
check returns `prompt`. The chat's callback implementation:

### 4.1 The modal

```
╭────────────────── Permission requested ──────────────────╮
│                                                          │
│  Tool:     bash                                          │
│  Command:  rg "build_llm" src/                           │
│                                                          │
│  Matched rule:  rl_bash_any  (effect: prompt)            │
│                                                          │
│  [y] allow once       [n] deny once                      │
│  [a] always allow     [d] always deny                    │
│                                                          │
╰──────────────────────────────────────────────────────────╯
```

Rendered via Rich `Prompt` or a small custom Rich layout.
Modal blocks the engine's tool-call dispatch (the callback
doesn't return until the user picks).

### 4.2 Pattern editing on `[a]`/`[d]`

The Phase 2.0 spec (§4.2) lets the user refine the rule's
`what` pattern before committing. In the 2.1 MVP:

- `[a]`/`[d]` → secondary prompt:
  ```
  Pattern for the new rule:
    who:  tool.bash
    what: ^rg .*$    ← editable (Enter accepts, edit with backspace)
    effect: allow / deny
  Apply? [Y/n]
  ```
- The default `what` is computed by the chat's pattern
  suggestor (§4.3). Accept with Enter, abandon with Esc (modal
  closes; the original `[y]`/`[n]` semantics apply for this
  call only — no rule written).

### 4.3 Pattern suggestor

A tiny pure function maps (tool, args) → suggested pattern:

- `tool.bash` with `command="rg foo"` → `^rg .*$` (first
  whitespace-bounded token of the command).
- `tool.write_file` with `path="/abs/path/X.py"` → `^/abs/path/.*$`
  (the path's parent directory).
- Other tools → exact-match on the call's target string.

The user always sees and can edit the suggestion; suggestor
quality doesn't have to be perfect. It exists to remove typing
friction.

### 4.4 The callback's return value

Per Phase 2.0 §4.3, the callback returns a tuple:
`(effect_for_this_call, persist_rule_or_None)`.

The chat's callback:
- `[y]` → `(ALLOW, None)`
- `[n]` → `(DENY, None)`
- `[a]` with pattern P → `(ALLOW, Rule(who=tool_name, what=P, effect=ALLOW))`
- `[d]` with pattern P → `(DENY, Rule(who=tool_name, what=P, effect=DENY))`

Phase 2.0's authz takes care of persisting the rule and
re-evaluating. The chat doesn't write to `platform.yaml`
directly.

---

## Part 5 — Slash Commands

Eight MVP commands. Slash commands are handled by the input
parser before any text goes to the engine — they're chat-local
operations.

| Command | Behavior |
|---|---|
| `/help` | List all slash commands with one-line descriptions. With argument (`/help <command>`), show that command's full hint. |
| `/exit`, `/quit` | Exit cleanly. Same as Ctrl-D. |
| `/new` | Start a fresh session with the same agent and model. Current session is preserved on disk; user can resume later via `kolsch chat <agent> --session <old_id>`. Scrollback clears; title bar updates with the new session id. |
| `/clear` | Clear the visible scrollback. The session and event log are **not** affected — same session continues, history still queryable via `kolsch sessions events`. Different from `/new` (which starts a new session). |
| `/model <ref>` | Switch the active model for this session. The next exchange uses the new model. ModelSpec input shape per Phase 1.1 §2.4. **No separate event** is emitted — the next `model.request` carries `data.model = <new ref>`, so the JSONL is self-documenting at exchange granularity. See §11 open item 7 for the engine-I1 reasoning. |
| `/thinking [on\|off\|budget <n>\|last]` | Multi-form command for extended-thinking models. See §5.2. |
| `/tools` | List the tools the current agent has access to (its `tools:` field) + the rule chain governing each. |
| `/session [<id>]` | With no arg: print the current session id. With arg: switch to a different session (load its history, future exchanges land in the new one). |

### 5.1 What's NOT a slash command

- **Approving/denying tool calls.** That's the modal, not a
  command. Slash commands run when input has the slash prefix
  AND no modal is active.
- **Editing config.** `kolsch permissions {list,add,remove}`
  exists for that, and `/exit; kolsch permissions add ...` works.
  No `/permissions` slash command in MVP.
- **Saving / exporting.** Sessions are auto-persisted via the
  event log. "Save" is not a meaningful operation. `kolsch
  sessions events <id>` exports.

### 5.2 `/thinking` — the multi-form command

Extended-thinking / reasoning support varies by model. `/thinking`
gives the user control over what's shown and (where the model
supports it) how much thinking is allowed.

| Form | Behavior |
|---|---|
| `/thinking` | Print current state: display mode, budget, last-reply thinking availability. |
| `/thinking on` | Show thinking content inline as it streams (renders as a dimmed/italic block above the agent reply). Persists for the chat session. |
| `/thinking off` | Hide thinking content (default). Reasoning events still land in the JSONL; replay can still show them. |
| `/thinking budget <n>` | Set the max thinking tokens for the next exchange (model-permitting; some models don't expose a budget knob). **No separate event** is emitted — connectors that honor the budget echo it into `model.request.data`; connectors that don't print a "not honored" note. Same engine-I1 reasoning as `/model` — see §11 open item 7. |
| `/thinking last` | Show the thinking content from the most recent agent reply, regardless of the current display mode. |

**Engine-side dependency.** `/thinking on/off/last` need the
engine to emit reasoning events distinctly from `model.response`.
Today's engine doesn't. The 2.1 chat ships the command with
graceful degradation:

- If no reasoning events are present in the session, `/thinking
  last` prints "No thinking content in this exchange — your
  current model may not emit reasoning events, or the engine
  doesn't yet expose them."
- `/thinking on` is accepted and persisted as a session-local
  flag; when reasoning events start landing (future engine
  patch), the flag immediately gates rendering.
- `/thinking budget <n>` requires the model adapter to honor
  it. If the connector ignores the field, the command prints
  a note: "Budget set; this model may not honor it."

Why ship the command before the engine work: it locks the UX
shape so future engine patches plug in without re-debating the
surface. The command is small enough that the degraded behavior
is acceptable.

Reasoning event support is a future phase — flagged in Part 10.

### 5.3 Slash-command hints and completion

When the user types `/`, prompt_toolkit shows a completion
dropdown listing all commands with one-line descriptions:

```
/  
├── /help     — show available commands or describe one
├── /exit     — leave the chat
├── /new      — start a fresh session, same agent
├── /clear    — clear visible scrollback (session unchanged)
├── /model    — switch model for this session
├── /thinking — control extended-thinking display and budget
├── /tools    — list tools available to this agent
└── /session  — show or switch current session
```

When the user types `/thinking ` (trailing space), the dropdown
narrows to that command's option list:

```
/thinking
├── on            — show thinking inline
├── off           — hide thinking (default)
├── budget <n>    — set max thinking tokens for next exchange
└── last          — show thinking from the most recent reply
```

For commands with free-form arguments (`/model <ref>`,
`/session <id>`), the dropdown shows suggestions but accepts any
input:

```
/model
├── opus       — openrouter/anthropic/claude-opus-4-7
├── haiku      — openrouter/anthropic/claude-haiku-4-5
├── gpt4o      — openrouter/openai/gpt-4o
└── <type any model ref>
```

Hints come from a static registry (commands + options) and the
runtime (aliases from `platform.yaml`, recent session IDs from
the Store). The registry is the single source of truth for
`/help` AND completion — adding a new command means adding ONE
entry that surfaces in both places.

### 5.4 Footer hint line

While the user is mid-typing a slash command, the footer
displays its usage:

```
> /thinking budg
─────────────────────────────────────────────────────────────
/thinking budget <n>   set max thinking tokens for next exchange
```

While the user is typing a normal message, the footer shows the
default hint ("Shift+Enter for new line • /help • Ctrl-D to exit").
During streaming, the footer shows "Ctrl-C cancels." During the
approval modal, the footer is hidden (the modal is the focus).

---

## Part 6 — Cancellation, Resume, Errors

### 6.1 Cancellation (Ctrl-C during streaming)

User presses Ctrl-C while the agent is generating:

1. The chat sends a cancellation signal to the engine's stream
   (Phase 1's typed cancellation, landed in commit `5d3efde`).
2. The engine emits `thread.exchange_failed` with cause
   `engine.cancelled`.
3. The chat renders the partial agent block in muted styling
   with a "[cancelled]" suffix.
4. Input prompt returns immediately.

Ctrl-C while idle (no streaming) is a no-op (or, for users
unfamiliar with TUIs, a hint: "Use /exit or Ctrl-D to leave").

### 6.2 Resume

`kolsch chat <agent> --session <ses_id>` loads the session.
The chat:

1. Fetches all events for the session's threads.
2. Renders them via the same event-to-render mapping (§3) —
   replaying the conversation into the scrollback.
3. Enters the input loop.

The user sees the same conversation history they had before,
verbatim. Continuation lands on the same thread (or creates a
new one if the agent's `io.mode` allows).

### 6.3 Recoverable errors mid-exchange

`model.rate_limited`, `model.server_error`, `model.timeout` —
the engine emits, Phase 1.3 fallback kicks in if implemented.
The chat renders:

- Each `model.fallback` event as an italic notice.
- `thread.exchange_failed` with `llm.all_models_failed` as a
  system notice in the scrollback. Chat continues.

The user can `/model <other_alias>` and retry, or wait and
re-prompt.

### 6.4 Fatal errors

- `workspace.not_initialized` → chat refuses to start; same
  friendly message as `kolsch run`.
- `llm.provider_not_configured` → printed as a system notice on
  the first exchange that needs the provider; chat continues
  (the user can `/model <other>` to recover).
- Anything that exits the runtime (assertion failure, OOM) →
  chat dies; the session JSONL is intact for inspection via
  `kolsch sessions events`.

---

## Part 7 — Failure Modes

New error codes. Adds §4.17 "Chat" to
`docs/explanation/error-model.md`.

| Code | Level | Detail | Where |
|---|---|---|---|
| `chat.agent_not_found` | Fatal | `{agent_ref}` | Startup, before runtime bootstrap |
| `chat.session_not_found` | Fatal | `{session_id}` | Startup with `--session` |
| `chat.model_override_invalid` | Fatal | `{model_ref, reason}` | Startup with `--model` |
| `chat.input_aborted` | Recoverable | `{}` | Ctrl-C during streaming |
| `chat.callback_resolution_failed` | Fatal | `{tool, reason}` | Modal callback couldn't construct a valid rule (e.g., user typed an invalid regex in pattern edit) |

All Fatals print one short message and exit cleanly — same
discipline as Phase 1.2's "no workspace" message.

---

## Part 8 — Required Spec Revisions

### CHAT-R1. `docs/explanation/tools-subsystem.md`

Add a §X "The chat as canonical callback consumer" cross-reference
pointing at Phase 2.0's §4.2 + this patch's §4. State that
non-chat callers (server, `kolsch run`, tests) must NOT implement
their own modal — they treat `prompt` as deny per Phase 2.0
§2.2.

### CHAT-R2. `docs/explanation/secrets-sandbox-authz-contract.md`

Reference the chat's callback as the reference implementation of
the Phase 2.0 callback. No contract changes; just a "see also"
pointing at this patch.

### CHAT-R3. `docs/explanation/v1-scope.md` §2.4

Add `kolsch chat [<agent_ref>] [--session] [--model]` to the CLI
surface.

### CHAT-R4. `docs/explanation/agent-engine-contract.md`

Note that `execute_stream` events are consumed as-is by the chat
renderer; the renderer adds no semantic layer on top of the
event vocabulary. Cross-reference this patch's §3.

### CHAT-R5. `docs/explanation/error-model.md` Part 4

Add §4.17 "Chat" with the five codes from Part 7.

### CHAT-R6. `docs/reference/module-api-reference.md`

Add Part 11.9 "Chat" covering: the command contract, the
callback consumption pattern, the slash-command surface, the
renderer's pure-consumer property.

### CHAT-R7. `process/development-testing-handbook.md`

Add success criteria §10.16 "Chat REPL":
- Streaming tokens render in real time (no buffer-until-complete).
- Tool-call blocks render before the result lands.
- The Phase 2.0 callback is the ONLY mechanism the chat uses to
  resolve `prompt` effects. No chat-local approval state.
- `/clear` does not modify the session or event log.
- Cancellation via Ctrl-C produces a `thread.exchange_failed`
  with cause `engine.cancelled`.
- `--session <id>` resume produces a renderer state byte-
  identical to what the original chat would have shown (same
  event log → same rendering).

### CHAT-R8. `process/test-catalog.md`

Add Part 9.9 "Chat (CHAT)" with TC-CHAT-01..12 (per Part 9 below).
Also add a CLI smoke test entry (per the
bug-sessions-namespace-attr-error.md hard rule): the chat
command has a `--help` smoke test AND a "minimal-args invocation
with fake LLM" smoke test.

### CHAT-R9. `AGENTS.md`

Add a Hard Rule:

> The chat renderer is a pure consumer of the engine's event
> stream. The renderer MUST NOT invent UI state that isn't
> derivable from events. If a UI element needs information the
> events don't carry, the engine grows the event; the chat
> doesn't fork state.

### CHAT-R10. `README.md` and `docs/tutorials/first-agent.md`

README quickstart points at `kolsch chat <agent>` as the
recommended day-2 command (after `kolsch setup`).
`first-agent.md` gains a step between current 9 and "What you
just saw" titled "10. Chat with your agent" showing a brief
session against the helper.

### CHAT-R11. New how-to: `docs/how-to/chatting-effectively.md`

Recipes: working with long conversations, switching models
mid-session, approving bash patterns wisely, using `--session`
to resume.

### CHAT-R12. `docs/index.md`

Add a pointer to the new how-to.

---

## Part 9 — Test Cases (referenced by CHAT-R8)

Full TC-CHAT-01..12 land in `process/test-catalog.md` Part
9.9. Note: many CHAT tests use prompt_toolkit's testing harness
(it supports headless input feeding) + a Rich console captured
to a buffer. Real TTY isn't required.

- **TC-CHAT-01**: basic exchange. Feed "hello"; assert agent
  block renders streamed tokens; assert `thread.exchange_*`
  events appear in the session JSONL.
- **TC-CHAT-02**: tool-call block. Configure the helper agent
  to invoke `read_file`. Assert the block renders with the
  panel header, then fills with the truncated result.
- **TC-CHAT-03**: approval modal `[y]`. Inject a `prompt`-effect
  rule for `bash`. Feed input that triggers a bash call. Assert
  the modal renders; feed `y`; assert the tool runs and result
  appears.
- **TC-CHAT-04**: approval modal `[a]` writes a persistent rule.
  Same setup; feed `a` then accept the suggested pattern.
  Assert: (a) the tool ran; (b) `platform.yaml` now contains
  the new rule; (c) the next call matching the pattern does NOT
  prompt — runs immediately.
- **TC-CHAT-05**: `/exit` and Ctrl-D both terminate cleanly with
  exit code 0.
- **TC-CHAT-06**: `/clear` clears the visible scrollback but
  the event log is unchanged. Subsequent `kolsch sessions events
  <id>` shows all prior events.
- **TC-CHAT-07**: `--session <id>` replays the existing
  conversation. Render-output byte-identical to original
  session's chat output (modulo timestamps).
- **TC-CHAT-08**: Ctrl-C during streaming. The engine receives
  the cancellation; `thread.exchange_failed` with
  `engine.cancelled` lands; the agent block ends in
  "[cancelled]" styling.
- **TC-CHAT-09**: Tool error renders as a red block. Inject a
  failing tool; assert the block has the error code visible.
- **TC-CHAT-10**: Up-arrow navigates prompt history within the
  current session.
- **TC-CHAT-11**: `/model <other_alias>` switches mid-session.
  Next exchange's `model.request.data.model` reflects the new
  model. **No separate event is emitted** (per §5 row for
  `/model` and §11 open item 7); assert the absence of any
  `session.model_overridden` event.
- **TC-CHAT-12**: `/tools` lists exactly the tools the current
  agent has access to. Tool not in the agent's `tools:` field
  is NOT listed.
- **TC-CHAT-13**: `/new` creates a fresh session under the same
  agent; old session id is recorded; new session id appears in
  the title bar; scrollback is cleared; `kolsch sessions list`
  shows both sessions.
- **TC-CHAT-14**: `/thinking` graceful degradation — no
  reasoning events in session → `/thinking last` prints the
  "no thinking content" note; `/thinking on` is accepted and
  persisted in chat-local state; `/thinking budget 1024` is
  accepted, and either (a) the next `model.request.data`
  carries the budget if the connector honors it, OR (b) the
  chat prints the "not honored" note. **No separate
  `session.thinking_budget_set` event is emitted** (per §5.2
  row and §11 open item 7); assert its absence.
- **TC-CHAT-15**: Slash-command completion. Type `/` →
  dropdown lists all 8 commands. Type `/th` → dropdown narrows
  to `/thinking`. Type `/thinking ` (trailing space) → dropdown
  lists the four options.
- **TC-CHAT-16**: Multi-line input. Feed text via
  prompt_toolkit's test harness with Shift+Enter (or its
  equivalent key sequence in the harness) → input contains a
  literal newline; Enter submits the whole multi-line message
  as one user.message event.
- **TC-CHAT-17**: Cursor editing — feed input "hello", then
  Home, then "say ", then Enter → submitted text is "say hello".
  Verifies prompt_toolkit's editing keys are wired (basic
  smoke; we don't re-test prompt_toolkit's own correctness).
- **TC-CHAT-18**: Scrolling and selection are NOT broken — this
  is verified by an ARCHITECTURAL test (not a runtime
  assertion). The test imports the chat module and asserts:
  (a) no call to `Application.run(full_screen=True)`,
  (b) no call to `enter_alternate_screen()` / equivalent,
  (c) the PromptSession is constructed without
  `full_screen=True` kwarg. If a future change introduces full-
  screen mode, this test fails LOUDLY. The "scrolling actually
  works in iTerm2" verification is a manual end-to-end check
  done by the reviewer, not automated.

Plus the CLI smoke tests per CHAT-R8 / the bug-sessions hard
rule: `kolsch chat --help` (exit 0, non-empty stdout, no
traceback) and `kolsch chat <agent_in_fake_workspace>` minimal
invocation (exits 0 after one fake-LLM exchange).

---

## Part 10 — Explicitly Deferred

1. **Textual TUI polish.** The "nice and responsive" rebuild
   with full-screen layout, mouse support, themes, pane
   management. Own phase later. **Note:** Textual's full-screen
   model will likely break the terminal-native scrolling and
   text selection that 2.1 deliberately preserves (per §2.4).
   The polish phase MUST address how it handles this trade-off
   — either by implementing Textual's own selection layer (it
   has one, partial support varies by terminal), accepting the
   loss, or shipping a non-full-screen mode alongside.
2. **Reasoning / thinking events in the engine.** The
   `/thinking` command (§5.2) ships in 2.1 with graceful
   degradation. Engine-side reasoning event emission (a new
   `model.reasoning` or similar event type, connector parsing
   of provider reasoning output, etc.) is a future phase. When
   it lands, the 2.1 `/thinking on` flag immediately gates
   rendering without any chat changes.
3. **Inline file viewer / project navigator.** A Cursor-style
   side panel. IDE phase.
4. **Multi-agent chat in one session.** Agent-to-agent
   conversations belong to the Workflow Engine phase.
5. **Image rendering in agent messages.** Terminals that support
   it (iTerm2, Kitty) could; not in MVP.
6. **`/replay <event_id>` to branch a session.** Phase 1's
   Session Store contract is single-branch; multi-branch is
   future.
7. **`/dry-run` for tool calls.** "Show me what bash would do
   without running it." Useful but not in MVP — the modal's
   pattern preview is the closest substitute.

---

## Part 11 — Open Items

1. **prompt_toolkit + Rich integration quirks.** Rich's `Live`
   context and prompt_toolkit's full-screen application don't
   compose cleanly out of the box. The chat uses prompt_toolkit
   in "below input only" mode (no full-screen takeover) and
   Rich for everything above. Flagged because the integration
   patterns are well-documented in both libraries' issues; the
   developer should verify the chosen pattern works on macOS
   Terminal, iTerm2, and Linux gnome-terminal at minimum.
2. **Windows ConPTY.** Both libraries support Windows, but
   color and ANSI escape handling differs. MVP supports macOS
   and Linux; Windows is "should work, not tested." Flagged.
3. **Wide-character handling.** CJK characters, emojis, combining
   marks — Rich handles most, but interactions with
   prompt_toolkit's cursor position can be off-by-one on some
   terminals. Flagged for the polish phase.
4. **Cursor placement during streaming.** The agent block grows
   as tokens arrive; the input prompt must stay at the bottom.
   prompt_toolkit's "bottom toolbar" idiom is the right
   mechanism; the implementation must use it correctly.
5. **The session-resume render-cost.** Replaying a 10,000-event
   session takes time. For MVP, accept the latency; later
   phases can collapse old events into a summary block.
6. **`/model` while a fallback chain (Phase 1.3) is mid-stream.**
   Undefined. Decision: ignore `/model` during streaming; require
   the user to wait for the current exchange to complete.
   Flagged.

7. **Session-level events and engine I1.** Earlier drafts of this
   patch promised `session.model_overridden` and
   `session.thinking_budget_set` events when `/model` and
   `/thinking budget` fire. During Step 24e implementation the
   developer correctly flagged that emitting these from chat
   would violate Engine I1 (engine is sole writer of the session
   log). Three resolution paths were considered:

     (a) Carve an I1 exception via `store.append_session_event()`
         — rejected. Sets a "anyone can write to the log"
         precedent that erodes replay reliability.
     (b) Add `engine.emit_session_event(session_id, type, data)`
         — engine remains sole writer; chat calls the engine to
         emit. Clean. Deferred.
     (c) Drop the events entirely; rely on exchange-level events
         being self-documenting (the next `model.request` carries
         the new model and, where supported, the budget) — chosen.

   **Decision:** ship (c) for Phase 2.1. The two events were
   over-specified — they duplicate information already in
   `model.request.data`. `/thinking on/off` is pure UI state and
   never belonged in the log. `/new`'s audit record is the
   session boundary itself.

   **Forward-looking:** when a future phase needs session-level
   events that are NOT auditable via exchange events (Phase 3
   IDE: tags, favorites, archive/unarchive notes), use approach
   (b). Engine I1 holds: the engine remains the sole writer; a
   small `emit_session_event` API extends the engine's role
   without breaking the invariant. Spec when needed.

---

## Part 12 — Status

This is **Phase 2.1 patch draft 0.1**. The patch:

- Closes the no-interactive-agent gap with a streaming REPL.
- Becomes the canonical consumer of Phase 2.0's prompt callback.
- Makes the engine's event stream user-visible without inventing
  new state or vocabulary.

**Apply in two passes:**

1. **Revisions.** CHAT-R1 through CHAT-R12 mechanically edit
   existing docs and create the new how-to.
2. **Implementation.** Add `src/cli/chat.py` (the renderer +
   input loop + slash commands), wire the modal as the Phase 2.0
   callback, register the `chat` subcommand in `src/cli/main.py`,
   add the prompt_toolkit + Rich deps to pyproject.toml, ship
   TC-CHAT-01..12.

After this patch lands, a user runs `kolsch chat` against the
helper agent and has a real conversation, with tool calls
visible, approvals modal'd, and history persistent. That is the
bar.
