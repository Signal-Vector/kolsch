---
name: author-workflow
description: Encode the Kolsch workflow grammar, iteration recipe, and error-class table so an LLM can author valid workflows from natural-language briefs.
allowed-tools:
  - read_file
  - write_file
  - list_files
  - bash
  - validate_workflow
  - introspect_node
disable-model-invocation: false
---

# author-workflow

This skill turns a natural-language brief — "build a workflow that classifies a ticket and emails the urgent ones" — into a runnable Kolsch workflow. It encodes the YAML grammar, the iteration recipe (draft → validate → revise), the error-class table the validator emits, and the canonical examples to reach for.

The contract is hard: **every workflow you declare ready PASSES the run-start gate.** `validate_workflow` is your only judge. Do not declare success on a draft that hasn't returned `is_valid: true`.

## When this skill applies

Trigger phrases the user might say:

- "build a workflow that…"
- "I want a workflow to…"
- "automate this: …"
- "turn this script into a workflow"
- "add a step to `<existing>` that…" (amendment loop)
- "fix the `<existing>` workflow so it…" (amendment loop)

This skill is for **workflow YAML + the authored Python `@node` files those workflows depend on**. It is NOT for:

- Generating new agent assets, tool assets, or skill assets — those have their own meta-agents (TBD).
- Auto-running the produced workflow — surface a "Try it: `/workflow <ref>`" hint and let the author decide.
- Reverse-engineering a workflow from an event log.

## The iteration recipe

The loop is identical for from-scratch authoring and for amendments; only the seed differs (blank slate vs. existing YAML).

```text
state: { brief, target_bundle, draft_yaml, attempts: 0 }

# 1. Discover what already exists. Reuse > re-invent.
bash: kolsch workflows list
bash: kolsch agents list
bash: kolsch nodes list

# 2. Sketch: what nodes, what edges, what binds.

# 3. If the brief needs a new authored @node:
write_file: bundles/<target>/nodes/<name>.py
introspect_node: bundles/<target>/nodes/<name>.py
# loop on signature errors before touching the workflow YAML

# 4. Write the workflow YAML.
write_file: bundles/<target>/workflows/<name>.yaml

# 5. Validate.
validate_workflow: yaml_path=<written path>

# 6. If is_valid: false:
#    READ the first error's error_class.
#    CONSULT the error-class table below.
#    FIX the root cause (not the symptom).
#    INCREMENT attempts.
#    GOTO 4 with the revised YAML.
#    MAX_ATTEMPTS = 5 — see "When to escalate".

# 7. On is_valid: true:
#    Surface the ref + "Try it: /workflow <ref> with <inputs>...".
#    STOP. Do NOT invoke the engine on the author's behalf.
```

**Always validate before declaring success.** "It looks plausible" is not the gate; `is_valid: true` is.

**Read the error class first, the message second.** The validator's structured findings carry an `error_class` and `identifying_names` — the table below is keyed on those, not on the prose `caller_message`. Diagnosing the wrong error class wastes attempts.

## The workflow YAML grammar

