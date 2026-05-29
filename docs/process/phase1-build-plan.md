# Phase 1 Build Plan — Developer & Reviewer Agent Prompts

**Draft 0.2** *(Step 12 split into 12-Prereq + 12a + 12b per the
framework integration finding; see "Step 12 history" below.)*

> Phase 1 broken into sequential steps for a developer agent to build and
> a reviewer agent to verify. Steps are in dependency order — each builds
> only on what came before. Every step has a developer prompt and a
> reviewer prompt, ready to use.
>
> The loop per step: run the developer prompt → run the reviewer prompt →
> if the reviewer fails it, feed the findings back to the developer →
> repeat until the reviewer passes → merge → next step.

---

## Step 12 history (read before Step 12)

In draft 0.1 of this plan, Step 12 was a single step — "Agent Engine
(CrewAI integration) — GATING." When the developer agent began the
work, it reported (correctly) that CrewAI's hook surface and message-
list mutation behavior carry real architectural risk that must be
spiked before committing the engine to a specific framework adapter.

The verdict: split Step 12 into three:

- **12-Prereq** (read-only research spike) — produces a design note
  proposing a minimal `Framework` Protocol informed by what CrewAI
  actually exposes.
- **12a** (engine + FakeFramework) — builds the engine against the
  `Framework` Protocol with a `FakeFramework` reference. Proves the
  engine's correctness in isolation. CrewAI is NOT installed.
- **12b** (CrewAIFramework adapter) — wraps real CrewAI as a
  `Framework` implementation. Re-runs TC-ENG-01..11 plus new
  CrewAI-specific TC-CRW-* cases. **Required for Phase 1 to ship** —
  not optional.

The split honors the agent-engine-contract's §0 principle (conformance
is observable outcomes, not control flow) and locates the architectural
gate at the right layer: the engine's correctness is proven in 12a, the
CrewAI adapter's conformance is proven in 12b. A failure in 12b is
about the adapter or about CrewAI, not about the engine.

The Test Catalog has been amended in lockstep: TC-ENG-01..11 are
framework-agnostic; a new TC-CRW-* section reserves CrewAI-specific
cases authored in 12b.

---

## How to use this plan

- Work the steps **in order**. Do not start step N+1 until step N's
  reviewer has passed and the work is merged to `main`.
- Each step is sized to be a single focused branch — small enough to
  review well, large enough to be a coherent unit.
- Both agents always operate under the **standing instructions** below,
  which encode the engineering and testing practices. The per-step
  prompts add only the step-specific detail.
- The reviewer is **adversarial but constructive**: its job is to find
  gaps against the contract, the invariants, and the test catalog — not
  to rubber-stamp.

---

## Standing instructions (prepended to every prompt)

Give both agents this block at the start of every step. It is the
shared context.

```
You are working on the Agent Platform, a local-first multi-agent
workflow platform. The repository contains the complete Phase 1
specification suite under docs/. Code does not exist yet (or exists
only up to the prior step).

Before doing anything:
1. Read AGENTS.md at the repo root — it is your operating manual.
2. Read docs/process/development-testing-handbook.md — engineering
   principles, Python practices, tooling, the containerized dev
   workflow, and the testing strategy. Follow it exactly.
3. Use the Codanna code index (not grep) to navigate existing code.

Non-negotiable practices:
- Work INSIDE the dev container (the canonical environment).
- Python 3.12+, type hints everywhere, mypy --strict must pass.
- Pydantic v2 for all data models. Async for all I/O.
- Raise PlatformError subclasses only; never let a non-PlatformError
  escape a public method. Use the registered error codes from the
  error catalog.
- The filesystem JSONL event log is ground truth; never add state that
  can't be rebuilt from it.
- Test-driven: write tests from the named Test Catalog cases first,
  then implement to pass them.
- Lint (ruff), format (ruff), type-check (mypy), and test on every
  change. The commit gate is a clean rebuild + full suite via
  compose.test.yml.
- Conventional Commits for messages, scoped to the subsystem.
- Document as you go: user-facing changes include their docs.

The contract is the law. Where the spec is silent, flag it rather than
guessing. Where the spec and your instinct conflict, the spec wins —
raise the conflict explicitly.
```

---

## Step 0 — Project scaffolding

- **Builds:** the repo skeleton that all code sits in — `pyproject.toml`
  (uv), `ruff`/`mypy`/`pytest` config, `pre-commit`, the `Containerfile`,
  `compose.dev.yml`, `compose.test.yml`, `.devcontainer/devcontainer.json`,
  the CI config, the `src/` and `tests/` layout, and the **Fake LLM stub
  interface** plus the **event-sequence assertion helpers** and **fixture
  factories** the test strategy depends on.
- **Depends on:** nothing.
- **Spec refs:** `docs/process/development-testing-handbook.md` (§3, §4,
  §8, §9), `AGENTS.md` (project layout).

- **Developer prompt:**
```
[standing instructions]

Step 0 — Project scaffolding. No platform logic yet; build the skeleton
everything else depends on.

Deliver:
- pyproject.toml managed by uv, with dependency groups: (default) runtime,
  dev (ruff, mypy, pre-commit), test (pytest, pytest-asyncio, pytest-cov,
  testcontainers). Pin nothing unnecessarily; commit uv.lock.
- ruff config (strict rule set per the handbook §3.2), mypy --strict
  config with the Pydantic plugin, pytest config (asyncio mode, coverage).
- pre-commit running ruff + mypy on staged files.
- Containerfile, compose.dev.yml (live-mounted source, named volumes for
  the venv and uv cache), compose.test.yml (clean rebuild, fresh
  Postgres + Redis services, runs the full gate), and
  .devcontainer/devcontainer.json pointing at the dev container. Use
  Podman; ensure Docker-compatible.
- src/ package layout matching AGENTS.md (kernel/, errors/, store/, bus/,
  assets/, providers/, tools/, skills/, engine/), each with an empty
  __init__.py.
- tests/ layout (unit/, component/, integration/, e2e/, golden/,
  fixtures/, conftest.py).
- A Fake LLM stub in tests/fixtures/ implementing the model-client
  interface, configurable with a scripted response list (text responses,
  tool-call responses, error responses) and recording the context it was
  asked to process. Per handbook §8.3.
- Event-sequence assertion helpers (assert_event_sequence,
  assert_event_content, assert_materialization) and factory fixtures
  (make_agent_config, make_event, make_session, temp_platform_root) per
  handbook §8.5–8.6. Stub the bodies if they depend on types not yet
  built; make them import-clean.
- A CI config that runs the compose.test.yml gate.

Acceptance: `uv sync` works; `uv run ruff check .`, `uv run mypy .`, and
`uv run pytest` all run clean (zero tests is fine) inside the dev
container; the commit gate via compose.test.yml runs green.

Commit with chore(scaffold): ... and report what you created.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 0 — Project scaffolding. Do not assume it works; verify.

Check:
- Does `uv sync` produce a working environment from the committed lockfile?
- Do ruff (strict), mypy --strict, and pytest all run clean inside the
  dev container?
- Does compose.dev.yml live-mount source and use named volumes for the
  venv/uv cache (so code edits need no rebuild)? Does compose.test.yml do
  a clean --no-cache rebuild with fresh PG + Redis and run the full gate?
- Does devcontainer.json correctly point at the dev container?
- Is the src/ layout aligned with AGENTS.md? Is every package importable?
- Is the Fake LLM stub faithful to the intended model-client interface,
  scriptable (text/tool-call/error), and does it record the asked
  context? This fixture is load-bearing for all later integration tests —
  scrutinize it.
- Are the assertion helpers and factory fixtures present and import-clean?
- Does CI run the same compose.test.yml gate as the local commit gate?

Produce a verdict: PASS or FAIL with a numbered list of specific defects.
For each defect, cite the file and the handbook section it violates.
```

