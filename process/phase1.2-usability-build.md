# Phase 1.2 Build Plan — Developer & Reviewer Agent Prompts

**Draft 0.1**

> Two-step build plan for Phase 1.2 (Workspace Resolution &
> First-Run Setup patch). Both steps follow the same
> developer-reviewer loop and standing instructions as the main
> Phase 1 build plan (`phase1-build-plan.md`) and the Phase 1.1
> build plan (`phase1.1-llm-provider.md`). Treat these as Step 17
> and Step 18 of the overall build sequence — they run after Phase
> 1.1 is complete and the patch document
> (`process/phase1.2-usability-specs.md`) has been read.

---

## How these steps relate to the main build plan

- **Step 17 (Revision pass)** is mechanical, like the LLM-R pass
  before it. One branch, one PR. Apply UX-R1 through UX-R13 to the
  affected spec files. No runtime code is written; no tests are
  added. The patch document is the source of truth.
- **Step 18 (Implementation)** is real engineering. Adds
  `src/platform_runtime/workspace.py` (the resolver), the `setup`
  subcommand in the CLI dispatcher, the friendly missing-workspace
  message, the `KOLSCH_WORKSPACE` env-var read, and the
  `platformdirs` dependency. Adds TC-UX-01..10 tests. One branch,
  one PR.

Step 17 must complete before Step 18 begins. The reviewer for Step
18 verifies against the *revised* specs, not the patch document
itself — so the revisions must be in `main` first or the reviewer is
working against a stale baseline.

---

## Step 17 — Apply UX-R1..R13 revisions (mechanical)

- **Builds:** the spec files reflect the patch. Existing docs
  updated per UX-R1..R12; one new doc created per UX-R13
  (`docs/how-to/workspace-and-setup.md`).
- **Depends on:** Phase 1.1 complete and merged.
- **Source of truth:** `process/phase1.2-usability-specs.md`
  (the patch — read it first; every revision flag references a
  section).

### Developer prompt

```
[standing instructions]

Step 17 — Apply Phase 1.2 revisions UX-R1 through UX-R13. This is a
mechanical revision pass, same pattern as the LLM-R pass and the
audit-revisions and modular-storage-revisions passes before it. No
runtime code is written; no tests are added.

Read first:
- process/phase1.2-usability-specs.md (the full patch — every
  revision flag references a section here).
- process/phase1.1-llm-provider.md Step 15 (precedent for how
  a UX-style revision pass is carried out).

Apply each revision flag exactly as specified in Part 4 of the
patch:

- UX-R1: add platformdirs >= 4.2 to pyproject.toml runtime
  dependencies.
- UX-R2: change src/cli/main.py --root default from Path.cwd() to
  None; update the help text per the patch. Do NOT add the resolver
  call yet — that's Step 18 implementation work; this is only the
  spec-aligned default change.
- UX-R3: add `kolsch setup` to docs/explanation/v1-scope.md §2.4
  CLI surface.
- UX-R4: update docs/explanation/v1-scope.md §2.5 deployment text
  per the patch.
- UX-R5: add §1.4 "Workspace Resolution" to
  docs/explanation/asset-management-contract.md per the patch.
- UX-R6: add §4.14 "Workspace" to docs/explanation/error-model.md
  Part 4 with the four codes from patch Part 3.
- UX-R7: add Part 11.6 "Workspace Resolution" to
  docs/reference/module-api-reference.md. Update the dependency
  matrix (Part 12) to show CLI and API depend on the workspace
  module.
- UX-R8: add success criteria §10.13 to process/
  development-testing-handbook.md per the patch.
- UX-R9: add Part 9.6 "Workspace Resolution & Setup (UX)" to
  process/test-catalog.md with TC-UX-01..10 per the patch.
  Update the coverage map.
- UX-R10: add the hard rule to AGENTS.md per the patch.
- UX-R11: update README.md install/quickstart to the three-line
  flow in the patch (install, setup, first run).
- UX-R12: update docs/tutorials/first-agent.md — add the "0. First
  run" preface and reframe Step 1 per the patch. Steps 2–9 stay
  word-for-word.
- UX-R13: CREATE docs/how-to/workspace-and-setup.md as a new
  how-to-type doc covering: switching workspaces, moving a
  workspace, backing up, resetting (per patch Part 4 UX-R13).

Each revision is mechanical. Where a flag says "add" or "insert",
add. Where it says "update" or "change", change the existing
content. Do NOT take initiative beyond what the flag specifies — if
something feels under-specified, surface it in your summary rather
than guessing.

UX-R2 is a tiny code change (CLI default value), but it is a SPEC-
ALIGNMENT change with no behavioral wiring — argparse will pass
None through to the existing handler. Step 18 wires the resolver
into the handler. Do not implement the resolver here.

Commit per revision (UX-R1, UX-R2, ...) or in two logical groups
(docs-only revisions in one commit, pyproject + CLI default in
another), whichever the patch reviewer prefers. Conventional
Commits scoped to docs() (or build() for pyproject, refactor(cli)
for the default change).

Acceptance: every UX-R1..R13 applied; no other file changed; the
patch document's "Apply in two passes — 1. Revisions" instruction
is complete.

Report what changed, per flag, in your summary.
```

