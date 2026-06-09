# QoL — Human-readable permission system: glob rules, always-qualified tools, chat-side recovery

**Draft 0.2** *(bumps 0.1 — dev + reviewer prompts tightened to focus on essential tasks per step. Same seven decisions; same four-step decomposition; just shorter, more actionable prompts.)*

> Four-step build plan consolidating seven decisions from the
> platform-yaml design conversation: simplify rule schema;
> replace regex with glob; require qualified tool names; add
> slash commands + denial-render capsule for in-chat recovery.
> Sequenced after `qol-prefix-defaults-and-unified-refs.md`;
> before `qol-action-namespace-bundle-scoped.md`.

> The current `platform.yaml` authz config is a footgun:
> rule IDs leak system internals into the human format; the
> `who` field is dead code; `what` is regex (dangerous + hard
> to read); the dual-match against action OR target is
> implementation detail leaking into the rule grammar; bare
> tool names invite ambiguity; persistent `deny` rules lock
> users out with no in-chat recovery path. Every one of
> these surfaced during normal use of the workspace authored
> in this conversation.
>
> This QoL rewrites the rule schema to three fields
> (`what` / `pattern` / `effect`), replaces regex with glob,
> requires fully-qualified tool names with fail-loud-on-
> ambiguity at startup, and adds `/perm <verb> <tool>
> <pattern>` slash commands plus a distinct `authz.denied`
> capsule that shows the matched rule and the exact slash
> incantation to undo. No more hand-editing YAML mid-chat;
> no more silent denials.

---

## Part 0 — Why this matters

Audit of the user's `~/Documents/kolsch/platform.yaml` (the
canonical post-setup state):

```yaml
- id: rl_ecbb791b      # ← system-generated leak
  who: .*              # ← dead field (always .*)
  what: agent\.execute\..*   # ← regex; could be glob
  effect: allow

- id: rl_4534193f      # ← system-generated leak
  who: .*              # ← dead field
  what: ^find .*$      # ← regex matching the bash COMMAND
                       #    (dual-match: target OR action)
                       #    suggests bash but doesn't NAME it
  effect: allow

- id: rl_default_deny  # ← descriptive but inconsistent with rl_*
  who: .*
  what: .*             # ← regex catch-all
  effect: deny
```

Six different shapes of "wrong" in three rules:

1. **`id:`** is system-managed and serves no human purpose.
   Makes the file non-editable; users can't safely diff or
   merge platform.yaml files across machines.
2. **`who:`** is dead code today. Every rule has `who: .*`.
   The principal dimension was reserved for Phase 3.x
   multi-agent use cases that haven't materialized.
3. **`what` is regex.** Modal-generated patterns like
   `^find .*$` permit command injection (`find . && rm -rf /`
   matches); pathological patterns risk ReDoS; visual
   inspection of regex is hard.
4. **`what` dual-matches** against action OR target.
   `tool\.bash` matches the action namespace;
   `^find .*$` matches the target string. Same field, two
   roles. Surprising.
5. **Tool identity is implicit** in patterns like
   `^find .*$`. Reader can't tell which tool the rule
   applies to without knowing the dual-match semantics.
6. **Persistent `deny` rules are silent** in chat. Modal
   only fires for `effect: prompt`; `deny`-effect rules just
   raise `ToolUnauthorized` and the model sees an opaque
   `authz.denied` error. Agent has no recovery path; user
   has no chat-side way to grant the permission without
   exiting to CLI.

This QoL fixes all six.

---

## Part 1 — Decisions locked

From the agent-YAML-design conversation (multiple turns):

### Rule schema

1. **Three fields: `what`, `pattern`, `effect`.** No `id`,
   no `who`. The schema becomes:
   ```yaml
   - what: kolsch/bash
     pattern: "find *"
     effect: allow
   ```

2. **`what` is the tool's fully-qualified ref** (e.g.,
   `kolsch/bash`) OR a system action namespace
   (`agent.execute`) OR the catch-all `*`. **No bare tool
   names.** Slash for bundle separator; matches the
   ref-grammar shipped by
   `qol-prefix-defaults-and-unified-refs.md`.

3. **`pattern` is a glob** (`*` for any-chars, `?` for
   one-char). Required (per the prior decision that broad
   scope needs explicit opt-in via `*`). No regex, no
   anchors, no character classes, no quantifiers.

