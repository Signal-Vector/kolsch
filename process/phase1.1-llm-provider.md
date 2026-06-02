# Phase 1.1 Build Plan — Developer & Reviewer Agent Prompts

**Draft 0.1**

> Two-step build plan for Phase 1.1 (LLM Provider Configuration patch).
> Both steps follow the same developer-reviewer loop and standing
> instructions as the main Phase 1 build plan (`phase1-build-plan.md`).
> Treat these as Step 15 and Step 16 of the overall build sequence —
> they run after Phase 1 is complete and the patch document
> (`process/revisions/phase1.1-external-services-patch.md`) has
> been read.

---

## How these steps relate to the main build plan

- **Step 15 (Revision pass)** is mechanical, like the audit revisions
  and modular-storage revisions before it. One branch, one PR. Apply
  LLM-R1 through LLM-R13 to the affected spec files. No code is
  written; no tests are added. The patch document is the source of
  truth.
- **Step 16 (Implementation)** is real engineering. Adds `src/llm/`
  with the helper, Protocols, registry, and the OpenCode + OpenRouter
  connectors. Adds the four CLI commands. Adds TC-LLM-* tests. One
  branch, one PR.

Step 15 must complete before Step 16 begins. The reviewer for Step 16
verifies against the *revised* specs, not the patch document itself —
so the revisions must be in `main` first or the reviewer is working
against a stale baseline.

---

## Step 15 — Apply LLM-R1..R13 revisions (mechanical)

- **Builds:** the spec files reflect the patch. Existing docs updated
  per LLM-R1..R11; two new docs created per LLM-R12 (`docs/explanation/
  llm-providers.md`) and LLM-R13 (`src/llm/providers/README.md`).
- **Depends on:** Phase 1 complete and merged.
- **Source of truth:** `process/revisions/
  phase1.1-external-services-patch.md` (the patch document — read it
  first; every revision flag references a section of the patch).

### Developer prompt

```
[standing instructions]

Step 15 — Apply Phase 1.1 revisions LLM-R1 through LLM-R13. This is a
mechanical revision pass, same pattern as the audit-revisions and
modular-storage-revisions passes before it. No code is written; no
tests are added.

Read first:
- process/revisions/phase1.1-external-services-patch.md (the
  full patch — every revision flag references a section here).
- process/revisions/audit-revisions.md and
  modular-storage-revisions.md (precedent for how a revision pass is
  carried out).

Apply each revision flag exactly as specified in Part 11 of the
patch:

- LLM-R1: update ModelSpec in docs/reference/schemas.md §A to permit
  three input shapes, normalized to canonical object form in Layer 1.
- LLM-R2: add llm_providers and model_aliases sections to
  docs/explanation/asset-management-contract.md §3.2 per patch Part 2.
- LLM-R3: insert pre-flight step 5.5 "Acquire LLM client" in
  docs/explanation/agent-engine-contract.md §2.1; renumber subsequent
  steps.
- LLM-R4: add build_llm helper and the connector registry to the
  engine's listed dependencies (docs/explanation/agent-engine-
  contract.md §1).
- LLM-R5: add Part 11.5 "LLM Provider Configuration" to
  docs/reference/module-api-reference.md per patch Part 1, 3, 4.
  Update the dependency matrix (Part 12) to add an LLM column showing
  the Agent Engine calls build_llm.
- LLM-R6: add §4.13 "LLM Configuration" to docs/explanation/
  error-model.md Part 4 with the seven codes from patch Part 10.
- LLM-R7: add success criteria §10.12 to process/development-
  testing-handbook.md per patch Part 11 LLM-R7.
- LLM-R8: add Part 9.5 "LLM Provider Configuration (LLM)" to
  process/test-catalog.md with TC-LLM-01..10 per patch Part 11
  LLM-R8. Update the coverage map.
- LLM-R9: add the four CLI commands to docs/explanation/v1-scope.md
  §2.4.
- LLM-R10: add the no-provider-required note to docs/explanation/
  v1-scope.md §2.5.
- LLM-R11: add the hard rule to AGENTS.md per patch Part 11 LLM-R11.
- LLM-R12: CREATE docs/explanation/llm-providers.md as a new
  explanation-type doc covering the architectural choice of "no
  separate LLM client abstraction", the ProviderConnector Protocol
  rationale, the metadata-source pattern, and the planning sketches
  from patch Part 9.
- LLM-R13: CREATE src/llm/providers/README.md as the in-tree
  contributor guide, with content per patch Part 7.

Each revision is mechanical. Where a flag says "add" or "insert",
add the new content. Where it says "update", change the existing
content. Do NOT take initiative beyond what the flag specifies — if
something feels under-specified, surface it in your summary rather
than guessing.

Commit per revision (LLM-R1, LLM-R2, ...) or in two logical groups
(existing-doc revisions in one commit, new-doc creations in another),
whichever the patch reviewer prefers. Conventional Commits scoped to
docs(): docs(schemas): apply LLM-R1 ..., etc.

Acceptance: every LLM-R1..R13 applied; no other file changed; the
patch document's "Apply in two passes — 1. Revisions" instruction is
complete.

Report what changed, per flag, in your summary.
```

