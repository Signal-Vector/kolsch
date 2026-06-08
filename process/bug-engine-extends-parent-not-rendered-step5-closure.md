# bug-engine-extends-parent-not-rendered.md — Step 5 closure

**Status:** REVISE-PENDING-USER. Steps 1–4 + the Step 5
verification are functionally complete; the reviewer verdict
stays REVISE-PENDING-USER until the user (Andre) walks
`docs/how-to/extending-an-agent.md` from their own session and
confirms both that the docs read clearly AND that butler
renders correctly in their workspace.

---

## What ships in Step 5

### Code

None. Step 5 is verification + closure; no production code
changes. The only files in this step's surface are:

- `~/Documents/kolsch/bundles/andre/agents/butler.yaml` —
  restored from the stop-gap "drop-prompt" form to the
  canonical `{% extends parent %}` form per the how-to's
  "Define your child agent" section.
- `process/qol-ship-canonical-kolsch-agents-as-package-resources.md`
  (NEW) — follow-up ticket per Step 5 task 5.
- `process/qol-skills-jinja-variable-symmetric-with-tools.md`
  (NEW) — follow-up ticket per Step 5 task 5.
- `process/bug-engine-extends-parent-not-rendered-step5-closure.md`
  (NEW — this file).

### Docs revision discovered during verification

`docs/how-to/extending-an-agent.md`'s "Verify it loads"
section was inaccurate as Step 4 shipped it. Two faults
surfaced when I ran the harness verbatim against the user's
workspace:

1. The harness used `AssetManagement.build_resolved_config`
   directly (bare asset manager, no Layer-3 subsystem
   validators). For an agent declaring
   `subsystems.tools.config` (the butler inherits one from
   `kolsch/helper`), the kernel pipeline raises
   `config.invalid:unknown_variant` immediately. The
   production path (`kolsch chat`, `kolsch run`) routes
   through `PlatformRuntime.build_resolved_config`, which
   wires the validators.