4. **`effect` is unchanged**: `allow` / `deny` / `prompt`.

5. **Catch-all is `{what: "*", pattern: "*", effect: <e>}`.**
   Both fields are `*`. The previous `who: .*, what: .*`
   catch-all collapses to this shape.

### Tool identification

6. **`what` always names a tool by its fully-qualified
   ref (no shorthand).** `what: bash` is rejected at
   config-load with a clear error naming the available
   candidates. The user must write `what: kolsch/bash`
   (or whichever bundle owns it). System actions (e.g.,
   `agent.execute`) don't have bundles and are accepted
   as-is.

7. **Ambiguity fails loud at kolsch startup.** If two
   bundles both ship a tool called `foo` and any rule has
   `what: foo`, kolsch refuses to start with a message
   that names both candidates AND the bare-form rule.

### Chat-side recovery

8. **New `/perm` slash commands.** Grammar:
   ```
   /perm allow <tool> <pattern>
   /perm deny  <tool> <pattern>
   /perm prompt <tool> <pattern>
   /perm remove <tool> <pattern>
   /perm list
   ```
   `<tool>` is fully qualified. `<pattern>` is required;
   `*` for broad. Same atomic write path as the modal's
   `[a]/[d]` flow (writes to `platform.yaml` via
   `cli/permissions._write_back`).

9. **Distinct render capsule on `authz.denied`.** When a
   tool fails because of a `deny`-effect rule (or the
   catch-all), the chat renders a capsule showing:
   - The tool that was denied.
   - The matched rule's `what`/`pattern` (rendered in glob,
     not regex).
   - The exact `/perm <allow|prompt>` command to undo.

   The same structured message is included in the
   `tool.failed` event data so the model also sees the
   actionable hint and stops flailing.

### Migration

10. **Existing regex rules are auto-converted on `kolsch
    setup` re-run** via a heuristic that covers every
    pattern produced by the existing `suggest_pattern`
    helper at `_modal.py:63-109`:
    - `what: tool\.<name>` → `what: <home_bundle>/<name>,
      pattern: "*"` (after resolving home_bundle from the
      catalog).
    - `what: ^<cmd> .*$` (bash-style suggest) →
      `what: <bash_bundle>/bash, pattern: "<cmd> *"`.
    - `what: agent\.execute\..*` →
      `what: agent.execute, pattern: "*"`.
    - `what: .*` → `what: "*", pattern: "*"`.
    - `id:` field stripped.
    - `who:` field stripped (only `who: .*` exists today;
      see Step 1 verification).

