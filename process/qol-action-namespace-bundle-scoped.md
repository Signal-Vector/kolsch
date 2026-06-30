# QoL — Unify the tool action namespace + rule `what:` on the 3-seg catalog ref (`<bundle>/tools/<name>`)

**Draft 0.2** *(supersedes 0.1's `tool.<bundle>.<name>` dotted-form proposal. Investigation surfaced that the codebase already runs three grammars for one identity — author-facing `declared:` uses 3-seg `<bundle>/tools/<name>`, rule `what:` uses 2-seg `<bundle>/<name>`, runtime action uses 1-seg `tool.<name>` — bridged by a translation map with a documented last-write-wins ambiguity. 0.1 would have added a fourth grammar. 0.2 picks the catalog ref as the one true form and **deprecates the 2-seg rule shape** the human-readable QoL shipped alongside it. Filed from `qol-tool-permission-discovery-uniform-with-stacking.md` E-QOL-PERMDISC-001 coordinator resolution; sequenced after that QoL has shipped.)*

> The current authz system has three different shapes for
> "this is the bash tool" depending on which surface you're
> reading. Authors type `kolsch/tools/bash` in
> `subsystems.tools.config.declared`. They type `kolsch/bash`
> in `permissions.what`. The runtime constructs `tool.bash`
> for the action passed to `authz.allow`. An explicit
> translation table in `src/providers/authz.py:144`
> bridges the action and the rule shape — and that map has
> a known last-write-wins ambiguity when two bundles ship a
> tool of the same name (its docstring points here as the
> fix).
>
> This QoL collapses all three onto the canonical 3-seg
> catalog ref. The action becomes
> `<bundle>/tools/<name>`. The rule `what:` field becomes
> `<bundle>/tools/<name>`. The translation map is deleted.
> Bundle-rooted action names mean `kolsch/tools/bash` and
> `andre/tools/bash` are finally distinct authz subjects.

---

## Part 0 — What needs to change

### 0.1 The three-grammars table (today)

| Surface | Form | Example |
|---|---|---|
| `subsystems.tools.config.declared[*].ref` | 3-seg catalog ref | `kolsch/tools/bash` |
| `permissions[*].what` (post human-readable QoL) | 2-seg | `kolsch/bash` |
| Runtime action passed to `authz.allow` | 1-seg with `tool.` prefix | `tool.bash` |

Bridged by `_build_action_to_tool_ref` (`src/providers/authz.py:144-178`) which fans every action `tool.<name>` to its rule shape `<bundle>/<name>` so a rule `what: kolsch/bash` can fnmatch against `tool.bash`. The bridge silently last-write-wins on cross-bundle name collisions (the comment block at lines 154-160 calls out the ambiguity and references this spec).

Even the docs disagree with themselves: `docs/how-to/permissions.md` uses 2-seg `kolsch/bash`; `docs/workflows.md` and `docs/explanation/workflow-policy-model.md` use 3-seg `kolsch/tools/bash`. Nobody noticed because the translation map made both work.

### 0.2 The three-grammars table (after this QoL)

| Surface | Form | Example |
|---|---|---|
| `subsystems.tools.config.declared[*].ref` | 3-seg (unchanged) | `kolsch/tools/bash` |
| `permissions[*].what` | 3-seg | `kolsch/tools/bash` |
| Runtime action passed to `authz.allow` | 3-seg | `kolsch/tools/bash` |

One grammar. Authors copy-paste between fields. Translation map is deleted. Cross-bundle name collisions are structurally impossible (the bundle segment is always present).

Nodes already use 3-seg `<bundle>/nodes/<name>` for rule `what:` (per Phase 3.0 Step 4); this QoL brings tools into alignment.

### 0.3 Concrete code changes

1. **`src/tools/decorator.py:124-128`** — `ToolDefinition.action_name` becomes `f"{home_bundle}/tools/{name}"`. Decorator can't know the home bundle at import time, so this property gains a fallback path: when `home_bundle` is unset, `action_name` raises a clear error. Registration sets it (Option B per spec 0.1 Part 2 — the engine already passes `home_bundle` through `register_for_agent` to source bundle permissions; the same value sets the ToolDefinition's bundle).

