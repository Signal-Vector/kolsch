# Phase 2.1 Build Plan — Developer, Reviewer & Coordinator Prompts

**Draft 0.2** *(replaces draft 0.1; splits the monolithic Step 24
into five dependency-ordered sub-steps so each developer prompt is
reviewable in one sitting and each reviewer prompt has ≤8
checks. Step 23 split into two sub-steps along the same line.)*

> Phase 2.1 (`kolsch chat` Interactive REPL) is delivered in
> **seven dependency-ordered steps**: two revision sub-steps
> (23a, 23b) and five implementation sub-steps (24a–24e). Each
> sub-step ships an independently mergeable PR with a focused
> developer prompt, a focused reviewer prompt, and a clear
> acceptance criterion. The coordinator-escalation pattern from
> Phase 2.0 applies across the entire phase — one escalation
> log at the bottom of this doc, shared by all seven sub-steps.

---

## Step dependency graph

```
23a → 23b → 24a → 24b → 24c → 24d → 24e
                                      │
                                      ▼
                              (Phase 2.1 done)
```

| Step | What lands | Lines (est.) |
|---|---|---|
| **23a** | Contract + reference revisions (CHAT-R1..R9) | docs only |
| **23b** | User-facing doc revisions (CHAT-R10..R12) | docs only |
| **24a** | Subcommand scaffold + input loop + key bindings + no-full-screen guard | ~150 |
| **24b** | Slash-command framework + simple commands (`/help`, `/exit`, `/clear`) + completer | ~120 |
| **24c** | Event renderer + engine wiring (real conversations work) | ~180 |
| **24d** | Approval modal + Phase 2.0 callback consumption | ~120 |
| **24e** | Lifecycle commands + streaming UX + remaining commands (`/new`, `/model`, `/session`, `/thinking`, cancellation, errors) | ~160 |

Each step depends only on its predecessor being in main.

---

## Step 23a — Contract and reference revisions (CHAT-R1..R9)

- **Builds:** spec files reflect the patch on the contracts and
  references that other code reads.
- **Depends on:** Phase 2.0 complete and merged.
- **Source of truth:** `process/phase2.1-chat-specs.md` Part 8
  flags R1 through R9.

### Developer prompt

```
[standing instructions]

Step 23a — Apply CHAT-R1 through CHAT-R9. Mechanical revision pass
covering contract cross-references, reference docs, and the
AGENTS.md hard rule.

Read first:
- process/phase2.1-chat-specs.md Part 8 (the nine flag bodies).
- process/phase2.0-tools-build.md Step 21 (precedent).

Apply each flag exactly as specified in Part 8 of the patch:

- CHAT-R1: cross-reference in docs/explanation/tools-subsystem.md.
- CHAT-R2: "see also" in docs/explanation/secrets-sandbox-authz-
  contract.md.
- CHAT-R3: add `kolsch chat` to docs/explanation/v1-scope.md §2.4.
- CHAT-R4: renderer-consumes-events note in docs/explanation/
  agent-engine-contract.md.
- CHAT-R5: §4.17 "Chat" in docs/explanation/error-model.md (5
  codes from patch Part 7).
- CHAT-R6: Part 11.9 in docs/reference/module-api-reference.md.
- CHAT-R7: success criteria §10.16 in process/development-
  testing-handbook.md.
- CHAT-R8: Part 9.9 "Chat (CHAT)" in process/test-catalog.md
  with TC-CHAT-01..18 (per patch Part 9) plus the two CLI smoke
  entries.
- CHAT-R9: Hard Rule in AGENTS.md (the renderer-as-pure-consumer
  rule).

Mechanical only. If a flag is under-specified, escalate per the
"How to escalate" section at the end of this doc — do not guess.

Acceptance: each of R1..R9 applied or escalated; no other file
changed. Commit per flag OR in one "docs(phase2.1): apply R1-R9
revisions" commit, whichever feels cleaner.

Report what changed, per flag.
```

### Reviewer prompt

```
[standing instructions]

Review Step 23a — CHAT-R1..R9 revisions. Mechanical review.

Read first:
- process/phase2.1-chat-specs.md Part 8 (flag bodies R1..R9).
- The escalation log at the end of this build doc.

For each flag R1..R9:
- Confirm the target file was changed (or escalated).
- Confirm the change matches what Part 8 specified.
- Confirm no other file was touched.

Focused checks:
- CHAT-R5: all five codes present with correct levels.
  `chat.input_aborted` is Recoverable; others are Fatal.
- CHAT-R8: TC-CHAT-01..18 carry full Setup/Action/Expected/Verifies
  form. The two CLI smoke entries (`chat --help`, minimal-args)
  are present per the bug-sessions-namespace hard rule.
- CHAT-R9: Hard Rule in the Hard Rules section, names both halves
  (renderer is consumer; engine grows the event if the UI needs
  info events don't carry).

Verdict: PASS or FAIL with specific defects.
```

---

## Step 23b — User-facing doc revisions (CHAT-R10..R12)

- **Builds:** the user-visible doc updates — README, tutorial,
  how-to, index.
- **Depends on:** Step 23a PASS.
- **Source of truth:** `process/phase2.1-chat-specs.md` Part 8
  flags R10 through R12.

### Developer prompt

```
[standing instructions]

Step 23b — Apply CHAT-R10 through CHAT-R12. User-facing doc
updates only — these are read by humans, not by other docs.

Read first:
- process/phase2.1-chat-specs.md Part 8 flags R10..R12.
- The current README.md and docs/tutorials/first-agent.md (both
  will be edited).

Apply each flag:

- CHAT-R10: update README.md (point at `kolsch chat` as the day-2
  command) AND docs/tutorials/first-agent.md (add the
  "10. Chat with your agent" step between current step 9 and
  "What you just saw"). Tutorial steps 0–9 must be byte-identical
  to pre-revision — verify with `git diff` before committing.
- CHAT-R11: CREATE docs/how-to/chatting-effectively.md as a
  recipe-style how-to covering: working with long conversations,
  switching models mid-session, approving bash patterns wisely,
  using `--session` to resume.
- CHAT-R12: add the pointer to docs/index.md.

Acceptance: R10..R12 applied or escalated; tutorial steps 0–9
byte-identical to pre-revision (git diff confirms).

Report what changed, per flag.
```

