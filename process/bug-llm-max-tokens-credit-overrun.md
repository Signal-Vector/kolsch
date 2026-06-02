# Bug — LLM call requests max_tokens=65536; OpenRouter rejects with 402 insufficient credits

**Draft 0.2** *(restructured from 0.1: single dev/reviewer pair
split into five dependency-ordered steps, each with focused
prompts.)*

> Reported symptom: a `kolsch chat` exchange against OpenRouter
> failed with HTTP 402:
>
> ```
> Error: OpenAI API call failed: Error code: 402 -
>   {'error': {
>     'message': 'This request requires more credits, or fewer
>                 max_tokens. You requested up to 65536 tokens, but
>                 can only afford 20512.',
>     'code': 402
>   }}
> ```
>
> OpenRouter pre-charges based on `prompt_tokens * prompt_price +
> max_tokens * completion_price` to guarantee fulfillment. The
> 402 fires when the user's credit balance is less than that
> pre-charge — even if the call would have consumed far fewer
> tokens than the cap. User has ~20,512 tokens of credit; we
> asked for up to 65,536. The mismatch blocks the call.
>
> User notes: "it wasn't a problem before" — suggesting a
> recent regression in the connector layer.

---

## Part 0 — Why this matters

OpenRouter's 402 is a hard block, not a soft warning. Two UX
problems compound:

1. **Cap is wildly disconnected from typical use.** Most chat
   exchanges produce 100–2,000 completion tokens; asking for
   65,536 means pre-charging ~30× what we'll consume.
2. **No friendly classification.** The error surfaces as raw
   text in the chat's failure-event capsule. Phase 1.3 spec'd
   `model.*` error classifications but never landed; this 402
   is currently a generic "LLM Call Failed."

---

## Part 1 — Hypotheses

### H1. Kolsch explicitly passes `max_tokens=65536` in the connector

The Phase 1.1 OpenRouter connector's `build_llm` might pass an
explicit `max_tokens` value. `65536` is `2^16` — suspicious
round number suggesting "model's max completion budget" rather
than a thoughtful default. Possibly introduced when the
bug-litellm-model-routing fix touched connector internals.

### H2. Kolsch passes `context_length` as `max_tokens`

The Phase 1.1 `ModelEntry` schema has a `context_length` field.
If this gets passed as `max_tokens` anywhere, the bug is field
misattribution — `context_length` is the model's TOTAL window
(prompt + completion), `max_tokens` is completion-only.

### H3. Kolsch doesn't set `max_tokens`; litellm/CrewAI defaults to the model's max

If `build_llm` constructs `crewai.LLM` without `max_tokens`,
litellm may default to whatever the model's metadata reports
as max completion. Some OpenRouter-served Claude models report
65,536 for the long-output variant; CrewAI passing through to
litellm with no override → litellm uses that default.

### H4. CrewAI's `Agent` or `Crew` sets a default `max_tokens`

Less likely: CrewAI's framework adapter sets `max_tokens` on
the `Agent` or `Crew` construction in
`src/engine/crewai_framework.py`.

### Most likely

**H3** (no explicit cap; litellm defaults to model's reported
max). The specific number `65536` matches Claude Opus/Sonnet
4-7's long-output budget. The OpenRouter connector almost
certainly constructs `crewai.LLM` without `max_tokens`, and
litellm picks the model's reported max.

---

## Part 2 — Acceptance criteria (applies across all steps)

The bug is fixed when **all** hold:

1. The OpenRouter connector (and OpenCode connector for parity)
   constructs `crewai.LLM(...)` with an explicit `max_tokens`
   defaulting to **4096**.
2. The default is overridable at three layers, per the
   qol-setup-evolution-and-defaults composition order:
   - Agent's `max_tokens:` field.
   - `platform.yaml`'s `defaults.max_tokens`.
   - Connector default (4096).
3. Running the original repro after the fix succeeds (or
   surfaces a clean error for a different reason).
4. Regression test asserts `crewai.LLM` is constructed with
   `max_tokens=4096` when no override is set.
5. Optional, deferrable: 402 errors classified as
   `model.insufficient_credits` per Phase 1.3's `model.*`
   vocabulary.

---

## Part 3 — Step dependency graph

