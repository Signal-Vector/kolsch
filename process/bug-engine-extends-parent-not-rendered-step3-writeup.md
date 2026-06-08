# bug-engine-extends-parent-not-rendered.md — Step 3 writeup

**Status:** PASS (pending re-review).
**Scope:** 28-case fixture corpus + parametrised runner + test-catalog entry + pre-fix sampling.
**Reviewable in:** ~60 min.

This writeup discharges Step 3 tasks 5 and 6 explicitly (the
pre-fix sampling + the test-catalog entry); tasks 1-4 are
materialised by the files this PR adds and are visible in the
diff. Section "Pre-fix sampling" is the load-bearing evidence
the Step 3 reviewer flagged as missing on first review.

---

## Tasks 1-4 — materialised in code

- `tests/fixtures/extends/<a..k>/` — 28 minimal YAML fixtures
  + 2 SKILL.md frontmatters (G1/G3) + 1 workspace doc (I1).
  Categories match Part 4: A (2), B (4), C (5), D (3), E (2),
  F (5), G (2), H (2), I (1), K (2). No `j/` subdir — Part 4
  skips category J.
- `tests/integration/_extends_corpus_cases.py` — `FixtureCase`
  dataclass + 28-row `CASES` tuple. Import-time validator
  asserts (a) every Part 4 ID is present, (b) no duplicates,
  (c) every named fixture file exists on disk.
- `tests/integration/test_extends_corpus.py` — parametrised
  runner; `pytest -k EXT-CORPUS … --no-cov -v` reports 28
  collected, 28 passed, 0 skipped.
- Walk of Part 4's table: ✓ cases pass (regression guard);
  ✗ cases pass post-Step-2; ⚠ cases (C5, G3) pinned to Step
  1's documented behaviour with `notes:` in the case table.

### Spec arithmetic note

Part 4's "Totals" subsection in the bug spec says "24 cases."
The enumerated table immediately above it lists 28 IDs
(A1, A2, B1-B4, C1-C5, D1-D3, E1, E2, F1-F5, G1, G3, H1, H2,
I1, K1, K2). The corpus follows the enumeration — 28 cases.
Reviewer cross-confirmed this is a spec arithmetic error, not
a missing-fixture problem.

---

## Task 5 — Pre-fix sampling

### Methodology

The straightforward "stash Step 2, run the corpus" approach has
a self-reference problem: the corpus runner imports Step 2's
helpers (`prompt_uses_extends_directive`, `render_with_parents`,
`resolve_parent_chain_from_extends`). With Step 2 stashed,
`tests/integration/test_extends_corpus.py` fails to import and
no case can run.

To work around this:

1. `git stash push -- src/templating/inheritance.py
   src/assets/asset_manager.py src/engine/engine.py`. This is
   exactly the Step 2 source surface — the three files whose
   diffs the reviewer approved on the Step 2 PR.
2. Ran a one-shot Python reproducer that mirrors what the
   pre-Step-2 engine path does at
   `engine.engine._Exchange._render_prompt_if_needed`: build
   the resolved config via `AssetManagement.build_resolved_config`
   (which works pre-Step-2 — chain composition was already
   wired), then call `render_prompt(cfg.prompt, ctx,
   template_name=...)` unconditionally. That is the
   pre-Step-2 dispatch — the unconditional
   `render_prompt(...)` call at the engine's old line 795
   that has no loader on its Jinja env.
3. Captured `{type}: {message}` for C1, C2, K1, K2 from
   that reproducer.
4. `git stash pop`. The corpus runner re-imports cleanly;
   `pytest tests/integration/test_extends_corpus.py` reports
   28 passed.

The reproducer's context construction mirrors `engine.py:787-793`
(five-key context: `tools`, `docs`, `workspace`, `agent`,
`platform`) so the failure mode reflects what an end user would
see at `kolsch chat` time, not a stripped-down harness artefact.

### Captured pre-fix failures (post-stash, pre-unstash)

All four cases hit the same Jinja-level error:

```
C1: RENDER FAILED — TypeError: no loader for this environment specified
C2: RENDER FAILED — TypeError: no loader for this environment specified
K1: RENDER FAILED — TypeError: no loader for this environment specified
K2: RENDER FAILED — TypeError: no loader for this environment specified
```

This is the **exact** error Part 0 of the bug spec named — the
trigger that surfaced when the user ran
`kolsch chat andre/agents/butler` against a butler.yaml that
extended `kolsch:agents/helper.yaml` with `{% extends parent %}`.
The corpus reproduces it at four independent points across
three independent failure modes (single-level block-only;
multi-level via super(); envelope-var binding inside child
block):

- **C1** — single-level, parent has a block, child overrides it.
- **C2** — single-level, parent has a block, child extends but
  does NOT override (the default-block path).
