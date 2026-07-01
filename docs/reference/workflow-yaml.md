# Workflow YAML Reference

Field-by-field reference for the workflow YAML format. Every workflow
file lives at `bundles/<bundle>/workflows/<name>.yaml` and is
addressable as `<bundle>/workflow/<name>`.

For the *concepts* behind the format — DAG execution, event
sourcing, pause and resume — see the
[Workflows architecture page](../architecture/workflows.md). For
authoring custom node types, see
[Write a workflow node](../how-to/write-a-workflow-node.md).

## Top-level fields

The parser rejects unknown top-level keys (`extra="forbid"`).

| Field | Type | Default | Semantics |
|---|---|---|---|
| `description` | `str` | `""` | Free-form human description. |
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
| `timeout_seconds` | `int \| None` | `None` | Per-node deadline. |
| `when` | `Any` | `None` | Node-level gate. YAML literal or Jinja-marked string (`"{{ ... }}"`). Falsy → node is skipped. |
| `retry` | `int` | `0` | Retry count. Wraps the runner with `with_retry`. |
| `secrets` | `list[str]` | `[]` | Secret store keys to inject. |

Two legacy keys are rejected at parse:

- `instance_of:` — replaced by `copy_of:`.
- `collect:` — fan-in is now structural via multi-input edges.

## Node types

### `kolsch/nodes/bash`

Run a shell command through the `kolsch/bash` tool. Branchable on
exit code.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `command` | `str` | required | Shell command. Jinja-rendered. |
| `cwd` | `str \| None` | `None` | Working directory. Jinja-rendered. |
| `env` | `dict[str, Any] \| None` | `None` | Env-var overrides. Values Jinja-rendered. |

```yaml
run_tests:
  type: kolsch/nodes/bash
  command: "pytest {{ inputs.suite }}"
  cwd: "{{ state.repo_root }}"
  env:
    CI: "1"
```

### `kolsch/nodes/agent`

Invoke a catalogued agent, spawning a child session.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `agent` | `str` | required | Catalog ref of the agent. |
| `outputs` | `IOSpec \| None` | `None` | Per-invocation override for the agent's output schema. |

```yaml
summarize:
  type: kolsch/nodes/agent
  agent: kolsch/agents/summarizer
```

### `kolsch/nodes/user_input`

Pause the run and wait for a chat reply.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `prompt` | `str` | required | Prompt shown to the user. Jinja-rendered. |

```yaml
confirm:
  type: kolsch/nodes/user_input
  prompt: "Deploy to {{ inputs.env }}? (yes/no)"
```

### `kolsch/nodes/noop`

Fan-out placeholder that captures nothing. No type-specific fields.

```yaml
fanout:
  type: kolsch/nodes/noop
  description: "Fan out to parallel branches"
```

### `kolsch/nodes/read_file`

Read a file through the `kolsch/read_file` tool.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `path` | `str` | required | File path. Jinja-rendered. |

```yaml
load_config:
  type: kolsch/nodes/read_file
  path: "{{ state.repo_root }}/config.yaml"
```

### `kolsch/nodes/write_file`

Write a file through the `kolsch/write_file` tool.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `path` | `str` | required | Target path. Jinja-rendered. |
| `content` | `str \| bytes` | required | Content. Jinja-rendered when a string. |

```yaml
save_report:
  type: kolsch/nodes/write_file
  path: "/tmp/report-{{ inputs.run_id }}.md"
  content: "{{ inputs.report_body }}"
```

### `kolsch/nodes/list_files`

Enumerate directory entries.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `path` | `str` | required | Directory to list. |
| `pattern` | `str \| None` | `None` | Optional glob-like filter. |

```yaml
find_specs:
  type: kolsch/nodes/list_files
  path: "docs/specs"
  pattern: "*.md"
```

### `kolsch/nodes/set_state`

Write keys into the workflow's run-scoped state dict.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `keys` | `dict[str, Any]` | required | Key → value map. Values are YAML literals or Jinja-marked strings. |

```yaml
stash_result:
  type: kolsch/nodes/set_state
  keys:
    last_summary: "{{ inputs.summary }}"
    ran_at: "{{ now() }}"
```

### `kolsch/nodes/delete_state`

Remove keys from the workflow state dict.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `keys` | `list[str]` | required | State keys to unset. |