```
Step 1 (investigation + writeup) ←── STOP gate
    │
    ▼
Step 2 (connector fix: max_tokens cap)
    │
    ▼
Step 3 (schema additions: agent + platform defaults)
    │
    ▼
Step 4 (regression tests)
    │
    ▼
Step 5 (manual e2e + handoff)
```

| Step | Ships | Reviewable in |
|---|---|---|
| **1** | Phase A writeup confirming hypothesis with file:line | ~20 min |
| **2** | Connector default in OpenRouter + OpenCode | ~20 min |
| **3** | `max_tokens` field on agent schema + platform defaults | ~25 min |
| **4** | Regression test pinning `crewai.LLM` kwargs + override layers | ~25 min |
| **5** | Manual real-LLM verification + findings handoff | ~15 min |

Each step ships as its own PR. Step 1 is the gate; Steps 2-5
are mechanical once Step 1's writeup names the cause.

---

## Step 1 — Investigation + root-cause writeup

- **Builds:** a written confirmation of H1-H4 with file/line
  citations + an estimated fix shape.
- **No code changes.** Pure read-only investigation.
- **Acceptance:** the writeup posted to the coordinator covers
  every hypothesis with concrete citations and confirms which
  one(s) hold.

### Developer prompt

```
[standing instructions]

Step 1 — Confirm the hypothesis chain.

Read first:
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/bug-llm-max-tokens-credit-overrun.md Parts 0-2.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase1.1-llm-specs.md §5 and §6
  (the OpenCode + OpenRouter connector specs).

Investigation tasks:

1. Grep for max_tokens usage:
     grep -rn "max_tokens" src/ --include="*.py"
     grep -rn "max_completion_tokens" src/ --include="*.py"
     grep -rn "65536" src/ --include="*.py"
     grep -rn "context_length" src/llm/ src/engine/ --include="*.py"
   Document what each surfaces.

2. Read src/llm/providers/openrouter.py and
   src/llm/providers/opencode.py. Inspect the build_llm bodies.
   Note the exact crewai.LLM(...) construction — is max_tokens
   passed? With what value?

3. Read src/engine/crewai_framework.py. Find the Agent and Crew
   constructions; check for any max_tokens parameters.

4. If you have OpenRouter credentials available, run the failing
   repro with verbose logging:
     LITELLM_LOG=DEBUG uv run kolsch chat
   Capture the request payload sent to OpenRouter; confirm the
   65536 value's origin (kolsch code vs litellm internal default).

5. If you don't have credentials: construct a fake LLM that
   captures the construction kwargs; run an exchange through;
   inspect what kolsch passes to crewai.LLM.

6. Identify the cause (H1, H2, H3, or H4) with concrete
   file:line citations. The expected answer is H3 (no explicit
   cap → litellm defaults to model's reported max), but verify
   rather than assume.

7. Estimate the fix shape: files changed, diff size, any
   surprising surface beyond the two connectors. Expected
   scope per this bug doc is ~80 LOC code + ~60 LOC tests
   across 2-3 files.

8. STOP and post the writeup before any code lands.

ESCALATION PATH: if the investigation reveals that:
 (a) max_tokens isn't set anywhere AND litellm's default isn't
     overridable from our connector layer (would require
     monkeypatching), or
 (b) the fix surface exceeds ~120 LOC across more than 3 files,
escalate via the Coordinator Escalation Log at the end of this
doc before proceeding.

Acceptance: writeup naming the cause with citations; coordinator
approves to proceed.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1 — investigation writeup.

NO code to review. The writeup is the deliverable.

Checks:

1. Every hypothesis (H1, H2, H3, H4) has concrete file:line
   citations or "I looked at X and didn't find it" notes.
   Vague "the connector does this" is a fail.

2. The chosen hypothesis is supported by the evidence cited.
   If the dev says H3 but the citations show explicit
   max_tokens=65536 in the connector, that's a contradiction —
   fail.

3. The fix shape estimate is concrete: specific files, rough
   line counts. "Modify the connector somehow" — fail.

4. Coordinator approval is requested explicitly; the dev did
   NOT proceed to code.

Verdict: APPROVE-TO-PROCEED or REVISE with specific gaps.
```

---

## Step 2 — Connector fix (max_tokens cap)

- **Builds:** both OpenRouter and OpenCode connectors construct
  `crewai.LLM(...)` with an explicit `max_tokens` defaulting to
  4096.
