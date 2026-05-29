# Phase 1 — The v1 Test Plan

**Draft 0.1**

> Two halves. **Per-module test plans** — what each module must
> demonstrate to be considered correctly implemented. **Overall
> validation tests** — what v1 as a whole must demonstrate to ship.
>
> Granularity is "behavior categories," not specific test cases.
> Implementers design the cases; this plan ensures the right ground
> is covered.

---

## Part 0 — Method

For each module, four categories of must-cover tests:

1. **Functional** — does the interface work as specified?
2. **Invariant** — do the contract's conformance invariants actually
   hold under exercise?
3. **Failure** — do specified error conditions produce specified errors?
4. **Integration** — does the module work correctly in combination
   with its dependencies and consumers?

Integration tests are deliberately not exhaustive at the module level —
the overall validation tests (Part 12) cover whole-system behavior.
Per-module integration covers specifically: does this module satisfy
the contracts it has with its immediate neighbors?

For the test plan to be useful, each category needs to be testable.
Where a behavior is hard to test (it happens inside a closed-source
framework, it requires LLM non-determinism, it requires real time
passing), the plan acknowledges this and proposes a proxy or
acceptance criterion.

---

## Part 1 — Kernel / Resolver

### Functional
- `validate_partial` accepts dicts matching the schema (with all
  fields optional) and returns `ok=True`.
- `validate_partial` rejects dicts with unknown fields, wrong types,
  malformed identifiers; returns structured errors.
- `validate_resolved` enforces required fields; rejects when missing.
- `resolve` correctly merges partials per RFC 7386: object
  deep-merge, scalar replacement, `null` deletes, omission inherits.
- `resolve_and_validate` runs the full three-layer pipeline; returns
  a validated `ResolvedAgentConfig`.
- `export_json_schema` produces parseable JSON Schema matching the
  Pydantic models.

### Invariant
- **I1 (purity)** — no I/O, no external state. Tests run without any
  infrastructure (no Postgres, no Redis, no filesystem).
- **I2 (RFC 7386 conformance)** — verifiable against the RFC's
  published test vectors.
- **I3 (dict-based merge)** — `resolve` operates on dicts; the
  `absent vs null` distinction is preserved through merge.
- **I4 (determinism)** — `resolve(partials)` produces byte-identical
  output for byte-identical input across multiple invocations.
- **I5 (three-layer ownership)** — Layer 3 validators are called for
  every subsystem in the resolved config; Layer 2 validators are
  called for every passed validator; Layer 1 is called twice (each
  partial + resolved).
- **I7 (error aggregation)** — when multiple validators fail,
  ALL errors are returned, not just the first.

### Failure
- Malformed JSON / non-dict input → structured error, not a Python
  exception escaping.
- Subsystem variant not registered → clear error naming the unknown
  variant.
- Schema version mismatch across partials → `SchemaVersionMismatch`.
- Validator function that itself raises → caught and converted to a
  `validator_internal_error`, other validators still run.

### Integration
- Variant validators supplied by subsystem modules work correctly
  through the dispatch mechanism.
- The resolved config that comes out is consumable by the Agent
  Engine without further parsing.

---

## Part 2 — Asset Management

### Functional
- Catalog build scans `bundles/`, reads each manifest, indexes assets.
- Bundle name must match directory name; mismatches rejected at build.
- Duplicate bundle names rejected.
- Asset refs in the catalog use full namespaced paths.
- Shorthand resolution: unambiguous shorthand → resolves to full ref;
  ambiguous → error listing candidates.
- PromptSpec pre-resolution: refs replaced with content; the resolved
  config carries content inline.
- Tool/skill refs pass through pre-resolution; the resolved config
  still has them as refs.
- Layer assembly produces the three-layer list correctly.
- Platform.yaml loaded at startup; sections exposed to subsystems via
  accessors.

### Invariant
- **Am-I1 (bundle directory = name)** — mismatch is a startup error.
- **Am-I2 (unique bundle names)** — duplicate bundle name is a
  startup error.
- **Am-I3 (reserved names)** — bundle named `core` rejected.
- **Am-I4 (pre-resolution of prompt content)** — no unresolved prompt
  refs reach the kernel resolver.
- **Am-I5 (tool/skill refs pass through)** — `config.set` events
  carry refs, not inlined tool/skill content.
- **Am-I6 (shorthand determinism)** — ambiguous shorthand never
  silently resolves to one.
- **Am-I9 (platform root is CWD)** — the platform's directories are
  resolved relative to current working directory at startup, not from
  environment variables or fixed paths.

