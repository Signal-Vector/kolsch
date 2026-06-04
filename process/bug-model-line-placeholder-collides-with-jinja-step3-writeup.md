# Step 3 closure — `bug-model-line-placeholder-collides-with-jinja.md`

**Status:** Step 3 complete. Acceptance criteria 1-8 verified end-to-end. Follow-up `qol-zero-config-first-run-experience.md` filed (Draft 0.1). Ready for closure.

---

## 1. Docs landed

| File | Change |
|---|---|
| `docs/how-to/workspace-and-setup.md` | New section "Where the starter agent's model comes from" (lines 116-145). Names the `defaults.model` mechanism, what setup writes per connectivity state, and the two override paths (workspace-wide via `platform.yaml` / per-agent via the agent YAML). |
| `docs/tutorials/first-agent.md` | Three updates: (a) the "you skipped the provider step" paragraph now points users at `platform.yaml`'s `defaults.model`; (b) the "what setup produced" example shows `defaults.model` populated and helper.yaml with no `model:` field; (c) the "swap your agent over" walkthrough leads with editing `defaults.model` and frames the agent-level `model:` as the per-agent escape hatch. |
| `docs/reference/schemas.md`, `AGENTS.md` | No change — pre-Step-3 grep found zero substitution refs. |

**Conscious deviation from reviewer check #1's strict sub-clause.** `docs/how-to/workspace-and-setup.md:142` carries one historical-context sentence:

> "The setup-time `{{MODEL_LINE}}` placeholder mechanism was removed in E-BUG-MODELLINE-001 (it collided with Phase 2.2 Jinja syntax in agent YAMLs). The starter file now ships as plain, unprocessed YAML."

This violates the literal "no mention of `{{MODEL_LINE}}`" reading. Kept deliberately as a one-line breadcrumb for readers hitting stale PRs / commit comments / old issues that still reference the placeholder. Reviewer flagged this as borderline-acceptable. If strict compliance is preferred, drop the sentence; the surrounding paragraph stands without it.

---

## 2. Manual e2e — Scenario A (fresh install, no provider connected)

**Setup:**

```bash
rm -rf /tmp/kolsch-e2e-modelline-a
uv run kolsch --root /tmp/kolsch-e2e-modelline-a setup --non-interactive
```

**On-disk verification:**

```
=== platform.yaml defaults ===
defaults:
  model:
    provider: fake
    model: fake-model

=== helper.yaml has no model: ===
0   # (`grep -c '^model:'` — zero hits, as expected)
```

**Agent run:**

```bash
uv run kolsch --root /tmp/kolsch-e2e-modelline-a \
  run kolsch/helper --input content="hello from scenario A"
# → {"content": {"text": "Echo: hello from scenario A"}, "exchange_id": "ev_ab2dd456-..."}
```

**`model.request` event excerpt** (extracted from `sessions/ses_*.jsonl`):

```json
{
  "event_id": "ev_f648140f-32ff-472e-a177-84c9daecc21e",
  "type": "model.request",
  "data": {
    "model": {
      "fallback": [],
      "model": "fake-model",
      "parameters": {},
      "provider": "fake"
    }
  }
}
```

**Verdict:** PASS. Acceptance #1 (no `MODEL_LINE` in src/), #2 (defaults.model carries the fake fallback), #3 (no helper.yaml mutation by setup), #5 (fresh install produces a runnable agent) all hold. The kernel's defaults composition flows the `defaults.model` value into the resolved agent's model field exactly as designed.

---

## 3. Manual e2e — Scenario B (non-default setup selection)