### Reviewer prompt

```
[standing instructions]

Review Step 15 — the Phase 1.1 revision pass. This is a mechanical
review, not an architectural one — the architectural decisions live
in the patch document and were made by the human. Your job is to
confirm the mechanical application is faithful.

Read first:
- process/revisions/phase1.1-external-services-patch.md, in full.

For each revision flag LLM-R1 through LLM-R13:
- Confirm the target file was changed.
- Confirm the change matches what the patch's Part 11 specified for
  that flag.
- Confirm no other file was changed outside the flag's scope (e.g.,
  LLM-R3 should touch only the engine contract — not the test
  catalog).
- Confirm the two new docs (LLM-R12 and LLM-R13) were created and
  contain the content the patch describes.

Specific checks worth running:
- ModelSpec normalization is described as happening at Layer 1, not
  somewhere downstream (LLM-R1).
- The engine contract's pre-flight has 10 steps now, with 5.5
  inserted between memory bind and config.set (LLM-R3); the
  renumbering is consistent throughout the section.
- The error catalog (LLM-R6) has all seven codes from patch Part 10
  with correct levels.
- TC-LLM-01 through TC-LLM-10 exist in the test catalog with their
  full Setup/Action/Expected/Verifies form (LLM-R8).
- docs/explanation/llm-providers.md (LLM-R12) actually covers the
  four points named: no-separate-abstraction rationale, Protocol
  rationale, metadata-source pattern, planning sketches.
- src/llm/providers/README.md (LLM-R13) is a contributor checklist,
  not a duplication of the architecture doc.

Do NOT let style preferences justify a fail — this is mechanical
faithfulness, not editorial improvement. If the developer added
content not specified in a flag, flag it as out-of-scope. If a flag
was missed entirely, fail and name which one.

Verdict: PASS or FAIL with specific defects citing flag + file +
patch section.
```

---

## Step 16 — Implement `src/llm/` and the CLI provider commands

- **Builds:** the `src/llm/` package with the `build_llm` helper, the
  `ProviderConnector` and `MetadataSourceConnector` Protocols, the
  connector registry, the OpenCode and OpenRouter connectors, and
  the four CLI commands (`connect`, `disconnect`, `providers`,
  `refresh`). Plus tests TC-LLM-01..10.
- **Depends on:** Step 15 PASS (revisions in main).
- **Spec refs:**
  - `docs/explanation/llm-providers.md` (the architecture)
  - `docs/reference/module-api-reference.md` Part 11.5 (the API)
  - `process/development-testing-handbook.md` §10.12 (success
    criteria)
  - `process/test-catalog.md` Part 9.5 (TC-LLM-01..10)
  - `docs/explanation/agent-engine-contract.md` §2.1 step 5.5 (the
    engine integration point)
  - `src/llm/providers/README.md` (the contributor pattern this
    implementation establishes)

### Developer prompt

