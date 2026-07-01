# Configure Model Fallback Chains

Short recipes for declaring backup models so a single provider
hiccup doesn't kill an exchange. For *why* fallback works the way
it does, read the [error model §4.15](../explanation/error-model.md)
and the [engine contract §2.1 step 7 + §2.4](../explanation/agent-engine-contract.md).
The first-agent tutorial introduces the field in
[step 10](../tutorials/first-agent.md).

## Per-agent fallback

Add `fallback_models:` next to `model:` in the agent's YAML. The
entries accept the same three shapes as `model:` itself (alias,
compact `provider/model` string, or canonical
`{provider, model}` object):

```yaml
# bundles/acme/agents/researcher.yaml
schema_version: "0.1"
agent_id: ag_researcher
name: researcher
model: opus
fallback_models:
  - haiku                                       # alias
  - openrouter/openai/gpt-4o                    # compact string
  - { provider: openrouter, model: anthropic/claude-haiku-4-5 }  # canonical
prompt: …
io: { mode: conversational }
```

The engine walks the list in order. On a Recoverable `model.*`
error from one entry, it moves to the next. Successful entries
end the exchange normally; if all entries fail, the engine raises
`llm.all_models_failed` (Fatal) carrying every attempt.

An empty list (`fallback_models: []`) is explicit "no fallback for
this agent" — it overrides any platform default. Omitting the
field entirely defers to the platform default.

## Platform-level default

Set `defaults.fallback_models` in `platform.yaml` to apply a chain
to every agent that doesn't specify its own:

```yaml
# platform.yaml
defaults:
  model:
    provider: anthropic
    model: claude-opus-4-7
  fallback_models:
    - haiku
```

Precedence is agent-wins, no merging: agent value (including `[]`)
fully replaces the platform default.

## Choosing chain order

A few practical orderings, depending on what you're protecting
against:

| Concern | Chain shape | Why |
|---|---|---|
| Provider-side rate limits | Same-provider alternatives first (`[opus, haiku]`), then a different provider (`gpt4o`). | Same provider's other models share infrastructure but not rate-limit pools per model. |
| Provider-side outages | Different providers first (`[openrouter/anthropic/…, anthropic/…]`). | Diversified routing — when OpenRouter is down, the direct Anthropic key still works. |
| Cost ceilings | Cheapest viable model first. | If the cheap model usually answers, only fall back to the expensive one on actual transient errors. |
| Context overflow | A larger-context model anywhere in the chain (`[gpt4o, claude-opus-200k]`). | `model.context_overflow` is Recoverable; a larger window is the fix. |

There's no weighting, no cost-aware reordering, no health-tracking
memory across exchanges in v1.3. The chain is a plain list,
walked sequentially, fresh state per exchange. Stateful policies
are deliberately deferred (Phase 1.3 patch Part 7).

## What triggers fallback

The engine consults the chain on Recoverable `model.*` errors only:

- `model.rate_limited` (HTTP 429)
- `model.server_error` (HTTP 5xx)
- `model.timeout`
- `model.context_overflow`

It does NOT consult the chain on:

- `model.authentication_failed` — same key fails on the next entry.
- `model.bad_request` — the request itself is broken.
- `llm.provider_not_configured` / `llm.client_init_failed` —
  config gaps that re-occur identically.
- Anything outside `model.*` (engine bugs, store failures, etc.).

The canonical table is [Error Model §4.15](../explanation/error-model.md).

## Reading the event log

`kolsch sessions events <session-id>` shows the full timeline.
A successful fallback looks like this:

```
thread.exchange_started
user.message
model.request          { model: anthropic/claude-opus-4-7 }
model.fallback         { from: …opus, to: …haiku,
                         reason: model.rate_limited, attempt: 1 }
model.request          { model: anthropic/claude-haiku-4-5 }
model.response         { model: anthropic/claude-haiku-4-5, … }
agent.message
thread.exchange_completed
```

Exactly one `model.fallback` per transition. The `attempt` field
is 1-indexed (attempt 0 was the primary). The
`model.response.data.model` field always names the model that
actually produced the answer.

A failed chain looks like this:

```
thread.exchange_started
user.message
model.request          (opus) → 429
model.fallback         (opus → haiku, rate_limited)
model.request          (haiku) → 500
model.fallback         (haiku → gpt4o, server_error)
model.request          (gpt4o) → timeout
thread.exchange_failed (cause: llm.all_models_failed,
                        attempts: [opus, haiku, gpt4o])
```

`detail.attempts` on the failure event lists every model the
engine tried, with the specific error each returned. No grep
required.

## Streaming caveat

Fallback applies only **before** the first content event has
fired. Once any token has been emitted to the user, the engine
ends the exchange with the truncation rather than swap models
mid-stream — `model.fallback` does not fire, and the user sees
the partial output. See the
[engine contract](../explanation/agent-engine-contract.md).

If you need mid-stream resilience, watch for a future patch — the
UX decisions around partial-output replay are non-trivial.

## What about retrying the same model?

The engine doesn't retry the primary before moving on. If you
want "try Opus twice before falling back to Haiku," list Opus
twice:

```yaml
model: opus
fallback_models:
  - opus
  - haiku
```

The Layer 1 validator emits a `config.fallback_duplicate` warning
at agent load (so you see the pattern in the CLI banner), but
the chain is honored as written. This is a feature: retry policy
is data, not a separate config knob.

Intra-model exponential backoff against the same endpoint is a
connector concern, not an engine one — see the LLM provider
patch's contributor README if you're implementing a new
connector that wants to handle 429 internally.

## Where to read next

- Tutorial: [first-agent.md step 10](../tutorials/first-agent.md) —
  the guided introduction.
- Spec: [error-model.md §4.15](../explanation/error-model.md) — the
  canonical trigger table + level declarations.
- Spec: [agent-engine-contract.md §2.1 + §2.4](../explanation/agent-engine-contract.md)
  — pre-flight chain build + the runtime loop.
- Reference: [API: Engine](../api/index.md#engine-execution-context)
  — the engine's API surface for fallback.