- **Depends on:** Step 1 approved.
- **Acceptance:** unit tests verify the kwarg is passed; both
  connectors symmetric.

### Developer prompt

```
[standing instructions]

Step 2 — Add the max_tokens cap to the connectors.

Read first:
- Step 1's writeup in the PR description / coordinator log.
- src/llm/providers/openrouter.py and src/llm/providers/opencode.py.

Tasks:

1. Add a max_tokens parameter to build_llm in both connectors.
   Default value: 4096 (the rationale is in Part 2 of the bug doc).

2. The crewai.LLM construction becomes:
     return crewai.LLM(
         model=...,
         api_key=api_key,
         base_url=provider_config.base_url,
         timeout=provider_config.timeout_seconds,
         max_tokens=max_tokens,   # NEW
     )

3. The two connectors must remain symmetric — same kwarg name,
   same default value, same call site. Asymmetric fixes between
   OpenRouter and OpenCode are how regressions in one but not
   the other go undetected.

4. NO schema changes in this step. Step 3 adds the agent-level
   and platform-defaults-level override fields. Here, the
   connector's max_tokens parameter just receives 4096 from the
   caller in this step; the caller's override-aware logic is
   Step 3.

5. Unit tests in tests/unit/llm/test_connector_max_tokens.py:
   - OpenRouterConnector.build_llm called with no max_tokens
     override → resulting crewai.LLM has max_tokens=4096.
   - OpenCodeConnector.build_llm with no override → max_tokens=4096.
   - Both called with max_tokens=8192 explicitly → resulting
     crewai.LLM has max_tokens=8192.

Scope: ~30 LOC code + ~40 LOC tests.

ESCALATION PATH: if crewai.LLM's constructor signature in the
pinned CrewAI version doesn't accept max_tokens as a direct
kwarg (e.g., the parameter is nested under a config object),
escalate. Don't pass it through litellm-internal mechanisms
without coordinator input.

Acceptance: tests pass; both connectors symmetric.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2 — connector fix.

Checks:

1. BOTH src/llm/providers/openrouter.py AND
   src/llm/providers/opencode.py are modified. Asymmetric fix
   is a fail.

2. The kwarg name passed to crewai.LLM is exactly `max_tokens`
   (not `max_completion_tokens` or any variant). Verify by
   reading both files.

3. The default is exactly 4096. Not 65536. Not 8192.

4. NO schema changes in this step — agent.py and platform.py
   schemas are untouched. Step 3 owns those.

5. Unit tests assert on the constructed crewai.LLM's max_tokens
   attribute, not just "no exception raised."

6. No regressions in existing connector tests.

Verdict: PASS or FAIL with specific defects.
```

---

## Step 3 — Schema additions (agent + platform defaults)

- **Builds:** optional `max_tokens` field on the agent schema;
  optional `max_tokens` in `platform.yaml`'s `defaults:` block;
  resolver wires the three-layer override.
- **Depends on:** Step 2 in main.
- **Acceptance:** agent with `max_tokens: 8192` resolves to that
  value; agent omitting + platform defaults set → defaults
  value; both omitted → connector default (4096).

### Developer prompt

```
[standing instructions]

Step 3 — Wire the three-layer max_tokens override.

Read first:
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/qol-setup-evolution-and-defaults.md
  Step 6 ("defaults block + resolver fallback") — the composition
  pattern this fix extends.
- src/schemas/agent.py and src/schemas/platform.py.
- The resolver code that builds resolved agent configs (likely
  src/platform_runtime/runtime.py).

Tasks:

1. Add `max_tokens: int | None = None` to the agent schema
   (src/schemas/agent.py). Optional; default None means "use
   defaults/connector fallback."

2. The platform's `defaults:` block (from
   qol-setup-evolution-and-defaults Step 6) already supports
   arbitrary agent-field defaults. Verify that `defaults.max_tokens:
   N` in platform.yaml parses correctly. If the defaults block
   isn't yet typed (it might be a generic dict), no schema work
   needed here.

3. In the resolver, when the engine constructs the LLM call,
   compute max_tokens as:
     resolved_max_tokens = (
         agent.max_tokens if agent.max_tokens is not None
         else platform.defaults.get("max_tokens")
         if platform.defaults else None
     )
   Then pass to build_llm:
     llm = await connector.build_llm(
         model_id=...,
         provider_config=...,
         api_key=...,
         max_tokens=resolved_max_tokens or 4096,
     )

4. The connector's default (4096 from Step 2) is the final
   fallback when neither agent nor defaults specify.

5. Composition order is now agent > defaults > connector — same
   pattern as the qol-setup-evolution Step 6 composition for
   other fields.

6. Tests in tests/unit/schemas/test_max_tokens_composition.py:
   - Agent with max_tokens=8192, no defaults set → resolved 8192.
   - Agent without max_tokens, defaults.max_tokens=2048 → 2048.
   - Both unset → connector default of 4096.
   - Agent has max_tokens=2048, defaults.max_tokens=8192 → 2048
     (agent wins).

Scope: ~50 LOC code + ~60 LOC tests.

ESCALATION PATH: if the resolver layer doesn't expose a clean
place to compute resolved_max_tokens AFTER the agent and
platform configs are resolved but BEFORE build_llm is called,
escalate.

Acceptance: composition tests pass; the agent's resolved
max_tokens flows through to the connector.
```