### Reviewer prompt

```
[standing instructions]

Review Step 17 — the Phase 1.2 revision pass. This is a mechanical
review, not an architectural one. The architectural decisions live
in the patch document and were made by the human. Your job is to
confirm the mechanical application is faithful.

Read first:
- process/phase1.2-usability-specs.md, in full.

For each revision flag UX-R1 through UX-R13:
- Confirm the target file was changed.
- Confirm the change matches what the patch's Part 4 specified for
  that flag.
- Confirm no other file was changed outside the flag's scope (e.g.,
  UX-R6 should touch only docs/explanation/error-model.md — not
  the test catalog).
- Confirm the new doc UX-R13 was created and contains the content
  the patch describes.

Specific checks worth running:
- platformdirs dependency landed in the runtime deps section, not
  dev/test (UX-R1).
- src/cli/main.py --root default is None and help text matches the
  patch (UX-R2). No further code in main.py changed.
- The error catalog (UX-R6) has all four codes from patch Part 3
  with correct levels.
- TC-UX-01 through TC-UX-10 exist in the test catalog with their
  full Setup/Action/Expected/Verifies form (UX-R9).
- README.md (UX-R11) reads as the three-line flow with no
  vestigial pre-setup boilerplate.
- docs/tutorials/first-agent.md (UX-R12) gained the "0. First run"
  preface and the Step 1 reframe; steps 2–9 are byte-identical to
  the pre-revision version (use git diff to confirm).

Do NOT let style preferences justify a fail — this is mechanical
faithfulness, not editorial improvement. If the developer added
content not specified in a flag, flag it as out-of-scope. If a flag
was missed entirely, fail and name which one.

Verdict: PASS or FAIL with specific defects citing flag + file +
patch section.
```

---

## Step 18 — Implement workspace resolver + `kolsch setup`

- **Builds:** the `src/platform_runtime/workspace.py` module with
  `resolve_workspace` and `workspace_is_initialized`. Wires the
  resolver into the CLI dispatcher and the API bootstrap. Adds the
  `setup` subcommand with the six-step flow (A–F per patch Part
  2.2) including `--non-interactive`. Adds the friendly
  missing-workspace message (exit code 3). Adds TC-UX-01..10.
- **Depends on:** Step 17 PASS (revisions in main).
- **Spec refs:**
  - `process/phase1.2-usability-specs.md` Parts 1–3 (the
    architecture, the setup flow, the failure modes).
  - `docs/reference/module-api-reference.md` Part 11.6 (the API
    surface).
  - `process/development-testing-handbook.md` §10.13 (success
    criteria).
  - `process/test-catalog.md` Part 9.6 (TC-UX-01..10).
  - `docs/explanation/asset-management-contract.md` §1.4 (the
    resolution contract).

### Developer prompt