### Failure
- Missing `bundle.yaml` in a bundle directory → bundle skipped with
  a warning, not a fatal startup error (gives users a chance to fix).
- Malformed `bundle.yaml` → bundle skipped with a warning.
- Asset reference to nonexistent asset → warning at catalog build,
  error at agent execute.
- `platform.yaml` missing → first-run UX generates one with defaults
  (per File Handling §4.1).

### Integration
- Asset catalog is consumable by the Tools subsystem (refs resolve to
  paths Tools can import).
- Asset catalog is consumable by the Skills subsystem (skill metadata
  has all fields per Skills §3.3).
- Layer assembly + Kernel resolve_and_validate produce a valid
  ResolvedAgentConfig.
- platform.yaml sections are correctly dispatched to subsystems
  (Authorization gets its rules; Secrets gets its path; etc.).

---

## Part 3 — Session/Run Store

### Functional
- `create_session` creates the JSONL file, allocates id, emits
  `session.opened`.
- `get_leaf` returns the most recent event in the thread (Phase 1
  semantics) or None for a fresh thread.
- `append_event` populates `parent_event_id` correctly; returns the
  appended event.
- `read_events` yields events in file (append) order.
- `read_path_to_leaf` yields events in chronological order (root
  first). In Phase 1, this is identical to `read_events` for
  single-thread sessions.
- `update_session` modifies the entity record without touching the
  log.
- `delete_session` removes both the entity record and the JSONL file.
- `write_content(content) → content_ref` writes oversized content to
  `content/<hash>.bin` and returns the reference.
- `reconstruct_from_filesystem` rebuilds entity records from JSONL
  files when Postgres is empty but files exist.

### Invariant
- **I1 (append-only)** — previously-written events cannot be
  modified, replaced, or deleted (except via session deletion).
- **I2 (lock-serialized appends)** — concurrent appenders for the
  same session are serialized; verifiable via stress test.
- **I3 (atomic-or-recoverable)** — kill the process mid-write; the
  next open recovers (truncates partial last line if necessary).
- **I4 (parent_event_id correctness)** — every event's
  `parent_event_id` points at the leaf at the moment of append.
- **I5 (leaf derivation)** — `get_leaf` is consistent with the
  on-disk state; not a stored value diverging from truth.
- **I6 (entity record consistency)** — entity record + JSONL file
  agree on existence.

### Failure
- Append to a session with no JSONL file → recovery or clean error.
- Read from a deleted session → `SessionNotFound`.
- Concurrent appends from the same process → serialized via lock.
- Concurrent appends from different processes → serialized via
  cross-process `flock`. Verifiable with two test processes.
- Corrupt JSONL line in the middle of a file → `store.corrupt_log`
  error with line number, not a silent error.

### Integration
- Engine appends events; subsequent reads see them.
- Memory provider can replay `memory.written` / `memory.deleted` from
  the store and reconstruct cache state correctly.
- The content store mechanism (`write_content`) integrates with the
  engine's "oversized event" detection (Tools spec §6.4).

---

## Part 4 — Event Bus

### Functional
- `publish` sends events to Redis pub/sub.
- `subscribe` registers a handler matched by topic pattern.
- Pattern matching: exact (`tool.failed`), prefix wildcard (`tool.*`),
  full wildcard (`*`).
- Attribute filters (`thread=th_abc`) work.
- `unsubscribe` removes the handler.
- Concurrent handler invocations don't block each other.

### Invariant
- **I1 (same vocabulary as the log)** — bus events are the same
  objects as log events. Test by publishing an event and verifying
  the subscriber receives the byte-identical object.
- **I2 (topic filtering before delivery)** — a subscriber to
  `tool.failed` never receives `thread.exchange_started`.
- **I4 (failure isolation)** — a handler that raises doesn't affect
  delivery to other subscribers or crash the bus.
- **I5 (best-effort delivery)** — under load (publishing fast, slow
  subscriber), some events may be dropped; this is acceptable and
  documented behavior.
- **I6 (no subscriber state)** — restarting the bus loses
  subscriptions; subscribers must re-register.

### Failure
- Redis unreachable → `publish` raises `bus.publish_failed`; engine's
  retry/continue behavior is the engine's choice (publish failures
  must not break agent execution).
- Handler raises exception → caught and logged; subscriber remains
  registered.
- Slow subscriber under fast publisher → events dropped silently
  (per Redis pub/sub behavior); subscriber must not block the bus.

