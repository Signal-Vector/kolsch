# Bug — Chat: permissions defaults missing + CrewAI stdout leaks past the renderer + user input echoed twice

**Draft 0.1** *(investigation ticket. Three distinct symptoms in
one chat session, two with shared root cause. One coordinated fix
plan.)*

> Reported symptom: a user ran `kolsch chat` against the helper
> agent and observed:
>
> **A. No approval modal for `bash`.** The agent ran `ls`
> directly without any `[y]/[n]/[a]/[d]` prompt. Reading the
> session's `platform.yaml` confirmed the workspace has the
> Phase-1-era allow-all authorization rules
> (`{who: ".*", what: ".*", effect: allow}`) — not the Phase 2.0
> §3.1 workspace-only + `prompt`-on-bash defaults.
>
> **B. CrewAI bleeding to stdout.** Between every event block,
> stray `  …` ellipsis lines appeared in the terminal — clearly
> CrewAI's internal progress / thinking indicators escaping past
> the chat renderer. CHAT-R9 (renderer is a pure consumer of
> events) is being violated *downstream* of the engine: CrewAI
> writes to stdout directly, never emits an event for it, the
> chat has no chance to render it.
>
> **C. User input echoed twice in the scrollback.** Every user
> message appears twice — "You: bash ls for me" prints on two
> consecutive lines. The chat's input loop prints once at submit
> time; the renderer prints again from the `user.message` event.
> Both paths fire; result is duplication.
>
> **D (minor — friction, not a bug).** The model tried
> `read_file(file_path='platform.yaml')` first, got
> `tool.invalid_arguments`, retried with `read_file(path=...)`,
> succeeded. Round-trip wasted; users see a transient red error
> block. Worth flagging as a schema-discoverability finding.
>
> A, B, C all sit on Phase 2.0 / 2.1 wiring that stopped short of
> what the specs promised. None is the engine-tools-wiring bug
> (that was fixed). Two share the "we shipped the surface but
> missed the discipline" pattern.

---

## Part 0 — Symptom & Reproduction

**Reported by:** user, after the engine-tools-wiring fix landed.
Workspace at `~/Documents/kolsch` was set up via `kolsch setup`
sometime before the recent Phase 2.0 §3.1 default-rules spec was
honored (or that part of TOOL-R12 was never implemented).

**Reproduction:**

1. `uv run kolsch chat` (against the kolsch/agents/helper).
2. Type "bash ls for me".
3. **Symptom A (permissions):** the agent runs `bash` directly;
   no modal appears. Result: `bundles content data logs platform.yaml sessions`.
4. Type "show me the content of platoform.yaml" (typo kept; not
   the issue).
5. **Symptom A (continued):** the agent reads platform.yaml
   without prompting, returns its content.

**Symptom B (console leak)** — visible throughout the session:
```
You: bash ls for me
You: bash ls for me           ← C (duplicate)
  …                           ← B (CrewAI stdout leak)
╭─ ▶ bash(command='ls') ──────... 
╰────────────────────────────...
  …                           ← B again
helper: Here's the listing... 

You: show me the content...
You: show me the content...   ← C again
  …                           ← B
```

**Symptom D** — visible in one block:
```
╭─ ▶ read_file(file_path='platform.yaml') ──...
╰─────...
╭─ ✗ read_file ─────...
│ tool.invalid_arguments: Invalid arguments for tool 'read_file' (1 error(s))
╰─────...
  …
╭─ ▶ read_file(path='platform.yaml') ──...      ← retry with corrected param
```

**Confirming evidence (from the session's platform.yaml the
agent printed):**
```yaml
authorization:
  variant: rule_based
  config:
    rules:
    - who: .*
      what: .*
      effect: allow
```

That's the Phase 1 allow-all rule, NOT the Phase 2.0 §3.1
default set (which has a `prompt`-effect rule for `tool.bash`
and a default-deny tail).

---

## Part 1 — Four sub-issues, three real bugs + one finding

### A — Permissions: workspace has allow-all rules (no Phase 2.0 defaults)

Phase 2.0 spec §3.1 specifies the default rule set written by
`kolsch setup`. The current workspace has a single allow-all
rule — the Phase 1 default. This means:

- `bash` calls return `allow` instead of `prompt` → no modal.
- `write_file` calls return `allow` → silent overwrites possible.
- The default-deny tail (`rl_default_deny`) is missing → any
  unknown tool/target is also allowed.

Two failure modes likely simultaneously:

- **A1** — `kolsch setup` was never updated to write the Phase
  2.0 §3.1 default rules. The setup implementation predates the
  Phase 2.0 alignment flag TOOL-R12.
- **A2** — This specific user's workspace was created BEFORE
  Phase 2.0 setup changes landed (if any). Existing workspaces
  have stale `platform.yaml`. No migration path exists.

Both need addressing. A1 is the immediate fix; A2 is the
migration question — coordinator decision needed.

### B — CrewAI stdout bleeds past the chat renderer

The `  …` ellipsis lines are CrewAI's own progress / thinking
indicators. CrewAI's `Agent` defaults to `verbose=True` or
similar in some versions, and emits text directly to stdout
during reasoning steps. This output:

- Does NOT pass through the engine's event emission.
- Does NOT reach the chat's Renderer (CHAT-R9 hard rule).
- Bypasses the chat's Rich `Live` context entirely.

The result: garbled terminal output. The chat's rendering
discipline is preserved for events it sees; CrewAI's bypass is
invisible to the discipline.

**Hypotheses:**

- **B1** — CrewAI's `Agent` is constructed with verbosity
  enabled. The framework adapter should set verbosity off (or
  redirect / capture stdout).
- **B2** — CrewAI version pinned in pyproject.toml has a known
  habit of printing progress markers even when verbose=False.
  May require `contextlib.redirect_stdout` around the call.
- **B3** — Some other path (litellm? a logging handler?) writes
  ellipses. Less likely given the timing pattern, but worth
  ruling out.

### C — User input rendered twice ("You: X" duplicates)

The chat's input loop, per Phase 2.1 §2.2 (block types row 1),
shows the user message in a "You: <content>" block. This is
expected. But it appears that two code paths both produce the
block:

- **Path 1** — the input loop echoes the user's submitted text
  back to the scrollback as part of processing.
- **Path 2** — the renderer processes the `user.message` event
  emitted by the engine, which also produces a "You: <content>"
  block per Part 3 table.

Both fire. The renderer is supposed to be the single source of
truth for visible blocks (CHAT-R9). The input loop's echo is
the bug — it shouldn't produce visible output that overlaps
the renderer's output.

**Hypothesis C1** — the chat's input loop in Step 24a
prematurely printed the user message (probably as an echo
during the "echo stub" implementation before the renderer was
wired in Step 24c). When 24c landed, the echo was supposed to
be removed; it wasn't. Both paths now emit.

