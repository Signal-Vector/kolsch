# Phase 1 — The Skills Subsystem Specification

**Draft 0.1**

> Skills are loadable knowledge — instructions, examples, scripts, and
> reference material that an agent can pull into context when needed.
> Built on the Agent Skills standard (SKILL.md + supporting files,
> progressive disclosure, force invoke). The mechanism is deliberately
> spare — most of the work uses existing platform machinery.
>
> Builds on Phase 1 Schemas §A.7, the Tools subsystem spec (skill
> loading is via the `read` tool), and the Materialization Rule (§B.6
> of the schemas).

---

## Part 0 — Decisions Locked

- **Skills subsystem is agent-level**, configured in
  `AgentConfig.subsystems.skills`. v1 ships one variant: `standard`.
- **Skills follow the Agent Skills standard.** SKILL.md format with
  frontmatter (name, description, optional fields).
- **Directory-per-skill.** A skill is a directory containing SKILL.md
  and optional supporting files (scripts, references, assets).
- **Hybrid discovery.** Skill directories are scanned per bundle and in
  the user's local setup; the agent's config enables specific skills
  by namespaced reference.
- **Progressive disclosure.** Only skill descriptions enter the system
  prompt; bodies load on demand when the model (or a force-invoke)
  calls the `read` tool on the SKILL.md path.
- **Force invoke** = the platform automatically calls `read` on a
  skill's SKILL.md before the next exchange, putting the body in
  context.
- **Skills do NOT use tags.** The tag system is reserved for memory
  data. Skills are enabled by explicit namespaced reference only.
- **Skills do NOT scope the agent's tools.** The agent's tool surface
  is determined by the standard config merge; skills are additive
  context, not subtractive capability. `allowed-tools` in SKILL.md
  frontmatter is parsed but NOT enforced in v1.
- **`disable-model-invocation`** IS enforced — affects skill
  discoverability, not capability scope.
- **No separate skill authorization namespace in v1.** Skill loading is
  `tool.read` on the SKILL.md path.

---

## Part 1 — What the Subsystem Is

### 1.1 What skills are

A skill is a bundled unit of agent-usable knowledge. It can include:
- **Instructions** for how to do something complex (e.g., "how to
  perform a literature review").
- **Reference material** (data sheets, format specifications,
  templates).
- **Scripts** that the skill's instructions tell the agent to use.
- **Examples** of inputs and outputs.

The Agent Skills standard, originated by Anthropic and adopted in the
agent ecosystem, defines the SKILL.md format. v1 adopts the standard
substantively.

### 1.2 What skills are NOT

Skills are **not tools.** A tool is a callable function the model
invokes. A skill is a body of knowledge the model reads. The two
sometimes interact (a skill's instructions may reference tools the
agent uses), but they are different mechanisms.

Skills are **not memory.** Memory captures conversational context and
explicitly-stored facts within a session. Skills are static knowledge
bundled at configuration time, not dynamic context accumulated during
execution.

Skills are **not extensions.** Pi's `extensions` mechanism (programmatic
hooks into the engine loop) is a Phase 2+ exploration. Skills are pure
content — they don't execute platform code beyond being read into
context.

### 1.3 What the subsystem owns

- Skill discovery — scanning directories for SKILL.md files.
- The metadata-to-prompt projection — taking descriptions from
  frontmatter and surfacing them in the agent's system prompt.
- The force-invoke mechanism — triggering automatic skill loads on
  request.
- The `disable-model-invocation` enforcement — hiding descriptions of
  skills marked this way from the system prompt.
- The action namespace contribution (which, in v1, is empty — skill
  loading reuses `tool.read`).

### 1.4 What the subsystem doesn't own

- The actual loading mechanism — that's the `read` built-in tool,
  same as reading any other file.
- The model's decision to load a skill — that's emergent from the
  model seeing the skill's description in the prompt and choosing to
  call `read` on the path.
- Authorization of skill access — that's the existing `tool.read`
  authorization check.
- The skill content itself — that's the bundle author's content.

---

## Part 2 — Skill File Format

### 2.1 Directory layout

A skill is a directory:

```
<bundle>/skills/<skill-name>/
├── SKILL.md           — required; the canonical entry point
├── scripts/           — optional; supporting scripts
│   └── helper.py
├── references/        — optional; reference documents
│   └── format-spec.md
└── assets/            — optional; static files
    └── template.txt
```

Only `SKILL.md` is required. Supporting files are referenced from
inside `SKILL.md` as relative paths.

### 2.2 SKILL.md format

A SKILL.md file is YAML frontmatter plus Markdown body:

```markdown
---
name: literature-review
description: Conduct a structured literature review on a given topic,
  searching academic sources and synthesizing findings into a report.
license: MIT
compatibility: ">=v1.0"
metadata:
  author: "Researcher Bot"
  version: "1.2.0"
allowed-tools: ["web_search", "read", "write"]
disable-model-invocation: false
---

# Literature Review Skill

When asked to perform a literature review, follow these steps:

1. Identify 3-5 key search terms...
2. Use `web_search` to find sources...
3. ...

## References

See `references/methodology.md` for the canonical workflow.

## Scripts

A summary-extraction script is available at `scripts/extract.py`.
```

### 2.3 Frontmatter fields

**`name`** (required) — the skill's identifier. Matches `[a-z][a-z0-9_-]*`.
Used in references and in force-invoke commands.

**`description`** (required) — a 1-3 sentence description of what the
skill does and when to use it. This text appears in the system prompt as
part of the available-skills list; it is what the model uses to decide
whether to load the skill. Quality of description directly affects
whether the model recognizes when the skill applies. Trigger-rich
descriptions are best.

**`license`** (optional) — license string, for documentation purposes.

**`compatibility`** (optional) — version constraint string. The platform
may use this to refuse loading a skill that targets a different platform
version. v1 records but does not enforce.

**`metadata`** (optional) — free-form object for arbitrary skill metadata.

**`allowed-tools`** (optional) — a list of tool names. Per the Agent
Skills standard, this declares the tools the skill's instructions assume
are available. **In v1 this is parsed but NOT enforced** (see Part 5).

**`disable-model-invocation`** (optional, default false) — if true, the
skill cannot be discovered by the model. Its description is excluded
from the system prompt; it can only be loaded via force-invoke. Useful
for skills with destructive or expensive operations that should be
deliberate.

### 2.4 The body

The Markdown body is the skill's actual content — instructions for the
model. There are no schema constraints; it's content. The body is what
gets read into context when the skill is loaded.

The body may reference supporting files by relative path. When the
model wants those, it calls `read` on the resolved path. The platform
doesn't pre-load supporting files; they're loaded as needed.

---

## Part 3 — Skill Discovery

### 3.1 Discovery directories

The platform scans for skills in:

```
<bundle>/skills/         — each installed bundle's skills directory
local/skills/            — the user's local un-packaged skills
kolsch/skills/           — platform-shipped skills (live inside the
                           `kolsch` bundle — no name reservation, just
                           the same uniqueness rule as any other bundle)
```

For each `<dir>/skills/`, the platform looks for subdirectories
containing `SKILL.md`. Each such subdirectory is a discovered skill.

### 3.2 Namespaced references

A discovered skill is referenced as `<package>/skills/<skill-name>`.
Examples:
- `local/literature-review` — user's local skill.
- `acme-research/literature-review` — skill from the
  `acme-research` bundle.

References are how agent configs name skills (Part 4) and how
discovered skills are uniquely identified across bundles. The same
skill name in two different bundles is two different skills.

### 3.3 Discovery output

Discovery produces a registry:

```
SkillRegistry
└── skills: dict[ref, SkillMetadata]

SkillMetadata
├── ref                       — "local/web-scraper"
├── path                       — absolute path to SKILL.md
├── name                       — from frontmatter
├── description                — from frontmatter
├── license                    — from frontmatter (optional)
├── compatibility              — from frontmatter (optional)
├── metadata                   — from frontmatter (optional)
├── allowed_tools              — from frontmatter (optional; not enforced)
└── disable_model_invocation   — from frontmatter (default false)
```

The registry is built at platform startup. Re-scan on platform restart
(no hot reload in v1, consistent with the Tools subsystem).

---

## Part 4 — The `standard` Variant Config

The Skills subsystem's config body, in AgentConfig:

```yaml
subsystems:
  skills:
    variant: standard
    config:
      enabled:
        - ref: "local/literature-review"
        - ref: "acme-research/scholarly-search"
        - ref: "local/dangerous-cleanup"
```

### 4.1 `enabled` — list of skill references

The agent's enabled skill set. Each entry is a `ref` to a discovered
skill. Skills not in this list are invisible to the agent — they don't
appear in the system prompt and can't be force-invoked (force-invoke
checks the enabled list before triggering).

If `enabled` is empty or omitted, the agent has no skills (the
subsystem might as well be disabled — and a higher-level config layer
that overrides with `null` would disable it entirely).

### 4.2 Layer 3 validation

The `standard` variant's validator is a `ConfigValidator` per the Kernel
contract — `Callable[[dict], ValidationResult]`. It receives the
subsystem config body's dict and returns structured validation results.
The Kernel calls it during Layer 3 validation per its dispatch mechanism
(Kernel §4).

The validator checks:
- Each `enabled[].ref` resolves to a discovered skill in the registry.
- No duplicate refs in the list.

Failures produce structured `ValidationError`s with
`source: "skills.standard"`.

### 4.3 Skills do NOT have tag enablement

Unlike the memory subsystem, skills have no tag-based filter for
enablement. Skills are enabled by explicit reference only. Tags are
reserved for memory data organization; using them for skills would
overload the concept.

If a future use case justifies skill organization at a coarser level
than per-skill, a dedicated mechanism (skill collections, skill
categories) can be added. Tags is not the right name for it.

---

## Part 5 — Progressive Disclosure: How Skills Reach the Model

The interaction model that makes skills useful.

### 5.1 At agent setup

The platform constructs the agent's system prompt. As part of this:

1. For each enabled skill, retrieve its `SkillMetadata` from the
   registry.
2. Filter out skills with `disable_model_invocation: true` — these are
   invisible to the model.
3. Build the `SkillsVar` Jinja template variable
   (`templating.vars.skills`) from the remaining skills and bind it
   as `skills` in the agent's render context.

The prompt template owns placement and wording — write
`{{ skills }}` (shorthand for `{{ skills.list() }}`) or iterate
explicitly with `{% for s in skills %}…{% endfor %}`. The default
`.list()` rendering matches the pre-QoL auto-appended block
byte-identically:

```
- **literature-review** — Conduct a structured literature review on
  a given topic. Path: `local/skills/literature-review/SKILL.md`
- **scholarly-search** — Search academic databases via institution
  access. Path: `acme-research/skills/scholarly-search/SKILL.md`
```

A canonical template:

```yaml
prompt: |
  You are {{ agent.name }}.

  ## Available skills

  {{ skills }}
```

Pre-QoL behaviour (the engine unconditionally appended a
`## Available Skills` block to every rendered prompt) was removed
by `qol-skills-jinja-variable-symmetric-with-tools.md`. Agents
that declare skills but whose template omits `{{ skills }}` emit
`agent.template_warning` at load with a pointer to add the
variable. See
`docs/how-to/agent-templating.md` § `skills` for the full surface.

### 5.2 At runtime

When the model decides a skill is relevant, it calls `read` on the
SKILL.md path. The `read` tool returns the file contents (the full
SKILL.md, frontmatter and body). The body is now in the model's context
for subsequent turns.

There is no special mechanism. The same `read` tool that reads any
file reads SKILL.md files. The platform doesn't need a separate
"skill loader" — it has one (`read`) and reuses it.

### 5.3 Supporting file access

If the SKILL.md body says "see `references/format.md`," the model can
call `read` on that path (resolving relative to the SKILL.md directory).
Same mechanism.

#### Validating references and frontmatter

`kolsch skills validate` (and the advisory that a `kolsch setup`
re-run prints) lints every catalogued skill, so broken references
surface before an agent trips over them. Both surfaces share one
validator, `cli.skills.validate_skill_entry_detailed`, and the
reference rule lives in one function,
`cli._skill_refs.find_missing_skill_refs`.

**Which body tokens are references.**

