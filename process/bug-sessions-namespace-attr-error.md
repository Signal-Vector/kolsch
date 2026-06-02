# Bug — `kolsch sessions` (no subcommand) crashes with `AttributeError`, plus systemic CLI-surface coverage gap

**Draft 0.1** *(investigation ticket — addresses a specific crash AND
the broader testing gap it surfaced)*

> Reported symptom: running `kolsch sessions` with no subcommand
> crashes with `AttributeError: 'Namespace' object has no attribute
> 'starred'`. The user's broader observation: "most user
> interactions with kolsch have not been tested even with mocking."
>
> The immediate fix is one line. The systemic fix is a CLI-surface
> smoke-test pass that catches this class of bug across every
> command and subcommand. Both belong in the same PR — the smoke
> tests are what prevent the next regression.
>
> Developer + reviewer + coordinator sections per the established
> pattern.

---

## Part 0 — Symptom & Reproduction

**Reported by:** user, on `main`.

**Trace (verbatim):**

```
$ uv run kolsch sessions
Traceback (most recent call last):
  File ".../bin/kolsch", line 10, in <module>
    sys.exit(main())
  File ".../src/cli/main.py", line 76, in main
    return asyncio.run(handler(args))
  ...
  File ".../src/cli/main.py", line 418, in _cmd_sessions
    return await _cmd_sessions_list(args)
  File ".../src/cli/main.py", line 363, in _cmd_sessions_list
    starred=(args.starred or None),
             ^^^^^^^^^^^^
AttributeError: 'Namespace' object has no attribute 'starred'
```

**Reproduction:** `uv run kolsch sessions` in an initialized
workspace. The crash is deterministic.

**What's happening:** the `sessions` subparser has its own
sub-subparsers (`list`, `events`, `update`, `delete`). When the
user types `kolsch sessions` with no operation, argparse sets
`args.sessions_command = None`. The dispatcher
(`_cmd_sessions`) treats `None` as "default to list":

```python
async def _cmd_sessions(args):
    if args.sessions_command == "list" or args.sessions_command is None:
        return await _cmd_sessions_list(args)
    ...
```

But `_cmd_sessions_list` reads `args.starred`, `args.archived`,
`args.limit` — flags that were only added to the *`list`*
subparser. When the user didn't type `list`, those attributes
don't exist on the namespace. Crash.

---

## Part 1 — The Two Layers

### Layer A — The immediate bug

`src/cli/main.py:293–302` (`_cmd_sessions`) falls through to
`_cmd_sessions_list` when no subcommand was given. That fall-through
is the wrong behavior — the right behavior is either:

1. **Print help and exit non-zero** — matches Unix CLI convention
   ("when you type a subcommand with no operation, show available
   operations"). Exit code 1 (generic).
2. **Default to `list` with default flag values** — also reasonable
   but more surprising. If chosen, the dispatcher must construct a
   Namespace with all `list`-subparser defaults populated.

Either fix is a few lines. Option 1 is the cleaner UX and the
recommended choice.

### Layer B — The systemic test-coverage gap

The user's broader point: this class of bug (attribute missing,
subcommand dispatch wrong, flag combination unexercised) is the
kind a comprehensive CLI smoke-test pass would catch immediately.
Today's test catalog has TC-CLI-01..05 covering `kolsch run`
happy paths plus streaming — but doesn't enumerate the full CLI
surface:

| Command | Happy-path test? | Edge-case tests? |
|---|---|---|
| `kolsch run <agent>` | ✓ (TC-CLI-01..02) | partial |
| `kolsch run --stream` | ✓ (TC-CLI-02) | none |
| `kolsch sessions` (no op) | **MISSING** | **MISSING** |
| `kolsch sessions list` | partial | none |
| `kolsch sessions list --starred` | none | none |
| `kolsch sessions list --archived` | none | none |
| `kolsch sessions list --limit N` | none | none |
| `kolsch sessions events <id>` | none | none (missing id?) |
| `kolsch sessions update <id> --star` | none | none |
| `kolsch sessions delete <id>` | none | none |
| `kolsch reconstruct` | partial | none |
| `kolsch connect <provider>` (Phase 1.1) | ? | ? |
| `kolsch disconnect <provider>` | none | none |
| `kolsch providers` | none | none |
| `kolsch refresh` | none | none |
| `kolsch setup` (Phase 1.2) | partial | partial |
| `kolsch server` | none | none |
| `kolsch --help` / `--version` | none | none |
| `kolsch --root <PATH>` precedence | partial (TC-UX-01..03) | none |

The systemic fix is a new test layer:
**TC-CLI-SMOKE-*** — one test per command/subcommand,
asserting the CLI doesn't crash on default invocation, returns a
sensible exit code, and produces non-empty stdout or stderr.
These are not "did the feature work" tests — they're "did the
parser-to-dispatcher wiring work for every reachable entry
point" tests.