### Reviewer prompt

```
[standing instructions]

Review Step 23b — CHAT-R10..R12 user-facing revisions.

Read first:
- process/phase2.1-chat-specs.md Part 8 flags R10..R12.

Specific checks:
- README quickstart now references `kolsch chat` for the day-2
  flow.
- docs/tutorials/first-agent.md has a NEW step 10 inserted
  between the existing step 9 and "What you just saw". Use
  `git diff` to confirm steps 0–9 are byte-identical to
  pre-revision. Any drift in those steps is a FAIL.
- docs/how-to/chatting-effectively.md is recipe-style: each
  section is "how to do X" with concrete commands, NOT
  re-explanation of why the architecture is what it is.
- docs/index.md has the new pointer.

Verdict: PASS or FAIL with specific defects.
```

---

## Step 24a — Subcommand scaffold + input loop + key bindings

- **Builds:** the `kolsch chat` command exists and dispatches to a
  module that bootstraps the runtime, loads the agent, opens a
  prompt_toolkit `PromptSession`, echoes input back, and exits
  cleanly. No engine integration yet. No slash commands yet. The
  architectural no-full-screen guard test is in place.
- **Depends on:** Step 23 PASS, Phase 2.0 in main.
- **Spec refs:** patch Parts 1, 2.4, 2.5.
- **Lands at end:** `kolsch chat <agent>` opens a prompt, accepts
  multi-line input, echoes "you said: <text>", exits on Ctrl-D.

### Developer prompt

