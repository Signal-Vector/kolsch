# Phase 2.0 Build Plan — Developer, Reviewer & Coordinator Prompts

**Draft 0.1**

> Two-step build plan for Phase 2.0 (Builtin Tools, Workspace-Only
> Permissions, Live Rule Edits). Both steps follow the same
> developer-reviewer loop and standing instructions as the Phase 1
> build plans before it. Treat these as Step 21 and Step 22 of the
> overall build sequence — they run after Phase 1.3 is complete and
> the patch document
> (`process/phase2.0-tools-specs.md`) has been read.
>
> New in this plan: a **Coordinator Escalation** section near the
> end. Either agent (developer or reviewer) raises an escalation
> when something unexpected happens — a spec gap, a Phase 1
> regression that's not obviously a bug, an architectural choice
> the spec doesn't cover. The coordinator records the decision in
> the escalation log so the rest of the work has a stable
> reference.

---

## How these steps relate to the main build plan

- **Step 21 (Revision pass)** is mechanical, like the LLM-R / UX-R /
  FB-R passes before it. One branch, one PR. Apply TOOL-R1 through
  TOOL-R13 to the affected spec files. No runtime code is written.
- **Step 22 (Implementation)** is real engineering. Ships the four
  tool files, extends the Authorization Protocol with `prompt`,
  adds `kolsch permissions` subcommands, wires the in-chat
  approval callback (without yet shipping the chat UI — that's
  Phase 2.1). Adds TC-TOOL-01..15. One branch, one PR.

Step 21 must complete before Step 22 begins. The reviewer for
Step 22 verifies against the *revised* specs, not the patch
document itself.

---

## Step 21 — Apply TOOL-R1..R13 revisions (mechanical)

- **Builds:** spec files reflect the patch; one new how-to created.
- **Depends on:** Phase 1.3 complete and merged.
- **Source of truth:** `process/phase2.0-tools-specs.md`.

### Developer prompt

```
[standing instructions]

Step 21 — Apply Phase 2.0 revisions TOOL-R1 through TOOL-R13. This
is a mechanical revision pass, same pattern as the LLM-R / UX-R /
FB-R passes before it. No runtime code is written.

Read first:
- process/phase2.0-tools-specs.md (the full patch — every
  revision flag references a section here).
- process/phase1.3-fallback-build.md Step 19 (precedent for
  how a recent revision pass was carried out).

Apply each revision flag exactly as specified in Part 6 of the
patch:

- TOOL-R1: add §X "Builtin Tools" to docs/explanation/tools-
  subsystem.md per patch Part 1.
- TOOL-R2: update path-resolution discussion in docs/explanation/
  file-handling.md per the patch.
- TOOL-R3: add §X "The `prompt` effect" to docs/explanation/
  secrets-sandbox-authz-contract.md per patch Part 2; update the
  Authz Protocol signature.
- TOOL-R4: add `prompt` effect value and rule-id field to
  docs/explanation/asset-management-contract.md schema.
- TOOL-R5: add §4.16 "Tools & Permissions" to docs/explanation/
  error-model.md with the nine codes from patch Part 5.
- TOOL-R6: add `kolsch permissions {list, add, remove, show}` to
  docs/explanation/v1-scope.md §2.4.
- TOOL-R7: add Part 11.8 "Permissions" to docs/reference/module-
  api-reference.md per patch §2, §4.
- TOOL-R8: add success criteria §10.15 "Tools & Permissions" to
  process/development-testing-handbook.md per the patch.
  Include the §10.15-also note about existing tests not relying
  on production defaults being permissive.
- TOOL-R9: add Part 9.8 "Tools & Permissions (TOOL)" to docs/
  process/test-catalog.md with TC-TOOL-01..15. Cases per the
  patch's success criteria and Part 4 surfaces.
- TOOL-R10: add the TWO hard rules to AGENTS.md per the patch.
- TOOL-R11: minor updates to README.md and docs/tutorials/
  first-agent.md per the patch.
- TOOL-R12: ALIGNMENT FLAG. process/phase1.2-usability-
  specs.md §2.4 references bundles/starter/. Add a one-paragraph
  note at the END of that section (do not modify the existing
  body) explaining Phase 2.0 + 2.3 land on bundles/kolsch/ and
  that 2.3's drift handles the migration. If phase1.2-usability-
  specs.md has already been implemented in main, propagate the
  note; do NOT mass-rename Phase 1.2 implementation files here.
- TOOL-R13: CREATE docs/how-to/permissions.md as a new how-to
  doc covering the recipes named in the patch.

Each revision is mechanical. Do NOT take initiative beyond what
the flag specifies — if something feels under-specified, raise
an escalation rather than guessing.

ESCALATION PATH: if you encounter anything not covered by the
spec OR any of the existing Phase 1.x patches conflicts with what
TOOL-R* asks you to write, append a structured entry to the
"Coordinator Escalation Log" section at the END of this build
doc. Use the template in §"How to escalate" below. Then STOP and
wait for the coordinator's resolution before continuing the
flagged revision.

Commit per revision or in two logical groups (existing-doc
revisions in one commit, new-doc creation in another), whichever
the patch reviewer prefers. Conventional Commits scoped to docs()
(or build() if pyproject changes).

Acceptance: every TOOL-R1..R13 applied or escalated; no other
file changed; the patch document's "Apply in two passes —
1. Revisions" instruction complete.

Report what changed, per flag, in your summary.
```

