# Phase 2.4 — Skills subsystem build plan

**Draft 0.1** *(three sub-phases, dependency-ordered. Each step ships as its own PR with focused dev + reviewer prompts. Smaller in total scope than Phase 2.1 because Phase 1 already shipped the subsystem mechanics — this phase wraps user-facing surface around them and lights up the first content.)*

> The skills subsystem mechanics are already in place from Phase 1
> (`src/skills/` — 443 LOC, 8 invariants Sk-I1..I8, engine
> force-invoke at pre-flight step 9, 7 component tests). What's
> missing: a user-facing CLI for the skill lifecycle, a path for
> users to trigger force-invoke, validation that the Phase 1
> contracts still hold under Phase 2.0 (tools authz) and Phase 2.2
> (`extends:` composition), and any actual content for users to
> install or write against.
>
> This doc covers Phase 2.4 end-to-end through 2.4.1. Phase 2.4.2
> (skill-creating skill) is sketched at the bottom and deferred —
> it depends on Phase 3.x workflow patterns.

---

## Part 0 — Why + decisions locked

### What's wrong today

- **No user-facing path to force-invoke.** The engine wires it at
  `src/engine/engine.py:467, 916-927` and accepts a trigger of
  shape `{"kind": "user_message_with_force_skill", "force_skills": [...]}`.
  Neither `kolsch chat` nor `kolsch run` surfaces a way to
  produce that trigger. Voluntary skill loading (the model
  `read`s SKILL.md on its own) works; explicit user-driven
  force-invoke does not.
- **No canonical skills.** `bundles/kolsch/` ships only
  `bundle.yaml` + `tools/` + `agents/`. No `skills/` directory.
  The mechanism is wired end-to-end but nothing exercises it.
- **No CLI for the skill lifecycle.** Users cannot list, show,
  install, validate, scaffold, or remove skills without
  hand-editing files.
- **Phase 1 contracts may have drifted.** `skills-subsystem.md`
  is dated "Draft 0.1." Phase 2.0 added tools authz (skills load
  via `tool.read`, so authz rules now gate skill bodies); Phase
  2.2 added `extends:` (does it correctly compose
  `subsystems.skills.config.enabled`?). Neither is reflected in
  the contract doc.
- **`allowed-tools` is parsed but unenforced (Sk-I3).** Decided
  to remain unenforced in v1; documented but worth re-confirming
  the trajectory before authoring canonical skills that depend
  on the behavior either way.

### Decisions locked (from conversation prior to drafting)

1. **Install via URL.** `kolsch skills install <url>` accepts a
   URL pointing at a directory whose root contains `SKILL.md`.
   **Strict shape**: multi-skill repos require sub-paths in the
   URL (`github.com/org/agent-skills/tree/main/skills/test-writer`).
   No `--all`, no auto-discovery of multi-skill repos in v1.
