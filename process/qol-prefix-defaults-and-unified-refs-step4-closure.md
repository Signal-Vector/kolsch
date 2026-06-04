# QoL — unified ref convention · Step 4 closure

**Status:** Both e2e scenarios pass; inheritance fix's Step 4
is unblocked; user-confirmation request below.

---

## 1. Manual e2e — Scenario A (new form, clean execution)

### Workspace

Tmp workspace at `/tmp/kolsch-e2e-refs/` (ephemeral; not part
of the change set).

```
/tmp/kolsch-e2e-refs/
├── .env                                       (empty)
├── platform.yaml                              (rule_based authz, llm_providers: {} → EchoLLM fallback)
├── bundles/
│   ├── kolsch/                                (parent bundle)
│   │   ├── bundle.yaml
│   │   ├── agents/helper.yaml                 (parent agent — minimal prompt + 2-seg tool ref)
│   │   └── tools/echo.py                      (stub @tool — catalogued as kolsch/tools/echo internally)
│   └── demo/                                  (child bundle)
│       ├── bundle.yaml
│       └── agents/
│           ├── child_new.yaml                 (Scenario A — new form)
│           └── child_legacy.yaml              (Scenario B — legacy form)
```

### `child_new.yaml` (verbatim)

```yaml
agent_id: ag_child_new
name: child_new
description: e2e — new ref convention
extends: kolsch/helper                          # ← 2-segment catalog ref (NO `workspace:` prefix)
prompt: |
  You use the new 2-segment ref form for tools.
io:
  mode: conversational
subsystems:
  tools:
    variant: standard
    config:
      declared:
        - ref: kolsch/echo                      # ← 2-segment tool ref
```

### Command + result

```
$ cd /tmp/kolsch-e2e-refs
$ KOLSCH_WORKSPACE=$(pwd) kolsch -v run demo/child_new --input content=hello
INFO assets: catalog: registered 1 tools from bundles/kolsch/tools/
{"content": {"text": "Echo: hello"}, "exchange_id": "ev_17775542-cebc-4979-a9a0-1db750b75939"}
```

**Zero `config.deprecated_ref_form` warnings** in stderr. The
agent loaded, the `extends:` chain composed parent + child,
the tool ref resolved through the catalog via `get_asset`
with `field_asset_type="tool"`, and the exchange ran to
completion under EchoLLMDriver.

### `config.set` excerpt — refs resolved correctly

From `sessions/ses_70ffdf0e-….jsonl`:

```json
{
  "type": "config.set",
  "event_id": "ev_455f1308-458e-4a2f-8102-693c95eba2fd",
  "data": {
    "resolved_config": {
      "agent_id": "ag_child_new",
      "name": "child_new",
      "description": "e2e — new ref convention",
      "extends": null,                          ← stripped by inheritance walker (correct)
      "prompt": "You use the new 2-segment ref form for tools....",
      "subsystems": {
        "tools": {
          "variant": "standard",
          "config": {
            "declared": [{"ref": "kolsch/echo"}]   ← 2-seg form preserved through merge
          }
        }
      },
      "model": {"provider": "fake", "model": "fake-model", ...}   ← inherited from parent
    }
  }
}
```

Cross-validation:
- `extends: null` after resolve — the inheritance walker strips
  the field after composing the chain (per
  `asset_manager.build_resolved_config:165`).
- `model.provider == "fake"` — inherited from
  `bundles/kolsch/agents/helper.yaml` (child YAML omits
  `model`); proves the RFC-7386 merge composed parent + child.
- `tools.config.declared[0].ref == "kolsch/echo"` — the
  canonical 2-segment form survives the round-trip into
  `config.set`.

---

## 2. Manual e2e — Scenario B (legacy form, deprecation
visible, agent still works)

### `child_legacy.yaml` (verbatim)