11. **Ambiguous or unconvertible rules** (a custom
    user-authored regex that doesn't fit the heuristic)
    surface as a `config.deprecated_regex_rule` Recoverable
    event at workspace load AND in
    `kolsch permissions list` with a `[legacy regex —
    please convert]` marker. The rule still parses during a
    migration window; doesn't crash kolsch.

12. **No automatic migration of user-edited rules
    mid-conversation.** Migration is at `kolsch setup`
    re-run only. A user editing platform.yaml live always
    sees the post-migration shape.

---

## Part 2 — Acceptance criteria

The QoL is shipped when **all** hold:

1. **Three-field schema is the canonical form.** Every rule
   in a freshly-set-up workspace's `platform.yaml` has
   only `what`/`pattern`/`effect`. `who` and `id` fields
   are absent.

2. **Glob matching is the only matching engine.** The
   authz provider's rule-matching code uses Python's
   `fnmatch` (or equivalent) — no regex compilation.
   Pathological-pattern tests confirm no ReDoS surface.

3. **Tool refs in `what` must be fully qualified.**
   Bare-name rules at workspace load raise a clear error
   naming the rule location (file:line) AND the available
   candidates. Kolsch refuses to start.

4. **Ambiguity is detected and fails loud.** Two bundles
   shipping the same tool name + any rule using the bare
   form → kolsch refuses to start with both candidates
   named. Same error class as `AssetsAmbiguousShorthand`
   (reused, not new).

5. **`/perm` slash commands work in chat.** All five verbs
   land. The grammar enforces `<tool> <pattern>` with no
   optional pattern (broad scope requires `*`). Tool refs
   are validated for qualification.

6. **Distinct deny capsule renders.** When the engine
   emits `tool.failed` with `error.type ==
   authz.denied`, the renderer surfaces:
   - Tool name + the matched rule's `what`/`pattern`.
   - The exact slash command to undo (e.g., `/perm allow
     kolsch/bash *`).
   - Distinct visual style from the generic tool-failure
     capsule.

7. **Model sees the actionable hint.** The `tool.failed`
   event's `error.detail` carries the `/perm`
   command text so the model surfaces it in its response
   instead of guessing.

8. **Migration completes cleanly.** A workspace with the
   pre-fix `who/what/effect/id` schema and regex `what`
   patterns runs through `kolsch setup` re-run and emerges
   with the new shape. Every rule that came from the
   `suggest_pattern` helper round-trips correctly.

9. **Legacy rules surface as deprecation warnings.** Any
   regex rule that doesn't match the migration heuristic
   loads with a `config.deprecated_regex_rule` Recoverable
   event AND appears in `kolsch permissions list` with the
   `[legacy regex]` marker. Doesn't block runtime.

10. **Regression tests cover every transition**:
    - Old → new schema migration on setup re-run.
    - Glob matching against the canonical patterns
      `*`, `?`, `<prefix>*`.
    - Bare-tool-name rule → clear startup error.
    - Ambiguous bare-tool-name rule → clear startup error
      naming both candidates.
    - Slash command writes a rule in the new form;
      reload finds it.
    - `authz.denied` event triggers the distinct capsule;
      capsule shows the right slash command.

11. **Documentation cascade.**
    `docs/how-to/permissions.md`, `docs/reference/schemas.md`,
    `docs/how-to/chatting-effectively.md` all reflect the
    new schema, glob grammar, slash commands, and capsule.
    No regex examples remain.

### Out of scope

- **Phase 3 principal-based scoping** (the eventual
  replacement for the dropped `who:` field). If multi-agent
  workflows ever need actor-scoping, a future `scope:`
  field can be added under a new spec. This QoL doesn't
  reserve syntax for it.
- **Tool action namespace migration to
  `tool.<home_bundle>.<name>`**. Tracked at
  `qol-action-namespace-bundle-scoped.md`. This QoL
  requires the tool ref form (`kolsch/bash`) in `what`
  but doesn't change the action namespace string the authz
  provider matches against internally — that's the
  follow-up's job.
- **Modal UX changes** beyond the underlying write path.
  The `[y]/[a]/[n]/[d]` choices stay; only the persisted
  rule shape changes.
- **Rule reordering or "specificity wins" semantics.**
  File order continues to determine precedence; the slash
  command's smart-insertion (positioning new rules above
  conflicts) is the only intelligence added.
- **Removing the catch-all default rule entirely.** The
  modal-on-no-rule path from
  `qol-tool-permission-discovery-uniform-with-stacking.md`
  combined with changing the catch-all from `deny` to
  `prompt` would obsolete the catch-all; tracked
  separately if pursued. This QoL ships the catch-all
  unchanged (just in the new shape).
- **System actions in slash commands.** Users edit tool
  rules via `/perm`; system action rules
  (`agent.execute`) are platform-managed and not surfaced
  to slash commands.

---

## Part 3 — Step dependency graph

```
qol-prefix-defaults-and-unified-refs.md (shipped)  ←── PREREQUISITE
   │
   ▼
Step 1 (verification + design picks + migration heuristic table) ←── STOP gate
   │
   ▼
Step 2 (schema + glob matcher + qualified-tool validation + migration shim)
   │
   ▼
Step 3 (slash commands + distinct denied capsule + structured tool.failed hint)
   │
   ▼
Step 4 (regression tests + doc cascade + manual e2e + handoff)
```

| Step | Ships | Reviewable in |
|---|---|---|
| **1** | Verification writeup + migration-heuristic table + slash-grammar pin + capsule shape | ~40 min |
| **2** | Schema change + glob matcher + qualified-tool validation + migration shim for legacy | ~60 min |
| **3** | Slash command family + denied capsule + tool.failed structured hint | ~45 min |
| **4** | Full doc cascade + manual e2e + handoff | ~30 min |

---

## Step 1 — Verification + migration table

### Developer prompt