Authoritative reference: docs/reference/workflow-yaml.md (field-by-field grammar, deep-linked from every validator error's "Fix:" suggestion) and docs/explanation/workflow-foundation.md (locked cross-phase decisions).

### Top-level fields

```yaml
description: "Free-text — chat capsules + show CLI render this."

extends: [ "<bundle>/workflows/<other>" ]   # OPTIONAL, list, strict union
policy: [ ... ]                              # OPTIONAL, run-only; NOT inheritable

timeout_seconds: 60                          # OPTIONAL, workflow-level cooperative
                                             # deadline (positive int).

concurrency: sequential                      # 'sequential' (default) or 'auto'.
                                             # See "Concurrency" below.

nodes:                                       # REQUIRED mapping.
  <name>:
    type: <bundle>/nodes/<typename>          # 3-seg ref. The 14 built-ins are
                                             # kolsch/nodes/<bash|noop|…>.
    # ...type-specific config fields here.
    description: "..."                       # OPTIONAL display label.
    when: "{{ ... }}"                        # OPTIONAL Jinja gate.
    retry: 0                                 # OPTIONAL non-negative int.
    timeout_seconds: 30                      # OPTIONAL positive int.
    secrets: [SECRET_NAME, ...]              # OPTIONAL.
    copy_of: <other-node-name>               # OPTIONAL — fresh local copy.

graph:                                       # REQUIRED for runnable workflows.
                                             # Omit to declare a node library.
  edges:                                     # List; may be empty for a
                                             # single-node workflow.
    - from: <source-node-name>
      to: <target-node-name>
      binds: { <key>: <value-or-jinja> }     # OPTIONAL.
      branch: true|false                     # OPTIONAL — required for true/false
                                             # outbound from branchable sources.
```

**The runnable-vs-library marker is `graph:` presence.** A workflow with no `graph:` key is a `node_library` — importable via `extends:` but not directly runnable. Omitting `graph:` is intentional only for library workflows; if the brief asks for something runnable, always include `graph:`.

**Names are stable identifiers, NOT descriptions.** The `nodes:` mapping key IS the node's identity. The `description:` modifier is display-only — never use it to identify or reference a node.

### Node types — the 14 built-ins

The 14 reserved kolsch/nodes/<name> slots, grouped by purpose:

| Slot | Purpose | Type-specific config | Outputs |
|---|---|---|---|
| kolsch/nodes/bash | Run a shell command. | `command: "..."` (Jinja-rendered). | `stdout`, `stderr`, `exit_code`. Branchable: `exit_code == 0` is the branch value. |
| kolsch/nodes/noop | Pass-through structural node. | — | none. Common entry-point pattern. |
| kolsch/nodes/agent | Invoke a conversational agent. | `agent: <bundle>/agents/<name>`, optional `prompt: "..."`. | Agent-defined. |
| kolsch/nodes/workflow | Invoke a sub-workflow. | `workflow: <bundle>/workflows/<name>`, optional `with: { inputs }`. | Terminal-keyed dict. |
| kolsch/nodes/user_input | Pause and wait for a chat answer. | `prompt: "..."`. | `response: str`. Run status flips to `waiting_for_input`. |
| kolsch/nodes/read_file | Read a UTF-8 file. | `path: "..."`. | `content: str`. |
| kolsch/nodes/write_file | Write a UTF-8 file. | `path: "..."`, `content: "..."`. | `bytes_written: int`. |
| kolsch/nodes/list_files | List a directory. | `path: "..."`, optional `glob: "..."`. | `entries: list`. |
| kolsch/nodes/set_state | Write a workflow-state key. | `key: "..."`, `value: <jinja>`. | none. |
| kolsch/nodes/delete_state | Delete a workflow-state key. | `key: "..."`. | none. |
| kolsch/nodes/filter | Boolean predicate. | `over: <list-jinja>`, `condition: <jinja>`. | `result: bool`. |
| kolsch/nodes/map | Fan-out a worker over a list. | `over: <list-jinja>`, `apply: <worker-node>`, `item_as: <var-name>` (the worker reads each element as `inputs.<var-name>`), optional `parallel: true`, optional `concurrency: <int>`. | `results: list`. Worker referenced from `apply:` is NOT addressable from outside the map. |
| kolsch/nodes/branch | Pure conditional. | `condition: <jinja>`. | `value: bool`. ALWAYS branchable; requires both `branch: true` and `branch: false` outbound arms. |
| kolsch/nodes/compute | Compute a value. | `value: <jinja>`. | `value`. |

**Branchable sources** (edges may carry `branch: true|false` from these):
- kolsch/nodes/branch — always; both arms required.
- kolsch/nodes/bash — exit-code-driven; either arm optional.
- Authored Python `@node` whose return type is `bool` or `tuple[bool, BaseModel]` — either arm optional.

Anything else with `branch:` on an outbound edge → `WorkflowBranchOnNonBranchable`.

### Authored Python `@node` files

Authored nodes live at bundles/<target>/nodes/<name>.py. One `@node`-decorated `run` per file. Reference them from workflow YAML as `type: <target>/nodes/<name>`.

```python
from pydantic import BaseModel
from workflows import NodeContext, node


class Inputs(BaseModel):
    ticket_text: str


class Outputs(BaseModel):
    classification: str


@node(label="Classify ticket")
async def run(inputs: Inputs, ctx: NodeContext) -> Outputs:
    # ctx.state — workspace-wide read-only Mapping
    # ctx.secrets — declared secrets only
    # ctx.bundle — the bundle this node lives in
    # ctx.emit(subkind, payload) — emit a custom event
    return Outputs(classification="urgent" if "fire" in inputs.ticket_text else "normal")
```

**Signature rules (`@node` contract):**

- Function MUST be named `run`. ONE `@node`-decorated `run` per file.
- Param 1: a `pydantic.BaseModel` subclass (the inputs).
- Param 2: MUST be annotated exactly `NodeContext`.
- Param 3 (OPTIONAL): a `pydantic.BaseModel` subclass (config).
- All params POSITIONAL.

**Return types:**

- `BaseModel` subclass → non-branching with structured output.
- `bool` → branching only (no output payload).
- `tuple[bool, BaseModel subclass]` → branching with structured output.

Anything else → `WorkflowNodeReturnTypeInvalid`.

**ALWAYS call `introspect_node` on a freshly-written `@node` file before referencing it from the YAML.** The tool returns `signature_ok: true|false` plus a structured diagnostic — far cheaper to fix at the file level than to chase the failure through the workflow validator's catalog-load pass.

### Edges, binds, and fan-in

Edges shape: `{from, to, binds, branch}`. No `ref:` field.

- **`binds:`** keys flow into the receiver as `inputs.<key>`. Two edges into the same target binding the same key → `WorkflowBindCollision`. Fix: rename one bind, or drop one edge.
- **AND-strict fan-in.** A node fires once ALL incoming edges' source nodes complete. There is NO "wait for ANY" mode. For OR semantics, hoist the choice into a kolsch/nodes/branch upstream and route a single edge to the consumer.
- **`binds:` RHS** is either a YAML literal (string, int, list, dict, etc.) accepted verbatim OR a Jinja expression in `{{ }}`. The legacy bare `source.field` shorthand is gone.
- **`nodes.<name>.<field>`** Jinja references are validated graph-reachability-wise — `<name>` must be a transitive ancestor of the consuming node. Reference outside the ancestor closure → `WorkflowJinjaRefUnknown(reason="not_predecessor")`. Reference a field the source's output schema doesn't declare → `WorkflowJinjaRefUnknown(reason="unknown_field")`.
- **Workflow inputs** appear as `inputs.<field>` on the start node (the node with zero incoming edges).

### Modifiers — orthogonal to node type

These are accepted on every node:

- **`when: "{{ ... }}"`** — execution gate. The node fires ONLY when the Jinja expression is truthy. Skipped nodes leave `nodes.<name>` undefined.
- **`retry: <int>`** — retry count on failure (non-negative). v1 is intentionally simple — no backoff, no exception-class filtering.
- **`timeout_seconds: <int>`** — per-node positive deadline. Failure fires `NodeTimeoutExceeded`; the retry policy applies.
- **`description: "..."`** — display-only label for chat capsules + `show` CLI. Never used to identify or reference the node.

The edge-level modifier:

- **`branch: true|false`** — only fire this edge when the source's branch value matches. Required for both arms on a `branch` node; optional for `bash` / authored Python.

### Concurrency — when to opt in

Default is `concurrency: sequential` — the original single-step behavior. Workflows opt into parallel execution by setting `concurrency: auto` at the top level.

- **`concurrency: auto`** — the worklist schedules ready nodes concurrently when their state-write/read sets do not overlap. Two parallel-eligible nodes that BOTH touch the same `state.*` key → `WorkflowParallelStateConflict` at the run-start gate. Fix: add an explicit edge to serialise them, drop `concurrency: auto`, or remove the state write from one branch.
- **`parallel: true`** on a `map` node — workers run concurrently. A `parallel: true` map whose worker subgraph transitively writes state ALSO trips `WorkflowParallelStateConflict`.
- **`concurrency: <int>`** on a map — bounds the worker concurrency. Must be a positive integer; non-positive → `WorkflowInvalidConcurrency`.

Default to `sequential`. Opt into `auto` ONLY when the brief explicitly asks for parallelism OR when you can demonstrably show two independent branches with no state overlap. The gate is strict; safety first.

### Sub-workflows + map

- **kolsch/nodes/workflow** invokes another workflow as a node. Its `workflow:` field is a 3-seg ref. `with: { inputs }` passes the parent's data into the child's start-node inputs. Output is a dict keyed by the child's terminal node names.
- **kolsch/nodes/map** fans out a `worker` node over an `over:` iterable. The worker is referenced by name (defined in the same `nodes:` map) AND IS NOT ADDRESSABLE from outside the map — references like `nodes.<worker>.<field>` from sibling nodes outside the map raise `WorkflowMapperWorkerNotAddressable`. Read the collected results via `nodes.<map>.results` instead.
- Sub-workflow `extends:` cycles → `WorkflowExtendsCycle`. Sub-workflow `workflow:` call cycles → `WorkflowCallCycle`. Both are unrecoverable structural errors; flatten or break the chain.

## The error-class table

Every concrete `WorkflowValidationError` subclass with what the engine sees, the typical cause, and the fix. Reach for this on every `is_valid: false` return.

| Class | What the engine sees | Likely cause | Fix |
|---|---|---|---|
| `WorkflowDuplicateNode` | Two nodes share a name. | Copy-paste error in `nodes:`. | Rename one. The `identifying_names` carries the colliding name. |
| `WorkflowDuplicateEdge` | Same `from`/`to` pair declared twice. | Editing-merge artefact. | Remove the duplicate edge. |
| `WorkflowNodeNotDefined` | An `edge.from`, `edge.to`, `copy_of:`, or `map.apply:` names a node that isn't in `nodes:`. | Typo in a reference. | Spell the source's name correctly OR add it to `nodes:`. `identifying_names.location` tells you WHERE the bad ref lives. |
| `WorkflowAmbiguousStart` | More than one node has zero incoming edges. | Two candidate start nodes. | Add an incoming edge to all but one. The intended start has no inbound edges; the others must receive one. |
| `WorkflowNoStartNode` | Every node has an incoming edge (cycle). | Closed loop with no entry. | Remove one inbound edge to expose a start, or break the cycle. |
| `WorkflowUnreachableNode` | A node is declared but no edge can reach it from the start. | Orphan node left after refactor. | Either delete the orphan or add an edge from a reachable ancestor. |
| `WorkflowBindCollision` | Two inbound edges bind the same key on the same receiver. | Multi-edge fan-in collided on a key name. | Rename one of the colliding binds OR drop one edge. `identifying_names` carries `{node, bind}`. |
| `WorkflowBindTypeMismatch` | A rendered bind value's runtime type doesn't match the receiver's input model. | The Jinja expression rendered to the wrong shape. | Check the source's output schema and adjust the bind expression (often a missing filter like `\| from_json` or a wrong field path). |
| `WorkflowJinjaRefUnknown` | A `nodes.<X>.<field>` reference can't be resolved. | One of: `<X>` is not a transitive ancestor (`reason="not_predecessor"`); `<X>` resolves but `<field>` isn't in its output schema (`reason="unknown_field"`); `<X>`'s schema is unknown — e.g., authored-Python failed to import (`reason="not_declared"`). | Predecessor: add an edge that makes `<X>` reachable; unknown_field: pick a field the source actually emits; not_declared: fix the source's import. |
| `WorkflowMapperWorkerNotAddressable` | A node outside a `map` references the map's worker by name. | Cross-boundary reference into the map. | Reference `nodes.<map>.results` instead of the worker. |
| `WorkflowBranchOnNonBranchable` | An edge with `branch:` originates from a source that doesn't branch. | Added `branch:` to a non-branchable node. | Drop the `branch:` on the edge OR change the source to a branchable type (`branch`/`bash`/authored Python returning bool). |
| `WorkflowBranchMissingArm` | A `branch` node is missing the `branch: true` OR `branch: false` outbound arm. | Wired only one arm. | Add the missing arm. Both are required on kolsch/nodes/branch. |
| `WorkflowMapperWorkerMissingField` | A map node expects a field its worker doesn't declare on its output schema. | Worker model is wrong. | Add the field to the worker's `Outputs(BaseModel)`. |
| `WorkflowExtendsCycle` | An `extends:` chain loops. | A → B → A. | Break the chain by removing one `extends:` ref. |
| `WorkflowExtendsDepthExceeded` | An `extends:` chain exceeds 16. | Deeply nested composition. | Flatten the chain. |
| `WorkflowExtendsUnresolved` | An `extends:` ref doesn't resolve in the catalog. | Spelling, missing bundle, or typo. | Spell the ref correctly OR add the asset. |
| `WorkflowExtendsPolicyForbidden` | An extended workflow declares `policy:` rules. | Tried to share policy via `extends:`. | Remove `policy:` from the extended workflow OR invoke it directly. `policy:` is NEVER inherited. |
| `WorkflowCallCycle` | A kolsch/nodes/workflow chain loops. | Sub-workflow A calls B calls A. | Break the call cycle. |
| `WorkflowNodeImportFailed` | An authored-Python `@node` file can't be imported. | Missing module, syntax error, bad import inside. | Run `introspect_node` directly on the path — the error reads `ImportError` / `SyntaxError` with detail. |
| `WorkflowNodeMissingRun` | An authored-Python module has no top-level `run` symbol. | Function named something else. | Rename the function to `run`. |
| `WorkflowNodeNotDecorated` | `run` exists but lacks `@node`. | Forgot the decorator. | Add `@node(label="...")` to `run`. |
| `WorkflowNodeSignatureInvalid` | `run`'s signature doesn't match the required shape. | Wrong param count, non-BaseModel inputs, missing/wrong NodeContext annotation. | Param 1 must be `BaseModel`, param 2 must be `NodeContext` EXACTLY. Optional param 3 must be `BaseModel`. All positional. |
| `WorkflowNodeReturnTypeInvalid` | `run`'s return annotation isn't allowed. | Returns a dict, a tuple-other-than-`tuple[bool, BaseModel]`, or has no annotation. | Use `BaseModel`, `bool`, or `tuple[bool, BaseModel subclass]`. |
| `WorkflowInvalidTimeout` | `timeout_seconds` is zero or negative. | Authoring slip. | Set a positive integer OR remove the field entirely. |
| `WorkflowParallelStateConflict` | Two parallel-eligible nodes touch the same state key (in `concurrency: auto`) OR a `parallel: true` map's worker writes state. | Race condition on `state.*`. | Add an explicit edge to serialise the pair, drop `concurrency: auto`, OR remove the state write from one branch. |
| `WorkflowInvalidConcurrency` | A map's `concurrency:` is not a positive integer. | Authoring slip. | Set a positive integer OR remove the field. |
| `WorkflowInvalidRef` | A `type:` value isn't a 3-segment catalog ref. | Used a short name (`bash` instead of kolsch/nodes/bash). | Rewrite as <bundle>/<plural>/<name>. |
| `WorkflowBuiltinShadowed` | An authored Python node lives at a reserved kolsch/nodes/<name> slot. | Wrote bundles/kolsch/nodes/bash.py. | Rename the authored node so it doesn't collide with a built-in. |
| `WorkflowFromShapeError` | A `policy:` rule's `from:` value isn't a 3-seg ref. | Used a short name. | Rewrite as <bundle>/agents/<name> or <bundle>/workflows/<name>. |
| `WorkflowFromAssetTypeError` | A `policy:` rule's `from:` ref's asset_type isn't `agents` or `workflows`. | Pointed at a tool/skill/node. | Point at an agent or a workflow. |
| `WorkflowFromNotFound` | A `policy:` rule's `from:` ref doesn't resolve. | Typo or missing asset. | Spell the ref correctly OR add the asset. |

And the one warning (does NOT block validity):

| Class | What it means | Action |
|---|---|---|
| `WorkflowTimeoutShadowsNode` | A per-node `timeout_seconds` exceeds the workflow-level `timeout_seconds` and will never fire. | Either lower the node's timeout or raise the workflow's — surface as advice to the author; don't auto-fix without asking. |

**When the validator emits a class this table doesn't cover** (a future addition the skill hasn't been updated for), fall back to: "I don't recognise `<error_class>` from my table; here is the raw `caller_message` and my best guess at the cause." Don't fabricate a fix.

