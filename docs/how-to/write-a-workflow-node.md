# Write a workflow node

The 14 built-in node types cover the common cases — running a shell
command, invoking an agent, mutating state, mapping over a list.
Anything beyond those, you write yourself as a **Python `@node`
module** inside a bundle. The parser routes any `type:` ref that
isn't a built-in to `CatalogNodeRunner`, which imports your module,
validates its inputs against your Pydantic model, and calls your
`run` function.

The end result: an authored node is a plain Python function with
typed inputs, typed outputs, and one decorator. No inheritance, no
registration boilerplate, no framework classes to memorise.

For the workflow-level concepts (DAG, events, resume), see the
[Workflows architecture page](../architecture/workflows.md). For
the YAML field spec covering both authored nodes and the 14
built-ins, see the
[Workflow YAML reference](../reference/workflow-yaml.md).

## The `@node` decorator

```python
from workflows import NodeContext, node
```

The decorator takes two optional keyword-only arguments:

- `label` — a human-readable name for chat capsules and the `show`
  CLI. When omitted, the function name is used with underscores
  turned into spaces.
- `secrets_needed` — a list of secret names the engine resolves and
  injects into `ctx.secrets` before your function runs. `None`
  normalises to an empty list.

The decorator attaches a `_kolsch_node` sentinel dataclass to the
function and returns it unchanged. It does shape checks only; the
deeper contract (signature, return type, catalog registration) is
enforced by the validator at run-start.

## The function contract

Your module must expose a top-level function named `run`. The
validator checks the signature strictly:

- **Parameter 1** — annotated with a `pydantic.BaseModel` subclass.
  This is your `Inputs` model. The parameter name is up to you.
- **Parameter 2** — annotated exactly as `NodeContext` (imported
  from `workflows`).
- **Parameter 3** *(optional)* — annotated with a `BaseModel`
  subclass. This is your `Config` model, for authored-node
  configuration that comes from the YAML rather than from binds.

Every parameter must be positional. `*args`, `**kwargs`, and
keyword-only parameters are rejected.

The return annotation must be exactly one of:

- **`BaseModel` subclass** — captured as `result.model_dump()`.
  Downstream nodes reference fields via
  `{{ nodes.<name>.<field> }}`.
- **`bool`** — captured as `{"value": result}`. Makes the node
  branchable — the edge `branch: true` / `branch: false` gates on
  this value.
- **`tuple[bool, BaseModel]`** — the branchable-with-payload form,
  captured as `{"value": flag, **payload.model_dump()}`. Both the
  branch value and the payload fields are addressable downstream.

Both sync and `async` functions are supported; the runner awaits
whichever you write.

## Inputs and Outputs

Both are plain `pydantic.BaseModel` subclasses in the same module.
No base class beyond `BaseModel`, no registration, no naming
convention. Field types, `Field(...)` constraints, validators, and
`ConfigDict` all behave the way Pydantic normally behaves.

The runtime calls `inputs_model.model_validate(bound_inputs)`
before dispatch, where `bound_inputs` is the dict produced by
rendering the edge `binds:` block into the target namespace. A
Pydantic `ValidationError` propagates as a node failure — you get
Pydantic's usual field-path error message for free.

## Secrets

**Never route a secret through a workflow input or an edge `bind:`.**
Inputs and binds are ordinary, observable data — they render into the
node's config preview, the `bound inputs:` / `workflow inputs used:`
blocks, the chat UI, and the JSONL session file, in full. There is no
redaction of that surface (a key-name filter was evaluated and rejected
as unsolvable — it cannot catch renamed keys, nested values, or
`nodes.*`-derived secrets, so it would only give false assurance).

Declare the secrets your node needs with `secrets_needed`, and read the
resolved values from `ctx.secrets` (a `Mapping[str, str]`):

```python
from workflows import NodeContext, node


@node(secrets_needed=["SERPAPI_KEY"])
async def run(inputs: SearchInputs, ctx: NodeContext) -> SearchOutputs:
    api_key = ctx.secrets["SERPAPI_KEY"]  # resolved at fire-time
    ...
```

The engine resolves each declared name through the configured secret
store and injects it into `ctx.secrets` before your function runs; a
missing secret fails the node before dispatch. Crucially, the resolved
value is placed **only** on `ctx.secrets` for your running function — it
is never added to the Jinja scope that renders node configs and previews
(that scope is `inputs` / `nodes` / `state` only) and never written to
the event log. So a secret read from `ctx.secrets` **never appears in
previews, the event log, or JSONL** (secrets/sandbox/authz invariant
**S-I1** + Engine **I10**). Treat session JSONL as sensitive regardless.

