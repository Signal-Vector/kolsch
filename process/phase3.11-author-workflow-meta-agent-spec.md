# Phase 3.11 — `author_workflow` Meta-Agent

This spec covers Phase 3.11 — the `kolsch/agents/author_workflow`
meta-agent that generates and iteratively refines workflow YAML +
authored Python `@node` files from natural-language requests. Depends
on every prior phase (3.0 → 3.10). Per roadmap §18.3 the dependency
chain is `3.8 + 3.9 + 3.10 → 3.11`.

Related docs:

- `process/phase3.x-roadmap.md` §18.2 / 3.11 — the roadmap entry.
- `process/phase3.0-workflow-spec.md` §1.4 (mental model), §2 (node
  shape), §4 (the 14 built-ins), §5 (authored Python `@node`
  contract), §6 (graph topology + fan-in), §7 (run-start gate) —
  the canonical reference the meta-agent's prompt + skill encode.
- `process/phase3.9-chat-surface-spec.md` §15 — the chat surface
  the meta-agent lives in (operators talk to it via the existing
  `/agent` / chat pathways; no new chat primitives this phase).
- `process/phase3.10-concurrent-executor-spec.md` §16 — concurrency
  semantics the meta-agent must respect when generating workflows
  that opt into `concurrency: auto` or `parallel: true`.
- `docs/explanation/workflow-foundation.md` — Locked Foundation
  Decisions; the meta-agent treats these as ground truth.
- `docs/how-to/authoring-a-workflow.md` — the human-author guide
  whose patterns the meta-agent's skill reuses and amplifies.

---

## 17. `author_workflow` Meta-Agent

### 17.0 Goal

Close the Phase 3.x engine roadmap by shipping the AI-assisted
authoring surface. After Phase 3.10, the engine supports a 14-node
built-in catalog + authored Python `@node` files + composition +
fork + chat + concurrency. The remaining gap is BOOTSTRAPPING: a
new author who knows what they want ("classify tickets and email
the urgent ones") but doesn't know the YAML schema, the bind
grammar, or which node types to compose. Phase 3.11 ships a chat
agent that produces a valid, runnable workflow from a natural
language brief in one to a few turns, then accepts iterative
amendments ("also append a row to a Google Sheet").

Two design pivots make this phase realistic without inventing new
engine surface:

- The meta-agent is JUST AN AGENT. It ships as a `bundles/kolsch/agents/author_workflow.yaml`
  asset — a conversational agent loaded by the existing chat surface
  (Phase 2.x + Phase 3.9). No new agent runtime, no new chat
  primitives, no new node types. The existing `/agent` /
  `kolsch chat` surfaces invoke it.
- The meta-agent's KNOWLEDGE lives in a Phase 2.4 skill (`bundles/kolsch/skills/author-workflow/SKILL.md`)
  that encodes the canonical workflow-authoring patterns, the
  run-start gate's error vocabulary, and the iteration loop. The
  agent's prompt is small; the skill is large. Updating the skill
  updates the meta-agent without re-shipping anything else.

The meta-agent's loop is straightforward and known-good:

1. Read the author's brief (chat input).
2. Inspect the catalog to discover existing agents / tools /
   `@node` modules / sub-workflows the author can compose.
3. Draft a workflow YAML (and any new `@node` Python files).
4. Call the new `validate_workflow` tool against the draft.
5. If the validator returns errors, READ the error class + identifying
   names, REVISE, GOTO 4.
6. When the validator returns `is_valid: True`, write the files to
   the workspace bundle, surface the new ref to the author, and
   stop. The author can then `/workflow <ref>` to run it.

For amendments ("add a step that emails the result"), the agent
reads the existing workflow YAML, drafts a modification, validates,
writes back. The agent ALWAYS validates before declaring success.

### 17.1 Scope

In scope at Phase 3.11:

- `bundles/kolsch/agents/author_workflow.yaml` — the meta-agent
  asset. Conversational mode; uses `read_file`, `write_file`,
  `list_files`, `bash`, the new `validate_workflow` tool, and the
  new `introspect_node` tool. Opts into the `author-workflow`
  skill.
- `bundles/kolsch/skills/author-workflow/SKILL.md` — the
  canonical skill encoding the workflow grammar + run-start
  gate's error vocabulary + iteration recipe. The skill is
  load-bearing — it carries the design intent that the
  meta-agent's small prompt doesn't.
- `bundles/kolsch/tools/validate_workflow.py` — NEW Phase 3.11
  tool. Wraps `workflows.validate_workflow_or_raise(...)`. Loads
  the named workflow YAML from disk; runs the validator; returns
  a structured dict `{is_valid: bool, errors: list[{class,
  identifying_names, message, file, line}], warnings: [...]}`.
  Pure read; no side effects.
- `bundles/kolsch/tools/introspect_node.py` — NEW Phase 3.11
  tool. Loads an authored Python `@node` module by file path;
  introspects via the existing `workflows.catalog_node`
  machinery; returns `{label, inputs_schema, outputs_schema,
  signature_ok: bool, errors: list[str]}`. The meta-agent calls
  this when it has just written a new `@node` file and wants to
  confirm the engine will accept it BEFORE re-running the
  workflow validator.
- Two starter example workflows that DOUBLE as few-shot training
  data for the meta-agent's skill: `bundles/kolsch/workflows/example-triage.yaml`
  + `bundles/kolsch/workflows/example-parallel-fanout.yaml`. Both
  already exist (Phase 3.6 + Phase 3.10); the skill REFERENCES
  them by ref and the meta-agent reads them as templates.
- A "default authoring target bundle" config (`platform.yaml`
  knob `author_workflow.target_bundle`) controlling which bundle
  the meta-agent writes new files into. Defaults to the
  workspace's primary user bundle (NOT the `kolsch` bundle —
  which is package-shipped and read-only by convention).
