# Bug — `agent_id` derive-from-ref · Step 3 regression + e2e writeup

**Status:** all four TCs pass on post-fix code; pre-fix
verification captured for TC-AGENTID-01; all three manual
scenarios verified via in-process probe against
`~/Documents/kolsch`.

---

## 1. Regression tests landed

File: `tests/integration/test_agent_id_derivation.py` (new).

Four async integration tests against
`AssetManagement.build_resolved_config` (the same surface the
CLI and engine pre-flight consume):

- **TC-AGENTID-01** `test_tc_agentid_01_partial_without_agent_id_derives` —
  tmp workspace with bundle `test` + agent `demo.yaml` whose
  payload omits `agent_id`. Asserts `cfg.agent_id ==
  "ag_test_demo"` and that the other resolved fields parse.
- **TC-AGENTID-02** `test_tc_agentid_02_explicit_agent_id_wins` —
  same workspace but YAML carries `agent_id: ag_custom`.
  Asserts `cfg.agent_id == "ag_custom"` AND
  `cfg.agent_id != "ag_test_demo"` (catches a future merge-
  priority regression).
- **TC-AGENTID-03**
  `test_tc_agentid_03_malformed_explicit_agent_id_rejected` —
  YAML with `agent_id: butler`. Asserts `ConfigInvalid` raised
  with a finding whose `path` contains `agent_id` AND `message`
  contains both `ag_` and `prefix` (the AgentId regex error
  message). Proves the fix is "let users omit" NOT "accept any
  string."
- **TC-AGENTID-04**
  `test_tc_agentid_04_starter_helper_keeps_explicit_id` — reads
  the ACTUAL `src/cli/_starter_bundle/kolsch/agents/helper.yaml`,
  substitutes the `{{MODEL_LINE}}` placeholder the same way
  `src/cli/_setup.py:546-549` does, lays it down under
  `bundles/kolsch/agents/helper.yaml`, and asserts
  `cfg.agent_id == "ag_helper"` (the explicit YAML value), NOT
  the derived `ag_kolsch_helper`. Subsystem validator for
  `tools.standard` is accept-anything (we're testing identity,
  not variant body shape).

Test run on post-fix code:

```
tests/integration/test_agent_id_derivation.py::test_tc_agentid_01_partial_without_agent_id_derives PASSED
tests/integration/test_agent_id_derivation.py::test_tc_agentid_02_explicit_agent_id_wins PASSED
tests/integration/test_agent_id_derivation.py::test_tc_agentid_03_malformed_explicit_agent_id_rejected PASSED
tests/integration/test_agent_id_derivation.py::test_tc_agentid_04_starter_helper_keeps_explicit_id PASSED

4 passed in 0.07s
```

---

## 2. Test catalog updated

`process/test-catalog.md` gained a new sub-section under Part 2
(Asset Management):

> ### Agent ID Derivation (AGENTID) — bug-agent-id-derive-from-ref.md

with full Setup / Action / Expected / Pre-fix bite / Verifies
entries for TC-AGENTID-01..04, matching the FRM-NOCAP entry
style.

---

## 3. Pre-fix verification — TC-AGENTID-01

Captured by spinning up a worktree at the pre-Step-2 SHA
(`e7e1b35`), copying the new test file in, and running
TC-AGENTID-01 against unmodified `src/assets/asset_manager.py`.

```
$ git worktree add /tmp/kolsch-prefix HEAD
Preparing worktree (detached HEAD e7e1b35)

$ cp tests/integration/test_agent_id_derivation.py \
     /tmp/kolsch-prefix/tests/integration/

$ cd /tmp/kolsch-prefix
$ uv sync --frozen --group test
$ uv run --frozen pytest \
    tests/integration/test_agent_id_derivation.py::test_tc_agentid_01_partial_without_agent_id_derives \
    -v --no-cov
```

Output:

```
errors = [ValidationError(
    layer=1,
    source='kernel',
    path='/agent_id',
    code='missing',
    message='Field required',
)]

    def _raise(errors: list[ValidationError]) -> None:
        payload = [e.model_dump(mode="json") for e in errors]
>       raise ConfigInvalid(validation_errors=payload, where=_WHERE_PIPELINE)
E       errors.kernel.ConfigInvalid: Configuration validation failed with 1 error(s)

src/kernel/pipeline.py:43: ConfigInvalid
FAILED tests/integration/test_agent_id_derivation.py::test_tc_agentid_01_partial_without_agent_id_derives
1 failed in 0.25s
```

This matches Step 1 §1's prediction exactly:

- The PARTIAL accepts the missing `agent_id` (no Layer-1 partial
  finding).
- After RFC 7386 merge, the merged dict has no `agent_id` key.
- `validate_resolved` runs `ResolvedAgentConfig.model_validate`
  on the merged dict; Pydantic flags `/agent_id` as `code:
  missing`, `message: "Field required"`.
- Pipeline aggregates the finding and raises `ConfigInvalid` at
  `src/kernel/pipeline.py:43`.

Worktree cleaned up post-verification: `git worktree remove
/tmp/kolsch-prefix --force`.

---

## 4. Manual end-to-end — three scenarios

Workspace: `~/Documents/kolsch` (existing user workspace).
Target agent: `andre/butler` at
`~/Documents/kolsch/bundles/andre/agents/butler.yaml`.
Provider config: `opencode/qwen3.6-plus`.

Backup: `cp ~/Documents/kolsch/bundles/andre/agents/butler.yaml
/tmp/butler.yaml.backup` taken before any edits. Restored at end
of scenario sequence; current butler.yaml is byte-equal to the
backup.

### Probe pattern

Each scenario uses the in-process probe pattern Step 2's
reviewer notes recommended — directly invokes
`AssetManagement.build_resolved_config` against the real
workspace, accepts the `tools.standard` subsystem variant via a
no-op validator:

```python
import asyncio
from pathlib import Path
from assets import AssetManagement
from errors import ConfigInvalid
from kernel import ValidationResult

def _accept(_body): return ValidationResult(ok=True)

async def main():
    am = AssetManagement(root=Path.home() / "Documents" / "kolsch")
    await am.initialize()
    cfg = await am.build_resolved_config(
        "andre/butler",
        subsystem_validators={"tools": {"standard": _accept}},
    )
    print(f"agent_id={cfg.agent_id!r}")

asyncio.run(main())
```

### Baseline (workspace as-found, explicit `ag_butler`)

```
PROBE: agent_id='ag_butler'  name='butler'  model=opencode/qwen3.6-plus
```

### Scenario A — `agent_id` line removed

butler.yaml edited to remove the `agent_id: ag_butler` line
between `assembly_version` and `name`.

```
SCENARIO_A (omitted): agent_id='ag_andre_butler'
  PASS: derived to ag_andre_butler
```

No `ConfigInvalid`. Resolved config carries the derived
identity. The user's reported friction is gone for new agents.

### Scenario B — explicit `agent_id: ag_butler` (file restored)

butler.yaml restored from `/tmp/butler.yaml.backup`. Same probe:

```
SCENARIO_B (explicit ag_butler): agent_id='ag_butler'
  PASS: explicit value honored
```

Confirms the prepend-as-partial design preserves the "explicit
wins" property — the agent partial overrides the prepended
derived baseline via the existing RFC 7386 merge.

### Scenario C — malformed `agent_id: butler` (no prefix)

butler.yaml edited so line 3 reads `agent_id: butler`.

```
SCENARIO_C (malformed): ConfigInvalid raised with 1 finding(s)
  - layer=1 path=/agent_id code='value_error'
    message: Value error, AgentId requires prefix 'ag_'; got 'butler'
```

The exact pre-fix error the user originally reported is still
raised when the user supplies a malformed explicit value. The
fix did NOT become a leniency layer — users who type
`agent_id: butler` are still corrected.

Per the bug doc's known CLI-surfacing gap (Part 0 #3 / Part 2
out-of-scope):

