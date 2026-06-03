# Bug — Framework loop cap surfaces as `platform.internal_error`; remove the cap

**Draft 0.1** *(three dependency-ordered steps, each with focused dev + reviewer prompts.)*

> Reported symptom: a `kolsch chat` exchange against a real LLM
> (qwen3.6-plus via opencode) failed mid-tool-use with:
>
> ```
> [exchange failed — platform.internal_error:
>   Internal platform error: engine.fake_framework.FakeFrameworkLoopExceededError]
> ```
>
> Reproduced twice in the same session
> (`ses_7af67129-ede8-4b07-80fb-e0d89c8b38da`): each failing
> exchange shows exactly 10 `model.request` / 10 `tool.called`
> events before the error event. The agent (`helper`) had four
> tools wired (`read_file`, `write_file`, `list_files`, `bash`)
> and was doing legitimate multi-step work.
>
> Root cause confirmed:
>
> 1. **Hardcoded cap**: `src/engine/engine.py:1102` passes
>    `max_iterations=10` to `Framework.run_exchange`.
> 2. **Cap raises a non-PlatformError**:
>    `src/engine/fake_framework.py:148-152` raises
>    `FakeFrameworkLoopExceededError` (a `RuntimeError`).
> 3. **Engine boundary mis-classifies**: `src/engine/engine.py:364-367`
>    catches any non-PlatformError and wraps it as
>    `PlatformInternalError` → `platform.internal_error`.
>    A legitimate "loop ran out of budget" condition surfaces as
>    if the engine crashed.

---

## Part 0 — Why this matters

Three problems compound:

1. **The cap is wrong for production.** 10 iterations means at
   most 9 tool calls before the exchange dies. Real multi-step
   work (read → inspect → write → verify → run tests → fix → re-run)
   hits this constantly. Other agentic CLIs (Claude Code,
   opencode, Aider) don't cap iterations at all — they rely on
   user interrupt + context-window limits. CrewAI's own default
   is 25 (per `Agent.max_iter`), and LangGraph ReAct sits at
   ~15-25 — 10 is a test-fixture value, not a production budget.

2. **The error code is a lie.** `platform.internal_error` is
   reserved for "the engine itself crashed" (`src/errors/engine.py:202-211`).
   A loop budget being exhausted is not an engine crash; it's a
   normal terminal condition the framework chose. Mis-classifying
   it sends users hunting for a code bug that doesn't exist.

3. **FakeFramework's docstring is aspirational, not real.**
   `src/engine/fake_framework.py:48-55` claims the engine "catches
   it at the boundary and wraps it as a Fatal `model.retry_exhausted`
   (or similar) per spec §1.6." No such wrap exists. The
   docstring sets the wrong expectation for anyone reading
   FakeFramework as the reference loop.

**Design decision (made before this doc):** rely on the user's
Esc / cancellation to stop runaway loops, the way Claude Code
and opencode do. **Don't introduce a continue-prompt UX.** The
cancellation seam is already in place (`cancellation: Callable[[], bool]`
on `Framework.run_exchange`); use it.

---

## Part 1 — What we already know

This is not a hypothesis-search bug — the cause is confirmed
above. Step 1 is a small verification pass before changing the
protocol; it's not "figure out what's broken."

### Known facts (with citations)

- `Framework` protocol declares `max_iterations: int` as a
  required kwarg (`src/engine/framework.py:195`).
- `FakeFramework.run_exchange` defaults it to `_DEFAULT_MAX_ITERATIONS = 10`
  (`src/engine/fake_framework.py:45, 72`) and raises
  `FakeFrameworkLoopExceededError` (a `RuntimeError`) on
  exhaustion (lines 148-152).
- `CrewAIFramework.run_exchange` also defaults to 10 and forwards
  to CrewAI's `Agent(max_iter=max_iterations)`
  (`src/engine/crewai_framework.py:103, 120, 152`).
