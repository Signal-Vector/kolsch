# Phase 1.3 Build Plan — Developer & Reviewer Agent Prompts

**Draft 0.1**

> Two-step build plan for Phase 1.3 (Model Fallback Chains patch).
> Both steps follow the same developer-reviewer loop and standing
> instructions as the main Phase 1 build plan
> (`phase1-build-plan.md`), the Phase 1.1 build plan
> (`phase1.1-llm-provider.md`), and the Phase 1.2 build plan
> (`phase1.2-usability-build.md`). Treat these as Step 19 and
> Step 20 of the overall build sequence — they run after Phase 1.2
> is complete and the patch document
> (`process/phase1.3-fallback-specs.md`) has been read.

---

## How these steps relate to the main build plan

- **Step 19 (Revision pass)** is mechanical, like the LLM-R and
  UX-R passes before it. One branch, one PR. Apply FB-R1 through
  FB-R12 to the affected spec files. No runtime code is written;
  no tests are added. The patch document is the source of truth.
- **Step 20 (Implementation)** is real engineering. Adds the
  engine fallback loop, the `model.fallback` event emission, the
  classification predicate, the schema-validation extension for
  `fallback_models`, and the fake-LLM error-injection extension.
  Adds TC-FB-01..10. One branch, one PR.

Step 19 must complete before Step 20 begins. The reviewer for
Step 20 verifies against the *revised* specs, not the patch
document itself.

---

## Step 19 — Apply FB-R1..R12 revisions (mechanical)

- **Builds:** the spec files reflect the patch. Existing docs
  updated per FB-R1..R11; one new doc created per FB-R12
  (`docs/how-to/configure-fallbacks.md`).
- **Depends on:** Phase 1.2 complete and merged.
- **Source of truth:** `process/phase1.3-fallback-specs.md`
  (the patch — read it first; every revision flag references a
  section).

### Developer prompt

```
[standing instructions]

Step 19 — Apply Phase 1.3 revisions FB-R1 through FB-R12. This is
a mechanical revision pass, same pattern as the LLM-R and UX-R
passes before it. No runtime code is written; no tests are added.

Read first:
- process/phase1.3-fallback-specs.md (the full patch — every
  revision flag references a section here).
- process/phase1.2-usability-build.md Step 17 (precedent for
  how a recent revision pass was carried out).

Apply each revision flag exactly as specified in Part 5 of the
patch:

- FB-R1: add fallback_models: list[ModelSpec input] to the agent
  config schema in docs/reference/schemas.md §A. Default []. Note
  the three input shapes inherit from ModelSpec.
- FB-R2: add defaults.fallback_models: list[ModelSpec input] to
  docs/explanation/asset-management-contract.md §3.2 with the
  precedence rule from the patch.
- FB-R3: update docs/explanation/agent-engine-contract.md §2.1
  step 5.5 and insert 5.6 per the patch. Update the exchange-
  execution model-call section to describe the loop in patch §2.1.
- FB-R4: add the model.fallback event type to
  docs/explanation/agent-engine-contract.md §3 with the ordering
  rule from patch §3.2.
- FB-R5: add §4.15 "Model Fallback" to docs/explanation/error-
  model.md Part 4 with the eight codes from patch Part 4. For the
  six model.* codes, declare Recoverable vs Fatal explicitly and
  cross-reference §2.2 of the patch as the canonical trigger
  table.
- FB-R6: add Part 11.7 "Model Fallback" to docs/reference/module-
  api-reference.md per patch §1, §2, §3. Update the dependency
  matrix (Part 12) to reflect multiple LLM-registry consumption
  per exchange.
- FB-R7: add success criteria §10.14 "Model Fallback" to
  process/development-testing-handbook.md per patch §5 FB-R7.
- FB-R8: add Part 9.7 "Model Fallback (FB)" to process/
  test-catalog.md with TC-FB-01..10 per patch Part 6. Update the
  coverage map.
- FB-R9: add fallback_models to the agent surface in docs/
  explanation/v1-scope.md §2.3 with the no-mid-stream-fallback
  boundary noted explicitly.
- FB-R10: add the hard rule to AGENTS.md per patch §5 FB-R10
  (every new model.* error declares Recoverable/Fatal and updates
  the trigger table).
- FB-R11: append a "10. Add a fallback model" section to
  docs/tutorials/first-agent.md per the patch. Steps 0–9 stay
  byte-identical.
- FB-R12: CREATE docs/how-to/configure-fallbacks.md as a new
  how-to-type doc covering per-agent chain, platform default,
  order rationale, reading events for model.fallback (per patch
  §5 FB-R12).

Each revision is mechanical. Where a flag says "add" or "insert",
add. Where it says "update", change the existing content. Do NOT
take initiative beyond what the flag specifies — if something
feels under-specified, surface it in your summary rather than
guessing.

Commit per revision (FB-R1, FB-R2, ...) or in two logical groups
(schema/contract revisions in one commit, doc-only revisions in
another), whichever the patch reviewer prefers. Conventional
Commits scoped to docs().

Acceptance: every FB-R1..R12 applied; no other file changed; the
patch document's "Apply in two passes — 1. Revisions" instruction
is complete.

Report what changed, per flag, in your summary.
```