The smoke-test pass would have caught the `args.starred` bug
without anyone needing to remember to test the no-subcommand
case — `kolsch sessions` would crash on a fresh
`subprocess.run`, and the test would fail.

---

## Part 2 — Hypotheses (for Layer A)

### H1. `_cmd_sessions` falls through to list when sessions_command is None

Confirmed by reading the code. The dispatcher's `or args.sessions_command is None` clause is the bug.

### H2. Other subcommand dispatchers may have the same bug

Look at the other multi-subcommand surfaces: `permissions`
(Phase 2.0 — not yet shipped), `connect/disconnect/providers/
refresh` (top-level, not nested, probably fine). If `permissions`
ships with the same pattern, the same bug recurs. Worth checking
preemptively even though Phase 2.0 isn't merged yet.

### H3. The unattributed-args pattern shows up in other handlers too

`_cmd_run` reads `args.stream`, `args.session`, `args.input`
— all flags on the `run` subparser, so they're always present.
That's the right pattern (a handler only touches attributes its
subparser defined). The bug is the cross-subparser dispatch in
`_cmd_sessions`. Audit other handlers for similar accidents.

---

## Part 3 — Acceptance Criteria

The bug is fixed when **all** of the following hold:

### Layer A — the immediate crash

1. `kolsch sessions` (no op) does not crash. It prints help text
   for the `sessions` subcommand naming the four operations
   (`list`, `events`, `update`, `delete`) and exits non-zero
   (recommend exit code 1).
2. `kolsch sessions list` works (the default operation that the
   bug was incorrectly synthesizing) and gets full default flag
   handling — the user explicitly typing `list` should still
   work.

### Layer B — the systemic gap

3. A new test module `tests/component/test_cli_smoke.py` (or
   equivalent) ships TC-CLI-SMOKE-01..N covering at minimum:
     - Every top-level subcommand invoked with `--help`. Assert
       exit 0, non-empty stdout, no traceback.
     - Every top-level subcommand invoked with NO arguments
       (where it has subcommands of its own — `sessions`,
       `permissions` if Phase 2.0 lands first). Assert exit
       non-zero, help-style stdout/stderr, NO Python traceback.
     - Every top-level subcommand invoked with the minimum
       required args + nothing else (e.g., `kolsch run agent_ref`).
       Assert deterministic behavior — pass against a fake-LLM
       fixture if it would otherwise hit network.
     - `kolsch --help` and `kolsch --version` if/when version
       exists. Assert exit 0, non-empty stdout.
4. The smoke tests run via subprocess (`subprocess.run([sys.executable,
   "-m", "kolsch", ...], ...)`), not in-process. Reason: in-process
   testing of argparse misses real bugs in argparse-to-dispatcher
   wiring, sys.exit handling, and stderr formatting. The smoke
   layer's whole point is "actually run the CLI."
5. The smoke tests are fast (<10s total) by using a tmp workspace
   pre-initialized via a fixture, the fake LLM with deterministic
   responses, and `--non-interactive` modes wherever they exist.
6. Regression test for THIS bug specifically: `TC-CLI-SMOKE-NN`
   runs `kolsch sessions` against an initialized tmp workspace and
   asserts exit code != 0 AND no `AttributeError` in stderr.
7. The catalog (`process/test-catalog.md`) gains a new Part
   9.X documenting TC-CLI-SMOKE-* and the policy: every new CLI
   command or subcommand added in any future phase MUST come with
   a smoke test, full stop.

---

## Part 4 — Out of Scope

- **Refactoring the CLI dispatcher.** The bug is one line. If
  the investigation reveals the dispatcher needs structural
  redesign, escalate — don't restructure under a bug-fix label.
- **Replacing argparse with click/typer.** Off-topic, not the
  bug.
- **Auditing every CLI handler for ergonomics.** Layer B is
  about *coverage*, not *quality*. The smoke tests catch crashes;
  they don't audit whether the help text is well-worded.
- **Phase 1.1 / 1.2 / 1.3 / 2.0 spec changes.** This bug fix
  doesn't revise prior specs; it adds tests.

---

## Part 5 — Developer Prompt

