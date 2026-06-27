# [SPEC-006] Phase 3.5 — File-IO + state mutators

**Type:** spec
**Doc #:** 006
**Status:** Draft
**Created:** 2026-06-27

---

## Glossary

| Term | Meaning |
| --- | --- |
| file-IO leaves | The 3 nodes `read_file`, `write_file`, `list_files`. Each dispatches to the same-named kolsch starter-bundle tool via `dispatch_tool(...)`. |
| state leaves | The 2 nodes `set_state`, `delete_state`. Mutate the executor's live `_state` dict; emit `workflow.state.mutated` events. |
| state mutation event | NEW event kind `workflow.state.mutated` with payload `{key, op: "set"\|"delete", value: Any\|None}`. The source of truth for resume + fork replay. |
| `PathPolicy` / `check_path_allowed` | The file-access subsystem at `src/files/__init__.py`. The three file-IO tools already call it; runners inherit the check transitively via `dispatch_tool`. |

---

## Background

Phase 3.1 shipped the state-dict scaffolding (`self._state = dict(self._run.inputs)` at `src/workflows/executor.py:368`) and the Jinja `state=` scope at `_render_jinja` (executor.py:658-670, extended Phase 3.3 with `inputs_overlay=`). Phase 3.0 shipped the typed `*NodeDef` and `*Inputs(BaseModel)` for all five Phase 3.5 types (parser.py:138-156 + 272-310). The `BUILTIN_PHASE_MAP` already maps `read_file`/`write_file`/`list_files`/`set_state`/`delete_state` to `"3.5"` (executor.py:82-102). The kolsch starter bundle ships the three file-IO tool implementations:

- `src/cli/_starter_bundle/kolsch/tools/read_file.py:78-114` — `@tool(name="read_file")`.
- `src/cli/_starter_bundle/kolsch/tools/write_file.py:56-86` — `@tool(name="write_file")`.
- `src/cli/_starter_bundle/kolsch/tools/list_files.py:62-87` — `@tool(name="list_files")`.

Each tool already calls `check_path_allowed(...)` from `src/files/__init__.py:18-25` so Phase 3.5 inherits the file-access discipline by dispatching through the same `dispatch_tool(...)` pipeline Phase 3.1's `BashRunner` uses (bash.py:129-136).

What 3.1 LEFT for 3.5:

1. **Five runners** at `src/workflows/builtins/{read_file,write_file,list_files,set_state,delete_state}.py`. File-IO runners follow `BashRunner`'s shape (render config → ToolContext → dispatch_tool → narrow output → remap `FileAccessDenied`). State runners mutate `self._state` (the executor passes it as the `state=` runner kwarg per `NodeRunner` protocol at executor.py:101-119) and emit `workflow.state.mutated` events.
2. **`workflow.state.mutated` event kind** in `src/workflows/events.py`. The SINGLE new event kind this phase ships.
3. **`NodeContext.emit(event)` callback** so runners can emit events without depending on the executor. Step 0 adds it; reuses the existing `emit_event(store, event)` helper.
4. **Phase 3.4 replay extension** — `src/workflows/_replay.py:replay_into(...)` must handle `workflow.state.mutated` events. Without this, resumed runs lose every state mutation made before the pause.
5. **`register_builtins(...)` extension** — three new `ToolDefinition` kwargs + five new `registry.register(...)` calls. CLI bootstrap loads the tool defs from the workspace's kolsch bundle.
6. **`_inputs_models.py:_BUILTIN_TO_INPUTS` extension** — five new entries so Phase 3.3's strict-mode bind validation enforces the correct policy per type.
7. **Starter workflows + goldens** — `state-evolves.yaml` + `file-roundtrip.yaml`.

The verification gate is five fixtures: file-IO roundtrip; file_access denial; three-write state evolution (the canonical pattern per master spec §4.3.1); resume after a state mutation; idempotent `delete_state` on an absent key.

**This spec breaks the phase into 4 sequential steps (Step 0 — Step 3).**

Related docs:

- `process/phase3.5-file-io-state-spec.md` — the master design spec (§4.2 / §4.3 elaboration, §10 state management revised, §11 event log extension).
- `process/phase3.4-user-input-build.md` Step 1 — the replay machinery this phase extends.
- `process/phase3.3-modifiers-branching-build.md` Step 0 — the `_inputs_models.py` + `ctx.render_jinja(..., inputs_overlay=)` hook.
- `process/phase3.0-workflow-spec.md` §4.2 + §4.3 — locked YAML schemas.

---

## Scope

**In scope (delivered across the 4 steps):**

- New runners at `src/workflows/builtins/read_file.py`, `.../write_file.py`, `.../list_files.py`, `.../set_state.py`, `.../delete_state.py`.
- `workflow.state.mutated` event kind + `WorkflowStateMutatedEvent` + `WorkflowStateMutatedPayload` in `src/workflows/events.py`.
- `NodeContext.emit(event)` callback bound at `_build_ctx` time; backs `set_state` / `delete_state` event emission.
- `_replay.py` extension — applies `workflow.state.mutated` events to `executor._state` during resume reconstruction.
- `register_builtins(...)` accepts `read_file_defn` / `write_file_defn` / `list_files_defn` kwargs; registers all five runners.
- CLI bootstrap (`_cmd_run` + `_cmd_resume`) loads the three tool defs from the workspace's kolsch bundle and threads them into `register_builtins(...)`.
- `_inputs_models.py:_BUILTIN_TO_INPUTS` gains 5 entries.
- Output narrowing per master spec §4.2: `read_file` → `{content}`; `write_file` → `{bytes_written}`; `list_files` → `{entries: list[str]}`.
- `FileAccessDenied` → `WorkflowFileAccessDenied` remap in each file-IO runner.
- Goldens at `tests/golden/runs/` for file-IO roundtrip, file_access denial, state evolution, resume-after-mutation, delete-state idempotency.
- Starter workflows `bundles/kolsch/workflows/state-evolves.yaml` and `bundles/kolsch/workflows/file-roundtrip.yaml`.