### Reviewer prompt

```
[standing instructions]

Review Step 19 — the Phase 1.3 revision pass. This is a mechanical
review, not an architectural one. The architectural decisions live
in the patch document and were made by the human. Your job is to
confirm the mechanical application is faithful.

Read first:
- process/phase1.3-fallback-specs.md, in full.

For each revision flag FB-R1 through FB-R12:
- Confirm the target file was changed.
- Confirm the change matches what the patch's Part 5 specified
  for that flag.
- Confirm no other file was changed outside the flag's scope.
- Confirm the new doc FB-R12 was created and contains the content
  the patch describes.

Specific checks worth running:
- ModelSpec inheritance: fallback_models entries accept the same
  three shapes as model: (object, compact string, alias) per
  FB-R1. The schema doc says so explicitly, not by implication.
- Engine contract step 5.6 is the BUILD step (resolution +
  ordering), NOT the build_llm step (clients are built lazily).
  This distinction must be in the prose per FB-R3.
- error-model.md (FB-R5) declares each of the six model.* codes
  as Recoverable or Fatal exactly as the patch's §2.2 table says.
  The cross-reference back to §2.2 (or its successor) is present.
- TC-FB-01 through TC-FB-10 in the test catalog (FB-R8) carry
  their full Setup/Action/Expected/Verifies form. TC-FB-07
  (streaming no-fallback) is captured as documented behavior, not
  marked TODO.
- AGENTS.md hard rule (FB-R10) is in the Hard Rules section, not
  some other section. The rule names BOTH obligations: declare
  level AND update the trigger table.
- docs/tutorials/first-agent.md (FB-R11) added step 10 only;
  steps 0–9 are byte-identical to pre-revision. Use git diff to
  confirm.
- docs/how-to/configure-fallbacks.md (FB-R12) is genuinely how-to
  (recipe-style), not a re-explanation of the architecture.

Do NOT let style preferences justify a fail — this is mechanical
faithfulness, not editorial improvement. If the developer added
content not specified in a flag, flag it as out-of-scope. If a
flag was missed, fail and name which one.

Verdict: PASS or FAIL with specific defects citing flag + file +
patch section.
```

---

## Step 20 — Implement the fallback loop, events, and classification

- **Builds:** the engine fallback loop in `src/engine/_fallback.py`
  (the predicate + the orchestration), the `model.fallback` event
  emission, the agent-load-time validation of `fallback_models`,
  the platform-default merge in the resolver, the fake-LLM error-
  injection extension, and the per-connector classification of
  provider responses into the six canonical `model.*` codes. Adds
  TC-FB-01..10.
- **Depends on:** Step 19 PASS (revisions in main).
- **Spec refs:**
  - `process/phase1.3-fallback-specs.md` Parts 1–4
  - `docs/explanation/agent-engine-contract.md` §2.1 step 5.5/5.6
    and §3 events
  - `docs/explanation/error-model.md` §4.15
  - `docs/reference/module-api-reference.md` Part 11.7
  - `process/development-testing-handbook.md` §10.14
  - `process/test-catalog.md` Part 9.7

### Developer prompt

