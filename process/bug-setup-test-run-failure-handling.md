# Bug — `kolsch setup` mishandles failed test-run and picks an unverified model

**Draft 0.1** *(investigation ticket, not a phase patch)*

> Reported symptom: a user ran `kolsch setup` against a workspace
> with OpenCode and OpenRouter already connected. Setup created the
> starter agent with model `opencode/minimax-m2.7`, ran the optional
> test exchange, the LLM call failed with HTTP 500 from the
> upstream provider — and setup proceeded to print "Kolsch is
> ready" anyway, suggesting `kolsch connect` even though the
> credentials weren't the problem. Three distinct UX failures
> nested in one event: a model picker that didn't sanity-check the
> choice, a success banner that fires on partial completion, and a
> diagnostic that points at the wrong cause.
>
> This document is for the developer agent (to investigate and
> fix) and the reviewer agent (to verify root cause, minimal fix,
> regression tests, and that the partial-completion exit code from
> Phase 1.2 spec is finally honored). Coordinator section included
> per the pattern established in Phase 2.0 build.

---

## Part 0 — Symptom & Reproduction

**Reported by:** user, on `main`.

**Setup:**
1. Workspace at `~/Documents/kolsch`, already initialized.
2. Two providers already connected: `opencode`, `openrouter`.
3. User runs `uv run kolsch setup` (re-run, not fresh).

**Actual output (verbatim from the report, abridged):**
```
Workspace location: /Users/andrecohen/Documents/kolsch
  Press Enter to use this path, or re-run with --root or KOLSCH_WORKSPACE to change it.
✓ workspace already initialized at /Users/andrecohen/Documents/kolsch
✓ provider(s) already connected: opencode, openrouter
Create starter agent at .../bundles/kolsch/agents/helper.yaml? [Y/n] Y
  ✓ materialized bundle at bundles/kolsch/
  ✓ wrote starter agent at bundles/kolsch/agents/helper.yaml
  → starter agent uses opencode/minimax-m2.7
Run the helper agent once to confirm everything works? [Y/n] y
Running kolsch/agents/helper...
ERROR root: OpenAI API call failed: Error code: 500 - {'type': 'error',
  'error': {'type': 'error', 'message': 'Internal server error'}}
ERROR root: OpenAI API call failed: Error code: 500 - {'type': 'error',
  'error': {'type': 'error', 'message': 'Internal server error'}}
[CrewAIEventsBus] Warning: Ending event 'llm_call_failed' emitted with
  empty scope stack. Missing starting event?
╭─ ❌ LLM Error ─╮
│  LLM Call Failed
│  Error: OpenAI API call failed: Error code: 500 ...
╰────────────────╯
(second identical panel — looks like a retry)

ERROR cli: agent execution failed: Agent execution failed at event
  ev_ca72d2cc-...
Traceback (most recent call last):
  File ".../src/cli/main.py", line 351, in _cmd_run
    agent_msg = await runtime.engine.execute(config, ctx)
  File ".../src/engine/engine.py", line 206, in execute
    raise AgentExecutionError(failed_event)
engine.context.AgentExecutionError: Agent execution failed at event
  ev_ca72d2cc-...

  → test run failed (rc=2); check provider credentials or re-run `kolsch connect`

Kolsch is ready.

Try:
  kolsch run kolsch/agents/helper --input content="<your prompt>"
  kolsch sessions list
  kolsch --help

Workspace: /Users/andrecohen/Documents/kolsch
Docs:      docs/tutorials/first-agent.md
```

**Three distinct failures in that output:**

1. **Wrong model picked.** Setup chose `opencode/minimax-m2.7` from
   the OpenCode catalog without sanity-checking it. The upstream
   returned 500 — likely a real upstream issue, but a "verified
   working at connect time" picker would have avoided it.
2. **"Kolsch is ready" prints despite test failure.** The success
   banner fires unconditionally after the test step, regardless of
   whether the test actually passed.
3. **Misleading diagnostic.** "Check provider credentials or
   re-run `kolsch connect`" implies an auth problem. Credentials
   are fine; OpenCode's `discover_models` worked, the connector
   built the LLM, the call reached the endpoint. The endpoint
   returned 500 from the upstream model.

