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
- `user_input` pauses render inline as a prompt; user's next message becomes the resume.
- Branch-not-taken edges visually muted in the rendered timeline.

**Minimal UX scope at v1 (L2).** The capsule BODY shows the captured output at NODE COMPLETION — the capsule renders once the node's `workflow.node.completed` event lands in the JSONL. Token-by-token streaming (incremental capsule updates while the LLM responds) is DEFERRED to a future phase. No `model.response.delta` event kind is reserved at v1; no `ctx.stream()` method on `NodeContext`. The v1 chat UX is intentionally minimal:

- **No streaming.** Capsules render at `workflow.node.completed`, not mid-call.
- **No progress display.** No spinner, no "running step 3 of 7", no per-node elapsed timer beyond what completion timestamps provide.
- **No cost display.** Dollar-cost UI is agent-layer territory and currently nowhere — capsules don't show estimated or actual cost.
- **No mid-run status.** The chat doesn't show "workflow is currently executing the `classify` node"; the user sees the next completed capsule when it lands.
- **No concurrent-node visualization.** Phase 3.10 ships parallel execution; v1 is sequential and capsules render in execution order. When parallelism arrives, the visualization design is a separate question (not pre-locked here).
- **CLI behavior matches.** `kolsch workflows run` (without `--follow`) BLOCKS until completion and prints the final JSON output; `--follow` tails the JSONL as events land but adds no synthesized progress UI.
- **`user_input` is a small inline prompt.** Chat surface: a one-line prompt referencing the input variable name; CLI surface: same one-line prompt from the resume command's perspective.

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