> The user sees "Configuration validation failed with 1
> error(s)" — no per-field message.

In the probe, the per-field message IS visible because we read
`exc.detail["validation_errors"]` directly. The CLI's failure to
surface this is a separate ticket; this fix narrows the surface
where users hit the bad error.

### File restored

After scenario C, butler.yaml was restored from backup. The file
now matches the original state byte-for-byte (`agent_id:
ag_butler` on line 3, no other changes).

### What was NOT verified manually

The Step 3 dev prompt also asks for:

> The agent runs at least one turn against the configured
> provider.

I did NOT exercise this. Reasons:

1. The configured provider is `opencode/qwen3.6-plus` — running
   it consumes user credit and requires their API key to be
   accessible in-process.
2. The bug doc's load-bearing acceptance criterion is "no
   ConfigInvalid raised + resolved config carries the derived
   id" (Part 2 #1, #2). The probe verifies both.
3. The downstream code path that consumes `agent_id` (engine
   pre-flight, authz principal, event actor, session/thread
   binding) treats it as an opaque str — same shape before and
   after the fix, no new behavior to verify at runtime.

If a live LLM-turn verification is needed for the PR, the
coordinator can run:

```
uv run --project /Users/andrecohen/Desktop/SV/Projects/kolsch \
  kolsch chat andre/butler
```

…after dropping the `agent_id` line from butler.yaml and confirm
no ConfigInvalid surfaces at startup. Re-add the line afterward.

---

## 5. Follow-up tickets to file

Per Step 3 dev-prompt task 7, the following follow-ups were
identified during Steps 1 and 2 but explicitly held out of
scope for this bug. Each deserves its own `process/` doc:

### 5.1 CLI surfacing of `ConfigInvalid.detail.validation_errors`

**Symptom.** When a config validation error fires (any layer,
any field), the CLI prints only:

```
Configuration validation failed with 1 error(s)
```

The actual per-field message sits in
`exc.detail["validation_errors"][i]["message"]` — never reaches
the user. This bug exposes the gap, but every config-invalid
path hits it: a typo in `model.parameters`, an unknown
subsystem variant, the malformed `agent_id` case in scenario C
above.

**Where.** `_cmd_run` (and likely `_cmd_chat`) in `src/cli/`.
The handler catches `ConfigInvalid` and prints the top-level
message; should iterate `exc.detail["validation_errors"]` and
print each finding's `path` + `message`.

**Why deferred.** Out of scope per bug-agent-id-derive-from-
ref.md Part 2 / "Explicitly out of scope" and Part 7. The
derive-from-ref fix narrows the surface where users hit the bad
CLI error but doesn't improve the error itself.

**Recommended file:** `process/qol-cli-config-error-surface.md`.

### 5.2 Full "authoring an agent" how-to

**Gap.** The derivation rule is now documented in
`docs/reference/schemas.md` §A.3 (Step 2 doc update), but a
step-by-step how-to that walks a new user through creating a
bundle + an agent YAML doesn't exist. The closest existing
docs (`docs/tutorials/first-agent.md`,
`docs/how-to/agent-templating.md`) assume the starter helper
is already installed.

**Why deferred.** Out of scope per bug-agent-id-derive-from-
ref.md Part 2 / "Explicitly out of scope" — explicitly named as
"a bigger doc effort and lives in its own ticket."

**Recommended file:** `process/doc-how-to-author-an-agent.md`.

### 5.3 Derived-id collision detection (Step 1 §5 escalation)

**Defect.** `f"ag_{bundle}_{name}"` is not collision-free across
distinct refs. Concrete counter-example: bundle `foo_bar` +
agent `baz` and bundle `foo` + agent `bar_baz` both derive
`ag_foo_bar_baz`. Catalog enforces unique refs, not unique
derived ids. The two agents would silently share authz
principals, event actor ids, and memory bindings.