---

## Step 1 — Error Model

- **Builds:** the `PlatformError` base class, the `Level` enum, the
  canonical `to_dict()` envelope, `WrappedError`, and the full error-type
  catalog as subclasses with their `code` and `level` constants.
- **Depends on:** Step 0.
- **Spec refs:** `docs/explanation/error-model.md`,
  `docs/reference/module-api-reference.md` §13.4.
- **Test Catalog:** TC-ERR-01..05.

- **Developer prompt:**
```
[standing instructions]

Step 1 — Error Model. Implement docs/explanation/error-model.md in
src/errors/.

Deliver:
- The Level enum (DIAGNOSTIC, RECOVERABLE, FATAL) as a str-Enum.
- PlatformError base with code/level class vars, (message, where, detail)
  constructor, and to_dict() producing the canonical envelope
  (type, message, where, detail, cause) with recursive cause
  serialization.
- WrappedError.from_exception for non-platform exceptions.
- Every error type in the catalog (Part 4 of the spec, all sections
  including 4.11 File Handling and 4.12 generic) as a PlatformError
  subclass with its registered code and assigned level, and a
  constructor enforcing its documented detail shape.
- A registry/enumeration mechanism so all codes can be listed
  programmatically (needed by TC-ERR-04).

Write tests first from TC-ERR-01..05 (docs/process/test-catalog.md). In
particular: every subclass serializes to the envelope; the cause chain
nests 3 deep; every class code is in the catalog; level is intrinsic.

Acceptance: TC-ERR-01..05 pass; success criteria in handbook §10.10 met.

Commit feat(errors): ... and report.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 1 — Error Model against docs/explanation/error-model.md and
success criteria §10.10.

Verify:
- to_dict() output exactly matches the canonical envelope; cause chains
  serialize recursively (construct a 3-deep chain and check).
- EVERY catalog entry (all of Part 4, including 4.11 and 4.12) has a
  corresponding subclass with the correct code AND level. Cross-check the
  implemented set against the spec table — list any missing or extra
  codes.
- Each subclass's constructor enforces the documented detail keys.
- WrappedError captures original type and args.
- Invariants E-I1 through E-I5 hold (especially: level intrinsic to type,
  registered types only).
- Tests TC-ERR-01..05 exist, pass, and actually assert the behavior (not
  just execute it). Is the "every emitted type is in the catalog" test
  driven by the real registry, or hand-maintained (it must be the
  registry)?

Verdict: PASS or FAIL with specific defects, each citing file + spec
section.
```

---

## Step 2 — Core schemas (events, entities, shared types)

- **Builds:** the behavior-free Pydantic models everything shares — the
  prefixed-UUID identifier types, `Event`, `Session`, `Thread`,
  `ContentRef`, the actor shape, and the event-`data` payload models per
  event type.
- **Depends on:** Step 1.
- **Spec refs:** `docs/reference/schemas.md` §B, §C;
  `docs/reference/module-api-reference.md` §13.
- **Test Catalog:** model-validation unit tests (author as TC-SCH-*).

- **Developer prompt:**
```
[standing instructions]

Step 2 — Core schemas. Implement the shared data models from
docs/reference/schemas.md (§B events, §C entities) and module-api-
reference §13. Place in src/ (e.g., src/schemas/ or co-located per the
layout) — choose per the handbook and note your choice.

Deliver:
- Prefixed-UUID identifier types (SessionId, ThreadId, EventId, AgentId,
  MemoryItemId, SubscriptionId) with prefix validation at construction.
- The Event model (event_id, parent_event_id, thread_id, session_id,
  type, data, actor, created_at, optional content_ref).
- Session, Thread, ContentRef models per §13.2 / schemas §C.
- Pydantic models for each event type's `data` payload defined in the
  schemas (tool.called, tool.result, tool.failed, model.request,
  model.response, thread.exchange_started/completed/failed, user.message,
  agent.message, config.set, memory.written/deleted/retrieved, etc.).
- The Error model (envelope) wired to the Step 1 PlatformError.to_dict().

These are data only — no behavior beyond validation. Write
TC-SCH-* unit tests: valid construction, prefix validation rejects bad
ids, round-trip serialization is stable, each event-data model rejects
malformed payloads.

Acceptance: models validate and serialize per spec; identifier prefixes
enforced.

Commit feat(schemas): ... and report, including where you placed the
models and why.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 2 — Core schemas against docs/reference/schemas.md §B/§C and
module-api-reference §13.

Verify:
- Every identifier type validates its prefix and rejects wrong prefixes.
- The Event model matches the spec field-for-field (names, types,
  optionality). parent_event_id nullable; content_ref optional.
- Every event type named in the schemas has a corresponding data-payload
  model. List any event types in the spec with no model.
- Serialization round-trips are stable and match the wire shape used in
  the JSONL log.
- Models are behavior-free (no I/O, no logic beyond validation).
- TC-SCH-* tests exist and meaningfully assert validation and rejection.

Verdict: PASS or FAIL with specific defects citing file + spec section.
```

---

## Step 3 — Config schema + Kernel/Resolver

- **Builds:** the `ResolvedAgentConfig` and sub-models (ModelSpec,
  IOSpec, SubsystemsConfig, RuntimeSpec, the simplified PromptSpec) and
  the pure resolver (`validate_partial`, `resolve`, `resolve_and_validate`,
  `export_json_schema`) with the three-layer validation and the
  `ConfigValidator` protocol.
- **Depends on:** Step 2.
- **Spec refs:** `docs/explanation/kernel-resolver-contract.md`,
  `docs/reference/schemas.md` §A, `docs/reference/module-api-reference.md`
  §1.
- **Test Catalog:** TC-KRN-01..10.

- **Developer prompt:**
```
[standing instructions]

Step 3 — Config schema + Kernel/Resolver. Implement the config models
(schemas §A) and the pure resolver (kernel-resolver-contract,
module-api-reference §1) in src/kernel/.

Deliver:
- ResolvedAgentConfig and sub-models per schemas §A: ModelSpec, IOSpec
  (with the file_ref type as a string), SubsystemsConfig, RuntimeSpec,
  and the simplified PromptSpec (list[str] of refs OR a single string).
- ValidationError and ValidationResult value types (NOT PlatformError
  subtypes — they are findings carried inside ConfigValidationError.detail
  per error-model §3.1).
- The ConfigValidator protocol: Callable[[dict], ValidationResult].
- validate_partial, resolve (RFC 7386 merge), resolve_and_validate
  (Layer 1 per partial → resolve → Layer 1 on resolved → Layer 2 → Layer
  3 dispatch), export_json_schema.
- Pure: zero I/O. Aggregate ALL validation errors, never just the first.

Write tests first from TC-KRN-01..10, including the RFC 7386 vectors,
determinism over 1000 runs, multi-error aggregation, and the purity test
(run with filesystem/network denied).

Acceptance: TC-KRN-01..10 pass; success criteria §10.1 met (including
≥95% coverage — this module is pure logic).

Commit feat(kernel): ... and report.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 3 — Config schema + Kernel/Resolver against
docs/explanation/kernel-resolver-contract.md, schemas §A,
module-api-reference §1, and success criteria §10.1.

Verify:
- resolve passes 100% of the RFC 7386 vectors; null-deletes; absent-
  inherits; deterministic over many runs.
- resolve_and_validate runs all three layers in the right order, calls
  every subsystem validator and every system validator, and validates
  Layer 1 both per-partial and on the resolved config.
- ALL validation errors are aggregated (craft a 3-error input and count).
- ValidationError is a finding type, NOT a PlatformError subtype.
- The module is genuinely pure — the purity test denies I/O and passes.
- PromptSpec is the simplified shape (list | string), no layer/id wrapper.
- Coverage ≥95% for the kernel.
- TC-KRN-01..10 exist, pass, and assert meaningfully.

Verdict: PASS or FAIL with specific defects citing file + spec section.
```