### Reviewer prompt

```
[standing instructions]

Review Step 3 — schema + resolver wiring.

Checks:

1. agent.max_tokens is OPTIONAL with default None. Not int=4096
   (that would prevent fallback to defaults).

2. The composition order is exactly agent > defaults >
   connector (4096). Verify with the four composition tests.

3. The resolver passes resolved_max_tokens to build_llm via
   the new max_tokens parameter from Step 2. Verify by tracing
   the call site.

4. No regression in existing agent / platform schema tests.

5. The agent schema's max_tokens field is documented in the
   schema file's docstring or comment (so the agent author
   knows the field exists).

Verdict: PASS or FAIL with specific defects.
```

---

## Step 4 — Regression test (end-to-end)

- **Builds:** a regression test that simulates the failing repro
  with a fake LLM, asserting the construction kwargs are correct.
- **Depends on:** Step 3 in main.
- **Acceptance:** test fails on pre-fix code (verify by git
  stash); passes after.

### Developer prompt

```
[standing instructions]

Step 4 — Regression test for the max_tokens cap.

Read first:
- Steps 2 and 3's PRs.
- tests/integration/test_tool_wiring.py (TC-TOOL-WIRE-01) as a
  template for end-to-end LLM-call assertion tests.

Tasks:

1. Add tests/integration/test_max_tokens_cap.py with
   TC-LLM-MAXTOK-01:

     def test_llm_construction_caps_max_tokens_at_4096():
         """When no override is set, the LLM call uses
         max_tokens=4096 — preventing OpenRouter 402 pre-charge
         rejections."""

         # Set up a workspace with no max_tokens overrides
         # anywhere; construct an agent; run an exchange against
         # a fake LLM that captures the construction kwargs.

         captured_kwargs = {}
         class CapturingLLM(FakeLLM):
             def __init__(self, **kwargs):
                 captured_kwargs.update(kwargs)
                 super().__init__(**kwargs)

         # ... drive the exchange ...

         assert captured_kwargs["max_tokens"] == 4096

2. Add TC-LLM-MAXTOK-02 for the agent override:
   - Agent yaml has max_tokens: 8192.
   - Captured kwargs show max_tokens=8192.

3. Add TC-LLM-MAXTOK-03 for the platform defaults override:
   - platform.yaml has defaults.max_tokens=2048.
   - Agent has no max_tokens.
   - Captured kwargs show max_tokens=2048.

4. Verify pre-fix FAIL: git-stash Steps 2 + 3's code; run
   TC-LLM-MAXTOK-01; confirm it fails because max_tokens is
   either missing from kwargs or set to a different value
   (likely 65536). Document this in the PR description.

5. Add TC-LLM-MAXTOK-01 through 03 to
   process/test-catalog.md under a new "Max Tokens (LLM-MAXTOK)"
   section.

Scope: ~80 LOC tests + small catalog entry.

ESCALATION PATH: if the existing fake LLM fixture doesn't expose
construction kwargs cleanly, escalate before extending the
fixture with substantial new surface.

Acceptance: TC-LLM-MAXTOK-01..03 pass on post-fix code; the
pre-fix demonstration in the PR description shows the failure
matches the bug's described mechanism.
```

### Reviewer prompt