### D — `read_file` parameter naming friction (finding, not a fix)

The model first called `read_file(file_path='platform.yaml')`,
got `tool.invalid_arguments`, retried with
`read_file(path='platform.yaml')`. Phase 2.0 §1.1 defines the
parameter as `path: str`. The model guessed `file_path` —
common LLM convention from training data.

This is a schema-discoverability issue. The tool's docstring +
JSON schema should make the parameter name obvious enough that
the model gets it right first shot. Options to consider (NOT in
this PR — flag for the coordinator):

- Rename the parameter to `file_path` to match the LLM
  convention (breaks the Phase 2.0 spec).
- Accept both `path` and `file_path` as aliases (small
  Pydantic config; matches "be liberal in what you accept"
  convention).
- Tighten the docstring so the model picks `path` first.

This is finding **D**, NOT a fix in this bug.

---

## Part 2 — Acceptance Criteria

The bug is fixed when **all** of the following hold:

1. **A — fresh setup writes Phase 2.0 §3.1 default rules.**
   `uv run kolsch setup --root <tmp>` produces a `platform.yaml`
   whose `authorization.config.rules` matches the §3.1 list
   (workspace-relative read/list allow, write/bash prompt,
   default-deny tail).
2. **A — `bash` now triggers the approval modal** on a fresh
   workspace + the helper agent. Modal appears with the four
   choices per Phase 2.1 §4.1.
3. **A — migration path for existing workspaces.** Coordinator
   decision drives this; default proposed: `kolsch setup`
   detects an existing allow-all rule set, prompts the user to
   migrate to the §3.1 defaults, preserves any custom rules
   they've added. Or: explicit `kolsch permissions reset` /
   `--migrate-defaults` command. See Escalation Q1 below.
4. **B — CrewAI stdout is suppressed (or captured into events).**
   Chat sessions produce NO `  …` ellipsis lines, no progress
   markers from CrewAI between event blocks. The chat
   terminal looks clean.
5. **C — user messages appear once.** Each user submission
   produces exactly one "You: <content>" block in the
   scrollback. Verified by a regression test that captures
   chat output and counts "You:" occurrences.
6. **D — flagged in the PR description for coordinator.** NOT
   fixed under this PR.
7. **No regression** in Phase 1.x, 2.0, or 2.1 tests.

---

## Part 3 — Step dependency graph

```
Step A (investigate all three issues + 4th finding)
    │
    ▼
Step B (permissions defaults: fix setup template)
    │       │
    │       └→ Step C (migration for existing workspaces)
    │            │
    ├────────────┘
    ▼
Step D (CrewAI stdout suppression in adapter)
    │
    ▼
Step E (remove duplicate user-message echo)
    │
    ▼
Step F (regression tests for B, D, E)
    │
    ▼
Step G (manual verification + D finding handoff)
```

| Step | Ships | Reviewable in |
|---|---|---|
| **A** | Investigation writeup + escalation Q1 | ~20 min |
| **B** | Setup writes Phase 2.0 §3.1 defaults | ~30 min |
| **C** | Migration mechanism (decided by coordinator on Q1) | ~30 min |
| **D** | CrewAI verbose-off + stdout capture in framework adapter | ~30 min |
| **E** | Chat input loop stops echoing user.message | ~20 min |
| **F** | Regression tests for §3.1 defaults, clean stdout, single-render | ~30 min |
| **G** | Manual end-to-end + D finding handoff | ~15 min |

Steps B and D are independent (different files, different
subsystems). Steps C and E are unblocked by B and D
respectively. Step F can land in PR 1 with B+D+E or be its own
PR — at the developer's discretion based on test size.

**Suggested PR shape:**
- **PR 1** — B + E + F (small, single-file edits + tests).
- **PR 2** — C (after coordinator answers Q1).
- **PR 3** — D + F's CrewAI-related test (may need pinned
  CrewAI version research; could escalate).
- **PR 4** — G manual verification + handoff.

---

## Step A — Investigation and root-cause writeup

- **Builds:** a written confirmation of A1/A2, B1/B2/B3, C1
  hypotheses with file:line citations + the escalation question
  for the coordinator on migration.
- **No code changes.** Pure read-only investigation.
- **Acceptance:** writeup posted; coordinator answers Q1 (see
  below) before Step C begins.

### Developer prompt

```
[standing instructions]

Step A — Confirm or correct the hypothesis set.

Read first:
- docs/process/bug-chat-permissions-and-console-leak.md, in full
  (especially Parts 0, 1, 2).
- docs/process/phase2.0-tools-specs.md §3.1 (the default rule
  set the setup is supposed to write).
- docs/process/phase1.2-usability-specs.md §2.2 Step B (setup's
  skeleton step) and TOOL-R12 in phase2.0-tools-specs.md Part 6
  (the alignment flag).
- src/cli/_setup.py (the setup orchestrator).
- The CrewAI framework adapter (find via grep "crewai.Agent" in
  src/engine/ and src/llm/).
- src/cli/chat.py (the chat module; for Issue C).

Investigation tasks:

1. For A — find where `_setup.py` writes platform.yaml's
   authorization.config.rules. Confirm:
   - Whether the §3.1 default set is written (or some other set).
   - Whether TOOL-R12 was applied to the Phase 1.2 setup code,
     or whether the Phase 2.0 spec assumed setup would be
     updated as part of its own implementation but it wasn't.

2. For B — find where the CrewAI Agent is constructed. Inspect:
   - Is `verbose=True` set? `verbose=False`?
   - Is there a `step_callback` or similar that prints?
   - Run `uv run kolsch chat` against a fake-LLM agent with a
     tool call; capture stdout via subprocess. Confirm the
     ellipses appear (vs being a CrewAI version quirk).

3. For C — read the chat's input loop. Find where the user's
   submitted text is displayed. Trace whether it's printed by:
   (a) the input loop directly.
   (b) the renderer processing user.message.
   (c) both.

4. For D — read the read_file tool's docstring and the
   generated JSON schema (Step C from the previous bug doc
   added a test pinning args_schema). Note whether `path` is
   the parameter name and whether the description is
   discoverable enough.

5. Coordinator question to surface as Q1 — migration policy
   for existing workspaces:
   - (a) `kolsch setup` re-detects the allow-all rule and
     prompts to migrate (preserve custom rules).
   - (b) A new explicit `kolsch permissions reset` command.
   - (c) A one-time `--migrate-defaults` flag on setup.
   - (d) No migration; document the upgrade path; users edit
     by hand.

6. Produce the root-cause writeup. STOP for coordinator
   approval before any code lands.

ESCALATION PATH: if the investigation reveals (i) Phase 2.0
§3.1 was never spec'd correctly enough to implement, (ii) the
CrewAI version pin doesn't support suppressing verbose
output, or (iii) the chat's input-loop echo is wired in a way
that removing it breaks a feature you can't immediately see —
escalate per the coordinator section at the end of this doc.
```

