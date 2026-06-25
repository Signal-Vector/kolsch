# Phase 3.4 — User Input (Pause/Resume)

This spec covers Phase 3.4 — user_input pause/resume. Depends on Phase 3.1 executor.

## 4.1.3 `user_input` (node type)

- Config: `prompt: str` (Jinja)
- Inputs: anything referenced in `prompt:`
- Outputs: `{response: str}`
- Branching: no
- Pause mechanism: the engine emits `workflow.node.started` for this node but no completion event; workflow_run status flips to `waiting_for_input`. The open invocation IS the marker that input is awaited (no separate pause event). Resume via `kolsch workflows resume` or chat reply completes the invocation with `{response: <text>}` and execution continues. See §10 for details.

### Catalog entries (cross-refs from §4.0, §4.0.1, §4.0.2 of `process/phase3.0-workflow-spec.md`)

- §4.0 built-in label and output shape: `kolsch/nodes/user_input` | `Wait for input` | `{response: str}`
- §4.0.2 `Inputs(BaseModel)` model: `kolsch/nodes/user_input` | `UserInputInputs` | `extra="allow"` | strict-typed fields: none

### Status-state reference (from §2.4.1 of `docs/explanation/workflow-foundation.md`)

`waiting_for_input` — A `user_input` node fired and is awaiting completion. Status is sticky until `kolsch workflows resume` (or chat reply) supplies the response, at which point the user_input node's invocation completes and execution continues.

---

## 10. Pause / Resume

### 10.1 Pause flow

1. `user_input` node fires; engine emits `workflow.node.started` for it (carrying the prompt).
2. No completion event yet — the node hasn't completed.
3. Workflow_run status flips to `waiting_for_input`.
4. Engine halts; process can exit. The JSONL ends with the started-but-not-completed user_input event.

No separate `workflow.run.paused_for_input` event. The user_input node's open invocation IS the marker that input is awaited.

### 10.2 Resume flow

- **CLI**: `kolsch workflows resume <run_id> --input response="<text>"` — engine reads back the JSONL, finds the open user_input invocation, completes it by emitting `workflow.node.completed` with `{response: "<text>"}` as the captured output, then continues execution.
- **Chat**: the user's next chat message becomes the response automatically (chat surface tracks the open user_input invocation).

### 10.3 Nested pauses

When a sub-workflow's `user_input` fires, the leaf child's status becomes `waiting_for_input`. Ancestor workflow_runs remain `running` — their `workflow` node invocation is open (mid-call), so they're conceptually "executing the sub-workflow that's waiting." CLI/chat surfaces show the leaf's pause; resuming the leaf completes the child's user_input node, which propagates back up the call chain naturally as each ancestor's workflow-call node completes.

Single source of truth: the leaf child's open user_input invocation. No bubbled events.

### 10.4 Resume authentication (single-user assumption) — S7 acknowledgment

Resume authentication relies on FILESYSTEM-LEVEL access controls at v1. Kolsch v1 assumes a single-user environment — the operator who launched the workflow run is the same person who resumes it, and the workspace directory's POSIX permissions are the security boundary. There is NO OS-user check, NO `resume_token`, and NO `--as <user>` validation on `kolsch workflows resume`. Multi-user host hardening (per-run resume tokens, OS-user identity checks, audit of resume principal) is deferred to Phase 3.x.

---

## 14. CLI Surface (resume-specific extracts)

### 14.1 Commands

```
kolsch [--format text|json] workflows resume <run_id> [--input k=v]...
```

### 14.2 Behavior notes

- `--input key=value` repeatable — assembled into a dict and validated against the workflow's derived input schema (run / fork) or the user_input node's output schema (resume; key is typically `response`). Matches the existing `kolsch run <agent> --input key=value` convention exactly.

### 14.3 CLI output format convention (resume row)

- `workflows resume` (Phase 3.1+) → execution result schema TBD

---

## 15. Chat Surface (user_input-specific extracts)

### 15.2 Rendering

See `process/phase3.9-chat-surface-spec.md` §15.2 for the canonical Rendering section. The user_input-relevant bullet is reproduced below for self-containment.

### 15.2.1 Minimal UX scope at v1 (L2) — user_input bullet

- **`user_input` is a small inline prompt.** Chat surface: a one-line prompt referencing the input variable name; CLI surface: same one-line prompt from the resume command's perspective.

---

## 18.2 Phase 3.4 — User_input with CLI pause/resume (sub-phasing entry)

**Scope**: `user_input` node type. Pause mechanism: status flips to `waiting_for_input`; JSONL ends with the user_input node's `workflow.node.started` event but no completion event. CLI `resume` command completes the open invocation with the user-supplied response.

**Verification gate**: a workflow with a user_input node pauses cleanly; the process can exit and resume in a separate command invocation. Multiple sequential pauses in one workflow work. Resume rejects when run isn't in `waiting_for_input` state.

**Why separate**: pause/resume is a real entity-lifecycle feature with persistence implications. Worth its own milestone.

**Effort**: ~1 week.
