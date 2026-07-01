# Build a workflow

A **workflow** is a YAML file that names some nodes and draws edges
between them. The executor topologically sorts the graph, fires each
node with its inputs, captures the output, and moves on. All the
plumbing — argument passing, branching, state, JSONL — is handled by
the 14 built-in node types. This guide shows you how to author one.

For a per-field reference see the
[Workflow YAML reference](../reference/workflow-yaml.md). To write a
custom node in Python, see [Write a workflow node](write-a-workflow-node.md).

## What a workflow looks like

Every workflow YAML has two top-level blocks:

- `nodes:` — a map of `<name>: <NodeDef>`. Each entry sets `type:` to
  one of the 14 built-in 3-segment refs (`kolsch/nodes/bash`,
  `kolsch/nodes/agent`, `kolsch/nodes/branch`, ...) or a catalog ref
  for an authored Python node.
- `graph:` — a `{ edges: [...] }` block. Each edge has `from`, `to`,
  optional `binds:` (Jinja expressions that become the target's
  inputs) and an optional `branch: true|false` field for gate arms.

An optional top-level `description:`, `policy:` (authz rules),
`timeout_seconds:`, and `concurrency: sequential | auto` round it
out. Presence of the `graph:` key — not the number of edges — is
what makes the file *runnable* vs. a node library.

## A concrete worked example

Run tests; on success write a summary file, on failure trigger an
alert agent.

```yaml
description: "Run tests, then branch on the result."

nodes:
  run_tests:
    type: kolsch/nodes/bash
    command: "pytest -q"
    description: "Structural branch source — exit_code drives dispatch."

  gate:
    type: kolsch/nodes/branch
    condition: "{{ inputs.passed }}"

  write_summary:
    type: kolsch/nodes/write_file
    path: "reports/last-run.txt"
    content: |
      PASS
      {{ inputs.stdout }}

  alert:
    type: kolsch/nodes/agent
    agent: kolsch/agents/oncall_alerter
    description: "Single-shot agent — receives the failing stderr."

graph:
  edges:
    - from: run_tests
      to: gate
      binds:
        passed: "{{ nodes.run_tests.exit_code == 0 }}"

    - from: gate
      to: write_summary
      branch: true
      binds:
        stdout: "{{ nodes.run_tests.stdout }}"

    - from: gate
      to: alert
      branch: false
      binds:
        stderr: "{{ nodes.run_tests.stderr }}"
```

Notice: `run_tests` is *itself* a branchable source (`bash`
exit-code semantics — see `branching-demo.yaml` in the starter
bundle), so the explicit `branch` node here is optional. It's shown
for clarity when the gate condition is non-trivial.

## The most useful node types

- **`bash`** — runs a shell command. Captures
  `{stdout, stderr, exit_code}`. A non-zero exit either routes down
  a `branch: false` edge or fails the run.

  ```yaml
  greet:
    type: kolsch/nodes/bash
    command: "echo 'hi {{ inputs.name }}'"
  ```

- **`agent`** — spawns a single-shot child session against a
  catalogued agent. Bound inputs become the agent's typed inputs;
  the agent's structured `agent.message.content` is the captured
  output.

  ```yaml
  classify:
    type: kolsch/nodes/agent
    agent: kolsch/agents/classifier
  ```

- **`branch`** — evaluates `condition:` (literal or Jinja) and
  captures `{value: bool}`. Outgoing edges with matching `branch:
  true|false` fire; the other arm is pruned.

- **`set_state` / `read_file` / `write_file`** — the workhorse
  side-effect nodes. `set_state` writes into the workflow's live
  state dict; `read_file`/`write_file` route through the same
  path-policy pipeline the bash tool uses.

## Wiring nodes with binds

Edges carry a `binds:` map. Each RHS is either a YAML literal or a
Jinja-marked string. The Jinja scope sees:

- `inputs.*` — the workflow's top-level `--input` values (visible
  at every node).
