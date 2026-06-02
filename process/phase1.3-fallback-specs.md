# Phase 1.3 Patch — Model Fallback Chains

**Draft 0.1**

> A reliability patch giving agents a way to declare a backup model
> when their primary one fails recoverably (rate-limit, transient
> 5xx, timeout). Phase 1.1 specified one model per agent; if the
> provider blinks, the exchange fails and the user retries by hand.
> Phase 1.3 lets the agent definition say "use opus; if it's
> unavailable, try haiku; failing that, gpt4o" — and the engine
> walks the list.
>
> This patch specifies: (1) the new `fallback_models` field on
> agent configs and the `defaults.fallback_models` knob in
> `platform.yaml`, (2) the engine's fallback loop (which errors
> trigger it, which don't), (3) the new `model.fallback` event so
> replay shows which model actually served the exchange, (4) the
> `llm.all_models_failed` Fatal that lands when the entire chain
> is exhausted, and (5) the deliberate "no mid-stream fallback in
> v1.3" boundary.

---

## Part 0 — Why This Patch Exists

Phase 1.1 made agents callable against real providers; Phase 1.2
made the first run frictionless. The remaining first-class
reliability gap: a single transient provider hiccup kills the
exchange entirely. The platform's contract with the user is
"events are ground truth" — a `thread.exchange_failed` *is* the
truth — but the user still re-issued the same prompt and got the
same answer two seconds later from a different model. The retry
logic was theirs to write.

Missing today:

- A declarative way to say "if Opus is rate-limited, try Haiku."
- A way to record *which* model actually served a successful
  exchange (the event log shows the request and the response, but
  not which fallback link in the chain produced them).
- A clean line between "transient — try the next model" and
  "fatal — stop entirely" (e.g., a bad API key shouldn't trigger
  cascading attempts against every model in the chain).

This patch fills the gap. Headline design decisions:

1. **An ordered list, not a strategy.** `fallback_models` is a
   plain list. The engine walks it in order. No weighting, no
   cost-aware reordering, no health-tracking memory across
   exchanges. Simpler, predictable, replayable.
2. **One classification, baked in.** Recoverable `model.*` errors
   trigger fallback; everything else doesn't. The classification
   lives in the engine's error-handling code, not in user config.
   If a user needs a different policy, they file the next patch
   with concrete examples — speculative config is worse than no
   config.
3. **Fresh state per exchange.** No "sticky" fallback that
   remembers the primary was down last time. Each new exchange
   starts from the primary. Memory of provider health belongs in
   Phase 3's hosted concerns, not in the agent contract.
4. **Pre-stream only in v1.3.** Fallback applies when the model
   call fails before any `model.response` event has been emitted.
   Mid-stream failure (token-by-token then a 5xx) is a separate
   beast — partial output was already committed to the log, the
   user already saw it, and "fall back and replay" is a UX
   decision, not a transport-level retry. Phase 2 or later.

---

## Part 1 — The `fallback_models` Field

### 1.1 Agent-level

A new optional field on every agent YAML. Lives next to `model:`.

```yaml
# bundles/acme/agents/researcher.yaml
schema_version: "0.1"
assembly_version: "1"
agent_id: ag_researcher
name: researcher
model: opus
fallback_models:
  - haiku
  - gpt4o
prompt: …
io: { mode: conversational }
```

Each entry accepts the same three input shapes `model:` accepts
(per Phase 1.1 §2.4):

- Object: `{provider: openrouter, model: anthropic/claude-haiku-4-5}`
- Compact string: `"openrouter/anthropic/claude-haiku-4-5"`
- Alias string: `"haiku"`

All are normalized to canonical `{provider, model}` during Layer 1
validation. Duplicates within the chain (or with the primary) are
allowed and emit a `config.fallback_duplicate` warning (not an
error — the user may have done this on purpose to retry the same
model). The warning surfaces at `kolsch run` startup, not silently
in logs.

An empty `fallback_models: []` is equivalent to omitting the field.

### 1.2 Platform-level default

In `platform.yaml`'s existing `defaults:` block:

```yaml
defaults:
  fallback_models:
    - haiku
```

When an agent omits `fallback_models`, the platform default applies.
When the agent specifies the field (even as `[]`), the agent value
wins — there is no merging. This is the same precedence rule as
every other agent/platform default in Kolsch.