- One starter workflow `bundles/kolsch/workflows/author-loop.yaml`
  — a tiny wrapper that pairs the meta-agent with a
  `user_input → agent → user_input → ...` loop so authors can
  iterate without leaving the workflow surface. Optional but
  helpful for the operator who wants a single-command flow.
- Integration tests covering the verification gate (§17.10).

Out of scope (deferred):

- Auto-execution of the generated workflow. The meta-agent
  produces files + emits a "run it with `/workflow <ref>`" hint;
  it does not invoke `kolsch workflows run` on the author's behalf.
  Author-in-the-loop confirmation matters more than two seconds
  saved.
- Fine-tuning a dedicated model on workflow YAML. v1 uses the
  workspace's configured model (Opus / Sonnet / whatever resolves
  per `platform.yaml`'s `defaults.model`). The skill carries the
  domain knowledge.
- Schema-aware autocomplete in the chat input box. No editor
  integration this phase.
- Generating `agent:` assets (`bundles/.../agents/*.yaml`). The
  meta-agent generates WORKFLOWS (and the `@node` Python files
  workflows depend on). Authoring an agent file is a separate
  meta-agent (TBD) and is NOT in scope at Phase 3.11.
- Generating `tool:` assets. Same reasoning — separate meta-agent.
- Generating SKILLS — same.
- Multi-bundle authoring. The meta-agent writes to a single
  configured target bundle. Cross-bundle composition is supported
  (the workflow CAN reference `<other_bundle>/agents/foo`) but
  generation always lands in one bundle.
- Reverse engineering (given a JSONL run, produce the workflow
  that emitted it). Future possibility; not v1.
- Workflow OPTIMISATION (rewrite for concurrency, dedupe
  sub-graphs). The meta-agent produces correct workflows; it does
  not auto-tune them. Authors can re-prompt with
  "make this parallel where safe."
- A `/author` slash command. v1 reuses the existing chat surface
  (`kolsch chat --agent kolsch/agents/author_workflow` OR the
  `/agent <ref>` slash if it exists). A dedicated `/author`
  is a possible QoL after v1.

### 17.2 Architecture

