# Phase 1 — The Event Bus Contract

**Draft 0.1**

> The inter-module notification layer. Modules announce that something
> happened; other modules react without the publisher knowing they exist.
> The mechanism that lets the strict downward-dependency rule survive in
> practice.
>
> v1 ships an ephemeral implementation (Redis pub/sub). Subscribers that
> need guaranteed delivery use the log via the Session/Run Store, not a
> separate durable bus mechanism.

---

## Part 0 — Decisions Locked

- **Single event vocabulary.** Bus events ARE log events. The Agent
  Engine, when it appends an event to the log, also publishes that same
  event to the bus. One vocabulary, two destinations.
- **Topic-based subscription with filtering.** Subscribers declare a
  pattern at subscription time (by event `type`, by `thread`, etc.). The
  bus filters before delivery. No event-by-event "is this for me" check
  in subscriber code.
- **v1 ships the ephemeral tier only**, and its **default backend is an
  in-process asyncio pub/sub** — zero external services. **Redis** is the
  opt-in backend for cross-process delivery and real-time fan-out
  (adopted at Tier 2+, e.g., when the Phase 2 IDE needs live updates).
  Both are ephemeral and fire-and-forget: if a subscriber is offline or
  slow, it misses events. Backend selected via `platform.yaml`'s
  `event_bus.backend` (`in_process` default, `redis` scale-up).
- **Strict subscribers achieve durability via the log**, not via a separate
  durable bus. They tail the JSONL via the Session/Run Store's
  `read_events(thread_id, from_event_id=...)` with a bookmark, and
  typically subscribe to the bus as a low-latency wakeup signal. (With the
  in-process default, all subscribers are in-process; the strict-subscriber
  pattern becomes relevant for cross-process subscribers at Tier 2+ when
  Redis is enabled.)
- **The contract describes both modes** so a Phase 2 strict subscriber
  knows which to use; v1's bus implementation is just the ephemeral half.
- **Reactions, not orchestration.** The bus is for "X happened, react";
  it is not a mechanism for sequencing processes. Workflows orchestrate;
  the bus notifies.

> **Backend terminology in this contract.** Where the text below says
> "Redis," read it as "the bus backend." The in-process asyncio pub/sub
> is the default implementation; Redis is the cross-process scale-up
> implementation. The API and semantics are identical across backends.

---

## Part 1 — What the Bus Is and Isn't

### 1.1 What it is

A pub/sub notification mechanism for inter-module communication. When a
module performs an action that produces an event, it publishes the event
to the bus. Other modules that have subscribed to matching topics receive
the event and react.

The bus sits low in the dependency graph (alongside the kernel), depended
on by most modules. It depends only on the kernel and its configured
backend (in-process by default; Redis at scale).

### 1.2 What it isn't

- **Not the event log.** The log (per the Phase 1 Schemas §B) is the
  ground-truth, durable, ordered record of execution. The bus is a
  low-latency notification layer that delivers the *same events* the log
  records, but with fire-and-forget semantics. Lose a bus message: the
  log still has the event.
- **Not orchestration.** A workflow is a definition-driven sequence of
  steps (Phase 2). The bus is for reactions. Processes go in workflows,
  not in chains of bus subscribers.
- **Not a queue.** Subscribers receive events in real time; there is no
  per-subscriber backlog of unprocessed events. A subscriber that misses
  events while disconnected does not "catch up" via the bus — it catches
  up via the log.
- **Not a request/response layer.** The bus is one-way: publish, deliver.
  Modules that need to call each other use direct API calls; the bus is
  strictly for after-the-fact notification.

### 1.3 The two notification modes

v1 modules choose between two notification mechanisms based on what they
need:

**The bus (this contract):**
- Real-time, low-latency.
- Topic-filtered subscription.
- Fire-and-forget — missed events are missed.
- For live UI updates, monitoring, casual reactions.

**The log (Session/Run Store API):**
- Catch-up-capable from any bookmark.
- Guaranteed delivery for events that are persisted.
- Higher-latency (the consumer pulls, on its own schedule).
- For strict subscribers that cannot miss events.