---

## Step 4 — Session/Run Store

- **Builds:** the store with the **SQLite default** entity backend, JSONL
  append with `flock`, leaf computation, `write_content`,
  `reconstruct_from_filesystem`, and the backend abstraction so Postgres
  can drop in.
- **Depends on:** Step 2 (events/entities), Step 1 (errors).
- **Spec refs:** `docs/explanation/session-run-store-contract.md`,
  `docs/reference/module-api-reference.md` §3.
- **Test Catalog:** TC-STR-01..12.

- **Developer prompt:**
```
[standing instructions]

Step 4 — Session/Run Store. Implement session-run-store-contract and
module-api-reference §3 in src/store/.

Deliver:
- The full public API: create_session, get_session, update_session,
  delete_session, get_thread, get_leaf, append_event, read_events,
  read_path_to_leaf, write_content, reconstruct_from_filesystem.
- A backend abstraction for entity records with a SQLite implementation
  as the DEFAULT (.platform/entities.db). Leave a clean seam for a
  Postgres implementation (same interface) but you need only ship SQLite
  now.
- JSONL per-session files; append via O_APPEND under flock(LOCK_EX) on
  the sidecar .lock; leaf computed by end-of-file scan; parent_event_id
  set to the current leaf atomically under the lock.
- Recovery on open: truncate a partial final line that fails to parse.
- write_content: SHA-256-addressed content/<hash>.bin, idempotent.
- reconstruct_from_filesystem: rebuild entity records from JSONL,
  non-destructive, skip-with-warning on corrupt files.

Write tests first from TC-STR-01..12. Critical ones: concurrent appends
(same process AND cross-process) all persist with a linear parent chain;
mid-write kill is recoverable; write_content idempotent; reconstruct
rebuilds. Parameterize the suite so it can later run against Postgres too.

Acceptance: TC-STR-01..12 pass; success criteria §10.3 met.

Commit feat(store): ... and report.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 4 — Session/Run Store against session-run-store-contract,
module-api-reference §3, and success criteria §10.3.

Verify:
- The append path is genuinely atomic under the lock: leaf-compute +
  parent-set + write happen while holding flock. Read the code; confirm
  no race window.
- Concurrent-append tests cover BOTH same-process (asyncio) and
  cross-process (two OS processes). 100 appends → 100 persisted, linear
  chain, none lost. Run them; don't trust the claim.
- Mid-write kill recovery truncates only the partial final line; all
  prior events intact.
- write_content is idempotent and immutable (STR I11); content never
  rewritten for an existing hash.
- reconstruct_from_filesystem is non-destructive and reports correct
  counts.
- The entity backend is behind an abstraction; the SQLite impl is the
  default; the seam for Postgres is clean (no SQLite-isms leaking into
  the contract surface).
- Invariants STR I1–I11 hold. TC-STR-01..12 pass and assert meaningfully.

Verdict: PASS or FAIL with specific defects citing file + spec section.
```

---

## Step 5 — Event Bus

- **Builds:** the bus with the **in-process asyncio** default backend —
  `publish`, `subscribe` (topic patterns + attribute filters),
  `unsubscribe`, failure isolation — behind a backend abstraction so
  Redis can drop in.
- **Depends on:** Step 2 (events).
- **Spec refs:** `docs/explanation/event-bus-contract.md`,
  `docs/reference/module-api-reference.md` §4.
- **Test Catalog:** TC-BUS-01..07.

- **Developer prompt:**
```
[standing instructions]

Step 5 — Event Bus. Implement event-bus-contract and module-api-reference
§4 in src/bus/.

Deliver:
- publish, subscribe (exact / prefix-wildcard / full-wildcard topic
  patterns plus attribute filters), unsubscribe.
- A backend abstraction with an IN-PROCESS asyncio pub/sub as the DEFAULT.
  Leave a clean seam for a Redis backend (same interface); ship in-process
  now.
- Failure isolation: a raising handler must not affect other subscribers
  or remove itself; publish failures must not raise into the caller
  (best-effort).

Write tests first from TC-BUS-01..07: matching delivery, non-matching
exclusion, wildcard, attribute filter, raising-handler isolation,
publish-failure-does-not-break. Parameterize so the suite can later run
against Redis.

Acceptance: TC-BUS-01..07 pass; success criteria §10.4 met.

Commit feat(bus): ... and report.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 5 — Event Bus against event-bus-contract,
module-api-reference §4, and success criteria §10.4.

Verify:
- Topic matching is correct for exact, prefix-wildcard, and full-wildcard;
  attribute filters work; non-matching subscribers are never called.
- A deliberately-raising handler does not break delivery to others and
  stays subscribed (BUS I4).
- publish swallows backend failure and does not raise into the caller.
- The in-process backend is the default; the Redis seam is clean.
- The suite is parameterized to run against both backends later.
- TC-BUS-01..07 pass and assert meaningfully.

Verdict: PASS or FAIL with specific defects citing file + spec section.
```

---

## Step 6 — Asset Management + platform bootstrap

- **Builds:** `platform.yaml` loading and section dispatch, the bundle
  catalog, ref resolution (full + shorthand), prompt pre-resolution, the
  three-layer assembly that feeds the kernel.
- **Depends on:** Step 3 (kernel/config), Step 1 (errors).
- **Spec refs:** `docs/explanation/asset-management-contract.md`,
  `docs/reference/module-api-reference.md` §2.
- **Test Catalog:** TC-ASM-01..12.

- **Developer prompt:**
```
[standing instructions]

Step 6 — Asset Management + platform bootstrap. Implement
asset-management-contract and module-api-reference §2 in src/assets/.

Deliver:
- initialize(): load platform.yaml (generate defaults on first run,
  including the storage + event_bus + file_access sections), validate
  top-level structure, expose sections via get_platform_config(section).
  This is the platform bootstrap (Am-I11) — the ONLY module that reads
  platform.yaml.
- Catalog build: scan bundles/, read+validate manifests, detect duplicate
  names (fatal) and name/dir mismatch (fatal), reject reserved name
  `core`, register assets (tool/skill/agent/prompt). Skill entries carry
  full SkillMetadata parsed from frontmatter; tool entries carry the path
  only (do NOT import Python — read-only, Am-I10).
- get_asset, resolve_ref (full + shorthand; ambiguous shorthand raises
  with all candidates), list_bundles, list_assets.
- build_resolved_config: three-layer assembly (defaults → agent → call
  overrides), PRE-RESOLVE prompt refs to content, PASS THROUGH tool/skill
  refs, call the kernel's resolve_and_validate.

Write tests first from TC-ASM-01..12. Critical: prompt pre-resolution vs
tool/skill pass-through (Am-I4/I5); ambiguous shorthand; section dispatch;
duplicate-name fatal; read-only discovery.

Acceptance: TC-ASM-01..12 pass; success criteria §10.2 met.

Commit feat(assets): ... and report.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 6 — Asset Management against asset-management-contract,
module-api-reference §2, and success criteria §10.2.

Verify:
- Asset Management is the SOLE reader of platform.yaml; sections dispatch
  correctly to the right consumers (Am-I11).
- First-run generates a default platform.yaml with the storage (sqlite)
  and event_bus (in_process) and file_access sections.
- Duplicate bundle name and name/dir mismatch are FATAL; `core` rejected.
- Discovery is READ-ONLY: tool Python files are NOT imported here
  (Am-I10); only paths and parsed metadata are catalogued. Confirm by
  inspection.
- Prompt refs are pre-resolved to content; tool/skill refs are NOT
  (Am-I4/I5) — inspect a built resolved config.
- Ambiguous shorthand raises with ALL candidates named; never silently
  picks one (Am-I6).
- TC-ASM-01..12 pass and assert meaningfully.

Verdict: PASS or FAIL with specific defects citing file + spec section.
```

