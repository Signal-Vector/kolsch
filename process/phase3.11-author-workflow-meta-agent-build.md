# [SPEC-012] Phase 3.11 — `author_workflow` meta-agent

**Type:** spec
**Doc #:** 012
**Status:** Draft
**Created:** 2026-06-28

---

## Glossary

| Term | Meaning |
| --- | --- |
| meta-agent | `kolsch/agents/author_workflow` — the conversational agent shipped this phase that generates + iteratively refines workflow YAML and authored Python `@node` files from natural-language briefs. |
| target bundle | The bundle the meta-agent writes new files into. Defaults to the workspace's primary user bundle (NOT `kolsch`, which is package-shipped). Configurable via `platform.yaml`'s `author_workflow.target_bundle`. |
| validation tool | `kolsch/tools/validate_workflow` — NEW tool that wraps `validate_workflow_or_raise(...)` and returns structured findings. |
| introspection tool | `kolsch/tools/introspect_node` — NEW tool that wraps the existing `@node` introspection machinery; the meta-agent uses it for iterating on freshly-written `@node` modules. |
| authoring skill | `kolsch/skills/author-workflow` — the Phase 2.4 skill carrying the workflow grammar, error-class table, iteration recipe, and canonical examples. The skill is the DESIGN PAYLOAD; the agent's prompt is intentionally small. |
| iteration loop | The meta-agent's draft → validate → revise loop. Caps at `MAX_ATTEMPTS = 5` per the skill; on cap, the agent escalates to the author. |
| amendment loop | The meta-agent's read-existing → modify → validate → write-back flow for "add a step that…" follow-ups. Structurally identical to iteration; only the seed differs. |

---

## Background

Phase 3.11 is a **surface phase** — it ships an agent asset, two
small tools, a skill, and a handful of starter files. No new
engine surface; no new node types; no new event kinds; no new chat
primitives. Every dependency already exists in the codebase per the
surface map:

- **Conversational agent runtime** — Phase 2.x asset loader +
  agent dispatch. The meta-agent is a regular `*.yaml` agent file
  loaded by the existing chat surface.
- **`validate_workflow_or_raise(...)`** at
  `src/workflows/validator.py:247-275` — the function the new
  validation tool wraps.
- **`load_workflow(path)`** at `src/workflows/parser.py:631-700` —
  the YAML loader the validation tool calls before validating.
- **`AssetCatalog.lookup(ref, expected_type=)`** at
  `src/assets/catalog.py` — the surface the meta-agent (via `bash`)
  and the validation tool both consult.
- **`@node` introspection** at
  `src/workflows/catalog_node.py` — the existing machinery the
  introspection tool wraps. Per Phase 3.0 the validator already
  introspects `@node` modules at run-start; Phase 3.11 simply
  exposes that introspection as a tool.
- **`read_file` / `write_file` / `list_files` / `bash` tools** at
  `bundles/kolsch/tools/` — existing tools the meta-agent uses
  for file I/O + catalog inspection.
- **Phase 2.4 skill subsystem** — the load-bearing surface the
  authoring skill rides on.
- **Phase 3.9 chat surface** — the operator's interface to the
  meta-agent. No changes.

What's LEFT for Phase 3.11:

1. **`bundles/kolsch/tools/validate_workflow.py`** — new Python
   tool wrapping `validate_workflow_or_raise(...)`. Returns
   `ValidationOutput(is_valid, errors, warnings)` per spec §17.3.
2. **`bundles/kolsch/tools/introspect_node.py`** — new Python tool
   wrapping the existing introspection machinery. Returns
   `NodeIntrospection(signature_ok, label, inputs_schema,
   outputs_schema, errors)` per spec §17.4.
3. **`bundles/kolsch/agents/author_workflow.yaml`** — meta-agent
   asset per spec §17.5.
4. **`bundles/kolsch/skills/author-workflow/SKILL.md`** — design
   payload per spec §17.6 + §17.7.
5. **`bundles/kolsch/workflows/author-loop.yaml`** (optional) —
   tiny `user_input → agent → user_input` wrapper per spec §17.1.
6. **`platform.yaml`** knob `author_workflow.target_bundle` —
   default-bundle config for where the agent writes files.
7. **End-to-end integration tests** at
   `tests/component/test_author_workflow_e2e.py` covering the
   four §17.10 verification scenarios via scripted chat
   conversation.
8. **Roadmap annotation** in `process/phase3.x-roadmap.md` —
   Phase 3.11 line ✓ with shipping SHA.

The verification gate is four scenarios per spec §17.10. The work
is mostly authoring (skill + agent prompt), with two small Python
tools wrapping existing engine surface.

**This spec breaks the phase into 4 sequential steps (Step 0 — Step 3)**.

Related docs:

- `process/phase3.11-author-workflow-meta-agent-spec.md` — master
  design spec (§17.0 — §17.15).
- `process/phase3.0-workflow-spec.md` §4 (node catalog), §5
  (authored Python contract), §6 (graph topology), §7 (run-start
  gate), §12 (error vocabulary) — the skill's authoritative
  reference.
- `process/phase3.9-chat-surface-spec.md` §15 — the chat surface
  the meta-agent lives in.
- `process/phase3.10-concurrent-executor-spec.md` §16 — concurrency
  the meta-agent must reason about when generating fan-out
  workflows.

---

## Scope

**In scope (delivered across the 4 steps):**