```text
┌────────────────────────────────────────────────────────────────┐
│ kolsch chat                                                    │
│                                                                │
│   author: "Build a workflow that classifies tickets and        │
│            emails the urgent ones."                            │
│                                                                │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │ kolsch/agents/author_workflow                           │  │
│   │                                                         │  │
│   │  • prompt: small; defers to skill                       │  │
│   │  • skill: kolsch/skills/author-workflow (big)           │  │
│   │  • tools:                                               │  │
│   │      - list_files, read_file, write_file (existing)     │  │
│   │      - bash (for `kolsch <asset> list/show`)            │  │
│   │      - validate_workflow (NEW Phase 3.11)               │  │
│   │      - introspect_node (NEW Phase 3.11)                 │  │
│   └─────────────────────────────────────────────────────────┘  │
│                                                                │
│   The agent's iteration loop:                                  │
│                                                                │
│      draft YAML ──> validate_workflow ──┐                      │
│         ▲                                │                     │
│         │                                │                     │
│         └──── revise on errors ──────────┘                     │
│                                                                │
│   On success:                                                  │
│     write_file <new_workflow>.yaml + <new_nodes>.py            │
│     surface ref: "Try it: /workflow <bundle>/workflows/<ref>"  │
└────────────────────────────────────────────────────────────────┘
```

The dotted line is the agent's CONTROL flow inside one chat
exchange. The chat surface (Phase 3.9) is the operator's interface;
the agent layer (Phase 2.x) is the LLM call + tool dispatch loop;
the engine (Phase 3.0 — 3.10) is what the validator + introspector
delegate to. Phase 3.11 wires the meta-agent at the agent layer
ONLY. No engine changes.

### 17.3 The new `validate_workflow` tool

```yaml
# bundles/kolsch/tools/validate_workflow.py — Python tool
```

```python
class ValidateWorkflowArgs(BaseModel):
    workflow_ref: str | None = None   # if given, resolve via catalog
    yaml_path: str | None = None      # OR a direct file path
    # Exactly one must be set.

class ValidationFinding(BaseModel):
    error_class: str
    identifying_names: dict[str, str | int]
    caller_message: str
    file: str
    line: int

class ValidationOutput(BaseModel):
    is_valid: bool
    errors: list[ValidationFinding]
    warnings: list[ValidationFinding]
```

The tool's `run` body:

```python
async def validate_workflow(args, ctx):
    if args.workflow_ref:
        entry = ctx.catalog.lookup(args.workflow_ref, expected_type="workflow")
        if entry is None:
            return ValidationOutput(
                is_valid=False,
                errors=[ValidationFinding(
                    error_class="WorkflowNotFound",
                    identifying_names={"ref": args.workflow_ref},
                    caller_message=f"workflow {args.workflow_ref!r} not in catalog",
                    file="<catalog>", line=0,
                )],
                warnings=[],
            )
        path = Path(entry.path)
    else:
        path = Path(args.yaml_path)
    try:
        workflow = load_workflow(path)
    except WorkflowValidationError as exc:
        # Parser-level rejection (YAML structural / Pydantic-shape).
        return ValidationOutput(
            is_valid=False,
            errors=[_finding_from(exc)],
            warnings=[],
        )
    result = validate_workflow(workflow, ctx.catalog, file=str(path))
    return ValidationOutput(
        is_valid=result.is_valid,
        errors=[_finding_from(e) for e in result.errors],
        warnings=[_finding_from(w) for w in result.warnings],
    )
```

The meta-agent CALLS this tool after every draft. The tool returns
structured errors with `error_class` + `identifying_names` so the
agent doesn't have to re-parse English. The agent's skill includes
a TABLE of error classes → likely root causes → suggested fixes
(see §17.7).

**Side effects:** none. `validate_workflow` is pure read against
the YAML at the given path + the asset catalog. The tool fits
cleanly under the kolsch default policy with NO `prompt` rule
(safe by construction).

### 17.4 The new `introspect_node` tool

```python
class IntrospectNodeArgs(BaseModel):
    py_path: str   # absolute path to the `@node` Python file

class NodeIntrospection(BaseModel):
    signature_ok: bool
    label: str | None
    inputs_schema: dict[str, Any] | None   # JSON-Schema'd Inputs model
    outputs_schema: dict[str, Any] | None  # JSON-Schema'd Outputs model
    errors: list[str]
```

Wraps the existing `workflows.catalog_node._introspect_node_module(...)`
(name TBD per Step 0). Returns structured info OR errors. The
meta-agent calls this RIGHT AFTER it writes a new `@node` file so
it can iterate on the Python signature BEFORE invoking
`validate_workflow` (which would also catch the issue but with a
less direct error chain).