---

## Step 7 — Secrets / Sandbox / Authorization providers

- **Builds:** the three Provider Modules — `dotenv` secrets, `native`
  sandbox, `rule_based` authorization — each behind its contract with a
  registry seam for future variants.
- **Depends on:** Step 6 (platform config), Step 1 (errors).
- **Spec refs:** `docs/explanation/secrets-sandbox-authz-contract.md`,
  `docs/reference/module-api-reference.md` §6–8.
- **Test Catalog:** TC-SEC-*, TC-SBX-*, TC-AUZ-*.

- **Developer prompt:**
```
[standing instructions]

Step 7 — Secrets, Sandbox, Authorization providers. Implement
secrets-sandbox-authz-contract and module-api-reference §6–8 in
src/providers/.

Deliver:
- Secrets `dotenv` variant: resolve, exists, list_names. Parse .env once
  at init. Values NEVER logged or placed in events.
- Sandbox `native` variant: execute(work, policy) — pass-through of result
  and exceptions. Validate policy; raise sandbox.policy_invalid on bad
  policy.
- Authorization `rule_based` variant: allow(principal, action) returning
  AuthorizationResult. Rules are {who, what, effect} with regex FULLMATCH,
  first-match-wins, default-deny. Compile regexes at init; raise
  authz.regex_invalid on bad regex.
- Each provider gets its config via assets.get_platform_config(section).
- A registry seam so future variants register without core changes.

Write tests first from TC-SEC-*, TC-SBX-*, TC-AUZ-*. Critical: secret
never appears in logs/events; sandbox passes results and exceptions
unchanged; authz fullmatch (tool.b ≠ tool.bash), first-match-wins,
default-deny.

Acceptance: those cases pass; success criteria §10.7 met.

Commit feat(providers): ... and report.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 7 — the three providers against secrets-sandbox-authz-
contract, module-api-reference §6–8, and success criteria §10.7.

Verify:
- Secrets: resolve returns values; a distinctive secret value appears in
  NO log and NO event (write a test that scans). exists/list_names do not
  leak values.
- Sandbox native: result and exception pass-through are byte-identical to
  a direct call (Sb-I1/I2).
- Authz: regex is FULLMATCH (tool.b must NOT match tool.bash);
  first-match-wins with a deny-before-allow; default-deny on no match;
  malformed regex fails at INIT not at allow-time.
- Each provider reads its config only via get_platform_config.
- The variant registry seam is clean.
- TC-SEC/SBX/AUZ cases pass and assert meaningfully.

Verdict: PASS or FAIL with specific defects citing file + spec section.
```

---

## Step 8 — Memory Provider

- **Builds:** the `recent_buffer` variant — `bind_session`,
  `MemoryContext` (write, retrieve, list, delete, compact), cache rebuilt
  from the log via `store.read_events`, asymmetric tag config.
- **Depends on:** Step 4 (store), Step 3 (kernel), Step 1 (errors).
- **Spec refs:** `docs/explanation/memory-provider-contract.md`,
  `docs/reference/module-api-reference.md` §5.
- **Test Catalog:** TC-MEM-01..09.

- **Developer prompt:**
```
[standing instructions]

Step 8 — Memory Provider. Implement memory-provider-contract and
module-api-reference §5 in src/providers/ (memory).

Deliver:
- The recent_buffer variant with its Layer 3 validator
  (ConfigValidator signature): write_tag singular no-wildcards, read_tags
  list with wildcards, recent_turns, max_tokens.
- bind_session: rebuild the in-process cache by calling
  store.read_events(thread_id) and filtering memory.written/deleted —
  NEVER read JSONL files directly (R12 / Store I8).
- MemoryContext: write (tags with the configured write_tag, rejects if
  none configured), retrieve (read_tags-filtered items + recent events),
  list, delete, compact.
- Memory NEVER emits events (MEM I1) — it returns results; the engine
  emits memory.* events.

Write tests first from TC-MEM-01..09. Critical: cache rebuild equals
pre-restart state; write applies configured tag and user can't override;
wildcard write_tag rejected at validation; session isolation; memory does
not import the store's emission methods.

Acceptance: TC-MEM-01..09 pass; success criteria §10.6 met.

Commit feat(memory): ... and report.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 8 — Memory Provider against memory-provider-contract,
module-api-reference §5, and success criteria §10.6.

Verify:
- bind_session rebuilds via store.read_events, NOT by reading JSONL
  directly (inspect imports/calls). Cache-rebuild test: retrievals
  identical before and after a simulated restart.
- write applies the configured write_tag; an override attempt is ignored/
  rejected (MEM I4). No write_tag → memory.write_disabled.
- A wildcard write_tag is rejected at config validation (MEM I5).
- read_tags wildcard matches hierarchically; session A never sees B's
  items (MEM I7).
- Memory emits NO events (MEM I1) — confirm it does not import or call the
  store's append/emit path.
- TC-MEM-01..09 pass and assert meaningfully.

Verdict: PASS or FAIL with specific defects citing file + spec section.
```

---

## Step 9 — File Handling / path policy

- **Builds:** the path-access policy (`allowed_paths`/`denied_paths`,
  deny-overrides, default-deny, symlink-safe normalization) and the
  `is_path_allowed` check the builtin tools will call.
- **Depends on:** Step 6 (platform config), Step 1 (errors).
- **Spec refs:** `docs/explanation/file-handling.md`,
  `docs/reference/module-api-reference.md` (file handling notes).
- **Test Catalog:** TC-FIL-01..07.

- **Developer prompt:**
```
[standing instructions]

Step 9 — File Handling / path policy. Implement file-handling.md (Part 4)
in src/ (e.g., src/files/ or within tools — choose per layout and note it).

Deliver:
- Load the file_access policy from platform.yaml via get_platform_config
  (one allowed_paths list, one denied_paths list).
- is_path_allowed(path) -> (bool, reason|None): normalize the path
  (resolve .. and follow symlinks), check denied first (deny overrides),
  then allowed, else default-deny.
- The file_ref type is just a string (already in schemas); validate it as
  a path at input boundaries.
- Raise file_access.denied (Diagnostic) on denial; the codes are in the
  error catalog 4.11.

Write tests first from TC-FIL-01..07. Critical: deny overrides allow even
when a path is under an allowed root; default-deny for unmatched;
symlink-to-denied is denied (create a real symlink); tools never write to
content/.

Acceptance: TC-FIL-01..07 pass; success criteria §10.11 met.

Commit feat(files): ... and report where you placed the module and why.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 9 — File Handling / path policy against file-handling.md and
success criteria §10.11.

Verify:
- Deny is checked BEFORE allow; adding .env to allowed_paths does NOT
  grant access (deny wins) — Fh-I3.
- Default-deny for paths matching neither list.
- Path normalization resolves .. AND follows symlinks before evaluation;
  a symlink under an allowed root pointing at a denied target is denied
  (create the symlink and test) — Fh-I4.
- The policy loads via get_platform_config (not by reading platform.yaml
  directly).
- file_access.denied uses the registered code/level.
- TC-FIL-01..07 pass and assert meaningfully.

Verdict: PASS or FAIL with specific defects citing file + spec section.
```

