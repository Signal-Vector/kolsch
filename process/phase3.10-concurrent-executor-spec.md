# Phase 3.10 — Concurrent Executor

This spec covers Phase 3.10 — concurrent execution of independent
branches and (optionally) of map iterations. Depends on every
prior phase (3.0 → 3.9). Builds most directly on Phase 3.1's
`IterativeExecutor` + module-isolation discipline and on Phase 3.7's
`MapRunner`.

Related docs:

- `process/phase3.x-roadmap.md` §18.2 / 3.10 — the roadmap entry.
- `process/phase3.0-workflow-spec.md` §1.2 / §8 — sequential-at-v1
  scope locks that Phase 3.10 partially lifts.
- `process/phase3.1-executor-spec.md` §8.1 / §8.2 — the algorithm
  and module-isolation discipline Phase 3.10 generalises.
- `process/phase3.5-file-io-state-spec.md` §11.3 — `workflow.state.mutated`
  event semantics; the load-bearing primitive for the state-conflict
  analysis Phase 3.10 ships.
- `process/phase3.7-composition-spec.md` §4.6.2 — the sequential
  map runner Phase 3.10 extends with an opt-in parallel mode.
- `docs/explanation/workflow-event-log.md` §11 — event-kind taxonomy
  and per-invocation `invocation_id` correlation Phase 3.10 leans on
  to keep interleaved JSONL streams forensically navigable.

---

## 16. Concurrent Executor

### 16.0 Goal

Lift the Phase 3.1 sequential-only constraint where doing so is
SAFE. The engine's hard contracts (deterministic captured outputs,
deterministic state evolution, deterministic JSONL replayability for
fork + resume) remain unchanged — concurrency must not introduce
race conditions that violate them. Phase 3.10 ships the smallest
concurrency surface that delivers real wall-clock gains for the two
shapes that benefit most:

1. **Graph fan-out** — sibling branches with disjoint state writes.
2. **Map iteration** — list-fan-out where the worker is pure (no
   state mutation).

Sequential execution remains the default. Authors opt into
concurrency at the workflow level (`concurrency:` top-level field)
and per-map (`parallel:` field on `kolsch/nodes/map`). The run-start
gate enforces the safety invariants. A workflow whose graph mixes
state mutators with concurrency opt-in is REJECTED at validate time
with a clear error — silent serialisation behind the author's back
is NOT a v1 contract.

### 16.1 Scope

In scope at Phase 3.10:

- Workflow-level `concurrency: sequential | auto` field on
  `Workflow` (default `sequential`).
- Per-map `parallel: bool = false` + optional `concurrency: int | None`
  fields on `MapNodeDef`.
- Run-start gate analysis: collect each parallel branch's state-write
  set and state-read set; reject conflicts with a typed error.
- Concurrent worklist for the `auto` workflow path: ready nodes
  dispatch into an `asyncio.gather` pool; the worklist re-walks on
  every completion. State mutators serialise via an `asyncio.Lock`
  held for the duration of the mutator's invocation.
- Concurrent map iteration for `parallel: true` maps: iterations
  fan out via `asyncio.gather` (or `asyncio.Semaphore(N)` when
  `concurrency: N` is set). `results` preserves index order even
  when completion order differs.
- Per-run module-isolation rewrite from snapshot-then-cleanup to
  per-run module prefix. Bundle modules load under a run-id-tagged
  prefix in `sys.modules`; cleanup removes only this run's entries.
  Two concurrent runs no longer race on `sys.modules` entries for
  the same bundle module.
- Event emission serialisation via an executor-private
  `asyncio.Lock` so concurrent invocations' events still produce a
  well-formed total order in the JSONL (matches the Phase 3.4
  replay contract — replay is line-by-line).
- New error class `WorkflowParallelStateConflict` raised by the
  run-start gate.
- One new orthogonal event-payload field on
  `workflow.run.started`: `concurrency_mode: "sequential" | "auto"`
  (so JSONL consumers can reason about whether interleaving is
  possible without inspecting the source workflow). NO new
  event KINDS.