2. **`src/tools/registry.py:38-56`** — `_AgentRegistration` gains a per-tool `home_bundle` stamp at registration time. Builtins (registered via `_make_builtin` in `subsystem.py:235`) get attributed to `kolsch`.

3. **`src/tools/dispatch.py:113-120`** — passes the new 3-seg action string through to `authz.allow` and the error envelopes.

4. **`src/tools/subsystem.py:266`** — same for the MCP path; MCP retains its `mcp.<server>.<tool>` namespace (MCP servers aren't bundles; out of scope).

5. **`src/providers/authz.py:144-178`** — `_build_action_to_tool_ref` is **deleted**. The rule.what is matched directly against the action string. Update the matcher's lookup path accordingly. (Nodes' bridge entry at line 170-177 stays for now — separate ticket.)

6. **`src/cli/_migrations/authz_rules.py`** — the existing startup-time auto-migration shim already rewrites 1-seg `tool\.bash` regex rules to 2-seg `kolsch/bash` glob rules. Extend its target to 3-seg `kolsch/tools/bash`, and add a new heuristic that rewrites 2-seg `kolsch/bash` → 3-seg `kolsch/tools/bash`. Emit a new `config.deprecated_rule_form` Recoverable for the 2-seg → 3-seg path; reuse the existing event for the 1-seg → 3-seg path.

7. **`src/cli/_modal.py:312`** — `action.removeprefix("tool.")` no longer parses the new form. Replace with a 3-seg parser (`bundle, "tools", name = action.split("/", 2)` or equivalent) that yields the tool name for the modal display.

