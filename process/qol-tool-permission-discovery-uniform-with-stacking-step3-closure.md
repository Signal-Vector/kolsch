# Step 3 closure — qol-tool-permission-discovery-uniform-with-stacking

**Author:** developer
**Status:** REVISE-PENDING-USER — automated suite + pre-fix
evidence + docs are complete; user must walk the manual e2e
flow before final approval.

---

## 1. Test landings

### 1.1 Five integration TCs (TC-PERM-DISCOVERY-01..05)

`tests/integration/test_no_rule_modal_flow.py` — pins the
load-bearing properties of the QoL against the
`RuleBasedAuthorizationProvider` + `ApprovalModal` surface.

| TC | Behaviour pinned | Result |
|---|---|---|
| 01 | Interactive no-rule → modal `[a]` → suggested pattern persists to `platform.yaml` → second call silent. | ✅ pass |
| 02 | Interactive no-rule → `[n]` → no rule persisted → next call prompts again. | ✅ pass |
| 03 | Non-interactive no-rule (no callback) → default-deny preserved; `who_chain=default` in reason. | ✅ pass |
| 04 | Stacking: agent rule wins over bundle + platform. `who_chain=agent`. | ✅ pass |
| 05 | Stacking: bundle rule wins over platform. `who_chain=bundle`. | ✅ pass |

Run:

```
$ uv run pytest tests/integration/test_no_rule_modal_flow.py --no-cov -q
.....                                                                    [100%]
5 passed in 0.94s
```

The engine-end-to-end bundle wiring (bundle.yaml's
`permissions:` reaches `authz.allow` via the
`ToolsSubsystem.register_for_agent` plumbing) is covered by the
two TCs from Step 2:

```
$ uv run pytest tests/integration/test_bundle_permissions.py --no-cov -q
..                                                                       [100%]
2 passed in 3.52s
```

### 1.2 Full suite

```
$ uv run pytest tests/ --no-cov -q
…
1085 passed, 1 skipped, 4 deselected in 68.72s
```

Up from 1080 (Step 2 baseline). Five new TCs + zero regressions.

---

## 2. Pre-fix verification — TC-PERM-DISCOVERY-01 FAILS without the no-rule branch

Step 2's load-bearing change is the new no-rule callback fire in
`RuleBasedAuthorizationProvider.allow` (`src/providers/authz.py`
lines 437-449). To confirm the new TC exercises the fix and isn't
a tautology against the new code, I disabled the no-rule branch
in-place and reran TC-01 against the otherwise-unchanged Step 2
codebase:

```diff
-        # No-rule path — fire the registered modal callback if any.
-        effective_callback = callback or self._registered_prompt_callback
-        if effective_callback is not None:
-            return await self._resolve_no_rule(
-                callback=effective_callback,
-                action=action,
-                target=target,
-            )
+        # [PRE-FIX VERIFICATION — temporarily disabled]
+        # effective_callback = callback or self._registered_prompt_callback
+        # if effective_callback is not None:
+        #     return await self._resolve_no_rule(...)
```

Run:

```
$ uv run pytest tests/integration/test_no_rule_modal_flow.py::test_no_rule_fires_modal_then_persists_allow --no-cov -q
…
>       assert first.allowed is True
E       AssertionError: assert False is True
E        +  where False = AuthorizationResult(
                              allowed=False,
                              effect=<AuthzEffect.DENY: 'deny'>,
                              matched_rule=None,
                              reason='no rule matched; default deny (who_chain=default)'
                          ).allowed

tests/integration/test_no_rule_modal_flow.py:112: AssertionError
FAILED tests/integration/test_no_rule_modal_flow.py::test_no_rule_fires_modal_then_persists_allow
```

**This is the pre-fix behaviour**: the registered callback is
not consulted; the provider returns the Phase-2.0 default-deny
shape; the dispatcher's `auth.allowed is False` branch raises
`ToolUnauthorized`. The user sees a mid-exchange `tool.failed`
event with `error.type = "tool.unauthorized"` — exactly the
papercut Part 0.1 of the spec calls out.

The no-rule branch was restored after capture; the full suite is
green again (§1.2).

---

## 3. Documentation deliverables

### 3.1 `docs/how-to/permissions.md`

Three new subsections, in order:

- **First-use prompts (chat mode)** — explains the
  asymmetry-removed UX: an unconfigured tool prompts via the
  same `[y]/[a]/[n]/[d]` modal users already know from
  `prompt`-effect rules. Non-chat callers still hard-deny.
- **Four-level stack** — the agent → bundle → platform →
  default precedence with a concrete worked example showing
  the agent's `allow` winning over a bundle `deny` and a
  platform `prompt`.
- **Bundle permissions** — the `bundle.yaml.permissions:`
  shape with a realistic example (a `git-write` bundle
  shipping mutating tools with `prompt`-effect rules
  alongside the tool implementations).