```
[standing instructions]

Step 18 — Implement Phase 1.2: the workspace resolver and the
`kolsch setup` command. The revisions from Step 17 are in main; the
revised specs are your contract.

Read first:
- process/phase1.2-usability-specs.md (the patch; especially
  Parts 1, 2, 3).
- docs/reference/module-api-reference.md Part 11.6 (the API surface
  you implement).
- docs/explanation/asset-management-contract.md §1.4 (the
  resolution contract).
- process/test-catalog.md Part 9.6 (TC-UX-01..10 — write these
  tests first, TDD).
- src/cli/main.py (existing CLI dispatcher you're extending).
- src/api/__init__.py (existing API bootstrap you're extending).

Deliver:

src/platform_runtime/
├── workspace.py          # resolve_workspace, workspace_is_initialized,
│                         # KOLSCH_ENV_VAR, DEFAULT_DIR_NAME constants
└── (existing files unchanged in shape)

src/cli/
├── main.py               # dispatcher wired to resolve_workspace;
│                         # friendly missing-workspace message;
│                         # `setup` subparser
└── _setup.py             # NEW: the six-step setup flow (A–F).
                           # Each step a small async function; the
                           # top-level orchestrator calls them in
                           # order. Keep step logic LOCAL to this
                           # file — do not push helpers into the
                           # runtime.

src/api/__init__.py       # build_app reads resolve_workspace() if
                           # the caller didn't pin a root.

tests/component/
└── test_setup.py         # TC-UX-01..10 (or split across
                           # test_workspace.py + test_setup.py if
                           # the file gets long).

Implementation requirements:

1. workspace.py: exactly the two functions in patch §1.1, plus the
   two module constants. ~30 lines including docstrings. No
   PlatformRuntime import — this module is pure. typing strict.

2. CLI dispatcher (src/cli/main.py): immediately after argparse
   parses args, call:
       resolved_root = resolve_workspace(args.root)
       if args.command != "setup" and not workspace_is_initialized(resolved_root):
           print(<friendly message per patch §1.4>, file=sys.stderr)
           return 3
       args.root = resolved_root
   Subcommand handlers continue to use args.root unchanged.

3. setup command (src/cli/_setup.py): six steps per patch §2.2.
   Each step takes the resolved root and the args namespace; each
   returns a status (proceeded / skipped / aborted). The
   orchestrator prints the banner, walks the steps, prints the
   final ready-banner.
   - Step C (LLM provider) MUST call the existing `kolsch connect`
     handler — do not re-implement connect. Hand it the chosen
     provider name + the resolved root.
   - Step D (starter agent) materializes the YAML files in patch
     §2.4 verbatim, with the chosen model substituted into the
     `model:` field. The default model is the first alias in
     priority order (opus > smart > the user's chosen alias) or
     `provider: fake, model: fake-model` if no provider connected.
   - Step E (test run) calls the existing `kolsch run` handler
     against the starter agent. Capture and pretty-print the result.

4. --non-interactive mode: per patch §2.5. Reads KOLSCH_PROVIDER
   and KOLSCH_PROVIDER_KEY from env for the auto-connect path. No
   prompts. Exit 0 on full success, 4 on partial.

5. API bootstrap (src/api/__init__.py): build_app, when not
   given a root, calls resolve_workspace(None). If the workspace is
   uninitialized, raise PlatformError(workspace.not_initialized) at
   startup (the API has no friendly-print layer; the operator sees
   the typed error).

Tests TC-UX-01..10 first. Write each test before its implementation.
Per process/test-catalog.md Part 9.6:
- TC-UX-01: resolve_workspace(--root=/x) returns /x (expanded,
  resolved).
- TC-UX-02: resolve_workspace() with KOLSCH_WORKSPACE=/y returns /y.
- TC-UX-03: resolve_workspace() with neither set returns
  platformdirs.user_documents_dir() / "kolsch". Use monkeypatch on
  platformdirs to make the test deterministic.
- TC-UX-04: CLI main() against an uninitialized path prints the
  exact friendly message (assertion on stderr) and returns 3, with
  NO Python traceback in stderr.
- TC-UX-05: kolsch setup --non-interactive --root <tmp> produces a
  workspace passing workspace_is_initialized(). Exit code 0.
- TC-UX-06: idempotency — run setup twice; assert no file mtime
  changes between the runs (or assert file contents byte-identical;
  whichever is easier).
- TC-UX-07: env-var auto-connect — set KOLSCH_PROVIDER=openrouter,
  KOLSCH_PROVIDER_KEY=sk-test, run setup --non-interactive against
  a mocked HTTP layer; assert llm_providers.openrouter exists in
  the resulting platform.yaml and .env contains OPENROUTER_API_KEY.
- TC-UX-08: starter agent file is at
  bundles/starter/agents/helper.yaml with model field reflecting
  the choice; falls back to "{provider: fake, model: fake-model}"
  when no provider connected.
- TC-UX-09: resumability — abort setup after step C (mock the
  prompt to raise KeyboardInterrupt). Re-run setup; confirm step C
  is detected as completed and step D runs fresh.
- TC-UX-10: end-to-end — run setup --non-interactive, then run
  `kolsch run starter/helper --input content=hello` against
  the produced workspace; assert exit code 0 and an agent_message
  on stdout.

Network in tests: use a fixture HTTP layer (respx or whichever the
project pinned in Phase 1.1) to mock provider responses for TC-UX-07.
NO real network. NO real API keys in fixtures.

Critical behaviors:
- The friendly missing-workspace message is plain text on stderr —
  no JSON, no formatting tokens. Exact string per patch §1.4 (or as
  close as practical; the TC-UX-04 assertion is exact).
- setup writes nothing outside the resolved root, except the
  provider's API key into the workspace's .env (per Phase 1.1).
- setup is interruptible at every prompt with no corrupted state.
- The Phase 1 fake LLM remains the fallback for the starter agent
  when no provider is connected — TC-UX-10 must pass without any
  provider.
- The dev container's devcontainer.json should set
  KOLSCH_WORKSPACE=/workspace (or similar) so the in-container
  experience uses live-mounted source. Add this to
  .devcontainer/devcontainer.json if not already present —
  flagged in patch Part 6 open item 2.

If during implementation you discover the CLI dispatcher's pre-
bootstrap workspace check breaks an existing subcommand (e.g.,
`server` should still boot even without a workspace, listening so
the operator can call /setup endpoints later — though no such
endpoints exist in v1), surface this as a finding. Do NOT silently
change the gate.

Acceptance: TC-UX-01..10 pass; success criteria §10.13 met; the
friendly missing-workspace message is the only thing users see on
an uninitialized workspace; `kolsch setup` is the only command that
creates a workspace; coverage for src/platform_runtime/workspace.py
and src/cli/_setup.py is ≥85% (per handbook §11.1).

Commit: feat(workspace,cli): implement Phase 1.2 workspace
resolution + setup command. Report what you built, the devcontainer
adjustment (if made), any open questions surfaced during
implementation (env-var name bikesheds, prompt text choices, etc.).
```