### Reviewer prompt

```
[standing instructions]

Review Step 21 — the Phase 2.0 revision pass. This is a mechanical
review.

Read first:
- process/phase2.0-tools-specs.md, in full.
- Any escalation entries in the build doc's coordinator log
  (some may have landed during Step 21; their resolutions affect
  what "faithful" means for the affected flag).

For each revision flag TOOL-R1 through TOOL-R13:
- Confirm the target file was changed (or escalated and resolved
  per the coordinator log).
- Confirm the change matches what the patch's Part 6 specified.
- Confirm no other file was changed outside the flag's scope.

Specific checks worth running:
- The `prompt` effect appears as a third value alongside `allow`
  and `deny` in docs/explanation/secrets-sandbox-authz-contract.md
  (TOOL-R3). The caller-dispatch table from patch §2.2 is present.
- error-model.md (TOOL-R5) lists all nine codes with correct
  levels. `authz.protected_rule` is documented as the protection
  on the default-deny tail.
- AGENTS.md (TOOL-R10) carries BOTH hard rules from the patch.
  The `allow_all_test` discipline is named.
- process/phase1.2-usability-specs.md alignment note
  (TOOL-R12) is appended to §2.4, not editing the body of an
  already-spec'd section.
- docs/how-to/permissions.md (TOOL-R13) is recipe-style, not a
  re-explanation of the architecture.

If any TOOL-R* was escalated and the coordinator resolved it,
verify the developer carried out the resolution. If the
coordinator hasn't responded yet, fail the review item ONLY for
that flag and pass everything else.

Verdict: PASS or FAIL with specific defects citing flag + file +
patch section, plus any unresolved escalations.
```

---

## Step 22 — Implement the tools, the `prompt` effect, and live rule editing

- **Builds:** the four tool files in `bundles/kolsch/tools/`; the
  Authorization Protocol's `prompt` effect; the chain of caller
  dispatch (chat callback hook, deny-for-non-chat); the four
  `kolsch permissions` subcommands; the in-flight rule reload;
  TC-TOOL-01..15 tests. Also: the minimal `bundles/kolsch/
  bundle.yaml` if it doesn't exist yet, and migration of Phase
  1.2's starter agent to `bundles/kolsch/agents/helper.yaml`
  with `tools:` list updated.
- **Depends on:** Step 21 PASS.
- **Spec refs:**
  - `process/phase2.0-tools-specs.md` Parts 1–5
  - `docs/explanation/tools-subsystem.md` (per TOOL-R1)
  - `docs/explanation/secrets-sandbox-authz-contract.md` (per
    TOOL-R3)
  - `docs/reference/module-api-reference.md` Part 11.8 (TOOL-R7)
  - `process/development-testing-handbook.md` §10.15
    (TOOL-R8)
  - `process/test-catalog.md` Part 9.8 (TOOL-R9)

### Developer prompt

