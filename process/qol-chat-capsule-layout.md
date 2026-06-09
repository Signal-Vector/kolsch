# QoL — Chat capsule layout: side-bar accent for all messages

**Status: shipped** — closed by commit `f669246` ("capsule rendering per qol-chat-capsule-layout.md"). Verified during the 2026-06-08 audit.

## Context

The current `kolsch chat` renderer outputs prose-style headers
(`You: <text>`, `helper: <text>`) plus boxed Rich panels for
tool calls and the permission modal. Three issues with this mix:

1. **No visual continuity between message types.** User/agent are
   prose; permission is a closed box; tool calls are closed boxes
   in their own style. The eye has to re-parse each block's shape
   to identify its source.
2. **The `_THINKING_INDICATOR = "  …"` at `_renderer.py:79`** fires
   on every `model.request` event, producing noise in tool-call
   flows (1 indicator per model decision; multi-tool exchanges
   show 3-4 indicators). The user's complaint that finally closed
   the CrewAI-stdout investigation (E-BUG-CREWAI-005).
3. **Closed-border boxes interrupt scroll and text selection.**
   Selecting an agent reply across border characters is ugly.

This QoL ships a unified side-bar capsule layout where every
message type (user, agent, system, permission, tool) uses the
same primitive — a colored vertical bar (`│`) with a `source ·
timestamp` header — and color is the only signal that
differentiates them. No closed borders anywhere except code
blocks (which use a thicker bar `┃` to signal "structured
payload, not prose"). The thinking indicator is removed entirely.

**This QoL supersedes PR G2 of `bug-crewai-stdout-leak-investigation.md`**
(the "fire indicator once per exchange" plan). The new
renderer doesn't emit the indicator at all. PR G1 of that bug
(commit the working-tree CrewAI wrap) is unchanged and still
ships separately.

---

## Visual contract

The final mockup the coordinator approved (mirrored here for the
implementing dev's reference):

```
kolsch chat • helper • opencode/deepseek-v4-pro • ses_b619d7f5…
─────────────────────────────────────────────────────────────────

│ system · 14:20:15                                  ┃ DARK GRAY
│ Resumed session ses_b619d7f5… (12 prior events)


│ You · 14:23:01                                     ┃ BLUE
│ Can you show me a Python function that says hello?


│ helper · 14:23:04                                  ┃ GRAY
│ Sure — here's a simple function:
│
│ ┃ python                                           ┃ CYAN (code)
│ ┃ def hello():
│ ┃     return "world"
│
│ It defines `hello()` that returns "world".


│ You · 14:23:42                                     ┃ BLUE
│ run that in my workspace


│ permission · 14:23:44                              ┃ ORANGE
│ bash(command='python -c …')  —  matched rl_bash_any
│
│ [y] once    [a] always allow this
│ [n] deny once    [d] always deny
│
│ ▍


│ tool · 14:23:48 → 14:23:50 (1.8s)                  ┃ CYAN
│ bash(command='python -c …')
│   world


│ helper · 14:23:51                                  ┃ GRAY
│ Ran successfully — output was `world`, exactly as expected.


│ system · 14:25:01                                  ┃ DARK GRAY
│ Exchange failed: llm.authentication_failed
│ The OPENROUTER_API_KEY may be stale. Try kolsch connect openrouter.

─────────────────────────────────────────────────────────────────
Shift+Enter for new line • /help • Ctrl-D to exit • Ctrl-C cancels

You: ▍
```

---

## Color palette (Rich names — final)

| Element | Rich color | Notes |
|---|---|---|
| User bar + "You" label | `bright_blue` | High contrast |
| Agent bar + agent-name label | `grey70` | Neutral |
| System bar + "system" label | `grey42` | Quiet |
| Permission bar + "permission" label | `dark_orange3` | Attention without alarm |
| Tool bar + "tool" label | `cyan3` | Distinct from conversation |
| Code block bar (`┃`) + language label | `cyan` | Brighter than tool; signals "structured" |
| Timestamps (all sources) | `grey50` | Always dim |
| Failed-tool marker | `cyan3 dim` | "Happened, didn't work" — not alarm |

Seven roles, six colors. Tool and code share cyan family but at different brightness; `┃` vs `│` character difference makes them unambiguous.

---

## Per-element specs

### Capsule primitive (the foundation)

A capsule is rendered as:

```
│ <source> · HH:MM:SS                              [color-of-source]
│ <content line 1>
│ <content line 2>
│ ...
```

- Bar character: `│` (U+2502) for conversation/system/permission/tool. `┃` (U+2503) for code blocks inside agent capsules.
- Bar color: per-source from the palette table above.
- Header format: `<source> · HH:MM:SS`. Source label in source color; timestamp in `grey50`.
- Content lines: each line prefixed `│ ` then the content. No closing border. Blank line between capsules; the bar does NOT bridge two capsules.

### User capsule

```
│ You · HH:MM:SS                                     [bright_blue]
│ <user's input, preserving newlines for multi-line>
```

### Agent capsule

```
│ <agent-name> · HH:MM:SS                            [grey70]
│ <agent's reply, markdown-rendered>
```

The agent's display name comes from the resolved agent config (`config.name`), not the agent_id. Falls back to "agent" if name absent.

### System capsule

Used for:
- Session resume: `Resumed session ses_… (N prior events)`
- Model switches: `Switched model to <ref>`
- `/clear`: `/clear — scrollback cleared (session continues; N events on disk)`
- `/new`: `/new — started fresh session ses_… (previous: ses_…)`
- Exchange failures: `Exchange failed: <code>` + actionable hint on next line
- Permission rule writes: `Added rule: <effect> <who> matching <what>`
- Tool truncation reminders: occasionally — see Tool capsule below

```
│ system · HH:MM:SS                                  [grey42]
│ <message, one or more lines>
```

### Permission capsule

Replaces the current Rich Panel modal. Inline; capsule IS the prompt.

```
│ permission · HH:MM:SS                              [dark_orange3]
│ <tool>(<arg-summary>)  —  matched <rule_id>
│
│ [y] once    [a] always allow this
│ [n] deny once    [d] always deny
│
│ ▍
```

User keystrokes land at the `▍`. Footer hint updates to `[y/a/n/d] then Enter • Esc to cancel`.

After the user chooses `y` / `n`, the capsule's cursor line is replaced:
```
│ ▸ y — allowed once
```

After `a` / `d`, a follow-up pattern-edit appears as an additional line inside the capsule:
```
│ ▸ a — write a rule
│   pattern (Enter to accept, edit to refine):
│   tool.bash | ^python -c .*$
│   ▍
```

Single-line edit field for now (scrolls horizontally if the pattern exceeds terminal width); multi-line wrap deferred per coordinator.

After commit:
```
│ ▸ a — write a rule
│   matched pattern: ^python -c .*$
│   rule rl_3f7a2b1c written; next matching call allowed.
```

The capsule is now static and complete — fully recorded for replay.

### Tool capsule (evolving — Rich `Live` in place)

One capsule, two states.

**While running:**
```
│ tool · HH:MM:SS                                    [cyan3]
│ <tool>(<arg-summary>)
│   ▍ running
```

**When completed (Live updates the same capsule):**
```
│ tool · HH:MM:SS → HH:MM:SS (Ns)                    [cyan3]
│ <tool>(<arg-summary>)
│   <result lines>
│   [N more lines, /show <id> for full]    (if truncated)
```

**When failed:**
```
│ tool · HH:MM:SS → HH:MM:SS (Ns)  failed            [cyan3 dim]
│ <tool>(<arg-summary>)
│   <error_code>: <message>
```

The header gains `→ <end-time> (<duration>)` on completion or failure; `failed` suffix on failure; the whole bar dims on failure to signal "transactional record, this didn't succeed."

Truncation footer `[N more lines, /show <id> for full]` appears as the last line of content when result lines exceed 20 (per Phase 2.0 §1.2 read_file truncation discipline; same threshold for all tool outputs).

### Code block (inside agent capsules)

```
│ <agent-name> · HH:MM:SS                            [grey70]
│ <prose before>
│
│ ┃ <language>                                       [cyan]
│ ┃ <code line 1>
│ ┃ <code line 2>
│
│ <prose after>
```

Thicker bar (`┃`) + cyan color + language label. Rich's existing Pygments-based syntax highlighting renders inside; the `┃` prefix is added per content line.

### Title bar + footer

**Title bar** at top, plain text, scrolls with content (no pinning — per §2.4 commitment to native scrolling):
```
kolsch chat • <agent-name> • <model> • <session_id>…
─────────────────────────────────────────────────────────────────
```

Re-prints on `/clear` (immediately after the system notice that records the clear).

**Footer hint** via prompt_toolkit's `bottom_toolbar`. Updates contextually per Phase 2.1 §5.4 (unchanged behavior; no work here other than verifying no thinking-indicator hint sneaks in).

---

## Critical files

- `src/cli/_renderer.py` — primary rewrite. Source-specific handlers; new capsule primitive; F5 indicator removal.
- `src/cli/_styles.py` (new) — color/style constants module; single source of truth for the palette.
- `src/cli/chat.py` — minor: ensure title bar prints once at start and on `/clear`; modal callback's "render the permission UI" handler becomes a capsule-emit instead of a Panel.
- `tests/component/cli/test_chat_renderer.py` — new tests pinning capsule structure per-source.

Estimated total: ~250 LOC code + ~150 LOC test across all five steps.

---

## Step dependency graph

```
Step 1 (foundation: _styles + capsule primitive + F5 removal)
    │
    ▼
Step 2 (source renderers: user/agent/system + title/footer)
    │
    ▼
Step 3 (tool capsule — evolving in place via Rich Live)
    │
    ▼
Step 4 (permission capsule — inline replacing the Panel modal)
    │
    ▼
Step 5 (code block ┃ styling)
    │
    ▼
Step 6 (regression tests + manual verification)
```

Linear chain. Each step lands as its own PR; intermediate state at each step is at-worst "the chat works but some message types haven't migrated to the new style yet" — never broken, just half-migrated. Total: 6 PRs, sized 50-100 LOC each.

---

## Step 1 — Foundation: styles + capsule primitive + F5 removal

### Developer prompt

```
[standing instructions]

Step 1 — Build the foundation: color/style constants, the
capsule primitive, and remove the thinking indicator.

Read first:
- process/qol-chat-capsule-layout.md (this doc, in full).
- src/cli/_renderer.py (the file you're refactoring).

Tasks:

1. Create src/cli/_styles.py with the seven color constants
   per the palette table above:
     USER_COLOR = "bright_blue"
     AGENT_COLOR = "grey70"
     SYSTEM_COLOR = "grey42"
     PERMISSION_COLOR = "dark_orange3"
     TOOL_COLOR = "cyan3"
     CODE_COLOR = "cyan"
     TIMESTAMP_COLOR = "grey50"
   Plus the bar characters: BAR = "│", CODE_BAR = "┃".

2. Add a render_capsule(source, timestamp, content_lines,
   color, bar_char="│") primitive in _renderer.py (or a new
   _capsule.py if it grows). Emits:
     │ <source> · <HH:MM:SS>
     │ <line 1>
     │ <line 2>
     │ ...
   With ANSI color codes (via Rich's Console) applied to the
   bar + source label. Timestamp in TIMESTAMP_COLOR.

3. REMOVE the _THINKING_INDICATOR constant and any code that
   emits it. Specifically:
   - src/cli/_renderer.py:79 _THINKING_INDICATOR = "  …" — delete.
   - The handler for model.request events should no longer
     emit anything to the terminal. (It may still observe the
     event for state tracking, but emit nothing.)

4. No source-specific renderers yet (Step 2). After Step 1
   lands, chat output looks UNCHANGED to the user — the
   primitive exists and the indicator is gone, but the
   existing prose handlers still emit "You: <text>" etc.

5. Tests in tests/component/cli/test_chat_renderer.py:
   - Test render_capsule with a fake source/timestamp/content;
     assert the bar char is present on every line including
     the header, source label is colored correctly (assert
     ANSI escape sequence presence), timestamp present.
   - Test that no thinking indicator appears in captured
     output for any sequence of model.request events.

Scope: ~50 LOC code + ~40 LOC test.

ESCALATION PATH: if Rich's Console doesn't render `grey70`
or `dark_orange3` correctly in the dev's terminal (some
older terminals lack 256-color support), escalate —
coordinator may need to provide fallback colors.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1 — foundation.

Checks:

1. _styles.py exists with all seven constants. Single source
   of truth — no color literals scattered through _renderer.py.

2. render_capsule is a single function (not a class). Simple
   signature. No state.

3. The thinking indicator is GONE. grep _renderer.py for
   "_THINKING_INDICATOR", "…", "thinking" — should return
   zero matches in active emission paths.

4. After this PR lands, chat output is byte-identical to
   before EXCEPT no more "  …" lines. The primitive exists
   but isn't called yet by source-specific renderers.

5. Tests verify ANSI escape presence (so renderer is actually
   coloring), not just text content.

Verdict: PASS or FAIL.
```

---

## Step 2 — Source renderers: user/agent/system + title/footer

### Developer prompt

```
[standing instructions]

Step 2 — Wire the capsule primitive to user, agent, and system
event handlers. Print the title bar at session start and on
/clear. Footer hint stays as-is.

Read first:
- Step 1's _styles.py and render_capsule.
- process/qol-chat-capsule-layout.md §Per-element specs
  for user/agent/system.

Tasks:

1. Update _renderer.py's event handlers:
   - user.message → render_capsule("You", event.created_at,
     [content], USER_COLOR)
   - agent.message → render_capsule(<agent.name>,
     event.created_at, content_lines, AGENT_COLOR)
   - thread.exchange_failed → render_capsule("system",
     event.created_at, [<formatted error>], SYSTEM_COLOR)

2. Add system-notice rendering for the five trigger cases:
   - Session resume (one-time, at chat start if --session)
   - Model switch (after /model)
   - /clear confirmation
   - /new confirmation
   - Permission rule write (after [a]/[d] commits a rule)

3. Title bar printing:
   - At session start: print "kolsch chat • <agent> •
     <model> • <session_id>…" followed by a horizontal rule.
   - On /clear: clear scrollback (existing behavior), then
     re-print the title bar.

4. REMOVE the old "You: <text>" / "<agent>: <text>" prose
   emission. The capsule replaces it.

5. Tests in test_chat_renderer.py:
   - Test that a sequence (user.message, agent.message,
     thread.exchange_completed) produces two capsules with
     the correct sources, colors, timestamps.
   - Test that thread.exchange_failed produces a system
     capsule with the error code visible.
   - Test that /clear emits a system capsule before clearing
     scrollback and that the title bar reprints after.

Scope: ~70 LOC code + ~50 LOC test.

ESCALATION PATH: if an existing test asserts on the old
prose format ("You: hello"), update those tests to match the
new capsule output. If the assertion is in a test that
covers something other than renderer output, escalate to
clarify.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2 — source renderers + title/footer.

Checks:

1. User/agent/system message handlers use render_capsule.
   No more "You: " or "<agent>: " prose emission.

2. All five system-notice cases trigger correctly. Walk
   through each manually or via test fixtures.

3. Title bar prints at session start AND on /clear. Verify
   in a manual chat session.

4. Existing tests that asserted on prose format have been
   updated to match the new capsule output. The TC-CHAT
   tests from the test catalog should still pass with
   updated assertions.

5. Footer hint behavior is unchanged. No accidental
   "thinking indicator" hint added.

6. Manual: run `kolsch chat`, send a message, observe the
   capsule. Send /clear, observe the system capsule + title
   bar reprint. Use /model to switch, observe the system
   capsule.

Verdict: PASS or FAIL.
```

---

## Step 3 — Tool capsule (evolving via Rich Live)

### Developer prompt

```
[standing instructions]

Step 3 — Replace the two-panel tool-call rendering with a
single evolving capsule. Use Rich Live to update in place.

Read first:
- process/qol-chat-capsule-layout.md §Tool capsule.
- Existing tool-panel rendering in _renderer.py (the
  ╭─ ▶ tool_name ─╮ and ╭─ ▼ tool_name ─╮ blocks).

Tasks:

1. Remove the old tool-panel rendering (the two-Panel pattern).

2. New rendering via render_capsule + Rich Live:
   - On tool.requested: start a Live region; render capsule
     with "tool · <start_time>" header and the running marker.
   - On tool.completed: Live updates the capsule — header
     gains "→ <end_time> (<duration>)", content shows the
     result (truncated to 20 lines with the [N more lines,
     /show <id> for full] footer if longer).
   - On tool.failed: same update path; header gains "failed";
     bar color dims to "cyan3 dim"; content shows the error
     code + message.

3. Live's refresh interval: 4-10Hz is fine. Same pattern as
   the existing agent-message streaming.

4. Tests:
   - Test the running → completed transition: run a sequence
     of (tool.requested, tool.completed) events through the
     renderer; assert the captured output shows the header
     evolving + the result content.
   - Test the failed transition: (tool.requested, tool.failed)
     → header has "failed", bar is cyan3-dim, error code
     visible.
   - Test truncation: feed a tool.completed with 50 lines of
     output; assert exactly 20 + the [N more lines] footer.

Scope: ~80 LOC code + ~50 LOC test.

ESCALATION PATH: if Rich Live doesn't compose cleanly with
the side-bar style (e.g., Live's region-update flickers
with the bar character), escalate — the alternative is to
render the running state as a static capsule and replace it
with a fresh static capsule on completion (no Live).
```

### Reviewer prompt

```
[standing instructions]

Review Step 3 — tool capsule.

Checks:

1. Old two-Panel pattern is gone. grep _renderer.py for
   "Panel" — should not appear in tool-call paths.

2. Single capsule per tool call. The running and completed
   states share the same Live region.

3. Failure marker is "failed" suffix + cyan3-dim bar (per
   coordinator's earlier confirm). Not red.

4. Truncation works at the 20-line threshold.

5. Manual: run a chat that triggers a tool call (e.g., ask
   the helper to list files). Watch the capsule evolve.
   Confirm no flicker, header updates smoothly, result fills
   in cleanly.

Verdict: PASS or FAIL.
```

---

## Step 4 — Permission capsule (inline, replaces Panel modal)

### Developer prompt

```
[standing instructions]

Step 4 — Replace the Rich Panel modal with an inline capsule
that doubles as the interaction surface.

Read first:
- process/qol-chat-capsule-layout.md §Permission capsule.
- Phase 2.0 callback signature in
  bug-chat-permissions-and-console-leak.md §4.

Tasks:

1. Remove the existing Panel-based modal rendering in the
   chat's permission-callback handler.

2. New rendering:
   - Emit a permission capsule with the tool/command/rule_id
     line + the four choices + a cursor line (▍).
   - prompt_toolkit handles the keystroke capture; the cursor
     line represents where input lands.
   - Footer hint updates to "[y/a/n/d] then Enter • Esc to
     cancel" while the capsule is active.

3. On user choice:
   - [y]/[n]: emit "▸ y — allowed once" or "▸ n — denied
     once" as the next content line of the capsule. Capsule
     becomes static.
   - [a]/[d]: emit "▸ a — write a rule" then a follow-up
     edit field. Single-line, scrolls horizontally for now
     (per coordinator). On Enter: capsule shows "rule
     <id> written; …".

4. The capsule remains visible in scrollback as the
   permanent record of "what was requested, what was chosen,
   what was persisted." Replay shows it identically.

5. Tests:
   - Test [y] path: feed a permission-callback invocation,
     simulate 'y' input via prompt_toolkit's test harness;
     assert the capsule contains "▸ y — allowed once".
   - Test [a] path: same setup, simulate 'a' then accept the
     suggested pattern; assert (i) the capsule shows the
     pattern + rule_id, (ii) the next matching call in the
     same session doesn't trigger a new permission capsule
     (Phase 2.0 callback persists the rule).

Scope: ~60 LOC code + ~50 LOC test.

ESCALATION PATH: if the inline-cursor interaction conflicts
with prompt_toolkit's natural input loop (e.g., the cursor
lands somewhere unexpected), escalate — we may need a
"pause input, render capsule, capture keystroke separately,
resume input" pattern.
```

### Reviewer prompt

```
[standing instructions]

Review Step 4 — permission capsule.

Checks:

1. Old Panel-based modal rendering is gone. The chat's
   permission callback now emits a side-bar capsule.

2. The four choices (y/a/n/d) are inline as content of the
   capsule; the cursor line is the last content line during
   the prompt.

3. After the user chooses, the capsule contains the choice
   as a final line and becomes static. The capsule is the
   permanent record of the permission interaction.

4. Pattern-edit field is single-line (no wrap yet). Long
   patterns scroll horizontally within the line.

5. Manual: in a test workspace with a prompt-effect bash
   rule, ask the helper to run bash. Confirm:
   (a) the permission capsule appears with orange bar.
   (b) the footer hint updates to [y/a/n/d] then Enter.
   (c) pressing y allows the call; the capsule records "▸ y".
   (d) pressing a opens the pattern edit; Enter commits the
       rule; the capsule records the rule_id.

Verdict: PASS or FAIL.
```

---

## Step 5 — Code block ┃ styling

### Developer prompt

```
[standing instructions]

Step 5 — Add the thicker ┃ side-bar treatment for code blocks
inside agent capsules.

Read first:
- process/qol-chat-capsule-layout.md §Code block.

Tasks:

1. Rich's markdown rendering currently outputs code blocks as
   syntax-highlighted text. Wrap that output so each line is
   prefixed with `┃ ` (CYAN color from _styles.py).

2. The language label appears as a `┃ <lang>` header line
   before the code (also CYAN).

3. Code blocks are nested INSIDE the agent's capsule:
     │ <agent> · <time>
     │ <prose>
     │
     │ ┃ python
     │ ┃ <code>
     │
     │ <prose after>
   The outer `│` is the agent's bar; the inner `┃ ` lives
   one column to the right of the outer bar.

4. Syntax highlighting (Pygments via Rich) still applies to
   the code content.

5. Tests:
   - Test an agent.message with a markdown code block:
     assert the rendered output contains `┃ python` (or
     whatever language) AND `┃ ` prefix on each code line.
   - Assert the outer `│` is still present on those lines
     (i.e., the agent capsule's bar continues through the
     code block).

Scope: ~40 LOC code + ~30 LOC test.

ESCALATION PATH: if Rich's markdown rendering doesn't expose
a hook for per-line prefixing of code blocks, escalate —
we may need to render code blocks manually instead of
relying on Rich's Markdown renderer.
```

### Reviewer prompt

```
[standing instructions]

Review Step 5 — code block styling.

Checks:

1. Code blocks have `┃ ` prefix on each line, cyan-colored.
2. Language label appears as the first line of the code
   block in `┃ <lang>` form.
3. Outer agent bar `│` is preserved on every line including
   code block lines.
4. Syntax highlighting still applies to code content.
5. Manual: ask the helper for a code example. Confirm the
   code block renders inside the agent capsule with the
   thicker bar.

Verdict: PASS or FAIL.
```

---

## Step 6 — Regression tests + manual verification

### Developer prompt

```
[standing instructions]

Step 6 — Final sweep. Comprehensive tests + manual end-to-end
verification.

Tasks:

1. Add a test that drives a complete chat exchange matching
   the doc's mockup: user message → tool call → permission
   capsule → tool result → agent message with code block.
   Assert each capsule appears in order, with the correct
   source/color/timestamp.

2. Verify no regressions in existing Phase 1.x / 2.x tests.
   Phase 2.1's TC-CHAT tests likely need assertion updates
   to match the new capsule output; preserve their semantic
   intent.

3. Manual end-to-end:
   - Fresh `kolsch chat` session.
   - Send a chat-only message (no tools): verify You + helper
     capsules.
   - Send a tool-triggering message: verify You + permission
     + tool + helper capsules.
   - Use /clear: verify system capsule + title-bar reprint.
   - Use /model: verify system capsule.
   - Trigger an error (e.g., disconnect provider mid-call):
     verify system capsule with the error + hint.

4. Capture the manual session as a screenshot or transcript
   in the PR description. The coordinator confirms the
   look matches the mockup in this doc.

Scope: ~80 LOC test + manual verification.

ESCALATION PATH: if existing TC-CHAT tests have semantic
content that doesn't translate to the new format, escalate —
the test catalog may need refreshing.
```

### Reviewer prompt

```
[standing instructions]

Review Step 6 — final sweep.

Checks:

1. New end-to-end test covers the full mockup flow.
2. Phase 2.x TC-CHAT tests pass with updated assertions.
3. Manual transcript in PR description matches the mockup in
   the spec doc. The coordinator confirms the look.
4. Full test suite green.

Verdict: PASS or FAIL. On PASS, this QoL is closed.
```

---

## Part 6 — Step 6 manual verification handoff

### 6.0 Status

- **Developer-verifiable (programmatic)**: COMPLETE. Steps 1-5
  + Step 6's mockup-flow test all pass; full suite 566/566;
  ruff + mypy clean. The pty-driven provisional smoke (§6.1
  below) shows the capsule layout working end-to-end against
  the real opencode/deepseek-v4-pro provider with zero CrewAI
  signatures.
- **Real-terminal smoke (human-required)**: PENDING USER
  ACTION. ``script -q`` requires a real TTY caller; the
  developer's sandbox can only fork pty's, which exercise the
  same prompt_toolkit + Rich code paths but cannot subject
  the rendering to human-eye judgement on color, alignment,
  or flicker. §6.2 lists the checklist; §6.3 captures the
  reviewer + coordinator sign-off pattern carried forward
  from ``bug-chat-permissions-and-console-leak.md`` §8.7.

### 6.1 Developer pty smoke — provisional evidence

Workspace: ``/tmp/kolsch-stepg`` (Phase 2.0 §3.1 default
rules + helper agent pinned to ``opencode/deepseek-v4-pro``).
Input: ``please bash ls -la`` → modal ``y`` → tool runs.

Representative excerpt from
``/tmp/kolsch_chat_capture.txt`` (3782 bytes, 82 lines, 0
CrewAI signatures):

```
kolsch chat • helper • opencode/deepseek-v4-pro • ses_…
─────────────────────────────────────────────────────────────────

You: please bash ls -la
│ You · 20:28:38
│ please bash ls -la

│ permission · 20:28:41
│ bash(command='ls -la')  —  matched rl_99a71d57
│
│ [y] once    [a] always allow this
│ [n] deny once    [d] always deny
│
> [y/a/n/d]: y
│ ▸ y — allowed once

│ tool · 20:28:41 → 20:28:56 (15.3s)
│ bash(command='ls -la')
│   total 160
│   drwxr-xr-x@  10 andrecohen  wheel    320 May 30 23:15 .
│   ... (real ls -la output) ...
│
│ helper · 20:29:04
│ The workspace contains the following files and directories:
│
│ | Type | Permissions | ... | Name |
│ |------|-------------|-----|------|
│ | dir  | drwxr-xr-x@ | ... | .platform |
│ | ...
│
│ The workspace is on macOS ... Is there anything else?
```

What this proves:

- **Step 1 (no thinking indicator)**: ``  …`` never appears.
- **Step 2 (user + agent + system capsules + title bar)**:
  banner with horizontal rule fires at startup; user message
  → ``│ You · `` capsule; agent reply → ``│ helper · ``
  capsule.
- **Step 3 (tool capsule via Rich Live)**: the tool capsule
  evolves from running to completed in place — captured
  final state shows ``│ tool · 20:28:41 → 20:28:56 (15.3s)``
  with the result lines below.
- **Step 4 (permission capsule)**: orange-bar capsule with
  inline four choices + ``▸ y — allowed once`` continuation
  marker on the same bar. No more Panel-vs-tool-running-
  panel interleave (the original UX complaint that started
  this QoL).
- **Step 5 (code blocks)**: not exercised by this particular
  input — the agent responded with a markdown table, not a
  fenced code block. To verify code-block rendering, ask the
  helper for "show me a Python function that says hello"
  in §6.2's manual smoke.

The pty driver's prompt-overlap artefacts ("WARNING: your
terminal doesn't support cursor position requests" and the
``> [y/a/n/d]:`` line being concatenated with the next
input) are pty-harness limitations and do NOT appear in a
real terminal.

### 6.2 Real-terminal smoke checklist — for the user

Run in a real Terminal.app / iTerm2 / kitty session (NOT an
editor's integrated terminal — some mask escape sequences):

1. ``script -q /tmp/kolsch_capsule_smoke.txt uv run kolsch --root /tmp/kolsch-stepg chat``

2. **Chat-only message** (no tools):
   - Send: ``what is kinderhook``
   - Expect: ``│ You · ...`` user capsule (BLUE bar) + ``│ helper · ...`` agent capsule (GRAY bar) with prose reply. **No code block bar** since the reply is prose.
   - **No** ``  …`` indicator anywhere.

3. **Code-block message** (tests Step 5):
   - Send: ``show me a Python function that says hello``
   - Expect: agent capsule with prose intro + ``│ ┃ python`` lang header (CYAN inner bar) + Pygments-highlighted ``def hello()`` etc. lines each prefixed with ``│ ┃``.

4. **Tool-call message** (tests Step 3 + Step 4):
   - Send: ``run bash ls -la in this directory``
   - Expect: orange ``│ permission · ...`` capsule with the four choices inline; press ``y``; orange continuation ``│ ▸ y — allowed once``; then cyan ``│ tool · HH:MM:SS → HH:MM:SS (Ns)`` capsule with the real ``ls -la`` output below.

5. **/clear**:
   - Send: ``/clear``
   - Expect: scrollback clears; title banner reprints; ``│ system · ...`` capsule with ``/clear — scrollback cleared``.

6. **/model**:
   - Send: ``/model anthropic/claude-opus-4.8`` (or any valid alias)
   - Expect: ``│ system · ...`` capsule with ``Switched model to anthropic/claude-opus-4.8``.

7. **Error path**:
   - Block the provider's network (e.g., toggle WiFi off) and send any prompt.
   - Expect: ``│ system · ...`` capsule with ``Exchange failed: <code>`` and the error message.

8. ``/exit``.

### 6.3 Per-criterion check — for the user to fill in

| # | Criterion | Pass? | Notes |
|---|---|---|---|
| 1 | Title bar + horizontal rule at session start | ☐ | |
| 2 | User capsule renders with blue bar + ``You ·`` header | ☐ | |
| 3 | Agent capsule renders with gray bar + agent name + ``·`` header | ☐ | |
| 4 | No ``  …`` thinking indicator anywhere | ☐ | |
| 5 | Code block: inner ``┃`` bar (cyan) with lang label + per-line syntax highlighting | ☐ | |
| 6 | Permission capsule: orange bar + four choices inline + ``▸ <choice>`` continuation marker | ☐ | |
| 7 | Tool capsule: cyan bar; running → completed transition with ``→ end (Ns)`` suffix | ☐ | |
| 8 | Tool failure: same capsule, ``failed`` suffix + dimmed bar | ☐ | (need a deny scenario or a bash typo to trigger) |
| 9 | /clear: scrollback clears + banner reprints + system capsule | ☐ | |
| 10 | /model: system capsule with new model id | ☐ | |
| 11 | Exchange failure: system capsule with error code | ☐ | |
| 12 | Native scrolling works (Page Up shows past capsules) | ☐ | |
| 13 | Text selection works across capsule lines (no broken selection due to closed borders) | ☐ | |
| 14 | Color rendering matches the §Color palette (blue / gray / dark-gray / orange / cyan3 / cyan) | ☐ | |

### 6.4 Sign-off

```
Developer:  pty smoke 2026-05-31 — Steps 1–5 visible + correct;
            Step 5 code-block not exercised by smoke input,
            verified by unit test only.
User (real terminal): ___________   Date: ___________
Reviewer:   ___________   Date: ___________
Coordinator: ___________   Date: ___________
```

### 6.5 Q-USER-1 carry-over

The standing process question from
``bug-crewai-stdout-leak-investigation.md`` §5.10 — whether
manual-verification (G/H/I-style steps) becomes an
AGENTS.md hard rule — remains open and orthogonal to this
QoL's closure.

---

## Acceptance criteria (across all six steps)

The QoL is shipped when **all** of the following hold:

1. Every message type renders as a side-bar capsule with the
   correct color and the `│ source · HH:MM:SS` header.
2. No `  …` thinking indicator appears in any chat output.
3. Tool calls render as a single evolving capsule (running →
   completed/failed in place via Rich Live).
4. Permission prompts render as inline orange capsules; the
   `[y/a/n/d]` interaction happens inline with the cursor
   `▍` as the last content line.
5. Code blocks inside agent capsules use the `┃` thicker bar
   with cyan color; syntax highlighting preserved.
6. System notices cover all five trigger cases (session
   resume, model switch, /clear, /new, exchange failed,
   permission rule write).
7. Title bar prints at session start and on /clear.
8. Native terminal scrolling and text selection work
   throughout — no closed borders to interrupt copy/paste.
9. No regression in Phase 1.x or 2.x tests (updated
   assertions ok).
10. Coordinator's manual transcript matches the mockup.

---

## Coordinator escalation

```markdown
### Escalation E-QOL-CAPSULE-<NNN> — <summary>

- **Raised by:** developer / reviewer
- **Step:** 1–6
- **Trigger:** concrete observation.
- **Why this is not a guess:**
- **Options:**
- **Estimated cost:**

---

### Coordinator resolution — E-QOL-CAPSULE-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Rich rendering issue (256-color support, Live composition,
  markdown hook absence).
- Existing test assertions don't translate cleanly to the
  new capsule format.
- The pattern-edit field's single-line behavior interacts
  poorly with prompt_toolkit's input loop.

When NOT to escalate:
- Exact wording of system notices. Pick reasonable strings;
  tests assert on substrings.
- Subtle color shading. The Rich color names are the
  contract; if the terminal renders them slightly off, that's
  the terminal, not us.

---

## Coordinator escalation log

*(no entries yet)*

---

## Out of scope

- **Theme support** (light vs dark mode, custom palettes).
  Future polish; Phase 2.4 territory.
- **Right-align user capsules** (iMessage-style). Terminal
  isn't a chat app; left-alignment is the convention.
- **Multi-line pattern edit** in permission [a]/[d] flow.
  Single line for now; revisit if real patterns exceed
  terminal width regularly.
- **Token / cost display in tool capsule headers.** Useful
  but not part of this layout pass. Add later as a
  metadata-display enhancement.
- **Mouse interaction** (clickable rule IDs, expandable
  tool results). Textual phase (Phase 2.4) concern.
- **Pinned title bar** (alternate-screen mode). Conflicts
  with native scrolling commitment.
- **Replacing `qol-chat-capsule-layout` is the supersedence
  of PR G2 from `bug-crewai-stdout-leak-investigation.md`.**
  The Crew-stdout bug doc's PR G1 (wrap commit) still ships
  separately; PR G2 (F5 fix) is folded into Step 1 of this
  QoL.