---

## Step 10 — Tools Subsystem

- **Builds:** the `@tool` decorator + args_schema, the dispatch pipeline
  (validate → authz → secrets → sandbox → execute), the four builtins
  (read/write/edit/bash), and MCP integration (eager/lazy).
- **Depends on:** Steps 7 (providers), 9 (path policy), 6 (assets), 1
  (errors).
- **Spec refs:** `docs/explanation/tools-subsystem.md`,
  `docs/reference/module-api-reference.md` §9.
- **Test Catalog:** TC-TLS-01..08.

- **Developer prompt:**
```
[standing instructions]

Step 10 — Tools Subsystem. Implement tools-subsystem and module-api-
reference §9 in src/tools/.

Deliver:
- The @tool decorator with a Pydantic args_schema; ToolContext
  (agent_id, session_id, thread_id, principal, resolved secrets).
- register_tools_for_agent: import declared tool Python files (this is
  where imports happen, NOT in assets), register builtins, register MCP
  tools (eager started now; lazy as placeholders that start on first use).
- invoke_tool dispatch in EXACT order: validate args (Pydantic) → authz
  allow(principal=agent.<id>, tool.<name>) → resolve secrets → sandbox.
  execute → run tool fn. Map failures to the right Diagnostic errors
  (tool.invalid_arguments, tool.unauthorized via the engine's event,
  tool.execution_failed, tool.timeout, mcp.*).
- The four builtins: read/write/edit consult is_path_allowed (Step 9);
  bash is EXEMPT in v1 (document the limitation in code + docs/how-to).
- The Layer 3 validator (ConfigValidator signature) per the spec.

Write tests first from TC-TLS-01..08. Critical: dispatch order
(instrument each stage); bad args rejected before execution; tool
exception → tool.execution_failed; missing secret → tool.setup_failed
with secret.not_found cause; verbatim result capture; read honors path
policy, bash bypasses it.

Acceptance: TC-TLS-01..08 pass; success criteria §10.8 met.

Commit feat(tools): ... and report.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 10 — Tools Subsystem against tools-subsystem,
module-api-reference §9, and success criteria §10.8.

Verify:
- Dispatch order is EXACTLY validate → authz → secrets → sandbox →
  execute. Instrument and assert call order; a wrong order is a fail.
- Tool Python imports happen HERE, not in Asset Management.
- Bad args raise tool.invalid_arguments BEFORE the tool fn runs.
- Tool exceptions become tool.execution_failed (Diagnostic); the
  exchange-continuation contract is honored at the engine layer (note for
  Step 12).
- Missing secret at registration → tool.setup_failed with secret.not_found
  in the cause chain.
- Result capture is verbatim (T-I5).
- read/write/edit enforce the path policy; bash's exemption is explicit
  and documented (TC-FIL-06 / Fh-I6). Secrets reach tools only via
  ToolContext (T-I3) — confirm tools can't call secrets.resolve directly.
- TC-TLS-01..08 pass and assert meaningfully.

Verdict: PASS or FAIL with specific defects citing file + spec section.
```

---

## Step 11 — Skills Subsystem

- **Builds:** skill discovery surfacing (descriptions → system prompt),
  `disable-model-invocation` handling, and force-invoke preparation (the
  synthetic `read` data).
- **Depends on:** Step 10 (tools/read), Step 6 (assets), Step 1 (errors).
- **Spec refs:** `docs/explanation/skills-subsystem.md`,
  `docs/reference/module-api-reference.md` §10.
- **Test Catalog:** TC-SKL-01..05.

- **Developer prompt:**
```
[standing instructions]

Step 11 — Skills Subsystem. Implement skills-subsystem and module-api-
reference §10 in src/skills/.

Deliver:
- get_enabled_skills(config): resolve enabled refs to EnabledSkill records
  (metadata from the catalog; body NOT loaded). The Layer 3 validator
  (ConfigValidator signature).
- format_skills_listing(skills): the system-prompt block; EXCLUDE skills
  with disable-model-invocation: true; empty string if none eligible.
- force_invoke(skill_ref, config): verify the ref is enabled (else
  skill.forced_not_enabled) and return SkillInvocation with the SKILL.md
  path the engine will pass to a synthetic read call. Skills do NOT emit
  events — the engine does.

Write tests first from TC-SKL-01..05. Critical: enabled-but-disabled skill
is excluded from the listing yet still force-invocable; force-invoke of a
non-enabled skill raises; force-invoke uses tool.read authorization.

Acceptance: TC-SKL-01..05 pass; success criteria §10.9 met.

Commit feat(skills): ... and report.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 11 — Skills Subsystem against skills-subsystem,
module-api-reference §10, and success criteria §10.9.

Verify:
- Only descriptions (not bodies) are surfaced (Sk-I1). Bodies load via
  read on demand.
- disable-model-invocation skills are excluded from the listing but remain
  force-invocable (Sk-I2).
- force_invoke rejects non-enabled refs (skill.forced_not_enabled) and
  targets the SKILL.md path for a synthetic read; authorization is against
  tool.read (Sk-I4), not a skill-specific action.
- Skills emit no events themselves.
- TC-SKL-01..05 pass and assert meaningfully.

Verdict: PASS or FAIL with specific defects citing file + spec section.
```

---

## Step 12-Prereq — Framework Protocol research spike (read-only)

- **Builds:** a written design note (committed to
  `docs/explanation/framework-protocol-design.md`) proposing the minimal
  `Framework` Protocol shape — the seam between the engine and whatever
  framework drives the agentic loop. This is a read-only spike that
  de-risks Step 12a by ensuring the Protocol assumes only guarantees a
  real framework (CrewAI initially) can plausibly deliver.
- **Why this step exists:** Step 12 was split into 12a (engine against a
  FakeFramework) and 12b (CrewAI adapter). The split makes the engine's
  correctness verifiable in isolation. But it carries a risk: if the
  FakeFramework gives the engine guarantees CrewAI can't match, the
  engine is built on sand. This prereq prevents that.
- **Depends on:** all prior steps; reads docs only.
- **Spec refs:** `docs/explanation/agent-engine-contract.md` §0 (the
  observable-outcomes principle), §2.3 (concurrent callbacks),
  `docs/reference/schemas.md` §B.5/§B.6 (Assembly Procedure,
  Materialization Rule).