```
[standing instructions]

Step 1 — Verify the design and lock the migration table.
No production code.

Read first:
- This spec, Parts 0-2.
- process/qol-prefix-defaults-and-unified-refs.md (PREREQ —
  confirm shipped).
- ~/Documents/kolsch/platform.yaml (canonical current state).
- src/providers/authz.py (Rule + matcher).
- src/cli/_modal.py:63-109 (suggest_pattern emits regex today).
- src/assets/refs.py:39-77 (resolve_shorthand reference).

Tasks:

1. Confirm prereq QoL shipped. If not, BLOCK.

2. Audit canonical platform.yaml. Build a table:
     OLD shape (id/who/what/effect)  →  NEW shape (what/pattern/effect)
   covering every rule + the bash-command patterns
   suggest_pattern produces.

3. Pin glob matcher: fnmatch.fnmatchcase. Confirm
   no-character-classes + ReDoS-safe.

4. Pin tool-ref validation site (provider construction)
   and confirm catalog access there.

5. Sketch the denied capsule shape (per
   qol-chat-capsule-layout.md): tool ref + matched
   rule + /perm hint.

6. STOP and post the writeup + the migration table.

ESCALATION:
- Prereq QoL not shipped.
- Authz provider lacks catalog access at construction.
- A current rule doesn't fit the migration heuristic.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1. No code; writeup is the deliverable.

Checks:
1. Prereq QoL confirmed shipped.
2. Migration table covers every rule in the canonical
   workspace; no TBD cells.
3. Glob matcher pick is fnmatch.fnmatchcase, ReDoS-safe.
4. Tool-ref validation site named; catalog access
   confirmed.
5. Capsule shape sketched.

Verdict: APPROVE-TO-PROCEED or REVISE.
```

---

## Step 2 — Schema + glob matcher + qualified-tool validation + migration shim

### Developer prompt