```
[standing instructions]

Step 16 — Implement Phase 1.1: the src/llm/ package and the four CLI
provider commands. The revisions from Step 15 are in main; the
revised specs are your contract.

Read first:
- docs/explanation/llm-providers.md (the architecture, especially the
  Protocol rationale and the metadata-source pattern).
- docs/reference/module-api-reference.md Part 11.5 (the API surface
  you implement).
- process/test-catalog.md Part 9.5 (TC-LLM-01..10 — write these
  tests first, TDD).
- docs/explanation/agent-engine-contract.md §2.1 step 5.5 (the
  engine call site you wire build_llm into).

Deliver src/llm/ with this structure:

src/llm/
├── __init__.py
├── build_llm.py              # build_llm(spec, config, secrets) -> crewai.LLM
├── protocol.py               # ProviderConnector + MetadataSourceConnector Protocols
│                             # plus AuthPromptSpec, PromptField, Credentials,
│                             # ValidationResult, ModelEntry, ModelPricing,
│                             # ModelMetadata
├── registry.py               # register, get, known_types,
│                             # connected_metadata_sources
└── providers/
    ├── __init__.py           # registers OpenCodeConnector and OpenRouterConnector
    ├── opencode.py           # ProviderConnector implementation
    ├── openrouter.py         # ProviderConnector + MetadataSourceConnector
    └── README.md             # already created by LLM-R13 in Step 15

Implementation requirements:

1. build_llm.py: the small dispatch helper exactly as specified in
   Part 1 of the patch document. ~30 lines. Consults the registry;
   resolves the API key via Secrets; calls the connector's build_llm.
2. protocol.py: typing.Protocol classes with the methods documented
   in docs/reference/module-api-reference.md Part 11.5. Supporting
   dataclasses use @dataclass(frozen=True) to match the spec.
3. registry.py: explicit registration via register(type_name, cls);
   raises on duplicate (PlatformError per LLM-R6 catalog code
   llm.connector_unknown for unknown type, and a distinct code for
   duplicate registration — propose in your summary if not in the
   catalog yet).
4. providers/opencode.py: full ProviderConnector implementation.
   OpenAI-compatible client construction via crewai.LLM(...).
   discover_models hits {base_url}/models via httpx (or whichever
   HTTP client is already in the project's deps).
5. providers/openrouter.py: implements BOTH Protocols. The
   lookup_model_metadata method handles the identifier translation
   between platform's (provider, model_id) and OpenRouter's id format
   (e.g., anthropic prefix). Caches the catalog fetched at connect
   time; lookup_model_metadata reads from this cache, doesn't make
   additional network calls per lookup.
6. providers/__init__.py: imports and registers both connectors. The
   only place outside src/llm/providers/ that grows when a new
   provider is added later.

CLI commands in src/cli/ (extending whatever was built in Phase 1
Step 13):

- pi-platform connect <provider>: per patch Part 8.1. Walks the
  connector's auth_prompt_spec, validates, discovers, augments via
  any connected MetadataSourceConnector (the few-line loop from
  patch §3.4 — NOT a separate module), writes platform.yaml and
  .env atomically (all-or-nothing).
- pi-platform disconnect <provider>: per patch Part 8.2.
- pi-platform providers: per patch Part 8.3.
- pi-platform refresh [<provider>]: per patch Part 8.4.
- Unknown-provider message per patch Part 8.5.

Wire the engine: add the build_llm call to the Agent Engine's
pre-flight step 5.5 per the revised agent-engine-contract. The engine
holds the returned crewai.LLM for the exchange's lifetime and passes
it to the Framework via whatever Protocol amendment was agreed (see
docs/explanation/framework-protocol-design.md if 12-Prereq has been
run, otherwise propose the Protocol extension in your summary).

Tests TC-LLM-01..10 first. Write each test before its implementation.
Per process/test-catalog.md Part 9.5:
- TC-LLM-01: alias resolves to canonical.
- TC-LLM-02: unknown alias raises config.unknown_alias.
- TC-LLM-03: unconfigured provider raises llm.provider_not_configured.
- TC-LLM-04: missing api_key_secret raises llm.secret_missing with
  secret.not_found in the cause chain.
- TC-LLM-05: OpenCodeConnector.discover_models returns expected shape
  from a recorded fixture (no live network in tests).
- TC-LLM-06: OpenRouterConnector implements both Protocols
  (isinstance + functional check of lookup_model_metadata).
- TC-LLM-07: connect flow augments OpenCode's discovery with
  OpenRouter metadata when OpenRouter is connected.
- TC-LLM-08: connect flow runs cleanly without OpenRouter (null
  parameters, no augmentation).
- TC-LLM-09: registry rejects duplicate register().
- TC-LLM-10: API key value does not appear in platform.yaml or any
  log/event after connect — scan to confirm (same discipline as
  Secrets I1).

Network in tests: use a fixture HTTP layer (respx or pytest-httpx)
to mock provider responses with recorded fixture data. NO real network
calls. NO real API keys in fixtures (use obvious placeholders like
"sk-test-...").

Critical behaviors:
- API key values never appear in platform.yaml (the spec stores the
  secret NAME there, not the value) and never in events or logs.
  TC-LLM-10 verifies this; the code should make it impossible.
- platform.yaml writes are atomic: write to a temp file, fsync,
  rename. A partial write leaves the old file intact.
- .env writes preserve other entries (don't rewrite the whole file).
- Failed connect leaves both files unchanged (no partial state).
- Unknown providers in connect/disconnect/refresh produce the
  friendly message from patch Part 8.5, never a stack trace.

If during implementation you discover the Framework Protocol needs
amendment to accept a build_llm-produced crewai.LLM rather than
constructing its own, surface this as a finding and propose the
amendment. Do NOT change the Framework Protocol unilaterally.

Acceptance: TC-LLM-01..10 pass; success criteria §10.12 met; the
engine's step 5.5 wires through cleanly; CLI commands work
end-to-end against mocked HTTP fixtures; coverage for src/llm/ is
≥85% (per handbook §11.1).

Commit: feat(llm): implement Phase 1.1 provider configuration. Report
what you built, any Framework Protocol amendments needed, any open
questions surfaced during implementation (CrewAI version specifics,
litellm prefix conventions for OpenCode, etc.).
```

