# Phase 3.9 — Chat Surface

This spec covers Phase 3.9 — chat surface for workflows. Depends on Phase 3.1 executor and Phase 3.2 agent integration.

## 15. Chat Surface

### 15.1 Slash commands

- `/workflow <ref>` — invoke a workflow with no inputs.
- `/workflow <ref> with key=value [key=value]...` — invoke with explicit inputs. Same key=value pattern as the CLI; matches the existing `kolsch run` agent convention.
- `/fork <event_id>` — fork the current workflow_run at the named event.
- `/fork <event_id> with key=value [key=value]...` — fork with input overrides.

At v1, `/fork` operates only on workflow_runs. If used in a chat with no active workflow_run, errors with a clear message.

### 15.2 Rendering

- Each workflow node invocation = one chat capsule (collapsible).
- Sub-workflow invocations render as nested capsules.
- `user_input` pauses render inline as a prompt; user's next message becomes the resume. Exact shape locked in §15.2.1.
- Branch-not-taken edges visually muted in the rendered timeline.
- Each `workflow.node.started` event renders a single dim "▸ `<label>`…" line; the matching completion capsule lands beneath it. Exact shape locked in §15.2.2.

**Minimal UX scope at v1 (L2).** The capsule BODY shows the captured output at NODE COMPLETION — the capsule renders once the node's `workflow.node.completed` event lands in the JSONL. Token-by-token streaming (incremental capsule updates while the LLM responds) is DEFERRED to a future phase. No `model.response.delta` event kind is reserved at v1; no `ctx.stream()` method on `NodeContext`. The v1 chat UX is intentionally minimal:

- **No streaming.** Capsules render at `workflow.node.completed`, not mid-call. The §15.2.2 running-node indicator is a separate one-line render on `workflow.node.started` and is NOT a streaming surface — its body never updates after emission.
- **No progress display BEYOND the §15.2.2 indicator.** No spinner animation, no "running step 3 of 7" counter, no live-updating elapsed timer, no multi-pane progress view. The single dim "▸ `<label>`…" line per started node is the WHOLE progress UI; it persists in scrollback unchanged.
- **No cost display.** Dollar-cost UI is agent-layer territory and currently nowhere — capsules don't show estimated or actual cost.
- **No concurrent-node visualization.** Phase 3.10 ships parallel execution; v1 is sequential and capsules render in execution order. When parallelism arrives, the visualization design is a separate question (not pre-locked here).
- **CLI behavior matches.** `kolsch workflows run` (without `--follow`) BLOCKS until completion and prints the final JSON output; `--follow` tails the JSONL as events land but adds no synthesized progress UI.
- **`user_input` is a small inline prompt capsule.** Chat and CLI both render the rendered `prompt:` text as the capsule body — see §15.2.1.

### 15.2.1 Inline `user_input` capsule shape

When a workflow pauses on a `user_input` node, the chat renders one capsule, then unlocks the input loop for the operator's reply (the post-pause routing is locked in §15.2 of `process/phase3.9-chat-surface-build.md`).

The capsule shape:

- **Header** — `display_label(node_def, type_label)` (per §15.4). The header carries the node's IDENTITY, not the question. A long prompt does NOT belong in the header line.
- **Body line 1** — the rendered `prompt:` field as the user_input runner already produces it (`src/workflows/builtins/user_input.py:46-57` renders the Jinja template through the executor's scope and stamps the result on `WorkflowPausedForInput.prompt`). The chat renders that string verbatim, wrapped by the existing capsule body bars; no quoting, no truncation.
- **Body line 2 (when the node binds an input variable)** — a dim `→ binds to inputs.<var_name>` line so sequential pauses are unambiguous when two `user_input` nodes share a label.
- **No placeholder.** Earlier drafts proposed a body line "(awaiting your response)"; that is REMOVED. The chat's input prompt (`:> `) is itself the unmistakable "your turn" cue; a placeholder line is visual noise.

The earlier scheme — header containing the prompt text and body containing "(awaiting your response)" — is GONE. Prompt text belongs in the body (where multi-word strings render readably under the bar prefix); the header stays the node's stable identity.

### 15.2.2 Running-node indicator

Without ANY render between `workflow.node.started` and `workflow.node.completed`, slow nodes make the chat look frozen. The agent-side equivalent is the per-exchange `_THINKING_INDICATOR` in `src/cli/_renderer.py:201` (a single dim italic line emitted once per exchange's first `model.request`). Phase 3.9 ships the workflow analog with the same minimalism.

Behavior, locked:

- **Trigger.** Each `workflow.node.started` event emits exactly one indicator line.
- **Render.** One line of dim text: `▸ <display_label(node_def, type_label)>…`. The arrow glyph (`▸` U+25B8) is the same shape as the workflows tail-follow legend; the trailing `…` is U+2026. Style: `dim` from Rich's palette. No timestamp, no event_id subscript, no border bar (this is NOT a capsule — it's a one-line marker that the matching capsule will land beneath).
- **Lifetime.** The line PERSISTS in scrollback after the capsule lands. It is NEVER overwritten or cleared (no carriage-return tricks, no `Rich.Live` region). For a single-node workflow the rendered sequence is:

  ```
  ▸ classify…
  │ classify · 14:32:01                                   [cyan]
  │ output: customer is requesting refund
  │   ev_<event_id>
  ```

- **Indent.** Nested sub-workflow children indent the indicator at the same depth as the child capsules (§15.2's "Sub-workflow invocations render as nested capsules").
- **Pruned nodes.** No indicator — the `workflow.node.pruned` event has no preceding `workflow.node.started` (pruning happens at edge-evaluation time), so the muted "(not taken)" capsule lands standalone.
- **Paused nodes.** The indicator fires on `workflow.node.started`; the inline `user_input` capsule (§15.2.1) lands beneath it; together they form the "started; now waiting for you" sequence operators read top-to-bottom.
- **Failed nodes.** The indicator fires on `workflow.node.started`; the failure capsule (§15.5 `caller_message`) lands beneath it.
- **Sub-workflow `workflow:` nodes.** The indicator fires for the parent node itself; the child's own per-node indicators and capsules render indented beneath the parent's indicator and capsule.

This is the WHOLE progress UI at v1. A future phase may add a spinner inside a `Rich.Live` region for streaming LLM-token previews; until then, the indicator is the sole "this is moving" signal.

TUI mode, multi-pane progress views, cost dashboards, and concurrent-node graph visualization are all deferred. The principle: ship the bare minimum that lets authors run workflows interactively, learn from real usage, and design the richer UX once we know what authors actually want.

### 15.3 Event ID surface

Each capsule shows its event_id (subscript or hover) so users can pick fork points without digging through CLI.

### 15.4 Capsule labels — 3-step fallback

Each capsule renders a one-line label. The fallback is a strict 3-step rule (introduced in Round 4 #6 alongside the type-level `label:` property):

1. **If the node INSTANCE declares `description:`** (the per-instance modifier from §2.6 of `docs/explanation/workflow-foundation.md` and §6.3 of `process/phase3.0-workflow-spec.md`) — render that string verbatim. This is the workflow-author override; it ALWAYS wins. `description:` is a USER AID for display, never an identifier.
2. **Else if the node TYPE declares a `label:`** (§2.6.1 of `docs/explanation/workflow-foundation.md` — built-in `LABEL = "..."` module constant, or `@node(label="...")` kwarg on authored Python) — render the type-level label. The canonical built-in labels are listed in the §4.0 table of `process/phase3.0-workflow-spec.md`.
3. **Else fall back to the cleaned class/function name** — for authored Python, the `run` function's enclosing module/function name (snake_case → title-case). For built-ins, the type's class name. This is the universal last-resort and guarantees there's always something legible.

`description:` REMAINS OPTIONAL universally. Authors are not required to write one; the type-level `label:` guarantees a reasonable default for every node type.

**Single source of truth: `kolsch.workflows.display_label()` (L7-8).** The 3-step fallback above is implemented exactly ONCE in the codebase, in the public helper `kolsch.workflows.display_label(node: NodeDef, type_label: str | None = None) -> str`. The function is listed in `kolsch.workflows.__all__` (per R4 #5 public-surface discipline) and is the SOLE call site every surface uses — chat capsules (§15.x of this spec), `kolsch workflows show`, `kolsch nodes show`, IDE rendering tooling, and `docs/workflows.md` examples all route through it. There is NO inline 3-step rule reimplemented in any chat-capsule or CLI rendering code. Callers pass the `NodeDef` (which carries the per-instance `description:`) and optionally the resolved type-level label (when the catalog has been consulted to surface it); the helper applies the rules and returns the final label string. Tests of the rule are tests of this one function; chat-renderer tests assume it Just Works.

The earlier per-type "show first 40 chars of the primary config field" rules (e.g., bash → command, branch → condition, write_file → path) are GONE — those mixed identity ("what is this node?") with content ("what does it do right now?"). The 3-step rule keeps the capsule label purely about the node's identity; the run-time content is shown inside the capsule body, not in its title.

### 15.5 Error rendering — `caller_message` is the surface

When an error surfaces in chat (a node fails, the workflow times out, a validation gate trips on invocation), the capsule renders the exception class's `caller_message` (per §12.4 of `process/phase3.0-workflow-spec.md`) — NOT the engineer-targeted `str(exc)`. The engineer-targeted single-line form remains available for log/CLI surfaces; chat is end-user-facing.
