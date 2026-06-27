# Phase 3.7 — Composition (map + workflow) Spec

**Intro.** This spec covers Phase 3.7 — the runtime for two **composite** node types: `workflow` (sub-workflow nesting) and `map` (sequential iteration). Builds on every prior phase. Particularly extends Phase 3.2's child-session/parent-pointer pattern (now applied to workflow runs themselves) and Phase 3.4's pause/resume machinery (now cascading across the parent↔child boundary).

Related docs:

- `process/phase3.x-roadmap.md` §18.2 / 3.7 — the roadmap entry.
- `process/phase3.0-workflow-spec.md` §4.6.1 (workflow node), §4.6.2 (map node), §7.4 (type-specific validation), §7.5 (depth caps).
- `process/phase3.1-executor-spec.md` §11.2 (sub-workflow event ordering: `workflow.node.called_workflow` / `_completed`), §11.3 (trace_id inheritance).
- `process/phase3.2-agent-integration-spec.md` §11.6 — the parent-pointer pattern Phase 3.7 mirrors for workflow→workflow.
- `process/phase3.4-user-input-spec.md` §10.3 — nested pauses leaf-as-source-of-truth rule.

---

## Glossary

| Term | Meaning |
| --- | --- |
| sub-workflow | A workflow invoked by a `kolsch/nodes/workflow` node inside a parent workflow. Owns its own `WorkflowRun`, JSONL, and `_state`. |
| parent pointer | `WorkflowRun.parent: {workflow_run_id, invocation_id}` recording the spawning parent's run + the workflow-node invocation that spawned this child. Mirrors Phase 3.2's `FromWorkflowRun` shape. |
| call chain | The ordered sequence of workflow refs leading from the root run to the current child. Used for cycle detection and depth-cap enforcement. |
| nested pause | A `user_input` pause inside a sub-workflow. Ancestor runs stay `running` with their `workflow` node invocations open; the leaf child's `waiting_for_input` is the SINGLE source of truth (master spec §10.3). |
| worker | The node referenced by `map.apply:` (a node name in the SAME workflow). The map node invokes it once per element of the rendered `over:` list. |
| `with:` constants | Optional `dict` on the map node providing values bound into every iteration's scope alongside `item_as`. |

---

## Background

Phase 3.0 shipped the parser models (`WorkflowNodeDef` at `src/workflows/parser.py:337`, `MapNodeDef` at parser.py:345) and the inputs models (`WorkflowSubInputs` / `MapInputs` at parser.py:121-167). Phase 3.0 also locked the error classes — `WorkflowCallCycle` (`src/workflows/errors.py:753-779`) and `WorkflowExtendsDepthExceeded` (errors.py:247-273, default `limit=16`) already exist; both are reused for sub-workflow validation. `BUILTIN_PHASE_MAP` (executor.py:87-107) maps `map`/`workflow` → `"3.7"`.

The executor's architecture supports nested runs. Phase 3.1's design explicitly anticipated this: `IterativeExecutor` holds no thread-local or process-level mutable state that prevents reentry. A child runner can construct another `IterativeExecutor(workflow=child_wf, run=child_run, ...)` and `await child.run()` from inside its `run()` callback. Module isolation operates per-run via snapshot/cleanup pairs that nest correctly.

Phase 3.2 shipped the **parent-pointer pattern** for spawned agent sessions (`FromWorkflowRun` at `src/schemas/entities.py`; `Session.from_workflow_run` field; `spawned_by` payload on `session.opened`). Phase 3.7 mirrors this pattern verbatim for workflow→workflow: a new `WorkflowRun.parent` field with the same `{workflow_run_id, invocation_id}` shape, populated by the workflow node's runner at child-creation time.

Phase 3.4 shipped pause/resume with the `_replay.py` machinery. Its line 36 comment explicitly anticipates Phase 3.7's nested resume scenario — "Future phases will extend the replay table as new event kinds land (... nested `workflow.run.resumed` in Phase 3.7 sub-workflows)." The replay extension Phase 3.7 ships honors this anticipation.

What's LEFT for Phase 3.7:

1. **`WorkflowRun.parent` field** at `src/workflows/run.py:70-91` — optional `parent: ParentRunPointer | None = None`. `ParentRunPointer` carries `{workflow_run_id, invocation_id}`.
2. **`WorkflowRunStore.create(...)` extension** — accepts `parent=` kwarg; populates the field; persists in `meta.json`.
3. **`WorkflowSubRunner`** at `src/workflows/builtins/workflow_sub.py` — the workflow-node runtime. Resolves the child ref via `AssetCatalog`, parses the child workflow, mints a parent pointer from `ctx`, creates the child `WorkflowRun` with `parent=` + inherited `trace_id`, constructs a nested `IterativeExecutor`, awaits the child run, harvests the child's terminal outputs (per master spec §6.6) as the workflow node's captured dict. Detects pause: when child status is `WAITING_FOR_INPUT`, the parent ALSO pauses (raises `WorkflowPausedForInput` — Phase 3.4's signal class — so the parent's executor follows the same exit-cleanly discipline).
4. **`MapRunner`** at `src/workflows/builtins/map.py` — sequential iteration. Renders `over:` → list (non-list raises `WorkflowNodeFailed`); for each element, resolves the worker node by name in the same workflow (`apply:` references the parent workflow's `nodes:` dict), builds the per-iteration `inputs_overlay = {**bound_inputs, **with_, item_as: element}`, dispatches the worker's runner directly (not via the worklist — map iterations are runner-owned, not graph-owned), accumulates `{results: list[<worker output dict>]}`. Per master spec §4.6.2, worker nodes are NOT addressable from outside the map (`nodes.<worker>` raises `WorkflowMapperWorkerNotAddressable` at parse time; the validator already enforces this per §4.6.2 line 384).
5. **Depth-cap tracking** — `IterativeExecutor` constructor gains optional `_call_depth: int = 0`. Phase 3.7's `WorkflowSubRunner` passes `_call_depth=ctx.call_depth + 1` when constructing the nested executor. At construction time, the executor checks `if _call_depth > 16: raise WorkflowExtendsDepthExceeded(depth=_call_depth, limit=16)` (reusing the existing class; the same 16-cap value covers both extends-chain depth AND call-chain depth, per master spec §7.5).
6. **Cycle detection** — At workflow-load time (when the child workflow is parsed), the runner walks the child's `workflow.ref:` references and the references-of-references transitively. If any visited ref matches a ref already in the active call chain (passed via `ctx.call_chain: tuple[str, ...]`), raise `WorkflowCallCycle(cycle=...)`. The chain is built incrementally as the parent invokes each child.
7. **`workflow.node.completed` payload extension** — for `kolsch/nodes/workflow` nodes specifically, the payload gains `child_run_id: str`. Mirrors Phase 3.2's `spawned_session_id` for agent nodes. Implementation: new `NodeContext.spawned_run_id: WorkflowRunId | None` attribute that the `WorkflowSubRunner` sets before returning; the executor's payload builder reads it.
8. **`_replay.py` extension for nested resume** — when the parent resumes and its open workflow-node invocation points to a child that's still `WAITING_FOR_INPUT`, the parent's resumed `WorkflowSubRunner` invocation detects this (by loading the child's `meta.json` and seeing the paused status), calls `child.resume(response=ctx.resume_response)` on a nested executor, and continues from there. The replay extension also handles a new event kind `workflow.node.called_workflow` (the parent-side event emitted before child invocation; carries `child_workflow_run_id`).
9. **Two new event kinds** — `workflow.node.called_workflow` (emitted by the parent's executor BEFORE the workflow node fires, carrying `{node_name, invocation_id, child_workflow_run_id, child_workflow_ref}`) and `workflow.node.called_workflow_completed` (emitted AFTER the child returns or pauses, carrying `{node_name, invocation_id, child_workflow_run_id, child_status}`). The pair makes parent-side forensics navigable without reading the child JSONL.
10. **Sub-workflow ID minting** — child `WorkflowRunId` is generated fresh per invocation; sub-workflow runs have their OWN events.jsonl in `runs/<child_wrk_id>/`. The `trace_id` is INHERITED from the parent unchanged (master spec §11.3).
11. **State isolation** — at v1, child workflow `_state` initializes from its OWN inputs (computed from `bound_inputs` mapped against the child's start-node input schema), NOT from the parent's state. The parent's state is not visible to the child. This LOCKS the previously-deferred Phase 3.5 question (sub-workflow state inheritance).

The verification gate is six scenarios: a 3-level nested workflow runs end-to-end; a cycle is rejected at workflow load; depth 17 is rejected with `WorkflowExtendsDepthExceeded`; a sub-workflow's `user_input` pauses correctly (parent stays running with open workflow invocation; leaf shows `waiting_for_input`); resume of the leaf bubbles up to parent completion; map iterates a worker over 10 elements producing `{results: list[10]}`.

**This spec breaks the phase into 4 sequential steps**, each with its own Coder and Reviewer cycle. The build plan lives at `process/phase3.7-composition-build.md`.

---

## Scope

**In scope (delivered across the 4 steps):**

- `WorkflowRun.parent` field + `ParentRunPointer` value object.
- `WorkflowRunStore.create(*, parent=None)` kwarg.
- `NodeContext.spawned_run_id` + `NodeContext.call_depth` + `NodeContext.call_chain` attributes.
- `IterativeExecutor.__init__(*, _call_depth=0, _call_chain=())` kwargs (private; for nested-executor construction).
- `WorkflowSubRunner` at `src/workflows/builtins/workflow_sub.py`.
- `MapRunner` at `src/workflows/builtins/map.py`.
- `compute_branch_value` extension: NONE — neither `map` nor `workflow` are branchable per master spec §4.6.
- `register_builtins(...)` extension — register both runners. The workflow runner takes constructor args `catalog`, `store`, and `executor_factory` (a callable `(workflow, run, ...) -> IterativeExecutor` so the runner doesn't import `IterativeExecutor` directly — avoids circular imports). The map runner takes `registry` (to resolve worker runners by type).
- New event kinds `workflow.node.called_workflow` + `workflow.node.called_workflow_completed`.
- `workflow.node.completed` payload extension — `child_run_id` for workflow nodes.
- `_replay.py` extension — handle the two new event kinds + nested `workflow.run.resumed`.
- Depth-cap enforcement at nested-executor construction.
- Cycle detection by call-chain walk at child workflow load.
- Pause cascade — child pause raises `WorkflowPausedForInput` from the workflow runner; parent's executor catches it via Phase 3.4's machinery and pauses ITSELF.
- Resume cascade — `IterativeExecutor.resume(...)` extension: when the open invocation is a workflow node AND the child's status is `WAITING_FOR_INPUT`, the parent reuses the workflow runner to call `nested.resume(response=...)`.
- Goldens at `tests/golden/runs/` for: 3-level nesting; cycle rejection; depth-cap rejection; nested pause+resume; map happy path.
- Starter workflows `bundles/kolsch/workflows/nested-greeting.yaml` (parent calls `hello-paused`) and `bundles/kolsch/workflows/parallel-tickets.yaml` (map over a list of ticket texts dispatching the Phase 3.6 classifier).
- Orphan-sweep extension — paused CHILD runs are skipped (status check is uniform per Phase 3.4's discipline); orphaned children whose parents crashed are reconciled as `failed` with `reason="orphaned_child"` so operators can investigate. Phase 3.7 does NOT auto-reconcile the parent.

**Out of scope (deferred per `phase3.x-roadmap.md`):**

- Concurrent map iteration (Phase 3.10). Phase 3.7's map is strictly sequential.
- Concurrent sub-workflow execution (Phase 3.10).
- Fork (Phase 3.8) — child runs are forkable in 3.8 via the same parent-pointer machinery this phase ships.
- Chat slash commands (Phase 3.9).
- `author_workflow` meta-agent (Phase 3.11).
- **Map worker addressable from outside the map** — explicitly forbidden per master spec §4.6.2. Validator already enforces `WorkflowMapperWorkerNotAddressable`. No runtime change.
- **Child workflow inheriting parent state** — explicitly locked NO at v1. Authors compose state via `set_state` in the child if needed; the parent passes state values into the child via `binds:` → child's input.
- **Mid-iteration map cancellation** — `cancel.flag` is checked at outer-node boundaries (Phase 3.1), not mid-map-iteration. A long-running map waits until the current iteration completes before the executor observes the cancel signal. Documented; not a regression.

**Decisions explicitly locked (do NOT relitigate):**

- **`workflow.ref` is the asset ref of the child workflow.** Validator already enforces "must be runnable — have a graph" per master spec §4.6.1. The runner trusts the validator.
- **`map.apply` is a node NAME in the SAME workflow.** Not a workflow ref, not a tool name, not a function. Validator already enforces (`WorkflowNodeNotDefined` for unresolved names).
- **`map.over` must resolve to a list at runtime.** Non-list raises `WorkflowNodeFailed`. Same pattern as `filter`.
- **`map.item_as` is REQUIRED** (no default, unlike `filter`'s `"item"` default). Master spec §4.6.2 line 378.
- **`map.with` is constants per iteration.** Bound at fire time (rendered once); the rendered dict overlays onto `bound_inputs` for every iteration (under `item_as`).
- **Child workflow state isolation.** Child starts with its own state initialized from its own inputs. Parent's `_state` is NOT visible.
- **Depth 16 cap is SHARED with `extends:`.** Same 16. Same error class (`WorkflowExtendsDepthExceeded`). Validator enforces at run-start; runtime double-checks at nested-executor construction.
- **Cycle is the call CHAIN's repetition, not the GRAPH's recursion.** A workflow that recursively calls itself once is caught (the second activation has the same ref already in chain). A workflow whose graph has a cycle is rejected by Phase 3.0's parser, not here.
- **Nested pause = parent pause.** The parent's workflow node is "mid-call" — it doesn't emit a completion event. The leaf child's `waiting_for_input` is the only paused status visible to operators.
- **`trace_id` inherits unchanged across nesting.** Already locked in Phase 3.1 §11.3; Phase 3.7 honors.
- **Map output is `{results: list[T]}` where T is the worker's full output dict.** Per master spec §4.6.2 line 380. Each element is the entire dict, not just one field.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2, ruamel.yaml, Jinja2, pytest. Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/workflows/run.py:70-91` — `WorkflowRun`. Add optional `parent: ParentRunPointer | None = None`.
- `src/workflows/run_store.py:72-106` — `WorkflowRunStore.create(...)`. Add `parent=` kwarg.
- `src/workflows/executor.py:254-267` — `IterativeExecutor.__init__`. Add `_call_depth=0` + `_call_chain=()` kwargs.
- `src/workflows/executor.py:943-985` — `workflow.node.completed` payload builder. Add `child_run_id` for workflow nodes.
- `src/workflows/executor.py:1131-1159` — `_render_jinja(expression, *, inputs_overlay=)`. Map runner uses it via `ctx.render_jinja(...)`.
- `src/workflows/parser.py:337-352` — `WorkflowNodeDef` + `MapNodeDef`. Already exist.
- `src/workflows/parser.py:121-167` — `WorkflowSubInputs` + `MapInputs`. Already exist.
- `src/workflows/errors.py:247-273` — `WorkflowExtendsDepthExceeded`. Already exists; reused.
- `src/workflows/errors.py:753-779` — `WorkflowCallCycle`. Already exists; raised by Phase 3.7's runner.
- `src/workflows/errors.py:1185` — `WorkflowNodeFailed`. Reused for runtime `over:` non-list etc.
- `src/workflows/_replay.py:1-119` — Phase 3.4 replay machinery. Extend for new event kinds + nested resume.
- `src/workflows/events.py` — `WorkflowEventKind`. Add `CALLED_WORKFLOW` + `CALLED_WORKFLOW_COMPLETED` enum values.
- `src/workflows/context.py` — `NodeContext`. Add `spawned_run_id` / `call_depth` / `call_chain` fields.
- `src/workflows/builtins/__init__.py` — `register_builtins(...)`. Phase 3.7 adds `executor_factory=` kwarg + two registrations.
- `src/workflows/builtins/agent.py:46-150` — the structural template the workflow runner mirrors.
- `src/workflows/_inputs_models.py:42-57` — `_BUILTIN_TO_INPUTS`. Already includes workflow + map slots.
- `src/cli/workflows.py:507-513` — `catalog.lookup(ref, expected_type="workflow")` + `load_workflow(Path(entry.path))` pattern. Reused by the workflow runner.

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch
uv run kolsch --root /tmp/kolsch-composite-test setup --non-interactive
uv run pytest tests/component/ -q
uv run pytest tests/golden/ -q
```

---

## §4.6.1 Workflow sub-runtime (elaboration)

### §4.6.1.1 Runner algorithm

```python
class WorkflowSubRunner:
    """Runtime for kolsch/nodes/workflow — Phase 3.7 §4.6.1."""

    def __init__(
        self,
        *,
        catalog: AssetCatalog,
        store: WorkflowRunStore,
        executor_factory: Callable[..., "IterativeExecutor"],
        registry: RunnerRegistry,
    ) -> None:
        ...

    async def run(
        self,
        *,
        node_name: str,
        node_def: WorkflowNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        # 1. Cycle detection — call_chain includes the CURRENT workflow ref.
        if node_def.ref in ctx.call_chain:
            cycle = (*ctx.call_chain, node_def.ref)
            raise WorkflowCallCycle(file=<parent_yaml_file>, line=<node_line>, cycle=cycle)
        # 2. Depth check (defensive; constructor will also check).
        if ctx.call_depth + 1 > 16:
            raise WorkflowExtendsDepthExceeded(
                file=<parent_yaml_file>, line=<node_line>,
                depth=ctx.call_depth + 1, limit=16,
            )
        # 3. Resolve + load the child workflow.
        entry = self._catalog.lookup(node_def.ref, expected_type="workflow")
        if entry is None:
            raise WorkflowNodeFailed(
                node=node_name, invocation_id=str(ctx.invocation_id),
                cause=ValueError(f"unknown workflow ref {node_def.ref!r}"),
            )
        child_workflow = load_workflow(Path(entry.path))
        # 4. Mint parent pointer.
        parent_ptr = ParentRunPointer(
            workflow_run_id=ctx.workflow_run_id,
            invocation_id=ctx.invocation_id,
        )
        # 5. Create child run with inherited trace_id.
        child_run = self._store.create(
            workflow_ref=node_def.ref,
            inputs=bound_inputs,           # child's inputs = parent's bound_inputs for this edge
            trace_id=ctx.trace_id,         # inheritance per master spec §11.3
            parent=parent_ptr,
        )
        # 6. Emit workflow.node.called_workflow on the PARENT'S JSONL.
        await ctx.emit(WorkflowNodeCalledWorkflowEvent(
            workflow_run_id=ctx.workflow_run_id,
            trace_id=ctx.trace_id,
            parent_event_id=ctx.parent_event_id,
            invocation_id=ctx.invocation_id,
            node_name=node_name,
            payload={
                "node_name": node_name,
                "invocation_id": str(ctx.invocation_id),
                "child_workflow_run_id": str(child_run.run_id),
                "child_workflow_ref": node_def.ref,
            },
        ))
        # 7. Stamp on ctx so the executor's workflow.node.completed payload picks it up.
        ctx.spawned_run_id = child_run.run_id
        # 8. Construct the nested executor.
        nested = self._executor_factory(
            workflow=child_workflow,
            run=child_run,
            store=self._store,
            registry=self._registry,
            catalog=self._catalog,
            _call_depth=ctx.call_depth + 1,
            _call_chain=(*ctx.call_chain, node_def.ref),
        )
        # 9. Run the child. If the child pauses, WorkflowPausedForInput
        #    bubbles up and the parent's executor catches it per Phase 3.4.
        try:
            status = await nested.run()
        except WorkflowPausedForInput:
            # Emit called_workflow_completed with status=waiting_for_input
            # then re-raise so the parent pauses too.
            await ctx.emit(WorkflowNodeCalledWorkflowCompletedEvent(
                workflow_run_id=ctx.workflow_run_id, trace_id=ctx.trace_id,
                parent_event_id=ctx.parent_event_id,
                invocation_id=ctx.invocation_id, node_name=node_name,
                payload={
                    "node_name": node_name, "invocation_id": str(ctx.invocation_id),
                    "child_workflow_run_id": str(child_run.run_id),
                    "child_status": "waiting_for_input",
                },
            ))
            raise  # parent's executor handles per Phase 3.4
        # 10. Map child terminal status → parent runner failure modes.
        if status == WorkflowRunStatus.SUCCEEDED:
            # Compute the child's inferred output dict per master spec §6.6:
            # dict keyed by terminal node names, with each value being that
            # terminal's captured output (or None if the terminal was pruned).
            child_output = compute_terminal_output_dict(child_workflow, nested._captured, nested._pruned)
            await ctx.emit(WorkflowNodeCalledWorkflowCompletedEvent(
                ..., payload={..., "child_status": "succeeded"},
            ))
            return child_output
        if status == WorkflowRunStatus.FAILED:
            await ctx.emit(WorkflowNodeCalledWorkflowCompletedEvent(
                ..., payload={..., "child_status": "failed"},
            ))
            raise WorkflowNodeFailed(
                node=node_name, invocation_id=str(ctx.invocation_id),
                cause=ChildWorkflowFailed(run_id=str(child_run.run_id)),
            )
        # ABORTED / TIMED_OUT — propagate as failure too.
        raise WorkflowNodeFailed(
            node=node_name, invocation_id=str(ctx.invocation_id),
            cause=ChildWorkflowFailed(run_id=str(child_run.run_id), status=str(status)),
        )
```

`compute_terminal_output_dict(child_workflow, captured, pruned)` is a new helper at `src/workflows/_terminal.py`. It walks the child's node graph; terminal nodes (no outgoing edges) contribute their captured dict (or `None` if pruned). The result is what `nodes.<workflow_node>.<terminal_name>` resolves to in the parent's downstream Jinja.

### §4.6.1.2 Pause cascade

When the child workflow's `user_input` runner raises `WorkflowPausedForInput` (per Phase 3.4), the nested executor catches it, writes the child's `waiting_at` sidecar, flips child status to `WAITING_FOR_INPUT`, runs module cleanup, exits cleanly. The parent's `WorkflowSubRunner` sees the child status as `WAITING_FOR_INPUT` (it ran `await nested.run()` which returned `WAITING_FOR_INPUT` instead of throwing) — OR alternatively, the child raises `WorkflowPausedForInput` directly to bubble through (cleaner; matches Phase 3.4's leaf pattern).

The chosen design: the nested executor's `run()` method, when catching `WorkflowPausedForInput`, RE-RAISES it after writing the child sidecar + flipping child status. The parent's `WorkflowSubRunner.run()` lets it propagate without catching. The parent's outer worklist catches it at the same site Phase 3.4 ships, performs the parent's own pause discipline (writes parent's `waiting_at` pointing at the workflow node's invocation_id; flips PARENT status to `WAITING_FOR_INPUT`; cleans up modules; exits).

The leaf's `waiting_at` carries the user_input node's invocation_id; the parent's `waiting_at` carries the workflow-node invocation_id. The parent's `waiting_at` is the operator-visible pause point per Phase 3.4's CLI surface — but to resume, the operator runs `kolsch workflows resume <leaf_run_id>` (the LEAF), not the parent. The leaf's resume propagates up.

### §4.6.1.3 Resume cascade

When the operator runs `kolsch workflows resume <leaf_run_id> --input response=...`, Phase 3.4's `IterativeExecutor.resume(...)` flows through normally for the leaf. The leaf's user_input completes; downstream nodes fire; the leaf eventually reaches a terminal status (SUCCEEDED, FAILED, etc.) AND its events.jsonl emits `workflow.run.completed`/`failed`/etc.

But the leaf's parent's status is still `WAITING_FOR_INPUT` — its run is paused too. How does the parent resume?

**Two-phase resume design.** The operator runs `kolsch workflows resume <leaf_run_id>` first; the leaf runs to terminal; the leaf's parent is now in an INCONSISTENT state (parent says `WAITING_FOR_INPUT`, child says `SUCCEEDED`). The operator then runs `kolsch workflows resume <parent_run_id>` (no `--input` needed — there's no user_input on the parent itself). Phase 3.4's `resume()` reads the JSONL, replays state, finds the open `workflow` invocation, and `_continue_worklist` re-enters. The workflow runner sees its child's `meta.json` shows `SUCCEEDED`, harvests results, emits `workflow.node.called_workflow_completed`, emits `workflow.node.completed`, and continues.

The runner's resume-detection branch:

```python
# Inside WorkflowSubRunner.run(), early in the algorithm:
# Phase 3.7 resume cascade — if the parent is resuming AND this node has
# a child_run_id already recorded (in the parent's replayed _captured?
# no — the captured dict for this node doesn't exist yet because the
# completed event hasn't fired. So check via the waiting_at sidecar +
# the called_workflow event in the parent's JSONL).
if ctx.is_resume_mode and <child_run_id from replayed events>:
    child_run = self._store.read(<child_run_id>)
    if child_run.status == WorkflowRunStatus.WAITING_FOR_INPUT:
        # Child is still paused. We can't actually continue here —
        # the operator needs to resume the leaf first. Raise.
        raise WorkflowNodeFailed(
            node=node_name, invocation_id=str(ctx.invocation_id),
            cause=ValueError(
                f"child run {child_run_id} is still paused; "
                f"resume it first with `kolsch workflows resume {child_run_id}`"
            ),
        )
    # Child has reached terminal. Harvest its results and continue.
    return _harvest_child(child_run)
```

This is the simpler design — the operator drives the cascade. A more automated design (parent's resume DETECTS the child needs resuming and propagates the response down) is deferred to a future QoL.

### §4.6.1.4 Trace-id inheritance

The parent's `ctx.trace_id` flows into `child_run = store.create(..., trace_id=ctx.trace_id, ...)`. The child's events all carry the same `trace_id`. Cross-JSONL forensics works exactly like Phase 3.2's agent/session pair.

---

## §4.6.2 Map runtime (elaboration)

### §4.6.2.1 Runner algorithm

```python
class MapRunner:
    """Runtime for kolsch/nodes/map — Phase 3.7 §4.6.2."""

    def __init__(self, *, registry: RunnerRegistry) -> None:
        self._registry = registry

    async def run(
        self,
        *,
        node_name: str,
        node_def: MapNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        # 1. Render `over:` to a Python list.
        rendered_over = self._render_if_jinja(node_def.over, ctx, bound_inputs)
        if not isinstance(rendered_over, list):
            raise TypeError(
                f"node {node_name!r} map.over must resolve to a list, "
                f"got {type(rendered_over).__name__}"
            )
        # 2. Render `with:` constants once (per master spec §4.6.2 — constants).
        rendered_with: dict[str, Any] = {}
        for key, raw_value in node_def.with_.items():
            if isinstance(raw_value, str) and raw_value.lstrip().startswith("{{"):
                rendered_with[key] = ctx.render_jinja(raw_value, inputs_overlay=bound_inputs)
            else:
                rendered_with[key] = raw_value
        # 3. Resolve worker NodeDef by name in the SAME workflow.
        if node_def.apply not in ctx.workflow.nodes:
            raise WorkflowNodeFailed(
                node=node_name, invocation_id=str(ctx.invocation_id),
                cause=ValueError(f"map.apply references undefined node {node_def.apply!r}"),
            )
        worker_def = ctx.workflow.nodes[node_def.apply]
        # 4. Fetch the worker's runner.
        worker_runner = self._registry.get(worker_def.type, node_name=node_def.apply)
        # 5. Sequential iteration.
        results: list[Any] = []
        for index, element in enumerate(rendered_over):
            iteration_overlay = {**bound_inputs, **rendered_with, node_def.item_as: element}
            # Per master spec §4.0.2 — strict-mode bind validation against worker's Inputs model.
            worker_inputs_model = _inputs_model_for(worker_def)
            if worker_inputs_model is not None:
                try:
                    worker_inputs_model.model_validate(iteration_overlay, strict=True)
                except ValidationError as exc:
                    raise WorkflowBindTypeMismatch(
                        file=<worker_yaml_file>, line=<worker_yaml_line>,
                        node=node_def.apply,
                        bind=str(exc.errors()[0]["loc"][0]),
                        expected_type=exc.errors()[0]["type"],
                        actual_type=type(iteration_overlay.get(str(exc.errors()[0]["loc"][0]), None)).__name__,
                    ) from exc
            # Build a child NodeContext for the worker.
            iter_ctx = ctx.derive_for_iteration(
                node_name=node_def.apply,
                parent_invocation_id=ctx.invocation_id,
                iteration_index=index,
                iteration_total=len(rendered_over),
            )
            output = await worker_runner.run(
                node_name=node_def.apply,
                node_def=worker_def,
                bound_inputs=iteration_overlay,
                state=state,
                ctx=iter_ctx,
            )
            results.append(dict(output))
        return {"results": results}

    @staticmethod
    def _render_if_jinja(value, ctx, overlay):
        if isinstance(value, str) and value.lstrip().startswith("{{"):
            return ctx.render_jinja(value, inputs_overlay=overlay)
        return value
```

### §4.6.2.2 NodeContext extension for iterations

`NodeContext.derive_for_iteration(node_name, parent_invocation_id, iteration_index, iteration_total)` creates a per-iteration context with a fresh `invocation_id` (so each iteration emits distinct `workflow.node.started`/`completed` events) and `parent_invocation_id` (so the events correlate back to the map node). Per master spec event-log §11.3, iteration events carry `parent_invocation_id` + `iteration_index` payload fields.

### §4.6.2.3 Iteration event emission

Each iteration emits a full `workflow.node.started` / `workflow.node.completed` pair for the worker. The events carry the worker node's name (NOT the map node's name), with `parent_invocation_id` = the map node's invocation_id, and `iteration_index` in payload. This makes the JSONL contain N+1 events per map invocation: the map node's `started`, N×(worker started + completed pairs), and the map node's `completed`. Forensics tools can correlate worker events back to their parent map.

### §4.6.2.4 Failure mode

If any iteration's worker raises, the map runner re-raises immediately (sequential semantics; no continue-on-error at v1). Per Phase 3.1's abort-on-failure rule, the run aborts. Partial `results` collected so far are NOT persisted (the map node's `workflow.node.completed` never fires). The map node emits `workflow.node.failed` instead.

A future QoL (`on_error: continue | skip | route_to`) is deferred per master spec §1.2.

### §4.6.2.5 Worker scope

The worker NodeDef sees only what map binds: `iteration_overlay` = `bound_inputs ∪ rendered_with ∪ {item_as: element}`. The worker does NOT see the parent's `nodes.*` map directly — its scope is the iteration's bindings only. This is intentional: workers are templates, instantiated N times.

**Caveat:** the worker's `node_def` may reference `nodes.<X>` in its Jinja config (e.g., a bash node's `command: "echo {{ nodes.prep.stdout }}"`). The validator's transitive-ancestor check at parse time (Phase 3.0 §7.2) would normally validate this. Master spec §4.6.2's "Worker nodes are NOT addressable as `nodes.<worker>` from outside the map node" rule cuts the OUTWARD direction; the INWARD direction (worker reading `nodes.*` from the parent graph) is permitted via the executor's `_captured` dict at the iteration time. The map runner passes `ctx` (which carries access to `_captured` through `ctx.render_jinja`'s scope) to the worker; the worker sees a frozen snapshot of `_captured` at map-fire time.

This is documented as load-bearing; tests cover it.

---

## §11 Event log extension

### §11.1 New event kinds

Two new kinds in `WorkflowEventKind`:

- `CALLED_WORKFLOW = "workflow.node.called_workflow"` — emitted by the parent BEFORE the workflow node fires. Payload: `{node_name, invocation_id, child_workflow_run_id, child_workflow_ref}`.
- `CALLED_WORKFLOW_COMPLETED = "workflow.node.called_workflow_completed"` — emitted by the parent AFTER the child returns / pauses / fails. Payload: `{node_name, invocation_id, child_workflow_run_id, child_status}`.

These are PARENT-side events; they live in the parent's JSONL. The CHILD-side events (its own `workflow.run.*` and `workflow.node.*`) live in the child's JSONL. Cross-JSONL linkage is via `trace_id` (universal) + `child_run_id` (parent→child) + `parent` field on `WorkflowRun` (child→parent).

### §11.2 `workflow.node.completed` payload extension

For `kolsch/nodes/workflow` nodes specifically:

```python
payload["child_run_id"] = str(ctx.spawned_run_id)
```

Mirrors Phase 3.2's `spawned_session_id`. Gated by `node_def.type == "kolsch/nodes/workflow"` AND `ctx.spawned_run_id is not None`.

### §11.3 Map iteration events

Each map iteration emits per master spec §11.3:

- `workflow.node.started` for the worker. Payload includes `parent_invocation_id` (= map node's invocation_id) + `iteration_index` (0-based) + `iteration_total`.
- `workflow.node.completed` for the worker.

The map node itself emits its own `workflow.node.started` (BEFORE the first iteration) and `workflow.node.completed` (AFTER all iterations succeeded). The map node's completed payload's `output` is `{results: [<dict>, <dict>, ...]}`.

---

## §12 Error classes (one new, two reused)

Phase 3.7 ships ONE new error class:

- `ChildWorkflowFailed(WorkflowRuntimeError)` — wrapper for a child workflow's failure, surfaced through the parent's `WorkflowNodeFailed(cause=ChildWorkflowFailed(...))`. Identifying names: `(run_id, status)`. Caller message: "A sub-workflow step failed. Share the run ID with your engineer."

Phase 3.7 reuses:

- `WorkflowCallCycle` (`errors.py:753-779`) — raised by `WorkflowSubRunner` on cycle detection.
- `WorkflowExtendsDepthExceeded` (`errors.py:247-273`) — raised by nested executor construction when depth > 16.
- `WorkflowNodeFailed` (`errors.py:1185`) — wraps all other failures (non-list `map.over:`, unresolved refs, etc.).
- `WorkflowBindTypeMismatch` (`errors.py:490-528`) — raised by map's per-iteration strict-mode bind validation.
- `WorkflowPausedForInput` (Phase 3.4 signal) — bubbles through the workflow runner unmodified.

---

## Phase-level Acceptance Criteria

- A workflow `kolsch/workflows/nested-greeting` (parent) invokes `kolsch/workflows/hello-paused` (child) via a `workflow:` node. The child's terminal output is harvested into the parent's `nodes.<workflow_node>` dict; downstream nodes consume it correctly.
- A 3-level nesting (great-grandparent → grandparent → parent → child) runs end-to-end. All four JSONLs share the same `trace_id`; each child's `WorkflowRun.parent` records the immediate spawning parent.
- A workflow with self-call (`A` calls `A`) raises `WorkflowCallCycle` at child workflow load; the parent's run aborts with `WorkflowNodeFailed(cause=...)`.
- A workflow chain reaching depth 17 raises `WorkflowExtendsDepthExceeded(depth=17, limit=16)` at nested executor construction.
- A sub-workflow `user_input` pauses; the parent's status flips to `WAITING_FOR_INPUT` with its `waiting_at` carrying the workflow-node's invocation_id; the leaf child's status is `WAITING_FOR_INPUT` with its `waiting_at` carrying the user_input's invocation_id.
- Operator resumes the leaf via `kolsch workflows resume <leaf_run_id> --input response="..."`; the leaf completes to `SUCCEEDED`.
- Operator then resumes the parent via `kolsch workflows resume <parent_run_id>` (no `--input`); the parent's workflow runner detects the child as `SUCCEEDED`, harvests results, completes the workflow node, and the parent runs to terminal.
- A 3-level nested resume chain (leaf → parent → grandparent → root) works.
- `map` over a list of 10 ticket texts dispatching the Phase 3.6 `kolsch/agents/classifier` produces `{results: [10 dicts]}`. Downstream `filter` on the results works.
- `map.over` resolving to a non-list raises `WorkflowNodeFailed`; run aborts.
- `map` with `with: {prefix: "{{ inputs.tag }}"}` correctly binds the rendered prefix into every iteration's scope.
- Iteration events carry `parent_invocation_id` + `iteration_index` payload fields; forensics correlates correctly.
- `workflow.node.completed` for a workflow node carries `child_run_id`; for a map node, carries `output: {results: [...]}`.
- Two new event kinds emit at the documented ordering.
- Child workflow state isolation: parent's `set_state` mutations are NOT visible to child; child's `set_state` mutations are NOT visible to parent.
- Orphan sweep: paused CHILD runs are skipped; orphaned children whose parents are dead are marked `failed` with `reason="orphaned_child"`.
- No regressions: full suite green; non-composite workflows unchanged.

---

## Phase-level Integration Tests

### Phase Test 1: 3-level nesting end-to-end
- **Run:** `uv run pytest tests/component/test_workflow_composition.py::test_three_level_nesting -v`
- **Expect:** all four JSONLs (root + 3 children) exist; share `trace_id`; each child's `WorkflowRun.parent` matches.

### Phase Test 2: Cycle rejected
- **Run:** `uv run pytest tests/component/test_workflow_composition.py::test_self_call_cycle_rejected -v`
- **Expect:** `WorkflowCallCycle` raised; root run aborts.

### Phase Test 3: Depth cap
- **Run:** `uv run pytest tests/component/test_workflow_composition.py::test_depth_seventeen_rejected -v`
- **Expect:** `WorkflowExtendsDepthExceeded(depth=17, limit=16)`.

### Phase Test 4: Nested pause + resume
- **Run:** `uv run pytest tests/component/test_workflow_composition.py::test_nested_pause_resume -v`
- **Expect:** parent + leaf both `WAITING_FOR_INPUT` after pause; leaf resume → leaf `SUCCEEDED`; parent resume → parent `SUCCEEDED`.

### Phase Test 5: Map happy path
- **Run:** `uv run pytest tests/component/test_workflow_map.py::test_map_over_ten_elements -v`
- **Expect:** `{results: [...len 10...]}`; downstream filter consumes correctly.

### Phase Test 6: Map non-list raises
- **Run:** `uv run pytest tests/component/test_workflow_map.py::test_map_over_non_list_raises -v`
- **Expect:** `WorkflowNodeFailed`; run aborts.

### Phase Test 7: State isolation
- **Run:** `uv run pytest tests/component/test_workflow_composition.py::test_child_state_isolated -v`
- **Expect:** parent `state.x=1`, child `set_state` to `x=99`, parent's `state.x` still `1` after child terminates.

### Phase Test 8: No regressions
- **Run:** `uv run pytest tests/ -q`

---