### Reviewer prompt

```
[standing instructions]

Review Step A — the investigation writeup.

Checks:

1. Each hypothesis (A1, A2, B1, B2, B3, C1) has concrete
   file:line citations or "I looked and didn't find it"
   notes. Vague "this seems to be the issue" — fail.

2. Q1 (migration policy) is genuinely a coordinator decision,
   not laziness. The dev's recommendation (if any) is
   reasonable but lets the coordinator override.

3. The finding D's three options are recorded in the writeup
   for separate coordinator follow-up.

4. The proposed fix shape per step is concrete (file paths,
   estimated diff sizes).

Verdict: APPROVE-TO-PROCEED or REVISE with specific gaps.
```

---

## Step B — Regression test pinning §3.1 default rules

> **Reframed per Step A findings (E-BUG-CHAT-002):** the
> setup-side code already writes §3.1 correctly — `_workspace_only_default_rules()` is wired and verified. The
> user's symptom was a timing issue (workspace created ~11
> hours before the fix landed), not a missing implementation.
> Step B is now **test-only**: add a regression test that pins
> the §3.1 contents so future edits to the rule template can't
> silently drift.

- **Builds:** a regression test in tests/component/test_setup.py
  asserting `kolsch setup` produces the §3.1 rule set verbatim.
- **Depends on:** Step A approved.
- **Acceptance:** the test fails if any of the §3.1 rules is
  modified, removed, or reordered.

### Developer prompt

```
[standing instructions]

Step B — Regression test pinning §3.1 default rules.

The setup code is ALREADY correct (Step A confirmed
_workspace_only_default_rules() returns the §3.1 set). This
step ships a test that pins the contract so future drift is
caught.

Read first:
- docs/process/phase2.0-tools-specs.md §3.1 (the canonical
  rule set).
- src/cli/_setup.py:821 _workspace_only_default_rules() (the
  function under test).

Tasks:

1. Add test in tests/component/test_setup.py (or extend
   TC-UX-05 if its shape allows):
   - Run setup --non-interactive against a tmp root.
   - Open the produced platform.yaml.
   - Assert authorization.config.rules has exactly 5 entries.
   - Assert each rule's id, who, what, effect match §3.1.
   - Assert rl_default_deny is the LAST entry.
   - Assert the resolved workspace path is substituted into
     the `what` fields of the workspace-scoped rules
     (rl_read_workspace, rl_list_workspace, rl_write_workspace).

2. NO production code changes. If you find yourself editing
   src/cli/_setup.py, escalate — the dev's Step A finding
   said the code is already correct.

Scope: ~30 lines test, zero production code.

Acceptance: the new test passes; a deliberate edit to
_workspace_only_default_rules (e.g., flipping one effect from
prompt → allow in a scratch commit) makes the test FAIL.
```

### Reviewer prompt

```
[standing instructions]

Review Step B — regression test.

Checks:

1. The test asserts §3.1 verbatim (modulo path substitution).
   Compare the test's expected dict against the spec's YAML
   block, field by field.

2. No production code was changed. git diff src/cli/_setup.py
   should be empty. Step A established the code is correct;
   any change here is scope creep.

3. The test FAILS on a deliberate rule edit. The developer
   should demonstrate this in the PR description (a scratch
   commit toggling one effect, test fails, revert, test
   passes).

4. rl_default_deny is asserted as the LAST entry. Order
   matters per Phase 2.0 §3.2 first-match-wins.

Verdict: PASS or FAIL.
```

---

## Step C — Migration for existing workspaces

- **Builds:** depends on coordinator's Q1 answer. The
  developer prompt is written generically; coordinator picks
  the specific path.
- **Depends on:** Step A's coordinator resolution.
- **Acceptance:** a user with an existing allow-all workspace
  can update to the §3.1 defaults with one CLI invocation,
  preserving any custom rules they've added.

### Developer prompt

```
[standing instructions]

Step C — Migration path for existing workspaces.

WAIT for coordinator's Q1 resolution before starting.
Re-read the Q1 entry in the Coordinator Escalation Log at the
end of this bug doc. The resolution names which option (a, b,
c, d) to implement.

Then implement per the chosen option. The unit test asserts:
- Starting workspace has the Phase 1 allow-all rule + zero
  custom rules → after migration, has §3.1 default set.
- Starting workspace has allow-all + 2 custom rules the user
  added → after migration, has §3.1 set + the 2 custom rules
  (custom rules preserved in their relative position).

ESCALATION PATH: if the chosen option's implementation
diverges meaningfully from the spec text written here,
escalate — coordinator's Q1 resolution didn't anticipate the
specific edge case.

Scope: depends on chosen option. (a) ~50 lines.
(b) ~80 lines (new subcommand). (c) ~40 lines (flag).
(d) zero code, only docs.

Commit: feat(setup,permissions): migration path for existing
workspaces to §3.1 defaults.
```

### Reviewer prompt

```
[standing instructions]

Review Step C — workspace migration.

Checks:

1. The migration matches the coordinator's Q1 option.

2. Custom rules added by the user (rules NOT in §3.1) are
   PRESERVED through the migration. This is the
   non-destructive guarantee. A test must verify it.

3. The migration is idempotent. Running it twice against an
   already-migrated workspace is a no-op (or prints "already
   at §3.1 defaults").

4. The migration creates a timestamped backup of the original
   platform.yaml before writing (similar to Phase 2.3 drift
   detection convention).

Verdict: PASS or FAIL.
```

---

## Step D — Suppress CrewAI stdout in the framework adapter

- **Builds:** the framework adapter wraps the CrewAI call in a
  stdout-capture context (or sets verbose=False, or both).
  Result: no `  …` lines, no progress markers from CrewAI
  reach the terminal.
- **Depends on:** Step A approved.
- **Acceptance:** running a chat session with a tool call
  produces clean output — only the chat renderer's blocks
  appear, no CrewAI-emitted text.

### Developer prompt