- Engine call site is `src/engine/engine.py:1102` (literal
  `max_iterations=10`).
- Engine boundary wraps non-PlatformError as `PlatformInternalError`
  (`src/engine/engine.py:364-367`).
- The `RuntimeSpec` config (`src/schemas/config.py:66-80`)
  carries `max_retries` and `timeout_seconds`. It does NOT have
  `max_iterations` and **we are not adding it** under this fix
  (no per-agent cap; rely on Esc).
- `FakeFrameworkLoopExceededError` is exported from
  `src/engine/__init__.py:35, 67` — anyone importing it from
  the public engine surface will break when it's removed.
- Cancellation is polled at the top of each iteration in
  FakeFramework (`fake_framework.py:88-90`). CrewAI's adapter
  threads `cancellation` through `_CrewAILLM` so the model-call
  thread checks it (`src/engine/crewai_framework.py:127-134`,
  and follow-on in `_CrewAILLM.call`).
- Golden tests `tests/golden/tc_eng_02_tool_call*.jsonl`
  exercise tool-call loops; verify they don't depend on the
  cap.

### What Step 1 confirms before code lands

- Every call site of `Framework.run_exchange` (in `src/`, `tests/`,
  and any starter bundle). Specifically, anything that pins
  `max_iterations=` will break if the kwarg becomes optional.
- The exact value to pass CrewAI's `Agent.max_iter` when "no
  cap" is requested. CrewAI requires an int; we'll pass a
  sentinel-large value (e.g. `10**9`). Step 1 confirms CrewAI
  accepts a value this large without overflow / validation
  rejection in the pinned version.
- Whether any test asserts `FakeFrameworkLoopExceededError` is
  raised, the way `tests/unit/test_errors.py` and any
  fake-framework unit test do. Those tests get
  removed / repurposed in Step 2.
- That the cancellation seam is sufficient on its own — i.e.
  Esc during a long loop actually fires `cancellation()` and
  the framework returns promptly. If the only poll site is
  "top of each iteration," a stuck tool call won't observe
  cancellation until the tool returns. That's existing behavior
  (out of scope for this bug), but Step 1 should NOTE it so a
  follow-up ticket can be filed if needed.

---

## Part 2 — Acceptance criteria (applies across all steps)

The bug is fixed when **all** hold:

1. The hardcoded `max_iterations=10` at `src/engine/engine.py:1102`
   is gone. The engine no longer imposes a per-exchange iteration
   budget.
2. `FakeFramework.run_exchange` loops without an iteration cap.
   `FakeFrameworkLoopExceededError` is **deleted** (the class,
   the raise site, and the public re-export from
   `src/engine/__init__.py`).
3. `CrewAIFramework.run_exchange` either drops `max_iterations`
   from its signature or treats `None` as "no cap" and forwards
   a sentinel-large value to CrewAI's `Agent.max_iter`. CrewAI
   continues to receive a valid int.
4. The `Framework` protocol (`src/engine/framework.py:187-197`)
   is updated to match — either the kwarg is dropped, or it's
   typed `int | None = None`. Pick one shape consistently across
   protocol + both implementations.
5. The original failing repro (real LLM, multi-step tool work
   past 10 iterations) no longer produces
   `platform.internal_error: ...FakeFrameworkLoopExceededError`.
   The exchange runs to completion OR fails for an unrelated
   real reason.
6. Pressing Esc / triggering cancellation during a long-running
   exchange still cleanly terminates the exchange (raises
   `asyncio.CancelledError` → `ExchangeCancelled` per current
   behavior). No regression vs. pre-fix cancellation latency
   on the iteration boundary.
7. Regression test: a FakeFramework loop driven through >10
   tool-call rounds completes without raising. A second test
   confirms cancellation interrupts an in-progress loop.
8. No regressions in the `tc_eng_*` golden suite or any
   engine/framework unit tests.

### Explicitly out of scope for this bug

