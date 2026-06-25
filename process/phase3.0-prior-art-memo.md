# Phase 3.0 — Workflow prior-art research memo

**Draft 0.1** *(research output, not a spec. Surveys five systems (Temporal, Argo Workflows, GitHub Actions, CrewAI Flow, Jupyter) against the four high-priority open questions from the design conversation: conditional execution syntax, loop semantics, trigger model, fork-from-event UX. Synthesizes recommendations for the eventual Phase 3.0 spec.)*

> Companion to the Phase 3.0 design conversation. Eight foundational
> decisions are already locked from that conversation; this memo's
> job is to inform the four remaining high-priority decisions
> (conditionals, loops, triggers, fork UX) using the most relevant
> existing systems as evidence.

---

## Part 0 — What's already decided

These eight design choices are locked from the conversation. The
memo's recommendations should NOT contradict them; they're the
shape we're building around.

1. **YAML-first authoring** with two sections: `nodes:` + `graph:`.
2. **Function-call semantics** for node reuse — each graph reference
   creates a fresh invocation.
3. **Explicit edge binding** via `binds:` block (key = target's input
   name, value = source's output name).
4. **Output aliasing** at the workflow-node level: built-ins expose
   raw names (e.g., bash's `stdout`); authors optionally alias to
   semantic names per-workflow.
5. **`workflow_run`** as a new entity type, child of `session`. Chats
   stay as threads at v1; unify under workflow model later.
6. **Fork = copy-up-to-point-then-continue**, producing a new
   `workflow_run` with a `forked_from: { parent_run_id, fork_event_id }`
   field. Original runs are immutable.
7. **Sequential execution at v1**; the graph topology supports
   fan-out/fan-in for v2+. Don't paint into a corner.
8. **Workflows compose agents**, not vice versa. Agents stay as
   leaf assets; workflows orchestrate them plus non-agent nodes.

Trigger model also locked from this conversation: **CLI + chat slash
in v1.** Both surfaces hit the same engine entry-points.

---

## Part 1 — Questions this memo targets

The four high-priority open questions from the conversation:

### Q1. Conditional execution — per-edge or per-node?

Two camps:

- **Per-edge `if:`** — the edge declares a guard that decides whether
  the data flows along it. Purer graph semantics; conditions live
  with the routing.
- **Per-node `if:`** — the node declares whether it executes. Simpler
  in YAML; couples conditions to specific nodes.

### Q2. Loop semantics

The conversation tagged this "need more investigation." Three
shapes considered:

- **`for-each` (map) over a list** — safest; most common pattern.
- **Bounded `while`** — looping until a condition with a max-iteration
  cap. Useful but easy to misuse.
- **Per-node retry policy** — looping a single node until it succeeds.

### Q3. Trigger model details

CLI + chat at v1 is locked. The memo addresses:

- What does the CLI command shape look like (`kolsch workflow run`,
  arguments, output)?
- What does the chat slash command look like (`/workflow <ref>`,
  argument passing)?
- Are there triggers we DON'T want at v1 (scheduled, event-driven)?

### Q4. Fork-from-event UX

Locked: fork creates a new run. The memo addresses:

- What CLI surface (`kolsch workflow fork <run_id> --at <event_id>`)?
- What chat surface (`/fork <event_id>`)?
- How does the user pick the fork point — UI? Inspecting JSONL?
- How does the user inject the modified input at the fork point?

---

## Part 2 — Per-system findings

### 2.1 Temporal — fork-from-event's closest analog

**What it is**: a durable workflow engine. You write workflow code
in a host language (Python, Go, Java); Temporal runs it with strong
durability guarantees and deterministic replay.

**Why it matters here**: Temporal has the most rigorous version of
the same "fork-from-event" problem we're solving. Their solution
informs our discipline.

**Key concepts**:

- **Deterministic replay**: when a worker restarts, the engine
  replays the event history to reconstruct the workflow's exact
  state. For this to work, workflow code must be deterministic —
  no `time.time()`, no random.random(), no direct I/O. All
  non-deterministic operations happen in *activities* (atomic
  units invoked from the workflow); the activity's input + result
  are captured in events.
- **Activities** vs. **workflow code**: workflows are pure
  orchestration; activities are the side-effecting work. This is
  the discipline that makes replay safe.
- **Signals** + **queries**: external events delivered to a running
  workflow; queries read live state without mutation. Both are
  recorded in event history.
- **Continue-as-new**: when an event history grows too large, the
  workflow can spawn a new execution with the same workflow ID,
  copying current state forward. Adjacent to our fork concept,
  though different (Temporal's continue-as-new ends the parent;
  our fork preserves it).

**What to borrow**:

- **The activity / orchestration split.** Translates directly: in
  kolsch terms, *nodes* are activities (their input/output is
  captured in events; they can be non-deterministic — LLM calls,
  bash, HTTP); *graph traversal* is orchestration (deterministic
  given the node outputs).
- **The discipline that workflow control flow is determined by
  events.** Means our graph executor reads "what edges have been
  taken so far" from the event log; doesn't recompute from
  scratch.
- **Captured event = source of truth.** When forking, we don't
  re-run upstream nodes; we copy their captured outputs forward and
  run only downstream nodes from the fork point.

**What to avoid**:

- Temporal's full deterministic-code discipline. We're YAML-first;
  control flow is declarative; we don't have arbitrary host code in
  the orchestration layer. So we get determinism by construction.
- Temporal's complexity. Their full feature surface (versioning,
  child workflows, signal handlers, activity heartbeats) is huge.
  Phase 3.0 needs maybe 20% of that.

### 2.2 Argo Workflows — the closest YAML-DAG analog

**What it is**: a Kubernetes-native workflow engine; workflows are
YAML resources describing a DAG of containerized steps.

**Why it matters here**: closest existing YAML schema for workflow
DAGs. Their primitives have been refined over years; mine for
syntax.

**Key concepts**:

```yaml
spec:
  entrypoint: my-flow
  templates:
    - name: my-flow
      dag:
        tasks:
          - name: A
            template: produce-data
          - name: B
            template: process-data
            dependencies: [A]
            arguments:
              parameters:
                - name: input
                  value: "{{tasks.A.outputs.result}}"

    - name: produce-data
      script:
        image: python:3.10
        command: [python]
        source: |
          import json
          print(json.dumps({"value": 42}))
      outputs:
        parameters:
          - name: result
            valueFrom:
              path: /tmp/result.txt
```

- **`templates:` block** = node-type definitions (similar to our
  `nodes:` section). Each template declares inputs and outputs.
- **`dag.tasks:`** = the graph, with `dependencies:` for ordering
  and `arguments:` for binding (similar to our `binds:`).
- **`withItems:`** = fan-out over a list (for-each):
  ```yaml
  - name: process-each
    template: handle-item
    withItems: ["a", "b", "c"]
    arguments:
      parameters: [{name: item, value: "{{item}}"}]
  ```
- **`when:`** = per-task conditional:
  ```yaml
  - name: cleanup
    template: clean
    dependencies: [main]
    when: "{{tasks.main.outputs.result.failed}} == true"
  ```
- **`retryStrategy:`** = per-template retry policy with
  `limit`, `retryPolicy: OnFailure`, exponential backoff.

**What to borrow**:

- **The `dag.tasks:` + `dependencies:` shape.** Closest direct match
  to our `graph: edges:` form.
- **`withItems:`** for `for-each` loops. Their syntax is clean.
- **`when:` clause on tasks** for conditional execution. Per-task
  rather than per-edge.
- **`outputs:` declarations at the template level.** Matches our
  "nodes declare their typed contracts" decision.

**What to avoid**:

- The Kubernetes-resource shape. We're not on K8s; our YAML doesn't
  need `apiVersion`, `kind`, `metadata.namespace`, etc.
- Argo's `script:` blocks for inline code. Too much surface area;
  Python nodes via referenced `.py` files are cleaner for us.
- `{{tasks.A.outputs.result}}` expression syntax. Our `binds:` is
  more direct; reserve expressions for filter / projection later.

### 2.3 GitHub Actions — battle-tested conditional grammar

**What it is**: CI/CD platform; workflows are YAML files in
`.github/workflows/`. Jobs + steps; conditional + matrix patterns.

**Why it matters here**: the most widely-used YAML workflow grammar
on the planet. Their `if:`, `needs:`, `matrix:`, and `outputs:`
syntax patterns are familiar to millions of users.

**Key concepts**:

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      version: ${{ steps.set-version.outputs.version }}
    steps:
      - id: set-version
        run: echo "version=1.2.3" >> $GITHUB_OUTPUT

  test:
    needs: build
    if: needs.build.outputs.version != ''
    strategy:
      matrix:
        python: ["3.10", "3.11", "3.12"]
    steps:
      - run: pytest --python=${{ matrix.python }}
```

- **`jobs:`** = top-level units, with `steps:` inside. Each job is
  like a sub-workflow.
- **`needs:`** = explicit dependencies (closest to our edges).
- **`if:`** = per-job AND per-step conditional, using a small
  expression language (`needs.X.outputs.Y != ''`,
  `success() || failure()`, etc.).
- **`outputs:`** at job level — declares what the job exposes for
  downstream jobs to read.
- **`matrix:`** = fan-out over a parameter grid (multi-dimensional
  for-each).
- **`continue-on-error:`** = per-step toggle for "tolerate failure
  and continue."

**What to borrow**:

- **`if:` at multiple levels.** Per-step AND per-job; both useful.
  Suggests we support `if:` per-node AND per-edge, not pick one.
- **`needs:` semantics for ordering** (though we'll spell it as
  edges, not as a separate `needs:` block).
- **`matrix:` as a fan-out primitive.** When v2 adds parallelism,
  matrix is the natural form.
- **Functions in expressions**: `success()`, `failure()`,
  `always()`, `cancelled()`. Small, well-defined set; covers most
  conditional needs.

**What to avoid**:

- GitHub Actions' expression language complexity. Templates with
  `${{ }}` interpolation, `contains()`, `startsWith()`, etc. become
  a programming language by stealth. Keep our expression surface
  tight — strictly comparisons + boolean ops + a handful of
  functions.
- The job/step distinction. We have nodes; we don't need a separate
  step level for v1.

### 2.4 CrewAI Flow — confirming we don't adopt

**What it is**: CrewAI's state-machine API for orchestrating crews +
agents + arbitrary Python methods. Decorator-driven.

**Verified in the venv at**
`crewai/flow/flow.py`. Public surface:

```python
class MyFlow(Flow[StateModel]):
    @start()
    def kickoff(self):
        self.state.value = "starting"
        return "initialized"

    @listen("kickoff")
    def first_step(self, result):
        ...
        return "first done"

    @listen(or_("first_step", "alt_path"))
    def merge(self, result):
        ...

    @router("merge")
    def route(self, result):
        if self.state.value > 10:
            return "high_path"
        return "low_path"
```

- **`@start`** / **`@listen`** / **`@router`** decorators define
  nodes + edges.
- **`or_()` / `and_()`** combinators express OR / AND of upstream
  conditions.
- **Shared `state`** model — typed via `Flow[StateModel]`.
- **`@persist`** decorator for durability.

**Why we don't adopt**:

- **Python-decorator-driven, not YAML.** Locks us to code-first
  authoring; contradicts our locked Decision 1.
- **Tightly coupled to CrewAI's Crew/Agent abstractions.** We
  already bypass most of CrewAI's machinery via `_CrewAILLM`;
  adopting Flow would re-couple.
- **Replay model is mutable shared state** — our fork-from-event
  needs captured-events-as-source-of-truth, not a global state
  object.

**What to borrow** (small):

- The `or_()` / `and_()` combinator idea. If we want compound
  conditionals at v1, `any_of:` / `all_of:` YAML forms are
  cleaner than free-form expression grammar.

That's it. CrewAI Flow informs the negative case (what we don't
build) more than the positive.

### 2.5 Jupyter — fork-from-cell as a UX precedent

**What it is**: notebook environments where users execute code cells
in arbitrary order, with state persisting between executions.

**Why it matters here**: the closest existing user mental model for
"fork from this point with different inputs and see what changes."
Jupyter's "Restart kernel and run all up to selected cell"
operation is exactly the workflow fork operation.

**Key concepts**:

- **Cells as discrete execution units.** Each cell's inputs/outputs
  are captured; rerunning from a midpoint requires upstream state
  to still be in memory (the kernel's local namespace).
- **"Run all above" / "Run all below"** — direct UX precedents for
  "fork from this point."
- **Output cells store the result of execution.** Notebooks-as-
  documents include the executed state; reload preserves it.
- **No re-runnable cells without re-executing predecessors** — this
  is Jupyter's weakness; users have to remember kernel state.

**What to borrow**:

- **The UX precedent.** Users coming from Jupyter understand
  "execute up to here, then continue with a modification" — we don't
  have to teach the concept, just connect it to our entity model.
- **The output-on-the-cell pattern.** Equivalent to our captured
  events per node invocation.

**What to AVOID**:

- Jupyter's stateful-kernel anti-pattern. State should live in the
  event log, not in a process-local namespace. Re-running a node
  shouldn't depend on what other nodes happen to have run in the
  same kernel session.

---

## Part 3 — Cross-cutting comparison

A side-by-side on the four target questions.

### 3.1 Conditional execution

| System | Where `if:` lives | Expression syntax | Notes |
|---|---|---|---|
| Temporal | In host code (if/else) | Host language | No special syntax — it's Python/Go |
| Argo | Per-task `when:` | Limited template grammar | `{{tasks.X.outputs.Y}} == "..."` |
| GH Actions | Per-job AND per-step `if:` | Expression language with functions | Most expressive; `success()`/`failure()` helpers |
| CrewAI Flow | Decorator args + host code | Python | Tight to language |
| Jupyter | Cell code | Host language | Not relevant — runs all cells per execution |

**Pattern**: every system with a YAML surface puts `if:` per-node
(or per-task). Per-edge `if:` exists in some research literature
but isn't common in shipped systems. Argo's `when:` is the closest
to per-edge but is actually attached to the task (which receives
the edge).

### 3.2 Loop semantics

| System | for-each | while | per-node retry |
|---|---|---|---|
| Temporal | Host language loops in workflow code | Yes (with care for determinism) | Activity-level via retryPolicy |
| Argo | `withItems:` | Workflow-level loops via recursive templates (awkward) | `retryStrategy:` per-template |
| GH Actions | `matrix:` (single-dim) + `strategy.matrix` (multi-dim) | No native while loop | `continue-on-error:` + custom retry steps |
| CrewAI Flow | None native; use host loops | Same | None native |
| Jupyter | Cells with for-loops | Cells with while-loops | None native |

**Pattern**: `for-each` is universal and well-supported. `while` is
rare in shipped systems — when it exists, it's by host-code recursion
(Argo) or by being a runtime language (Temporal). Per-node retry is
universal.

### 3.3 Trigger model

| System | CLI | Programmatic | Schedule | Event |
|---|---|---|---|---|
| Temporal | `temporal workflow start` | SDK | Cron (via Scheduler) | Signals |
| Argo | `argo submit` | K8s API | CronWorkflow CRD | Sensor (events) |
| GH Actions | manual via UI/`gh` | API | `schedule:` cron | `on:` event triggers |
| CrewAI Flow | Python `flow.kickoff()` | SDK | None native | None native |
| Jupyter | Run cell | API | nbconvert + cron (external) | None native |

**Pattern**: every system has at minimum a manual trigger
(CLI/SDK/UI). Schedules and event triggers are common in
production-oriented systems (Temporal, Argo, GH Actions) but absent
from notebook-style or library-style systems (CrewAI Flow,
Jupyter). For v1, CLI + chat covers manual entirely; scheduled and
event triggers are future scope.

### 3.4 Fork-from-event

| System | Native support? | Mechanism |
|---|---|---|
| Temporal | Limited — "continue-as-new" copies state forward but ends the parent | Workflow ID inherited, history reset |
| Argo | No native fork | Re-submit workflow with new parameters (no state inheritance) |
| GH Actions | No native fork | Re-run job (uses fresh inputs, not from-point continuation) |
| CrewAI Flow | No native fork | Re-instantiate Flow with new state |
| Jupyter | Closest analog | "Restart & run all up to selected cell" — mutates kernel, not a new run |

**Pattern**: **fork-from-event with state inheritance and original
preservation is uncommon.** The closest is Jupyter's mental model
(re-run up to here) but Jupyter mutates the kernel rather than
forking. **The kolsch model — original run preserved, fork creates a
new run with copied upstream state — is genuinely novel territory.**
Means we can't lift the design from any single system; we have to
synthesize.

The closest practical analog: **git branches**. Original branch
preserved; new branch starts from a commit; subsequent commits
diverge.

---

## Part 4 — Synthesis: recommendations on the four open questions

### Q1. Conditional execution syntax

**Recommended: per-node `if:` only at v1.** Per-edge is purer but
shipped systems converge on per-node. Author the `if:` at the node
declaration:

```yaml
nodes:
  cleanup:
    type: bash
    command: "rm -rf /tmp/work"
    if: "${{ build.outputs.failed }}"
```

Expression grammar: **strictly comparisons + boolean ops + a
small function library**:

- Comparisons: `==`, `!=`, `<`, `>`, `<=`, `>=`
- Boolean: `&&`, `||`, `!`
- Functions: `success()`, `failure()`, `always()`, `cancelled()`
  (referring to the workflow's overall state)
- Reference syntax: `${{ <node_name>.outputs.<output_name> }}`

Borrowed from GitHub Actions. No string concatenation, no
arithmetic, no function calls beyond the four. Keep it boring.

Per-edge `if:` deferred to v2+ if a real use case surfaces (e.g.,
routing the same source's output to different targets based on
content).

### Q2. Loop semantics

**Recommended at v1**:

- **`map:` node** for `for-each`. Argo's `withItems:` shape:

```yaml
nodes:
  process_each:
    type: map
    over: "${{ fetch_list.outputs.items }}"
    body:
      type: agent
      agent: kolsch/processor
```

Sequential execution at v1 (each item processed in order); v2
parallelizes.

- **Per-node `retry:` policy** for "run this node until it
  succeeds":

```yaml
nodes:
  fetch_data:
    type: bash
    command: "curl https://api.example.com/data"
    retry:
      max_attempts: 3
      on: failure   # only retry on non-zero exit
```

**Deferred from v1**:

- General `while` loops. No shipped system makes them clean in
  YAML; Argo's recursive templates are clunky. If real use cases
  surface, v2 can add a bounded `while:` with mandatory
  `max_iterations:`.
- Map with reducer (the classic map-reduce). Phase 3.3-ish; for
  v1 a downstream node fans the map's outputs in via fan-in
  edges.

### Q3. Trigger model details

**CLI surface at v1**, mirroring `kolsch skills`:

```bash
kolsch workflows list
kolsch workflows show <ref>
kolsch workflows validate <ref>
kolsch workflows run <ref> [--input key=value]...
kolsch workflows fork <run_id> --at <event_id> [--input key=value]...
```

Output: prints a `workflow_run_id` on `run` and `fork`. JSONL
event stream optionally tail-followable with `--follow`.

**Chat surface at v1**:

```
/workflow <ref>                  # run a workflow from chat
/workflow <ref> with key=value   # with inputs
/fork <event_id>                 # fork the current chat or workflow at event
```

Both surfaces hit the same engine entrypoint (`workflows.run` /
`workflows.fork`). The CLI surface is the canonical one; the chat
slashes are thin shims that publish the same dispatch.

**Out of scope for v1** (filed as future qol-* docs if not picked
up earlier):

- **Scheduled triggers** (cron). Future qol once `kolsch workflows`
  is stable.
- **Event triggers** (file-watch, webhook, MCP event). Future
  qol; the platform's event-bus seam is the natural home.
- **Workflow chaining** (workflow A triggers workflow B on
  completion). Probably solved as sub-workflows + an `on_complete`
  edge type.

### Q4. Fork-from-event UX

**At the entity layer**:

Each `workflow_run` carries:

```
workflow_run {
    id: str
    workflow_ref: str
    session_id: str | None         # absent for scheduled/event-triggered runs
    forked_from: { run_id: str, fork_event_id: str } | None
    started_at, ended_at, status, ...
}
```

Original runs have `forked_from: null`. Forked runs point at their
parent + the fork point. The forest structure is queryable.

**At the CLI**:

```bash
kolsch workflows fork <run_id> --at <event_id> [--input key=value]...
```

`<event_id>` must be an event from `<run_id>`'s log. Engine:

1. Reads the parent run's event log up to (and including)
   `<event_id>`.
2. Constructs a new `workflow_run` with copied state — every node
   invocation captured BEFORE the fork point has its outputs
   preserved; nodes downstream of the fork point are not yet
   executed.
3. Applies any `--input key=value` overrides (each override replaces
   the captured output of a node before the fork point — used to
   inject "what if I had answered differently at the user_input
   node").
4. Begins execution from the fork point with the modified state.

**At the chat**:

```
/fork <event_id> [with key=value]
```

The user sees the current run's event timeline (already rendered
by the chat capsules); they pick an event by ID. The fork
spawns a NEW chat thread (or workflow_run; same shape post-unification)
and continues from there. The original session remains intact.

**UX for "picking an event"**:

- The event_id is visible in the rendered chat (each capsule
  shows its event_id in a small subscript or on hover).
- `kolsch sessions events <run_id>` prints the event stream with
  IDs.
- Future qol: a `/timeline` chat command renders an interactive
  picker.

---

## Part 5 — What to borrow, invent, avoid

### Borrow

| From | What |
|---|---|
| Temporal | The "activities are the side-effecting work; orchestration is deterministic by construction" discipline |
| Argo | `dag.tasks:` + `dependencies:` shape; `withItems:` for-each; `when:` (renamed `if:`); `retryStrategy:` (renamed `retry:`) |
| GH Actions | `if:` per-node; `outputs:` at the node level; `success()`/`failure()`/`always()`/`cancelled()` functions |
| Jupyter | "Fork-from-here" UX mental model |
| CrewAI Flow | The negative case (what NOT to build) |

### Invent (no good prior art)

| What | Why |
|---|---|
| **Fork-as-new-run with copied state** | No shipped system does this cleanly; we're closest to git-branch semantics |
| **`workflow_run` as a new entity** | Most workflow systems don't separate "the recipe" from "an execution"; we need to because of fork |
| **Chat-side `/workflow` + `/fork` slashes** | Workflow systems generally aren't conversational; this seam is unique to kolsch |
| **`binds:` syntax with key=target / value=source** | Most systems use string interpolation (`${{ ... }}`); we want explicit names because of fork-from-event captured-state reconstruction |

### Avoid

| Pattern | Why |
|---|---|
| Decorator-driven authoring (CrewAI Flow, Temporal) | Contradicts YAML-first |
| String interpolation everywhere (Argo, GH Actions) | Becomes a language by stealth; we want declarative |
| Mutable shared state (CrewAI Flow's `state`) | Breaks fork-from-event captured-state reasoning |
| Kubernetes-resource shape (Argo) | Not on K8s; adds noise |
| Inline code blocks (Argo `script:`) | Python nodes via referenced `.py` files are cleaner |
| `while` loops at v1 | No clean YAML form exists in shipped systems |

---

## Part 6 — Open questions still NOT resolved by the memo

These came up during the research and deserve their own
conversation before drafting the spec.

1. **Workflow inputs at run-time**: how does the user provide
   inputs to a workflow on `kolsch workflows run`? We have a YAML
   IOSpec-like declaration on the workflow itself; `--input
   key=value` at the CLI. But how do chat slashes pass
   complex inputs (dicts, lists)? Probably JSON-on-the-line for v1.

2. **Workflow asset extends:** — can a workflow `extends:` another
   workflow? My instinct: no for v1. Composition via sub-workflow
   nodes is enough. Confirm.

3. **Workflow run lifetime**: how long does a `workflow_run` retain
   state for fork operations? Forever (matching session JSONL
   persistence)? Or with a TTL? Probably forever for v1, matching
   session retention; revisit when sessions get garbage-collected.

4. **Node body for Python nodes**: inline in YAML (Argo-style
   `script:`) or referenced via a Python file in
   `bundles/<bundle>/workflows/nodes/<name>.py`? Latter is cleaner
   and matches how tools work (`bundles/<bundle>/tools/<name>.py`).
   Probably referenced files.

5. **Bash node sandboxing**: re-uses the existing bash-tool sandbox?
   Or workflows get their own? Probably re-use; the tool's authz
   model is already wired to permissions and sandbox.

6. **Multi-input fan-in synchronization**: how does the engine
   decide when to fire a node with multiple incoming edges? Wait
   for all? Wait for any (race)? Wait for some quorum?

7. **`max_runtime:`** at the workflow level: do we cap workflow
   runs at a maximum duration? `runtime.timeout_seconds` exists on
   agents; extends naturally to workflows.

8. **Error propagation**: when a node fails (its `if:` evaluates
   false vs. it raises), does the workflow continue or abort? Per-
   node `on_error: { continue | abort | route_to: <node> }`?

---

## Part 7 — Recommended spec structure for Phase 3.0

Based on this research, the Phase 3.0 spec should have:

1. **Decisions locked** — the 8 from the conversation + the 4 from
   this memo's recommendations = 12 locked.
2. **Asset type registration** — `workflows` joining `agents`,
   `tools`, `skills` in the catalog.
3. **Schema** — `workflow.yaml` form with `nodes:`, `graph:`, and
   the workflow-level IOSpec.
4. **Node types**:
   - `bash`, `python`, `agent`, `user_input`, `conditional`, `map`,
     `sub-workflow`.
   - Per-type contract (inputs/outputs); aliasing convention.
5. **Edges**: `binds:` shape; load-time validation rules.
6. **Engine**:
   - Workflow run entity + JSONL events.
   - Sequential executor with fan-out/fan-in support.
   - Fork operation.
7. **CLI surface**: `kolsch workflows {list, show, run, fork, validate}`.
8. **Chat surface**: `/workflow`, `/fork` slashes.
9. **Migration story**: zero — workflows are new; existing chats
   stay as threads.
10. **Tests**: corpus of workflows exercising every node type, fan-
    out, fan-in, conditional, map, fork, sub-workflow.
11. **Docs**: `docs/how-to/authoring-a-workflow.md`,
    `docs/reference/schemas.md` updated, `docs/explanation/workflows-contract.md`
    as the formal contract doc.

Spec will probably need 4-5 sub-phases:

- **3.0** — asset type + parser + load-time validation (no
  execution).
- **3.1** — sequential executor + the core node types (bash,
  python, agent, user_input, conditional, map).
- **3.2** — fork-from-event mechanism (depends on 3.1 capturing
  per-node state in events).
- **3.3** — chat + CLI surfaces.
- **3.4** — sub-workflows + workflow IOSpec composition.
- **3.5** — multi-thread engine support (the long-standing roadmap
  item).
- **3.6** — `author_workflow` meta-agent (depends on everything
  above being stable).

---

## Part 8 — Where this leaves us

The four high-priority questions have memo-recommended answers:

- **Q1 conditional**: per-node `if:` only at v1; GH-Actions-style
  expression grammar (kept boring).
- **Q2 loops**: `map:` node for for-each; per-node `retry:` for
  retries; no `while` at v1.
- **Q3 triggers**: CLI + chat slashes; both hit the same engine
  entrypoint; scheduled/event triggers are future.
- **Q4 fork UX**: CLI `kolsch workflows fork <run_id> --at
  <event_id>`; chat `/fork <event_id>`; both spawn a new
  `workflow_run` with copied upstream state and original
  preserved.

Eight remaining open questions (Part 6) for the next round of
design conversation.

**Recommended next move**: a focused design conversation on the
Part 6 questions; then draft the Phase 3.0 spec proper, structured
per Part 7.
