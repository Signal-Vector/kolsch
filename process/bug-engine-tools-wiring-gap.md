# Bug — Engine never binds declared tools to the framework; LLM gets no tools and hallucinates

**Status: shipped** — 7-commit fix ending at `1875a70` ("make_llm_callable + tool descriptions — PR 3"). Verified during the 2026-06-08 audit.

**Draft 0.2** *(replaces draft 0.1; splits the monolithic two-prompt
fix into eight dependency-ordered sub-steps so each developer prompt
is reviewable in one sitting and each reviewer has ≤6 checks. Coverage
unchanged.)*

> Reported symptom: a user runs the starter helper agent against
> `opencode/minimax-m2.7` with input "run the bash command ls". The
> agent replies with a confidently hallucinated Linux root listing
> (`bin boot dev etc ...`). The session JSONL shows `config.set`
> correctly carries `subsystems.tools.config.declared = [read_file,
> write_file, list_files, bash]`, but the subsequent `model.request`
> event sends ZERO tools to the model. No `tool.requested` events.
> No `tool.completed` events. The tools are declared in config but
> never reach the framework.
>
> The root cause is a wiring gap: nothing in `src/engine/`,
> `src/providers/`, or `src/llm/` reads `subsystems.tools` from
> the resolved config. Phase 2.0 implementation shipped the tool
> files but stopped before the engine→framework hand-off.

---

## Part 0 — Symptom & Reproduction

**Reported by:** user, on `main`, with Phase 2.0 tool files
present at `bundles/kolsch/tools/{read_file,write_file,list_files,
bash}.py` and Phase 2.1 at Step 24a.

**Setup:**
1. Workspace at `~/Documents/kolsch`, set up via `kolsch setup`.
2. OpenCode + OpenRouter connected.
3. Starter agent at `bundles/kolsch/agents/helper.yaml` with
   `subsystems.tools.config.declared` listing all four builtin
   refs.

**Action:**
```bash
uv run kolsch run kolsch/helper --input content="run the bash command ls"
```