- Adding `max_iterations` to `RuntimeSpec`. The user's decision
  is no per-agent cap.
- Adding a "continue?" prompt at the cap. The user's decision
  is rely on Esc instead.
- Fixing the broader pattern of `PlatformInternalError` swallowing
  non-PlatformError exceptions at `engine.py:364-367`. Removing
  the cause of THIS occurrence is enough; the general improvement
  is its own ticket.
- Improving mid-tool cancellation responsiveness. If Step 1
  notes that cancellation only fires at iteration boundaries,
  that becomes a follow-up ticket, NOT a Step in this bug.

---

## Part 3 — Step dependency graph

```
Step 1 (verification + writeup) ←── STOP gate
    │
    ▼
Step 2 (remove the cap: protocol + both frameworks + engine call site)
    │
    ▼
Step 3 (regression tests + manual e2e + handoff)
```

| Step | Ships | Reviewable in |
|---|---|---|
| **1** | Verification writeup naming the exact contract change to make | ~15 min |
| **2** | Protocol + FakeFramework + CrewAIFramework + engine call site + deleted class | ~25 min |
| **3** | Regression tests + manual run of original repro + handoff | ~20 min |

Each step ships as its own PR. Step 1 is the gate; Steps 2-3
are mechanical once Step 1's writeup pins the protocol shape.

---

## Step 1 — Verification + protocol-shape decision

- **Builds:** a written confirmation that names (a) every call
  site of `Framework.run_exchange`, (b) the final protocol
  shape (kwarg dropped vs. typed `int | None = None`),
  (c) what value CrewAI's `Agent.max_iter` will receive in the
  no-cap path, and (d) any test that pins
  `FakeFrameworkLoopExceededError`.
- **No code changes.** Pure read-only verification.
- **Acceptance:** the writeup is posted to the coordinator with
  file:line citations for every claim.

### Developer prompt

