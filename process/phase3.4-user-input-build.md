# [SPEC-005] Phase 3.4 — User Input pause/resume

**Type:** spec
**Doc #:** 005
**Status:** Draft
**Created:** 2026-06-27

---

## Glossary

| Term | Meaning |
| --- | --- |
| pause signal | The mechanism by which the user_input runner tells the executor "stop here, do not emit completion, exit cleanly." This phase introduces `WorkflowPausedForInput` as the typed signal. |
| open invocation | The user_input node's `workflow.node.started` event with no matching `completed` / `failed` / `timed_out` event in the JSONL. The marker that input is awaited (spec §10.1). |
| paused run | A workflow run whose status is `waiting_for_input`. The executor process has exited; the JSONL is durable on disk. |
| resume | The act of loading a paused run, supplying the user's response, completing the open invocation, and continuing execution. Triggered by `kolsch workflows resume <run_id> --input response=<text>`. |
| replay | Rebuilding `_captured` and `_state` on resume by walking the JSONL — for each `workflow.node.completed`, restore its payload into `_captured[<node_name>]`; for each `set_state` / `delete_state` (Phase 3.5 future), mutate `_state`. At Phase 3.4 only `workflow.node.completed` replays. |

---

## Background

Phase 3.0 shipped the parser/validator including `UserInputNodeDef` (`src/workflows/parser.py:258-264`) and `UserInputInputs` (parser.py:134-136). Phase 3.1 shipped:

- `WorkflowRunStatus.WAITING_FOR_INPUT` enum value (`src/workflows/run.py:42-56`) — already present.
- `WorkflowRunStatus.terminal_set()` (`run.py:58-67`) returning {SUCCEEDED, FAILED, TIMED_OUT, ABORTED} — explicitly EXCLUDES `WAITING_FOR_INPUT` so tail polling terminates correctly at pause.
- `WorkflowRunStore` API (`src/workflows/run_store.py`): `read`, `read_events`, `append_event`, `tail`, `directory` — all stable; no new methods needed.
- `BUILTIN_PHASE_MAP["user_input"] == "3.4"` (`src/workflows/executor.py:82-102`) — the Phase 3.4 registration shadows the `NodeTypeNotImplemented` raise.
- PID sidecar discipline (`runs/<wrk_id>/pid`) — orphan sweep reads it. Phase 3.4 overwrites on resume.

What 3.1 LEFT for 3.4:

1. **The pause signal** — `WorkflowPausedForInput` is the typed signal the `UserInputRunner` raises to tell `IterativeExecutor` "stop without emitting completion." NEW exception class (subclass of `Exception`, NOT `WorkflowError` — it's a control-flow signal, not a failure).
2. **`UserInputRunner`** — the runner for `kolsch/nodes/user_input`. Renders the `prompt:` via Jinja, then raises `WorkflowPausedForInput`. Does NOT return.
3. **Pause handling in the worklist** — the executor catches `WorkflowPausedForInput`, flips run status to `WAITING_FOR_INPUT`, runs the module-isolation cleanup, exits cleanly. Does NOT emit `workflow.run.failed` / `workflow.run.completed`.
4. **Resume entry point on `IterativeExecutor`** — `async resume(run_id, response_dict)` that loads the run, replays prior `workflow.node.completed` events into `_captured` + `_state`, finds the open user_input invocation, emits its `workflow.node.completed` with the response, then continues the worklist from the next node.
5. **CLI `kolsch workflows resume <run_id> --input response=<text>`** — new subcommand. Reuses `_parse_input_pairs` (already factored at `src/cli/workflows.py:597-611`).

The verification gate is a three-node workflow `prep → ask → echo` where `ask` is a `user_input` node. First invocation pauses cleanly (process exits, JSONL ends with `ask`'s started event). `kolsch workflows resume <run_id> --input response="hello"` completes the invocation and the workflow runs to terminal. Second fixture: two sequential `user_input` nodes (`ask1` → `ask2`) prove sequential-pause works. Third fixture: `resume` on a run NOT in `waiting_for_input` rejects with a clear error.

**This spec breaks the phase into 4 sequential steps (Step 0 — Step 3)**, each with its own Coder and Reviewer cycle. The phase is "~1 week" per `phase3.x-roadmap.md`; each step is small.

Related docs:

- `process/phase3.4-user-input-spec.md` — the master design spec (§4.1.3 node type, §10 pause/resume mechanism, §10.3 nested pauses, §10.4 single-user assumption, §14.1 CLI surface).
- `process/phase3.1-executor-build.md` — predecessor build plan; particularly Step 4 (cancellation/orphan sweep — the pause discipline mirrors its module-isolation cleanup contract).
- `process/phase3.0-workflow-spec.md` §4.1.3 — locked user_input contract.
- `docs/explanation/workflow-event-log.md` — event log conventions (resume emits a new event with `parent_event_id` chained to the user_input node's started event).

---

## Scope

**In scope (delivered across the 4 steps):**

- New exception class `WorkflowPausedForInput(Exception)` in `src/workflows/errors.py` (it's NOT a `WorkflowError` subclass — pause is normal control flow, not failure). Carries `node_name: str`, `invocation_id: str`, `prompt: str` (the rendered prompt, useful for chat surfaces in Phase 3.9).
- New module `src/workflows/builtins/user_input.py` — `UserInputRunner` implementing `NodeRunner`. Renders `node_def.prompt` via the executor's `_render_jinja` (with `inputs_overlay=bound_inputs` per Phase 3.3 §6.4), then raises `WorkflowPausedForInput`.
- Registration of `UserInputRunner` in `src/workflows/builtins/__init__.py:register_builtins()` against `kolsch/nodes/user_input`. `NodeTypeNotImplemented(phase="3.4")` stops firing for this slot.
- Pause handling in `IterativeExecutor.run()` — catch `WorkflowPausedForInput` from the worklist's runner-invocation try block. Flip status to `WAITING_FOR_INPUT`. Persist the open invocation's id in a sidecar file at `runs/<wrk_id>/waiting_at` so resume doesn't have to walk the JSONL backwards. Run module-isolation cleanup. Exit cleanly with status `waiting_for_input`. Do NOT emit `workflow.run.completed` / `workflow.run.failed`.
- Resume entry point `IterativeExecutor.resume(run_id, response_dict)` (or a dedicated `ResumeExecutor`-shaped wrapper — Step 1's design call). The resume path:
  - Reads the run + the JSONL.
  - Replays prior `workflow.node.completed` payloads into `_captured` (the source of truth for downstream Jinja).
  - For Phase 3.4 only `workflow.node.completed` events replay; `workflow.node.pruned` events from Phase 3.3 also need to be replayed into `_pruned` + `_completed_or_skipped` so the cascade survives resume.
  - Reads `runs/<wrk_id>/waiting_at` to identify the open invocation; emits `workflow.node.completed` for that invocation with payload `{node_name, invocation_id, output: response_dict}`.
  - Re-enters the worklist starting from the user_input node's outgoing edges; runs to terminal status.
- Bind validation on resume — the user's `--input` dict is validated against the user_input node's output schema (the spec at §4.1.3 locks `{response: str}`). Mismatch (missing `response` key, wrong type) raises `WorkflowInputInvalid` (existing class at `src/workflows/errors.py`).
- CLI `kolsch workflows resume <run_id> --input k=v` — new subcommand in `src/cli/workflows.py`. Reuses `_parse_input_pairs`. Refuses with exit 1 + a clear message if the run is not in `waiting_for_input` state.
- PID sidecar discipline — on resume, write the new process's PID to `runs/<wrk_id>/pid` atomically before re-entering the worklist. On pause, the previous PID is left stale; the orphan sweep's check is gated by status (status=`waiting_for_input` skips the sweep — paused runs are NOT orphans).
- Module isolation discipline — on resume, take a FRESH `snapshot_sys_modules()`; on terminal status (including resume's terminal), the cleanup removes only modules loaded during this resume process's lifetime. Symmetric with initial-run discipline.
- Orphan sweep update — `src/workflows/run.py` (or wherever the sweep lives, Phase 3.1 Step 4) — skip runs with status `waiting_for_input` from the sweep. The pause exited cleanly; the absent PID is correct, not orphaned.
- Sequential pause support — a workflow with two user_input nodes (`ask1` → `process → ask2`) pauses on `ask1`, resumes, pauses on `ask2`, resumes, completes. Each resume re-uses the same machinery; nothing per-node-specific.
- `workflow.run.resumed` event — NEW event kind emitted as the FIRST event of every resume invocation (before the user_input's completion event). Payload: `{run_id, resumed_at_invocation_id}`. Useful for forensics and the Phase 3.9 chat capsule that visually marks resumed runs.
- Golden trace fixtures at `tests/golden/runs/` for: single-pause workflow (JSONL spans two process invocations); sequential-pause workflow; failed-resume (wrong-status) error envelope.
- Starter workflow `bundles/kolsch/workflows/hello-paused.yaml` runs end-to-end via pause + manual resume.

**Out of scope (deferred per `phase3.x-roadmap.md`):**

- **Chat reply path** (Phase 3.9). The CLI `resume` is v1. The same `IterativeExecutor.resume(...)` machinery will be reused by the chat surface; no chat changes ship in 3.4.
- **Sub-workflow nested pauses** — covered by spec §10.3 conceptually, but the `workflow` node runtime ships in Phase 3.7. At Phase 3.4 a `user_input` inside a sub-workflow can't be exercised (no sub-workflows yet). The machinery is forward-compatible: the leaf executor's pause discipline applies uniformly regardless of nesting depth, and ancestor executors stay `running` per the spec.
- **Resume authentication** (S7 / spec §10.4) — single-user filesystem-permission boundary stands. No `--as <user>`, no resume tokens.
- **`read_file` / `write_file` / `list_files` / `set_state` / `delete_state`** (Phase 3.5) — these don't pause; they replay their captured outputs on resume just like any other completed node. The replay machinery is forward-compatible (it walks every `workflow.node.completed` regardless of type).
- **Timeout on a paused run** — `Workflow.timeout_seconds` from Phase 3.1 explicitly PAUSES the workflow timer during `waiting_for_input` per master spec §2.6. The workflow timer logic in Phase 3.1's executor reads the elapsed wall-clock; Phase 3.4 must NOT decrement that timer during the pause-to-resume interval. Confirmed at Step 1's review (no change needed if the timer is measured at fire-time of the next node — there is no "background timer" that runs during pause).
- **Concurrent resume** (Phase 3.10) — two operators running `resume` simultaneously is undefined at v1. The PID file write is atomic; whichever resumes second observes the run already in `running` and rejects. Documented but not test-covered.

**Decisions explicitly locked in `process/phase3.4-user-input-spec.md` (do NOT relitigate):**

- **The open invocation IS the pause marker.** No separate `workflow.run.paused_for_input` event. The user_input node's `workflow.node.started` event without a matching completion IS the signal.
- **Resume input key is `response`.** The CLI's `--input response="<text>"` is the canonical shape; the runner's output is `{response: str}`. Mismatch raises `WorkflowInputInvalid`.
- **Filesystem-permission security.** No resume tokens at v1; the workspace directory's POSIX permissions are the boundary.
- **Status is sticky until resume.** `waiting_for_input` does not auto-time-out or self-clear. Operator must `resume` or `cancel`.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2, ruamel.yaml, Jinja2 (`JINJA_ENV` at `src/workflows/jinja_env.py`), pytest. Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/workflows/parser.py:258-264` — `UserInputNodeDef`. Already correctly shaped from Phase 3.0; Phase 3.4 does NOT modify the parser.
- `src/workflows/parser.py:134-136` — `UserInputInputs(extra="allow")`. Permissive input policy; Phase 3.4 trusts.
- `src/workflows/executor.py:82-102` — `BUILTIN_PHASE_MAP`. Phase 3.4's registration shadows the `NodeTypeNotImplemented` raise.
- `src/workflows/executor.py:101-119` — `NodeRunner` protocol. `UserInputRunner` satisfies it (raises `WorkflowPausedForInput` instead of returning).
- `src/workflows/executor.py:249-300` — `IterativeExecutor.__init__`. Phase 3.4 adds `_waiting_at_path` and (optionally) a separate `_resume_mode: bool` flag.
- `src/workflows/executor.py:403-490` (the worklist body) — Phase 3.4 inserts a `WorkflowPausedForInput` catch in the runner-invocation try block.
- `src/workflows/executor.py:617-670` — `_render_jinja` extended in Phase 3.3 with `inputs_overlay=`. Phase 3.4 uses it for `prompt:` rendering.
- `src/workflows/run.py:42-67` — `WorkflowRunStatus` + `terminal_set()`. Already correctly exclude `WAITING_FOR_INPUT`.
- `src/workflows/run.py:70-92` — `WorkflowRun`. No new fields needed; pause state lives in the `waiting_at` sidecar.
- `src/workflows/run_store.py:48-270` — `WorkflowRunStore`. Phase 3.4 adds NO new methods; uses existing `read` / `read_events` / `append_event` / `directory`.
- `src/cli/workflows.py:160-199` — `cmd_workflows` dispatcher. Phase 3.4 adds the `resume` case.
- `src/cli/workflows.py:597-611` — `_parse_input_pairs`. Phase 3.4 reuses verbatim.
- `src/workflows/builtins/__init__.py` — `register_builtins(...)`. Phase 3.4 extends.
- `src/workflows/events.py:182-185` — `WorkflowNodeCompletedEvent`. Phase 3.4 emits it on resume.
- `src/workflows/module_isolation.py:1-100` — `snapshot_sys_modules` / `cleanup_bundle_imports`. Phase 3.4 calls both on the resume process's lifetime.

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch

# Bootstrap a fresh workspace for smoke testing
uv run kolsch --root /tmp/kolsch-userinput-test setup --non-interactive

# Run the test suite
uv run pytest tests/component/ -q
uv run pytest tests/golden/ -q
```

---

## Phase-level Acceptance Criteria

- A three-node workflow `prep → ask → echo` (where `ask` is `user_input`) pauses on `ask`: process exits cleanly; run status is `waiting_for_input`; JSONL ends with `ask`'s `workflow.node.started` and NO completion event; the `waiting_at` sidecar file contains the open invocation_id.
- `kolsch workflows resume <run_id> --input response="hello"` completes `ask` with `{response: "hello"}` and fires `echo`. Final status `succeeded`.
- A workflow with two sequential `user_input` nodes (`ask1 → process → ask2`) pauses → resume → pauses → resume → terminal. Each pause writes a fresh `waiting_at`; the second resume overwrites the first's PID.
- `kolsch workflows resume <run_id>` against a run NOT in `waiting_for_input` (e.g., `succeeded` / `failed` / `running`) exits 1 with `WorkflowResumeRejected` (NEW error class — flagged below) and a clear human message.
- Resume's input dict missing `response` OR `response` of wrong type raises `WorkflowInputInvalid`; run exits 1; status NOT updated (remains `waiting_for_input` so the operator can retry with a correct response).
- The orphan sweep does NOT mark `waiting_for_input` runs as orphaned, even though the PID may be stale.
- `workflow.run.resumed` is emitted as the FIRST event of the resume invocation, BEFORE the user_input's completion event.
- Replay correctly reconstructs `_captured` (every `workflow.node.completed` payload's `output` lands at `_captured[<node_name>]`) and `_pruned` (every `workflow.node.pruned` event marks its node in `_pruned` + `_completed_or_skipped`).
- Downstream nodes that reference `{{ nodes.prep.<key> }}` AFTER resume render correctly — the replay made it visible.
- Module isolation works across the pause boundary: a bundle module loaded during the pre-pause execution is gone from `sys.modules` at process exit; the resume process re-loads it fresh and cleans up at its own exit.
- The `bundles/kolsch/workflows/hello-paused.yaml` starter runs end-to-end via pause + manual resume on a fresh workspace.
- No regressions: non-paused workflows produce byte-identical JSONLs (modulo the new `workflow.run.resumed` event class definition — non-paused workflows never emit it).

---

## Phase-level Integration Tests

### Phase Test 1: Single pause + resume
- **Run:** `uv run kolsch --root /tmp/kolsch-userinput-test workflows run kolsch/workflows/hello-paused` (terminates with status `waiting_for_input`); then `uv run kolsch --root /tmp/kolsch-userinput-test workflows resume <run_id> --input response="world"`.
- **Expect:** first invocation exits 0 with status `waiting_for_input` printed; resume exits 0 with final outputs JSON printed; JSONL is a complete event stream spanning both processes; `_waiting_at` sidecar cleaned up at terminal status.
- **Fail means:** pause discipline broken OR resume entry point broken.

### Phase Test 2: Sequential pauses
- **Run:** `uv run pytest tests/component/test_workflow_user_input.py::test_sequential_pauses -v`
- **Expect:** workflow with two `user_input` nodes pauses-resumes-pauses-resumes to terminal; both responses appear in the captured output dict.
- **Fail means:** resume's worklist re-entry doesn't handle subsequent pauses correctly.

### Phase Test 3: Resume rejected when not waiting
- **Run:** `uv run pytest tests/component/test_workflow_user_input.py::test_resume_rejected_when_running -v`
- **Expect:** running `resume` on a `succeeded` run exits 1 with `WorkflowResumeRejected` in the JSON error envelope; the run's JSONL is UNCHANGED (no spurious events appended).
- **Fail means:** resume command doesn't gate on status.

### Phase Test 4: Replay reconstructs state
- **Run:** `uv run pytest tests/component/test_workflow_user_input.py::test_replay_reconstructs_captured -v`
- **Expect:** a workflow `prep → ask → echo` where `echo`'s `command: "echo {{ nodes.prep.stdout }} {{ inputs.response }}"` produces output containing both prep's stdout AND the resume response. Confirms replay's `_captured` reconstruction works.
- **Fail means:** replay machinery missing OR `nodes.*` scope on resume drops captured data.

### Phase Test 5: Bad-input rejected without status update
- **Run:** `uv run pytest tests/component/test_workflow_user_input.py::test_bad_resume_input_preserves_paused_status -v`
- **Expect:** resume with `--input wrong_key=foo` exits 1 with `WorkflowInputInvalid`; run status remains `waiting_for_input`; operator retry with `--input response=foo` succeeds.
- **Fail means:** status update happens before input validation.

### Phase Test 6: Orphan sweep ignores paused runs
- **Run:** `uv run pytest tests/component/test_workflow_user_input.py::test_orphan_sweep_skips_waiting -v`
- **Expect:** a paused run whose PID is dead is NOT reconciled by the orphan sweep. Status remains `waiting_for_input`.
- **Fail means:** orphan sweep's status check missing.

### Phase Test 7: No regressions
- **Run:** `uv run pytest tests/ -q`
- **Expect:** full suite green. Non-paused workflows produce byte-identical JSONLs.

---

## Coordinator Prompt

> You are the strategic decision-maker for this task. You do not write or review code.
> Your job is to drive the 4-step Coder/Reviewer flow, make calls when there is ambiguity
> or disagreement, and unblock the work when it stalls.

**Your task:**

Ship Phase 3.4 of the kolsch workflow system through 4 sequential Coder/Reviewer cycles. The phase's purpose is the **pause/resume lifecycle**: the first phase that exits a workflow process cleanly mid-run and resurrects it from disk later. If the JSONL-as-source-of-truth + replay discipline is wrong, we discover it here.

**Authoritative references:**

- THIS spec: `process/phase3.4-user-input-build.md`.
- Master design spec: `process/phase3.4-user-input-spec.md` — §4.1.3 + §10 + §14.
- Phase 3.1 build plan: `process/phase3.1-executor-build.md` — Step 4 (cancellation + orphan sweep — the pause discipline mirrors its cleanliness contract).
- Phase 3.0 master spec: `process/phase3.0-workflow-spec.md` §4.1.3 — locked decisions.

**The 4 steps:**

| Step | Focus | Effort |
| --- | --- | --- |
| **Step 0** | **PREREQ** — `WorkflowPausedForInput` signal class + `WorkflowResumeRejected` error class + `UserInputRunner` + pause handling in the worklist (catch the signal; write `waiting_at` sidecar; flip status; clean exit). NO resume entry point yet. | ~1.5 days |
| **Step 1** | Resume entry point on `IterativeExecutor` — `async resume(run_id, response_dict)`; JSONL replay of `workflow.node.completed` + `workflow.node.pruned`; emit `workflow.run.resumed` + user_input's `workflow.node.completed`; re-enter worklist. Bind validation on resume. | ~2 days |
| **Step 2** | CLI `kolsch workflows resume <run_id> --input k=v` + orphan sweep update (skip `waiting_for_input`) + PID sidecar refresh on resume. | ~1 day |
| **Step 3** | Golden trace fixtures (single pause + sequential pauses + bad-resume error envelope) + `bundles/kolsch/workflows/hello-paused.yaml` starter + Phase Tests 1-6 wired. | ~1.5 days |

**Flow per step:**

1. Hand the Coder the section titled **"Step N — Coder Prompt"** verbatim. Coder reads it + the master spec sections it references.
2. Coder implements + replies "Step N Complete — [summary]", "Blocked — ...", or "Decision needed — ...".
3. On Complete: hand the Reviewer the section titled **"Step N — Reviewer Prompt"** verbatim. Reviewer verifies + replies "Step N Approved" or "Changes requested — ...".
4. On Approved: advance to Step N+1. On Changes requested: send the change list back to the Coder.
5. After Step 3 is Approved: run the **Phase-level Integration Tests** above. If all pass, mark this spec Status: Done and annotate `phase3.x-roadmap.md`'s Phase 3.4 line with shipping commit SHA.

**When to make a strategic call:**

- Coder asks to relitigate a master-spec-locked decision (resume tokens, multi-user authn, alternate pause-signal mechanism) → REJECT, cite the spec, restate.
- Coder finds master-spec ambiguity → consult master spec, make a binding call, append the call as a note to THIS spec, re-brief.
- Coder discovers Phase 3.0 / 3.1 / 3.3 contract was wrong and needs amendment → flag as a separate patch on this branch; do NOT widen Phase 3.4's scope.
- Scope creep (chat reply path, sub-workflow runtime, resume tokens, multi-user) → REJECT and pull back.
- The pause-signal mechanism (`WorkflowPausedForInput` raise vs sentinel return vs executor-special-cases-the-runner) — DEFAULT to the raise approach as documented; if the Coder presents a compelling alternative AND its review surface is smaller, accept and document.

**Done when:** All 4 steps Approved by Reviewer AND all 7 Phase-level Integration Tests pass AND `phase3.x-roadmap.md` Phase 3.4 line annotated with the shipping SHA.

---

# STEP 0 — Pause signal + UserInputRunner + worklist pause handling (PREREQ)

**Goal**: ship the pause signal mechanism end-to-end up to pause. The user_input node fires; the runner raises `WorkflowPausedForInput`; the executor catches, writes the `waiting_at` sidecar, flips status, cleans up modules, exits. No resume yet (Step 1 owns that).

**Files created/modified:**

- `src/workflows/errors.py` (modified) — add `WorkflowPausedForInput(Exception)` (NOT a `WorkflowError` subclass) and `WorkflowResumeRejected(WorkflowRuntimeError)`.
- `src/workflows/builtins/user_input.py` (new) — `UserInputRunner` class.
- `src/workflows/builtins/__init__.py` (modified) — `register_builtins(...)` registers the user_input runner.
- `src/workflows/executor.py` (modified) — catch `WorkflowPausedForInput` in the worklist; write `waiting_at` sidecar; flip status; clean exit.
- `src/workflows/run.py` (modified) — `RunDirectory.waiting_at_path` computed property.
- `tests/component/test_workflow_user_input_pause.py` (new) — pause path tests via a fake runner.

**Effort**: ~1.5 days.

## Step 0 — Coder Prompt

> You are implementing Step 0 of a 4-step phase. PREREQ that gates Steps 1-3.
> Reply "Step 0 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the pause signal end-to-end up to pause. The user_input node fires; the runner raises `WorkflowPausedForInput`; the executor catches the signal cleanly (NOT as a failure), writes `runs/<wrk_id>/waiting_at`, flips run status to `waiting_for_input`, runs module-isolation cleanup, exits. NO resume entry point yet.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.4-user-input-build.md` — Step 0.
2. `process/phase3.4-user-input-spec.md` — §10.1 (pause flow).
3. `process/phase3.0-workflow-spec.md` §4.1.3 — user_input contract.
4. `src/workflows/executor.py:101-119` — `NodeRunner` protocol.
5. `src/workflows/executor.py:403-490` — worklist body (the existing failure-catching try/except).
6. `src/workflows/parser.py:258-264` — `UserInputNodeDef` (already exists).

**What to implement:**

1. **`WorkflowPausedForInput`** in `src/workflows/errors.py` — NEW signal class, NOT a `WorkflowError` subclass:

   ```python
   class WorkflowPausedForInput(Exception):  # noqa: N818 — control flow signal, not an error
       """Phase 3.4 — the user_input runner's pause signal.

       NOT a WorkflowError subclass: pause is normal control flow,
       not failure. The executor's failure path (which emits
       workflow.node.failed + workflow.run.failed) does NOT handle
       this — the worklist catches it separately.
       """

       def __init__(
           self,
           *,
           node_name: str,
           invocation_id: str,
           prompt: str,
       ) -> None:
           self.node_name = node_name
           self.invocation_id = invocation_id
           self.prompt = prompt
           super().__init__(
               f"workflow pause for input at node {node_name!r} "
               f"(invocation {invocation_id!r})"
           )
   ```

2. **`WorkflowResumeRejected`** in `src/workflows/errors.py` — NEW runtime error:

   ```python
   class WorkflowResumeRejected(WorkflowRuntimeError):
       """Phase 3.4 — resume against a run not in waiting_for_input."""

       caller_message: ClassVar[str] = (
           "This workflow can't be resumed right now — it isn't paused. "
           "Check the run's status with `kolsch workflows show`."
       )
       identifying_names_keys: ClassVar[tuple[str, ...]] = ("run_id", "status")

       def __init__(self, *, run_id: str, status: str) -> None:
           self.run_id = run_id
           self.status = status
           super().__init__(
               _runtime_str(
                   self,
                   problem=(
                       f"workflow run {run_id!r} cannot be resumed "
                       f"(status={status!r}; expected 'waiting_for_input')"
                   ),
               )
           )
   ```

3. **`UserInputRunner`** in `src/workflows/builtins/user_input.py`:

   ```python
   class UserInputRunner:
       """Runtime for ``kolsch/nodes/user_input`` — Phase 3.4 §10.1."""

       async def run(
           self,
           *,
           node_name: str,
           node_def: UserInputNodeDef,
           bound_inputs: dict[str, Any],
           state: dict[str, Any],
           ctx: NodeContext,
       ) -> dict[str, Any]:
           # Render the prompt via the executor's _render_jinja so
           # the inputs.* overlay (Phase 3.3) is honored.
           prompt = ctx.render_jinja(
               node_def.prompt,
               inputs_overlay=bound_inputs,
           )
           raise WorkflowPausedForInput(
               node_name=node_name,
               invocation_id=str(ctx.invocation_id),
               prompt=prompt,
           )
   ```

   **Note:** `ctx.render_jinja(...)` is a NEW method on `NodeContext` exposing the executor's `_render_jinja` to runners. Add it in this step:

   ```python
   @dataclass(slots=True)
   class NodeContext:
       ...
       _render_jinja_callback: Callable[[str, dict[str, Any] | None], Any] | None = None

       def render_jinja(self, expression: str, *, inputs_overlay: dict[str, Any] | None = None) -> Any:
           if self._render_jinja_callback is None:
               raise RuntimeError("NodeContext has no Jinja renderer bound")
           return self._render_jinja_callback(expression, inputs_overlay)
   ```

   The executor's `_build_ctx` populates the callback. Bash and other runners may use this same hook in the future (Phase 3.6 onward); for Phase 3.4 only user_input consumes it.

4. **`RunDirectory.waiting_at_path`** in `src/workflows/run.py`:

   ```python
   @dataclass(frozen=True, slots=True)
   class RunDirectory:
       root: Path

       @property
       def waiting_at_path(self) -> Path:
           return self.root / "waiting_at"
       # existing properties unchanged
   ```

5. **Worklist pause handling** in `src/workflows/executor.py` — extend the runner-invocation try block (executor.py:428-476):

   ```python
   try:
       runner = self._registry.get(...)
       runner = _apply_node_modifiers(runner, node_def=node_def)
       output = await runner.run(...)
   except WorkflowPausedForInput as pause:
       # Phase 3.4 — pause discipline. DO NOT emit workflow.node.failed.
       # The workflow.node.started event for this invocation is already
       # in the JSONL; the absence of a completion event IS the pause
       # marker. Write the waiting_at sidecar so resume can locate the
       # open invocation without walking the JSONL.
       run_dir = self._store.directory(self._run.run_id)
       tmp = run_dir.waiting_at_path.with_suffix(".tmp")
       tmp.write_text(pause.invocation_id, encoding="utf-8")
       tmp.replace(run_dir.waiting_at_path)
       # Status update.
       await self._store.update_status(self._run.run_id, WorkflowRunStatus.WAITING_FOR_INPUT)
       # Module isolation cleanup — same as terminal status (paused
       # processes exit cleanly).
       cleanup_bundle_imports(before=self._module_snapshot, workspace_root=self._workspace_root)
       return WorkflowRunStatus.WAITING_FOR_INPUT
   except Exception as exc:  # noqa: BLE001 — existing failure path
       # ... existing code unchanged ...
   ```

   **`WorkflowRunStore.update_status(run_id, status)`** — NEW method (or use whatever the existing store API offers; `WorkflowRunStore` may already update status via meta.json — check `src/workflows/run_store.py` for the existing pattern). If no clean method exists, add one that writes the status to meta.json atomically.

6. **`register_builtins(...)` extension** in `src/workflows/builtins/__init__.py`:

   ```python
   def register_builtins(registry: RunnerRegistry, ...) -> None:
       # existing registrations unchanged
       registry.register("kolsch/nodes/user_input", UserInputRunner())
   ```

   The `UserInputRunner` has no constructor args — it doesn't depend on the catalog, engine, or session store. Simplest registration.

7. **Tests in `tests/component/test_workflow_user_input_pause.py`**:
   - **Pause emits started + no completion:** workflow with `prep → ask`; run it; assert JSONL has `workflow.node.started` for `ask` and NO `workflow.node.completed` for `ask`; assert status is `WAITING_FOR_INPUT`; assert `waiting_at` sidecar contains the right invocation_id.
   - **Status flips, no failed event:** assert JSONL has NO `workflow.node.failed` and NO `workflow.run.failed` after pause.
   - **Module cleanup runs:** authored Python node loaded before the pause; assert its module is gone from `sys.modules` after pause exit.
   - **Multiple pauses in one run:** workflow with `ask1 → process → ask2`; run pauses at `ask1`; sidecar carries `ask1`'s invocation_id. (Step 1 wires resume; this test only verifies the first pause.)
   - **Prompt Jinja rendering:** workflow with `prep → ask` where `ask.prompt: "How to handle {{ nodes.prep.stdout }}?"`; assert `WorkflowPausedForInput.prompt` field contains the rendered prompt.

**Step 0 Acceptance Criteria:**

0.1. `WorkflowPausedForInput` exists; NOT a `WorkflowError` subclass; carries `node_name` / `invocation_id` / `prompt`.
0.2. `WorkflowResumeRejected` exists; subclass of `WorkflowRuntimeError`; identifying names = `(run_id, status)`.
0.3. `UserInputRunner.run(...)` renders `prompt:` via `ctx.render_jinja` then raises `WorkflowPausedForInput`.
0.4. `NodeContext.render_jinja(expression, *, inputs_overlay=)` method added; populated by executor's `_build_ctx`.
0.5. `RunDirectory.waiting_at_path` computed property exists.
0.6. Executor catches `WorkflowPausedForInput` BEFORE the generic failure catch; writes `waiting_at` sidecar atomically; flips status; runs module cleanup; exits with `WAITING_FOR_INPUT`.
0.7. No `workflow.node.failed` / `workflow.run.failed` emitted on pause.
0.8. `register_builtins(...)` registers `UserInputRunner` against `kolsch/nodes/user_input`.
0.9. NO resume entry point shipped yet (Step 1 owns it).

**Self-check before handoff:**

- [ ] All 9 ACs met
- [ ] `grep resume src/workflows/executor.py` returns ZERO matches (Step 1 owns it)
- [ ] All test cases pass
- [ ] No regressions: `uv run pytest tests/ -q` green

**Handoff trigger:** All boxes checked → "Step 0 Complete — [summary]".

## Step 0 — Reviewer Prompt

> Reply "Step 0 Approved" or "Changes requested — [list]".

Verify each AC. PAY EXTRA ATTENTION to:

- AC 0.1 — `WorkflowPausedForInput` MUST inherit from `Exception`, NOT `WorkflowError`. Read the class definition. The whole pause-isn't-failure design hinges on this: if pause is a `WorkflowError`, the existing failure path catches it and emits `workflow.node.failed`.
- AC 0.6 — catch ordering. Read the worklist source: the `WorkflowPausedForInput` catch MUST come before the generic `except Exception`. Otherwise the failure path wins.
- AC 0.7 — assert by reading a generated JSONL after a pause run. Confirm zero failed-event kinds.
- AC 0.4 — `NodeContext.render_jinja`. Confirm it's bound by the executor (not a default-stub that raises). The user_input runner depends on it.

---

# STEP 1 — Resume entry point + JSONL replay + bind validation

**Goal**: ship the resume entry point on `IterativeExecutor`. Reads the run + JSONL; replays prior `workflow.node.completed` / `workflow.node.pruned` events into `_captured` + `_pruned` + `_completed_or_skipped`; emits `workflow.run.resumed` + the user_input's `workflow.node.completed`; re-enters the worklist starting from the user_input's outgoing edges; runs to terminal.

**Files created/modified:**

- `src/workflows/executor.py` (modified) — `async resume(...)` method.
- `src/workflows/events.py` (modified) — `WorkflowRunResumedEvent` Pydantic model.
- `src/workflows/_replay.py` (new) — `replay_into(executor, events)` helper that walks events and rebuilds `_captured` / `_pruned` / `_completed_or_skipped`.
- `tests/component/test_workflow_user_input_resume.py` (new) — resume path tests.

**Effort**: ~2 days.

## Step 1 — Coder Prompt

> Reply "Step 1 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the resume entry point. The CLI (Step 2) will call it.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.4-user-input-build.md` — Step 1.
2. `process/phase3.4-user-input-spec.md` — §10.2 (resume flow).
3. `src/workflows/executor.py:249-300` — `IterativeExecutor.__init__`. The resume method reuses construction + state init.
4. `src/workflows/run_store.py:142-180` — `append_event` + `read_events`.

**What to implement:**

1. **`WorkflowRunResumedEvent`** in `src/workflows/events.py` — mirror existing event-class shape:

   ```python
   class WorkflowRunResumedEvent(WorkflowEvent):
       kind: Literal[WorkflowEventKind.RUN_RESUMED] = WorkflowEventKind.RUN_RESUMED
       payload: WorkflowRunResumedPayload

   class WorkflowRunResumedPayload(BaseModel):
       resumed_at_invocation_id: str  # the user_input invocation being completed
   ```

   Add `RUN_RESUMED = "workflow.run.resumed"` to `WorkflowEventKind`.

2. **`src/workflows/_replay.py`** — new helper:

   ```python
   def replay_into(
       executor: IterativeExecutor,
       events: Iterable[dict[str, Any]],
   ) -> dict[str, str | None]:
       """Walk historical events; rebuild _captured / _pruned / _completed_or_skipped.

       Returns a mapping {node_name: invocation_id_or_None} of nodes
       known to have been started. The user_input node whose
       invocation has no matching completed event is the open
       invocation.
       """
       started: dict[str, str | None] = {}
       for event in events:
           kind = event.get("kind")
           if kind == "workflow.node.started":
               started[event["node_name"]] = event["invocation_id"]
           elif kind == "workflow.node.completed":
               # Restore captured + completion mark.
               executor._captured[event["node_name"]] = dict(event["payload"]["output"])
               executor._completed_or_skipped.add(event["node_name"])
               started[event["node_name"]] = None  # closed
           elif kind == "workflow.node.pruned":
               # Phase 3.3 pruning replay.
               executor._pruned.add(event["payload"]["node_name"])
               executor._completed_or_skipped.add(event["payload"]["node_name"])
               started[event["payload"]["node_name"]] = None
           elif kind == "workflow.node.failed":
               # The run had failed; shouldn't have reached resume.
               raise WorkflowResumeRejected(
                   run_id=str(executor._run.run_id),
                   status="failed",
               )
       return started
   ```

   The function MUTATES the executor's private fields directly — coupled but simple. Document the coupling in the function's docstring; the alternative (a typed snapshot model) is over-engineered for this scope.

3. **`IterativeExecutor.resume(...)`** in `src/workflows/executor.py`:

   ```python
   async def resume(self, *, response: dict[str, Any]) -> WorkflowRunStatus:
       """Resume a paused run with the user's response.

       The CLI loads the run via WorkflowRunStore.read, constructs
       this executor with the loaded run + workflow + store, then
       awaits resume(response=...).
       """
       # 1. Status check.
       if self._run.status != WorkflowRunStatus.WAITING_FOR_INPUT:
           raise WorkflowResumeRejected(
               run_id=str(self._run.run_id),
               status=str(self._run.status),
           )
       # 2. Module isolation snapshot — fresh process, fresh discipline.
       self._module_snapshot = snapshot_sys_modules()
       # 3. Replay historical events.
       events = list(self._store.read_events(self._run.run_id))
       open_invocations = replay_into(self, events)
       # 4. Find the open user_input invocation via waiting_at sidecar.
       run_dir = self._store.directory(self._run.run_id)
       waiting_invocation_id = run_dir.waiting_at_path.read_text(encoding="utf-8").strip()
       # 5. Cross-check: walk open_invocations to find the matching node_name.
       open_node = None
       for node_name, inv_id in open_invocations.items():
           if inv_id == waiting_invocation_id:
               open_node = node_name
               break
       if open_node is None:
           raise WorkflowResumeRejected(
               run_id=str(self._run.run_id),
               status="inconsistent_state",
           )
       # 6. Validate the response against the user_input node's output schema.
       #    Master spec §4.1.3 locks {response: str}. Missing or wrong-type → WorkflowInputInvalid.
       if "response" not in response or not isinstance(response["response"], str):
           raise WorkflowInputInvalid(
               ref=str(self._run.run_id),
               reason=(
                   "resume response must include a 'response' string; "
                   f"received {list(response.keys())!r}"
               ),
           )
       # 7. Status flip to RUNNING.
       await self._store.update_status(self._run.run_id, WorkflowRunStatus.RUNNING)
       # 8. Emit workflow.run.resumed.
       resumed_event_id = emit_event(
           self._store,
           WorkflowRunResumedEvent(
               workflow_run_id=self._run.run_id,
               trace_id=self._run.trace_id,
               parent_event_id=self._last_event_id_from_jsonl_tail(),
               payload=WorkflowRunResumedPayload(
                   resumed_at_invocation_id=waiting_invocation_id,
               ),
           ),
       )
       # 9. Emit workflow.node.completed for the paused user_input.
       completed_payload = {
           "node_name": open_node,
           "invocation_id": waiting_invocation_id,
           "output": dict(response),
       }
       completed_event_id = emit_event(
           self._store,
           WorkflowNodeCompletedEvent(
               workflow_run_id=self._run.run_id,
               trace_id=self._run.trace_id,
               parent_event_id=resumed_event_id,
               invocation_id=InvocationId(waiting_invocation_id),
               node_name=open_node,
               payload=completed_payload,
           ),
       )
       self._captured[open_node] = dict(response)
       self._completed_or_skipped.add(open_node)
       self._last_event_id = completed_event_id
       # 10. Re-enter the worklist. The user_input's outgoing edges will fire
       #     downstream nodes; everything from here is the same as initial-run flow.
       return await self._continue_worklist(starting_from=open_node)
   ```

   **`_continue_worklist(starting_from=)`** — extract the worklist body from `run()` into a private method so both `run()` and `resume()` invoke it. `run()` calls `_continue_worklist(starting_from=<start_node>)`; `resume()` calls it with the user_input node name (the dispatcher's outgoing-edge expander handles the rest).

   **`_last_event_id_from_jsonl_tail()`** — read the last event in the JSONL; return its `event_id`. Used to populate `parent_event_id` on the `workflow.run.resumed` event so the chain is unbroken across processes.

4. **Cleanup on resume's terminal status** — at terminal status (succeeded/failed/timed_out/aborted), the `waiting_at` sidecar should be removed (it's stale). Wire this into the existing terminal-status cleanup path in `run()` / `_continue_worklist()`:

   ```python
   if run_dir.waiting_at_path.exists():
       run_dir.waiting_at_path.unlink()
   ```

5. **Tests in `tests/component/test_workflow_user_input_resume.py`**:
   - **Resume happy path:** pause a workflow (uses Step 0 machinery); call `resume(response={"response": "hello"})`; assert final status `SUCCEEDED`; assert JSONL has `workflow.run.resumed` + `workflow.node.completed` for the user_input node + downstream events.
   - **Resume rejected when not waiting:** run the workflow to completion (no pause); call `resume(...)`; assert `WorkflowResumeRejected` raises with `status="succeeded"`.
   - **Replay reconstructs captured:** workflow `prep → ask → echo`; pause; resume with `response="world"`; assert `echo`'s rendered command (which references `{{ nodes.prep.stdout }}`) interpolates correctly — confirms replay restored `_captured["prep"]`.
   - **Bad response key:** call `resume(response={"wrong_key": "x"})`; assert `WorkflowInputInvalid` raises; assert run's JSONL is UNCHANGED (no spurious events); assert status remains `WAITING_FOR_INPUT`.
   - **Resume with replayed pruning:** workflow `bash → user_input` where `bash` branches false to `user_input` (the user_input is pruned). The run reaches a terminal status without pausing. (Edge case — confirms replay handles pruning before pause discovery.)

**Step 1 Acceptance Criteria:**

1.1. `WorkflowRunResumedEvent` shipped; event kind `workflow.run.resumed`.
1.2. `replay_into(executor, events)` correctly restores `_captured` / `_pruned` / `_completed_or_skipped`.
1.3. `IterativeExecutor.resume(response={...})` flows through: status check → replay → response validation → emit resumed event → emit node.completed → worklist continue.
1.4. Resume against non-waiting status raises `WorkflowResumeRejected`.
1.5. Bad response (missing `response` key or wrong type) raises `WorkflowInputInvalid`; status UNCHANGED (still `WAITING_FOR_INPUT`).
1.6. `parent_event_id` chains correctly across the pause boundary (resumed-event's parent is the last JSONL tail event).
1.7. `waiting_at` sidecar removed at terminal status post-resume.
1.8. `_continue_worklist(starting_from=)` extracted from `run()`; both `run()` and `resume()` invoke it.

**Self-check before handoff:**

- [ ] All 8 ACs met
- [ ] Resume happy path produces a complete, well-formed JSONL spanning both processes
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 1 Complete — [summary]".

## Step 1 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 1.2 — replay correctness. Read `_replay.py`; confirm it handles `workflow.node.pruned` (Phase 3.3's event). A missing-pruning-replay leads to a downstream re-firing of pruned nodes on resume.
- AC 1.5 — status PRESERVED on bad input. Read the resume source: the response-validation check MUST come BEFORE the status flip to `RUNNING`. Otherwise the operator's retry-with-correct-input fails because the run is no longer in `WAITING_FOR_INPUT`.
- AC 1.6 — `parent_event_id` chain. Inspect a generated JSONL; the `workflow.run.resumed` event's `parent_event_id` should be the last `workflow.node.started` (or whatever the JSONL tail was at pause).
- AC 1.8 — extraction. Confirm `run()` and `resume()` share the same continuation logic; bugs introduced in one should manifest in the other.

---

# STEP 2 — CLI `kolsch workflows resume` + orphan sweep + PID refresh

**Goal**: wire the CLI command + update the orphan sweep to skip `waiting_for_input` runs + write the PID sidecar on resume.

**Files created/modified:**

- `src/cli/workflows.py` (modified) — add `resume` subparser, dispatcher case, and `_cmd_resume(...)` handler.
- `src/workflows/run_store.py` (or wherever the orphan sweep lives) — skip `waiting_for_input` runs.
- `src/workflows/executor.py` (modified) — `resume(...)` writes PID sidecar before the worklist re-entry.
- `tests/component/test_workflow_resume_cli.py` (new) — CLI integration tests.

**Effort**: ~1 day.

## Step 2 — Coder Prompt

> Reply "Step 2 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the CLI `kolsch workflows resume <run_id> --input k=v` command + the orphan-sweep update + the PID refresh.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.4-user-input-build.md` — Step 2.
2. `process/phase3.4-user-input-spec.md` — §14.1 (CLI surface).
3. `src/cli/workflows.py:87-199` — existing subparser pattern; mirror.
4. `src/cli/workflows.py:597-611` — `_parse_input_pairs` (reuse verbatim).
5. Whatever module implements the orphan sweep (Phase 3.1 Step 4 owned it; likely `src/workflows/run_store.py` or `src/workflows/orphan_sweep.py`).

**What to implement:**

1. **Subparser** in `src/cli/workflows.py`:

   ```python
   resume_p = sub.add_parser("resume", parents=[fmt_p], help="resume a paused workflow run")
   resume_p.add_argument("run_id", help="the workflow run ID (wrk_*)")
   resume_p.add_argument("--input", action="append", default=[], dest="inputs", metavar="K=V", help="user input (repeatable)")
   ```

2. **Dispatcher case** in `cmd_workflows`:

   ```python
   if args.subcommand == "resume":
       return _cmd_resume(args.run_id, args.inputs, args.fmt, out, err)
   ```

3. **`_cmd_resume(run_id, input_pairs, fmt, out, err)`**:

   ```python
   def _cmd_resume(run_id_str: str, input_pairs: list[str], fmt: str, out, err) -> int:
       response = _parse_input_pairs(input_pairs)
       store = _open_store()
       run = store.read(WorkflowRunId(run_id_str))
       workflow = _load_workflow(run.workflow_ref)
       catalog = _open_catalog()
       registry = _build_registry(...)
       executor = IterativeExecutor(workflow=workflow, run=run, store=store, registry=registry, catalog=catalog)
       try:
           status = asyncio.run(executor.resume(response=response))
       except WorkflowResumeRejected as exc:
           render_error(exc, fmt=fmt, out=err)
           return 1
       except WorkflowInputInvalid as exc:
           render_error(exc, fmt=fmt, out=err)
           return 1
       # Render success per Phase 3.1's _cmd_run pattern.
       ...
       return 0 if status == WorkflowRunStatus.SUCCEEDED else 1
   ```

4. **PID sidecar refresh** in `IterativeExecutor.resume(...)` — right after the status check and before the worklist re-entry:

   ```python
   run_dir = self._store.directory(self._run.run_id)
   tmp_pid = run_dir.pid_path.with_suffix(".tmp")
   tmp_pid.write_text(str(os.getpid()), encoding="utf-8")
   tmp_pid.replace(run_dir.pid_path)
   ```

5. **Orphan sweep update** — find the sweep function (Phase 3.1 Step 4 shipped it; likely a function in `src/workflows/run_store.py` like `reconcile_orphans` or similar). Add a status check:

   ```python
   def reconcile_orphans(...) -> None:
       for run_id in <all runs>:
           run = store.read(run_id)
           if run.status == WorkflowRunStatus.WAITING_FOR_INPUT:
               continue  # Phase 3.4 — paused runs are not orphans
           if run.status != WorkflowRunStatus.RUNNING:
               continue
           # ... existing PID-check logic ...
   ```

6. **Tests in `tests/component/test_workflow_resume_cli.py`**:
   - **Resume CLI happy path:** pause a workflow via the CLI; invoke `cmd_workflows(["resume", run_id, "--input", "response=hello"], ...)`; assert exit 0; assert final status succeeded.
   - **Resume wrong status:** invoke resume on a succeeded run; assert exit 1; assert JSON error envelope has `WorkflowResumeRejected`.
   - **Resume bad input:** invoke resume on a paused run with `--input wrong=x`; assert exit 1; assert `WorkflowInputInvalid`.
   - **PID refresh:** assert `runs/<wrk_id>/pid` after resume contains the resume process's PID (i.e., the test's own PID since the test runs the executor in-process).
   - **Orphan sweep skips paused:** seed a paused run with a dead PID; run the sweep; assert status stays `WAITING_FOR_INPUT`.

**Step 2 Acceptance Criteria:**

2.1. `kolsch workflows resume <run_id> --input k=v` parses + dispatches; reuses `_parse_input_pairs`.
2.2. Successful resume exits 0; rejected/invalid resume exits 1 with the right error class in the envelope.
2.3. `--format text` (default) + `--format json` both work for success and error paths.
2.4. PID sidecar atomically refreshed on resume.
2.5. Orphan sweep skips `WAITING_FOR_INPUT` runs.
2.6. Help text lists `resume` alongside `run`/`list`/`show`/`validate`/`cancel`.
2.7. No regressions.

**Self-check before handoff:**

- [ ] All 7 ACs met
- [ ] Manual smoke: pause + resume via CLI on a fresh workspace (Step 3 ships the starter, but a hand-crafted test workspace works)
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 2 Complete — [summary]".

## Step 2 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 2.5 — orphan sweep status check. Read the sweep source; the skip-paused check MUST come before the PID-aliveness check. A paused run with a stale PID is normal, not orphan.
- AC 2.4 — atomic write. Read the resume source; the PID write must use `tmp + rename`, not direct write (interrupted write leaves a corrupt PID file).
- AC 2.6 — `--help`. Run `kolsch workflows --help` and confirm `resume` appears.

---

# STEP 3 — Golden trace fixtures + starter demo + integration tests

**Goal**: ship reference JSONL goldens for the single-pause + sequential-pause scenarios + the starter workflow `hello-paused.yaml` + Phase Tests 1-6 wired.

**Files created/modified:**

- `bundles/kolsch/workflows/hello-paused.yaml` (new) — `prep → ask → echo` starter demo.
- `bundles/kolsch/workflows/two-pauses.yaml` (new) — `ask1 → process → ask2` sequential pauses.
- `tests/golden/runs/user-input-single-pause.workflow.jsonl` (new) — golden for single pause + resume.
- `tests/golden/runs/user-input-sequential-pauses.workflow.jsonl` (new) — golden for two pauses.
- `tests/golden/runs/user-input-rejected-resume.workflow.jsonl` (new) — wait, this one's a CLI error envelope, not a workflow JSONL — use `tests/golden/cli/` instead OR generate it inline in the integration test.
- `tests/golden/runs_test.py` (modified) — extend with pause/resume fixtures (semantic equality across the cross-process JSONL).
- `tests/component/test_workflow_user_input_e2e.py` (new) — Phase Tests 1-6 wired end-to-end.

**Effort**: ~1.5 days.

## Step 3 — Coder Prompt

> Reply "Step 3 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the goldens + starter + Phase Tests.

**What to implement:**

1. **`bundles/kolsch/workflows/hello-paused.yaml`** — minimal pause/resume demo:

   ```yaml
   inputs:
     greeting: { type: str }
   nodes:
     prep:
       type: kolsch/nodes/bash
       command: "echo '{{ inputs.greeting }}'"
     ask:
       type: kolsch/nodes/user_input
       prompt: "Reply to: {{ nodes.prep.stdout }}"
     echo:
       type: kolsch/nodes/bash
       command: "echo 'response={{ inputs.response }}'"
   graph:
     start: prep
     edges:
       - { from: prep, to: ask }
       - { from: ask, to: echo, binds: { response: "{{ nodes.ask.response }}" } }
   ```

2. **`bundles/kolsch/workflows/two-pauses.yaml`** — sequential-pause fixture:

   ```yaml
   nodes:
     ask1:
       type: kolsch/nodes/user_input
       prompt: "First answer?"
     process:
       type: kolsch/nodes/bash
       command: "echo 'first={{ inputs.r1 }}'"
     ask2:
       type: kolsch/nodes/user_input
       prompt: "Second answer (given first was {{ nodes.ask1.response }})?"
     done:
       type: kolsch/nodes/bash
       command: "echo 'first={{ nodes.ask1.response }} second={{ inputs.r2 }}'"
   graph:
     start: ask1
     edges:
       - { from: ask1, to: process, binds: { r1: "{{ nodes.ask1.response }}" } }
       - { from: process, to: ask2 }
       - { from: ask2, to: done, binds: { r2: "{{ nodes.ask2.response }}" } }
   ```

3. **Golden JSONLs** — run the workflows once (pause + resume); canonicalize volatile fields per the Phase 3.1 substitution table; commit as goldens. The cross-process nature means a single JSONL spans BOTH process invocations — the canonicalization should NOT distinguish process boundaries; the JSONL is one file.

4. **`runs_test.py` extension** — add the new fixtures to the parametrized golden runner; the test harness must drive the pause + resume flow programmatically (use `IterativeExecutor.resume(...)` directly).

5. **`test_workflow_user_input_e2e.py`** — wire Phase Tests 1-6 from the master spec.

**Step 3 Acceptance Criteria:**

3.1. `bundles/kolsch/workflows/hello-paused.yaml` parses, validates, runs end-to-end (pause + manual resume).
3.2. `bundles/kolsch/workflows/two-pauses.yaml` parses, validates, runs through both pauses.
3.3. Three golden JSONLs at `tests/golden/runs/` cover single-pause, sequential-pauses, and rejected-resume scenarios.
3.4. `runs_test.py` asserts semantic equality on the goldens.
3.5. `test_workflow_user_input_e2e.py` covers Phase Tests 1-6.
3.6. Manual smoke: `uv run kolsch --root /tmp/kolsch-userinput-test workflows run kolsch/workflows/hello-paused --input greeting=hi` pauses; `uv run kolsch ... workflows resume <run_id> --input response="world"` completes successfully.
3.7. No regressions.

**Handoff trigger:** All ACs met → "Step 3 Complete — [summary]".

## Step 3 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 3.6 — manual smoke. RUN IT. The CLI path is the user-facing surface; an in-process test doesn't exercise the cross-process JSONL durability + `waiting_at` sidecar disk-roundtrip.
- AC 3.3 — golden canonicalization. Volatile fields (`ts`, all `*Id` prefix-typed values) MUST be the SAME sentinels across the pause boundary — a sloppy canonicalizer that resets sentinels per-process makes the chain look broken even when it isn't.

---

## Done when

- All 4 steps Approved by Reviewer.
- All 7 Phase-level Integration Tests pass.
- `process/phase3.x-roadmap.md` Phase 3.4 line annotated with the shipping SHA.
- Manual smoke test:

  ```bash
  uv run kolsch --root /tmp/kolsch-fresh setup --non-interactive
  uv run kolsch --root /tmp/kolsch-fresh workflows run kolsch/workflows/hello-paused --input greeting=hi
  # Read printed run_id; status is waiting_for_input.
  ls /tmp/kolsch-fresh/runs/wrk_*/  # expect events.jsonl + pid + waiting_at sidecars
  cat /tmp/kolsch-fresh/runs/wrk_*/waiting_at  # expect the inv_<uuid>
  uv run kolsch --root /tmp/kolsch-fresh workflows resume <run_id> --input response="world"
  # Status now succeeded; waiting_at sidecar removed.
  cat /tmp/kolsch-fresh/runs/wrk_*/events.jsonl  # one continuous JSONL with workflow.run.resumed mid-stream
  ```
