# [SPEC-009] Phase 3.8 — Fork-from-event

**Type:** spec
**Doc #:** 009
**Status:** Draft
**Created:** 2026-06-27

---

## Glossary

| Term | Meaning |
| --- | --- |
| fork | The single operation `kolsch workflows fork <run_id> --at <event_id> [--input k=v]...`. Replays parent events up to the fork point, optionally applies `--input` overrides, executes forward from there. |
| fork point | The `<event_id>` chosen by the operator. Must be an event in the PARENT's `runs/<wrk_id>/events.jsonl` (not in a spawned agent session). |
| forked run | The new `WorkflowRun` created by fork; carries `forked_from: {run_id, fork_event_id}`. Inherits parent's `trace_id` unchanged. |
| `workflows events` | NEW CLI command this phase ships. Required prerequisite — operators discover `event_id` values through it. |

---

## Background

Phase 3.4 shipped `_replay.py` (`src/workflows/_replay.py`) and the resume entry point on `IterativeExecutor`. Phase 3.5 extended replay to handle `workflow.state.mutated` events. Phase 3.7 extended replay to handle the two new sub-workflow event kinds (`workflow.node.called_workflow` / `_completed`). Phase 3.8 reuses ALL of this replay machinery — fork is structurally "replay then start running from a different point," not "replay then wait for input."

Phase 3.2 shipped the parent-pointer pattern (`Session.from_workflow_run`). Phase 3.7 shipped the workflow→workflow mirror (`WorkflowRun.parent: ParentRunPointer`). Phase 3.8 adds a SIBLING `WorkflowRun.forked_from: ForkedFrom` carrying `{run_id: WorkflowRunId, fork_event_id: EventId}` — the same shape pattern. A run can have BOTH `parent` (if it was spawned by a sub-workflow node) AND `forked_from` (if it was forked from another run); the two are independent.

Phase 3.1 shipped event-ID minting (`new_event_id()` at `src/workflows/events.py:95-97` — `ev_<uuid4>`). Phase 3.8 needs to look up a specific event by ID in the parent's JSONL — a new helper `_find_event_by_id(events_iter, target_id)`.

What's LEFT for Phase 3.8:

