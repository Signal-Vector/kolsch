---
name: review-pr-diff
description: Review a pull request or raw diff. Read the surrounding code (not just the hunks), apply the kolsch reviewer checklist, and surface findings as Critical / Should-fix / Suggestion. Includes a cross-reference to AGENTS.md hard rules.
allowed-tools:
  - read_file
  - list_files
  - bash
disable-model-invocation: false
---

# review-pr-diff

Use this skill when the user asks for a review of changes — a GitHub
PR, a `git diff` output they pasted, or "review what's on this branch
before I push." This skill encodes the kolsch review discipline so
the findings you produce read like a teammate's review, not a generic
"LGTM" or a noisy linter dump.

## When this skill applies

Trigger phrases the user might say:

- "review PR 42"
- "review this diff"
- "what's wrong with this change before I push?"
- "code review on the current branch"

When the user wants a fix-and-commit rather than a review, that's a
different shape — surface the fixes if you can, but lead with the
review so the user sees the issue independently of the proposed fix.

## Workflow

### Step 1 — Get the diff

| The user said | Bash command |
|---|---|
| "review PR 42" or a github.com/.../pull/42 URL | `gh pr diff 42` |
| "review the current branch" | `git diff main...HEAD` (or whichever base they're targeting) |
| "review what's staged" | `git diff --staged` |
| They pasted a diff into the message | Use that text directly; skip the fetch |

If `gh` is not installed and the user named a PR by number, ask them
to either install `gh` or paste the diff.

### Step 2 — Read the surrounding code, NOT just the hunks

The diff shows lines that changed. The review needs the context those
lines live in. For every non-trivial hunk:

1. Read the full function/class the hunk is inside, not just the
   diff window. The cause of a subtle bug is often three lines above
   the first `+`.
2. Read the call sites. Use `grep` (via bash) to find who calls the
   touched function. A signature change three callers haven't been
   updated for is the most common "tests pass locally but break in
   prod" failure mode.
3. Read the corresponding test file. If the change touches behaviour
   and the test file is unchanged, that's a finding.

If you're reviewing a multi-file diff, do this for each file. Don't
skim. The cost of reading is small; the cost of a missed regression
landing is large.

### Step 3 — Walk the checklist

For each hunk, run through these heads. Each one fires a finding when
it triggers; aggregate them at the end.

**Logic correctness.** Does the change actually achieve the stated
intent? Off-by-one errors, inverted conditionals, missing else
branches, silently-swallowed exceptions. The most common form: a
"fix" that handles the example case but breaks a sibling case the
reviewer would catch by reading the surrounding function.

**Test coverage.** Every behaviour change should have a corresponding
test change. If the diff adds a function with three branches and the
test file only exercises one, that's a Should-fix finding. Conversely,
if the diff changes existing behaviour and existing tests still pass
unchanged, those tests probably weren't testing the behaviour that
changed — flag it.

**Permissions / authz drift.** Any new tool registration, any new
authz rule, any change to a tool's `action_name` or its
`@tool(description=...)` — flag it. Permissions are load-bearing and
the chat's modal pattern means a sloppy change here is the
difference between "user gets prompted" and "tool runs silently."

**Schema / contract changes.** Any edit under docs/explanation/ or
process/*.md is a contract. Has the corresponding code change
landed? Has the test catalog been updated? Has the bug-ticket or
spec-amendment been referenced from the commit message? These are
the seams where production drift starts.

**Backward compatibility.** Renamed function, removed parameter,
moved class to a different module — all callers must be updated in
the same diff. If the touched API has callers in other repos (rare in
this codebase, but possible), call that out as a Critical finding.

**Docstrings + comments.** New code without a "why this exists" line
where the reader would have a hard time guessing — flag it. AGENTS.md
is explicit: "default to writing no comments" — but the corollary is
"when you do, the comment should explain WHY, not WHAT." A function
named `_validate_skill_entry` with a one-line docstring saying
"Validates a skill entry" is a Should-fix.

**Hidden constraints.** A change that introduces a new dependency on
a global (module-level variable, env var, registry singleton) or a
specific ordering (must call X before Y) needs to be load-bearingly
obvious. If it isn't, flag it.

### Step 4 — Cross-reference AGENTS.md

Read AGENTS.md before posting findings — the project pins specific
hard rules there. The current set includes:

- Tools shipped by the platform live in bundles/kolsch/tools/ as
  plain Python files. Not in the engine binary.
- The production authorization rule set must end with a default-deny
  tail.
- Agent prompts are Jinja2 templates; typo'd variables raise at
  agent-load.
- Secrets never go in `platform.yaml`.
- Every new CLI subcommand requires a smoke-test entry in
  the `_TOP_LEVEL_SUBCOMMANDS` tuple inside
  tests/component/test_cli_smoke.py.

A diff that violates any of these is a Critical finding regardless
of whether the change otherwise looks reasonable. Hard rules are
hard rules.

### Step 5 — Surface findings

Group by severity. Lead with the worst:

**Critical** — blocking; this change cannot land as-is. Hard-rule
violations, missing tests for behaviour changes, broken backward
compatibility, security regressions.

**Should-fix** — should land before merge but isn't blocking. Subtle
logic concerns the author needs to address, missing docstrings on
non-obvious surfaces, test-name-says-X-but-tests-Y mismatches.

**Suggestion** — optional; the reviewer's call. Naming nits,
opportunities to simplify, alternative implementations worth
considering. Mark these clearly so the author knows they're not
required.

For each finding, include:

- The file + line (use the diff's line numbers, not absolute).
- One sentence describing what's wrong.
- One sentence describing why it matters.
- A concrete suggested fix (when it's small) OR a question to draw
  out the author's intent (when it's a design call).

Bad finding format: "this is wrong, fix it."
Good finding format: "src/foo.py:42 — the loop swallows
`KeyError` silently. If `key` is mistyped in the call site, the
caller sees an empty result rather than an exception. Either log the
miss or re-raise; silent skip is the worst of both."

## What this skill is NOT for

- **Writing the fix yourself** — that's a different skill. Review
  first; if the user asks for the fix, switch modes.
- **Approving a PR** — that's a human judgment + a GitHub action
  outside this skill's scope. Surface findings; let the user act.
- **Style-only nits the linter already catches.** Don't comment on
  whitespace or import order — Ruff/Black handle those. If you spot
  one, run the linter and report the result instead of a comment per
  occurrence.