```yaml
agent_id: ag_child_legacy
name: child_legacy
description: e2e — legacy ref convention (still works, emits deprecation warnings)
extends: workspace:bundles/kolsch/agents/helper.yaml   # ← legacy workspace: + 4-seg file path
prompt: |
  You use the legacy 3-segment and workspace-prefixed ref forms.
io:
  mode: conversational
subsystems:
  tools:
    variant: standard
    config:
      declared:
        - ref: kolsch/tools/echo                       # ← legacy 3-segment ref
```

### Command + result

```
$ KOLSCH_WORKSPACE=$(pwd) kolsch -v run demo/child_legacy --input content=hello
INFO assets: catalog: registered 1 tools from bundles/kolsch/tools/
WARNING templating.context_files: deprecated ref form 'workspace:bundles/kolsch/agents/helper.yaml'; prefer 'bundles/kolsch/agents/helper.yaml' at context_files[].path or extends:
WARNING assets: deprecated ref form 'kolsch/tools/echo'; prefer 'kolsch/echo'
{"content": {"text": "Echo: hello"}, "exchange_id": "ev_36e4f72e-dc2a-421d-b22e-8c2ec39fe0c0"}
```

Two `config.deprecated_ref_form` (Recoverable) warnings fired
— one for the `workspace:` prefixed `extends:` (logged via
`templating.context_files` logger), one for the 3-segment
tool ref (logged via the `assets` logger). The agent
**still ran to completion** — the legacy form's parser fallback
preserved behavior end-to-end.

### `config.set` excerpt — legacy ref preserved verbatim

From `sessions/ses_86a74053-….jsonl`:

```json
{
  "type": "config.set",
  "data": {
    "resolved_config": {
      "agent_id": "ag_child_legacy",
      "name": "child_legacy",
      "extends": null,
      "subsystems": {
        "tools": {
          "config": {
            "declared": [{"ref": "kolsch/tools/echo"}]   ← legacy form preserved on the wire
          }
        }
      },
      "model": {"provider": "fake", ...}                ← inherited from parent (same as Scenario A)
    }
  }
}
```

The resolved config preserves the legacy `kolsch/tools/echo`
verbatim in `subsystems.tools.config.declared[0].ref`. The
canonical 2-segment form is what the migration table
recommends, but the legacy form is accepted: the engine's
tool-registration pass at `engine.py:_resolve_declared_tool_paths`
calls `get_asset(ref, field_asset_type="tool")` which routes
through `resolve_shorthand` and accepts the legacy form
(emitting the deprecation warning observed above).

---

## 3. Acceptance criteria — Part 2 sweep

| # | Criterion | Status |
|---|---|---|
| 1 | `<name>` (within-bundle) resolves correctly | ✓ — exercised by `test_resolve_shorthand_1_segment_returns_full_ref` |
| 2 | `<bundle>/<name>` (cross-bundle) resolves correctly | ✓ — Scenario A above + `test_extends_new_catalog_ref_form` |
| 3 | Plain paths in `extends:` and `context_files[].path` default to `workspace:` | ✓ — `test_plain_path_resolves_under_workspace_root` + Scenario A's `extends: kolsch/helper` |
| 4 | `kolsch:<path>` still resolves to installed package | ✓ — `test_kolsch_prefix_resolves_real_repo_doc` |
| 5 | Legacy 3-segment form still parses with deprecation warning | ✓ — Scenario B above + `test_parse_ref_3_segment_legacy_emits_deprecation_warning` |
| 6 | Legacy `workspace:` prefix still parses with similar warning | ✓ — Scenario B above + `test_workspace_prefix_resolves_under_workspace_root` |
| 7 | Catalog rejects name-collision within a bundle across asset types | ✓ — `test_scan_rejects_agent_and_tool_sharing_name_in_same_bundle` |
| 8 | Starter bundle migrated to new convention | ✓ — `src/cli/_starter_bundle/kolsch/agents/helper.yaml:83-86` shows 4× `kolsch/<name>` |
| 9 | Documentation surfaces the new convention | ✓ — `docs/reference/schemas.md` §A.8 rewritten; §A.8.1 migration table; how-to + tutorial examples migrated |
| 10 | Regression tests cover all four major paths | ✓ — `test_refs_grammar.py` (1/2/3-seg + invalid char rejection); `test_extends_new_form.py` (new + legacy); `test_catalog_uniqueness.py` (collision); `test_context_files_implicit_workspace.py` (plain / workspace: / kolsch:) |