### Reviewer prompt

```
[standing instructions]

Review Step 16 — the Phase 1.1 implementation. The revisions from
Step 15 are in main; review against the revised specs, not against
the patch document.

Read first:
- docs/explanation/llm-providers.md (architecture)
- docs/reference/module-api-reference.md Part 11.5 (API surface)
- process/development-testing-handbook.md §10.12 (success
  criteria)
- process/test-catalog.md Part 9.5 (TC-LLM-01..10)
- docs/explanation/agent-engine-contract.md §2.1 step 5.5 (engine
  integration)

The architectural review questions:

1. THE MODULARITY PROPERTY. Open src/llm/providers/. Could a future
   contributor add a third connector (say, Mistral) by:
     (a) creating one file mistral.py implementing ProviderConnector,
     (b) adding one line to providers/__init__.py,
     (c) writing one test file.
   With NO other changes anywhere in the codebase? Walk the codebase
   looking for places that import OpenCodeConnector or OpenRouter-
   Connector by name (other than providers/__init__.py). Any such
   places are a modularity violation — fail and name them.

2. The build_llm helper (~30 lines per spec). Confirm it actually
   does ~30 lines of dispatch logic, not a hidden orchestration
   layer that grew. If the helper has provider-specific branches,
   that logic belongs in the connector, not the helper.

3. The connector Protocols are typing.Protocol classes (structural
   typing), not ABCs. OpenCodeConnector and OpenRouterConnector
   satisfy them by duck typing, not inheritance. Confirm.

4. MetadataSourceConnector is the second Protocol, layered on top of
   ProviderConnector. OpenRouterConnector implements both. OpenCode-
   Connector implements only the base. Confirm both isinstance
   checks behave as expected and there's no "always implement, return
   None if not a source" anti-pattern.

5. The metadata augmentation loop lives in the CLI's connect/refresh
   commands, NOT in a separate src/llm/metadata.py module. Confirm
   no such module exists; the few-line loop is in the CLI.

The functional review questions:

6. Run TC-LLM-01..10. All pass? Each one actually asserts the
   behavior (not just executes lines)?

7. TC-LLM-10 specifically: API key value scan. Connect a provider
   with a distinctive key like "sk-test-DISTINCTIVE-VALUE-1234".
   Then grep platform.yaml, any emitted events, any log files. The
   key value must appear in NO output file. Run it.

8. Atomicity: kill the connect command mid-write (interrupt the
   process between platform.yaml write and .env write, or vice versa).
   Confirm the half-written state is recoverable — either both files
   updated or neither.

9. Unknown provider behavior: pi-platform connect mistral returns
   the friendly message per patch Part 8.5, NOT a stack trace.

10. Engine integration: step 5.5 in pre-flight calls build_llm before
    config.set emission. Inspect the engine's pre-flight code; confirm
    order matches the revised spec. Confirm llm.* Fatal errors emit
    thread.exchange_failed and raise.

11. Coverage: src/llm/ at ≥85% per handbook §11.1. Run coverage and
    confirm. Investigate any uncovered branches in error paths.

12. The Framework Protocol amendment (if proposed): is the change
    justified? Documented? Confirmed via human review before merge?
    An unjustified Protocol amendment is a fail.

The "modularity is obvious" review:

13. Open src/llm/providers/README.md. Read it as if you were a
    contributor wanting to add Mistral. Could you do it in 30 minutes?
    If the README leaves you guessing at any step, the modularity
    isn't actually obvious — that's a fail.

14. OpenCodeConnector reads like a template. OpenRouterConnector
    reads like the same template plus the metadata extension.
    Confirm — if they're stylistically divergent or use different
    patterns for the same thing, future contributors won't know
    which to follow. Flag any divergence.

Verdict: PASS or FAIL with specific defects citing file + spec
section. This is the patch landing; review thoroughly.
```