- **Developer prompt:**
```
[standing instructions]

Step 12-Prereq — Framework Protocol research spike. READ-ONLY: do NOT
install CrewAI, do NOT write engine code, do NOT modify any other module.

Goal: propose the minimal Framework Protocol that the engine will depend
on in Step 12a, informed by what CrewAI actually exposes today, so that
the FakeFramework used in 12a only assumes guarantees CrewAI can also
provide.

Deliver `docs/explanation/framework-protocol-design.md` (2-4 pages)
covering:

1. **What the engine needs from the framework.** Walk the engine's
   observable-event obligations (model.request, model.response,
   tool.called, tool.result, tool.failed, retry attempts) and the
   Materialization Rule (the assembled context must be captured verbatim
   at the moment of the model call). Translate each obligation into a
   hook or interception point the framework must support.

2. **What CrewAI actually exposes.** From public CrewAI documentation,
   release notes, and source on GitHub (read-only — do NOT pip install)
   determine:
   - Which lifecycle events exist (crewai.utilities.events.*) and
     whether they fire before-call or after-call.
   - Whether the LLM client is pluggable (litellm-based; BaseLLM
     subclass; callback injection — the three mechanisms named in the
     stop report).
   - Whether tool dispatch is observable before AND after the call.
   - Whether CrewAI mutates the message list before sending it to the
     LLM (template wrapping, role normalization, system-prompt
     manipulation). This is the Materialization Rule's main risk.
   - Whether callbacks fire from a single async context or multiple
     (informs whether the engine's per-thread serialization assumption
     holds).

3. **The proposed Framework Protocol.** A minimal Python Protocol the
   engine depends on. Each method documented with: what the engine
   needs, what CrewAI plausibly delivers, and any gap between the two.
   The Protocol must be MINIMAL — only what the engine strictly needs.
   Over-broad guarantees that CrewAI can't deliver are the failure mode
   to avoid.

4. **Open questions / verdict.** Are there obligations the engine has
   (per the contract) that NO mechanism in CrewAI can satisfy? If so,
   list them and propose: (a) a contract amendment, (b) an in-engine
   workaround that doesn't require framework support, or (c) escalation.

Do NOT install CrewAI. Do NOT write src/ code. Do NOT modify
pyproject.toml. The deliverable is purely the design note. Use web
research (CrewAI docs and GitHub) to gather facts.

Commit docs(engine): research Framework Protocol shape and report.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 12-Prereq — the Framework Protocol design note at
docs/explanation/framework-protocol-design.md.

Verify:
- The note covers ALL the engine's observable-event obligations from the
  agent-engine-contract (model.request/response, tool.called/result/
  failed, retries) and the Materialization Rule. Spot-check that no
  contract obligation is unmentioned.
- The CrewAI assessment cites specific docs/source (URLs to GitHub
  files, version refs). Claims about hook timing and message mutation
  must be supported, not asserted.
- The proposed Protocol is MINIMAL — only what the engine strictly
  needs. Anything broader is a red flag (it would mean FakeFramework
  could pass tests CrewAI's adapter cannot).
- Each Protocol method names the gap (if any) between what the engine
  needs and what CrewAI delivers. Gaps must have an explicit handling
  proposal (amendment / workaround / escalation).
- No code was installed or written — this was a read-only spike.

If the note identifies a contract obligation that no CrewAI mechanism
can satisfy AND proposes only "we'll figure it out later," FAIL —
escalate to human review before Step 12a begins.

Verdict: PASS or FAIL with specific defects citing file + spec section.
PASS means Step 12a may proceed against the proposed Protocol.
```

---

## Step 12a — Agent Engine against FakeFramework

- **Builds:** the agent engine — pre-flight (authz, memory bind,
  config.set, force-invoke, exchange_started, trigger materialization),
  setup (assembly + skills listing, tool registration, secrets), the
  observation/emission loop, the three error levels,
  `execute`/`execute_stream`, principal derivation — built against the
  `Framework` Protocol from the 12-Prereq spike, with a `FakeFramework`
  reference implementation used by all engine tests.
- **Depends on:** Step 12-Prereq (PASS), all prior steps.
- **Spec refs:** `docs/explanation/agent-engine-contract.md`,
  `docs/explanation/framework-protocol-design.md` (from 12-Prereq),
  `docs/reference/schemas.md` §B.5/§B.6,
  `docs/reference/module-api-reference.md` §11.
- **Test Catalog:** TC-ENG-01..11 (now framework-agnostic — run against
  any conforming `Framework` implementation; FakeFramework is the one
  used here).
- **Note:** This step builds and proves the engine's correctness in
  isolation. The Materialization Rule is verified against FakeFramework
  where observability is perfect; the question of whether CrewAI can
  honor it end-to-end is Step 12b's question.

- **Developer prompt:**
```
[standing instructions]

Step 12a — Agent Engine against FakeFramework. Implement
agent-engine-contract and module-api-reference §11 in src/engine/. Do
NOT install CrewAI in this step; the engine is built against the
`Framework` Protocol from docs/explanation/framework-protocol-design.md
(produced in Step 12-Prereq).

Deliver:
- The `Framework` Protocol exactly as designed in the prereq note
  (typing.Protocol). Place at src/engine/framework.py or similar.
- `FakeFramework` — a reference implementation of `Framework` used in
  all engine tests. It must give the engine the same guarantees the
  Protocol promises and nothing more (no extra observability that the
  Protocol doesn't surface). Place at src/engine/fake_framework.py.
  This is engine-internal, not a test fixture, because it's the v1
  default framework for engine tests and possibly for development
  scenarios where users want zero LLM cost.
- Pre-flight (§2.1, the 9 steps): config validation, capability check,
  authz allow(agent.execute.<id>), open session + compute leaf, memory
  bind_session, config.set emission when changed, force-invoke
  processing (synthetic read with principal=system), exchange_started,
  trigger materialization as user.message.
- Setup (§2.2): run the Assembly Procedure (schemas §B.5) to build
  model context, APPEND the skills listing (Skills §5.1), register
  tools (route through sandbox), register the engine's observers with
  the Framework, resolve secrets.
- Driver loop: hand control to the Framework via the Protocol; observe
  the events the Framework surfaces; for each, emit the corresponding
  platform event via store.append_event THEN bus.publish (best-effort).
- Three error levels: Diagnostic (tool.failed; exchange continues),
  Recoverable (retry per RuntimeSpec.max_retries), Fatal
  (thread.exchange_failed; sync execute raises AgentExecutionError;
  execute_stream ends).
- Principal derivation (§1.8): user.local top-level, agent.<id> for
  tool calls, system for synthetic.
- Serialize event construction per thread (R8) so the engine handles
  concurrent Framework callbacks correctly even when the Framework
  Protocol permits them.
- CRITICAL: honor the Materialization Rule. The assembled context the
  engine hands to the Framework must be captured verbatim in the
  emitted model.request event. The Fake LLM (running inside
  FakeFramework) records what it was asked; TC-ENG-03 asserts equality.

Write tests first from TC-ENG-01..11 — they run against FakeFramework
in this step. Treat them as framework-agnostic going forward (Step 12b
will re-run them against CrewAIFramework). Include golden sequences and
the Materialization check across 3 diverse scenarios; include
statelessness-across-restart (TC-ENG-08).

If during implementation you discover the `Framework` Protocol as
designed in 12-Prereq is insufficient or over-broad, STOP and report —
the Protocol may need amendment before continuing.

Acceptance: TC-ENG-01..11 pass against FakeFramework; success criteria
§10.5 met; golden files established. CrewAI is NOT installed in this
step.

Commit feat(engine): ... and report. State explicitly whether the
Framework Protocol as designed held up under implementation, or whether
amendments were needed.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 12a — Agent Engine against FakeFramework — against
agent-engine-contract, framework-protocol-design (from 12-Prereq),
schemas §B.5/§B.6, module-api-reference §11, and success criteria §10.5.

This is the gating step for engine correctness — review hardest here.
But note: the question being answered here is "is the engine correct,"
NOT "does CrewAI work." The latter is Step 12b.

Verify:
- The `Framework` Protocol matches the design note from 12-Prereq, OR
  the developer explicitly reported and justified amendments. Unjustified
  drift is a fail.
- FakeFramework assumes ONLY the guarantees the Protocol promises — no
  extra observability the Protocol doesn't surface (this would mean
  TC-ENG-03 passes against FakeFramework on cheats CrewAI cannot match).
  Inspect FakeFramework for "convenient" extras and reject them.
- Pre-flight runs ALL 9 steps in order; authz denial emits
  thread.exchange_failed and raises with NO model/tool events
  (TC-ENG-07).
- The skills listing is appended to the system prompt; force-invoke
  emits the synthetic read before the user message (TC-ENG-10/11).
- Every emitted event goes append_event THEN bus.publish; bus failure
  does not break execution.
- THE MATERIALIZATION CHECK (TC-ENG-03): the context the Fake LLM was
  asked to process equals what the events captured, byte-for-byte
  (modulo normalized fields), across 3 diverse scenarios. Run it. If it
  does not hold against FakeFramework, FAIL — the engine itself is
  wrong, regardless of any framework concern.
- Diagnostic errors do NOT end the exchange; Fatal errors do and
  sync-raise / stream-end correctly (TC-ENG-04/05/06).
- Statelessness: kill + rebuild + resume continues correctly; turn-2
  golden matches (TC-ENG-08).
- Per-thread event construction is serialized (no parent_event_id races
  under concurrent Framework callbacks) — inspect the locking.
- Golden files are committed and the diffs are sensible.
- CrewAI is NOT installed; pyproject.toml has not gained any LLM/
  framework dependencies.

Verdict: PASS or FAIL with specific defects citing file + spec section.
PASS means the engine is proven correct against the agreed Protocol; the
"does CrewAI work" question moves to Step 12b.
```

