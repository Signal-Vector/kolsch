# Step 1 writeup — qol-tool-permission-discovery-uniform-with-stacking

**Author:** developer
**Status:** STOP — escalation E-QOL-PERMDISC-001 raised; coordinator approval required before Step 2.

---

## 1. Prerequisite QoL status

`qol-prefix-defaults-and-unified-refs.md` is **shipped** as commit
`5d744f3` on `main` (`feat(assets,schemas,docs): unified 2-segment ref
convention + workspace: implicit default ...`).

What that QoL shipped:
- Asset **refs** collapsed to `<bundle>/<name>` (with field-context
  asset-type inference).
- Catalog per-bundle name uniqueness across asset types
  (`catalog.py:123-144`, `AssetsDuplicateName`).
- `workspace:` prefix implicit for `extends:` and
  `context_files[].path`; legacy 3-segment + explicit `workspace:`
  parse with `config.deprecated_ref_form` Recoverable.
- Starter bundle + docs + fixtures migrated.

What that QoL did **NOT** ship:
- **Action namespace** for authz checks was untouched.
  `decorator.py:128` still derives `action_name` as `tool.<name>`,
  NOT `tool.<bundle>.<name>`. `git show --stat 5d744f3 | grep
  decorator` returns empty; the prereq QoL never touched
  `tools/decorator.py` or `tools/dispatch.py`.

**This contradicts PERM-DISCOVERY Decision #5** ("Action namespace is
home-bundle-rooted. A tool defined at `bundles/kolsch/tools/bash.py`
always has action name `tool.kolsch.bash` ... This QoL doesn't
*change* the namespace derivation — it just relies on the
home-bundle-rooted convention.").

Confirmed by grep:
```
src/tools/decorator.py:128  return self.action or f"tool.{self.name}"
src/tools/dispatch.py:84    auth = await authz.allow(context.principal, defn.action_name, target=target)
src/tools/subsystem.py:241  auth = await self._authz.allow(context.principal, action)   # MCP path
src/engine/engine.py:412    auth = await self.engine._authz.allow(principal, action)     # agent.execute.<id>
```

**→ Escalation E-QOL-PERMDISC-001 raised** (see §10).

---

## 2. Authz flow audit (today's `RuleBasedAuthorizationProvider.allow`)

Source: `src/providers/authz.py:330-405`.

```
allow(principal, action, *, target=None, callback=None,
      agent_permissions=None) -> AuthorizationResult

  ┌─ if agent_permissions is non-empty:
  │    compile each Rule in place; walk via
  │    _match_against_rules(layer="agent") — first match wins
  │    PROMPT → _resolve_prompt (with layer="agent")
  │    matched → return AuthorizationResult
  │
  ├─ walk self._rules via _match_against_rules(layer="platform")
  │    PROMPT → _resolve_prompt
  │    matched → return
  │
  └─ default deny: AuthorizationResult(allowed=False, effect=DENY,
       matched_rule=None, reason="no rule matched; default deny
       (who_chain=default)")
```

`_match_against_rules` (authz.py:407-454): first-match-wins, fullmatch
semantics, `target OR action` tried against the rule's `what`. PROMPT
fan-out hands off to `_resolve_prompt`.

`_resolve_prompt` (authz.py:456-530): resolves PROMPT effects only.
- If neither `callback` parameter nor `_registered_prompt_callback`
  is set → collapse to deny (allowed=False) carrying the matched
  PROMPT rule's id in the result.
- Else fire the effective callback with
  `(rule_id=compiled.raw.id, action=action, target=target)`.
- Callback returns `AuthzEffect` or `(AuthzEffect, Rule)`; the tuple
  form persists the rule via `_insert_rule_before` then fires the
  `_rule_added_hook` (chat's hook calls `cli.permissions._write_back`
  per chat.py:856-858).

**Key observation**: today's default-deny path returns silently;
**no callback is ever fired on the no-rule path**. The callback only
fires on `effect: prompt` matches.