A strict subscriber typically uses BOTH: it tails the log for guaranteed
delivery, and also subscribes to the bus as a low-latency wakeup signal
("something new arrived — go check the log"). The bus is the doorbell;
the log is the mail.

This separation means the bus contract stays simple; the durability
guarantee comes from the log, which already exists for other reasons.

---

## Part 2 — The Event Vocabulary

The bus carries the same event types defined in the Phase 1 Schemas §B —
the session event log vocabulary. Examples include `user.message`,
`agent.message`, `tool.called`, `tool.result`, `model.request`,
`model.response`, `thread.exchange_completed`, `config.set`,
`session.opened`, and the others enumerated in §B.7.

There is no separate "bus event" vocabulary. The bus carries full event
objects (per §B.2's envelope and §B.8's per-type `data` payloads) — the
same objects appended to the JSONL file.

### 2.1 Why full events, not summaries

The decision is intentional. A summary-event vocabulary would require:
- A second schema (bus events distinct from log events).
- The publisher deciding which log events warrant bus publication and
  what summary to produce.
- A translation step from log event to bus event.

Three sources of complexity, three sources of bug. By carrying the actual
event, subscribers see ground truth without a translation layer.

The cost — bus messages are sometimes large (e.g., `tool.result` with a
file's content) — is real but acceptable. Subscribers filter by topic,
so high-volume subscribers see only the events they care about. Redis
pub/sub handles large messages adequately for v1's scale.

### 2.2 Event identity

Each bus event carries the same `id` (`ev_...`) as the log event.
Subscribers can use this id for deduplication and as a bookmark when
correlating with log reads.

---

## Part 3 — Topic-Based Subscription

### 3.1 Topic format

A topic is a dot-separated pattern matching one or more event types,
optionally narrowed by attributes. The supported pattern grammar:

```
<event-type-pattern>[ : <attribute-filters> ]

event-type-pattern  = exact-type | type-prefix.*
exact-type          = the event type, e.g. "tool.failed"
type-prefix.*       = a prefix wildcard, e.g. "tool.*" matches all tool.* events
attribute-filters   = key=value pairs joined by ","
                      filters supported in v1: thread=<th_id>
```

Examples:
- `tool.failed` — exact match on the event type.
- `tool.*` — any event whose type starts with `tool.` (`tool.called`,
  `tool.result`, `tool.failed`).
- `*` — all events.
- `model.response:thread=th_abc123` — `model.response` events on a
  specific thread.
- `thread.exchange_*:thread=th_abc123` — exchange progress events on a
  specific thread.

### 3.2 Implementation note

The pattern grammar maps cleanly to Redis pub/sub's `PSUBSCRIBE` glob
patterns. The bus implementation may use Redis channels directly (one
channel per event type, with wildcard subscription) or one channel with
in-process filtering. The contract permits either; the choice is an
implementation detail.

v1 implementation: one Redis channel per (event-type), with the bus
implementation filtering by attribute filters in the subscribing process.
Coarse but simple.

### 3.3 Subscriber lifecycle

Subscribers are *transient*. A subscriber registers a handler with the
bus at process startup (or whenever it needs to start listening), and
unregisters when shutting down. Subscriptions do not persist across
restarts — a subscriber that restarts re-registers its interest.

This is consistent with the ephemeral-tier semantics: subscriptions are
in-memory, in-process; they die with the process.

---

## Part 4 — Interface

All methods are `async`.

### 4.1 Publishing

```
async def publish(event: Event) -> None
```
Publishes an event to the bus. The bus filters the event against every
active subscription and delivers it (via Redis pub/sub) to any matching
subscribers. Returns immediately after the publish call to Redis
completes; does NOT wait for subscribers to process.

The Agent Engine MUST call `publish(event)` for every event it appends
to the log. The two operations (append to log, publish to bus) form a
loose pair — the append is durable, the publish is best-effort. If the
publish fails (Redis unavailable), the append still succeeded and the
event is still in the log; bus subscribers may miss it but a log-tailing
subscriber recovers.

There is no transactional guarantee that both succeed or neither does.
The bus is fire-and-forget by design.

### 4.2 Subscribing

```
async def subscribe(
    topic: str,
    handler: Callable[[Event], Awaitable[None]],
) -> SubscriptionHandle
```
Registers a handler to receive events matching `topic`. Returns a handle
the subscriber uses to unsubscribe.

The handler is invoked asynchronously when a matching event is published.
Multiple subscribers may register the same handler with different topics;
multiple subscribers may register different handlers with the same topic;
the bus does not deduplicate.

```
async def unsubscribe(handle: SubscriptionHandle) -> None
```
Removes the subscription. The handler will not be invoked for subsequent
events.

### 4.3 Subscriber handler obligations

The handler receives one event per invocation. It MUST be safe to call
concurrently — the bus may invoke handlers for different events in
parallel.

The handler SHOULD be idempotent in principle, even though the v1
ephemeral bus does not redeliver: idempotency is the discipline that
makes a subscriber safe to upgrade to "tails the log with a bookmark"
later, which CAN redeliver on restart.

The handler MUST handle its own errors. An exception raised from a
handler:
- Is caught by the bus (so it does not propagate or crash the bus).
- Is logged (the bus's own logging, not the session event log).
- Does NOT cause redelivery (ephemeral tier).
- Does NOT affect delivery to other subscribers.

Handlers should be fast. A slow handler does not block delivery to other
subscribers (they are invoked concurrently), but a slow handler that
blocks behind I/O can starve its own subscription if events arrive
faster than it processes them. Redis pub/sub does not buffer beyond a
small per-client window; events that arrive while a subscriber is
behind may be dropped silently. This is acceptable for the ephemeral
tier; strict subscribers should not use the bus.

---

## Part 5 — Bus and Log Together

The bus and the log work together for subscribers that need durability.

### 5.1 The strict subscriber pattern

A subscriber that cannot miss events does this:

1. **Bookmark state.** The subscriber persists somewhere (in its own
   storage, in Postgres, in a file) the `id` of the last event it
   successfully processed.
2. **Catch up on startup.** When it starts, it calls
   `read_events(thread_id, from_event_id=<bookmark>)` on the Session/Run
   Store for each thread it cares about. It processes the returned
   events and updates its bookmark.
3. **Subscribe to the bus** for low-latency wakeup. On receipt of a bus
   event, it knows new events exist; it can either process the event
   directly (if its handler is idempotent and the bus event is enough)
   or trigger another `read_events` from the current bookmark.
4. **On restart.** Repeat the startup sequence — read from bookmark,
   process, resubscribe.

### 5.2 Why this is sufficient

- The log is durable; nothing is ever lost from it.
- The bookmark is the subscriber's own concern, not the bus's. The bus
  has no per-subscriber state.
- Bus messages may be missed, but every missed message is also in the
  log and will be processed on the next read_events call.
- No outbox, no dispatcher, no retry/backoff/dead-letter machinery on
  the bus side. The log already has the durability properties such
  machinery would provide.

### 5.3 What v1 doesn't include

The contract describes the strict-subscriber pattern (above) but v1
ships no strict subscribers — there are none in v1's scope (no workflow
engine, no IDE, no scheduler). The pattern is documented so a Phase 2
component (e.g., the Workflow Engine, which needs to react to
`thread.exchange_completed` to advance a workflow node) knows how to
build a strict subscriber when it needs one.

A future durable-bus implementation (with its own outbox + dispatcher)
is permitted by the contract but not required and not delivered in v1.
The log-based durability pattern works for the foreseeable needs.

---

## Part 6 — Failure Modes

### 6.1 Bus unavailable

If Redis is unreachable, `publish` raises an error. The Agent Engine's
behavior on bus-unavailable is up to its implementation: ignore (log a
warning, continue), or treat as fatal (raise from `execute`). v1
recommendation: log a warning and continue — the bus is best-effort,
the log captured the event, no information is lost.

`subscribe` on a non-running bus raises. A subscriber that loses its
connection to Redis mid-session sees its handler stop being invoked;
when Redis recovers, the subscriber may need to re-register
(implementation-dependent on the Redis client library).

### 6.2 Subscriber failure

A handler that raises is caught by the bus, logged, and dropped (for
the failing event). The subscriber remains registered and receives
subsequent events. No retry, no dead-letter, no removal of the
subscriber for repeated failures. v1 keeps this simple.

### 6.3 Subscriber slow

A subscriber that processes events slower than they arrive may have
events dropped silently by Redis pub/sub when its client-side buffer
fills. This is documented as ephemeral-tier behavior; strict
subscribers should use the log pattern instead.

### 6.4 Publisher outpacing subscribers

The publisher (typically the Agent Engine) does not throttle based on
subscriber capacity. Slow subscribers do not slow publishers. This is
the right tradeoff for the bus — its job is to notify, not to
coordinate.

---

## Part 7 — Conformance Invariants

What any Event Bus implementation must honor.

### I1 — Same vocabulary as the log

Bus events MUST be the same event objects as those appended to the
session event log. The publisher (Agent Engine) MUST publish the same
event it appended. No summary, no translation. (Implementations MAY
publish the event object before, after, or in parallel with the log
append; ordering is not contract-specified.)

### I2 — Topic filtering before delivery

Events MUST be filtered by topic before being delivered to a subscriber.
A subscriber that subscribed to `tool.failed` MUST NOT receive
`thread.exchange_started` events.

### I3 — Concurrent handler invocation safety

The bus MAY invoke handlers concurrently — including the same handler
twice in parallel for different events. Handlers MUST be safe to call
concurrently. (This is a discipline imposed on subscribers, not a
guarantee from the bus.)

### I4 — Failure isolation

A handler that raises MUST NOT prevent delivery to other subscribers,
MUST NOT crash the bus, and MUST NOT cause redelivery. The failing
handler's error is logged out-of-band; the event is otherwise dropped
for that subscriber.

### I5 — Best-effort delivery

The bus provides best-effort, real-time delivery. It MAY drop events
under load (slow subscribers, Redis buffer pressure). Subscribers that
cannot tolerate drops MUST use the log pattern (§5.1), not the bus.

### I6 — No subscriber state

The bus MUST NOT persist any per-subscriber state — no backlogs, no
acknowledgments, no bookmarks, no delivery records. Subscriptions are
transient and in-memory. (This is what makes the bus genuinely
fire-and-forget; a Phase 2 durable bus, if built, would relax this.)

### I7 — Publisher decoupling

The bus MUST NOT make `publish` calls fail in a way that affects the
publisher's primary work. If Redis is unavailable, `publish` may raise;
the publisher decides how to handle that (ignore vs. fail). The bus is
not the publisher's transactional dependency.

### I8 — Async semantics

All bus methods are `async`. The implementation MUST NOT block the
event loop. Redis client libraries used MUST be async-compatible.

---

## Part 8 — Open Items

1. **Cross-process Redis client configuration.** The exact Redis library
   (`redis-py` with asyncio, `aioredis`, etc.) and connection-pool
   configuration is implementation. v1 picks one and pins it.

2. **Pattern matching subtleties.** §3.1 defines a simple grammar; edge
   cases (escape characters, dot literals inside attribute values, case
   sensitivity) are not specified. v1 implementation makes simple
   choices (all lowercase, no escapes, no dots in values).

3. **Subscriber registration discovery.** In v1, subscribers register
   programmatically in their own startup code. There is no central
   "list of all subscribers" or auto-discovery. If observability of
   active subscriptions becomes useful (debugging, "who's listening for
   `tool.failed`?"), it's a Phase 2 addition.

4. **Performance under load.** Redis pub/sub has known fan-out
   characteristics; for v1's scale (single-user local-first) any sane
   implementation suffices. If multi-tenant or high-volume scenarios
   arrive, the per-channel design may need revision.

5. **The Phase 2 durable bus.** If a Phase 2 use case genuinely needs
   transactional publish semantics (event must commit with the
   underlying change, or not at all), a Postgres-outbox-based durable
   tier can be added behind this contract. The interface
   (`publish`/`subscribe`) stays the same; the implementation gains
   transactional semantics on the publisher side and bookmark-based
   redelivery on the subscriber side. Deferred until a use case appears.

6. **Local development without Redis.** A degraded in-process bus
   implementation (just for tests, local-only) is plausible. v1 ships
   with Redis required; an in-process variant is a later convenience.