```
[standing instructions]

You are fixing a CLI bug (`kolsch sessions` with no subcommand
crashes) AND closing a systemic gap (no smoke-test layer covers
the CLI surface).

Read first:
- process/bug-sessions-namespace-attr-error.md (this
  ticket — Parts 1, 2, 3 are your contract).
- src/cli/main.py (the dispatcher).
- tests/component/test_cli.py (existing TC-CLI-* tests for
  reference; the smoke layer is new but should fit alongside).

WORK IN TWO PHASES — investigation, then fix.

Phase A — Investigation (no code changes).

1. Confirm H1: read _cmd_sessions and _cmd_sessions_list. Note
   the line where the bad fall-through happens.

2. Audit per H3: read EVERY _cmd_* handler. For each, list which
   attributes on `args` it touches. Confirm each touched attribute
   is defined on the corresponding subparser. If any other handler
   has the same cross-subparser-access bug, flag it for the same
   fix.

3. Sketch the smoke-test design before writing code. For each
   top-level subcommand, list:
     - The minimal invocation that should pass (e.g.,
       `kolsch --help`).
     - The minimal invocation that should fail non-zero without
       crashing (e.g., `kolsch sessions` with no op).
     - Any fixture setup needed (tmp workspace, fake-LLM
       injection).

4. STOP. Surface the audit + smoke-test design before writing
   the fix. If the audit reveals MULTIPLE cross-subparser bugs
   beyond H1, escalate the scope decision — fixing many at once
   may need coordinator sign-off.

Phase B — Fix (after coordinator "proceed" or unambiguous
single-bug confirmation).

5. Layer A fix in src/cli/main.py:

   Option chosen: print help and exit non-zero when
   sessions_command is None. Implementation:

       async def _cmd_sessions(args):
           if args.sessions_command is None:
               # Print the sessions-subparser's help text;
               # easiest via stashing the parser reference at
               # build time, OR by re-invoking argparse to print
               # `sessions --help`-equivalent output to stderr.
               <print help to stderr>
               return 1
           if args.sessions_command == "list":
               return await _cmd_sessions_list(args)
           ...

   Apply the same pattern to any OTHER cross-subparser bugs
   found in the audit (if scope was approved).

6. Layer B — create tests/component/test_cli_smoke.py with
   TC-CLI-SMOKE-01..N per acceptance criterion 3. Use
   subprocess.run, not in-process invocation:

       def test_smoke_sessions_no_op(tmp_workspace):
           result = subprocess.run(
               [sys.executable, "-m", "kolsch", "sessions"],
               env={**os.environ, "KOLSCH_WORKSPACE": str(tmp_workspace)},
               capture_output=True, text=True,
           )
           assert result.returncode != 0
           assert "Traceback" not in result.stderr
           assert "list" in (result.stdout + result.stderr).lower()
           # the help should mention available operations

   Create a `tmp_workspace` fixture that initializes a workspace
   non-interactively (via `kolsch setup --non-interactive --root
   <tmp>`) with a fake-LLM-only configuration so no network is
   touched.

7. Tighten any handler that accesses attributes its subparser
   didn't define, per H3 audit findings. Two acceptable patterns:
     - getattr(args, "starred", False) — defensive, easy patch.
     - Refactor: each subparser handler reads only its own
       subparser's attributes; the dispatcher chooses the right
       handler before any attribute access.

8. Add the new test-catalog section per acceptance criterion 7.
   process/test-catalog.md gains a Part 9.X documenting
   TC-CLI-SMOKE-* and stating the policy: every new CLI command
   or subcommand requires a smoke test.

9. Run the full test suite. Acceptance criterion: zero regressions
   + the new smoke tests pass.

ESCALATION PATH: if the H3 audit finds multiple cross-subparser
bugs beyond H1, append an entry to the Coordinator Escalation Log
at the bottom of this doc using the template in §"How to escalate."
Wait for resolution before fixing more than the one named bug.

Commit: fix(cli,test): handle subcommand-without-op via help-and-
exit; add CLI smoke-test layer.

Report:
- The H3 audit findings.
- The fix scope (one bug or multiple).
- The smoke-test inventory (commands covered).
- Any escalations and resolutions.
```

---

## Part 6 — Reviewer Prompt

