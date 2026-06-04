# QoL — Wire `PermissionsBinding` at tool dispatch so `check`/`require` from tool bodies actually work

**Draft 0.1** *(stub spec, filed alongside the E-QOL-PERMDISC-001 resolution. Captures a latent Phase 2.2 carry-over the Step 1 dev verification surfaced. Not yet sequenced.)*

> `src/tools/permissions.py` defines `PermissionsBinding`
> with methods `check(action, target)` and
> `require(action, target)`. The intent: tool bodies can
> call `ctx.permissions.check("workspace.git.write",
> target=path)` to verify they're authorized before running
> a side-effect.
>
> Today `permissions.bind(...)` is **never called from
> production code**. The PermissionsBinding class exists,
> its methods are defined, but the context attribute
> (`ctx.permissions`) is never populated. Tools that try to
> invoke `ctx.permissions.check(...)` or `require(...)` from
> their bodies raise `ToolPermissionsNoContext` immediately.
>
> Filed from `qol-tool-permission-discovery-uniform-with-stacking.md`
> Step 1 verification — surfaced as an orthogonal finding
> while the dev was tracing the dispatch boundary.

---

## Part 0 — Where the gap is

1. **Definition (lives)**: `src/tools/permissions.py`
   declares `PermissionsBinding`, `check`, `require`, and
   the `ToolPermissionsNoContext` error.

2. **Context attribute (declared but never populated)**:
   `ToolContext.permissions` (verify in
   `src/tools/decorator.py` or `src/tools/subsystem.py`)
   is documented as a `PermissionsBinding | None`. Tools
   that read it find `None` because no code path calls
   `binding.bind(...)` to populate it.

3. **Call sites (zero)**: a grep for
   `permissions.bind(`, `PermissionsBinding(`,
   `.bind_permissions(` returns no production hits — only
   the definition itself.

4. **User-visible symptom**: a tool author who reads the
   `PermissionsBinding` docstring writes
   `ctx.permissions.check(...)` and gets a
   `ToolPermissionsNoContext` at runtime. The error message
   names the gap but doesn't tell them how to fix it
   (because the fix is in the platform, not the tool).

---

## Part 1 — Why this matters

Phase 2.2 shipped agent-level permissions and the
`PermissionsBinding` surface as the in-tool authz API. The
intent: tool authors can guard specific actions inline:

```python
@tool(name="git_force_push", ...)
async def git_force_push(args, ctx):
    ctx.permissions.require("workspace.git.force_push", target=args.branch)
    # ... do the push
```

Without `bind(...)` ever firing, this pattern is dead. Tool
authors who try it get an unhelpful error. The four
builtin tools don't use this pattern (they rely on the
dispatch-boundary authz check at `src/tools/dispatch.py:84`
instead), so the gap is invisible until a user-authored
tool tries the in-body API.

---

## Part 2 — What needs to change

1. **Populate `ctx.permissions`** at tool invocation. The
   dispatch code (`src/tools/dispatch.py`) builds the
   `ToolContext`; this is the natural site to construct and
   bind a `PermissionsBinding` instance.

2. **Binding payload** — the PermissionsBinding needs:
   - A reference to the authz provider (so `check` can
     run `authz.allow(...)`).
   - The principal (`agent.<agent_id>`).
   - The agent's permissions list (for the in-tool check
     to participate in the same four-level stack as the
     dispatch-boundary check shipped by
     `qol-tool-permission-discovery-uniform-with-stacking.md`).
   - The bundle name (for bundle-level rules).

3. **Lifecycle** — the binding is per-invocation (one per
   tool call). After the call returns, the binding can be
   discarded. Stateless aside from the held authz reference.

---

## Part 3 — Design questions worth resolving before specing the build

1. **Action namespace for in-tool checks.** A tool author
   calling `ctx.permissions.check("foo.bar")` is providing
   their own action string. Should the platform prefix it
   (`tool.<name>.foo.bar`)? Pass through as-is? Validate
   against a known taxonomy?

2. **Interaction with the dispatch-boundary check.** The
   tool is already authorized to run (`tool.<name>`
   passed at dispatch). The in-body `check` is for a
   FINER-grained action (e.g., `workspace.git.write`).
   Does the four-level stack apply? Probably yes —
   uniformity matters.

3. **Modal trigger from in-tool check?** The
   modal-on-no-rule path from
   `qol-tool-permission-discovery-uniform-with-stacking.md`
   fires when the dispatch-boundary check finds no rule.
   Should in-tool `require(...)` ALSO trigger the modal
   when no rule covers the requested action? Probably
   yes, for consistency — but it interrupts the tool's
   execution flow with a modal mid-run. Worth thinking
   about UX.

4. **Should `check` be sync or async?** Today the dispatch-
   boundary `authz.allow` is async. `PermissionsBinding`'s
   methods might need to be async too, which changes the
   in-tool calling convention.

---

## Part 4 — Suggested step shape (when picked up)

- **Step 1**: verification + design picks for the four
  open questions above + decide who in dispatch builds the
  binding.
- **Step 2**: implement binding construction at dispatch +
  populate `ctx.permissions` + tests for `check`/`require`
  happy paths and deny paths.
- **Step 3**: docs covering the in-tool API + a worked
  example.

Likely 60-100 LOC code + tests.

---

## Part 5 — Related work

- **Filed from**:
  `qol-tool-permission-discovery-uniform-with-stacking.md`
  Step 1 verification.
- **Composes with**: the four-level stack shipped by that
  QoL. In-tool checks should participate in the same
  stacking; otherwise the in-tool API has different
  semantics from the dispatch-boundary check.
- **Depends on**:
  `qol-tool-permission-discovery-uniform-with-stacking.md`
  must ship first (it ships the agent_permissions threading
  at dispatch that the binding needs to call into).
- **Sibling stub filed alongside this one**:
  `qol-action-namespace-bundle-scoped.md`.

---

## Part 6 — When to pick this up

- When a user files a bug because they tried
  `ctx.permissions.check(...)` and got
  `ToolPermissionsNoContext`.
- When the canonical bundle wants to ship a tool that
  uses in-body permission checks for fine-grained
  actions (e.g., a `git_ops` tool that checks
  `workspace.git.write` vs. `workspace.git.force_push`).
- When third-party bundles emerge and start documenting
  the `ctx.permissions` API for their own tools.

Not urgent for v1; the four builtins don't need it.
