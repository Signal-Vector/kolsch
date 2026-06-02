# Bug — Agents summarize tool output when asked to show it verbatim; tool descriptions lack presentation discipline

**Draft 0.1** *(investigation ticket. Holistic fix at the
tool-description layer so any agent using these tools — not just
the starter helper — gets the right behavior.)*

> Reported symptom: user asks `kolsch chat`'s helper agent to
> "show me the contents of platform.yaml". The `read_file` tool
> returns 77 KB of YAML correctly. The agent then **summarizes**
> the YAML into bullet points (Secrets, Sandbox, Authorization,
> Storage…) instead of returning the raw content. The user
> wanted to **see** the file; the agent **interpreted** it.
>
> The current four builtin tool descriptions (`read_file`,
> `write_file`, `list_files`, `bash`) describe WHAT each tool
> does, but say nothing about HOW the agent should present the
> result to the user. Combined with the helper agent's prompt
> ("answer clearly and concisely"), the model defaults to
> summarization — which is the wrong default for a file-dump
> request.
>
> A system-prompt fix on the helper agent would solve it for
> THIS agent only. The same tools will be used by other agents
> (user-authored agents, future reference agents, agents from
> other bundles) — each of which will hit the same bias unless
> the **tool descriptions themselves carry the presentation
> discipline**. This bug fixes it at the tool layer.

---

## Part 0 — Symptom & Reproduction

**Reported by:** user, session `ses_358e6f01-294c-40ff-87e8-5455f0fac8e6`, line 29 (`agent.message`).

**Reproduction:**
1. `kolsch chat` against helper agent on any model with tool support.
2. Type: "show me the contents of platform.yaml" (or any file).
3. The tool returns the file's content verbatim in
   `tool.result.data.result.value`.
4. The agent's final `agent.message` summarizes/paraphrases the
   content instead of quoting it.

**Evidence from the session:**

`tool.result` (line 26ish) carried:
```
{"value":"secrets:\n  variant: dotenv\n  config:\n    path: .env\nsandbox:\n  variant: native\n  ..."}
```

`agent.message` (line 29) replied:
> "Here's the full content of `platform.yaml` (77 KB). It's a
> platform configuration file. Here's a summary of its main
> sections:
>
> **Secrets**: Uses a `.env` file (dotenv variant) at path `.env`
> **Sandbox**: Native variant (no special sandboxing config)
> **Authorization**: Rule-based: allows everything..."

The agent had the raw content. It chose to paraphrase.

---

## Part 1 — Why a system-prompt fix is the wrong layer

The most obvious fix is tightening the helper agent's prompt:

> *"When the user asks to **see**, **show**, **print**, or
> **display** file content, return it verbatim in a code fence
> — do not summarize."*

This works for the helper agent. It does NOT work for:

- User-authored agents in `bundles/<user_bundle>/agents/`.
- Future reference agents (OpenClaw, Pi, others shipped in
  Phase 2.3).
- Agents installed from third-party bundles via git URL.
- Any agent that reuses the four builtin tools.

Each of those would hit the same bias unless their prompts
also include the verbatim discipline — and users writing
agents don't know to include it.

**The right layer is the tool description.** When the model
inspects the JSON schema for `read_file`, it sees the tool's
description. That description should encode HOW the agent
should handle the result, not just WHAT the tool does. Then
EVERY agent using `read_file` inherits the discipline.

This is how well-designed tool ecosystems work:
- **Claude Code's tool descriptions** specify both behavior
  and presentation (e.g., the `Read` tool's description names
  line-numbering and limit semantics that affect output).
- **MCP tools** are documented with explicit "use this when…"
  and "the result represents…" framing.
- **Anthropic Console tools** typically include a "when to use"
  + "when NOT to use" + result-handling guidance.

Our four tools today carry only "what the tool does" and
parameter specs. Missing: presentation, anti-cases, recovery
guidance.

---

## Part 2 — The pi.dev / Claude-style tool-description shape

Every tool's description string should answer five questions
in this order:

1. **What** the tool does (one sentence).
2. **When to use it** (user-intent triggers — "use this when
   the user asks to read / view / display a file").
3. **When NOT to use it** (anti-cases — "do NOT use this to
   load very large files; use `list_files` first to check size").
4. **How to present the result** (the missing layer — "return
   the content in a code fence verbatim; do not summarize
   unless the user explicitly asks to summarize").
5. **Parameter shape** (already covered by Pydantic schema +
   Field descriptions, no change).

Item 4 is the load-bearing addition. Items 2 and 3 are good
hygiene that also improve tool-selection (Bug 5's F4 about the
model guessing `filePath` is partially a "the model isn't sure
when to use this tool" problem; clearer "when to use" reduces
the guessing).

### Proposed descriptions

#### `read_file`

```
Read a file's full text contents and return them VERBATIM.

When to use:
- The user asks to "read", "see", "show", "display", "print",
  or "cat" a file.
- You need the file's content to answer a specific question
  about it.

When NOT to use:
- For very large files (>1MB), use `list_files` first to
  check size and warn the user before reading.
- For binary files (images, executables) — this tool is
  text-only.

Presenting the result:
- When the user asked to SEE the file, return its content
  VERBATIM inside a fenced code block (```), preserving all
  whitespace and formatting. Do NOT summarize, paraphrase,
  or interpret.
- When the user asked a SPECIFIC QUESTION about the file
  (e.g., "what model is configured?"), answer the question
  directly with a short quote of the relevant lines.
- Only summarize the file when the user EXPLICITLY asks
  ("summarize", "explain", "what does this file do").

Parameters:
- path: workspace-relative or absolute path.
```

#### `write_file`

```
Write text content to a file in the workspace (full overwrite).

When to use:
- The user asks to "write", "save", "create", or "edit" a file.
- You need to persist changes you've made to file content.

When NOT to use:
- For incremental edits to a file — this tool overwrites the
  entire file. Read first, modify in memory, then write.
- To create binary files — this tool is text-only.

Presenting the result:
- Confirm the write succeeded with ONE LINE naming the path
  and byte count. Example: "Wrote 1.2 KB to platform.yaml."
- Do NOT re-quote the content you just wrote — the user can
  read it themselves. Quoting it back wastes their context.

Parameters:
- path: workspace-relative or absolute path.
- content: the full new content as a UTF-8 string.
```

#### `list_files`

```
List the entries (files + directories) under a path.

When to use:
- The user asks "what's in this directory" or "show me the
  files in X".
- You need to discover the layout of a directory before
  reading specific files.

When NOT to use:
- To find a specific file by content — use `bash grep` or
  `read_file` directly.
- For very deep recursion — pass an explicit glob like
  `**/*.py` instead.

Presenting the result:
- Format the listing as a simple table (Name, Type, Size).
- Do NOT interpret what the contents "look like" or guess
  the project's purpose from the listing — the user can
  read it themselves.

Parameters:
- path: directory to list (defaults to ".").
- glob: filter pattern (defaults to "*").
- include_hidden: include dotfiles (defaults to false).
```

#### `bash`

```
Run a shell command in the workspace and return its output.

When to use:
- The user asks you to run a specific command.
- You need shell capabilities the other tools don't provide
  (grep, find, git, custom scripts).

When NOT to use:
- For file reads — use `read_file` (cleaner, structured
  result).
- For directory listings — use `list_files` (cleaner,
  structured result).
- For destructive operations the user didn't explicitly
  authorize — bash runs whatever you give it; double-check
  the command before invoking.

Presenting the result:
- Return the command's stdout VERBATIM inside a fenced code
  block.
- If exit_code != 0, surface stderr clearly and explain what
  failed.
- Do NOT silently summarize a long stdout — quote the
  relevant lines or warn the user it's truncated.

Parameters:
- command: the shell command to execute (single string).
- cwd: working directory (defaults to workspace root).
- timeout_seconds: kill after N seconds (defaults to 60).
```

---

## Part 3 — Acceptance Criteria

The bug is fixed when **all** of the following hold:

1. Each of the four builtin tools' descriptions (as emitted in
   the JSON schema the model sees) carries the five-question
   structure: what / when / when-not / **how to present** /
   parameters. Verifiable by reading the schema generated by
   `tool.info` introspection or by capturing a `model.request`
   event and reading its `tools` array.
2. Regression test: ask an LLM (fake or real with the new
   descriptions) to "show me the content of <file>" — agent
   returns the content verbatim in a code fence, NOT a summary.
   Asserted on the `agent.message.data.content.text` field.
3. Regression test: ask an LLM to "summarize <file>" — agent
   DOES summarize (verbatim-by-default doesn't mean
   verbatim-always; explicit summarization request still
   works).
4. Regression test: ask an LLM to write a file — agent
   responds with a one-line acknowledgment, NOT a re-quote of
   the content.
5. The four user-facing tool files (`bundles/kolsch/tools/*.py`)
   are updated to carry the new descriptions in their
   `@tool(description=...)` block.
6. No regression in existing Phase 1.x / 2.0 tests.
7. Manual verification: the original repro (asking helper to
   "show me the contents of platform.yaml") now returns
   verbatim YAML.

---

## Part 4 — Step dependency graph

```
Step A (investigate + draft the four descriptions for review)
    │
    ▼
Step B (rewrite the four tool files' descriptions)
    │
    ▼
Step C (regression tests against fake LLM with the new descriptions)
    │
    ▼
Step D (manual verification with a real LLM)
```

| Step | What lands | Reviewable in |
|---|---|---|
| **A** | Drafted descriptions for the four tools; coordinator approves wording before edits land | ~20 min |
| **B** | Updated `bundles/kolsch/tools/*.py` descriptions | ~20 min |
| **C** | Three regression tests (show verbatim / summarize when asked / no-re-quote on write) | ~30 min |
| **D** | Manual real-LLM test against the original repro | ~15 min |

Each is small; could land as one PR if coordinator prefers,
but the wording approval gate in Step A is the load-bearing
one.

---

## Step A — Draft and approve the descriptions

### Developer prompt

```
[standing instructions]

Step A — Draft the four updated tool descriptions.

Read first:
- process/bug-tool-output-summarization-bias.md, in full.
- bundles/kolsch/tools/{read_file,write_file,list_files,bash}.py
  (the current descriptions).
- process/phase2.0-tools-specs.md §1 (the spec for what
  each tool does — your "what" answer comes from here).

Tasks:

1. For each of the four tools, draft a description following
   the five-question shape from Part 2:
   - What (1 sentence)
   - When to use (intent triggers)
   - When NOT to use (anti-cases)
   - How to present the result (the missing layer)
   - Parameters (already in Field descriptions, don't restate)

   The proposed descriptions in Part 2 are a starting point;
   refine the wording but keep the five-question shape.

2. Pay attention to consistency: all four tools should use
   the same vocabulary ("the user asks to", "do NOT use this
   to", "presenting the result"). Variation creates noise in
   the model's tool-selection heuristic.

3. Post the four drafted descriptions in the PR description
   (as a comment, since no code is changing yet). Tag the
   coordinator for wording approval.

4. STOP. Do not edit bundles/kolsch/tools/*.py until
   coordinator approves the wording.

ESCALATION PATH: if a tool's "what" doesn't match the Phase
2.0 §1 spec (you'd need to clarify behavior in the description
beyond what the spec says), escalate — that's a spec change,
not a description rewrite.

Scope: zero code changes; ~150 lines of prose total across
the four descriptions.
```

### Reviewer prompt

```
[standing instructions]

Review Step A — the four drafted descriptions.

This is a wording review, not a code review.

Checks:

1. Each description follows the five-question shape (What /
   When / When-not / How-to-present / Parameters).

2. The "how to present" guidance is concrete and actionable:
   - read_file: "verbatim in a code fence" (not "appropriately").
   - write_file: "one-line acknowledgment, do NOT re-quote".
   - list_files: "as a table" (not "however looks best").
   - bash: "stdout verbatim in a code fence".

3. The "when NOT to use" is meaningful — not a token gesture.
   Each tool has at least one real anti-case.

4. Vocabulary is consistent across the four ("the user asks
   to", "presenting the result", etc.).

5. No description contradicts the Phase 2.0 §1 spec.

Verdict: APPROVE-WITH-EDITS (specify the edits) or
APPROVE-AS-IS.
```

---

## Step B — Apply the approved descriptions

### Developer prompt

```
[standing instructions]

Step B — Apply the approved descriptions to the four tool files.

Read first:
- The coordinator-approved descriptions from Step A's PR
  comment.

Tasks:

1. For each of bundles/kolsch/tools/{read_file, write_file,
   list_files, bash}.py:
   - Update the @tool(description="""...""") block with the
     approved description text.
   - Preserve the function signature, parameter Field
     descriptions, and body — only the @tool description
     string changes.

2. Run the existing tool tests to verify nothing breaks at
   the schema-generation layer. The argument-schema pin from
   the Phase 2.0 follow-up (test_declared_tool_info_carries_
   valid_json_schema) should continue to pass — it tests
   shape, not description content.

3. Inspect the JSON schema for one tool via the catalog API
   or a small inline print. Confirm the new description text
   appears in the `description` field of the schema.

Scope: ~50 lines changed across four files; zero new tests.

Acceptance: existing tests pass; the description-text edit is
isolated to the @tool blocks.
```

### Reviewer prompt

```
[standing instructions]

Review Step B — applied descriptions.

Checks:

1. Only the @tool(description=...) string changed in each
   file. Function bodies, signatures, Field descriptions
   unchanged. git diff confirms.

2. The text matches Step A's approved wording byte-for-byte
   (modulo whitespace adjustment if any).

3. Existing tests pass.

Verdict: PASS or FAIL.
```

---

## Step C — Regression tests

### Developer prompt

```
[standing instructions]

Step C — Three regression tests for the presentation
discipline.

Read first:
- The four updated tool descriptions in main.
- tests/integration/test_tool_wiring.py (the TC-TOOL-WIRE-01
  pattern from the engine-tools-wiring fix; reuse the fake LLM
  + tool_use injection).

Tasks:

1. TC-TOOL-DESC-01 — read_file verbatim discipline:
   - Agent declared with read_file.
   - Fake LLM responds with tool_use(read_file, path="X").
   - Tool returns the file content (test fixture: a short
     fake YAML).
   - Fake LLM's NEXT response is the agent's final reply.
   - Assert: the final agent.message.data.content.text
     contains the literal file content (verbatim presence,
     not paraphrase).
   - Note: this test exercises the END-TO-END flow but the
     ACTUAL behavior depends on the LLM. With a fake LLM you
     can rig the final response to be verbatim, which tests
     the WIRING but not the LLM's interpretation of the
     description. The real test is manual verification in
     Step D.
   - Pragmatic alternative: assert that the JSON schema for
     read_file (introspected via catalog.get_definition)
     contains the phrase "verbatim" in its description.
     That's a description-content pin, not a behavioral
     test. It catches a future edit that strips the
     discipline.

2. TC-TOOL-DESC-02 — write_file no-re-quote discipline:
   - Same shape; assert the JSON schema for write_file
     contains "do NOT re-quote" or equivalent in its
     description.

3. TC-TOOL-DESC-03 — list_files table-format hint:
   - Same shape; assert list_files' description contains
     "table" or equivalent.

Note: these are schema-content pins, not behavioral tests.
Behavioral tests against an LLM are flaky and expensive.
The pins catch silent regressions in the description text.

Scope: ~60 lines across the three tests.

Acceptance: three tests pass; each fails if the corresponding
discipline word is removed from the description.
```

### Reviewer prompt

```
[standing instructions]

Review Step C — regression tests.

Checks:

1. Each test asserts a SPECIFIC discipline phrase in the
   description (e.g., "verbatim", "do NOT re-quote",
   "table"). Vague assertions ("description is non-empty")
   are fails.

2. Tests load the description via the tool catalog
   (introspection path), NOT by reading the .py file
   directly. The catalog's path is what the LLM sees.

3. Each test would FAIL if the discipline phrase were
   removed. Demonstrate by stashing the description edit
   and re-running.

4. No real-LLM calls. Pure pin tests.

Verdict: PASS or FAIL.
```

---

## Step D — Manual verification

### Developer prompt

```
[standing instructions]

Step D — Manual verification against a real LLM.

NO new code; manual verification only.

Tasks:

1. Connect a real provider (Claude Opus, GPT-4o, DeepSeek
   v4 Pro — anything with native tool_use).

2. `kolsch chat` against the helper agent.

3. Run THREE prompts and capture results in the PR
   description:

   (a) "show me the contents of platform.yaml"
       — expected: verbatim YAML in a code fence.
       — failure: a summary like "Here's a summary of main
         sections…".

   (b) "summarize platform.yaml"
       — expected: a summary.
       — failure: a verbatim dump (would show the
         discipline is now over-applied).

   (c) "write 'hello' to data/note.txt"
       — expected: a one-line "Wrote N bytes to
         data/note.txt." acknowledgment.
       — failure: re-quoting the content "I wrote: hello".

4. Capture each agent.message in the PR description.

5. If (a) still summarizes, the description rewrite didn't
   sufficiently shift the model. Escalate with the new
   evidence — coordinator decides whether to (i) tighten
   the description further, (ii) add system-prompt
   reinforcement to the helper as backstop, or (iii) accept
   that some models will summarize regardless and live with
   it.

Acceptance: prompts (a), (b), (c) behave per expectations
above on at least one tool-capable model.
```

### Reviewer prompt

```
[standing instructions]

Review Step D — manual verification.

Checks:

1. PR description includes captured transcripts from at
   least one tool-capable model showing:
     (a) verbatim dump.
     (b) summary when explicitly requested.
     (c) one-line write acknowledgment.

2. If model behavior didn't change despite the description
   rewrite, the developer escalated — DO NOT accept silent
   "it almost works" framing.

3. Re-run Step C's three regression tests; still pass.

4. Re-run the full test suite. No regressions.

Verdict: PASS or FAIL with the captured transcripts
attached.
```

---

## Part 5 — How to Escalate

```markdown
### Escalation E-BUG-DESC-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** A / B / C / D
- **Trigger:** concrete observation.
- **Why this is not a guess:**
- **Options:**
- **Estimated cost:**

---

### Coordinator resolution — E-BUG-DESC-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- A tool's "what" requires clarifying behavior beyond the
  Phase 2.0 §1 spec.
- The description rewrite doesn't shift model behavior in
  manual verification.
- Different models behave inconsistently (some summarize,
  some dump).

---

## Part 6 — Coordinator Escalation Log

*(no entries yet)*

---

## Part 7 — Notes

- **This is a description fix, not a system-prompt fix.** The
  motivation is that other agents (user-authored, future
  reference agents) will use these tools too. Putting the
  discipline in the tool description gives every agent the
  benefit; putting it in the helper's system prompt would
  not.
- **Pin tests are intentionally weaker than behavioral
  tests.** We're testing the description content (what the
  LLM sees), not the LLM's interpretation of it. Real
  verification is Step D's manual smoke. Pin tests catch
  silent regression of the description text.
- **F4 (schema-discoverability for `filePath` vs `path`) is
  related but separate.** This bug fixes presentation
  bias; F4 fixes parameter-name guessing. Both improve
  tool-description quality; they don't overlap in
  implementation.
- **System prompt for the helper agent stays simple.** The
  current prompt ("answer clearly and concisely") is fine
  as a general disposition. The tools provide the specific
  presentation discipline for tool-result handling. Two
  layers; clean separation.