2. The harness claimed `cfg.prompt` shows the rendered
   prompt. It does not — the asset-manager layer returns
   the **unrendered Jinja template**; the engine's
   `_render_prompt_if_needed` does the actual render at
   pre-flight (and overwrites `self.config.prompt` on the
   exchange's local copy of the config). The how-to needed
   to be honest about this and route the user to the
   session JSONL for the rendered output.

Per the Step 5 escalation rule ("Docs need editing to match
what the dev actually does → Step 4 inaccurate; revise Step
4"), I revised the section inline:

- The harness now uses
  `platform_runtime.bootstrap_runtime` — the same entry
  point `kolsch chat` / `kolsch run` use, so the Layer-3
  validators are wired.
- The section is explicit that `cfg.prompt` is the raw
  template and points the reader at the `config.set` event
  in the session JSONL for the rendered output.
- The "Run a chat session" section now also mentions
  `kolsch run` as the non-interactive alternative
  (load-bearing for capturing the JSONL evidence the user
  will check).

Resulting how-to length: 258 lines, 8 over the Step 4
reviewer's 150–250 ceiling. The +8 is the cost of the
correction; the alternative was leaving an inaccurate
harness in the docs. Flagged here for reviewer awareness.

---

## Verification evidence

### Task 1 — butler.yaml restored to match how-to verbatim

`~/Documents/kolsch/bundles/andre/agents/butler.yaml` now
reads:

```yaml
schema_version: "0.1"
assembly_version: "1"
agent_id: ag_butler
name: butler
description: A personal butler agent inheriting from kolsch helper.
extends: kolsch/helper
prompt: |
  {% extends parent %}
  {% block role %}a personal butler — attentive, precise, and proactive{% endblock %}
  {% block special_instructions %}
  Manage files, run commands, keep the workspace organized.
  {% endblock %}
```

This is byte-equivalent to the how-to's "Define your child
agent" example (after substituting `<your-bundle>` →
`andre`). No deviation needed.

### Task 2 — `build_resolved_config` harness output

Running the (revised) harness against the user's workspace:

```
model: provider='opencode' model='qwen3.6-plus' parameters={} fallback=[]
tools: ['kolsch/read_file', 'kolsch/write_file', 'kolsch/list_files',
        'kolsch/bash', 'kolsch/fetch_website']
context_files: ['kolsch:docs/index.md', 'kolsch:AGENTS.md']
permissions: [PermissionRule(what='tool\\.bash', pattern='*',
              effect='prompt', id='', who='.*')]
extends preserved: kolsch/helper
```

- **5 tools inherited** (the canonical 4 plus a user-added
  `kolsch/fetch_website`; the user's workspace helper.yaml
  was customised post-`kolsch setup`). The how-to states
  "four inherited tools … plus any extras you've added";
  the user's workspace exercises the "plus any extras"
  branch.
- **2 context_files inherited** — `kolsch:docs/index.md`,
  `kolsch:AGENTS.md`.
- **bash-prompt permission inherited** — the parent's
  `tool.bash` → `prompt` rule survives RFC 7386 merge.
- **`cfg.extends == "kolsch/helper"`** — the leaf's
  `extends:` value is preserved on the resolved config, per
  the Step 2 wiring at `asset_manager.py:262-269`.

### Task 3 — real chat session JSONL excerpt

Ran: `uv run kolsch run andre/butler --input
content="What tools do you have? List them."` in
`~/Documents/kolsch`. The exchange completed; session
landed at
`sessions/ses_b708877c-1e90-44d0-a7c1-00af4af3ff58.jsonl`.

Pulled the `config.set` event's `resolved_config.prompt`
field; head + special-instructions zone:

```
=== rendered system prompt (head, first 350 chars) ===
You are butler, a personal butler — attentive, precise, and proactive
running on opencode/qwen3.6-plus.

## Where you are

## Active workspace

Path: /Users/andrecohen/Documents/kolsch
You can read from: data/, bundles/, sessions/, content/, logs/, platform.yaml
You cannot access: .env, .platform/

## What you can do

- **read_file**: Read a UTF-8 text file by line range.
- **write_file**: Write a UTF-8 text file to the workspace, replacing …
- **list_files**: List the entries (files + subdirectories) under a …
- **bash**: Run a shell command in the workspace.
- **fetch_website**: Fetch a webpage and return its content as text.

=== rendered system prompt (special_instructions zone, end) ===
...
Manage files, run commands, keep the workspace organized.


Be concise. If the question is ambiguous, ask one clarifying
question before acting.
```

Acceptance-criteria walk per spec Part 3:

| Criterion | Evidence |
|---|---|
| `{{ agent.name }}` → `"butler"` | Line 1: "You are butler, …" |
| Child's `role` block content | "a personal butler — attentive, precise, and proactive" replaces parent's "a friendly general-purpose helper agent" default |
| Parent prose surrounds child blocks | "running on opencode/qwen3.6-plus", "## Where you are", "## What you can do", "Be concise…" closer all present |
| Child's `special_instructions` block content | "Manage files, run commands, keep the workspace organized." appears at the parent's `{% block special_instructions %}{% endblock %}` position |
| Tools listed in rendered prompt | All 5 tools inlined under "## What you can do" |
| Loads without error | Exchange completed; no `TypeError: no loader for this environment specified`; no `config.*` Fatals |

This is exactly the Part 0 acceptance case the bug report
opened against — and it was the case that crashed pre-Step-2.
End-to-end fix confirmed.

### Task 4 — both helper.yaml copies carry `{% block %}` placeholders

```
$ grep -n "{% block " \
    ~/Documents/kolsch/bundles/kolsch/agents/helper.yaml \
    src/cli/_starter_bundle/kolsch/agents/helper.yaml

~/Documents/kolsch/.../helper.yaml:49:  …{% block role %}a friendly general-purpose helper agent{% endblock %}
~/Documents/kolsch/.../helper.yaml:76:  {% block special_instructions %}{% endblock %}
src/cli/_starter_bundle/.../helper.yaml:47:  …{% block role %}a friendly general-purpose helper agent{% endblock %}
src/cli/_starter_bundle/.../helper.yaml:74:  {% block special_instructions %}{% endblock %}
```

Both copies have the two `{% block %}` placeholders the
child needs to override. The workspace and the starter
bundle agree.

### Corpus status

```
$ uv run pytest tests/integration/test_extends_corpus.py --no-cov -q
............................                                             [100%]
28 passed in 0.18s
```

All 28 EXT-CORPUS cases pass post-Step-2; no regressions
from the Step 4 docs touch (docs-only diff has no test
surface). The spec's Part 3 acceptance criterion #4 ("All
24 cases in the Part 4 corpus produce the expected
outcome") is satisfied — 28 cases per the enumerated table
(the "24" in the spec's totals subsection is an arithmetic
error in the spec; Step 3 flagged it).

### Full suite

```
$ uv run pytest --no-cov -q
1180 passed, 1 skipped, 4 deselected in 69.60s
```

The 1 skipped is the unrelated Postgres-backend Phase 2
work; the 4 deselected are unrelated test filters. No
regressions.

---

## Task 5 — follow-up tickets filed

Per the Step 5 dev prompt, three follow-up tickets were
named. Two are filed in this PR; the third does not apply.

1. **`process/qol-ship-canonical-kolsch-agents-as-package-resources.md`**
   (NEW). The Step 4 docs route readers away from
   `extends: kolsch:agents/...` because the prefix doesn't
   resolve (installed package ships only `docs/` +
   `AGENTS.md`). This QoL reverses that — make `kolsch:`
   reach the package's `agents/` so the form works.
2. **`process/qol-skills-jinja-variable-symmetric-with-tools.md`**
   (NEW). The asymmetry between `{{ tools }}` (Jinja
   variable) and the post-append skills listing
   (`assembly.py:83-87`) is the right ticket to track the
   symmetric fix.
3. **"Empty-context Jinja partial render"** — **not
   filed**. Step 2 picked load-all-ancestors-into-DictLoader
   (Step 1 Task 5) which sidesteps the deferred-variable
   evaluation problem entirely; the escalation condition
   ("Step 2 hit the deferred-variable-evaluation
   escalation") never fired. Per the spec's conditional,
   the ticket isn't applicable.

---

## Acceptance-criteria roll-up (spec Part 3)

| # | Criterion | Status |
|---|---|---|
| 1 | Trigger case (butler.yaml with `{% extends parent %}`) loads + renders | ✓ Task 3 — `kolsch run` exchange completed; rendered system prompt verified |
| 2 | `apply_inheritance` preserves the leaf's `extends:` | ✓ Step 2 wiring at `asset_manager.py:262-269`; verified at runtime (`cfg.extends == "kolsch/helper"`) |
| 3 | Engine dispatches correctly | ✓ Step 2 wiring at `engine.py` `_render_prompt_if_needed` (three-path dispatch: orphan / inheritance / plain); EXT-CORPUS C1-C4 + K1-K2 verify positive paths, C5 verifies orphan |
| 4 | All 28 corpus cases produce expected outcome | ✓ `pytest tests/integration/test_extends_corpus.py` → 28 passed |
| 5 | No regression on Category A / B | ✓ EXT-CORPUS A1, A2, B1-B4 pass (regression guards) |
| 6 | `config.required_block_missing` fires for D2 | ✓ EXT-CORPUS D2 asserts this code at agent-load |
| 7 | Documentation reflects the working feature | ✓ Step 4 PR + this step's revision to `extending-an-agent.md`'s "Verify it loads" section |

---

## Task 7 — user-confirmation request

@andre — please follow `docs/how-to/extending-an-agent.md`
from your own session and confirm both:

1. **The docs read clearly.** Pick parent, decide what to
   override (Patterns A/B/C tied to B1/B4/C1 fixtures),
   define the child, verify it loads via the
   `bootstrap_runtime` harness, run a chat session. If
   anything reads unclear or you have to deviate to make it
   work, that's a Step 4 bug — flag it on this PR and we
   revise the how-to.
2. **butler renders correctly.** The acceptance shape is
   in this writeup's Task 3 evidence — the rendered system
   prompt should have "You are butler, a personal butler
   — attentive, precise, and proactive" at the top, the
   inherited helper structure (`## Where you are`,
   `## What you can do`, `## Your documentation`) in the
   middle, and "Manage files, run commands, keep the
   workspace organized." then "Be concise. If the question
   is ambiguous, ask one clarifying question before
   acting." at the end. Confirm against your own
   `kolsch chat` or `kolsch run` against
   `andre/butler` (or the equivalent ref in your workspace).

Reviewer verdict stays **REVISE-PENDING-USER** until both
confirmations land. Once confirmed, verdict moves to
**PASS** and the bug closes.

---

## Files changed in Step 5

- `~/Documents/kolsch/bundles/andre/agents/butler.yaml`
  (M) — restored per how-to.
- `docs/how-to/extending-an-agent.md` (M) — Step 5-discovered
  correction to "Verify it loads" + "Run a chat session"
  sections; the harness now uses `bootstrap_runtime`, and
  the rendered-prompt source of truth is the
  `config.set` event in the session JSONL.
- `process/qol-ship-canonical-kolsch-agents-as-package-resources.md`
  (NEW) — follow-up ticket.
- `process/qol-skills-jinja-variable-symmetric-with-tools.md`
  (NEW) — follow-up ticket.
- `process/bug-engine-extends-parent-not-rendered-step5-closure.md`
  (NEW — this file).

No production code changes. No new test fixtures. No
bundle YAML changes outside the user workspace's
`butler.yaml` restoration.
