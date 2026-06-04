# QoL — version fields optional · Step 3 docs + e2e writeup

**Status:** docs updated; manual e2e verified defaults reach
the resolved config end-to-end; all Part 2 acceptance
criteria green; no follow-ups surfaced.

---

## 1. Doc updates landed

File: `docs/reference/schemas.md` (+13 LOC, within Step 3
~20 LOC scope).

### §A.2 — Shape diagram

`schema_version` and `assembly_version` rows annotated
inline so the diagram itself signals optionality at a
glance, with a back-pointer to the prose section that
carries the full rationale:

```diff
-├── schema_version       string        — schema version this conforms to
+├── schema_version       string        — schema version this conforms to;
+│                                        optional, defaults to "0.1" when
+│                                        omitted (§A.10)
 ...
-├── assembly_version     string        — assembly procedure version in effect
-│                                        (see Part B.5; recorded on config.set)
+├── assembly_version     string        — assembly procedure version in effect
+│                                        (see Part B.5; recorded on config.set);
+│                                        optional, defaults to "1" when
+│                                        omitted (§A.10)
```

### §A.10 — Schema and procedure versioning

Both bullets extended with the optionality note + the
load-bearing capability-check semantic for
`assembly_version`:

```diff
 - `schema_version` is on every artifact; older snapshots remain
-  interpretable.
+  interpretable. Optional in YAML; the resolved config defaults to `"0.1"`
+  when omitted. No active migration framework branches on the value today —
+  the field is reserved for future schema evolution.
 - `assembly_version` is part of every resolved config; an older event log
   replays under the assembly procedure version recorded in its `config.set`
-  events, preserving fidelity across procedure evolution.
+  events, preserving fidelity across procedure evolution. Optional in YAML;
+  the resolved config defaults to `"1"` when omitted. The engine's
+  capability check (`engine.py:_preflight` Step 2) runs against the
+  *resolved* value: declaring a version the engine doesn't support raises
+  `CapabilityMismatch` (fatal). Authors who want their YAML pinned to a
+  specific assembly contract should still declare it explicitly.
```

No new how-to or tutorial added (per Step 3 prompt task 4).

---

## 2. Manual end-to-end

### Workspace

Tmp workspace at `/tmp/kolsch-e2e-versions/` (not part of
the change set; ephemeral). Layout:

```
/tmp/kolsch-e2e-versions/
├── .env                              # empty (DotenvSecretsProvider input)
├── platform.yaml                     # rule_based authz, in_process bus,
│                                     # llm_providers: {} → EchoLLMDriver
├── bundles/
│   └── demo/
│       ├── bundle.yaml
│       └── agents/
│           └── minimal.yaml          # ← OMITS both version fields
└── sessions/
    └── ses_10a4bf3d-….jsonl          # produced by `kolsch run`
```

`minimal.yaml` (verbatim — both version fields absent):

```yaml
agent_id: ag_minimal
name: minimal
description: minimal test agent
model:
  provider: fake
  model: fake-model
prompt: "You are minimal. Be terse."
io:
  mode: conversational
```

### Command + result

```
$ cd /tmp/kolsch-e2e-versions
$ KOLSCH_WORKSPACE=$(pwd) kolsch run demo/agents/minimal --input content=hello
{"content": {"text": "Echo: hello"}, "exchange_id": "ev_2b57123f-1185-42be-9a0e-bcad1da9f254"}
```

Loads cleanly (no `ConfigInvalid`, no `CapabilityMismatch`),
runs one exchange under `EchoLLMDriver` (default fallback
when `llm_providers: {}`).

### `config.set` event excerpt — defaults applied

Extracted from `sessions/ses_10a4bf3d-….jsonl`:

```json
{
  "type": "config.set",
  "event_id": "ev_2b8363fc-8191-4dc0-92a1-59002624368f",
  "session_id": "ses_10a4bf3d-d326-4024-8d2c-d8d353357eb8",
  "thread_id": "th_46d2aa5e-f819-4f00-911e-c3cf17dec21e",
  "data": {
    "config_hash": "cf41824e3b95a30430a5cf688f742e2a7e6411bd3c2500832cca6c9b2ab0815f",
    "resolved_config": {
      "agent_id": "ag_minimal",
      "assembly_version": "1",
      "schema_version": "0.1",
      "name": "minimal",
      "description": "minimal test agent",
      "model": {"provider": "fake", "model": "fake-model", "parameters": {}, "fallback": []},
      "prompt": "You are minimal. Be terse.",
      "io": {"mode": "conversational", "inputs": {}, "outputs": {}},
      "runtime": {"max_retries": 2, "timeout_seconds": null},
      "subsystems": {},
      "extends": null,
      "permissions": [],
      "context_files": [],
      "fallback_models": []
    }
  }
}
```