**Out of scope (deferred per `phase3.x-roadmap.md`):**

- Sub-workflow state inheritance (Phase 3.7) — `_state` is local; child workflows get a fresh dict from their inputs.
- Per-key state typing — `state.*` is `Any` at v1.
- Fork replay (Phase 3.8) — Phase 3.5 ships the EVENT KIND fork-replay will consume; fork itself is 3.8.
- `branch` node / `filter` / `compute` runtime (Phase 3.6).
- `user_input` (already shipped Phase 3.4).
- `agent` (already shipped Phase 3.2).
- `map` / `workflow` (Phase 3.7).
- File pagination (`offset` / `limit` on `read_file`) — future QoL.
- Recursive `list_files` — future QoL.
- State key validation (non-identifier keys allowed; authors use bracket notation in Jinja).

**Decisions already locked (do NOT relitigate):**

- File-IO runners route through existing tool implementations; no reimplementation.
- `state.*` is `Any`.
- Multiple `set_state` writes to the same key is CANONICAL, not a warning.
- State mutations emit ONE event per key (not one per `set_state` invocation).
- `workflow.state.mutated` emitted BEFORE the node's `workflow.node.completed`.
- `delete_state` is silent on absent keys.
- Output narrowing per spec — drop tool fields the spec doesn't promise.

**Decisions made during Step 0:**

- **`NodeContext.emit_workflow_event` (NOT `emit`)** — the spec example
  shows `await ctx.emit(WorkflowStateMutatedEvent(...))`, but
  `NodeContext.emit(subkind: str, payload: Mapping)` is an existing
  author-facing API (wraps the wire kind as `node.<bundle>.<subkind>`,
  used by authored Python `@node`s). The runner-facing hook is therefore
  exposed as **`emit_workflow_event(event)`** instead of overloading the
  existing surface. Same semantics as the spec example; distinct method
  name. Steps 1+ use `await ctx.emit_workflow_event(WorkflowStateMutatedEvent(...))`.
- **`_last_event_id` advances on each `emit_workflow_event` call** so
  the next CROSS-node event (next `workflow.node.started`) chains to
  the latest in-invocation emission. The per-invocation chain
  semantics for `workflow.node.completed` (master spec §11.3 — chain
  to last `state.mutated`) ship alongside the state runners in Step 1;
  Step 0 only ships the plumbing.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2, ruamel.yaml, Jinja2, pytest. Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/files/__init__.py:18-25` — `PathPolicy` + `check_path_allowed`. Existing.
- `src/cli/_starter_bundle/kolsch/tools/{read,write,list}_file.py` — existing tool implementations.
- `src/tools/dispatch.py` — `dispatch_tool(defn=, args=, context=, authz=, secrets=, sandbox=)`.
- `src/workflows/builtins/bash.py:76-165` — `BashRunner` pattern; file-IO runners mirror.
- `src/workflows/executor.py:101-119` — `NodeRunner` protocol; `state: dict[str, Any]` is the mutable state dict.
- `src/workflows/executor.py:368` — `self._state` initialization.
- `src/workflows/executor.py:658-670` — `_render_jinja(expression, *, inputs_overlay=)`.
- `src/workflows/parser.py:138-156` — `*Inputs` for all 5 new types (already exist).
- `src/workflows/parser.py:272-310` — `*NodeDef` for all 5 new types (already exist).
- `src/workflows/errors.py:1270-1289` — `WorkflowFileAccessDenied`.
- `src/workflows/events.py:64-88` — `WorkflowEventKind` enum.
- `src/workflows/_replay.py` — Phase 3.4 replay helper.
- `src/workflows/builtins/__init__.py:65-78` — `register_builtins(...)` signature.
- `src/workflows/_inputs_models.py` — Phase 3.3's strict-mode validation helper.
- `src/workflows/context.py:42-100` — `NodeContext` (Phase 3.4 extension added `render_jinja`; Phase 3.5 adds `emit`).

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch
uv run kolsch --root /tmp/kolsch-state-test setup --non-interactive
uv run pytest tests/component/ -q
uv run pytest tests/golden/ -q
```

---

## Phase-level Acceptance Criteria

- `read_file` exposes `{content}` via `{{ nodes.<n>.content }}` downstream.
- `write_file` writes content; `bytes_written` reflects the actual write size.
- `list_files` returns non-recursive immediate children as `entries: list[str]`; `pattern:` filters.
- File-IO node attempting denied path raises `WorkflowFileAccessDenied(path, node)`; run aborts.
- `set_state.keys` with YAML literals and Jinja expressions handles both correctly.
- Multiple sequential `set_state` writes to the same key correctly evolve the value; no warnings.
- `delete_state` removes named keys; absent keys silent; downstream `{{ state.<deleted> }}` is Jinja `Undefined`.
- Every state mutation emits `workflow.state.mutated` BETWEEN the node's `started` and `completed` events; payload `{key, op, value}` correct.
- Resume reconstructs `_state` via the extended replay.
- All five runners registered; `RunnerRegistry.get("kolsch/nodes/<n>")` returns the runner.
- Starter workflows run end-to-end.
- No regressions; non-state JSONLs byte-identical (modulo new event-kind class).

---

## Phase-level Integration Tests

### Phase Test 1: File-IO roundtrip
- **Run:** `uv run kolsch --root /tmp/kolsch-state-test workflows run kolsch/workflows/file-roundtrip --input filename=hello.txt --input body=greetings`
- **Expect:** exit 0; file created; downstream reads it back; JSONL carries `bytes_written` / `content` / `entries`.