`introspect_node` is also pure read; same policy posture as
`validate_workflow`.

### 17.5 The meta-agent asset

```yaml
# bundles/kolsch/agents/author_workflow.yaml
schema_version: "0.1"
assembly_version: "1"
agent_id: ag_author_workflow
name: author_workflow
description: |
  Generates and iteratively refines workflow YAML + authored Python
  @node files from natural-language requests. Reads the workspace
  asset catalog to discover existing components; writes new files
  into the configured target bundle; validates every draft against
  the run-start gate before declaring success.

io: { mode: conversational }

context_files:
  - path: kolsch:docs/explanation/workflow-foundation.md
    summary: Locked Foundation Decisions for workflow design.
    primary: true
  - path: kolsch:docs/how-to/authoring-a-workflow.md
    summary: Author-facing recipe for writing workflows.
    primary: true

# v1 ships ONE skill that carries the bulk of the authoring
# knowledge. The agent's prompt is intentionally small; the skill
# is the level of indirection at which authors can tune the
# meta-agent's behaviour without re-shipping the agent file.
subsystems:
  skills:
    config:
      enabled:
        - ref: kolsch/skills/author-workflow
  tools:
    variant: standard
    config:
      declared:
        - ref: kolsch/tools/read_file
        - ref: kolsch/tools/write_file
        - ref: kolsch/tools/list_files
        - ref: kolsch/tools/bash
        - ref: kolsch/tools/validate_workflow
        - ref: kolsch/tools/introspect_node

# permissions: identical to the helper agent's defaults — bash is
# `prompt` so the operator sees a modal before any shell call.
permissions:
  - who: ".*"
    what: "tool\\.bash"
    effect: prompt

prompt: |
  You are {{ agent.name }}, a workflow-authoring assistant.

  Your job is to help the author write VALID Kolsch workflow YAML
  (and any authored Python @node files the workflow needs). You
  iterate: draft → validate → revise. You DO NOT declare success
  until `validate_workflow` returns `is_valid: true`.

  Read the `kolsch/skills/author-workflow` skill BEFORE drafting
  your first workflow; the skill carries the grammar, the canonical
  examples, and the iteration recipe.

  Inspect the workspace catalog to discover what already exists
  before inventing new components. Reuse whenever possible — use
  `bash` to call `kolsch workflows list`, `kolsch agents list`,
  `kolsch nodes list`, etc.

  Write files into the configured target bundle (see your skill's
  §"Where to write" — typically the workspace's primary user
  bundle, NEVER the `kolsch` package bundle).

  When the validator returns errors, READ the error class first.
  Your skill maps every error class to its likely root cause. Fix
  the root cause, then re-validate.
```

The asset is otherwise standard — same shape as the existing
`kolsch/agents/helper`. No new agent-layer machinery is required.

### 17.6 The `author-workflow` skill

```text
bundles/kolsch/skills/author-workflow/SKILL.md
```

The skill is the design payload. It encodes:

- **Grammar.** YAML field-by-field — the load-bearing fields are
  `nodes:`, `graph:`, `extends:`, `policy:`, `concurrency:`.
  Sub-grammar for each node type (the 14 built-ins + the
  `bundles/<...>/nodes/<...>` form for authored Python).
- **Inputs / binds.** The `nodes.<name>.<field>` Jinja
  vocabulary; the `inputs.<bind_key>` overlay at receivers;
  AND-strict fan-in.
- **The 14 built-in node types.** A reference table (cribbed from
  master spec §4) with usage notes and one canonical example per
  type.
- **Authored Python `@node`.** The decorator contract;
  `Inputs(BaseModel)` + `Outputs(BaseModel)` + the three return
  shapes (BaseModel, `bool`, `tuple[bool, BaseModel]`);
  where the file lives in the bundle.
- **The iteration recipe.**
  1. Discover catalog assets.
  2. Sketch the graph (nodes + edges).
  3. Write the YAML.
  4. If any `@node` file is needed, write it (and run
     `introspect_node` to confirm).
  5. Call `validate_workflow`.
  6. If errors, fix per the error-class table; re-validate.
  7. On success, surface the ref + suggested next step.