### Integration
- Engine publishes after `append_event`; subscriber sees the event.
- Strict subscriber pattern: subscriber tails the log via Session/Run
  Store with bookmark, uses bus only as wakeup signal.

---

## Part 5 — Agent Engine

This is the most complex module to test, partly because most behavior
happens inside CrewAI (which we don't control).

### Functional
- `execute_stream` yields events as they occur during an exchange.
- `execute` returns the final `agent.message` event.
- Cancellation via `CancelToken` cleanly emits
  `thread.exchange_failed` with cancelled cause.
- Multiple sequential `execute` calls on the same thread continue the
  conversation (stateless across calls per I8).
- `capabilities()` returns the engine's actual capabilities.

### Invariant
- **I1 (sole emission)** — only the engine writes to the session log
  for an exchange; verifiable by snooping log file writes from other
  modules during a test exchange.
- **I3 (Materialization Rule)** — for every event involving model
  context, the relevant content is in the event verbatim. This is
  the hardest invariant to test; see Part 12.
- **I4 (Assembly Procedure determinism)** — same tree path + same
  assembly version → byte-identical context. Verifiable by running
  the procedure twice on a captured tree state.
- **I6 (event order)** — for a successful exchange:
  `thread.exchange_started` first, `agent.message` immediately
  before `thread.exchange_completed`, `model.request` before
  matching `model.response`, `tool.called` before matching
  `tool.result`/`tool.failed`.
- **I7 (failure surface)** — Diagnostic-level events do not raise;
  Fatal-level events emit `thread.exchange_failed` and (sync) raise
  / (streaming) end.
- **I8 (statelessness across calls)** — kill the engine between
  calls; reconstruct from log; second call works.

### Failure
- All retries exhausted → `model.retry_exhausted` (Fatal); exchange
  ends.
- Cancellation mid-execution → exchange ends cleanly with cancelled
  cause.
- Authorization denied at setup → `agent.unauthorized` (Fatal); no
  events emitted beyond the failed-exchange events.
- Tool throws unexpected exception → wrapped as
  `tool.execution_failed` (Diagnostic); exchange continues.
- A required asset (tool, skill, prompt file) missing → engine setup
  fails with a clear error.

### Integration
- The engine integrated with Session/Run Store: every emitted event
  is on disk after the engine returns.
- The engine integrated with Memory: bind_session is called at
  setup; retrievals during execution surface `memory.retrieved`
  events.
- The engine integrated with Tools: tool calls produce
  `tool.called`/`tool.result`/`tool.failed` events.
- The engine integrated with the framework (CrewAI): exchanges
  produce model.request/response pairs in the log per the engine's
  observation.

### Testing Materialization (Invariant I3)

Genuinely hard, deserving special attention. The test:

1. Run an exchange that involves: a user.message, memory retrieval, a
   tool call producing a result, an agent.message. Capture the
   resulting events.
2. Reconstruct the model context by running the Assembly Procedure
   on the path.
3. Compare to what the model actually received during execution
   (requires capturing CrewAI's input to the LLM call — feasibility
   depends on CrewAI hooks).
4. They must be byte-identical.

This test is the most important and the most fragile. If CrewAI's
hooks don't expose enough information, the Materialization invariant
is unverifiable in v1, and we accept that limitation honestly.

---

## Part 6 — Memory Provider

### Functional
- `bind_session(session_id, agent_id, config)` returns a
  MemoryContext.
- `MemoryContext.write(content)` returns a MemoryItem with
  `write_tag` applied; rejects with `memory.write_disabled` if no
  write_tag configured.
- `MemoryContext.retrieve(query)` returns memory items + recent
  events blended.
- `MemoryContext.list(filters)` returns items matching filters
  without ranking.
- `MemoryContext.delete(item_id)` removes the item.
- `MemoryContext.compact(events, target_tokens)` produces a summary.

### Invariant
- **I1 (no direct event emission)** — Memory operations return
  results; the Engine emits events. Verifiable by checking that
  Memory's code paths don't import the Session/Run Store's
  event-emission methods.
- **I3 (cache derived from log)** — kill the platform; restart;
  rebuild cache from `memory.written`/`memory.deleted` events;
  retrievals produce identical results.
- **I4 (tag enforcement)** — writes are tagged with the bound
  write_tag; user cannot override at write time.
- **I5 (wildcard restriction on write)** — config with `write_tag:
  "research/*"` is rejected at validation.
- **I7 (session scope)** — items from session A never appear in
  session B's retrievals.

### Failure
- Write with no `write_tag` configured → `memory.write_disabled`.
- Delete an unknown item id → `memory.item_not_found`.
- Compact when underlying model call fails → `memory.compaction_failed`.

### Integration
- After engine emits `memory.written` event, the cache reflects the
  write.
- Across session resume (kill platform, restart, reopen session),
  cache rebuilds from log and is consistent.

---

## Part 7 — Secrets / Sandbox / Authorization Providers

Combined test plan because the modules are small.

### Secrets Functional + Invariant
- `dotenv` variant: parses `.env` once at startup.
- `resolve` returns value; `exists` returns boolean; `list_names`
  returns keys.
- Missing secret → `secret.not_found`.
- **S-I1 (secrets never in events)** — `resolve` does not log the
  value; verifiable by capturing logs.
- **S-I2 (pure resolution)** — `resolve` makes no network calls.

### Sandbox Functional + Invariant
- `native` variant: `execute(work, policy)` returns work's result.
- Tool execution actually runs (the passthrough doesn't accidentally
  swallow the call).
- **Sb-I1 (result transparency)** — result returned unchanged.
- **Sb-I2 (error transparency)** — exception from work re-raises.

### Authorization Functional + Invariant
- `allow(principal, action)` returns AuthorizationResult.
- Default rule (allow .* / .*) returns allowed=True for any input.
- Specific deny rule blocks the matching action.
- First-match-wins: a deny placed before a general allow correctly
  blocks despite the allow.
- Default-deny: no matching rules → allowed=False.
- **A-I1 (fullmatch)** — `tool.b` regex does NOT match `tool.bash`.
- **A-I3 (default deny)** — empty rule list → deny.
- **A-I5 (result transparency)** — matched rule is returned.

### Failure
- Malformed regex in a rule → `authz.regex_invalid` at startup.
- Auth provider unavailable → caller fails-closed (denies).

### Integration
- Engine calls auth before execute; deny propagates as
  `agent.unauthorized`.
- Tools subsystem calls auth before tool invocation; deny surfaces
  as `tool.unauthorized` Diagnostic event.
- Memory operations under restrictive rule set are gated.

---

## Part 8 — Tools Subsystem

### Functional
- `@tool` decorator with full arguments parses correctly; the tool
  is registered in the catalog.
- Tools with no args declaration work.
- Pydantic args validation runs before tool invocation; bad args
  produce `tool.invalid_arguments`.
- Tool functions can return strings, dicts, or other JSON-serializable
  values.
- Async tool functions work.
- Sync tool functions wrapped at registration also work.
- MCP server tools registered and proxiable.
- Lazy MCP servers start on first use; eager start at setup.

### Invariant
- **T-I1 (tool action namespace)** — every tool has an action name
  `tool.<name>` or `mcp.<server>.<tool>`.
- **T-I3 (secrets in ToolContext only)** — tools cannot access
  `secrets.resolve()` directly; verifiable by checking imports.
- **T-I4 (sandbox routing)** — every invocation goes through
  Sandbox; tests check the sandbox.execute call site.
- **T-I5 (verbatim result capture)** — `tool.result.data.result`
  matches the tool's return value byte-for-byte (for non-oversized
  results) or is correctly stored in content store (for oversized).