```
[standing instructions]

Review the sessions-namespace bug fix AND the new CLI smoke-test
layer.

Read first:
- process/bug-sessions-namespace-attr-error.md (the ticket).
- The developer's H3 audit findings in the PR description.
- The escalation log at the end of this ticket.
- The diff.

Architectural / correctness review:

1. The H3 audit was carried out. Confirm the PR description lists
   every CLI handler and the attributes it touches. If the audit
   wasn't done, fail and request it — half-fixing this means the
   next user finds the next cross-subparser bug.

2. The chosen fix (help + exit non-zero on missing subcommand)
   is consistent across `sessions` AND any other multi-subcommand
   top-level surface (e.g., `permissions` if Phase 2.0 landed
   first). Confirm the pattern is uniform — don't ship one
   handler that prints help and another that defaults to a
   silent fall-through.

3. The smoke tests use subprocess.run, NOT in-process. Read the
   test file. Any test that calls `cli.main(...)` directly is
   not a smoke test — fail and request subprocess invocation.

4. Smoke tests use a fake-LLM-only workspace. No real network
   calls. Verify the fixture's `kolsch setup --non-interactive`
   invocation succeeds without provider connection.

5. The test-catalog Part 9.X exists and states the policy
   ("every new CLI command requires a smoke test"). Confirm the
   policy is also added as a Hard Rule in AGENTS.md — without
   that, the policy decays. If AGENTS.md wasn't updated, fail
   and request it.

Functional review:

6. Reproduce the original bug on pre-fix code: git stash the
   fix, run `uv run kolsch sessions` against an initialized
   workspace, confirm the AttributeError crash. Restore the
   fix and confirm `kolsch sessions` now exits non-zero with
   help text and NO traceback.

7. Run every smoke test. For each, manually confirm:
     - Exit code matches the acceptance criterion's expectation.
     - stderr has NO Python traceback.
     - stdout/stderr contains the expected hint or help text.

8. Coverage map sanity check. Open the smoke test file and
   the test-catalog Part 9.X. Confirm:
     - Every command from the table in this ticket's Part 1
       Layer B has at least one smoke entry.
     - Commands that don't exist yet (e.g., `permissions` if
       Phase 2.0 hasn't merged) are noted as "to be added when
       the command ships" — NOT silently omitted.

9. Performance sanity check. Run the smoke suite in isolation.
   It should complete in under 10s on a developer's laptop. If
   it takes a minute, the subprocess overhead × N commands is
   too high — recommend running it in parallel via
   pytest-xdist, or reducing fixture setup cost.

10. Run the FULL test suite. Zero regressions. The TC-CLI-* tests
    from Phase 1 should still pass — they were happy-path tests
    and remain valuable.

The "this stops the next regression" review:

11. The new AGENTS.md hard rule about CLI smoke tests. Read it.
    Could a future contributor implement a new CLI subcommand
    and skip the smoke test? If the rule is too soft ("smoke
    tests recommended"), fail and request it be tightened
    ("smoke tests required; PR blocked without one").

12. The smoke fixtures should be reusable. Open the fixture
    file. Could the next contributor write a new smoke test in
    under 5 minutes by composing existing fixtures? If the
    fixture is bespoke per-test, the rule from item 11 will
    decay because writing the next smoke test is too expensive.

Verdict: PASS or FAIL with specific defects citing file + spec
section, plus any unresolved escalations.
```

---

## Part 7 — How to Escalate (developer + reviewer)

Either agent escalates by appending an entry to the "Coordinator
Escalation Log" at the bottom of this doc using this template.
After appending, STOP work on the affected item.

```markdown
### Escalation E-BUG-SESSIONS-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Phase:** investigation / fix / review
- **Affected item:** acceptance criterion N, or a specific handler.
- **Trigger:** concrete observation with file/line citations.
- **Why this is not a guess:** what you tried; why the spec
  doesn't cover this.
- **Options as you see them:** 2–3 paths forward with trade-offs.
- **Estimated cost:** rough effort.

---

### Coordinator resolution — E-BUG-SESSIONS-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale (one sentence):**
- **Resume instruction:**
```

When to escalate:
- H3 audit finds 3+ cross-subparser bugs beyond H1. The "tighten
  the one handler" scope no longer fits; coordinator decides on
  scope.
- A subcommand is so broken that the smoke test can't even be
  written deterministically (e.g., the underlying call requires
  real provider credentials with no fake-LLM substitute).
- Adding the AGENTS.md hard rule would conflict with an existing
  rule.

When NOT to escalate:
- Exact help-text wording. Pick a reasonable string; the test
  asserts on substrings.
- Whether the smoke test runs in parallel. Optimization, not
  correctness.

---

## Coordinator Escalation Log

*(no entries yet)*

---

## Part 8 — Notes on Running This Investigation

- **The systemic fix matters more than the immediate fix.** The
  one-line bug is easy; the test layer is what prevents the next
  ten of these. If the developer ships only the one-line fix and
  drops the smoke-test layer, fail the review.
- **The user's framing is right.** "Most user interactions with
  kolsch have not been tested even with mocking" is true. The
  test catalog focuses on success paths and golden event-log
  outputs; the surface-area of "does the CLI even dispatch
  correctly" is undertested. This patch closes that gap by
  policy AND by infrastructure.
- **Smoke tests are cheap to maintain.** Each one is ~5 lines.
  The fixture cost is the upfront investment; once it exists,
  adding the next test is trivial. That's why the AGENTS.md
  hard rule is non-negotiable — when the cost of compliance is
  low, the rule sticks.
- **Don't conflate smoke tests with feature tests.** Smoke tests
  prove "the CLI dispatched without crashing." Feature tests
  (TC-CLI-01..05 and friends) prove "the command actually does
  what it claims." Both layers need to exist; this patch adds
  the missing layer.
- **The fixture is the leverage.** A `tmp_workspace` fixture
  that produces a fake-LLM-only workspace via
  `kolsch setup --non-interactive` is reusable across every
  smoke test. Spend the upfront cost there.