---

## 4. Migration impact stats

### Production code
| Area | Change |
|---|---|
| `src/errors/` | `ConfigDeprecatedRefForm` (Recoverable) + `AssetsDuplicateName` (Fatal) added; catalog registrations + re-exports |
| `src/assets/refs.py` | Rewritten — new `parse_ref(field_asset_type, emit_warning)` + extended `resolve_shorthand` |
| `src/assets/catalog.py` | Pass-4 `(bundle, name)` collision check |
| `src/assets/asset_manager.py` | `get_asset(field_asset_type=…)`; `build_resolved_config` uses `agent_entry` for bundle/name; threads catalog to `resolve_extends_chain` |
| `src/assets/assembler.py` | Singular asset-type comparison (`"prompt"` not `"prompts"`) |
| `src/templating/context_files.py` | `resolve_path(emit_warning=…)`; plain paths workspace-rooted; explicit `workspace:` deprecated |
| `src/templating/inheritance.py` | New `_resolve_extends_to_path` handles catalog refs |
| `src/schemas/config.py`, `src/kernel/partials.py` | Validators loosened to non-empty string |
| `src/skills/validator.py`, `src/skills/subsystem.py` | Layer-3 validator + subsystem route through `resolve_shorthand` |
| `src/engine/engine.py`, `src/api/routes.py`, `src/cli/skills.py` | `get_asset(field_asset_type=…)` at each call site; sync helper `_get_catalog_entry_sync` extended for 1/2-seg input |
| `src/cli/_starter_bundle/kolsch/agents/helper.yaml` | 4× declared tool refs migrated to 2-segment |
| `src/cli/chat.py:101` | `_DEFAULT_AGENT_REF = "kolsch/helper"` (was `"kolsch/agents/helper"`) |
| `src/cli/main.py`, `src/cli/skills.py`, `src/cli/_slash.py` | Help-text examples + comments migrated |
| **Total src/ files touched in this QoL** | **~20** |

### Tests
| Area | Change |
|---|---|
| New tests added | `tests/unit/test_refs_grammar.py` (25 tests) · `tests/unit/test_catalog_uniqueness.py` (4) · `tests/unit/test_context_files_implicit_workspace.py` (3) · `tests/integration/test_extends_new_form.py` (2) |
| Tests updated for new behavior | `tests/component/templating/test_context_files.py`, `tests/unit/schemas/test_envelope_fields.py`, `tests/unit/test_errors.py`, `tests/component/cli/test_chat_slash.py` (registry now 9 with `/skill`), `tests/component/test_cli_smoke.py` (`skills` added to coverage list) |
| Test fixtures migrated (refs) | `tests/component/cli/test_chat_force_skill.py` (24), `tests/component/cli/test_chat_lifecycle.py` (17), `tests/integration/test_skills_extends.py` (2 `workspace:` prefixes), `tests/integration/test_tool_wiring.py` (1 setup ref), plus the 11 files migrated for `kolsch/agents/helper → kolsch/helper` |

### Docs
- `docs/reference/schemas.md` — §A.8 rewritten, §A.8.1 migration table + §A.8.2 uniqueness rule added.
- `docs/how-to/{chatting-effectively,agent-templating,forcing-a-skill,workspace-and-setup,find-models,configure-fallbacks}.md` — examples migrated.
- `docs/tutorials/first-agent.md` — `kolsch chat kolsch/helper` etc.
- `docs/reference/module-api-reference.md`, `docs/explanation/{architecture-roadmap,asset-management-contract,skills-subsystem,tools-subsystem,error-model,file-handling,framework-protocol-design}.md` — examples migrated.