### Phase Test 2: File-access denial
- **Run:** `uv run pytest tests/component/test_workflow_file_io.py::test_path_outside_workspace_denied -v`
- **Expect:** workflow with `read_file.path: "/etc/passwd"` raises `WorkflowFileAccessDenied`; JSONL ends with `workflow.node.failed`.

### Phase Test 3: State evolves
- **Run:** `uv run pytest tests/component/test_workflow_state.py::test_state_evolves_across_writes -v`
- **Expect:** three sequential `set_state` writes to `state.attempts` produce three `workflow.state.mutated` events with values 0, 1, 2; downstream sees 2.

### Phase Test 4: Resume reconstructs state
- **Run:** `uv run pytest tests/component/test_workflow_state.py::test_resume_reconstructs_state_via_replay -v`
- **Expect:** `prep → set_state(attempts=5) → ask → echo` workflow pauses at `ask`; resume; `echo` renders `5`.

### Phase Test 5: Delete state idempotent
- **Run:** `uv run pytest tests/component/test_workflow_state.py::test_delete_state_silent_on_absent -v`
- **Expect:** `delete_state.keys: [foo]` on state without `foo` succeeds silently; `workflow.state.mutated` event with `op: "delete"` still emitted.

### Phase Test 6: No regressions
- **Run:** `uv run pytest tests/ -q`
- **Expect:** full suite green.

---

## Coordinator Prompt

> You are the strategic decision-maker. You do not write or review code.
> Drive the 4-step Coder/Reviewer flow.

**Your task:**

Ship Phase 3.5 of the kolsch workflow system through 4 sequential cycles. This phase wires 5 leaf node types: 3 file-IO + 2 state-mutation. The pattern is mature (file-IO mirrors `BashRunner`; state runners are tiny mutators), so the surface is small. The non-obvious work is the new `workflow.state.mutated` event kind and the replay extension that makes pause/resume preserve state.

**Authoritative references:**

- THIS spec: `process/phase3.5-file-io-state-build.md`.
- Master design spec: `process/phase3.5-file-io-state-spec.md` — §4.2 / §4.3 / §10 / §11.
- Phase 3.4 build plan: `process/phase3.4-user-input-build.md` — for the `_replay.py` machinery extended here.
- Phase 3.3 build plan: `process/phase3.3-modifiers-branching-build.md` — for `_inputs_models.py` + `ctx.render_jinja(..., inputs_overlay=)`.
- Phase 3.1 build plan: `process/phase3.1-executor-build.md` — for the `BashRunner` pattern file-IO mirrors.
- Phase 3.0 master spec: `process/phase3.0-workflow-spec.md` §4.2 / §4.3 — locked decisions.

**The 4 steps:**

| Step | Focus | Effort |
| --- | --- | --- |
| **Step 0** | **PREREQ** — `workflow.state.mutated` event kind + `WorkflowStateMutatedEvent` + `NodeContext.emit(...)` callback + `_replay.py` extension for state events. Pure plumbing; no runners yet. | ~1 day |
| **Step 1** | State runners — `SetStateRunner` + `DeleteStateRunner`. Render values (YAML literal vs Jinja), mutate `state`, emit per-key events, return `{}`. Register both in `register_builtins(...)`. | ~1 day |
| **Step 2** | File-IO runners — `ReadFileRunner` + `WriteFileRunner` + `ListFilesRunner`. Mirror `BashRunner`'s shape. Render config strings, build `ToolContext`, dispatch, narrow output, remap `FileAccessDenied`. Extend `register_builtins(...)` with the three tool-defn kwargs + CLI bootstrap loads them. | ~2 days |
| **Step 3** | Golden trace fixtures (5) + starter workflows (`state-evolves.yaml` + `file-roundtrip.yaml`) + Phase Tests 1-5 wired. | ~1.5 days |

**Flow per step:** standard Coder/Reviewer cycle (see prior phase build plans).

**When to make a strategic call:**

- Coder asks to relitigate a master-spec-locked decision (per-key typing, recursive list, output narrowing, sub-workflow state inheritance) → REJECT.
- Coder finds ambiguity → consult master spec, make a binding call, append to THIS spec, re-brief.
- Coder discovers Phase 3.0 / 3.1 / 3.3 / 3.4 contract was wrong → flag as separate patch; do NOT widen 3.5's scope.
- Scope creep (`branch` / `compute` runtime, Phase 3.7 nodes, agent layer changes) → REJECT.

**Done when:** All 4 steps Approved AND all 6 Phase-level Integration Tests pass AND `phase3.x-roadmap.md` Phase 3.5 line annotated with the shipping SHA.

---

# STEP 0 — Event kind + NodeContext.emit + replay extension (PREREQ)

**Goal**: ship the `workflow.state.mutated` event kind + the `NodeContext.emit(...)` callback + the `_replay.py` extension. NO runners yet — Steps 1+2 own those.

**Files created/modified:**

- `src/workflows/events.py` (modified) — `WorkflowEventKind.STATE_MUTATED` + `WorkflowStateMutatedEvent` + `WorkflowStateMutatedPayload`.
- `src/workflows/context.py` (modified) — add `emit: Callable[[WorkflowEvent], Awaitable[Any]] | None = None` field; add `async def emit(self, event)` method.
- `src/workflows/executor.py` (modified) — `_build_ctx` binds the `emit` callback to the executor's event-emission infrastructure.
- `src/workflows/_replay.py` (modified) — handle `workflow.state.mutated` events.
- `tests/component/test_workflow_state_event_kind.py` (new) — event class shape + replay coverage.

**Effort**: ~1 day.

## Step 0 — Coder Prompt