```
[standing instructions]

Step 1 — Verify the contract change before any code lands.

Read first:
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/bug-framework-iteration-cap-removal.md Parts 0-2.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/engine/framework.py (the Framework protocol).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/engine/fake_framework.py.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/engine/crewai_framework.py.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/engine/engine.py around line 1102.

Verification tasks:

1. Find every call site of run_exchange across the repo:
     grep -rn "run_exchange" src/ tests/ --include="*.py"
   Document each: who calls it, with what max_iterations value
   (literal, default, or omitted).

2. Find every importer of FakeFrameworkLoopExceededError:
     grep -rn "FakeFrameworkLoopExceededError" src/ tests/ docs/ --include="*.py" --include="*.md"
   Document each. These break when the class is removed.

3. Decide the protocol shape and document the choice:
   - Option A: drop max_iterations from Framework.run_exchange
     entirely. Cleanest, but a breaking change for any external
     Framework implementation we don't own. Acceptable in v1.
   - Option B: keep max_iterations on the protocol but type it
     `int | None = None`. None means "no cap." Engine passes
     None or omits the kwarg.
   Recommendation: Option B. Smaller blast radius, preserves
   the surface in case a per-framework override is wanted
   later. Confirm or argue otherwise.

4. CrewAI's Agent.max_iter — confirm the pinned crewai version
   accepts a sentinel-large int (e.g. 10**9) without validation
   error:
     grep -rn "max_iter" .venv/lib/python*/site-packages/crewai/agents/
   Or read crewai's Agent model definition. Document the
   constraint (is it a positive int? bounded? unbounded?).
   The chosen sentinel goes in CrewAIFramework as a module-level
   constant, NOT a literal embedded in the call.

5. Cancellation reach: trace the cancellation callback through
   FakeFramework and CrewAIFramework. Identify every await /
   blocking point where cancellation is NOT polled. Specifically:
   - In FakeFramework, cancellation is polled at fake_framework.py:88-90
     (top of each iteration). If the LLM call or a tool call
     hangs, cancellation won't fire until that await returns.
   - In CrewAIFramework, _CrewAILLM.call schedules engine
     awaitables onto the main loop; check what happens if
     cancellation flips while the worker thread is blocked.
   Document the cancellation latency model. If gaps exist,
   note them as a FOLLOW-UP ticket, do NOT fold them into this
   bug.

6. Tests that pin the loop cap:
     grep -rn "max_iterations\|FakeFrameworkLoopExceeded" tests/ --include="*.py"
   Document each. They get updated or removed in Step 2.

7. Docs that describe the cap: AGENTS.md, docs/explanation/agent-engine-contract.md,
   docs/explanation/error-model.md, docs/reference/module-api-reference.md.
   Grep for "max_iterations" and "iteration" in docs/; document
   each mention; flag the ones that need updating in Step 2.

8. STOP and post the writeup before any code lands.

ESCALATION PATH: if Step 1 reveals that:
 (a) CrewAI's Agent.max_iter has an upper bound smaller than
     "reasonable for unbounded use" (e.g. validated <= 100), or
 (b) more than one external Framework implementation exists
     outside this repo's control, making a protocol change
     blast outside our boundary, or
 (c) cancellation is fundamentally broken (not just latent at
     iteration boundaries) such that removing the cap would
     leave users with no way to stop a runaway loop,
escalate via the Coordinator Escalation Log at the end of this
doc before proceeding.

Acceptance: writeup with file:line citations for every call
site, importer, test, and doc reference; chosen protocol shape
documented with rationale; CrewAI sentinel value documented;
coordinator approves to proceed.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1 — verification writeup.

NO code to review. The writeup is the deliverable.

Checks:

1. Every call site of run_exchange is listed with file:line.
   Cross-check with: grep -rn "run_exchange" src/ tests/ --include="*.py"
   If the writeup misses any, fail.

2. Every importer of FakeFrameworkLoopExceededError is listed.
   Cross-check with: grep -rn "FakeFrameworkLoopExceededError" src/ tests/ docs/
   If any caller isn't accounted for in the Step 2 plan, fail.

3. The protocol-shape choice (A or B) is named explicitly with
   one-sentence rationale. "Option B" with no reasoning is a fail.

4. The CrewAI sentinel value is named (e.g. 10**9) AND backed
   by evidence (the writeup cites where in crewai's source
   the max_iter constraint lives). "Some large number" is a fail.

5. Cancellation latency model is documented. Specifically: the
   writeup states whether Esc during a long LLM call or tool
   call reaches the framework, and how quickly. If "we don't
   know" — fail; this is a verification step.

6. Doc references are listed (AGENTS.md, error-model.md, etc.)
   with the line(s) that need updating in Step 2.

7. Any escalation path concerns (a/b/c in the dev prompt) are
   explicitly addressed: either confirmed not-an-issue with
   citations, or escalated.

Verdict: APPROVE-TO-PROCEED or REVISE with specific gaps.
```

---

## Step 2 — Remove the cap

- **Builds:** the protocol change + both framework
  implementations + the engine call site update + deletion of
  `FakeFrameworkLoopExceededError` + doc updates.
- **Depends on:** Step 1 approved.
- **Acceptance:** unit tests pass; the existing
  `tc_eng_*` golden suite is unchanged; `FakeFrameworkLoopExceededError`
  no longer appears anywhere in `src/`, `tests/`, or `docs/`.

### Developer prompt

