# Workflow YAML Reference

Field-by-field reference for the workflow YAML format. Every workflow
file lives at `bundles/<bundle>/workflows/<name>.yaml` and is
addressable as `<bundle>/workflows/<name>`.

For the *concepts* behind the format — DAG execution, event
sourcing, pause and resume — see the
[Workflows architecture page](../architecture/workflows.md). For
authoring custom node types, see
[Write a workflow node](../how-to/write-a-workflow-node.md).

## Top-level fields

The parser rejects unknown top-level keys (`extra="forbid"`).

| Field | Type | Default | Semantics |
|---|---|---|---|
| `description` | `str` | required | Free-form human description. Must be non-empty. |
| `extends` | `list[str]` | `[]` | Parent workflow refs to inherit from. |
| `policy` | `list[Rule]` | `[]` | Authorization rules for this workflow. The legacy `permissions:` key is rejected. |
| `timeout_seconds` | `int \| None` | `None` | Workflow-level cooperative deadline. Non-positive values are rejected by the validator. |
| `concurrency` | `"sequential" \| "auto"` | `"sequential"` | Worklist mode. `sequential` runs one node at a time; `auto` runs ready-set nodes in parallel. |
| `nodes` | `dict[str, NodeDef]` | `{}` | Node name → definition. Each value dispatches to a specific `NodeDef` by its `type:` field. |
| `graph` | `GraphDef \| None` | `None` | Presence flips the workflow kind from `node_library` to `workflow`. Empty `edges: []` still counts as present. |

A workflow *with* `graph:` is a runnable pipeline. A workflow
*without* `graph:` is a **node library** — a set of reusable node
definitions another workflow can `copy_of:` or reference by ref.

## Node modifiers (common to every node)

Every built-in node type and every authored node accepts this
common modifier set:

| Field | Type | Default | Semantics |
|---|---|---|---|
| `description` | `str \| None` | `None` | Human description. |
| `timeout_seconds` | `int \| None` | `None` | Per-node deadline. Wraps the runner with `with_timeout`; exceeding it raises `NodeTimeoutExceeded`. |
| `when` | `Any` | `None` | Node-level gate. YAML literal or Jinja-marked string (`"{{ ... }}"`). Falsy → node is skipped. |
| `retry` | `int` | `0` | Retry count. Wraps the runner with `with_retry`; on failure the runner sleeps 100 ms and re-fires, swallowing intermediate errors and surfacing only the final one. |
| `secrets` | `list[str]` | `[]` | Secret store keys to inject. |

Two legacy keys are rejected at parse:

- `instance_of:` — replaced by `copy_of:`.
- `collect:` — fan-in is now structural via multi-input edges.

## How binds and captures fit together

Every node body sees three read-only namespaces:

- **`inputs.*`** — what edges bound into this node via `binds:`. The
  values are Jinja-rendered against the sender's captured output at
  the moment the edge fires.
- **`state.*`** — the workflow's shared, mutable state dict. Only
  `set_state` writes; only `delete_state` unsets. Every node reads.
- **`nodes.<name>.*`** — the captured output of any earlier node in
  the DAG. Each node type below documents the shape it captures.

A downstream author writing `binds:` picks values from any of the
three. The receiver never sees the sender directly — it sees whatever
the edge chose to hand it under `inputs.*`.

## Node captured outputs — quick reference

Every node's captured output is available downstream via
`{{ nodes.<name>.<key> }}` and can be forwarded through an edge's
`binds:`.

| Node type | Captured shape |
|---|---|
| `kolsch/nodes/bash` | `{stdout: str, stderr: str, exit_code: int}` |
| `kolsch/nodes/agent` | The child agent's declared output schema. |
| `kolsch/nodes/user_input` | The resume payload — whatever `kolsch workflows resume` supplied. |
| `kolsch/nodes/noop` | `{}` |
| `kolsch/nodes/read_file` | `{content: str}` |
| `kolsch/nodes/write_file` | `{bytes_written: int}` |
| `kolsch/nodes/list_files` | `{entries: list[str]}` (leaf names only) |
| `kolsch/nodes/set_state` | `{}` |
| `kolsch/nodes/delete_state` | `{}` |
| `kolsch/nodes/filter` | `{result: list}` |
| `kolsch/nodes/compute` | `{value: Any}` |
| `kolsch/nodes/map` | `{results: list[<worker capture>]}` |
| `kolsch/nodes/branch` | `{value: bool}` |
| `kolsch/nodes/workflow` | The child workflow's terminal-node captured output dict. |
| Authored `@node` | Whatever the node returns — a `BaseModel`, a `bool`, or `(bool, BaseModel)`. |