- `bundles/kolsch/tools/validate_workflow.py` — new tool.
- `bundles/kolsch/tools/introspect_node.py` — new tool.
- `bundles/kolsch/agents/author_workflow.yaml` — meta-agent
  asset.
- `bundles/kolsch/skills/author-workflow/SKILL.md` — authoring
  skill.
- `bundles/kolsch/workflows/author-loop.yaml` — optional wrapper
  workflow.
- `platform.yaml` knob `author_workflow.target_bundle` with
  default resolution to the workspace's primary user bundle.
- Integration tests at `tests/component/test_author_workflow_e2e.py`.
- Roadmap line annotated.

**Out of scope (per spec §17.1 + roadmap):**

- Auto-executing the generated workflow on the author's behalf.
- Fine-tuning a dedicated model.
- Editor / IDE integration.
- Generating non-workflow assets (agents, tools, skills).
- Multi-bundle authoring.
- Reverse engineering JSONL → workflow.
- Workflow optimisation passes.
- A dedicated `/author` slash command (deferred QoL).

**Decisions explicitly locked (do NOT relitigate):**

- **The meta-agent is JUST AN AGENT.** No new agent-layer
  machinery; no new chat surface; no new engine API.
- **Knowledge lives in the SKILL, not the prompt.** The agent's
  prompt is small; the skill is the design payload.
- **The agent ALWAYS validates before declaring success.** No
  "looks plausible, ship it" shortcuts.
- **Files are written to the configured target bundle.** Never
  the `kolsch` package bundle (read-only by convention).
- **Author confirms before running.** The agent surfaces the
  ref and a "Try it: `/workflow <ref>`" hint; it does not invoke
  the engine on the author's behalf.
- **`MAX_ATTEMPTS = 5`.** On cap, escalate to the author with
  the closest draft + remaining errors. No silent failure; no
  infinite loop.
- **No new event kinds.** The meta-agent's actions are
  agent-layer; the engine sees `tool.called` for `validate_workflow`
  + `introspect_node` like any other tool.
- **No new error classes.** Validation errors flow through the
  existing `WorkflowValidationError` hierarchy; the tool
  serialises them into structured findings.