The two load-bearing lines for the QoL:

```
"schema_version": "0.1"     ← layer default applied (YAML omitted the field)
"assembly_version": "1"     ← layer default applied (YAML omitted the field)
```

Nothing in the resolved config differs from a YAML that
declared the fields explicitly — `config_hash` is stable
across both forms (verified by re-running with an explicit
`schema_version: "0.1"` / `assembly_version: "1"` and
diffing the event).

---

## 3. Acceptance criteria sweep vs. spec Part 2

| # | Criterion | Status |
|---|---|---|
| 1 | Both fields optional at the partial layer | ✓ — `PartialAgentConfig` was already `str \| None = None` (Step 1 §1) |
| 2 | Defaults realized at the resolved layer | ✓ — `src/schemas/config.py:166-167` (per E-QOL-VERS-001: partial-Pydantic defaults don't propagate through the dict-flow pipeline; resolved-layer defaults do) |
| 3 | Declared YAMLs load identically | ✓ — `test_resolved_preserves_explicit_versions` + `test_build_resolved_config_preserves_explicit_versions`; full suite 1036 passed |
| 4 | Omitted YAMLs load with defaults | ✓ — `test_build_resolved_config_applies_version_defaults_when_omitted` + manual e2e excerpt above |
| 5 | `CapabilityMismatch` still fires | ✓ — `test_preflight_rejects_unsupported_assembly_version` drives `_Exchange._preflight()` with `assembly_version="999"` |
| 6 | Regression tests pin BOTH behaviors | ✓ — 4 unit + 2 integration + 1 component (capability) |
| 7 | Docs updated | ✓ — §A.2 + §A.10 |

### Note on criterion #2 vs. spec Part 1

Spec Part 1 decision #1 framed the defaults as living on
the *partial* layer ("at the partial config layer"). Step
1's verification surfaced E-QOL-VERS-001: the kernel
pipeline passes raw dicts through `resolve()`, so Pydantic
defaults on `PartialAgentConfig` are dropped before the
merge sees them. The coordinator approved moving the
defaults to `ResolvedAgentConfig` instead. The
user-visible contract Part 2 acceptance #4 specifies
(resolved config has `"0.1"` / `"1"` when YAML omits) is
preserved; only the implementation site moved.

---

## 4. Follow-up tickets

None surfaced during Steps 1–3. The Step 3 prompt task 3
instructed to file only if something came up during this
work (no fishing). Manual e2e showed the defaults flowing
exactly as Step 2 predicted; no hidden code path overrode
them; no doc gap beyond the §A.10 update was surfaced
(reference doc is sufficient — no new how-to needed per
prompt task 4).

---

## 5. Out-of-scope reminders (still hold)

Per spec Part 6:

- Removing the fields entirely from `ResolvedAgentConfig`
  — they stay; just become implicit at authoring time.
- New version-validation framework — future versions
  declare themselves explicitly; old YAMLs default to v1.
- Starter-bundle YAML simplification — reference examples
  in `src/cli/_starter_bundle/kolsch/agents/helper.yaml`
  keep both versions explicit (untouched by this QoL).
- Migrating existing user-authored YAMLs — no `kolsch
  agents migrate` command; users may leave existing YAMLs
  as-is or drop the fields on next edit.

---

## 6. PR body excerpt — for paste convenience

The reviewer asked the developer to paste the `config.set`
excerpt into the PR description. Recommended block:

> Manual e2e against tmp workspace at
> `/tmp/kolsch-e2e-versions/`. YAML omits both version
> fields; `kolsch run` loads cleanly + emits a `config.set`
> event with the layer defaults applied:
>
> ```json
> "resolved_config": {
>   ...,
>   "schema_version": "0.1",   ← default applied
>   "assembly_version": "1",   ← default applied
>   ...
> }
> ```
>
> Full event captured in
> `process/qol-yaml-version-fields-optional-step3-writeup.md`
> §2.
