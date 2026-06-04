# QoL — Uniform tool permission discovery with four-level stacking

**Draft 0.2** *(bumps 0.1 after Step 1 verification surfaced E-QOL-PERMDISC-001: the prereq QoL did not ship the bundle-rooted action-namespace change that spec Decision #5 attributed to it. Resolution defers the namespace migration to a follow-up ticket; this QoL ships against single-segment action namespaces with a documented v1 caveat. Step 2 expanded to also fix a latent dispatch-boundary defect — `dispatch.py:84` never threaded `agent_permissions=` to the authz provider. Full resolution + spec corrections in Part 5.)*

> Today, when an agent invokes a tool whose action namespace
> has no matching authz rule, the platform default-denies and
> raises `ToolUnauthorized`. The user sees a tool failure
> mid-exchange with no path forward except hand-editing
> `platform.yaml` or the agent's `permissions:`. Existing
> `effect: prompt` rules surface the modal — but only when
> some rule already covers the action.
>
> This QoL makes the no-rule case fire the modal too. The
> user gets the same `[y]/[a]/[n]/[d]` UX they already know
> from the prompt-effect path; their answer persists (to
> `platform.yaml` by default — preserving current modal
> behavior) so they're only asked once. Non-interactive
> sessions (`kolsch run`, server, tests) auto-deny — same
> treatment the modal gets today.
>
> Adds a four-level stacking story: agent rules > bundle
> rules > platform rules > default-deny. `bundle.yaml` gains
> a `permissions:` field with the same shape as agent
> permissions; users can declare bundle-wide rules
> alongside the platform-wide ones they already have.

---

## Part 0 — Why this matters

### 0.1 Default-deny is a dead end for new users

Adding a new tool to an agent today:

1. Author edits `subsystems.tools.config.declared` in the
   agent YAML.
2. Runs the agent.
3. Tool is invoked. Authz check fires. No rule covers the
   action. **`ToolUnauthorized`** raised at
   `src/tools/subsystem.py:243-245`; tool returns a
   `tool.failed` event with `authz.denied`.
4. User sees a mid-exchange tool failure. Has to either:
   - Read the docs to find where to edit rules, OR
   - Manually add a rule to `platform.yaml` or the agent's
     `permissions:` block, OR
   - Give up.

The existing modal (`src/cli/_modal.py`) handles the
**interactive add** case — but only when a rule with
`effect: prompt` already exists. For tools with no
covering rule at all, the modal never fires.

### 0.2 Existing rule discovery is asymmetric

Today's modal trigger:

```
Tool invoked
  → authz.allow(principal, action)
    → if no rule matches: raise ToolUnauthorized (silent to user beyond the failure event)
    → if rule matches with effect=prompt: fire modal
    → if rule matches with effect=allow: pass
    → if rule matches with effect=deny: raise ToolUnauthorized
```

The modal fires only on the `prompt`-effect path. The
no-rule path goes straight to denial. From the user's
perspective: "the platform asks me about some tools but
silently fails others." That asymmetry is the papercut.

### 0.3 Bundle-level rules don't exist

`bundle.yaml` currently has `name`, `version`, `description`.
No `permissions:` field. So a bundle author distributing a
tool that needs specific rules (e.g., a `git-ops` bundle
shipping tools that need elevated write permissions) has no
way to ship the rules alongside the tools. Every consumer
has to wire the rules in their own workspace.

This QoL adds the field with the same shape as agent
permissions and threads it through the resolver as the
middle layer of the stack.

---

## Part 1 — Decisions locked

From the agent-YAML-design conversation:

1. **Uniform behavior across all tools.** No `@tool`
   decorator change. No per-tool flag. When the authz check
   finds no rule covering the action, the platform fires the
   modal (interactive) or denies (non-interactive). Every
   tool behaves identically.

2. **Four-level stack, deepest wins.** Precedence order at
   the authz check:
   ```
   agent.yaml `permissions:`            ← most specific
      ↑
   bundle.yaml `permissions:`           ← NEW field
      ↑
   platform.yaml `authorization.rules`  ← workspace default
      ↑
   default: prompt-on-first-use (interactive) / deny (non-interactive)
   ```

3. **Persistence target: `platform.yaml` by default.** The
   modal's `[a]/[d]` writes a new rule to
   `platform.yaml`'s authz config — preserving the current
   modal behavior. Users who want narrower scope hand-edit
   `bundle.yaml` or the agent's `permissions:` directly; the
   stacking ensures their narrower rule wins.

4. **Non-interactive auto-denies.** `kolsch run`, server,
   tests don't have a TTY for the modal. The no-rule path
   collapses to default-deny in non-interactive contexts —
   same treatment the existing `prompt`-effect path gets per
   `phase2.0-tools-specs.md` §2.2 ("non-chat callers
   collapse `prompt` to deny so unattended paths never
   hang").

5. **Action namespace is `tool.<name>` (single-segment) for
   v1.** Per E-QOL-PERMDISC-001 coordinator resolution: the
   prereq QoL (`qol-prefix-defaults-and-unified-refs.md`)
   shipped the *ref* convention (`kolsch/bash`) but NOT the
   *action namespace* change. `src/tools/decorator.py:128`
   still returns `f"tool.{self.name}"` for the four
   builtins. This QoL ships against the existing
   single-segment form.

   **v1 caveat**: tools sharing a name across bundles
   (e.g., `kolsch/bash` and `andre/bash`) collide on action
   name (`tool.bash`) and therefore share authz rules.
   Workaround for users authoring their own bundles: name
   tools uniquely across the bundles they care about until
   the namespace migration ships.

   The bundle-rooted form (`tool.kolsch.bash`) is the right
   long-term design and is tracked at
   `qol-action-namespace-bundle-scoped.md`. This QoL's
   four-level stack works correctly against single-segment
   names — the bundle layer just stacks by rule pattern,
   not by namespace prefix.

6. **Existing `effect: prompt` rules still work
   unchanged.** That path covers the "always ask, every
   time" use case. The new path covers the "no rule yet,
   ask once, persist." Two distinct code paths converging
   on the same modal UI.

7. **No `[scope: workspace/bundle/agent]` toggle on the
   modal.** Persistence target is hard-coded to
   `platform.yaml` for v1. If users frequently want narrower
   scope and complain that hand-editing is friction, add the
   toggle in a follow-up.

8. **Sequenced after `qol-prefix-defaults-and-unified-refs.md`.**
   That QoL pins the home-bundle-rooted ref convention this
   spec depends on (Decision #5). If the unified-refs QoL
   hasn't shipped, BLOCK Step 2.

---

## Part 2 — Acceptance criteria

The QoL is shipped when **all** hold:

1. **Tools work identically.** No `@tool` decorator
   kwarg added. No per-tool migration. The four builtin
   tools (`bash`, `read_file`, `write_file`, `list_files`)
   keep their decorator declarations unchanged.

2. **`bundle.yaml` accepts a `permissions:` field** with
   the same shape as `agent.yaml`'s `permissions:`
   (`list[PermissionRule]` — reuses the existing schema at
   `src/schemas/config.py:129`).

3. **Four-level stack composes correctly.** A tool
   invocation's authz check looks up rules in precedence
   order: agent → bundle → platform → default. Deepest
   match wins. (Per RFC 7386 replace-only semantics — child
   list replaces parent list when present.)

4. **Interactive mode: no rule → modal.** When `authz.allow`
   finds no matching rule AND a prompt callback is
   registered (chat mode), the modal fires with the same
   `[y]/[a]/[n]/[d]` UX it uses for existing
   `effect: prompt` rules. The user's `[a]/[d]` answer
   persists a new rule to `platform.yaml` via the existing
   `cli/permissions._write_back` path.

5. **Non-interactive mode: no rule → deny.** When
   `authz.allow` finds no matching rule AND no prompt
   callback is registered, raise `ToolUnauthorized` — same
   as today.

6. **Existing `effect: prompt` rules still fire the modal**
   on every invocation as today. No regression on the
   pre-existing path.

7. **Action namespace is home-bundle-rooted** at the authz
   check site. Verify the tool registry passes the correct
   action namespace (`tool.<home_bundle>.<name>`) to
   `authz.allow`.

8. **Regression tests cover all four paths**:
   - Interactive happy path: no rule → modal → `[a]` →
     rule persisted → second invocation passes silently.
   - Interactive deny path: no rule → modal → `[d]` →
     deny-rule persisted → second invocation denied
     silently.
   - Non-interactive deny path: no rule → no callback →
     `ToolUnauthorized` (existing behavior).
   - Stacking precedence: agent rule wins over bundle rule
     wins over platform rule.

9. **Documentation surfaces the change.**
   `docs/how-to/permissions.md` covers the four-level
   stack, the new bundle-level field, and the
   first-use-prompt UX. `docs/reference/schemas.md`
   documents `bundle.yaml.permissions`.

### Out of scope

- **`[scope: workspace/bundle/agent]` toggle on modal.**
  Hard-code to `platform.yaml` for v1; revisit if users ask.
- **`@tool` decorator changes.** Tools behave identically;
  no per-tool flag.
- **Action namespace redesign.** This QoL relies on the
  home-bundle-rooted convention shipped by
  `qol-prefix-defaults-and-unified-refs.md` Step 2; doesn't
  change it.
- **Audit log of persisted rules.** Beyond the existing
  event log; no new audit surface.
- **Permission inheritance under `extends:`.** Agent-level
  permissions already inherit per RFC 7386 (per
  `bug-engine-extends-parent-not-rendered.md` Category B);
  bundle permissions follow the same composition rule once
  Step 2 wires them in.
- **Removing the existing `effect: prompt` rule mechanism.**
  Still useful for "always ask" use cases (e.g., per-call
  confirmation on destructive operations).

---

## Part 3 — Step dependency graph

```
qol-prefix-defaults-and-unified-refs.md (Step 2 — home-bundle-rooted refs)  ←── BLOCKS
   │
   ▼
Step 1 (verification + design picks) ←── STOP gate
   │
   ▼
Step 2 (bundle schema + resolver wiring + authz boundary + modal trigger)
   │
   ▼
Step 3 (regression tests + docs + manual e2e + handoff)
```

| Step | Ships | Reviewable in |
|---|---|---|
| **1** | Writeup naming the authz-boundary change + stacking semantics + modal-callback contract extension | ~30 min |
| **2** | Bundle schema + resolver composition + authz code change + modal trigger | ~40 min |
| **3** | Tests + docs + manual e2e | ~30 min |

---

## Step 1 — Verification + design picks

### Developer prompt

```
[standing instructions]

Step 1 — Verify the design picks before any code lands.

Read first (this is the canonical context for the spec):
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/qol-tool-permission-discovery-uniform-with-stacking.md Parts 0-2 (this whole spec — Parts 0, 1, 2 establish what's being decided).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/qol-prefix-defaults-and-unified-refs.md (BLOCKING dependency — confirm it's shipped; this spec depends on home-bundle-rooted action namespaces).

Read next (current code surfaces):
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/providers/authz.py (the authorization provider — focus on `allow` around line 419, `_resolve_prompt` around line 456, `register_prompt_callback` around line 537, and the `_registered_prompt_callback` attribute around line 267).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/tools/subsystem.py:243-245 (the existing `ToolUnauthorized` raise site).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/tools/dispatch.py (how invocations route to the authz check).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/cli/_modal.py (the chat modal — the existing prompt callback implementation; lines 190-251 are `__call__`).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/cli/permissions.py (the `_write_back` helper that persists rules to `platform.yaml`).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/schemas/config.py:129-194 (the `PermissionRule` class and the agent-side `permissions:` field on `ResolvedAgentConfig`).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/assets/manifest.py:24 (the current `BundleManifest` schema — confirm it lacks `permissions:`).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/cli/chat.py:680-725 (where chat registers + clears the prompt callback at session lifecycle).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase2.0-tools-specs.md §2.2 (the "non-chat callers collapse prompt to deny" discipline this QoL relies on).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/docs/how-to/permissions.md (the current authoring-side how-to; Step 3 will update this).

Verification tasks:

1. Confirm the prerequisite QoL has shipped.
   - `qol-prefix-defaults-and-unified-refs.md` must be in
     main with the home-bundle-rooted action namespace
     convention.
   - Grep the codebase for the action-namespace derivation
     site (likely in src/tools/subsystem.py or
     src/tools/registry.py). Confirm the action string for
     a tool defined at bundles/X/tools/Y.py is `tool.X.Y`.
   - If the unified-refs QoL hasn't shipped, BLOCK this
     step and escalate.

2. Audit the existing authz flow.
   - Read `RuleBasedAuthorizationProvider.allow` body
     (`src/providers/authz.py:419+`). Trace exactly when
     `_resolve_prompt` fires (today only on
     effect: prompt match) and when ToolUnauthorized is
     raised (today on default-deny or effect: deny).
   - Confirm the prompt callback contract:
     `PromptCallback.__call__(rule_id, action, target)`.
     The current callback signature passes `rule_id` for an
     EXISTING matched rule. The no-rule path has no rule_id.
   - Document the contract change Step 2 needs.

3. Pin the prompt-callback contract extension.
   - Today: callback fires when a `prompt`-effect rule
     matches. `rule_id` is the matched rule's id.
   - New: callback also fires when NO rule matches.
     `rule_id` is None (or a sentinel).
   - The callback's return value (single AuthzEffect or
     `(effect, Rule)` tuple per `_modal.py:146`) drives
     persistence the same way for both paths.
   - For the new path, when callback returns
     `(effect, Rule)`, the Rule's `who` and `what` need to
     be computed by the callback. The modal can default to
     `who: ".*"`, `what: "<action>"` (the action namespace
     of the failing invocation) — same suggest_pattern path
     today's modal uses.
   - Document the exact change to the callback's call site.

4. Pin the action namespace at the authz-check site.
   - Verify that today's call site passes the correct
     home-bundle-rooted action namespace
     (`tool.<home_bundle>.<name>`) to `authz.allow`.
   - If not, the unified-refs QoL didn't fully wire it
     through — escalate (that's a defect in that QoL, not
     a fix for this one).

5. Pin the four-level stack composition.
   - Today's resolver merges defaults > agent_config >
     call_overrides per `assembler.py:35` +
     `resolver.py:53-56`. Agent's `permissions:` block
     enters at agent_config level.
   - For bundle permissions: where does bundle.yaml get
     loaded? Read `src/assets/manifest.py:24+` and the
     catalog scan path (`src/assets/catalog.py:84-135`).
     Determine where bundle.yaml's permissions can enter
     the resolver chain.
   - Recommend: bundle permissions enter as a layer BETWEEN
     defaults (platform.yaml) and agent_config. So the
     final RFC 7386 merge order becomes:
       defaults (platform) > bundle_permissions > agent_config (which carries agent permissions)
     Confirm this matches "agent > bundle > platform >
     default" precedence (last-merged wins per RFC 7386).
   - **Wait — there's a subtlety.** The current resolver
     merges full configs, not just permissions. Permissions
     are a `list[PermissionRule]` field on the agent
     config — RFC 7386 replaces lists entirely. So the
     stacking only works if bundle permissions are MERGED
     IN before the agent's permissions list shows up,
     such that agent permissions OVERRIDE bundle. Document
     the merge dynamics carefully.
   - **Alternative**: bundle permissions could be threaded
     through SEPARATELY (not via the YAML merge) by the
     authz provider, which would check rules in priority
     order: agent.permissions → bundle.permissions →
     platform.rules → default. This avoids the RFC 7386
     list-replace problem.
   - Step 1 picks one approach. Recommend the **alternative**
     (authz-side stacking) — simpler, doesn't require
     changing the kernel resolver's per-field merge for one
     special case, and matches the natural priority-search
     mental model.

6. Confirm non-interactive auto-deny preserves.
   - Today: when `register_prompt_callback` hasn't been
     called (non-chat contexts), `_resolve_prompt` collapses
     `effect: prompt` rules to deny per
     `phase2.0-tools-specs.md` §2.2.
   - The new no-rule path needs the same treatment:
     callback registered → fire modal; callback NOT
     registered → raise ToolUnauthorized (same as today).
   - Confirm by tracing: the callback registration
     attribute is `_registered_prompt_callback`
     (authz.py:267); the new code branches on whether it's
     None.

7. Pin the persistence target.
   - Modal's `[a]/[d]` writes to platform.yaml via
     `cli/permissions._write_back` (current behavior per
     `_modal.py` docstring lines 17-27).
   - For the no-rule path: same persistence target.
   - Confirm the rule write doesn't conflict with the
     stack composition (if rule lands in platform.yaml and
     a future agent.yaml rule wants to override, the agent
     rule wins per the precedence; that's the intended
     UX).

8. Test surface — list existing tests that pin the
   "no-rule raises ToolUnauthorized silently" behavior.
   - grep for ToolUnauthorized in tests/
   - For each: confirm whether the assertion is
     "tool fails with this exception" (will continue
     working in non-interactive mode) or "no modal fires"
     (will need updating once the new branch is wired).

9. Bundle.yaml schema impact.
   - Confirm bundle.yaml's current shape:
     name + version + description (per
     `src/assets/manifest.py:24+`).
   - Step 2 adds `permissions: list[PermissionRule] =
     Field(default_factory=list)`.
   - Confirm the catalog walker
     (`src/assets/catalog.py:84-135`) loads the manifest
     and that the new field will flow into a struct the
     authz provider can read at runtime. If the catalog
     drops bundle.yaml's extra fields today, the loader
     needs updating.

10. STOP and post the writeup before any code lands.

ESCALATION PATH: if Step 1 finds:
 (a) The home-bundle-rooted action namespace isn't wired
     correctly post-unified-refs-QoL (defect in that QoL,
     not this one).
 (b) The catalog walker drops bundle.yaml extra fields,
     requiring a larger loader refactor.
 (c) Approach #5 (authz-side stacking) has a hidden
     interaction with the existing prompt-callback registration
     surface.
escalate before proceeding.

Acceptance: writeup posted naming the callback contract
extension, the stack-composition approach pick (resolver-side
vs. authz-side), the persistence target, and the test
surface; coordinator approves to proceed.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1.

NO production code. Writeup is the deliverable.

Checks:

1. Prerequisite QoL status confirmed (qol-prefix-defaults-and-unified-refs.md
   shipped). If unshipped, the dev escalated — verify the
   escalation is on the spec's escalation log.

2. Authz flow audit cites authz.py:419 (allow) +
   authz.py:456 (_resolve_prompt) +
   authz.py:537 (register_prompt_callback) with concrete
   trace.

3. Callback contract extension is concrete. The writeup
   names exactly:
   - What rule_id becomes for the no-rule case (None or
     sentinel).
   - How the callback computes who/what for the new rule.
   - Where in authz.py the new branch lands.

4. Action namespace is confirmed home-bundle-rooted
   (`tool.<home_bundle>.<name>`). If not, escalation
   triggered.

5. Stack-composition approach is picked between
   resolver-side merge and authz-side priority search.
   Rationale is named. The dev's recommended
   approach is the authz-side priority search — verify
   that pick is justified.

6. Non-interactive auto-deny is confirmed to preserve via
   the existing `_registered_prompt_callback` None check.

7. Persistence target locked to platform.yaml; the dev
   confirmed the existing `_write_back` path handles the
   write.

8. Test surface listed. Tests that assert
   "no-rule raises ToolUnauthorized" are flagged with
   per-test action: "passes unchanged" (non-interactive
   path, still raises) or "needs update" (interactive
   path, behavior changes).

9. bundle.yaml schema impact concrete. The dev names the
   new field declaration + the loader path that needs to
   pass it through.

10. All three escalation conditions are confirmed
    not-an-issue or escalated.

Verdict: APPROVE-TO-PROCEED or REVISE.
```

---

## Step 2 — Bundle schema + resolver wiring + authz boundary + modal trigger

### Developer prompt

```
[standing instructions]

Step 2 — Implement the four-level stacking + no-rule modal
trigger.

Read first:
- Step 1's writeup (coordinator-approved).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/qol-tool-permission-discovery-uniform-with-stacking.md Parts 1-2.
- src/assets/manifest.py:24+ (BundleManifest).
- src/assets/catalog.py:84-135 (bundle scan + manifest load).
- src/providers/authz.py (allow + _resolve_prompt + register_prompt_callback).
- src/cli/_modal.py:146-251 (the callback contract + the choice-collection flow).
- src/schemas/config.py:129 (PermissionRule).

Tasks (in this order):

1. Bundle schema — add permissions field.
   - In src/assets/manifest.py, add to BundleManifest:
       permissions: list[PermissionRule] = Field(default_factory=list)
     Reuse PermissionRule from src/schemas/config.py.
   - Ensure model_config still has the right
     `extra=` policy (verify by reading current shape).
     If extra="forbid", new field must be declared; OK.
   - Update the catalog walker
     (src/assets/catalog.py around lines 84-135) so the
     loaded BundleManifest carries the new permissions
     field. Confirm by reading whether the manifest is
     passed by reference (no change needed) or
     reconstructed (need to preserve the field).

2. Wire BOTH agent_permissions AND bundle_permissions at
   the dispatch boundary (the companion fix Step 1
   surfaced).
   - **Existing defect**: `src/tools/dispatch.py:84` calls
     `authz.allow(context.principal, defn.action_name,
     target=target)` — does NOT pass `agent_permissions=`.
     The agent-level `permissions:` field on
     `ResolvedAgentConfig` is wired in the schema and in
     `authz.allow`'s parameter list (per
     `authz.py:337`) but the dispatch call site never
     supplies it. Result: today's agent permissions are
     dead code at the dispatch boundary.
   - **Fix**: dispatch.py:84 needs `agent_permissions=`
     threaded through. The agent's permissions list is
     available via the dispatch context (which carries
     the resolved agent config — verify in Step 2 by
     reading dispatch.py's signature).
   - **Add the new bundle_permissions= kwarg** to
     `authz.allow` symmetric with `agent_permissions=`.
     Get the bundle permissions via a new accessor on the
     authz provider OR via dispatch reading from the
     catalog (Step 1 pinned the accessor approach).

3. Update authz.allow's rule-search order.
   - With both kwargs now threaded through, the rule-search
     order in `authz.allow` becomes:
       agent_permissions (from call kwarg)
       → bundle_permissions (from call kwarg)
       → self.rules (platform rules)
       → default-deny / modal trigger
   - First match wins. Today's `allow` body iterates over
     self.rules; new body iterates over the priority order
     and short-circuits on first match.

4. Wire the no-rule modal trigger.
   - In `allow`'s body, AFTER the priority-order rule
     search finds no match:
     - If `self._registered_prompt_callback is not None`:
       fire the callback with `rule_id=None` (or a sentinel),
       `action=<action>`, `target=<target>`.
     - The callback's return shape unchanged
       (`AuthzEffect` or `(AuthzEffect, Rule)`). If the
       callback returns a Rule, persist it via the existing
       `on_rule_added` hook (chat-registered hook calls
       `_write_back` per current behavior).
     - The returned AuthzEffect determines the immediate
       allow/deny outcome.
   - If `self._registered_prompt_callback is None`:
     raise ToolUnauthorized (current behavior preserved
     for non-interactive callers).

5. Extend the modal's __call__ to handle rule_id=None.
   - Current modal docstring (`_modal.py:190-211`) assumes
     `rule_id` names an existing matched rule.
   - When `rule_id is None`, the modal renders the panel
     but the suggested-pattern call (for `[a]/[d]` →
     persisted rule) uses `who=".*"`, `what=action` as the
     default (the `suggest_pattern` helper at
     `_modal.py:63-109` already produces a reasonable
     default for any action; reuse it).
   - The panel's render at `_modal.py:257-284` references
     `rule_id` for display ("matched <rule_id>"). When None,
     show "no matching rule" or omit that field gracefully.

6. NO change to:
   - The `@tool` decorator (no new kwarg).
   - `src/tools/decorator.py:128` — action namespace stays
     `tool.<name>` (single-segment). Bundle-rooted form is
     a separate ticket per E-QOL-PERMDISC-001.
   - Any of the four builtin tool YAML configs.
   - Engine pre-flight or capability check.
   - `_resolve_prompt` body — that path stays for the
     existing `effect: prompt` rules.
   - The existing `cli/permissions._write_back` helper —
     it gets called the same way; just from a new trigger.

7. Unit tests in tests/unit/test_authz.py (or the closest
   existing file):
   - test_allow_no_rule_no_callback_raises_unauthorized:
     no rule + no registered callback → ToolUnauthorized
     (regression guard).
   - test_allow_no_rule_with_callback_fires:
     no rule + registered callback that returns ALLOW →
     allow returns true; callback called with rule_id=None.
   - test_allow_no_rule_with_callback_persists_rule:
     no rule + callback returns (ALLOW, Rule) → the
     on_rule_added hook is called with the new rule.
   - test_priority_agent_wins_over_bundle:
     agent has allow rule + bundle has deny rule for the
     same action → agent wins → allow returns true.
   - test_priority_bundle_wins_over_platform:
     bundle has allow rule + platform has deny rule → allow.
   - test_dispatch_threads_agent_permissions:
     dispatch.py:84 passes agent_permissions= to authz.allow.
     Regression guard against the latent defect resurfacing.

8. Integration test in tests/integration/test_bundle_permissions.py:
   - Author a tmp workspace with a bundle.yaml carrying a
     `permissions:` block.
   - Construct an agent in that bundle.
   - Invoke a tool; verify the bundle rule fires.

Scope: ~80 LOC code (manifest + authz + modal) + ~120 LOC
tests.

ESCALATION PATH: if integrating the bundle-permissions
lookup requires the authz provider to learn about the asset
catalog (cross-subsystem coupling), document and escalate.
The clean shape is a small accessor on the provider; if it
turns into a tangled dependency, the design needs revisit.

Acceptance: unit + integration tests pass; manual probe
(register a fake callback, invoke a tool with no rule;
confirm callback fires with rule_id=None).
```

### Reviewer prompt

```
[standing instructions]

Review Step 2.

Checks:

1. bundle.yaml schema has `permissions:
   list[PermissionRule] = Field(default_factory=list)`.
   Reuses PermissionRule from schemas/config.py — no
   duplicated schema.

2. Catalog walker passes the permissions field through.
   Verify by reading the manifest load + the post-load
   data flow.

3. authz.allow has a priority-order rule search:
   agent → bundle → platform → default. NOT a flat
   list iteration. First match wins.

4. No-rule branch fires the registered callback with
   rule_id=None. Verify by reading.

5. No-rule branch raises ToolUnauthorized when no
   callback is registered. Non-interactive preservation
   verified.

6. _modal.py __call__ handles rule_id=None gracefully:
   the panel renders without crashing; the persisted rule
   defaults `who=".*"`, `what=action`.

7. NO change to:
   - The `@tool` decorator surface.
   - The four builtin tool YAML configs.
   - `_resolve_prompt` body (existing prompt-effect path
     stays).
   - The `cli/permissions._write_back` helper.
   Verify by `git diff src/tools/decorator.py` +
   `git diff src/cli/_starter_bundle/kolsch/tools/` (zero
   changes in both).

8. Unit tests cover the five paths (no-rule-no-callback,
   no-rule-allow, no-rule-persist, agent-wins-over-bundle,
   bundle-wins-over-platform).

9. Integration test for bundle-level permissions exists +
   passes.

10. Run the full test suite. tc_eng_* goldens still
    byte-equal.

Verdict: PASS or FAIL.
```

---

## Step 3 — Regression tests + docs + manual e2e + handoff

### Developer prompt

```
[standing instructions]

Step 3 — Lock the QoL in with end-to-end tests, doc
updates, and manual verification.

Read first:
- Step 2's PR.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/docs/how-to/permissions.md (the current authoring how-to — needs updating).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/docs/reference/schemas.md (schemas reference — needs bundle.yaml.permissions documented).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase2.0-tools-specs.md §2.2 (the non-chat callers discipline this QoL aligns with).
- tests/component/cli/test_chat_modal.py (existing modal tests — patterns to follow).

Tasks:

1. End-to-end regression test in
   tests/integration/test_no_rule_modal_flow.py:

   TC-PERM-DISCOVERY-01 — Interactive no-rule → modal → allow → second invocation silent.
     def test_no_rule_fires_modal_then_persists_allow():
         """A tool invocation with no covering rule fires
         the modal; user picks [a]; rule persists; second
         invocation passes without re-prompting."""
         # Setup: chat-style fixture with a registered
         # modal whose input_fn is scripted to "a" → suggested pattern.
         # Action: invoke a tool whose action has no rule.
         # Expected:
         #   - First call: modal panel rendered; rule
         #     persisted to a tmp platform.yaml; tool runs.
         #   - Second call: no modal; rule matches; tool
         #     runs silently.

   TC-PERM-DISCOVERY-02 — Interactive no-rule → deny once → next call prompts again.
     def test_no_rule_deny_once_does_not_persist():
         """User picks [n] (deny once); no rule persisted;
         next invocation prompts again."""

   TC-PERM-DISCOVERY-03 — Non-interactive no-rule → deny.
     def test_no_rule_no_callback_raises_unauthorized():
         """Without a registered callback (non-chat
         context), no-rule still raises ToolUnauthorized.
         Regression guard against silent default-allow."""

   TC-PERM-DISCOVERY-04 — Stacking: agent rule wins.
     def test_agent_permission_overrides_bundle_and_platform():
         """An agent's `permissions:` rule for tool.X.Y
         wins over a bundle rule + a platform rule for the
         same action."""

   TC-PERM-DISCOVERY-05 — Stacking: bundle wins over platform.
     def test_bundle_permission_overrides_platform():
         """A bundle's `permissions:` rule wins over a
         platform rule for the same action."""

2. Add the TCs to process/test-catalog.md under a new
   "Tool Permission Discovery (PERM-DISCOVERY)" section.

3. Update docs/how-to/permissions.md:
   - New subsection "First-use prompts": explain that in
     chat mode, an unconfigured tool prompts on first
     invocation rather than hard-failing. The modal is the
     same one users already know.
   - New subsection "Four-level stack": document
     agent > bundle > platform > default precedence with
     a concrete example.
   - New subsection "Bundle permissions": show the
     `bundle.yaml.permissions:` shape with a realistic
     example (e.g., a bundle shipping a `git-write` tool
     with a `prompt`-effect rule).
   - **New subsection "Naming tools across bundles" (v1 caveat
     per E-QOL-PERMDISC-001)**: action names are currently
     single-segment (`tool.<name>`), so two tools with the
     same name in different bundles share authz rules.
     Workaround: name your bundle's tools uniquely if you
     want distinct rules. Forward-reference to
     `qol-action-namespace-bundle-scoped.md` for the
     migration to bundle-rooted names.
   - Cross-reference docs/reference/schemas.md for the
     formal schema.

4. Update docs/reference/schemas.md:
   - At the BundleManifest section, document the new
     `permissions: list[PermissionRule]` field.
   - At the existing `permissions` table, note the new
     bundle-level location + the four-level stack.

5. Pre-fix verification:
   - git-stash Step 2's code. Run TC-PERM-DISCOVERY-01.
     Confirm it FAILS with the pre-fix behavior
     (ToolUnauthorized at first call; no modal). Document
     the failure output in the PR description.

6. Manual e2e:
   a. In ~/Documents/kolsch (or a fresh workspace), create
      a fresh agent in a fresh bundle that declares a tool
      whose action namespace has NO rule in platform.yaml.
   b. Run `kolsch chat` against the agent.
   c. Send a prompt that invokes the tool.
   d. Verify:
      - The modal fires with the no-rule UX (panel shows
        action name + tool name + no "matched rule" line).
      - Pick [a] to persist.
      - Re-invoke the tool; verify no modal; tool runs.
   e. Capture session JSONL excerpt showing:
      - The first invocation's authz path (no rule → modal).
      - The persisted rule in platform.yaml after the
        chat exits.
      - The second invocation with no modal.

7. Hand off to the user. The PR description must:
   - Show the manual e2e JSONL excerpts.
   - Ask the user to follow docs/how-to/permissions.md's
     new "First-use prompts" subsection and confirm the
     UX matches what's described.
   - Note the reviewer's verdict stays REVISE-PENDING-USER
     until the user confirms.

8. Follow-up tickets:
   - If the modal's panel render with rule_id=None
     looks awkward, file a tiny qol-* for the wording.
   - If users frequently want to persist to bundle.yaml
     or agent.yaml directly from the modal, file
     `qol-modal-persistence-scope-toggle.md`.

Scope: ~150 LOC tests + doc updates + manual report.

ESCALATION PATH: if the manual e2e shows the modal panel
rendering breaks with rule_id=None (e.g., a Rich/string
formatting error), revise Step 2 — don't patch here.

Acceptance: all five TCs pass; pre-fix verification
documented; manual e2e shows the no-rule modal flow
works; user is asked to confirm.
```

### Reviewer prompt

```
[standing instructions]

Review Step 3.

Checks:

1. All five TCs (PERM-DISCOVERY-01..05) exist and pass.

2. Pre-fix verification: PR description shows
   TC-PERM-DISCOVERY-01 FAILING on pre-fix code with the
   expected pre-fix failure (ToolUnauthorized, no modal).
   If absent, fail.

3. Manual e2e: PR description has captured JSONL
   excerpts for:
   - First invocation (no rule → modal → user picks [a]).
   - Persisted rule in platform.yaml (post-chat exit).
   - Second invocation (no modal; rule matches).

4. docs/how-to/permissions.md gained the three new
   subsections (First-use prompts, Four-level stack,
   Bundle permissions). Each has a concrete example.

5. docs/reference/schemas.md documents the new
   BundleManifest.permissions field.

6. process/test-catalog.md has the PERM-DISCOVERY section.

7. The user-confirmation request is in the PR
   description. Verdict stays REVISE-PENDING-USER until
   the user confirms.

8. Follow-up tickets named are FILED in process/ (not
   just listed).

9. Full test suite green.

10. NO regressions in existing modal tests
    (test_chat_modal.py).

Verdict: PASS (only after user confirms), REVISE-PENDING-USER,
or FAIL.
```

---

## Part 4 — How to escalate

```markdown
### Escalation E-QOL-PERMDISC-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–3
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:** 2–3 paths.
- **Estimated cost:**

---

### Coordinator resolution — E-QOL-PERMDISC-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Step 1 finds the home-bundle-rooted action namespace
  isn't wired correctly (defect in
  qol-prefix-defaults-and-unified-refs.md).
- Step 1 finds the catalog walker drops bundle.yaml extra
  fields and the loader needs a larger refactor.
- Step 2 finds integrating bundle permissions tangles the
  authz provider into the asset catalog in surprising
  ways.
- Step 3's manual e2e shows the no-rule modal panel
  renders incorrectly (Step 2 incomplete).

When NOT to escalate:
- Doc wording.
- The exact field name for the new bundle.yaml field
  (`permissions:` matches agent.yaml).
- Whether the new branch in `allow` uses an if/elif
  ladder or a small Strategy class.

---

## Part 5 — Coordinator Escalation Log

### E-QOL-PERMDISC-001 — Action namespace is single-segment, not bundle-rooted

- **Raised by:** developer (Step 1 verification writeup), seconded by reviewer (APPROVE-TO-PROCEED verdict noting the escalation is parked).
- **Step:** 1
- **Trigger:** Spec Decision #5 asserted that
  `qol-prefix-defaults-and-unified-refs.md` shipped a
  bundle-rooted action namespace (`tool.kolsch.bash`).
  Verified: that QoL shipped at commit `5d744f3` with the
  ref convention only (`kolsch/bash` instead of
  `kolsch/tools/bash`). The action namespace was untouched.
  `src/tools/decorator.py:128` still returns
  `f"tool.{self.name}"`. `src/tools/dispatch.py:84` passes
  `defn.action_name` — a single-segment string. The four
  builtins all have actions `tool.bash`, `tool.read_file`,
  `tool.write_file`, `tool.list_files`.
- **Why this is not a guess:** verified by
  `git show --stat 5d744f3 | grep decorator` returning
  empty + reading decorator.py:128 + dispatch.py:84
  directly.
- **Companion finding (same writeup):** `dispatch.py:84`
  does NOT pass `agent_permissions=` to `authz.allow`. The
  agent-level `permissions:` field on `ResolvedAgentConfig`
  is wired in the schema and accepted by `authz.allow` (per
  `authz.py:337`) but never actually supplied at the
  dispatch boundary. Agent permissions are dead code at
  dispatch today. Not a blocker for E-QOL-PERMDISC-001 but
  needs fixing in Step 2 since this QoL is the first one
  to USE layered authz at dispatch.
- **Orthogonal finding (same writeup, not an escalation):**
  `tools.permissions.PermissionsBinding` is defined but
  `permissions.bind(...)` is never called in `src/`. So
  `check`/`require` from inside tool bodies raise
  `ToolPermissionsNoContext` today. Worth a separate
  ticket; not blocking.
- **Options:**
  - **(1)** Fold the namespace migration into this QoL.
    ~3 hours. Inflates scope to "modal-on-no-rule +
    bundle permissions + global rule-string migration +
    deprecation warning surface + starter-bundle rule
    updates + doc cascade."
  - **(2)** Defer to a new follow-on
    `qol-action-namespace-bundle-scoped.md` filed now so
    the deferred work has a home. This QoL ships against
    single-segment action names.
  - **(3)** Accept v1 caveat; document in permissions.md;
    no follow-up doc filed.
- **Developer's recommendation:** Option 3 (accept +
  document).

---

### Coordinator resolution — E-QOL-PERMDISC-001

- **Decided by:** coordinator
- **Decision:** **Option 2.** Defer the action-namespace
  migration to a new follow-on ticket
  (`qol-action-namespace-bundle-scoped.md`) filed alongside
  this resolution. This QoL ships against single-segment
  action namespaces with a documented v1 caveat. The
  companion `dispatch.py:84 agent_permissions` defect goes
  in Step 2.
- **Rationale:** Option 1 inflates scope ~3 hours into a
  coherent project of its own (migration + deprecation +
  doc cascade); bundling delays both halves. Option 3 is
  functionally close to Option 2 but loses the tracked
  follow-up — the namespace migration is real work that
  shouldn't be forgotten between conversations. Option 2
  costs a small stub doc but ensures the work has a home.
  The v1 caveat (tools sharing names across bundles share
  rules) is acceptable because the four builtins all live
  in the kolsch bundle and don't collide; user-authored
  bundles can name uniquely until migration ships.
- **Affects:**
  - Spec Part 1 Decision #5 rewritten to reflect the
    single-segment reality + the v1 caveat.
  - Spec Part 6 Out-of-scope gains the namespace-migration
    line + the PermissionsBinding orthogonal-finding line,
    both with forward references.
  - Step 2 task list expanded: new task 2 wires BOTH
    `agent_permissions=` (companion fix) AND
    `bundle_permissions=` at `dispatch.py:84`.
  - Step 2 task 3 (renumbered) updates `authz.allow`'s
    rule-search to use both kwargs.
  - Step 2 task 6 (renumbered) "NO change" list adds
    `decorator.py:128` to explicitly forbid namespace
    edits.
  - Step 2 task 7 (renumbered) gains
    `test_dispatch_threads_agent_permissions` as a
    regression guard.
  - Step 3 task 3 (docs) gains a "Naming tools across
    bundles" v1-caveat subsection in permissions.md with
    forward reference to the migration follow-up.
  - Two follow-up stubs filed alongside this resolution:
    - `qol-action-namespace-bundle-scoped.md` —
      migration to bundle-rooted action names.
    - `qol-tool-permissions-binding-unused.md` —
      `PermissionsBinding.bind(...)` dead-code finding.
- **Resume instruction:** Dev proceeds with Step 2 against
  the revised task list. The two follow-up stubs exist as
  placeholders; future agents pick them up cold from the
  design context captured there.

---

## Part 6 — Out of scope (consolidated)

- `@tool` decorator changes.
- Per-tool flag for opting in / out.
- `[scope]` toggle on the modal for choosing persistence
  target.
- **Action namespace migration to bundle-rooted form
  (`tool.<home_bundle>.<name>`).** The prereq QoL
  (`qol-prefix-defaults-and-unified-refs.md`) shipped the
  ref convention but NOT the action namespace. The
  migration is filed at
  `qol-action-namespace-bundle-scoped.md` per
  E-QOL-PERMDISC-001 resolution. This QoL ships against
  single-segment action names with a v1 caveat.
- **`tools.permissions.PermissionsBinding` wiring.** The
  PermissionsBinding class is defined but `permissions.bind(...)`
  is never called in `src/` today, so `check`/`require`
  from inside tool bodies raise `ToolPermissionsNoContext`.
  Filed at `qol-tool-permissions-binding-unused.md`. Not
  blocking this QoL.
- Audit log of persisted rules beyond the existing event
  log.
- Removing the `effect: prompt` rule mechanism.
- Migrating existing tools or workspaces.

---

## Part 7 — Related work

- **Blocks**: NONE. This QoL is leaf in the dependency
  graph.
- **Blocked by**:
  `qol-prefix-defaults-and-unified-refs.md` Step 2 must be
  in main first (home-bundle-rooted action namespace
  convention).
- **Sibling specs from the same conversation**:
  - `qol-yaml-version-fields-optional.md` — independent;
    shipped first.
  - `bug-model-line-placeholder-collides-with-jinja.md` —
    independent.
  - `bug-engine-extends-parent-not-rendered.md` — uses the
    same Phase 2.2 `permissions:` composition shape;
    confirms inheritance composes per RFC 7386 (Category B
    of that bug's corpus).
- **Existing modal context**: built in Phase 2.1 Step 24d
  (`src/cli/_modal.py` + `bug-chat-deny-stops-and-esc-interrupts.md`
  for the deny-stops-the-exchange behavior layered on top).
- **Existing authz model**:
  `docs/explanation/secrets-sandbox-authz-contract.md` is
  the contract surface this QoL extends.
- **Possible follow-ups**:
  - `qol-modal-persistence-scope-toggle.md` — let users
    pick persistence target (workspace / bundle / agent)
    from the modal.
  - `qol-tool-permission-audit-log.md` — surface a
    history of persisted rules + their triggering tool.