```
[standing instructions]

Step 2 — Remove the framework loop cap.

Read first:
- Step 1's writeup in the PR description / coordinator log.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/bug-framework-iteration-cap-removal.md Parts 1-2.
- src/engine/framework.py, src/engine/fake_framework.py,
  src/engine/crewai_framework.py, src/engine/engine.py,
  src/engine/__init__.py.

Tasks (do them in this order; the protocol change comes first
so the implementations are forced to match):

1. Framework protocol (src/engine/framework.py):
   - Apply the shape chosen in Step 1. Recommended (Option B):
     change `max_iterations: int` to `max_iterations: int | None = None`
     on Framework.run_exchange. Update the docstring to say
     "None means no cap; the framework loops until the LLM
     stops calling tools or cancellation fires."

2. FakeFramework (src/engine/fake_framework.py):
   - Delete the FakeFrameworkLoopExceededError class entirely.
   - Delete the _DEFAULT_MAX_ITERATIONS constant.
   - Change run_exchange's loop from `for _ in range(max_iterations):`
     to `while True:`.
   - Cancellation poll stays at the top of the loop (line ~88-90).
     Don't add new poll sites in this step — Step 1 documented
     the latency model; any improvement is a follow-up ticket.
   - Delete the raise + the msg construction at the bottom
     (lines ~148-152).
   - Update the module docstring (top of file): remove the
     "max_iterations is hit" language; reflect the new "loops
     until content-only response or cancellation" contract.

3. CrewAIFramework (src/engine/crewai_framework.py):
   - At module scope, add: `_UNBOUNDED_MAX_ITER = 10**9` (or
     the sentinel value chosen in Step 1).
   - Change run_exchange's signature to match the protocol
     (`max_iterations: int | None = None`).
   - At the Agent(...) construction site, pass:
     `max_iter=max_iterations if max_iterations is not None else _UNBOUNDED_MAX_ITER`
   - Keep `max_retry_limit=0` as-is (engine still owns retry).

4. Engine call site (src/engine/engine.py:1102):
   - Remove `max_iterations=10,` from the run_exchange call.
     The protocol's default (None) takes over.

5. Public re-export (src/engine/__init__.py):
   - Delete the `FakeFrameworkLoopExceededError` import (line 35).
   - Delete the entry from `__all__` (line 67).

6. Errors:
   - No change to src/errors/. PlatformInternalError stays as-is —
     the broader question of whether it should swallow every
     non-PlatformError is OUT OF SCOPE (Part 2 final block).

7. Tests:
   - Any test asserting FakeFrameworkLoopExceededError is raised
     gets DELETED (not updated). The condition no longer exists.
     Per Step 1's grep, this is at most ~2-3 test functions.
   - Any test that constructs FakeFramework with max_iterations=
     and expects the cap to fire — same treatment.
   - Tests that pass max_iterations= to drive a specific number
     of iterations for a stable golden are fine to leave alone
     IF the FakeLLM scripts terminate cleanly (a content-only
     response) before the implied cap. Verify by reading.

8. Docs:
   - Update docs/explanation/agent-engine-contract.md if it
     mentions a max_iterations budget. Replace with "exchanges
     run until the LLM stops calling tools or cancellation fires."
   - Update docs/explanation/error-model.md if it lists
     FakeFrameworkLoopExceededError or describes the cap path.
   - Update AGENTS.md if it surfaces the cap. (Step 1's writeup
     lists the exact docs that need touching.)

Scope: ~80 LOC changed across ~6 files (mostly deletions);
~10 LOC of doc updates.

ESCALATION PATH: if any test that asserts the cap also relies
on side effects that would change without the cap (e.g. a
golden that records the FakeFrameworkLoopExceededError event),
escalate before deleting it — it may be a load-bearing
behavioral test we want to preserve in a different shape.

Acceptance: full test suite green; FakeFrameworkLoopExceededError
appears nowhere; the original failing-repro path no longer
triggers in a smoke test driven by FakeFramework + FakeLLM
scripted to call tools 12 times.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2 — cap removal.

Checks:

1. The Framework protocol signature change matches the shape
   chosen in Step 1 and applied consistently across:
   - src/engine/framework.py (protocol)
   - src/engine/fake_framework.py (run_exchange)
   - src/engine/crewai_framework.py (run_exchange)
   Asymmetric signatures across the three files is a fail.

2. FakeFrameworkLoopExceededError is GONE:
     grep -rn "FakeFrameworkLoopExceededError" src/ tests/ docs/
   should return zero hits. Including __init__.py re-export.

3. CrewAIFramework uses a MODULE-LEVEL sentinel constant
   (e.g. _UNBOUNDED_MAX_ITER = 10**9), not a literal embedded
   in the Agent(...) call. This was the architectural touch
   E-BUG-MAXTOKENS-001 emphasized for that bug; same principle
   applies here.

4. The engine call site at src/engine/engine.py:1102 no longer
   passes max_iterations=10. Either omits the kwarg or passes
   None.

5. FakeFramework's loop is `while True:` with cancellation
   polled at the top. No new poll sites were added (those are
   a follow-up per Step 1).

6. CrewAI's Agent(...) construction still receives a valid int
   for max_iter — None is substituted with the sentinel BEFORE
   it reaches CrewAI.

7. Docs (AGENTS.md, agent-engine-contract.md, error-model.md)
   no longer claim there's an iteration cap. Cross-check with
   the doc list from Step 1's writeup.

8. Run the FULL test suite. Specifically verify:
   - tc_eng_*  golden tests pass byte-equal (the FakeFramework
     loop shape didn't change the assistant turn order).
   - tests/unit/test_errors.py no longer references
     FakeFrameworkLoopExceededError.
   - Any test deleted under task 7 is justified by either
     "asserted a behavior we're removing" or "drove a budget
     that's no longer enforced." Anything else — fail.

Verdict: PASS or FAIL with specific defects.
```