```
[standing instructions]

Review Step 4 — regression tests.

Checks:

1. All three test cases (TC-LLM-MAXTOK-01, 02, 03) exist and
   pass. Run them.

2. Pre-fix verification: the dev's PR description shows
   evidence that TC-LLM-MAXTOK-01 FAILS on pre-fix code with a
   failure mode matching the bug (max_tokens missing or set to
   65536). If the pre-fix verification is absent or shows a
   different failure, fail this review.

3. The test asserts on the actual kwarg passed to crewai.LLM,
   not just "an exception didn't raise" or "the exchange
   completed."

4. The test uses a FAKE LLM. No real network calls.

5. test-catalog.md gained the new entries in full
   Setup/Action/Expected/Verifies form.

6. Run the FULL test suite. No regressions in Phase 1.x or
   2.0 tests.

Verdict: PASS or FAIL.
```

---

## Step 5 — Manual end-to-end + handoff

- **Builds:** verification against the user's original failing
  repro (OpenRouter chat).
- **Depends on:** Step 4 in main.
- **Acceptance:** the user's exact failure path no longer
  triggers a 402 caused by the 65,536 cap.

### Developer prompt

```
[standing instructions]

Step 5 — Manual end-to-end verification.

NO new code (unless a tiny fix is needed; in which case revise
Steps 2-4 or escalate).

Tasks:

1. Connect a real OpenRouter account with a deliberately small
   credit balance. (Alternatively: use the user's exact
   credentials if they're shareable with the dev environment.)

2. Run `kolsch chat` against the helper agent on OpenRouter.

3. Send a prompt that previously triggered the 402.

4. Verify:
   - The exchange COMPLETES (or fails for a different reason,
     e.g., the model itself errors — that's a separate
     concern).
   - The model.request event in the JSONL shows
     `data.max_tokens` near 4096 (not 65536).
   - No 402 from OpenRouter with the "requires more credits"
     message.

5. Capture the relevant JSONL excerpt in the PR description.

6. Document any follow-ups (e.g., if the model.insufficient_credits
   classification deserves its own ticket per Part 2 criterion
   5 of the bug doc).

Scope: zero new code; verification + reporting only.

ESCALATION PATH: if the manual run STILL produces a 402 with
the 65,536 message, Steps 2-3 didn't fully wire through. Don't
declare the bug fixed; escalate with the captured payload.

Acceptance: captured JSONL shows max_tokens at the expected
cap; OpenRouter accepts the request.
```

### Reviewer prompt

```
[standing instructions]

Review Step 5 — manual e2e.

Checks:

1. PR description includes the captured JSONL excerpt showing
   model.request.data.max_tokens at the expected value (4096
   or whatever override applies).

2. The 402 with the 65,536 message does NOT appear in the
   transcript.

3. If the run produced a different error (e.g., the model
   errored independently), that's documented as a separate
   concern, not blocking this fix's closure.

4. Step 4's TC-LLM-MAXTOK tests still pass.

5. Full test suite still green.

6. Any follow-up tickets (e.g., model.insufficient_credits
   classification) are documented in the PR description, NOT
   silently dropped.

Verdict: PASS or FAIL. On PASS, this bug is closed.
```

---

## Part 4 — How to escalate