### Failure
- Tool with `secrets_needed` for a missing secret → registration
  fails (`tool.setup_failed`) with the underlying `secret.not_found`.
- Authorization denies tool → `tool.unauthorized` Diagnostic event,
  exchange continues.
- Tool function raises → `tool.execution_failed` Diagnostic event.
- Tool timeout → `tool.timeout` Diagnostic event.
- MCP server crashes → `mcp.server_unreachable` for subsequent calls;
  lazy server retries on next invocation.

### Integration
- Tools work end-to-end through the engine: model decides to call,
  engine dispatches, tool runs, result returns.
- Built-in tools (read/write/edit/bash) work; path-policy respected
  for the first three; bash exempt.
- Secrets flow from `.env` through Secrets to ToolContext correctly.

---

## Part 9 — Skills Subsystem

### Functional
- Skill discovery: scan `bundles/<bundle>/skills/<skill>/SKILL.md`,
  parse frontmatter, register.
- Skill descriptions in system prompt at agent setup.
- Skills with `disable-model-invocation: true` excluded from prompt.
- Force-invoke produces a synthetic `tool.called`/`tool.result` pair
  for `read` on the skill's path.

### Invariant
- **Sk-I1 (progressive disclosure)** — skill bodies are NOT in the
  system prompt at setup; only descriptions.