The callback contract today:
```python
async def __call__(self, *, rule_id: str | None, action: str,
                   target: str | None
                  ) -> AuthzEffect | tuple[AuthzEffect, Rule]
```
`rule_id` is already typed `str | None` (authz.py:178). The modal at
`src/cli/_modal.py:218` already takes `rule_id: str | None`. The
panel render at `_modal.py:361` conditionally shows the rule id only
when non-None: `suffix = f"  —  matched {rule_id}" if rule_id is not
None else ""`.

The pattern suggestor at `_modal.py:65-111` has a sensible fallback
(`^tool\.<name>$`) when target is None, derived purely from
`action.removeprefix("tool.")`.

**Conclusion**: the callback contract is already extension-ready. No
type change needed. The work in Step 2 is purely flow: invoke the
callback on the no-rule path.

---

## 3. Callback contract extension — exact change

Step 2 inserts a new branch in `RuleBasedAuthorizationProvider.allow`
**between** the (now extended) layered rule walk and the default-deny
return:

```python
# After agent/bundle/platform layers have all returned None
# (no rule matched anywhere) but BEFORE the default-deny return:

if self._registered_prompt_callback is not None:
    answer = await self._registered_prompt_callback(
        rule_id=None,           # ← the "no rule" signal
        action=action,
        target=target,
    )
    if isinstance(answer, tuple):
        resolved_effect, persist_rule = answer
        # Insert at end-before-default-deny tail (no anchor;
        # use _tail_index() to put the new rule above the
        # default-deny tail — same position kolsch permissions add
        # uses when no `before`/`after` is supplied).
        self._insert_at_tail(persist_rule)
        if self._rule_added_hook is not None:
            with contextlib.suppress(Exception):
                self._rule_added_hook([c.raw for c in self._rules])
    else:
        resolved_effect = answer
    if resolved_effect not in (AuthzEffect.ALLOW, AuthzEffect.DENY):
        msg = ("no-rule prompt callback must resolve to ALLOW or "
               f"DENY; got {resolved_effect!r}")
        raise ValueError(msg)
    return AuthorizationResult(
        allowed=resolved_effect is AuthzEffect.ALLOW,
        effect=resolved_effect,
        matched_rule=None,        # no matched rule on the no-rule path
        reason=(
            f"no rule matched; callback resolved to "
            f"{resolved_effect.value!r} (who_chain=no_rule)"
        ),
    )

# Existing A-I3 default deny tail unchanged.
return AuthorizationResult(allowed=False, effect=AuthzEffect.DENY,
    matched_rule=None,
    reason="no rule matched; default deny (who_chain=default)")
```

Notes:
- `_insert_at_tail` is a small helper that wraps the existing
  `_tail_index()` + `_compile_rule_record` machinery; ≤10 LOC.
  Alternatively, reuse `add_rule` — but `add_rule` is `async` and
  raises `AuthzRuleInvalid` on duplicate `before`/`after`; the
  no-rule path doesn't need either. A direct helper is cleaner.
- `matched_rule=None` in the result (vs. the PROMPT-effect path which
  carries the matched PROMPT rule). The dispatch layer only consumes
  `auth.matched_rule.model_dump()` for the `ToolUnauthorized`
  envelope on denial — None is safe there.
- The callback's `who`/`what` for the persisted rule come from the
  callback itself (the modal computes `who=".*"`, `what=<pattern>`
  via the existing `suggest_pattern` flow at `_modal.py:289-293`).
  No change at the authz boundary.

---

## 4. Action namespace at the authz-check site

**Today's site**: `src/tools/dispatch.py:84`
```python
auth = await authz.allow(context.principal, defn.action_name, target=target)
```
`defn.action_name` from `tools/decorator.py:128` returns
`self.action or f"tool.{self.name}"`. Tools today never set
`action=...` in `@tool(...)`; all four builtins (`bash`, `read_file`,
`write_file`, `list_files`) use the default. So an invocation of the
`bash` tool from `bundles/kolsch/tools/bash.py` passes action
`"tool.bash"` — NOT `"tool.kolsch.bash"`.