**Setup:** pre-seeded `/tmp/kolsch-e2e-modelline-b/platform.yaml` with the workspace-only authz rule set + `llm_providers.openrouter` carrying one model (`anthropic/claude-opus-4-7`) + `defaults: {}`. Pre-seeded `.env` with `OPENROUTER_API_KEY=sk-test-stub` so the secret-resolution step passes and the engine reaches the model.request emit site (a real key isn't needed to verify the *wiring* — only to verify a real model response, which is out of scope here).

```bash
uv run kolsch --root /tmp/kolsch-e2e-modelline-b setup --non-interactive
# → ✓ materialized bundle at bundles/kolsch/
# → ✓ wrote starter agent at bundles/kolsch/agents/helper.yaml
# → → starter agent uses openrouter/anthropic/claude-opus-4-7
```

**On-disk verification:**

```
=== platform.yaml defaults block (post-setup) ===
defaults:
  model:
    provider: openrouter
    model: anthropic/claude-opus-4-7

=== helper.yaml has no model: ===
0   # (`grep -c '^model:'` — zero hits)
```

**Agent run:**

```bash
uv run kolsch --root /tmp/kolsch-e2e-modelline-b \
  run kolsch/helper --input content="hello from scenario B"
# → AgentExecutionError (expected — sk-test-stub is not a valid OpenRouter key;
#    the LLM call errors at the wire, AFTER model.request was emitted)
```

**`model.request` event excerpt** (extracted from the session JSONL):

```json
{
  "event_id": "ev_db84a8bc-8c4b-4c7c-8511-b0574a41580a",
  "type": "model.request",
  "data": {
    "model": {
      "fallback": [],
      "model": "anthropic/claude-opus-4-7",
      "parameters": {},
      "provider": "openrouter"
    }
  }
}
```

**Verdict:** PASS. The picker chose `openrouter/anthropic/claude-opus-4-7` (the only model on the only connected provider), `_write_defaults_model` wrote it to `platform.yaml`'s `defaults.model.provider` / `defaults.model.model`, the helper agent's resolved config picked up the value via defaults composition, and the engine's pre-LLM-call `model.request` event records the exact picked provider+model. Acceptance #6 (user-driven selection takes effect) holds. The downstream API-call failure is unrelated to the bug fix — it's the test stub key, not the wiring.

---

## 4. Manual e2e — Scenario C (existing-workspace re-run)

**Target:** `~/Documents/kolsch` — the real user workspace, with the pre-existing setup-time-substituted form:

```yaml
# ~/Documents/kolsch/bundles/kolsch/agents/helper.yaml (pre-existing):
model: { provider: opencode, model: qwen3.6-plus }
```

**Pre-run hashes:**

```
MD5 (~/Documents/kolsch/platform.yaml)                        = c15a06b2efcd8840aba27a1389834a8d
MD5 (~/Documents/kolsch/bundles/kolsch/agents/helper.yaml)    = daa9ac857b354a39c5c7c9035b8a1141
```

**Re-run:**

```bash
uv run kolsch --root ~/Documents/kolsch setup --non-interactive
# → ✓ workspace already initialized at /Users/andrecohen/Documents/kolsch
# → ✓ validated 3 skill(s)
# → ✓ provider(s) already connected: opencode, openrouter
# → ✓ starter agent already at /Users/andrecohen/Documents/kolsch/bundles/kolsch/agents/helper.yaml
# → ✓ test run completed successfully
```

**Post-run hashes:**

```
MD5 (~/Documents/kolsch/platform.yaml)                        = c15a06b2efcd8840aba27a1389834a8d
MD5 (~/Documents/kolsch/bundles/kolsch/agents/helper.yaml)    = daa9ac857b354a39c5c7c9035b8a1141
```

**Bytes are identical pre and post.** No model.request JSONL excerpt is meaningful here — the observation IS that nothing happens.

**Drift handling — load-bearing line:** the drift detector is a silent no-op today. Step D's "agent already at <path>" short-circuit at `src/cli/_setup.py:507-509` fires; the user's existing helper.yaml is untouched; the new shipped form (no `model:` field) is irrelevant on re-run because the bundle is never re-materialized. Per Step 1 §5.2 of the bug spec, this becomes meaningful only after `qol-setup-evolution-and-defaults.md` Step 4's bundle-drift detector lands — at which point the detector will surface the new-shipped-form vs. user-edited-form mismatch and offer the four-choice prompt. Until then, the migration story for existing workspaces is "do nothing, and that's fine, because every existing workspace already has a substituted (no-`{{MODEL_LINE}}`) helper.yaml that continues to resolve correctly."

**Verdict:** PASS for acceptance #4 (existing user workspaces are unaffected). The escalation condition ("existing-workspace re-run shows unexpected drift-handling behavior") is NOT triggered — the observed silent no-op is the expected current state.

---

## 5. Follow-up filed

`process/qol-zero-config-first-run-experience.md` (Draft 0.1). Owns the `default_platform_payload` seed-value decision that E-BUG-MODELLINE-001 explicitly declared out of scope. The QoL lays out three candidate seed values (keep `anthropic/claude-opus-4-7`; switch to `fake/fake-model`; conditional on env-detected provider keys), three open coordinator questions (seed value; should `_setup_platform_payload`'s `defaults: {}` wipe stop; should we add a `kolsch doctor` advisory), and draft acceptance criteria pending coordinator selection.

The QoL also surfaces an adjacent latent issue caught during Scenario B's first run-attempt: **alias strings in `defaults.model` aren't expanded by the resolver.** `pre_resolve_model` is defined at `src/assets/assembler.py:165` but never called from the assembly flow (`asset_manager.py:236-245` invokes only `pre_resolve_prompts`). So a `defaults.model: opus` value (alias string) reaches `ResolvedAgentConfig.model_validate(...)` un-expanded, and Pydantic raises `extra_forbidden` / type errors because `ModelSpec` requires `{provider, model}` dicts. Today's setup-time picker happens to write a canonical dict in the most common path, so users don't hit this — but a future picker change OR a manual `defaults.model: <alias>` edit would. Flagged in the QoL's Part 3 "Related work" as needing its own ticket.

---

## 6. Test suite

```
uv run pytest tests/ --no-cov -q
# → 1073 passed, 1 skipped, 4 deselected in 68.67s
```

The lone skip is the pre-existing unrelated TC-INT-07 (PostgresBackend + RedisEventBus — Phase 2 scale-up work). Touched-file ruff: clean (`src/cli/_setup.py`, `tests/component/test_setup_no_model_line.py`, the four updated test files, the two updated docs files).

---

## 7. Closure summary

All eight acceptance criteria from `bug-model-line-placeholder-collides-with-jinja.md` Part 2 satisfied:

| # | Criterion | Verified by |
|---|---|---|
| 1 | `{{MODEL_LINE}}` appears nowhere in `src/` | `grep -rn MODEL_LINE src/` → 0 hits |
| 2 | Starter ships a working default via `platform.yaml.defaults.model` | Scenario A on-disk verification + model.request excerpt |
| 3 | `kolsch setup` no longer string-substitutes helper.yaml | Step 2 code removal + Scenario A/B helper.yaml has no `model:` line |
| 4 | Existing user workspaces are unaffected | Scenario C md5 byte-equality + drift-handling line |
| 5 | Fresh init produces a runnable agent | Scenario A's `"Echo: hello from scenario A"` + model.request `fake/fake-model` |
| 6 | User-driven setup with a different model takes effect | Scenario B's defaults.model write + model.request `openrouter/anthropic/claude-opus-4-7` |
| 7 | Docs surface the change | Step 3 §1 — workspace-and-setup.md new section + first-agent.md updates |
| 8 | Regression tests cover both paths | Step 2's `tests/component/test_setup_no_model_line.py` (3 tests: shape, default selection, non-default selection) + the six existing-test updates |

Bug ready for closure.