- **Sk-I2 (disable-model-invocation enforcement)** — disabled
  skills' descriptions not in prompt; still force-invocable.
- **Sk-I3 (`allowed-tools` informational)** — `allowed-tools`
  frontmatter parsed but tool surface unchanged.
- **Sk-I4 (force invoke as `tool.read`)** — auth checked against
  `tool.read`, not a skill-specific action.
- **Sk-I6 (enablement is explicit)** — skills NOT in enabled list
  are invisible to the agent.

### Failure
- Skill ref in agent config doesn't exist → validation error at
  config resolution.
- SKILL.md missing required frontmatter → `skill.frontmatter_invalid`.
- Force-invoke of a non-enabled skill → `skill.forced_not_enabled`.

### Integration
- The model can choose to load a skill via `read`; the body appears
  as `tool.result` in the log; the body is included in the next
  turn's assembled context (via Assembly Procedure).
- Force-invoke works end-to-end: trigger includes
  `force_skills: ["..."]`; the `read` is emitted; the body is in
  context for the user message.

---

## Part 10 — Error Model

### Functional
- `PlatformError` subclasses serialize via `to_dict()` to the
  canonical envelope.
- `WrappedError.from_exception(exc)` correctly wraps a non-platform
  exception.
- `__cause__` chain serializes as recursive `cause` field.

### Invariant
- **E-I1 (canonical shape)** — every error appearing in an event or
  API response matches the envelope.
- **E-I2 (registered types only)** — `type` values match the catalog.
- **E-I3 (level intrinsic)** — same error type always has same level.
- **E-I4 (cause chain preservation)** — wrapping never loses the
  original.