**The PERM-DISCOVERY spec assumes** the home-bundle-rooted form. The
prerequisite QoL did NOT ship it. → **Escalation case (a)** —
documented in §10.

---

## 5. Stack composition pick — authz-side priority search

**Pick: alternative (authz-side stacking).** Confirmed by reading the
provider:

- The authz provider's `allow` already does a layered walk:
  `agent_permissions` (Phase 2.2 §3.8.1) → platform rules → default
  deny (authz.py:375-405). Step 2 adds the bundle layer between
  agent and platform.
- The resolver (`src/assets/assembler.py`, `src/kernel/resolver.py`)
  doesn't need to know about bundle permissions. The
  `BundleManifest.permissions` field reads directly off the catalog
  at authz check time.

Rationale (matches spec recommendation):
1. RFC 7386 replace-only semantics on `list[PermissionRule]` would
   *replace* the bundle list with the agent list when the agent
   has any permissions — breaking the "agent wins over bundle wins
   over platform" precedence story (the agent would silently shadow
   the entire bundle rule list).
2. Authz-side priority search matches the natural "look in agent
   rules first, fall through if nothing matches" mental model.
3. It avoids a one-off special case in the kernel resolver's
   per-field merge logic.
4. It keeps the bundle-rules lookup as a tiny accessor on the
   provider (or a small helper passed at the dispatch boundary)
   rather than wiring bundle data through the multi-layer resolver.

**Open design question for Step 2** (NOT escalating; flagged for the
implementer): how does the provider learn the agent's home bundle?
Two shapes:

  **A. Accessor on the provider** — at construction time,
  `RuleBasedAuthorizationProvider` is handed a `bundle_permissions:
  dict[str, list[Rule]]` map keyed by bundle name. The provider
  derives the agent's home bundle from the principal string
  (currently `agent.ag_<hash>` per `who_chain` discussion) — but the
  principal does NOT carry the bundle name today, so this is a dead
  end.

  **B. Per-call parameter** — extend `allow(...)` with an optional
  `bundle_permissions: list[Rule] | None = None` keyword (same
  shape as the existing `agent_permissions`). The dispatcher
  (`tools/dispatch.py`) looks up the bundle for the home tool from
  the catalog and passes the bundle's rules through. Symmetric to
  the existing `agent_permissions` plumbing.

  Recommended: **(B)** — symmetric with Phase 2.2's existing
  `agent_permissions` keyword, no need to mutate provider
  construction, principal-string format stays as-is.

**Companion fix Step 2 must address**: today's `dispatch.py:84`
calls `authz.allow(...)` **without** the `agent_permissions=` kwarg
already supported by the provider. So the agent-layer machinery
exists but **is not wired through dispatch today** — meaning the
four-level stack is effectively a two-level stack (platform →
default-deny) at the dispatch boundary. Step 2 needs to wire
*both* `agent_permissions` AND `bundle_permissions` at the dispatch
call site. Confirmed by grep:
```
$ grep -rn "agent_permissions" src/ --include="*.py" | grep -v "permissions.py\|authz.py"
(empty — no caller threads the kwarg)
```
The `tools/permissions.py` ContextVar binding (`PermissionsBinding`)
is set up but `permissions.bind(...)` is **never called anywhere**
in `src/` — confirmed by grep. So `check`/`require` from inside
tool bodies raise `ToolPermissionsNoContext` today; that's a Phase
2.2 carry-over orthogonal to this QoL.

---

## 6. Non-interactive auto-deny preservation

Confirmed:
- `register_prompt_callback` (authz.py:537-569) sets
  `self._registered_prompt_callback`.
- The new no-rule branch (§3) checks
  `if self._registered_prompt_callback is not None:`. When None
  (non-chat: `kolsch run`, server, tests), falls through to the
  existing default-deny return → `AuthorizationResult(allowed=False,
  ...)` → `ToolUnauthorized` raised at `dispatch.py:86-93`.