## Node types

Every built-in node section below uses the same layout: **Config
schema**, **Binds accepted**, **State read / written**, **Return
shape**, **Errors surfaced**, **Example**.

### `kolsch/nodes/bash`

Run a shell command through the `kolsch/tools/bash` tool. Branchable on
exit code.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `command` | `str` | required | Shell command. Jinja-rendered. |
| `cwd` | `str \| None` | `None` | Working directory. Jinja-rendered. |
| `env` | `dict[str, Any] \| None` | `None` | Reserved. The runner renders but currently drops it — the bash tool does not accept an env argument today. Set environment via the wrapping shell instead. |

**Binds accepted:** any keys the `command`, `cwd`, or `env` Jinja
expressions reference under `inputs.*`.

**State read / written:** reads any `state.*` referenced by the
Jinja expressions. Writes nothing.

**Return shape:**

```json
{
  "stdout": "…",
  "stderr": "…",
  "exit_code": 0
}
```

**Errors surfaced:** `BashNonZeroExit` when `exit_code != 0` (unless
the caller opts out via the tool's `check` argument),
`NodeTimeoutExceeded` on `timeout_seconds`.

**Example:**

```yaml
run_tests:
  type: kolsch/nodes/bash
  command: "pytest {{ inputs.suite }}"
  cwd: "{{ state.repo_root }}"
```

### `kolsch/nodes/agent`

Invoke a catalogued agent, spawning a child session.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `agent` | `str` | required | Catalog ref of the agent. |
| `outputs` | `IOSpec \| None` | `None` | Per-invocation override for the agent's output schema. |

**Binds accepted:** narrowed to the child agent's declared
`IOSpec.inputs` — the parser enforces the child's Inputs model at
run-start.

**State read / written:** reads any `state.*` referenced by binds.
Writes nothing.

**Return shape:** the child agent's declared output schema (a dict
shaped by its `IOSpec.outputs`, or the `outputs:` override).

**Errors surfaced:** `WorkflowBindTypeMismatch` when a bound value
fails the child's Inputs model; `WorkflowNodeFailed` on any
child-agent error; `NodeTimeoutExceeded` on `timeout_seconds`.

**Example:**

```yaml
summarize:
  type: kolsch/nodes/agent
  agent: kolsch/agents/summarizer
```

### `kolsch/nodes/user_input`

Pause the run and wait for a chat reply.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `prompt` | `str` | required | Prompt shown to the user. Jinja-rendered. |

**Binds accepted:** any keys the `prompt` Jinja expression references.

**State read / written:** reads any `state.*` referenced by `prompt`.
Writes nothing.

**Return shape:** the resume payload — the dict passed to
`kolsch workflows resume <run_id>`. The node **never returns during
the pause** — the runtime raises `WorkflowPausedForInput` at fire
time and the value only lands on resume.

**Errors surfaced:** `WorkflowPausedForInput` at fire time (expected;
consumed by the resume flow), `NodeTimeoutExceeded` on
`timeout_seconds`.

**Example:**

```yaml
confirm:
  type: kolsch/nodes/user_input
  prompt: "Deploy to {{ inputs.env }}? (yes/no)"
```

### `kolsch/nodes/noop`

Fan-out placeholder that captures nothing.

**Config schema:** no type-specific fields.

**Binds accepted:** any (ignored — noop reads nothing from `inputs`).

**State read / written:** none.

**Return shape:** `{}`.

**Errors surfaced:** none of its own; `NodeTimeoutExceeded` on
`timeout_seconds`.

**Example:**

```yaml
fanout:
  type: kolsch/nodes/noop
  description: "Fan out to parallel branches"
```

### `kolsch/nodes/read_file`

Read a file through the `kolsch/tools/read_file` tool.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `path` | `str` | required | File path. Jinja-rendered. |

**Binds accepted:** any keys the `path` Jinja expression references.

**State read / written:** reads any `state.*` referenced by `path`.
Writes nothing.

**Return shape:**

```json
{ "content": "…" }
```

**Errors surfaced:** `WorkflowFileAccessDenied` when the resolved
path violates the workspace path policy; `FileNotFound` when the file
doesn't exist; `NodeTimeoutExceeded` on `timeout_seconds`.

**Example:**

```yaml
load_config:
  type: kolsch/nodes/read_file
  path: "{{ state.repo_root }}/config.yaml"
```

### `kolsch/nodes/write_file`

Write a file through the `kolsch/tools/write_file` tool.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `path` | `str` | required | Target path. Jinja-rendered. |
| `content` | `str \| bytes` | required | Content. Jinja-rendered when a string. |