Out of scope (deferred):

- Cross-RUN concurrency primitives (queue, scheduler, fairness).
  The engine still runs ONE workflow_run per `kolsch workflows run`
  invocation. Cross-run scheduling belongs to `kolsch-server`.
- Distributed execution (multi-host). The `LocalController` stays
  single-host at v1.
- Concurrent EDGES per node (a node with N outgoing edges fires N
  receivers in parallel naturally — this is graph concurrency, the
  Phase 3.10 happy path, not "per-edge concurrency").
- Concurrent sub-workflow execution beyond what `auto` workflow
  concurrency at the parent level naturally produces. A workflow
  node that runs in parallel with a sibling is concurrent at the
  parent's level; the child workflow's OWN concurrency is governed
  by the child workflow's own `concurrency:` field (independent
  per-run decision).
- Automatic re-rendering of `nodes.<X>.<field>` references after a
  concurrent sibling writes (Jinja is rendered ONCE per node fire,
  at fire time — this is the existing Phase 3.1 contract, unchanged
  under Phase 3.10).
- Cancellation mid-concurrent-node — `cancel.flag` continues to
  poll at node boundaries (Phase 3.1 §8.4). A concurrent batch is
  cancellable only when the slowest in-flight node observes the
  flag at its own boundary.
- `parallel:` on `kolsch/nodes/filter` — filter remains a
  comprehension-style leaf in Phase 3.10. Parallelising it would
  buy nothing the `compute` recipe couldn't.
- Per-iteration progress reporting in chat (no new chat surface
  this phase; existing §15.2.2 running-node indicator fires per
  invocation, which now means per concurrent iteration).

### 16.2 Workflow-level concurrency mode

```yaml
concurrency: auto   # default 'sequential'
```

- `sequential` (default) — the executor walks the topology in a
  serial worklist, identical to Phase 3.1 — Phase 3.7 behavior.
  Every existing workflow continues to run sequentially with NO
  behavior change. This is the load-bearing back-compat guarantee.
- `auto` — the executor dispatches all ready nodes concurrently.
  "Ready" means every predecessor is in `_completed_or_skipped`
  AND no in-flight predecessor will mutate state that THIS node
  reads or writes.

The run-start gate REJECTS `auto` if any pair of nodes that COULD
run concurrently (per static topology) violate one of these invariants:

- **Disjoint state writes.** Two concurrent nodes' state-write sets
  (computed transitively from `set_state` / `delete_state` keys
  declared statically) must be disjoint.
- **Read-after-write ordering preserved.** If node B's bound inputs
  Jinja-reference `state.k`, no concurrent sibling A may write to
  `state.k`. (Resolution: serialise A → B via an explicit edge.)
- **No write-after-read on the same parallel arm.** Within one
  branch of fan-out, sequential ordering inside that branch already
  enforces this — it's the cross-branch case the gate rejects.

When the gate trips, raise `WorkflowParallelStateConflict` (§16.4)
with the conflicting nodes + state key named in `identifying_names_keys`.

### 16.2.1 Auto-mode worklist algorithm

```text
ready := { n : predecessors(n) ⊆ _completed_or_skipped }
in_flight := { }

while ready ∪ in_flight is non-empty:
    for n in ready - in_flight:
        if n.touches_state:
            await state_mutation_lock.acquire()   # serialise mutators
        in_flight.add(n)
        spawn_task(_run_node(n))
    completed := await wait_first(in_flight)
    for c in completed:
        in_flight.remove(c)
        update _captured, _state, _completed_or_skipped per Phase 3.1
        if c.touches_state:
            state_mutation_lock.release()
    ready := { n : predecessors(n) ⊆ _completed_or_skipped } - in_flight
```

`touches_state` is TRUE for any node whose `type:` is
`set_state` / `delete_state` (the run-start gate already enforces
that no `auto`-mode workflow has both a state mutator and a
concurrent sibling that READS the same key — so the lock here is
defensive against MULTIPLE concurrent mutators of DISJOINT keys
racing for the JSONL emit ordering, not against semantic state
corruption). The lock guarantees the `workflow.state.mutated` event
stream is total-ordered per master spec §11.3.