- **Determinism of the LLM is NOT a contract.** The hard contract
  is "every shipped workflow passes the run-start gate."
  Output-text reproducibility is not a Phase 3.11 deliverable.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2, ruamel.yaml, Jinja2, pytest.
Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/workflows/validator.py:247-275` — `validate_workflow_or_raise`.
- `src/workflows/parser.py:631-700` — `load_workflow`.
- `src/workflows/catalog_node.py` — `@node` introspection.
- `src/assets/catalog.py` — `AssetCatalog.lookup`.
- `src/cli/_starter_bundle/kolsch/tools/*.py` — existing tools
  the new tools mirror in shape (Pydantic args model, async `run`,
  `ToolContext` dispatch).
- `src/cli/_starter_bundle/kolsch/agents/helper.yaml` — existing
  conversational agent the meta-agent mirrors in shape.
- `src/cli/_starter_bundle/kolsch/skills/write-python-test/SKILL.md`
  — existing skill the authoring skill mirrors in shape.
- `src/cli/_starter_bundle/kolsch/bundle.yaml` — bundle manifest;
  the new files are auto-discovered via existing manifest patterns.

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch
uv run kolsch --root /tmp/kolsch-author-test setup --non-interactive
uv run pytest tests/component/test_author_workflow_e2e.py -v
```

---

## Phase-level Acceptance Criteria

- `kolsch chat --agent kolsch/agents/author_workflow` opens a
  chat session with the meta-agent. The agent's first response
  acknowledges the brief and outlines its plan (per the skill's
  iteration recipe).
- A scripted from-scratch brief ("Build a workflow that classifies
  a ticket and prints urgent or normal") results in (a) one or
  more `validate_workflow` tool calls; (b) a final
  `is_valid: true` validation result; (c) a written workflow file
  in the target bundle; (d) a surfaced ref the author can
  `/workflow <ref>` to run.
- The produced workflow RUNS end-to-end via `kolsch workflows run`
  and produces the expected output.
- An amendment brief ("Also write the result to a file") triggers
  the agent to read the existing workflow, modify it, re-validate,
  write back. The amended workflow runs end-to-end.
- An authored Python `@node` generation brief ("Count rows in a
  CSV") triggers the agent to write a new `@node` file under the
  target bundle's `nodes/` directory; call `introspect_node` to
  confirm the signature; reference it from the workflow YAML;
  validate; run.
- A brief that produces a `WorkflowBindCollision` on the first
  draft triggers the agent to revise based on the error class +
  identifying names. Second draft passes validation.
- `MAX_ATTEMPTS = 5` cap respected: a brief the agent cannot
  satisfy in 5 attempts surfaces the closest draft + remaining
  errors and asks the author for direction (not silent failure).
- The meta-agent never writes to the `kolsch` package bundle
  (verified by manual smoke + integration assertion).
- The validation tool returns structured findings with
  `error_class`, `identifying_names`, `caller_message`, `file`,
  `line` — the same fields the skill's error-class table indexes.
- The introspection tool returns `signature_ok: false` + a clear
  error list for an `@node` module that violates the §5 contract
  (e.g., missing `Inputs(BaseModel)` parameter; wrong return
  type).
- No regressions: full suite green; existing chat / agent /
  workflow flows produce byte-identical output.

---

## Phase-level Integration Tests

### Phase Test 1: From-scratch authoring
- **Run:** `uv run pytest tests/component/test_author_workflow_e2e.py::test_from_scratch_authoring -v`
- **Expect:** scripted brief produces a workflow file in the
  target bundle; `validate_workflow` returns `is_valid: true`;
  the workflow runs and prints the expected classification.

### Phase Test 2: Iterative refinement
- **Run:** `uv run pytest tests/component/test_author_workflow_e2e.py::test_amendment_loop -v`
- **Expect:** post-Phase-Test-1 session continues with "Also
  write the result to a file"; the modified workflow validates
  + runs end-to-end.

### Phase Test 3: Authored Python `@node` generation
- **Run:** `uv run pytest tests/component/test_author_workflow_e2e.py::test_node_module_generation -v`
- **Expect:** brief "Count rows in a CSV" produces both a new
  `@node` file (introspected with `signature_ok: true`) and a
  workflow YAML referencing it; the workflow runs end-to-end.

### Phase Test 4: Validation-driven refinement
- **Run:** `uv run pytest tests/component/test_author_workflow_e2e.py::test_validation_driven_revision -v`
- **Expect:** a brief that would produce a `WorkflowBindCollision`
  on first draft is recovered by the agent on second draft
  using the error-class table's guidance.

### Phase Test 5: MAX_ATTEMPTS cap
- **Run:** `uv run pytest tests/component/test_author_workflow_e2e.py::test_max_attempts_cap -v`
- **Expect:** a deliberately-impossible brief (e.g., "use a node
  type that doesn't exist") triggers 5 attempts, then the agent
  surfaces a summary + closest draft + remaining errors WITHOUT
  silent looping.

### Phase Test 6: Target bundle isolation
- **Run:** `uv run pytest tests/component/test_author_workflow_e2e.py::test_writes_to_target_bundle_only -v`
- **Expect:** scripted authoring against a workspace with two
  bundles writes ONLY to the configured target bundle. The
  `kolsch` package bundle's files are unchanged.

### Phase Test 7: validate_workflow tool round-trip
- **Run:** `uv run pytest tests/component/test_validate_workflow_tool.py -v`
- **Expect:** the tool correctly serialises every
  `WorkflowValidationError` subclass into the structured
  finding shape; invalid YAML surfaces as a parser-level finding.

### Phase Test 8: introspect_node tool round-trip
- **Run:** `uv run pytest tests/component/test_introspect_node_tool.py -v`
- **Expect:** the tool correctly identifies valid + invalid
  `@node` modules; signature errors surface with clear messages.

### Phase Test 9: No regressions
- **Run:** `uv run pytest tests/ -q`
- **Expect:** full suite green; existing chat / agent / workflow
  goldens byte-identical.

---

## Coordinator Prompt

> You are the strategic decision-maker. You do not write or review code.
> Drive the 4-step Coder/Reviewer flow.

**Your task:**

Ship Phase 3.11 through 4 sequential cycles. This is the **closing
phase of the v1 engine roadmap** — it composes existing surface
into an authoring loop. The risk is mostly authoring quality
(the skill carrying the design payload) and prompt iteration
(the meta-agent's small prompt being well-scoped).

**Authoritative references:**

- THIS spec: `process/phase3.11-author-workflow-meta-agent-build.md`.
- Master design spec: `process/phase3.11-author-workflow-meta-agent-spec.md`
  — §17.
- Phase 3.0 master spec: `process/phase3.0-workflow-spec.md` — §4,
  §5, §6, §7, §12. The skill encodes this content.
- Phase 3.10 spec: `process/phase3.10-concurrent-executor-spec.md`
  — §16. The skill's concurrency guidance.
- Existing helper agent: `src/cli/_starter_bundle/kolsch/agents/helper.yaml`
  — the asset shape to mirror.
- Existing skills: `src/cli/_starter_bundle/kolsch/skills/*/SKILL.md`
  — the skill shape to mirror.

**The 4 steps:**

| Step | Focus | Effort |
| --- | --- | --- |
| **Step 0** | `validate_workflow` + `introspect_node` tools end-to-end. Pure-function tools wrapping existing engine surface; structured findings + introspection output. Phase Tests 7, 8 wired. | ~1.5 days |
| **Step 1** | `author_workflow` meta-agent asset + the `author-workflow` skill. The skill is the design payload — most of Step 1's effort is authoring the markdown + error-class table + canonical examples. Manual chat smoke. | ~3 days |
| **Step 2** | Scripted integration tests for the four §17.10 scenarios. The harness mocks the LLM by recording + replaying scripted agent responses against `validate_workflow` tool dispatches. Phase Tests 1, 2, 3, 4 wired. | ~2 days |
| **Step 3** | Edge-case integration tests (MAX_ATTEMPTS cap, target-bundle isolation). Optional `author-loop.yaml` wrapper workflow. `platform.yaml` config knob. Phase Tests 5, 6, 9 wired. Roadmap line annotated. | ~1.5 days |

**Flow per step:** standard Coder/Reviewer cycle.

**When to make a strategic call:**

- Coder asks to relitigate a locked decision (auto-execute the
  generated workflow, generate agents/tools/skills, multi-bundle
  authoring, fine-tuning a dedicated model, dedicated `/author`
  slash) → REJECT.
- Coder asks to add new engine surface (a new event kind for
  "agent generated workflow X," a new error class, a runtime
  hook) → REJECT. Phase 3.11 is a surface phase.
- Coder asks to put the design payload in the prompt instead of
  the skill → REJECT. The skill is the level of indirection that
  matters; agents can be updated without re-shipping the asset.
- Coder finds master-spec ambiguity → consult, make a binding
  call, append to THIS spec, re-brief.
- Coder discovers a Phase 3.0 — 3.10 contract doesn't survive
  the LLM-generation workflow (e.g., the validator's error
  messages aren't actionable enough) → flag as a separate patch
  on the relevant phase; do not block Step 1.
- Scope creep (editor integration, IDE hover, agent-authoring
  meta-agent) → REJECT.

**Done when:** All 4 steps Approved AND all 9 Phase-level
Integration Tests pass AND `phase3.x-roadmap.md` Phase 3.11 line
annotated with the shipping SHA AND the v1 engine roadmap (3.0 →
3.11) is COMPLETE.

---

# STEP 0 — `validate_workflow` + `introspect_node` tools

**Goal**: ship the two new pure-read tools the meta-agent uses to
close its iteration loop. NO agent or skill yet; the tools are
independently usable.

**Files created/modified:**

- `bundles/kolsch/tools/validate_workflow.py` (new) — tool +
  Pydantic args/output models.
- `bundles/kolsch/tools/introspect_node.py` (new) — tool +
  Pydantic args/output models.
- `tests/component/test_validate_workflow_tool.py` (new) — Phase
  Test 7.
- `tests/component/test_introspect_node_tool.py` (new) — Phase
  Test 8.

**Effort**: ~1.5 days.

## Step 0 — Coder Prompt

> Reply "Step 0 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the two new tools. NO agent or skill yet.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.11-author-workflow-meta-agent-build.md` — Step 0.
2. `process/phase3.11-author-workflow-meta-agent-spec.md` — §17.3,
   §17.4.