**Binds accepted:** any keys referenced by `path` or `content`.

**State read / written:** reads any `state.*` referenced by the
Jinja expressions. Writes nothing.

**Return shape:**

```json
{ "bytes_written": 1024 }
```

**Errors surfaced:** `WorkflowFileAccessDenied` when the resolved
path violates the workspace path policy; `NodeTimeoutExceeded` on
`timeout_seconds`.

**Example:**

```yaml
save_report:
  type: kolsch/nodes/write_file
  path: "/tmp/report-{{ inputs.run_id }}.md"
  content: "{{ inputs.report_body }}"
```

### `kolsch/nodes/list_files`

Enumerate directory entries. The `pattern:` field maps to the
underlying tool's `glob:` argument.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `path` | `str` | required | Directory to list. |
| `pattern` | `str \| None` | `None` | Optional glob-like filter. |

**Binds accepted:** any keys referenced by `path` or `pattern`.

**State read / written:** reads any `state.*` referenced by the
Jinja expressions. Writes nothing.

**Return shape:**

```json
{ "entries": ["a.md", "b.md"] }
```

Entries are leaf names only, not full paths.

**Errors surfaced:** `WorkflowFileAccessDenied` when the resolved
path violates the workspace path policy; `NodeTimeoutExceeded` on
`timeout_seconds`.

**Example:**

```yaml
find_specs:
  type: kolsch/nodes/list_files
  path: "docs/specs"
  pattern: "*.md"
```

### `kolsch/nodes/set_state`

Write keys into the workflow's run-scoped state dict.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `keys` | `dict[str, Any]` | required | Key → value map. Values are YAML literals or Jinja-marked strings. |

**Binds accepted:** any keys referenced by the value expressions.

**State read / written:** writes each `keys.<name>` into `state.<name>`.
Reads whatever the value expressions reference.

**Return shape:** `{}`.

**Errors surfaced:** none of its own; `NodeTimeoutExceeded` on
`timeout_seconds`.

**Example:**

```yaml
stash_result:
  type: kolsch/nodes/set_state
  keys:
    last_summary: "{{ inputs.summary }}"
    ran_at: "{{ now() }}"
```

### `kolsch/nodes/delete_state`

Remove keys from the workflow state dict.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `keys` | `list[str]` | required | State keys to unset. |

**Binds accepted:** none used.

**State read / written:** unsets each listed key from `state`.

**Return shape:** `{}`.

**Errors surfaced:** none of its own; `NodeTimeoutExceeded` on
`timeout_seconds`.

**Example:**

```yaml
cleanup:
  type: kolsch/nodes/delete_state
  keys: [last_summary, ran_at]
```

### `kolsch/nodes/filter`

Reduce a list by a predicate.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `over` | `Any` | required | Source list. Literal or Jinja-marked. |
| `condition` | `Any` | required | Predicate. Literal or Jinja-marked. |
| `item_as` | `str` | `"item"` | Loop variable inside `condition`. |

**Binds accepted:** any keys referenced by `over` or `condition`.

**State read / written:** reads any `state.*` referenced by the
Jinja expressions. Writes nothing.

**Return shape:**

```json
{ "result": [ /* filtered subset of `over` */ ] }
```

**Errors surfaced:** `WorkflowNodeFailed` on a predicate that raises
during evaluation; `NodeTimeoutExceeded` on `timeout_seconds`.

**Example:**

```yaml
open_tickets:
  type: kolsch/nodes/filter
  over: "{{ inputs.tickets }}"
  condition: "{{ t.status == 'open' }}"
  item_as: t
```

### `kolsch/nodes/compute`

Evaluate a Jinja expression.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `value` | `Any` | required | YAML literal or Jinja-marked string. Literals pass through unchanged. |

**Binds accepted:** any keys referenced by `value`.

**State read / written:** reads any `state.*` referenced by `value`.
Writes nothing.

**Return shape:**

```json
{ "value": "…" }
```

The `value` field is whatever the expression evaluated to — any type.

**Errors surfaced:** `WorkflowNodeFailed` on an expression that
raises; `NodeTimeoutExceeded` on `timeout_seconds`.

**Example:**

```yaml
build_url:
  type: kolsch/nodes/compute
  value: "https://api.example.com/{{ inputs.tenant }}/items"
```

### `kolsch/nodes/map`

Fan out over a list, dispatching a worker node per element.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `apply` | `str` | required | Name of the worker node in the SAME workflow. |
| `over` | `Any` | required | Source list. Rendered once at fire time. |
| `item_as` | `str` | required | Per-iteration variable name. |
| `with` (alias `with_`) | `dict[str, Any]` | `{}` | Constants passed to each iteration. Rendered once. |
| `parallel` | `bool` | `False` | Run iterations concurrently. |
| `concurrency` | `int \| None` | `None` | Cap on in-flight iterations. `None` = unbounded. |