### 16.2.2 Failure semantics under `auto`

The first in-flight node whose invocation raises causes the run to
abort. The executor cancels every other in-flight task via
`task.cancel()`. Cancelled tasks emit `workflow.node.failed` with
`error_class="asyncio.CancelledError"` and `reason="sibling_failure"`
in payload. The aborting node's own `workflow.node.failed` carries
the original exception per Phase 3.1's existing contract.

`cancel.flag` observation by an in-flight node propagates through
the same cancellation arm: the observing node emits `workflow.run.aborted`
(per Phase 3.1 §8.4), the executor cancels siblings, the run's
status flips to `aborted`. Sibling cancellations land their own
`workflow.node.failed` with `reason="user_cancel"` to keep the
JSONL forensics complete.

### 16.3 Map parallel iteration

```yaml
nodes:
  fan_out:
    type: kolsch/nodes/map
    apply: process_one
    over: "{{ inputs.tickets }}"
    item_as: ticket
    parallel: true              # NEW — Phase 3.10
    concurrency: 8              # NEW — optional; defaults to len(over) if unset
```

- `parallel: false` (default) — sequential iteration, identical to
  Phase 3.7's contract.
- `parallel: true` — iterations dispatch concurrently via
  `asyncio.gather`. The `results` list still preserves index order
  (results[i] is the i-th iteration's worker output, regardless of
  completion order). The map node's own `workflow.node.completed`
  fires after every iteration has either completed or been
  cancelled by a failure.
- `concurrency: N` — semaphore-bounded parallelism (at most N
  iterations in flight). Without it, parallelism is unbounded
  (every iteration dispatched at once). The run-start gate rejects
  `concurrency: <= 0` with `WorkflowInvalidConcurrency`.
- **Parallel maps MUST NOT mutate state.** The run-start gate
  rejects a `parallel: true` map whose worker (transitively)
  reaches a `set_state` / `delete_state` node with
  `WorkflowParallelStateConflict`. Sequential maps remain free to
  mutate state, matching Phase 3.7.

The map node's `workflow.node.started` payload carries a new
`concurrency_mode: "sequential" | "parallel"` field plus
`concurrency: int | null` (the bound). The per-iteration
`workflow.node.started` events continue to carry the §11.3
`parent_invocation_id` + `iteration_index` payload fields per
Phase 3.7 contract.

### 16.3.1 Failure under parallel map

First failing iteration cancels the rest. Cancelled iterations
emit `workflow.node.failed` with `error_class="asyncio.CancelledError"`
and `reason="sibling_failure"` in payload. The map node's own
`workflow.node.failed` fires last, with the FIRST exception as the
cause; cancellation order is non-deterministic for the JSONL but
the map's eventual status is deterministic ("first iteration to
raise wins"). Partial `results` are NOT persisted — same as Phase 3.7.

### 16.4 Per-run module prefix (parallel-safe imports)

Phase 3.1's module isolation snapshots `sys.modules` keys at run
start and removes new bundle entries at run end (`src/workflows/module_isolation.py`).
Under cross-run concurrency this races: two runs concurrently
loading `bundles.kolsch.nodes.transform` see the same
`sys.modules[bundles.kolsch.nodes.transform]` entry, and the loser's
top-level `import` side effects bleed into the winner's view.

Phase 3.10 ships per-run import namespacing. Bundle modules load
under a run-id-tagged key:

```text
sys.modules["_kolsch_wrk_<run_id_short>.bundles.kolsch.nodes.transform"] = <module>
```

The original `bundles.kolsch.nodes.transform` key is NEVER written
to during a run; the prefix-tagged key is what the CatalogNodeRunner
holds the reference to. Cleanup at run end iterates
`sys.modules` and deletes every entry whose key startswith
`_kolsch_wrk_<run_id_short>.` for this run's id. Two concurrent
runs each have their own copy and their own cleanup; neither sees
the other.