```
[standing instructions]

Step 24a — Scaffold the chat command and the input loop. No
engine integration yet; no slash commands; no renderer. Just the
command exists, the input loop works correctly, and the
no-full-screen architectural guard is in place.

Read first:
- process/phase2.1-chat-specs.md Parts 1, 2.4 (scrolling
  commitment), 2.5 (input editing).
- src/cli/main.py (the dispatcher you extend).

Deliver:

src/cli/
├── chat.py                      # NEW: cmd_chat entry point +
│                                # the input loop. Keep ONE file.
└── main.py                      # extended with the `chat`
                                  # subparser, dispatching to
                                  # cmd_chat.

pyproject.toml                   # add prompt_toolkit >=3.0.43
                                  # and rich >=13.7.1 to runtime
                                  # deps.

tests/component/cli/
├── test_chat_smoke.py           # CLI smoke entries:
│                                #   - `kolsch chat --help`
│                                #   - `kolsch chat <agent>` minimal
└── test_chat_input.py           # TC-CHAT-16, 17, 18

Implementation requirements:

1. cmd_chat(args):
   - Resolves workspace via Phase 1.2's resolve_workspace.
   - Bootstraps the runtime.
   - Loads agent config via build_resolved_config.
   - Applies --model override if present.
   - Creates a new session (resume support is 24e).
   - Prints the title banner (agent • model • session id).
   - Enters the input loop.
   - For now: each submitted line prints "you said: <text>".
     This stub is replaced by the renderer in 24c.
   - Exits cleanly on Ctrl-D or EOF (return 0).

2. The input loop uses prompt_toolkit's PromptSession with custom
   key bindings per patch §2.5:
   - multiline=True.
   - Enter submits (override prompt_toolkit's default Enter=newline
     for multiline mode).
   - Shift+Enter inserts newline. The portable binding is
     KeyBindings.add('c-j') for Ctrl+J (universal newline key).
     Add a separate Shift+Enter binding for terminals that support
     it (iTerm2, Kitty, Alacritty).
   - Alt+Enter (Esc+Enter) also inserts newline — universal
     fallback.
   - No backslash-continuation.
   - History via InMemoryHistory (per-chat session only).
   - Ctrl-C raises KeyboardInterrupt (loop catches; cancels stream
     in later steps; for now just clears input).
   - Ctrl-D cleanly exits.
   - Default editing keys per patch §2.5 table.

3. NO FULL-SCREEN MODE. Per patch §2.4:
   - NEVER call Application(full_screen=True).
   - NEVER call PromptSession(full_screen=True).
   - NEVER use alternate-screen mode.
   - Use bottom_toolbar for any footer.

4. Tests:
   - test_chat_smoke.py: subprocess.run invocations of `kolsch
     chat --help` (exit 0, no traceback) and a minimal-args
     invocation feeding "/exit" (assuming /exit lands in 24b,
     use Ctrl-D / EOF for now → exit 0, no traceback).
   - TC-CHAT-16: multi-line input via the prompt_toolkit test
     harness with Ctrl-J (or the chosen newline key) inserting
     newline. Submitted text must contain a literal `\n`.
   - TC-CHAT-17: cursor editing — type "hello", Home, type "say ",
     Enter. Submitted text is "say hello".
   - TC-CHAT-18: STATIC analysis test that imports src/cli/chat.py
     and asserts NO occurrence of `full_screen=True`, NO call to
     `alternate_screen`, NO `\033[?1049h` in the module source.
     This is the regression guard per patch §2.4 / Part 10 item 1.

ESCALATION PATH: if prompt_toolkit + Rich integration doesn't
compose cleanly in your environment (especially on macOS Terminal
.app), escalate per "How to escalate" at the end of this doc.

Acceptance: the four tests pass; `kolsch chat <agent>` echoes
input back and exits on Ctrl-D; subprocess smoke test exits 0
with no traceback; coverage for src/cli/chat.py at this step
≥80%.

Commit: feat(cli,chat): scaffold chat subcommand + input loop +
no-full-screen guard (24a).

Report:
- Files added; line counts.
- The specific key-binding choice for Shift+Enter (which terminals
  it works on, which need Alt+Enter or Ctrl-J).
- Any escalations raised.
```

### Reviewer prompt

```
[standing instructions]

Review Step 24a — chat scaffold + input loop.

Read first:
- process/phase2.1-chat-specs.md Parts 1, 2.4, 2.5.

Architectural checks (4):

1. NO FULL-SCREEN MODE. Read src/cli/chat.py. grep the file:
     - `full_screen` — must NOT appear in any constructor.
     - `alternate_screen` / `enter_alternate_screen` — must NOT
       appear.
     - `?1049` — must NOT appear.
   TC-CHAT-18 enforces this; manually verify it actually does by
   adding `full_screen=True` to a PromptSession in a scratch
   commit and confirming the test fails.

2. The chat module is currently ONE file. If 24a already split
   into _renderer.py / _modal.py / etc., that's premature —
   those are for later steps. Fail and request consolidation.

3. The input loop's key binding for newline: confirm both the
   Shift+Enter binding AND the Alt+Enter / Ctrl-J fallback are
   present. A single binding is insufficient because Terminal.app
   doesn't distinguish Shift+Enter.

4. Manual verification on macOS Terminal.app: open the chat,
   type "hello", press Alt+Enter (or whatever the fallback is),
   type "world", press Enter. The echoed output must contain
   `hello\nworld`. If Alt+Enter submits instead, fail.

Functional checks (3):

5. Run TC-CHAT-16, 17, 18. All pass?

6. The CLI smoke tests: invoke via subprocess.run, NOT in-process.
   The minimal-args smoke test should exit 0 cleanly.

7. Mouse scroll works in the actual terminal — open chat, type a
   few lines, scroll back with mouse wheel. The lines must
   reappear from terminal scrollback. If the terminal doesn't
   scroll, the chat is in alternate-screen mode somehow — fail.

Verdict: PASS or FAIL with specific defects.
```

---

## Step 24b — Slash-command framework + simple commands + completer

- **Builds:** a command registry that feeds dispatch, `/help`,
  completion, and footer-hint. Ships the four simplest commands
  (`/help`, `/exit`, `/quit`, `/clear`). The completion popup
  appears when the user types `/`.
- **Depends on:** Step 24a PASS.
- **Spec refs:** patch §5, §5.3, §5.4.
- **Lands at end:** typing `/` in the chat shows all 8 commands
  in the popup (with 4 of them functional, 4 placeholder); `/exit`
  exits; `/clear` clears scrollback; `/help` lists commands.

### Developer prompt

```
[standing instructions]

Step 24b — Add the slash-command framework, the registry, the
completer, and the four simplest commands.

Read first:
- process/phase2.1-chat-specs.md Part 5 (commands), §5.3
  (completer popup), §5.4 (footer hint).
- src/cli/chat.py from 24a.

Add to src/cli/chat.py (or split into a _slash.py submodule ONLY
if the chat module is approaching ~400 lines):

1. The COMMAND_REGISTRY data structure. Single source of truth for
   command metadata. Each entry:
     - name (e.g. "/help")
     - description (one line)
     - arg_shape (None, "free-form", or a list of sub-options)
     - handler (callable or None for placeholders)

   Register all EIGHT commands (per patch §5):
   /help, /exit, /quit, /new, /clear, /model, /thinking, /tools,
   /session. For 24b, only /help, /exit, /quit, /clear have real
   handlers; the others are placeholders that print "not yet
   implemented (Step 24X)" and return CONTINUE.

2. The dispatch function: parses input starting with `/`, looks up
   the command in the registry, calls the handler. Returns a
   control signal (CONTINUE / EXIT / NEW_SESSION / RELOAD).

3. Implement the four handlers:
   - /help: prints the full registry as a table (one row per
     command). With argument `/help <command>`, prints that
     command's hint specifically.
   - /exit, /quit: returns EXIT.
   - /clear: clears the visible scrollback (Rich Console.clear()
     or equivalent ANSI sequence). The session and event log are
     untouched. Patch §5 row 4.

4. The Completer:
   - prompt_toolkit Completer subclass.
   - When the input buffer starts with `/`, return Completion
     objects for each matching command in the registry.
   - When the buffer is `/<command> ` (trailing space), return
     completions for that command's sub-options (only `/thinking`
     has discrete options at this step; `/model` and `/session`
     have free-form args — return "<type any ref>" placeholder).
   - Hook the Completer into the PromptSession.

5. The Footer-hint generator:
   - Pure function: takes the current input buffer + chat state,
     returns a string for the bottom_toolbar.
   - When input is empty: "Shift+Enter for new line • /help •
     Ctrl-D to exit".
   - When input starts with `/<command>`: print the command's
     usage hint (from the registry).
   - Hook into PromptSession's bottom_toolbar parameter.

Tests:
- TC-CHAT-05 (/exit): submit `/exit` → exit code 0.
- TC-CHAT-06 (/clear): clear scrollback; subsequent
  `kolsch sessions events <id>` still shows the history.
- TC-CHAT-15 (completion): feed `/` via prompt_toolkit's harness;
  assert all 8 commands appear in the completer's output. Feed
  `/thinking ` (trailing space); assert exactly the four options
  appear.

Critical behaviors:
- The registry is the SINGLE source of truth. /help reads from
  it. Completer reads from it. Footer-hint reads from it. Adding
  a future command means ONE entry — not three parallel edits.
- /clear must NOT touch the session or event log. Patch §5
  row 4. Verifiable via TC-CHAT-06's assertion.

ESCALATION PATH: if the completer popup conflicts with the input
loop's multi-line handling (some prompt_toolkit configurations
make the popup eat newlines), escalate. Don't ship a degraded
completer.

Acceptance: TC-CHAT-05, 06, 15 pass; /help, /exit, /clear work
in manual testing; the completer popup appears for `/` and
narrows for `/thinking `; coverage stays ≥80%.

Commit: feat(cli,chat): slash command framework + registry +
completer + simple commands (24b).

Report:
- The command registry data structure (paste it in PR).
- Which commands are placeholders ("not yet implemented") at this
  step.
- Any prompt_toolkit completer integration quirks observed.
```

### Reviewer prompt

```
[standing instructions]

Review Step 24b — slash framework + simple commands.

Read first:
- process/phase2.1-chat-specs.md Part 5.

Architectural checks (3):

1. THE REGISTRY IS THE SINGLE SOURCE OF TRUTH. Open the chat
   module. Find the COMMAND_REGISTRY. Then trace:
     - /help's output → must read from the registry.
     - The Completer → must read from the registry.
     - The footer-hint generator → must read from the registry.
   If any of those has a HARDCODED list of commands separate
   from the registry, fail. Adding a future command should
   require one edit.

2. /clear does NOT touch the session or event log. Read the
   handler. Confirm it calls Console.clear() (or similar ANSI)
   and does NOT call any runtime.store method. TC-CHAT-06 enforces
   this; the test must actually assert the JSONL is unchanged
   post-/clear.

3. The four "real" commands have real handlers. The four "placeholder"
   commands return CONTINUE with a "not yet implemented" message —
   verify they don't crash, and the message names which step
   ships them.

Functional checks (4):

4. Run TC-CHAT-05, 06, 15. All pass?

5. Manual: open chat, type `/`. Confirm the popup shows all 8
   commands. Type `/h`. Confirm only `/help` matches. Type `/help
   `. Confirm the popup hides (no sub-options).

6. Manual: type `/thinking ` (trailing space). Confirm the four
   sub-options appear in the popup.

7. The footer-hint updates as the user types. Verify by feeding
   input via the test harness OR by manual observation. Empty
   input → default hint. `/exit` → footer shows `/exit — leave
   the chat` or similar.

Verdict: PASS or FAIL with specific defects.
```

---

## Step 24c — Event renderer + engine wiring

- **Builds:** the pure-consumer renderer that maps engine events
  to terminal blocks. Replaces 24a's echo stub with real
  `runtime.engine.execute_stream` consumption. Tool calls render
  as collapsible blocks. Streaming tokens land live. /tools shows
  the agent's tool list.
- **Depends on:** Step 24b PASS.
- **Spec refs:** patch Part 3, Part 2.2 (block types), §2.3.
- **Lands at end:** real conversations work end-to-end — type a
  message, see streamed agent reply, see tool calls render
  inline.

### Developer prompt

```
[standing instructions]

Step 24c — Replace the echo stub with real engine integration via
the Renderer. The renderer is a PURE CONSUMER of engine events;
this is the load-bearing architectural property of the chat.

Read first:
- process/phase2.1-chat-specs.md Part 2.2 (block types),
  §2.3 (rendering discipline), Part 3 (event-to-render mapping).
- docs/explanation/agent-engine-contract.md §3 (event types).
- AGENTS.md (the CHAT-R9 hard rule from Step 23a).

Add a Renderer to src/cli/chat.py (or split into _renderer.py if
chat.py is approaching ~500 lines):

1. The Renderer class with ONE public method:
       async def render(self, event_stream) -> None:
   Consumes events from runtime.engine.execute_stream and prints
   blocks to the terminal.

2. Per-event handling per patch Part 3 table. Each event type
   maps to ONE rendering action:
   - thread.exchange_started → no-op (silent boundary).
   - user.message → print "You: <content>".
   - model.request → start Rich Live with "Thinking..." spinner.
   - model.response (streaming tokens) → append tokens to the
     in-progress agent block inside Live.
   - model.response (final, non-streaming) → render final content
     (markdown).
   - tool.requested → print tool-call block in "▶ running" state.
     (For 24c, all rules return allow; modal handling is 24d.)
   - tool.completed → fill in the block; collapse to summary.
   - tool.failed → render block in red.
   - agent.message → if differs from streamed tokens, update
     final.
   - thread.exchange_completed → exit Live; unlock input.
   - thread.exchange_failed → print system notice; unlock input
     (chat continues).

3. Rendering discipline (patch §2.3):
   - Rich Live scoped to the IN-PROGRESS agent block ONLY. Exit
     Live before printing the next static block. Live MUST NOT
     span multiple exchanges.
   - Tool-call panels collapse if result is >20 lines.
   - Markdown rendering on by default for agent messages.

4. NO STATE outside the current exchange. The Renderer can be
   re-instantiated per exchange; nothing leaks across.

5. Wire it in: cmd_chat's input loop, on each user submission:
     ctx = ExecutionContext(thread_id, trigger={"kind":
       "user_message", "content": user_text})
     await renderer.render(runtime.engine.execute_stream(config,
       ctx))

6. Implement /tools (was a placeholder in 24b):
   - Reads the resolved agent config's `tools:` field.
   - Prints each tool name + the rule chain currently governing
     it (per patch §5 row 7).
   - For 24c, the rule chain can be a simple lookup against the
     Authz provider's current rule list.

Tests:
- TC-CHAT-01 (basic exchange): feed "hello"; assert the agent
  block contains the fake LLM's response; assert the JSONL has
  the expected event sequence (thread.exchange_started,
  user.message, model.request, model.response, agent.message,
  thread.exchange_completed).
- TC-CHAT-02 (tool call rendering): rig the fake LLM to invoke
  read_file; assert the tool-call block renders with the panel
  header and the truncated result.
- TC-CHAT-12 (/tools): assert /tools output lists the tools the
  agent has access to AND no others.

Critical behaviors:
- THE RENDERER IS A PURE CONSUMER. Every visible block MUST trace
  back to a specific engine event from patch Part 3. No
  `mark_done`, `simulate_event`, or any state-inventing method.
- Replay must use the SAME render code (Step 24e adds the replay
  path; 24c's renderer must already be replay-safe — pure
  consumption of events, no real-time-only assumptions).
- Live MUST NOT span multiple exchanges. Exit Live before
  printing static blocks above it.

ESCALATION PATH: if the engine emits an event type the spec
doesn't cover, escalate. Do not invent rendering logic for
unspecified events.

Acceptance: TC-CHAT-01, 02, 12 pass; manual smoke against the
fake LLM produces a real-looking conversation; coverage ≥80%.

Commit: feat(cli,chat): event renderer + engine wiring (24c).

Report:
- The renderer's method count (should be small — ~12 events × 1
  handler).
- Any event types you handled that aren't in patch Part 3 (these
  are escalations).
- /tools' output format choice.
```

### Reviewer prompt

```
[standing instructions]

Review Step 24c — renderer + engine wiring.

Read first:
- process/phase2.1-chat-specs.md Part 3.
- AGENTS.md (the CHAT-R9 hard rule).

Architectural checks (5):

1. THE RENDERER IS A PURE CONSUMER (load-bearing property).
   Open the Renderer class. Confirm:
     (a) Every visible block traces back to a specific event
         type from patch Part 3.
     (b) NO method named `mark_as_done`, `simulate_event`,
         `pretend_x`, or anything that invents state.
     (c) The render method takes an event_stream parameter ONLY
         (no other state-injection parameters).

2. Rich Live is scoped to the in-progress agent block ONLY. grep
   the renderer for `Live(`. Confirm:
     (a) Live is entered when model.request fires.
     (b) Live is exited before any subsequent static block prints.
     (c) Live does NOT span multiple exchanges.

3. The renderer has NO real-time-only assumption. The 24e step
   will replay events from the JSONL through this same code.
   Read the renderer with a "what if this event came from a file
   instead of a stream" lens. Any `time.sleep()`, any
   `is_streaming()` branch, any "first token vs later tokens"
   distinction that breaks on file-replay → fail.

4. /tools reads from the resolved agent config + the Authz
   provider. Does NOT hardcode any tool list. Confirm by reading
   the handler.

5. Live MUST NOT take over the terminal. The patch §2.4
   commitment from 24a still holds — verify by running the chat
   and confirming scroll and selection still work mid-stream.

Functional checks (4):

6. Run TC-CHAT-01, 02, 12. All pass?

7. Manual: start chat, ask a question, watch the agent reply
   stream. Tokens should land progressively. The input prompt
   should stay at the bottom; tokens shouldn't push it off.

8. Manual: ask the agent to read a file. The tool-call block
   should appear BEFORE the result lands (in "▶ running"
   state), then fill in. The collapsed/expanded state should
   make sense.

9. Manual: scroll up mid-stream. The conversation above must
   still be selectable and scrollable per the §2.4 commitment
   from 24a. If streaming breaks scroll, fail.

Verdict: PASS or FAIL with specific defects.
```

---

## Step 24d — Approval modal + Phase 2.0 callback consumption

- **Builds:** the four-choice modal that fires when authz returns
  `prompt`. Consumes the Phase 2.0 callback hook; never writes
  to platform.yaml directly. Pattern suggestor + secondary
  editable prompt for `[a]`/`[d]`.
- **Depends on:** Step 24c PASS, Phase 2.0 callback hook in main.
- **Spec refs:** patch Part 4 (full).
- **Lands at end:** ask the agent to run `bash`; modal appears;
  press `a` to write a persistent rule; next `bash` call doesn't
  prompt.

### Developer prompt

```
[standing instructions]

Step 24d — Implement the approval modal and plug it into Phase
2.0's callback hook. The chat becomes the canonical consumer of
the callback; non-chat callers continue to treat `prompt` as
deny.

Read first:
- process/phase2.1-chat-specs.md Part 4 (all of it).
- process/phase2.0-tools-specs.md §4.2, §4.3 (the callback
  signature you implement, the rule shape you return).

Add a Modal callback to src/cli/chat.py (or split into a
_modal.py submodule):

1. The Modal callback's signature (per Phase 2.0 §4.3):
       async def __call__(rule_id, tool, target, args) ->
           tuple[AuthzEffect, Rule | None]:
   Returns (effect_for_this_call, persist_rule_or_None) per
   patch §4.4.

2. The modal rendering per patch §4.1:
   - Rich Panel with the modal layout.
   - Shows tool, target/command, matched rule_id.
   - Four choices: [y] [a] [n] [d].
   - Blocks on keyboard input until the user picks.

3. Pattern suggestor (patch §4.3): a small pure function that
   takes (tool, args) and returns a suggested `what` pattern.
   - tool.bash with command="rg foo" → "^rg .*$".
   - tool.write_file with path="/abs/X.py" → "^/abs/.*$".
   - Other tools → exact-match on target string.

4. For [a] and [d] (per patch §4.2):
   - After the first prompt, show a secondary editable prompt
     letting the user accept the suggested pattern (Enter) or
     edit it before committing. Esc abandons the rule write
     (the original [y]/[n] for this call still applies).

5. Callback registration:
   - cmd_chat registers the modal callback with the Authorization
     provider before entering the input loop.
   - On exit (clean or via exception), DEREGISTER the callback.
     This is critical for tests — a leaked callback breaks
     subsequent in-process commands.

6. Critical behaviors:
   - The Modal NEVER writes to platform.yaml directly. It returns
     the Rule object; Phase 2.0's authz persists it.
   - The Modal NEVER calls into src/providers/authz/ except via
     the public callback signature.

Tests:
- TC-CHAT-03 (modal allow): inject a `prompt`-effect rule for
  bash. Feed input that triggers a bash call. Assert modal
  renders; feed `y`; assert the tool runs and result appears.
- TC-CHAT-04 (modal always-allow + persistence): same setup;
  feed `a`, then accept the suggested pattern; assert (a) the
  tool ran, (b) platform.yaml now contains the new rule, (c)
  the next call matching the pattern does NOT prompt.

ESCALATION PATH: if the Phase 2.0 callback signature is
insufficient for what the modal needs (e.g., the modal wants
more context than rule_id/tool/target/args provide), escalate
— do not silently extend the callback shape.

Acceptance: TC-CHAT-03, 04 pass; manual end-to-end: bash call
modal appears; `a` writes a rule to platform.yaml; next bash
matching the pattern runs without prompting; coverage ≥85%
(modal logic must be well-tested because security gate).

Commit: feat(cli,chat): approval modal + callback consumption
(24d).

Report:
- Whether the callback signature was sufficient (no extension
  needed) or whether you escalated.
- The pattern suggestor's heuristics.
- Whether the callback deregistration test (subsequent
  in-process command doesn't see the modal) passes — this is
  load-bearing for tests.
```

### Reviewer prompt

```
[standing instructions]

Review Step 24d — approval modal + callback.

Read first:
- process/phase2.1-chat-specs.md Part 4.
- process/phase2.0-tools-specs.md §4.

Architectural checks (4):

1. THE MODAL CONSUMES THE PHASE 2.0 CALLBACK, NOTHING MORE. Read
   the modal's authz hookup. Confirm:
     (a) It registers via the Phase 2.0 callback mechanism.
     (b) It calls NO functions from src/providers/authz/ other
         than the public callback signature.
     (c) It does NOT write to platform.yaml directly.

2. THE EXIT PATH UNPLUGS THE CALLBACK. Find the cleanup in
   cmd_chat. Confirm the callback is deregistered on clean exit
   AND on exception. Write a test that instantiates chat,
   exits, then starts a non-chat in-process command — the modal
   must NOT pop up.

3. THE PATTERN SUGGESTOR IS A PURE FUNCTION. Read it. No I/O, no
   side effects. Takes (tool, args), returns string. If the
   suggestor reads platform.yaml or any runtime state, fail.

4. THE MODAL'S [a]/[d] PATH: the editable secondary prompt must
   default to the suggestor's output, accept Enter to commit,
   Esc to abandon. Manually test all three.

Functional checks (4):

5. Run TC-CHAT-03, 04. All pass?

6. TC-CHAT-04 specifically: after the test, manually open the
   test's tmp platform.yaml. The new rule must be there with the
   expected `who`, `what`, `effect`. Don't trust just the
   assertion.

7. Manual: ask the agent to `bash ls`. Modal appears. Press `a`.
   Accept the suggested pattern (probably `^ls .*$` or similar).
   Press Enter. The tool runs. Then ask the agent to `bash ls -la`
   — must NOT prompt this time.

8. The callback deregistration: open the chat, run one
   exchange that prompts the modal, exit. Then start `kolsch run
   <agent>` in the same process (or via a test harness that
   doesn't reset state). The modal must NOT appear; `kolsch run`
   should treat `prompt` as deny per Phase 2.0 §2.2.

Verdict: PASS or FAIL with specific defects.
```

---

## Step 24e — Lifecycle commands + streaming UX + remaining commands

- **Builds:** the leftover surface — `/new`, `/model`, `/session`,
  `/thinking` (all forms, with graceful degradation); session
  resume via `--session`; Ctrl-C cancellation; tool-error
  rendering; recoverable-error inline rendering; dynamic
  completions (aliases for `/model`, session IDs for `/session`).
- **Depends on:** Step 24d PASS.
- **Spec refs:** patch §5.2, §6.1, §6.2, §6.3, Part 3 (the
  error events).
- **Lands at end:** all 18 TC-CHAT tests pass; the chat is
  feature-complete for Phase 2.1.

### Developer prompt

```
[standing instructions]

Step 24e — Finish Phase 2.1: lifecycle commands, streaming UX,
remaining slash commands.

Read first:
- process/phase2.1-chat-specs.md §5.2 (/thinking), §6
  (cancellation, resume, errors), Part 3 (error event handling).

Add to src/cli/chat.py (split into more submodules if approaching
600 lines):

1. Session resume per patch §6.2:
   - cmd_chat's --session flag loads the existing session.
   - Read all events for the session's threads in order.
   - Pipe them through the SAME Renderer used for live events.
     No replay-specific code path. The renderer's purity
     (verified in 24c) must already support this.
   - Enter the input loop after replay completes.

2. /new (per patch §5 row 3):
   - Creates a fresh session under the same agent + model.
   - Old session is NOT deleted; the user can resume via
     --session later.
   - Clears scrollback (reuse /clear's logic).
   - Reprints the title bar with the new session id.
   - Records nothing special in either session's event log
     (the new session simply starts; the old session's last
     event remains its last event).

3. /model (per patch §5 row 5):
   - Switches the active model for THIS session only.
   - ModelSpec input shape per Phase 1.1 §2.4.
   - Records a `session.model_overridden` event.
   - Validates the model ref; on invalid, prints error and
     stays in session (does NOT crash).

4. /session (per patch §5 row 8):
   - With no arg: prints the current session id.
   - With arg: switches to a different session (loads its
     history, reprints, future exchanges land in the new
     session).

5. /thinking (per patch §5.2 — multi-form, graceful
   degradation):
   - bare /thinking: print state (display mode, budget,
     last-reply availability).
   - /thinking on / off: set a session-local display flag.
     Persists for this chat session only.
   - /thinking budget <n>: emit session.thinking_budget_set
     event. If the engine adapter doesn't honor budgets, print
     "Budget set; this model may not honor it."
   - /thinking last: scan recent JSONL events for reasoning
     content. If none, print "No thinking content in this
     exchange — your current model may not emit reasoning
     events, or the engine doesn't yet expose them."

6. Ctrl-C cancellation per patch §6.1:
   - Catch KeyboardInterrupt during streaming.
   - Call the engine's typed cancellation hook (Phase 1).
   - Wait for thread.exchange_failed with cause engine.cancelled.
   - Render the partial agent block in muted styling with
     "[cancelled]" suffix.
   - Return control to the input loop.

7. Tool error and recoverable error rendering per patch Part 3
   table:
   - tool.failed → red bordered block.
   - thread.exchange_failed mid-conversation → system notice;
     chat continues.
   - model.fallback (Phase 1.3 if implemented; gracefully
     handle if not) → italic notice.

8. Dynamic completions for the completer (extending 24b):
   - /model: pull aliases from platform.yaml's model_aliases
     section AS suggestions (not closed list).
   - /session: pull recent session IDs from the store as
     suggestions.

9. Tests (the remaining six TC-CHAT cases):
   - TC-CHAT-07 (resume): existing session with events → chat
     replays them, byte-identical output.
   - TC-CHAT-08 (Ctrl-C): feed Ctrl-C via test harness during
     streaming; assert thread.exchange_failed with cause
     engine.cancelled.
   - TC-CHAT-09 (tool error): rig a failing tool; assert red
     block renders with error code.
   - TC-CHAT-11 (/model): switch mid-session; assert
     session.model_overridden event and the next model.request
     carries the new model.
   - TC-CHAT-13 (/new): create new session; assert both
     sessions in kolsch sessions list; assert title bar
     reflects the new id.
   - TC-CHAT-14 (/thinking graceful): verify each form behaves
     per patch §5.2.

ESCALATION PATH: if any of the streaming UX scenarios (e.g.,
tool call mid-token-stream) is undefined by the spec, escalate.
Do not invent UX without coordinator input.

Acceptance: ALL 18 TC-CHAT pass; the two CLI smoke tests pass;
manual end-to-end against a real provider (or fake) is
satisfying; coverage ≥85%; Phase 2.0 component tests still pass.

Commit: feat(cli,chat): lifecycle + streaming UX + remaining
commands (24e).

Report:
- Final file inventory and line counts.
- Coverage report.
- Open questions for Phase 2.4 (the TUI polish phase): what
  did you wish you could do but couldn't, given prompt_toolkit
  + Rich? Concrete examples (e.g., "Textual would let me X").
```

### Reviewer prompt

```
[standing instructions]

Review Step 24e — the Phase 2.1 finale.

Read first:
- process/phase2.1-chat-specs.md §5.2, §6, Part 3.
- The escalation log at the end of this build doc — any open
  escalations? Any resolved escalations whose resolutions
  should be honored here?

Architectural checks (3):

1. SESSION RESUME USES THE SAME RENDER CODE AS LIVE EVENTS. Read
   the resume path. Confirm it constructs the same Renderer and
   pipes events through. If you find a separate "replay"
   render function, fail (patch §6.2 requires single-path).

2. /thinking graceful degradation. Read each of the four forms.
   Confirm /thinking on/off persist a session-local flag (not a
   global one), and that /thinking last prints the
   documented "no thinking content" note when no reasoning
   events exist.

3. Ctrl-C cancellation path. Read the handler. Confirm it calls
   the engine's typed cancellation hook (Phase 1's mechanism),
   NOT a generic SIGINT-then-cleanup. The JSONL must record
   thread.exchange_failed with cause engine.cancelled, NOT
   engine.unknown_error.

Functional checks (5):

4. Run all 18 TC-CHAT plus the 2 CLI smoke tests. All pass?

5. TC-CHAT-07 (replay) specifically: render-output byte-identical.
   Run the test with `pytest -s` and compare. Modulo timestamps
   and rule IDs (deterministic per Phase 2.0 §4.1), the strings
   should match. If they don't, the renderer has a hidden
   live-vs-replay branch.

6. Run /new manually. Confirm:
     (a) scrollback clears.
     (b) title bar shows the NEW session id.
     (c) `kolsch sessions list` shows BOTH sessions.

7. Run /thinking budget 1024. Confirm the
   session.thinking_budget_set event appears in the JSONL.

8. Run /model <bad-alias>. Chat must show an error and stay in
   the session. Crashing is a fail.

The "user experience" review (manual):

9. Start a chat. Run a few exchanges with tool calls and at
   least one approval modal. Press Ctrl-C mid-stream once.
   /new to start fresh. Run another exchange. Exit. Read the
   full transcript. Is it the kind of conversation you'd want
   to scroll back through? If the renders are noisy or
   confusing, raise it as polish-phase feedback (not a Phase
   2.1 fail unless it's actively broken).

10. The developer's "Phase 2.4 open questions" note. Read it.
    Are the questions concrete? Concrete is useful for the
    polish-phase planner.

11. Escalation log review. All resolved? Implementation matches
    resolutions?

Verdict: PASS or FAIL with specific defects citing file + spec
section, plus any unresolved escalations.
```

---

## How to escalate (developer + reviewer)

Either agent escalates by appending an entry to the "Coordinator
Escalation Log" at the bottom of this doc using this template.
After appending, STOP work on the affected item.

```markdown
### Escalation E-2.1-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Phase / Step:** 2.1 / Step <23a|23b|24a|24b|24c|24d|24e>
- **Affected revision flag / spec section:** e.g. CHAT-R4, patch §3
- **Trigger:** what made you stop. Concrete, with file/line citations.
- **Why this is not a guess:** what you tried, what the spec said,
  why the spec doesn't cover the case.
- **Options as you see them:** 2–3 paths forward, with trade-offs.
- **Estimated cost of each option:** rough effort + blast radius.

---

### Coordinator resolution — E-2.1-<NNN>

- **Decided by:** human
- **Decision:** which option, or a fourth path.
- **Rationale (one sentence):**
- **Affects:** which other patches/phases need to know.
- **Resume instruction:** what the developer / reviewer does next.
```

When to escalate:
- prompt_toolkit + Rich integration doesn't compose cleanly in
  your environment.
- The Phase 2.0 callback signature is insufficient for what the
  modal needs.
- A streaming UX choice the spec doesn't cover.
- A Phase 2.0 implementation detail conflicts with what the
  chat needs.
- The engine emits an event type the spec doesn't cover.

When NOT to escalate:
- Exact modal wording. Pick reasonable text; tests assert on
  substrings.
- Color choices, panel widths. Aesthetic decisions live in 2.4.
- Whether to use Rich's `Live` vs `Console.print` for a
  particular block. Implementation detail.

---

## Coordinator Escalation Log

### Escalation E-2.1-001 — `session.model_overridden` / `session.thinking_budget_set` events violate Engine I1

- **Raised by:** developer
- **Phase / Step:** 2.1 / Step 24e
- **Affected revision flag / spec section:** patch §5 (row for `/model`), §5.2 (`/thinking budget`), §6.2 (resume), TC-CHAT-11, TC-CHAT-14.
- **Trigger:** the spec promised two session-level events. Engine
  I1 (`docs/explanation/agent-engine-contract.md` §1) states the
  engine is the sole writer of the session log. Chat code
  appending events directly would violate I1; no
  `engine.emit_session_event()` API exists.
- **Why this is not a guess:** I1's "sole writer" wording is
  explicit; the engine's existing emission paths are all tied
  to an `ExecutionContext` and an exchange lifecycle. There is
  no exchange-less emit API today.
- **Options as I see them:**
  (a) Carve an I1 exception via `store.append_session_event()`.
      Cost: small, breaks the invariant, sets precedent for
      future erosion.
  (b) Add `engine.emit_session_event(session_id, type, data)`.
      Cost: medium — new engine API + spec revision.
  (c) Accept in-memory state only — the next `model.request`
      carries the new model (auditable at the exchange level,
      not at the session-state level).
- **Estimated cost:** (a) ~20 lines + spec change to I1.
  (b) ~50 lines + engine contract revision. (c) zero — already
  shipped this way.

---

### Coordinator resolution — E-2.1-001

- **Decided by:** human
- **Decision:** Ship **(c)** for Phase 2.1. Reject (a)
  permanently. Defer (b) to Phase 3 when there's a real need.
- **Rationale (one sentence):** The two events were over-
  specified — `model.request.data` is the self-documenting
  audit record for `/model` and `/thinking budget`, `/thinking
  on/off` is UI-only state, and `/new`'s audit record is the
  session boundary; no log writes needed.
- **Affects:**
  - `process/phase2.1-chat-specs.md` §5 (row for `/model`),
    §5.2 (row for `/thinking budget`), TC-CHAT-11, TC-CHAT-14,
    and §11 (new open item 7 documenting the decision and the
    forward-looking pointer to approach (b) for Phase 3).
  - No engine contract change. I1 holds verbatim.
- **Resume instruction:** developer's current implementation
  (option c) is final — no rework needed. Re-run TC-CHAT-11 and
  TC-CHAT-14 against the revised assertions (now assert
  `model.request.data` carries the new model AND assert the
  absence of `session.model_overridden` / `session.thinking_
  budget_set` events). Reviewer for Step 24e checks the
  revised spec, not the original.

---

## Notes on running these seven steps

- **Strict order: 23a → 23b → 24a → 24b → 24c → 24d → 24e.** Each
  step is its own PR. Reviewer for step N consults the specs as
  revised by 23a/23b — not the unrevised baseline.
- **The "pure consumer" property (24c Reviewer Q1) is the
  load-bearing architectural test.** If the renderer invents
  state, the chat will drift from the event log over time and
  bugs in one path won't surface in the other. The single
  renderer code path (verified in 24c) is what makes 24e's
  resume work without a separate replay implementation.
- **The Phase 2.0 callback consumption (24d Reviewer Q1–Q2) is
  the load-bearing security test.** If the chat bypasses or
  monkey-patches authz, the rule discipline is broken.
- **Manual end-to-end smoke before declaring 24e done.** Fresh
  workspace via `kolsch setup`; connect a provider; `kolsch
  chat`; have a real conversation with a few tool calls, at
  least one approval modal, one cancellation, one /new, one
  /model switch. The "feels like Claude Code" bar is
  subjective; the reviewer + coordinator agree before merge.
- **MVP scope discipline across all five 24x steps.** No
  Textual, no themes, no mouse, no inline images, no file
  pickers. Those are 2.4.
- **Coordinator escalation is per-phase, not per-step.** The
  escalation log at the bottom of this doc serves all seven
  steps. An escalation raised in 24a may affect 24c; the
  coordinator's resolution lives in one log all agents can
  read.

---

## What this build plan does NOT include

- **Textual TUI polish, themes, mouse support.** Phase 2.4.
- **Conversation memory / compaction.** Phase 2.5.
- **`/replay`, `/branch`, `/dry-run` slash commands.** Out of
  scope per patch Part 10.
- **Multi-agent in one chat.** Workflow Engine phase.
- **The `kolsch chat` extension into HTTP (Slack/Discord bot).**
  Possible later as a consumer of the API + SSE bridge, never
  as core.
- **Inline image rendering.** Out of scope per patch Part 10
  item 5.