## Anti-patterns

These will read as plausible to a model that doesn't know the contract. Avoid them.

- **Putting behaviour in `description:`.** It's a display label, never read by the engine. The agent's behaviour lives in `prompt:` on the agent asset, not in the workflow's node description.
- **Reusing one node name for two invocations.** Use `copy_of: <other-node-name>` instead — `nodes:` keys are unique identifiers.
- **Using `inputs.*` from a Jinja expression on the start node.** The start node's `inputs.*` is the workflow's input shape — only `nodes.*` references on downstream nodes are validated against an upstream's output schema.
- **Writing `condition:` as a synonym for `when:`.** They are DIFFERENT: `when:` is the node-level gate; `condition:` is the primary config field on `branch` and `filter` nodes.
- **Emitting `concurrency: auto` on a workflow whose branches share state.** The gate rejects this with `WorkflowParallelStateConflict`. Default to sequential.
- **Referencing a map worker from outside its map.** Use `nodes.<map>.results`.
- **Calling `validate_workflow` against a `workflow_ref` for a draft you haven't written to disk yet.** The tool reads from disk. Use `yaml_path` for fresh drafts that aren't catalog-registered.
- **Declaring a workflow ready without a green validation.** Hardest rule. Never skip the validator.
- **Authoring at bundles/kolsch/nodes/<reserved-name>.py.** `WorkflowBuiltinShadowed`. The 14 built-in slots are reserved.
- **Writing to the `kolsch` package bundle.** Read-only by convention. See "Where to write" below.