> Reply "Step 0 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the event-kind plumbing + the `ctx.emit(...)` hook + the replay extension. NO runners.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.5-file-io-state-build.md` — Step 0.
2. `process/phase3.5-file-io-state-spec.md` — §11 (event log extension), §10.2 (event log as source of truth for replay).
3. `src/workflows/events.py` — existing event-kind enum + Pydantic event-class pattern.
4. `src/workflows/_replay.py` — Phase 3.4's replay helper.
5. `src/workflows/context.py` — existing `NodeContext` shape; Phase 3.4 added `render_jinja`; Phase 3.5 adds `emit`.

**What to implement:**

1. **`WorkflowEventKind.STATE_MUTATED`** in `src/workflows/events.py`:

   ```python
   class WorkflowEventKind(StrEnum):
       ...  # existing values unchanged
       STATE_MUTATED = "workflow.state.mutated"
   ```

2. **`WorkflowStateMutatedPayload` + `WorkflowStateMutatedEvent`**:

   ```python
   class WorkflowStateMutatedPayload(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")
       key: str
       op: Literal["set", "delete"]
       value: Any | None  # the new value for "set"; None for "delete"

   class WorkflowStateMutatedEvent(WorkflowEvent):
       kind: Literal[WorkflowEventKind.STATE_MUTATED] = WorkflowEventKind.STATE_MUTATED
       payload: WorkflowStateMutatedPayload
   ```

   Mirror the shape of `WorkflowNodeStartedEvent` / etc. — same envelope fields (`workflow_run_id`, `trace_id`, `parent_event_id`, `ts`), just a different `kind` + payload.

3. **`NodeContext.emit`** in `src/workflows/context.py`:

   ```python
   @dataclass(slots=True)
   class NodeContext:
       ...
       _emit_callback: Callable[[WorkflowEvent], Awaitable[str]] | None = None

       async def emit(self, event: WorkflowEvent) -> str:
           """Emit a workflow event from a runner.

           Returns the event id. Phase 3.5 uses this for
           workflow.state.mutated; other runners may use it for
           workflow.node.logged (future).
           """
           if self._emit_callback is None:
               raise RuntimeError("NodeContext has no emit callback bound")
           return await self._emit_callback(event)
   ```

4. **Bind in `_build_ctx`** at `src/workflows/executor.py`:

   ```python
   def _build_ctx(self, *, inv_id: InvocationId, node_def: Any, parent_event_id: str) -> NodeContext:
       async def _emit(event: WorkflowEvent) -> str:
           return emit_event(self._store, event)
       return NodeContext(
           ...,
           _emit_callback=_emit,
           _render_jinja_callback=...,  # Phase 3.4 already binds this
       )
   ```

5. **`_replay.py` extension** — handle the new event kind:

   ```python
   def replay_into(executor: IterativeExecutor, events: Iterable[dict[str, Any]]) -> dict[str, str | None]:
       ...
       for event in events:
           kind = event.get("kind")
           ...
           elif kind == "workflow.state.mutated":
               payload = event["payload"]
               if payload["op"] == "set":
                   executor._state[payload["key"]] = payload["value"]
               else:
                   executor._state.pop(payload["key"], None)
           ...
   ```

   Order matters — events are walked in JSONL order. The last `set` for any key wins; intervening `delete` correctly removes it. This is the canonical "evolving variable" replay.

6. **Tests in `tests/component/test_workflow_state_event_kind.py`**:
   - **Event class shape:** construct `WorkflowStateMutatedEvent(...)` with valid payload; assert serialization round-trip; assert `frozen=True` rejects mutation.
   - **`NodeContext.emit` callback:** construct a `NodeContext` with a stub `_emit_callback` that records calls; invoke `await ctx.emit(<event>)`; assert the callback received the event.
   - **`NodeContext.emit` raises without callback:** `NodeContext(...)` with `_emit_callback=None`; `await ctx.emit(...)` raises `RuntimeError`.
   - **Replay applies set:** stub an `IterativeExecutor` with `_state = {"x": 1}`; feed `[{kind: "workflow.state.mutated", payload: {key: "x", op: "set", value: 2}}]`; assert `_state["x"] == 2`.
   - **Replay applies delete:** stub `_state = {"x": 1}`; feed `[{kind: "workflow.state.mutated", payload: {key: "x", op: "delete", value: null}}]`; assert `"x" not in _state`.
   - **Replay silent on delete-of-absent:** stub `_state = {}`; feed delete event; no exception; `_state` still empty.
   - **Replay applies in-order:** feed a sequence `[set x=1, set x=2, delete x, set x=3]`; assert `_state["x"] == 3`.

**Step 0 Acceptance Criteria:**

0.1. `WorkflowEventKind.STATE_MUTATED = "workflow.state.mutated"` added.
0.2. `WorkflowStateMutatedPayload` + `WorkflowStateMutatedEvent` Pydantic models ship; frozen; shape per spec §11.
0.3. `NodeContext.emit(event)` method ships; raises clearly when callback unbound.
0.4. `_build_ctx` binds the emit callback to the executor's `emit_event(store, event)`.
0.5. `_replay.py` handles `workflow.state.mutated` events: `set` writes, `delete` pops (silent on absent), in-order.
0.6. NO runners shipped yet (Steps 1+2 own them).
0.7. No regressions: `uv run pytest tests/ -q` green.

**Self-check before handoff:**

- [ ] All 7 ACs met
- [ ] `grep WorkflowStateMutatedEvent src/workflows/builtins/` returns ZERO matches (Steps 1+2 own emission)
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 0 Complete — [summary]".

## Step 0 — Reviewer Prompt

> Reply "Step 0 Approved" or "Changes requested — [list]".

Verify each AC. PAY EXTRA ATTENTION to:

- AC 0.5 — replay order. Multiple writes to one key MUST land at the last set; the test fixture `[set x=1, set x=2, delete x, set x=3]` is the canonical correctness check.
- AC 0.3 — failure mode when callback is unbound. The runtime check protects against future regressions where a runner is invoked outside the executor (tests, dry-run paths).
- AC 0.6 — Step 1/2 ownership. Grep `src/workflows/builtins/` for `WorkflowStateMutatedEvent` — ZERO matches.

