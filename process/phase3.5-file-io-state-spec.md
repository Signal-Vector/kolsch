# Phase 3.5 — File IO + State Mutators Spec

**Intro.** This spec covers Phase 3.5 — the runtime for the 3 file-IO leaf node types (`read_file`, `write_file`, `list_files`) and the 2 state-mutation leaves (`set_state`, `delete_state`). Builds on Phases 3.0 (parser/validator), 3.1 (sequential executor + state dict initialization), 3.2 (agent integration), 3.3 (branching + multi-input binds), and 3.4 (pause/resume).

Related docs:

- `process/phase3.x-roadmap.md` §18.2 / 3.5 — the roadmap entry that scopes this phase.
- `process/phase3.0-workflow-spec.md` §4.2 (file-IO leaves), §4.3 (state leaves), §6.4 (binds/inputs.* namespace, strict-mode validation).
- `process/phase3.1-executor-spec.md` §8.3 (state management — initialization rules and Jinja exposure).
- `process/phase3.4-user-input-build.md` Step 1 — the JSONL replay machinery the state work extends.
- `docs/explanation/workflow-event-log.md` — event log conventions; this phase adds ONE new event kind.

---

## Glossary

| Term | Meaning |
| --- | --- |
| file-IO leaves | The three nodes `read_file`, `write_file`, `list_files`. Each routes through the existing kolsch bundle tool of the same name. |
| state leaves | The two nodes `set_state`, `delete_state`. Each mutates the executor's live `_state` dict and emits a `workflow.state.mutated` event per affected key. |
| `_state` | The executor's mutable workflow-state dict, initialized at run start from workflow inputs per Phase 3.1 §8.3. Exposed to Jinja as `{{ state.* }}`. |
| state mutation event | The new event kind `workflow.state.mutated`, emitted by the state-leaf runners alongside `workflow.node.completed`. Payload: `{key, op: "set" \| "delete", value: Any \| None}`. Source of truth for fork replay (Phase 3.8) and resume reconstruction (Phase 3.4 extension). |
| `PathPolicy` / `check_path_allowed` | The existing file-access subsystem at `src/files/__init__.py`. The three file-IO tools already call it; the workflow runners route through the tools and inherit the check. |
| file_access denial | When a path violates the workspace's allow/deny policy. The tools raise an internal exception; the runner remaps to `WorkflowFileAccessDenied`. |

---

## Background

Phase 3.1 shipped the **state dict scaffolding** but no runtime to mutate it:

- `_state = dict(run.inputs)` initialization at `src/workflows/executor.py:368`.
- `_render_jinja` exposes `state=self._state` to every node's config interpolation (executor.py:658-670; extended in Phase 3.3 with `inputs_overlay=`).
- `BUILTIN_PHASE_MAP` (executor.py:82-102) maps `read_file`/`write_file`/`list_files`/`set_state`/`delete_state` → `"3.5"`, so unregistered slots currently raise `NodeTypeNotImplemented(phase="3.5")`.
- The parser's `*NodeDef` classes (`src/workflows/parser.py:272-310`) ship the canonical YAML shape; the `*Inputs(BaseModel)` models (parser.py:138-156) ship strict-mode-validatable input contracts that Phase 3.3's executor enforces at fire time.
- The error class `WorkflowFileAccessDenied(WorkflowRuntimeError)` exists at `src/workflows/errors.py:1270-1289` with `path` + `node` identifying names — never raised today; Phase 3.5 raises it.

Phase 3.0/3.1 also shipped the kolsch starter-bundle file-IO tools:

- `src/cli/_starter_bundle/kolsch/tools/read_file.py` — `@tool(name="read_file")`; signature `async def read_file(args: ReadFileArgs, ctx: ToolContext) -> dict`; returns `{content, lines_returned, total_lines, next_offset, truncated}`.
- `src/cli/_starter_bundle/kolsch/tools/write_file.py` — `@tool(name="write_file")`; returns `{path, bytes_written, created}`.
- `src/cli/_starter_bundle/kolsch/tools/list_files.py` — `@tool(name="list_files")`; returns `list[{name, path, is_dir, size_bytes}]`.