1. *Candidates* are inline code spans (`` `…` `` on one line) and
   Markdown link targets (`[text](target)`, including images; any
   `#fragment` or `?query` is dropped). Lines inside fenced code
   blocks (```` ``` ```` / `~~~`) are not scanned, and a link written
   inside a code span is code, not a link.
2. One leading `./` is stripped. Link targets are percent-decoded
   for the existence check (`with%20space.md` → `with space.md`);
   the reported ref is the target as written.
3. A candidate is skipped unless it is entirely path-safe
   (`^[\w.\-+@%/]+$`). That rules out URLs with a scheme, HTML
   (`<hr/>`), placeholders (`<slug>`, `{company}`, `$VAR`), globs
   (`*.yaml`), ellipses, `~` home paths, anything with a space, and
   code such as `utils/foo()`. Absolute paths (leading `/`) and code
   spans without a `/` are skipped too.
4. *Anchor and base.*
    - A ref whose first segment is `bundles` resolves against the
      **workspace root** and must stay under `<root>/bundles`. This
      is the form a skill uses when it tells the model to run a
      script "from the workspace root", for example
      `bundles/acme/skills/report/scripts/run.py`.
    - Any other ref resolves against the **skill directory** and
      must stay under it. It is checked only when it is a link
      target, or a code span whose first segment is `scripts`,
      `references` or `assets` (the Agent Skills sub-directories) or
      names an entry that exists in the skill directory. Every other
      code span is treated as prose, such as an example path or a
      host name like `app.example.io/x`.
    - A ref that escapes its base through `..` or a symlink is
      skipped. So is a token the filesystem can't check (for
      example, a segment longer than the OS name limit).
5. Each missing ref is reported once, as `missing-ref:<ref>`, in
   first-seen order. The table view names the base it was resolved
   against: "…not found under the skill directory" or "…not found
   under the workspace root".

**Frontmatter codes.**

| Code | When | Example message |
|------|------|-----------------|
| `frontmatter-missing` | The file is empty or its first line isn't `---` | ``no YAML frontmatter; SKILL.md must start with a `---` line`` |
| `frontmatter-unparseable` | The block is never closed, isn't valid YAML, or isn't a mapping | `SKILL.md line 3, column 236: mapping values are not allowed in this context; values containing ': ' must be quoted` |

While either frontmatter code is present, `missing-name` and
`missing-description` are not reported, since they would only
restate the same problem. A well-formed but empty block (`---` then
`---`) is not a frontmatter error; it reports `missing-name` and
`missing-description`. YAML messages are reduced to one line (at most
200 characters). `--json` rows carry `ref`, `ok`, `errors` (the
stable codes) and `details` (`[{"code", "message"}]`, one entry per
code). The setup advisory prints codes only, adding the message in
parentheses after `frontmatter-*` codes. A failing `missing-ref`
still makes `kolsch skills validate` exit 1.

The detailed parse is `assets.skill_metadata.parse_skill_frontmatter_detailed`.
The catalog keeps using `parse_skill_frontmatter`, which still
returns `{}` for any unusable frontmatter. Both read `SKILL.md` as
UTF-8 with an optional byte-order mark, so a file saved with a BOM
parses normally. (Previously the catalog silently catalogued such a
file with empty metadata.)

**Known limitations.** The rule prefers silence to noise, so these
are not checked (false negatives):

- a path inside a longer command span, such as
  `` `python3 scripts/run.py` `` (the space makes it prose);
- angle-bracket link targets (`[x](<references/a.md>)`);
- reference-style links (`[x][1]` with `[1]: references/a.md`) and
  autolinks;
- a link wrapped around an image (`[![badge](i.png)](target.md)`):
  only the image is seen;
- backslash (Windows-style) paths.

A fence inside a list item indented four or more spaces is not
recognised as a fence, so code spans on its lines are scanned.

### 5.4 The Materialization Rule applies

When a skill is loaded into context, the SKILL.md body becomes part of
the agent's context for subsequent model calls. Per the Materialization
Rule (§B.6 of the schemas), this content is captured in the event log
— specifically, via the `tool.result` of the `read` call.

The Assembly Procedure, walking the tree, sees the `tool.result` with
the SKILL.md content and projects it as part of the model's context.
Replay reconstructs the same context. The skill body is in the log;
no special event type is needed.

This is the entire reason the "skills don't get a special context layer"
choice works: by funneling skill loads through `read`, they
automatically inherit the Materialization mechanism that `tool.result`
already has.

---

## Part 6 — Force Invoke

### 6.1 What it is

A mechanism to load a skill before the user's next message, ensuring
the model sees the skill body in context for the upcoming exchange.
Useful when:
- The user knows which skill applies and doesn't want to wait for the
  model to discover it.
- A skill is marked `disable_model_invocation` (the model can't choose
  it; the user must invoke).
- The user wants to combine multiple skills for an exchange.

### 6.2 Mechanism

Force-invoke is mechanically simple: the platform automatically calls
`read` on the skill's SKILL.md path before the next user message
processing.

Concretely, in an `execute_stream` call:
1. The platform identifies that a force-invoke was requested (from the
   `ExecutionContext.trigger`, see §6.4 below).
2. Before emitting `thread.exchange_started`, the platform emits a
   synthetic `tool.called` for `read` on the SKILL.md path.
3. The `read` runs (with its usual Authorization check on `tool.read`).
4. The platform emits `tool.result` with the SKILL.md content.
5. The platform proceeds with the normal exchange — emitting
   `thread.exchange_started`, the user.message, etc. The SKILL.md
   content is now on the path and will be included in the assembled
   context.

The model sees the skill body as the result of a tool call it didn't
make — exactly as if it had called `read` itself. This is fine: the
content gets to the model, the log is honest about what happened, and
the Assembly Procedure works without modification.

### 6.3 Authorization

Force-invoke uses `tool.read` as the authorization action, same as any
other read. If `tool.read` is denied, force-invoke is denied; the
attempt fails with a clear error. This is consistent with the v1
position that skills have no separate authorization namespace.

### 6.4 The trigger interface

The `ExecutionContext.trigger` (Agent Engine §1.3) carries the
information needed to force-invoke. A new trigger kind:

```
trigger:
  kind: "user_message_with_force_skill"
  content: "<the user's message>"
  force_skills: ["local/literature-review"]
```

The CLI or IDE translates `/skill:literature-review` into this trigger
shape. The list permits multiple skills to be invoked at once.

The platform validates each force-invoke against the agent's enabled
skill list. Attempting to force-invoke a skill the agent doesn't have
enabled is rejected.

### 6.5 Force invoke and `disable_model_invocation`

A skill with `disable_model_invocation: true` is invisible to the model
in the system prompt — but it CAN be force-invoked. This is the whole
point of the field: skills that should be loaded deliberately, not
opportunistically.

---

## Part 7 — `allowed-tools` in v1

The Agent Skills standard includes an `allowed-tools` frontmatter field
that declares which tools the skill assumes the agent has. The intent in
the standard is to allow skill authors to be explicit about
dependencies.

### 7.1 v1's position

**The field is parsed but NOT enforced.**

Reason: in this platform's architecture, the agent's tool surface is
determined at config-resolution time by the standard layered merge.
Skills are additive context — they cannot extend the agent's tools and
should not narrow them. A skill that declares `allowed-tools: [read,
write, bash]` does not constrain the agent to those tools while the
skill is active; the agent's full tool set is available throughout.

The field is preserved in the parsed metadata for:
- Forward compatibility with skills authored against the broader
  standard.
- Documentation — a reader of SKILL.md sees what tools the skill
  assumes.
- Possible future enforcement (a Phase 2 concern).

### 7.2 What happens if assumed tools are missing

If a skill says `allowed-tools: [web_search]` but the agent doesn't
have `web_search` enabled, v1's behavior:

- At config validation: the platform MAY emit a warning ("skill X
  assumes tool Y which is not in this agent's config"). Not an error.
- At runtime: the model loads the skill, follows its instructions,
  tries to call `web_search`, gets a "tool not found" error from the
  framework. The instruction failure is visible; the model can
  recover or report it.

This is honest failure — the discrepancy surfaces as a tool error,
which is observable. A Phase 2 enforcement layer could catch the
mismatch at validation time and refuse the config; v1 doesn't.

### 7.3 Why not enforce in v1

Enforcement requires either:
- Narrowing the agent's tool set while a skill is active (which
  creates runtime-dependent capability, contaminating what should be
  config-time fact), OR
- Refusing skills that reference missing tools (which is unnecessarily
  strict — a skill author may legitimately expect users to enable a
  superset).

Neither is clearly right for v1. The honest answer is "v1 doesn't
enforce this; the skill says what it assumes; the agent has what it
has; mismatches surface as runtime errors." Phase 2 may add an
enforcement layer if real cases justify it.

---

## Part 8 — Conformance Invariants

What any Skills subsystem implementation must honor.

### Sk-I1 — Progressive disclosure

Skill metadata (description, name, ref) MUST be projected into the
system prompt at agent setup. Skill bodies MUST NOT be loaded into the
prompt at setup — they load only via `read` tool calls.

### Sk-I2 — `disable-model-invocation` enforcement

Skills with `disable_model_invocation: true` MUST be excluded from the
prompt's available-skills list. They MUST remain force-invocable.

### Sk-I3 — `allowed-tools` is informational

The `allowed-tools` frontmatter field MUST be parsed and preserved in
the skill's metadata. It MUST NOT be enforced at config validation,
agent setup, or runtime in v1. The agent's tool surface is determined
solely by `subsystems.tools` config.

### Sk-I4 — Force invoke as `tool.read`

Force-invoke MUST be implemented as a synthetic `tool.called` /
`tool.result` pair for `read` on the SKILL.md path, prepended to the
exchange. Authorization MUST check `tool.read`, not a separate
skill-level action.

### Sk-I5 — Materialization

When a skill's body is loaded (via the model's read or via force-invoke),
the content MUST be captured verbatim in the `tool.result` event. The
Assembly Procedure on replay will use this captured content. The Skills
subsystem MUST NOT participate in event emission — the Tools subsystem
(via the `read` tool) does that.

### Sk-I6 — Enablement is explicit

Skills are enabled by explicit `ref` in the agent's `subsystems.skills.config.enabled`
list. No tag-based enablement, no auto-enablement, no glob patterns.
Each skill is listed by full namespaced ref or it is not enabled.

### Sk-I7 — Skill discovery is read-only

Discovery MUST be a read-only scan of declared directories. It MUST
NOT execute any code from skills (the body is plain content, not
script). Supporting files (scripts in `scripts/`) are NOT executed
during discovery; they are loaded only by the agent's tool calls at
runtime.

### Sk-I8 — One skill registration per ref

A discovered skill is uniquely identified by its `ref`. If two
discovery directories produce skills with the same `ref` (which
shouldn't happen given the directory structure, but possible with
misconfigured bundles), the platform raises a config error. No silent
last-write-wins.

---

## Part 9 — Open Items

1. **Hot reload of skills.** Editing a SKILL.md while the platform runs
   doesn't update the registry until restart. Like tools, v1 accepts
   the restart cost.

2. **Skill versioning.** Multiple versions of "the same" skill (same
   name, different bundles) are different skills via different refs.
   But a bundle that ships v1 and v2 of the same skill needs naming
   discipline (e.g., `literature-review-v2`). v1 doesn't address this
   beyond the explicit-ref policy.

3. **Pre-validation of skill body integrity.** §5.3 mentions
   pre-validating supporting file references at discovery time. Not
   required by the contract; flagged as a nice-to-have.

4. **`compatibility` enforcement.** §2.3 notes the field is recorded
   but not enforced. When platform-version-checking matters (a skill
   that requires a Phase 2 feature being loaded into a v1 platform),
   enforcement should be added. Not v1.

5. **Skill loading metrics / observability.** "Which skills get loaded
   most often" is a real telemetry question for the IDE-later.
   Producible from the event log (count `tool.called` for `read` with
   SKILL.md paths) but no dedicated facility in v1.

6. **The synthetic `tool.called` from force-invoke.** §6.2's mechanism
   produces a `tool.called` event that the model didn't request. This
   is mildly unusual — most `tool.called` events are model-initiated.
   The data on the event identifies it as a force-invoke (origin
   metadata). Worth being explicit in the event spec; flagged for
   schema follow-up.

7. **Skill-to-skill reference.** A skill might reference another skill
   ("see skill `local/web-search` for usage"). Currently this
   is just text in the body; the model would have to recognize it and
   call `read` on the other skill. No special mechanism in v1; flagged
   for the IDE-later when skill graphs might be visualized.