### 1.3 Validation rules

- Each entry must resolve to a canonical `ModelSpec`. Unknown
  aliases raise `config.unknown_alias` (existing code).
- Each entry's `provider` must appear in `llm_providers` at validation
  time. A fallback to an unconnected provider raises
  `llm.provider_not_configured` at agent-load time, not at fallback
  time — fail fast, with a clear message naming which entry in the
  chain is the problem.
- The list length is unbounded by spec. (Practically, a chain of 5+
  models indicates a different problem; we don't artificially cap.)

---

## Part 2 — The Engine's Fallback Loop

### 2.1 Where it lives

In the Agent Engine pre-flight, extending step 5.5 from Phase 1.1.
The revised step:

> **5.5. Acquire LLM client for the primary model.** Call
> `build_llm(config.model, …)`. Hold the result for the exchange.

> **5.6. Build the fallback chain.** Resolve each entry in
> `config.fallback_models` (or `defaults.fallback_models` if the
> agent omitted the field). Store as an ordered list of resolved
> `ModelSpec`s — do NOT pre-build their LLM clients (they may
> never be needed, and credentials could fail per provider).

The model call itself, at exchange execution time, becomes a loop
around steps 5.5+5.6:

```
attempt 0: call primary
  on Recoverable model.* error:
    emit model.fallback {from=primary, to=fallback[0], reason}
    build_llm(fallback[0])
    call fallback[0]
    on Recoverable: emit model.fallback {from=fallback[0], to=fallback[1], ...}
    …
  on Fatal model.* error:
    abort — fallback does not apply
  on success:
    emit model.response as today; exchange continues
```

### 2.2 Which errors trigger fallback

Triggers fallback (Recoverable):

| Error | Why fallback helps |
|---|---|
| `model.rate_limited` (HTTP 429) | Another provider/model isn't rate-limited. |
| `model.server_error` (HTTP 5xx) | Transient infrastructure. |
| `model.timeout` | Provider unresponsive; another may answer. |
| `model.context_overflow` | A different model may have larger context. |

Does NOT trigger fallback (Fatal):

| Error | Why fallback won't help |
|---|---|
| `model.authentication_failed` (HTTP 401/403) | Same key used everywhere; chain would cascade-fail. |
| `model.bad_request` (HTTP 400, malformed payload) | The request itself is broken. |
| `llm.provider_not_configured` | Config gap; chain entries against same gap would fail identically. |
| `llm.client_init_failed` | Setup-time failure; the model never made the call. |
| Any non-`model.*` error | Out of scope — fallback is about model availability, not engine bugs. |

The mapping lives in `src/engine/_fallback.py` as a single
`is_fallback_triggering(err) -> bool` predicate, exhaustively
documented. New error codes added later must explicitly decide
which side they fall on.

### 2.3 Streaming

When the agent is invoked with streaming (`--stream` or HTTP SSE):

- **Before the first token** (HTTP connection failed, connection
  established but server returned 5xx before any data, request
  timed out before first byte): fallback applies, same as
  non-streaming.
- **After the first token** (provider streamed some response then
  errored mid-flight): fallback does NOT apply. The engine emits
  `thread.exchange_failed` with the partial output captured in the
  `model.response` event's data, and the user sees the truncation
  in their terminal. The exchange ends.

The boundary is the first `model.response` content event. Until it
fires, we can swap models cleanly. After it fires, partial state is
out in the world.

### 2.4 No retries within a single model

The engine does not retry the *same* model before falling back. If
Opus 429s, the very next attempt is Haiku, not Opus-again. Rationale:
the connector (or `crewai.LLM` itself) is the right layer for
intra-model retries — exponential backoff against the same endpoint
is connector concern. Fallback is for *different* endpoints.

If a user wants "retry Opus twice before falling back to Haiku,"
they put Opus in the chain twice. This is a feature of the patch,
not a bug — it's why `config.fallback_duplicate` is a warning, not
an error.

---

## Part 3 — The `model.fallback` Event

A new event type emitted once per fallback transition. Recorded on
the thread's JSONL like every other event.

### 3.1 Schema

```yaml
type: model.fallback
data:
  from:
    provider: openrouter
    model: anthropic/claude-opus-4-7
  to:
    provider: openrouter
    model: anthropic/claude-haiku-4-5
  reason: "model.rate_limited"
  reason_detail: "HTTP 429 from openrouter; retry-after: 30s"
  attempt: 1               # 1-indexed; attempt 0 was the primary
```

### 3.2 Where in the event sequence

Between the failed `model.response` (or its absence — if the call
errored before any response event, only the `model.request` fires)
and the next `model.request`. For a successful fallback to
secondary:

```
thread.exchange_started
user.message
model.request          (primary: opus)
model.fallback         (from: opus, to: haiku, reason: rate_limited)
model.request          (fallback: haiku)
model.response         (success)
agent.message
thread.exchange_completed
```

For an exhausted chain:

```
thread.exchange_started
user.message
model.request          (opus) → 429
model.fallback         (opus → haiku, rate_limited)
model.request          (haiku) → 500
model.fallback         (haiku → gpt4o, server_error)
model.request          (gpt4o) → timeout
thread.exchange_failed (cause: llm.all_models_failed)
```

### 3.3 Replay

`kolsch reconstruct` rebuilds entities from JSONL. The reconstructed
thread carries the chain of `model.fallback` events untouched. The
final agent message (when successful) records which model produced
it via the existing `model.response.data.model` field; the
`model.fallback` events alone make the chain visible in `kolsch
sessions events`.

Golden tests update to include the new event type in fallback
scenarios.

---

## Part 4 — Failure Modes

New error codes. Adds to `docs/explanation/error-model.md`.

| Code | Level | Detail | Where |
|---|---|---|---|
| `llm.all_models_failed` | Fatal | `{primary, attempts: [{model, error_code, detail}], reason}` | engine model loop, after exhausting the chain |
| `config.fallback_duplicate` | Warning | `{agent, model, positions}` | Layer 1 validator at agent load |
| `model.rate_limited` | Recoverable | `{provider, model, retry_after_seconds?}` | connector's call layer |
| `model.server_error` | Recoverable | `{provider, model, status_code}` | connector's call layer |
| `model.timeout` | Recoverable | `{provider, model, timeout_seconds}` | connector's call layer |
| `model.context_overflow` | Recoverable | `{provider, model, requested_tokens, max_tokens?}` | connector's call layer |
| `model.authentication_failed` | Fatal | `{provider, model, status_code}` | connector's call layer |
| `model.bad_request` | Fatal | `{provider, model, status_code, reason}` | connector's call layer |

The `model.*` codes 3–8 above are NOT all new — some existed in
Phase 1.1 but as generic "the model call failed" without level
distinctions. Phase 1.3 forces each to declare Recoverable vs
Fatal, which is the basis of the fallback decision in §2.2.

`llm.all_models_failed`'s `attempts` field is the full ordered list
of (model, error) pairs the engine tried. The cause chain on the
emitted `thread.exchange_failed` event references this for replay.

---

## Part 5 — Required Spec Revisions

Mechanical edits to existing docs. Apply as a single revision pass.

### FB-R1. `docs/reference/schemas.md` §A

Add `fallback_models: list[ModelSpec input]` to the agent config
schema, optional, default `[]`. Document the three input shapes
inherit from `ModelSpec`.

### FB-R2. `docs/explanation/asset-management-contract.md` §3.2

Add `defaults.fallback_models: list[ModelSpec input]` to the
`platform.yaml` schema. Document the precedence rule (agent value
wins; absent ⇒ platform default; both absent ⇒ no fallback).

### FB-R3. `docs/explanation/agent-engine-contract.md` §2.1

Update step 5.5 to read "Acquire LLM client for the primary model"
and insert step 5.6 "Build the fallback chain" (resolution only;
clients built lazily). Update the model-call section of the
exchange execution to describe the loop in patch §2.1.

### FB-R4. `docs/explanation/agent-engine-contract.md` §3 (events)

Add the `model.fallback` event type to the engine's emitted-events
list. Specify ordering: between the failing `model.response`/
`model.request` and the next attempt's `model.request`.

### FB-R5. `docs/explanation/error-model.md` Part 4

Add §4.15 "Model Fallback" with the eight codes from patch Part 4.
For the six `model.*` codes, declare Recoverable vs Fatal
explicitly and cross-reference §2.2 of this patch (which becomes
the canonical "does this trigger fallback" table).

### FB-R6. `docs/reference/module-api-reference.md`

Add Part 11.7 "Model Fallback" covering: the `fallback_models`
field on agent configs, the platform default, the engine's loop,
the `is_fallback_triggering` predicate, and the `model.fallback`
event. Update the dependency matrix (Part 12) to note that the
engine consumes the LLM registry multiple times per exchange in
fallback scenarios.

### FB-R7. `process/development-testing-handbook.md` §10

Add success criteria §10.14 "Model Fallback":

- The engine retries the next model in the chain on Recoverable
  `model.*` errors and stops on Fatal ones.
- Exactly one `model.fallback` event per transition; ordering
  matches §3.2.
- `llm.all_models_failed` includes every attempted model in its
  `attempts` field.
- Agent-level `fallback_models` overrides
  `defaults.fallback_models`; absence of both means no fallback.
- A fallback to an unconfigured provider fails at agent load with
  a clear message — not at first attempt-time.
- Streaming exchanges do not fall back after the first content
  event.

### FB-R8. `process/test-catalog.md`

Add Part 9.7 "Model Fallback (FB)" with cases TC-FB-01..10 per
patch Part 6 below.

### FB-R9. `docs/explanation/v1-scope.md`

Add `fallback_models` to the agent surface in §2.3, with a
sentence noting the no-mid-stream-fallback boundary so the scope
is explicit.

### FB-R10. `AGENTS.md`

Add to "Hard rules":

> Every new `model.*` error code MUST declare Recoverable or Fatal
> in the same change that introduces it, and update the fallback
> trigger table in `process/phase1.3-fallback-specs.md`
> §2.2 (or its successor in the error model doc).

### FB-R11. `docs/tutorials/first-agent.md`

Add a short "10. Add a fallback model" section after step 9,
showing the user how to edit `bundles/starter/agents/helper.yaml`
to add `fallback_models: [haiku]` and demonstrating that with
network simulated down to Opus the agent still responds.

### FB-R12. New doc: `docs/how-to/configure-fallbacks.md`

Short how-to covering: declaring a per-agent chain, declaring a
platform default, choosing fallback order (cost vs. capability vs.
provider diversity), reading `kolsch sessions events` for
`model.fallback` entries.

---

## Part 6 — Test Cases (referenced from FB-R8)

The full test cases that land in `process/test-catalog.md`
Part 9.7. Each follows the Setup/Action/Expected/Verifies form
used throughout the catalog.

- **TC-FB-01**: Agent declares `fallback_models: [haiku]`. Primary
  (Opus) returns `model.rate_limited`. Engine emits
  `model.fallback{from: opus, to: haiku}` and Haiku returns
  success. Final agent_message attributed to Haiku in the event
  log.
- **TC-FB-02**: All three models in the chain fail with
  Recoverable errors. Engine emits the chain of fallbacks then
  `thread.exchange_failed` with cause `llm.all_models_failed`
  carrying every attempt.
- **TC-FB-03**: Primary succeeds. No `model.fallback` events; chain
  is never consulted; lazy LLM clients for fallbacks were never
  built.
- **TC-FB-04**: `fallback_models` includes an alias; resolver
  normalizes it to canonical at Layer 1. The `model.fallback`
  event's `to.model` field is the canonical name, not the alias.
- **TC-FB-05**: `fallback_models` includes an unknown alias. Agent
  load fails with `config.unknown_alias` naming the offending
  alias — not at exchange time, not at fallback trigger time.
- **TC-FB-06**: `model.fallback` event has the exact schema from
  patch §3.1: `from`, `to`, `reason`, `reason_detail`, `attempt`.
- **TC-FB-07**: Streaming exchange. Primary returns one content
  event then 5xx. Engine emits `thread.exchange_failed` (NOT
  `model.fallback`); the chain is not consulted. Documented
  behavior, not a bug.
- **TC-FB-08**: Chain of three models; primary and first fallback
  fail; second fallback succeeds. Verifies order: Opus → Haiku →
  GPT4o, two `model.fallback` events in that order.
- **TC-FB-09**: Primary returns `model.authentication_failed`
  (Fatal). Engine does NOT fall back; emits
  `thread.exchange_failed` immediately. Chain untouched.
- **TC-FB-10**: Reconstruction. Write a JSONL containing the full
  fallback chain. Delete the entity DB. Run `kolsch reconstruct`.
  Verify the reconstructed session's events include every
  `model.fallback` in the original order.

---

## Part 7 — Explicitly Deferred

1. **Mid-stream fallback.** Captured in §2.3. Requires UX
   decisions and partial-state handling beyond this patch.
2. **Health-tracking memory.** "Provider X failed twice in the
   last minute; skip it on the next exchange." Stateful, opens
   replay-determinism questions, hosted concern. Phase 3.
3. **Cost-aware reordering.** "Prefer the cheapest model that
   meets the response quality bar." Requires a quality signal
   the platform doesn't have. Out of scope.
4. **Per-error fallback policy customization.** Users can't yet
   say "fall back on 429 but not on 5xx." Spec'd as a future patch
   *if* concrete demand surfaces. Speculative knobs rejected.
5. **Connector-level retries.** Out of scope per §2.4. If a
   connector needs in-model retry (e.g., exponential backoff on
   429 with retry-after), that's a connector implementation
   choice, not engine concern.
6. **Cross-exchange model preference.** "Remember which model
   served last time so next time skip ahead." Stateful, replay-
   hostile. Not in v1.

---

## Part 8 — Open Items

1. **What counts as `model.context_overflow`?** Connectors must
   classify provider-specific responses. OpenAI returns
   `context_length_exceeded` in the error body; Anthropic returns
   `invalid_request_error` with a specific code; OpenRouter mostly
   passes through. Each connector's call layer must map its
   provider's responses to the canonical `model.context_overflow`
   code. Flagged so Step 18-developer plans the mapping per
   connector.
2. **Retry-after semantics.** `model.rate_limited` carries
   `retry_after_seconds` when the provider supplies it. Does the
   fallback loop *wait* before trying the next model? Decision:
   NO. Fallback's point is to bypass the rate-limited model
   entirely, not to wait it out. The retry-after is informational
   for client-side backoff in higher-level integrations, not for
   the engine loop. Flagged in case this is wrong.
3. **Token-budget accounting across fallback.** If Opus's failed
   request consumed input tokens (some providers bill for failed
   calls), and Haiku's succeeded request consumed input tokens,
   the `usage` field on the final `model.response` reflects only
   the successful call. Cumulative cost across fallbacks is a
   metrics concern, deferred. Flagged for the eventual
   observability patch.
4. **Order vs. parallel attempts.** Could the engine attempt the
   chain in parallel and take the first success? Faster but more
   expensive (multiple billable calls per exchange in the
   happy-fail path) and contention with provider rate limits.
   Sequential is the right v1.3 default. Parallel "race" mode is
   a future knob if anyone asks.
5. **Test fake LLM and fallback.** The fake LLM used in unit /
   component / integration tests needs a mode to inject specific
   error codes on demand so TC-FB-* can be deterministic. The
   fixture in `tests/fixtures/fake_llm.py` will need a small
   extension. Flagged for Step 20 developer.

---

## Part 9 — Status

This is **Phase 1.3 patch draft 0.1**. The patch:

- Closes the silent-on-transient-failure gap (a single 429 no
  longer kills an exchange).
- Establishes the model-fallback contract as a one-list-in-YAML
  feature, not a strategy engine.
- Forces every existing and future `model.*` error to declare
  Recoverable vs Fatal — a structural improvement to the error
  model.

**Apply in two passes:**

1. **Revisions.** FB-R1 through FB-R12 mechanically edit existing
   docs and create the one new how-to. Same pattern as the LLM-R
   and UX-R passes.
2. **Implementation.** Extend the agent schema validator;
   implement the engine's fallback loop in `src/engine/_fallback.py`;
   classify the existing `model.*` errors per §2.2; emit
   `model.fallback` events; extend the fake LLM fixture to inject
   specific errors; add TC-FB-* tests. One focused developer-
   agent branch, reviewed against §10.14 and Part 9.7.

After this patch lands, agents survive transient provider blips
without user intervention, and the event log records exactly
which model produced the final answer. That is the bar.