Each tool calls `check_path_allowed(args.path, policy=ctx.path_policy, root=ctx.workspace_root)` from `src/files/__init__.py` before doing any disk I/O. Phase 3.5 runners dispatch through `dispatch_tool(...)` (same mechanism as Phase 3.1's `BashRunner` at `src/workflows/builtins/bash.py:129-136`) so the existing file-access discipline is inherited "for free."

What's LEFT for Phase 3.5:

1. **Three file-IO runners** — `ReadFileRunner`, `WriteFileRunner`, `ListFilesRunner` in `src/workflows/builtins/`. Each constructs a synthetic `ToolContext`, renders config strings through the executor's `_render_jinja` (with `inputs_overlay=bound_inputs` per Phase 3.3 §6.4), invokes `dispatch_tool`, catches `FileAccessDenied` and remaps to `WorkflowFileAccessDenied`, and reshapes the tool's return into the master spec's canonical output shape per master spec §4.2.
2. **Two state runners** — `SetStateRunner`, `DeleteStateRunner`. Mutate `self._state` (the executor's live dict, passed to runners as the `state=` kwarg per `NodeRunner` protocol at `src/workflows/executor.py:101-119`), emit one `workflow.state.mutated` event per affected key, return `{}`.
3. **`workflow.state.mutated` event kind** — NEW. The SINGLE event-kind addition this phase. Payload `{key: str, op: "set" | "delete", value: Any | None}` with the standard envelope fields (`workflow_run_id`, `trace_id`, `parent_event_id`, `ts`). Emitted by state runners; consumed by Phase 3.4's replay (extension required — see §10) and Phase 3.8's fork replay (future).
4. **Phase 3.4 replay extension** — `src/workflows/_replay.py:replay_into(...)` must walk `workflow.state.mutated` events and apply them to `executor._state`. Without this, a resumed run loses every state mutation made before the pause (since `workflow.node.completed` for state nodes carries `{}` output, the existing replay restores nothing).
5. **Registration** — `register_builtins(...)` accepts three new `ToolDefinition` kwargs (`read_file_defn` / `write_file_defn` / `list_files_defn`) and registers all five new runners.
6. **Starter workflow + fixtures** — a `bundles/kolsch/workflows/state-evolves.yaml` demonstrating multiple `set_state` writes to the same key (the canonical "evolving variable" pattern per master spec §4.3.1) + file-IO demo + golden trace fixtures.

The verification gate has five parts: file-IO success (read+write+list); file_access denial surfacing as `WorkflowFileAccessDenied`; state mutations visible via `{{ state.* }}` in downstream nodes; the canonical multi-write evolution pattern (three `set_state` writes to the same key produce no warning, correctly evolve); resume reconstructs `_state` via the new replay extension (a paused workflow that mutated state pre-pause resumes with state preserved).

**This spec breaks the phase into 4 sequential steps**, each with its own Coder and Reviewer cycle. The build plan lives at `process/phase3.5-file-io-state-build.md`.

---

## Scope

**In scope (delivered across the 4 steps):**

- **`ReadFileRunner`** at `src/workflows/builtins/read_file.py`. Renders `node_def.path` via `ctx.render_jinja(..., inputs_overlay=bound_inputs)` (Phase 3.4 hook). Constructs `ToolContext` with workflow principal + caller_chain + path_policy from the workspace's `PathPolicy`. Invokes `dispatch_tool(defn=self._read_file_defn, args=ReadFileArgs(path=rendered_path), context=tool_ctx, ...)`. Catches `FileAccessDenied` → raises `WorkflowFileAccessDenied(path=rendered_path, node=node_name)`. Returns `{content: str}` per master spec §4.2.1 (the tool returns more fields like `lines_returned` / `truncated`; the runner narrows to the spec's canonical shape and discards the rest — pagination of large files is a future QoL).
- **`WriteFileRunner`** at `src/workflows/builtins/write_file.py`. Renders `node_def.path` AND `node_def.content` (both Jinja-eligible). Constructs args, dispatches, remaps `FileAccessDenied`. Returns `{bytes_written: int}` per spec §4.2.2.
- **`ListFilesRunner`** at `src/workflows/builtins/list_files.py`. Renders `node_def.path` (and optional `node_def.pattern`). Dispatches, remaps. The tool returns a list of file-metadata dicts; the runner narrows to `{entries: list[str]}` per spec §4.2.3 — names only, non-recursive. Optional `pattern:` does substring/glob filtering at the runner level (the underlying tool may or may not honor it; the runner ensures it does).
- **`SetStateRunner`** at `src/workflows/builtins/set_state.py`. Iterates `node_def.keys: dict[str, Any]`. For each (key, value):
  - If the value is a string starting with `"{{"` (Jinja-marked per Round 6 J5/β), render via `ctx.render_jinja(value, inputs_overlay=bound_inputs)`.
  - Otherwise (YAML literal — int, float, list, dict, bool, None, plain string), pass through verbatim.
  - Mutate `state[key] = rendered_value` (the runner receives `state=self._state` from the executor; the mutation is live).
  - Emit `workflow.state.mutated` event with payload `{key, op: "set", value: rendered_value}`.
  Returns `{}`.
- **`DeleteStateRunner`** at `src/workflows/builtins/delete_state.py`. Iterates `node_def.keys: list[str]`. For each key:
  - `state.pop(key, None)` (silent on absent — matches Python `dict.pop` semantics; absent-key deletion is not an error).
  - Emit `workflow.state.mutated` event with payload `{key, op: "delete", value: None}`.
  Returns `{}`.
- **`workflow.state.mutated` event kind** — added to `src/workflows/events.py`'s `WorkflowEventKind` enum + a `WorkflowStateMutatedEvent` Pydantic model + `WorkflowStateMutatedPayload`. Emitted via the existing `emit_event` helper. Ordering: emitted BEFORE the `workflow.node.completed` event for the state-mutating node, so replay applies mutations BEFORE marking the node completed (atomicity matters for fork integrity in Phase 3.8).
- **`_replay.py` extension** — `replay_into(executor, events)` handles `workflow.state.mutated`:
  ```python
  elif kind == "workflow.state.mutated":
      payload = event["payload"]
      if payload["op"] == "set":
          executor._state[payload["key"]] = payload["value"]
      else:  # "delete"
          executor._state.pop(payload["key"], None)
  ```
  Applied in-order; the last event wins for any key with multiple writes (the canonical "evolving variable" pattern per master spec §4.3.1 — multiple writes to the same key are explicit, replay is order-preserving).
- **`register_builtins(...)` extension** — three new `ToolDefinition` kwargs (`read_file_defn`, `write_file_defn`, `list_files_defn`) and five new `registry.register(...)` calls. CLI bootstrap (`src/cli/workflows.py`'s `_cmd_run` / `_cmd_resume` paths) loads the three new tool defs from the workspace's kolsch bundle and threads them in.
- **`_inputs_models.py` extension** — `_BUILTIN_TO_INPUTS` mapping gains entries for the five new types. Phase 3.3's strict-mode bind validation then enforces `extra="forbid"` on the four file-IO + delete_state types (binds are NOT the way to feed them data; config strings reference `{{ inputs.* }}` from edge binds, but the binds dict itself has no model fields), and `extra="allow"` on `SetStateInputs` (author-named binds accepted, used to pull edge-bound values into `set_state.keys`).
- **State mutation observable by Phase 3.3's `inputs.*` overlay** — confirmation that `_render_jinja(..., inputs_overlay=bound_inputs)` continues to see `state=self._state` (a live reference). When a downstream node references `{{ state.attempts }}`, the latest mutation is visible. No new wiring; the live-dict design already supports this.
- **Golden trace fixtures** at `tests/golden/runs/` for: file-IO success (read → write → list); file_access denial; state evolution (three sequential `set_state` writes to the same key); delete_state.
- **Starter workflow** `bundles/kolsch/workflows/state-evolves.yaml` and `bundles/kolsch/workflows/file-roundtrip.yaml` running end-to-end.

**Out of scope (deferred to later sub-phases per `phase3.x-roadmap.md`):**

- **Sub-workflow state inheritance** (Phase 3.7). Master spec §4.6.1 leaves this open. Phase 3.5's `_state` is LOCAL to the workflow run; a sub-workflow run (when Phase 3.7 ships) starts with its own fresh `_state` initialized from its inputs, NOT from the parent's state. Phase 3.5 must NOT bake the opposite assumption — no parent-state-copy hook in `set_state` / `delete_state`.
- **Per-key state typing** (deferred indefinitely per master spec §2). `state.*` is `Any` at v1; Phase 3.5 does not validate value shapes against any schema.
- **`map` / `workflow` runtime** (Phase 3.7).
- **`branch` / `filter` / `compute` runtime** (Phase 3.6).
- **`user_input` runtime** — already shipped in Phase 3.4.
- **Fork replay** (Phase 3.8). Phase 3.5 ships the EVENT KIND that Phase 3.8 will read; the fork-replay logic itself is Phase 3.8 work.
- **File pagination + streaming** — `read_file` returns the entire content as a single string. The underlying tool supports pagination (`lines_returned`, `next_offset`); the workflow node does not expose it at v1. Authors with large files use the tool from an agent context until a workflow-side pagination QoL ships.
- **`list_files` recursive walk** — non-recursive per master spec §4.2.3. Recursive walks are a future QoL (`recursive: bool` field on the node).
- **State key validation** — no parse-time rule that state keys must be valid Python identifiers. Authors using non-identifier keys (e.g., `key-with-dash`) access via bracket notation: `{{ state['key-with-dash'] }}`. Documented as author's responsibility.

**Decisions explicitly locked (do NOT relitigate):**

- **Five runners route through existing implementations.** No reimplementation of file IO; the three file-IO runners dispatch to the kolsch starter-bundle tools. Behavior parity is structural.
- **`state.*` typed as `Any` at v1.** No per-key validation.
- **Multiple `set_state` writes to the same key is canonical**, NOT a warning. Per master spec §4.3.1 lines 314-338.
- **State mutation events are atomic per key.** A `set_state` with `keys: {attempts: 0, status: "pending"}` emits TWO `workflow.state.mutated` events, one per key. Replay applies them in order; partial-mutation states are visible across the boundary (acceptable — the worklist's per-node atomicity is at the node-completion level, not the per-key-mutation level).
- **`workflow.state.mutated` is emitted BEFORE `workflow.node.completed`** for state-mutating nodes. This ordering matters for fork replay: an interrupted fork that replays events up to `workflow.node.completed` sees the state mutation. An interrupted fork that stops at `workflow.node.started` does NOT see it — the per-node atomicity boundary is intentionally at completion.
- **File-IO outputs are NARROWED to the master spec's canonical shape.** The tools return richer dicts (pagination, file metadata); the runners discard fields the spec doesn't promise. Authors wanting the richer shape must invoke the tool from an agent context.
- **`delete_state` is silent on absent keys.** No error; the operation is idempotent. Matches Python `dict.pop(key, None)` semantics.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2, ruamel.yaml, Jinja2 (`JINJA_ENV` at `src/workflows/jinja_env.py`), pytest. Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/files/__init__.py:18-25` — `PathPolicy` + `check_path_allowed(path, policy, root)` + `is_path_allowed(...)`. Existing file-access subsystem.
- `src/cli/_starter_bundle/kolsch/tools/read_file.py:78-114` — existing `@tool` implementation.
- `src/cli/_starter_bundle/kolsch/tools/write_file.py:56-86` — existing.
- `src/cli/_starter_bundle/kolsch/tools/list_files.py:62-87` — existing.
- `src/tools/dispatch.py` — `dispatch_tool(defn=, args=, context=, authz=, secrets=, sandbox=)`. The unified tool-invocation pipeline. Same path Phase 3.1's `BashRunner` uses.
- `src/workflows/builtins/bash.py:76-165` — `BashRunner`. Phase 3.5's file-IO runners mirror this shape: render config → build `ToolContext` → `dispatch_tool` → narrow output.
- `src/workflows/executor.py:101-119` — `NodeRunner` protocol. `state: dict[str, Any]` is the mutable state dict passed to every runner. Set_state / delete_state mutate it in place; other runners read but don't mutate.
- `src/workflows/executor.py:368` — `self._state = dict(self._run.inputs)`. Initialization rule.
- `src/workflows/executor.py:658-670` (extended in Phase 3.3) — `_render_jinja(expression, *, inputs_overlay=None)`. Scope: `nodes`/`state`/`inputs`. State is exposed as a LIVE reference, so post-mutation reads see fresh values.
- `src/workflows/parser.py:138-156` — `*Inputs(BaseModel)` for the five new types. Already exist; Phase 3.5 wires them into `_BUILTIN_TO_INPUTS`.
- `src/workflows/parser.py:272-310` — `*NodeDef` for the five new types. Already exist; Phase 3.5 consumes them.
- `src/workflows/errors.py:1270-1289` — `WorkflowFileAccessDenied`. Already exists; Phase 3.5 raises it.
- `src/workflows/events.py:64-88` — `WorkflowEventKind` enum. Phase 3.5 adds `STATE_MUTATED = "workflow.state.mutated"`.
- `src/workflows/_replay.py` — Phase 3.4 replay helper. Phase 3.5 extends it.
- `src/workflows/builtins/__init__.py:65-78` — `register_builtins(...)` signature. Phase 3.5 extends.
- `src/workflows/_inputs_models.py` — Phase 3.3's strict-mode validation helper. Phase 3.5 extends `_BUILTIN_TO_INPUTS`.

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch
uv run kolsch --root /tmp/kolsch-state-test setup --non-interactive
uv run pytest tests/component/ -q
uv run pytest tests/golden/ -q
```

---

## §4.2 File-IO runtime (elaboration)

### §4.2.0 Shared runner shape

All three file-IO runners follow the same skeleton:

```python
class FileIORunner:
    def __init__(self, *, tool_defn: ToolDefinition, authz, secrets, sandbox) -> None: ...

    async def run(
        self,
        *,
        node_name: str,
        node_def: ReadFileNodeDef | WriteFileNodeDef | ListFilesNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        # 1. Render config strings via ctx.render_jinja with the Phase 3.3 overlay.
        rendered_path = ctx.render_jinja(node_def.path, inputs_overlay=bound_inputs)
        # ... per-type extra fields (content for write_file, pattern for list_files) ...

        # 2. Construct ToolContext mirroring BashRunner's pattern (executor.py:117-126 style).
        tool_ctx = ToolContext(
            principal=f"workflow.{ctx.workflow_run_id}",
            caller_chain=ctx.caller_chain,
            workspace_root=ctx.workspace_root,
            path_policy=<from workspace config>,
            agent_id=None,
            session_id=None,
            thread_id=None,
        )

        # 3. Dispatch through the existing tool pipeline.
        try:
            result = await dispatch_tool(
                defn=self._tool_defn,
                args=<typed args object>,
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

        # 4. Narrow to the spec's canonical output shape.
        return _narrow(result)
```

The `FileAccessDenied` catch is the SINGLE custom remap; every other exception bubbles to the executor's existing failure path and becomes `WorkflowNodeFailed`.

### §4.2.1 `ReadFileRunner` specifics

- Args: `ReadFileArgs(path=rendered_path)`. (The underlying tool may support `offset` / `limit`; Phase 3.5 does NOT thread these — fixed at full-content read.)
- Narrowing: `{content: result["content"]}`. Drop `lines_returned`, `total_lines`, `next_offset`, `truncated` — not in the master-spec output contract.

### §4.2.2 `WriteFileRunner` specifics

- Renders BOTH `node_def.path` AND `node_def.content` (both Jinja-eligible per spec §4.2.2 line 296).
- Args: `WriteFileArgs(path=rendered_path, content=rendered_content)`.
- Narrowing: `{bytes_written: result["bytes_written"]}`. Drop `path` and `created`.
- Content type: `str | bytes` per `WriteFileNodeDef.content: str | bytes` (parser.py). When `bytes`, Jinja rendering is a no-op (strings only); pass through verbatim. (Real workflows authoring binary writes are rare; the parser permits both for forward-compat.)

### §4.2.3 `ListFilesRunner` specifics

- Renders `node_def.path` AND optional `node_def.pattern`.
- Args: `ListFilesArgs(path=rendered_path, pattern=rendered_pattern)`.
- Narrowing: the tool returns a list of metadata dicts; Phase 3.5 narrows to `{entries: list[str]}` where each string is the file name (NOT the full path — names are non-confusing in the typical usage `{{ nodes.list.entries }}`).
- Pattern filtering: if the underlying tool honors `pattern` argument, the runner trusts it. If not, the runner applies `fnmatch.filter(names, pattern)` after the tool returns.
- Non-recursive (immediate children only) — master spec §4.2.3 line 303.

---

## §4.3 State-mutation runtime (elaboration)

### §4.3.1 `SetStateRunner` specifics

Algorithm per master spec §4.3.1 (lines 309-338):

```python
async def run(self, *, node_name, node_def: SetStateNodeDef, bound_inputs, state, ctx) -> dict[str, Any]:
    for key, value in node_def.keys.items():
        if isinstance(value, str) and value.lstrip().startswith("{{"):
            rendered = ctx.render_jinja(value, inputs_overlay=bound_inputs)
        else:
            rendered = value  # YAML literal — pass through
        state[key] = rendered
        await ctx.emit(
            WorkflowStateMutatedEvent(
                workflow_run_id=ctx.workflow_run_id,
                trace_id=ctx.trace_id,
                parent_event_id=ctx.parent_event_id,
                payload=WorkflowStateMutatedPayload(
                    key=key,
                    op="set",
                    value=rendered,
                ),
            )
        )
    return {}
```

The `ctx.emit(...)` is a NEW method on `NodeContext` (added in Step 0) that calls back to the executor's event-emission infrastructure. The executor's existing `emit_event(store, event)` helper backs it; the callback is bound at `_build_ctx` time.

**Iteration order:** Python 3.7+ `dict` preserves insertion order. The YAML source's declaration order determines mutation order. Replay applies events in the JSONL's order, which is the same order — deterministic.

**Multiple keys, partial failure:** if `ctx.render_jinja` raises mid-iteration (e.g., a `WorkflowJinjaRefUnknown` against a missing `nodes.*` ref), some keys have been mutated; the exception propagates and the run aborts per Phase 3.1's abort-on-failure rule. The already-emitted `workflow.state.mutated` events are PERSISTENT — they're in the JSONL — so a downstream forensics/replay sees the partial mutations correctly. The `workflow.node.failed` event marks the node as having failed; consumers of the event log understand the partial-mutation state is real, not phantom.

### §4.3.2 `DeleteStateRunner` specifics

```python
async def run(self, *, node_name, node_def: DeleteStateNodeDef, bound_inputs, state, ctx) -> dict[str, Any]:
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

Silent-on-absent is intentional: deleting a key that doesn't exist is a no-op semantically. The event is still emitted — replay applies a no-op `pop` consistently.

---

## §10 State management (revised)

Phase 3.1 §8.3 said:

- State dict initialized to a copy of workflow inputs at start.
- `set_state` / `delete_state` nodes mutate the dict.
- All other nodes see the current state via Jinja `{{ state.* }}`.

Phase 3.5 extends this with the LIVE-DICT + EVENT-LOG dual representation:

### §10.1 Live dict (transient, in-process)

`self._state` is the in-process source of truth during a run. Every node's Jinja rendering sees the latest value. Mutations are O(1) — direct dict ops.

### §10.2 Event log (persistent, on-disk)

Every mutation emits a `workflow.state.mutated` event to the JSONL. The event sequence is the source of truth for:

- **Resume** (Phase 3.4 — extended here): on resume, replay walks `workflow.state.mutated` events to reconstruct `_state` exactly.
- **Fork** (Phase 3.8 — future): fork-from-event replays events up to the fork point to materialize the captured state at that moment.
- **Forensics**: the JSONL alone is sufficient to reconstruct the run's complete state history. No external store, no per-key version tracking.

### §10.3 Cross-references

When a downstream node references `{{ state.foo }}` AFTER a `set_state` upstream, Jinja sees the live `_state["foo"]`. The reference does NOT walk the event log at runtime — that's overhead the live dict avoids. Replay machinery is the ONLY consumer of the event log (and only during resume / fork).

### §10.4 Initialization rule (unchanged)

`self._state = dict(self._run.inputs)` at run start. The initial state is a SHALLOW COPY of the run's inputs dict — mutations don't leak into the inputs. This is the existing Phase 3.1 behavior; Phase 3.5 preserves it.

---

## §11 Event log extension

### §11.1 New event kind

`workflow.state.mutated` — added to `WorkflowEventKind` enum at `src/workflows/events.py`.

Payload:

```python
class WorkflowStateMutatedPayload(BaseModel):
    key: str
    op: Literal["set", "delete"]
    value: Any | None  # None for "delete"; the new value for "set"
```

Common envelope fields per master spec §11.3: `kind`, `workflow_run_id`, `trace_id`, `parent_event_id`, `ts`.

### §11.2 Ordering rules (extension)

For state-mutating nodes (`set_state` / `delete_state`):

1. `workflow.node.started` — first.
2. One or more `workflow.state.mutated` events — one per key in the node's `keys` field; in YAML declaration order.
3. `workflow.node.completed` — last for this node; output payload is `{}`.

For non-state-mutating nodes — unchanged from Phase 3.3.

### §11.3 Parent-event-id chain

Each `workflow.state.mutated` event's `parent_event_id` is the node's `workflow.node.started` event id (NOT the previous `workflow.state.mutated` event — keeping each mutation directly chained to the node's start simplifies replay and matches the existing `workflow.node.logged` pattern from Phase 3.1).

The `workflow.node.completed` event's `parent_event_id` chains to the LAST `workflow.state.mutated` event for this node (or to `workflow.node.started` if the node had zero mutations — applies when `set_state.keys: {}` is empty).

---

## §12 Error classes (no additions beyond what exists)

Phase 3.5 reuses two existing error classes:

- `WorkflowFileAccessDenied` (`src/workflows/errors.py:1270-1289`) — raised by the three file-IO runners on `FileAccessDenied` from the underlying tool. Fields: `path` (the rendered, normalized path), `node` (node name).
- `WorkflowNodeFailed` (`src/workflows/errors.py:1185`) — the existing catch-all for any unhandled runner exception, including state-mutation failures (Jinja errors mid-iteration, etc.). No change.

No new error classes ship in Phase 3.5.

---

## Phase-level Acceptance Criteria

- `read_file` workflow node reads a file within the workspace and exposes `content` via `{{ nodes.<n>.content }}` downstream.
- `write_file` workflow node writes content to a file within the workspace; `bytes_written` reflects the actual write size.
- `list_files` returns immediate child names (non-recursive) as `entries: list[str]`; optional `pattern:` filters correctly.
- A file-IO node attempting a denied path raises `WorkflowFileAccessDenied(path, node)`; the run aborts via the Phase 3.1 failure path.
- `set_state` with `keys: {attempts: 0, status: "pending"}` mutates both keys; downstream `{{ state.attempts }}` and `{{ state.status }}` see the new values.
- Multiple sequential `set_state` writes to the same key (`attempts: 0` → `attempts: "{{ state.attempts + 1 }}"`) correctly evolve the value; no warnings; no errors.
- `set_state.keys` with mixed YAML literals (`status: pending`) and Jinja expressions (`attempts: "{{ state.attempts + 1 }}"`) handles both correctly.
- `delete_state` removes named keys from `_state`; absent keys are silent; downstream `{{ state.<deleted> }}` resolves to Jinja `Undefined` (existing behavior).
- Every state mutation emits a `workflow.state.mutated` event between the node's `started` and `completed` events; payload carries `{key, op, value}` correctly.
- Resume reconstructs `_state` correctly via the extended replay: a workflow that mutates `state.attempts` to 3 pre-pause and is resumed sees `state.attempts == 3` in the next node's rendering.
- All five new runners are registered; `RunnerRegistry.get("kolsch/nodes/<each>")` returns the runner, NOT `NodeTypeNotImplemented`.
- The starter `bundles/kolsch/workflows/state-evolves.yaml` and `bundles/kolsch/workflows/file-roundtrip.yaml` run end-to-end via `kolsch workflows run`.
- No regressions: full suite still green; non-state-mutating workflows produce byte-identical JSONLs (modulo the new event-kind class definition).

---

## Phase-level Integration Tests

### Phase Test 1: File-IO roundtrip
- **Run:** `uv run kolsch --root /tmp/kolsch-state-test workflows run kolsch/workflows/file-roundtrip --input filename=hello.txt --input body=greetings`
- **Expect:** exit 0; the file is created on disk; downstream node reads it back; JSONL shows the canonical `bytes_written` / `content` / `entries` payloads.
- **Fail means:** runner wiring broken OR canonical output narrowing wrong.

### Phase Test 2: File-access denial
- **Run:** `uv run pytest tests/component/test_workflow_file_io.py::test_path_outside_workspace_denied -v`
- **Expect:** workflow with `read_file.path: "/etc/passwd"` raises `WorkflowFileAccessDenied`; workflow JSONL ends with `workflow.node.failed`.
- **Fail means:** file_access enforcement not surfaced OR remap class wrong.

### Phase Test 3: State evolves across writes
- **Run:** `uv run pytest tests/component/test_workflow_state.py::test_state_evolves_across_writes -v`
- **Expect:** three sequential `set_state` writes to `state.attempts` produce three `workflow.state.mutated` events with values 0, 1, 2; downstream node sees final value 2.
- **Fail means:** YAML literal vs Jinja distinction broken OR multi-write evolution drops mutations.

### Phase Test 4: Resume reconstructs state
- **Run:** `uv run pytest tests/component/test_workflow_state.py::test_resume_reconstructs_state_via_replay -v`
- **Expect:** a `prep → set_state(attempts=5) → ask → echo` workflow where `echo` references `{{ state.attempts }}`. Run pauses at `ask`; resume; `echo` renders `5`. The replay's state restoration is verified.
- **Fail means:** `_replay.py` extension missing OR ordering wrong.

### Phase Test 5: Delete state idempotent
- **Run:** `uv run pytest tests/component/test_workflow_state.py::test_delete_state_silent_on_absent -v`
- **Expect:** `delete_state.keys: [foo]` on a state without `foo` succeeds silently; `workflow.state.mutated` event with `op: "delete"` still emitted.
- **Fail means:** delete throws on absent key OR event not emitted.

### Phase Test 6: No regressions
- **Run:** `uv run pytest tests/ -q`
- **Expect:** full suite green; non-state JSONLs byte-identical (modulo timestamps + IDs).

---