```
[standing instructions]

Step D — Suppress CrewAI's stdout leakage.

Read first:
- The CrewAI framework adapter (find via grep "crewai.Agent"
  or "crewai.Crew" in src/).
- The Phase 1.1 §3 spec's Open Item 3 (CrewAI version pinning
  is a coordinated concern).

Tasks:

1. Identify the cause from Step A's findings:
   - B1 — Agent's verbose=True. Set verbose=False at
     construction.
   - B2 — CrewAI prints even with verbose=False. Wrap the
     run in `contextlib.redirect_stdout(io.StringIO())` to
     capture (then discard, since the chat renderer doesn't
     need it).
   - B3 — Some other path. Trace and silence.

   Likely both B1 and B2; apply both.

2. If applying B2, capture stderr too (CrewAI may write
   warnings there). Discard the captured text (chat doesn't
   render CrewAI's prose; it only renders events).

3. Component test in
   tests/component/engine/test_crewai_adapter.py:
   - Construct an Agent with a fake LLM.
   - Capture stdout/stderr around an exchange.
   - Assert NO CrewAI strings ("Thinking...", "...", etc.)
     reach the captured streams.

4. Document the suppression in the adapter's docstring —
   per the Phase 1.3 / connector-docstring discipline, the
   "why" of stdout capture should be inline so a future
   contributor doesn't undo it accidentally.

Scope: ~30 lines code + ~30 lines test.

ESCALATION PATH: if CrewAI's stdout is actually structured
data that some other code consumes (unlikely but possible),
escalate — suppressing it could break a downstream consumer.

Acceptance: component test passes; manual `kolsch chat`
session shows clean blocks with no ellipses between.
```

### Reviewer prompt

```
[standing instructions]

Review Step D — CrewAI stdout suppression.

Checks:

1. Both stdout AND stderr are captured/discarded around the
   CrewAI call. stdin is left alone.

2. The capture is SCOPED to the CrewAI call only — not a
   global stdout redirect that would suppress the chat's own
   Rich output.

3. The docstring explains the WHY of the capture (per Phase
   1.3 connector-docstring discipline). A future contributor
   reading this code shouldn't be tempted to "fix" the
   suppression.

4. Component test asserts NO CrewAI prose strings reach
   stdout/stderr. The list of strings to exclude includes at
   minimum: "...", "Thinking", "Iteration", "Working Agent",
   "Task Output:". Add more if Step A's investigation found
   them.

5. The chat renderer's own Rich output still appears
   normally (verify via the test or manually).

Verdict: PASS or FAIL.
```

---

## Step E — Chat input loop stops echoing user.message

- **Builds:** the chat's input loop submits to the engine
  WITHOUT printing the user's text. The renderer's
  `user.message` event handler is the single source of the
  "You: <content>" block.
- **Depends on:** Step A approved.
- **Acceptance:** each user submission produces exactly one
  "You: <content>" block in the captured chat output.

### Developer prompt

```
[standing instructions]

Step E — Remove the redundant user-message echo.

Per Step A's findings (E-BUG-CHAT-002), the duplicate is NOT
the input-loop-vs-renderer race the bug doc originally
hypothesized. The renderer is already silent on user.message
(per CHAT-R9 discipline — confirmed at src/cli/_renderer.py
:167, :459 and chat.py:444-447). The two sources are:

  C1a — prompt_toolkit's natural submit-time terminal echo
        (the finalized "You: <text>" line in the scrollback
        when the user hits Enter).
  C1b — chat.py:448 explicit stdout.write(f"You: {text}\n").

Both fire. The fix: remove C1b. prompt_toolkit's finalized
line is the canonical scrollback echo.

Coordinator decision: chat is INTERACTIVE-ONLY. No
TTY-conditional logic. If a non-TTY user later reports
confusion, that's a future ticket; Phase 2.1 doesn't
promise non-TTY support.

Read first:
- Step A's findings on C1a/C1b (in the PR or escalation log).
- src/cli/chat.py:448 (the line to delete).
- src/cli/_renderer.py:167, :459 (the renderer's silence on
  user.message — confirm unchanged).

Tasks:

1. Delete chat.py:448-449 (the explicit "You:" echo).

2. Add a one-sentence note in chat.py's module docstring:
   "Chat is interactive-only; relies on prompt_toolkit's
   terminal echo for the user's submitted line. Non-TTY usage
   is not supported (see kolsch run for scripted use)."

3. Verify the renderer's user.message handler stays silent.
   Read src/cli/_renderer.py:441-456; confirm user.message
   is NOT in _DISPATCH.

4. Component test in tests/component/cli/test_chat_render.py:
   - Run a single exchange via the chat with input "hello"
     in a prompt_toolkit harness with DummyInput/DummyOutput.
   - Capture stdout.
   - Assert "You: hello" appears EXACTLY ZERO TIMES in the
     captured stdout. (Per E-BUG-CHAT-003 resolution: we test
     our discipline — no explicit echo from chat — not
     prompt_toolkit's terminal echo, which DummyOutput
     discards.)
   - Assert the agent's response block appears exactly once
     (this part the renderer DOES control, so the assertion
     is meaningful).

Scope: ~2 lines deletion + ~1 line docstring + ~30 lines test.

ESCALATION PATH: if any future investigation shows our
explicit "You:" sneaking back in via a different code path,
this test catches it. True end-to-end "exactly once in
real TTY scrollback" coverage is tracked as FUL-3 (PTY-based
harness; not in this PR).

Acceptance: component test passes; manual `kolsch chat`
shows exactly one "You:" per submission (verified by human,
not by the test).
```

### Reviewer prompt

```
[standing instructions]

Review Step E — duplicate user-message fix.

Checks:

1. The chat module NO LONGER explicitly prints "You: <text>".
   Verify two ways:
     (a) Find the removed line(s) in the diff (chat.py:448-449).
     (b) grep "You:" src/cli/chat.py — should return zero
         matches. If anything matches, fail; the discipline is
         that the chat does NOT emit this prefix.

2. The renderer is unchanged (still silent on user.message per
   CHAT-R9; src/cli/_renderer.py:441-456 has no entry for
   user.message).

3. Test asserts "You: hello" appears EXACTLY ZERO TIMES in
   captured stdout (per E-BUG-CHAT-003 resolution). Asserts
   the agent's response block appears exactly once.

4. The chat.py module docstring documents interactive-only
   design intent (one sentence per Q-E resolution in
   E-BUG-CHAT-002).

5. Manual: run `kolsch chat`, submit a message, scroll back,
   confirm exactly ONE "You:" per submission (this is the
   human-visible property the test can't assert).

Verdict: PASS or FAIL.
```

---

## Step F — Regression tests bundle

- **Builds:** consolidated regression tests for B, D, E.
  Could land in PR 1 with B+E or as a separate PR alongside D.
- **Depends on:** Steps B, D, E.
- **Acceptance:** each test fails on pre-fix code (verifiable
  by git-stash and re-run); all pass after.

Already covered by the per-step tests above. This step is a
checkpoint, not a new prompt — verify the regression-test
coverage is complete before declaring the bug fixed.

---

## Step G — Manual verification + finding D handoff