- **Error-class table.** Every `WorkflowValidationError`
  subclass with: (i) what the engine is complaining about; (ii)
  the typical mistake; (iii) the fix. The skill keeps this list
  in sync with the engine's actual error vocabulary; updates ship
  when new error classes land.
- **Anti-patterns.** "Don't put behaviour in the `description:`;
  it's display-only." "Don't reuse the same node name for two
  invocations — copy_of is for that." "Don't make `auto`-mode
  workflows write conflicting state — the gate rejects them."
- **Canonical examples.** Three end-to-end transcripts: a small
  bash pipeline, a triage-with-agent workflow, an auto-mode
  fan-out. Each shows the human brief, the agent's catalog
  inspection, the draft, the validator output, the fix, the
  final ref.

The skill is the durable design surface — when the engine adds a
new node type (e.g. Phase 4+), authors patch the skill; the
meta-agent picks it up the next chat session. No re-shipping of
the agent asset.

### 17.7 The error-class table (skill payload, summary)

The skill renders a table like this (one row per
`WorkflowValidationError` subclass):

| Error class | What the engine sees | Likely cause | Fix |
| --- | --- | --- | --- |
| `WorkflowNoStartNode` | No node has zero incoming edges. | Forgot to add a start node, or every node has an inbound edge from a cycle. | Add one entry node OR break the cycle. |
| `WorkflowDuplicateNode` | Two nodes share a name. | Copy-paste error. | Rename one. |
| `WorkflowBindCollision` | Two inbound edges define the same bind key on the same receiver. | Multi-input fan-in collided on a key. | Rename one bind OR pick one source. |
| `WorkflowBindTypeMismatch` | A bind value's runtime type doesn't match the receiver's `Inputs(BaseModel)`. | Wrong field on `nodes.<X>.<field>`. | Re-check the source node's output schema. |
| `WorkflowJinjaRefUnknown` | A Jinja reference points at a node / state key / input that doesn't exist. | Typo in `nodes.<name>.<field>`. | Spell the source's name correctly. |
| `WorkflowParallelStateConflict` | Two parallel branches fight over a state key. | `concurrency: auto` plus two branches both touching `state.*`. | Serialise via an explicit edge OR drop concurrency. |
| `WorkflowMapperWorkerNotAddressable` | A node references a map worker from outside the map. | `nodes.<worker>.<field>` reference. | Reference the map node's `results` instead. |
| `WorkflowExtendsCycle` | A workflow `extends:` chain loops. | A → B → A. | Break the chain. |
| `WorkflowCallCycle` | A workflow's `workflow:` node chain loops. | A calls B calls A. | Break the call chain. |
| `WorkflowExtendsDepthExceeded` | Chain depth > 16. | Deeply nested composition. | Flatten or refactor. |

The skill's table is the AUTHORITATIVE summary the meta-agent
reads at the start of each session. When the validator emits a
class the table doesn't cover, the agent falls back to: "I don't
recognise this error class; here's the raw `caller_message` and
my best guess at the cause."

### 17.8 Iteration loop in detail

```text
state: { brief, target_bundle, current_yaml, attempts: 0 }

while attempts < MAX_ATTEMPTS:
    if attempts == 0:
        draft = synthesise_from_brief(brief, catalog_assets)
    else:
        draft = revise(current_yaml, last_errors)

    write_to_disk(draft)                        # write_file tool

    if draft.has_new_node_modules:
        for mod in draft.new_node_modules:
            ok = introspect_node(mod)           # introspect_node tool
            if not ok: revise_module(); continue

    result = validate_workflow(draft.path)      # validate_workflow tool

    if result.is_valid:
        return SuccessResponse(ref=draft.ref)

    last_errors = result.errors
    current_yaml = draft.yaml
    attempts += 1

# Out of attempts — escalate to the author:
return BlockedResponse(
    last_yaml=current_yaml,
    last_errors=last_errors,
    suggestion="Here's the closest I got. Could you advise on <X>?",
)
```

`MAX_ATTEMPTS` is set in the skill (currently 5). When the agent
hits the cap, it summarises the closest draft + the remaining
errors and asks the author for a directional hint — better than
silent failure or infinite looping.

### 17.9 Amendment loop

For modifications ("add a step that emails the urgent ones"):