```
[standing instructions]

Step 22 — Implement Phase 2.0: the four builtin tools, the
`prompt` authorization effect, the workspace-only default rules,
and live rule editing.

Read first:
- process/phase2.0-tools-specs.md (the patch — Parts 1–5).
- docs/explanation/tools-subsystem.md (the Tools Protocol you're
  using to register the four builtins).
- docs/explanation/secrets-sandbox-authz-contract.md (the Authz
  Protocol you're extending).
- docs/reference/module-api-reference.md Part 11.8 (the API
  surface you implement).
- process/test-catalog.md Part 9.8 (TC-TOOL-01..15 — write
  these tests first, TDD).
- src/tools/, src/providers/authz/ (existing code you're
  extending).

Deliver:

bundles/kolsch/
├── bundle.yaml                  # name: kolsch; version: 0.2 if Phase 1.2 was 0.1
├── agents/
│   └── helper.yaml              # migrated from bundles/starter/agents/helper.yaml
│                                # gains tools: [read_file, write_file, list_files, bash]
└── tools/
    ├── read_file.py             # per patch §1.1
    ├── write_file.py            # per patch §1.2
    ├── list_files.py            # per patch §1.3
    └── bash.py                  # per patch §1.4

src/providers/authz/
├── rule_based.py                # extended to accept `prompt` effect, rule IDs,
│                                # and an injectable prompt callback
└── allow_all_test.py            # NEW: test-only variant; refuses to load in
                                  # production builds (per TOOL-R10)

src/cli/
└── permissions.py               # NEW: kolsch permissions {list, add, remove, show}

src/cli/main.py                  # extended to wire the `permissions` subparser
                                  # AND to provide the prompt-callback that 2.0
                                  # plumbs through to authz (the callback is
                                  # `deny` for `kolsch run` and the server; chat
                                  # provides a real callback in Phase 2.1).

src/cli/_setup.py                # Phase 1.2's setup, updated to write the
                                  # workspace-only default rules per patch §3.1.

tests/component/tools/
├── test_read_file.py            # TC-TOOL-01..04
├── test_write_file.py           # TC-TOOL-05..07
├── test_list_files.py           # TC-TOOL-08..09
├── test_bash.py                 # TC-TOOL-10..11

tests/component/authz/
├── test_prompt_effect.py        # TC-TOOL-12 (prompt → deny non-chat)
└── test_live_edit.py            # TC-TOOL-13..15

Implementation requirements:

1. The four tool files. Each is a single @tool-decorated function
   per patch §1.1–1.4. Type annotations strict. Docstrings include
   :raises sections naming the canonical error codes from patch
   Part 5.
   - read_file: enforces max_bytes BEFORE opening (stat first).
   - write_file: parents creation honors create_parents; the temp+
     rename atomicity pattern from Phase 1.1.
   - list_files: glob via pathlib.Path.glob; recursion via `**/*`
     when explicit; cap entries.
   - bash: uses the existing Sandbox provider (`native`); captures
     stdout/stderr to bytes then decodes; truncates to 64 KiB.

2. The `prompt` effect in Authz.
   - Add AuthzEffect.PROMPT to the enum.
   - rule_based.py accepts `prompt` in rule effects.
   - The Authz Protocol gains an optional callback parameter on
     the check method, defaulting to None. None means "treat
     prompt as deny." A callback receives (rule_id, tool, target)
     and returns AuthzEffect.{ALLOW, DENY} for the resolution.
   - The Tools dispatcher uses the callback path when invoked
     from chat; uses None elsewhere.

3. Rule IDs.
   - Assigned deterministically at rule-load time:
     rl_<sha256(who+what+effect)[:8]>.
   - The default-deny tail's id is rl_default_deny (fixed string)
     and refuses removal (TOOL-R10 hard rule). Protect via a
     constant in rule_based.py.

4. Live edit semantics.
   - `kolsch permissions add` writes platform.yaml atomically;
     re-loads the in-memory rule list if a runtime is currently
     bootstrapped in the same process.
   - The in-chat callback path (for Phase 2.1's chat) writes the
     same way: callback returns a tuple (effect_for_this_call,
     persist_rule | None). If persist_rule is provided, the rule
     is appended in-memory AND written to disk before this call
     resolves. The next call in the same exchange sees the new
     rule via re-evaluation.

5. Setup integration (TOOL-R12).
   - src/cli/_setup.py's Step B (workspace skeleton) writes
     authorization.config.rules per patch §3.1 with the resolved
     workspace path substituted.
   - The starter agent at bundles/kolsch/agents/helper.yaml gets a
     `tools:` field listing the four builtins.
   - Phase 1.2's bundles/starter/ → bundles/kolsch/ migration:
     setup detects an existing bundles/starter/ and prompts the
     user to migrate (or leave it; both work, but bundles/kolsch/
     becomes the canonical going forward).

6. The `allow_all_test` variant.
   - Lives in src/providers/authz/allow_all_test.py.
   - Its register() call gated by os.environ.get("KOLSCH_TEST_
     BUILD") == "1".
   - Production builds run the test suite via that env var; users
     never have it set.

Tests TC-TOOL-01..15 first. Per patch + revised test-catalog
Part 9.8. Specifically:

- TC-TOOL-01: read_file inside workspace → success.
- TC-TOOL-02: read_file outside workspace → file.access_denied.
- TC-TOOL-03: read_file too large → file.too_large.
- TC-TOOL-04: read_file not found → file.not_found.
- TC-TOOL-05: write_file inside workspace, prompt → modal callback
  invoked; on allow, file written.
- TC-TOOL-06: write_file with no callback (kolsch run) →
  authz.prompt_unattended emitted, then authz.denied.
- TC-TOOL-07: write_file atomic — interrupt between temp and
  rename leaves old file intact.
- TC-TOOL-08: list_files non-recursive default.
- TC-TOOL-09: list_files with `**/*` recursive, hits max_entries.
- TC-TOOL-10: bash success → exit_code, stdout, stderr captured.
- TC-TOOL-11: bash timeout → tool.timeout, partial output preserved.
- TC-TOOL-12: prompt effect → callback returns ALLOW; tool runs;
  no rule written. Callback returns ALLOW with persist_rule;
  rule written; next call hits the new rule.
- TC-TOOL-13: kolsch permissions list → outputs rules in order
  with stable IDs.
- TC-TOOL-14: kolsch permissions remove rl_default_deny →
  authz.protected_rule fatal.
- TC-TOOL-15: live edit during a running runtime — call A blocked
  by prompt, callback persists rule, call B in the same exchange
  hits the new rule and is allowed.

Critical behaviors:
- The four tools must work AGAINST the kolsch bundle directly. No
  hidden builtin registry in the engine. Acceptance verified by
  TOOL-R10's hard rule.
- The `prompt` effect must not surface to non-chat callers as a
  hang. Tests TC-TOOL-06 and TC-TOOL-12 both verify this.
- platform.yaml writes are ATOMIC (temp + fsync + rename). Tested
  by TC-TOOL-07 for tool writes; the same pattern applies to
  permissions writes.
- No code outside src/providers/authz/ or src/cli/permissions.py
  knows about rule IDs or the rule list shape. Phase 1's
  Authorization Protocol surface stays clean.

ESCALATION PATH: if implementation reveals a Phase 1 regression,
a spec gap, or a design conflict with an earlier patch, append a
structured entry to the "Coordinator Escalation Log" at the END
of this build doc and STOP. Use the template in §"How to escalate"
below. Do NOT mass-rewrite Phase 1 code under a 2.0 label.

Acceptance: TC-TOOL-01..15 pass; success criteria §10.15 met; the
four tools register and call cleanly; `kolsch permissions` works
end-to-end; coverage for src/providers/authz/ and src/cli/
permissions.py ≥85%.

Commit: feat(tools,authz,cli): builtin tools + workspace-only
defaults + live permissions.

Report:
- What you built (file inventory).
- TC-TOOL-* pass/fail.
- Any escalations raised and their resolutions.
- Open questions surfaced (especially around the in-chat callback
  shape since Phase 2.1 will consume it).
```