---

## Step 12b — CrewAIFramework adapter (conditional on 12a PASS)

- **Builds:** a `CrewAIFramework` implementing the `Framework` Protocol
  by wrapping real CrewAI, including the Fake-LLM injection mechanism
  (a `BaseLLM` subclass, a `litellm.completion` monkeypatch fixture, or
  the callback-based provider injection — chosen and justified). All 11
  TC-ENG cases re-run against `CrewAIFramework` to prove the adapter
  produces the same conformant event sequences. New CrewAI-specific
  tests (TC-CRW-*) pin the adapter's edge cases (CrewAI's hook timing,
  message-list pass-through, retry behavior).
- **Depends on:** Step 12a (PASS), Step 12-Prereq (PASS).
- **Conditional:** This step is **required for Phase 1 to ship**. The
  v1 production framework is CrewAI. Step 12a's FakeFramework is for
  engine testing and zero-cost development scenarios; it is NOT the v1
  deliverable. Phase 1 is not complete until 12b passes.
- **Spec refs:** `docs/explanation/agent-engine-contract.md` (the same
  contract — the engine's behavior is unchanged; only the framework
  underneath changes), `docs/explanation/framework-protocol-design.md`.
- **Test Catalog:** TC-ENG-01..11 re-run against `CrewAIFramework`;
  TC-CRW-* (new CrewAI-specific cases, authored in this step).

- **Developer prompt:**
```
[standing instructions]

Step 12b — CrewAIFramework adapter. Implement a `CrewAIFramework` that
satisfies the `Framework` Protocol by wrapping real CrewAI. This is the
v1 production framework; Phase 1 is not shippable without it.

Deliver:
- Add CrewAI to `[dependency-groups.test]` (NOT to runtime dependencies
  — the engine's contract remains framework-agnostic). Pin a specific
  CrewAI version; document the pin in a comment with a link to the
  release notes. Run `uv sync` and commit the lockfile update as a
  separate prep commit if you prefer, then the implementation commit.
- `CrewAIFramework` at src/engine/crewai_framework.py: implements the
  `Framework` Protocol by translating between the engine's hook
  expectations and CrewAI's actual lifecycle events. Where the
  Protocol's design note flagged gaps (12-Prereq), implement the
  agreed handling (workaround / amendment-now-applied / etc.).
- Fake-LLM injection: choose one of the three mechanisms named in the
  prior stop report (BaseLLM subclass, litellm monkeypatch, or callback
  injection) and implement it. Justify the choice in a docstring with
  the trade-offs considered. The chosen mechanism must let our existing
  tests/fixtures/fake_llm.py drive CrewAI without hitting any real
  provider.
- For test layer placement: `CrewAIFramework` tests live in
  tests/component/crewai_framework/ (component-level, no real LLM) and
  tests/integration/ (the full engine + CrewAIFramework + Fake LLM
  combinations).
- Re-run TC-ENG-01..11 with the engine wired to CrewAIFramework. They
  MUST pass with the same golden files. If a golden diff appears against
  CrewAIFramework that did not appear against FakeFramework, that is a
  finding — investigate before normalizing it away.
- New TC-CRW-* cases:
  - TC-CRW-01: CrewAI's lifecycle events fire in the order the Protocol
    requires (pre-call vs post-call observability where the Protocol
    demands it).
  - TC-CRW-02: CrewAIFramework's Fake-LLM injection captures the
    EXACT message list CrewAI sends to the LLM (this is the
    Materialization Rule's CrewAI-specific check — if CrewAI mutates the
    message list before sending, we need to know).
  - TC-CRW-03: Retry behavior. CrewAI's internal retry policy
    interacts with our `RuntimeSpec.max_retries`; document and test the
    composition.
  - TC-CRW-04: Concurrent tool callbacks (if CrewAI dispatches
    multiple tool calls in parallel) do not produce parent_event_id
    races at the engine layer.
  - Additional cases as the spike note (12-Prereq) flagged.

CRITICAL: if any TC-ENG case fails against CrewAIFramework that passed
against FakeFramework, do NOT silently adjust the test to "the
CrewAIFramework way." That is the Materialization Rule failing in
production. STOP and report it as a blocking finding — this is exactly
the gating risk the engine architecture was designed to detect.

Acceptance: TC-ENG-01..11 pass against CrewAIFramework (same golden
files as 12a); TC-CRW-* exist and pass; pyproject.toml runtime deps
still framework-agnostic.

Commit feat(engine): ... and report. State explicitly: (a) which
Fake-LLM injection mechanism was chosen and why, (b) whether any
TC-ENG golden differed between FakeFramework and CrewAIFramework, (c)
the discovered CrewAI version-specific quirks worth knowing.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 12b — CrewAIFramework against agent-engine-contract,
framework-protocol-design (12-Prereq), and the discoveries documented
during 12a. This is the architectural gate for Phase 1 — review hardest
here.

Verify:
- CrewAI is in [dependency-groups.test] only, NOT in runtime deps. The
  engine contract remains framework-agnostic; only tests pull CrewAI.
- The CrewAI version is pinned with a docstring justifying the pin.
- CrewAIFramework implements the Framework Protocol completely; no
  method is stubbed.
- TC-ENG-01..11 pass against CrewAIFramework using the SAME golden
  files as 12a. If any golden was modified for CrewAIFramework, that is
  a fail — the engine's correctness should be framework-independent. A
  Materialization failure manifesting as "let me adjust the golden" is
  the architectural risk realized; mark BLOCKING.
- THE CREWAI-SPECIFIC MATERIALIZATION CHECK (TC-CRW-02): the EXACT
  message list CrewAI sends to the LLM is captured byte-for-byte in the
  engine's model.request events. This is where CrewAI's potential
  pre-call mutation gets caught. Run it.
- Fake-LLM injection mechanism is justified, documented, and works
  cleanly. Spot-check that no test accidentally talks to a real provider.
- TC-CRW-01 (hook ordering), TC-CRW-03 (retry composition), TC-CRW-04
  (concurrent tool callbacks) exist and pass.
- Concurrent callbacks from CrewAI do not produce parent_event_id races
  — the engine's per-thread serialization (built in 12a) handles real
  CrewAI's actual concurrency model.
- The developer's report names: chosen injection mechanism, any
  golden diffs, version-specific quirks. Absence of these is suspicious
  — CrewAI in practice has them.

If TC-ENG-03 (Materialization) or TC-CRW-02 (CrewAI message-list
capture) does not pass cleanly, FAIL and mark BLOCKING. This is the
architectural gate; escalate to human review before Phase 1 proceeds.

Verdict: PASS or FAIL with specific defects citing file + spec section.
PASS means Phase 1's framework integration is real; Phase 1 may
proceed to Step 13.
```