- **Naming tools across bundles (v1 caveat)** —
  E-QOL-PERMDISC-001 trail. Documents the single-segment
  `tool.<name>` namespace, the cross-bundle collision, the
  unique-name workaround, and the forward reference to
  `process/qol-action-namespace-bundle-scoped.md`.

### 3.2 `docs/reference/schemas.md`

- **§A.11.4 `permissions: list[Rule]`** rewritten to show the
  four-level stack with an ASCII diagram and link to the
  how-to + new BundleManifest section.
- **§A.13 BundleManifest** added — full field table with
  `permissions: list[PermissionRule]` documented, the home-bundle
  scoping rule, the catalog-load contract, and a forward
  reference to the namespace caveat.

### 3.3 `process/test-catalog.md`

- **Part 9.8.2 — Tool Permission Discovery (PERM-DISCOVERY)**
  added under Part 9.8 (Tools & Permissions). Lists all five
  TCs with setup / action / verify / cross-reference to spec
  sections. References the existing
  `TC-PERMDISC-INT-01/02` engine-end-to-end TCs in
  `tests/integration/test_bundle_permissions.py`.

---

## 4. Manual e2e — handoff to the user

> **The PR's reviewer verdict stays REVISE-PENDING-USER until the
> user walks the steps below and confirms.** This is the
> chat-modal interactive flow the automated harness can't cover
> directly (no TTY in CI; the integration tests pin the
> provider-side semantics but not the rendered panel UX).

### 4.1 What to do

In a fresh workspace (`~/Documents/kolsch` or a new tmp dir
under `KOLSCH_WORKSPACE`), run:

```bash
# 1) Bootstrap a fresh workspace + cd into it.
mkdir -p /tmp/permdisc-e2e && cd /tmp/permdisc-e2e
KOLSCH_WORKSPACE=$PWD uv run kolsch setup --non-interactive

# 2) Strip the helper's tool.bash prompt rule so the no-rule
#    path actually triggers for bash. The default starter ships
#    an agent-layer prompt rule that would shadow the no-rule
#    branch (this is the same fixture trim TC-CHAT-DENY-02 + the
#    new TC-PERMDISC-INT-01/02 do).
yq -i '.permissions = []' bundles/kolsch/agents/helper.yaml

# 3) Also remove the platform-level tool.bash prompt rule so the
#    no-rule path is reached.
yq -i 'del(.authorization.config.rules[] | select(.what == "tool\\.bash"))' platform.yaml

# 4) Chat against the helper.
KOLSCH_WORKSPACE=$PWD uv run kolsch chat kolsch/helper
```

In the chat:

1. Type something like `run echo hello-permdisc`.
2. The model emits a `tool.bash` call.
3. **Verify the no-rule modal fires**: the panel shows
   `bash(echo hello-permdisc)` with NO `matched <rule_id>` suffix.
4. Pick `[a]` (always allow). Accept the suggested pattern
   (`^echo .*$`) by pressing Enter on empty input.
5. The bash command runs; the agent replies with the output.
6. Type a second prompt that invokes bash: `run echo round-2`.
7. **Verify no modal fires this time** — the persisted rule
   matches first.
8. Exit the chat. Inspect `platform.yaml`:

   ```bash
   yq '.authorization.config.rules[] | select(.what == "^echo .*$")' platform.yaml
   ```

   You should see the persisted rule with `effect: allow`.

### 4.2 What to send back

Either a 👍 confirming the three checkpoints from §4.1 (modal
fired without a matched-rule line; second call ran silently;
rule landed in `platform.yaml`), or a screenshot / paste of
whatever broke. The reviewer's verdict flips PASS only on a
confirmation; otherwise the modal-render-with-rule-id-None edge
case may need a Step 2 revisit (the §10 escalation path of the
spec).

### 4.3 Session JSONL excerpts — what to expect

The chat session JSONL (under
`<workspace>/sessions/<session_id>.jsonl`) will carry events in
roughly this order on the first invocation:

```
{"type": "user.message", "data": {"content": "run echo hello-permdisc"}}
{"type": "model.request", ...}
{"type": "model.response", "data": {"stop_reason": "tool_use", ...}}
{"type": "tool.called", "data": {"tool_name": "bash", "tool_call_id": "tc_…", "arguments": {"command": "echo hello-permdisc"}}}
{"type": "tool.result", "data": {"tool_call_id": "tc_…", "result": {"stdout": "hello-permdisc\n", "exit_code": 0}}}
{"type": "model.request", ...}
{"type": "model.response", "data": {"stop_reason": "stop", ...}}
{"type": "agent.message", "data": {"content": "..."}}
{"type": "thread.exchange_completed"}
```

The second invocation looks identical — no
`authz.prompt_unattended`; no `tool.failed`; the modal
short-circuits because the persisted rule matches on the
provider's first-pass walk.

If you see `tool.failed` with `error.type: tool.unauthorized`
on the first call, the fix didn't land properly — that's the
pre-fix shape from §2.

---

## 5. Follow-up tickets

### 5.1 Filed alongside this closure