```
[standing instructions]

Step 2 — Build the new rule shape, glob matcher,
qualified-tool validation, and legacy migration.

Read first:
- Step 1's writeup.
- src/providers/authz.py (Rule + provider).
- src/cli/_migrations/ (existing migrator framework).

Tasks:

1. New Rule class with three fields: `what`, `pattern`,
   `effect`. Drop `id` and `who`. Field validators reject
   regex characters in `pattern` (no `[`, `^`, `$`).

2. Replace the rule matcher with fnmatch.fnmatchcase.
   Match `what` against action OR tool ref; match
   `pattern` against target.

3. Validate every rule's `what` at provider
   construction:
   - `*` → accept
   - Contains `.` (e.g., `agent.execute`) → accept
   - Contains `/` → validate bundle + tool exist
   - Bare name → raise (reuse AssetsAmbiguousShorthand
     or sketch a new error per Step 1's pick) with
     candidates list

4. Migration shim under src/cli/_migrations/. Convert
   per Step 1's table. Unconvertible legacy regex rules
   stay in legacy form + emit
   `config.deprecated_regex_rule` (Recoverable).

5. Unit tests: new shape loads; legacy migrates;
   bare-name rejected; ambiguous bare-name rejected; glob
   matches `*` / `?` / `<prefix>*`. Integration test:
   migration roundtrip on a pre-fix workspace.

6. NO change to _modal.py, _slash.py, _renderer.py,
   engine/observers.py.

ESCALATION: a real rule contains regex features
(character classes, lookahead) the heuristic can't
convert AND can't safely deprecate.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2.

Checks:
1. Rule has exactly {what, pattern, effect}. No id, no
   who.
2. Glob matcher uses fnmatch; no re.compile / re.match
   in new code.
3. Qualified-tool validation runs at provider
   construction; bare names raise with candidates.
4. Migration shim handles every Step 1 case;
   deprecation event fires for unconvertible legacy.
5. NO change to modal, slash, renderer, observer (git
   diff confirms).
6. Unit tests cover the five paths; integration test
   passes.
7. Full test suite green; goldens byte-equal.

Verdict: PASS or FAIL.
```

---

## Step 3 — Slash commands + distinct denied capsule + recovery hint

### Developer prompt

```
[standing instructions]

Step 3 — Register /perm commands, render a distinct
denied capsule, and thread a recovery hint into
tool.failed events.

Read first:
- Step 2's PR.
- src/cli/_slash.py (slash framework).
- src/cli/_modal.py:63-109 (suggest_pattern; switch to
  glob output).
- src/cli/_renderer.py.
- src/schemas/events.py (ToolFailedData).
- process/qol-chat-capsule-layout.md (capsule
  conventions).

Tasks:

1. Register five /perm commands in _slash.py:
   /perm {allow, deny, prompt, remove} <tool> <pattern>
   /perm list
   Each verb validates `<tool>` qualification + glob
   `<pattern>`, persists via cli/permissions._write_back.
   New allow/deny/prompt rules land ABOVE the catch-all
   and ABOVE conflicts; output shows the user where it
   landed.

2. Update modal at _modal.py:63-109 — suggest_pattern
   emits glob (`find *`) instead of regex (`^find .*$`).
   Rule write path uses the new schema.

3. Render distinct authz.denied capsule in _renderer.py:
   tool ref (qualified) + matched rule's what+pattern +
   exact /perm command to undo.

4. Extend ToolFailedData with `recovery_hint: str | None`.
   Engine populates on authz.denied so the model sees the
   actionable text.

5. Tests: each slash verb writes the right rule; bare
   tool name → friendly slash error; denied capsule
   renders; ToolFailedData accepts recovery_hint.

6. NO change to Rule class, glob matcher, catch-all
   behavior, engine's authz check (only event emission
   gains a field).

ESCALATION: smart-insertion produces ambiguous ordering
on a user's hand-arranged rules. Slash command should
prompt-not-overwrite.
```

### Reviewer prompt

```
[standing instructions]

Review Step 3.

Checks:
1. Five /perm verbs registered; each validates qualified
   tool + glob pattern.
2. _modal.py:63-109 emits glob; modal writes new schema.
3. Distinct authz.denied capsule with tool ref, matched
   rule (new form), and /perm hint. Visual matches
   qol-chat-capsule-layout.md.
4. ToolFailedData has recovery_hint; engine populates it
   on authz.denied.
5. Smart insertion verified (new rules above catch-all
   + conflicts).
6. NO change to Rule, matcher, engine authz check (git
   diff confirms).
7. Tests cover slash verbs + capsule + schema field.
8. Full suite green; goldens byte-equal except where
   updated for recovery_hint.

Verdict: PASS or FAIL.
```

---

## Step 4 — Tests + doc cascade + manual e2e + handoff

### Developer prompt

```
[standing instructions]

Step 4 — Integration tests, docs, real-workspace e2e,
user handoff.

Read first:
- Steps 1-3 PRs.
- docs/how-to/permissions.md (rewrite target).
- docs/reference/schemas.md.
- docs/how-to/chatting-effectively.md.

Tasks:

1. tests/integration/test_perm_system_e2e.py with five
   cases:
   - Migration round-trip (legacy → new shape).
   - Bare tool name blocks startup.
   - /perm allow persists + takes effect on next call.
   - authz.denied renders distinct capsule with /perm
     hint.
   - tool.failed event carries recovery_hint.
   Add catalog entries under "Permission System
   (PERM-SYS)".

2. Doc cascade:
   - permissions.md: full rewrite. Sections: rule shape,
     tool refs (qualified required), patterns (glob),
     editing rules (modal / /perm / hand-edit), when a
     tool is denied, migrating from legacy.
   - schemas.md: update Rule to three-field form; add
     "Legacy rule form" subsection naming the
     deprecation event.
   - chatting-effectively.md: add /perm section.
   - grep -rn "who:\|rl_" docs/ — clean up remaining
     mentions.

3. Pre-fix verification: stash Step 2-3's code; run the
   migration test; document the pre-fix failure mode.

4. Manual e2e on ~/Documents/kolsch:
   - Run kolsch setup re-run; confirm platform.yaml's
     new shape.
   - Run kolsch chat; trigger an unconfigured tool;
     observe the denied capsule + /perm hint; type the
     command; confirm persistence + next-call passes
     silently.
   - Capture session JSONL excerpts.

5. Hand off: PR description includes JSONL excerpts +
   asks the user to confirm against their own workspace.
   Reviewer verdict stays REVISE-PENDING-USER until
   confirmed.

ESCALATION: migration produces wrong output for the
canonical workspace — revise Step 2, don't patch in
Step 4.
```

### Reviewer prompt

```
[standing instructions]

Review Step 4.

Checks:
1. Five PERM-SYS integration tests pass; catalog
   entries added.
2. Docs rewritten: permissions.md, schemas.md,
   chatting-effectively.md. grep -rn "who:\|rl_" docs/
   returns only marked-legacy entries.
3. Pre-fix verification documented.
4. Manual e2e JSONL shows migration + denied capsule +
   /perm flow.
5. User-confirmation request in PR.
6. Full suite green; no unrelated diff.

Verdict: PASS (after user confirms), REVISE-PENDING-USER,
or FAIL.
```

---

## Part 4 — How to escalate

```markdown
### Escalation E-QOL-PERMSYS-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–4
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:** 2–3 paths.
- **Estimated cost:**

---

### Coordinator resolution — E-QOL-PERMSYS-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Step 1 finds the prereq QoL didn't ship as expected.
- Step 1 finds a current rule that doesn't fit the
  migration heuristic.
- Step 2 finds the authz provider can't access the
  catalog at construction (qualified-tool validation
  needs it).
- Step 3 finds the slash command's smart-insertion
  produces ambiguous ordering decisions.
- Step 4's migration produces unexpected output for the
  canonical workspace.

When NOT to escalate:
- Doc wording.
- Exact capsule visual styling (match qol-chat-capsule-layout.md
  but micro-tweaks are fine).
- Slash command tab-completion behavior beyond "completes
  qualified tool refs from the catalog."

---

## Part 5 — Coordinator Escalation Log

### Escalation E-QOL-PERMSYS-001 — Transitional `Rule` fields (retro-logged from Step 2 review)

- **Raised by:** reviewer (retro-log after Step 2 PASS verdict)
- **Step:** 2
- **Trigger:** Step 2 task 1 says "Drop `id` and `who`"; Step 2 task 6
  says "NO change to `_modal.py`". `_modal.py:289-294` constructs
  `Rule(id="", who=".*", what=pattern, effect=...)` — the two
  constraints are mutually unsatisfiable as written.
- **Why this is not a guess:** verified at `src/cli/_modal.py:289-294`
  on the Step 2 branch; the construction site is load-bearing for
  the `[a]`/`[d]` modal persist flow.
- **Options considered:**
  - (a) Hard-break the field surface in Step 2; touch `_modal.py`
    anyway. Violates task 6.
  - (b) Keep `id` / `who` as optional/legacy on the class; matcher
    dispatches on `who is not None`. Modal keeps working. Step 3's
    modal rewrite drops the fields cleanly.

---

### Coordinator resolution — E-QOL-PERMSYS-001

- **Decided by:** reviewer (Step 2 PASS verdict)
- **Decision:** option (b). `Rule` carries 5 fields transitionally:
  `what`, `pattern`, `effect`, plus optional legacy `id: str = ""`
  and `who: str | None = None`. Matcher keys off `who is not None`
  via the `is_legacy` property.
- **Rationale:** the transitional shape is defensible; constraints
  were genuinely mutually unsatisfiable; Step 3 task 2 already
  rewrites the modal to emit new-shape rules at which point the
  legacy fields can be dropped cleanly.
- **Resume instruction:** Step 3 — once `_modal.py:289-294` lands
  on the new shape (Step 3 task 2), drop `id` and `who` from
  `Rule`, remove the `is_legacy` discriminator, and unify the
  matcher on glob. Update `_compile_legacy` callers in
  `providers/authz.py` accordingly. Drop `_compile_rule_record`'s
  legacy branch.

---

### Escalation E-QOL-PERMSYS-002 — `ConfigDeprecatedRegexRule` not raised end-to-end (carryover from Step 2)

- **Raised by:** reviewer (Step 2 partial on item 4)
- **Step:** 2 → 4
- **Trigger:** Step 2 reviewer item 4 reads "deprecation event
  fires for unconvertible legacy." The migrator at
  `cli/_migrations/authz_rules.py` correctly produces a
  `MigrationResult.deprecated: list[DeprecatedRegexRule]` for
  every un-converted legacy rule, but no production caller
  raises `errors.ConfigDeprecatedRegexRule` from those entries —
  the class is defined + registered + tested in isolation only.
  Acceptance criterion 9 ("loads with a
  `config.deprecated_regex_rule` Recoverable event AND appears in
  `kolsch permissions list` with the `[legacy regex]` marker") is
  not satisfied at e2e level.
- **Why this is not a guess:** `grep -rn "ConfigDeprecatedRegexRule"
  src/` finds only the class definition + the catalog
  registration + the dataclass-to-error binding in the migrator
  module. No callsite in setup or workspace-load emits one.
- **Options considered:**
  - (a) Patch in Step 2 follow-up: wire the migrator into
    `_setup.py:_maybe_migrate_authz_rules` (or a new Step-2 sibling
    helper) and raise per entry. ~1 callsite.
  - (b) Defer to Step 4 alongside the setup-runner + manual e2e
    work, which already touches the migration surface.

---

### Coordinator resolution — E-QOL-PERMSYS-002

- **Decided by:** reviewer (Step 2 PASS-with-carryover)
- **Decision:** option (b) — defer to Step 4 with explicit
  acknowledgment. Step 4 task 1's integration test for
  "migration round-trip (legacy → new shape)" is the natural
  place to assert the Recoverable event surfaces alongside the
  YAML rewrite.
- **Rationale:** Step 4 already plans the setup-runner wiring +
  manual e2e on `~/Documents/kolsch`; bundling the deprecation
  emission with that work keeps the change set focused and
  avoids a one-off PR.
- **Resume instruction:** Step 4 — extend the setup-runner's
  authz-rules migration step (currently
  `_maybe_migrate_authz_rules` in `cli/_setup.py`) so it calls
  `cli._migrations.authz_rules.migrate_authz_rules` and emits
  one `ConfigDeprecatedRegexRule` per
  `MigrationResult.deprecated` entry (via the existing
  Recoverable-event surface). Also update
  `cli/permissions.py:_cmd_list` to mark legacy-form rules
  with `[legacy regex — please convert]` per acceptance
  criterion 9. Add an integration test that builds a workspace
  with one un-migratable rule and asserts the event fires.

---

### Escalation E-QOL-PERMSYS-003 — Spec/code naming drift: `authz.denied` vs `tool.unauthorized` (Step 3 → Step 4)

- **Raised by:** reviewer (Step 3 PASS carryover #1)
- **Step:** 3 → 4
- **Trigger:** the spec describes the denied-capsule trigger and
  the `error.type` in `ToolFailedData` as
  `authz.denied`, but the dispatcher actually raises
  `ToolUnauthorized` (`tools/dispatch.py:102`) whose `code` is
  `tool.unauthorized`. The renderer's discriminator at
  `_renderer.py:464-470` correctly keys off
  `error.code == "tool.unauthorized"` AND
  `recovery_hint is str`, matching what actually flows. The
  registered `AuthzDenied` error class (`errors/authz.py:84`,
  code `authz.denied`) has zero call sites in production code.
- **Why this is not a guess:**
  `grep -rn "AuthzDenied\|raise AuthzDenied" src/` finds only
  the class definition + the catalog registration; the
  dispatcher boundary at `tools/dispatch.py:102-109` only
  raises `ToolUnauthorized`.
- **Options considered:**
  - (a) Reconcile spec wording in `docs/how-to/permissions.md`
    + Acceptance Criterion 6 to name `tool.unauthorized` (with
    a one-line note that the dispatcher wraps any
    `authz.denied`-equivalent decision in `tool.unauthorized`
    at the tool boundary).
  - (b) Switch the dispatcher to raise `AuthzDenied` instead
    of `ToolUnauthorized` — bigger blast radius, touches
    Diagnostic-level error surface used by 41+ tests.

---

### Coordinator resolution — E-QOL-PERMSYS-003

- **Decided by:** reviewer (Step 3 PASS-with-carryover)
- **Decision:** option (a). The dispatcher's wrap is correct
  spec-wise (a tool boundary error is `tool.*`); the spec
  language just lagged the implementation.
- **Rationale:** behavior is correct; the issue is wording.
  Larger refactors at the error-class level violate Step 3
  task 6 ("engine's authz check" must not change) anyway.
- **Resume instruction:** Step 4 doc cascade — update
  `docs/how-to/permissions.md`, `docs/reference/schemas.md`,
  and Part 2 Acceptance Criterion 6 of this spec to say
  `tool.unauthorized` (with the wrap explanation). Optionally
  delete the unused `AuthzDenied` class (`errors/authz.py:84`)
  + its catalog registration; flag the deletion separately
  so the test for "every registered code has a class" stays
  green.

---

### Escalation E-QOL-PERMSYS-004 — `/perm` smart-insertion only matches exact (what, pattern) duplicates (Step 3 → Step 4)

- **Raised by:** reviewer (Step 3 PASS carryover #3)
- **Step:** 3 → 4
- **Trigger:** Step 3's escalation guidance contemplated
  smart-insertion producing ambiguous ordering on a user's
  hand-arranged rules. The implementation (`_perm_upsert` →
  `_remove_matching_new_rule` at `cli/_slash.py:827-868`)
  replaces only exact `(what, pattern)` duplicates and otherwise
  inserts above the default-deny tail. A user typing
  `/perm allow kolsch/bash "find *"` while an existing
  `deny kolsch/bash "*"` rule sits ABOVE the catch-all lands the
  new allow rule between them — but since the broader deny
  still fires first, the allow rule is silently dead.
- **Why this is not a guess:** rule list order shown by
  `/perm list` makes the precedence visible, but a user without
  rule-precedence intuition could land a no-op rule without a
  warning. Reviewer's exact words: "the broader-shadow case
  isn't surfaced."
- **Options considered:**
  - (a) Add a shadow-detection pass: after inserting, walk the
    rules above the new one and warn when any matches a strict
    superset of the new rule's `(what, pattern)`.
  - (b) Add a one-line caveat to the `/perm` doc + `kolsch
    permissions` doc telling users to verify with `/perm list`
    after an insert.
  - (c) Both — warn AND document.

---

### Coordinator resolution — E-QOL-PERMSYS-004

- **Decided by:** reviewer (Step 3 PASS-with-carryover)
- **Decision:** option (b) for Step 4; (a) deferred unless a
  user actually trips this in practice.
- **Rationale:** shadow detection on globs is non-trivial (any
  rule with `*` is technically a superset of any sibling); the
  documentation-only fix surfaces the failure mode without
  imposing a "false-positive" warning cascade on the user's
  hand-arranged rule set.
- **Resume instruction:** Step 4 — add a one-paragraph note
  to `docs/how-to/permissions.md` ("Why didn't my /perm rule
  take effect?") pointing the user at `/perm list` to verify
  ordering. The note should call out the explicit case where a
  broad earlier deny/prompt rule shadows a more specific later
  allow. No code change.

---

## Part 6 — Out of scope (consolidated)

- Phase 3 principal-based scoping (the eventual
  replacement for the dropped `who:` field).
- Tool action namespace migration to
  `tool.<home_bundle>.<name>` — tracked at
  `qol-action-namespace-bundle-scoped.md`. This QoL
  requires the tool ref form in `what` but doesn't
  change the action namespace string the authz provider
  matches against internally.
- Modal UX changes beyond the persistence target shape.
  The `[y]/[a]/[n]/[d]` choices stay.
- "Most specific wins" rule precedence. File order
  continues to win; slash command's smart-insertion is
  the only intelligence added.
- Removing the catch-all default rule entirely. (See
  `qol-default-catchall-deny-to-prompt.md` if filed.)
- System actions in slash commands. Users edit tool
  rules via `/perm`; system action rules are
  platform-managed.
- CLI parity (`kolsch permissions allow <tool> <pattern>`
  etc.). The existing CLI command surface stays; this
  QoL adds slash equivalents but doesn't unify the two.
  A future qol-permission-cli-parity.md could.

---

## Part 7 — Related work

- **Blocks**: NONE directly, but
  `qol-tool-permission-discovery-uniform-with-stacking.md`
  Step 3 docs benefit from this QoL shipping first —
  that step's "Naming tools across bundles" subsection
  becomes easier to write against the new schema.
- **Blocked by**:
  `qol-prefix-defaults-and-unified-refs.md` (shipped at
  commit `5d744f3`) — uses its ref grammar + the
  `resolve_shorthand` helper.
- **Follow-up dependencies**:
  - `qol-action-namespace-bundle-scoped.md` — will
    migrate the action namespace from `tool.bash` to
    `tool.kolsch.bash`; the rule shape stays compatible
    because `what` already uses the qualified tool ref
    form.
- **Sibling**: `qol-tool-permission-discovery-uniform-with-stacking.md`
  ships the modal-on-no-rule path. After both QoLs ship,
  the full picture is: no rule → modal (their QoL);
  catch-all deny → distinct capsule with /perm hint
  (this QoL).
- **Modal context**:
  `bug-chat-deny-stops-and-esc-interrupts.md` is the
  Phase 2.1 work this QoL builds on top of (the modal
  framework).
- **Capsule conventions**:
  `qol-chat-capsule-layout.md` is the design surface for
  the new denied capsule's visual style.
- **Setup migration framework**:
  `qol-setup-evolution-and-defaults.md` Step 6 ships
  the per-artifact migrator pattern this QoL's
  authz-config migrator plugs into.