### Reviewer prompt

```
[standing instructions]

Review Step 22 — the Phase 2.0 implementation. The revisions from
Step 21 are in main; review against the revised specs.

Read first:
- process/phase2.0-tools-specs.md (architecture, the four
  tools, the prompt-effect contract, the rule semantics).
- docs/explanation/tools-subsystem.md and secrets-sandbox-authz-
  contract.md (post-revision contracts).
- process/development-testing-handbook.md §10.15.
- process/test-catalog.md Part 9.8.
- The escalation log at the end of this build doc.

The architectural review questions:

1. THE TOOL FILES ARE FILES, NOT REGISTERED CLASSES IN THE
   ENGINE. grep for "read_file", "write_file", "list_files",
   "bash" across src/. The function definitions should appear
   ONLY in bundles/kolsch/tools/, not in src/engine/ or src/
   tools/. Registration happens via the existing Tools subsystem
   catalog, which discovers bundle tools. If you find a "builtin
   registry" inside src/, fail — TOOL-R10 hard rule violated.

2. THE PROMPT EFFECT IS A THIRD ENUM VALUE. Read
   src/providers/authz/rule_based.py. Confirm:
     (a) AuthzEffect has ALLOW, DENY, PROMPT as the three values.
     (b) The check method accepts an optional callback parameter.
     (c) When callback is None and effect is PROMPT, the return
         is DENY (with an event emitted).
     (d) The callback receives (rule_id, tool, target) and returns
         (effect, persist_rule_or_None).

3. RULE IDS ARE DETERMINISTIC. Re-load platform.yaml twice and
   verify rule IDs are identical. Add/remove a rule and verify
   unaffected rule IDs don't change. If IDs are sequential
   counters (which would change on every load), fail.

4. THE DEFAULT-DENY TAIL IS PROTECTED. Try `kolsch permissions
   remove rl_default_deny` — must fail with authz.protected_rule.
   Try editing platform.yaml directly to remove it — runtime
   bootstrap should refuse to load. Verify.

5. NO PROMPT-HANDLING UI IN AUTHZ. Read rule_based.py. There should
   be no print(), no input(), no terminal rendering. Authz returns
   classifications; the caller renders. Phase 2.1's chat will
   provide the actual modal.

The functional review questions:

6. Run TC-TOOL-01..15. All pass?

7. TC-TOOL-07 (atomic write) specifically: use a chaos test or
   manual SIGKILL between temp write and rename; confirm the old
   file is intact. If the test simulates this via mock, verify
   the mock matches the production atomic-write path.

8. TC-TOOL-15 (live edit in same exchange): this is the headline
   feature. Run it manually with a debugger; confirm the rule
   write propagates BEFORE the next authz check runs, not at
   exchange boundary.

9. Phase 1 + 1.1 + 1.2 component tests — do they still pass?
   If they don't, was it because they relied on allow-all
   defaults? The TOOL-R8 success criterion is "no test relies on
   production defaults being permissive" — failing tests should
   have been updated to declare their own rule set or use
   allow_all_test. Verify.

10. The `allow_all_test` variant — confirm it refuses to load
    without KOLSCH_TEST_BUILD=1. Test:
      KOLSCH_TEST_BUILD=  uv run kolsch run … (variant rejected)
      KOLSCH_TEST_BUILD=1 uv run kolsch run … (variant loads)

11. The setup-produced platform.yaml. After `kolsch setup`, open
    platform.yaml and confirm:
      - The five default rules from patch §3.1 are present in
        order with the resolved workspace path.
      - rl_default_deny is the last rule.
      - No allow-all rule snuck through.

12. The helper agent at bundles/kolsch/agents/helper.yaml now has
    `tools: [read_file, write_file, list_files, bash]`. Verify;
    test that the helper agent can actually call these tools
    end-to-end (a mock or fake LLM call with a forced tool-use
    response).

The "modularity is obvious" review:

13. Open docs/how-to/permissions.md (created by TOOL-R13). Read
    it as if you're a new user. Could you tighten the default
    rules to "no bash at all"? Could you grant `bash` for a
    specific pattern? If the recipes are missing a step, fail
    and request additions.

14. Open bundles/kolsch/tools/read_file.py. Read it as if you're
    a new contributor wanting to add a fifth tool (e.g.,
    `grep`). Could you do it by copying the file, editing the
    function, and putting it in bundles/kolsch/tools/grep.py?
    If the file references anything not available to a
    user-authored tool, fail.

15. Escalation log review. Read every escalation entry from
    Step 21 and Step 22. For each, confirm:
      - The coordinator's resolution is documented.
      - The implementation matches the resolution.
    Unresolved escalations are a fail.

Verdict: PASS or FAIL with specific defects citing file + spec
section, plus any unresolved escalations.
```

