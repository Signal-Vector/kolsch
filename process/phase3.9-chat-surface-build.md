# [SPEC-010] Phase 3.9 — Chat surface for workflows

**Type:** spec
**Doc #:** 010
**Status:** Draft
**Created:** 2026-06-27

---

## Glossary

| Term | Meaning |
| --- | --- |
| chat capsule | A bordered text region rendered in the chat scrollback. Phase 3.9 ships ONE NEW capsule per workflow-node completion. |
| workflow capsule | A capsule rendered by Phase 3.9 specifically for a `workflow.node.completed` / `workflow.node.pruned` event. Header carries `display_label(node)` + the event_id subscript; body shows the captured output. |
| inline prompt | The capsule shape rendered for a `user_input` pause: a single-line prompt referencing the input variable name. The user's next non-slash message becomes the resume response. |
| pending workflow_run_id | `ChatSessionState.pending_workflow_run_id` — the run that `/fork <event_id>` targets and the run that the next non-slash chat message resumes (if paused). |

---

## Background

Phase 3.9 is a **surface phase** — it composes existing machinery; no new node types, no new event kinds, no new runtime behavior. Every dependency is already in the codebase per the surface map:

- **Slash command system** at `src/cli/_slash.py:989-1075` — `COMMAND_REGISTRY` dict; `dispatch_slash(text, ctx)` at line 1097. Phase 3.9 adds two entries (`/workflow`, `/fork`).
- **Chat session state** at `src/cli/_slash.py:87-155` — `ChatSessionState` dataclass. Phase 3.9 adds `pending_workflow_run_id: str | None`.
- **Capsule rendering primitives** at `src/cli/_renderer.py:143-181` — `_capsule(...)` helper; `_CAPSULE_PALETTE` at lines 79-87. Phase 3.9 adds a "workflow" palette entry + a workflow-event renderer module.
- **Modal callback integration** at `src/cli/_modal.py:266-379` — already registered per-session at chat startup. Workflow-invoked actions that trigger policy prompts fire this callback transitively via the existing authz subsystem; NO new code needed.
- **`display_label(node, type_label=None)`** at `src/workflows/labels.py:52-83` — the 3-step fallback (per-instance `description:` → type-level `label:` → cleaned name). Phase 3.9 calls this from the capsule renderer per spec §15.4 (SOLE call site).
- **`caller_message`** on every `WorkflowError` subclass at `src/workflows/errors.py:93-96`. Phase 3.9 renders this (NOT `str(exc)`) in failure capsules per spec §15.5.
- **JSONL tail-follow** at `src/cli/workflows.py:_run_with_follow` (Phase 3.1 Step 5). Phase 3.9 reuses this pattern — spawn the executor as a concurrent task, tail the JSONL on a separate task, render events as they land.
- **User-input pause signal** at `src/workflows/builtins/user_input.py:36-62` (Phase 3.4) — `WorkflowPausedForInput` raised by the runner. The executor catches it and exits cleanly with `WAITING_FOR_INPUT` status. Chat detects this status post-executor-completion (or via the JSONL tail's last event = `workflow.node.started` for a user_input node with no matching completed) and renders the inline prompt capsule.
- **`workflow.node.pruned`** events (Phase 3.3) — Phase 3.9 renders these as MUTED capsules (dim color from `_CAPSULE_PALETTE`).
- **AssetCatalog lookup** at `src/workflows/catalog.py` / `src/assets/catalog.py` — pattern `catalog.lookup(ref, expected_type="workflow")` (per `cli/workflows.py:_cmd_run`). Reused by `/workflow` handler.
- **`_parse_input_pairs`** at `src/cli/workflows.py:562-572` — `--input k=v` parser. Phase 3.9 factors it out to a shared module so slash handlers can import without depending on the CLI module.
- **`kolsch workflows fork`** machinery (Phase 3.8) — `IterativeExecutor.start_from_fork(...)` + `WorkflowRunStore.create(forked_from=)` + `ForkedFrom` value object. Phase 3.9's `/fork` handler reuses verbatim — it's just a different invocation surface for the same machinery.

What's LEFT for Phase 3.9:

1. **Shared `_parse_input_pairs`** at a non-CLI-coupled module (e.g., `src/workflows/_args.py`).
2. **`with k=v` parser** for the slash-command argument format. The slash command `/workflow <ref> with k1=v1 k2=v2` differs from the CLI's `--input k=v --input k=v` because the slash has free-form text. Phase 3.9 ships a parser that handles `<ref> [with <pairs>]`.
3. **`ChatSessionState.pending_workflow_run_id`** field.
4. **`src/cli/_workflow_capsules.py`** — new renderer module dedicated to workflow events. Subscribes to a workflow-run's JSONL tail; emits one capsule per `workflow.node.completed` / `pruned` / `started`-with-no-completion (user_input) / `failed`. The header uses `display_label`; the body shows the captured output or `caller_message`; an event_id subscript appears in dim text.
5. **`/workflow` slash command** — parses args, resolves catalog ref, constructs `WorkflowRun` + executor, spawns concurrent executor task + JSONL tail task, sets `ChatSessionState.pending_workflow_run_id`, returns control to the input loop while the renderer streams capsules.
6. **`/fork` slash command** — gates on `pending_workflow_run_id`; if absent, errors with "no active workflow_run in this chat" per spec §15.1. Otherwise calls Phase 3.8's `start_from_fork` machinery.
7. **`user_input` chat resume path** — when the executor task completes with status `WAITING_FOR_INPUT`, the chat loop:
   - Renders an inline-prompt capsule (the user_input node's rendered prompt + the variable name).
   - Sets a flag on `ChatSessionState`: `expecting_user_input_response: bool = False` → True.
   - The next user message: if it starts with `/`, treat as a slash (operator chose to cancel/redirect — flag is cleared, the workflow remains paused); otherwise treat as the resume response and invoke `executor.resume(response={"response": <text>})`.
8. **Branch-not-taken muting** — the workflow-event renderer reads `workflow.node.pruned` events and emits a capsule with a "muted" color from `_CAPSULE_PALETTE`. The body shows just "(not taken)".
9. **Sub-workflow nesting visualization** — when the JSONL has a `workflow.node.called_workflow` event (Phase 3.7), the chat tails BOTH the parent's JSONL AND the child's JSONL (by `child_workflow_run_id` from the parent's event payload). Child capsules render INDENTED beneath the workflow node's capsule. Cross-JSONL correlation by `trace_id` is the integrity check (per master spec §11.3).
10. **Event_id subscript** — every rendered capsule shows its source event's `event_id` in dim text below the header. Operators copy this id for `/fork`.

The verification gate is six scenarios: (1) `/workflow kolsch/workflows/hello` runs end-to-end with capsules streaming; (2) `/workflow kolsch/workflows/hello-paused with greeting=hi` pauses at user_input — inline prompt renders; next non-slash message resumes; (3) `/fork <event_id>` re-runs from the chosen capsule with optional `with k=v` overrides; (4) failed nodes render `caller_message`, NOT `str(exc)`; (5) branch-not-taken capsules render muted; (6) sub-workflow capsules nest correctly.

**This spec breaks the phase into 4 sequential steps (Step 0 — Step 3)**.

Related docs:

- `process/phase3.9-chat-surface-spec.md` — master design spec (§15.1 / §15.2 / §15.3 / §15.4 / §15.5).
- `process/phase3.4-user-input-build.md` — pause/resume machinery; the chat resume path mirrors the CLI resume.
- `process/phase3.8-fork-build.md` — fork machinery; `/fork` reuses verbatim.
- `process/phase3.0-workflow-spec.md` §15.4 — `display_label` rule.

---

## Scope

**In scope (delivered across the 4 steps):**

- `src/workflows/_args.py` (new shared module) — factored-out `parse_input_pairs(items: list[str]) -> dict[str, Any]`.
- `parse_slash_workflow_args(text: str) -> tuple[str, dict[str, Any]]` — splits `<ref> [with k1=v1 k2=v2]` into ref + dict.
- `parse_slash_fork_args(text: str) -> tuple[str, dict[str, Any]]` — splits `<event_id> [with k1=v1 ...]`.
- `ChatSessionState.pending_workflow_run_id: str | None = None`.
- `ChatSessionState.expecting_user_input_response: bool = False`.
- `src/cli/_workflow_capsules.py` (new) — the workflow-event renderer. Subscribes to a run's JSONL tail; emits capsules per event.
- `/workflow` slash command in `COMMAND_REGISTRY`.
- `/fork` slash command in `COMMAND_REGISTRY`.
- `user_input` chat resume path: chat loop detects `WAITING_FOR_INPUT` status post-executor; renders inline prompt; next non-slash message → `executor.resume(response={"response": text})`.
- Branch-not-taken muting.
- `caller_message` rendering on failure.
- Sub-workflow nested capsule rendering.
- Event_id subscript on every capsule.
- Integration tests at `tests/component/test_chat_workflow_slash.py`.

**Out of scope (per spec §15.2 + roadmap):**

- Token-by-token streaming inside agent-node capsules.
- Progress display (spinner, "step N of M", per-node elapsed timer beyond completion timestamps).
- Cost display.
- Mid-run status ("running classify now…").
- Concurrent-node visualization (Phase 3.10 ships parallel; visualization is a separate question then).
- TUI mode / multi-pane progress views.
- `/workflow` autocomplete (deferred QoL).
- Persisting the chat's `pending_workflow_run_id` across chat-process restarts (deferred QoL; operator can re-bind via `/workflow` or explicit run_id reference).

**Decisions explicitly locked (do NOT relitigate):**

- **No streaming.** Capsules at `workflow.node.completed` only. The §15.2.2 running-node indicator (one dim line on `workflow.node.started`) is NOT streaming — it is emitted once and never updated.
- **No new event kinds.** Phase 3.9 consumes existing events.
- **`display_label` is the SOLE label source.** Per spec §15.4. Chat does NOT reimplement the 3-step fallback.
- **`caller_message` is the failure surface.** Per spec §15.5. Engineer-targeted `str(exc)` stays in logs/CLI; chat is end-user-facing.
- **Inline `user_input` capsule shape per spec §15.2.1.** Header = `display_label(node)`; body line 1 = rendered `prompt:` field; body line 2 (when present) = `→ binds to inputs.<var_name>` in dim text. NO `(awaiting your response)` placeholder.
- **Running-node indicator per spec §15.2.2.** One dim `▸ <label>…` line per `workflow.node.started`. Persists in scrollback unchanged; the matching completion capsule lands beneath. Not a capsule; no event_id subscript; no timestamp.
- **Sub-workflow nesting is visual indent + cross-JSONL correlation by `trace_id`.** No new event kinds for nesting; the parent's `workflow.node.called_workflow` event carries `child_workflow_run_id` which the chat uses to tail the child JSONL.
- **`pending_workflow_run_id` is the targeting mechanism.** Set by `/workflow`; consumed by `/fork` and the user_input resume path. There's exactly one pending workflow per chat session at v1.
- **Operator escape from `expecting_user_input_response`**: typing any slash command BEFORE replying clears the expectation flag. The workflow remains paused; the operator can resume later via the CLI `kolsch workflows resume` OR by typing the response inline. This matches spec §15.2.1's "user's next message becomes the resume" but acknowledges operator escape.

---

## Technical Context

**Repo / path:** `/Users/andrecohen/Desktop/SV/Projects/kolsch`

**Stack:** Python 3.13, Pydantic v2, asyncio, pytest. Package manager: `uv`.

**Key existing modules to integrate with:**

- `src/cli/chat.py:112-140, 382-553` — `cmd_chat` entry + `_run_input_loop`. The slash dispatch hook is line 487-ish.
- `src/cli/_slash.py:87-155, 216-262, 989-1075, 1097` — `ChatSessionState`, `SlashCommand`, `COMMAND_REGISTRY`, `dispatch_slash`.
- `src/cli/_renderer.py:79-87, 143-181, 298-311, 746-759` — `_CAPSULE_PALETTE`, `_capsule`, `render(event_stream)`, `_DISPATCH`.
- `src/cli/_modal.py:266-379` — `PromptCallback`. Already wired per chat session; no change.
- `src/cli/workflows.py:_run_with_follow, _drain_tail_to_writer, _parse_input_pairs` — JSONL tail-follow pattern + input parser (to be factored).
- `src/workflows/labels.py:52-83` — `display_label`.
- `src/workflows/builtins/user_input.py:36-62` — `WorkflowPausedForInput` signal.
- `src/workflows/executor.py` — `IterativeExecutor.run()` / `resume()` / `start_from_fork()`.
- `src/workflows/events.py` — event kinds + Pydantic models.
- `src/workflows/_replay.py` — replay machinery (used transitively via `resume`/`start_from_fork`).
- `src/workflows/errors.py:93-96` — `caller_message` ClassVar.
- `src/workflows/catalog.py` / `src/assets/catalog.py` — `lookup(ref, expected_type=)`.

**Local setup:**

```bash
cd /Users/andrecohen/Desktop/SV/Projects/kolsch
uv run kolsch --root /tmp/kolsch-chat-test setup --non-interactive
uv run pytest tests/component/ -q
```

---

## Phase-level Acceptance Criteria

- In chat, `/workflow kolsch/workflows/hello` parses, resolves, runs end-to-end. One capsule renders per node completion. Each capsule's header uses `display_label`; body shows captured output; an event_id subscript appears in dim text.
- `/workflow <ref> with key=value [key=value]...` parses correctly; `with` separator is recognized; key=value pairs are bound as workflow inputs.
- A workflow with a `user_input` node pauses; chat renders an inline-prompt capsule referencing the variable name; the next non-slash message becomes the resume response; the workflow continues to terminal.
- Typing a slash command (e.g., `/help`) while `expecting_user_input_response` is True cancels the resume expectation; the workflow stays paused; the operator can resume later via CLI.
- `/fork <event_id>` against the `pending_workflow_run_id` invokes the Phase 3.8 fork machinery; the forked run renders fresh capsules.
- `/fork <event_id> with key=value` correctly threads input overrides per Phase 3.8 contract.
- `/fork` without an active `pending_workflow_run_id` errors with a clear "no active workflow_run in this chat" message; chat loop continues.
- Failed nodes render `caller_message` (NOT `str(exc)`).
- `workflow.node.pruned` events render as muted capsules with body "(not taken)".
- Sub-workflow capsules render INDENTED beneath the parent's `workflow:` node capsule.
- Existing chat-with-agent functionality is unchanged (no regressions in `cmd_chat`).
- Modal callback integration: a workflow-invoked action that triggers a policy prompt correctly fires the existing chat modal.

---

## Phase-level Integration Tests

### Phase Test 1: `/workflow` end-to-end
- **Run:** `uv run pytest tests/component/test_chat_workflow_slash.py::test_slash_workflow_runs_to_completion -v`
- **Expect:** simulated chat input `/workflow kolsch/workflows/hello`; capsules stream to stdout; final capsule shows the terminal output.

### Phase Test 2: `/workflow with k=v`
- **Run:** `uv run pytest tests/component/test_chat_workflow_slash.py::test_slash_workflow_with_inputs -v`
- **Expect:** `/workflow kolsch/workflows/hello with name=Alice`; `inputs.name == "Alice"` flows through the run.

### Phase Test 3: User input chat resume
- **Run:** `uv run pytest tests/component/test_chat_workflow_slash.py::test_user_input_chat_resume -v`
- **Expect:** `/workflow kolsch/workflows/hello-paused`; inline-prompt capsule renders; simulated next message `world`; workflow resumes; completes.

### Phase Test 4: Slash escape from user_input expectation
- **Run:** `uv run pytest tests/component/test_chat_workflow_slash.py::test_slash_cancels_user_input_expectation -v`
- **Expect:** after pause, simulated next input `/help`; the workflow REMAINS paused; chat shows help; `expecting_user_input_response` is cleared.

### Phase Test 5: `/fork` end-to-end
- **Run:** `uv run pytest tests/component/test_chat_workflow_slash.py::test_slash_fork_at_event -v`
- **Expect:** `/workflow ...` completes; operator picks an event_id from a capsule; `/fork <event_id> with label=urgent` re-runs from that point.

### Phase Test 6: `/fork` with no pending run
- **Run:** `uv run pytest tests/component/test_chat_workflow_slash.py::test_fork_without_pending_errors -v`
- **Expect:** `/fork ev_xxx` with no prior `/workflow` errors with the documented message.

### Phase Test 7: Failed node `caller_message` rendering
- **Run:** `uv run pytest tests/component/test_chat_workflow_slash.py::test_failed_node_caller_message -v`
- **Expect:** workflow with a failing bash node; capsule body shows the `WorkflowNodeFailed.caller_message`, NOT the engineer-targeted `str(exc)`.

### Phase Test 8: Branch-not-taken muted rendering
- **Run:** `uv run pytest tests/component/test_chat_workflow_slash.py::test_pruned_capsule_muted -v`
- **Expect:** `workflow.node.pruned` events produce a muted-color capsule with body "(not taken)".

### Phase Test 9: Sub-workflow nesting
- **Run:** `uv run pytest tests/component/test_chat_workflow_slash.py::test_subworkflow_nested_capsules -v`
- **Expect:** sub-workflow capsules render indented; parent's `workflow:` node capsule wraps the child's capsules visually.

### Phase Test 10: No regressions
- **Run:** `uv run pytest tests/ -q`
- **Expect:** full suite green; existing chat agent flows produce byte-identical output.

---

## Coordinator Prompt

> You are the strategic decision-maker. You do not write or review code.
> Drive the 4-step Coder/Reviewer flow.

**Your task:**

Ship Phase 3.9 through 4 sequential cycles. This is a **surface phase** — no new node types, no new event kinds, no new runtime behavior. Most work is composing existing pieces correctly. The non-obvious work is the concurrent-task design (executor task + JSONL tail task + chat input loop coexisting safely without races).

**Authoritative references:**

- THIS spec: `process/phase3.9-chat-surface-build.md`.
- Master design spec: `process/phase3.9-chat-surface-spec.md` — §15.
- Phase 3.4 build plan: `process/phase3.4-user-input-build.md` — pause/resume.
- Phase 3.8 build plan: `process/phase3.8-fork-build.md` — fork machinery.
- `src/cli/_slash.py` — slash command framework; mirror existing handlers.
- `src/cli/_renderer.py` — capsule rendering primitives.

**The 4 steps:**

| Step | Focus | Effort |
| --- | --- | --- |
| **Step 0** | **PREREQ** — factor `parse_input_pairs` to `src/workflows/_args.py`; add `parse_slash_workflow_args` + `parse_slash_fork_args`; extend `ChatSessionState` with `pending_workflow_run_id` + `expecting_user_input_response`; ship `src/cli/_workflow_capsules.py` skeleton (workflow-event renderer that emits one capsule per event). | ~1.5 days |
| **Step 1** | `/workflow <ref> [with k=v]...` slash command end-to-end: parse, catalog resolve, executor spawn (concurrent task), JSONL tail task, capsule rendering with `display_label` + event_id subscript + branch-not-taken muting + `caller_message` failure surface + sub-workflow nested indentation. Sets `pending_workflow_run_id`. | ~2.5 days |
| **Step 2** | `user_input` chat resume — chat loop detects post-executor `WAITING_FOR_INPUT` (or last-event-is-started-no-completed); renders inline-prompt capsule; sets `expecting_user_input_response`; next non-slash message → `executor.resume(...)`; slash escape clears flag. | ~1 day |
| **Step 3** | `/fork <event_id> [with k=v]...` slash command — gates on `pending_workflow_run_id`; reuses Phase 3.8 machinery + tail-renderer from Step 0. Phase Tests 5-6 wired. Phase Tests 7-10 (failure, muting, nesting, regression) wired across all surfaces. | ~1.5 days |

**Flow per step:** standard Coder/Reviewer cycle.

**When to make a strategic call:**

- Coder asks to relitigate a locked decision (streaming, progress display, cost UI, multi-line user_input prompt, persisted pending_workflow_run_id) → REJECT.
- Coder asks to add new event kinds for chat-specific rendering → REJECT. Phase 3.9 consumes existing events.
- Coder finds master-spec ambiguity → consult, make a binding call, append to THIS spec, re-brief.
- Coder discovers a Phase 3.0-3.8 contract was wrong → flag as separate patch.
- Scope creep (TUI mode, autocomplete, concurrent-node viz) → REJECT.

**Done when:** All 4 steps Approved AND all 10 Phase-level Integration Tests pass AND `phase3.x-roadmap.md` Phase 3.9 line annotated with the shipping SHA.

---

# STEP 0 — Shared helpers + ChatSessionState extensions + workflow capsule renderer skeleton (PREREQ)

**Goal**: ship the shared `parse_input_pairs` + the two slash-arg parsers + the `ChatSessionState` extensions + a skeleton `src/cli/_workflow_capsules.py` module that can render one capsule per workflow event. NO slash commands yet.

**Files created/modified:**

- `src/workflows/_args.py` (new) — `parse_input_pairs(items: list[str]) -> dict[str, Any]` (factored from `src/cli/workflows.py`).
- `src/cli/workflows.py` (modified) — replace inline `_parse_input_pairs` with import from `src/workflows/_args.py`.
- `src/cli/_workflow_slash_args.py` (new) — `parse_slash_workflow_args(text) -> tuple[str, dict]` + `parse_slash_fork_args(text) -> tuple[str, dict]`.
- `src/cli/_slash.py` (modified) — extend `ChatSessionState` with two new fields.
- `src/cli/_workflow_capsules.py` (new) — `WorkflowCapsuleRenderer` class that consumes a JSONL tail iterator and emits capsules via the existing `_renderer._capsule(...)` primitives.
- `src/cli/_renderer.py` (modified) — add a "workflow" + "workflow-muted" entry to `_CAPSULE_PALETTE`.
- `tests/component/test_workflow_args.py` (new) — coverage for the shared helpers.
- `tests/component/test_workflow_capsules_renderer.py` (new) — coverage for the renderer module.

**Effort**: ~1.5 days.

## Step 0 — Coder Prompt

> Reply "Step 0 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the shared helpers + the renderer skeleton. NO slash commands yet.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.9-chat-surface-build.md` — Step 0.
2. `process/phase3.9-chat-surface-spec.md` — §15.2, §15.4, §15.5.
3. `src/cli/workflows.py:562-572` — existing `_parse_input_pairs`.
4. `src/cli/_slash.py:87-155` — existing `ChatSessionState`.
5. `src/cli/_renderer.py:79-87, 143-181` — `_CAPSULE_PALETTE` + `_capsule`.
6. `src/workflows/labels.py:52-83` — `display_label`.

**What to implement:**

1. **`src/workflows/_args.py`** — factored helper:

   ```python
   from typing import Any

   def parse_input_pairs(items: list[str]) -> dict[str, Any]:
       """Parse a list of ``KEY=VALUE`` strings into a dict.

       Used by both the CLI ``--input k=v`` form and the chat
       ``with k=v`` form. Values are returned as strings; downstream
       Pydantic strict-mode validation coerces or rejects.

       Lists/dicts: pass JSON literals (``tags=["a","b"]``);
       Pydantic coerces.
       """
       out: dict[str, Any] = {}
       for item in items:
           if "=" not in item:
               raise ValueError(f"input pair must be KEY=VALUE; got {item!r}")
           key, value = item.split("=", 1)
           out[key] = value
       return out
   ```

   Update `src/cli/workflows.py` to `from workflows._args import parse_input_pairs` and remove the inline `_parse_input_pairs`.

2. **`src/cli/_workflow_slash_args.py`**:

   ```python
   def parse_slash_workflow_args(text: str) -> tuple[str, dict[str, Any]]:
       """Parse '/workflow <ref> [with k=v k=v...]' into (ref, inputs).

       The text passed in is everything AFTER '/workflow ' (stripped).
       """
       text = text.strip()
       if not text:
           raise ValueError("'/workflow' requires a workflow ref")
       # Find ' with ' (whitespace-bounded), split.
       parts = text.split(" with ", 1)
       ref = parts[0].strip()
       if not ref:
           raise ValueError("'/workflow' requires a workflow ref before 'with'")
       inputs: dict[str, Any] = {}
       if len(parts) == 2:
           pairs = parts[1].split()  # whitespace-delimited; for now no quoted-string support
           inputs = parse_input_pairs(pairs)
       return ref, inputs

   def parse_slash_fork_args(text: str) -> tuple[str, dict[str, Any]]:
       """Parse '/fork <event_id> [with k=v...]' into (event_id, overrides)."""
       text = text.strip()
       if not text:
           raise ValueError("'/fork' requires an event_id")
       parts = text.split(" with ", 1)
       event_id = parts[0].strip()
       if not event_id:
           raise ValueError("'/fork' requires an event_id before 'with'")
       overrides: dict[str, Any] = {}
       if len(parts) == 2:
           pairs = parts[1].split()
           overrides = parse_input_pairs(pairs)
       return event_id, overrides
   ```

   **Decision needed flag:** quoted-string support for spaces in values. The CLI uses shell quoting; slash commands don't. For v1, document that values cannot contain spaces (operators use JSON arrays or quoted-shorter values). Future QoL.

3. **`ChatSessionState` extensions** in `src/cli/_slash.py`:

   ```python
   @dataclass
   class ChatSessionState:
       ...  # existing fields
       pending_workflow_run_id: str | None = None       # NEW
       expecting_user_input_response: bool = False       # NEW
   ```

4. **`_CAPSULE_PALETTE` extension** in `src/cli/_renderer.py`:

   ```python
   _CAPSULE_PALETTE = {
       ...  # existing
       "workflow": "cyan",          # NEW
       "workflow_muted": "bright_black",  # NEW — for pruned / branch-not-taken
       "workflow_paused": "yellow",  # NEW — for user_input pause
       "workflow_failed": "red",     # NEW — for failed nodes
   }
   ```

5. **`src/cli/_workflow_capsules.py`** — renderer skeleton:

   ```python
   _RUNNING_GLYPH = "▸"   # ▸
   _ELLIPSIS = "…"        # …

   class WorkflowCapsuleRenderer:
       """Consumes a workflow-run's event stream; emits one capsule per event.

       Phase 3.9: capsules render at workflow.node.completed (success),
       workflow.node.pruned (muted), workflow.node.failed (caller_message),
       and workflow.run.* (run-level framing). workflow.node.started emits
       the one-line running indicator (spec §15.2.2) — NOT a capsule.
       workflow.node.started for a user_input node is paired with the
       pause capsule rendered post-executor by the chat loop, not here.
       All other events are silent.
       """

       def __init__(self, *, console, store, run_id: WorkflowRunId, indent_depth: int = 0):
           self._console = console
           self._store = store
           self._run_id = run_id
           self._indent_depth = indent_depth
           self._indent = "  " * indent_depth  # for sub-workflow nesting
           self._child_renderers: dict[str, "WorkflowCapsuleRenderer"] = {}  # by child_run_id

       async def render(self) -> WorkflowRunStatus:
           """Subscribe to the run's JSONL tail; emit capsules; return final status."""
           async for event in self._tail_events():
               await self._dispatch(event)
           return self._final_status

       async def _tail_events(self):
           """Reuse the Phase 3.1 _drain_tail_to_writer pattern, yielding events."""
           ...

       async def _dispatch(self, event: dict) -> None:
           kind = event.get("kind", "")
           if kind == "workflow.node.started":
               self._render_running_indicator(event)        # §15.2.2
           elif kind == "workflow.node.completed":
               self._render_completed_capsule(event)
           elif kind == "workflow.node.pruned":
               self._render_pruned_capsule(event)
           elif kind == "workflow.node.failed":
               self._render_failed_capsule(event)
           elif kind == "workflow.node.called_workflow":
               # Phase 3.7 — sub-workflow event. Spawn a child renderer.
               await self._spawn_child_renderer(event)
           # ... etc.

       def _render_running_indicator(self, event: dict) -> None:
           """Spec §15.2.2 — one dim ``▸ <label>…`` line per started node.

           Look up the node_def + type_label; call ``display_label(...)``;
           print one line `f"{self._indent}{_RUNNING_GLYPH} {label}{_ELLIPSIS}"`
           in the `"dim"` style. No timestamp, no event_id subscript, no
           capsule border. The line persists in scrollback unchanged;
           the matching `_render_completed_capsule` / `_render_failed_capsule`
           / `_render_pruned_capsule` lands beneath it.
           """
           ...

       def _render_completed_capsule(self, event):
           # Look up the node_def in the parsed workflow to get the type-label.
           # Call display_label(node_def, type_label) for the header.
           # Format the captured output for the body.
           # Print the event_id subscript in dim text.
           ...
   ```

6. **Tests in `tests/component/test_workflow_args.py`**:
   - **CLI input pairs:** `parse_input_pairs(["foo=bar", "baz=qux"])` → `{"foo": "bar", "baz": "qux"}`.
   - **Missing `=` raises:** `parse_input_pairs(["foo"])` → `ValueError`.
   - **`with` separator on `/workflow`:** `parse_slash_workflow_args("kolsch/workflows/hello with name=Alice")` → `("kolsch/workflows/hello", {"name": "Alice"})`.
   - **No `with`:** `parse_slash_workflow_args("kolsch/workflows/hello")` → `("kolsch/workflows/hello", {})`.
   - **Empty after `/workflow`:** raises with documented message.
   - **`/fork` args:** `parse_slash_fork_args("ev_abc with override=foo")` → `("ev_abc", {"override": "foo"})`.

7. **Tests in `tests/component/test_workflow_capsules_renderer.py`** (skeleton coverage):
   - **Empty stream:** renderer with no events produces no capsule output; returns final status by inspecting run's meta.
   - **One completed event:** renderer emits one capsule with the right header/body/subscript.
   - **Display label:** the header uses `display_label(node_def, type_label=...)`.
   - **Pruned event:** muted color capsule with body "(not taken)".
   - **Running indicator (§15.2.2):** a `workflow.node.started` event emits ONE dim line `▸ <label>…` ABOVE the matching completion capsule; the line is not a capsule (no `│` bar, no timestamp); the captured stdout for a started+completed pair shows the indicator line followed by the capsule, in order.
   - **Indicator + indent:** with `indent_depth=1`, the indicator line is prefixed with two spaces; matches the indented capsules in the sub-workflow nesting test.
   - **Pruned has no preceding indicator:** a `workflow.node.pruned` event without a preceding `workflow.node.started` produces only the muted capsule — no spurious indicator line.

**Step 0 Acceptance Criteria:**

0.1. `parse_input_pairs` factored to `src/workflows/_args.py`; CLI imports work.
0.2. `parse_slash_workflow_args` + `parse_slash_fork_args` ship with documented semantics.
0.3. `ChatSessionState.pending_workflow_run_id` + `expecting_user_input_response` fields added; defaults preserve backward compat.
0.4. `_CAPSULE_PALETTE` has the four new entries.
0.5. `WorkflowCapsuleRenderer` skeleton exists; emits one capsule per `workflow.node.completed` / `pruned` / `failed` event; uses `display_label`.
0.6. `WorkflowCapsuleRenderer._render_running_indicator` emits one dim `▸ <label>…` line per `workflow.node.started` event per spec §15.2.2 — single line, no `│` bar, no timestamp, no event_id subscript; indent honors `indent_depth`.
0.7. NO slash commands yet (grep `COMMAND_REGISTRY` for `/workflow` → 0 matches; grep for `/fork` → 0 matches).
0.8. No regressions.

**Self-check before handoff:**

- [ ] All 7 ACs met
- [ ] `grep _parse_input_pairs src/` returns ZERO matches (the inline version is gone)
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 0 Complete — [summary]".

## Step 0 — Reviewer Prompt

> Reply "Step 0 Approved" or "Changes requested — [list]".

Verify each AC. PAY EXTRA ATTENTION to:

- AC 0.1 — CLI still works. Run a small workflow via `kolsch workflows run --input k=v`; assert no regression in the CLI path.
- AC 0.5 — `display_label` is called. Read the renderer source; the SOLE label source must be `display_label(node_def, type_label)`. Any inline 3-step rule reimplementation is a spec §15.4 violation.

---

# STEP 1 — `/workflow` slash command end-to-end

**Goal**: ship `/workflow <ref> [with k=v...]` end-to-end. Parses args, resolves catalog, constructs executor + tail-renderer, sets `pending_workflow_run_id`, runs to terminal status. NO `/fork` yet; NO user_input chat resume yet (Step 2 owns the resume; Step 3 owns fork).

**Files created/modified:**

- `src/cli/_slash.py` (modified) — register `/workflow` in `COMMAND_REGISTRY`; add handler.
- `src/cli/_workflow_capsules.py` (modified — fill out skeleton) — full event-dispatch logic; sub-workflow nesting; failure rendering via `caller_message`.
- `src/cli/chat.py` (modified) — extend chat input loop to surface workflow execution. The slash dispatcher returns a `SlashSignal` that may include a coroutine; the loop awaits it before returning to prompt.
- `tests/component/test_chat_workflow_slash.py` (new) — Phase Tests 1, 2, 7, 8 wired.

**Effort**: ~2.5 days.

## Step 1 — Coder Prompt

> Reply "Step 1 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship `/workflow` end-to-end.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.9-chat-surface-build.md` — Step 1.
2. `process/phase3.9-chat-surface-spec.md` — §15.1, §15.2, §15.4, §15.5.
3. `src/cli/_slash.py:216-262, 989-1075` — `SlashCommand` shape + `COMMAND_REGISTRY`.
4. `src/cli/workflows.py:_run_with_follow` — concurrent executor + tail pattern.
5. `src/cli/_modal.py` — modal callback (already wired; no change).

**What to implement:**

1. **`/workflow` handler** in `src/cli/_slash.py`:

   ```python
   async def _handle_workflow(text: str, ctx: ChatSessionState) -> SlashSignal:
       try:
           ref, inputs = parse_slash_workflow_args(text)
       except ValueError as exc:
           return SlashSignal.error(str(exc))
       # Resolve via catalog.
       entry = ctx.runtime.asset_management.catalog().lookup(ref, expected_type="workflow")
       if entry is None:
           return SlashSignal.error(f"workflow {ref!r} not found")
       workflow = load_workflow(Path(entry.path))
       # Create run.
       run = ctx.runtime.workflow_store.create(
           workflow_ref=ref,
           inputs=inputs,
           # trace_id: use ctx.session_id-derived trace? For v1, mint fresh; future QoL ties to session.
       )
       # Construct executor.
       executor = IterativeExecutor(
           workflow=workflow,
           run=run,
           store=ctx.runtime.workflow_store,
           registry=ctx.runtime.workflow_registry,
           catalog=ctx.runtime.asset_management.catalog(),
       )
       # Set pending.
       ctx.pending_workflow_run_id = str(run.run_id)
       # Spawn concurrent executor + renderer.
       executor_task = asyncio.create_task(executor.run())
       renderer = WorkflowCapsuleRenderer(
           console=ctx.console,
           store=ctx.runtime.workflow_store,
           run_id=run.run_id,
       )
       try:
           await renderer.render()
       finally:
           status = await executor_task
       # Post-run: detect pause.
       if status == WorkflowRunStatus.WAITING_FOR_INPUT:
           ctx.expecting_user_input_response = True
           # Step 2 ships the resume path.
       return SlashSignal.ok()
   ```

2. **`COMMAND_REGISTRY` extension**:

   ```python
   COMMAND_REGISTRY["/workflow"] = SlashCommand(
       name="/workflow",
       description="Invoke a workflow.",
       usage="/workflow <ref> [with key=value ...]",
       handler=_handle_workflow,
       free_form_arg_hint="<ref> [with key=value ...]",
   )
   ```

3. **`WorkflowCapsuleRenderer` completion** in `src/cli/_workflow_capsules.py`:
   - Fill in `_render_running_indicator`, `_render_completed_capsule`, `_render_pruned_capsule`, `_render_failed_capsule`.
   - **Running indicator (§15.2.2):** `_render_running_indicator` prints one line `f"{indent}▸ {display_label(node_def, type_label)}…"` in `dim` style. Single `console.print(Text(..., style="dim"))` call. No bar, no timestamp, no event_id subscript. The line persists in scrollback; the matching completion / failure / pruned capsule lands beneath it.
   - For sub-workflows (`workflow.node.called_workflow`): spawn a child `WorkflowCapsuleRenderer` with `indent_depth+1`; await its `render()` concurrently with the parent's tail; ensure child capsules appear BENEATH the parent's workflow-node capsule. Children emit their own indented `▸ <label>…` lines.
   - For user_input pauses (§15.2.1): the indicator fires from `_render_running_indicator` on the user_input node's `workflow.node.started`. The pause capsule itself is rendered by the chat loop AFTER the executor returns `WAITING_FOR_INPUT` (Step 2 owns the chat-side render); the renderer here does NOT synthesize the pause capsule from the JSONL — it only emits the indicator. The pause capsule shape (locked in spec §15.2.1):
     - **Header:** `display_label(node_def, type_label)` — the node's identity. NO prompt text in the header.
     - **Body line 1:** the rendered `prompt:` text from the executor's `WorkflowPausedForInput.prompt` (already Jinja-rendered by `src/workflows/builtins/user_input.py:46-57`).
     - **Body line 2 (when the node binds an input variable):** dim `→ binds to inputs.<var_name>`.
     - **No `(awaiting your response)` placeholder** — the chat's `:> ` input prompt is the unmistakable "your turn" cue.
   - Event_id subscript: dim text below each capsule's header showing `ev_<short_id>` (truncate `ev_<uuid>` to first 8 chars for compactness; full id shown in title-bar hover or `--verbose` flag — defer to Step 3).

4. **`caller_message` rendering** in failure capsules:
   - The `workflow.node.failed` event payload has `error_class` + `message`. The CHAT renders the failure capsule's body from the corresponding error class's `caller_message` ClassVar.
   - Lookup: `cls = getattr(workflow_errors_module, error_class_name)`; `body = getattr(cls, "caller_message", "Something went wrong running the workflow.")`.
   - Fallback: if the class isn't found in the errors module, use the generic `WorkflowError.caller_message`.

5. **Tests in `tests/component/test_chat_workflow_slash.py`**:
   - **Phase Test 1:** simulated chat input `/workflow kolsch/workflows/hello`; assert stdout contains the expected capsule headers (display_label-derived).
   - **Phase Test 2:** simulated `/workflow kolsch/workflows/hello with name=Alice`; assert downstream capsule body contains "Alice".
   - **Phase Test 7:** simulated workflow with a failing bash node; assert the failure capsule body equals `WorkflowNodeFailed.caller_message`, NOT `str(exc)`.
   - **Phase Test 8:** simulated workflow with a branch that prunes one arm; assert the pruned capsule is rendered in the muted color with body "(not taken)".
   - **`pending_workflow_run_id` set:** after `/workflow ...`, assert `ctx.pending_workflow_run_id == str(run.run_id)`.
   - **Catalog miss:** simulated `/workflow nonexistent/ref`; assert error message contains the ref.
   - **Bad args:** simulated `/workflow with foo=bar`; assert documented error.

**Step 1 Acceptance Criteria:**

1.1. `/workflow` registered in `COMMAND_REGISTRY` with usage hint.
1.2. `parse_slash_workflow_args` correctly splits ref + inputs.
1.3. Catalog resolve via `lookup(ref, expected_type="workflow")`; missing ref → user-friendly error.
1.4. `WorkflowCapsuleRenderer` emits capsules with header = `display_label(node_def, type_label)`, body = captured output or `caller_message`, event_id subscript in dim text.
1.5. `workflow.node.started` produces ONE dim `▸ <label>…` line ABOVE the matching completion / failure / pruned-target capsule per spec §15.2.2; the line is not a capsule (no `│` bar, no timestamp); it persists in scrollback unchanged.
1.6. Failed nodes render `caller_message`, NOT `str(exc)`.
1.7. Pruned events render muted color + body "(not taken)"; no preceding indicator (pruned nodes never emit `workflow.node.started`).
1.8. Sub-workflow capsules render INDENTED beneath the parent's workflow-node capsule; child indicators indent at the same depth as child capsules.
1.9. `pending_workflow_run_id` set on successful `/workflow` invocation.
1.10. Modal callback fires correctly when a workflow-invoked action triggers a policy prompt (manual smoke OR integration test).
1.11. NO `/fork` yet (Step 3). NO user_input chat resume yet (Step 2 — the post-run pause detection MAY set `expecting_user_input_response=True` but no resume path).
1.12. No regressions: existing chat-with-agent flows unchanged.

**Self-check before handoff:**

- [ ] All 11 ACs met
- [ ] Manual smoke: `kolsch chat ... `; in the chat type `/workflow kolsch/workflows/hello`; observe capsules
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 1 Complete — [summary]".

## Step 1 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 1.4 — `display_label` is the SOLE label source. Grep `_workflow_capsules.py` for any inline label fallback logic (per-instance description check, type-level label check). The ONLY call site should be `display_label(...)`.
- AC 1.5 — running indicator shape per spec §15.2.2. Read `_render_running_indicator` source: it MUST be a single `console.print(Text(..., style="dim"))` of the form `▸ <label>…`. NO `│` bar prefix (that would make it look like a capsule). NO timestamp. NO event_id subscript. NO Rich `Live` / carriage-return tricks. Grep the test output of a started+completed pair: the indicator line and the capsule must both appear, in order, both present.
- AC 1.6 — `caller_message` lookup. The failed event's `error_class` is a STRING; the renderer must look up the actual class to read its ClassVar `caller_message`. A typo'd class name should fall back to the generic message gracefully.
- AC 1.8 — sub-workflow indentation. Run a test workflow that calls a sub-workflow; visually inspect the output; confirm child capsules appear BENEATH and INDENTED relative to the parent's workflow-node capsule. Confirm child indicators indent at the same depth as child capsules.

---

# STEP 2 — `user_input` chat resume

**Goal**: ship the chat-driven resume path. After a workflow pauses, render the inline prompt; the next non-slash user message resumes via `executor.resume(...)`; slash commands clear the expectation.

**Files created/modified:**

- `src/cli/chat.py` (modified) — input loop checks `ctx.expecting_user_input_response`; routes next message to resume OR slash dispatch.
- `src/cli/_slash.py` (modified) — `dispatch_slash(text, ctx)` clears `expecting_user_input_response` when invoked.
- `tests/component/test_chat_workflow_slash.py` (modified) — Phase Tests 3, 4 wired.

**Effort**: ~1 day.

## Step 2 — Coder Prompt

> Reply "Step 2 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship the chat resume path.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.9-chat-surface-build.md` — Step 2.
2. `process/phase3.9-chat-surface-spec.md` — §15.2.1.
3. `process/phase3.4-user-input-build.md` — `IterativeExecutor.resume(response={...})`.
4. `src/cli/chat.py:382-553` — input loop.

**What to implement:**

1. **Input loop extension** in `src/cli/chat.py`:

   ```python
   while True:
       text = await prompt_session.prompt_async("You: ")
       if ctx.expecting_user_input_response:
           if is_slash_command(text):
               # Operator escape — clear expectation; proceed to slash dispatch.
               ctx.expecting_user_input_response = False
               # Fall through to existing slash path.
           else:
               # Resume the workflow.
               await _resume_pending_workflow(ctx, response_text=text)
               continue
       if is_slash_command(text):
           signal = await dispatch_slash(text, ctx)
           ...
       else:
           # Existing agent path.
           ...
   ```

2. **`_resume_pending_workflow`** in `src/cli/chat.py`:

   ```python
   async def _resume_pending_workflow(ctx: ChatSessionState, *, response_text: str) -> None:
       run_id = WorkflowRunId(ctx.pending_workflow_run_id)
       run = ctx.runtime.workflow_store.read(run_id)
       workflow = load_workflow_from_run(ctx.runtime, run)
       executor = IterativeExecutor(
           workflow=workflow, run=run,
           store=ctx.runtime.workflow_store,
           registry=ctx.runtime.workflow_registry,
           catalog=ctx.runtime.asset_management.catalog(),
       )
       executor_task = asyncio.create_task(
           executor.resume(response={"response": response_text})
       )
       renderer = WorkflowCapsuleRenderer(
           console=ctx.console,
           store=ctx.runtime.workflow_store,
           run_id=run_id,
       )
       try:
           await renderer.render()
       finally:
           status = await executor_task
       ctx.expecting_user_input_response = False
       if status == WorkflowRunStatus.WAITING_FOR_INPUT:
           # Another user_input pause; set flag again.
           ctx.expecting_user_input_response = True
   ```

3. **`dispatch_slash` extension**:

   ```python
   async def dispatch_slash(text: str, ctx: ChatSessionState) -> SlashSignal:
       if ctx.expecting_user_input_response:
           ctx.expecting_user_input_response = False  # operator escape clears it
       ...
   ```

4. **Tests in `tests/component/test_chat_workflow_slash.py`**:
   - **Phase Test 3:** simulated `/workflow kolsch/workflows/hello-paused`; assert inline-prompt capsule; simulated next message `world`; assert workflow resumes and produces the expected terminal capsule.
   - **Phase Test 4:** simulated pause; simulated `/help`; assert chat shows help; assert workflow is STILL paused (`run.status == WAITING_FOR_INPUT`); assert `expecting_user_input_response` is False.
   - **Sequential pauses:** workflow with two `user_input` nodes; pause-resume-pause-resume sequence works correctly through chat.

**Step 2 Acceptance Criteria:**

2.1. Post-pause, chat renders an inline prompt capsule per spec §15.2.1: header = `display_label(user_input_node)`; body line 1 = rendered `prompt:` text; body line 2 (when the node binds a variable) = dim `→ binds to inputs.<var_name>`; NO `(awaiting your response)` placeholder.
2.2. `expecting_user_input_response = True` after pause; `False` after resume OR slash-escape.
2.3. Non-slash next message routes to `executor.resume(response={"response": text})`.
2.4. Slash next message clears the flag and dispatches normally (workflow stays paused).
2.5. Resume continues from the paused invocation; downstream capsules appear.
2.6. Sequential pauses handled correctly (pause-resume-pause-resume).
2.7. No regressions.

**Self-check before handoff:**

- [ ] All 7 ACs met
- [ ] Manual smoke: run `kolsch/workflows/hello-paused` in chat; type response; observe resume
- [ ] No regressions

**Handoff trigger:** All boxes checked → "Step 2 Complete — [summary]".

## Step 2 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 2.4 — slash escape. The workflow MUST remain paused (status `WAITING_FOR_INPUT`); the chat just dispatches the slash and returns. Don't accidentally clear the run's pause state.
- AC 2.6 — sequential pauses. Each pause + resume is a fresh executor instance (Phase 3.4 ships resume as a new method on a new executor). Verify the chat correctly re-detects pause after each resume.

---

# STEP 3 — `/fork` slash + remaining Phase Tests

**Goal**: ship `/fork <event_id> [with k=v...]` and wire Phase Tests 5, 6, 9, 10.

**Files created/modified:**

- `src/cli/_slash.py` (modified) — register `/fork` in `COMMAND_REGISTRY`; add handler.
- `tests/component/test_chat_workflow_slash.py` (modified) — Phase Tests 5, 6, 9 wired; Phase Test 10 (regression) audited.

**Effort**: ~1.5 days.

## Step 3 — Coder Prompt

> Reply "Step 3 Complete — [summary]", "Blocked — ...", or "Decision needed — ...".

**Your task:**

Ship `/fork` + final integration tests.

**Authoritative references — read BEFORE coding:**

1. `process/phase3.9-chat-surface-build.md` — Step 3.
2. `process/phase3.9-chat-surface-spec.md` — §15.1.
3. `process/phase3.8-fork-build.md` — fork machinery.

**What to implement:**

1. **`/fork` handler** in `src/cli/_slash.py`:

   ```python
   async def _handle_fork(text: str, ctx: ChatSessionState) -> SlashSignal:
       if ctx.pending_workflow_run_id is None:
           return SlashSignal.error(
               "no active workflow_run in this chat; run /workflow <ref> first"
           )
       try:
           event_id, overrides = parse_slash_fork_args(text)
       except ValueError as exc:
           return SlashSignal.error(str(exc))
       parent_run_id = WorkflowRunId(ctx.pending_workflow_run_id)
       parent_run = ctx.runtime.workflow_store.read(parent_run_id)
       workflow = load_workflow_from_run(ctx.runtime, parent_run)
       # Create forked run via Phase 3.8 machinery.
       forked_run = ctx.runtime.workflow_store.create(
           workflow_ref=parent_run.workflow_ref,
           inputs=dict(parent_run.inputs),
           trace_id=parent_run.trace_id,
           forked_from=ForkedFrom(
               run_id=parent_run.run_id,
               fork_event_id=event_id,
           ),
       )
       executor = IterativeExecutor(
           workflow=workflow, run=forked_run,
           store=ctx.runtime.workflow_store,
           registry=ctx.runtime.workflow_registry,
           catalog=ctx.runtime.asset_management.catalog(),
       )
       # Update pending to point at the forked run.
       ctx.pending_workflow_run_id = str(forked_run.run_id)
       # Spawn executor + renderer.
       executor_task = asyncio.create_task(
           executor.start_from_fork(
               fork_event_id=event_id,
               input_overrides=overrides,
           )
       )
       renderer = WorkflowCapsuleRenderer(
           console=ctx.console,
           store=ctx.runtime.workflow_store,
           run_id=forked_run.run_id,
       )
       try:
           await renderer.render()
       except (WorkflowRunNotFound, WorkflowRuntimeError) as exc:
           return SlashSignal.error(getattr(exc, "caller_message", str(exc)))
       finally:
           status = await executor_task
       if status == WorkflowRunStatus.WAITING_FOR_INPUT:
           ctx.expecting_user_input_response = True
       return SlashSignal.ok()
   ```

2. **`COMMAND_REGISTRY` extension**:

   ```python
   COMMAND_REGISTRY["/fork"] = SlashCommand(
       name="/fork",
       description="Fork the current workflow_run at a specific event.",
       usage="/fork <event_id> [with key=value ...]",
       handler=_handle_fork,
       free_form_arg_hint="<event_id> [with key=value ...]",
   )
   ```

3. **Tests in `tests/component/test_chat_workflow_slash.py`** (Phase Tests 5, 6, 9, 10):
   - **Phase Test 5:** simulated `/workflow kolsch/workflows/branching-demo --input check_cmd="true"`; pick an event_id from a capsule; simulated `/fork <event_id> with check_cmd="false"`; assert the forked run takes the other branch.
   - **Phase Test 6:** simulated `/fork ev_xxx` with no prior `/workflow`; assert documented error.
   - **Phase Test 9:** sub-workflow capsules render indented (test exists from Step 1; verify still passes after fork integration).
   - **Phase Test 10:** full regression `uv run pytest tests/ -q`.

**Step 3 Acceptance Criteria:**

3.1. `/fork` registered in `COMMAND_REGISTRY`.
3.2. No active `pending_workflow_run_id` → user-friendly error.
3.3. `parse_slash_fork_args` correctly splits event_id + overrides.
3.4. Fork machinery (Phase 3.8) reused verbatim; forked run renders capsules just like a fresh `/workflow`.
3.5. `pending_workflow_run_id` updated to the forked run after `/fork` (chained forks supported).
3.6. Phase Tests 5, 6, 9 pass.
3.7. Phase Test 10 (full regression) passes.

**Handoff trigger:** All ACs met → "Step 3 Complete — [summary]".

## Step 3 — Reviewer Prompt

Verify each AC. PAY EXTRA ATTENTION to:

- AC 3.5 — re-fork chaining. After `/fork ev_xxx`, `ctx.pending_workflow_run_id` should point at the FORKED run. A subsequent `/fork ev_yyy` should fork the FORK, not the original. Verify by test.
- AC 3.7 — regression. The chat agent flow is the load-bearing existing functionality; any regression is a P0.

---

## Done when

- All 4 steps Approved by Reviewer.
- All 10 Phase-level Integration Tests pass.
- `process/phase3.x-roadmap.md` Phase 3.9 line annotated with the shipping SHA.
- Manual smoke test:

  ```bash
  uv run kolsch --root /tmp/kolsch-fresh setup --non-interactive
  uv run kolsch --root /tmp/kolsch-fresh chat
  # In chat:
  # > /workflow kolsch/workflows/hello-paused with greeting=hi
  # (capsules stream; inline prompt capsule appears)
  # > world
  # (workflow resumes; terminal capsule appears)
  # > /workflow kolsch/workflows/branching-demo with check_cmd=true
  # (capsules show the urgent arm)
  # (copy an event_id from a capsule)
  # > /fork ev_abcd1234 with check_cmd=false
  # (forked run takes the other arm)
  ```