1. The author references the existing workflow by ref OR by
   name in chat (the agent resolves via catalog).
2. The agent reads the existing YAML via `read_file`.
3. The agent drafts the modification (typically an inserted node
   + adjusted edges) and any new `@node` files.
4. The agent runs `validate_workflow` against the modified draft.
5. If valid, the agent writes the modified file back (overwriting
   the original) and surfaces the change summary.

The amendment loop is structurally the same as the initial-author
loop; the only difference is the seed (existing YAML vs blank
slate).

### 17.10 Verification gate

The verification gate is FOUR scenarios:

1. **From-scratch authoring.** The author types "Build a workflow
   that classifies a ticket as urgent or normal and prints the
   result." The meta-agent produces a workflow YAML referencing
   `kolsch/agents/classifier`, a `branch` node, two terminal
   bash nodes. `validate_workflow` returns `is_valid: true`. The
   workflow runs end-to-end via `/workflow <ref> with ticket_text=...`
   and produces the expected output.
2. **Iterative refinement.** The author then says "Also write the
   classification to a file at `out/result.txt`." The meta-agent
   reads the workflow, adds a `write_file` node downstream of the
   classifier, re-validates, writes back. The amended workflow
   runs end-to-end.
3. **Authored Python node generation.** The author says "Build a
   workflow that takes a CSV path, counts rows, and prints the
   count." The meta-agent realises no existing `@node` covers
   "count rows in CSV"; writes a new `bundles/<target>/nodes/csv_row_count.py`
   `@node` module; introspects it; references it from the
   workflow YAML; validates; runs end-to-end.
4. **Validation-driven refinement.** The author types a brief
   that, if naively translated, would produce a workflow with a
   `WorkflowBindCollision`. The meta-agent's FIRST draft includes
   the collision; `validate_workflow` returns the error; the
   agent revises (renames the colliding bind) and re-validates;
   second draft passes; workflow runs end-to-end.

A starter run-script ships at `tests/component/test_author_workflow_e2e.py`
exercising all four against a SCRIPTED conversation (the chat
loop receives the scripted author messages; the agent's responses
are verified for `validate_workflow` calls + final ref emission;
the produced workflow is then `kolsch workflows run` against and
its terminal output asserted).

### 17.11 CLI surface

NO new CLI commands. The meta-agent is invoked via the existing
chat surface:

```bash
# Open chat with the meta-agent.
kolsch chat --agent kolsch/agents/author_workflow

# Or, if the existing `/agent` slash exists, from inside any chat:
/agent kolsch/agents/author_workflow
```

The `validate_workflow` and `introspect_node` tools are also
callable from any other agent (or the human via
`kolsch chat --agent kolsch/agents/helper`) — they're regular
tools, not meta-agent-exclusive.

### 17.12 Chat surface

NO new slash commands. The meta-agent is a regular conversational
agent invoked via the existing surfaces. Capsules, modal prompts,
running-node indicators all behave per Phase 2.x + Phase 3.9.

### 17.13 Concurrency posture

The meta-agent generates workflows that DEFAULT to
`concurrency: sequential` per Phase 3.10. When the author asks
for concurrency ("run those three branches in parallel"), the
agent emits `concurrency: auto` and the skill's grammar reminds
it to inspect the branches for state writes — which the run-start
gate enforces anyway. The agent's drafts gracefully accept the
gate's rejection and serialise the state-mutating branch.

### 17.14 Determinism and trust

The meta-agent is an LLM — the generated YAML may vary across
runs for the same brief. The hard contract is "every generated
workflow passes the run-start gate before the agent declares
success." Determinism of the output text is NOT a goal; correctness
+ runnability are.

Authors are responsible for REVIEWING the generated workflow
before running it. The chat surface's policy modal (Phase 2.x)
already gates `bash` invocations; running an LLM-generated
workflow that calls bash will fire the modal at execution time.
The meta-agent does NOT bypass these safeguards.

### 17.15 Migration and back-compat

Nothing migrates. Existing workflows are unchanged. New starter
files ship via `bundles/kolsch/`; no schema changes; no
event-kind additions; no executor changes.

Authors who already write workflows by hand continue to do so —
the meta-agent is opt-in, not on-by-default.

---