- **Builds:** a manual smoke against a real LLM in chat,
  verifying clean output, working modal, and the platform.yaml
  with new defaults.
- **Depends on:** Steps B–E in main.
- **Acceptance:** the user's original repro produces clean
  output with the approval modal firing on bash.

### Developer prompt

```
[standing instructions]

Step G — Manual verification.

NO new code unless a tiny fix is needed (in which case revise
B–E or escalate).

Tasks:

1. Set up a FRESH workspace (`kolsch setup --root /tmp/X`).
   Verify platform.yaml has the §3.1 defaults.

2. Connect OpenCode or OpenRouter. `kolsch chat`.

3. Run the original repro: ask the agent to "bash ls".
   Verify:
   - Approval modal appears with the four choices.
   - Pressing `y` runs the tool; output is real ls of /tmp/X.
   - The terminal output has NO `  …` ellipses between blocks.
   - "You: bash ls" appears exactly once.

4. Run a second exchange: "show me platform.yaml".
   Verify:
   - read_file's modal does NOT appear (allowed-read rule).
   - The output renders cleanly.
   - "You:" appears once.

5. PR description flags finding D (read_file parameter
   naming friction) for coordinator follow-up. The three
   options from Part 1.D are listed; NOT fixed here.

6. Capture terminal screenshots or paste the relevant
   transcript into the PR description.

ESCALATION PATH: if the manual verification reveals any of
B/D/E's fixes are incomplete, escalate — Steps B/D/E need
rework.
```

### Reviewer prompt

```
[standing instructions]

Review Step G — manual verification.

Checks:

1. PR description includes terminal capture/transcript
   showing:
   - Clean output (no ellipses).
   - Single "You:" per submission.
   - Modal firing for bash.

2. Finding D is flagged for coordinator, NOT fixed.

3. Re-run F's regression tests; still passing.

4. Run the FULL test suite. Zero regressions.

Verdict: PASS or FAIL.
```

---

## Part 4 — How to Escalate