### Reviewer prompt

```
[standing instructions]

Review Step 18 — the Phase 1.2 implementation. The revisions from
Step 17 are in main; review against the revised specs, not against
the patch document.

Read first:
- process/phase1.2-usability-specs.md (architecture, flow,
  failure modes — to know what *should* be there)
- docs/reference/module-api-reference.md Part 11.6 (workspace API)
- docs/explanation/asset-management-contract.md §1.4 (resolution
  contract)
- process/development-testing-handbook.md §10.13 (success
  criteria)
- process/test-catalog.md Part 9.6 (TC-UX-01..10)

The architectural review questions:

1. THE SINGLE-RESOLVER PROPERTY. grep the entire src/ tree for
   imports of `platformdirs` and reads of `KOLSCH_WORKSPACE`. Both
   should appear in EXACTLY ONE file: src/platform_runtime/
   workspace.py. Any other location is a violation — fail and name
   the file.

2. The resolver function (~30 lines per spec). Confirm it actually
   is ~30 lines of pure logic with no hidden orchestration. No
   filesystem creation, no I/O beyond the env-var read. The
   "is this path initialized?" check is a separate function, also
   pure.

3. The CLI dispatcher's missing-workspace gate. Read src/cli/main.py.
   Confirm:
     (a) The gate fires BEFORE any handler runs (i.e., before any
         async dispatch).
     (b) The gate exempts the `setup` subcommand (otherwise setup
         can never bootstrap a missing workspace — chicken/egg).
     (c) The gate's stderr message contains the path being checked
         AND the env-var name as actionable guidance.
     (d) Exit code is exactly 3, distinct from 1 (generic error)
         and 2 (engine Fatal).

4. The `setup` command lives in src/cli/_setup.py (or wherever the
   developer put it) and the SIX STEPS are individual functions or
   clearly-delineated blocks. If the steps are stuffed into one
   400-line function, that's a maintainability fail.

5. Setup CALLS the existing connect handler — does NOT
   reimplement it. Read step C's code. If you see argparse,
   AuthPromptSpec walking, or registry lookups other than the
   one-line "call kolsch connect", that's a violation.

6. Setup CALLS the existing run handler for step E — does NOT
   reimplement engine invocation.

7. The starter agent YAML matches patch §2.4 exactly. Diff the
   produced file against the spec.

The functional review questions:

8. Run TC-UX-01..10. All pass? Each one actually asserts the
   behavior?

9. TC-UX-04 specifically: capture stderr from a real CLI invocation
   (subprocess.run, not in-process) against a fresh empty
   directory. Assert NO Python traceback strings present
   ("Traceback", "  File ", "    ^^^^"). The user must see ONLY the
   friendly message.

10. TC-UX-06 specifically: idempotency. Capture file stats / hashes
    before and after the second run. ZERO files should differ.

11. TC-UX-10 end-to-end: actually run the produced workspace, not
    just check files exist. The starter agent must reply.

12. Run `kolsch setup --non-interactive --root <fresh-tmp>` manually
    (you, the reviewer, not just in tests). Read what the user sees.
    Is it clear what happened? Does the final banner give them
    actionable next steps?

13. Run `kolsch setup` (interactive, default location, mocking
    stdin for the prompts). Walk through Steps A–F. Are the
    prompts unambiguous? Does "skip" mean the right thing at each
    step?

14. Coverage: src/platform_runtime/workspace.py and src/cli/_setup.py
    at ≥85% per handbook §11.1. Run coverage and confirm. Investigate
    any uncovered branches in the abort/resume paths.

15. The devcontainer adjustment (if made by the developer). Read
    .devcontainer/devcontainer.json. Is KOLSCH_WORKSPACE set to a
    sensible in-container path? Does the dev-container README or
    AGENTS.md mention it?

The "ergonomics is obvious" review:

16. Open the produced workspace's platform.yaml. Is it readable —
    sane defaults, clear comments — or is it a 200-line config dump?
    If a user opens it post-setup and recoils, the setup defaults
    are wrong.

17. The friendly missing-workspace message. Show it to someone
    unfamiliar with Kolsch. Could they figure out the next step
    without reading docs? If not, the message text needs another
    pass.

Verdict: PASS or FAIL with specific defects citing file + spec
section. This is the usability patch landing; the bar is "a new
user runs `kolsch setup` and gets a working agent reply within 2
minutes."
```