---

## Step 13 — CLI + API surfaces

- **Builds:** the CLI (`run`, `server`, `reconstruct`, session listing)
  and the FastAPI surface exposing the same operations, plus the SSE
  streaming of `execute_stream`.
- **Depends on:** Step 12 (engine) and the store/assets.
- **Spec refs:** `docs/explanation/v1-scope.md` §2.4,
  `docs/explanation/agent-engine-contract.md` (execute surfaces).
- **Test Catalog:** author CLI/API cases (TC-CLI-*, TC-API-*) plus
  integration scenarios.

- **Developer prompt:**
```
[standing instructions]

Step 13 — CLI + API surfaces. Implement the v1 entry surfaces (v1-scope
§2.4) in src/ (e.g., src/cli/, src/api/).

Deliver:
- CLI: `pi-platform run <agent> [--input ...]` (single-shot and
  conversational), `pi-platform server` (start FastAPI), `pi-platform
  reconstruct` (store.reconstruct_from_filesystem), and session listing.
  The default tier (SQLite + in-process bus) must work with NO external
  services.
- FastAPI: endpoints to create/list sessions, run an agent
  (sync + streaming via SSE over execute_stream), fetch session events.
  Validate file_ref inputs against the path policy at the boundary.
- Both surfaces share the same engine/store calls — no logic duplicated.

Write TC-CLI-* and TC-API-* cases (component/integration) plus a check
that `pi-platform run` works end to end with the Fake LLM and no external
services.

Acceptance: surfaces work against the default tier with zero external
services; streaming yields events in order.

Commit feat(cli) / feat(api): ... and report.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 13 — CLI + API against v1-scope §2.4 and the engine surfaces.

Verify:
- `pi-platform run` works end to end with the Fake LLM and NO Postgres/
  Redis (default tier). This is the "great first-run experience" promise —
  test it on a clean default config.
- The API exposes session CRUD, sync run, streaming run (SSE over
  execute_stream yielding events in order), and event fetch.
- file_ref inputs are validated against the path policy at the boundary
  before the agent runs.
- CLI and API share engine/store calls; no duplicated orchestration logic.
- reconstruct command calls the store method correctly.
- TC-CLI-* / TC-API-* pass and assert meaningfully.

Verdict: PASS or FAIL with specific defects citing file + spec section.
```

---

## Step 14 — Integration wiring + E2E validation

- **Builds:** the end-to-end wiring and the integration/E2E test suites,
  including the four overall validation tests and the backend-swap
  transparency test.
- **Depends on:** all prior steps.
- **Spec refs:** `docs/process/test-plan.md` §12,
  `docs/process/test-catalog.md` Parts 12–13.
- **Test Catalog:** TC-INT-01..07, TC-E2E-01..04.

- **Developer prompt:**
```
[standing instructions]

Step 14 — Integration wiring + E2E validation. Wire the full platform and
implement the integration + E2E suites from test-catalog Parts 12–13.

Deliver:
- TC-INT-01..07: full single-agent exchange (Fake LLM + real CrewAI);
  events durable after return; memory write/retrieve across turns; authz
  gating a tool mid-exchange; error propagation across modules; resume-
  after-restart; and the backend-swap transparency test (run a scenario
  on SQLite+in_process AND on Postgres+redis via containers and assert
  identical event sequences).
- TC-E2E-01..04 (these use real LLM / real infra; gate them behind a
  marker so they do NOT run on every change):
  * E2E-01 Feasibility (GATING): real CrewAI honors the contract incl.
    Materialization over 3 diverse tasks.
  * E2E-02 Soundness: v1 vs baseline CrewAI on 5–10 tasks.
  * E2E-03 Robustness: real-use soak; zero corruption, recoverable
    crashes.
  * E2E-04 Default install needs no external services (clean machine,
    SQLite + in_process).
- Ensure the compose.test.yml gate runs unit+component+integration; E2E
  runs on the separate (nightly/pre-release) schedule.

Acceptance: TC-INT-01..07 pass in CI; TC-E2E-01..04 defined and runnable;
the coverage map (test-catalog §14) is satisfied (every §10 criterion has
a passing case).

Commit test(integration) / test(e2e): ... and report, including the
soundness comparison results if E2E-02 was run.
```

- **Reviewer prompt:**
```
[standing instructions]

Review Step 14 — Integration + E2E against test-plan §12 and test-catalog
Parts 12–14.

Verify:
- TC-INT-01..07 exist and pass in CI under the Fake LLM + real CrewAI.
  The backend-swap test (TC-INT-07) genuinely runs both tiers and asserts
  IDENTICAL event sequences — run it.
- Resume-after-restart (TC-INT-06) rebuilds leaf, config, and memory from
  the log alone.
- TC-E2E-01 (Feasibility, GATING) is defined and, when run, confirms
  Materialization + ordering on 3 diverse tasks. If it does not, mark
  BLOCKING.
- E2E tests are marked so they do NOT run on every change (token cost).
- The coverage map (§14): walk it — every §10 success criterion has at
  least one passing catalog case. List any criterion with no passing test.
- Overall: does the assembled platform honor the cross-cutting invariants
  (filesystem-as-truth, engine-sole-emitter, materialization)? Spot-check
  by reading a real session JSONL produced by an integration run.

Verdict: PASS or FAIL with specific defects citing file + spec/test
section. This is the final gate before Phase 1 is "done" — be thorough.
```

---

## After Step 14

- Phase 1 is functionally complete and validated. Update the docs
  (`docs/how-to/`, `docs/tutorials/`) with the guides the handbook
  flagged as deferred (build-your-first-agent, write-a-custom-tool,
  switch-to-postgres, etc.).
- Run the full E2E validation deliberately (Feasibility, Soundness,
  Robustness, Desirability) per the test plan before declaring v1
  shippable.
- Begin Phase 2 planning against the architecture roadmap's seams.

---

## Notes on running the loop

- **One step per branch.** The developer works a branch; the reviewer
  reviews the branch; merge only on PASS.
- **Feed reviewer findings back verbatim** to the developer for the fix
  pass; re-review until PASS.
- **The Materialization Rule is gated at two layers** after the Step 12
  split:
  - **Engine-layer gate (Step 12a, TC-ENG-03):** the engine's own
    correctness against the agreed `Framework` Protocol, verified with
    FakeFramework. Pass here means the engine is right.
  - **Adapter-layer gate (Step 12b, TC-CRW-02 + TC-E2E-01):** the
    CrewAI adapter's conformance — whether real CrewAI's hooks and
    message handling let us honor the Rule end to end. Pass here means
    Phase 1's framework integration is real.
  Either gate failing means the architecture (engine layer) or the
  adapter (CrewAI layer) needs rework before building further. Escalate
  to a human rather than working around it.
- **Step 12b is required for Phase 1 to ship.** FakeFramework is a
  test/dev fixture, not the v1 deliverable. A passing 12a with a failed
  or skipped 12b is not a complete Phase 1.
- **Keep the spec as the arbiter.** If the developer and reviewer
  disagree, the cited spec section settles it; if the spec is silent or
  contradictory, escalate rather than letting either agent decide
  unilaterally.