3. `src/workflows/validator.py:188-275` — `validate_workflow` /
   `validate_workflow_or_raise`.
4. `src/workflows/parser.py:631-700` — `load_workflow`.
5. `src/workflows/catalog_node.py` — `@node` introspection.
6. `src/cli/_starter_bundle/kolsch/tools/write_file.py` — the
   shape every new tool mirrors (args model + async run +
   `@tool(...)` decorator).

**What to implement:**

1. **`bundles/kolsch/tools/validate_workflow.py`**:

   ```python
   from pathlib import Path
   from typing import Any
   from pydantic import BaseModel, Field

   from workflows import validate_workflow as engine_validate, \
       validate_workflow_or_raise
   from workflows.parser import load_workflow
   from workflows.errors import WorkflowValidationError, \
       WorkflowValidationWarning
   from kolsch.tools import tool, ToolContext

   class ValidateWorkflowArgs(BaseModel):
       workflow_ref: str | None = Field(default=None,
           description="Catalog ref of the workflow to validate")
       yaml_path: str | None = Field(default=None,
           description="OR an absolute path to a workflow YAML")

   class ValidationFinding(BaseModel):
       error_class: str
       identifying_names: dict[str, Any]
       caller_message: str
       file: str
       line: int

   class ValidationOutput(BaseModel):
       is_valid: bool
       errors: list[ValidationFinding]
       warnings: list[ValidationFinding]

   def _finding_from(exc):
       return ValidationFinding(
           error_class=type(exc).__name__,
           identifying_names={
               k: getattr(exc, k, None)
               for k in getattr(exc, "identifying_names_keys", ())
           },
           caller_message=getattr(exc, "caller_message", str(exc)),
           file=getattr(exc, "file", "<unknown>"),
           line=getattr(exc, "line", 0),
       )

   @tool(
       name="validate_workflow",
       summary=(
           "Validate a workflow YAML against the run-start gate. "
           "Returns structured findings."
       ),
       args_schema=ValidateWorkflowArgs,
   )
   async def validate_workflow_tool(args, ctx):
       # exactly one of workflow_ref / yaml_path
       if (args.workflow_ref is None) == (args.yaml_path is None):
           # both or neither — surface as a finding so the agent
           # doesn't crash trying to read it.
           return ValidationOutput(
               is_valid=False,
               errors=[ValidationFinding(
                   error_class="InvalidToolArgs",
                   identifying_names={},
                   caller_message="exactly one of workflow_ref / yaml_path required",
                   file="<args>", line=0,
               )],
               warnings=[],
           )
       if args.workflow_ref:
           entry = ctx.catalog.lookup(
               args.workflow_ref, expected_type="workflow"
           )
           if entry is None:
               return ValidationOutput(
                   is_valid=False,
                   errors=[ValidationFinding(
                       error_class="WorkflowNotFound",
                       identifying_names={"ref": args.workflow_ref},
                       caller_message=(
                           f"workflow {args.workflow_ref!r} not in catalog"
                       ),
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
           return ValidationOutput(
               is_valid=False,
               errors=[_finding_from(exc)],
               warnings=[],
           )
       result = engine_validate(workflow, ctx.catalog, file=str(path))
       return ValidationOutput(
           is_valid=result.is_valid,
           errors=[_finding_from(e) for e in result.errors],
           warnings=[_finding_from(w) for w in result.warnings],
       )
   ```

   - Tool registered with name `validate_workflow`.
   - Pure read; no `prompt:` policy effect needed (operators
     don't need a modal for a validation tool).
   - Catalog access via `ctx.catalog` (per the existing tool
     dispatch contract).

2. **`bundles/kolsch/tools/introspect_node.py`**:

   ```python
   class IntrospectNodeArgs(BaseModel):
       py_path: str = Field(
           description="absolute path to the authored @node module"
       )

   class NodeIntrospection(BaseModel):
       signature_ok: bool
       label: str | None
       inputs_schema: dict[str, Any] | None
       outputs_schema: dict[str, Any] | None
       errors: list[str]

   @tool(
       name="introspect_node",
       summary=(
           "Load an authored Python @node module and return its "
           "signature info or errors."
       ),
       args_schema=IntrospectNodeArgs,
   )
   async def introspect_node_tool(args, ctx):
       try:
           introspection = await _safe_introspect(Path(args.py_path))
       except Exception as exc:
           return NodeIntrospection(
               signature_ok=False, label=None,
               inputs_schema=None, outputs_schema=None,
               errors=[f"{type(exc).__name__}: {exc}"],
           )
       return introspection
   ```

   - `_safe_introspect(path)` wraps the existing
     `workflows.catalog_node` introspection helper.

     > **Amendment (post-Step-0 review).** The original wording
     > said "Loads the module under the per-run prefix per Phase
     > 3.10's `RunModuleNamespace` discipline." That doesn't fit:
     > the introspection tool is invoked from chat (or any
     > non-workflow context), so there is no active
     > `WorkflowRunContext` from which to derive a
     > `RunModuleNamespace`. The ship-side implementation uses a
     > synthetic per-path `sys.modules` key
     > (`_kolsch_introspect_<hash>_<stem>`) which achieves the
     > same goal — two concurrent introspections of different
     > files don't collide; re-introspecting the same path is
     > idempotent — without requiring a run id. Phase 3.10's
     > namespace remains the discipline for in-workflow imports;
     > this is the equivalent discipline for the pure-read,
     > out-of-workflow surface.
   - The Pydantic `Inputs` / `Outputs` schemas are returned via
     `Model.model_json_schema()` so the agent can read them.

3. **Tests in `tests/component/test_validate_workflow_tool.py`**:
   - **Valid workflow** (`kolsch/workflows/hello`) → returns
     `is_valid: true`, empty errors + warnings.
   - **YAML parse error** (malformed YAML on disk) → returns
     `is_valid: false` with a single finding whose
     `error_class` carries the parser's class name.
   - **Validator error** (workflow with two nodes sharing a name)
     → returns `WorkflowDuplicateNode` with
     `identifying_names: {"node": <name>}`.
   - **Missing catalog ref** → returns `WorkflowNotFound`.
   - **Both args set** → `InvalidToolArgs` finding.
   - **Neither arg set** → `InvalidToolArgs` finding.
   - **Warnings round-trip** — a workflow that emits a warning
     (e.g., a future warning class) surfaces it under `warnings`.

4. **Tests in `tests/component/test_introspect_node_tool.py`**:
   - **Valid `@node`** (the existing `kolsch/nodes/echo.py`) →
     `signature_ok: true`, label `"Echo"`, schemas populated.
   - **Missing decorator** → `signature_ok: false`, error names
     the violation.
   - **Wrong signature** (run accepts no parameters) →
     `signature_ok: false`, error names the violation.
   - **Missing Inputs/Outputs models** →
     `signature_ok: false`, error mentions both.
   - **Non-existent file** → `signature_ok: false`, error is a
     `FileNotFoundError`-style message.
   - **Syntax error in module** → `signature_ok: false`, error
     is a `SyntaxError`-style message.

**Step 0 Acceptance Criteria:**

0.1. `validate_workflow` tool ships under
     `bundles/kolsch/tools/`; tool registered with the kolsch
     starter bundle's manifest auto-discovery.
0.2. `introspect_node` tool ships similarly.
0.3. Both tools are pure read; no file mutations.
0.4. Both tools' Pydantic output models serialise cleanly to
     JSON so the agent can read them.
0.5. `_finding_from(exc)` correctly maps every
     `WorkflowValidationError` subclass's `identifying_names_keys`
     into the structured `identifying_names` dict.
0.6. Phase Tests 7, 8 pass.
0.7. No regressions: full suite green.

**Self-check before handoff:**

- [ ] All 7 ACs met
- [ ] Manual smoke: `kolsch tools list` shows the two new tools;
      `kolsch tools show kolsch/tools/validate_workflow` renders
      the schema cleanly
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 0 Complete — [summary]".

## Step 0 — Reviewer Prompt

> Reply "Step 0 Approved" or "Changes requested — [list]".

Verify each AC. PAY EXTRA ATTENTION to:

- AC 0.5 — `_finding_from` coverage. Read the helper; trace
  through every error class in `src/workflows/errors.py` to
  confirm each's `identifying_names_keys` round-trip correctly.
  A missed class would surface as `identifying_names: {}` and
  the meta-agent's skill table would mis-diagnose.
- AC 0.3 — purity. Grep both tools for any `write_text` /
  `subprocess.run` / `tempfile.write` calls. There must be
  none. The tools are pure read.

---

# STEP 1 — Meta-agent asset + authoring skill

**Goal**: ship the `author_workflow` agent asset + the
`author-workflow` skill. Manual chat smoke at the end of the step.
NO automated integration tests yet (Step 2 owns those).

**Files created/modified:**

- `bundles/kolsch/agents/author_workflow.yaml` (new) — the
  meta-agent asset.
- `bundles/kolsch/skills/author-workflow/SKILL.md` (new) — the
  authoring skill.
- `bundles/kolsch/skills/author-workflow/examples/` (new) —
  canonical example workflows + transcripts.

**Effort**: ~3 days.

## Step 1 — Coder Prompt

> Reply "Step 1 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the agent asset + skill. The skill is the DESIGN PAYLOAD —
budget most of your time on its quality.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.11-author-workflow-meta-agent-build.md` — Step 1.
2. `process/phase3.11-author-workflow-meta-agent-spec.md` — §17.5,
   §17.6, §17.7, §17.8, §17.9.
3. `process/phase3.0-workflow-spec.md` — §4 (node catalog), §5
   (authored Python contract), §6 (graph topology), §7 (run-start
   gate), §12 (error vocabulary). The skill encodes this content.
4. `src/cli/_starter_bundle/kolsch/agents/helper.yaml` — agent
   asset shape to mirror.
5. `src/cli/_starter_bundle/kolsch/skills/write-python-test/SKILL.md`
   — skill shape to mirror.

**What to implement:**

1. **`bundles/kolsch/agents/author_workflow.yaml`** per spec
   §17.5. Key details:
   - `io: { mode: conversational }`.
   - `context_files:` includes both `workflow-foundation.md` and
     `authoring-a-workflow.md` (with `primary: true`).
   - `subsystems.skills.config.enabled` includes the new skill ref.
   - `subsystems.tools.config.declared` includes the four standard
     tools (`read_file`, `write_file`, `list_files`, `bash`)
     plus the two new Phase 3.11 tools.
   - `permissions:` matches the helper agent's pattern for `bash`
     (`effect: prompt`).
   - Small prompt (under 50 lines) that DEFERS the design payload
     to the skill. The prompt's role is to bind the agent's
     identity + delegate the rest.

2. **`bundles/kolsch/skills/author-workflow/SKILL.md`** per spec
   §17.6. The skill's sections:

   ```markdown
   ---
   name: author-workflow
   description: Encode the Kolsch workflow grammar + iteration recipe + error-class table so an LLM can author valid workflows from natural-language briefs.
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

   ## When this skill applies

   ## The iteration recipe

   ## The workflow YAML grammar
     ### Top-level fields
     ### Node types — the 14 built-ins
     ### Authored Python @node files
     ### Edges + binds + fan-in
     ### Modifiers — `when:`, `retry:`, `timeout_seconds:`, `description:`
     ### Concurrency — when to opt in
     ### Sub-workflows + map

   ## The error-class table

   ## Anti-patterns

   ## Canonical examples
     ### Example 1 — small bash pipeline
     ### Example 2 — triage with agent
     ### Example 3 — auto-mode fan-out

   ## Where to write
     ### Resolving the target bundle

   ## When to escalate (MAX_ATTEMPTS cap)
   ```

   Each section is fleshed-out prose + code blocks. The
   error-class table covers every concrete subclass of
   `WorkflowValidationError` in `src/workflows/errors.py`.

3. **`bundles/kolsch/skills/author-workflow/examples/`** — three
   subdirectories, each with:
   - `brief.txt` — the natural-language brief.
   - `transcript.md` — a sample agent transcript showing the
     iteration loop (draft → validate → revise).
   - `workflow.yaml` — the final workflow.
   - `nodes/*.py` — any authored Python the example needs.

4. **Manual chat smoke** — at the end of Step 1, run:

   ```bash
   uv run kolsch --root /tmp/kolsch-author-test setup --non-interactive
   uv run kolsch --root /tmp/kolsch-author-test chat --agent kolsch/agents/author_workflow
   # Then type a brief:
   #   "Build a workflow that classifies a ticket as urgent or normal."
   # Observe the agent: discovers existing classifier agent, drafts YAML,
   # calls validate_workflow, surfaces the ref.
   ```

   Document any iteration-loop misbehavior + adjust the skill +
   prompt to address it.

**Step 1 Acceptance Criteria:**

1.1. Agent asset ships under
     `bundles/kolsch/agents/author_workflow.yaml`.
1.2. Skill ships under `bundles/kolsch/skills/author-workflow/`
     including `SKILL.md` + three examples.
1.3. Skill's error-class table covers every concrete
     `WorkflowValidationError` subclass in
     `src/workflows/errors.py` (verified by a grep against the
     errors module).
1.4. Skill's grammar section covers all 14 built-in node types
     + the authored Python `@node` form.
1.5. Manual chat smoke: a from-scratch brief produces a valid
     workflow file within 3 turns.
1.6. The agent's prompt is under 50 lines and DEFERS to the
     skill for design content.
1.7. The skill writes only to the configured target bundle
     (its "Where to write" section enforces this rule).
1.8. No regressions.

**Self-check before handoff:**

- [ ] All 8 ACs met
- [ ] Manual chat smoke succeeds
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 1 Complete — [summary]".

## Step 1 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 1.3 — error-class coverage. Run
  `grep -E '^class Workflow[A-Z]\w+.*WorkflowValidationError' src/workflows/errors.py`;
  every class name returned must appear in the skill's table.
  Missing classes are a foreseeable agent failure mode (the
  agent can't diagnose what isn't in the table).
- AC 1.5 — manual smoke quality. Run the smoke yourself; if the
  agent's first response is generic or doesn't invoke
  `validate_workflow`, the skill's iteration recipe isn't sharp
  enough. Reject + iterate.

---

# STEP 2 — Scripted integration tests for the four §17.10 scenarios

**Goal**: ship the test harness + automated Phase Tests 1-4. The
harness mocks the LLM by replaying recorded agent responses; the
real engine surface (validator, introspection, tools, file I/O)
runs unmocked.

**Files created/modified:**

- `tests/component/test_author_workflow_e2e.py` (new) — Phase
  Tests 1-4.
- `tests/component/fixtures/author_workflow/` (new) — recorded
  agent response scripts + expected workflow YAMLs.

**Effort**: ~2 days.

## Step 2 — Coder Prompt

> Reply "Step 2 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the four §17.10 verification tests via a scripted harness.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.11-author-workflow-meta-agent-build.md` — Step 2.
2. `process/phase3.11-author-workflow-meta-agent-spec.md` — §17.10.
3. Existing agent integration tests under `tests/component/` —
   the patterns for mocking the LLM provider.

**What to implement:**

1. **Test harness**: `_run_author_workflow_session(*, brief, scripted_responses)`:
   - Spins up the chat with the meta-agent.
   - Feeds the brief.
   - Mocks the LLM provider to return `scripted_responses` in
     order. Each scripted response either invokes a tool (which
     runs UNMOCKED) or emits a final text response.
   - Returns the final session state + any files written.

2. **Phase Test 1** — from-scratch authoring:
   - Brief: "Build a workflow that classifies a ticket and prints
     urgent or normal."
   - Scripted responses: (a) call `validate_workflow` on
     `kolsch/workflows/hello-agent` to inspect the existing
     classifier pattern; (b) call `write_file` to draft a new
     workflow YAML; (c) call `validate_workflow` on the new
     YAML; (d) on success, emit a final ref + suggested next
     step.
   - Assertions:
     - Workflow file exists at the expected path under the
       target bundle.
     - `validate_workflow` returned `is_valid: true`.
     - Running the produced workflow via
       `kolsch workflows run <ref>` succeeds.

3. **Phase Test 2** — amendment loop:
   - Reuses Phase Test 1's produced workflow.
   - Brief: "Also write the classification to a file at
     `out/result.txt`."
   - Scripted responses: (a) `read_file` the existing YAML; (b)
     `write_file` the modified YAML; (c) `validate_workflow`; (d)
     final ref.
   - Assertions: workflow file updated, validates, runs, writes
     the file.

4. **Phase Test 3** — `@node` generation:
   - Brief: "Build a workflow that takes a CSV path, counts
     rows, and prints the count."
   - Scripted responses: (a) `write_file` the new `@node`
     module; (b) `introspect_node` (returns `signature_ok: true`);
     (c) `write_file` the workflow YAML; (d) `validate_workflow`;
     (e) final ref.
   - Assertions: both files exist; both validate; workflow runs.

5. **Phase Test 4** — validation-driven revision:
   - Brief: any brief whose naive translation triggers
     `WorkflowBindCollision`.
   - Scripted responses: (a) `write_file` draft 1; (b)
     `validate_workflow` returns the collision finding; (c)
     `write_file` draft 2 (renamed bind); (d) `validate_workflow`
     returns success; (e) final ref.
   - Assertions: TWO `write_file` calls; the second YAML differs
     from the first only at the colliding bind key; second
     validates.

**Step 2 Acceptance Criteria:**

2.1. `_run_author_workflow_session` harness implemented and
     reusable across the four scenarios.
2.2. Phase Tests 1, 2, 3, 4 pass.
2.3. Each test asserts BOTH the agent's tool-call sequence AND
     the produced workflow's runnability via
     `kolsch workflows run`.
2.4. Scripted responses live under
     `tests/component/fixtures/author_workflow/` so they're
     easy to update when the skill or prompt changes.
2.5. No regressions.

**Self-check before handoff:**

- [ ] All 5 ACs met
- [ ] Each test runs in under 5 seconds (harness avoids real LLM
      calls)
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 2 Complete — [summary]".

## Step 2 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 2.3 — actual runnability. The test must ACTUALLY run the
  produced workflow via the real `IterativeExecutor` (not a
  mock) and assert the terminal output. A test that only checks
  `validate_workflow` returns true misses the real verification
  gate ("validates AND runs").

---

# STEP 3 — Edge cases + config knob + roadmap annotation

**Goal**: ship the MAX_ATTEMPTS cap test + target-bundle isolation
test + the optional `author-loop.yaml` wrapper + the
`platform.yaml` `author_workflow.target_bundle` knob + the
roadmap annotation.

**Files created/modified:**

- `tests/component/test_author_workflow_e2e.py` (modified) —
  Phase Tests 5, 6 wired.
- `bundles/kolsch/workflows/author-loop.yaml` (new) — wrapper
  workflow.
- `src/cli/_setup.py` (modified, if needed) — default
  `author_workflow.target_bundle` resolution in
  `platform.yaml`.
- `process/phase3.x-roadmap.md` (modified) — Phase 3.11 line
  annotated.

**Effort**: ~1.5 days.

## Step 3 — Coder Prompt

> Reply "Step 3 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship Phase Tests 5, 6, 9 + the config knob + the wrapper + the
roadmap annotation.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.11-author-workflow-meta-agent-build.md` — Step 3.
2. `process/phase3.11-author-workflow-meta-agent-spec.md` — §17.1
   (config knob), §17.8 (MAX_ATTEMPTS), §17.10 (verification).
3. `process/phase3.x-roadmap.md` — Phase 3.11 line.

**What to implement:**

1. **Phase Test 5** — MAX_ATTEMPTS cap:
   - Brief: "Use a node type called `kolsch/nodes/quantum_compute`."
     (Does not exist.)
   - Scripted responses: five drafts each invoking
     `validate_workflow`, each returning an error (e.g., unknown
     type ref). The fifth attempt's response surfaces a summary
     + closest draft + remaining errors WITHOUT a sixth
     `validate_workflow` call.
   - Assertions: exactly 5 `validate_workflow` invocations; final
     response includes the cap-reached escalation message.

2. **Phase Test 6** — target-bundle isolation:
   - Setup workspace with `kolsch` (package) and `my-bundle`
     (user) bundles. `platform.yaml`'s
     `author_workflow.target_bundle: my-bundle`.
   - Run Phase Test 1's brief.
   - Assertions: every `write_file` invocation targets
     `bundles/my-bundle/`; the `kolsch` bundle is unchanged
     (mtime + content snapshot).

3. **`platform.yaml` config knob**:
   - Default-bundle resolution: if not set, defaults to the
     workspace's primary user bundle (the first non-`kolsch`
     bundle in `bundles/`). If only `kolsch` exists, the agent
     surfaces an error: "no user bundle configured — set
     `author_workflow.target_bundle` or run
     `kolsch bundles init my-bundle`."
   - The skill reads this config and surfaces the resolved
     target bundle to the agent on session start.

4. **`bundles/kolsch/workflows/author-loop.yaml`** (optional
   wrapper):

   ```yaml
   description: |
     Wrap the author_workflow agent in a user_input → agent loop
     so authors can iterate hands-off via the workflow surface.

   nodes:
     get_brief:
       type: kolsch/nodes/user_input
       prompt: "Describe the workflow you want to build."
     author:
       type: kolsch/nodes/agent
       agent: kolsch/agents/author_workflow
     follow_up:
       type: kolsch/nodes/user_input
       prompt: "Any changes, or press enter to finish."

   graph:
     edges:
       - from: get_brief
         to: author
         binds:
           brief: "{{ nodes.get_brief.response }}"
       - from: author
         to: follow_up
   ```

   Optional but useful — ships under the kolsch starter bundle so
   `kolsch/workflows/author-loop` is available immediately after
   `kolsch setup`.

5. **Phase Test 9** — full regression `uv run pytest tests/ -q`.

6. **Roadmap annotation**:

   ```
   ### 3.11 — `author_workflow` meta-agent — ✅ Done (`<SHA>`)
   ```

   After ship: update with the shipping SHA per the existing
   roadmap convention.

**Step 3 Acceptance Criteria:**

3.1. Phase Test 5 (MAX_ATTEMPTS cap) passes; agent never loops
     past 5 attempts.
3.2. Phase Test 6 (target-bundle isolation) passes; `kolsch`
     bundle bytes unchanged.
3.3. `platform.yaml` config knob shipped with default resolution
     to primary user bundle.
3.4. `author-loop.yaml` ships under the kolsch starter bundle
     (or is explicitly skipped with a documented rationale).
3.5. Phase Test 9 (full regression) passes.
3.6. Roadmap line annotated with shipping SHA.
3.7. No regressions.

**Handoff trigger:** All ACs met → "Step 3 Complete — [summary]".

## Step 3 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 3.1 — cap discipline. The agent MUST stop at 5 attempts.
  An off-by-one (6 attempts) is a foreseeable bug; trace the
  iteration recipe in the skill carefully.
- AC 3.2 — `kolsch` bundle inviolability. Snapshot the bundle's
  files BEFORE the test; assert byte-identical AFTER. A single
  accidental write to the package bundle is a P0 (package
  bundles are read-only by convention).
- AC 3.5 — full suite green. The v1 engine roadmap closes with
  this step. Any regression here is a sign Phase 3.11 broke
  something earlier; investigate before declaring done.

---

## Done when

- All 4 steps Approved by Reviewer.
- All 9 Phase-level Integration Tests pass.
- `process/phase3.x-roadmap.md` Phase 3.11 line annotated with
  the shipping SHA.
- The v1 engine roadmap (Phase 3.0 → Phase 3.11) is COMPLETE.
- Manual smoke test:

  ```bash
  uv run kolsch --root /tmp/kolsch-fresh setup --non-interactive
  # set platform.yaml's author_workflow.target_bundle, or accept default

  uv run kolsch --root /tmp/kolsch-fresh chat \
      --agent kolsch/agents/author_workflow

  # In chat:
  # > Build me a workflow that classifies a ticket and prints urgent or normal.
  # (agent inspects catalog, drafts YAML, calls validate_workflow,
  #  emits final ref)
  # > Also write the classification to a file at out/result.txt.
  # (agent reads, modifies, validates, writes back)
  # > /workflow <ref> with ticket_text="my server is on fire"
  # (the produced workflow runs end-to-end; result.txt gets written)
  ```