---

# STEP 1 — State runners: SetStateRunner + DeleteStateRunner

**Goal**: ship the two state runners. Register them. Tests cover the YAML-literal vs Jinja distinction, multi-key mutations, multi-write evolution, and the `delete-silent-on-absent` semantics.

**Files created/modified:**

- `src/workflows/builtins/set_state.py` (new) — `SetStateRunner`.
- `src/workflows/builtins/delete_state.py` (new) — `DeleteStateRunner`.
- `src/workflows/builtins/__init__.py` (modified) — register both.
- `src/workflows/_inputs_models.py` (modified) — `_BUILTIN_TO_INPUTS` gains `set_state` + `delete_state` entries.
- `tests/component/test_workflow_set_state.py` (new) — state-set coverage.
- `tests/component/test_workflow_delete_state.py` (new) — delete coverage.

**Effort**: ~1 day.

## Step 1 — Coder Prompt

> Reply "Step 1 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship `SetStateRunner` + `DeleteStateRunner`. Both are tiny — most of the work is correctness around the YAML-literal vs Jinja distinction in `set_state` and event emission ordering in both.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.5-file-io-state-build.md` — Step 1.
2. `process/phase3.5-file-io-state-spec.md` — §4.3.1 (`SetStateRunner` algorithm), §4.3.2 (`DeleteStateRunner`), §11.2 (event ordering).
3. `process/phase3.0-workflow-spec.md` §4.3 — canonical YAML schema + the "evolving variable" pattern at lines 314-338.
4. `src/workflows/parser.py:295-310` — `SetStateNodeDef` + `DeleteStateNodeDef`.

**What to implement:**

1. **`src/workflows/builtins/set_state.py`**:

   ```python
   class SetStateRunner:
       """Runtime for kolsch/nodes/set_state — Phase 3.5 §4.3.1."""

       async def run(
           self,
           *,
           node_name: str,
           node_def: SetStateNodeDef,
           bound_inputs: dict[str, Any],
           state: dict[str, Any],
           ctx: NodeContext,
       ) -> dict[str, Any]:
           for key, raw_value in node_def.keys.items():
               if isinstance(raw_value, str) and raw_value.lstrip().startswith("{{"):
                   rendered = ctx.render_jinja(raw_value, inputs_overlay=bound_inputs)
               else:
                   rendered = raw_value  # YAML literal — pass through verbatim
               state[key] = rendered
               await ctx.emit(
                   WorkflowStateMutatedEvent(
                       workflow_run_id=ctx.workflow_run_id,
                       trace_id=ctx.trace_id,
                       parent_event_id=ctx.parent_event_id,  # the node.started event
                       payload=WorkflowStateMutatedPayload(
                           key=key,
                           op="set",
                           value=rendered,
                       ),
                   )
               )
           return {}
   ```

   The Jinja-detection check `raw_value.lstrip().startswith("{{")` is from Phase 3.3's `_is_jinja_marked` helper — REUSE it (`from workflows.executor import _is_jinja_marked`) rather than reimplementing.

2. **`src/workflows/builtins/delete_state.py`**:

   ```python
   class DeleteStateRunner:
       """Runtime for kolsch/nodes/delete_state — Phase 3.5 §4.3.2."""

       async def run(
           self,
           *,
           node_name: str,
           node_def: DeleteStateNodeDef,
           bound_inputs: dict[str, Any],
           state: dict[str, Any],
           ctx: NodeContext,
       ) -> dict[str, Any]:
           for key in node_def.keys:
               state.pop(key, None)  # silent on absent
               await ctx.emit(
                   WorkflowStateMutatedEvent(
                       workflow_run_id=ctx.workflow_run_id,
                       trace_id=ctx.trace_id,
                       parent_event_id=ctx.parent_event_id,
                       payload=WorkflowStateMutatedPayload(
                           key=key,
                           op="delete",
                           value=None,
                       ),
                   )
               )
           return {}
   ```

3. **`register_builtins(...)` extension** in `src/workflows/builtins/__init__.py`:

   ```python
   registry.register("kolsch/nodes/set_state", SetStateRunner())
   registry.register("kolsch/nodes/delete_state", DeleteStateRunner())
   ```

   No new constructor args; both runners are dependency-free.

4. **`_inputs_models.py:_BUILTIN_TO_INPUTS`** — add:

   ```python
   "kolsch/nodes/set_state": SetStateInputs,
   "kolsch/nodes/delete_state": DeleteStateInputs,
   ```