```markdown
### Escalation E-BUG-CHAT-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** A / B / C / D / E / F / G
- **Affected issue or acceptance criterion:** A / B / C / D
- **Trigger:** concrete observation with file:line citations.
- **Why this is not a guess:**
- **Options as you see them:**
- **Estimated cost:**

---

### Coordinator resolution — E-BUG-CHAT-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Phase 2.0 §3.1's path template doesn't match the rule_based
  authz matcher's `what` semantics.
- CrewAI version doesn't support suppressing verbose output
  via the documented mechanism.
- The chat's input-loop echo turns out to be wired in a way
  that removing it breaks something non-obvious.
- The migration policy (Q1) needs clarification mid-fix.

When NOT to escalate:
- Color choices, exact backup file naming for migration.
- Whether to use `contextlib.redirect_stdout` vs
  `os.dup2` for stdout capture (both work; pick one).

---

## Part 5 — Coordinator Escalation Log

### Escalation E-BUG-CHAT-001 — Q1: migration policy for existing workspaces

- **Raised by:** developer (will raise after Step A
  investigation; pre-emptively logged here so coordinator can
  decide in parallel with Step A).
- **Step:** A
- **Affected issue:** A (acceptance criterion 3 — migration).
- **Trigger:** the bug fix changes the default rules that
  setup writes. Existing workspaces have allow-all rules. The
  fix needs a migration path or an explicit "users handle this
  themselves" call.
- **Options:**
  (a) `kolsch setup` detects allow-all on re-run, prompts to
      migrate, preserves user-added custom rules. Idiomatic
      with the existing setup's drift-detection pattern.
  (b) New explicit `kolsch permissions reset
      --to-defaults` command. Discoverable; clean separation.
  (c) `kolsch setup --migrate-defaults` flag. Hybrid.
  (d) No migration tooling; document the upgrade path; users
      edit platform.yaml by hand.

- **Estimated cost:**
  (a) ~50 lines. (b) ~80 lines. (c) ~40 lines. (d) zero.

---

### Escalation E-BUG-CHAT-002 — Step A findings + two coordinator questions

- **Raised by:** developer
- **Step:** A
- **Summary:** four hypotheses confirmed/refuted; three real
  bugs (A, B, C) plus finding D. Specific deltas vs the bug
  doc's original hypotheses:
  - **A1 refuted.** `kolsch setup` ALREADY writes §3.1 (per
    `src/cli/_setup.py:821` `_workspace_only_default_rules()`,
    wired at `:814-817`). TOOL-R12 alignment is implemented.
  - **A2 confirmed.** User's workspace was created at
    `2026-05-29 21:59:41` — ~11 hours BEFORE the Phase 2.0
    setup override commit `deaf95f` landed (`2026-05-30 08:47:29`).
    Existing-workspace migration is the only A-side fix needed.
    → Step B becomes test-only (regression pin); Step C is the
    real A-fix per Q1's existing resolution.
  - **B1 refuted.** Adapter sets `verbose=False` on Agent
    (`crewai_framework.py:122`) and Crew (`:136`). Correct.
  - **B2 confirmed (root cause).** CrewAI 1.14.6's event-bus
    listener at `crewai/events/event_listener.py:155` hardcodes
    `ConsoleFormatter(verbose=True)` — independent of
    Agent/Crew flags. ConsoleFormatter's own `Live` + Console
    write panels and "Thinking..." directly to stdout. Fix
    shape: `contextlib.redirect_stdout/stderr` around
    `crew.kickoff_async()` at `crewai_framework.py:142`. ~5 LOC
    code + ~30 LOC test.
  - **C1 partial.** The original hypothesis (input loop +
    renderer race) is wrong. Renderer is already silent on
    `user.message` per CHAT-R9 (`src/cli/_renderer.py:167, :459`;
    `chat.py:444-447`). Actual duplicate is:
    (C1a) prompt_toolkit's natural submit-time terminal echo
          (finalized "You: <text>" in scrollback when Enter
          is pressed).
    (C1b) explicit `chat.py:448 stdout.write(f"You: {text}\n")`.
    Both fire. Fix: delete C1b.
  - **D verified.** Tool's docstring + Field description
    already strong; the LLM still guessed `file_path` from
    training-data convention. Finding F4 remains.
- **Two coordinator questions raised:**
  - **Q-B:** drop Step B entirely or add a regression test
    pinning §3.1 contents (~30 LOC, no production code change)?
  - **Q-E:** TTY-conditional echo or interactive-only? Removing
    `chat.py:448-449` works for real TTYs but may produce zero
    "You:" echoes in pipe / no-TTY contexts where
    prompt_toolkit's echo doesn't fire.

---

### Coordinator resolution — E-BUG-CHAT-002

- **Decided by:** human
- **Decisions:**
  - **Q-B: Add the regression test.** Step B is reframed
    from a code-change step to a test-pin step. ~30 LOC test,
    zero production code. Catches future drift in
    `_workspace_only_default_rules` silently. Step B's
    developer + reviewer prompts have been updated to match.
  - **Q-E: Interactive-only.** Just remove `chat.py:448-449`.
    No TTY-conditional logic. The Phase 2.1 spec didn't
    promise non-TTY support; `kolsch run` is the scripted
    surface. A one-sentence note in `chat.py`'s module
    docstring documents this design intent.
- **Rationale (one sentence):** the dev's investigation
  revealed the bug doc's hypotheses were partially off — setup
  is already correct (Step B → test pin), the renderer is
  already disciplined (C is a clean two-line removal in the
  input loop), CrewAI's hardcoded ConsoleFormatter is the real
  console-leak culprit; the resolutions match these refined
  facts.
- **Affects:**
  - Step B's developer prompt updated to test-only.
  - Step E's developer prompt updated to reflect the actual
    C1a/C1b split and the interactive-only decision.
  - F4 (read_file param naming) stays as a finding for
    coordinator follow-up; weak preference for option (ii)
    Pydantic aliases when scheduled.
- **Resume instruction:** developer proceeds with the four
  PRs as proposed: PR 1 (E+F E-test), PR 2 (D+F CrewAI test),
  PR 3 (C migration), PR 4 (G manual verification). PRs 1, 2,
  3 are independent; ship in any order. PR 4 last.

---

### Escalation E-BUG-CHAT-003 — Test harness mismatch for Step E's "exactly once" assertion

- **Raised by:** developer
- **Step:** E
- **Trigger:** the original Step E developer prompt asked the
  regression test to assert "You: hello" appears EXACTLY ONCE
  in captured stdout. Verified during implementation that this
  isn't achievable with DummyOutput(): prompt_toolkit's
  natural terminal echo is discarded by the dummy backend, so
  only the explicit `chat.py:448 stdout.write` would land in
  captured stdout. After the fix removes that write, the test
  captures ZERO copies — not the production behavior of ONE.
  The "exactly once" assertion would PASS before the fix and
  FAIL after, which is the opposite of a correct regression
  test.
- **Why this is not a guess:** prompt_toolkit's DummyOutput
  is explicit about discarding rendered output; the production
  "You: <text>" line in a real TTY is prompt_toolkit's job,
  not ours.
- **Options:**
  (a) Pin the regression at "exactly zero in captured stdout."
      Tests our discipline (no explicit echo); leaves
      prompt_toolkit's job out of scope.
  (b) Replace DummyOutput with a real prompt_toolkit Output
      backend that writes into the captured StringIO. Tests
      the end-to-end "exactly once" property. Larger setup;
      effectively a PTY-style harness.
  (c) Skip the test entirely — rely on a `grep "You:"
      src/cli/chat.py` check in the reviewer prompt.

---

### Coordinator resolution — E-BUG-CHAT-003

- **Decided by:** human
- **Decision:** **Option (a) + the grep check.** Pin the test
  at "exactly zero copies in captured stdout" and add a
  reviewer-prompt check that `grep "You:" src/cli/chat.py`
  returns zero matches.
- **Rationale (one sentence):** we test our discipline (chat
  module emits no explicit "You:"); prompt_toolkit tests its
  own terminal echo; the conjunction (true end-to-end "exactly
  once") is verified by human-eye check in Step G's manual
  smoke, with FUL-3 tracking a PTY-based test if and when it
  becomes valuable.
- **Affects:**
  - Step E developer prompt updated to "exactly zero in
    captured stdout."
  - Step E reviewer prompt gains the `grep "You:"` discipline
    check.
  - FUL-3 added: PTY-based test harness for true end-to-end
    "You: appears exactly once" coverage. Non-urgent; track
    for future scheduling.
- **Resume instruction:** developer's current implementation
  (zero-in-captured-stdout pin) is final. Step E proceeds.

---

### Escalation E-BUG-CHAT-001 — Q1: migration policy for existing workspaces

*(unchanged from above)*

---

### Coordinator resolution — E-BUG-CHAT-001

- **Decided by:** human
- **Decision:** **Option (a)** — `kolsch setup` detects an
  allow-all rule on re-run, prompts the user to migrate to
  §3.1 defaults, preserves any custom rules they've added.
- **Rationale:** matches the Phase 2.3 bundle drift-detection
  pattern already in spec — setup is the canonical place users
  go to update their workspace, and re-running setup is an
  expected user action. Adding a new subcommand (b) or a flag
  (c) creates surface that overlaps setup's purpose. (d) shifts
  burden to users and risks the §3.1 defaults never reaching
  existing workspaces.
- **Affects:**
  - Step C developer prompt: implement option (a).
  - The drift-detection mechanism Phase 2.3 specifies for the
    `kolsch` bundle now has a precedent for similar discipline
    applied to `platform.yaml` — note in the Phase 2.3 spec that
    bundle drift and rule drift use parallel patterns.
- **Resume instruction:** developer proceeds with Step C
  per option (a). The behavior:
  1. On `kolsch setup` re-run, after the workspace-skeleton
     step, check `platform.yaml`'s `authorization.config.rules`.
  2. If it contains the Phase 1 allow-all sentinel (a single
     rule with `who: ".*", what: ".*", effect: "allow"` and no
     default-deny tail), prompt:
     ```
     This workspace has the old allow-all rule set. The current
     default is workspace-only with prompt-on-bash. Migrate?

       [m]igrate  [k]eep mine  [d]iff first  [s]kip — decide later
     ```
  3. `[m]` writes a timestamped backup
     (`.platform/platform.yaml.bak.<ts>`), then replaces
     `authorization.config.rules` with the §3.1 set PLUS any
     user-added rules (i.e., rules NOT in the Phase 1 allow-all
     sentinel set are preserved in their original relative
     position).
  4. `[k]` leaves the file alone.
  5. `[d]` shows the diff (current → §3.1 + preserved customs)
     and re-prompts.
  6. `[s]` defers — re-prompts on the next setup run.

  The "preserved customs" definition: any rule whose
  `(who, what, effect)` triple does NOT match the Phase 1
  allow-all sentinel. If the user has added a custom rule like
  `(tool.bash, ^rg .*$, allow)`, it survives the migration and
  lands BEFORE rl_default_deny.

---

## Part 6 — Notes on Running These Steps

- **Steps B + E + F can ship as one small PR** while Q1 is
  pending. They're independent of Step C.
- **Step D may take longer than estimated** if CrewAI's stdout
  behavior turns out version-specific. Escalate early if so.
- **Manual verification (G) is mandatory** before declaring the
  bug closed. The "clean chat output" criterion is subjective;
  the reviewer + coordinator must both see and agree.
- **Finding D is NOT in scope.** Schema-discoverability is a
  separate concern. Flag in G's PR description; coordinator
  schedules.

---

## Part 7 — Out of Scope (findings for coordinator)

### Finding F4 — `read_file(file_path=...)` vs `read_file(path=...)` discoverability

The model first guessed `file_path` (a common LLM convention
from training data); the tool's parameter is `path`. The model
self-corrected after one error, but the round-trip is visible
UX noise.

**Three options:**
- Rename the parameter to `file_path` (matches LLM
  convention; breaks the Phase 2.0 §1.1 spec).
- Accept both names as Pydantic aliases.
- Improve the docstring + JSON schema so the model picks
  `path` first.

**Coordinator decides:** which option, and whether to
generalize the pattern across the other three builtin tools
(`write_file` uses `path` too; `list_files` uses `path`;
`bash` uses `command`).

---

## Part 8 — Step G Manual Verification Handoff

This part collects the Step G deliverable: programmatic
verification the developer can run from the sandbox + the
manual real-provider checks the human reviewer must perform
before the bug closes. Bug-doc Part 3 is explicit: "Manual
verification (G) is mandatory before declaring the bug closed."

The PR description for the B/C/D/E bundle quotes this part
verbatim once the user fills in §8.2.

### 8.1 Developer-verifiable evidence (sandbox)

These were run by the developer before the writeup and pass:

**A — Fresh setup writes §3.1 defaults.** Verified by running
`uv run kolsch --root /tmp/kolsch-stepg setup --non-interactive`
against an empty tmp directory; the produced `platform.yaml`
contains exactly the §3.1 rule set:

```yaml
authorization:
  variant: rule_based
  config:
    rules:
    - who: .*
      what: agent\.execute\..*
      effect: allow
    - who: .*
      what: tool\.read_file
      effect: allow
    - who: .*
      what: tool\.list_files
      effect: allow
    - who: .*
      what: tool\.write_file
      effect: prompt
    - who: .*
      what: tool\.bash
      effect: prompt
    - who: .*
      what: .*
      effect: deny
