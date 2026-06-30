# Step 1 writeup — qol-action-namespace-bundle-scoped

**Author:** developer
**Status:** PROPOSE-TO-PROCEED — no escalation conditions hit; Step 2 ready once reviewer approves.

---

## 1. Prerequisite QoL status

`qol-tool-permission-discovery-uniform-with-stacking.md` is shipped on `main`. The four-level stack (agent → bundle → platform → default) and the no-rule modal are live; the engine's `_register_tools_with_subsystem` (`src/engine/engine.py:898+`) already resolves `home_bundle = self._assets.get_bundle_for_agent_id(self.agent_id)` (line 952) and threads `bundle_permissions` through to the registry. That same `home_bundle` value is the input this QoL needs for tool attribution — no new lookup site required.

The two `_build_action_to_tool_ref` map entries (tool + node) are also live; this QoL deletes the tool entry while leaving the node entry in place per spec Decision #10.

---

## 2. Home-bundle plumbing trace (Option B confirmed)

| Step | File | Line | What it does |
|---|---|---|---|
| Resolve | `engine/engine.py` | 952 | `home_bundle = self.engine._assets.get_bundle_for_agent_id(self.agent_id)` — already cached, already populated |
| Pass | `engine/engine.py` | 961–967 | `register_for_agent(agent_id, builtin_names, declared_paths, agent_permissions, bundle_permissions)` — needs new `home_bundle=` kwarg |
| Receive | `tools/subsystem.py` | 82–92 | `register_for_agent` signature — add `home_bundle: str \| None = None` (None for legacy callers / tests) |
| Stamp builtins | `tools/subsystem.py` | 124–127 | `_make_builtin(name)` is called once per builtin; after `get_tool_definition` returns, set `defn.home_bundle = "kolsch"` (per Decision #8 of the spec: builtins are attributed to `kolsch`) |
| Stamp declared | `tools/subsystem.py` | 118–122 | `discover_tools_in_file(path)` returns functions; after `get_tool_definition`, set `defn.home_bundle = home_bundle` (the value passed into `register_for_agent`) |
| Stamp directly-passed | `tools/subsystem.py` | 113–116 | Same treatment for the `tools=` list (test helper); stamps with `home_bundle` if supplied, else leaves None |

**Decision A1**: when `home_bundle is None` AND a stamp is attempted on a passed-in tool (Phase 1 test helper path), leave `defn.home_bundle = None`. The `action_name` property falls back to the legacy form (see §3 below).

**Decision A2**: `discover_tools_in_file` is **not** modified to derive bundle from path. The path may be `<workspace>/bundles/andre/tools/bash.py` and the bundle is parseable from the path segment after `bundles/`, but Option B has the engine pass `home_bundle` in explicitly; the discovery helper stays single-purpose.

---

## 3. ToolDefinition + action_name property

`src/tools/decorator.py:103-135` — `ToolDefinition` dataclass gains one field:

```python
home_bundle: str | None = None
```

`action_name` property (line 123-128) changes:

```python
@property
def action_name(self) -> str:
    if self.action is not None:
        return self.action
    if self.home_bundle is None:
        # Phase 1 test path — no bundle attribution; legacy 1-seg form
        # preserved for programmatic registrations that don't go through
        # the engine. The engine path always sets home_bundle.
        return f"tool.{self.name}"
    return f"{self.home_bundle}/tools/{self.name}"
```

**Why the legacy fallback**: there are tests in `tests/component/test_tools.py` and `tests/component/tools/test_permissions_api.py` that construct `ToolDefinition` directly via `register_for_agent(tools=[fn], ...)` without going through the engine. Those tests are programmatic registrations exercising tool-subsystem internals; forcing them to set `home_bundle` would expand Step 3's test cascade significantly without testing new behavior. The fallback lets those tests keep working byte-identical; Step 2's regression tests for the new form go through the engine path.

The `@tool` decorator stays bundle-unaware (Option B). No new kwarg on `@tool`.

---

## 4. Action-string sites — full inventory

### Construction (3 sites)

| File | Line | Today | After |
|---|---|---|---|
| `src/tools/decorator.py` | 128 | `f"tool.{self.name}"` | `f"{self.home_bundle}/tools/{self.name}"` (fallback to legacy when home_bundle is None — see §3) |
| `src/cli/_slash.py` | 1744 | `f"tool.{tool_name}"` | Registry lookup: find the tool by name in the agent's registration, read its `defn.action_name`. `agent_id` is already in scope at the call site. |
| `src/tools/subsystem.py` (MCP path) | 266 | `action = invocation_name` | **Unchanged** — MCP uses `mcp.<server>.<tool>` per Decision #7; out of scope. |

### Parsing (1 site)

| File | Line | Today | After |
|---|---|---|---|
| `src/cli/_modal.py` | 312 | `tool_name = action.removeprefix("tool.")` | Parse 3-seg `<bundle>/tools/<name>`: `tool_name = action.rsplit("/", 1)[-1]` (last segment). Falls through cleanly on legacy 1-seg too (no `/` → returns whole string after `removeprefix`). Single guarded change. |

The modal's `_modal.py:482` docstring example (`"tool.bash"`) gets updated to `"kolsch/tools/bash"` in the same edit.

---

## 5. Translation map deletion plan

`src/providers/authz.py:144-178` — `_build_action_to_tool_ref` currently returns a dict mapping action strings to rule-shape strings. The function builds two kinds of entries:

- **Tool entries** (lines 162-169): `tool.<name> → <bundle>/<name>`. **Delete.** After this QoL, the action string IS the 3-seg ref; no mapping needed.
- **Node entries** (lines 170-177): `node.<name> → <bundle>/nodes/<name>`. **Keep.** Nodes still use their 1-seg action namespace; out of scope per Decision #8.

Three other sites reference the map's tool entries:

| Site | File | Line | Disposition |
|---|---|---|---|
| Construction | `authz.py` | 532 | `self._action_to_tool_ref = _build_action_to_tool_ref(...)` — stays (still populated for nodes). |
| Matching | `authz.py` | 1085-1088 | `tool_ref_match = tool_ref is not None and fnmatch.fnmatchcase(tool_ref, what_pattern)` — stays. After this QoL, `tool_ref` is None for tool actions (only node actions populate the map), so the OR branch never fires for tools. The direct `fnmatch(action, what_pattern)` branch one line up handles all tool matches. **Net behavior**: a rule `what: kolsch/tools/bash` matches the action `kolsch/tools/bash` directly. |
| Public method | `authz.py` | 1249-1252 | `lookup_tool_ref(action)` returns the map value. Callers: `cli/_modal.py` + `cli/_slash.py` use it to construct the `what:` field when persisting a new rule from `[a]/[d]`. After this QoL: `lookup_tool_ref("kolsch/tools/bash")` returns None (not in map); callers fall back to using the action string directly as the rule's `what:`. Net result: persisted rules carry `what: kolsch/tools/bash` — the new form. |

**Decision A3**: `lookup_tool_ref`'s callers need updating to use `action` as the fallback when the lookup returns None. Two grep-narrow callers (modal, slash); ~5 lines total.

---

## 6. Migration shim extension plan

`src/cli/_migrations/authz_rules.py` is run unconditionally at startup via `src/cli/_setup.py:_maybe_migrate_authz_rules_to_qol_schema` (line ~1024, called at line ~387). It walks `platform.yaml`'s `authorization.config.rules` list, runs `_try_migrate` per entry, and emits `config.deprecated_regex_rule` events for the ones that survive in legacy form.

### Heuristic addition

`_try_migrate` (lines 148-207) tries four heuristics in priority order: catch-all, agent.execute, tool action, bash-suggest. **Add a fifth** between heuristics 3 and 4:

```python
# NEW — heuristic 3b: 2-seg glob `<bundle>/<name>` → 3-seg `<bundle>/tools/<name>`
# Fires for already-migrated 2-seg rules from the human-readable QoL.
# Catalog confirms the bundle owns a tool of that name.
two_seg_match = _TWO_SEG_GLOB_RE.match(what)
if two_seg_match and raw.get("pattern") is not None:
    if catalog is None:
        return None
    bundle, name = two_seg_match.group(1), two_seg_match.group(2)
    if not _bundle_owns_tool(catalog, bundle, name):
        return None
    return {
        "what": f"{bundle}/tools/{name}",
        "pattern": raw["pattern"],
        "effect": effect,
    }
```

with `_TWO_SEG_GLOB_RE = re.compile(r"^([a-z0-9][a-z0-9_-]*)/([a-z][a-z0-9_-]*)$")` and a small `_bundle_owns_tool` helper that walks `catalog.assets` looking for `asset_type == "tool"` and `entry.bundle == bundle` and `entry.ref.rsplit("/", 1)[-1] == name`.

**Decision A4**: the new heuristic requires `raw.get("pattern") is not None` — i.e., it only fires on **new-shape** rules (which have a `pattern` field). The 1-seg legacy regex shape (heuristic 3) handles `what: tool\.bash` without a pattern; the 2-seg shape (this new heuristic 3b) handles the new-shape `what: kolsch/bash, pattern: "*"`. The 1-seg heuristic also gets updated to land at 3-seg (`f"{bundle}/tools/{name}"`) instead of 2-seg — see §6.2.

### 1-seg → 3-seg (heuristic 3 update)

Lines 178-190 of `_try_migrate`: the existing tool-action heuristic currently rewrites `tool\.bash` → `kolsch/bash`. After this QoL it rewrites to `kolsch/tools/bash`. Same change for the bash-suggest heuristic at lines 192-205 (`{"what": f"{bundle}/bash"}` → `{"what": f"{bundle}/tools/bash"}`).

### New deprecation event class

`src/errors/authz.py` gains a sibling to `ConfigDeprecatedRegexRule` (existing class for 1-seg → 2-seg path under the human-readable QoL):

```python
class ConfigDeprecatedRuleForm(Recoverable):
    """The 2-seg `<bundle>/<name>` rule shape is deprecated; use 3-seg `<bundle>/tools/<name>`."""
    code = "config.deprecated_rule_form"
```

The 1-seg heuristic continues emitting `config.deprecated_regex_rule` (legacy regex path); the new 2-seg heuristic emits `config.deprecated_rule_form` (decision #3 in spec).

`src/cli/_setup.py:_maybe_migrate_authz_rules_to_qol_schema` walks `migration_result.deprecated` and emits one event per deprecated rule. Two changes: (1) `DeprecatedRegexRule` dataclass in `authz_rules.py` becomes a sealed union (or sibling class) `DeprecatedRule2Seg` so the emitter can pick the right event code; (2) the emitter switch on the dataclass subtype.

---

## 7. Test cascade inventory

Total: **~28 test files** with legacy rule strings to update. Two categories.

### A. Tests using 1-seg regex (`r"tool\.bash"`) — 23 files

These construct legacy-shape rules directly via `Rule(who=".*", what=r"tool\.bash", effect="prompt")`. The rule's `is_legacy` flag fires (presence of `who`); the matcher uses the regex path against `action OR target`.

After this QoL: **tests can be split by purpose**:

1. **Tests asserting that legacy-shape rules still work** (a small subset — `test_authz_qol_migration.py`, `test_authz_qol_schema.py` legacy-compat tests): keep the rule string in legacy form. The matcher's regex path stays. These tests assert the migration shim's behavior; preserving them is correct.

2. **Tests exercising "what happens when a rule matches the bash tool" without caring about legacy shape** (the bulk — `test_chat_modal.py`, `test_chat_deny_stops.py`, `test_permissions.py`, `test_providers.py`, etc.): rewrite the rule construction to use **new-shape 3-seg form**: `Rule(what="kolsch/tools/bash", pattern="*", effect="prompt")`. Same semantic intent; drops the legacy flag; matcher uses the glob path against action directly.

Inventory (file → suggested action):

```
tests/unit/test_authz_qol_schema.py             KEEP legacy in migration tests; MIGRATE others
tests/unit/schemas/test_envelope_fields.py      MIGRATE (4 occurrences)
tests/unit/templating/test_platform_var.py      MIGRATE (2)
tests/unit/templating/test_agent_var.py         MIGRATE (1)
tests/integration/test_authz_qol_migration.py   KEEP (4 — this IS the migration test)
tests/integration/test_perm_system_e2e.py       MIGRATE (4)
tests/integration/test_bundle_permissions.py    MIGRATE (4)
tests/integration/test_no_rule_modal_flow.py    MIGRATE (5)
tests/integration/test_tool_wiring.py           MIGRATE (2)
tests/integration/test_agent_envelope.py        MIGRATE (1)
tests/integration/test_skills_authz.py          MIGRATE (1)
tests/integration/test_integration.py           MIGRATE (1)
tests/component/authz/test_phase2_authz.py      KEEP some, MIGRATE others (10)
tests/component/authz/test_composition.py       MIGRATE (4)
tests/component/test_providers.py               MIGRATE (8)
tests/component/test_setup.py                   MIGRATE (1)
tests/component/test_tools.py                   MIGRATE (1)
tests/component/tools/test_permissions_api.py   MIGRATE (3)
tests/component/cli/test_chat_modal.py          MIGRATE (5)
tests/component/cli/test_chat_deny_stops.py     MIGRATE (2)
tests/component/cli/test_chat_deny_stops_e2e.py MIGRATE (3)
tests/component/cli/test_permissions.py         MIGRATE (7)
tests/component/cli/test_perm_slash.py          MIGRATE (2)
```

### B. Tests using 2-seg glob (`what: "kolsch/bash"`) — 5 files

Already on new-shape; just expand to 3-seg.

```
tests/unit/test_authz_qol_schema.py             19 occurrences (overlap with category A)
tests/integration/test_authz_qol_migration.py   2 (KEEP if testing migration intermediate state)
tests/integration/test_perm_system_e2e.py       3
tests/component/test_workflow_authz.py          8
tests/component/cli/test_perm_slash.py          9
```

### C. Golden YAML workflow fixtures — 5 files

```
tests/golden/workflows/invalid-from-not-found.yaml          MIGRATE
tests/golden/workflows/invalid-from-shape.yaml              MIGRATE
tests/golden/workflows/invalid-from-asset-type.yaml         MIGRATE
tests/golden/workflows/invalid-permissions-rejected.yaml    MIGRATE (1 line)
tests/golden/workflows/valid-policy-from-agent.yaml         MIGRATE (1 line)
```

`tests/golden/workflows/_targets/extends-policy-target.yaml` already uses 3-seg — no change.

### New regression tests

Step 2 should add at least three new tests covering acceptance criteria 5-7:

- **2-seg → 3-seg migration**: load `platform.yaml` with `{what: kolsch/bash, pattern: "*", effect: allow}`; assert the rule is rewritten to `what: kolsch/tools/bash` at startup; assert one `config.deprecated_rule_form` event fired.
- **Cross-bundle isolation**: register a `kolsch` bash tool AND an `andre` bash tool for one agent (synthetic test); a rule `what: kolsch/tools/bash` matches the kolsch action but not the andre action; vice versa with andre rule.
- **Translation map deletion**: rule `what: kolsch/tools/bash` matches action `kolsch/tools/bash` with `_action_to_tool_ref` returning `{}` for tools (only node entries populated).

---

## 8. Doc cascade inventory

### User-facing docs to convert (2 files, ~25 examples)

- `docs/how-to/permissions.md` — lines 29, 56, 125-127, 135, 144, 156-157, 177, 206-214, 232, 273, 316-323. Every `what: kolsch/<name>` → `what: kolsch/tools/<name>`.
- `docs/reference/schemas.md` — lines 421-422. Same.

### Docs showing action-string examples (~10 files)

- `docs/explanation/tools-subsystem.md` — lines 124, 431, 476, 496, 522, 550, 556, 895. `f"tool.{name}"` → `f"{home_bundle}/tools/{name}"`; action examples → 3-seg.
- `docs/explanation/error-model.md` — line 231. `"action": "tool.bash"` → `"action": "kolsch/tools/bash"`.
- `docs/explanation/workflow-policy-model.md` — line 152. Same payload example.
- `docs/explanation/file-handling.md` — lines 324, 369, 474.
- `docs/explanation/secrets-sandbox-authz-contract.md` — lines 384, 526.
- `docs/explanation/contract-audit.md` — line 386. `authz.allow(principal, "tool.<name>")` → `authz.allow(principal, "<bundle>/tools/<name>")`.
- `docs/how-to/extending-an-agent.md` — lines 53, 185-186. Verify tool-ref examples are 3-seg (most likely already are).
- `docs/how-to/chatting-effectively.md` — lines 144, 198, 220-223.
- `docs/reference/module-api-reference.md` — lines 977, 1066, 1783.
- `docs/tutorials/first-agent.md` — line 368.

### Docs already on 3-seg — verify no regressions

- `docs/workflows.md` — already uses `what: kolsch/tools/bash`. No change.
- `docs/explanation/workflow-policy-model.md` — already uses 3-seg for `what:` blocks (the line 152 action-payload example is the only legacy hit).

### Event-bus `tool.*` examples — NO CHANGE

- `docs/explanation/event-bus-contract.md:157-158` — `"tool.*"` is an event-type prefix wildcard, not an action. Stays per Decision #9.
- `docs/reference/module-api-reference.md:680` — same.

### Process/ spec docs — annotation only

- `process/qol-permission-system-human-readable.md` — add a "**SUPERSEDED for action namespace**" note pointing here. Don't rewrite content; the spec is historical.
- `process/qol-tool-permission-discovery-uniform-with-stacking.md` — Decision #5's "v1 caveat" can be marked resolved by this QoL once it ships.
- `process/qol-action-namespace-bundle-scoped.md` — no changes (this spec is the source of truth).

---

## 9. Starter bundle migration

`src/cli/_starter_bundle/kolsch/agents/helper.yaml:36-39`:

```yaml
permissions:
  - who: ".*"
    what: "tool\\.bash"
    effect: prompt
```

Current state: legacy 1-seg regex form. The startup migration shim would auto-rewrite this to 3-seg after the QoL ships, but new workspaces created post-QoL should start with the canonical form. Step 3 rewrites helper.yaml directly:

```yaml
permissions:
  - what: kolsch/tools/bash
    pattern: "*"
    effect: prompt
```

Note: drops the legacy `who:` field (per Decision #1 of the human-readable QoL — new-shape rules have no `who`).

The comment block at lines 27-35 explaining the rule grammar gets updated too: `who` is dropped; the explanation focuses on `what` (3-seg ref) + `pattern` (glob).

---

## 10. Acceptance criteria self-check

| # | Criterion | Step | Notes |
|---|---|---|---|
| 1 | Builtins advertise `kolsch/tools/<name>` | Step 2 | §2, §3 |
| 2 | User-bundle tool at `andre/tools/bash.py` advertises `andre/tools/bash` | Step 2 | §2 (engine passes home_bundle for declared paths) |
| 3 | Rule `what: kolsch/tools/bash` matches action directly | Step 2 | §5 |
| 4 | `_build_action_to_tool_ref` tool branch deleted | Step 2 | §5 |
| 5 | 2-seg rules auto-migrate at startup; emit `config.deprecated_rule_form` | Step 2 | §6 |
| 6 | 1-seg legacy regex rules auto-migrate to 3-seg | Step 2 | §6.2 |
| 7 | Cross-bundle isolation works | Step 2 | §7 regression test |
| 8 | Starter bundle on 3-seg | Step 3 | §9 |
| 9 | Four docs files on 3-seg | Step 3 | §8 |
| 10 | Regression tests cover all migration paths | Step 2 + Step 3 | §7 |

All criteria mapped. No criterion left unassigned.

---

## 11. Risk + escalation

### Escalation conditions checked

(a) **The home-bundle plumbing site already exists.** Engine line 952 resolves `home_bundle`; the spec's "thread through `register_for_agent`" is a one-kwarg addition. NO ESCALATION.

(b) **The translation map's node entries safely coexist with the tool deletion.** The map dict mixes both via separate dict-key namespaces (`tool.X` vs `node.X`); deleting tool entries doesn't touch node entries. The matcher's OR branch at line 1085-1088 gracefully handles `tool_ref is None` (just doesn't fire). NO ESCALATION.

(c) **Test cascade is large but bounded.** 28 files identified; no file requires structural redesign. The new-shape rule construction is a straight string replacement plus dropping the `who:` field. NO ESCALATION.

(d) **The `_modal.py` parser update is a one-line change.** `rsplit("/", 1)[-1]` works for both the new 3-seg form and (degenerately) the legacy 1-seg form (no `/` present, returns the whole string after `removeprefix("tool.")` if added as a fallback). The cleanup is one line. NO ESCALATION.

### Known risks for Step 2

- **R1**: Tests in `test_authz_qol_schema.py` and `test_authz_qol_migration.py` exist specifically to test the human-readable QoL's migration shim. Some of those tests assert that 2-seg form is the **target** of the migration. Step 2 must update those assertions to target 3-seg instead, and the migration shim's intermediate "2-seg state" tests can be deleted (or kept as historical regression). Reviewer should flag if any of these tests can't be cleanly updated.

- **R2**: `lookup_tool_ref` returning None for tool actions changes the persistence path for new rules from the modal. After this QoL, `[a]` from the modal persists `{what: kolsch/tools/bash, pattern: "*", effect: allow}` directly. Verify the modal's rule-construction call site (`cli/_modal.py:_persist_rule` or similar) handles the None return without raising.

- **R3**: Builtins use `home_bundle = "kolsch"` per Decision #8. This hardcodes the canonical bundle name in `tools/subsystem.py:_make_builtin`. If kolsch is ever renamed or vendored, that string needs updating. Acceptable — the package is named `kolsch` everywhere; the hardcode is consistent with the existing `kolsch:` prefix in context_files and the `kolsch/` bundle prefix in catalog refs.

### Step 2 ship checklist

1. `ToolDefinition.home_bundle` field added; `action_name` property updated.
2. `register_for_agent` accepts `home_bundle=` kwarg; stamps it on every registered ToolDefinition.
3. `_make_builtin` callers stamp `home_bundle="kolsch"`.
4. Engine's `_register_tools_with_subsystem` passes `home_bundle=self.engine._assets.get_bundle_for_agent_id(self.agent_id)` to the subsystem.
5. `_build_action_to_tool_ref` tool branch deleted (lines 162-169 removed; node branch at 170-177 stays).
6. `_modal.py:312` parser updated.
7. `_modal.py:482` docstring example updated.
8. `_slash.py:1744` registry-lookup site updated.
9. Migration shim heuristic 3b added; heuristic 3 + bash-suggest land at 3-seg.
10. `ConfigDeprecatedRuleForm` event class added; setup loop emits it.
11. Three new regression tests (per §7).
12. Existing tests still pass — legacy fallback in `action_name` covers Phase 1 programmatic registrations.

---

**Verdict from developer**: ready for reviewer approval. No design questions outstanding; all decisions trace to spec Part 2.