```
[standing instructions]

Step 20 — Implement Phase 1.3: model fallback chains. The
revisions from Step 19 are in main; the revised specs are your
contract.

Read first:
- process/phase1.3-fallback-specs.md (the patch, especially
  Parts 1, 2, 3, 4).
- docs/explanation/agent-engine-contract.md §2.1 (the engine pre-
  flight and model-call loop).
- docs/explanation/error-model.md §4.15 (the eight codes and their
  levels).
- docs/reference/module-api-reference.md Part 11.7 (the API
  surface you implement).
- process/test-catalog.md Part 9.7 (TC-FB-01..10 — write
  these tests first, TDD).
- src/engine/ (the engine you're extending).
- src/llm/providers/ (the connectors whose error classification
  you're tightening).

Deliver:

src/engine/
├── _fallback.py             # NEW: is_fallback_triggering(err) predicate;
│                            #      build_fallback_chain(config, defaults);
│                            #      execute_with_fallback(...) orchestrator.
├── (existing files)         # engine.py extended to call into _fallback
│                            # at the model-call site (step 5.5/5.6+).

src/schemas/
├── (agent schema)           # fallback_models added as optional field.

src/llm/providers/
├── opencode.py              # error classification: map provider responses
├── openrouter.py            # to model.rate_limited / model.server_error /
│                            # model.timeout / model.context_overflow /
│                            # model.authentication_failed / model.bad_request.
│                            # Each connector's call layer is now responsible
│                            # for mapping. Document the mapping in the
│                            # connector's module docstring.

tests/fixtures/
├── fake_llm.py              # extended: inject specific error code on next
│                            # call. The fixture has been "Fake LLM" already;
│                            # add per-instance error-injection API.

tests/component/engine/
├── test_fallback.py         # NEW: TC-FB-01..10.

Implementation requirements:

1. _fallback.py:
   - is_fallback_triggering(err: PlatformError) -> bool. Pure.
     Exhaustively covers the six model.* codes plus a final else:
     return False. Add an assert-like check that any new model.*
     code (detected via the catalog at import time) raises an
     ImportError naming the missing classification — this enforces
     the AGENTS.md hard rule from FB-R10 at runtime.
   - build_fallback_chain(agent_config, platform_defaults) ->
     list[ModelSpec]. Resolves each entry via the existing kernel
     resolver (which handles the three input shapes). Validates
     each entry's provider is in llm_providers; raises
     PlatformError(llm.provider_not_configured) with the offending
     position in the chain on failure. Emits the
     config.fallback_duplicate warning for any repeats.
   - execute_with_fallback(primary_spec, chain, request, secrets,
     emit) -> AgentMessage. The orchestrator. Tries primary; on
     fallback-triggering error, emits model.fallback and tries
     next; on Fatal, raises immediately; on exhaustion, raises
     PlatformError(llm.all_models_failed) with the full attempts
     list.

2. Engine integration (engine.py):
   - At pre-flight step 5.5: build_llm for primary ONLY. Step 5.6:
     resolve and validate the chain (clients NOT built yet).
   - At model-call time: call execute_with_fallback. The engine's
     model-call helper becomes a one-line delegate.
   - DO NOT thread a "current model" mutable through the engine —
     the chain orchestrator owns that state for the duration of
     the model call.

3. Connector error classification:
   - Each connector's call layer (whatever wraps the crewai.LLM
     call) catches connector-level exceptions and raises
     PlatformError(<one of the six model.* codes>) instead.
   - Per-connector mapping documented in the module docstring with
     a table: provider response → canonical code.
   - For OpenRouter (which proxies to many providers), the
     mapping is more permissive — anything 429 is rate_limited,
     any 5xx is server_error, etc. — and the connector docstring
     explains the conservative approach.

4. Event emission:
   - model.fallback event added to the schemas/events module. The
     event's data fields exactly match patch §3.1.
   - Engine emits one per transition, between the failing
     model.request/response and the next attempt's model.request,
     per patch §3.2.

5. Streaming boundary (patch §2.3):
   - The engine's stream path detects "first content token has
     been emitted" via a flag. Once set, fallback is short-
     circuited; the failing connector exception turns straight
     into thread.exchange_failed.

6. Fake LLM extension:
   - Add an `inject_error(code: str, after_call: int = 0)` method
     so tests can deterministically force a model.rate_limited on
     the Nth call, etc.
   - The fake LLM remains crewai.LLM-compatible — tests inject it
     the same way they do today.

Tests TC-FB-01..10 first. Write each test before its
implementation. Per process/test-catalog.md Part 9.7:

- TC-FB-01: agent with [haiku] fallback; primary rate_limits;
  haiku succeeds. Assert model.fallback present, agent_message
  attributed to haiku.
- TC-FB-02: all three fail recoverably. Assert llm.all_models_
  failed with attempts list of length 3.
- TC-FB-03: primary succeeds; no model.fallback; fake LLMs for
  fallbacks never invoked (assert via fake call counters).
- TC-FB-04: alias in fallback_models resolves to canonical;
  model.fallback.to.model is the canonical name.
- TC-FB-05: unknown alias in fallback_models → config.unknown_
  alias at agent load (NOT at fallback time).
- TC-FB-06: model.fallback event schema matches patch §3.1
  exactly (assert on serialized JSON).
- TC-FB-07: streaming exchange; primary emits one token then 5xx;
  thread.exchange_failed emitted; no model.fallback; chain not
  consulted. (Use the streaming fake LLM with mid-stream error
  injection.)
- TC-FB-08: chain of three; second fallback succeeds. Verify two
  model.fallback events in order Opus→Haiku, Haiku→GPT4o.
- TC-FB-09: model.authentication_failed (Fatal) on primary.
  thread.exchange_failed immediately; chain untouched. Assert
  fallback fake LLMs never called.
- TC-FB-10: reconstruct. Write a JSONL with full chain; rm
  entity.db; kolsch reconstruct; verify session events include
  every model.fallback in original order.

Critical behaviors:
- fallback_models entries that resolve to a provider not in
  llm_providers fail at AGENT LOAD, not at exchange time. The
  error message names the entry position and the missing provider.
- Lazy client construction: build_llm is called for fallback N
  only when fallback N-1 has failed. Verifiable via call counts.
- No retries on the same model. If Opus 429s, the next call is
  Haiku, not Opus.
- Each model.fallback emits BEFORE the next attempt's model.
  request. Ordering matters for replay.
- The engine does not "remember" which fallback worked last
  exchange. Every exchange starts from the primary.

Document the per-connector error-classification mapping in each
connector's module docstring. This makes the AGENTS.md hard rule
(declare level on every new model.* code) checkable per
connector.

If during implementation you discover the existing crewai.LLM
exception surface doesn't cleanly map to the six model.* codes
(e.g., crewai wraps everything in a generic LLMException), surface
this as a finding and propose either a per-provider httpx layer or
a crewai-level exception inspector. DO NOT silently swallow into
a single error code.

Acceptance: TC-FB-01..10 pass; success criteria §10.14 met;
existing engine and CLI tests still pass; coverage for
src/engine/_fallback.py is ≥95% (it's a small focused module —
the bar is higher); coverage for the connector error-
classification additions is ≥85%.

Commit: feat(engine,llm): implement Phase 1.3 model fallback. Report
what you built, the per-connector mapping table, any open
questions surfaced during implementation (crewai exception
surface, retry-after handling, etc.).
```

