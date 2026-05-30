# Bug — Echo response on real LLM provider

**Draft 0.1** *(investigation ticket, not a phase patch)*

> Reported symptom: a user ran the starter agent with a real LLM
> provider configured (post Phase 1.1 connect flow), expected a real
> LLM reply, but the agent's response was their own input echoed back.
> That is the *fake-LLM* behavior — the platform is somehow routing
> through `FakeFramework` / `EchoLLM` instead of the configured
> CrewAI + real provider path.
>
> This document is for the developer agent (to investigate and fix)
> and the reviewer agent (to verify root cause + minimal fix +
> regression test). The investigation IS the work. The fix is
> probably small; finding it isn't.

---

## Part 0 — Symptom & Reproduction

**Reported by:** user, on `main` after Phase 1.1 implementation
landed.

**Setup:**
1. Workspace at `~/Documents/kolsch` (or any path) with a connected
   real provider (`kolsch connect openrouter` succeeded).
2. Starter / sample agent with `model:` set to a real-provider alias
   (e.g. `opus`) or canonical form (`openrouter/anthropic/claude-
   opus-4-7`).
3. `platform.yaml` has the provider in `llm_providers.openrouter`
   and the alias in `model_aliases`.

**Action:**
```bash
uv run kolsch run starter/agents/helper --input content="hello, kolsch"
```

**Expected:** A real LLM-generated reply from Claude Opus (or the
configured model).

**Actual:** The reply is the user's input echoed (e.g. `"Echo: hello,
kolsch"`, or the literal `"hello, kolsch"` depending on which path
got hit).

**Inferred:** Somewhere in the call path, the engine is using the
fake LLM / FakeFramework / EchoLLM despite the agent's resolved
`ModelSpec` pointing at a real provider.

---

## Part 1 — Hypotheses (ranked by likelihood)

Investigate in order; stop at the first that proves out and read the
others as "also possibly contributing."

### H1. The engine always wires `FakeFramework` regardless of model

The Phase 1 implementation used `FakeFramework` + the fake LLM as the
only working path. Phase 1.1 added `CrewAIFramework` and `build_llm`,
but the wiring that *selects* which framework to instantiate per
exchange may still pick FakeFramework unconditionally.

- **Where to look:** `src/engine/` for framework instantiation;
  `src/platform_runtime/runtime.py` for the engine-construction path;
  any `FakeFramework(...)` or `EchoLLM(...)` instantiation outside
  `tests/`.

### H2. `build_llm` is implemented but never called

Phase 1.1 step 5.5 of the engine pre-flight is supposed to call
`build_llm`. If the engine's pre-flight wasn't updated, the LLM
client passed to the framework is whatever default the framework
constructs — and for a default-constructed CrewAIFramework that may
be unspecified, but for FakeFramework it's the echo LLM.

- **Where to look:** the engine's pre-flight in
  `src/engine/engine.py` (or similar) at the step 5.5 site;
  `grep -rn 'build_llm' src/`; check if Phase 1.1 Step 16 actually
  landed or if it was partial.

### H3. The provider classification routes `fake` through CrewAI but real providers through nothing

Maybe `provider: fake` is treated as the canonical "use the fake
framework" path AND it's the only path that actually reaches the
runtime. Real providers may resolve correctly at config-load but
have nothing on the runtime side that knows to construct a
`CrewAIFramework` for them. The model name is normalized but the
*selection of framework* still defaults to fake.

- **Where to look:** framework selection logic — anywhere the
  engine asks "which framework should I use for this agent?". Check
  whether `FakeFramework` is selected when `provider != "fake"` or
  whether there's a registry / dispatch.

### H4. The connector's `build_llm` is wrong (returns fake)

OpenCode / OpenRouter connector's `build_llm` is supposed to return
a real `crewai.LLM(...)` per Phase 1.1 §§5.1/6.1. If it accidentally
returns a fake LLM (e.g. an import shadow, a test stub committed by
mistake, or a stale `pass` placeholder), the engine has the right
plumbing but the wrong terminal node.

- **Where to look:** `src/llm/providers/openrouter.py` and
  `src/llm/providers/opencode.py`'s `build_llm` method. Read the
  body. Does it actually call `crewai.LLM(...)` with a real model
  string and api_key?

### H5. The CLI's `run` path bypasses the resolved config

`src/cli/main.py` _cmd_run currently does:
```
config = await runtime.build_resolved_config(args.agent_ref)
...
agent_msg = await runtime.engine.execute(config, ctx)
```
If `build_resolved_config` produces the right `ModelSpec` but the
engine ignores `config.model` and reads from a default (or from a
session-stored fallback), the model swap never takes effect.