1. **`ForkedFrom` value object** at `src/schemas/entities.py` (or alongside `ParentRunPointer` from Phase 3.7).
2. **`WorkflowRun.forked_from` field** + `WorkflowRunStore.create(*, forked_from=)` extension.
3. **`WorkflowRunNotFound`** error class (Phase 3.0 §12.1 mentioned it; this phase ships it if absent). Raised when the parent run's JSONL has been deleted.
4. **`workflow.run.forked_from` event kind** — emitted as the FIRST event of the forked run's JSONL. Mirrors Phase 3.4's `workflow.run.resumed` pattern (new kind, not a `workflow.run.started` payload extension — keeps event-stream consumers' switch statements clean).
5. **`kolsch workflows events <run_id>`** CLI command — NEW. Lists the run's JSONL events with `event_id` / `kind` / `ts` / a brief payload preview. The CLI was mentioned in Phase 3.0 §14.1 but never shipped; this phase ships it because operators need it to discover fork point candidates.
6. **`_find_event_by_id(events, target_id)`** helper at `src/workflows/_replay.py` — iterates events, returns the matching event dict OR `None`. Used by the fork validator.
7. **Fork-point legality check** — `fork_event_id` MUST exist in the PARENT's `events.jsonl`. If absent, raise `WorkflowRuntimeError` with a clear message (the agent-session JSONL events explicitly cannot be fork points per spec §9.5).
8. **`IterativeExecutor.start_from_fork(*, fork_event_id, input_overrides)`** entry point. Mirrors Phase 3.4's `resume(...)`. Algorithm:
   - Status check (forked run is freshly created; status `PENDING` is expected).
   - Read parent's events; truncate at fork_event_id (inclusive).
   - Replay events via existing `replay_into(self, events)` to rebuild `_captured` + `_state` + `_pruned` + `_completed_or_skipped`.
   - Apply `--input` overrides: replace specific captured outputs at the fork point (each override key targets the node whose output is being overridden — confirmed at Step 1's design call). Default interpretation: each override key replaces the corresponding key in the LAST-completed node's captured output at the fork point. ALTERNATIVE: overrides set workflow-level inputs, not per-node captured outputs. The cleaner reading per spec §9.1 is "Apply optional --input overrides (replaces captured outputs at the fork point)." Step 1 picks the implementation. **Recommended:** overrides replace the fork-point node's captured output — per spec, that's the "re-roll this node with different inputs" semantics. The override dict is merged into the captured output of whichever node owns the fork_event_id (e.g., if fork_event_id is a `workflow.node.completed` for node `classify`, overrides update `_captured["classify"]`'s dict).
   - Mint a fresh `WorkflowRun` PID; emit `workflow.run.forked_from` as the FIRST event of the new JSONL (carrying `forked_from_run_id` + `fork_event_id` in payload).
   - Re-enter `_continue_worklist` per Phase 3.4's pattern. The worklist sees `_completed_or_skipped` populated with everything up to the fork point and resumes from the next-eligible node.
9. **`_cmd_fork(...)`** in `src/cli/workflows.py`. Mirrors `_cmd_resume(...)`'s shape: parse `--input` via existing `_parse_input_pairs`, read parent run, validate parent existence, construct forked `WorkflowRun` with `forked_from=` populated, construct executor, await `start_from_fork(...)`, render the result envelope per spec §14.3.
10. **Sub-workflow fork semantics** — handled by replay's existing `_captured`-restoration logic. Per spec §9.3:
    - Forking AFTER a workflow-call node: the parent's `workflow.node.completed` event for that node carries the child's harvested outputs in `payload.output` plus `child_run_id` in payload. Replay restores `_captured["<workflow_node>"]` from `payload.output`. No child JSONL re-replay; the captured output is canonical.
    - Forking BEFORE a workflow-call node: the workflow node hasn't fired yet; `_captured["<workflow_node>"]` is not in the replayed dict. The forked run executes the workflow node fresh (spawning a NEW child run).
    - Forking AT a `workflow.node.called_workflow` event (which fires BEFORE the workflow node runs): the workflow node is treated as not-yet-completed; forked run runs it fresh.

The verification gate is six scenarios: fork at a `workflow.node.completed` event reuses upstream captured outputs; fork at a `workflow.run.started` event runs the whole workflow fresh with potentially-overridden inputs; fork at an event_id that doesn't exist in the parent's JSONL raises a clear error; fork at an event_id that exists in a SPAWNED SESSION's JSONL raises `WorkflowRuntimeError` (per spec §9.5); fork of a run whose JSONL was deleted raises `WorkflowRunNotFound`; `--input` overrides correctly modify the fork-point node's captured output.

**This spec breaks the phase into 4 sequential steps (Step 0 — Step 3)**, each with its own Coder and Reviewer cycle.

Related docs:

- `process/phase3.8-fork-spec.md` — the master design spec (§9 fork mechanism, §14.1 CLI surface).
- `process/phase3.4-user-input-build.md` — `_replay.py` + `IterativeExecutor.resume(...)` + `_continue_worklist` pattern; Phase 3.8 mirrors all three.
- `process/phase3.7-composition-build.md` — `ParentRunPointer` pattern Phase 3.8's `ForkedFrom` mirrors.
- `process/phase3.0-workflow-spec.md` §12.1 — `WorkflowRunNotFound` placement.
- `docs/explanation/workflow-event-log.md` §11 — event-kind taxonomy.

---

## Scope

**In scope (delivered across the 4 steps):**

- `ForkedFrom` value object at `src/schemas/entities.py`.
- `WorkflowRun.forked_from: ForkedFrom | None = None` field.
- `WorkflowRunStore.create(*, forked_from=)` kwarg; meta.json round-trip.
- `WorkflowRunNotFound` error class (if not already shipped by Phase 3.0/3.4).
- `WorkflowEventKind.RUN_FORKED_FROM = "workflow.run.forked_from"` enum + `WorkflowRunForkedFromEvent` + `WorkflowRunForkedFromPayload`.
- `_find_event_by_id(events_iter, target_id) -> dict | None` helper.
- Fork-point legality check (raises `WorkflowRuntimeError` for missing event_id OR event in spawned-session JSONL — the latter detected by absence from parent's JSONL).
- `IterativeExecutor.start_from_fork(*, fork_event_id, input_overrides)` entry point.
- `_cmd_fork(...)` CLI handler + subparser registration.
- `kolsch workflows events <run_id>` CLI command (new — prerequisite for operators).
- `--input` override semantics: replace the fork-point node's captured output keys (per Step 1's design call documented inline).
- Sub-workflow fork handling — relies on the parent's replayed `_captured["<workflow_node>"]` from the parent's existing `workflow.node.completed` payload's `output` field. No recursive child-JSONL replay.
- Goldens at `tests/golden/runs/` for: fork-at-completed; fork-at-started; fork-at-invalid-event-id; fork-with-input-override.
- A reference test fixture exercising the spec §9.2 side-effect-discipline contract — upstream side effects are NOT undone.
- Two starter workflows: `bundles/kolsch/workflows/fork-demo.yaml` (a simple 3-node pipeline ideal for fork experimentation) + reuse `bundles/kolsch/workflows/triage-tickets.yaml` (Phase 3.6) for the agent-fork test case.

**Out of scope (deferred per `phase3.x-roadmap.md`):**

- Fork-and-merge / fork-multiple / automatic upstream side-effect undo (per spec §Non-goals).
- Chat surface for fork (Phase 3.9 ships `/fork` slash).
- Concurrent fork execution (Phase 3.10).
- Auto-retention policies (per spec retention context — "forever retention" at v1).
- Multi-tenant fork authorization (deferred to Phase 4+).
- Resume of a forked run that paused at a `user_input` — works transitively via Phase 3.4's machinery (no new code needed); tests cover.

**Decisions explicitly locked (do NOT relitigate):**

- **Fork point is a single event_id, not a range.** No "fork from N to M" semantics.
- **Upstream side effects are NOT undone.** Per spec §9.2. The forked run starts with the filesystem / external services in whatever state the parent left them.
- **`trace_id` inherits unchanged.** Original + fork belong to the same causal cascade. Per spec §9.1 + master spec §11.3.
- **Fork-point events must be in the workflow's JSONL** (per spec §9.5). Spawned-session events (`model.request.sent`, `session.*`) are illegal fork points; the engine rejects them.
- **`workflow.run.forked_from` is a NEW event kind** (not a payload extension on `workflow.run.started`). Symmetric with Phase 3.4's `workflow.run.resumed`.
- **`--input` overrides replace the FORK-POINT NODE's captured output keys.** Not workflow-level inputs (that's the `kolsch workflows run` semantic). Per spec §9.1's "replaces captured outputs at the fork point."
- **No new fork modes** (per spec §Non-goals).

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2, ruamel.yaml, Jinja2, pytest. Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/workflows/_replay.py` — Phase 3.4/3.5/3.7 replay machinery. Phase 3.8 reuses + adds `_find_event_by_id`.
- `src/workflows/run.py:70-92` — `WorkflowRun`. Add `forked_from` field.
- `src/workflows/run_store.py:72-106` — `create(...)`. Add `forked_from=` kwarg.
- `src/workflows/run_store.py:186-206` — `read_events(...)`. Used to iterate parent's events.
- `src/workflows/run_store.py:108-136` — `read(...)`. Used to load parent run; raises if meta.json absent.
- `src/workflows/events.py:65-92` — `WorkflowEventKind` enum. Add `RUN_FORKED_FROM`.
- `src/workflows/events.py:95-97` — `new_event_id()`. Unchanged.
- `src/workflows/executor.py:446-660` — `IterativeExecutor.resume(...)` (Phase 3.4). The structural template for `start_from_fork`.
- `src/workflows/executor.py:_continue_worklist` — extracted in Phase 3.4 Step 1; reused by fork's entry point.
- `src/workflows/errors.py` — add `WorkflowRunNotFound` if not present.
- `src/cli/workflows.py:641-768` — `_cmd_resume` template; mirror for `_cmd_fork`.
- `src/cli/workflows.py:190-241` — dispatcher table; add `fork` + `events` cases.
- `src/cli/workflows.py:771-785` — `_parse_input_pairs`; reused verbatim.
- `src/cli/workflows.py` — `_cmd_show` patterns for new `_cmd_events`.
- `src/workflows/_inputs_models.py` — strict-mode validation for `--input` overrides (per-node check at fork point).
- `src/schemas/entities.py` — `ParentRunPointer` (Phase 3.7); add `ForkedFrom` alongside.

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch
uv run kolsch --root /tmp/kolsch-fork-test setup --non-interactive
uv run pytest tests/component/ -q
uv run pytest tests/golden/ -q
```

---

## Phase-level Acceptance Criteria

- `kolsch workflows events <run_id>` lists JSONL events with `event_id`, `kind`, `ts`, brief payload preview. Text + JSON formats supported.
- `kolsch workflows fork <parent_run_id> --at <event_id>` creates a new run with `forked_from: {run_id: parent, fork_event_id: <id>}`; emits `workflow.run.forked_from` as the FIRST event of the new JSONL; replays parent events up to `<event_id>`; resumes execution from the fork point.
- Forked run inherits parent's `trace_id` unchanged. Both parent's JSONL and forked run's JSONL carry the same `trace_id` on every event.
- Fork at an `event_id` that doesn't exist in the parent's JSONL raises `WorkflowRuntimeError` with a clear "event_id not found in parent's JSONL" message; exit 1.
- Fork at an `event_id` from a spawned-session JSONL (e.g., `model.request.sent`) is REJECTED by the same check — those events aren't in the parent's `events.jsonl`. The error message guides users to fork at the workflow-level event preceding the agent invocation.
- Fork against a parent whose JSONL has been deleted raises `WorkflowRunNotFound`; exit 1.
- `--input key=value` overrides correctly replace the fork-point node's captured output keys.
- Forking at a `workflow.node.completed` for a node downstream of a `workflow:` (sub-workflow) node correctly reuses the parent's captured child outputs. The child's JSONL is NOT recursively replayed (per spec §9.3).
- Forking BEFORE a workflow-call node causes the forked run to spawn a FRESH child workflow_run (new `child_run_id`).
- Side-effect discipline (per spec §9.2) is documented in `_cmd_fork`'s `--help` output; tests verify upstream side effects (e.g., a bash node that touched a file) remain when the fork begins.
- `WorkflowRun.forked_from` round-trips through `WorkflowRunStore.create` → `read` cleanly.
- A forked run with a `user_input` node downstream of the fork point can be PAUSED and RESUMED via Phase 3.4's machinery; resume produces correct downstream outputs.
- A forked run with a child sub-workflow can be FORKED AGAIN (compose); chain of `forked_from` is preserved per-run.
- No regressions: full suite green; non-forked workflows produce byte-identical JSONLs.

---

## Phase-level Integration Tests

### Phase Test 1: Events command
- **Run:** `uv run kolsch --root /tmp/kolsch-fork-test workflows run kolsch/workflows/fork-demo` then `uv run kolsch --root /tmp/kolsch-fork-test workflows events <run_id>`.
- **Expect:** events command lists `event_id`, `kind`, `ts`, payload preview for every event; JSON format yields a well-formed array.

### Phase Test 2: Fork at completed event
- **Run:** `uv run pytest tests/component/test_workflow_fork.py::test_fork_at_completed -v`
- **Expect:** fork at the `workflow.node.completed` of node `B` in a `A → B → C` pipeline produces a new run whose JSONL starts with `workflow.run.forked_from`, has replayed state, and executes only `C`.

### Phase Test 3: Fork at started event
- **Run:** `uv run pytest tests/component/test_workflow_fork.py::test_fork_at_started -v`
- **Expect:** fork at the `workflow.run.started` event re-runs the whole workflow from scratch (with overridden inputs if provided).

### Phase Test 4: Invalid event_id
- **Run:** `uv run pytest tests/component/test_workflow_fork.py::test_fork_invalid_event_id -v`
- **Expect:** fork at `ev_doesnotexist` raises `WorkflowRuntimeError`; exit 1.

### Phase Test 5: Spawned-session event_id rejected
- **Run:** `uv run pytest tests/component/test_workflow_fork.py::test_fork_session_event_rejected -v`
- **Expect:** fork at a `model.request.sent` event (which lives in the agent session JSONL, NOT the parent workflow's JSONL) raises `WorkflowRuntimeError`. Error message guides operator to fork at the workflow-level event.

### Phase Test 6: Parent JSONL deleted
- **Run:** `uv run pytest tests/component/test_workflow_fork.py::test_fork_parent_deleted -v`
- **Expect:** delete parent's `events.jsonl`; fork raises `WorkflowRunNotFound`.

### Phase Test 7: Input override
- **Run:** `uv run pytest tests/component/test_workflow_fork.py::test_fork_with_input_override -v`
- **Expect:** fork at `workflow.node.completed` of `classify` with `--input label=urgent`; forked run's downstream node sees `nodes.classify.label == "urgent"`.

### Phase Test 8: Sub-workflow fork — reuse vs re-execute
- **Run:** `uv run pytest tests/component/test_workflow_fork.py::test_fork_with_subworkflow -v`
- **Expect:** fork AFTER the workflow-call node reuses the parent's captured child outputs (no new child run created); fork BEFORE the workflow-call node creates a fresh child run.

### Phase Test 9: No regressions
- **Run:** `uv run pytest tests/ -q`

---

## Coordinator Prompt

> You are the strategic decision-maker. You do not write or review code.
> Drive the 4-step Coder/Reviewer flow.

**Your task:**

Ship Phase 3.8 of kolsch-engine workflows through 4 sequential Coder/Reviewer cycles. The phase is structurally **replay + new-entry-point**: most of the machinery already exists (Phase 3.4's `_replay.py` + `_continue_worklist`); the new work is one entry-point method, one CLI subcommand, one event kind, and the `kolsch workflows events` command that operators need to discover fork point candidates.

**Authoritative references:**

- THIS spec: `process/phase3.8-fork-build.md`.
- Master design spec: `process/phase3.8-fork-spec.md` — §9 + §14.1 + §Non-goals + retention context.
- Phase 3.4 build plan: `process/phase3.4-user-input-build.md` — `_replay.py` + `IterativeExecutor.resume(...)` + `_continue_worklist` structural template.
- Phase 3.7 build plan: `process/phase3.7-composition-build.md` — `ParentRunPointer` mirror.
- Phase 3.0 master spec: `process/phase3.0-workflow-spec.md` §12.1 (error placement) + §14.1 (CLI surface).
- `docs/explanation/workflow-event-log.md` §11 — event-kind taxonomy; fork-point legality.

**The 4 steps:**

| Step | Focus | Effort |
| --- | --- | --- |
| **Step 0** | **PREREQ** — `ForkedFrom` + `WorkflowRun.forked_from` + store extension + `WorkflowRunNotFound` (if needed) + `WorkflowEventKind.RUN_FORKED_FROM` + `WorkflowRunForkedFromEvent` + `kolsch workflows events <run_id>` CLI command (the operator-facing prerequisite). | ~1.5 days |
| **Step 1** | Fork-point validation + `_find_event_by_id` + `IterativeExecutor.start_from_fork(...)` entry point + `--input` override semantics + replay-then-continue wiring. NO CLI yet. | ~2 days |
| **Step 2** | `_cmd_fork(...)` CLI handler + subparser registration + sub-workflow fork handling (rely on existing replay; defensive tests). | ~1 day |
| **Step 3** | Golden trace fixtures + `bundles/kolsch/workflows/fork-demo.yaml` starter + Phase Tests 1-9 wired end-to-end. | ~1.5 days |

**Flow per step:** standard Coder/Reviewer cycle.

**When to make a strategic call:**

- Coder asks to relitigate a locked decision (fork-and-merge, automatic side-effect undo, payload extension instead of new event kind, multi-event-id ranges) → REJECT.
- Coder asks about `--input` override scope (per-node captured outputs vs workflow-level inputs) → the recommended call is **per-node captured outputs at the fork point**, per spec §9.1's "replaces captured outputs at the fork point." Document if the Coder presents a compelling alternative.
- Coder finds master-spec ambiguity → consult, make a binding call, append to THIS spec, re-brief.
- Coder discovers a prior-phase contract was wrong → flag as separate patch; do NOT widen 3.8's scope.
- Scope creep (chat `/fork`, concurrent fork, auto-retention, multi-tenant authz) → REJECT.

**Done when:** All 4 steps Approved AND all 9 Phase-level Integration Tests pass AND `phase3.x-roadmap.md` Phase 3.8 line annotated with the shipping SHA.

---

# STEP 0 — Plumbing + `workflow.run.forked_from` event + `kolsch workflows events` CLI (PREREQ)

**Goal**: ship the `ForkedFrom` value object, `WorkflowRun.forked_from` field, store extension, new event kind, and the `kolsch workflows events <run_id>` CLI command (operator prerequisite). NO fork logic yet.

**Files created/modified:**

- `src/schemas/entities.py` (modified) — add `ForkedFrom` value object.
- `src/workflows/run.py` (modified) — `WorkflowRun.forked_from` field.
- `src/workflows/run_store.py` (modified) — `create(*, forked_from=None)` kwarg; meta.json round-trip.
- `src/workflows/errors.py` (modified) — add `WorkflowRunNotFound` if absent (grep first; Phase 3.0 may have shipped it).
- `src/workflows/events.py` (modified) — `WorkflowEventKind.RUN_FORKED_FROM` + `WorkflowRunForkedFromEvent` + `WorkflowRunForkedFromPayload`.
- `src/cli/workflows.py` (modified) — add `events` subparser + dispatcher case + `_cmd_events(...)` handler.
- `tests/component/test_workflow_fork_prereq.py` (new) — coverage for the plumbing.
- `tests/component/test_workflow_events_cli.py` (new) — coverage for the events command.

**Effort**: ~1.5 days.

## Step 0 — Coder Prompt

> Reply "Step 0 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the plumbing + the new event kind + the `kolsch workflows events <run_id>` CLI command. NO fork logic yet (Step 1 owns it).

**Authoritative references — read BEFORE coding:**

1. `process/phase3.8-fork-build.md` — Step 0.
2. `process/phase3.8-fork-spec.md` — §9.1, §14.1, retention context.
3. `src/schemas/entities.py` — `ParentRunPointer` (Phase 3.7); mirror the shape.
4. `src/workflows/events.py:181-196` (or wherever) — `WorkflowRunResumedEvent` template.
5. `src/cli/workflows.py:_cmd_show` — CLI handler pattern; mirror for `_cmd_events`.

**What to implement:**

1. **`ForkedFrom`** in `src/schemas/entities.py`:

   ```python
   class ForkedFrom(BaseModel):
       """Phase 3.8 — parent-run pointer on forked runs.

       Mirrors the shape of :class:`ParentRunPointer` (Phase 3.7's
       sub-workflow pointer) and :class:`FromWorkflowRun` (Phase 3.2's
       agent-session pointer). The three patterns are intentionally
       symmetric.
       """

       model_config = ConfigDict(frozen=True, extra="forbid")

       run_id: WorkflowRunId
       fork_event_id: EventId
   ```

2. **`WorkflowRun.forked_from`** in `src/workflows/run.py`:

   ```python
   class WorkflowRun(BaseModel):
       ...  # existing
       forked_from: ForkedFrom | None = None
   ```

3. **`WorkflowRunStore.create(...)`** extension:

   ```python
   def create(
       self, *,
       workflow_ref: str,
       inputs: dict[str, Any] | None = None,
       trace_id: TraceId | None = None,
       parent: ParentRunPointer | None = None,  # from Phase 3.7
       forked_from: ForkedFrom | None = None,   # NEW Phase 3.8
   ) -> WorkflowRun:
       ...
   ```

   Both `parent` and `forked_from` are independent — a run can have either, both, or neither. Round-trip both through `meta.json`.

4. **`WorkflowRunNotFound`** — grep `src/workflows/errors.py` first. If absent:

   ```python
   class WorkflowRunNotFound(WorkflowRuntimeError):
       """Phase 3.8 — fork target's events.jsonl has been deleted."""

       caller_message: ClassVar[str] = (
           "The workflow run you're trying to fork from is no longer available. "
           "Share the run ID with your engineer."
       )
       identifying_names_keys: ClassVar[tuple[str, ...]] = ("run_id",)

       def __init__(self, *, run_id: str) -> None:
           self.run_id = run_id
           super().__init__(
               _runtime_str(
                   self,
                   problem=f"workflow run {run_id!r} not found or its events log has been deleted",
               )
           )
   ```

5. **New event kind** in `src/workflows/events.py`:

   ```python
   class WorkflowEventKind(StrEnum):
       ...
       RUN_FORKED_FROM = "workflow.run.forked_from"

   class WorkflowRunForkedFromPayload(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")
       forked_from_run_id: str
       fork_event_id: str
       input_overrides: dict[str, Any] = Field(default_factory=dict)

   class WorkflowRunForkedFromEvent(WorkflowEvent):
       kind: Literal[WorkflowEventKind.RUN_FORKED_FROM] = WorkflowEventKind.RUN_FORKED_FROM
       payload: WorkflowRunForkedFromPayload
   ```

   Mirror Phase 3.4's `WorkflowRunResumedEvent` shape.

6. **`kolsch workflows events <run_id>`** CLI command:

   - Subparser registration:

     ```python
     events_p = sub.add_parser("events", parents=[fmt_p], help="list events in a workflow run's JSONL")
     events_p.add_argument("run_id", help="the workflow run ID (wrk_*)")
     ```

   - Dispatcher case:

     ```python
     if args.subcommand == "events":
         return _cmd_events(args.run_id, args.fmt, out, err)
     ```

   - Handler:

     ```python
     def _cmd_events(run_id_str: str, fmt: str, out, err) -> int:
         store = _open_store()
         try:
             run = store.read(WorkflowRunId(run_id_str))
         except (WorkflowRunNotFound, FileNotFoundError):
             render_error(WorkflowRunNotFound(run_id=run_id_str), fmt=fmt, out=err)
             return 1
         events = list(store.read_events(WorkflowRunId(run_id_str)))
         if fmt == "json":
             rows = [
                 {
                     "event_id": e["event_id"],
                     "kind": e["kind"],
                     "ts": e["ts"],
                     "payload_preview": _preview_payload(e.get("payload", {})),
                 }
                 for e in events
             ]
             # Render via existing render helper
             ...
         else:  # text
             # Table format: event_id | kind | ts | preview
             ...
         return 0
     ```

   Payload preview truncates strings to ~50 chars; useful for operator-eyeballing event candidates.

7. **Tests in `tests/component/test_workflow_fork_prereq.py`**:
   - `ForkedFrom` frozen + extra=forbid.
   - `WorkflowRun.forked_from` defaults to `None`; round-trips via store.
   - `WorkflowRunStore.create(forked_from=)` persists in meta.json.
   - `WorkflowRunNotFound` raises and renders correctly.
   - `WorkflowRunForkedFromEvent` serializes/deserializes.
   - Both `parent` and `forked_from` can coexist on a single `WorkflowRun`.

8. **Tests in `tests/component/test_workflow_events_cli.py`**:
   - **List events:** run a workflow; `_cmd_events(run_id, "text", ...)`; assert stdout contains every event_id from the JSONL.
   - **JSON format:** assert returned JSON is a valid array; each entry has `event_id`/`kind`/`ts`/`payload_preview`.
   - **Run not found:** `_cmd_events("wrk_doesnotexist", ...)`; exit 1 with `WorkflowRunNotFound`.
   - **Run with no events yet:** newly-created run with empty JSONL; returns empty list cleanly.

**Step 0 Acceptance Criteria:**

0.1. `ForkedFrom` exists; frozen; carries `run_id`/`fork_event_id`.
0.2. `WorkflowRun.forked_from` field; backward-compat default `None`.
0.3. `WorkflowRunStore.create(forked_from=)` round-trips through meta.json.
0.4. `WorkflowRunNotFound` exists (or already extant from earlier phase) and serializes correctly through `render_error`.
0.5. `WorkflowEventKind.RUN_FORKED_FROM` + event class shipped.
0.6. `kolsch workflows events <run_id>` text format prints event table.
0.7. `kolsch workflows events <run_id> --format json` returns well-formed JSON array.
0.8. `events` command on a missing run exits 1 with `WorkflowRunNotFound`.
0.9. No fork logic yet (grep `src/workflows/executor.py` for `start_from_fork` returns ZERO matches; grep `src/cli/workflows.py` for `_cmd_fork` returns ZERO matches).
0.10. No regressions.

**Self-check before handoff:**

- [ ] All 10 ACs met
- [ ] Manual smoke: `kolsch workflows run kolsch/workflows/hello && kolsch workflows events <run_id>` works
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 0 Complete — [summary]".

## Step 0 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 0.3 — round-trip with `parent` also set. Phase 3.7's `parent` and Phase 3.8's `forked_from` are independent fields; both must persist correctly through meta.json.
- AC 0.6 — text format usability. The output should be operator-friendly; an event_id buried in a wall of payload data is unusable. The preview MUST truncate cleanly.
- AC 0.9 — Step 1 ownership. Grep both modules; ZERO fork logic at this step.

---

# STEP 1 — Fork point validation + replay + `IterativeExecutor.start_from_fork(...)`

**Goal**: ship `_find_event_by_id`, fork-point legality check, `IterativeExecutor.start_from_fork(...)` entry point, and `--input` override application. NO CLI yet.

**Files created/modified:**

- `src/workflows/_replay.py` (modified) — add `find_event_by_id(events_iter, target_id) -> dict | None`.
- `src/workflows/executor.py` (modified) — `IterativeExecutor.start_from_fork(*, fork_event_id, input_overrides)` method.
- `tests/component/test_workflow_fork_executor.py` (new) — coverage for the executor entry point.

**Effort**: ~2 days.

## Step 1 — Coder Prompt

> Reply "Step 1 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the executor's fork entry point. NO CLI yet (Step 2). The entry point is invokable from tests with a constructed `WorkflowRun` whose `forked_from` is populated.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.8-fork-build.md` — Step 1.
2. `process/phase3.8-fork-spec.md` — §9 (full).
3. `src/workflows/_replay.py` — current replay helpers.
4. `src/workflows/executor.py:resume(...)` — structural template.
5. `src/workflows/executor.py:_continue_worklist` — Phase 3.4's extraction.

**What to implement:**

1. **`find_event_by_id`** in `src/workflows/_replay.py`:

   ```python
   def find_event_by_id(
       events: Iterable[dict[str, Any]],
       target_id: str,
   ) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
       """Walk events; return (events_up_to_and_including_target, target_event) OR None.

       Used by Phase 3.8's fork to truncate parent's JSONL at the fork point.
       """
       collected: list[dict[str, Any]] = []
       for event in events:
           collected.append(event)
           if event.get("event_id") == target_id:
               return collected, event
       return None
   ```

2. **`IterativeExecutor.start_from_fork`** in `src/workflows/executor.py`:

   ```python
   async def start_from_fork(
       self,
       *,
       fork_event_id: str,
       input_overrides: dict[str, Any],
   ) -> WorkflowRunStatus:
       """Phase 3.8 — fork entry point.

       Algorithm per spec §9.1:
       1. Read parent's events; locate fork_event_id; truncate at it.
       2. Replay events to rebuild _captured / _state / _pruned / _completed_or_skipped.
       3. Apply input_overrides to the fork-point node's captured output.
       4. Module isolation snapshot (fresh process discipline).
       5. Write PID sidecar.
       6. Emit workflow.run.forked_from as the FIRST event.
       7. Re-enter _continue_worklist.
       """
       # 1. Sanity: this executor's run must have forked_from set.
       if self._run.forked_from is None:
           raise ValueError("start_from_fork requires WorkflowRun.forked_from to be set")

       # 2. Read parent's events; locate fork_event_id.
       parent_id = self._run.forked_from.run_id
       try:
           parent_run = self._store.read(parent_id)
       except (WorkflowRunNotFound, FileNotFoundError):
           raise WorkflowRunNotFound(run_id=str(parent_id))

       parent_events = list(self._store.read_events(parent_id))
       lookup = find_event_by_id(parent_events, fork_event_id)
       if lookup is None:
           raise WorkflowRuntimeError(
               f"fork_event_id {fork_event_id!r} not found in parent run {parent_id!r}'s "
               f"events.jsonl. If you copied this id from a chat capsule or agent session, "
               f"fork at the workflow-level event preceding the agent invocation instead."
           )
       events_to_replay, fork_event = lookup

       # 3. Replay events.
       self._module_snapshot = snapshot_sys_modules()
       open_invocations = replay_into(self, events_to_replay)

       # 4. Apply input_overrides — replace fork-point node's captured output keys.
       #    The fork event identifies WHICH node: for workflow.node.completed events,
       #    the event's node_name is the target. For workflow.run.started events,
       #    overrides go to workflow-level inputs (special-case: no node target).
       if fork_event["kind"] == "workflow.node.completed":
           target_node = fork_event["node_name"]
           if target_node in self._captured:
               # Merge overrides onto the captured output dict.
               for k, v in input_overrides.items():
                   self._captured[target_node][k] = v
       elif fork_event["kind"] == "workflow.run.started":
           # Overrides become workflow-level inputs for the forked run.
           # The forked run's WorkflowRun.inputs was already populated; merge.
           for k, v in input_overrides.items():
               self._run.inputs[k] = v
               self._state[k] = v  # state was initialized from inputs
       else:
           # Other event kinds (workflow.node.pruned, workflow.state.mutated, etc.)
           # are valid fork points but don't have an obvious "fork-point node" target
           # for overrides. If overrides are provided with a non-completed fork event,
           # raise — the operator probably wanted to fork at a completion event.
           if input_overrides:
               raise WorkflowRuntimeError(
                   f"--input overrides require forking at a workflow.node.completed "
                   f"or workflow.run.started event; fork event {fork_event_id!r} is "
                   f"kind {fork_event['kind']!r}"
               )

       # 5. Write PID sidecar.
       run_dir = self._store.directory(self._run.run_id)
       tmp_pid = run_dir.pid_path.with_suffix(".tmp")
       tmp_pid.write_text(str(os.getpid()), encoding="utf-8")
       tmp_pid.replace(run_dir.pid_path)

       # 6. Status flip → RUNNING.
       await self._store.update_status(self._run.run_id, WorkflowRunStatus.RUNNING)

       # 7. Emit workflow.run.forked_from as FIRST event.
       forked_event_id = emit_event(
           self._store,
           WorkflowRunForkedFromEvent(
               workflow_run_id=self._run.run_id,
               trace_id=self._run.trace_id,
               parent_event_id=None,  # FIRST event of the JSONL
               payload=WorkflowRunForkedFromPayload(
                   forked_from_run_id=str(parent_id),
                   fork_event_id=fork_event_id,
                   input_overrides=dict(input_overrides),
               ),
           ),
       )
       self._last_event_id = forked_event_id

       # 8. Re-enter the worklist from the fork point.
       #    The worklist sees _completed_or_skipped populated with everything up to
       #    the fork; it naturally fires the next-eligible nodes.
       return await self._continue_worklist(starting_from=None)
       # starting_from=None means "scan _completed_or_skipped and fire next-eligible";
       # the _continue_worklist contract already supports this (Phase 3.4 Step 1).
   ```

3. **Tests in `tests/component/test_workflow_fork_executor.py`**:
   - **Fork at completed event:** construct a parent run that completes A → B → C; create a forked `WorkflowRun(forked_from=...)`; invoke `start_from_fork(fork_event_id=<B's completed>, input_overrides={})`; assert only C runs in the fork; assert forked JSONL starts with `workflow.run.forked_from`.
   - **Fork with input override:** same setup but `input_overrides={"value": 42}`; assert C sees the overridden `nodes.B.value == 42` instead of B's original output.
   - **Fork at started event:** invoke with fork_event_id = parent's `workflow.run.started.event_id`; assert the whole workflow re-runs in the fork.
   - **Invalid fork_event_id:** invoke with `fork_event_id="ev_doesnotexist"`; assert `WorkflowRuntimeError` with the expected message.
   - **Parent JSONL deleted:** invoke after deleting parent's `events.jsonl`; assert `WorkflowRunNotFound`.
   - **trace_id inheritance:** assert the forked run's events all carry the parent's `trace_id`.
   - **Both parent and forked_from set:** construct a forked run that also has a `parent` (it was originally spawned by a sub-workflow); assert `start_from_fork` doesn't conflict with the parent pointer.

**Step 1 Acceptance Criteria:**

1.1. `find_event_by_id(events, target_id)` returns `(prefix_list, target_event)` or `None`.
1.2. `start_from_fork(fork_event_id, input_overrides)` reads parent's events; truncates at fork point.
1.3. `WorkflowRunNotFound` raised when parent's events.jsonl is missing.
1.4. `WorkflowRuntimeError` raised when fork_event_id not in parent's JSONL; message guides user.
1.5. Replay via existing `replay_into(self, events_to_replay)` reuses all Phase 3.4/3.5/3.7 handlers.
1.6. `--input` overrides correctly applied to fork-point node's captured output (or workflow inputs for fork-at-started).
1.7. `workflow.run.forked_from` event is the FIRST event of the forked JSONL; `parent_event_id` is `None`.
1.8. `trace_id` inherited from parent unchanged.
1.9. `_continue_worklist(starting_from=None)` re-enters cleanly; only nodes downstream of the fork point fire.
1.10. PID sidecar atomically written.
1.11. NO CLI wiring yet (grep `_cmd_fork` in `src/cli/workflows.py` returns ZERO matches).
1.12. No regressions.

**Self-check before handoff:**

- [ ] All 12 ACs met
- [ ] All test scenarios pass
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 1 Complete — [summary]".

## Step 1 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 1.7 — first-event invariant. Read the JSONL produced by a test; confirm `workflow.run.forked_from` is line 1. A `workflow.run.started` ahead of it would break the resumed-vs-forked event-stream consumer contract.
- AC 1.5 — replay reuse. Read `_replay.py`; confirm Phase 3.5's state events + Phase 3.7's sub-workflow events ARE handled (not stubbed). A missing handler silently drops state on fork.
- AC 1.6 — override application. Test fixture: parent has B output `{value: 10, label: "x"}`; fork at B with `--input value=42`. Expect `_captured["B"] == {value: 42, label: "x"}` (label preserved, value overridden). NOT `_captured["B"] == {value: 42}` (which would drop label).

---

# STEP 2 — `_cmd_fork` CLI handler + sub-workflow handling

**Goal**: wire the CLI command + verify sub-workflow fork behavior end-to-end.

**Files created/modified:**

- `src/cli/workflows.py` (modified) — add `fork` subparser + dispatcher case + `_cmd_fork(...)` handler.
- `tests/component/test_workflow_fork_cli.py` (new) — CLI integration tests including sub-workflow fork.

**Effort**: ~1 day.

## Step 2 — Coder Prompt

> Reply "Step 2 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the CLI command + sub-workflow fork verification.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.8-fork-build.md` — Step 2.
2. `process/phase3.8-fork-spec.md` §9.3 (sub-workflow forking) + §14.1 (CLI surface).
3. `src/cli/workflows.py:_cmd_resume` — structural template.

**What to implement:**

1. **Subparser** registration:

   ```python
   fork_p = sub.add_parser("fork", parents=[fmt_p], help="fork a workflow run from a specific event")
   fork_p.add_argument("run_id", help="the parent workflow run ID (wrk_*)")
   fork_p.add_argument("--at", required=True, dest="fork_event_id", help="the event ID to fork at (ev_*)")
   fork_p.add_argument("--input", action="append", default=[], dest="inputs", metavar="K=V", help="input override (repeatable)")
   ```

2. **Dispatcher case**:

   ```python
   if args.subcommand == "fork":
       return _cmd_fork(args.run_id, args.fork_event_id, args.inputs, args.fmt, out, err)
   ```

3. **`_cmd_fork`** handler:

   ```python
   def _cmd_fork(parent_id_str, fork_event_id, input_pairs, fmt, out, err) -> int:
       overrides = _parse_input_pairs(input_pairs)
       store = _open_store()
       try:
           parent_run = store.read(WorkflowRunId(parent_id_str))
       except (WorkflowRunNotFound, FileNotFoundError):
           render_error(WorkflowRunNotFound(run_id=parent_id_str), fmt=fmt, out=err)
           return 1
       # Load the same workflow the parent used.
       workflow = _load_workflow(parent_run.workflow_ref)
       # Create the forked run with parent pointer + inherited trace_id.
       forked_run = store.create(
           workflow_ref=parent_run.workflow_ref,
           inputs=dict(parent_run.inputs),
           trace_id=parent_run.trace_id,
           forked_from=ForkedFrom(
               run_id=parent_run.run_id,
               fork_event_id=fork_event_id,
           ),
       )
       # Construct executor + invoke start_from_fork.
       catalog = _open_catalog()
       registry = _build_registry(...)
       executor = IterativeExecutor(workflow=workflow, run=forked_run, store=store, registry=registry, catalog=catalog)
       try:
           status = asyncio.run(executor.start_from_fork(
               fork_event_id=fork_event_id,
               input_overrides=overrides,
           ))
       except WorkflowRunNotFound as exc:
           render_error(exc, fmt=fmt, out=err)
           return 1
       except WorkflowRuntimeError as exc:
           render_error(exc, fmt=fmt, out=err)
           return 1
       # Per spec §14.3, success envelope:
       # { new_run_id, parent_run_id, fork_event_id }
       if fmt == "json":
           render_json(
               {
                   "new_run_id": str(forked_run.run_id),
                   "parent_run_id": str(parent_run.run_id),
                   "fork_event_id": fork_event_id,
                   "status": str(status),
               },
               out=out,
           )
       else:
           print(f"Forked run created: {forked_run.run_id}", file=out)
           print(f"Forked from: {parent_run.run_id} at {fork_event_id}", file=out)
           print(f"Final status: {status}", file=out)
       return 0 if status == WorkflowRunStatus.SUCCEEDED else 1
   ```

4. **Tests in `tests/component/test_workflow_fork_cli.py`**:
   - **Fork at completed CLI:** end-to-end smoke; `_cmd_fork` returns 0; forked run exists with correct `forked_from`.
   - **Fork with --input:** `--input label=urgent`; verify downstream node sees override.
   - **Invalid event_id CLI:** `--at ev_doesnotexist`; exit 1.
   - **Spawned-session event_id rejected:** create a workflow that spawns an agent session; copy a session-side event_id; attempt to fork at it; assert exit 1 with the message guiding to fork at the workflow-level event.
   - **Parent JSONL deleted:** rm parent's events.jsonl; fork; exit 1 with `WorkflowRunNotFound`.
   - **Sub-workflow fork AFTER child:** parent calls child; fork AFTER the workflow-call node's `completed` event; assert the forked run uses the parent's captured child output (no new child run created).
   - **Sub-workflow fork BEFORE child:** fork BEFORE the workflow-call node's `started` event; assert the forked run spawns a FRESH child workflow_run.
   - **JSON output envelope:** `--format json` returns `{new_run_id, parent_run_id, fork_event_id}` per spec §14.3.
   - **Help text includes side-effect discipline note:** assert `--help` output contains language about side effects not being undone.
   - **Forked run can be paused + resumed:** forked run has user_input downstream; pauses; resume via `kolsch workflows resume <forked_run_id>` works correctly.
   - **Re-fork compose:** fork a forked run; verify `forked_from.run_id` is the IMMEDIATE parent (the previous fork), not the original run.

**Step 2 Acceptance Criteria:**

2.1. `kolsch workflows fork <run_id> --at <event_id>` subcommand registered; appears in `--help`.
2.2. `--input k=v` parsed via existing `_parse_input_pairs`.
2.3. CLI success exits 0 with `{new_run_id, parent_run_id, fork_event_id}` JSON envelope.
2.4. CLI failure paths exit 1 with appropriate error class.
2.5. Sub-workflow fork AFTER child reuses captured child output (no new child run).
2.6. Sub-workflow fork BEFORE child creates fresh child run.
2.7. Forked run can be paused + resumed via Phase 3.4 machinery (transitive correctness; no new code).
2.8. Re-fork chains correctly (`forked_from.run_id` = immediate parent).
2.9. `--help` text includes the side-effect-discipline note.
2.10. No regressions.

**Self-check before handoff:**

- [ ] All 10 ACs met
- [ ] Manual smoke: fork happy path + invalid event_id path + sub-workflow fork path on a fresh workspace
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 2 Complete — [summary]".

## Step 2 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 2.5 — sub-workflow fork AFTER child. Verify by inspecting the forked run's `runs/` directory: there should be NO new child wrk_id directory. The parent's captured child output is canonical.
- AC 2.7 — pause/resume on forked run. The forked run uses the SAME pause/resume machinery; verify via test that a `user_input` downstream of the fork point pauses correctly and resume completes the forked run (not the original parent).
- AC 2.8 — re-fork chains. Re-fork a forked run; the `forked_from.run_id` of the re-fork should point at the FIRST fork's run_id, not at the ORIGINAL parent's run_id. This is operator-visible via `kolsch workflows show <re_fork_run_id>`.

---

# STEP 3 — Goldens + starter workflow + Phase Tests

**Goal**: ship reference JSONLs for fork scenarios + starter workflow + Phase Tests 1-9 wired.

**Files created/modified:**

- `bundles/kolsch/workflows/fork-demo.yaml` (new) — simple 3-node pipeline ideal for fork experimentation.
- `tests/golden/runs/` (new files):
  - `fork-at-completed.parent.jsonl` + `fork-at-completed.fork.jsonl`.
  - `fork-at-started.fork.jsonl`.
  - `fork-with-input-override.fork.jsonl`.
  - `fork-subworkflow-after.fork.jsonl`.
- `tests/golden/runs_test.py` (modified) — add fork fixtures.
- `tests/component/test_workflow_fork_e2e.py` (new) — Phase Tests 1-9 wired.

**Effort**: ~1.5 days.

## Step 3 — Coder Prompt

> Reply "Step 3 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship goldens + starter + Phase Tests.

**What to implement:**

1. **`bundles/kolsch/workflows/fork-demo.yaml`** — minimal fork-experimentable workflow:

   ```yaml
   inputs:
     seed: { type: str }
   nodes:
     classify:
       type: kolsch/nodes/compute
       value: "{{ 'urgent' if 'urgent' in inputs.seed else 'normal' }}"
     route:
       type: kolsch/nodes/branch
       condition: "{{ nodes.classify.value == 'urgent' }}"
     handle_urgent:
       type: kolsch/nodes/bash
       command: "echo 'URGENT: {{ inputs.seed }}'"
     handle_normal:
       type: kolsch/nodes/bash
       command: "echo 'normal: {{ inputs.seed }}'"
   graph:
     start: classify
     edges:
       - { from: classify, to: route }
       - { from: route, to: handle_urgent, branch: true }
       - { from: route, to: handle_normal, branch: false }
   ```

   This workflow is ideal for fork-experimentation: an operator can fork at `classify`'s completed event and override `value: urgent` (or `normal`) to re-roll the routing decision.

2. **Four golden JSONLs** — capture parent run + fork run for each scenario; canonicalize volatile fields per the Phase 3.1 substitution table; commit as goldens. The fork goldens prove the cross-JSONL state replay is correct.

3. **`runs_test.py` extension** — add the fork fixtures.

4. **`test_workflow_fork_e2e.py`** — wire Phase Tests 1-9 (Test 1 is `events` command, Test 9 is regression).

**Step 3 Acceptance Criteria:**

3.1. `fork-demo.yaml` parses, validates, runs end-to-end.
3.2. Four golden JSONLs at `tests/golden/runs/`.
3.3. `runs_test.py` asserts semantic equality.
3.4. `test_workflow_fork_e2e.py` covers Phase Tests 1-9.
3.5. Manual smoke: `kolsch workflows run kolsch/workflows/fork-demo --input seed="urgent issue"` runs; `events <run_id>` shows the JSONL; `fork <run_id> --at <classify_completed_event> --input value=normal` produces a forked run that routes to `handle_normal`.
3.6. No regressions.

**Handoff trigger:** All ACs met → "Step 3 Complete — [summary]".

## Step 3 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 3.5 — manual smoke end-to-end. The complete chain (run → events → fork → resume) IS the user-facing experience; verify each transition works.
- AC 3.2 — golden canonicalization. Parent + fork run share `trace_id`; the canonicalization MUST use the same sentinel for both files.

---

## Done when

- All 4 steps Approved by Reviewer.
- All 9 Phase-level Integration Tests pass.
- `process/phase3.x-roadmap.md` Phase 3.8 line annotated with the shipping SHA.
- Manual smoke test:

  ```bash
  uv run kolsch --root /tmp/kolsch-fresh setup --non-interactive
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/fork-demo --input seed="urgent issue"
  # ... handles urgent. Note <run_id>.
  uv run kolsch --root /tmp/kolsch-fresh workflows events <run_id>
  # ... copy classify's completed event_id.
  uv run kolsch --root /tmp/kolsch-fresh workflows fork <run_id> --at <classify_completed_id> --input value=normal
  # ... forked run handles normal, despite the original input saying "urgent".
  # Both runs share trace_id; both JSONLs exist; original is untouched.
  ```
