# Step 1 writeup — qol-tool-permissions-binding-unused

**Author:** developer
**Status:** PROPOSE-TO-PROCEED.

---

## 1. Spec correction — the API isn't `ctx.permissions`

The stub spec's Part 0 §2 says `ToolContext.permissions` is "declared but never populated." That's not quite right:

- `ToolContext` (`src/tools/decorator.py:29-72`) has **no `permissions` field** today. Fields are `agent_id`, `session_id`, `thread_id`, `principal`, `secrets`, `workspace_root`, `path_policy`, `tool_call_id`. No `permissions`.
- The real API is **module-level**, via a `ContextVar`:
  ```python
  from tools import permissions
  await permissions.require("workspace.git.force_push", args.branch)
  ```
- Binding is via `permissions.bind(PermissionsBinding(...))` / `permissions.reset(token)` — the dispatch wraps the tool call in this pair.

The spec example showing `ctx.permissions.check(...)` was the author's *intent* for the API shape, not the existing one. The Phase 2.2 implementation landed on a ContextVar-based design (see `src/tools/permissions.py:21-48` for the rationale).

**Implication for the fix**: we don't need to add a `permissions` field to `ToolContext`. We need `dispatch_tool` to call `permissions.bind(...)` immediately before invoking the tool function and `permissions.reset(token)` in a `finally`. The existing `check`/`require` functions are correct as-is.

---

## 2. Resolutions to Part 3 design questions

### Q1 — Action namespace for in-tool checks

**Decision: pass through as-is.** The tool author owns the action string they pass to `permissions.check("X")` / `require("X")`. The platform doesn't prefix or rewrite it. The rule author who wants to authorize a fine-grained action writes `what: X` with the same string.

Rationale: prefixing (e.g., `tool.<name>.X`) couples the rule's grammar to the tool's identity, defeating the point of in-body checks for cross-cutting capabilities like `workspace.git.write`. The author who DOES want tool-scoped checks can prefix manually (`what: kolsch/tools/git_ops.workspace.git.write`) — but that's their choice, not ours.

### Q2 — Four-level stack participation

**Decision: yes, in-tool checks use the full agent → bundle → platform → default stack.** Same composition as the dispatch-boundary check.

To make this work, `PermissionsBinding` needs **both** `agent_permissions` and `bundle_permissions`. Today it carries only `agent_permissions` (line 80). Step 2 adds `bundle_permissions: list[Rule] = field(default_factory=list)` and the binding constructor at dispatch passes both.

The provider's existing `allow(layers=(...))` API supports this directly — `PermissionLayer(name="agent", ...)` + `PermissionLayer(name="bundle", ...)` in priority order.

### Q3 — Modal trigger from in-tool check

**Decision: no.** The in-tool `check`/`require` does NOT fire the modal on no-rule.

Rationale: the dispatch-boundary check is the right place to ask "do you want to allow this tool?" — it's a discoverable single decision the user makes once. The in-tool check is a defensive guard fired by the tool body, often inside a hot loop or mid-iteration over many sub-actions. Firing a modal mid-execution would:

- Block the tool's coroutine until user input, breaking the framework's timeout/cancel contract.
- Surface the modal to the user in a context where they have no idea what the tool is doing or why the check fires.
- Conflict with the existing `require()` semantics — already documented (line 130-140) as "raises `AuthzDenied` on anything other than ALLOW" with PROMPT-without-callback collapsing to deny.

So `check`/`require` behavior is: walk the stack, return effect (or raise on `require`). `check` returns `PROMPT` if a prompt rule matches but the user is in a non-chat context — the tool can branch on it. `require` treats `PROMPT` and `DENY` the same.

If a future user complaint surfaces the need for in-tool modals, that's a separate ticket.

### Q4 — Sync vs async

**Decision: stays async.** Already implemented this way (lines 96, 130). No change.

---

## 3. Implementation plan

### Files touched (3)

1. **`src/tools/permissions.py`** — `PermissionsBinding` gains `bundle_permissions: list[Rule] = field(default_factory=list)`. `check` / `require` walk both layers when building the `layers=` tuple.

2. **`src/tools/dispatch.py`** — between Step 3 (secrets) and Step 4 (sandbox), construct the binding and wrap the `_work` closure body in `bind` / `reset`:

   ```python
   binding = PermissionsBinding(
       provider=authz,
       agent_permissions=list(agent_permissions or []),
       bundle_permissions=list(bundle_permissions or []),
       principal=context.principal,
       tool=defn.name,
   )

   async def _work() -> Any:
       token = permissions.bind(binding)
       try:
           return await defn.func(validated_args, invocation_context)
       finally:
           permissions.reset(token)
   ```

   The wrap lands *inside* `_work` so the binding lifetime exactly matches the tool function body (not the sandbox setup). Sandbox setup logic doesn't call into `permissions.check` — only the tool body does.

3. **`tests/component/tools/test_permissions_api.py`** — gains two new tests:
   - `test_dispatcher_binds_permissions_around_tool_function` — invoke a tool via `dispatch_tool` whose body calls `permissions.check(...)`; assert the binding is populated and returns the right effect.
   - `test_dispatcher_binding_includes_bundle_layer` — bundle rule matches a finer-grained action; tool body's `require()` succeeds.

### Lifecycle

- One binding per `dispatch_tool` call. Sandbox-internal retries (none today, but if added later) would share the binding — correct, since they're the same tool invocation.
- The binding is frozen (already true via `@dataclass(frozen=True, slots=True)`), so the tool body can't mutate the engine's authz state through it.
- The `try`/`finally` ensures the ContextVar is restored on BOTH success and exception paths. Already covered by `tests/component/tools/test_permissions_api.py::test_context_var_cleared_after_clean_exit` + the exception-path sibling.

### Acceptance criteria self-check

| Criterion | Verifying test |
|---|---|
| `ctx.permissions.check(...)` works — actually `permissions.check(...)` from `tools` module | `test_dispatcher_binds_permissions_around_tool_function` |
| In-tool check participates in four-level stack | `test_dispatcher_binding_includes_bundle_layer` |
| ContextVar cleared on exception | already covered by existing `test_context_var_cleared_after_exception_path` |
| Binding lifecycle is per-call | implicit in the dispatch wrap |

---

## 4. Risks

- **R1**: There's no existing user of `permissions.check`/`require` in the canonical bundle's four builtins (they rely on dispatch-boundary authz alone). So this QoL ships a feature with no in-tree consumer beyond tests. The user-visible win is for *future* user-authored bundles. Acceptable — the spec calls this out in Part 6.

- **R2**: The dispatch wraps `_work` body in bind/reset. If `sandbox.execute(_work, policy={})` does anything fancy with the work closure (retries, parallel runs), the binding would propagate. Today `sandbox.execute` is a passthrough; not a current concern.

- **R3**: The principal-and-tool fields on `PermissionsBinding` already exist; just gain `bundle_permissions`. No public-API churn.

---

## 5. Step 2 ship checklist

1. `PermissionsBinding.bundle_permissions` field added.
2. `check` / `require` pass both `agent_permissions` AND `bundle_permissions` as `PermissionLayer`s in `layers=` (agent first, then bundle).
3. `dispatch_tool` constructs the binding and wraps `_work` body in `bind/reset`.
4. Two new regression tests added; existing `test_permissions_api.py` still passes.
5. No `ToolContext` changes (the API is module-level, not field-based).

---

**Verdict from developer**: ready for Step 2.
