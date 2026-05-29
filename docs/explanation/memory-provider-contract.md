# Phase 1 — The Memory Provider Contract

**Draft 0.1**

> Memory is a Provider Module. The contract is what the rest of the system
> depends on. v1 ships exactly one variant: `recent_buffer`, an in-memory
> dual-purpose implementation that combines recent conversation events with
> explicitly-written tagged memory items.
>
> The architecture's wager: by exposing a contract close to the shape the
> field has converged on (Mem0's API), future variants can wrap real memory
> backends (Mem0, Letta, Zep, Cognee) as thin adapters when the platform
> needs them.

---

## Part 0 — Decisions Locked

- **API shape modeled on Mem0** — `write`, `retrieve`, `list`, `delete`,
  `compact`. Five methods.
- **Memory items have structured fields** — `id`, `session_id`, `agent_id`,
  `tags`, `metadata`, `content`, `created_at`. No `user_id` in v1.
- **Filter language** — `tags_any` (union, hierarchical prefix-matched),
  `tags_all` (intersection, prefix-matched), scope-field equality. Combined
  via implicit AND.
- **Tags are freeform** — hierarchical strings with `/` separators. No
  managed vocabulary.
- **`recent_buffer` is dual-purpose** — retrieval combines (a) recent
  conversational events from the session log AND (b) explicitly-written
  memory items. The variant decides how to blend them.
- **Memory items persist as events** in the session log
  (`memory.written` / `memory.deleted`). The in-memory store is a cache
  rebuilt by replaying those events on session open.
- **Memory is session-scoped in v1.** Tags organize within a session;
  cross-session memory is a Phase 2 concern.
- **Asymmetric read/write tag config:**
  - `write_tag`: a single tag string. Cannot contain `*`. Omitted = the
    agent does not write memory.
  - `read_tags`: a list of tag strings, may include `*` wildcards. Empty
    or omitted = the agent does not read memory.
- **`MemoryContext` is the session-lifetime handle** — issued by the Memory
  provider at engine setup, bundles scope and tag policy, gates all
  per-session operations.

---

## Part 1 — What Memory Is in v1

Memory in v1 is **short-term, session-scoped context memory.** It is NOT:

- Long-term cross-session memory (the agent does not remember across
  sessions in v1).
- Retrieval-augmented context over a knowledge base (knowledge files are a
  separate concern, addressed via tools/skills).
- An agent self-management mechanism (no Letta/MemGPT-style "agent decides
  what to remember"; that's a Phase 2+ exploration).

What it IS:

- A way to capture conversational context that the engine reads back when
  assembling the model's input.
- A way for the agent to *explicitly* store tagged facts during a session
  that retrieval will surface in subsequent turns.
- The mechanism through which compaction summaries are produced.

The `recent_buffer` v1 variant combines two sources:

1. **Recent conversational events from the session log.** No separate
   storage; the variant pulls from the Session/Run Store on demand.
2. **Explicitly-written memory items**, persisted as `memory.written`
   events in the session log and cached in process memory for fast
   retrieval. Items are tagged with the agent's `write_tag`.

Retrieval blends both: recent events (for "what just happened") plus tag-
filtered memory items (for "what was explicitly noted"). The variant's
implementation decides the blend.

---

## Part 2 — The Memory Item

A memory item is the unit of explicitly-stored memory.

```
MemoryItem
├── id              string      — "mem_..." prefixed UUID
├── session_id      string      — "ses_..." the session it belongs to
├── agent_id        string      — "ag_..." the agent that wrote it
├── tags            [string]    — hierarchical tags; v1 always single-element
│                                 (one tag per write); reserved as list for
│                                 future variants
├── metadata        object      — arbitrary user-supplied data
├── content         object      — the actual content (typed; opaque to
│                                 the Memory provider)
└── created_at      string      — ISO-8601 UTC
```

Notes:
- `tags` is a list because the schema reserves the shape for future
  multi-tag-per-item variants. The v1 `recent_buffer` always writes
  single-element lists (the agent's `write_tag`).
- `content` is opaque to the Memory provider — it does not interpret the
  structure. The agent that wrote it knows the shape.
- Items are immutable once written. To "update" memory, write a new item
  (possibly delete the old). The Memory contract has no `update` operation.

---

## Part 3 — The MemoryContext (Session Initialization)

The MemoryContext is the agent's bound memory handle for a session.

### 3.1 Why it exists

The agent declares its memory engagement in its config (write_tag,
read_tags). Without a MemoryContext, every memory operation would need to
re-parse the agent's config to know its scope. With it, the binding
happens once at session setup, and all subsequent operations route
through the handle.

It also enforces the policy: an agent with no `write_tag` cannot write,
period — `MemoryContext.write()` raises. The handle is where the agent's
declared memory stance becomes operational.

### 3.2 Shape

```
MemoryContext
├── session_id      string
├── agent_id        string
├── write_tag       string?     — None if writes are disabled
├── read_tags       [string]    — may include wildcards; empty if reads disabled
├── variant_state   object      — variant-specific runtime state (cache, etc.)
└── (methods, see Part 4)
```

### 3.3 Acquisition

The Agent Engine, at session setup (per the Agent Engine contract §2.1),
asks the Memory provider for a `MemoryContext`:

```
context = memory_provider.bind_session(
    session_id=...,
    agent_id=...,
    config=resolved_config.subsystems.memory.config,
)
```

The provider:
1. Validates the config (Layer 3 validation — the variant's own validator
   already ran during config resolution; this is a safety re-check).
2. Replays `memory.written` and `memory.deleted` events from the session
   log to rebuild the in-memory cache for this session. The Memory
   provider does this by calling `store.read_events(thread_id)` and
   filtering for memory event types — NOT by reading the JSONL file
   directly. This preserves the Session/Run Store's role as sole
   mediator of log access (Store I8).
3. Returns a MemoryContext bound to this session/agent.

The Agent Engine holds the MemoryContext for the session's lifetime. On
session resume, a new MemoryContext is bound (cache rebuilt from log).
There is no persistent MemoryContext — it is per-runtime-instance state.

### 3.4 Lifetime

A MemoryContext lives as long as the engine's active processing of the
session. When the engine returns from `execute`, the context may be
discarded or held (implementation choice — likely held in a runtime
cache for the duration of an interactive session, discarded for one-shot
invocations).

Crucially: nothing persistent about the agent's memory state lives only
in the MemoryContext. The cache it holds is a projection of events in
the log; the log is authoritative; discarding the context loses nothing.

---

## Part 4 — The Interface

All methods are `async`. Errors are raised, not returned as status values.

### 4.1 Provider-level methods

```
async def bind_session(
    session_id: str,
    agent_id: str,
    config: MemoryConfigBody,
) -> MemoryContext
```
See Part 3. Returns a session-bound context.

### 4.2 MemoryContext methods

```
async def write(
    content: object,
    metadata: dict | None = None,
) -> MemoryItem
```
Writes a memory item. Tags it with the bound `write_tag`. Raises
`MemoryWriteDisabled` if `write_tag` is None (the agent declared no
write_tag).

The actual persistence flow: the MemoryContext does NOT directly append
to the session log. It returns the constructed `MemoryItem` to the
caller (the Agent Engine), which then emits a `memory.written` event
via its normal event-emission path. The MemoryContext updates its
in-memory cache as part of this flow.

```
async def retrieve(
    query: RetrievalQuery,
) -> RetrievalResult
```
Returns memory content suitable for inclusion in a model context. The
query specifies optional filters and a token budget. The `recent_buffer`
implementation combines recent conversational events and tag-filtered
memory items.

```
async def list(
    filters: FilterExpression | None = None,
) -> list[MemoryItem]
```
Returns matching memory items without relevance ranking. For
introspection, debugging, the IDE's "show me my memories" view, etc.
NOT for context assembly (use `retrieve` for that).

```
async def delete(
    item_id: str,
) -> None
```
Deletes a memory item. The MemoryContext returns to the caller; the
Agent Engine emits a `memory.deleted` event, the cache updates.

```
async def compact(
    events: list[Event],
    target_tokens: int,
) -> CompactionResult
```
Produces a summary of the given events suitable to replace them in the
model's context. The caller (Agent Engine) is responsible for emitting
the resulting `context.compacted` event with the summary; the
MemoryContext does not emit events directly.

This is intentionally a Memory responsibility because future variants
(Mem0-backed, dedicated summarization model) will own clever summarization
algorithms. The v1 `recent_buffer` implementation does a basic
"join the events and ask the LLM for a summary" using the agent's
configured model — adequate, not impressive.

### 4.3 RetrievalQuery shape

```
RetrievalQuery
├── max_tokens       int           — budget for the assembled memory
├── tags_any         [string]?     — override the bound read_tags for this call
├── tags_all         [string]?     — items must match all of these
├── metadata         dict?         — equality filter on metadata fields
├── semantic_query   string?       — for future variants (ignored by recent_buffer)
└── time_range       tuple?        — for future variants (ignored by recent_buffer)
```

All fields optional. By default (no overrides), retrieval uses the
MemoryContext's bound `read_tags` as `tags_any` and respects the
configured `max_tokens` from the agent's config. Per-call query
parameters override the defaults for that call.

### 4.4 RetrievalResult shape

```
RetrievalResult
├── items            [RetrievedItem]
├── total_tokens     int           — actual tokens of returned content
└── source_summary   dict          — diagnostic: how the result was assembled
                                     (events vs. memory items, counts)

RetrievedItem
├── content          object        — the item's content
├── source           enum          — "log_event" | "memory_item"
├── source_id        string        — ev_... for log_event, mem_... for memory_item
├── relevance        float?        — when the variant computes one (recent_buffer
                                     does not — items are returned in temporal
                                     order)
└── tags             [string]      — the item's tags (for memory_item) or empty
                                     (for log_event)
```

The `source` field distinguishes recent events from memory items. The
Agent Engine uses this when constructing the model's input — the items
become messages in the assembled context. The `memory.retrieved` event
(emitted by the engine, not the provider) carries the full
RetrievalResult content verbatim (Materialization Rule).

### 4.5 FilterExpression shape

For `list()`. Same primitives as RetrievalQuery's filters but without the
token budget:

```
FilterExpression
├── tags_any         [string]?
├── tags_all         [string]?
├── metadata         dict?
├── agent_id         string?       — defaults to context's agent
├── created_after    string?       — ISO-8601
└── created_before   string?
```

---

## Part 5 — The `recent_buffer` Variant

The v1 implementation. Specified concretely because it ships in v1.

### 5.1 Config body

The variant's validator is a `ConfigValidator` per the Kernel contract —
`Callable[[dict], ValidationResult]`. It receives the subsystem config
body's dict and returns structured validation results. The Kernel calls
it during Layer 3 validation per its dispatch mechanism (Kernel §4).

```
memory:
  variant: recent_buffer
  config:
    write_tag       string?      — exact tag for writes; no wildcards
    read_tags       [string]     — list of tags (wildcards permitted)
    recent_turns    int          — how many recent exchanges to include in
                                   retrieval (default 10)
    max_tokens      int          — max budget for assembled memory
                                   (default 4000)
```

Layer 3 validator rules:
- If `write_tag` is present, it must NOT contain `*` (raises
  `code: "wildcard_in_write_tag"`).
- If `read_tags` is empty or omitted, retrieval returns only recent
  events, no memory items.
- If `write_tag` is omitted, writes are disabled (MemoryContext.write
  raises).
- At least one of `write_tag` or `read_tags` must be present (otherwise
  the subsystem should be omitted entirely — absence = disabled).

### 5.2 Storage

In-process dict per session, keyed by `MemoryItem.id`. Not persisted
separately — persistence is via the session log's `memory.*` events. On
session open, the variant rebuilds its cache by replaying the log's
`memory.written` and `memory.deleted` events for that session.

The cache structure:

```
session_memory: dict[session_id, {
    items: dict[item_id, MemoryItem],
    by_tag: dict[tag_string, set[item_id]],   — for fast tag lookup
}]
```

`by_tag` indexes items by *exact* tag. Hierarchical prefix queries walk
the tag space (small in practice; v1 has dozens to hundreds of tags per
session at most). For larger scales a more clever index is justifiable;
the v1 implementation does not need it.

### 5.3 Retrieval algorithm

For a `retrieve(query)` call:

1. Determine the effective `tags_any` (from query if specified, else
   bound `read_tags`).
2. Determine the effective `max_tokens` (from query, else config).
3. **Collect memory items matching the tag filter.** Walk the cache,
   match items against `tags_any` (prefix-matched union) and any
   additional filters. Sort by `created_at` descending.
4. **Collect recent conversational events.** Ask the Session/Run Store
   for the last `recent_turns` exchanges (one exchange = user.message +
   model+tool events + agent.message). Project the events into the
   message format.
5. **Blend.** Interleave by `created_at`. Truncate to fit `max_tokens`.
   The exact blend policy is the variant's choice; v1's choice is:
   recent events first (latest first), then memory items (newest first),
   stopping when budget is exhausted. This biases toward recency.
6. Return RetrievalResult.

This is the entire algorithm. It is deliberately simple. The intent is
that v2 variants (semantic search, hybrid retrieval) replace this
algorithm with something better while the contract stays the same.

### 5.4 Compaction algorithm

For `compact(events, target_tokens)`:

1. Join the events' content into a single text blob.
2. Construct a summarization prompt: "Summarize the following
   conversation segment, preserving facts, decisions, and unresolved
   threads. Target length: ~target_tokens tokens."
3. Call the agent's configured model with the prompt.
4. Return the result as a `CompactionResult` with the summary text and
   the source event IDs.

Crude but functional. The agent's own model is used; no separate
summarization model in v1. Variants can do better.

---

## Part 6 — Failure Modes

### 6.1 Write disabled

`write()` called when `write_tag` is None raises `MemoryWriteDisabled`.
The Agent Engine surfaces this as a tool failure if a tool triggered
the write.

### 6.2 Item not found

`delete()` for an unknown `item_id` raises `MemoryItemNotFound`. Idempotent
deletes are a future feature.

### 6.3 Tag validation

A write with a malformed `write_tag` (containing `*`) is rejected at
config resolution time, not at write time. By the time a MemoryContext
exists, its `write_tag` is valid.

### 6.4 Compaction model failure

If the underlying model call for compaction fails, `compact` raises.
The Agent Engine treats this as an in-exchange failure (the exchange
may proceed with the uncompacted context, hitting the model's window
limit, OR the engine may abort the exchange — implementation choice).

### 6.5 Cache miss

A memory operation against a session not yet bound (no MemoryContext
was created for it) raises `MemorySessionUnbound`. This indicates a
programming error in the Agent Engine — it should always bind before
operations.

---

## Part 7 — Conformance Invariants

What any Memory Provider implementation must honor.

### I1 — No direct event emission

The Memory provider MUST NOT write events to the session log directly.
It returns operations' results to the Agent Engine, which emits the
`memory.written`, `memory.deleted`, and `memory.retrieved` events per
the Agent Engine contract. This preserves the engine's sole-emitter
invariant (Agent Engine I1).

### I2 — Materialization compatible

Memory's `retrieve` MUST return actual content for inclusion in the
model context. The Agent Engine captures this content verbatim on the
`memory.retrieved` event (per the Materialization Rule, Schemas §B.6).
The Memory provider MUST NOT return references to be re-resolved later
— actual content, every time.

### I3 — Cache derived from log

The Memory provider's runtime cache MUST be reconstructable from the
session log's `memory.written` and `memory.deleted` events. On session
resume, replaying these events MUST produce the same cache state as
existed before the previous shutdown.

### I4 — Tag enforcement

The MemoryContext MUST enforce the agent's declared tag policy:
- Writes are tagged with the bound `write_tag` (no override permitted at
  write time in v1).
- Default retrieval uses the bound `read_tags`; per-call override is
  permitted via the query.

### I5 — Wildcard restriction on write

A `write_tag` containing `*` is invalid and MUST be rejected at config
validation time (the variant's Layer 3 validator). It MUST NOT reach
the MemoryContext.

### I6 — Async semantics

All Memory Provider methods are `async`. Implementations MUST NOT block
the event loop on storage or computation.

### I7 — Session scope (v1)

All v1 operations are scoped to a single session. The Memory provider
MUST NOT return memory items from one session to another, even if their
agent_ids or tags match. Cross-session memory is a Phase 2 capability
behind a different variant.

### I8 — Variant-owned config validation

Each variant owns its `config` body schema and validates it at Layer 3
(per the Kernel contract). The Memory Provider contract specifies the
shape of MemoryConfig at the wrapper level (`{variant, config}`); the
contents of `config` are the variant's concern.

### I9 — Determinism of replay

Given the same sequence of `memory.written` and `memory.deleted` events,
cache reconstruction MUST produce identical results. Specifically: item
ordering after replay must be deterministic (by `created_at` then `id`),
so that retrieval results from a replayed cache match a never-shutdown
cache.

---

## Part 8 — Open Items

1. **The compaction prompt template.** §5.4 sketches a prompt; the
   exact text needs tuning. This is an implementation detail; the
   contract specifies the operation.

2. **Cache eviction.** v1 keeps the entire per-session cache in memory.
   For very long sessions with many memory items, this could grow
   unbounded. v1 makes no commitment to eviction; if sessions exceed
   reasonable bounds, the variant can be enhanced. Phase 2 likely
   moves to a real backend with native handling.

3. **The Agent Engine's blend policy for retrieval results.** §4.4's
   RetrievalResult contains both `log_event` and `memory_item` sources;
   how the engine actually assembles them into the model's message list
   is in the engine's Assembly Procedure (§B.5 of the schemas), not
   here. Likely worth a small refinement to §B.5 explicitly handling
   memory results.

4. **Cross-agent shared tags within a session.** If two agents in the
   same session (Phase 2 multi-agent) both write to a tag that the
   other reads from, they effectively share. v1 has one agent per
   session so this is moot. Behavior in Phase 2 needs explicit
   specification.

5. **Memory item content typing.** §2's MemoryItem has `content: object`,
   opaque. An advanced variant might want to type memory content (e.g.,
   to support semantic indexing). v1 doesn't; the contract permits a
   variant to enforce typing in its own validation.

6. **The MemoryContext's `variant_state` field.** §3.2 mentions it; v1's
   `recent_buffer` uses it for the cache. The shape is variant-specific;
   the contract just reserves the field.

7. **Future variant: Mem0-backed.** A Phase 2 variant wrapping the Mem0
   library is a natural fit. The translation is mostly direct: write_tag
   maps to a single category tag, read_tags map to category union
   filters, memory items map to Mem0 memories. Documented here so a
   future implementer has the shape in mind.