8. **`src/cli/_slash.py:1744`** — `action = f"tool.{tool_name}"` becomes a registry lookup (the tool's home_bundle is needed to construct the 3-seg form). The slash path needs an agent_id in scope to find the tool's bundle.

9. **`src/cli/_starter_bundle/kolsch/agents/helper.yaml`** — every `what: kolsch/<name>` becomes `what: kolsch/tools/<name>`.

10. **Test cascade** — ~50 test files use `r"tool\.bash"`-style legacy regex rules or 2-seg `kolsch/bash` glob rules in their setup. They get rewritten to 3-seg form. Catalog the grep:
    ```
    grep -rln 'tool\\\.bash\|"kolsch/bash"' tests/ src/
    ```

11. **Docs cascade** — see Part 4.

---

## Part 1 — Why the three grammars exist today

Five QoLs touched this surface, each picking a different segment:

- **Phase 2.0 Tools** shipped `tool.<name>` as the action namespace (single segment was sufficient with one canonical bundle).
- **`qol-prefix-defaults-and-unified-refs.md`** unified author-facing refs on 3-seg `<bundle>/<plural>/<name>`. It deliberately did NOT touch the runtime action namespace.
- **`qol-permission-system-human-readable.md`** rewrote rule schema to `{what, pattern, effect}` and picked 2-seg `<bundle>/<name>` for `what:` (reasoning: shorter is more readable for permission rules). Bridged to the unchanged 1-seg action via `_build_action_to_tool_ref`. Documented the limitation; pointed here for the fix.
- **`qol-tool-permission-discovery-uniform-with-stacking.md`** added the four-level stack and the no-rule modal. Its Decision #5 explicitly defers the action-namespace migration to this spec (E-QOL-PERMDISC-001 coordinator resolution).
- **Phase 3.0 Step 4 (workflow nodes)** shipped node actions as `node.<name>` with rule `what:` as 3-seg `<bundle>/nodes/<name>`. Same translation-map pattern as tools, but with a different rule-shape decision. Tools and nodes already disagree on which segment of the grammar carries the bundle.

The 2-seg readability argument from the human-readable QoL was the right call at the time given that 3-seg was already in use for the `declared:` field — but the field-by-field shape difference is exactly what the user notices as inconsistency. One grammar everywhere wins.

---

## Part 2 — Decisions locked

1. **One grammar across action, rule.what, and `declared:`: the 3-seg catalog ref `<bundle>/tools/<name>`.**

2. **Home-bundle attribution at registration time** (Option B from spec 0.1). The engine's `_register_tools_with_subsystem` (`src/engine/engine.py:898`) already looks up the home bundle to source `bundle_permissions`; it passes the same value to `register_for_agent` for each tool. Builtins are attributed to `kolsch`. The `@tool` decorator stays bundle-unaware.

3. **The 2-seg rule.what shape (`kolsch/bash`) is deprecated.** The existing rule-migration shim in `src/cli/_migrations/authz_rules.py` gains a new heuristic that rewrites 2-seg → 3-seg at startup. Emit `config.deprecated_rule_form` Recoverable naming the offending rule. No removal window enforced; warning-only for at least one minor version (matches the policy `qol-prefix-defaults-and-unified-refs.md` set for `config.deprecated_ref_form`).

4. **The 1-seg legacy regex shape (`tool\.bash`) stays deprecated.** Already covered by the existing shim; this QoL extends its rewrite target from 2-seg to 3-seg.

5. **No `kolsch permissions migrate` CLI.** Startup-time auto-migration handles the rewrite. Drop the CLI proposal from spec 0.1.

6. **No convenience glob sugar.** Users wanting "all tools in bundle X" write `<bundle>/tools/*` against the existing glob matcher. No new grammar layer.

7. **MCP namespace unchanged.** `mcp.<server>.<tool>` stays — MCP servers aren't bundles.

8. **Node action namespace unchanged** (already 1-seg `node.<name>` bridged to 3-seg `<bundle>/nodes/<name>` rule.what). Same unification could be done for nodes; out of scope for this QoL.

9. **Event types unchanged.** `tool.called` / `tool.result` / `tool.failed` are event types, not action strings. They live in a different namespace consumed by the event bus, not by `authz.allow`. The accidental shared `tool.` prefix between today's actions and event types goes away as a side effect — both spaces become unambiguous.

10. **The `_build_action_to_tool_ref` map is deleted for tools.** Nodes' entries stay until the node-namespace ticket lands.

---

## Part 3 — Acceptance criteria + step shape

### Acceptance criteria

The QoL ships when **all** hold:

1. The four builtin tools (`bash`, `read_file`, `write_file`, `list_files`) advertise their action as `kolsch/tools/<name>` at the `authz.allow` site.

2. A user-authored bundle tool at `<workspace>/bundles/andre/tools/bash.py` advertises its action as `andre/tools/bash`.

3. A rule `{what: kolsch/tools/bash, pattern: "*", effect: allow}` matches a `kolsch/tools/bash` action invocation without any translation map.

4. `_build_action_to_tool_ref`'s tool-entry path is deleted. The matcher hits rule.what directly against action.

5. Legacy 2-seg rules (`what: kolsch/bash`) auto-migrate to 3-seg at startup. Emit `config.deprecated_rule_form` Recoverable naming the legacy `what:` value.

6. Legacy 1-seg regex rules (`what: tool\.bash`) continue to auto-migrate via the existing shim, now landing at 3-seg.

7. The bundle-collision case works: a rule `{what: kolsch/tools/bash}` does NOT match an `andre/tools/bash` invocation, and vice versa.

8. The starter bundle's `helper.yaml` ships with 3-seg `what:` values.

9. All four docs files in Part 4 use 3-seg in their examples.

10. Regression tests cover: new-form-happy-path, 2-seg-migration, 1-seg-migration, cross-bundle-isolation, modal-render with new action string.

### Out of scope

- **`kolsch permissions migrate` CLI.** Startup-time shim is sufficient.
- **Convenience glob sugar** (`<bundle>/tools/*`).
- **MCP action namespace redesign.**
- **Node action namespace** (separate ticket).
- **Removing the legacy 2-seg form entirely.** Warning-only for at least one minor version.
- **Renaming the `tool.*` event type prefix.** Event types stay; the namespace collision with the old action form resolves itself as actions move out of the `tool.` prefix.

### Step shape

Three steps. Probably-skip the `kolsch permissions migrate` step (Decision #5 above).

- **Step 1** — Verification + plumbing audit + migration impact list. STOP gate.
- **Step 2** — Implement: registration plumbing, action shape change, migration shim extension, translation map delete.
- **Step 3** — Starter bundle, ~50 test rule updates, 4 docs files, manual e2e.

---

## Part 4 — Doc cascade

Every file that shows a tool action string or a permission rule with `what:` needs an update. Inventory from grep:

**User-facing docs to convert to 3-seg:**

- `docs/how-to/permissions.md` — every `what: kolsch/<name>` becomes `what: kolsch/tools/<name>` (~10 examples).
- `docs/reference/schemas.md` — same (~3 examples).

**Docs already on 3-seg — verify no regressions:**

- `docs/workflows.md` — already uses `what: kolsch/tools/bash`.
- `docs/explanation/workflow-policy-model.md` — already uses 3-seg.

**Docs showing the action string in error/event examples:**

- `docs/explanation/tools-subsystem.md:124,895` — `f"tool.{name}"` → `f"{bundle}/tools/{name}"`.
- `docs/explanation/error-model.md:231,359` — `"action": "tool.bash"` → `"action": "kolsch/tools/bash"`.
- `docs/explanation/contract-audit.md:386` — `authz.allow(principal, "tool.<name>")` → `authz.allow(principal, "<bundle>/tools/<name>")`.
- `docs/explanation/secrets-sandbox-authz-contract.md:560,803` — same.
- `docs/reference/module-api-reference.md:1066` — `f"tool.{tool_name}"` → 3-seg construction.
- `docs/explanation/workflow-policy-model.md:152` — `"action": "tool.bash"` example in event payload.

**Event-bus prefix wildcards in docs that are NOT actions (no change needed):**

- `docs/explanation/event-bus-contract.md:157-158` — `"tool.*"` matches event types like `tool.failed`. Leave as-is; event types keep their `tool.` prefix.
- `docs/reference/module-api-reference.md:680` — same.

**Spec inventory the writeup should verify before Step 3:**

- `process/qol-permission-system-human-readable.md` — update with a follow-up note that 2-seg has been superseded.
- `process/qol-tool-permission-discovery-uniform-with-stacking.md` — Decision #5's v1 caveat about bundle collision can be marked resolved.

---

## Part 5 — Related work

- **Filed from**: `qol-tool-permission-discovery-uniform-with-stacking.md` E-QOL-PERMDISC-001 resolution.
- **Supersedes its own 0.1 draft**: the dotted-form `tool.<bundle>.<name>` proposal is dropped in favor of the 3-seg catalog ref shipped by `qol-prefix-defaults-and-unified-refs.md`.
- **Deprecates 2-seg `<bundle>/<name>` rule.what** shipped by `qol-permission-system-human-readable.md`. Existing rule-migration shim handles the rewrite; new deprecation event class added.
- **Parallel pattern**: `qol-prefix-defaults-and-unified-refs.md`'s `config.deprecated_ref_form` Recoverable — same pattern, new event code.
- **Sibling stub filed alongside the 0.1 draft**: `qol-tool-permissions-binding-unused.md`.

---

## Part 6 — When to pick this up

- **Now-ready trigger**: shipped. `qol-tool-permission-discovery-uniform-with-stacking.md`'s four-level stack is in; the bundle-collision limitation is now the most visible papercut for users authoring more than one bundle.
- **Forced trigger**: a user files a bug or qol complaint about two same-named tools sharing rules.
- **Coordination**: any new doc or how-to that ships rule examples should use 3-seg from day one; the doc cascade in Part 4 catches existing ones at Step 3.

Not urgent for v1; documented v1 caveat in `docs/how-to/permissions.md` per `qol-tool-permission-discovery-uniform-with-stacking.md` Step 3.