---

## How to escalate (developer + reviewer)

Either agent escalates by appending an entry to the
"Coordinator Escalation Log" at the bottom of this doc using
the following template. After appending, STOP work on the
affected item and await coordinator resolution. Other unaffected
work can continue.

```markdown
### Escalation E-2.0-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Phase / Step:** 2.0 / Step 21 or 22
- **Affected revision flag / spec section:** e.g. TOOL-R6, or patch §2.2
- **Trigger:** what made you stop. Concrete, with file/line citations if applicable.
- **Why this is not a guess:** what you tried, what the spec said,
  why the spec doesn't cover the case.
- **Options as you see them:** 2–3 concrete paths forward, with
  trade-offs. NOT a recommendation.
- **Estimated cost of each option:** rough effort (lines of code,
  test churn, blast radius).

---

### Coordinator resolution — E-2.0-<NNN>

- **Decided by:** human
- **Decision:** which option, or a fourth path.
- **Rationale (one sentence):** why this option.
- **Affects:** which other patches/phases (if any) need to know.
- **Resume instruction:** what the developer / reviewer does next.
```

When to escalate:
- Spec ambiguity that can't be resolved by reading the cited spec
  + the related Phase 1 contract.
- A Phase 1 / 1.1 / 1.2 / 1.3 regression you'd cause by following
  the spec literally.