5. **Tests in `tests/component/test_workflow_set_state.py`**:
   - **Single set:** `set_state.keys: {x: 5}`; assert `_state["x"] == 5` post-run; assert one `workflow.state.mutated` event with `{key: "x", op: "set", value: 5}`.
   - **Multiple keys:** `set_state.keys: {x: 1, y: 2}`; assert both keys set; assert TWO events emitted in YAML declaration order.
   - **YAML literal pass-through:** `set_state.keys: {flag: true, list: [1, 2], obj: {a: 1}}`; assert each is passed verbatim (no string interpretation).
   - **Jinja expression evaluated:** `set_state.keys: {x: "{{ 1 + 1 }}"}`; assert `_state["x"] == 2` (NativeEnvironment may return int 2 OR string "2" depending on the jinja_env wiring — check Phase 3.3's Jinja env decision).
   - **Multi-write evolution:** workflow with `init (set_state x=0) → bump1 (set_state x="{{ state.x + 1 }}") → bump2 (set_state x="{{ state.x + 1 }}")`; assert final `_state["x"] == 2`; assert three `workflow.state.mutated` events for key `x` with values 0, 1, 2.
   - **Mixed literal + Jinja in one node:** `set_state.keys: {status: "pending", attempts: "{{ state.attempts | default(0) + 1 }}"}`; both handled correctly.
   - **Empty `keys`:** `set_state.keys: {}`; no events; `_state` unchanged; node still completes with `{}` output.

6. **Tests in `tests/component/test_workflow_delete_state.py`**:
   - **Delete existing:** `state = {"x": 1, "y": 2}`; `delete_state.keys: [x]`; assert `_state == {"y": 2}`; assert one event.
   - **Delete absent silent:** `state = {}`; `delete_state.keys: [x]`; no exception; one event still emitted (`{key: "x", op: "delete", value: None}`).
   - **Delete multiple:** `delete_state.keys: [x, y, z]`; three events; only existing keys removed; absent keys generate events.
   - **Downstream sees absence:** workflow with `init → delete (delete_state x) → reader (bash with command: "echo {{ state.x | default('gone') }}")`; assert `reader`'s output is `"gone"`.

**Step 1 Acceptance Criteria:**

1.1. `SetStateRunner` mutates `_state` per key; emits one `workflow.state.mutated` per key in declaration order.
1.2. YAML literal values pass through verbatim; Jinja-marked strings render via `ctx.render_jinja(..., inputs_overlay=bound_inputs)`.
1.3. `DeleteStateRunner` pops keys silently; emits one event per key (including for absent keys).
1.4. Both runners return `{}`.
1.5. `register_builtins(...)` registers both.
1.6. `_BUILTIN_TO_INPUTS` gains `set_state` (extra="allow") + `delete_state` (extra="forbid") entries.
1.7. Multi-write evolution pattern works (three writes to same key → three events → final value latest).
1.8. No regressions.

**Self-check before handoff:**

- [ ] All 8 ACs met
- [ ] Multi-write fixture in tests; assert exact event sequence
- [ ] `grep dispatch_tool src/workflows/builtins/set_state.py` returns ZERO matches (state runners don't dispatch tools)
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 1 Complete — [summary]".

## Step 1 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 1.1 — declaration-order emission. Python dict preservation makes this work, but read the test fixture: a fixture with `keys: {z: 1, a: 2}` should emit `z` BEFORE `a` (declaration order), NOT `a` before `z` (alphabetical).
- AC 1.2 — YAML literal pass-through. Read the runner source: an `int` literal `5` is NOT a string; the Jinja check (`isinstance(raw_value, str)`) MUST be there to avoid trying to render non-strings.
- AC 1.3 — silent delete-of-absent. Verify by running the test; an exception here would break the canonical idempotency.
- AC 1.7 — multi-write evolution. Inspect the JSONL of the test fixture; expect three `workflow.state.mutated` events for key `attempts` with values 0, 1, 2.

---

# STEP 2 — File-IO runners: ReadFileRunner + WriteFileRunner + ListFilesRunner

**Goal**: ship the three file-IO runners. Each mirrors `BashRunner`'s shape — render config → ToolContext → `dispatch_tool` → narrow output → remap `FileAccessDenied`. Extend `register_builtins(...)` to accept the three tool defs.

**Files created/modified:**

- `src/workflows/builtins/read_file.py` (new) — `ReadFileRunner`.
- `src/workflows/builtins/write_file.py` (new) — `WriteFileRunner`.
- `src/workflows/builtins/list_files.py` (new) — `ListFilesRunner`.
- `src/workflows/builtins/__init__.py` (modified) — `register_builtins(...)` accepts `read_file_defn` / `write_file_defn` / `list_files_defn` kwargs; registers all three.
- `src/cli/workflows.py` (modified) — `_cmd_run` and `_cmd_resume` load the three tool defs from the workspace's kolsch bundle and pass to `register_builtins(...)`.
- `src/workflows/_inputs_models.py` (modified) — `_BUILTIN_TO_INPUTS` gains `read_file` + `write_file` + `list_files` entries.
- `tests/component/test_workflow_file_io.py` (new) — coverage including file-access denial.

**Effort**: ~2 days.

## Step 2 — Coder Prompt

> Reply "Step 2 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the three file-IO runners. Each mirrors `BashRunner`'s shape verbatim, customized for the per-type config + args shape.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.5-file-io-state-build.md` — Step 2.
2. `process/phase3.5-file-io-state-spec.md` — §4.2 (full).
3. `src/workflows/builtins/bash.py:76-165` — the runner template.
4. `src/cli/_starter_bundle/kolsch/tools/{read,write,list}_file.py` — the underlying tools' arg shapes + return shapes (for narrowing logic).
5. `src/tools/dispatch.py` — `dispatch_tool(...)` signature.
6. `src/files/__init__.py:18-25` — `FileAccessDenied` exception; raised by the tools when path policy fails.

**What to implement:**

1. **`src/workflows/builtins/read_file.py`**:

   ```python
   class ReadFileRunner:
       """Runtime for kolsch/nodes/read_file — Phase 3.5 §4.2.1.

       Dispatches to the kolsch/read_file tool via dispatch_tool(...);
       inherits the tool's check_path_allowed enforcement.
       """

       def __init__(
           self,
           *,
           tool_defn: ToolDefinition,
           authz: AuthorizationProvider,
           secrets: SecretsProvider,
           sandbox: SandboxProvider,
       ) -> None:
           self._tool_defn = tool_defn
           self._authz = authz
           self._secrets = secrets
           self._sandbox = sandbox

       async def run(
           self,
           *,
           node_name: str,
           node_def: ReadFileNodeDef,
           bound_inputs: dict[str, Any],
           state: dict[str, Any],
           ctx: NodeContext,
       ) -> dict[str, Any]:
           rendered_path = ctx.render_jinja(node_def.path, inputs_overlay=bound_inputs)
           tool_ctx = self._build_tool_ctx(ctx, rendered_path)
           try:
               result = await dispatch_tool(
                   defn=self._tool_defn,
                   args={"path": rendered_path},
                   context=tool_ctx,
                   authz=self._authz,
                   secrets=self._secrets,
                   sandbox=self._sandbox,
               )
           except FileAccessDenied as exc:
               raise WorkflowFileAccessDenied(
                   path=rendered_path,
                   node=node_name,
               ) from exc
           # Narrow to {content} per spec §4.2.1.
           return {"content": result["content"]}

       def _build_tool_ctx(self, ctx: NodeContext, path: str) -> ToolContext:
           ...  # mirror BashRunner's _build_tool_ctx pattern
   ```

2. **`src/workflows/builtins/write_file.py`** — renders BOTH `path` AND `content`:

   ```python
   rendered_path = ctx.render_jinja(node_def.path, inputs_overlay=bound_inputs)
   if isinstance(node_def.content, bytes):
       rendered_content = node_def.content  # pass through; bytes don't Jinja
   else:
       rendered_content = ctx.render_jinja(node_def.content, inputs_overlay=bound_inputs)
   ...
   return {"bytes_written": result["bytes_written"]}
   ```

3. **`src/workflows/builtins/list_files.py`** — renders `path` and optional `pattern`; narrows result to `{entries: list[str]}`:

   ```python
   rendered_path = ctx.render_jinja(node_def.path, inputs_overlay=bound_inputs)
   rendered_pattern = (
       ctx.render_jinja(node_def.pattern, inputs_overlay=bound_inputs)
       if node_def.pattern is not None else None
   )
   try:
       result = await dispatch_tool(
           defn=self._tool_defn,
           args={"path": rendered_path, "pattern": rendered_pattern},
           context=tool_ctx,
           authz=..., secrets=..., sandbox=...,
       )
   except FileAccessDenied as exc:
       raise WorkflowFileAccessDenied(path=rendered_path, node=node_name) from exc
   # The tool returns a list of {name, path, is_dir, size_bytes} dicts.
   entries = [item["name"] for item in result]
   # Apply pattern filter if the tool didn't already (fnmatch).
   if rendered_pattern is not None:
       entries = fnmatch.filter(entries, rendered_pattern)
   return {"entries": entries}
   ```

   Note: the underlying tool MAY honor `pattern` itself — if so, the `fnmatch.filter` is a no-op (idempotent). If not, the runner-side filter is the safety net. Either way, the spec contract is satisfied.

4. **`register_builtins(...)` extension** in `src/workflows/builtins/__init__.py`:

   ```python
   def register_builtins(
       registry: RunnerRegistry,
       *,
       bash_defn: ToolDefinition,
       read_file_defn: ToolDefinition,    # NEW
       write_file_defn: ToolDefinition,   # NEW
       list_files_defn: ToolDefinition,   # NEW
       authz: AuthorizationProvider,
       secrets: SecretsProvider,
       sandbox: SandboxProvider,
       catalog: AssetCatalog | None = None,
       engine: AgentEngine | None = None,
       session_store: SessionStore | None = None,
       config_builder: ... | None = None,
   ) -> None:
       ...  # existing
       registry.register("kolsch/nodes/read_file", ReadFileRunner(
           tool_defn=read_file_defn, authz=authz, secrets=secrets, sandbox=sandbox,
       ))
       registry.register("kolsch/nodes/write_file", WriteFileRunner(
           tool_defn=write_file_defn, authz=authz, secrets=secrets, sandbox=sandbox,
       ))
       registry.register("kolsch/nodes/list_files", ListFilesRunner(
           tool_defn=list_files_defn, authz=authz, secrets=secrets, sandbox=sandbox,
       ))
       # Step 1 registrations stay
       registry.register("kolsch/nodes/set_state", SetStateRunner())
       registry.register("kolsch/nodes/delete_state", DeleteStateRunner())
   ```

5. **CLI bootstrap** in `src/cli/workflows.py` — `_cmd_run` and `_cmd_resume` already load `bash_defn` from the workspace's kolsch bundle. Find that path and add three more loads:

   ```python
   read_file_defn = _load_tool_defn(workspace_root, "kolsch", "read_file")
   write_file_defn = _load_tool_defn(workspace_root, "kolsch", "write_file")
   list_files_defn = _load_tool_defn(workspace_root, "kolsch", "list_files")
   ```

   Pass them into `register_builtins(...)`.

6. **`_BUILTIN_TO_INPUTS`** — add three entries (all `extra="forbid"`).

7. **Tests in `tests/component/test_workflow_file_io.py`**:
   - **Read-write roundtrip:** write a file with known content; read it back; assert `content` matches.
   - **Path Jinja rendering:** `read_file.path: "{{ inputs.filename }}"`; assert correct path used.
   - **Content Jinja rendering:** `write_file.content: "{{ inputs.greeting }}!"`; assert correct content written.
   - **List_files entries:** create a few files in a tmp dir; `list_files.path: "{{ inputs.dir }}"`; assert `entries` is the list of names.
   - **List_files pattern:** with `pattern: "*.txt"`; assert only `.txt` files returned.
   - **File access denied:** workflow with `read_file.path: "/etc/passwd"` (outside workspace); assert `WorkflowFileAccessDenied` raised; workflow JSONL ends with `workflow.node.failed`; the failed event's payload mentions the denied path.
   - **Bytes written reflects size:** `write_file.content: "abc"`; assert `bytes_written == 3`.

**Step 2 Acceptance Criteria:**

2.1. Three file-IO runners ship; each routes through `dispatch_tool` against the kolsch starter-bundle tool of the same name.
2.2. Output narrowing per spec — `{content}` / `{bytes_written}` / `{entries: list[str]}` only; other tool fields dropped.
2.3. `FileAccessDenied` from `dispatch_tool` is caught and remapped to `WorkflowFileAccessDenied(path, node)` with `from exc` preserving `__cause__`.
2.4. `register_builtins(...)` accepts the three new `*_defn` kwargs; registers all three runners.
2.5. CLI bootstrap loads the three tool defs; `_cmd_run` and `_cmd_resume` thread them in.
2.6. `_BUILTIN_TO_INPUTS` gains three entries (all `extra="forbid"`).
2.7. `list_files` pattern filter works (either via the underlying tool OR runner-side fnmatch).
2.8. Path + content config strings rendered via `ctx.render_jinja(..., inputs_overlay=bound_inputs)`.
2.9. No regressions.

**Self-check before handoff:**

- [ ] All 9 ACs met
- [ ] Manual smoke: `uv run kolsch ... workflows run kolsch/workflows/file-roundtrip ...` works (Step 3 ships the starter)
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 2 Complete — [summary]".

## Step 2 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 2.2 — output narrowing. Read each runner's return statement; the dict MUST have ONLY the spec-promised keys. Extra fields leaking through is a contract violation downstream consumers will rely on.
- AC 2.3 — exception remap. Verify the `from exc` is present so `__cause__` flows. Test the failure path to confirm `WorkflowFileAccessDenied.cause` is the original.
- AC 2.5 — CLI bootstrap. Run `kolsch workflows run` on a workflow USING file-IO nodes; confirm the registration succeeds (no `NodeTypeNotImplemented` raise).

---

# STEP 3 — Golden trace fixtures + starter demos + integration tests

**Goal**: ship reference JSONLs + the two starter workflows + Phase Tests 1-5.

**Files created/modified:**

- `bundles/kolsch/workflows/file-roundtrip.yaml` (new) — write → read → list demo.
- `bundles/kolsch/workflows/state-evolves.yaml` (new) — three-write `set_state` evolution demo.
- `tests/golden/runs/` (new files):
  - `file-roundtrip.workflow.jsonl`.
  - `file-access-denied.workflow.jsonl`.
  - `state-evolves.workflow.jsonl`.
  - `state-resume.workflow.jsonl`.
  - `delete-state-idempotent.workflow.jsonl`.
- `tests/golden/runs_test.py` (modified) — add 5 fixtures.
- `tests/component/test_workflow_state_e2e.py` (new) — Phase Tests 3 + 4 + 5.

**Effort**: ~1.5 days.

## Step 3 — Coder Prompt

> Reply "Step 3 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the goldens + starters + Phase Tests.

**What to implement:**

1. **`bundles/kolsch/workflows/file-roundtrip.yaml`** — write + read + list:

   ```yaml
   inputs:
     filename: { type: str }
     body: { type: str }
   nodes:
     write:
       type: kolsch/nodes/write_file
       path: "{{ inputs.filename }}"
       content: "{{ inputs.body }}"
     read:
       type: kolsch/nodes/read_file
       path: "{{ inputs.filename }}"
     list:
       type: kolsch/nodes/list_files
       path: "."
   graph:
     start: write
     edges:
       - { from: write, to: read }
       - { from: read, to: list }
   ```

2. **`bundles/kolsch/workflows/state-evolves.yaml`** — three-write evolution (master spec §4.3.1 lines 314-338 pattern):

   ```yaml
   nodes:
     init:
       type: kolsch/nodes/set_state
       keys: { attempts: 0, status: pending }
     bump1:
       type: kolsch/nodes/set_state
       keys: { attempts: "{{ state.attempts + 1 }}" }
     bump2:
       type: kolsch/nodes/set_state
       keys: { attempts: "{{ state.attempts + 1 }}" }
     report:
       type: kolsch/nodes/bash
       command: "echo 'attempts={{ state.attempts }} status={{ state.status }}'"
   graph:
     start: init
     edges:
       - { from: init, to: bump1 }
       - { from: bump1, to: bump2 }
       - { from: bump2, to: report }
   ```

3. **Goldens** — run each scenario; canonicalize volatile fields; commit.

4. **`runs_test.py` extension** — five new parametrize cases.

5. **`test_workflow_state_e2e.py`** — Phase Tests 3 + 4 + 5 wired (file-IO tests live in `test_workflow_file_io.py` from Step 2).

**Step 3 Acceptance Criteria:**

3.1. Two starter workflows ship; both parse, validate, run end-to-end.
3.2. Five golden JSONLs at `tests/golden/runs/`.
3.3. `runs_test.py` asserts semantic equality.
3.4. `test_workflow_state_e2e.py` covers Phase Tests 3, 4, 5.
3.5. Manual smoke: `state-evolves` produces `attempts=2 status=pending`.
3.6. Manual smoke: `file-roundtrip --input filename=hello.txt --input body=greetings` writes and reads correctly.
3.7. No regressions.

**Handoff trigger:** All ACs met → "Step 3 Complete — [summary]".

## Step 3 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 3.5 — RUN IT. The three-write evolution exercises the canonical master-spec pattern; if `attempts` ends at anything OTHER than 2 (e.g., 1 because Jinja evaluated against pre-mutation state), the live-dict-mutation discipline is broken.
- AC 3.6 — manual smoke. Run on a fresh workspace; confirm the file lands at the workspace-relative path AND `read_file` finds it.

---

## Done when

- All 4 steps Approved by Reviewer.
- All 6 Phase-level Integration Tests pass.
- `process/phase3.x-roadmap.md` Phase 3.5 line annotated with the shipping SHA.
- Manual smoke test:

  ```bash
  uv run kolsch --root /tmp/kolsch-fresh setup --non-interactive
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/file-roundtrip --input filename=hello.txt --input body=greetings
  cat /tmp/kolsch-fresh/hello.txt
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/state-evolves
  cat /tmp/kolsch-fresh/runs/wrk_*/events.jsonl | grep workflow.state.mutated  # expect 3 events for "attempts"
  ```