### Process
- `process/test-catalog.md` — TC titles + invocation strings migrated.
- `process/bug-engine-extends-parent-not-rendered.md` — Part 7 Coordinator Escalation Log gained an entry confirming Step 4 pre-condition is satisfied (see §5 below).

### Final test results
**1070 passed, 1 skipped** (the lone skip is the pre-existing
TC-INT-07 Postgres/Redis scaffolding skip, unrelated to this
QoL). Zero failures.

### Final grep audit
**22 files / 55 hits remain.** Each is in one of four
legitimate categories pre-cleared by the reviewer:

| Category | Example |
|---|---|
| (a) Filesystem-path docstrings, NOT refs | `src/tools/subsystem.py:153` — `<workspace>/bundles/kolsch/tools/` |
| (b) Explicit migration table | `docs/reference/schemas.md` §A.8.1 |
| (c) Intentional legacy-deprecation regression tests | `tests/unit/test_refs_grammar.py` (deliberate 3-seg form) |
| (d) Catalog-internal 3-seg keys / production error-payload assertions | `tests/component/test_canonical_skills.py:175` (catalog `entry.ref`) |

---

## 5. Inheritance-fix Step 4 — UNBLOCKED

`process/bug-engine-extends-parent-not-rendered.md` Step 4
(line 912 onward) carries a load-bearing PRE-CONDITION:

> **PRE-CONDITION**: `qol-prefix-defaults-and-unified-refs.md`
> MUST be in main before this step starts. … If the QoL
> hasn't shipped, BLOCK this step and escalate.

That pre-condition is **satisfied as of this writeup**. I've
added an entry to the bug's Part 7 Coordinator Escalation
Log naming this QoL's Steps 1–4 closure and pointing the
inheritance fix's Step 4 docs at the new convention. The
inheritance fix's developer may proceed with
`docs/how-to/extending-an-agent.md` using the canonical
2-segment ref form from day one.

**No reverse blocker.** This QoL does not need anything from
the inheritance fix's Step 4 to close — the e2e ran cleanly
against agent inheritance using new-form refs (Scenario A),
exercising the integration `asset_manager.build_resolved_config`
→ `templating.inheritance.resolve_extends_chain` →
`templating.inheritance._resolve_extends_to_path` path.

---

## 6. User-confirmation request (for the PR description)

Please confirm in the PR comments:

- [ ] A fresh `kolsch chat` session against a workspace agent
  authored in the **new** convention (`extends: kolsch/helper`,
  2-segment tool refs in
  `subsystems.tools.config.declared[].ref`) works cleanly with
  **no** deprecation warnings in stderr. Scenario A above is
  the minimal repro; running against `~/Documents/kolsch`'s
  helper (which the user's workspace migration in this PR
  already converted to the new form) is the natural
  user-facing check.
- [ ] When a session uses the **legacy** form (`extends:
  workspace:bundles/X/agents/Y.yaml` and/or `kolsch/tools/X`
  in declared refs), `kolsch run -v` / `kolsch chat -v` shows
  `WARNING ... deprecated ref form '...'; prefer '...'` lines
  at agent load AND the agent continues to run to completion.

If either check fails, escalate per Step 1 escalation path —
that would mean Step 2 missed a parser case.

---

## 7. Out of scope reminders (Part 6)

Still hold:
- Shipping canonical kolsch agents as package resources
  (`kolsch:agents/helper` becoming a real reference) — separate
  ticket.
- Glob patterns in refs (`kolsch/tools/*`) — future.
- Removing the legacy form entirely — stays as warning-only
  for at least one minor version.
- Refactoring the catalog's internal data model — internal
  storage stays keyed on 3-segment refs.
- Multi-prefix vocabularies (`http://`, `git://`) — future.
- Auto-migrating user YAMLs — no `kolsch agents migrate` CLI.