**Binds accepted:** narrowed to the worker node's Inputs model. Each
iteration is bound `{item_as: <element>, **with}`.

**State read / written:** reads any `state.*` referenced by `over`
or `with`. Writes nothing.

**Return shape:**

```json
{ "results": [ /* one entry per iteration, shaped by the worker's return */ ] }
```

**Errors surfaced:** `WorkflowNodeFailed` when any worker iteration
fails and no per-iteration retry absorbs it; `NodeTimeoutExceeded` on
`timeout_seconds`.

**Example:**

```yaml
process_each:
  type: kolsch/nodes/map
  apply: worker
  over: "{{ inputs.items }}"
  item_as: item
  parallel: true
  concurrency: 4
```

### `kolsch/nodes/branch`

Boolean route gate. Drives which outgoing edges fire via
`branch: true` / `branch: false` on the edge.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `condition` | `Any` | required | Literal `true`/`false` or Jinja-marked. Coerced to `bool`. |

**Binds accepted:** any keys referenced by `condition`.

**State read / written:** reads any `state.*` referenced by
`condition`. Writes nothing.

**Return shape:**

```json
{ "value": true }
```

**Branchable sources:** an edge's `branch: true` / `branch: false`
selector is valid only when the sender's captured output has a
`value: bool` field. The three branchable sources are: any
`kolsch/nodes/branch`, any `kolsch/nodes/bash` (via `exit_code == 0`
convention when opted in), and any authored `@node` whose return
type is `bool` or `(bool, BaseModel)`.

**Errors surfaced:** `WorkflowNodeFailed` on a condition expression
that raises; `NodeTimeoutExceeded` on `timeout_seconds`.

**Example:**

```yaml
gate:
  type: kolsch/nodes/branch
  condition: "{{ inputs.exit_code == 0 }}"
```

Paired edges:

```yaml
edges:
  - from: gate
    to: deploy
    branch: true
  - from: gate
    to: rollback
    branch: false
```

### `kolsch/nodes/workflow`

Invoke a sub-workflow. The child run gets its own JSONL and shares
the parent's trace id.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `ref` | `str` | required | 3-seg catalog ref of the child workflow. |

**Binds accepted:** narrowed to the child workflow's declared
inputs — the runtime enforces the child's Inputs model at run-start.

**State read / written:** the child receives its own fresh `state`;
parent state is not visible inside the child.

**Return shape:** the child workflow's terminal-node captured output
dict — the dict returned by `compute_terminal_output_dict` on the
child's terminal node(s). Downstream nodes read individual keys via
`{{ nodes.<sub_workflow_node_name>.<terminal_key> }}`.

**Errors surfaced:** `ChildWorkflowFailed` when the child's own
terminal state is `FAILED`; `WorkflowCallCycle` when the ref forms a
cycle across workflows; `NodeTimeoutExceeded` on `timeout_seconds`.

**Example:**

```yaml
run_sub:
  type: kolsch/nodes/workflow
  ref: kolsch/workflows/deploy
```

### `copy_of:` — inheritance from another node

Reuse a node definition and override selected fields. Uses
`copy_of:` instead of `type:`.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `copy_of` | `str` | required | Name of another node in the same workflow. |
| *(extras)* | `Any` | — | Any additional field overrides the source. `type:` must NOT be present. |

**Binds / state / return / errors:** inherited from the source node's
type. Overrides land field-by-field.

**Example:**

```yaml
retry_tests:
  copy_of: run_tests
  retry: 2
```

### Authored `@node` types

Any `type:` value pointing at an authored node's 3-segment catalog
ref (e.g. `mybundle/nodes/classify`) lands as `CatalogNodeDef`. The
parser rejects non-3-segment refs with `WorkflowInvalidRef`.
Type-specific fields land untouched on the node — the validator
checks them against the authored node's declared `Inputs` Pydantic
model at run-start.

**Config schema:**

| Field | Type | Default | Semantics |
|---|---|---|---|
| `type` | `str` | required | 3-seg catalog ref. |
| `copy_of` | `str \| None` | `None` | Optional inheritance pointer. |
| *(extras)* | `Any` | — | Validated against the authored node's Inputs model. |

**Binds accepted:** narrowed to the authored node's `Inputs` model.

**State read / written:** whatever the authored body does with its
`NodeContext`.

