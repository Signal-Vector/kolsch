# Workflows — author reference

This is the day-to-day reference for authoring kolsch workflows. Error messages from the validator and runtime link directly to the anchored sections below. For the full design specification, see [`process/phase3.0-workflow-spec.md`](../process/phase3.0-workflow-spec.md).

A workflow is a YAML-declared DAG of nodes that compose tools, agents, sub-workflows, and authored Python into reproducible, replayable executions. Each workflow lives at `bundles/<bundle>/workflows/<name>.yaml`; each authored Python node lives at `bundles/<bundle>/nodes/<name>.py`. Workflows ship alongside agents, tools, and skills as first-class catalog assets.

This doc is organized in four quadrants, [Diataxis](https://diataxis.fr/)-style:

- **[Tutorial](#tutorial)** — learn workflows by building one, five nodes at a time.
- **[How-to](#how-to)** — task-shaped recipes for common shapes (testing a node, eval over a test set, OR fan-in, structured LLM output).
- **[Reference](#reference)** — the anchored sections. Error messages deep-link here.
- **[Explanation](#explanation)** — the "why" behind Jinja, fan-in, and policy.

---

## Glossary

A handful of words get used precisely throughout this doc. When you see them bare (no qualifier), this is what they mean.

| Term | Meaning |
|---|---|
| node | An entry in a workflow's `nodes:` mapping. The default reading of "node" everywhere unless qualified. |
| node type | The catalog citizen, addressable via a 3-segment ref (e.g., `kolsch/nodes/bash`). The thing a node `type:` field points at. |
| captured output | The value addressable via `nodes.<name>` in Jinja expressions — what a node produced when it ran. |
| invocation | The runtime fire of a node. Each invocation owns a unique `invocation_id` recorded in the event log. |

When prose says just "node," it means an entry in the workflow's `nodes:` map. References to the catalog-level thing always say "node type."

---

# Tutorial

## Build a 5-node triage workflow

We're going to build a triage workflow one node at a time. Each step adds exactly one concept. By step 5 you have a runnable workflow that fetches a ticket, classifies it, tracks state, and branches on the result.

### Step 1 — a single bash node

The smallest possible workflow: one node, no edges, no inputs.

```yaml
description: greet the world
nodes:
  greet:
    type: kolsch/nodes/bash
    command: "echo hello world"
graph:
  edges: []
```

```
$ kolsch workflows run mybundle/workflows/triage
hello world
```

The workflow's output is the bash node's captured output: `{stdout: "hello world\n", stderr: "", exit_code: 0}`.

### Step 2 — add an edge with binds

Add a second node and an edge. The edge's `binds:` map data from the source to the receiver's `inputs.*` namespace.

```yaml
description: greet and log
nodes:
  greet:
    type: kolsch/nodes/bash
    command: "echo hello world"
  log_it:
    type: kolsch/nodes/bash
    command: "echo {{ inputs.greeting | quote }} >> greetings.log"

graph:
  edges:
    - from: greet
      to: log_it
      binds:
        greeting: "{{ nodes.greet.stdout }}"
```

New concept: `binds:` projects the source's output onto a name in the receiver's `inputs.*` namespace. The receiver references `inputs.greeting` directly in its Jinja config fields. See [`#binds-rhs`](#binds-rhs).

### Step 3 — add state

Workflow state is a flat key/value map you mutate explicitly via `set_state`. Use YAML literals directly — strings, ints, lists, bools.

```yaml
description: greet, log, track counter
nodes:
  greet:
    type: kolsch/nodes/bash
    command: "echo hello world"
  log_it:
    type: kolsch/nodes/bash
    command: "echo {{ inputs.greeting | quote }} >> greetings.log"
  bump:
    type: kolsch/nodes/set_state
    keys:
      last_greeting: "{{ nodes.greet.stdout | trim }}"
      status: greeted
      attempts: 0

graph:
  edges:
    - from: greet
      to: log_it
      binds:
        greeting: "{{ nodes.greet.stdout }}"
    - from: log_it
      to: bump
```

New concept: `set_state` mutates state. Each value is a Jinja-evaluated config field — it accepts a YAML literal (`greeted`, `0`) directly, or a `{{ }}` expression for Jinja evaluation. No quote-the-string-then-wrap-in-Jinja gymnastics. See [`#node-types`](#node-types) under State.

### Step 4 — add an agent node

Agents are first-class assets in the catalog. The `agent` node invokes one by 3-segment ref.

```yaml
description: triage one ticket
nodes:
  fetch:
    type: kolsch/nodes/bash
    command: "cat ticket.json"
  classify:
    type: kolsch/nodes/agent
    agent: mybundle/agents/triage_classifier
    description: "classify a single ticket"
  bump:
    type: kolsch/nodes/set_state
    keys:
      last_label: "{{ nodes.classify.label }}"
      status: classified

graph:
  edges:
    - from: fetch
      to: classify
      binds:
        ticket: "{{ nodes.fetch.stdout }}"
    - from: classify
      to: bump
```

New concept: the agent reads its `inputs.ticket` per the agent's declared input contract. The captured output (`nodes.classify.*`) is whatever the agent declares as its output schema. The workflow doesn't override the agent's model, prompt, or system prompt — those are agent identity, defined at the agent asset.

### Step 5 — add a branch node

Branch on the classifier's result; route urgent tickets to one path and the rest to another.

```yaml
description: triage one ticket, branch on urgency
nodes:
  fetch:
    type: kolsch/nodes/bash
    command: "cat ticket.json"

  classify:
    type: kolsch/nodes/agent
    agent: mybundle/agents/triage_classifier

  decide:
    type: kolsch/nodes/branch
    condition: "{{ nodes.classify.is_urgent }}"

  escalate:
    type: kolsch/nodes/agent
    agent: mybundle/agents/escalator
    description: "page on-call"

  queue:
    type: kolsch/nodes/bash
    command: "echo {{ nodes.classify.id | quote }} >> queue.txt"
    description: "queue for normal handling"

graph:
  edges:
    - from: fetch
      to: classify
      binds: { ticket: "{{ nodes.fetch.stdout }}" }
    - from: classify
      to: decide
    - from: decide
      to: escalate
      branch: true
    - from: decide
      to: queue
      branch: false
```

New concept: the branch node evaluates a boolean and signals which downstream edges fire. Both arms (`branch: true` and `branch: false`) MUST be present. See [`#branch`](#branch).

That's the full shape. From here:

- More elaborate fan-out → [`#map`](#map)
- Reusing nodes across workflows → [`#extends`](#extends)
- Authored Python nodes — see the [How-to](#how-to) recipes.

---

# How-to

Task-shaped recipes. Each one solves a specific problem you've probably already run into.

## Testing your authored `@node`

Authored Python `@node` functions take an `inputs` Pydantic model and a `NodeContext`. To unit-test one, use the `NodeContext.for_test(**overrides)` classmethod. It supplies sensible defaults for every field; override only the ones your test actually cares about (typically `secrets`, `state`, and `emit_fn` when you want to capture emitted events).

```python
# tests/test_enrich.py
from types import MappingProxyType
from workflows import NodeContext
from bundles.mybundle.nodes.enrich import run, EnrichInput

def test_enrich_happy_path():
    emitted = []
    ctx = NodeContext.for_test(
        bundle="mybundle",
        secrets={"CRM_API_KEY": "fake_test_key"},
        emit_fn=lambda subkind, payload: emitted.append((subkind, payload)),
    )
    inputs = EnrichInput(lead_id="L_42")

    output = run(inputs, ctx)

    assert output.enriched is True
    assert ("crm_lookup_completed", {"lead_id": "L_42"}) in emitted
```

Want to feed state into the node under test? Pass `state=`:

```python
def test_enrich_reads_state():
    ctx = NodeContext.for_test(
        bundle="mybundle",
        state={"region": "us-east-1"},
        secrets={"CRM_API_KEY": "fake_test_key"},
    )
    # node code reads ctx.state["region"] (read-only Mapping)
    ...
```

`NodeContext.for_test()` accepts keyword overrides for any of the 7 public fields:

| Override | Default | When to set it |
|---|---|---|
| `workflow_run_id` | `"wrk_test"` | Only if your assertions inspect the value. |
| `invocation_id` | `"inv_test"` | Only if your assertions inspect the value. |
| `bundle` | `"test"` | Set to the bundle your node lives under (drives the wire-kind prefix on emitted events). |
| `workspace_root` | a fresh `tempfile.mkdtemp()` | Set to a `tmp_path` fixture if you want pytest-managed cleanup. |
| `log` | a no-op `StructLogger` | Set to a recording logger if you assert on log calls. |
| `emit_fn` | `lambda *_: None` | Set to a recorder to assert on `ctx.emit(...)` calls. |
| `secrets` | `{}` | Set to a dict of test secret values your node reads via `ctx.secrets[...]`. |
| `state` | `{}` | Set to a dict your node reads via `ctx.state[...]` (read-only, see [`#node-types`](#node-types) under State). |

Assert on the function's return value and on the recorder's contents. No engine setup, no event-log writing, no fixtures beyond what your node specifically needs.

The fields `bundle` and `emit_fn` are the public test seams: `ctx.bundle` is the bundle string the engine sets in production; `ctx.emit_fn` is the callable backing `ctx.emit()`. Earlier drafts spelled these `ctx._bundle` and `ctx._emit_fn` — the underscores are gone, both are public.

## Calling an LLM with structured output

Use an `agent` node with the `outputs:` override. The override shapes the workflow's consumption of the agent's response — it's a workflow-side concern, not agent identity, so it's the one exempt override.

```yaml
nodes:
  classify:
    type: kolsch/nodes/agent
    agent: mybundle/agents/triage_classifier
    outputs:
      label: str
      is_urgent: bool
      confidence: float
```

`outputs:` uses the IOSpec grammar. The agent's response is structured against this schema (via CrewAI's structured-output mechanism). All other agent identity fields — model, prompt, system prompt, temperature, iteration limits — are NOT overridable from the workflow side. For A/B testing prompts or models, define separate agent assets.

## Evaluating prompt variants over a test set

Use `map` to fan out test cases across an agent variant, then `compute` to score, then `compute` to aggregate. No new primitives, no eval node type — just the shapes you already know.

```yaml
description: eval variant_a against the gold test set
state:
  variant: variant_a

nodes:
  load_cases:
    type: kolsch/nodes/read_file
    path: "evals/triage_gold.json"

  parse_cases:
    type: kolsch/nodes/compute
    value: "{{ nodes.load_cases.content | from_json }}"

  classify_one:
    type: kolsch/nodes/agent
    agent: "mybundle/agents/{{ state.variant }}"
    outputs:
      label: str

  classify_all:
    type: kolsch/nodes/map
    apply: classify_one
    over: "{{ nodes.parse_cases.value }}"
    item_as: case

  score:
    type: kolsch/nodes/compute
    value: >
      {{ nodes.classify_all.results
         | zip(nodes.parse_cases.value)
         | map('match_gold')
         | list }}

  accuracy:
    type: kolsch/nodes/compute
    value: "{{ (nodes.score.value | sum) / (nodes.score.value | length) }}"

graph:
  edges:
    - { from: load_cases, to: parse_cases }
    - { from: parse_cases, to: classify_all }
    - { from: classify_all, to: score }
    - { from: score, to: accuracy }
```

Run with a different agent variant by changing the `state.variant` default (or pass via `--input`). Compare runs by their final `accuracy` value, which is the workflow's terminal output.

## Expressing OR fan-in (per-arm terminals)

You can't draw two edges into a shared consumer downstream of a `branch` — that deadlocks under AND-strict fan-in (see the [Explanation](#or-fan-in-deadlock-shape) for the picture). The supported pattern is per-arm terminals: each branch arm runs its own terminal node, with nothing waiting on both.

```yaml
nodes:
  classify: { type: kolsch/nodes/agent, agent: mybundle/agents/classifier }
  decide:   { type: kolsch/nodes/branch, condition: "{{ nodes.classify.is_urgent }}" }
  fast:     { type: kolsch/nodes/agent, agent: mybundle/agents/fast_responder }
  slow:     { type: kolsch/nodes/agent, agent: mybundle/agents/slow_responder }
  log_fast:
    type: kolsch/nodes/bash
    command: "echo {{ inputs.response | quote }} >> fast.log"
  log_slow:
    type: kolsch/nodes/bash
    command: "echo {{ inputs.response | quote }} >> slow.log"

graph:
  edges:
    - { from: classify, to: decide }
    - { from: decide, to: fast, branch: true }
    - { from: decide, to: slow, branch: false }
    - { from: fast, to: log_fast, binds: { response: "{{ nodes.fast.response }}" } }
    - { from: slow, to: log_slow, binds: { response: "{{ nodes.slow.response }}" } }
```

Whichever arm fires runs its own terminal. Nothing waits on the skipped arm. See [`#fan-in`](#fan-in) for Pattern B (hoist convergence into the upstream `compute`).

## Reading a JSON API response

kolsch ships one non-stock Jinja filter at v1, `from_json`, exactly for this case. Fetch the raw JSON in bash and pipe the captured `stdout` through `from_json` on the receiving side:

```yaml
nodes:
  fetch_user:
    type: kolsch/nodes/bash
    command: "curl -s /user/{{ id }}"          # raw JSON; no shell-side parsing

  greet:
    type: kolsch/nodes/bash
    command: "echo hello {{ inputs.name }}"

graph:
  edges:
    - from: fetch_user
      to: greet
      binds:
        name: "{{ nodes.fetch_user.stdout | from_json | attr('first_name') }}"
```

`from_json` parses the string into the underlying Python object; chain filters (`attr`, `selectattr`, indexing) to reach the field you want. For the full v1 filter list see the master spec §2.7.3.

If you need the whole JSON structure with typed access from many downstream nodes, write an authored Python node that returns a typed Pydantic model — that gives you `nodes.<name>.<field>` access in Jinja with real types.

## Writing an authored Python node

Drop a file at `bundles/<bundle>/nodes/<name>.py`:

```python
# bundles/mybundle/nodes/enrich.py
from pydantic import BaseModel
from workflows import node, NodeContext

class EnrichInput(BaseModel):
    lead_id: str

class EnrichOutput(BaseModel):
    enriched: bool
    enriched_at: str

@node(label="Enrich lead with CRM data", secrets_needed=["CRM_API_KEY"])
def run(inputs: EnrichInput, ctx: NodeContext) -> EnrichOutput:
    api_key = ctx.secrets["CRM_API_KEY"]
    # ... do work ...
    ctx.emit("crm_lookup_completed", {"lead_id": inputs.lead_id})
    return EnrichOutput(enriched=True, enriched_at="2026-01-01T00:00:00Z")
```

Reference it from a workflow with the 3-segment ref `mybundle/nodes/enrich` (same shape as built-ins).

> **Trust model.** Authored Python `@node` files are fully-trusted code running at engine privilege. Bundles are a code-review surface, not a config surface. `ctx.secrets` is a convenience for clean access — NOT a security boundary. Operators who load bundles from external sources should treat that as equivalent to running arbitrary code on the engine host. Treat bundles like Ansible playbooks or GitHub Actions — code, not config. See the master spec §1.4 / §1.5 for the full trust model.

---

# Reference

The anchored sections. Error messages from the validator deep-link to these by URL fragment, so the anchor names are stable contracts.

## #binds-rhs — Jinja templates in `binds:`

The `binds:` field on an edge passes data from the source node to the target. Each value is a **Jinja-evaluated config field**: use `{{ ... }}` for Jinja evaluation; otherwise YAML literals are accepted as-is (string, int, float, list, bool, null).

```yaml
edges:
  - from: fetch
    to: classify
    binds:
      content:  "{{ nodes.fetch.stdout }}"          # Jinja expression
      version:  2                                    # YAML literal int
      tags:     [urgent, ops]                        # YAML literal list
      label:    pending                              # YAML literal string
```

The right-hand side, when wrapped in `{{ }}`, can be any Jinja expression — references, filters, conditionals:

```yaml
binds:
  count:    "{{ nodes.list_items.entries | length }}"
  upper:    "{{ nodes.fetch.stdout | upper }}"
  fallback: "{{ nodes.classify.label | default('unknown') }}"
```

Under kolsch's sandboxed `NativeEnvironment`-derived environment, single-expression templates preserve Python types — `{{ items }}` returns the actual `list`, not the string representation. Multi-segment templates with mixed text fall back to strings, which is what you want for shell commands and prompts. See the [Jinja primer](#jinja-in-kolsch) for the full picture.

> **Sandbox note.** The Jinja environment is sandboxed: attribute access to Python internals (`__class__`, `__mro__`, `__globals__`, etc.) is blocked, as are some callable-introspection escape patterns. Workflows that try those patterns raise `WorkflowJinjaSecurityError`. Day-to-day expressions (`nodes.<x>.field`, filters, arithmetic) are unaffected.

### What `binds:` delivers at the receiving node

Every key in `binds:` lands in the receiving node's `inputs.*` namespace. If an edge writes `binds: { content: "{{ nodes.fetch.stdout }}" }`, the receiver sees that value as `inputs.content`.

For authored Python nodes, `inputs.*` is the Pydantic input model — `inputs.content` in the node function. For built-in nodes that take Jinja config fields (e.g., `bash.command`, `compute.value`), reference `inputs.content` directly in those fields:

```yaml
nodes:
  fetch:
    type: kolsch/nodes/bash
    command: "curl {{ url }}"
  classify:
    type: kolsch/nodes/bash
    command: "echo {{ inputs.content | quote }} | classifier"   # inputs.content came from binds:

graph:
  edges:
    - from: fetch
      to: classify
      binds:
        content: "{{ nodes.fetch.stdout }}"
```

`nodes.<name>.<field>` references still work everywhere they always did — for reading any earlier node's output by name. The two forms coexist. `binds:` + `inputs.*` is the explicit per-edge contract; `nodes.<X>.<field>` is the direct cross-graph reach.

See [`#inputs-namespace`](#inputs-namespace) for the full inputs-namespace reference.

---

## #extends — workflow inheritance

A workflow can `extends:` one or more other workflows to pull in their node libraries (workflows with no `graph:` block). Extending is a union: nodes from each parent merge into the child's `nodes:` dict; node-name collisions raise `WorkflowDuplicateNode`. Cycles raise `WorkflowExtendsCycle`. Chain depth is capped at 16 — exceeding raises `WorkflowExtendsDepthExceeded`.

```yaml
# bundles/mybundle/workflows/library.yaml — a node library (no graph:)
nodes:
  classify:
    type: kolsch/nodes/agent
    agent: mybundle/agents/classifier
    description: "Classify incoming records"

# bundles/mybundle/workflows/main.yaml — extends the library
extends:
  - mybundle/workflows/library
nodes:
  fetch:
    type: kolsch/nodes/bash
    command: "curl ..."
graph:
  edges:
    - from: fetch
      to: classify                # 'classify' was contributed by the library
      binds: { record: "{{ nodes.fetch.stdout }}" }
```

`extends:` is for declarative node sharing across workflows. For node-level copies within one workflow with field overrides, see [`#copy-of`](#copy-of). For invoking another workflow as a unit (sub-workflow call), use the `kolsch/nodes/workflow` node type.

`extends:` imports nodes + edges only — **never `policy:`**. If an extended workflow declares a `policy:` block, extending it raises `WorkflowExtendsPolicyForbidden`. Share policy via `bundle.yaml` or `platform.yaml` instead (the policy layer-walk picks both up).

---

## #fan-in — combining outputs from multiple upstream nodes

Fan-in is structural in v1: a node can have any number of incoming edges. Need a consumer that joins two parallel fetches? Just draw two edges into it.

```yaml
nodes:
  fetch_user:   { type: kolsch/nodes/bash, command: "curl /user/{{ id }}" }
  fetch_orders: { type: kolsch/nodes/bash, command: "curl /orders?user={{ id }}" }
  build_report:
    type: kolsch/nodes/compute
    value: "{{ {'user': inputs.user, 'orders': inputs.orders} }}"
    description: "join user + orders"

graph:
  edges:
    - from: fetch_user
      to: build_report
      binds: { user: "{{ nodes.fetch_user.stdout }}" }
    - from: fetch_orders
      to: build_report
      binds: { orders: "{{ nodes.fetch_orders.stdout }}" }
```

The engine waits for ALL incoming edges to fire before firing the target — this is AND-strict trigger semantics. `build_report` runs once `fetch_user` AND `fetch_orders` both complete.

If two edges into the same node bind the same key, you get a parse-time `WorkflowBindCollision` error. Pick distinct names per edge.

You can mix the two reference styles freely: a fan-in consumer can read `inputs.user` (from `binds:`) and `nodes.some_other_node.field` (direct cross-graph reach) in the same expression.

### OR semantics — exactly one of N branches fires

Because incoming edges are AND-strict, you can't express "consumer fires when EITHER branch_a OR branch_b finishes" by simply drawing two edges into a shared consumer. A shared convergence node downstream of a `branch` would deadlock: AND-strict means the consumer waits on BOTH incoming arms, but the branch guarantees one arm is skipped, so the consumer never fires.

For the picture of the deadlock shape, see [the OR fan-in deadlock diagram](#or-fan-in-deadlock-shape) in the Explanation section.

OR fan-in via a single convergence node is structurally impossible at v1 under AND-strict. Supported patterns: per-arm terminals; or hoist the convergence into the upstream branch node's own value field.

**Pattern A — per-arm terminals.** Give each branch arm its own terminal node; no shared downstream consumer. See [the how-to recipe](#expressing-or-fan-in-per-arm-terminals) for a worked example.

**Pattern B — hoist convergence into the branch node's `value`.** If you really need a single downstream consumer, push the OR resolution up into the branching node itself (or an upstream `compute`) so the value flowing downstream is already unified:

```yaml
nodes:
  classify: { type: kolsch/nodes/agent, agent: mybundle/agents/classifier }
  pick:
    type: kolsch/nodes/compute
    value: "{{ nodes.classify.is_urgent and 'fast' or 'slow' }}"
    description: "pick which path's payload to forward"
  consumer:
    type: kolsch/nodes/bash
    command: "echo {{ inputs.choice | quote }}"
```

Future enhancement: a `WorkflowFanInDeadlock` validator check that flags the structurally-impossible shape at parse time. Not in v1.

---

## #node-types — the 14 built-in node types

The 14 built-in types group naturally into 6 categories. The "Label" column is the display name the node TYPE declares for itself — what chat capsules, the CLI, and other surfaces fall back to when a node instance has no `description:`. See [`#chat-capsules`](#chat-capsules) for the full label/description fallback rules.

All refs in workflow YAML — `type:`, `agent:`, `extends:`, sub-workflow targets — are full 3-segment `<bundle>/<asset_type>/<name>`. The 1-segment shorthand has been removed; see [`process/qol-refs-1seg-removal.md`](../process/qol-refs-1seg-removal.md) for the migration codemod.

For your own Python nodes, see authored-node refs (`bundles/<bundle>/nodes/<name>.py` with `@node` decorator); reference them as `type: <bundle>/nodes/<name>` (same 3-segment shape). The decorator takes an optional `label=` kwarg to set the type's label: `@node(label="Enrich lead with CRM data", secrets_needed=[...])`.

> `description:` is a USER AID for display only. It is NEVER used to identify, reference, or address a node. The node NAME (the key in the `nodes:` mapping) is the only identifier.

### Execution

Run something — a shell command, an agent, a sub-workflow, or a deliberate pass-through.

| Type | Label | Purpose | Output shape |
|---|---|---|---|
| `kolsch/nodes/bash` | Shell | Run a shell command | `{stdout, stderr, exit_code}` |
| `kolsch/nodes/agent` | Agent | Invoke a kolsch agent | agent's declared output schema |
| `kolsch/nodes/workflow` | Sub-workflow | Invoke a sub-workflow | the sub-workflow's terminal dict |
| `kolsch/nodes/noop` | Pass through | Do nothing (pass-through) | `{}` |

> **`bash` shell-safety.** The bash node is a basic shell-string wrapper at v1. Shell-safe interpolation is the author's responsibility — quote interpolated values that may contain whitespace or shell metacharacters (the `| quote` filter is the conventional approach). Future improvements (argv-form, automatic shell-escape, validator warnings) are deferred to Phase 3.x QoL.

> **`agent` identity is the agent's, not the workflow's.** Per-invocation overrides of agent identity (model, prompt, system prompt, temperature, iteration limits) are out of scope at all phases — agents are first-class assets. For prompt or model A/B testing, define separate agent assets (the agent system supports inheritance). The `outputs:` override is exempt: it shapes the workflow's consumption of the agent's response (workflow-side concern), and goes through CrewAI's structured-output mechanism using the IOSpec grammar. See the [how-to](#calling-an-llm-with-structured-output) for the `outputs:` shape.

> **Context, retries, fallback live at the agent layer.** Context management (pre-flight token budgeting, overflow strategies, context-window selection) lives at the AGENT level, not the workflow level. Agents own their context discipline as part of their definition. Workflow `agent:` nodes that fail due to context overflow surface as `WorkflowNodeFailed` with the agent layer's error in `caller_message`. Multi-provider fallback (try Anthropic → fall back to OpenAI on rate-limit/5xx) is similarly an agent-layer concern — agent assets define provider selection and fallback chains; workflow `agent:` nodes inherit whatever the agent declares.

### File IO

Read, write, and list files in the workspace.

| Type | Label | Purpose | Output shape |
|---|---|---|---|
| `kolsch/nodes/read_file` | Read file | Read a file's contents | `{content: str}` |
| `kolsch/nodes/write_file` | Write file | Write a file | `{bytes_written: int}` |
| `kolsch/nodes/list_files` | List files | List a directory | `{entries: list[str]}` |

### State

Mutate workflow state — a flat key/value map persisted in the run's checkpoint.

| Type | Label | Purpose | Output shape |
|---|---|---|---|
| `kolsch/nodes/set_state` | Set state | Mutate workflow state keys | `{}` |
| `kolsch/nodes/delete_state` | Delete state | Remove workflow state keys | `{}` |

> **State is read-only from inside `@node` code.** Authored Python nodes can read state via `ctx.state["foo"]` — `ctx.state` is a `Mapping[str, Any]` wrapped in `MappingProxyType`, the same shape as `ctx.secrets`. Mutation is forbidden: `ctx.state["foo"] = ...` raises `TypeError`. The only legal way to mutate workflow state is a `set_state` or `delete_state` node in the graph — that constraint keeps state changes visible in the workflow YAML and the event log, not buried in node-private code.

### Lists & expressions

Filter, map, and compute over lists and expressions.

| Type | Label | Purpose | Output shape |
|---|---|---|---|
| `kolsch/nodes/filter` | Filter | Filter a list by predicate | `{result: list[T]}` |
| `kolsch/nodes/compute` | Compute | Evaluate a Jinja expression | `{value: <evaluated>}` |

### Composition

Compose the graph itself — map a worker over a list, or branch the graph based on a condition.

| Type | Label | Purpose | Output shape |
|---|---|---|---|
| `kolsch/nodes/map` | Map | Apply a worker to each element | `{results: list[T]}` |
| `kolsch/nodes/branch` | Branch | Evaluate a condition; signal branch | `{value: bool}` |

### Interaction

Pause for a human.

| Type | Label | Purpose | Output shape |
|---|---|---|---|
| `kolsch/nodes/user_input` | Wait for input | Pause for human input | `{response: str}` |

---

## #copy-of — node copy semantics

`copy_of:` creates a fresh node instance copying another node's config, with optional field-level overrides. The source must itself be graph-reachable; there are no "template-only" nodes (use YAML anchors for pure-config-sharing without an executing source).

```yaml
nodes:
  fetch_a: { type: kolsch/nodes/bash, command: "curl {{ url_a }}" }
  fetch_b: { copy_of: fetch_a, command: "curl {{ url_b }}" }    # same config; command overridden
  fetch_c: { copy_of: fetch_a, command: "curl {{ url_c }}", retry: 2 }
```

`copy_of:` is in-workflow; it does NOT cross workflow boundaries. For cross-workflow node sharing, use [`#extends`](#extends).

Override semantics are shallow: a field in the child replaces the source's same-named field entirely (no deep merge for dicts/lists). If you need merge behavior, use Jinja explicitly inside the field value.

---

## #map — mapper invocation

The `kolsch/nodes/map` node applies a worker node to each element of a list, producing a list of worker outputs.

```yaml
nodes:
  fetch_ids:
    type: kolsch/nodes/bash
    command: "cat ids.txt"

  classify_one:                                        # the worker — not in graph; reached via apply
    type: kolsch/nodes/agent
    agent: mybundle/agents/classifier

  classify_all:
    type: kolsch/nodes/map
    apply: classify_one                                # invokes classify_one per element
    over: "{{ nodes.fetch_ids.stdout.splitlines() }}"
    item_as: ticket                                    # element name visible in apply's binds:

  summarize:
    type: kolsch/nodes/agent
    agent: mybundle/agents/summarizer
    description: "summarize classification results"

graph:
  edges:
    - from: fetch_ids
      to: classify_all
    - from: classify_all
      to: summarize
      binds:
        results: "{{ nodes.classify_all.results | tojson }}"
```

Worker nodes (named by `apply:`) live in `nodes:` for shape introspection but are NOT addressable from outside the mapper. A reference `nodes.classify_one.*` from a non-mapper node raises `WorkflowMapperWorkerNotAddressable`. Use `nodes.classify_all.results` (the mapper's output list) to read all worker outputs.

Each iteration's events carry `parent_invocation_id` (the mapper's `invocation_id`) and `iteration_index: int` so forensics can pinpoint a specific iteration.

---

## #branch — conditional execution

The `kolsch/nodes/branch` node evaluates a Jinja boolean and signals which downstream edges fire.

```yaml
nodes:
  classify: { type: kolsch/nodes/agent, agent: mybundle/agents/classifier }
  decide:
    type: kolsch/nodes/branch
    condition: "{{ nodes.classify.label == 'urgent' }}"
  urgent_handler: { type: kolsch/nodes/agent, agent: mybundle/agents/escalator }
  normal_handler: { type: kolsch/nodes/agent, agent: mybundle/agents/normal }

graph:
  edges:
    - from: classify
      to: decide
      binds: { label: "{{ nodes.classify.label }}" }
    - from: decide
      to: urgent_handler
      branch: true                       # fires when condition is true
    - from: decide
      to: normal_handler
      branch: false                      # fires when condition is false
```

Branchable nodes (`branch`, `bash` nodes branching on `exit_code == 0`, authored Python nodes returning `bool` or `tuple[bool, BaseModel]`) MUST have BOTH `branch: true` and `branch: false` outgoing edges declared. Missing arm raises `WorkflowBranchMissingArm`. Edges with `branch:` from non-branchable nodes raise `WorkflowBranchOnNonBranchable`.

The `condition:` field is Jinja-evaluated: use `{{ }}` for an expression, or a YAML literal `true` / `false` for an unconditional branch.

Branch nodes are also the idiomatic seed for OR-fan-in shapes. The canonical patterns at v1 are per-arm terminals or hoisting the OR resolution upstream into a `compute` or the branch node's own value — a shared downstream consumer waiting on both arms would deadlock under AND-strict. See [`#fan-in`](#fan-in) for the worked examples.

---

## #policy-from — caller scoping via `from:` field

> ⚠️ **Workflow policy does NOT elevate embedded agents.**
> When an `agent` node runs inside a workflow and that agent makes a tool call,
> only the agent's own `policy:` layer is consulted (plus bundle + platform).
> The workflow's `policy:` block is NOT walked for the agent's calls. This is
> the proximate-caller rule and it's a structural escalation guarantee — there is
> no way to express "this workflow allows the embedded agent to do X" by editing
> the workflow. To grant the agent extra capabilities, edit the agent.
>
> For the engine's layer-selection algorithm, see [`#layer-selection`](#layer-selection).
> For the rationale, see [Why workflow policy exists](#why-workflow-policy-exists) in Explanation.

Policy rules support an optional `from:` field that scopes a rule to specific callers. The grammar is strict: `from:` MUST be a 3-segment ref where the asset-type segment is `agents` or `workflows`, and the target MUST exist in the catalog.

```yaml
# bundles/mybundle/workflows/triage.yaml — proximate-caller rule in action

# BEFORE: this would have leaked the workflow's elevated bash to embedded agents
policy:
  - what: kolsch/tools/bash
    pattern: "curl https://*"
    effect: allow                                # too broad — but actually safe!

# AFTER: explicitly scoped to the workflow itself for clarity
policy:
  - what: kolsch/tools/bash
    pattern: "curl https://*"
    from: mybundle/workflows/triage              # only when THIS workflow is calling
    effect: allow

  - what: kolsch/tools/bash
    pattern: "rm -rf*"
    effect: deny                                 # universal deny still applies
```

Both forms are equivalent for embedded agents because of the proximate-caller rule: an agent's tool calls don't consult the workflow layer at all, regardless of `from:`. The `from:` field is useful for cross-workflow scoping (e.g., bundle-level rules that only apply to one workflow in the bundle).

### Strict `from:` grammar (parse-time errors)

| Error | When it fires |
|---|---|
| `WorkflowFromShapeError` | `from:` is not a 3-segment ref. |
| `WorkflowFromAssetTypeError` | `from:` ref's asset-type segment is not `agents` or `workflows`. |
| `WorkflowFromNotFound` | `from:` ref's target doesn't exist in the catalog. |

The `from:` field matches against the caller chain (proximate frame first). If the chain is empty AND `from:` is set, the rule fails-closed (no match). Omit `from:` to match any caller (the default).

For audit-level telemetry, see the `audit_level` setting in `platform.yaml`'s policy config — see the spec §13.5. The default is `rule_matched` (persist decisions where an explicit rule fired); compliance-driven operators opt into `all`.

---

## #layer-selection — which policy layers the engine walks

When any tool call happens inside a workflow run, the engine builds an ordered tuple of policy layers and walks them in proximate-caller order. The first allow or deny rule that matches wins.

The proximate caller is the closest frame that owns the call — for a tool call made by an embedded agent, the proximate caller is the agent, not the workflow.

| Proximate caller | Layers walked, in order |
|---|---|
| Workflow node calling a tool directly (e.g., a `bash` node) | `workflow` → `bundle` → `platform` |
| Agent (running inside a workflow's `agent` node) calling a tool | `agent` → `bundle` → `platform` |
| Sub-workflow node calling a tool directly | the sub-workflow's `workflow` layer → `bundle` → `platform` |

The workflow layer is NEVER consulted when an embedded agent is the proximate caller. This is the structural escalation guarantee: you cannot grant an embedded agent extra capabilities by editing the workflow that hosts it.

Error messages that need to talk about layer selection deep-link to this anchor.

---

## #timeout — node and workflow timeouts

Two levels of `timeout_seconds:` field:

- **Per-node** (in node config): bounds an individual node's execution; on expiry, the node fails with `NodeTimeoutExceeded` and the workflow's `retry:` policy applies
- **Workflow-level** (top-level YAML field): bounds the entire run; on expiry at a node boundary, the workflow halts with `WorkflowTimeoutExceeded` and status `timed_out`

```yaml
description: "Daily report; budget 5 minutes overall"
timeout_seconds: 300                       # workflow-level backstop

nodes:
  fetch:
    type: kolsch/nodes/bash
    command: "curl https://slow-api/data"
    timeout_seconds: 30                    # per-node cap
    retry: 2                               # on timeout, retry up to 2x
  
  process:
    type: kolsch/nodes/agent
    agent: mybundle/agents/processor
    timeout_seconds: 120                   # LLM call cap
```

Composition rules:
- Workflow-level fires at next-node-boundary cooperative stop (current node finishes first)
- Per-node timeout fires for a single node only; workflow continues per retry policy
- `waiting_for_input` status PAUSES the workflow-level timer (otherwise human-in-the-loop is uselessly time-bounded)
- Sub-workflows have INDEPENDENT budgets; the parent's deadline doesn't propagate

Validator catches:
- Non-positive timeout values → `WorkflowInvalidTimeout` error
- Per-node `timeout_seconds` > workflow `timeout_seconds` → `WorkflowTimeoutShadowsNode` warning (the per-node timer will never fire)

For interrupting a hung node operator must `kill -9` the process; see [`#cancel`](#cancel) for the cooperative cancel path.

---

## #cancel — workflow cancellation

`kolsch workflows cancel <run_id>` writes a sidecar `cancel.flag` file in the run's directory. The executor polls this flag between node boundaries; on detection, it emits `workflow.run.aborted` and exits cleanly. The run's status flips to `aborted`.

```
$ kolsch workflows cancel wrk_abc123
Cancel signal written for wrk_abc123 (will halt at next node boundary)
```

Cancel does NOT interrupt a running node — bash subprocesses, agent reasoning, and authored Python don't have a uniform interrupt mechanism. For a hung node, operator must `kill -9` the OS process first; then on the next engine restart, the orphan-sweep marks `status=running` runs without a live PID as `failed` with reason `orphaned`.

The recovery toolkit:
- **Hangs caught by `timeout_seconds:`** → automatic retry or failure (see [`#timeout`](#timeout))
- **Intentional stops** → `kolsch workflows cancel`
- **Post-`kill -9` JSONL** → reconciled by orphan sweep on next engine startup

Cancel + timeout + orphan sweep together provide the operational story; no single mechanism handles every failure shape.

> **Single-user assumption.** kolsch v1 assumes a single-user environment. The `cancel.flag` write and resume authentication rely on filesystem-level access controls — there is no OS-user check or resume token. Multi-user host hardening is deferred to Phase 3.x.

---

## #trace-id — the chain-spanning ID

Every event in the kolsch event log carries a `trace_id` (prefix `tr_<uuid>`). The trace_id identifies the entire causal cascade triggered by one user-initiated entry point — a CLI invocation, a chat reply, a scheduled run.

Inheritance rules:
- The outermost entry assigns the trace_id once; downstream code never resets it
- Sub-workflow invocations inherit the caller's trace_id
- Forks share the source's trace_id (they're part of the same investigation)
- Agent sessions spawned by workflow `agent` nodes inherit the workflow's trace_id
- Sub-agent invocations (agent X invokes agent Y as a tool) inherit X's trace_id

Workflow events and agent-session events are written to **separate JSONLs** (workflow runs at `runs/<wrk_id>/events.jsonl`; agent sessions at `sessions/<ses_id>/events.jsonl`) but they're linked by `trace_id`. Forensic queries that need the full picture read both and join on `trace_id`.

Forensic queries:
```bash
# Everything related to a single user request, across all JSONLs
grep '"trace_id": "tr_abc123"' runs/*/events.jsonl sessions/*/events.jsonl

# What triggered a specific event
# (Follow parent_event_id once; recurse for the chain)
```

For the full ID model — `workflow_run_id`, `invocation_id`, `tool_call_id`, `event_id`, plus parent pointers — see the master spec §11.2.1. Engine-level token aggregation is not provided at v1; operators who want totals read `model.response.received.data.usage` events across the JSONL and sum themselves.

---

## #cli-nodes — discovering node types from the CLI

Phase 3.0 ships symmetric asset-discovery commands for the `nodes` asset type, matching the existing `workflows`, `agents`, and `tools` shapes:

```
$ kolsch nodes list [--bundle <name>] [--format text|json]
$ kolsch nodes show <ref>          [--format text|json]
```

`nodes list` enumerates every node type registered across the loaded bundles (built-in `kolsch/nodes/*` plus any authored Python `@node` functions). Filter to one bundle with `--bundle <name>`. The JSON shape is `{nodes: [{ref, kind, label, secrets_needed}, ...]}`.

`nodes show <ref>` prints the full structure for a single node type, including its label, input/output schema, declared `secrets_needed`, and source location.

Deep documentation lives in the master spec §14 alongside the other asset-discovery commands.

---

## #error-format — reading kolsch error messages

Every workflow validation error follows a strict single-line format:

```
<ClassName>: <file>:<line> <identifying-names>: <problem summary>. Fix: <one-line action> (see docs/workflows#<anchor>).
```

Example:
```
WorkflowJinjaRefUnknown: workflows/triage.yaml:42 edge 'merge→summarize', bind 'x': reference 'nodes.fetc.stdout' does not match a known node ('fetc' — typo of 'fetch'?). Fix: correct the reference or add the missing node (see docs/workflows#binds-rhs).
```

Components:
- **`ClassName`** — exact Python exception class (grep-friendly)
- **`<file>:<line>`** — the YAML source location
- **identifying-names** — single-quoted identifiers (node name, edge endpoints, bind target, etc.)
- **summary** — one-line description of the problem
- **`Fix:`** — one-line action (literal `Fix: ` token), pointing at the section here for syntax

### Multi-error output

When more than one validator error accumulates in a single layer, they all surface together — rendered through Python's stdlib `ExceptionGroup`. The CLI prints each sub-exception's message on its own line:

```
$ kolsch workflows validate mybundle/workflows/triage
ExceptionGroup: 3 errors validating mybundle/workflows/triage:
  WorkflowJinjaRefUnknown: workflows/triage.yaml:14 node 'classify', field 'agent': reference 'mybundle/agents/classifr' does not match a catalog asset ('classifr' — typo of 'classifier'?). Fix: correct the agent ref (see docs/workflows#node-types).
  WorkflowFromShapeError: workflows/triage.yaml:32 policy rule #2: 'from:' value 'workflows/triage' is not a 3-segment ref. Fix: use full <bundle>/<asset_type>/<name> shape (see docs/workflows#policy-from).
  WorkflowNodeNotDefined: workflows/triage.yaml:48 edge 'classify→summrize': 'to:' references node 'summrize' which is not defined in this workflow ('summrize' — typo of 'summarize'?). Fix: define the node or correct the reference (see docs/workflows#node-types).
exit code: 1
```

Non-engineer callers (e.g., chat users running a workflow built by their team) see a separate `caller_message` channel — a one-sentence plain-English statement rendered in chat capsules instead of the engineer-targeted line. The engineering-targeted `str(exc)` always remains available via logs and `--format json`.

---

## #secrets — secrets discipline

Authored Python nodes declare which secrets they need via the `@node(secrets_needed=[...])` decorator:

```python
# bundles/mybundle/nodes/upload.py
from workflows import node, NodeContext

@node(secrets_needed=["AWS_ACCESS_KEY", "AWS_SECRET"])
def run(inputs, ctx: NodeContext):
    aws_key = ctx.secrets["AWS_ACCESS_KEY"]
    # ... use the secret
```

At run-start, the validator verifies each named secret exists in the secrets subsystem. Missing secret → `WorkflowSecretUnresolved` at gate time (fail before any node runs).

`ctx.secrets` is a read-only `Mapping[str, str]` wrapped in `MappingProxyType` — node code cannot mutate it.

Authored Python nodes can also emit ad-hoc events via `ctx.emit(subkind, payload)`. The `subkind` is freeform — pick whatever short tag fits the moment (e.g., `"ticket_classified"`, `"crm_lookup_failed"`). The engine prepends `node.<bundle>.` based on the bundle that owns the running node, so the wire kind on JSONL is `node.<bundle>.<subkind>`. You only supply the suffix; the engine guarantees the prefix.

> **Subkind convention.** By convention, use past-tense verb subkinds matching the engine event-kind style: `<thing>.<verb-past>` (e.g., `ticket_classified`, `crm_lookup_failed`). The engine prepends the bundle prefix automatically.

```python
@node(secrets_needed=["CRM_API_KEY"])
def run(inputs, ctx: NodeContext):
    ctx.emit("checkpoint_reached", {"step": "validated"})
    # JSONL line carries kind = "node.mybundle.checkpoint_reached"
```

Captured outputs in the event JSONL NEVER contain resolved secret values. References preserve the literal Jinja form (`{{ secrets.aws_key }}`) at capture time; substitution happens only at execution time and is never persisted. There is no post-hoc string-scanning of payloads — the rule is structural, applied at capture-emit.

For workflow-author Jinja references in command strings, prompts, or config:
```yaml
nodes:
  api_call:
    type: kolsch/nodes/bash
    command: 'curl -H "Authorization: Bearer {{ secrets.api_token }}" https://api.example.com'
```

The engine substitutes `secrets.api_token` at execution; the captured `workflow.tool.called` event records the literal Jinja form, not the resolved token.

> **Event-log integrity.** Event log integrity (tamper detection) is deferred to Phase 3.x. v1 relies on filesystem-level access controls; there is no `kolsch workflows events verify` command and no HMAC chaining.

---

## #chat-capsules — chat surface rendering

When a workflow runs from chat (Phase 3.9+), each node invocation surfaces as a collapsible chat capsule. The capsule title is picked by walking three sources in order — first hit wins:

1. **Node instance `description:`** — set by the workflow author. Always wins if present.
2. **Node type label** — declared by the node type. Built-in node types all declare one (see the Label column in [`#node-types`](#node-types)). Authored Python nodes declare theirs via `@node(label="...")`.
3. **Class/function name** — last-resort fallback. For built-ins, the type name (`bash`, `agent`); for authored Python, the function name, lightly cleaned up.

`description:` remains OPTIONAL universally — there's always a usable fallback. Set it when the type label isn't specific enough (most common on `compute`, where the type label is just "Compute" but the actual computation is workflow-specific).

```yaml
nodes:
  fetch_one:
    type: kolsch/nodes/bash
    command: "curl ..."
    description: "Fetch primary record"           # capsule shows "Fetch primary record"

  classify:
    type: kolsch/nodes/agent
    agent: mybundle/agents/classifier             # no description → capsule shows "Agent" (type label)

  count_urgent:
    type: kolsch/nodes/compute
    value: "{{ items | selectattr('urgent') | list | length }}"
    description: "Count urgent items"             # recommended on compute — type label "Compute" is too generic
```

For authored Python nodes, attach a label at decoration time:

```python
@node(label="Enrich lead with CRM data", secrets_needed=["CRM_API_KEY"])
def run(inputs, ctx: NodeContext):
    ...
```

A node instance can still override the type's label by setting `description:` in YAML — author control beats type defaults.

> Reminder: `description:` is a USER AID for display only. It is NEVER used to identify, reference, or address a node. The node NAME (the `nodes:` mapping key) is the only identifier.

Errors in chat capsules show the exception's `caller_message` (the plain-English channel), with the engineer-targeted `str(exc)` available behind a "show technical details" expansion.

> **Capsule body shows captured output at node completion.** Token-by-token streaming (incremental capsule updates while an LLM responds) is deferred to a future phase — at v1, a capsule renders once at `node.completed`. The CLI `run` command blocks until the workflow completes and then prints the terminal output. `user_input` nodes show a small inline prompt referencing the input variable name; rich TUI, progress display, and cost display are deferred.

---

## #inputs-namespace — the `inputs.*` Jinja namespace

Every key in an edge's `binds:` becomes a name under the receiver's `inputs.*` namespace. The receiver references those names from its own Jinja config fields (built-in nodes) or as fields on the Pydantic input model (authored Python nodes).

```yaml
nodes:
  fetch:
    type: kolsch/nodes/bash
    command: "curl /user/{{ id }}"                # raw JSON

  greet:
    type: kolsch/nodes/bash
    command: "echo hello {{ inputs.name }}"       # inputs.name came from binds:

graph:
  edges:
    - from: fetch
      to: greet
      binds:
        name: "{{ nodes.fetch.stdout | from_json | attr('first_name') }}"
```

For JSON data crossing the bash→Jinja boundary, kolsch ships a `from_json` filter at v1 (the only non-stock filter). For the full v1 filter set, see master spec §2.7.3. For complex JSON with many downstream consumers, prefer an authored Python node that returns a typed Pydantic model.

Behavior at fire time:

- Each `binds:` Jinja expression renders against the up-to-date `nodes.*` view.
- The rendered values populate the receiver's input model.
- For authored Python nodes, the input model is a Pydantic model in strict mode — a string `"5"` will NOT be coerced to int `5`. Mismatches raise `WorkflowBindTypeMismatch` at the point the receiver fires.
- For built-in nodes, `inputs.<key>` is available in any Jinja config field on that node.

Two binds across multiple incoming edges that write the same key is a parse-time error: `WorkflowBindCollision`. Pick distinct names per edge.

`inputs.*` and `nodes.*` coexist. `inputs.*` is the explicit per-edge contract — the only path declared on the edge itself. `nodes.<X>.<field>` is the direct cross-graph reach for reading any earlier node's output by name. Use whichever fits.

For the original RHS Jinja rules, see [`#binds-rhs`](#binds-rhs).

---

# Explanation

The "why" behind the design. Skim this when something feels arbitrary — there's usually a structural reason.

## Jinja in kolsch

kolsch uses Jinja2 for all expression evaluation in workflow YAML — every `command:`, `value:`, `condition:`, `binds:` RHS, etc. is rendered through one shared environment.

The two pieces of syntax you'll use constantly:

- **`{{ expression }}`** — evaluate and substitute. `{{ nodes.fetch.stdout }}` reads a captured output. `{{ 1 + 2 }}` is `3`. `{{ inputs.name }}` reads the inputs namespace.
- **`{{ value | filter }}`** — pipe a value through a transformation. `{{ items | length }}` is the list's length; `{{ name | upper }}` upper-cases a string. Chain filters: `{{ users | selectattr('active') | list | length }}`.

The filters you'll reach for most:

| Filter | What it does | Example |
|---|---|---|
| `length` | Length of a list or string | `{{ items | length }}` |
| `selectattr` | Filter a list of dicts/objects by an attribute | `{{ users | selectattr('active') | list }}` |
| `default` | Fall back to a value if the input is undefined | `{{ inputs.maybe | default('none') }}` |
| `from_json` | Parse a JSON string into the underlying Python value | `{{ nodes.fetch.stdout | from_json }}` |

`from_json` is the **only** non-stock filter kolsch ships at v1; everything else is stdlib Jinja2. The filter set is intentionally core-only — bundles, plugins, and pip-installable packages CANNOT register custom Jinja filters. Filter additions are kolsch's responsibility, kept small and audited. For the full v1 filter list and the rationale, see master spec §2.7.3.

kolsch wires Jinja through a sandboxed `NativeEnvironment`-derived environment, which means **single-expression templates preserve Python types**. That distinction matters more than you'd think:

```jinja
{{ items }}           ← single segment; returns the actual Python list
items: {{ items }}    ← multi-segment with surrounding text; returns the string "items: [...]"
```

So `binds: { ids: "{{ nodes.fetch.ids }}" }` delivers the list itself to the receiver — your authored Python node sees `inputs.ids` as a `list[int]`, not as the string `"[1, 2, 3]"`. As soon as you add any literal text around the `{{ }}`, the result is a string.

For Jinja-evaluated config fields (`binds:`, `set_state` values, `compute.value`, `filter.condition`, `branch.condition`, `when:`, `map.over:`), the rule is uniform: **YAML literals pass through as-is; `{{ }}` triggers Jinja evaluation.** A bare integer is an integer, a bare list is a list, and a string wrapped in `{{ }}` is whatever the expression evaluates to.

## OR fan-in deadlock shape

Why does a shared convergence node downstream of a `branch` deadlock under AND-strict fan-in? Here's the picture:

```
        ┌──────────┐
        │  branch  │
        └────┬─────┘
       true │ │ false
       ┌────┘ └─────┐
       ▼            ▼
   ┌───────┐    ┌───────┐
   │   A   │    │   B   │     ← only ONE of these fires; the other is skipped
   └───┬───┘    └───┬───┘
       │            │
       └─────┬──────┘
             ▼
        ┌─────────┐
        │ consumer│              ← AND-strict: waits for BOTH A and B
        └─────────┘                  one is skipped → consumer waits forever
```

The consumer's incoming edges are AND-strict, so it waits for ALL upstream firings — but the branch guarantees one arm is skipped, never fires, never satisfies the wait. That's the deadlock. The validator may catch this shape in the future (`WorkflowFanInDeadlock`), but at v1 it does not.

The two supported workarounds (per-arm terminals; hoist convergence into a `compute`) are documented in [`#fan-in`](#fan-in) and worked through in the [how-to](#expressing-or-fan-in-per-arm-terminals).

## Why workflow policy exists

Workflows can call tools — a `bash` node literally executes a shell command; an authored Python node may invoke a tool directly; a sub-workflow can call other tools. Tools have allow/deny rules (the `policy:` block). A workflow's `policy:` block governs the tools the **workflow itself** calls. So far so symmetric.

What workflow policy does NOT do: it does not grant capabilities to embedded agents. An `agent:` node inside a workflow runs an agent, and when that agent makes a tool call, the engine walks the **agent's** policy layer (plus bundle + platform) — never the workflow's. This is the proximate-caller rule, and it's a structural safety feature, not a bug. The point is that you can't elevate an embedded agent's capabilities by editing the workflow that hosts it; agent capabilities are governed at the agent.

**Before / after scenario.** Imagine an agent `claude_helper` whose `policy:` denies all bash. Someone authors a workflow `risky.yaml` that uses this agent and also runs a bash node with elevated allow rules:

```yaml
# risky.yaml — policy granting the workflow bash access
policy:
  - what: kolsch/tools/bash
    pattern: "*"
    effect: allow

nodes:
  call_helper:
    type: kolsch/nodes/agent
    agent: mybundle/agents/claude_helper        # this agent's bash is still denied
  run_script:
    type: kolsch/nodes/bash
    command: "deploy.sh"                         # this is allowed by the workflow's policy
```

The bash node runs (the workflow is the proximate caller; the workflow's `allow *` applies). But `claude_helper` cannot call bash — it's the proximate caller for its own tool calls, and its `deny *` applies. The workflow author had no path to elevate the agent. That's the safety feature.

## AND-strict fan-in semantics

Every incoming edge into a node is part of an implicit AND. A node fires only when ALL its incoming edges have fired (or been determined to definitively not fire — e.g., the source was skipped because of a branch arm).

The pragmatic consequence: fan-in across siblings always means "wait for all of them." There is no OR semantics for fan-in at the edge level; OR happens at the branch level upstream (see the deadlock shape above for why the two don't compose freely).

## The test-harness model

Authored Python nodes are unit-testable because the `NodeContext` is a plain dataclass with no engine dependencies — there's no engine to spin up, no event log to write to, no separate harness module to import. The contract is exactly the public surface of `NodeContext`: hand it the fields you want, and the node runs. See the [how-to](#testing-your-authored-node) for the construction pattern.

`NodeContext.for_test(**overrides)` is the one piece of test-flavored API. It's ~30 lines that supply sensible defaults for every field (a tmpdir workspace, a no-op logger, a swallowing emit_fn, empty secrets and state) so the test author only writes the overrides their assertion actually cares about. Critically, `for_test` returns a real `NodeContext` instance — the same class production uses, with the same public fields (`bundle`, `emit_fn`, `secrets`, `state`, etc.). There's no parallel `TestNodeContext` type to drift from production, no `kolsch.workflows.testing` module, no `CapturedEmits` recorder. The `emit_fn` override slot is where the test author plugs in their own recorder if they want one — usually a one-liner list-append lambda.

---

## Further reading

- Master design spec — [`process/phase3.0-workflow-spec.md`](../process/phase3.0-workflow-spec.md)
- Implementation spec — [`process/phase3.0-workflow-build.md`](../process/phase3.0-workflow-build.md)
- Migration codemod — [`process/qol-refs-1seg-removal.md`](../process/qol-refs-1seg-removal.md)
- Cross-cutting CLI format convention — [`process/qol-cli-format-output-rollout.md`](../process/qol-cli-format-output-rollout.md)