---

## Step 3 — Regression tests + manual e2e + handoff

- **Builds:** new regression tests pinning the no-cap behavior
  and pinning cancellation; manual verification against the
  user's original failing repro.
- **Depends on:** Step 2 in main.
- **Acceptance:** new tests pass; original repro completes
  without `platform.internal_error`.

### Developer prompt

```
[standing instructions]

Step 3 — Regression tests + manual end-to-end.

Read first:
- Step 2's PR.
- tests/component/ or tests/integration/ for the closest
  existing tests on FakeFramework loop behavior (Step 1's
  writeup pinned the location).
- process/test-catalog.md to see where new TC entries land.

Tasks:

1. Add tests/integration/test_framework_no_cap.py (or extend the
   nearest existing FakeFramework test file — Step 1's writeup
   names which) with:

   TC-FRM-NOCAP-01 — FakeFramework loops past the old cap.
     def test_fake_framework_runs_past_ten_iterations():
         """The old cap was 10. Drive a FakeLLM that scripts
         15 tool calls then a content-only response; confirm
         the exchange completes with the expected text and
         no error is raised."""
         # Setup: FakeLLM with a script of 15 tool-call turns
         # then one content-only turn. A trivial tool that
         # echoes its args.
         # Action: await framework.run_exchange(...)
         # Expected: returns the final content string; no raise.

   TC-FRM-NOCAP-02 — Cancellation interrupts an in-progress loop.
     def test_fake_framework_observes_cancellation_mid_loop():
         """A cancellation callback that flips True after the
         3rd iteration causes the framework to raise
         asyncio.CancelledError on the next poll."""
         # Setup: FakeLLM that issues unlimited tool calls.
         #        A counter-based cancellation that flips at N=3.
         # Action: run_exchange.
         # Expected: raises asyncio.CancelledError.

2. Add tests/integration/test_framework_no_cap_crewai.py (or
   extend the closest CrewAI framework test) with:

   TC-FRM-NOCAP-03 — CrewAIFramework forwards a sentinel max_iter.
     def test_crewai_framework_uses_unbounded_sentinel():
         """When run_exchange is called with no max_iterations
         (or None), CrewAIFramework constructs Agent with
         max_iter == _UNBOUNDED_MAX_ITER."""
         # Setup: patch crewai.Agent to capture construction
         # kwargs; run an exchange (the FakeLLM-equivalent test
         # path already established for CrewAI tests).
         # Action: run_exchange(max_iterations=None).
         # Expected: captured max_iter equals the module-level
         # sentinel (10**9 or whichever Step 2 chose).

3. Add the three TCs to process/test-catalog.md under a new
   "Framework Iteration (FRM-NOCAP)" section. Use the full
   Setup / Action / Expected / Verifies format the catalog
   already uses for other sections.

4. Pre-fix verification: git-stash Step 2's code on a worktree
   or local branch; run TC-FRM-NOCAP-01; confirm it FAILS with
   a FakeFrameworkLoopExceededError (or equivalent post-engine-
   wrap: platform.internal_error). Document the failure output
   in the PR description. This proves the new test would have
   caught the regression.

5. Manual end-to-end with a real LLM:
   a. Use the user's existing workspace at ~/Documents/kolsch
      (the one where ses_7af67129-... lives) or a fresh one
      connected to the same provider (opencode).
   b. Run `uv run kolsch chat` with the helper agent on
      qwen3.6-plus (or whichever model is currently configured).
   c. Send a prompt that forces multi-step tool work past 10
      iterations. Suggested prompt:
        "list everything in src/engine, then read each file
         in turn and summarize what it does"
      (FakeFramework cap would have killed this around the
      4th read — now it should run to completion or stop on
      something else entirely.)
   d. Verify in the resulting session jsonl
      (~/Documents/kolsch/sessions/ses_*.jsonl):
      - No thread.exchange_failed event with
        type=platform.internal_error and
        original_type=engine.fake_framework.FakeFrameworkLoopExceededError.
      - The exchange either completes (thread.exchange_completed)
        or fails for an UNRELATED real reason
        (e.g. model.upstream_error). The fake-framework cap
        is not in the failure path.
   e. Also verify cancellation: send a prompt that triggers a
      tool loop; press Esc mid-loop; confirm the exchange
      terminates cleanly (thread.exchange_failed with
      type=exchange.cancelled). Same JSONL check.
   f. Capture the relevant JSONL excerpts in the PR description.

6. Document any follow-ups Step 1 noted (cancellation latency
   at mid-tool-call, PlatformInternalError wrapping pattern)
   as separate process/ tickets — do NOT silently drop them.

Scope: ~100 LOC tests + catalog entries + manual report; no
production code changes (unless step 5d reveals a hole, in
which case revise Step 2 — don't patch it in Step 3).

ESCALATION PATH: if the manual run STILL produces a
platform.internal_error with FakeFrameworkLoopExceededError
in the detail, Step 2 didn't fully land. Don't declare the
bug fixed; escalate with the captured JSONL.

Acceptance: TC-FRM-NOCAP-01..03 pass on post-fix code; pre-fix
verification documented; manual e2e shows the original
failure path no longer triggers; cancellation verified;
follow-up tickets filed (or explicitly N/A).
```