- **E-I8 (Diagnostic doesn't raise)** — a Diagnostic-level error
  becomes an event; the exchange continues.
- **E-I9 (Fatal terminates)** — a Fatal-level error ends the
  exchange and (sync) raises.

### Integration
- An error originating in Memory (e.g., compact failure) propagates
  to the engine, becomes an event of the correct level.
- The same error type appears with the same `type` string regardless
  of where it's emitted from.

---

## Part 11 — File Handling

### Functional
- Path policy correctly evaluates allow/deny lists.
- Denies always win.
- Default deny when neither list matches.
- `read` / `write` / `edit` reject denied paths with
  `file_access.denied`.
- `bash` runs anything (v1's documented limitation).

### Invariant
- **Fh-I2 (path policy enforcement)** — builtin tools refuse to
  operate outside policy.
- **Fh-I3 (denies override)** — adding `.env` to allowed_paths
  doesn't grant access (denies still win).
- **Fh-I4 (path normalization)** — symlink pointing to `.env`
  is denied.
- **Fh-I5 (tools never write to `content/`)** — verifiable by code
  inspection.
- **Fh-I10 (no automatic archival)** — referenced files in `data/`
  are not copied anywhere.

### Failure
- Tool attempts to read denied path → `file_access.denied`
  Diagnostic event.
- File at path was modified since the session referenced it → replay
  sees current content; no automatic fidelity guarantee (per design).

### Integration
- Path policy loaded from `platform.yaml` correctly; takes effect
  for all tool invocations.

---

## Part 12 — Overall Validation Tests

What v1 as a whole must demonstrate to be considered shippable. These
test the system, not the modules. Each one is genuinely
go/no-go for v1.

### Test 1 — Feasibility: CrewAI Honors the Engine Contract

**Hypothesis:** CrewAI's lifecycle hooks expose enough information
for the Agent Engine to honor its conformance invariants — in
particular, the Materialization Rule (I3) and event ordering (I6).

**Method:** Implement the engine wrapper around CrewAI. Run a real
exchange involving model calls, tool calls, and a memory retrieval.
Capture all events the engine emits and the LLM inputs CrewAI sent.
Verify:

- Every event the engine should emit per its contract is actually
  emitted, with verbatim content.
- The Assembly Procedure run on the captured event tree produces a
  context byte-identical to what CrewAI sent to the model.
- Event ordering matches I6.

**Pass criterion:** All three checks pass for at least three diverse
exchanges (different tool sequences, varying complexity). If CrewAI
hooks insufficient for any one, document the limitation and accept
or pivot.

**No-pass implications:** If feasibility fails, the engine wrapper
strategy needs rework — wrap a different framework, or write more
of the agent loop ourselves. This is the test that validates the
"engine as recorder, framework as driver" architecture.

### Test 2 — Soundness: The Architecture Produces Honest Output

**Hypothesis:** A v1 agent using the platform's mechanisms (typed
I/O, tagged memory, skills, sandboxed-ish tool use) produces *better*
outputs than the naive baseline (CrewAI without our scaffolding) for
a representative class of tasks.

**Method:** Define 5-10 representative agent tasks. Build each as
both (a) a v1 platform agent with our subsystems and (b) a baseline
CrewAI agent. Run each on identical inputs. Have humans (or an
LLM-as-judge) compare outputs.

**Pass criterion:** v1 outputs are subjectively better on a majority
of tasks. "Better" includes: more grounded, fewer hallucinations,
better tool use, better recovery from errors, better context
management.

**No-pass implications:** If the architecture doesn't visibly help,
v1's value proposition is suspect. Reconsider whether the abstraction
overhead is worth it.

### Test 3 — Robustness: The Platform Survives Real Use

**Hypothesis:** A v1 platform installation runs reliably under the
kinds of usage a single developer would put it through over weeks.

**Method:** Run the platform in real use (the conversation's drafter
uses it; one or two early users use it). Track:

- Crashes, hangs, data corruption.
- Session resumption correctness across days.
- Memory leaks during long sessions.
- Filesystem-state coherence (lock files, partial writes, missing
  sessions).

**Pass criterion:** Zero data corruption. Crashes are recoverable
(restart and resume work). Long sessions don't degrade.

**No-pass implications:** v1 isn't shippable. Fix the failure modes
before considering ready.

### Test 4 — Desirability: Real Users Return Unprompted

**Hypothesis:** The platform's value proposition is real enough that
early users come back to it on their own.

**Method:** Three real users (not the drafter; people with their own
agent-development needs). Give them v1, brief introduction, then no
nudging. Track:

- Do they use it more than once unprompted?
- Do they describe it favorably to peers?
- Do they request features that suggest deeper engagement?
- Do they stick with it after the first week?

**Pass criterion:** All three return unprompted at least once after
the initial session.

**No-pass implications:** The product isn't ready. May not be a v1
defect — could be value proposition, could be onboarding, could be
target user mismatch. Investigate the specific failure mode.

---

## Part 13 — Test Plan Status

For each module: ✓ Functional, Invariant, Failure, Integration
covered.

For overall validation: four tests, in order:

1. Feasibility (CrewAI viability) — must pass FIRST. Failure means
   architectural rework before continuing.
2. Soundness (architecture value) — must pass to justify v1.
3. Robustness (real use) — must pass to ship.
4. Desirability (users return) — must pass to be considered
   successful, not just shipped.

The four are ordered from "system can be built" to "system is worth
building" to "system stays running" to "system is wanted."

A v1 that passes 1 and 3 but fails 2 is technically shippable but
strategically wrong — we built a thing that works but doesn't help.
A v1 that passes 1 and 2 but fails 3 is strategically right but
operationally not ready — fix the robustness and try again.

---

## Part 14 — Open Items

1. **Test data sets.** Where do the soundness-test tasks come from?
   Need a defined set of 5-10 representative agent tasks. Could be
   drawn from the AGENT-BENCH style benchmarks or hand-crafted for
   the platform's intended use cases.

2. **LLM-as-judge for soundness.** Using an LLM to judge output
   quality has known limitations. Worth pairing with human evaluation
   for at least a sample.

3. **The Materialization test (Part 5) feasibility.** Verifying that
   the Assembly Procedure produces byte-identical output to what
   CrewAI sent requires capturing CrewAI's LLM input. If CrewAI's
   hooks don't expose this directly, a wrapper or interception layer
   may be needed for testing purposes.

4. **Robustness duration.** "A week of real use" is the target, but
   how long is enough? Worth defining a concrete duration (4 weeks?
   100 sessions?) for go/no-go on Test 3.

5. **Desirability with three users.** Three is a small N. Real
   confidence requires more. v1 ships with three; growing user count
   is a Phase 2 concern.

6. **CI / automated test infrastructure.** This plan describes WHAT
   to test, not HOW. The test infrastructure (pytest, fixtures,
   integration test harness) is implementation-level and not
   specified here. Probably worth a small companion document when
   implementation begins.

7. **Tests for the audit-corrected specs.** This plan was written
   against the audit-corrected versions of the specs (per the
   contract audit). The actual spec files need to be revised before
   the tests are implementable. Pending follow-up work.