- **Where to look:** the engine's `execute` method — does it read
  `config.model` and pass it forward, or does it pull from somewhere
  else?

### H6. The `agent.message` is being constructed from the user message, not the model response

A really sneaky version of this bug: the LLM IS called, gets back a
real response, but the engine constructs the `agent.message` event
from `ctx.trigger.content` rather than from `model.response.data`.
The fake-looking echo isn't from the fake LLM at all — it's the
engine echoing its own input into the wrong event field.

- **Where to look:** the engine code that emits `agent.message`.
  Verify the source of its `content`/`text` field is the
  `model.response`, not `ctx.trigger`.

### H7. `provider: fake` is the platform default and overrides agent intent

`platform.yaml`'s `defaults` block might carry a default `model:`
that's `fake-model`. If the kernel resolver merges defaults *over*
the agent's value (instead of agent-wins), every agent ends up on
the fake path regardless of what its YAML says.

- **Where to look:** asset-management contract §3 (resolution
  precedence) and the kernel resolver's merge logic. Confirm
  agent-wins precedence holds for `model`.

---

## Part 2 — Investigation Strategy

The first investigation pass is **purely observational**. Do not
modify code yet. The deliverable is a root-cause writeup naming
which hypothesis (or combination) is the actual bug.

### Step A — Inspect the produced event log

The single most informative artifact is a JSONL file from a real
reproduction. Run the failing case yourself, then read
`sessions/<ses_id>/threads/*.jsonl` and answer:

1. Is there a `model.request` event? If yes, what's the `provider`
   and `model` in its data?
2. Is there a `model.response` event? What's its `data.content`?
   Does it match the echoed output?
3. Is the `model.response.data.model` the real model or
   `fake-model`?
4. What does the `agent.message` event's `data.content.text` look
   like — is it the same string as `user.message.data.content`?

This narrows H1/H2/H3 (fake framework wired) vs H4/H5 (real
framework but echoey result) vs H6 (event-construction bug).

### Step B — Confirm Phase 1.1 implementation actually landed

The Phase 1.1 specs and build plan exist
(`docs/process/phase1.1-llm-specs.md`,
`docs/process/phase1.1-llm-provider.md`). Confirm Step 16 was
actually executed:

```bash
# Does the llm package exist as specified?
ls src/llm/ src/llm/providers/ 2>/dev/null

# Is build_llm callable and called?
grep -rn 'def build_llm\|build_llm(' src/

# Is CrewAIFramework instantiated anywhere outside tests?
grep -rn 'CrewAIFramework\|FakeFramework' src/

# Is EchoLLM instantiated outside tests/fixtures?
grep -rn 'EchoLLM\|fake_llm' src/
```

If `src/llm/` is empty or `build_llm` only exists in tests, the
bug is "Phase 1.1 Step 16 wasn't implemented (or was partially
implemented)" — H2 is confirmed and the fix is to complete Step 16
per its existing developer prompt.

### Step C — Trace the framework selection for a single exchange

Add a one-line print or breakpoint (revert before committing) at
the point where the engine selects which framework to instantiate.
Run the failing case and confirm which class is chosen. If it's
`FakeFramework` for a `provider: openrouter` agent, H1 or H3 is the
answer.

### Step D — Inspect the connector's `build_llm`

Read `src/llm/providers/openrouter.py` (and `opencode.py`). The
`build_llm` method must return a real `crewai.LLM(...)` per the
spec — not raise, not return `None`, not return a fake. If it
returns a stub, H4 is the answer.

### Step E — Confirm precedence on the `model` field

Construct a deliberately conflicting case: agent says `model: opus`,
platform `defaults.model: fake-model`. Resolve the config (without
running the engine — use the resolver directly). Does the resolved
config have `opus` (correct, agent-wins) or `fake-model` (H7
confirmed)?

### Decision criteria

- If Step A shows `model.request` carries the real provider/model
  AND `model.response.data.content` is real text, but
  `agent.message.data.content.text` matches `user.message` →
  **H6 confirmed.**
- If Step A shows `model.request` carries `fake/fake-model` despite
  agent config saying otherwise → **H1, H3, or H7 confirmed**
  depending on whether the resolved config also points at fake
  (H7) or the engine ignores the resolved config (H1/H3).
- If `src/llm/` is empty or `build_llm` isn't called → **H2
  confirmed.**