### Reviewer prompt

```
[standing instructions]

Review Step 3 — regression tests + manual e2e.

Checks:

1. All three TCs exist and pass:
   - TC-FRM-NOCAP-01 (FakeFramework loops past 10)
   - TC-FRM-NOCAP-02 (cancellation interrupts the loop)
   - TC-FRM-NOCAP-03 (CrewAI uses the sentinel)
   Run them. Asymmetric coverage (e.g. NOCAP-01 only) is a fail.

2. Pre-fix verification: the PR description shows TC-FRM-NOCAP-01
   FAILING on pre-fix code with the FakeFrameworkLoopExceeded
   path. If pre-fix verification is absent or shows a different
   failure mode, fail this review.

3. Manual e2e: the PR description contains a JSONL excerpt from
   a real chat run showing either:
   - thread.exchange_completed for a multi-step prompt that
     would have hit the 10-iteration cap pre-fix, OR
   - thread.exchange_failed with a DIFFERENT error type
     (model.*, exchange.timeout, etc.) — not
     platform.internal_error/FakeFrameworkLoopExceeded.
   Either is acceptable; the bug-cause is what we're proving
   absent.

4. Cancellation verified manually with a JSONL excerpt showing
   thread.exchange_failed with type=exchange.cancelled at a
   reasonable iteration count (not waiting until the model
   stops on its own).

5. process/test-catalog.md gained the three FRM-NOCAP entries
   in full Setup/Action/Expected/Verifies form.

6. Full test suite green. Specifically:
   - tc_eng_* goldens still byte-equal.
   - No new test depends on FakeFrameworkLoopExceededError
     existing.

7. Follow-up tickets named in Step 1 are either filed in
   process/ as new bug- or qol- docs OR explicitly listed in
   the PR description as "deferred and tracked here." Silent
   drops are a fail.

Verdict: PASS or FAIL. On PASS, this bug is closed.
```

