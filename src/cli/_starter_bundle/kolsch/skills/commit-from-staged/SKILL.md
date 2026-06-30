---
name: commit-from-staged
description: Draft a conventional-commit message from currently staged changes. Inspects the staged diff, matches the repo's existing log shape (type(scope) subject), and produces a message ready to paste into git commit -m.
allowed-tools:
  - read_file
  - bash
disable-model-invocation: false
---

# commit-from-staged

Use this skill when the user asks for help writing a commit message
for changes they've already staged. The skill matches this repo's
existing log shape so the new commit reads like a sibling, not an
outlier.

## When this skill applies

Trigger phrases the user might say:

- "draft a commit message"
- "what should I commit this as?"
- "give me a commit message for what's staged"
- "summarise these changes for a commit"

When the user has NOT staged yet (`git status` shows nothing staged,
or all changes are in the working tree), point them at `git add` or
`git add -p` first. Don't draft a message based on `git diff` of the
working tree — the message should describe what's actually about to
land.

## Workflow

1. **Confirm there's something staged.** Run `git status` via bash.
   If the staged section is empty, stop and ask the user to stage
   first.
2. **Read the staged diff.** Run `git diff --staged` (or `git diff
   --cached` — same thing). This is the source of truth for the
   message; nothing else matters.
3. **Sample the existing log.** Run `git log --oneline -10`. The
   shape you see is the shape your message MUST match. Don't invent
   a new convention; match what's there.
4. **Identify the type, scope, and verb.** Apply the format below.
5. **Draft the subject + body.** Print it inside a code fence so the
   user can copy it. Don't run `git commit` unless the user
   explicitly asks — the message is theirs to approve.

## Format

This repo uses Conventional Commits in the form:

```
type(scope): imperative subject

(optional body)
```

### Type — pick exactly one

| Type | When |
|---|---|
| `feat` | A new feature, new command, new public surface |
| `fix` | A bug fix — including a doc bug if the doc was load-bearing |
| `test` | New or changed tests, no production-code change |
| `docs` | Docs-only change (`docs/`, `process/`, README, AGENTS.md) |
| `refactor` | Restructure without behaviour change |
| `chore` | Build/CI/tooling/dep updates — no production code change |

If the diff straddles two types (a fix that also adds a test for the
fix), pick the dominant one and mention the other in the body. For
the bug-fix-with-its-own-test case, `fix` wins because the test is in
service of the fix.

### Scope — what was touched

The scope is the subsystem (or comma-separated subsystems) the diff
touched. Look at the top-level directory of the changed files. Common
scopes in this repo, lifted from the existing log:

- `engine`, `framework` — agent engine, framework adapters
- `cli`, `chat` — CLI commands, chat surface
- `assets`, `schemas`, `kernel` — config layer
- `skills`, `tools`, `memory` — subsystems
- `setup`, `permissions` — first-run + authz CLI verbs
- `docs`, `process` — documentation, spec docs

Multi-scope is fine: `fix(cli,chat): ...`. Pick the two most relevant;
don't list every file's parent dir.

### Subject — imperative, ≤ 72 chars

The subject describes what the commit DOES, not what was done.
Imperative mood, lowercase first word, no trailing period.

Bad: `fix(cli): Fixed the bug where help printed nothing.`
Good: `fix(cli): print parent help when subcommand op is missing`

If the change is tied to a bug ticket or process doc, append the
reference. The existing log does this consistently with a trailing
`per <doc>.md`:

```
fix(assets,schemas): derive agent_id from <bundle>/<name> when YAML omits it per bug-agent-id-derive-from-ref.md
```

### Body — only when "why" needs writing down

A body is OPTIONAL. Add one when the WHY isn't obvious from the
subject + diff alone. Examples that warrant a body:

- A trade-off was made between two approaches; document which and why.
- The fix is non-obvious (the cause was three call-stacks away from
  the symptom).
- A spec amendment or open question was answered as part of the
  change — mention the doc that captures the answer.

Wrap the body at 72 chars. Leave one blank line between subject and
body. Don't restate the diff — `git show` already does that.

## Examples drawn from this repo's log

```
fix(cli,chat): deny stops the exchange + Esc cancels per bug-chat-deny-stops-and-esc-interrupts.md
test(integration): TC-AGENTID-01..04 + e2e verification for bug-agent-id-derive-from-ref.md
feat(setup,permissions): migrate existing workspaces to new permission rules
docs(process): bug-agent-id-derive-from-ref.md + Step 1 verification writeup
```

Note the trailing `per <doc>.md` tag on fix commits — it lets `git
log --grep=bug-xyz` find every commit related to a ticket. Use it
when applicable.

## What this skill is NOT for

- **Authoring the change itself** — that's a different request. This
  skill describes what's already staged.
- **Squashing or rewriting history.** Drafting an interactive-rebase
  plan is a different shape; this skill produces ONE commit message
  for what's currently staged.
- **PR descriptions.** PR descriptions are longer + narrative; this
  skill stops at the commit message. If the user wants the PR
  description too, do that as a separate, follow-on step.