Implementation seam: `src/workflows/module_isolation.py` gains a
new `RunModuleNamespace` class that the CatalogNodeRunner consults
when resolving an import. The Phase 3.1 snapshot/cleanup helpers
remain for back-compat — they're a no-op when no entries match the
new prefix discipline, so existing call sites continue to work
during the migration window.

**Third-party imports remain global.** `httpx`, `pandas`,
`jinja2`, etc. continue to share `sys.modules` entries across runs
— their import cost is amortised, and they're stateful only in
ways that already work concurrently (or already broke under
sequential concurrent runs, in which case the author's problem).
The per-run namespace covers ONLY bundle code under `bundles/`.

### 16.5 Event emission under concurrency

JSONL is the source of truth for fork + resume. The replay
machinery at `src/workflows/_replay.py` reads it line-by-line; any
broken-line write under concurrent emission would corrupt every
downstream replay. Phase 3.10 protects this with one executor-private
`asyncio.Lock` (`self._emit_lock`) acquired around the file write
in `emit_event` for the duration of one event's serialisation +
append.

The lock holds only for the WRITE — not for the dispatch of the
event-producing node. Concurrent nodes contend only when two would
emit simultaneously; in steady state the lock is uncontended
because event emission is a microsecond-scale operation per node.

Event ORDERING under concurrency is the natural async-completion
order. Two concurrent nodes' events interleave based on real-time
completion; the JSONL line ordering MATCHES THE REAL-TIME EMISSION
ORDER. There is no attempt to re-order events to match topology
(that would be expensive and not actually useful — consumers
correlate by `invocation_id`, not by line number).

### 16.5.1 Replay determinism under concurrency

`_replay.py` rebuilds `_captured` / `_state` / `_pruned` /
`_completed_or_skipped` from JSONL events line-by-line. The
existing logic already handles "events for node A interleaved with
events for node B" — it's idempotent per invocation_id and accumulates
state mutations in JSONL order. Phase 3.10 verifies via test that a
concurrent-fan-out run, replayed for fork, reconstructs the same
final state as the original (modulo `_state` mutations whose order
in the JSONL is determined by real-time emission, which fork
inherits unchanged — fork replays the parent's exact ordering).

### 16.6 Cancellation semantics

`cancel.flag` polling (Phase 3.1 §8.4) extends to the concurrent
worklist:

- The worklist polls `cancel.flag` AT THE TOP of each iteration of
  the gather loop — once per "completed batch," not once per node.
- On detection: cancel every in-flight task; emit `workflow.run.aborted`
  per the existing contract; sibling cancellations emit their own
  `workflow.node.failed` with `reason="user_cancel"`.
- Mid-node observation is impossible (node code can't observe the
  flag). Long-running nodes — bash subprocesses, agent calls — are
  cancelled via `task.cancel()` which propagates through asyncio
  to the underlying I/O; bash subprocesses get SIGTERM via the
  same pathway Phase 3.1 ships for timeout.

### 16.7 Determinism guarantees

Under both `sequential` and `auto`, the engine guarantees:

- **Final captured output dict is deterministic** for the same
  workflow + same inputs + same external-tool responses. Two
  concurrent siblings can't disagree because their writes are
  disjoint by gate-enforced invariant.
- **Final `_state` dict is deterministic** because the run-start
  gate forbids the only way two concurrent writers could touch
  the same key.
- **JSONL replay is determinstic** — the same events.jsonl
  replayed twice produces the same `_captured` / `_state` /
  `_pruned` / `_completed_or_skipped`.
- **Forked runs from concurrent parents inherit the parent's
  exact replay ordering** — fork is structurally "replay parent's
  JSONL," and that JSONL captures the parent's real-time emission
  order whatever it was.

What's NOT guaranteed:

- **JSONL line order across runs.** Two `auto`-mode runs of the
  same workflow may produce events.jsonl files whose interleaving
  differs (depends on real-time scheduling). The captured outputs +
  state + terminal status match; the linear log doesn't have to.
- **`workflow.node.started`/`completed` pair adjacency.** Under
  concurrency, two nodes' started events can appear before either
  one's completed event lands. Consumers grouping by `invocation_id`
  remain correct; consumers grouping by adjacency break (and were
  already brittle).

### 16.8 New error classes

One new error class:

- **`WorkflowParallelStateConflict(WorkflowValidationError)`** —
  raised at the run-start gate when an `auto`-mode workflow has
  two parallel-eligible nodes that conflict on a state key, OR a
  `parallel: true` map whose worker transitively writes state.
  `identifying_names_keys = ("node_a", "node_b", "state_key")`.
  Caller message: "The workflow tries to run two nodes in parallel
  that fight over the same state key. Fix: add an explicit edge to
  serialise them, set `concurrency: sequential`, or remove the
  state write from one of them."

One reused error class:

- **`WorkflowInvalidConcurrency(WorkflowValidationError)`** —
  raised when a numeric `concurrency:` field on a workflow or map
  is `<= 0` or not an integer. May already exist for
  `timeout_seconds`-style validation; if so, reused. If not, ships
  alongside `WorkflowParallelStateConflict`.
  `identifying_names_keys = ("node", "value")`.

### 16.9 CLI surface

NO new CLI commands. The existing `kolsch workflows run` /
`resume` / `fork` / `events` / `cancel` work unchanged.

Run output adds one row to the terminal envelope:

```
concurrency_mode: auto
peak_in_flight: 4
```

`peak_in_flight` is the maximum number of concurrent in-flight
node invocations observed during the run — a forensics aid for
authors tuning their workflows. It's recorded on
`workflow.run.completed.payload.peak_in_flight` and surfaced in
the `text` and `json` envelopes.

### 16.10 Chat surface

NO new chat capsules or slash commands. The Phase 3.9 running-node
indicator (`▸ <label>…` per `workflow.node.started`) fires per
concurrent invocation — so an `auto`-mode fan-out renders as
multiple indicator lines in close succession, then their
completion capsules land as each finishes. No mid-stream
synchronisation; consumers see the natural interleaving.

A future phase (TBD) may layer a multi-pane progress view for
`auto`-mode runs. Phase 3.10 explicitly defers that — see §16.1
Out of scope.

---

## 16.11 Verification gate

A workflow `bundles/kolsch/workflows/parallel-fan-out-demo.yaml`
ships as the canonical Phase 3.10 example: three sibling branches,
each a small bash node, dispatched concurrently under `concurrency:
auto`. The integration test asserts wall-clock < the sum of the
sibling durations (with a generous slack) AND that the captured
outputs match the sequential variant byte-for-byte.

A second workflow `bundles/kolsch/workflows/parallel-map-demo.yaml`
exercises `parallel: true` over a 10-element list, the worker being
a small bash node that sleeps briefly to make the timing distinction
measurable.

A negative test fixture `bundles/kolsch/workflows/parallel-state-conflict.yaml`
declares two concurrent branches both writing to `state.counter`;
the run-start gate REJECTS it with `WorkflowParallelStateConflict`.

Per-run module-isolation safety: a regression test loads the same
bundle module from two concurrent runs and asserts that each run's
module instance is distinct (`id(mod_a) != id(mod_b)`), AND that
cleanup at run end removes only this run's prefix-tagged entries.

---

## 16.12 Migration guidance for existing workflows

Every existing workflow is unaffected. Default `concurrency:
sequential` keeps Phase 3.1 — Phase 3.7 behavior unchanged.
Authors who want concurrency opt in explicitly. The run-start gate
gives them a clear error if they opt in without doing the state
analysis — so the migration path is "set `concurrency: auto`, run
the validator, fix the gate trips, done."

The per-run module prefix is a transparent rewrite — author code
sees the same module objects through the CatalogNodeRunner; only
the `sys.modules` key changes. A workflow that previously worked
under sequential isolation continues to work under per-run prefix
isolation.

---