**Why deferred.** The Step 2 dev prompt did not include the
collision check, and the coordinator did not explicitly resolve
Step 1's A/B/C/D pick before authorizing Step 2. The fix as
shipped works correctly for all collision-free workspaces
(which is almost everyone today), but a deliberately or
accidentally colliding setup ships a silent correctness defect.

**Recommended fix (Step 1 §5 option A).** Add a Pass-5 to
`assets/catalog.py:scan` that computes `derive_agent_id` for
every agent entry and raises a new
`AssetsDuplicateDerivedAgentId(refs: list[str])` error when any
two collide. ~20 LOC + new error class + one test.

**Recommended file:** `process/bug-derived-agent-id-collision-
detection.md` (or fold as a follow-up step into the existing
bug doc).

### 5.4 Prepend-leniency for malformed explicit `agent_id` —
NOT recommended

Per Step 1 §3 and Step 2 reviewer prompt check 7, accepting
`agent_id: butler` (no prefix) and silently prepending `ag_`
was considered and explicitly rejected. The fix is "let users
omit." This is documented for completeness; no follow-up doc
needed unless someone changes their mind.

---

## 6. Acceptance check vs. bug doc Part 2

| Criterion | Status |
|---|---|
| 1. `agent_id` optional in agent YAML | ✓ — partial schema already optional, prepend pattern in `asset_manager.py:173` fills in derived default |
| 2. Resolver derives canonical `agent_id` when omitted | ✓ — `derive_agent_id(bundle, name) -> AgentId` in `schemas/identifiers.py`; TC-AGENTID-01 + Scenario A |
| 3. Explicit `agent_id` honored over derivation | ✓ — TC-AGENTID-02 + Scenario B (explicit `ag_butler` wins via RFC 7386 merge) |
| 4. Malformed explicit `agent_id` still fails | ✓ — TC-AGENTID-03 + Scenario C (existing AgentId prefix error still raised) |
| 5. `ResolvedAgentConfig` still has required, non-None `agent_id: AgentId` | ✓ — `schemas/config.py:164` unchanged |
| 6. No migration for `kolsch/helper.yaml` | ✓ — TC-AGENTID-04 + `git diff src/cli/_starter_bundle/` empty |
| 7. Authoring docs surface the new shape | ✓ — `docs/reference/schemas.md` §A.2 + §A.3 + `docs/reference/module-api-reference.md` §13.3 |
| 8. Regression tests cover all paths | ✓ — TC-AGENTID-01..04 |
| 9. No regressions in `tc_eng_*` goldens or existing tests | ✓ — full suite 904 passed, 1 pre-existing skip, 4 deselected (Step 2 verification) |

---

## 7. Open escalation — Step 1 §5 (still pending coordinator decision)

Re-raised here for visibility: **Step 1 §5 has not been formally
resolved.** Step 2 shipped without collision detection because the
prompt didn't include it; the coordinator either (a) accepted-and-
documented (option D), (b) is planning to address it as a separate
follow-up, or (c) will request it as a Step 4 here.

If the answer is (a) — accept-and-document — the resolver's
derivation rule documentation should be amended to call out the
collision risk explicitly, and the bug doc should record the
decision in Part 5 (Coordinator Escalation Log).

If the answer is (b)/(c) — file as follow-up — §5.3 above
provides the scope and entry point.

**This is a coordinator-level decision; flagging in this writeup
so it doesn't get lost.**

---

## 8. Step 3 → bug-close handoff

If the coordinator's response to §7 is "accept-and-document, close
the bug," the work is complete:

- Step 2's branch carries: code + helper + asset_manager hook +
  docs + 4 unit tests.
- Step 3's branch carries: integration test file + catalog entry +
  this writeup.

If the response is "file the collision check as a follow-up,"
ship as-is and open §5.3.

If the response is "land the collision check before close," that's
a Step 4 on top of Step 2's main-line scope (~20 LOC + one error
class + one assets test).

Either way, the user-facing bug (`agent_id: butler` failing with
an opaque message; the surprise that the field is required at all)
is fixed and verified end-to-end on the user's actual workspace.