- If Step D shows the connector's `build_llm` returns a stub →
  **H4 confirmed.**
- If Step E shows defaults overriding the agent value → **H7
  confirmed.**

---

## Part 3 — Acceptance Criteria

The bug is fixed when **all** of the following hold:

1. A user with a connected provider and an agent whose `model:` is
   set to a real-provider alias/string can run that agent and get a
   model-generated response, not an echo of their input.
2. The JSONL for the exchange contains a `model.request` with the
   real provider/model, a `model.response` whose `data.content` is
   the real LLM output, and an `agent.message` whose `content.text`
   equals the `model.response` content (NOT the user message).
3. Agents with `model: { provider: fake, model: fake-model }` still
   work and still produce the echo — fake-LLM behavior is preserved
   for tests and tutorials.
4. A new regression test (`TC-BUG-echo-01` or similar) reproduces
   the original failure on the pre-fix code and passes on the post-
   fix code. The test does NOT use real network — it uses the fake
   LLM with a deliberate non-echo response so we can assert the
   model output reaches `agent.message` end-to-end.
5. No existing test in `tests/unit`, `tests/component`,
   `tests/integration`, or `tests/golden` regresses.

---

## Part 4 — Out of Scope

- **Phase 1.2 / 1.3 work.** Those patches are spec'd but not yet
  implemented. Don't pull them in.