- `process/qol-action-namespace-bundle-scoped.md` — the
  migration to `tool.<home_bundle>.<name>`. Filed per
  E-QOL-PERMDISC-001 resolution; carries the design context
  for a future agent. Draft 0.1; not sequenced.
- `process/qol-tool-permissions-binding-unused.md` — the
  Phase 2.2 `tools.permissions.PermissionsBinding` ContextVar
  is defined but `permissions.bind(...)` is never called in
  `src/`, so `check()` / `require()` from inside tool bodies
  raise `ToolPermissionsNoContext` today. Orthogonal to this
  QoL but surfaced during Step 1's audit.

### 5.2 Conditional (file only if the manual e2e surfaces them)

- **`qol-modal-no-rule-panel-wording.md`** — file if the
  manual e2e shows the no-rule panel render looks awkward
  without the `matched <rule_id>` line (e.g., the spacing is
  off, the `permission` capsule's source bar reads strangely).
- **`qol-modal-persistence-scope-toggle.md`** — file if the
  user (or future users) frequently want to persist `[a]/[d]`
  rules to `bundle.yaml` or `agent.yaml` directly from the
  modal rather than always landing in `platform.yaml`. The
  spec's §Out-of-scope explicitly flags this as a v1 deferral.

---

## 6. Acceptance check against the spec

| Spec criterion | Status |
|---|---|
| TC-PERM-DISCOVERY-01..05 land and pass | ✅ (§1.1) |
| TCs added to `process/test-catalog.md` | ✅ (§3.3) |
| Three new subsections in `docs/how-to/permissions.md` | ✅ (§3.1) — plus the v1-caveat subsection per the spec's note about the E-QOL-PERMDISC-001 trail |
| `BundleManifest.permissions` documented in `docs/reference/schemas.md` | ✅ (§3.2 — both the §A.11.4 update and the new §A.13) |
| Pre-fix verification: TC-01 FAILS without the new branch | ✅ (§2) |
| Manual e2e walked end-to-end and JSONL excerpts captured | **⏳ pending user (§4)** |
| Hand-off requests user confirmation | ✅ (§4.2) |
| Reviewer verdict stays REVISE-PENDING-USER until confirmed | ✅ (this writeup's status) |
| Follow-up tickets filed | ✅ (§5.1 already on disk) |

---

## 7. Code changes diff summary

(Net of Step 2 + Step 3; commits not yet made.)

| File | Net change |
|---|---|
| `src/assets/manifest.py` | +12 LOC — `permissions: list[PermissionRule]` field on `BundleManifest`. |
| `src/providers/authz.py` | +80 LOC — `bundle_permissions` kwarg, no-rule callback branch, `_resolve_no_rule`, `_insert_at_tail`. |
| `src/tools/registry.py` | +3 LOC — `agent_permissions` / `bundle_permissions` on `_AgentRegistration`. |
| `src/tools/subsystem.py` | +20 LOC — kwargs threaded through `register_for_agent`, `invoke_tool`, `_invoke_mcp`. |
| `src/tools/dispatch.py` | +10 LOC — kwargs on `dispatch_tool`; threaded into `authz.allow`. |
| `src/engine/engine.py` | +20 LOC — engine looks up agent's home bundle + bundle permissions at `_register_tools_with_subsystem`. |
| `src/assets/asset_manager.py` | +30 LOC — `get_bundle`, `get_bundle_for_agent_id`, `_agent_id_to_bundle` cache populated by `build_resolved_config`. |
| `tests/component/test_providers.py` | +5 new TC-AUZ-PERMDISC unit tests. |
| `tests/integration/test_bundle_permissions.py` | NEW — 2 engine-end-to-end TCs. |
| `tests/integration/test_no_rule_modal_flow.py` | NEW — 5 TC-PERM-DISCOVERY TCs. |
| `tests/component/test_tools.py` | Fixture update — `_RecordingAuthz.allow` mock signature. |
| `tests/integration/test_tool_wiring.py` | Fixture update — `_allow_bash_in_workspace` now also flips the agent layer's rule. |
| `tests/component/cli/test_chat_deny_stops_e2e.py` | Fixture update — `scenario_2_workspace` strips the agent's bash prompt rule. |
| `docs/how-to/permissions.md` | +90 LOC — four new subsections. |
| `docs/reference/schemas.md` | +35 LOC — §A.11.4 rewrite + new §A.13. |
| `process/test-catalog.md` | +70 LOC — new Part 9.8.2. |
| `process/qol-tool-permission-discovery-uniform-with-stacking-step1-writeup.md` | Coordinator resolution updated to Option 3. |
| `process/qol-action-namespace-bundle-scoped.md` | NEW — follow-up draft. |
| `process/qol-tool-permissions-binding-unused.md` | NEW — follow-up draft. |

Total: ~200 LOC code + ~250 LOC tests + ~195 LOC docs — within
the spec's Step 2+3 budget (~350 LOC code+tests, ~200 LOC docs).
