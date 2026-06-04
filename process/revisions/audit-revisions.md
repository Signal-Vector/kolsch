# Phase 1 — Spec Revisions from the Audit

**Draft 0.1**

> Concrete revisions to apply to existing spec files. Each entry names
> the target spec, the section, the gap it addresses, and the exact
> text to add or change.
>
> Apply in any order. Revisions are independent.

---

## R1. Session/Run Store §3.1 — Path Correction

**Gap addressed:** G10 (the old default path `~/.<platform-name>/sessions/`
conflicts with the later decision "platform root = CWD").

**Target section:** Phase 1 — The Session/Run Store Contract, Part 3.1
"Canonical paths."

**Change:**

Replace the opening sentence:

> "The store roots all session data at a configurable base directory,
> defaulting to `~/.<platform-name>/sessions/`. Within it:"

With:

> "The store roots all session data at `<platform-root>/sessions/`,
> where `<platform-root>` is the current working directory at platform
> startup. The base path is NOT configurable in v1 — moving the platform
> installation means changing directories. Within it:"

---

## R2. Session/Run Store §2.3 — Add Content-Store API

**Gap addressed:** G11 (content store mechanism referenced by Tools spec,
Asset Management, and File Handling, but no spec specifies who writes
the files).

**Target section:** Part 2.3 "Event operations" — add a new method at the
end of the section.

**Change:**

Add a new method:

```
async def write_content(content: bytes | str) -> ContentRef
```

The store writes content to `<platform-root>/content/<hash>.bin`, where
`<hash>` is the SHA-256 hex digest of the content. Returns a
`ContentRef`:

```
ContentRef
├── hash       string    — SHA-256 hex digest
├── size       int       — byte count
└── stored_at  string    — relative path "content/<hash>.bin"
```

If the file already exists (same hash), `write_content` is idempotent —
no rewrite, returns the existing ref. (Hash collision is
probabilistically impossible.)

Called by the Agent Engine when an event's payload exceeds the inline
threshold (per Tools spec §6.4, Memory Provider §3, et al.). The engine
embeds the resulting `ContentRef` in the event before appending.

Add to conformance invariants:

**I11 (content store immutability)** — files in `content/` MUST be
written exactly once and never modified. The store MUST treat a write
to an existing hash as a no-op (already-immutable, identical content).

---

## R3. Session/Run Store §2 — Add reconstruct_from_filesystem

**Gap addressed:** Known pending revision flagged earlier; supports the
"Postgres-as-cache, filesystem-as-truth" property.

**Target section:** Part 2 — add a new subsection 2.5.

**Change:**

Add a new subsection:

### 2.5 Filesystem-Based Reconstruction

```
async def reconstruct_from_filesystem() -> ReconstructionResult
```

Rebuilds Postgres entity records from on-disk session JSONL files.
Called when:
- Postgres has been wiped but session files exist on disk.
- The user is migrating an installation.
- Recovery from accidental database loss.

Behavior:

1. Scan `<platform-root>/sessions/` for `.jsonl` files.
2. For each session file, read its `session.opened` event (the first
   line). Extract session id, agent id, created_at.
3. Check whether a Session entity record exists in Postgres for that
   id. If not, create one with reconstructed fields. UI-state fields
   (name, starred, archived) default to "the session id as name,
   not starred, not archived" — they cannot be reconstructed from the
   filesystem.
4. Scan the file for thread events. Reconstruct Thread entity records
   similarly.
5. Return a `ReconstructionResult` with counts: sessions found,
   sessions reconstructed, threads found, threads reconstructed,
   warnings (e.g., session files with no `session.opened` event).

Reconstruction is **non-destructive**: existing entity records are
NOT overwritten. If both filesystem and Postgres agree on a session's
existence, the Postgres record is left as-is.

If a session file exists but its content is malformed (corrupt JSONL,
no `session.opened`), it is skipped with a warning. v1 does not
attempt heroic recovery — that's a manual user task.