### Reviewer prompt

```
[standing instructions]

Review Step 20 — the Phase 1.3 implementation. The revisions from
Step 19 are in main; review against the revised specs, not against
the patch document.

Read first:
- process/phase1.3-fallback-specs.md (architecture, flow,
  failure modes)
- docs/explanation/agent-engine-contract.md §2.1 (engine
  integration)
- docs/explanation/error-model.md §4.15 (code levels)
- docs/reference/module-api-reference.md Part 11.7 (API surface)
- process/development-testing-handbook.md §10.14 (success
  criteria)
- process/test-catalog.md Part 9.7 (TC-FB-01..10)

The architectural review questions:

1. THE LOOP LIVES IN ONE PLACE. grep for "fallback" across src/.
   The orchestration (try primary, fall back, emit event, retry)
   should be in src/engine/_fallback.py. If you find a second
   implementation in src/cli/, src/api/, src/llm/, or anywhere
   else — fail. The engine owns this.

2. is_fallback_triggering is a pure predicate over PlatformError
   codes. Open the function. Confirm:
     (a) It returns True for exactly the four Recoverable model.*
         codes (rate_limited, server_error, timeout, context_
         overflow).
     (b) It returns False for the two Fatal model.* codes
         (authentication_failed, bad_request).
     (c) It returns False for everything else (default branch).
     (d) The function is annotated and ≤20 lines.

3. The "every new model.* code must declare its level" check. The
   developer claimed to implement an import-time check that fails
   if a model.* code in the catalog isn't classified. Find that
   check. Confirm it actually fires: temporarily add a fake
   model.foobar code to the catalog (in a dev branch) and confirm
   the engine fails to import with a clear message. (Don't merge
   the temp change — just verify the mechanism.)

4. Lazy client construction. Read execute_with_fallback. Confirm:
     - The primary LLM client is built before the loop.
     - Fallback LLM clients are built INSIDE the except branch,
       only when reached.
     - TC-FB-03 verifies this via call counts; rerun it and
       inspect.

5. The streaming boundary (patch §2.3). Read the engine's stream
   path. Confirm:
     - There is an explicit "first content event emitted" flag.
     - Once set, the fallback path is short-circuited.
     - TC-FB-07 verifies this; rerun and inspect the assertion.

6. Per-connector error mapping. Read each connector's module
   docstring. Confirm:
     - A mapping table is present (provider response → canonical
       code).
     - The mapping is exhaustive (every documented provider error
       maps to one of the six codes or to "let it through as
       Fatal default").
     - The implementation in the call wrapper matches the table.

7. Validation at agent load (patch §1.3). Try to load an agent
   with fallback_models referencing an unknown alias — confirm
   it fails at LOAD, with a clear message naming the position.
   Same for a provider not in llm_providers. TC-FB-05 verifies
   the alias case; manually verify the provider case.

The functional review questions:

8. Run TC-FB-01..10. All pass? Each one actually asserts the
   behavior?

9. TC-FB-02 specifically: assert that llm.all_models_failed's
   attempts field carries (primary, fallback[0], fallback[1])
   with the right error codes in order. The cause chain matters
   for debuggability.

10. TC-FB-06 specifically: serialize the model.fallback event to
    JSON and compare against the expected shape from patch §3.1
    field-by-field. No "approximate match" — exact.

11. TC-FB-09 specifically: when primary returns Fatal, the chain
    must NOT be consulted. Verify the fallback fake LLMs were
    instantiated (or not, per lazy policy) and confirm via call
    counts they were never invoked.

12. TC-FB-10 reconstruct round-trip: write the JSONL by hand
    (don't run a real exchange to generate it; this isolates
    reconstruction from emission). Then verify reconstruction
    surfaces the same chain.

13. Existing engine tests. Run the full engine test suite. Any
    regressions? Fallback should be a PURE addition for agents
    without fallback_models — same events, same outcomes.

14. Coverage targets. src/engine/_fallback.py at ≥95%. Per-
    connector error mapping at ≥85%. Run and confirm. Investigate
    uncovered branches.

15. The fake-LLM extension. Read the inject_error API. Is it
    sufficient for TC-FB-01..10 deterministically, or do tests
    depend on flaky timing? Flaky tests block the patch.

The "this is replayable" review:

16. Sessions with model.fallback events must be re-readable by
    `kolsch sessions events <id>` in a way the user understands.
    Run it against a TC-FB-01-produced session. Read the output.
    Is it clear which model produced the final answer? If a user
    reading this output can't tell, the event design failed.

17. The cumulative usage / cost story (patch §8 open item 3) is
    NOT in scope, but the model.response.usage on the successful
    fallback should reflect only the successful call's tokens.
    Confirm this is the case — failed primary call tokens are
    NOT double-counted on the response.

Verdict: PASS or FAIL with specific defects citing file + spec
section. This patch hardens reliability under transient failure;
the bar is "a single 429 no longer breaks a user's exchange, and
the event log shows exactly what happened."
```