---

## Notes on running these two steps

- **Step 15 first, in its own PR, merged before Step 16 starts.** The
  reviewer of Step 16 reads the revised specs as the contract. If the
  revisions aren't merged, the reviewer is reviewing against the
  patch document, which is the wrong layer — the patch is a *plan
  for* revisions, not the revisions themselves.
- **The modularity property in Step 16's reviewer prompt is the
  critical one.** Question 1 of the reviewer prompt is the test of
  whether the patch's central design goal was met. If a future
  contributor can't add a provider by changing one file plus one
  registry line, the implementation undermined the patch's purpose
  even if all the tests pass.
- **Network fixtures, not real network.** Step 16's developer prompt
  is explicit about this because real-network tests are slow, flaky,
  and burn credits. The component tests must mock HTTP via a
  fixture library. The only place real provider calls happen is
  manual end-to-end testing during development — not in the
  committed test suite.
- **Step 16 cannot conclude Phase 1.1 by itself.** After Step 16
  passes, run a manual end-to-end verification: connect OpenCode,
  connect OpenRouter, run a real agent through the full pipeline
  with a real LLM. This isn't a Step in the build plan because it
  involves real credentials and real cost — but it's the final
  confirmation before declaring Phase 1.1 done.

---

## What this build plan does NOT include

- **The four planned-but-unimplemented providers** (Anthropic, OpenAI,
  Bedrock, Llama-local) are out of scope. Each is documented as a
  planning sketch in the patch document Part 9. When you're ready to
  implement them, each is one branch following the established
  pattern — likely Step 17 (Anthropic), Step 18 (OpenAI), Step 19
  (Bedrock), Step 20 (Llama-local). They're small enough that they
  don't need full developer/reviewer prompts of their own; the
  prompts from Step 16 plus the patch's Part 9 sketch should be
  sufficient. If they turn out larger than expected, add prompts
  later.
- **The Framework Protocol amendment** if needed (Step 16 developer
  prompt flags this). If Step 12-Prereq has run, the amendment is
  done there; if Phase 1 shipped without 12-Prereq, the amendment
  happens during Step 16 with human review.
- **The CLI user-perspective review** still owed from earlier in our
  threads. That's a separate document.