This is a periodic-or-on-demand operation, not part of the normal
`execute` path. Likely invoked via a CLI command (`kolsch
reconstruct`).

---

## R4. Agent Engine §1.3 — ExecutionContext force_skills

**Gap addressed:** G16 partial (force-invoke trigger field missing from
the engine's ExecutionContext).

**Target section:** Part 1.3 "ExecutionContext" — update the trigger
shape.

**Change:**

Replace the trigger description:

> "trigger: object — the cause of this call:
> { kind: \"user_message\", content: <typed> }
> for conversational mode;
> { kind: \"invocation\", inputs: <typed> }
> for single_shot mode"

With:

> "trigger: object — the cause of this call, one of several kinds:
>
> Conversational mode (no force-invoke):
> ```
> { kind: "user_message", content: <typed> }
> ```
>
> Conversational mode with force-invoke of one or more skills:
> ```
> {
>     kind: "user_message_with_force_skill",
>     content: <typed>,
>     force_skills: ["<bundle>/skills/<name>", ...]
> }
> ```
>
> Single-shot mode:
> ```
> { kind: "invocation", inputs: <typed> }
> ```
>
> The engine processes `force_skills` per Skills subsystem §6.2 — emit
> synthetic `tool.called`/`tool.result` for each skill's SKILL.md
> before processing the user message."

---

## R5. Agent Engine §2.1 — Add Pre-Flight Steps

**Gap addressed:** G13 (authz check), G14 (memory binding), G16 (skills
listing).

**Target section:** Part 2.1 "Pre-flight (before the framework runs)" —
expand the numbered steps.

**Change:**

Replace the current numbered list (which has 6 steps) with the expanded
version:

> 1. **Validate the resolved config** against §A's schema. The kernel
>    normally does this; the engine double-checks (defense in depth).
> 2. **Validate** that `config.assembly_version` is in
>    `capabilities().assembly_versions`. If not, refuse with a clear
>    error.
> 3. **Authorization check.** Call
>    `authz.allow(principal, f"agent.execute.{agent_id}")`. The
>    principal is `user.local` for top-level invocations,
>    `agent.<calling_agent_id>` if a workflow node or another agent
>    is triggering this execution. If denied, raise
>    `agent.unauthorized` (Fatal); emit `thread.exchange_failed`; no
>    further work.
> 4. **Open the session's JSONL file** for append. Compute the
>    thread's current leaf by reading the log (§B.4).
> 5. **Bind memory context.** Call
>    `memory.bind_session(session_id, agent_id, resolved_memory_config)`.
>    Hold the returned `MemoryContext` for the session's duration.
>    The Memory provider replays `memory.written` / `memory.deleted`
>    events from the log to rebuild its cache (per Memory contract
>    §3.3).
> 6. **Resolve a `config.set` event if needed.** If the most recent
>    `config.set` on the thread differs from the incoming `config`
>    (or there is none), emit a new `config.set` carrying the full
>    resolved config (per Materialization Rule). The new event
>    becomes part of the tree path before the exchange begins.
> 7. **Process force-invoke skills, if any.** If
>    `trigger.kind == "user_message_with_force_skill"`, for each
>    skill ref in `trigger.force_skills`:
>    - Verify the skill is in the agent's enabled list (per Skills
>      subsystem §4). If not, raise `skill.forced_not_enabled` (Fatal).
>    - Emit a synthetic `tool.called` for `read` on the SKILL.md path
>      (the principal is `system` for these synthetic calls).
>    - Read the file (subject to path policy and Authorization on
>      `tool.read`).
>    - Emit a `tool.result` carrying the SKILL.md content.
> 8. **Emit `thread.exchange_started`** with the trigger event id
>    (which is the `user.message` for conversational mode, or a
>    synthetic input event for single_shot mode — §B.7).
> 9. **Materialize the trigger** as an event on the tree:
>    - Conversational: emit `user.message` with the trigger content
>      verbatim.
>    - Single_shot: emit `user.message` (still the term used) with
>      the typed inputs verbatim.

(The original step 7 "Build the model's input using the Assembly
Procedure" moves to Part 2.2 step 7, unchanged.)

Also update Part 2.2 step 7 "Build the model's input using the
Assembly Procedure" to include:

> "After running the Assembly Procedure (§B.5) which projects the
> tree path into model-facing messages, append the **available
> skills listing** per Skills subsystem §5.1 to the assembled system
> prompt. The skills listing is appended by the engine, not declared
> in the PromptSpec."

---

## R6. Agent Engine §2.4 — Document Bus Publication

**Gap addressed:** G9 (bus publication path unspecified at engine layer).

**Target section:** Part 2.4 "Observe and record" — add at the end of
the section.

**Change:**

After the existing list of observable events ending with "Compaction
triggered:", add:

> "All emissions are appended to the JSONL file in order via
> `store.append_event(...)`. After each successful `append_event`
> returns, the engine calls `bus.publish(event)` to publish the
> same event to the Event Bus's ephemeral tier.
>
> If `bus.publish` raises (Redis unavailable, etc.), the engine logs
> a warning and continues — the bus is best-effort, the log is the
> source of truth, and a `bus.publish_failed` Diagnostic error
> doesn't affect agent execution.
>
> The two operations (append, publish) are intentionally NOT
> transactionally atomic. The log is the ground-truth record; bus
> subscribers that need guaranteed delivery use the strict-subscriber
> pattern (per Event Bus contract §5.1) against the log, not against
> the bus."

---

## R7. Agent Engine — Add Principal Derivation Subsection

**Gap addressed:** G13 partial, G17 (principal threading).

**Target section:** Add a new subsection at the end of Part 1
"Interface."

**Change:**

Add subsection 1.8:

### 1.8 Principal Derivation

The Agent Engine threads a `principal` through authorization checks
per the Authorization Provider's namespace conventions (§3.2.4 of the
Authorization spec). Rules:

- **The top-level `execute` call's principal is `user.local`** in v1
  (single-user local-first). When real multi-user support arrives in
  a later phase, this becomes `user.<id>` of the invoking user.
- **Tool invocations within an agent's execution carry the agent's
  principal: `agent.<agent_id>`.** When the model decides to call a
  tool, the platform's tool adapter checks authorization with this
  principal, not the user's. This is correct: the agent is the
  immediate actor.
- **Synthetic platform actions carry `system`.** This includes
  force-invoke `read` calls for SKILL.md files (per R5 step 7),
  reconstructive operations, and any platform-initiated maintenance.

The principal is threaded explicitly through every layer that calls
into Authorization. It is not stored on the ExecutionContext; it is
computed at each check site based on what's calling into auth.

(A future variant could make principal a first-class field on
ExecutionContext if call-site computation proves error-prone. For v1,
explicit derivation at each site is simpler.)

---

## R8. Agent Engine §2.3 — Concurrent Callback Handling Note

**Gap addressed:** G7 (concurrent callback delivery from the framework).

**Target section:** Part 2.3 "Hand to the framework" — add a note at the
end of the section.

**Change:**

After step 11, add:

> **Note on concurrent callbacks.** The framework (CrewAI) may invoke
> lifecycle callbacks (model.called, tool.dispatched, etc.) from
> different async contexts concurrently. The engine's adapter MUST
> serialize event construction per thread to avoid race conditions in
> `parent_event_id` computation.
>
> Specifically: the Session/Run Store's `append_event` is lock-
> protected (Store I2), but the engine's *callback handler* code
> that constructs events from framework data is not automatically
> serialized. The adapter must use an asyncio Lock (or equivalent)
> around event-construction-and-append for each active thread.
>
> This is engine-internal correctness work; it's not visible at the
> contract surface, but it's a real implementation obligation.

---

## R9. Asset Management — Add Platform Bootstrap Subsection

**Gap addressed:** G6 (platform bootstrap was unowned).

**Target section:** Add a new subsection at the end of Part 3
"Platform Filesystem Layout."

**Change:**

Add subsection 3.10:

### 3.10 Platform Bootstrap and platform.yaml Ownership

Asset Management is the platform-bootstrap module. At startup, in
order:

1. **Read `<platform-root>/platform.yaml`.** If absent, generate a
   default (per File Handling §4.1 for the file_access section, and
   sensible defaults for other sections — see §3.2 for the schema).
2. **Parse and validate the top-level structure.** Each section
   (`secrets`, `sandbox`, `authorization`, `event_bus`, `file_access`,
   `defaults`) is checked for required fields.
3. **Make config sections available to subsystems** via accessors:

```
class AssetManagement:
    def get_platform_config(self, section: str) -> dict
```

Subsystems request their section by name at their own initialization.
The Authorization provider calls `get_platform_config("authorization")`;
the Secrets provider calls `get_platform_config("secrets")`; etc.

This makes Asset Management the platform-bootstrap module. It owns
config loading, dispatches to subsystems by name, and provides the
discovery surface for the asset catalog.

After platform.yaml is loaded and providers are initialized, Asset
Management builds the asset catalog (per Part 4).

**Failure handling:** A malformed `platform.yaml` is a fatal startup
error. The platform refuses to start with a clear error message
pointing at the offending field.

Add to conformance invariants:

**Am-I11 (platform bootstrap ownership)** — Asset Management owns the
loading and dispatch of `platform.yaml`. No other module reads the
file directly; subsystems retrieve their config via Asset Management's
accessor.

---

## R10. Asset Management §5.3 — Clarify Read-Only Discovery

**Gap addressed:** G3 (who triggers Python imports for discovered tools).

**Target section:** Part 5.3 "Resolution to content."

**Change:**

Replace the "Tool:" bullet:

> "**Tool**: Python file path. Imported lazily (per Tools subsystem
> spec §6.1). Resolution at the asset level is just \"yes, this
> exists and here's where to find it.\""

With:

> "**Tool**: Python file path. Asset Management returns the path
> string only — Asset Management is read-only and does NOT import
> the Python file or execute its decorator. The Tools subsystem
> performs the import at engine setup time (per Tools spec §6.1),
> using the path Asset Management provides. This preserves Asset
> Management's read-only invariant (Am-I10) — discovery scans
> filesystem and parses YAML/JSON, but never executes Python."

---

## R11. Asset Management §4.3 — Skills AssetEntry Fields

**Gap addressed:** G4 (skill metadata in catalog).

**Target section:** Part 4.3 "Asset types," the `skill` bullet.

**Change:**

Replace the `skill` bullet:

> "**`skill`** — a directory in a bundle's `skills/` directory
> containing SKILL.md. (Per Skills subsystem spec.) The directory
> name is the skill name; the ref ends with the directory name."

With:

> "**`skill`** — a directory in a bundle's `skills/` directory
> containing SKILL.md. The directory name is the skill name; the
> ref ends with the directory name. At catalog build, Asset
> Management parses SKILL.md's YAML frontmatter and stores the full
> SkillMetadata in the AssetEntry (per Skills subsystem spec §3.3):
> name, description, license, compatibility, metadata, allowed_tools,
> disable_model_invocation. The skill body is NOT parsed at catalog
> build — it loads lazily via the `read` tool when needed."

---

## R12. Memory Provider §3.3 — Cache Reconstruction Via Store

**Gap addressed:** G8 (Memory's log-access path).

**Target section:** Part 3.3 "Acquisition" — second numbered step.

**Change:**

Replace step 2:

> "2. Replays `memory.written` and `memory.deleted` events from the
>    session log to rebuild the in-memory cache for this session."

With:

> "2. Replays `memory.written` and `memory.deleted` events from the
>    session log to rebuild the in-memory cache for this session.
>    The Memory provider does this by calling
>    `store.read_events(thread_id)` and filtering for memory event
>    types — NOT by reading the JSONL file directly. This preserves
>    the Session/Run Store's role as sole mediator of log access
>    (Store I8)."

---

## R13. Multiple Specs — Variant Validator Signature

**Gap addressed:** G1 (variant validator function signatures).

**Target sections:** Memory §5.1, Tools §3.4, Skills §4.2.

**Change:**

In each of the three sections, add this sentence at the start:

> "The variant's validator is a `ConfigValidator` per the Kernel
> contract — `Callable[[dict], ValidationResult]`. It receives the
> subsystem config body's dict and returns structured validation
> results. The Kernel calls it during Layer 3 validation per its
> dispatch mechanism (Kernel §4)."

---

## R14. Error Model §4 — File Handling Section

**Gap addressed:** G19 (file handling errors not in catalog).

**Target section:** Part 4 — add a new subsection 4.11.

**Change:**

Add subsection 4.11 (after Skills, before "Generic platform"):

### 4.11 File Handling

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `file_access.denied` | Diagnostic | `{path, reason, matched_rule}` | `tools.builtin.<read\|write\|edit>` |
| `file_access.path_normalization_failed` | Diagnostic | `{path, os_error}` | `tools.builtin.<read\|write\|edit>` |
| `file_access.policy_load_failed` | Fatal | `{config_section, error}` | `assets.bootstrap.load_file_policy` |

(And renumber what was 4.11 "Generic platform" to 4.12.)

---

## R15. Error Model §3 — ValidationError Note

**Gap addressed:** G2 (ValidationError shape vs. canonical envelope).

**Target section:** Part 3.1 "The PlatformError base class" — add a note
at the end of the section.

**Change:**

After the `PlatformError` code listing, add:

> **A clarification about ValidationError.** The Kernel's
> `ValidationError` (per Kernel §4.2) — with fields `layer, source,
> path, code, message` — is a *validation finding*, not a
> `PlatformError` subtype. ValidationErrors are values inside the
> `detail` field of a `ConfigValidationError` (which IS a
> PlatformError, with `code: "config.invalid"`).
>
> So the structure is:
>
> ```
> ConfigValidationError (PlatformError, code="config.invalid")
>   .detail.validation_errors: [ValidationError, ...]
>     ↑
>     ValidationError is NOT a PlatformError; it's a value inside
>     ConfigValidationError's detail.
> ```
>
> This avoids conflating "the kind of error" (validation, at some
> layer) with "the canonical platform error envelope."

---

## R16. File Handling §4.6 — Mechanism

**Gap addressed:** G18 (path policy enforcement mechanism).

**Target section:** Part 4.6 "Where the check happens" — expand.

**Change:**

After the existing paragraph in §4.6, add:

> **Mechanism.** The path policy is loaded from `platform.yaml`
> (the `file_access` section) at platform startup via Asset
> Management's platform-bootstrap flow (Asset Management §3.10).
> Asset Management exposes the policy via its `get_platform_config`
> accessor.
>
> The builtin tools (`read`, `write`, `edit`) hold a reference to the
> path policy at registration time. Each invocation calls a check
> function:
>
> ```
> def is_path_allowed(path: str) -> tuple[bool, str | None]:
>     # Normalize path (resolve .., follow symlinks).
>     # Check denied_paths; return (False, "denied: rule X").
>     # Check allowed_paths; return (True, None) if matched.
>     # Default: return (False, "default deny").
> ```
>
> The check function is pure (no side effects, no I/O beyond symlink
> resolution). Path normalization MUST resolve `..` segments and
> follow symlinks before policy evaluation, to prevent escape via
> symlink (per Fh-I4).

---

## R17. v1 Scope §3.4 — Event Bus Subscriber Note

**Gap addressed:** G12 (no internal v1 subscribers).

**Target section:** Part 3.4 "Provider Module sophistication" — add at
the end of the subsection (or as a new entry).

**Change:**

Add as a new entry near the bottom of §3.4:

> **No internal Event Bus subscribers in v1.** The Event Bus
> infrastructure is in place (Redis pub/sub, publish from the Agent
> Engine after every `append_event`), but v1 has no internal modules
> that subscribe to it. The bus is available for external consumers
> (a debug tool watching session activity, a third-party metrics
> exporter), but the v1 platform's own modules do not depend on it.
>
> Subscribers will arrive in later phases: the IDE for live updates,
> the Workflow Engine for reaction-driven workflows, the Scheduler
> for time-triggered runs. v1 ships the publish side; the subscribe
> side is mechanism without consumers.

---

## R18. Phase 1 Schemas §A.6 — PromptSpec Simplification

**Gap addressed:** Consistency with Asset Management §6.4 (the
PromptSpec was redefined to be simpler in Asset Management, but the
schema spec wasn't updated).

**Target section:** Phase 1 Schemas draft 0.4, Part A.6 "PromptSpec."

**Change:**

Replace the entire §A.6:

> "## A.6 PromptSpec
>
> ```
> PromptSpec
> └── layers   [PromptLayer]   — ordered; concatenated in order
>
> PromptLayer
> ├── id       string          — e.g. \"agents\", \"soul\", \"tools\"
> ├── source   enum            — file | inline
> ├── ref      string          — file path, when source = file
> └── content  string          — literal text, when source = inline
> ```
>
> Default preset: three layers — `agents`, `soul`, `tools` — from
> AGENTS.md / SOUL.md / TOOLS.md. Order is data."

With:

> "## A.6 PromptSpec
>
> Two shapes:
>
> **List of refs** — a list of asset refs that get loaded from disk
> and concatenated in order:
>
> ```yaml
> prompt:
>   - \"acme-research/role.md\"
>   - \"my-bundle/project-context.md\"
> ```
>
> **Single string** — inline content used directly:
>
> ```yaml
> prompt: \"You are a research assistant. Your purpose is to...\"
> ```
>
> The platform infers which shape from the YAML structure (list → list
> of refs; scalar → inline content). At Asset Management's
> pre-resolution step (per Asset Management contract §6.2), refs in a
> list are replaced with file content; the inline string passes through
> unchanged. The resolved config carries actual content, not refs.
>
> The Agent Engine, when assembling the system prompt at execution
> time, concatenates the list items (in the list-shape case) or uses
> the string directly. It then appends the available-skills listing
> (per Skills subsystem §5.1) — the skills listing is engine-appended,
> not declared in PromptSpec.
>
> There is no `id`, no per-item wrapper, no `source` discriminator.
> Composition (sharing content across multiple agents) is the user's
> authoring concern; they handle it via file references and standard
> markdown patterns."

---

## Part 19 — Status

18 revisions specified, addressing 19 gaps from the audit (some
revisions address multiple gaps). All revisions are textual changes to
existing specs; none requires re-architecting anything.

**Revisions by target spec:**

- **Session/Run Store contract**: R1, R2, R3 (three revisions)
- **Agent Engine contract**: R4, R5, R6, R7, R8 (five revisions)
- **Asset Management contract**: R9, R10, R11 (three revisions)
- **Memory Provider contract**: R12 (one revision)
- **Memory + Tools + Skills**: R13 (one revision across three specs)
- **Error Model**: R14, R15 (two revisions)
- **File Handling**: R16 (one revision)
- **v1 Scope**: R17 (one revision)
- **Phase 1 Schemas**: R18 (one revision)

Total: 18 distinct revision blocks affecting 8 different specs. All
mechanical edits, no architectural changes.

After application, the spec set is internally consistent and the test
plan's prerequisites are met.