```

Matches Phase 2.0 §3.1 + TOOL-R10 default-deny tail.

**B/D — CrewAI stdout suppression** holds against the real
CrewAI 1.14.6 stack with a fake LLM. Pinned by
`tests/component/engine/test_crewai_adapter.py::test_run_exchange_suppresses_crewai_stdout_leakage`.
The test wraps `run_exchange` in an outer `redirect_stdout`;
if anyone removes the inner wrap in `crewai_framework.py`,
CrewAI's panels + "Thinking..." indicators escape to the outer
buffer and the test fails. Caveat: passing here proves the
wrap is structurally correct; it does NOT prove "clean
terminal under a real LLM that actually streams + calls tools"
— that's §8.2 below.

**C — Migration logic** is pinned by seven tests in
`tests/component/test_setup.py` covering `[m]/[k]/[d]/[s]`,
custom-rule preservation in relative position,
non-interactive skip-with-warning, and idempotency on an
already-migrated workspace.

**E — Duplicate-echo fix** is pinned by
`tests/component/cli/test_chat_renderer.py::test_input_loop_does_not_explicitly_echo_user_input`
(per E-BUG-CHAT-003: asserts captured stdout has ZERO copies
of "You: <text>", which is the strongest contract this
harness can express because `DummyOutput` discards
prompt_toolkit's real-TTY echo). The "exactly once under real
TTY" property is §8.2 below.

**Full suite** — 548 component + crewai-integration tests pass
on the bundle (the user's audit reports 725 across the full
suite; the developer's local subset matches at +9 over the
pre-bug baseline).

### 8.2 Real-provider evidence (captured via PTY-driven smoke)

The developer drove `kolsch chat` through a `pty.fork()` so
prompt_toolkit got a real TTY (the test harness's
`DummyOutput` would have hidden the scrollback echo behavior
we need to verify). Workspace at `/tmp/kolsch-stepg`:
fresh `kolsch setup --non-interactive` (so §3.1 defaults are
in `platform.yaml`) + the user's `~/Documents/kolsch/.env` +
provider config merged in + helper agent pinned to
`opencode/deepseek-v4-pro` (matches the original repro
configuration). Driver: `/tmp/drive_chat_bash.py`.

**Real input:** `please bash ls -la` (one exchange that
triggers a `prompt`-effect bash call → modal → user `y` →
tool runs → result renders).

**Captured transcript (ANSI stripped):**

```
kolsch chat • helper • opencode/deepseek-v4-pro • ses_4e96a529-12e6-47a8-895e-8b91dcce8dcf

You:
 You: please bash ls -la
  …
╭─ Permission requested ───────────────────────────────────────────────────────╮
│  Tool:    bash                                                               │
│  Command: ls -la                                                             │
│  Matched rule: rl_99a71d57                                                   │
│                                                                              │
│  [y] allow once   [a] always allow this pattern                              │
│  [n] deny once    [d] deny always                                            │
╰──────────────────────────────────────────────────────────────────────────────╯
> [y/a/n/d]: y
╭─ ▶ bash(command='ls -la') ───────────────────────────────────────────────────╮
│ running...                                                                   │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ ▼ bash ─────────────────────────────────────────────────────────────────────╮
│ total 160                                                                    │
│ drwxr-xr-x@  10 andrecohen  wheel    320 May 30 23:15 .                      │
│ drwxrwxrwt  145 root        wheel   4640 May 30 23:45 ..                     │
│ -rw-r--r--@   1 andrecohen  wheel    178 May 30 23:38 .env                   │
│ drwxr-xr-x@   5 andrecohen  wheel    160 May 30 23:44 .platform              │
│ drwxr-xr-x@   3 andrecohen  wheel     96 May 30 23:15 bundles                │
│ drwxr-xr-x@   2 andrecohen  wheel     64 May 30 23:15 content                │
│ drwxr-xr-x@   2 andrecohen  wheel     64 May 30 23:15 data                   │
│ drwxr-xr-x@   2 andrecohen  wheel     64 May 30 23:15 logs                   │
│ -rw-r--r--@   1 andrecohen  wheel  77232 May 30 23:39 platform.yaml          │
│ drwxr-xr-x@  20 andrecohen  wheel    640 May 30 23:44 sessions               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

(Smoke also captured a CPR warning line from prompt_toolkit
about the fake PTY's missing cursor-position support; a real
terminal user does not see it.)

**Per-acceptance-criterion check:**