---

## Part 4 — How to escalate

```markdown
### Escalation E-BUG-ITERCAP-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–3
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:** 2–3 paths.
- **Estimated cost:**

---

### Coordinator resolution — E-BUG-ITERCAP-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Step 1 finds CrewAI's `Agent.max_iter` has an upper bound
  smaller than reasonable for unbounded use.
- Step 1 finds external Framework implementations outside our
  repo that would be silently broken by the protocol change.
- Step 1 finds cancellation fundamentally broken (not just
  latent at iteration boundaries).
- Step 2 reveals a test asserting the cap behavior that ALSO
  exercises an unrelated load-bearing assertion (so deleting
  it would lose coverage we want).
- Step 3's manual run still produces the original failure.

When NOT to escalate:
- Whether to drop `max_iterations` from the protocol entirely
  vs. keep it as `int | None = None`. Pick Option B unless
  Step 1 surfaces a concrete reason. Document in the PR.
- The exact sentinel value for CrewAI's `max_iter`. `10**9` is
  fine unless CrewAI rejects it; pick the next reasonable
  power-of-ten if so.
- Doc wording. Minor variations don't need coordinator input.

---

## Part 5 — Coordinator Escalation Log

*(empty — populate as escalations land)*

---

## Part 6 — Notes on running these three steps

- **Strict order: 1 → 2 → 3.** Step 1 is the gate. Don't start
  Step 2 until the protocol shape is confirmed.
- **Step 2 must ship as one PR.** Splitting the protocol change
  from the implementations creates a window where the protocol
  and impls disagree; the type checker will catch it but the
  PR review is harder.
- **Don't fold the broader `PlatformInternalError` swallow fix
  into this bug.** That's a real concern, but it's separable:
  removing this specific RuntimeError eliminates the symptom
  without touching the boundary catch-all. The general policy
  question (should the boundary classify more precisely?) gets
  its own ticket.
- **Don't add `max_iterations` to `RuntimeSpec` "while we're
  here."** The user's decision is no per-agent cap. Adding a
  config field implies the cap exists; the whole point of this
  fix is that it doesn't.

---

## Part 7 — Out of scope

- **Per-agent iteration budget via config.** Explicitly the
  user's choice not to add.
- **Continue-prompt UX on cap hit.** Explicitly the user's
  choice not to add.
- **Mid-tool-call cancellation responsiveness.** Cancellation
  currently fires at iteration boundaries; improving this is a
  separate concern (Step 1 documents it; if material, a
  follow-up ticket is filed in Step 3).
- **`PlatformInternalError` boundary swallowing every
  non-PlatformError.** Same: separable, its own ticket.
- **CrewAI version pin update.** The sentinel assumes the
  currently-pinned CrewAI version. A future bump revisits.
- **FakeLLM script changes to test the new behavior in
  goldens.** The existing `tc_eng_*` goldens don't exercise
  >10 iterations; new ones can be added under a future test
  expansion, not under this bug.