---

## Notes on running these two steps

- **Step 17 first, in its own PR, merged before Step 18 starts.**
  The reviewer of Step 18 reads the revised specs as the contract.
  If the revisions aren't merged, the reviewer is reviewing against
  the patch document, which is the wrong layer.
- **The single-resolver property in Step 18's reviewer prompt is
  the critical one.** Question 1 is the test of whether the patch's
  central design goal was met. If `platformdirs` or
  `KOLSCH_WORKSPACE` is read from anywhere else in the codebase,
  future changes to workspace resolution become a multi-file edit
  instead of a single-file edit.
- **Manual end-to-end test before declaring Phase 1.2 done.** After
  Step 18 passes, on a clean machine (or fresh container): install
  kolsch, run `kolsch setup` with a real OpenRouter (or OpenCode)
  key, send a real prompt to the starter agent. Confirm the time
  from `install` to "first real LLM reply" is under 3 minutes. This
  isn't in the build plan because it involves real credentials and
  real cost — but it's the final confirmation.
- **Network fixtures, not real network, for TC-UX-07.** The setup
  command's auto-connect path goes through `kolsch connect`, which
  already has fixture HTTP wiring from Phase 1.1. Reuse that
  fixture layer; do not stand up a separate one.

---

## What this build plan does NOT include

- **Workspace migration tooling.** Out of scope per patch Part 5
  item 2. Future patch when needed.
- **A `kolsch workspaces` subcommand for managing multiple
  workspaces.** Out of scope per patch Part 5 item 1. Multi-
  workspace ergonomics are a Phase 3 hosted concern.
- **Telemetry opt-in.** Out of scope per patch Part 5 item 5; no
  telemetry exists.
- **A prompt gallery for the starter agent.** Out of scope per
  patch Part 6 open item 3. The starter prompt is intentionally
  generic; users edit the YAML if they want a different default.
- **Tool/skill scaffolding in setup.** Out of scope per patch Part
  5 item 4. Setup creates agents; tools and skills come later.