**Return shape:** whatever the node returns. A `@node` may return a
`BaseModel` (captured as its dict), a `bool` (captured as
`{value: bool}` — branchable), or `(bool, BaseModel)` (captured as
`{value: bool, ...model_dict}` — branchable and rich).

**Errors surfaced:** any authored exceptions surface as
`WorkflowNodeFailed`. See [Write a workflow node](../how-to/write-a-workflow-node.md).

**Example:**

```yaml
classify:
  type: mybundle/nodes/classify
  threshold: 0.75
  labels: [pos, neg, neutral]
```

## Edges

`EdgeDef` — the parser rejects unknown edge keys.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `from` (alias `from_`) | `str` | required | Source node name. |
| `to` | `str` | required | Target node name. Multiple edges sharing a `to:` form a fan-in. |
| `binds` | `dict[str, Any]` | `{}` | Key → value map projected onto the target's `inputs.*` namespace. RHS is literal or Jinja-marked. |
| `branch` | `bool \| None` | `None` | When set, edge fires only if the source returned a matching bool. Valid only from branchable sources (see the `branch` node's "Branchable sources" note). |
| `when` | `Any` | `None` | Edge-level gate. Literal or Jinja-marked. |

Two legacy edge keys are rejected: `condition:` (rename to `when:`)
and `ref:` (fan-in is now structural via multi-input edges).

## Worked example — end-to-end chain

Show `config → binds → state → captured output` handoff end-to-end.
A minimal three-node chain: read a file, list a directory, summarize
both.

```yaml
description: Read a manifest and enumerate its target dir.

nodes:
  read_manifest:
    type: kolsch/nodes/read_file
    path: "manifest.yaml"

  list_targets:
    type: kolsch/nodes/list_files
    path: "{{ inputs.dir }}"

  summarize:
    type: kolsch/nodes/agent
    agent: kolsch/agents/summarizer

graph:
  edges:
    - from: read_manifest
      to: list_targets
      binds:
        dir: "{{ inputs.content | from_yaml | attr('target_dir') }}"
    - from: list_targets
      to: summarize
      binds:
        manifest: "{{ nodes.read_manifest.content }}"
        entries: "{{ inputs.entries }}"
```

Walk-through:

- `read_manifest` captures `{content: "..."}`.
- The edge into `list_targets` reads the sender's `content`, parses
  YAML, plucks `target_dir`, binds it as `inputs.dir`.
- `list_targets` captures `{entries: [...]}`.
- The edge into `summarize` binds two keys: `manifest` from
  `read_manifest.content` (reached across the DAG by name), and
  `entries` from the immediate sender's `inputs.entries`.

## Full skeleton

```yaml
description: Build, test, and conditionally deploy a service.
extends: []
policy: []
timeout_seconds: 1800
concurrency: auto

nodes:
  read_manifest:
    type: kolsch/nodes/read_file
    path: "manifest.yaml"

  run_tests:
    type: kolsch/nodes/bash
    command: "pytest {{ inputs.suite }}"
    cwd: "{{ state.repo_root }}"
    timeout_seconds: 600
    retry: 1

  gate:
    type: kolsch/nodes/branch
    condition: "{{ inputs.exit_code == 0 }}"

  build_each:
    type: kolsch/nodes/map
    apply: build_one
    over: "{{ inputs.targets }}"
    item_as: target
    parallel: true
    concurrency: 3

  build_one:
    type: kolsch/nodes/bash
    command: "make build TARGET={{ inputs.target }}"

  summarize:
    type: kolsch/nodes/agent
    agent: kolsch/agents/summarizer

  save_report:
    type: kolsch/nodes/write_file
    path: "/tmp/report.md"
    content: "{{ inputs.summary }}"

  rollback:
    type: kolsch/nodes/bash
    command: "./scripts/rollback.sh"

  stash:
    type: kolsch/nodes/set_state
    keys:
      last_run: "{{ now() }}"

graph:
  edges:
    - from: read_manifest
      to: run_tests
      binds:
        suite: "{{ inputs.content | from_yaml | attr('suite') }}"
    - from: run_tests
      to: gate
      binds:
        exit_code: "{{ inputs.exit_code }}"
    - from: gate
      to: build_each
      branch: true
      binds:
        targets: "{{ state.targets }}"
    - from: gate
      to: rollback
      branch: false
    - from: build_each
      to: summarize
    - from: summarize
      to: save_report
      binds:
        summary: "{{ inputs.summary }}"
    - from: save_report
      to: stash
      when: "{{ state.record_history }}"
```

---

The concepts are in the [Workflows architecture page](../architecture/workflows.md).
The authoring contract for custom node types is in
[Write a workflow node](../how-to/write-a-workflow-node.md).