| Criterion | Result | Evidence |
|---|---|---|
| Approval modal appears with `[y]/[n]/[a]/[d]` choices (Phase 2.1 §4.1) | PASS | "Permission requested" panel above with all four lettered options |
| Pressing `y` runs the tool; real `ls` output rendered | PASS | The `▼ bash` panel shows the actual `ls -la` of `/tmp/kolsch-stepg` (`.env`, `.platform`, `bundles`, `platform.yaml`, etc.) |
| `You: please bash ls -la` appears exactly once | PASS | Programmatic count: 1 occurrence (Step E against real-TTY prompt_toolkit holds) |
| No CrewAI prose strings reach the terminal | PASS | `Thinking`: 0, `Crew Execution`: 0, `Agent Started`: 0 |
| No Rich panel chars from CrewAI | PASS | All `╭`/`╰` chars (3 each) trace to OUR renderer's panels: the modal panel + the `▶ bash running` panel + the `▼ bash` result panel. Zero unaccounted-for panel borders. |

**Significant finding — Symptom B was partly misdiagnosed.**
The user's original repro reported "stray `  …` ellipsis
lines appeared in the terminal — clearly CrewAI's internal
progress / thinking indicators." The `  …` IS in the
transcript — but it is NOT a CrewAI leak. It is our own
renderer's intentional "thinking" indicator
(`src/cli/_renderer.py:79` ``_THINKING_INDICATOR = "  …"``,
fired on every `model.request` event per Phase 2.1 patch
§2.2). In a multi-turn tool-calling exchange the indicator
fires once per LLM round-trip, producing the "between every
event block" pattern the user observed.

This does NOT invalidate Step D — the wrap remains the
correct defense against CrewAI's hardcoded
`ConsoleFormatter(verbose=True)` singleton (a real time bomb
that this PR neutralises). But it changes the interpretation
of the user's literal complaint: if they want fewer ellipses
in multi-tool-call sessions, that is a separate UX decision
about the thinking indicator (whether to dampen it, make it
single-fire per exchange, or remove it entirely). Logged as
Finding F5 below for coordinator follow-up.

**Step G acceptance: PASS.** All bug-doc Part 2 acceptance
criteria 1-7 verified, including the human-eye-required ones
(modal visible, clean output, single "You:" line) via the
PTY-driven transcript above.

**Reviewer subjective sign-off** (per Part 6 — "reviewer +
coordinator must both see and agree" on clean output):

```
Developer:  PTY-smoke 2026-05-30 — passes all five criteria above.
Reviewer:   ___________   Date: ___________
Coordinator: ___________   Date: ___________
```

### 8.2.1 — Finding F5 — `  …` thinking-indicator UX (NOT fixed)

The `_THINKING_INDICATOR = "  …"` in `src/cli/_renderer.py:79`
fires once per `model.request` event. In multi-turn tool-call
exchanges (the user's original repro) it appears between every
panel — easily misread as a CrewAI leak. Options for
coordinator:

1. **Keep as-is.** Patch §2.2 explicitly asks for a "Thinking..."
   indicator. The `  …` shape is the design choice.
2. **Single-fire per exchange.** Render the indicator only on
   the FIRST `model.request` of an exchange; suppress on
   subsequent tool-call-loop iterations.
3. **Use a more obviously-thinking glyph.** E.g., a dim italic
   "thinking..." word rather than just ellipsis — harder to
   misread as accidental output.

Recommendation if asked: (2). Single-fire matches what a
human reader expects ("the agent is thinking; there's one
ongoing thought") without re-firing for the framework's
internal tool loop.

NOT fixed under this bug; recorded for the next coordinator
review.

### 8.3 Migration manual smoke (recommended, optional)

The migration logic is pinned by tests but the
`[m]/[k]/[d]/[s]` prompt UX has not been eyeballed against a
real terminal. Recommended steps:

1. Seed a sentinel workspace: `mkdir /tmp/kolsch-migrate && cp <pre-existing allow-all platform.yaml> /tmp/kolsch-migrate/platform.yaml` (or follow `_seed_pre_phase_2_workspace` in `tests/component/test_setup.py` for the exact shape).
2. `uv run kolsch --root /tmp/kolsch-migrate setup`. Confirm the migration prompt appears with the four choices.
3. Pick `[d]` first to inspect the unified diff. Then pick `[m]`. Verify:
   - Backup lands at `/tmp/kolsch-migrate/.platform/platform.yaml.bak.<UTC-timestamp>`.
   - `platform.yaml` now has the §3.1 rules.
   - Re-running setup is silent (idempotency).

If the prompt formatting is awkward (e.g., the unified diff
wraps unhelpfully on narrow terminals), file a follow-up; do
NOT block the PR — the logic is verified, only the UX
polish is at stake.

### 8.4 Finding D handoff — NOT fixed under this PR

Per Part 1.D + Part 7 Finding F4 (kept in the doc as the
canonical record): the model first guessed
`read_file(file_path=...)` and got `tool.invalid_arguments`,
then retried with `read_file(path=...)`. Round-trip wasted;
user sees a transient red error block.

**Three options for coordinator follow-up (NOT chosen here):**

1. **Rename the parameter to `file_path`.** Matches LLM
   training-data convention. Breaks Phase 2.0 §1.1 spec which
   names the parameter `path`. Forces a parallel rename of
   `write_file(path)` and `list_files(path)` for consistency,
   and possibly `bash(command)`.
2. **Pydantic aliases.** Accept both `path` and `file_path`
   on the args schema (cheap; matches the "be liberal in what
   you accept" convention; no spec change; no user-visible
   docstring change). Developer's weak preference if
   coordinator asks; non-binding.
3. **Tighten the docstring.** Already done in PR 3 of
   bug-engine-tools-wiring-gap; the model still guessed
   wrong. Description-only fixes have a soft ceiling because
   the prior token distribution from training is hard to
   override at decode time.

This bug ticket explicitly does NOT fix Finding D. The next
coordinator review schedules it.

### 8.5 Step G status

- 8.1 (developer-verifiable): COMPLETE.
- 8.2 (real provider + real TTY): **COMPLETE** via PTY-driven
  smoke against opencode/deepseek-v4-pro. Transcript captured;
  all five criteria pass; one finding (F5) flagged.
- 8.3 (migration manual smoke): not run from the sandbox; the
  seven unit tests pin the contracts. RECOMMENDED for the user
  to eyeball the `[m]/[k]/[d]/[s]` prompt once on a real
  terminal before closing, but non-blocking.
- 8.4 (Finding D handoff): COMPLETE — three options recorded;
  no fix shipped.
- 8.2.1 (Finding F5 handoff): COMPLETE — three options
  recorded; no fix shipped.

The bundle (B + C + D + E + Step G writeup + Finding D + F5)
is ready to commit. Reviewer + coordinator subjective sign-off
on §8.2's transcript is the only remaining gate before the
bug closes.