- No change in behavior for non-chat callers. Aligns with
  `phase2.0-tools-specs.md` §2.2 ("non-chat callers collapse prompt
  to deny so unattended paths never hang").

Regression-test coverage today (`tests/component/test_providers.py`):
- `test_default_deny_when_no_rule_matches` (line 283): no callback;
  asserts `allowed=False`, `matched_rule=None`, "default deny" in
  reason. **Continues to pass unchanged.**
- `test_default_deny_with_empty_rule_set` (line 291): no callback;
  asserts `allowed=False`. **Continues to pass unchanged.**

These tests don't register a callback so they hit the non-interactive
path. The new branch only activates when a callback IS registered.

---

## 7. Persistence target

Confirmed: chat.py:858 installs `_on_rule_added = _persist_authz_rules
= cli.permissions._write_back`. `_write_back` atomically rewrites
`platform.yaml`'s `authorization.config.rules` (cli/permissions.py:258-294).

For the new no-rule path: the same `_rule_added_hook` fires (§3 code
sketch). Same `_write_back` → same `platform.yaml` target.

Stack-composition implication: a rule persisted to `platform.yaml`
that a future agent.yaml or bundle.yaml rule wants to override — the
narrower rule wins per Step 2's authz-side priority search. UX is:
"the modal persists broadly by default; users can narrow later by
hand-editing bundle.yaml or agent.yaml without conflicting." Matches
the spec's Decision #3 + the Out-of-scope note about the
`[scope]` toggle.

---

## 8. Test surface (existing tests pinning the no-rule behavior)

```
grep -rln "ToolUnauthorized" tests/ → 5 files
```

Per-test assessment:

| Test | File:Line | Asserts | Step 2 action |
|---|---|---|---|
| `test_tool_authz_denial` | test_tools.py:253 | Explicit `effect: deny` rule → ToolUnauthorized | **passes unchanged** (not no-rule path) |
| `test_default_deny_when_no_rule_matches` | test_providers.py:283 | No callback, no rule → `allowed=False`, `matched_rule=None`, "default deny" | **passes unchanged** (non-interactive path; no callback registered) |
| `test_default_deny_with_empty_rule_set` | test_providers.py:291 | No callback, empty rule set → `allowed=False` | **passes unchanged** |
| `test_clear_prompt_callback_makes_subsequent_allow_collapse_to_deny` | test_chat_modal.py:395 | Callback registered then cleared; PROMPT-effect rule matches → collapse to deny, modal never invoked | **passes unchanged** (tests an `effect: prompt` rule, not the no-rule path) |
| `test_skills_authz_denial` | test_skills_authz.py:136 | Skills subsystem authz check fails → tool.failed event | **passes unchanged** (skills test; different action namespace) |
| `test_chat_lifecycle.py:864` | test_chat_lifecycle.py:864 | `contextlib.suppress(ToolExecutionFailed, ToolUnauthorized)` swallow at session-cleanup boundary | **passes unchanged** |
| `test_fallback.py:819-821` | test_fallback.py:819 | Constructs a ToolUnauthorized for event-serialization test | **passes unchanged** (constructs error, doesn't test allow flow) |

**Summary**: every existing assertion that pins
"ToolUnauthorized on the no-rule path" is non-interactive (no
callback registered) and continues to pass. There are no
"a callback IS registered AND the no-rule path is hit AND the
callback is NOT fired" tests today — the new branch is purely
additive.

New tests Step 2 needs (per spec §4 acceptance + Step 2 §6):
- `test_allow_no_rule_no_callback_raises_unauthorized` (regression
  guard for the existing non-interactive path).
- `test_allow_no_rule_with_callback_fires` (callback registered →
  fired with `rule_id=None`).
- `test_allow_no_rule_with_callback_persists_rule` (tuple-return →
  rule list grows + hook fires).
- `test_priority_agent_wins_over_bundle` (stacking).
- `test_priority_bundle_wins_over_platform` (stacking).

---

## 9. Bundle.yaml schema impact

Current shape (`src/assets/manifest.py:24-38`):
```python
class BundleManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1)
    version: str
    description: str
    required_env_vars: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    path: str
```

Step 2 adds:
```python
from schemas import PermissionRule  # already in src/schemas/config.py:129

class BundleManifest(BaseModel):
    ...
    permissions: list[PermissionRule] = Field(default_factory=list)
```

**`extra="forbid"`** means the new field MUST be declared (which Step
2 does). Bundles that don't carry the field default to empty list —
backwards compatible.

**Catalog walker** (`src/assets/catalog.py:84-146`): `load_manifest`
parses YAML into `BundleManifest`; the catalog stores
`BundleManifest` **by reference** in `AssetCatalog.bundles`
(line 146: `manifests: dict[str, BundleManifest] = {m.name: m for _, m
in raw}`). **No walker changes needed** — adding the field on the
manifest schema flows through to the catalog's stored manifest
record. Confirmed by reading the build path: no reconstruction, no
field-by-field copy.

At authz check time, the bundle layer can be looked up via
`catalog.bundles[bundle_name].permissions`. Step 2 picks the call
site — likely in the engine's dispatch wiring where both the
agent_config and the catalog are reachable.

---

## 10. Escalations

### Escalation E-QOL-PERMDISC-001 — Home-bundle-rooted action namespace not shipped by prereq QoL

- **Raised by:** developer (Step 1)
- **Step:** 1
- **Trigger:** `qol-tool-permission-discovery-uniform-with-stacking.md`
  Part 1 Decision #5 attributes the home-bundle-rooted action
  namespace convention (`tool.<home_bundle>.<name>`) to
  `qol-prefix-defaults-and-unified-refs.md`. That prereq QoL is
  shipped at commit `5d744f3` on main. Inspection of the prereq's
  spec (`process/qol-prefix-defaults-and-unified-refs.md`) finds
  zero mentions of "action namespace" or `tool.<bundle>`; its
  acceptance criteria cover refs only. Inspection of the shipped
  code confirms:
    - `src/tools/decorator.py:128` still derives `action_name =
      self.action or f"tool.{self.name}"`.
    - `git show --stat 5d744f3 | grep -E "tools/|decorator"` returns
      empty.
    - All call sites pass `tool.<name>`:
      `src/tools/dispatch.py:84`,
      `src/tools/subsystem.py:241`,
      `docs/explanation/tools-subsystem.md:34,71,161,808` etc.
- **Why this is not a guess:** The PERM-DISCOVERY spec's Decision #5
  asserts the convention is shipped; the code shows otherwise; the
  prereq spec's own acceptance criteria don't include the change.
- **Implication if unresolved:** The four-level stack still works in
  the "deepest-wins" sense — but bundle-level permissions don't
  cleanly *scope to the bundle*. A bundle A that ships a tool
  `bash` and writes a rule `what: "tool.bash"` would match *any*
  bundle's `bash` tool. Since the catalog enforces per-bundle name
  uniqueness across asset types (not across bundles), two bundles
  CAN ship `bash` independently — and bundle A's rule leaks across.
- **Options:**
  1. **Add the action-namespace change as part of Step 2** of this
     QoL. Change `decorator.action_name` to use the catalog-derived
     home-bundle prefix (or inject it at the dispatch site by
     looking up the tool's bundle via the catalog). Probably ~30
     LOC of code + a fixture update for existing rules. Side
     effect: every `what: "tool.bash"` rule in `platform.yaml` (and
     anywhere else) becomes invalid; the migration window from the
     prereq QoL's deprecation tooling needs to cover this too.
     Likely needs starter-bundle + docs updates similar to the
     prereq QoL's Step 3.
  2. **Defer to a follow-on QoL** — accept v1 bundle permissions
     under the flat `tool.<name>` namespace, document the caveat in
     `permissions.md`, ship a follow-on
     `qol-action-namespace-bundle-scoped.md` to do the rename
     properly with its own migration window.
  3. **Accept as-is.** Bundle rules under the flat namespace work
     fine for the common case (only the home bundle ships a tool
     of that name); the cross-bundle name collision is edge-case.
     Document the caveat; no schema or code change.
- **Estimated cost:** Option 1 ~3 hours (code + tests + fixture
  migration). Option 2 ~10 min (writeup of the caveat). Option 3 ~5
  min (doc note).
- **Developer's recommendation:** **Option 3** for v1. Two reasons:
  (a) The cross-bundle name collision today is hypothetical — the
  starter bundle is the only widely-used bundle, and per-bundle name
  uniqueness is enforced. (b) Pulling the action-namespace change
  into this QoL inflates the scope from "modal-on-no-rule + bundle
  permissions" to "modal-on-no-rule + bundle permissions + global
  rule-string migration" — which violates the "ships in ~30 minutes"
  Step 2 budget. The caveat is small and addressable in docs.

### Coordinator resolution — E-QOL-PERMDISC-001

- **Decided by:** human
- **Decision:** **Option 3** — accept the flat `tool.<name>` action
  namespace for v1. Bundle permissions work cleanly in the common
  case (only the home bundle ships a tool of a given name); the
  cross-bundle name-collision edge case is documented as a caveat
  in `docs/how-to/permissions.md` during Step 3. Namespace migration
  deferred to a follow-on QoL if/when users hit the collision.
- **Rationale:** Matches developer's recommendation. Keeps Step 2
  scope bounded ("modal-on-no-rule + bundle permissions"); pulling
  the global rule-string migration into this QoL would more than
  double the scope.
- **Resume instruction:** Proceed to Step 2. Implement bundle
  permissions under the existing `tool.<name>` action namespace.

---

## 11. Other escalations — not triggered

- **(b) Catalog walker drops bundle.yaml extras** — NOT triggered.
  `BundleManifest` is stored by reference in `AssetCatalog.bundles`
  (`catalog.py:146`); adding a `permissions` field on the schema
  flows through without walker changes. `extra="forbid"` forces
  the new field to be declared — Step 2's schema edit covers that.
- **(c) Authz-side stacking hidden interaction with prompt-callback
  registration** — NOT triggered. `_registered_prompt_callback` is
  independent of the rule-search machinery. The new no-rule
  branch checks the callback registration after the layered rule
  search returns no match; the existing PROMPT-effect path
  (`_resolve_prompt`) is unchanged.

---

## 12. Summary for the reviewer

| Decision | Pick |
|---|---|
| Callback contract extension | Re-use existing `rule_id: str | None` shape (already typed); fire callback with `rule_id=None` on the no-rule path. Modal already handles None. |
| Stack-composition approach | Authz-side priority search (alternative). Provider's `allow` extends to walk `agent_permissions` → `bundle_permissions` (NEW kwarg) → platform rules → no-rule callback → default-deny. |
| Bundle-permissions wiring | NEW kwarg `bundle_permissions: list[Rule] | None = None` on `allow`. Dispatcher looks up the tool's home bundle via the catalog and passes the bundle's rules through. (Step 2 also wires the existing `agent_permissions` kwarg, which dispatch.py:84 doesn't pass today.) |
| Persistence target | `platform.yaml` via existing `cli.permissions._write_back` (chat.py:856-858 hook unchanged). |
| Non-interactive auto-deny | Preserved by the `if self._registered_prompt_callback is not None:` guard on the new branch. |
| Test surface | Existing "no-rule raises ToolUnauthorized" tests all run without a callback; **all pass unchanged**. Step 2 adds the five new tests in spec §6. |
| Bundle schema change | Add `permissions: list[PermissionRule] = Field(default_factory=list)` to `BundleManifest`. No catalog walker changes. |

**Blocking escalation:** **E-QOL-PERMDISC-001**. Coordinator must
pick option 1/2/3 before Step 2 begins. Developer recommends option
3 (accept v1 caveat; document; defer namespace migration to a
follow-on QoL).