- An architectural call the spec doesn't cover (e.g., "the test
  fixture API for the prompt callback wasn't named").
- A user-visible behavior change the spec implies but doesn't
  state (e.g., "TOOL-R12 says align with Phase 1.2 but Phase 1.2
  isn't implemented yet — what does setup write?").

When NOT to escalate:
- You disagree with a design decision recorded in the patch.
  Implement faithfully; if you have feedback, raise it AFTER
  the implementation is in main.
- A spec citation contradicts something on the internet. The
  spec is the contract.

---

## Coordinator Escalation Log

### Escalation E-2.0-001 — bundle template location for the four Phase 2.0 tool files

- **Raised by:** developer
- **Phase / Step:** 2.0 / Step 22
- **Affected revision flag / spec section:** patch §1.5 ("Bundle layout"), TOOL-R10 hard rule, build-plan Step 22 deliverable layout
- **Trigger:** the build plan's deliverable layout lists `bundles/kolsch/tools/read_file.py` (etc.) as a repo-relative path, but the existing platform architecture is:
  - Phase 1's bundles directory is *inside the workspace* (`<workspace>/bundles/`, not `<repo>/bundles/`). `bundles/` does not exist at the repo root; nothing in `src/` references it. `src/cli/_setup.py:227-262` scaffolds `<workspace>/bundles/` at setup time. The asset catalog (`src/assets/`) discovers bundles from the resolved workspace, not from the repo tree.
  - Phase 1's builtin tools (`read`, `write`, `edit`, `bash`) are registered at runtime by `src/tools/builtins.py` as `@tool`-decorated factories. The four Phase 2.0 builtins (`read_file`, `write_file`, `list_files`, `bash`) need a way to reach the running platform. Two paths exist; the patch picks neither explicitly.
- **Why this is not a guess:** the patch §1.5 ("Ship as `@tool`-decorated Python files in `bundles/kolsch/tools/`") describes the *user-visible layout* the workspace ends up with. The patch §0 says "Phase 1's Tools subsystem already discovers tool files in bundles; we add four files." But the existing Tools subsystem registers builtins via `make_builtin()` in `src/tools/builtins.py`; bundle-supplied tools are *discovered* via the asset catalog when a workspace exists. The patch doesn't say whether the four files should:
  - (a) Ship as a packaged template under `src/` that setup *copies* into `<workspace>/bundles/kolsch/tools/` at first-run (matches the patch §1.5 phrasing literally; respects TOOL-R10's "users can read them" intent because the user finds them on disk after setup; gives Phase 2.3 a real migration target), OR
  - (b) Register as builtin factories in `src/tools/builtins.py` and be presented as workspace-relative files via a synthetic stub the user can read (closer to how Phase 1's `read/write/edit/bash` work today; lighter touch on setup; harder for the user to "swap" them per TOOL-R10's intent).
- **Options as you see them:**
  - **Option A — Packaged template + copy at setup.** Add `src/cli/_starter_bundle/kolsch/tools/*.py` as packaged data; setup step D copies them into `<workspace>/bundles/kolsch/tools/`. The asset catalog discovers them at runtime. Trade-off: setup tests need to verify the copy; the .py files are duplicated in the repo (template) and the workspace (copy). The user *can* edit the copy.
  - **Option B — Register as builtins (current Phase 1 pattern).** Add four new factories to `src/tools/builtins.py`. Setup writes only the bundle.yaml + agent.yaml; no .py files in the workspace. Trade-off: the user can't "read" the tool implementation by opening a file in their workspace; TOOL-R10's "users can read them" intent is only partially served (they can read `src/tools/builtins.py` in the repo). The patch's `bundles/kolsch/tools/` layout becomes aspirational only.
  - **Option C — Hybrid.** Register as builtins AND ship the .py files in the workspace as documentation/templates. Most ergonomic; most code; the "are these the actual tools?" question becomes ambiguous (the workspace files would be inert if the runtime is using the builtin factories).
- **Estimated cost of each option:**
  - A: ~1 day. Template files (~100 LOC each × 4 = 400 LOC), setup step extension (~50 LOC), 2-3 setup tests.
  - B: ~0.5 day. Four factories in builtins.py (~80 LOC each × 4 = 320 LOC), zero setup changes.
  - C: ~1.5 days. A + B + clarity comments about which path is authoritative.

This blocks the bundle layout decision in Step 22. The authz extensions, error codes, and tool factories themselves are independent of the layout question and proceed unblocked; the layout question lands in the follow-up commit that wires setup + CLI permissions.

---

### Coordinator resolution — E-2.0-001

- **Decided by:** human (coordinator), via the Step 22 review verdict
- **Decision:** **Option A** — packaged template copied to the workspace at setup.
- **Rationale (one sentence):** Option A satisfies TOOL-R10's "users can read them, copy them as templates, or swap them" intent *literally* — the files land at `<workspace>/bundles/kolsch/tools/` where users find them; Phase 2.3's bundle drift detection has a real migration target.
- **Affects:** Phase 2.0 follow-up implementation (the CLI permissions subcommand + setup integration + bundle materialization land together); Phase 2.3 (drift detection knows to migrate `bundles/starter/` → `bundles/kolsch/`).
- **Resume instruction:** follow-up PR ships: (1) `src/cli/_starter_bundle/kolsch/` packaged template (`bundle.yaml` + `agents/helper.yaml` + four `tools/*.py` files), (2) setup step D materialization that copies it into the workspace, (3) `_step_b_skeleton` writes the five workspace-only default rules per patch §3.1, (4) helper agent declares `tools: [read_file, write_file, list_files, bash]`, (5) `kolsch permissions {list, add, remove, show}` CLI subcommand wiring through the already-landed `add_rule`/`remove_rule`/`get_rule`/`list_rules` runtime API. Re-run Q1, Q4 (CLI path), Q11, Q12, Q13, Q14 from the Step 22 reviewer rubric against the materialized state.



---

## Notes on running these two steps

- **Step 21 first, in its own PR, merged before Step 22 starts.**
- **The `prompt` effect contract (patch §2.2) is the load-bearing
  abstraction.** Phase 2.1's chat will plug into the callback
  hook. If Step 22 implements the callback shape sloppily, 2.1
  inherits the mess. The reviewer's Q2 is the gate.
- **Manual end-to-end test before declaring 2.0 done.** Fresh
  workspace via `kolsch setup`; run the helper agent (still no
  chat UI — use `kolsch run` with a fake LLM rigged to emit a
  `tool_use: bash` request); confirm the unattended `kolsch run`
  gets `authz.prompt_unattended` → `authz.denied` and the
  exchange ends cleanly.
- **Drift in the in-chat modal shape.** Phase 2.0 ships the
  callback hook but no UI. The reviewer should NOT block 2.0 on
  "the modal text isn't pretty" — that's 2.1's surface. They
  should block 2.0 on "the callback's return shape can't carry
  what the modal needs to write back" — that's structural.

---

## What this build plan does NOT include

- **The `kolsch chat` REPL.** That's Phase 2.1. Phase 2.0 ships
  the callback hook only.
- **Time-limited rules** (patch Part 7 item 2).
- **Per-agent rule scopes** (patch Part 7 item 1).
- **Pattern suggestions / mining** (patch Part 7 item 3).
- **Docker sandbox** (patch Part 7 item 5).
- **Real Windows `bash` support** (patch Part 8 item 4) — Windows
  users use WSL or get a "not supported on Windows" message.