```markdown
### Escalation E-BUG-MAXTOKENS-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–5
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:** 2–3 paths.
- **Estimated cost:**

---

### Coordinator resolution — E-BUG-MAXTOKENS-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Step 1 reveals max_tokens isn't set anywhere AND litellm's
  default isn't overridable from kolsch's connector layer
  (would require monkeypatching).
- crewai.LLM's constructor signature in the pinned version
  doesn't accept max_tokens as a direct kwarg.
- The resolver lacks a clean hook for computing resolved
  max_tokens (Step 3).
- Step 4's fake LLM fixture doesn't expose construction kwargs.
- Step 5 reveals the fix didn't fully wire through; Steps 2-3
  need rework.

When NOT to escalate:
- The default value (4096 vs 8192). Pick 4096 unless there's
  a real argument otherwise; document in the PR.
- Whether to add the model.insufficient_credits classification
  in this PR or defer.

---

## Part 5 — Coordinator Escalation Log

### E-BUG-MAXTOKENS-001 — Where does the three-layer composition live?

- **Raised by:** developer (during Step 3 planning)
- **Step:** 3
- **Trigger:** the resolver-side wiring for `agent > defaults >
  connector default` could live in either of two places:
  - (a) Orchestrator level: each `build_llm(..., max_tokens=...)`
    accepts an explicit value; the engine pre-flight resolves
    the agent/defaults chain BEFORE calling the orchestrator;
    the connector applies its own default if `None`.
  - (b) Framework-adapter level: connector always gets the
    connector default (4096); per-exchange override happens in
    `crewai_framework.py` where `Agent`/`Crew` know about the
    active agent's `ModelSpec`.
- **Developer's recommendation:** (a) — keeps connector
  knowledge of "what's a safe default" in the connector,
  aligns with qol-setup-evolution Step 6's composition order.

---

### Coordinator resolution — E-BUG-MAXTOKENS-001

- **Decided by:** human
- **Decision:** **Option (a) with one refinement.** Three layers
  each cleanly own one responsibility:
  - **Engine pre-flight (Phase 2.2 step 5.5-area)** resolves
    `agent.max_tokens ?? platform.defaults.max_tokens ?? None`.
  - **`build_llm` orchestrator** (`src/llm/__init__.py`) accepts
    `max_tokens: int | None = None` and threads through to the
    connector. No knowledge of per-connector defaults.
  - **Connector** (e.g. `OpenRouterConnector.build_llm`) owns
    its own default as a class constant
    `DEFAULT_MAX_TOKENS = 4096`. Applies `max_tokens or
    self.DEFAULT_MAX_TOKENS` when constructing `crewai.LLM`.
- **Rationale (one sentence):** the connector knowing its own
  safe default lines up with the Phase 1.1 §9 future connectors
  (Anthropic, OpenAI, Bedrock, Llama-local) — each contributes
  one file with its own appropriate default; the orchestrator
  stays agnostic.
- **Affects:**
  - Step 2's deliverable becomes "connector exposes
    `DEFAULT_MAX_TOKENS` class constant + accepts `max_tokens`
    kwarg with None default; substitutes the class constant if
    None."
  - Step 3's deliverable becomes "engine pre-flight computes
    resolved max_tokens from agent + platform defaults;
    `build_llm` orchestrator gains the `max_tokens` parameter;
    threads to the connector."
  - The composition is forward-compatible with Phase 1.3's
    fallback chains — each fallback model's `build_llm` call
    resolves independently against the same chain, so cross-
    provider fallbacks naturally pick up per-provider
    defaults.
- **Resume instruction:** developer proceeds with Steps 2 and 3
  per the refined shape. Step 2's reviewer should additionally
  verify the `DEFAULT_MAX_TOKENS = 4096` is a class constant (not
  a literal embedded in an `or` clause) — this is the small
  architectural touch that matters.

---

## Part 6 — Notes on running these five steps

- **Strict order: 1 → 2 → 3 → 4 → 5.** Step 1 is the gate.
- **Step 2 and Step 3 must be separate PRs.** Step 2 ships the
  connector default in isolation; Step 3 adds the override
  layers. Bundling them makes the review harder and creates
  the merge-gap risk that E-BUG-CREWAI-002 D3 flagged.
- **The composition order (Step 3) must match qol-setup-evolution
  Step 6's pattern.** Don't invent a parallel resolution
  mechanism — the qol-setup-evolution defaults block already
  defines how agent fields fall back to platform defaults.
  max_tokens is just another agent field; same rules apply.
- **Don't fold in the model.insufficient_credits classification.**
  It's tempting because the 402 error message is right there,
  but Phase 1.3 owns the model.* classification surface. If you
  ship it under this bug fix, you're partially implementing
  Phase 1.3 in an unreviewed scope.

---

## Part 7 — Out of scope

- **Phase 1.3's broader fallback / classification work.** The
  model.insufficient_credits code addition is optional and
  deferrable; the rest of Phase 1.3 is its own phase.
- **Auto-detecting credit balance.** OpenRouter exposes
  /api/v1/auth/key per Phase 1.1 §6.1; using it to pre-flight
  every exchange is expensive and out of scope.
- **Per-model max_output_tokens discovery.** A future
  enhancement could read provider-reported max_output_tokens
  and use that as the cap; for now a flat 4096 is the right
  default.
- **`max_tokens` on workflow nodes** (Phase 3). Out of scope
  here; revisit when Phase 3 spec'd.