The Phase 1.2 spec's exit-code contract ("Exit code 0 on full
success, 4 on partial completion") almost certainly is not being
honored either — the user's shell returned with no visible error
code, but the "Kolsch is ready" banner reads like a 0-exit.

---

## Part 1 — Hypotheses (ranked by likelihood)

### H1. Setup's "Test run" step doesn't gate the success banner

`src/cli/_setup.py`'s orchestrator calls Step E (test run) then
unconditionally proceeds to Step F (ready banner). The Step E
function returns a status — proceeded/skipped/aborted — but the
orchestrator either ignores it or maps all of them to "continue."

- **Where to look:** `src/cli/_setup.py` orchestrator. Find the
  Step E → Step F transition. Confirm whether Step E's failure
  short-circuits or merely logs and falls through.

### H2. Exit code 4 (partial) is never produced

Phase 1.2 spec §2.5 promises "Exit code 0 on full success, 4 on
partial completion (e.g., skeleton created but no provider
connected)." A failed test run is partial completion by
definition. The CLI's `_cmd_setup` probably returns 0 unless an
exception escapes — which it doesn't, because Step E catches
`AgentExecutionError`.

- **Where to look:** the return value chain from Step E up to
  `_cmd_setup`. Verify exit code on the failure case.

### H3. The diagnostic text is hardcoded