```yaml
cleanup:
  type: kolsch/nodes/delete_state
  keys: [last_summary, ran_at]
```

### `kolsch/nodes/filter`

Reduce a list by a predicate. Captures `{result: [...]}`.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `over` | `Any` | required | Source list. Literal or Jinja-marked. |
| `condition` | `Any` | required | Predicate. Literal or Jinja-marked. |
| `item_as` | `str` | `"item"` | Loop variable inside `condition`. |

```yaml
open_tickets:
  type: kolsch/nodes/filter
  over: "{{ inputs.tickets }}"
  condition: "{{ t.status == 'open' }}"
  item_as: t
```

### `kolsch/nodes/compute`

Evaluate a Jinja expression. Captures `{value: ...}`.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `value` | `Any` | required | YAML literal or Jinja-marked string. Literals pass through unchanged. |

```yaml
build_url:
  type: kolsch/nodes/compute
  value: "https://api.example.com/{{ inputs.tenant }}/items"
```

### `kolsch/nodes/map`

Fan out over a list, dispatching a worker node per element.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `apply` | `str` | required | Name of the worker node in the SAME workflow. |
| `over` | `Any` | required | Source list. Rendered once at fire time. |
| `item_as` | `str` | required | Per-iteration variable name. |
| `with` (alias `with_`) | `dict[str, Any]` | `{}` | Constants passed to each iteration. Rendered once. |
| `parallel` | `bool` | `False` | Run iterations concurrently. |
| `concurrency` | `int \| None` | `None` | Cap on in-flight iterations. `None` = unbounded. |

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
`branch: true` / `branch: false`.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `condition` | `Any` | required | Literal `true`/`false` or Jinja-marked. Coerced to `bool`. |

```yaml
gate:
  type: kolsch/nodes/branch
  condition: "{{ inputs.exit_code == 0 }}"
```

### `kolsch/nodes/workflow`

Invoke a sub-workflow. The child run gets its own JSONL and shares
the parent's trace id.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `ref` | `str` | required | 3-seg catalog ref of the child workflow. |

```yaml
run_sub:
  type: kolsch/nodes/workflow
  ref: kolsch/workflows/deploy
```

### `copy_of:` — inheritance from another node

Reuse a node definition and override selected fields. Uses
`copy_of:` instead of `type:`.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `copy_of` | `str` | required | Name of another node in the same workflow. |
| *(extras)* | `Any` | — | Any additional field overrides the source. `type:` must NOT be present. |

```yaml
retry_tests:
  copy_of: run_tests
  retry: 2
  env:
    CI: "1"
    RETRY: "true"
```

### Authored `@node` types

Any `type:` ref that is not one of the 14 reserved
`kolsch/nodes/<name>` slots lands as `CatalogNodeDef`. Type-specific
fields land untouched on the node — the validator checks them
against the authored node's declared `Inputs` Pydantic model at
run-start.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `type` | `str` | required | 3-seg catalog ref, e.g. `mybundle/nodes/classify`. |
| `copy_of` | `str \| None` | `None` | Optional inheritance pointer. |
| *(extras)* | `Any` | — | Node-specific config. Validated against the authored node's Inputs model. |

```yaml
classify:
  type: mybundle/nodes/classify
  threshold: 0.75
  labels: [pos, neg, neutral]
```

See [Write a workflow node](../how-to/write-a-workflow-node.md) for the
authoring contract.

## Edges

`EdgeDef` — the parser rejects unknown edge keys.

| Field | Type | Default | Semantics |
|---|---|---|---|
| `from` (alias `from_`) | `str` | required | Source node name. |
| `to` | `str` | required | Target node name. Multiple edges sharing a `to:` form a fan-in. |
| `binds` | `dict[str, Any]` | `{}` | Key → value map projected onto the target's `inputs.*` namespace. RHS is literal or Jinja-marked. |
| `branch` | `bool \| None` | `None` | When set, edge fires only if the source returned a matching bool. Valid only from branchable sources. |
| `when` | `Any` | `None` | Edge-level gate. Literal or Jinja-marked. |

Two legacy edge keys are rejected: `condition:` (rename to `when:`)
and `ref:` (fan-in is now structural via multi-input edges).

```yaml
edges:
  - from: gate
    to: deploy
    branch: true
    binds:
      target: "{{ state.env }}"
  - from: gate
    to: rollback
    branch: false
```

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