**Observed event sequence (verbatim from the user's JSONL):**

```
session.opened
config.set                ← subsystems.tools.config.declared = [4 tools]
thread.exchange_started
user.message              ← "run the bash command ls"
model.request             ← messages = [system, user]; NO tools field
model.response            ← "Here is the result of the `ls` command: bin boot dev etc home lib ..."
agent.message             ← (same content as model.response — pure hallucination)
thread.exchange_completed
```

No `tool.requested`, no `tool.completed`, no `authz.*` events.

**Four ground-truth checks confirm the wiring gap:**

1. `ls bundles/kolsch/tools/` → all four files exist on disk.
2. `uv run kolsch run kolsch/helper --input content="hi" -v 2>&1 | grep -iE "tool|skill|catalog"` → no log lines mention tool discovery or registration.
3. `uv run kolsch chat --help` → succeeds; help text contains
   `(24e — accepted but ignored in 24a)` — Phase 2.1 is at Step
   24a only.
4. `grep -rn "subsystems.tools" src/engine/ src/providers/ src/llm/`
   → **zero matches.**

---

## Part 1 — Hypotheses (chain, all likely true simultaneously)

### H1. Tool Catalog is unpopulated at runtime

The catalog scanner doesn't run, or doesn't scan `bundles/*/tools/`.
No callables to bind even if downstream code asked for them.

### H2. The "standard" tools subsystem variant is config-only

The variant accepts and emits the declared list at config-resolution
time, but has no runtime `load(declared) → list[Callable]` method.

### H3. Engine pre-flight has no "bind tools" step

Phase 1's pre-flight § 2.1 lists steps; Phase 1.1 added 5.5
("acquire LLM client"). Phase 2.0 should have inserted a 5.7
("bind declared tools"). Grep proves no engine code reads
`subsystems.tools` — the step was never added.

### H4. CrewAI framework adapter ignores tools

The adapter constructs the CrewAI Agent without a `tools=[...]`
parameter. Even if H1–H3 were fixed, tools couldn't reach the
model.

**Cause chain:** H1 → no callables → H2 → no transform from
refs to callables → H3 → no engine step requesting them → H4 →
adapter wouldn't pass them anyway. All four need fixing.

---

## Part 2 — Acceptance Criteria (applies across all steps)

The bug is fixed when **all** of the following hold:

1. End-to-end tool use works against a fake LLM rigged to emit
   `tool_use("bash", command="echo hello")`. Events emitted:
   `tool.requested`, `tool.completed` with real output, then
   `agent.message` reflecting that real output.
2. `model.request.data.tools` is non-empty when the agent
   declares tools. Asserted by inspecting the event payload.
3. The four builtin tools are discovered by the catalog at
   bootstrap; a `-v` log line names them.
4. A declared tool ref that doesn't resolve fails at agent-load
   with `config.unknown_tool` — NOT silently at first call.
5. Zero regression in existing Phase 1.x or 2.0 tests.
6. The user's hallucination repro fails on pre-fix code, passes
   on post-fix code. `model.response` reflects REAL tool output,
   not `bin boot dev etc...`.

---

## Part 3 — Step dependency graph

```
Step A (investigate)
    │
    ▼
Step B (catalog scans bundles/*/tools/) ─────────────────┐
    │                                                    │
    ▼                                                    │
Step C (variant load: refs → callables)                  │
    │                                                    │
    ├──────────────────┐                                 │
    │                  │                                 │
    ▼                  ▼                                 │
Step E (engine     Step D (validation:                   │
  pre-flight         missing refs raise                  │
  binds tools)       config.unknown_tool)                │
    │                                                    │
    ▼                                                    │
Step F (CrewAI adapter passes tools=[...] to model) ◄────┘
    │
    ▼
Step G (end-to-end regression test)
    │
    ▼
Step H (manual e2e + findings report)
```

| Step | What lands | Status |
|---|---|---|
| **A** | Investigation + writeup; no code | **done** (see E-BUG-WIRE-001) |
| **B** | Catalog scans bundle tools at bootstrap | **already in main** (verify only) |
| **C** | "standard" variant has `load()` method | **already in main** (verify only) |
| **D** | `config.unknown_tool` raised when ref missing (engine-side) | combined into PR 1 |
| **E** | Engine pre-flight registers declared tools with subsystem | combined into PR 1 |
| **F** | CrewAI adapter passes tools to model | **already in main** (verify only) |
| **G** | End-to-end regression test (TC-TOOL-WIRE-01) | combined into PR 1 |
| **H** | Manual verification + F1/F2/F3 findings handoff | PR 2 |

**Execution plan (updated 2026-05-30 after Step A):** the Step A
investigation found B, C, and F already in `main`. The actual fix
ships as **two PRs**:

- **PR 1 — combined D + E + G** (~80 lines, 3 files): adds
  `config.unknown_tool` engine-side, wires pre-flight
  registration, ships the regression test. Reviewer also runs
  the verify-only checks for B, C, F in this single review.
- **PR 2 — Step H**: manual real-LLM verification + findings
  handoff in the PR description.

The detailed per-step dev/reviewer prompts below remain accurate
as specifications for what each step needs to satisfy, even where
they collapse into a single PR. The original 8-step shape is
retained as the record of the investigation's findings.

---

## Step A — Investigation and root-cause writeup

- **Builds:** a written confirmation (or correction) of the H1–H4
  hypothesis chain with file/line citations + an estimated fix
  shape (file list, diff size).
- **No code changes.** Pure read-only investigation.
- **Acceptance:** the writeup posted to the coordinator covers
  every hypothesis with concrete citations and proposes a fix
  shape under 250 lines / 4 files; coordinator approves to
  proceed to Step B.

### Developer prompt

```
[standing instructions]

Step A — Confirm or correct the hypothesis chain.

Read first:
- process/bug-engine-tools-wiring-gap.md, in full
  (especially Parts 0, 1, 2).
- docs/explanation/tools-subsystem.md (the catalog contract).
- docs/explanation/agent-engine-contract.md §2.1 (pre-flight
  step list).
- process/phase2.0-tools-specs.md Parts 1, 2.

Investigation tasks:

1. Re-run the four ground-truth checks from Part 0 of the bug
   doc. Confirm they still produce the same outputs. If anything
   has changed since the bug was filed (someone partially fixed
   it), note that.

2. Read src/ for these four questions:
   (a) H1 — Does anything scan bundles/*/tools/ at bootstrap?
       Find the scanner or confirm none exists.
   (b) H2 — Does the "standard" tools variant have a runtime
       load method? Find the class and read its public surface.
   (c) H3 — Where in src/engine/ would step 5.7 go? Find the
       existing pre-flight step list; identify the insertion
       point.
   (d) H4 — Find the CrewAI adapter; locate the Agent (or
       equivalent) construction. Confirm tools= is absent.

3. Write a root-cause document with this structure:
   - For each of H1, H2, H3, H4: status (confirmed / corrected),
     file:line citations, what the code currently does, what
     it needs to do.
   - Proposed fix shape: file list, estimated diff size per file.
   - Open questions: anything in the spec that's ambiguous and
     needs coordinator clarification.

4. STOP. Post the writeup. Do NOT proceed to Step B without
   coordinator approval (the next sub-step prompt is in this
   build doc; the coordinator will give you go).

ESCALATION PATH: if the writeup reveals the fix exceeds 250
lines OR touches more than 4 files OR requires structural
rework of an existing subsystem, escalate per the coordinator
section at the end of this doc.
```

### Reviewer prompt

```
[standing instructions]

Review Step A — the investigation writeup. There is NO code
to review at this step. The writeup itself is the deliverable.

Checks:

1. Every hypothesis (H1, H2, H3, H4) has concrete file:line
   citations. "I looked and didn't find it" is acceptable for a
   "this code doesn't exist" finding, but the developer must
   say where they looked.

2. The proposed fix shape is concrete: specific file paths,
   specific functions to add or modify, rough line counts. A
   vague "modify the engine" is a FAIL.

3. The fix size estimate is realistic. Under 250 lines / 4
   files matches the bug doc's expectation; significantly over
   is an escalation trigger.

4. Open questions (if any) are real ambiguities, not "I didn't
   bother to look." A genuine question gets escalated to the
   coordinator; a lazy question gets sent back for more digging.

Verdict: APPROVE-TO-PROCEED or REVISE with specific gaps.
```

---

## Step B — Catalog scans `bundles/*/tools/` at bootstrap

- **Builds:** the Tool Catalog scans every workspace bundle's
  `tools/` directory at runtime bootstrap, importing each `.py`
  file and registering `@tool`-decorated functions. A diagnostic
  log line names how many tools were registered from where.
- **Depends on:** Step A approved.
- **Does NOT make `kolsch run` work with tools.** This step
  populates the catalog; consumers come in C–F.
- **Acceptance:** unit test confirms a synthetic bundle's tools
  are discoverable from the catalog after bootstrap; the four
  builtin tools are discoverable in any workspace that has
  them.

### Developer prompt

```
[standing instructions]

Step B — Make the Tool Catalog scan bundle tool files at
runtime bootstrap.

Read first:
- Step A's writeup (in the PR description of step-A's PR or
  the build doc's escalation log).
- docs/explanation/tools-subsystem.md (catalog contract).
- The existing src/tools/ (or wherever the catalog lives).

Tasks:

1. Find or create the catalog's `scan_bundles(workspace_root)`
   method. It walks `<workspace_root>/bundles/*/tools/*.py`,
   imports each module, and registers all `@tool`-decorated
   functions under their canonical ref
   (`<bundle>/tools/<filename_without_ext>`).

2. Call `scan_bundles` from the runtime bootstrap path so the
   catalog is populated before any agent loads.

3. Add ONE diagnostic log line per bundle scanned:
   `INFO catalog: registered N tools from bundles/<bundle>/tools/`
   This is what `kolsch run -v` shows.

4. Unit test in tests/unit/tools/test_catalog_scan.py (or
   equivalent):
   - Create a tmp workspace with one bundle containing two
     tool files.
   - Bootstrap the catalog with `scan_bundles(tmp)`.
   - Assert both tool refs are in the catalog.
   - Assert the log line was emitted.

5. Do NOT wire the catalog into the engine yet. Step E does
   that. This PR only ensures the catalog HAS the tools.

Scope: ~50–80 lines of code + ~30 lines of test. Single file
plus the bootstrap call-site.

ESCALATION PATH: if the catalog has no scanner abstraction at
all and you'd be creating a new module rather than adding a
method, escalate — the spec assumed the abstraction existed.

Acceptance: unit test passes; `kolsch run -v` against the real
workspace shows the "registered N tools" line; no engine code
touched.
```

### Reviewer prompt

```
[standing instructions]

Review Step B — catalog scan at bootstrap.

Checks:

1. The scanner imports each .py file once. No duplicate scans,
   no recursive import loops. Verify by mocking the importer
   and asserting call counts in the unit test.

2. The scanner is called from runtime bootstrap, NOT from the
   engine's per-exchange path. Find the call site; confirm it's
   in the runtime construction, not in `execute()`.

3. The diagnostic log line is INFO level (visible with `-v`),
   NOT DEBUG. The user's earlier `-v | grep -iE "tool"` finding
   nothing must be reversed.

4. The unit test creates a SYNTHETIC bundle (not pointing at
   bundles/kolsch/tools/). Reason: the test must work in any
   environment, including CI without the workspace.

5. NO code in src/engine/ was touched. Step B does not wire
   the catalog into anything.

6. Run the test. Run `kolsch run -v ...` against the real
   workspace and confirm the new log line appears.

Verdict: PASS or FAIL with specific defects.
```

---

## Step C — "standard" tools subsystem variant gets a `load()` method

- **Builds:** the tools subsystem's `standard` variant gains a
  runtime method that takes declared refs and returns a list of
  bound tool objects (callable + metadata for the framework).
- **Depends on:** Step B in main.
- **Acceptance:** unit test confirms `variant.load([refs]) →
  [BoundTool]` correctly maps refs to catalog entries.

### Developer prompt

```
[standing instructions]

Step C — Add a runtime load method to the "standard" tools
variant.

Read first:
- Step B's PR (the catalog API is your dependency).
- The "standard" variant's current code (find via grep
  'standard' under src/tools/ or src/providers/tools/).

Tasks:

1. Define a `BoundTool` dataclass (or named tuple):
   - `ref: str` — the canonical ref, e.g. "kolsch/bash".
   - `name: str` — the function name.
   - `callable: Callable` — the imported function.
   - `schema: dict` — the JSON schema for tool-use (derived from
     the function signature + docstring). CrewAI/litellm needs
     this for the model's tool_use protocol.

2. Add `StandardToolsVariant.load(declared_refs: list[str],
   catalog: Catalog) -> list[BoundTool]`:
   - For each ref, look it up in the catalog (Step B).
   - If found, produce a BoundTool with the catalog entry's
     callable + derived schema.
   - If NOT found, raise NotImplementedError for now — Step D
     replaces this with `config.unknown_tool`.

3. Unit test in tests/unit/tools/test_standard_variant.py:
   - Build a catalog with two synthetic tools.
   - Build declared refs pointing at both.
   - Call `variant.load(refs, catalog)`.
   - Assert the returned list has length 2 and the callables
     match.

4. Do NOT wire this method into the engine yet. Step E does.

Scope: ~60–90 lines of code (mostly the schema derivation —
introspect the function signature into JSON schema, or use a
library like pydantic if already imported) + ~40 lines of test.

ESCALATION PATH: if the schema-derivation logic is non-trivial
(some tool functions have complex signatures that don't map
cleanly to JSON schema), escalate — the coordinator decides
whether to ship "schema or None" with a fallback or invest in
the derivation.
```

### Reviewer prompt

```
[standing instructions]

Review Step C — variant load method.

Checks:

1. BoundTool's `schema` field is a valid JSON schema. Manually
   inspect the test's output for one tool — does the schema
   correctly describe the function's parameters?

2. The load method is PURE. No side effects. No log writes
   beyond debug. Same inputs → same outputs.

3. Missing-ref handling is `raise NotImplementedError` (or
   similar placeholder) — Step D replaces this. Confirm the
   placeholder is obvious and not silently returning None.

4. The unit test covers (a) all refs resolve, (b) test with
   one bad ref — currently asserts the NotImplementedError.

5. No engine code was touched.

Verdict: PASS or FAIL.
```

---

## Step D — Validation: missing tool refs raise `config.unknown_tool` at agent-load

- **Builds:** the Layer-1 validator catches declared tool refs
  that the catalog doesn't know. Replaces Step C's
  `NotImplementedError` placeholder with the typed
  `config.unknown_tool` error.
- **Depends on:** Steps B and C in main.
- **Acceptance:** loading an agent with `tools: [{ref:
  nonexistent}]` fails with `config.unknown_tool`.

### Developer prompt

```
[standing instructions]

Step D — Add validation so an agent with bad tool refs fails
at load time.

Read first:
- Step C's PR (the placeholder NotImplementedError).
- docs/explanation/error-model.md (the error catalog — you'll
  add a new code here).

Tasks:

1. Add `config.unknown_tool` to the error catalog in
   docs/explanation/error-model.md. Level: Fatal. Detail:
   `{ref, available_refs}`. Where: agent-load via the resolver.

2. Replace Step C's `raise NotImplementedError` with
   `raise PlatformError("config.unknown_tool", {"ref": ref,
   "available_refs": catalog.known_refs()})`.

3. Wire the variant's load method into the resolver's Layer-1
   validation path so the error fires at agent-load time, not
   at exchange execution. Specifically: when the resolver
   processes `subsystems.tools.config.declared`, it should
   immediately call the variant's load (with the catalog
   reference threaded through). Any failure raises before
   `build_resolved_config` returns.

4. Unit test in tests/unit/tools/test_validation.py:
   - Build a catalog with two tools.
   - Load an agent declaring tools that include ONE bad ref.
   - Assert `PlatformError(config.unknown_tool)` raised at
     load, with detail naming the bad ref and listing the
     available ones.

5. Also update process/test-catalog.md: add a TC entry
   under the existing TOOL section for this validation case.

Scope: ~40–60 lines code + ~30 lines test + small doc additions.

ESCALATION PATH: if the resolver doesn't have a clean place to
plug variant validation, escalate — the spec assumed this
extension point exists per Phase 2.0 spec §6.
```

### Reviewer prompt

```
[standing instructions]

Review Step D — agent-load validation.

Checks:

1. The error code `config.unknown_tool` is in the catalog
   (docs/explanation/error-model.md). Level Fatal. Detail
   matches the patch.

2. The error fires at AGENT LOAD, not at exchange execution.
   Verify by tracing the call: load_resolved_config(...) →
   variant.load(...) → raises. NOT engine.execute(...) →
   raises.

3. The error message names BOTH the bad ref AND the available
   refs — actionable for the user. A generic "unknown tool"
   message is a fail.

4. The unit test exercises the load path, not the engine path.

5. The test-catalog entry exists and matches the spec format.

Verdict: PASS or FAIL.
```

---

## Step E — Engine pre-flight step 5.7 binds tools to the exchange

- **Builds:** new step in the engine's pre-flight that reads
  `config.subsystems.tools.config.declared`, calls the
  variant's load (Step C), and stores the result on the
  exchange's ExecutionContext for the framework adapter.
- **Depends on:** Steps B, C, D in main.
- **Acceptance:** unit test confirms the engine's pre-flight
  runs the bind step and the bound tools are accessible from
  the ExecutionContext.

### Developer prompt

```
[standing instructions]

Step E — Add the "bind tools" step to the engine pre-flight.

Read first:
- docs/explanation/agent-engine-contract.md §2.1 (the pre-flight
  step list; find where step 5.7 goes — after 5.5 acquire LLM,
  before the model call).
- src/engine/engine.py (the existing pre-flight implementation).

Tasks:

1. Insert step 5.7 in the engine's pre-flight (or wherever the
   existing 5.5 lives — keep it adjacent):
   ```
   # Step 5.7: bind declared tools to the exchange.
   declared = config.subsystems.tools.config.declared
   bound_tools = self.tools_variant.load(declared, self.catalog)
   ctx.bound_tools = bound_tools
   ```
   Adjust naming to match existing patterns.

2. Add `bound_tools: list[BoundTool]` to the ExecutionContext.

3. Update docs/explanation/agent-engine-contract.md §2.1 to
   document step 5.7 as part of the pre-flight. (The contract
   doc should reflect the runtime; this is a small spec
   revision.)

4. Unit test in tests/unit/engine/test_preflight.py (extend an
   existing test file if one exists):
   - Run an exchange (with a fake LLM that emits no tool_use)
     against an agent with declared tools.
   - Inspect the ExecutionContext mid-exchange (via a hook or
     by capturing it in the fake framework).
   - Assert ctx.bound_tools has the expected number of entries.

5. Do NOT wire bound_tools into the framework adapter yet.
   Step F does. This step only ensures the context HAS the
   tools available.

Scope: ~30–50 lines code + ~30 lines test + small contract doc
revision.

ESCALATION PATH: if the existing pre-flight isn't structured as
a numbered list of steps (i.e., it's a long single function),
escalate — refactoring the pre-flight is bigger than this bug.
```

### Reviewer prompt

```
[standing instructions]

Review Step E — engine pre-flight bind step.

Checks:

1. Step 5.7 is at the correct position: AFTER 5.5 (acquire
   LLM client) and BEFORE the model call. Trace the function
   line by line.

2. ExecutionContext.bound_tools is populated. Verify via the
   unit test's inspection point.

3. The agent-engine-contract.md doc revision describes the new
   step accurately.

4. The variant's load method is called with the SAME catalog
   instance the runtime bootstrapped (Step B). No new catalog
   instantiation in the engine.

5. The unit test runs against a fake LLM that emits no
   tool_use — the test is about the binding step running, not
   the tool actually being called.

Verdict: PASS or FAIL.
```

---

## Step F — CrewAI adapter passes `tools=[...]` to the model

- **Builds:** the CrewAI framework adapter reads
  `ctx.bound_tools` and passes them to the CrewAI Agent (or
  equivalent) constructor. The resulting `model.request.data`
  has a non-empty `tools` field.
- **Depends on:** Step E in main.
- **Acceptance:** running an exchange with declared tools
  produces a `model.request` event with the tools array.

### Developer prompt

```
[standing instructions]

Step F — Wire bound_tools through the CrewAI adapter so the
model sees them.

Read first:
- Step E's PR (ExecutionContext.bound_tools is your input).
- The CrewAI framework adapter (Step A's writeup named the
  file; if not, grep for "crewai.Agent" or "crewai.LLM" in
  src/engine/ and src/llm/).

Tasks:

1. In the adapter, read ctx.bound_tools when constructing the
   exchange's Agent (or equivalent CrewAI surface).

2. Pass them to CrewAI in whatever shape CrewAI accepts.
   Typically: `crewai.Agent(... tools=[t.callable for t in
   bound_tools], ...)` — or a similar param name; verify
   against the installed CrewAI version.

3. Ensure the tools' SCHEMAS reach the LLM. CrewAI may need a
   different shape than raw callables for the model's tool_use
   protocol (some versions auto-derive; some need explicit
   schemas). The schema field from Step C is the source.

4. Update the model.request event emission to include the
   serialized tools in `data.tools`. This is what the
   regression test in Step G will assert against.

5. Component test in tests/component/engine/test_adapter_tools.py:
   - Run an exchange with declared tools against a fake LLM
     that captures the request payload.
   - Assert the request payload has a non-empty tools array.
   - Assert the model.request event includes the same array
     in data.tools.

6. NO regression test of end-to-end tool USE yet. Step G is
   that. This step only proves tools REACH the model.

Scope: ~50–80 lines code + ~40 lines test.

ESCALATION PATH: if CrewAI's Agent doesn't accept tools= in
the version pinned (Phase 1.1 §3 + Open Item 3 noted CrewAI's
version was a coordinated concern), escalate — coordinator
decides whether to bump CrewAI or use a different integration
point.
```

### Reviewer prompt

```
[standing instructions]

Review Step F — adapter passes tools to model.

Checks:

1. The adapter reads ctx.bound_tools. Trace the call site.

2. CrewAI's Agent (or whatever's being constructed) receives
   the tools in whichever param shape CrewAI expects. Verify
   against the installed CrewAI version's docstring.

3. model.request.data.tools is non-empty when the agent has
   declared tools. The component test asserts this — re-run
   and inspect the captured event.

4. The schema field reaches the model in a usable shape (the
   model needs to know parameter names + types to emit
   tool_use). Inspect one captured request manually; the tools
   array should have JSON schemas, not raw Python callables.

5. The component test runs with a FAKE LLM (no real network).

Verdict: PASS or FAIL.
```

---

## Step G — End-to-end regression test

- **Builds:** the regression test that exercises the full
  chain. Fake LLM emits `tool_use("bash", command="echo
  hello")`; engine routes it; bash runs; model gets the
  result; final agent.message reflects real output.
- **Depends on:** Steps B–F in main.
- **Acceptance:** test passes on post-fix code, FAILS on
  pre-fix code (verifiable by git-stash and re-run).

### Developer prompt

```
[standing instructions]

Step G — Write the end-to-end regression test.

Read first:
- Part 2 acceptance criteria of this bug doc, especially
  criteria 1 and 6.
- The fake LLM fixture (tests/fixtures/fake_llm.py).

Tasks:

1. Extend the fake LLM fixture if needed with an
   `inject_tool_use(tool_name, args)` mode that makes the LLM's
   next response emit a tool_use call.

2. In tests/integration/test_tool_wiring.py (new file):

   def test_tc_tool_wire_01_end_to_end():
       """TC-TOOL-WIRE-01 — agent declares tools, fake LLM
       emits tool_use, engine routes to real tool, model
       receives result, final agent.message reflects real
       output."""

       # Setup: workspace, agent with all four declared tools,
       # fake LLM rigged with inject_tool_use.
       fake = FakeLLM(responses=[
           tool_use("bash", {"command": "echo hello"}),
           final_text("The command output: hello"),
       ])
       # Run the exchange.
       result = run_exchange(agent="kolsch/helper",
                              llm=fake,
                              input="run echo hello")

       # Assert events.
       events = result.events
       assert any(e.type == "tool.requested" for e in events)
       assert any(e.type == "tool.completed" for e in events)
       tc = next(e for e in events if e.type == "tool.completed")
       assert "hello" in tc.data.stdout

       # Assert final agent message contains real output.
       final = result.agent_message.data.content.text
       assert "hello" in final
       # And does NOT contain the hallucination signature.
       assert "bin boot dev etc" not in final

3. Add to process/test-catalog.md as TC-TOOL-WIRE-01.

4. Verify pre-fix FAIL by git-stashing Steps B–F's changes and
   running the test. It must fail in a way that matches the
   bug (no tool.requested event present). Document in PR.

5. Restore and confirm post-fix PASS.

Scope: ~60–100 lines test + small fixture extension + catalog
entry. NO production code changes in this step.

ESCALATION PATH: if the fake LLM fixture doesn't support
multi-response sequencing (one response with tool_use, then
another with text after the tool result), escalate — extending
that is its own work, not this step.
```

### Reviewer prompt

```
[standing instructions]

Review Step G — the regression test.

Checks:

1. The test asserts ALL FOUR of:
   (a) tool.requested event appears.
   (b) tool.completed event with REAL stdout (echo hello →
       "hello\n").
   (c) Final agent.message contains "hello".
   (d) Final agent.message does NOT contain the
       hallucination signature.
   Missing any → fail.

2. Pre-fix verification: the developer ran the test against
   pre-fix code and the test FAILED. Confirmed in their PR
   description with specific output.

3. The test uses NO real network. Fake LLM only.

4. The test is in tests/integration/, not tests/unit/. This
   is an integration-level concern.

5. TC-TOOL-WIRE-01 is in the test catalog with full
   Setup/Action/Expected/Verifies form.

Verdict: PASS or FAIL.
```

---

## Step H — Manual end-to-end verification + findings handoff

- **Builds:** a manual smoke run against a real LLM + the user's
  real workspace, plus a PR description that flags F1, F2, F3
  findings to the coordinator without "fixing" them.
- **Depends on:** Steps B–G in main; TC-TOOL-WIRE-01 passing.
- **Acceptance:** the user's original repro (asking the agent
  to `ls`) now produces a real directory listing matching
  the workspace.

### Developer prompt

```
[standing instructions]

Step H — Manual verification and findings handoff.

NO new code in this step (unless a tiny fix is needed to make
manual verification work, in which case revise Steps B–G or
escalate).

Tasks:

1. Set up a fresh workspace (`kolsch setup`) or use the user's
   `~/Documents/kolsch`. Connect a real tool-capable provider:
   Claude Opus, GPT-4o, or another model with native tool_use.
   (Models without native tool_use will be handled by the
   future Phase 1.3 / model-fallback work; not this bug.)

2. Run:
   uv run kolsch run kolsch/helper \
     --input content="run the bash command ls"

3. Verify:
   - The output reflects the user's REAL workspace contents
     (bundles/, sessions/, content/, etc. — NOT the Linux root
     listing).
   - The JSONL contains tool.requested and tool.completed
     events.
   - Capture the JSONL and paste relevant excerpts into the PR.

4. Document the three findings in the PR description WITHOUT
   fixing them under this PR's scope:

   ### Findings for coordinator follow-up

   **F1 — Phase 2.1 is at Step 24a only.** `kolsch chat
   --help` self-documents this. Steps 24b–24e (slash commands,
   renderer, modal, lifecycle) are not in main. The "Phase 2.1
   complete" framing is inaccurate. The build doc
   (`phase2.1-chat-build.md`) has all five sub-steps
   specified.

   **F2 — Phase 2.0 acceptance gate was insufficient.** This
   bug would have been caught by a real LLM + real tool
   exchange. The Phase 2.0 build doc's "Manual end-to-end test
   before declaring 2.0 done" wasn't run, or was run against a
   path that bypassed the wiring. Recommend a Hard Rule in
   AGENTS.md: any tools-touching phase requires this smoke
   before "done."

   **F3 — Starter agent had tool refs even when wiring was
   broken.** Step D's validation closes this going forward. No
   separate action needed.

5. Do NOT modify AGENTS.md or the Phase 2.0/2.1 docs under
   this PR. F1 and F2 are coordinator decisions, not bug-fix
   scope.

Scope: zero or near-zero code; pure verification + reporting.

ESCALATION PATH: if the manual verification reveals the fix is
incomplete (e.g., the real LLM emits tool_use but the engine
still drops it), escalate — Steps B–G need rework. Don't
declare success on a partial fix.
```

### Reviewer prompt

```
[standing instructions]

Review Step H — manual verification and handoff.

Checks:

1. The PR description includes manual verification evidence:
   actual JSONL excerpts showing tool.requested and
   tool.completed against a real LLM. Screenshots of the
   terminal output also useful. Without evidence, fail.

2. The agent's reply reflects the user's REAL workspace
   contents — verifiable by comparing with `ls
   ~/Documents/kolsch`.

3. F1, F2, F3 are flagged in the PR description and NOT fixed
   under this PR. If the developer "also fixed Phase 2.1
   while they were in there", fail — that's scope creep that
   bypasses the coordinator.

4. AGENTS.md is unchanged. The recommended Hard Rule under F2
   is a recommendation, not an action.

5. Re-run TC-TOOL-WIRE-01 from Step G after this PR. Still
   passes.

6. Run the full test suite. Zero regressions in Phase 1.x /
   2.0 tests.

Verdict: PASS or FAIL. On PASS, the bug is closed.
```

---

## Part 4 — How to Escalate (developer + reviewer)

Either agent escalates by appending an entry to the
"Coordinator Escalation Log" at the bottom of this doc using
the template. After appending, STOP work on the affected item.

```markdown
### Escalation E-BUG-WIRE-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Phase / Step:** Step A / B / ... / H
- **Affected hypothesis or acceptance criterion:** e.g. H2, criterion 4
- **Trigger:** concrete observation with file/line citations.
- **Why this is not a guess:** what you tried; why the spec
  doesn't cover this.
- **Options as you see them:** 2–3 paths with trade-offs.
- **Estimated cost:** rough effort.

---

### Coordinator resolution — E-BUG-WIRE-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale (one sentence):**
- **Resume instruction:**
```

When to escalate:
- The "standard" tools variant doesn't exist (Phase 2.0 was
  even less complete than this ticket assumed).
- The CrewAI adapter needs structural rework, not a kwarg add.
- The catalog has no scanner abstraction.
- The pre-flight isn't structured as discrete steps.
- The fake LLM fixture doesn't support multi-response
  sequencing.
- The fix exceeds ~250 lines / 4 files (compared to Step A's
  estimate).

When NOT to escalate:
- Exact error message wording for `config.unknown_tool`.
- The diagnostic log line format.
- Whether the variant load is async or sync (match surrounding
  code).

---

## Part 5 — Coordinator Escalation Log

### Escalation E-BUG-WIRE-001 — Step A findings + three scoping questions

- **Raised by:** developer
- **Step:** A (investigation)
- **Summary:** investigation found H1, H2, H4 already implemented
  in `main`. Catalog scans bundle tools; "standard" variant has a
  load path (via `discover_tools_in_file`); CrewAI adapter passes
  tools through. The actual gap is narrower:
    - H3 partially: `_register_tools_with_subsystem` exists and is
      called per-exchange pre-flight, but missing refs fail
      silently (returns empty list).
    - No `config.unknown_tool` error code.
    - No regression test that exercises the wiring end-to-end.

  Three questions raised:

  **Q1 — validator-side cross-check (a) vs engine-side only (b)?**
  (a) makes `_register_tools_with_subsystem`'s missing-ref check
      run at agent-load via the validator-factory pattern (mirrors
      `src/platform_runtime/runtime.py:253`'s skills validator).
      Requires async validator OR sync `catalog.has_ref()`.
  (b) Keeps the check in the engine pre-flight; raises
      `ConfigUnknownTool` on first exchange. Simpler.

  **Q2 — per-exchange tool re-registration is acceptable?**
  `_register_tools_with_subsystem` runs every exchange. Python's
  import cache makes it cheap, but the discovery-vs-registration
  separation could be cleaner.

  **Q3 — collapse the 8-step plan?**
  B, C, F are no-ops (already in main). Actual fix is D+E+G
  combined.

---

### Coordinator resolution — E-BUG-WIRE-001

- **Decided by:** human
- **Decisions:**

  **Q1: Ship (b). Track (a) as a follow-up.** The criterion
  "fails at agent-load" has two clauses — the positive
  ("agent-load") and the negative ("NOT silently"). For Phase 2.x
  interactive usage (`kolsch run`, `kolsch chat`), agents load at
  session start, so "agent-load" and "first exchange"
  collapse functionally. (b) satisfies the user-facing intent and
  unblocks the user faster. (a) remains the architecturally
  correct long-term answer for server / API / lint-style usage;
  track it as a follow-up ticket but do NOT do it under this
  bug-fix label.

  **Q2: Ship as-is. Flag the discovery-vs-registration
  architecture as a Phase 2.x cleanup item.** I8 forbids
  *engine* state across exchanges, not *subsystem* state. The
  cleaner architecture (catalog populated once at bootstrap;
  subsystem holds the registry for runtime lifetime; engine reads
  the subsystem) is consistent with I8 and would improve
  per-exchange overhead. Not under this bug-fix label; track for
  Phase 2.x.

  **Q3: Agree. Collapse to two PRs:** combined D + E + G
  (~80 lines, 3 files) + Step H. Steps B, C, F become
  verify-only checks in PR 1's review. The original 8-step
  shape stays in this doc as the record of the investigation's
  starting hypothesis; the table in Part 3 has been annotated
  with the actual execution plan.

- **Rationale (one sentence):** the bug fix is smaller than the
  doc originally anticipated; ship the minimum that closes the
  user-visible failure (b + D + E + G + H), track the
  architectural refinements (a, the discovery-vs-registration
  cleanup) as follow-ups so they don't get lost.
- **Affects:**
  - This bug doc (Part 3 annotated; this escalation logged).
  - Two follow-up items to be tracked separately:
    - **FUL-1** — validator-side `config.unknown_tool` at
      agent-load via the validator-factory pattern (Q1 path (a)).
    - **FUL-2** — discovery-vs-registration architecture
      cleanup so the engine doesn't re-register tools every
      exchange (Q2).
  Both are non-urgent; coordinator schedules when convenient.
- **Resume instruction:** proceed with the combined D + E + G PR
  per the developer prompts in Steps D, E, G below. Then PR 2
  for Step H. PR 1's reviewer also walks the verify-only checks
  for B, C, F (confirm they're in place; no code change). The
  follow-ups FUL-1 and FUL-2 are NOT in scope for either PR.

---

### Escalation E-BUG-WIRE-002 — Step B reviewer check #1 mismatches the actual architecture

- **Raised by:** reviewer (during PR 1 verify-only walk of Step B)
- **Step:** B (verify-only)
- **Trigger:** Step B reviewer check #1 said "Scanner imports
  each .py file once; verify by mocking the importer and asserting
  call counts." But `src/assets/catalog.py:_scan_tools` is
  import-free — it builds `AssetEntry` records from filesystem
  paths only. The inline comment says "the Tools subsystem
  (which DOES import at registration time) expands per-decorator
  at that point." Importing happens one layer down in
  `_register_tools_with_subsystem` via `discover_tools_in_file`,
  per-exchange. The architectural property the check was
  guarding is held by construction:
    - **Am-I10** (asset discovery is read-only of Python) forbids
      the scanner from importing.
    - Python's `sys.modules` cache makes second-and-subsequent
      imports at the discovery layer cache hits.
- **Why this is not a guess:** the reviewer cited the inline
  comment and the architectural invariant by name. The check's
  test scenario can't fail without subverting either Am-I10
  (code organization) or Python's import semantics.
- **Options:**
  (a) Drop the check; the property is held by construction.
  (b) Replace with a static check that guards Am-I10 directly
      (grep the scanner for `import_module`, `importlib`,
      `__import__`, `exec(` — none should appear).
  (c) Keep the check but redirect to `discover_tools_in_file`
      with a mocked Python importer — but the property under
      test is then Python's own cache behavior, not our code.

---

### Coordinator resolution — E-BUG-WIRE-002

- **Decided by:** human
- **Decisions:**
  - **Drop check #1** as written. Replace with **(b)** — a static
    grep-based guard for Am-I10.
  - The updated check (B-1'):
    > *Am-I10 is preserved. grep `src/assets/catalog.py` (and any
    > other scanner module) for `import_module`, `importlib`,
    > `__import__`, `exec(`. None should appear. If any do, the
    > architectural invariant is broken — the scanner has started
    > importing, which means the per-exchange `discover_tools_in_file`
    > layer is being duplicated.*
  - Also acknowledged: the Step B reviewer prompt was written for
    "B is being built fresh." The Step A investigation flipped B
    to verify-only, but the per-step reviewer prompts were NOT
    correspondingly updated. Going forward, PR 1's reviewer
    interprets the verify-only checks as "the architecture in
    main matches what the bug-fix needs." If the architecture
    in main is BETTER than the original spec (as here), update
    the check to match reality. If WORSE, flag as a regression
    risk for coordinator.
- **Rationale (one sentence):** the original check tested a
  property that can't fail without subverting Python; the
  replacement check guards the actual architectural invariant
  (Am-I10) at the right layer.
- **Affects:**
  - Step B reviewer prompt below — check #1 is superseded by
    the B-1' wording above. Don't bother editing the doc inline;
    PR 1's reviewer uses this resolution as the authoritative
    spec for that check.
  - No new follow-ups.
- **Resume instruction:** reviewer applies B-1' instead of the
  original check #1 to PR 1. Other Step B checks (#2 — call
  sites, #3 — log level, #4 — synthetic test, #5 — no engine
  code, #6 — run the test) remain as written; verify them
  against the existing-in-main implementation. If any of THEM
  mismatches reality the same way, escalate a new entry.

---

## Part 6 — Notes on Running These Eight Steps

- **Strict order: A → B → C → {D, E} → F → G → H.** Each step
  is its own PR. D and E can run in parallel after C if two
  developers are available; otherwise serial.
- **Step A is the load-bearing gate.** A confident wrong-shape
  fix will close the symptom (the hallucination) but leave a
  half-implemented system. The dev must trace ALL FOUR
  hypotheses before any code lands.
- **Steps B–F build toward the regression test in G.** None of
  B–F is individually visible end-to-end; they each ship a
  small piece. The dev and reviewer must hold the chain in
  mind: B alone fixes nothing user-visible, but B + C + E + F
  is the wiring restored.
- **Don't burn tokens during the fix.** The fake LLM with
  `inject_tool_use` is the right tool for Step G. Real-LLM
  verification is Step H, run once.
- **F1, F2, F3 are findings for the coordinator, NOT to-do
  items in any of these PRs.** A bug fix that silently fixes
  three other things is a scope-creep PR that bypasses the
  coordinator's prioritization.
- **The error-catalog code for `config.unknown_tool`** lives
  in `docs/explanation/error-model.md` per Phase 1 discipline.
  Adding new codes requires the doc revision in Step D.

---

## Part 7 — Out of Scope (the three findings)

These are real issues that this bug ticket DOES NOT fix; the
developer flags them in Step H's PR description for the
coordinator.

### Finding F1 — Phase 2.1 is at Step 24a only

`kolsch chat --help` self-documents that Phase 2.1
implementation stopped at Step 24a. Steps 24b–24e are not in
main. The "Phase 2.1 complete" framing is inaccurate. The
build doc (`phase2.1-chat-build.md`) has all five sub-steps
specified.

**Coordinator decides:** schedule 24b–24e as separate work, or
defer.

### Finding F2 — Phase 2.0 acceptance gate was insufficient

This bug would have been caught by a real LLM + agent that
declares tools + ask the agent to use one + verify the tool
ran. The Phase 2.0 build doc's "Manual end-to-end test before
declaring 2.0 done" called for this but either wasn't run or
was run against a path that bypassed the wiring.

**Coordinator decides:** add an AGENTS.md Hard Rule requiring
a real-LLM real-tool smoke for tools-touching phases before
"done."

### Finding F3 — Starter agent had tool refs even when wiring was broken

Addressed by Step D's validation — going forward, declared
tools that don't resolve fail at agent-load. No separate
action needed for this specific case; the validation prevents
the class.