The line `→ test run failed (rc=2); check provider credentials
or re-run kolsch connect` is canned advice that doesn't inspect
the actual failure. The failing event carries an error code in
the JSONL (`llm.server_error` or similar per Phase 1.3 spec's
classification, though 1.3 isn't implemented yet). Setup should
read it and tailor the hint.

- **Where to look:** `src/cli/_setup.py` Step E's failure branch.
  Confirm the message is a string literal, not derived from
  the failed event.

### H4. The model picker selects an arbitrary discovered model

Setup's Step D picked `opencode/minimax-m2.7`. Two sub-questions:

  - **H4a.** Did setup default to the first discovered model? The
    first model in OpenCode's `discover_models` response order is
    arbitrary and may not be reliable.
  - **H4b.** Did setup pick from `model_aliases` (the curated
    list) and fall back to a raw discovery only if no aliases
    existed? If `opus`/`haiku`/`gpt4o` aliases were already
    written by `connect openrouter`, those should have been
    preferred over a raw OpenCode model.

- **Where to look:** Step D's model-selection logic. Trace the
  default chain. Confirm what "preferred from your connected
  providers' aliases" actually means in code.

### H5. The CrewAI events bus warning is a latent bug

```
[CrewAIEventsBus] Warning: Ending event 'llm_call_failed' emitted
with empty scope stack. Missing starting event?
```

This suggests CrewAI is emitting a "failed" event for an event
chain it doesn't have an open scope for. Likely related to the
retry — CrewAI's internal retry emitting a failure event after
its own scope already closed. Not user-facing severity, but worth
noting; almost certainly an unrelated CrewAI/integration issue.

- **Investigation depth:** light. Confirm it's CrewAI-internal,
  not our code, and file as an upstream note.

### H6. The upstream 500 is real

OpenCode's `/v1/chat/completions` returned 500 with body
`{'type': 'error', 'error': {'type': 'error', 'message':
'Internal server error'}}`. This may be a genuine upstream
problem (Minimax model down, OpenCode routing layer error). The
fix here is NOT to make the 500 go away — it's to make setup
handle a failed test run gracefully. H6 is the **trigger**, not
the **bug**.

### H7. Phase 1.3 fallback would have caught this

If `phase1.3-fallback-specs.md` were implemented, the starter
agent would have had `defaults.fallback_models: [haiku]` (or
similar), and the 500 would have triggered a fallback to a
working model. Out of scope for this fix — but worth noting that
the bug is partly an absence of 1.3.

---

## Part 2 — Investigation Strategy

Observational first; do not modify code yet.

### Step A — Trace the Step E → Step F flow

Open `src/cli/_setup.py`. Find the orchestrator function. Read
the exact code path from "test run completed" to "print ready
banner." Document:

- Does Step E return a result?
- Does the orchestrator check it?
- Where does `→ test run failed (rc=2)` come from?
- Where does "Kolsch is ready" come from?
- What's the function's return value to the CLI dispatcher?
- What's the CLI dispatcher's exit-code mapping?

This nails H1, H2, and H3.

### Step B — Reproduce with a deterministic failure

Don't depend on OpenCode's flaky 500. Inject a failure
deterministically: configure the starter agent with a model alias
that resolves to a fake provider rigged to return an error. Run
setup; observe the same three failures (or not). This isolates
the setup bugs from the upstream symptom.

The Phase 2.0 spec already requires an `allow_all_test` variant;
similar discipline would help here, but for now the fake LLM with
error injection (introduced by Phase 1.3 build plan Step 20) is
the right tool. If that fixture doesn't exist yet, escalate per
the coordinator section — see §"Coordinator Escalation Log."

### Step C — Inspect the model picker

Read `src/cli/_setup.py`'s Step D (starter agent materialization).
Specifically:

- The line that fills in the agent's `model:` field.
- The preference chain: aliases first vs. raw discovery first.
- What happens when both providers (opencode and openrouter)
  have discovered models — which one wins?
- What happens when openrouter has aliases (`opus`, `haiku`,
  `gpt4o`) but opencode has only raw model IDs?

Confirm H4a vs H4b. The behavior the user saw —
`opencode/minimax-m2.7` chosen over presumably-existing
`openrouter`-based aliases — suggests opencode raw IDs won the
race. That's almost certainly wrong.

### Step D — Inspect exit code

After setup runs, check `echo $?`. If 0, confirm H2. If 4, the
exit code is right but the banner is wrong (still a bug, but
narrower).

### Step E — Read the diagnostic line's source

`grep -n 'check provider credentials' src/cli/`. Confirm the
string is hardcoded. Read the surrounding context — is there a
classifier function that could be informed by the failure
event?

### Step F — Decision criteria

- **If Step A confirms Step E's result is ignored** → H1
  confirmed. Fix: orchestrator gates Step F on Step E's outcome.
- **If Step D shows exit 0 despite test failure** → H2 confirmed.
  Fix: return 4 from `_cmd_setup` when any step's status is
  "failed."
- **If Step E shows the diagnostic is canned text** → H3
  confirmed. Fix: derive from the failed event's error code.
- **If Step C shows the model picker prefers raw discovery over
  aliases** → H4 confirmed. Fix: alias-first preference.
- All four can be true simultaneously. The fix likely touches
  one file (`src/cli/_setup.py`) with four narrow edits.

---

## Part 3 — Acceptance Criteria

The bug is fixed when **all** of the following hold:

1. When the test exchange in setup fails, the success banner
   ("Kolsch is ready") does NOT print. Instead, setup emits a
   partial-completion banner that names: the failed step, the
   workspace path (so the user knows it's there), and one
   targeted next-step suggestion derived from the failure event's
   error code (not a hardcoded string).
2. The process exit code on partial completion is exactly **4**
   per Phase 1.2 spec §2.5. Full success is **0**. A user pressing
   `[n]` on the test-run prompt (declining the test) is full
   success — exit 0 — because they actively skipped, not because
   the test failed.
3. The starter agent's `model:` field prefers a model alias from
   `model_aliases` over a raw discovered model ID, when both are
   available. Tie-breaker: deterministic ordering documented in
   the docstring (e.g., `opus` > `smart` > `cheap` > the
   alphabetically-first alias > raw provider/model).
4. The diagnostic line ("check provider credentials or re-run
   `kolsch connect`") is replaced with a classifier-driven hint:
     - `model.server_error` / 5xx → "the model returned a 5xx;
       this is usually transient. Try again, or pick a different
       model with `kolsch permissions` / by editing the agent."
       (Phase 2.0 lands `kolsch permissions`; if it hasn't yet,
       use the agent-edit path.)
     - `model.authentication_failed` / 401/403 → "credentials
       failed; re-run `kolsch connect <provider>`."
     - `llm.provider_not_configured` → "the agent's provider isn't
       connected; run `kolsch connect <provider>`."
     - Default (any other code) → a generic "the test exchange
       failed; check logs and re-run setup or `kolsch run` directly."
5. A new regression test reproduces the bug on pre-fix code and
   passes after. The test runs setup non-interactively with a
   fake-LLM-injected 5xx response, asserts the partial-completion
   banner appears (not the "ready" banner), asserts exit code 4,
   and asserts the diagnostic mentions "model returned a 5xx" (or
   equivalent) rather than "check provider credentials."
6. The Phase 1.2 success criteria §10.13 line "kolsch setup
   --non-interactive produces a runnable workspace" remains
   true for the success path. The new partial-completion exit
   code is observable via TC-UX-05's exit-code assertion.
7. No existing test regresses.

---

## Part 4 — Out of Scope

- **Making OpenCode's 500 stop happening.** That's upstream.
- **Implementing Phase 1.3 model fallback.** Out of scope (per
  H7); the fix here makes the failure handle-able, not
  recoverable.
- **The CrewAI events-bus warning (H5).** Note in the fix
  commit's body that it's upstream and untouched; do not chase
  it.
- **Wholesale rework of setup's flow.** This is a four-edit bug
  in `src/cli/_setup.py`. If the investigation reveals a larger
  structural problem, escalate via the coordinator section
  rather than ambushing the scope.

---

## Part 5 — Developer Prompt

```
[standing instructions]

You are investigating a bug in `kolsch setup`: when the test
exchange step fails, setup nonetheless declares "Kolsch is
ready," exits 0, and gives a misleading diagnostic. Additionally,
the starter agent's model picker chose a raw OpenCode model over
the available OpenRouter aliases, surfacing a transient upstream
500 that should never have been the model in use.

Read first:
- process/bug-setup-test-run-failure-handling.md (this
  ticket — the hypotheses, investigation strategy, acceptance
  criteria).
- process/phase1.2-usability-specs.md (the setup contract;
  especially §2.2 Step E, §2.4 starter agent, §2.5 exit codes).
- src/cli/_setup.py (the orchestrator and step functions).
- src/cli/main.py (the dispatcher's exit-code mapping for
  setup).

WORK IN TWO PHASES — investigation, then fix.

Phase A — Investigation (no code changes yet).

1. Follow the investigation strategy in §2 of the ticket, Steps
   A through E. Record findings for each.

2. Build a deterministic reproduction: a unit/component test
   that runs setup --non-interactive with a fake LLM rigged to
   return a 5xx on the test-exchange call. Observe the three
   failures (banner, exit code, diagnostic). If the fake LLM
   error-injection API doesn't exist yet (Phase 1.3 hasn't
   landed), see the escalation path below — escalate, do not
   build a parallel fixture.

3. Produce a root-cause writeup naming which hypotheses (H1–H4)
   hold, with file/line citations. H5 (CrewAI events bus) is
   acknowledged as upstream and untouched; H6 (the upstream
   500) is the trigger not the bug; H7 (Phase 1.3 fallback)
   is noted as out of scope.

4. STOP. Surface the writeup before writing any fix. If the
   root cause is bigger than four edits to _setup.py (e.g.,
   the orchestrator needs restructuring), escalate per the
   coordinator section — don't grow scope unilaterally.

Phase B — Fix (after coordinator "proceed" or unambiguous match
to the four-edit estimate).

5. Make the four edits in src/cli/_setup.py:

   (a) Step E (test run) returns a richer result — an enum or
       small dataclass with at least: outcome ∈ {pass, fail,
       skipped}, failure_code (optional, the error code from the
       failed event), failure_detail (optional, human-readable).

   (b) Orchestrator branches on Step E's outcome:
       - pass    → print the existing "Kolsch is ready" banner;
                   return SetupResult(exit_code=0)
       - skipped → print a "Kolsch is ready (test skipped)"
                   banner; return SetupResult(exit_code=0) — the
                   user actively skipped.
       - fail    → print a partial-completion banner (NOT
                   "ready"); name the failed step, the
                   workspace path, and the classifier-derived
                   hint (criterion 4); return
                   SetupResult(exit_code=4).

   (c) New helper `_classify_failure(failure_code: str) -> str`
       maps error codes to user-facing hints per acceptance
       criterion 4. Default branch (unknown code) gives the
       generic hint. Single source of truth, easy to extend
       when Phase 1.3 lands new model.* codes.

   (d) Step D's model picker: prefer aliases from
       `platform.yaml`'s `model_aliases` over raw discovered
       models. Tie-breaker order documented in the function's
       docstring: opus > smart > cheap > first alphabetical
       alias > raw provider/model (first by provider precedence
       openrouter > opencode > others, then alphabetical).

6. Update the CLI dispatcher (src/cli/main.py) to translate
   SetupResult.exit_code into the process exit. If Setup
   already returns an int, the change may be smaller — just
   ensure 4 is propagated on partial completion.

7. Add a regression test in tests/component/test_setup.py (or
   the existing TC-UX-* file):

   - TC-UX-11 (new): setup --non-interactive with fake LLM
     injected to return 5xx on test exchange. Assert:
       * partial-completion banner appears in stdout
       * "Kolsch is ready" string does NOT appear in stdout
       * exit code == 4
       * the diagnostic line mentions "5xx" or "server" (the
         classifier's hint for model.server_error)
       * platform.yaml and starter agent exist (workspace IS
         set up; only the test failed)

   - TC-UX-12 (new): setup --non-interactive with two providers
     connected, openrouter has aliases, opencode has raw model
     IDs. Assert: starter agent's model is an openrouter alias
     (e.g., "opus"), NOT a raw opencode/<model> string.

   - TC-UX-13 (new): setup with user declining the test run
     ([n] in interactive, or the non-interactive equivalent).
     Assert: success banner appears with the "(test skipped)"
     suffix; exit code 0.

8. Run the full test suite. Acceptance criterion 7 — zero
   regressions. Existing TC-UX-05 may need updating if it asserts
   exit code 0 unconditionally; that assertion is correct for
   the success path and should stay; add the partial-completion
   path as TC-UX-11.

ESCALATION PATH: if the investigation reveals (a) the fake-LLM
error-injection API from Phase 1.3 doesn't exist (you can't build
TC-UX-11 deterministically without it), (b) the setup orchestrator
needs restructuring beyond four narrow edits, or (c) the model
picker's "alias preference" interacts with code paths the spec
doesn't cover — append a structured entry to the "Coordinator
Escalation Log" at the END of this bug doc using the template in
§"How to escalate" below, and STOP. The coordinator decides
whether to expand scope, reach a temporary compromise, or wait
for Phase 1.3 to land.

Commit: fix(cli,setup): gate ready-banner + exit-code on test
outcome; pick model from aliases over raw discovery; classify
failure into actionable hint.

Body: the root-cause paragraph and the four-edit summary. Cite
this ticket.

Report:
- The root-cause writeup from Phase A.
- The four edits (file + line ranges).
- TC-UX-11/12/13 added; pass/fail.
- Any escalations raised and their resolutions.
- One-line note on the CrewAI events-bus warning (H5) confirming
  it's upstream and untouched.
```

---

## Part 6 — Reviewer Prompt

```
[standing instructions]

Review the setup test-run failure-handling fix.

Read first:
- process/bug-setup-test-run-failure-handling.md (the
  ticket — Part 3 acceptance criteria are your checklist).
- The developer's root-cause writeup in the PR description.
- The escalation log at the end of this ticket.
- process/phase1.2-usability-specs.md (the setup contract).
- The diff.

Architectural / correctness review:

1. The root-cause writeup names hypotheses (H1–H4) with file/
   line citations. Hand-wavy "something in setup" → fail.

2. The fix is FOUR edits in src/cli/_setup.py (plus optional
   small CLI dispatcher edit). If the diff rewrites the
   orchestrator or restructures the step functions, that's
   over-reach — verify the coordinator approved the expanded
   scope in the escalation log, or fail.

3. The Step E result is now a structured value, not a bool. The
   orchestrator dispatches on it explicitly. No "if status !=
   None: return" sloppy short-circuits.

4. The model picker's preference chain is documented in the
   function's docstring — explicit order — and the picker
   reads from platform.yaml's model_aliases section, not from a
   hardcoded list.

5. The _classify_failure helper is a single function with a
   table-driven body. New error codes added later need to
   extend this table — confirm a docstring or inline comment
   makes that obligation visible.

Functional review:

6. Reproduce the original bug on pre-fix code: git stash the
   fix, run TC-UX-11, confirm it FAILS in the expected way (the
   "Kolsch is ready" string appears in stdout, exit code is 0,
   diagnostic mentions credentials). Restore the fix and confirm
   PASS.

7. TC-UX-12 specifically: read the assertion. It must check the
   agent's `model:` field is an alias string (e.g., "opus"),
   NOT just "any non-empty string." A test that passes whether
   the value is "opus" or "opencode/minimax-m2.7" doesn't catch
   the bug.

8. TC-UX-13 specifically: the user-declined path. The banner
   should still say "ready" — declining the test isn't a
   failure. Verify this works without the user feeling like
   they've broken something.

9. Manual end-to-end test: fresh workspace, run `kolsch setup`
   with a provider whose first-discovered-model is known broken
   (or mock it). Confirm:
     - The model picker chose an alias, not the broken model.
     - If you force a 5xx via the fake-LLM injection, the
       partial-completion banner appears and the hint mentions
       the model server error, not credentials.
     - Exit code is 4 (verify with `echo $?`).

10. The four error-code classifications in acceptance criterion
    4 — confirm each one produces the right hint by passing it
    through _classify_failure in a unit test. Five entries:
    server_error, auth_failed, provider_not_configured, the new
    default, and one more (e.g., model.timeout) to verify the
    table is extensible.

11. Run FULL test suite. Acceptance criterion 7.

The "the diagnostic is actually helpful" review:

12. Read each of the four classifier outputs as if you're a new
    user. Could you act on the hint? "Check provider
    credentials" was uselessly broad; the new hints should each
    point at a specific corrective action.

13. Phase 1.3 fallback note. The developer's report should
    mention H7 (1.3 would have caught this). Read the report;
    confirm the note exists and is accurate — not a promise to
    implement, just a "this would be cleaner with 1.3."

14. Escalation log review. Any open escalations? Any resolved
    escalations whose resolutions the fix should have honored?
    Confirm.

Verdict: PASS or FAIL with specific defects citing file + spec
section, plus any unresolved escalations.
```

---

## Part 7 — How to Escalate (developer + reviewer)

Either agent escalates by appending an entry to the "Coordinator
Escalation Log" at the bottom of this doc using this template.
After appending, STOP work on the affected item and await
coordinator resolution.

```markdown
### Escalation E-BUG-SETUP-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Phase / Step:** investigation / fix / review
- **Affected item:** acceptance criterion N, hypothesis H<n>, or
  a specific spec section.
- **Trigger:** concrete observation, with file/line citations.
- **Why this is not a guess:** what you tried, what the spec
  said, why the spec doesn't cover the case.
- **Options as you see them:** 2–3 paths forward with trade-offs.
- **Estimated cost of each:** rough effort and blast radius.

---

### Coordinator resolution — E-BUG-SETUP-<NNN>

- **Decided by:** human
- **Decision:** which option, or a fourth path.
- **Rationale (one sentence):**
- **Affects:** which other patches/phases need to know.
- **Resume instruction:** what the developer/reviewer does next.
```

When to escalate:
- Phase 1.3 fake-LLM error-injection isn't available and you
  can't build TC-UX-11 deterministically without it.
- The four-edit estimate is wrong; the fix actually needs a
  setup-orchestrator restructure.
- A classifier mapping is ambiguous (e.g., an error code that
  could be either auth or model issue).

When NOT to escalate:
- The exact wording of a hint message. Pick a reasonable string;
  the test asserts on key substrings, not full match.
- The CrewAI events-bus warning. Leave it alone.

---

## Coordinator Escalation Log

*(no entries yet)*

---

## Part 8 — Notes on Running This Investigation

- **Three bugs, one fix.** The model-picker bug, the
  banner-gating bug, and the diagnostic-text bug all surface
  during the same setup flow, all live in `_setup.py`, and all
  should be fixed in the same PR. Splitting them buys nothing.
- **The CrewAI events-bus warning is upstream noise.** Don't
  chase it. One-line acknowledgement in the commit body.
- **Phase 1.3 would have made this softer.** With the fallback
  feature, a single 500 wouldn't have killed the test exchange.
  But the bug here is "setup mishandles failure," not "failure
  shouldn't happen" — fix the handler now, ship 1.3 later.
- **The "model picker prefers aliases" change is a Phase 1.2
  spec strengthening.** The original spec said "the first alias
  in priority order (opus > smart > the user's chosen alias)"
  but the code apparently went the other way. After this fix
  lands, consider tightening Phase 1.2 §2.4 to be explicit about
  the precedence chain — flag for a Phase 1.2 revision, not a
  required edit here.