---

## Notes on running these two steps

- **Step 19 first, in its own PR, merged before Step 20 starts.**
  The reviewer of Step 20 reads the revised specs as the contract.
- **The single-loop property in Step 20's reviewer Q1 is the
  critical structural check.** Fallback logic is engine concern.
  If it leaks into the CLI or API or connector layers, future
  changes become multi-file edits.
- **The "every new model.* code must declare its level" mechanism
  (Q3) is the long-term hygiene win of this patch.** Even if
  someone forgets to update the trigger table in a future patch,
  the engine refuses to start until they do. Verify the mechanism
  actually works.
- **Manual end-to-end test before declaring Phase 1.3 done.**
  After Step 20 passes: connect OpenRouter; set up an agent with
  `fallback_models: [haiku]`; deliberately rate-limit Opus (or
  use a tiny throttle on your end); run an exchange; confirm
  Haiku served and the JSONL shows the fallback. The CLI's
  `sessions events` output should be human-readable.
- **Network fixtures, not real network.** TC-FB-* all use the
  fake LLM with error injection. The component test suite does
  not call real providers.

---

## What this build plan does NOT include

- **Mid-stream fallback.** Out of scope per patch §2.3 / Part 7
  item 1. Future patch with UX decisions about partial-output
  handling.
- **Health-tracking memory across exchanges.** Out of scope per
  patch Part 7 item 2. Phase 3 hosted concern.
- **Cost-aware reordering.** Out of scope per patch Part 7 item 3.
- **Per-error policy customization** (e.g., "fall back on 429
  but not 5xx"). Out of scope per patch Part 7 item 4. Add later
  if concrete demand surfaces.
- **Connector-level intra-model retries.** Out of scope per patch
  §2.4. Connectors handle their own retry policy if they want
  exponential backoff against the same endpoint.
- **Parallel race-mode fallback** (try all in parallel, take
  first success). Out of scope per patch Part 8 item 4.