2. **Install destination.** `--bundle <name>` installs into that
   bundle (fails if bundle doesn't exist). Omitted = auto-create
   a single-skill bundle named after the URL's last meaningful
   segment, with collision discriminator. Skills always live
   inside a bundle; no workspace-level `skills/` directory.
3. **Composition under `extends:`.** Replace-only via RFC 7386
   (existing kernel resolver behavior — verified at
   `src/kernel/resolver.py:20-42`). Child omitting
   `subsystems.skills.config.enabled` inherits parent's list;
   child specifying replaces. Same rule for
   `subsystems.tools.config.declared`. **No additive `extend:`
   syntax.** Decision is uniform across tools and skills.
4. **`allowed-tools` stays informational in v1** (Sk-I3
   preserved). Enforcement, if ever, is a future flip with its
   own ticket.
5. **2.4.2 deferred.** Skill-creating skill depends on workflow
   patterns from Phase 3.x. Sketched here for context; built
   later.

---

## Part 1 — Sub-phase decomposition

### 2.4.0 — Audit, hardening, and force-invoke surface

The shipped mechanics catch up with two phases of downstream
work, and the engine's force-invoke hook gets a user-facing path.

- **Step 1**: Contract audit. Re-verify Sk-I1..I8 against Phase
  2.0 + 2.2 reality. Update `docs/explanation/skills-subsystem.md`
  to whatever version the contracts are actually at.
  STOP gate.
- **Step 2**: Missing-tests pass. Add tests for the gaps the
  audit surfaces — minimum: `extends:` composition of
  `subsystems.skills.config.enabled` is replace-only; authz
  denial on SKILL.md path produces a sensible error;
  `disable-model-invocation` filters listing but allows
  force-invoke.
- **Step 3**: Force-invoke surface. `/skill <ref>` slash command
  in `kolsch chat` + `--skill <ref>` repeatable flag on `kolsch
  run`. Both plumb into the engine's existing
  `user_message_with_force_skill` trigger shape.

### 2.4.1 — Lifecycle CLI + first canonical skills

The seven verbs the user needs, plus the first content that
exercises them.

- **Step 4**: Read-only inspection — `kolsch skills list`,
  `kolsch skills show <ref>`, `kolsch skills which <agent_ref>`.
- **Step 5**: Scaffold — `kolsch skills new <name> [--bundle <b>]`
  generates a SKILL.md skeleton + dir.
- **Step 6**: Validate — `kolsch skills validate [--all | <ref>]`
  checks frontmatter shape, body present, script references
  resolve. Hooks into `kolsch setup` re-run via the existing
  `_migrations/` framework.
- **Step 7**: Install — `kolsch skills install <url> [--bundle <name>]`.
  Git clone or tarball download per URL shape; strict-root SKILL.md
  validation; auto-create bundle when `--bundle` omitted.
- **Step 8**: Remove — `kolsch skills remove <ref>`. Cleanup
  pass surfaces agents enabled-on the missing ref before delete.
- **Step 9**: First canonical skills shipped in
  `bundles/kolsch/skills/`. Proposed set in Step 9's prompt.

### 2.4.2 — Skill-creating skill (deferred)

A meta-skill that guides a user through authoring a new skill
via conversation. Depends on workflow patterns from Phase 3.x;
not built under this phase.

Sketched in Part 5 of this doc only.

---

## Part 2 — Step dependency graph

```
2.4.0 — Audit, hardening, force-invoke surface
─────────────────────────────────────────────
Step 1 (contract audit)  ←── STOP gate
   │
   ▼
Step 2 (missing-tests pass)
   │
   ▼
Step 3 (force-invoke user surface)
   │
   ▼
2.4.1 — Lifecycle CLI + first canonical skills
──────────────────────────────────────────────
Step 4 (list / show / which — read-only)
   │
   ▼
Step 5 (new — scaffold)
   │
   ▼
Step 6 (validate + migrations hook)
   │
   ▼
Step 7 (install — URL → bundle)
   │
   ▼
Step 8 (remove + cleanup)
   │
   ▼
Step 9 (first canonical skills)
```

| Step | Ships | Reviewable in |
|---|---|---|
| **1** | Audit writeup + refreshed `skills-subsystem.md` | ~30 min |
| **2** | New tests for gaps the audit surfaced | ~25 min |
| **3** | `/skill` slash + `--skill` flag wired to force-invoke trigger | ~30 min |
| **4** | Three read-only CLI verbs | ~25 min |
| **5** | `skills new` + SKILL.md template | ~20 min |
| **6** | `skills validate` + setup-migration hook | ~25 min |
| **7** | `skills install` (URL + bundle resolution) | ~40 min |
| **8** | `skills remove` + dependent-agent surfacing | ~20 min |
| **9** | 3-5 canonical skills + TC entries | ~30 min |

Each step ships its own PR. Step 1 is the gate; Steps 2–9 are
mechanical once the audit pins what's true.

---

## Step 1 — Contract audit + `skills-subsystem.md` refresh

- **Builds:** an audit writeup re-verifying Sk-I1..I8 against
  Phase 2.0 + 2.2 reality + a refreshed
  `docs/explanation/skills-subsystem.md`.
- **No code changes.**
- **Acceptance:** every invariant has a current-code citation;
  any drift is documented; the refreshed doc supersedes Draft 0.1.

### Developer prompt

```
[standing instructions]

Step 1 — Contract audit + skills-subsystem.md refresh.

Read first:
- /Users/andrecohen/Desktop/SV/Projects/kolsch/docs/explanation/skills-subsystem.md (Draft 0.1, 607 lines).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/skills/ (subsystem.py, models.py, validator.py, listing.py).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/assets/skill_metadata.py (frontmatter parser).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/engine/engine.py around lines 467 and 916-927 (force-invoke pre-flight).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/kernel/resolver.py (RFC 7386 merge_patch).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase2.0-tools-specs.md (authz model).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/phase2.2-agent-envelope-specs.md (extends: composition).

Audit tasks (one per invariant + cross-phase impact):

1. Sk-I1 — Progressive disclosure. Confirm only descriptions
   enter the system prompt; bodies load via tool.read on demand.
   Cite: format_skills_listing in src/skills/listing.py +
   engine's prompt assembly.

2. Sk-I2 — disable-model-invocation enforcement. Confirm
   listing filtering happens but force-invoke still routes
   through. Cite: src/skills/subsystem.py:get_enabled_skills
   docstring (already names this) + format_skills_listing.

3. Sk-I3 — allowed-tools is informational. Confirm no code
   path enforces it. Cite the parsing in
   src/skills/subsystem.py:_make_enabled_skill (around line 153)
   + grep for any allowed_tools enforcement (should return
   zero hits in src/, modulo the parser itself).

4. Sk-I4 — Force invoke as tool.read. Confirm the engine
   synthesizes a tool.called + tool.result pair for
   SkillInvocation.skill_md_path. Cite: engine.py:_process_force_invoke
   around line 916-927.

5. Sk-I5 — Skills emit NO events. Confirm SkillsSubsystem
   methods do not call _emit / append to the event log. Cite:
   src/skills/subsystem.py (no event imports).

6. Sk-I6 — Enablement is explicit. Confirm a non-enabled ref
   raises SkillForcedNotEnabled (Fatal) on force-invoke. Cite:
   subsystem.py:force_invoke around line 86.

7. Sk-I7 — Skill discovery is read-only. Confirm the catalog
   scan does not write back to the bundle directory. Cite:
   src/assets/catalog.py + skill_metadata.py.

8. Sk-I8 — One skill registration per ref. Confirm the catalog
   uniqueness invariant covers skill refs the same way it
   covers other asset types.

CROSS-PHASE IMPACT — call out and document:

9. Phase 2.0 (tools authz) ⇒ skills.
   - Force-invoke synthesizes tool.read. That tool.read goes
     through the standard authz path (action="tool.read",
     target=SKILL.md path).
   - Question: does the default-deny / workspace-only authz
     ruleset allow reading SKILL.md from a bundle directory
     under bundles/<b>/skills/<name>/?
   - If yes — document the path. If no — document the gap; it
     becomes its own follow-up ticket (and may block Step 3's
     manual e2e).
   - Voluntary read also goes through authz; same question.

10. Phase 2.2 (extends:) ⇒ skills + tools composition.
    - kernel.resolver.merge_patch is RFC 7386. Arrays are
      replaced atomically. So subsystems.skills.config.enabled
      and subsystems.tools.config.declared are both replaced
      when the child specifies them. Confirm by reading +
      pointing at the merge_patch code.
    - Write a small probe (or unit test) that demonstrates: a
      child agent extending a parent with skills: {enabled:
      [{ref: "X"}]} who specifies enabled: [{ref: "Y"}] gets
      only Y in the resolved config. Capture the result in
      the writeup.
    - Note: this is the locked decision. The audit confirms
      reality matches the decision; it does NOT change
      behavior.

DOC REFRESH:

11. Update docs/explanation/skills-subsystem.md:
    - Bump to Draft 0.2 (or later).
    - Add a new section under each invariant noting the
      cross-phase impact discovered in tasks 9-10.
    - Add an explicit "Composition under `extends:`" subsection
      naming the replace-only rule with a citation to
      resolver.py.
    - Add an explicit "Tools authz applies to SKILL.md reads"
      subsection naming the path policy + authz interaction.
    - DO NOT add new invariants. The audit pins existing ones;
      new ones come with new specs.

12. STOP and post the writeup + the doc diff before any code
    or test changes. The writeup IS the deliverable.

ESCALATION PATH: if the audit reveals that:
 (a) one of Sk-I1..I8 is actually broken in current code (not
     just under-documented), or
 (b) Phase 2.0's authz blocks legitimate SKILL.md reads in a
     way that makes force-invoke unusable end-to-end, or
 (c) Phase 2.2's RFC 7386 composition does NOT in fact
     produce replace-only behavior for these fields
     (contradicts the locked decision),
escalate before proceeding.

Acceptance: writeup posted; doc diff against
skills-subsystem.md ready for review; coordinator approves to
proceed.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1 — contract audit + doc refresh.

NO production code to review. The writeup + the
skills-subsystem.md diff are the deliverable.

Checks:

1. Every Sk-I (I1..I8) has a current-code citation. Vague
   "I checked" is a fail.

2. Cross-phase impact tasks 9-10 are both addressed with
   concrete answers, not "TBD" or "not investigated."

3. The Phase 2.2 composition probe is included (either as a
   unit test in the doc or pasted output). If the probe shows
   anything other than replace-only behavior, the locked
   decision needs reopening — escalate.

4. skills-subsystem.md is bumped past Draft 0.1 with explicit
   sections for cross-phase impact. No new invariants added.

5. If the audit surfaced a real broken invariant or an authz
   gap blocking force-invoke, it's escalated rather than
   silently patched in this step.

Verdict: APPROVE-TO-PROCEED or REVISE with specific gaps.
```

---

## Step 2 — Missing-tests pass

- **Builds:** new tests in `tests/component/test_skills.py` (or
  a new file) covering the gaps Step 1 surfaced.
- **Depends on:** Step 1 approved.
- **Acceptance:** new tests pass; full suite green.

### Developer prompt

```
[standing instructions]

Step 2 — Add tests for the gaps Step 1 surfaced.

Read first:
- Step 1's audit writeup.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/tests/component/test_skills.py (existing 7 tests).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/tests/integration/ for the closest extends-composition pattern.

Mandatory test additions (Step 1's audit may have surfaced
more; add those too):

1. TC-SKILL-COMP-01 — extends: replaces enabled list.
   def test_extends_replaces_skills_enabled_list():
       """A child agent's subsystems.skills.config.enabled
       replaces the parent's list atomically per RFC 7386."""
       # Setup: parent agent enables [{ref: "kolsch/skills/A"}];
       # child extends parent and enables [{ref: "kolsch/skills/B"}].
       # Action: build_resolved_config(child_ref).
       # Expected: resolved enabled == [{ref: "kolsch/skills/B"}].

2. TC-SKILL-COMP-02 — extends: child omits, parent's list flows.
   def test_extends_inherits_skills_when_child_omits():
       """A child agent that does not specify
       subsystems.skills.config inherits the parent's enabled
       list verbatim."""

3. TC-SKILL-AUTHZ-01 — tool.read on SKILL.md respects authz.
   def test_skill_md_read_denied_by_authz():
       """When the workspace authz rules deny tool.read for
       a path that resolves into a bundle's skills directory,
       force-invoke surfaces a Diagnostic tool failure (NOT a
       crash)."""
       # If Step 1 surfaced that default authz ALREADY allows
       # SKILL.md reads, this test scripts a custom deny rule
       # to confirm the deny path works. Either way, prove
       # the seam.

4. TC-SKILL-DISABLE-01 — disable-model-invocation filters listing,
   not force-invoke.
   def test_disable_model_invocation_blocks_listing_allows_forceinvoke():
       """A skill with disable_model_invocation: true does
       NOT appear in the system-prompt listing
       (format_skills_listing excludes it) but force_invoke
       still routes through to it."""

5. Any additional tests the Step 1 audit explicitly named.

If a new test file is warranted (the audit covers a path that
isn't really "skills subsystem" — e.g., the authz interaction
might live in tests/integration/test_skills_authz.py), place
it there. Follow existing layout conventions.

Add the TCs to process/test-catalog.md under a new
"Skills (SKILL-*)" section.

Scope: ~100-150 LOC tests + catalog entries.

ESCALATION PATH: if writing TC-SKILL-AUTHZ-01 reveals the seam
between tool.read authz and SKILL.md paths is broken (e.g., the
path policy doesn't cover bundles/ at all), escalate — that's
a Step 3 blocker.

Acceptance: new tests pass; existing test_skills.py still passes;
goldens unchanged.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2 — missing-tests pass.

Checks:

1. All four mandatory TCs (COMP-01, COMP-02, AUTHZ-01,
   DISABLE-01) exist and pass.

2. Any extra TCs Step 1 named are present.

3. The composition tests pin REPLACE-only behavior, NOT
   union or extend. If COMP-01 asserts union, that's the
   wrong semantics — fail.

4. The authz test exercises a REAL deny path, not a mock
   that bypasses the dispatcher. The whole point is to prove
   the seam.

5. process/test-catalog.md gained the SKILL-* entries in full
   Setup/Action/Expected/Verifies format.

6. Full test suite green.

Verdict: PASS or FAIL.
```

---

## Step 3 — Force-invoke user-facing surface

- **Builds:** `/skill <ref>` slash command in `kolsch chat` +
  `--skill <ref>` repeatable flag on `kolsch run`. Both produce
  the engine's `user_message_with_force_skill` trigger shape.
- **Depends on:** Step 2 in main.
- **Acceptance:** the user can invoke a skill from both
  surfaces end-to-end against a real LLM.

### Developer prompt

```
[standing instructions]

Step 3 — Wire force-invoke into chat + run surfaces.

Read first:
- src/cli/_slash.py (the slash-command framework + existing
  commands like /tools, /clear).
- src/cli/chat.py for the ExecutionContext construction (the
  trigger dict lives there).
- src/cli/main.py for cmd_run (the --input handling lives there).
- src/engine/engine.py around lines 467 and 916-927 (force-
  invoke pre-flight).

Tasks:

1. Slash command — /skill <ref>.
   - Register a new slash command in _slash.py following the
     existing pattern (registry + dispatch).
   - When the user types `/skill <ref>` and then sends their
     next message, the next exchange's ExecutionContext.trigger
     is shaped as:
       {
         "kind": "user_message_with_force_skill",
         "content": "<the user message text>",
         "force_skills": ["<ref>"],
       }
     instead of the usual {"kind": "user_message", "content": ...}.
   - Stash the pending skill ref on ChatSessionState (e.g.
     state.pending_force_skill: list[str]). The chat input
     loop reads it when building the next ExecutionContext,
     then clears it.
   - Support multiple /skill calls before the next message —
     state.pending_force_skill accumulates a list; one
     exchange consumes all of them.
   - Tab-completion: enrich the slash completer to suggest
     enabled skill refs (config.subsystems.skills.config.enabled).

2. Run flag — --skill <ref>.
   - Add a repeatable --skill flag to `kolsch run` in
     main.py / argparse setup. Multiple --skill args collect
     into a list.
   - When run constructs the ExecutionContext, if --skill was
     passed it builds the user_message_with_force_skill
     trigger shape with the collected refs. Otherwise the
     existing user_message shape.

3. Validation at submit time.
   - If the user types `/skill <ref>` for a ref that is NOT in
     the agent's enabled list, print a friendly error and
     clear pending_force_skill. The engine would raise
     SkillForcedNotEnabled at pre-flight; surfacing it at the
     /skill command site is the better UX.
   - Same check for --skill on run.

4. NO change to:
   - SkillsSubsystem.force_invoke or the engine's pre-flight
     step 9. The trigger shape is the existing contract.
   - The slash-command framework's general dispatch.

5. Tests:
   - tests/component/cli/test_chat_force_skill.py:
     * /skill enabled-ref then user-message → trigger has
       force_skills.
     * /skill not-enabled-ref → friendly error; trigger stays
       user_message.
     * Two /skill calls before submit → trigger has both refs.
   - tests/component/cli/test_run_force_skill.py:
     * --skill enabled-ref → trigger has force_skills.
     * --skill repeated → trigger has all refs.
     * --skill not-enabled-ref → friendly error before
       execute_stream is called.

6. Document the new surface in docs/how-to/chatting-effectively.md
   (one short subsection) and a fresh
   docs/how-to/forcing-a-skill.md (single-page recipe).

Scope: ~80 LOC code (slash command + run flag) + ~60 LOC tests
+ ~30 LOC docs.

ESCALATION PATH: if Step 2's TC-SKILL-AUTHZ-01 revealed
SKILL.md reads are denied by default authz, Step 3's manual e2e
will fail. Escalate that gap as its own bug; Step 3 can ship
with TC-only coverage and the manual e2e blocked behind the
authz fix.

Acceptance: tests pass; manual run shows /skill kolsch/skills/<ref>
in chat triggers a synthetic tool.read + tool.result for the
SKILL.md path before the user's exchange begins, and the agent
text reflects awareness of the skill's content.
```

### Reviewer prompt

```
[standing instructions]

Review Step 3 — force-invoke user surface.

Checks:

1. /skill <ref> registered as a slash command following the
   existing _slash.py pattern. Dispatcher integration is
   minimal — no parallel dispatch path.

2. ChatSessionState.pending_force_skill (or equivalent) is
   cleared after consumption AND on /clear / /new. Leaked
   state across exchanges is a fail.

3. The trigger shape is exactly
   {"kind": "user_message_with_force_skill", "content": ..., "force_skills": [...]}
   to match engine.py:921-923. Any deviation is a fail.

4. --skill on `kolsch run` accepts multiple via repetition
   (action="append" in argparse).

5. Friendly errors at submit time, NOT at engine pre-flight,
   for unenabled refs. Verify by tracing.

6. Tests cover the four paths: chat happy, chat error, run
   happy, run error.

7. Docs updated: chatting-effectively.md mentions /skill;
   forcing-a-skill.md exists.

8. Manual e2e in PR description: a real chat run with /skill
   shows tool.called and tool.result events for the SKILL.md
   path before the user message lands.

Verdict: PASS or FAIL.
```

---

## Step 4 — Read-only inspection: `list`, `show`, `which`

- **Builds:** `kolsch skills list`, `kolsch skills show <ref>`,
  `kolsch skills which <agent_ref>`.
- **Depends on:** Step 3 in main.
- **Acceptance:** the three commands work against the existing
  catalog; no skill content needed yet.

### Developer prompt

```
[standing instructions]

Step 4 — Add read-only skill CLI verbs.

Read first:
- src/cli/permissions.py as a template — its subparser shape
  (add_permissions_subparser + cmd_permissions dispatcher)
  is the pattern to mirror.
- src/cli/main.py around line 278-283 (where permissions
  subparser is registered).
- src/assets/asset_manager.py (catalog access).
- src/skills/subsystem.py (get_enabled_skills, _enabled_entries).

Tasks:

1. Create src/cli/skills.py with:
   - add_skills_subparser(sub) — registers `skills` subparser
     with three ops: list, show, which.
   - cmd_skills(args) — dispatches to op handlers.

2. `kolsch skills list`
   - Walks runtime.asset_management's catalog; collects entries
     where asset_type == "skill".
   - Prints a table: bundle, ref, name, description (truncated),
     disable-model-invocation flag.
   - --bundle <name>: filter to one bundle.
   - --json: machine-readable.

3. `kolsch skills show <ref>`
   - Looks up the skill in the catalog.
   - Prints frontmatter (name, description, allowed-tools,
     disable-model-invocation, license, compatibility) +
     the SKILL.md body.
   - Errors:
     * ref doesn't resolve → AssetsNotFound surface (use
       existing CLI error wrapper).
     * ref resolves but isn't a skill → friendly error
       naming the actual asset_type.

4. `kolsch skills which <agent_ref>`
   - Resolves the agent config (runtime.build_resolved_config).
   - Reads config.subsystems.skills.config.enabled.
   - For each enabled entry: prints the ref + the resolved
     skill name + a short status indicator (✓ if catalogued,
     ✗ if missing — meaning the agent enables a ref the
     catalog can't resolve).
   - --json: machine-readable.

5. Wire skills_parser into main.py's dispatcher map (same
   pattern as permissions).

6. Tests:
   - tests/component/cli/test_skills_list.py — at least one
     test for each of list / show / which; the no-skills case
     for list should print "no skills" cleanly (NOT crash).

7. NO changes to SkillsSubsystem internals.

Scope: ~150 LOC + ~80 LOC tests.

ESCALATION PATH: if asset_management.get_asset's existing
return shape doesn't surface skill metadata (frontmatter) in a
form that `show` can pretty-print, escalate — the metadata
parser may need a small refactor first.

Acceptance: three commands work end-to-end; tests pass; help
text reads cleanly via `kolsch skills --help` and
`kolsch skills <op> --help`.
```

### Reviewer prompt

```
[standing instructions]

Review Step 4 — read-only skill CLI.

Checks:

1. cli/skills.py mirrors cli/permissions.py's structure
   (add_..._subparser + cmd_... dispatcher). Asymmetric
   structure makes the codebase noisier; fail if it diverges
   without reason.

2. `list` handles empty catalog ("no skills found") cleanly.

3. `show` distinguishes "ref not found" from "ref found but
   wrong type" — different exit codes / messages.

4. `which` correctly displays missing-catalog entries (the
   ✗ case). A skill enabled in YAML but absent from the
   catalog is exactly the case Phase 2.4.1 install/remove
   should help users debug.

5. --json output is parseable (verify by piping through jq
   in the PR description).

6. Tests cover each verb plus the empty / error cases.

7. main.py wiring matches the permissions pattern.

Verdict: PASS or FAIL.
```

---

## Step 5 — Scaffold: `kolsch skills new`

- **Builds:** `kolsch skills new <name> [--bundle <b>] [--into <path>]`
  generating a SKILL.md skeleton + dir.
- **Depends on:** Step 4 in main.
- **Acceptance:** generated skill validates (will be re-checked
  by Step 6's `validate`).

### Developer prompt

```
[standing instructions]

Step 5 — Add `kolsch skills new`.

Read first:
- src/cli/skills.py from Step 4.
- src/assets/skill_metadata.py (frontmatter parser — the
  template must match what this expects).
- docs/explanation/skills-subsystem.md Part 0 + the SKILL.md
  spec section.

Tasks:

1. Add the `new` op to cli/skills.py:
     kolsch skills new <name> [--bundle <bundle-name>] [--description "..."]

   - --bundle: which bundle to write into. If omitted, default
     to "user" (or whichever convention Step 1's audit pinned
     for the user's own bundle). Fail if the bundle doesn't
     exist.
   - --description: optional one-liner; if omitted, use a
     placeholder.

2. The skeleton:
     bundles/<bundle>/skills/<name>/SKILL.md

   ```
   ---
   name: <name>
   description: <description-or-placeholder>
   allowed-tools: []
   disable-model-invocation: false
   ---

   # <name>

   <body placeholder — instructions for the model on when and
   how to use this skill>
   ```

3. Refuse to overwrite an existing file. Print a
   pointer-to-existing message.

4. After write: print the resulting ref
   (<bundle>/skills/<name>) so the user can immediately enable
   it on an agent.

5. Tests:
   - tests/component/cli/test_skills_new.py:
     * Creates a tmp workspace + bundle; runs `skills new`;
       asserts file exists + frontmatter parses + ref is
       resolvable in the catalog after a re-scan.
     * Refuse-overwrite path.
     * Refuse-missing-bundle path.

Scope: ~80 LOC + ~50 LOC tests.

ESCALATION PATH: if the catalog doesn't auto-rescan after a
file write (which would make the "ref is resolvable" assertion
fail), document the workaround (manual rescan or a setup
re-run) but do NOT add automatic filesystem watching — that's
an explicitly bigger scope.

Acceptance: new command works; created skill is well-formed;
tests pass.
```

### Reviewer prompt

```
[standing instructions]

Review Step 5 — scaffold.

Checks:

1. The SKILL.md template matches what skill_metadata.py
   actually parses. Run the parser against a freshly-generated
   skill in the PR description and paste the parsed dict.

2. Refuse-overwrite is implemented (no silent file
   destruction).

3. Missing-bundle failure mode is friendly.

4. The post-write message names the ref the user should
   enable on an agent.

5. Tests cover happy path + both refuse paths.

Verdict: PASS or FAIL.
```

---

## Step 6 — Validate + setup-migration hook

- **Builds:** `kolsch skills validate [--all | <ref>]` checking
  frontmatter shape, body present, script references resolve.
  Hooks into `kolsch setup` re-run through the existing
  `_migrations/` framework.
- **Depends on:** Step 5 in main.
- **Acceptance:** validate catches malformed skills; setup
  re-run surfaces issues with a clear summary.

### Developer prompt

```
[standing instructions]

Step 6 — Validate + migration hook.

Read first:
- src/cli/_migrations/ (the existing migration framework
  Step 2-8 of qol-setup-evolution-and-defaults shipped).
- src/assets/skill_metadata.py (existing frontmatter
  validation surface).
- src/skills/validator.py (Layer-3 validator for the skills
  subsystem config — different from skill-FRONTMATTER
  validation; do not confuse).

Tasks:

1. Add the `validate` op to cli/skills.py:
     kolsch skills validate              # all skills
     kolsch skills validate --all
     kolsch skills validate <ref>

   Checks performed per skill:
   - SKILL.md exists at the catalogued path.
   - Frontmatter parses to a dict.
   - Required keys present: name, description.
   - allowed-tools is a list (informational; just shape-check).
   - disable-model-invocation, if present, is bool.
   - Body (post-frontmatter content) is non-empty.
   - Script references in the body (paths under the skill's
     dir) actually exist. Heuristic: scan body for backticked
     paths beginning with `./` or matching the skill's dir
     prefix; for each, check the file exists.
   - License / compatibility, if present, are strings.

   Output:
   - One summary line per skill: "[OK] <ref>" or
     "[FAIL] <ref>: <reason>".
   - Exit code 0 if all ok; 1 if any failed.
   - --json: structured per-skill result.

2. Migration hook into kolsch setup re-run.
   - Read src/cli/_migrations/__init__.py + the existing
     orchestrator. The shape per artifact is "detect drift,
     offer the user a choice."
   - Add a new validator-only migration: on each setup re-run,
     after the existing bundle drift pass, run skill validation
     across all catalogued skills. Surface a summary section
     in the setup output.
   - Do NOT block setup on validation failures. Print a
     warning summary and continue. The user can run `kolsch
     skills validate` for details.
   - Why warning-not-block: a user's authored skill being
     mid-edit (broken frontmatter while they iterate) should
     not block setup from completing.

3. Tests:
   - tests/component/cli/test_skills_validate.py:
     * Valid skill → OK.
     * Missing description → FAIL with the right code.
     * Body empty → FAIL.
     * Broken script reference → FAIL.
     * --json structure.
   - tests/component/cli/test_setup_skill_warnings.py:
     * Setup re-run with a broken skill prints a warning but
       exits 0.

Scope: ~150 LOC + ~100 LOC tests.

ESCALATION PATH: if the existing _migrations/ framework expects
migrations to PRODUCE diffs to write back to disk (mutating
behavior), and skill validation is purely read-only / advisory,
the integration may need a different hook than "another
migrator." Step 1's audit should have surfaced this; if not,
read the framework and pick the right hook.

Acceptance: validate works standalone; setup re-run surfaces
warnings without blocking.
```

### Reviewer prompt

```
[standing instructions]

Review Step 6 — validate + setup hook.

Checks:

1. validate covers ALL the checks in the dev prompt task 1.
   Missing checks (e.g., "I didn't check script references")
   is a fail.

2. Setup re-run does NOT block on validation failures. A
   user with an in-flight broken SKILL.md must still be able
   to complete setup.

3. The migration-hook integration matches the existing
   _migrations/ orchestrator shape. A parallel hook outside
   the orchestrator is a fail (consistency matters here).

4. --json output is parseable.

5. Tests cover both happy + broken paths for validate AND
   the setup-warning-not-block contract.

Verdict: PASS or FAIL.
```

---

## Step 7 — Install: `kolsch skills install <url>`

- **Builds:** the install command — strict URL, single-skill
  root, `--bundle` or auto-create.
- **Depends on:** Step 6 in main.
- **Acceptance:** install from a GitHub URL works end-to-end;
  the installed skill validates + is catalog-discoverable.

### Developer prompt

```
[standing instructions]

Step 7 — Add `kolsch skills install <url>`.

Read first:
- cli/skills.py (Steps 4-6).
- The locked decisions in process/phase2.4-skills-build.md
  Part 0 + Part 1's 2.4.1 description.
- Any existing HTTP / git client code (likely none beyond
  what build_llm / connectors use; install gets to pick).

Tasks:

1. URL handling.
   - For v1 accept: HTTPS URLs pointing at git repositories
     (resolve via git clone) OR direct tarball URLs (.tar.gz /
     .zip).
   - GitHub URL syntax: support both repo-root
     (https://github.com/owner/repo) and sub-path
     (https://github.com/owner/repo/tree/<branch>/<subdir>).
     The sub-path form translates to: clone the repo at the
     branch, then operate on the subdir.
   - For non-GitHub git hosts (gitlab.com, bitbucket.org,
     self-hosted): accept the same shape if recognisable;
     otherwise require the user to point at an archive URL.
   - Strict-root contract: after fetch + resolution to a
     directory, that directory MUST contain SKILL.md at its
     root. Else fail with a clear "this URL does not point
     at a SKILL.md root" message.

2. Destination resolution.
   - --bundle <name>: install into bundles/<name>/skills/<derived-skill-name>/.
     Bundle MUST exist; fail if not.
   - --bundle omitted: auto-create a single-skill bundle.
     - Bundle name derivation: from URL's last meaningful
       segment, lowercased + sanitised (alnum + - + _ only).
       - github.com/foo/test-writer → "test-writer".
       - github.com/foo/agent-skills/tree/main/skills/test-writer
         → "test-writer" (last segment after the sub-path).
     - Bundle collision: append a numeric discriminator
       ("test-writer", "test-writer-2", ...).
     - Skill name derivation: same algorithm — last meaningful
       URL segment.
   - --bundle-name <name> + auto-create flow: lets the user
     name the auto-created bundle explicitly. Useful for
     scripted installs.

3. Trust prompts.
   - Before write, list the files being installed AND any
     scripts (non-SKILL.md files) under the skill dir. Show
     the SKILL.md's allowed-tools declaration.
   - Prompt: "[y] install / [n] cancel". Skip prompt with
     --yes (non-interactive).
   - Non-chat / non-TTY: refuse to prompt; require --yes.

4. Atomicity.
   - Fetch + extract to a tmp dir.
   - If validation (strict-root SKILL.md + frontmatter parse)
     fails, clean up the tmp dir.
   - Only after validation succeeds, move the dir into place
     under bundles/<bundle>/skills/<name>/.
   - If --bundle omitted and the auto-created bundle is
     net-new: create bundle.yaml + the skills/ subdir + move
     the skill in, all in one atomic block. If any step fails,
     roll back the bundle creation.

5. Post-install:
   - Print the installed ref (<bundle>/skills/<name>) and a
     one-liner pointing to `kolsch skills show <ref>` and
     `kolsch skills which <agent>` so the user knows how to
     adopt it.
   - Run `kolsch skills validate <ref>` on the new install;
     surface its result.

6. Tests:
   - tests/component/cli/test_skills_install.py:
     * Local-file URL → install + validate. Use a tmp
       directory + file:// URLs OR a fake fetcher for unit-
       level isolation; the e2e (real git clone) is the
       manual scenario.
     * Strict-root failure (URL points at a multi-skill repo
       root with no SKILL.md) → install fails cleanly with
       no partial write.
     * --bundle nonexistent → fails before fetch.
     * --bundle valid → installed in the right place.
     * Auto-create flow → new bundle.yaml is well-formed.
     * --yes bypasses the trust prompt.

7. Manual e2e (in PR description):
   - Install one real skill from a public GitHub repo (pick
     one from the Anthropic skill ecosystem or write a
     throwaway test repo).
   - Verify catalog picks it up; `skills list` shows it;
     `skills show` displays it.

Scope: ~250 LOC + ~150 LOC tests.

ESCALATION PATH: if the trust-prompt UX clashes with the
chat-modal pattern from Phase 2.0 (different prompt callback
shape, different I/O assumptions), pick one consistent shape
across the CLI. Don't reinvent prompts for this command.

Acceptance: install works for at least one real GitHub URL
end-to-end; the strict-root contract is enforced; bundle
auto-create is atomic.
```

### Reviewer prompt

```
[standing instructions]

Review Step 7 — install.

Checks:

1. Strict-root contract: a URL that does NOT have SKILL.md at
   its root fails with a clear message AND leaves no partial
   write. Verify by running the failing case in the PR
   description.

2. --bundle nonexistent fails BEFORE the network fetch.
   Wasting bandwidth on a no-op is a fail.

3. Auto-create flow is atomic: a failure mid-stream rolls
   back the bundle. Verify by inducing a failure (e.g.,
   permission denied on the move) and checking that the
   bundle dir doesn't linger.

4. Trust prompt shows the script files AND allowed-tools
   declaration before any write.

5. --yes bypasses the prompt; non-TTY without --yes refuses
   to proceed (no silent install on CI).

6. Post-install runs validate. A skill that installs but
   fails validate is surfaced — NOT silently OK.

7. Tests cover all five enumerated paths.

8. Manual e2e in the PR description shows a real install
   from a real URL.

Verdict: PASS or FAIL.
```

---

## Step 8 — Remove + cleanup pass

- **Builds:** `kolsch skills remove <ref>`. Surfaces agents
  enabled-on the missing ref before delete.
- **Depends on:** Step 7 in main.
- **Acceptance:** remove deletes the skill dir; affected
  agents are warned.

### Developer prompt

```
[standing instructions]

Step 8 — Add `kolsch skills remove <ref>`.

Read first:
- cli/skills.py (Steps 4-7).
- The catalog walker to understand how a missing skill ref
  is currently surfaced (it raises SkillNotFound at agent
  config load).

Tasks:

1. Add `remove` op.
     kolsch skills remove <ref> [--yes]

   - Resolves the skill in the catalog.
   - Walks all agents in the catalog; collects those whose
     subsystems.skills.config.enabled lists this ref.
   - Prints:
     * The skill path that will be deleted.
     * The list of affected agents (with refs).
     * One-liner: "These agents will fail to load on next
       exchange until you remove or re-target the enabled
       entry."
   - Prompts "[y] delete / [n] cancel". --yes bypasses.
   - On confirm: delete the skill's directory (NOT the
     bundle).
   - DO NOT auto-edit the affected agents' YAMLs. The user
     is in charge of cleanup. (This matches the existing
     authz pattern — kolsch permissions remove doesn't
     auto-cleanup either.)

2. Special case — last skill in an auto-created bundle.
   - If removing this skill empties the bundle of all skills
     AND the bundle has no other assets (agents, tools,
     prompts), offer to ALSO delete the bundle. Prompt
     separately ("[y] delete bundle too / [n] keep").

3. Tests:
   - tests/component/cli/test_skills_remove.py:
     * Happy path: remove a skill; dir is gone; bundle.yaml
       remains.
     * Affected-agent warning is printed.
     * --yes bypasses the prompt.
     * Last-skill-in-bundle scenario offers the bundle-delete
       prompt and honors the response.
     * Ref doesn't resolve → friendly error.

Scope: ~120 LOC + ~80 LOC tests.

ESCALATION PATH: if the dependent-agent walk is O(N) per
agent file (the catalog already loaded them), confirm it's
not re-loading from disk. If it IS re-loading, escalate —
that's a catalog-design concern beyond Step 8.

Acceptance: remove works + affected agents are surfaced +
auto-created-bundle cleanup works.
```

### Reviewer prompt

```
[standing instructions]

Review Step 8 — remove.

Checks:

1. The dependent-agent walk uses the in-memory catalog, NOT
   a fresh disk scan. Verify by reading.

2. The "affected agents" warning is shown BEFORE the
   confirmation prompt. Reverse order is a UX fail.

3. Remove does NOT auto-edit affected agents' YAMLs.
   Consistency with permissions remove.

4. Last-skill-in-bundle prompts separately for bundle
   deletion. Bundle deletion is opt-in, not automatic.

5. --yes bypass works.

6. Tests cover the five paths.

Verdict: PASS or FAIL.
```

---

## Step 9 — First canonical skills shipped in `bundles/kolsch/skills/`

- **Builds:** 3-5 concrete skills under
  `bundles/kolsch/skills/` that exercise the existing tools
  (bash, read_file, write_file, list_files).
- **Depends on:** Step 8 in main.
- **Acceptance:** skills are catalogued; helper agent can
  invoke them; `kolsch skills validate --all` passes.

### Developer prompt

```
[standing instructions]

Step 9 — Ship first canonical skills.

Read first:
- All of Steps 1-8.
- bundles/kolsch/agents/helper.yaml to understand the agent
  surface skills will compose with.
- docs/explanation/skills-subsystem.md (Step 1's refresh).

Tasks:

1. Author three skills under
   src/cli/_starter_bundle/kolsch/skills/<name>/SKILL.md.

   Proposed initial set (pick exactly these unless the
   authoring reveals one is too thin to justify):

   a. write-python-test
      - When the user asks to write tests for a Python module,
        this skill provides the pytest patterns kolsch
        prefers: parametrize-over-subtests, no mocking inside
        a unit's own dependency boundary, tmp_path over
        tempdir, freeze time via freezegun.
      - allowed-tools: [bash, read_file, write_file, list_files]
      - Body: 100-200 lines of opinionated guidance + 1-2
        concrete examples.

   b. review-pr-diff
      - When the user asks to review a diff (or a PR — bash
        gh pr diff <n>), this skill provides the reviewer
        checklist kolsch uses: read the surrounding code, not
        just the diff; flag tests-not-updated; flag
        permissions/authz drift; flag schema/contract changes.
      - allowed-tools: [bash, read_file, list_files]
      - Body: review heuristics + the AGENTS.md cross-reference.

   c. commit-from-staged
      - When the user asks to draft a commit message from
        staged changes, this skill provides the conventional-
        commit format kolsch uses (verified by `git log
        --oneline`'s shape in this repo) — fix/feat/docs/test/
        chore + scope + imperative subject.
      - allowed-tools: [bash, read_file]
      - Body: format spec + how to inspect the staged diff.

2. NO automatic enablement on the helper agent.
   - bundles/kolsch/agents/helper.yaml stays unchanged.
   - Users opt-in by adding entries to
     subsystems.skills.config.enabled OR by force-invoking
     with /skill at chat time.
   - Why: Sk-I6 (enablement is explicit) is the load-bearing
     invariant. Auto-enabling on helper would dilute it.

3. Update the starter bundle's bundle.yaml description if it
   names what the bundle contains (probably does).

4. process/test-catalog.md: add a "Canonical Skills (KSKILL-*)"
   section with one TC per skill verifying it parses + appears
   in the catalog.

5. Tests:
   - tests/component/test_canonical_skills.py:
     * Each skill parses its frontmatter.
     * Each skill's body is >= some sanity threshold
       (>= 50 lines).
     * Each skill's catalog ref is what the file system says.

6. NO tests of the SKILL CONTENT itself (e.g., that the test-
   writing guidance is good). That's not testable in CI; it's
   reviewed at PR time by reading the body.

7. Migration: existing workspaces won't see the skills until
   `kolsch setup` re-run picks them up via the drift
   detector. Confirm by running setup re-run against a fixture
   workspace.

Scope: ~600-1000 LOC of authored skill bodies + ~80 LOC tests.

ESCALATION PATH: if authoring reveals that one of the three
proposed skills is too thin (the guidance would fit on a
single page and doesn't justify being a skill), escalate the
set choice — pick a different third skill OR ship just two.
Quality > count.

Acceptance: three skills authored; tests pass; manual chat
shows /skill kolsch/skills/write-python-test producing a
sensible follow-on response.
```

### Reviewer prompt

```
[standing instructions]

Review Step 9 — first canonical skills.

Checks:

1. Three skill directories exist under
   src/cli/_starter_bundle/kolsch/skills/ with SKILL.md.

2. Each SKILL.md's frontmatter has name + description +
   allowed-tools + disable-model-invocation.

3. Each body is substantive (>= 50 lines, not a stub).
   Read each one for actual content — a skill that's "TODO:
   add real guidance" is a fail.

4. The bodies match kolsch's actual conventions:
   - write-python-test: parametrize-over-subtests pattern
     visible (read AGENTS.md / existing tests for ground
     truth).
   - review-pr-diff: cross-references AGENTS.md if it should.
   - commit-from-staged: conventional-commit format matches
     `git log --oneline` output in this repo.

5. helper.yaml is UNCHANGED. Sk-I6 (explicit enablement) is
   preserved.

6. Each skill's allowed-tools list is a sane subset of the
   bundle's tools.

7. process/test-catalog.md gained KSKILL-* entries.

8. tests/component/test_canonical_skills.py passes.

9. Manual e2e in PR description: /skill kolsch/skills/<one>
   in chat shows the SKILL.md body being read and the agent
   using it.

Verdict: PASS or FAIL.
```

---

## Part 3 — How to escalate

```markdown
### Escalation E-PHASE-2.4-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–9
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:** 2–3 paths.
- **Estimated cost:**

---

### Coordinator resolution — E-PHASE-2.4-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Step 1 finds one of Sk-I1..I8 actually broken (not just
  under-documented).
- Step 1 finds Phase 2.2 composition is NOT replace-only —
  contradicts the locked decision.
- Step 1 finds default authz blocks SKILL.md reads in a way
  that makes Step 3 unusable.
- Step 6 cannot integrate validation into the existing
  `_migrations/` orchestrator without a refactor — pick a
  different hook with input.
- Step 7's URL fetch / git clone surface has no obvious
  candidate library — pick a dep with input.
- Step 9 authoring reveals a proposed skill is too thin to
  justify.

When NOT to escalate:
- Doc wording in the SKILL.md template.
- The exact bundle-name discriminator format
  (`<name>-2` vs `<name>.2`).
- Whether `kolsch skills` subcommand help text uses verbs
  or noun-verb phrasing.

---

## Part 4 — Coordinator Escalation Log

*(empty — populate as escalations land)*

---

## Part 5 — Phase 2.4.2 sketch (deferred)

The "skill-creating skill" — a meta-skill that walks a user
through authoring a new skill via conversation. The user
describes what they want; the meta-skill generates the
SKILL.md frontmatter, drafts the body from a template,
identifies which tools are needed, and writes the file.

Why deferred:
- The interaction is fundamentally multi-step: "what's it
  for?" → "what tools?" → "let me draft → look OK?" → write.
  That shape is a workflow, not a single exchange. Phase 3.x
  (workflows) is the natural foundation.
- Without workflows, 2.4.2 would either be a heavy single-shot
  generation (low quality) or require ad-hoc multi-turn
  scaffolding inside the skill itself (Sk-I3 + Sk-I7 push
  back).

Suggested shape when 3.x makes it viable:
- A workflow that uses two-three agents (interviewer →
  drafter → reviewer).
- A canonical skill that lives in `bundles/kolsch/skills/
  skill-author/` and is force-invoked via `/skill` or
  `kolsch run --skill skill-author --input what="..."`.
- The drafter agent uses `write_file` to land the new
  SKILL.md; the reviewer agent uses `kolsch skills validate`
  via bash.

Pre-flight checklist before opening 2.4.2:
- Phase 3.0 + 3.1 shipped (asset type + sequential nodes).
- Authoring conventions stable (Step 9's set has been used
  for a few weeks; the patterns have congealed).
- One or two community-contributed skills exist so the
  meta-skill has examples to learn from.

---

## Part 6 — Notes on running this phase

- **Strict order: 1 → 2 → … → 9.** Step 1 is the gate. Step 1's
  audit shapes the test surface in Step 2 AND the authz
  prerequisite for Step 3.
- **Each step ships as its own PR.** Bundling multiple verbs
  into one CLI PR makes review harder. The pattern from Phase
  2.1's 24a..24e split is the model.
- **No new event types** added in this phase. Sk-I5 stays
  intact (skills emit no events; the engine's synthetic
  tool.called / tool.result pair for force-invoke is
  existing infrastructure).
- **No schema-level changes** to `subsystems.skills.config`
  shape. The user-facing change is CLI surface + content,
  not config-shape changes.
- **Don't bundle Phase 2.3 work** (OpenClaw / Pi agents)
  into Step 9's canonical skills. Phase 2.3 is its own
  scope; mixing them muddies both.
- **Don't add multi-skill repo discovery to Step 7.** The
  locked decision is strict-root. If a future version wants
  multi-skill repo support, it's a separate ticket and a
  separate UX (probably an interactive picker, not auto-all).

---

## Part 7 — Out of scope

- **Multi-skill repo auto-discovery** (locked decision: strict
  URL → single SKILL.md root).
- **Additive `extend:` syntax for composition** (locked
  decision: RFC 7386 replace-only stays).
- **`allowed-tools` enforcement** (Sk-I3 informational stays
  in v1).
- **Workspace-level `skills/` dir outside any bundle** (all
  skills live in bundles; auto-create handles standalone).
- **Auto-enablement of canonical skills on the helper agent**
  (Sk-I6 explicit-enablement preserved).
- **Signature verification on install** (trust = "I trust this
  URL"; signing is a future concern).
- **A skill registry / index** (out of scope until a registry
  partner exists; URLs are the v1 source).
- **`kolsch skills publish`** (no upstream destination exists
  for v1).
- **Phase 2.4.2 skill-creating skill** (deferred to post-Phase
  3.x; sketched in Part 5).
- **Per-skill versioning UX** (the frontmatter `compatibility`
  field stays parsed-not-enforced).
- **Filesystem-watching for live catalog reload** (current
  catalog rescans on setup; users run `kolsch setup` or
  restart to pick up changes).