See the [Secrets / Sandbox / Authz contract](../explanation/secrets-sandbox-authz-contract.md)
for the resolution model, the `dotenv` / `native` store variants, and how
built-in tool nodes (e.g. bash) declare and consume the secrets they need.

## Where the file lives

```
bundles/<bundle>/nodes/<name>.py
```

The workflow YAML addresses it as `<bundle>/nodes/<name>` — the
same 3-segment ref format as tools, skills, agents, and prompts.
No manifest entry is required; the catalog scanner finds every
`.py` file under `nodes/` at startup.

**Reserved names.** These 14 slugs are reserved for the built-in
runners in `kolsch/nodes/`: `agent`, `bash`, `branch`, `compute`,
`delete_state`, `filter`, `list_files`, `map`, `noop`, `read_file`,
`set_state`, `user_input`, `workflow`, `write_file`. A file that
would collide with one raises `WorkflowBuiltinShadowed` at catalog
scan time.

Nothing stops you from shipping `mybundle/nodes/bash` — bundles are
namespaced, so your `bash` and the built-in `kolsch/nodes/bash`
coexist.

## What the validator checks

When a workflow that uses an authored node loads, the
`catalog-node-defs` layer runs seven checks per authored type:

1. The `<bundle>/nodes/<name>` ref resolves in the catalog.
2. The module imports without raising.
3. The module exposes a top-level `run`.
4. `run._kolsch_node` sentinel is present (the decorator was
   applied).
5. The signature matches the contract above.
6. The return annotation is one of the three allowed shapes.
7. The declared Inputs model exists and is a `BaseModel` subclass.

Any failure surfaces as a `WorkflowNode*` finding with a
file-and-line pointer. All checks run per node — a workflow with
five broken authored nodes gets five findings, not one.

## A minimal working example

From the starter bundle at
`bundles/kolsch/nodes/echo.py`:

```python
from __future__ import annotations

from pydantic import BaseModel

from workflows import NodeContext, node


class EchoInputs(BaseModel):
    message: str


class EchoOutputs(BaseModel):
    message: str


@node(label="Echo")
async def run(inputs: EchoInputs, ctx: NodeContext) -> EchoOutputs:
    _ = ctx
    return EchoOutputs(message=inputs.message)
```

Used in a workflow:

```yaml
nodes:
  say_hi:
    type: kolsch/nodes/echo
    description: "Echo the greeting"

graph:
  edges:
    - from: start
      to: say_hi
      binds:
        message: "hello world"
```

Downstream nodes read the output as `{{ nodes.say_hi.message }}`.

## A branchable example

An authored node that classifies a value and returns both a boolean
and a payload:

```python
from pydantic import BaseModel
from workflows import NodeContext, node


class ClassifyInputs(BaseModel):
    score: float


class ClassifyOutputs(BaseModel):
    label: str
    confidence: float


@node(label="Classify")
async def run(
    inputs: ClassifyInputs,
    ctx: NodeContext,
) -> tuple[bool, ClassifyOutputs]:
    high = inputs.score >= 0.5
    return high, ClassifyOutputs(
        label="positive" if high else "negative",
        confidence=abs(inputs.score - 0.5) * 2,
    )
```

Wired into a graph with branching:

```yaml
graph:
  edges:
    - from: score
      to: classify
      binds:
        score: "{{ nodes.score.value }}"
    - from: classify
      to: publish
      branch: true                # fires when the tuple bool is True
      binds:
        label: "{{ nodes.classify.label }}"
    - from: classify
      to: hold
      branch: false
```

## Runtime behaviour

`CatalogNodeRunner` handles every non-built-in node ref:

1. Imports the module via `importlib.util` under a synthetic
   `_kolsch_node_<ref>` name (cached per ref for the lifetime of
   the runner).
2. Re-verifies the `_kolsch_node` sentinel and extracts the Inputs
   model from the first parameter's type hint.
3. Validates the incoming binds against the Inputs model.
4. Calls `run(inputs, ctx)` — or `await run(inputs, ctx)` for
   coroutines.
5. Coerces the return value into the captured dict form.
6. Writes the terminal `workflow.node.completed` (or `failed` /
   `timed_out` / `pruned`) event.

Under `concurrency: auto`, multiple authored nodes can run at the
same time in one workflow — each invocation is isolated from the
others (module import is cached; execution isn't shared).

## The introspection tool

`kolsch/tools/introspect_node` is a bundle tool that wraps the same
`introspect_node_module` helper the validator uses. Meta-agents (or
you, at the CLI) can call it on a fresh authored-node file to check
whether it will pass validation before it ends up in a workflow.

---

The parser and runtime references are at
[API: Workflows](../api/index.md#workflows). For the complete YAML
field spec covering both authored nodes and the 14 built-ins, see
the [workflow YAML reference](../reference/workflow-yaml.md).