## Canonical examples

Three examples ship in `examples/` next to this skill. Each subdirectory carries a `brief.txt`, a sample agent `transcript.md`, the final `workflow.yaml`, and any nodes/*.py files.

- **examples/small-bash-pipeline/** — three bash nodes, linear, no agents. Smallest possible end-to-end.
- **examples/triage-with-agent/** — agent classifier → branch → two terminal bash nodes. The canonical verification scenario.
- **examples/parallel-fanout/** — a map node over a list of items with `parallel: true`, no state writes. Demonstrates safe concurrency.

When the brief resembles one of these, read the matching example before drafting. The transcripts show the full iteration loop including a deliberate first-draft error and its fix.

## Where to write

**Always write to the configured target bundle. NEVER write to the `kolsch` package bundle.**

The `kolsch` bundle ships with the engine and is read-only by convention. New workflows + authored `@node` files land in a user bundle.

### Resolving the target bundle

On every session, discover the target before writing the first file:

1. **Check `platform.yaml`.** If `author_workflow.target_bundle: <name>` is set, use it.
2. **Otherwise, pick the first non-`kolsch` bundle** in `bundles/`. Run `bash: ls bundles/` (filtered) and take the first match.
3. **If only `kolsch` exists**, STOP and surface: "No user bundle is configured. Either set `author_workflow.target_bundle` in `platform.yaml`, or create a bundle with `kolsch bundles init <name>`." Do NOT write into bundles/kolsch/.

Once resolved, every `write_file` target lives under bundles/<target>/workflows/ (for YAML) or bundles/<target>/nodes/ (for authored Python). Surface the resolved target to the author on the first response so they know where files are landing.

## When to escalate (MAX_ATTEMPTS cap)

`MAX_ATTEMPTS = 5` for the draft → validate → revise loop.

**Why 5:** the validator's errors are structured and actionable; a brief that needs more than five revisions is one whose intent isn't yet clear to either party. Looping further wastes the author's time AND degrades the skill's signal (silent failures train bad habits).

**On hitting the cap:**

1. Stop validating. Do NOT spawn a sixth draft.
2. Summarise the closest draft: where it landed, what the LATEST validator errors were (`error_class`, `identifying_names`, the closest table row).
3. Ask the author for direction. Examples:
   - "I keep landing in `WorkflowBindCollision` between `<X>` and `<Y>`. Should `<X>`'s output drive `<bind_key>`, or `<Y>`'s? Or do you want a different fan-in shape?"
   - "The `count_rows.py` node fails `WorkflowNodeReturnTypeInvalid` — I'm trying `tuple[bool, RowCount]`, you may want a plain `RowCount`. What does the workflow do with the result?"

Escalation is a SUCCESS for the loop, not a failure. A clear question beats five more guesses.