- **Architectural changes.** This is a bug fix. If the
  investigation reveals a larger design problem (e.g., "framework
  selection has no clean dispatch and needs a registry"), STOP and
  write a finding rather than rebuilding the wiring. Surface it,
  don't ambush.
- **Renaming `pi-platform` → `kolsch`.** The rename plan is its own
  ticket. Use whichever name is current at the time of the fix.
- **Performance, observability, retry logic.** Not the bug.

---

## Part 5 — Developer Prompt

```
[standing instructions]

You are investigating a bug: running a sample agent against a
real LLM provider produces the user's input echoed back instead
of an LLM-generated reply. Investigate, root-cause, fix, and add
a regression test.

Read first:
- docs/process/bug-echo-on-real-provider.md (this ticket — the
  hypotheses, investigation strategy, acceptance criteria).
- docs/process/phase1.1-llm-specs.md Parts 1, 2.4, 5, 6 (the
  build_llm contract and the connector implementations).
- docs/explanation/agent-engine-contract.md §2.1 (the pre-flight
  steps, especially the post-Phase-1.1 step 5.5).
- src/engine/, src/llm/, src/platform_runtime/runtime.py, and
  src/cli/main.py (the call path you'll trace).

WORK IN TWO PHASES — investigation, then fix.

Phase A — Investigation (no code changes yet).

1. Set up a reproduction. Use a real connected provider if you
   have credentials; otherwise mock the provider via the
   fixture HTTP layer and a fake LLM rigged to return a
   distinctive non-echo string (e.g. "DISTINCTIVE_REPLY_42")
   so you can tell whether the LLM was actually called.

2. Follow the investigation strategy in §2 of the ticket Steps
   A through E. For each, record what you found.

3. Produce a root-cause writeup: which hypothesis (H1–H7) holds,
   with file/line citations, a one-paragraph explanation of how
   the bug manifests, and a one-paragraph explanation of why the
   bug exists (was it Phase 1.1 incomplete? a regression? an
   integration gap?).

4. STOP. Surface the writeup before writing any fix. If the
   root cause is larger than a small fix (e.g., requires
   reworking framework selection), surface it as a finding —
   don't grow the scope unilaterally. The coordinator decides
   whether to expand scope.

Phase B — Fix (only after Phase A is reviewed or you have
explicit "proceed" from the coordinator).

5. Make the smallest possible change that satisfies all five
   acceptance criteria in §3 of the ticket. Prefer one file
   over multiple; prefer one function edit over a refactor.

6. Add a regression test. Use the fake LLM with
   inject_response("DISTINCTIVE_REPLY_42") (or extend the
   fixture if such API doesn't exist) so the test runs without
   network. Assert:
     - model.request event present, with the agent's resolved
       provider/model (not fake-model unless the agent uses fake).
     - model.response.data.content == "DISTINCTIVE_REPLY_42".
     - agent.message.data.content.text == "DISTINCTIVE_REPLY_42".
   This single test catches H1, H3, H4, H5, H6 simultaneously.

7. Run the full test suite (tests/unit, tests/component,
   tests/integration, tests/golden) and confirm zero regressions.
   If a golden test changes, evaluate whether that change is the
   bug-fix expressing itself in the event log (good) or a
   regression (bad). Document either way.

CRITICAL: the fake-LLM / EchoLLM behavior must be PRESERVED for
agents that explicitly choose provider: fake. Acceptance
criterion 3. The fix shouldn't remove the echo path — it should
ensure non-fake agents take a different path.

Commit message:
  fix(engine|llm|cli): correctly route real-provider exchanges
    through build_llm + CrewAIFramework, not the fake echo path

Body: the one-paragraph root-cause explanation from Phase A
plus what the fix does. Include the hypothesis number(s) from
the ticket so the reviewer can map your work back to it.

Report:
- The root-cause writeup.
- What you changed and where (file + line).
- The regression test you added.
- Test results (which suites ran, pass/fail counts).
- Any findings outside scope you surfaced rather than fixed.
```

---

## Part 6 — Reviewer Prompt

```
[standing instructions]

Review the echo-on-real-provider bug fix. The deliverable is a
root-cause writeup, a minimal fix, and a regression test.

Read first:
- docs/process/bug-echo-on-real-provider.md (the ticket — Part 3
  acceptance criteria are your checklist).
- The developer's root-cause writeup (in the PR description).
- The diff.

Architectural / correctness review:

1. The root-cause writeup must name a specific hypothesis (H1–H7
   or a clearly-explained combination) with file/line citations.
   If the writeup is hand-wavy ("something in the engine"), fail
   and request a concrete trace.

2. The fix matches the root cause. If the writeup says H1 but
   the fix is in the connector, something's off — either the
   writeup or the fix is wrong. Reconcile.

3. Minimality. The diff should be small. If the fix rewrites a
   subsystem when a one-function change would suffice, flag the
   over-reach. Bug fixes don't get to refactor.

4. The fake-LLM path is preserved. Find at least one existing test
   that exercises `provider: fake` and confirm it still passes
   unchanged. Acceptance criterion 3.

Functional review:

5. Reproduce the original symptom against the pre-fix code (git
   stash the fix, run the new regression test, confirm it FAILS
   in a way that matches the bug description). Then restore the
   fix and confirm the regression test PASSES.

6. Read the new regression test. Does it actually assert what
   acceptance criterion 4 requires?
     - model.request present with real provider/model
     - model.response content is the distinctive string
     - agent.message content.text == model.response content
   If any assertion is missing, fail and request it. A test
   that asserts only "exit code 0" doesn't catch H6.

7. Run the FULL test suite:
     uv run pytest tests/unit tests/component tests/integration
     uv run pytest tests/golden
   Acceptance criterion 5 — zero regressions. Any failing test is
   a fail unless the developer documented it as an intentional
   expectation update for the bug fix.

8. End-to-end smoke. Use the fake LLM with an injected non-echo
   response. Run:
     kolsch run starter/agents/helper --input content="hello"
   Confirm the agent reply is the injected string, NOT "hello".
   (If you have real provider credentials and budget, also run
   against a real model — but the fake injection is sufficient
   for the review gate.)

The "investigation quality" review:

9. Did the developer rule out the other hypotheses, or just stop
   at the first explanation that fit? If H1 is named, did they
   also verify H6 isn't *also* present (a partial fix that leaves
   the echoey event-construction bug latent)? Ask if unclear.

10. If the developer surfaced a finding outside scope (e.g.
    "framework selection has no real dispatch"), read it. Is it
    accurate? Should the coordinator be aware? Flag it in your
    verdict so it doesn't get lost.

Verdict: PASS or FAIL. On FAIL, name the acceptance criterion
violated and the specific defect (file + line, or missing test
assertion). On PASS, summarize the root cause in one sentence and
confirm the regression test catches it.
```

---

## Part 7 — Notes on Running This Investigation

- **The investigation is the work.** A confident wrong-answer fix
  is worse than a careful writeup that surfaces a bigger issue.
  Phase A of the developer prompt is the gate.
- **Don't burn tokens.** The reproduction can use the fake LLM with
  an injected non-echo response. Real-network tests are reserved
  for the developer's optional manual confirmation, not the
  committed test suite.
- **If H2 is the answer**, the "fix" is really "complete Phase 1.1
  Step 16." Coordinate with the human before doing that under a
  bug-fix label — that's a phase implementation, not a bug fix,
  and it gets its own scope review.
- **Preserve the echo for `provider: fake`.** The Phase 1 fake-LLM
  path is the tutorial's first reward, the test suite's workhorse,
  and the offline-development workflow. Don't kill it while fixing
  the real-provider path.