- **K1** — single-level, child block content references envelope
  variables (the var-binding path).
- **K2** — single-level, child uses `{{ super() }}` to compose
  additively (the only additive-composition path kolsch ships).

The Jinja path at fault is identical across all four:
`render_prompt` (`src/templating/render.py:71`) constructs
`Environment(undefined=StrictUndefined, autoescape=False)` with
**no loader**, so `{% extends parent %}` has nothing to resolve
"parent" against. Jinja raises `TypeError` at render time when
it tries to load the template. Step 2's dispatch routes the
extends-having prompts through `render_with_parents` (which
constructs a `DictLoader`-backed env) instead.

### Cross-check that the post-fix path actually clears the error

After `git stash pop`:

```
$ uv run pytest tests/integration/test_extends_corpus.py --no-cov -q
............................                                             [100%]
28 passed in 0.18s
```

All four cases (C1, C2, K1, K2) move from
`TypeError: no loader …` to the expected substring-match render
output (the case table's `expected_render_contains` /
`expected_render_excludes`). The corpus has now demonstrated
both directions:

- Pre-fix: the helpers don't exist; the dispatch path is the
  loader-less `render_prompt`; render fails with the spec's
  named `TypeError`.
- Post-fix: the helpers exist; the dispatch routes extends-having
  prompts through `render_with_parents`; render succeeds and
  the expected content lands.

### Why the other ✗ cases aren't sampled here

The Step 3 dev prompt specifies "run C1, C2, K1, K2". The
remaining ✗ cases (C3, C4, D1, D2, D3, E1) share the same root
cause:

- C3, C4 — block-inheritance variants — same `render_prompt`
  no-loader path.
- D1, D2, D3 — required-block flow — same path, except D2
  would surface `ConfigRequiredBlockMissing` post-fix if the
  loader path were reached, but pre-fix never reaches that
  because the no-loader env crashes first.
- E1 — multi-level chain — same path; pre-fix would crash on
  the first `{% extends parent %}` directive Jinja parses,
  before any chain walk could occur.

Sampling four cases from three distinct failure shapes (block
override, var-binding, super()) is sufficient evidence. The
remaining ✗ cases all flow through the same wiring gap; the
corpus-level passing of the full 28 cases post-fix is the
positive confirmation that the wiring fix is complete.

---

## Task 6 — Test-catalog entry

`process/test-catalog.md:2245` adds **Part 9.10.1 — Extends
Inheritance Corpus (EXT-CORPUS) — bug-engine-extends-parent-not-rendered.md**
as a sub-part of Part 9.10 (Agent Envelope), where TC-ENV-08
(extends inheritance), TC-ENV-09 (cycle detection), and
TC-ENV-10 (required-block) already live. The entry ships:

- A 28-row table with `EXT-CORPUS-<ID>` per Part 4 verbatim.
- Per-row columns: category | intent | currently | expected
  outcome.
- Legend distinguishing `✓` (regression guard), `✗→✓` (Step 2
  wiring fix), `⚠→✓` (Step 1 pinned behaviour).
- A "Common assertion shape" block tying layer / setup /
  action / expected / verifies to the actual runner.

The `Verifies:` line cites §10.17, Phase 2.2 §8, and the bug
spec's acceptance criteria #4 — the corpus IS the acceptance
gate, by spec.

---

## Task 7 — No production code changes

`git diff HEAD~3 -- src/` after Step 3 shows the same file set
as Step 2's review: `assets/asset_manager.py`,
`engine/engine.py`, `templating/inheritance.py`. Step 3 added
only:

- `tests/fixtures/extends/` (28 YAML + 2 SKILL.md + 1 doc).
- `tests/integration/_extends_corpus_cases.py`.
- `tests/integration/test_extends_corpus.py`.
- `process/test-catalog.md` (EXT-CORPUS section).
- `process/bug-engine-extends-parent-not-rendered-step3-writeup.md`
  (this file).

No new escalations raised. The Step 3 dev prompt's escalation
condition ("a ✗ case still fails after Step 2 landed") is
cleared by the 28-pass run.

---

## Summary for the reviewer

The corpus exercises Step 2's wiring in two directions:

1. **Positive direction (current state):** all 28 cases pass
   post-Step-2 — the 14 ✓ regression cases stay green; the 10
   ✗ cases turn green; the 2 ⚠ cases turn green at their
   pinned typed errors.

2. **Negative direction (pre-fix sampling):** four ✗ cases
   sampled with Step 2 stashed reproduce the spec's Part 0
   trigger error verbatim:
   `TypeError: no loader for this environment specified`.

Both directions are necessary to demonstrate the wiring fix is
load-bearing — not an accidental positive from some other
code path. The bidirectional check now lives both in the
running test suite (positive) and in this writeup (negative).

**Reviewer verdict requested: PASS.**