- `nodes.<name>.*` — captured outputs of *any completed upstream
  node* (e.g. `nodes.run_tests.stdout`).
- `state.*` — the live state dict, evolved by `set_state`.

At the target node, edge-bound values land under `inputs.*`
(overlaid on the workflow inputs). That's why in the example above
`gate` reads `{{ inputs.passed }}` even though `passed` was computed
from `nodes.run_tests.exit_code` on the edge.

```yaml
- from: prep
  to: classify
  binds:
    context: "{{ nodes.prep.stdout }}"
    tone: "polite"                        # literal — no Jinja
```

## Branching

Two source types produce a boolean the edge dispatcher uses:

- `kolsch/nodes/bash` — the dispatcher treats `exit_code == 0` as
  `True`.
- `kolsch/nodes/branch` — captures `{value: bool}` from its
  `condition:`.

An edge with `branch: true` fires only when the source's boolean is
`True`; `branch: false` fires only when it's `False`. Edges without
`branch:` fire unconditionally. The unfired arm and everything
downstream emit `workflow.node.pruned` events. Attaching `branch:`
edges to a non-branchable source is caught at validate time.

## State

`set_state` mutates `state.*`; `delete_state` removes keys. Use them
for accumulators, retry counters, and any value that has to survive
past one node's capture. Values are Jinja-eligible and see the
*live* state dict — a chain of increments works out of the box.

```yaml
init:
  type: kolsch/nodes/set_state
  keys:
    attempts: 0

bump:
  type: kolsch/nodes/set_state
  keys:
    attempts: "{{ state.attempts + 1 }}"

report:
  type: kolsch/nodes/bash
  command: "echo attempts={{ state.attempts }}"
```

Each write emits a `workflow.state.mutated` event; the JSONL is the
audit trail.

## Where to put the file

`bundles/<bundle>/workflows/<name>.yaml`. The catalog indexes it as
the 3-segment ref `<bundle>/workflow/<name>` (singular
`workflow`). List with:

```
kolsch workflows list
```

## How to run the workflow

```
kolsch workflows run <bundle>/workflow/<name> \
    --input key=value --input other=42
```

Useful flags: `--dry-run` prints the topological plan without
executing, `--follow` tails the JSONL as events land, `--format
json` prints machine-readable envelopes. For a pause-and-resume
workflow (`user_input` nodes), the run exits early with
`waiting_for_input`; resume with:

```
kolsch workflows resume <wrk_id> --input response=<text>
```

## How to see what happened

Every run gets `runs/<wrk_id>/events.jsonl` under the workspace
root — one line per event. Look at `workflow.node.started`,
`workflow.node.completed` (carries `captured_output` and the
`branch_value` when applicable), `workflow.node.pruned` for skipped
arms, and `workflow.state.mutated` for state writes.

```
kolsch workflows events <wrk_id>          # list events
kolsch workflows show <ref>                # inspect the parsed graph
kolsch workflows fork <wrk_id> --at <event_id>  # replay from a point
```

## Common failure modes

- **Load-time validation errors** — YAML shape, `type:` not
  3-segment, deprecated keys (`permissions:`, `instance_of:`,
  edge-level `condition:`), unknown node type, DAG cycle,
  bind-collision. The parser and validator surface them with file
  path and line number; the run never starts.
- **Runtime bind errors** — Jinja references a missing
  `nodes.<name>.<field>`. Raises `WorkflowJinjaRefUnknown` and
  fails the run.
- **Non-zero bash exit with no `branch:` edges** — `BashNonZeroExit`
  is fatal. Add a `branch: false` arm to recover.
- **Pruned branches** — the arm you thought would fire didn't. The
  JSONL will carry `workflow.node.pruned` with the source's
  `branch_value`; check whether your `condition:` renders to the
  boolean you expect.
- **Agent permission denials** — the starter `platform.yaml` maps
  `kolsch/bash` to `prompt`, which collapses to `deny` under
  headless `workflows run`. Either add an explicit `allow` rule in
  the workflow's `policy:` field or swap to an authored node.
