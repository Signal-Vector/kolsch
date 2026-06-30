# Phase 1 — Module API Reference

**Draft 0.1**

> The gold-standard reference for every public API that one Phase 1
> module exposes to another. Each method: signature, semantics,
> preconditions, postconditions, raises. Cross-references to the
> originating spec for deeper context.
>
> When implementations diverge from the existing specs, this document
> is the arbiter for "what does method X actually do."
>
> Scope: runtime APIs only. Configuration schemas remain specified in
> the originating specs and the schema document.

---

## Part 0 — Conventions

### 0.1 Async

All I/O-touching methods are `async`. Pure functions (validators,
resolvers, schema operations) are sync. The convention follows naturally
from each module's nature.

### 0.2 Identifiers

All entity identifiers use prefixed UUIDs per the schema convention:

- `ses_<uuid>` — session
- `th_<uuid>` — thread
- `ev_<uuid>` — event
- `ag_<id>` — agent (id is user-chosen, validated)
- `cl_<uuid>` — agent call
- `mem_<uuid>` — memory item

Types in Python:

```python
SessionId = NewType("SessionId", str)
ThreadId = NewType("ThreadId", str)
EventId = NewType("EventId", str)
AgentId = NewType("AgentId", str)
MemoryItemId = NewType("MemoryItemId", str)
```

These are runtime-validated for the prefix at construction time.

### 0.3 Error handling

Every method may raise `PlatformError` subclasses per the Error Model
spec. Specific raises documented per method. Non-PlatformError exceptions
escaping a public method are contract violations.

The error catalog (Error Model §4) is normative. Each method below names
the specific error types it may raise; implementations MUST use those
exact codes.

### 0.4 Return values vs. raises

Where a method has a structured "not found" or "denied" case, it may
return a result object (e.g., `AuthorizationResult`) rather than raise.
This is documented per method. Default: missing/denied raises;
result-typed exceptions are explicit.

### 0.5 Common imports

Throughout this document, assume:

```python
from typing import AsyncIterator, Callable, Optional
from datetime import datetime
from pydantic import BaseModel
```

Each module's specific types are imported in its section.

---

## Part 1 — Kernel / Resolver

**Role:** Pure library for config validation and JSON Merge Patch
resolution. Zero I/O, zero state.

**Originating spec:** `phase1-kernel-resolver-contract-draft-0.1.md`

### 1.1 Public API

```python
def validate_partial(
    partial: dict,
    layer: int,
    source: str,
) -> ValidationResult:
    """Validate one config partial against the structural schema."""
```

**Preconditions:**
- `partial` is a dict (not None, not a primitive).
- `layer` is 1, 2, or 3.
- `source` is a non-empty string identifying the partial's origin.

**Postconditions:**
- Returns `ValidationResult` with `ok: bool` and `errors: list[ValidationError]`.
- All structural issues found are in `errors` (not just the first).
- Validation is pure — no I/O, no side effects.

**Raises:** Never. Internal exceptions are caught and converted to
`ValidationError` entries with code `validator_internal_error`.

---

```python
def resolve(
    partials: list[dict],
) -> dict:
    """Merge partials via JSON Merge Patch (RFC 7386), left-to-right."""
```

**Preconditions:**
- `partials` is a list of dicts.
- Each dict has been individually structurally valid (caller's
  responsibility to validate first if needed).

**Postconditions:**
- Returns a single dict that is the RFC 7386 merge of all partials.
- Determinism: same input list produces byte-identical output across
  invocations.
- `null` values delete keys per the RFC.
- Absent keys inherit from earlier partials.

**Raises:** `config.merge_failed` if internal merge fails (should not
happen for valid inputs).

---

```python
def resolve_and_validate(
    partials: list[dict],
    subsystem_validators: dict[str, ConfigValidator],
    system_validators: list[ConfigValidator],
) -> ResolvedAgentConfig:
    """Full three-layer resolution: Layer 1 → resolve → Layer 1 → Layer 2 → Layer 3."""
```

**Where:**

```python
ConfigValidator = Callable[[dict], ValidationResult]
```

**Preconditions:**
- `partials` is the ordered list to merge.
- `subsystem_validators` maps subsystem names to their Layer 3 validator.
- `system_validators` is the list of Layer 2 validators.

**Postconditions:**
- Returns a `ResolvedAgentConfig` (Pydantic model) when all layers pass.
- All validation errors aggregated across all layers; failure means all
  errors are surfaced together, not just the first.

**Raises:** `config.invalid` (Fatal) with `detail.validation_errors`
containing all `ValidationError` entries from all failed layers. The
exception's `__cause__` is None — this isn't wrapping a deeper cause,
it IS the validation error.

---

```python
def export_json_schema() -> dict:
    """Export the AgentConfig schema as JSON Schema for editor support."""
```

**Postconditions:**
- Returns a parseable JSON Schema dict matching the current Pydantic
  models.
- Pure — same models produce same schema.

**Raises:** Never (unless models are themselves malformed, which is a
programmer error).

### 1.2 Common types

```python
class ValidationError(BaseModel):
    layer: int              # 1, 2, or 3
    source: str             # validator source identifier
    path: str               # JSON Pointer to the offending field
    code: str               # machine-readable error code
    message: str            # human-readable

class ValidationResult(BaseModel):
    ok: bool
    errors: list[ValidationError]
```

Note: `ValidationError` here is the kernel's internal finding shape,
NOT a `PlatformError` subtype. See Error Model §3.1 clarification.

---

## Part 2 — Asset Management

**Role:** Filesystem discovery, asset catalog, platform bootstrap,
reference resolution, layer assembly for the resolver.

**Originating spec:** `phase1-asset-management-contract-draft-0.1.md`

### 2.1 Public API

```python
async def initialize() -> None:
    """Bootstrap the platform: load platform.yaml, build asset catalog."""
```

**Preconditions:**
- Current working directory is the intended platform root.
- `platform.yaml` exists at platform root, OR will be auto-generated
  with defaults.

**Postconditions:**
- `platform.yaml` loaded; its sections accessible via
  `get_platform_config`.
- Asset catalog built; subsequent calls to `get_asset` and
  `resolve_ref` work.
- Eager MCP servers (if declared in any agent's config) NOT started here
  — that happens at engine setup. Asset Management is read-only.

**Raises:**
- `config.invalid` if `platform.yaml` is malformed.
- `assets.duplicate_bundle_name` if two bundles share a name.
- `assets.bundle_name_mismatch` if a bundle's manifest name disagrees
  with its directory.

---

```python
def get_platform_config(section: str) -> dict:
    """Retrieve a section of platform.yaml after initialization."""
```

**Preconditions:**
- `initialize()` has completed successfully.
- `section` is one of: `"secrets"`, `"sandbox"`, `"authorization"`,
  `"event_bus"`, `"file_access"`, `"defaults"`.

**Postconditions:**
- Returns the dict subtree for that section.
- Idempotent — multiple calls return the same content (snapshots from
  initialize-time load).

**Raises:** `KeyError` if `section` is unknown. (Not a `PlatformError`
— this is a programmer error, not a runtime condition.)

---

```python
async def get_asset(ref: str) -> AssetEntry:
    """Look up an asset by its full namespaced ref."""
```

**Preconditions:**
- `ref` is a full ref of form `<bundle>/<asset-type>/<name>`.
- `initialize()` has completed.

**Postconditions:**
- Returns the matching `AssetEntry` with path and metadata.
- For skills: includes parsed SkillMetadata (per Asset Mgmt §4.3).
- For tools: includes path; Python file NOT imported by Asset Mgmt.

**Raises:**
- `assets.not_found` if the ref doesn't match any catalog entry.
- `assets.invalid_ref` if the ref doesn't parse as `<bundle>/<type>/<name>`.

---

```python
async def resolve_ref(
    ref: str,
    asset_type: str,
) -> str:
    """Resolve a shorthand or full ref to a full ref."""
```

**Preconditions:**
- `ref` is either a full ref or a bare name.
- `asset_type` is one of `"tool"`, `"skill"`, `"agent"`, `"prompt"`.

**Postconditions:**
- If `ref` contains `/`, returns it unchanged (after verifying it
  resolves to a real asset).
- If `ref` has no `/` and exactly one asset of the given type has that
  name, returns the full ref.
- If multiple matches, raises with all candidates listed.

**Raises:**
- `assets.not_found` if no asset matches.
- `assets.ambiguous_shorthand` if multiple assets match.

---

```python
async def build_resolved_config(
    agent_ref: str,
    call_overrides: dict | None = None,
) -> ResolvedAgentConfig:
    """Build the three-layer resolved config for an agent."""
```

**Preconditions:**
- `agent_ref` resolves to a registered agent.
- `call_overrides` is an optional dict of one-time runtime overrides.
- Kernel is available (this method internally calls `resolve_and_validate`).

**Postconditions:**
- Returns a fully-resolved, validated `ResolvedAgentConfig`.
- PromptSpec refs in the config are PRE-RESOLVED to inline content (Asset
  Mgmt Am-I4); tool and skill refs PASS THROUGH (Am-I5).
- The Kernel's three-layer validation has run.

**Raises:**
- `assets.not_found` if agent_ref doesn't resolve.
- `config.invalid` from the kernel if validation fails.
- `assets.prompt_load_failed` if a referenced prompt file can't be read.

---

```python
def list_bundles() -> list[BundleManifest]:
    """List all installed bundles (after initialize)."""
```

**Preconditions:** `initialize()` complete.

**Postconditions:** Returns a snapshot list of all bundles' manifests.

**Raises:** Never.

---

```python
def list_assets(asset_type: str | None = None) -> list[AssetEntry]:
    """List all assets, optionally filtered by type."""
```

**Preconditions:** `initialize()` complete. `asset_type` if provided
must be one of `"tool"`, `"skill"`, `"agent"`, `"prompt"`.

**Postconditions:** Returns a snapshot list of asset entries.

**Raises:** Never.

### 2.2 Common types

```python
class BundleManifest(BaseModel):
    name: str
    version: str
    description: str
    required_env_vars: list[str]
    dependencies: list[str]
    path: str  # absolute path to bundle directory

class AssetEntry(BaseModel):
    ref: str               # full namespaced ref
    asset_type: str        # "tool" | "skill" | "agent" | "prompt"
    bundle: str            # bundle name
    path: str              # absolute path to asset file/directory
    metadata: dict         # type-specific metadata (e.g., SkillMetadata)
```

---

## Part 3 — Session/Run Store

**Role:** Owner of sessions, threads, JSONL event logs, and the content
sidecar store.

**Originating spec:** `phase1-session-run-store-contract-draft-0.1.md`

> **Backend note.** The API below is backend-agnostic. The entity store
> has two implementations selected via `platform.yaml`'s
> `storage.entity_backend`: **SQLite** (default, zero external services)
> and **Postgres** (scale-up). No method signature changes between
> backends; only the storage engine differs. The filesystem JSONL event
> log is ground truth regardless of backend.

### 3.1 Public API

```python
async def create_session(
    agent_id: AgentId,
    name: str | None = None,
) -> Session:
    """Create a new session entity, allocate JSONL file, emit session.opened."""
```

**Preconditions:**
- `agent_id` is a valid identifier (validated at construction).
- `name` is optional; defaults to the session id.

**Postconditions:**
- Postgres entity record created.
- JSONL file at `<platform-root>/sessions/<session_id>.jsonl` created.
- `session.opened` event written as first line.
- Returns `Session` with assigned id.

**Raises:**
- `store.write_failed` if filesystem write fails.

---

```python
async def get_session(session_id: SessionId) -> Session:
    """Look up a session entity record."""
```

**Raises:**
- `session.not_found` if no record exists.

---

```python
async def update_session(
    session_id: SessionId,
    name: str | None = None,
    starred: bool | None = None,
    archived: bool | None = None,
) -> Session:
    """Mutate UI-state fields on the entity record. Does NOT touch the log."""
```

**Preconditions:** Session exists.

**Postconditions:** Entity record updated; JSONL file untouched.

**Raises:** `session.not_found`.

---

```python
async def delete_session(session_id: SessionId) -> None:
    """Permanently delete session: entity record + JSONL file + lock file."""
```

**Postconditions:** Both Postgres record and on-disk JSONL gone.
Idempotent on already-deleted.

**Raises:** Never (deletion of nonexistent is a no-op).

---

```python
async def get_thread(thread_id: ThreadId) -> Thread:
    """Look up a thread entity record."""
```

**Raises:** `thread.not_found`.

---

```python
async def get_leaf(thread_id: ThreadId) -> Event | None:
    """Return the most recent event in the thread, or None for fresh threads."""
```

**Preconditions:** Thread exists OR is being initialized (caller's
responsibility — engine handles both cases).

**Postconditions:**
- Returns the event whose `event_id` matches the tail of the thread's
  events.
- Returns None if no events for this thread.
- Pure read — does NOT modify state.

**Raises:** `store.read_failed` on I/O error.

---

```python
async def append_event(
    thread_id: ThreadId,
    event: Event,
) -> Event:
    """Atomically append an event to the thread's log. Returns the appended event with parent_event_id populated."""
```

**Preconditions:**
- `thread_id` references a thread whose session has an open JSONL file.
- `event` is a fresh event (no `event_id`, no `parent_event_id`, no
  `created_at`).
- Caller holds no lock on the JSONL file (this method acquires it).

**Postconditions:**
- `event.event_id` populated with a fresh `ev_<uuid>`.
- `event.parent_event_id` populated with the current leaf's id (or
  None for first event).
- `event.created_at` populated.
- File-locked write to JSONL.
- After return, `get_leaf(thread_id)` returns this event.

**Atomicity:**
- Lock + leaf-compute + append are atomic per Store I2.
- Cross-process lock (via `flock`) serializes appends from multiple
  processes.

**Raises:**
- `store.lock_timeout` if the file lock cannot be acquired.
- `store.write_failed` on filesystem error.

---

```python
async def read_events(
    thread_id: ThreadId,
) -> AsyncIterator[Event]:
    """Yield events for a thread in append (file) order."""
```

**Postconditions:**
- Events yielded in file order (which equals append order, which equals
  creation order for Phase 1's single-thread sessions).
- Lazy iteration: large logs don't load fully into memory.
- Reads are NOT serialized against concurrent appends — caller may see
  partial state if a write is in progress. (For consistent reads, use
  `read_path_to_leaf` after a known stable state.)

**Raises:**
- `store.corrupt_log` if a JSONL line fails to parse, with line number.
- `session.not_found` if no JSONL file exists.

---

```python
async def read_path_to_leaf(
    thread_id: ThreadId,
) -> AsyncIterator[Event]:
    """Yield events on the path from root to current leaf, in chronological order."""
```

**Postconditions:**
- For Phase 1 (single-thread, no branching): same as `read_events`.
- For Phase 2+ (branching): yields only events on the active path.
- Yields in chronological order (root first).

**Raises:** Same as `read_events`.

---

```python
async def write_content(content: bytes | str) -> ContentRef:
    """Write oversized event content to the content store. Returns a reference."""
```

**Preconditions:**
- `content` is bytes or str; will be encoded as UTF-8 if str.

**Postconditions:**
- File at `<platform-root>/content/<hash>.bin` exists with the content
  (where `<hash>` is the SHA-256 hex digest).
- Idempotent — writing identical content returns the existing ref;
  no rewrite.
- Returns `ContentRef` with hash, size, stored_at path.

**Raises:**
- `store.write_failed` on filesystem error.

---

```python
async def reconstruct_from_filesystem() -> ReconstructionResult:
    """Rebuild Postgres entity records from on-disk JSONL files."""
```

**Preconditions:**
- `<platform-root>/sessions/` exists.
- Postgres connection is available.

**Postconditions:**
- For each JSONL with a `session.opened` event: Session entity exists
  in Postgres (created if missing; existing NOT overwritten).
- Threads similarly reconstructed.
- Returns counts of: sessions found, sessions reconstructed, threads
  found, threads reconstructed, plus warnings list.

**Raises:**
- `store.read_failed` for individual file errors are converted to
  warnings (not raised). A failure to read the sessions directory
  itself is raised.

### 3.2 Common types

```python
class Session(BaseModel):
    session_id: SessionId
    agent_id: AgentId
    name: str
    starred: bool
    archived: bool
    created_at: datetime
    updated_at: datetime

class Thread(BaseModel):
    thread_id: ThreadId
    session_id: SessionId
    created_at: datetime

class Event(BaseModel):
    event_id: EventId
    parent_event_id: EventId | None
    thread_id: ThreadId
    session_id: SessionId
    type: str               # e.g., "tool.called", "model.request"
    data: dict              # type-specific payload
    actor: dict             # who emitted; {kind: "system"|"agent"|"user", id?: str}
    created_at: datetime

class ContentRef(BaseModel):
    hash: str               # SHA-256 hex digest
    size: int               # byte count
    stored_at: str          # "content/<hash>.bin"

class ReconstructionResult(BaseModel):
    sessions_found: int
    sessions_reconstructed: int
    threads_found: int
    threads_reconstructed: int
    warnings: list[str]
```

---

## Part 4 — Event Bus

**Role:** Ephemeral notification of events to subscribers. Best-effort
delivery via the configured backend.

**Originating spec:** `phase1-event-bus-contract-draft-0.1.md`

> **Backend note.** The API below is backend-agnostic. The bus has two
> implementations selected via `platform.yaml`'s `event_bus.backend`:
> **in_process** (default, an asyncio pub/sub, zero external services)
> and **redis** (cross-process scale-up). No method signature changes
> between backends.

### 4.1 Public API

```python
async def publish(event: Event) -> None:
    """Publish an event to the bus for any subscribers matching its topic."""
```

**Preconditions:**
- `event` is a fully-formed `Event` (id, parent, type, data populated).
- Caller is the Agent Engine (per Phase 1 — no other publishers).

**Postconditions:**
- Event published to Redis on a topic derived from `event.type` and
  metadata (per Bus §2).
- Best-effort: a Redis outage does NOT raise; logs a warning and
  returns normally.

**Raises:**
- `bus.publish_failed` ONLY if the bus is configured to fail-loud
  (off by default in v1). Normally never raises.

---

```python
async def subscribe(
    topic_pattern: str,
    handler: Callable[[Event], Awaitable[None]],
    filters: dict | None = None,
) -> SubscriptionId:
    """Register an async handler for events matching the topic pattern."""
```

**Preconditions:**
- `topic_pattern` is a string matching the topic conventions: exact
  (`"tool.failed"`), prefix wildcard (`"tool.*"`), or full wildcard
  (`"*"`).
- `handler` is an async callable that takes one Event.
- `filters` is an optional dict of attribute filters (per Bus §2.3),
  e.g., `{"thread_id": "th_abc"}`.

**Postconditions:**
- Handler will be invoked for every published event matching pattern
  AND filters.
- Returns a `SubscriptionId` usable to unsubscribe.
- Failure isolation: a raising handler does NOT prevent delivery to
  other subscribers and does NOT remove the failing handler.

**Raises:** `bus.unavailable` if Redis is unreachable at subscribe time.

---

```python
async def unsubscribe(subscription_id: SubscriptionId) -> None:
    """Remove a previously-registered subscription."""
```

**Postconditions:**
- Handler no longer invoked.
- Idempotent on already-removed.

**Raises:** Never.

### 4.2 Common types

```python
SubscriptionId = NewType("SubscriptionId", str)
```

---

## Part 5 — Memory Provider

**Role:** Per-session memory operations. Variants are pluggable; v1
ships `recent_buffer`.

**Originating spec:** `phase1-memory-provider-contract-draft-0.1.md`

### 5.1 Public API

```python
async def bind_session(
    session_id: SessionId,
    agent_id: AgentId,
    config: dict,
) -> MemoryContext:
    """Create a session-bound memory context. Rebuilds cache from log."""
```

**Preconditions:**
- `session_id` references an existing session.
- `agent_id` matches the session's agent.
- `config` is the resolved memory subsystem config body (variant
  config).
- Session/Run Store is available (for log reads).

**Postconditions:**
- Returns a `MemoryContext` bound to this session/agent for its lifetime.
- Variant has consulted `store.read_events(thread_id)` and rebuilt any
  in-memory cache.
- Subsequent context operations are O(1) wrt log size for the
  `recent_buffer` variant.

**Raises:**
- `memory.session_unbound` would be raised if session doesn't exist,
  but this method is the binding — `session.not_found` propagates from
  the store.

---

**`MemoryContext` methods:**

```python
class MemoryContext:
    async def write(
        self,
        content: str,
        metadata: dict | None = None,
    ) -> MemoryItem:
        """Write a memory item, tagged with the configured write_tag."""
```

**Preconditions:**
- The bound config has a `write_tag` (singular, no wildcards).
- `content` is a string.

**Postconditions:**
- New `MemoryItem` created with the bound `write_tag`.
- Memory cache updated.
- (The engine, having called this, separately emits `memory.written`
  event — Memory provider does NOT emit events itself, per Memory I1.)

**Raises:** `memory.write_disabled` if no `write_tag` configured.

---

```python
async def retrieve(
    self,
    query: str | None = None,
    limit: int | None = None,
) -> RetrievalResult:
    """Return memory items + recent events blended per the variant's policy."""
```

**Preconditions:** Context is bound.

**Postconditions:**
- Returns a `RetrievalResult` with items matching the bound
  `read_tags` (tag-filtered) and recent events from the buffer.
- For `recent_buffer`: items matching read_tags + last N exchanges from
  the session log.
- Item content is captured verbatim — the engine, when using these
  results, emits `memory.retrieved` events with the actual content.

**Raises:** Never under normal operation.

---

```python
async def list(
    self,
    filters: dict | None = None,
) -> list[MemoryItem]:
    """Return memory items matching filters, without ranking or limit."""
```

**Postconditions:**
- Returns all items matching `filters` (tag, timestamp, etc.).
- For inspection/management, not for retrieval into context.

**Raises:** Never under normal operation.

---

```python
async def delete(self, item_id: MemoryItemId) -> None:
    """Remove a memory item. Engine separately emits memory.deleted event."""
```

**Raises:** `memory.item_not_found`.

---

```python
async def compact(
    self,
    events: list[Event],
    target_tokens: int,
) -> CompactionResult:
    """Produce a summary of the given events fitting target_tokens."""
```

**Preconditions:**
- `events` is a list of events to be summarized.
- `target_tokens` is a positive integer.

**Postconditions:**
- Returns `CompactionResult` with the summary text and metadata.
- Internally may call a model (cost recorded in result).
- Idempotent on identical input (modulo model nondeterminism).

**Raises:** `memory.compaction_failed` (Recoverable) on model error.

### 5.2 Common types

```python
class MemoryItem(BaseModel):
    item_id: MemoryItemId
    content: str
    tags: list[str]
    metadata: dict
    created_at: datetime

class RetrievalResult(BaseModel):
    items: list[MemoryItem]
    recent_events: list[Event]
    total_tokens: int

class CompactionResult(BaseModel):
    summary: str
    original_token_count: int
    summary_token_count: int
    cost: float | None
```

---

## Part 6 — Secrets Provider

**Role:** Resolve secret values for tool registration and MCP server
spawning.

**Originating spec:** `phase1-secrets-sandbox-authz-contract-draft-0.1.md` §1

### 6.1 Public API

```python
async def resolve(name: str) -> str:
    """Return the secret value, or raise if unavailable."""
```

**Preconditions:**
- `name` is a non-empty string identifying a secret.
- Provider has been initialized (loaded `.env` for `dotenv` variant).

**Postconditions:**
- Returns the secret value as a string.
- The value is NEVER logged or written to events.
- Pure resolution — no network call for `dotenv` variant.

**Raises:** `secret.not_found` (Fatal) if the secret is not available.

---

```python
async def exists(name: str) -> bool:
    """Check whether a secret is available without resolving it."""
```

**Postconditions:**
- Returns True if `resolve(name)` would succeed.
- The check itself does not log.

**Raises:** Never under normal operation.

---

```python
async def list_names() -> list[str]:
    """Return the names of all available secrets (NOT their values)."""
```

**Postconditions:**
- Returns a list of secret names.
- Values are NOT returned.

**Raises:** Never under normal operation.

---

## Part 7 — Sandbox Provider

**Role:** Wrap tool execution. v1 `native` variant is pass-through; future
variants enforce isolation.

**Originating spec:** `phase1-secrets-sandbox-authz-contract-draft-0.1.md` §2

### 7.1 Public API

```python
async def execute(
    work: Callable[[], Awaitable[T]],
    policy: dict,
) -> T:
    """Execute `work` under the sandbox policy. Returns work's result."""
```

**Preconditions:**
- `work` is an async callable that takes no arguments.
- `policy` is the sandbox config dict from the agent's config.

**Postconditions:**
- Returns whatever `work()` returns (`native` variant: pass-through).
- Exceptions from `work` propagate unchanged.
- (Future variants will impose timeouts, resource limits, isolation.)

**Raises:** Same as `work` raises; plus `sandbox.policy_invalid` if
the policy is malformed.

---

## Part 8 — Authorization Provider

**Role:** Allow/deny decisions per principal and action.

**Originating spec:** `phase1-secrets-sandbox-authz-contract-draft-0.1.md` §3

### 8.1 Public API

```python
async def allow(
    principal: str,
    action: str,
) -> AuthorizationResult:
    """Evaluate whether the principal may perform the action."""
```

**Preconditions:**
- `principal` is a string in the namespace conventions:
  `user.<id>`, `agent.<id>`, or `system`.
- `action` is an action identifier. For tools the canonical form is
  the 3-segment catalog ref (e.g. `kolsch/tools/bash`,
  `kolsch/tools/read_file`); for system actions, the dotted form
  (e.g. `agent.execute.ag_x`); for MCP, `mcp.<server>.<tool>`.

**Postconditions:**
- Returns `AuthorizationResult` with `allowed: bool`, `matched_rule:
  Rule | None`, and `reason: str | None`.
- First-match-wins: the first rule matching both principal and action
  determines the result.
- Default-deny if no rule matches.

**Raises:** Never under normal operation. (Configuration errors —
malformed rules, invalid regexes — raise `authz.malformed_rule` or
`authz.regex_invalid` at provider initialization, not at `allow`-time.)

### 8.2 Common types

```python
class Rule(BaseModel):
    who: str                # regex (fullmatch) for principal
    what: str               # regex (fullmatch) for action
    effect: str             # "allow" | "deny"

class AuthorizationResult(BaseModel):
    allowed: bool
    matched_rule: Rule | None
    reason: str | None
```

---

## Part 9 — Tools Subsystem

**Role:** Manage the catalog of declared tools, MCP servers, and built-in
tools. Provides the adapter the Agent Engine uses to register tools with
the framework.

**Originating spec:** `phase1-tools-subsystem-draft-0.1.md`

### 9.1 Public API

```python
async def register_tools_for_agent(
    config: ResolvedAgentConfig,
    framework_registry: FrameworkRegistry,
) -> ToolRegistration:
    """Register all tools (builtin, declared, MCP) with the framework."""
```

**Preconditions:**
- `config` carries the resolved tools subsystem config.
- `framework_registry` is the framework-specific (CrewAI) tool
  registration handle.
- Asset Management has been initialized (for declared tool paths).

**Postconditions:**
- Each builtin tool from `config.subsystems.tools.config.builtin` is
  registered.
- Each declared tool from `config.subsystems.tools.config.declared` is
  loaded (Python import happens here, NOT in Asset Mgmt) and registered.
- Each MCP server: eager servers started now; lazy ones registered as
  placeholders.
- Returns `ToolRegistration` with the registered tools list and any
  warnings.

**Raises:**
- `tool.not_found` if a declared ref doesn't resolve.
- `tool.setup_failed` if a tool's `@tool` decorator parse fails, with
  underlying cause in `__cause__`.
- `secret.not_found` (propagated, possibly wrapped) if a tool's
  `secrets_needed` is unmet.

---

```python
async def invoke_tool(
    tool_name: str,
    args: dict,
    context: ToolContext,
) -> ToolResult:
    """Invoke a registered tool by name with validated args."""
```

**Preconditions:**
- `tool_name` matches a registered tool.
- `args` is a dict (will be validated against the tool's args_schema).
- `context` is a populated `ToolContext`.

**Postconditions:**
- Dispatch order (per Tools §6.2):
  1. Validate `args` against the tool's Pydantic args_schema.
  2. Authorize via `authz.allow(principal, defn.action_name)` —
     `action_name` resolves to `f"{home_bundle}/tools/{name}"`
     (3-segment catalog ref) for engine-driven registrations.
  3. Resolve secrets if the tool declares any.
  4. Route through `sandbox.execute(...)`.
  5. Execute the tool function.
- Returns `ToolResult` with the tool's return value, or raises.

**Raises:**
- `tool.invalid_arguments` (Diagnostic) on args validation failure.
- `tool.unauthorized` (Diagnostic) on authorization deny.
- `tool.execution_failed` (Diagnostic) on tool function exception.
- `tool.timeout` (Diagnostic) on execution timeout.
- `mcp.upstream_error` (Diagnostic) for MCP tool failures.

---

```python
def list_registered_tools(agent_id: AgentId) -> list[ToolInfo]:
    """Return info on tools currently registered for this agent."""
```

**Postconditions:** Returns metadata only (name, description,
args_schema). No code references.

**Raises:** Never.

### 9.2 Common types

```python
class ToolContext(BaseModel):
    agent_id: AgentId
    session_id: SessionId
    thread_id: ThreadId
    principal: str          # "agent.<id>" for tool calls
    secrets: dict[str, str] # resolved values for this tool's needs

class ToolResult(BaseModel):
    result: any             # JSON-serializable return value
    metadata: dict          # latency, cost if applicable

class ToolInfo(BaseModel):
    name: str
    description: str
    args_schema: dict       # JSON Schema
```

---

## Part 10 — Skills Subsystem

**Role:** Manage skill discovery and surface available skills to the
agent (in the system prompt) for progressive disclosure.

**Originating spec:** `phase1-skills-subsystem-draft-0.1.md`

### 10.1 Public API

```python
async def get_enabled_skills(
    config: ResolvedAgentConfig,
) -> list[EnabledSkill]:
    """Return the list of skills enabled for this agent."""
```

**Preconditions:**
- `config` carries the resolved skills subsystem config.
- Asset Management has been initialized.

**Postconditions:**
- Returns `EnabledSkill` records for each ref in
  `config.subsystems.skills.config.enabled`.
- Each record includes the SKILL.md metadata (parsed at catalog build
  per Asset Mgmt §4.3) but NOT the body.
- Skills with `disable-model-invocation: true` are still included in
  this list (the engine filters them out of the system-prompt listing).

**Raises:**
- `skill.not_found` if any enabled ref doesn't resolve.

---

```python
def format_skills_listing(
    skills: list[EnabledSkill],
) -> str:
    """Format the skills listing block for inclusion in the system prompt."""
```

**Preconditions:** `skills` from `get_enabled_skills`.

**Postconditions:**
- Returns a string suitable for appending to the system prompt
  (per Skills §5.1).
- Skills with `disable-model-invocation: true` are EXCLUDED from the
  output.
- Returns empty string if no eligible skills.

**Raises:** Never.

---

```python
async def force_invoke(
    skill_ref: str,
    config: ResolvedAgentConfig,
) -> SkillInvocation:
    """Prepare a force-invoke of a skill — returns the synthetic call data."""
```

**Preconditions:**
- `skill_ref` is in `config.subsystems.skills.config.enabled`.
- Asset Management has the skill catalogued.

**Postconditions:**
- Returns `SkillInvocation` with the SKILL.md path the engine should
  pass to a synthetic `read` tool call.
- Does NOT emit events itself; the engine is responsible (per Skills
  §6.2).

**Raises:** `skill.forced_not_enabled` if the ref isn't in enabled list.

### 10.2 Common types

```python
class EnabledSkill(BaseModel):
    ref: str
    name: str
    description: str
    license: str | None
    compatibility: str | None
    metadata: dict
    allowed_tools: list[str]
    disable_model_invocation: bool

class SkillInvocation(BaseModel):
    ref: str
    skill_md_path: str       # absolute path to the SKILL.md to read
```

---

## Part 11 — Agent Engine

**Role:** Wrap CrewAI to execute one agent's exchange. The recorder
that emits events while the framework drives.

**Originating spec:** `phase1-agent-engine-contract-draft-0.1.md`

### 11.1 Public API

```python
async def execute(
    config: ResolvedAgentConfig,
    context: ExecutionContext,
) -> Event:
    """Execute one exchange. Returns the final agent.message event."""
```

**Preconditions:**
- `config` is a fully resolved, validated agent config.
- `context` carries thread_id, trigger, and optional cancellation token.

**Postconditions:**
- Pre-flight: authz check, memory bind, config.set emission,
  force-invoke handling, exchange_started emission, trigger
  materialization.
- Framework setup: context assembly + skills listing append, tool
  registration, secret resolution.
- Hand to framework: CrewAI runs the loop, engine observes via
  lifecycle hooks.
- Each observed event: `store.append_event` then `bus.publish`.
- On success: emit `agent.message` then `thread.exchange_completed`;
  return the agent.message event.
- On Fatal-level failure: emit `thread.exchange_failed`; raise
  `AgentExecutionError` carrying the failed event.

**Raises:**
- `agent.unauthorized` (Fatal) per pre-flight.
- `agent.capability_mismatch` (Fatal) per pre-flight.
- `model.retry_exhausted` (Fatal) after retries exhausted.
- `exchange.cancelled` (Fatal) on cancellation.
- `exchange.timeout` (Fatal) on timeout.
- `skill.forced_not_enabled` (Fatal) per pre-flight.
- `AgentExecutionError` wrapping any of the above with the failed event
  attached.

---

```python
def execute_stream(
    config: ResolvedAgentConfig,
    context: ExecutionContext,
) -> AsyncIterator[Event]:
    """Streaming counterpart of execute. Yields events as they are emitted."""
```

**Preconditions:** Same as `execute`.

**Postconditions:**
- Yields every event the engine emits, in order.
- Final event is `thread.exchange_completed` (success) or
  `thread.exchange_failed` (failure).
- Does NOT raise on Fatal errors — the failed event is the final
  yielded item; the iterator ends after it.
- Cancellation token observed; cancellation cleanly emits
  `thread.exchange_failed` and ends.

**Raises:** Only for setup errors before streaming starts (e.g.,
malformed config that fails pre-flight). Once streaming begins, all
errors are events.

---

```python
def capabilities() -> EngineCapabilities:
    """Declare what this engine can do."""
```

**Postconditions:** Returns a structure listing engine_version,
assembly_versions, supported_modes, supported_features,
supported_providers.

**Raises:** Never.

### 11.2 Common types

```python
class ExecutionContext(BaseModel):
    thread_id: ThreadId
    trigger: dict           # see Engine §1.3 for shape
    cancellation: CancelToken | None

class EngineCapabilities(BaseModel):
    engine_version: str
    assembly_versions: list[str]
    supported_modes: list[str]      # ["single_shot", "conversational"]
    supported_features: list[str]
    supported_providers: list[str]
```

---

## Part 11.5 — LLM Provider Configuration (Phase 1.1, LLM-R5)

**Role:** Bridge between the agent's `ModelSpec` and a usable
`crewai.LLM`. Owns the connector registry, the connector Protocols,
and the small `build_llm` helper the Agent Engine calls per exchange.

**Originating spec:** `process/phase1.1-llm-specs.md` +
`docs/explanation/llm-providers.md`.

### 11.5.1 Public API

```python
async def build_llm(
    spec: ModelSpec,
    provider_config: ProviderConfig,
    secrets: SecretsProvider,
) -> crewai.LLM:
    """Construct a CrewAI LLM from a ModelSpec and provider config."""
```

**Preconditions:**
- `spec` is canonical-form `{provider, model}` (alias expansion has
  already happened in the kernel resolver — see Schemas §A.4.1).
- `provider_config` is the entry from `platform.yaml`'s
  `llm_providers[spec.provider]`.
- `secrets` provides the resolved API key (if any).

**Postconditions:**
- Returns a configured `crewai.LLM` ready to call.
- The provider's connector decides how to construct it.

**Raises:**
- `llm.provider_not_configured` (Fatal) — provider absent from
  `llm_providers`.
- `llm.secret_missing` (Fatal) — configured `api_key_secret` not
  resolvable; `secret.not_found` chained as `__cause__`.
- `llm.client_init_failed` (Fatal) — connector raised during
  construction.
- `llm.connector_unknown` (Fatal) — `provider_config.type` not in
  the registry.

### 11.5.2 The `ProviderConnector` Protocol

Lives in `src/llm/protocol.py`. Every provider implements this.

```python
class ProviderConnector(Protocol):
    type_name: ClassVar[str]

    def auth_prompt_spec(self) -> AuthPromptSpec: ...
    async def validate_credentials(
        self, credentials: Credentials, base_url: str | None,
    ) -> ValidationResult: ...
    async def discover_models(
        self, credentials: Credentials, base_url: str | None,
    ) -> list[ModelEntry]: ...
    async def build_llm(
        self, model_id: str, provider_config: ProviderConfig,
        api_key: str | None,
    ) -> crewai.LLM: ...
```

### 11.5.3 The `MetadataSourceConnector` extension

Layered on top of `ProviderConnector` for connectors that ALSO serve
as a cross-provider metadata source. Today only OpenRouter
implements it.

```python
class MetadataSourceConnector(ProviderConnector, Protocol):
    async def lookup_model_metadata(
        self, provider: str, model_id: str,
        credentials: Credentials | None = None,
    ) -> ModelMetadata | None: ...
```

### 11.5.4 The connector registry

```python
def register(type_name: str, connector_cls: type[ProviderConnector]) -> None: ...
def get(type_name: str) -> ProviderConnector: ...
def known_types() -> list[str]: ...
def connected_metadata_sources() -> list[MetadataSourceConnector]: ...
```

Registration happens once at import-time in
`src/llm/providers/__init__.py` — one line per provider. The CLI
commands (`kolsch connect`, `kolsch refresh`, etc.) iterate the
registry; the engine only consults it via `build_llm`.

### 11.5.5 Supporting dataclasses

`AuthPromptSpec`, `PromptField`, `Credentials`, `ValidationResult`,
`ModelEntry`, `ModelMetadata`, `ModelPricing` — see
`docs/explanation/llm-providers.md` §3 for full shapes.

---

## Part 11.6 — Workspace Resolution (Phase 1.2, UX-R7)

The single point of truth for "where does this `kolsch` invocation
operate?". Owned by `src/platform_runtime/workspace.py`. Consumed by
the CLI dispatcher, the HTTP server bootstrap, and the
`kolsch setup` command. No other module reads `KOLSCH_WORKSPACE` or
imports `platformdirs` — see AGENTS.md hard rules + the Phase 1.2
patch §1.3.

### 11.6.1 Public API

```python
KOLSCH_ENV_VAR = "KOLSCH_WORKSPACE"
DEFAULT_DIR_NAME = "kolsch"


def resolve_workspace(cli_root: Path | None = None) -> Path:
    """Return the canonical workspace path for this invocation.

    Resolution order (highest precedence first):
      1. ``cli_root`` — the parsed value of the CLI ``--root`` flag.
      2. ``$KOLSCH_WORKSPACE`` — the env-var override.
      3. ``<platformdirs.user_documents_dir()>/kolsch`` — the default.

    The returned path is absolute (``.resolve()``-d) and ``~``-expanded.
    The path is NOT guaranteed to exist. Callers that need a populated
    workspace should pair this with :func:`workspace_is_initialized`
    (or instruct the user to run ``kolsch setup``).

    Pure modulo ``os.environ`` read. Idempotent. Safe to call multiple
    times per process.
    """


def workspace_is_initialized(root: Path) -> bool:
    """Return ``True`` iff ``root / "platform.yaml"`` exists and is a file.

    The single source of truth for "is this directory a Kolsch
    workspace?". CLI dispatchers gate non-setup commands on this so an
    uninitialized workspace produces a friendly message (exit code 3)
    rather than a runtime traceback.
    """
```

### 11.6.2 Resolution precedence

| Source | When set | Notes |
|---|---|---|
| `--root <PATH>` (CLI flag) | Per invocation | Highest precedence; explicit user override. Default value is `None`. |
| `KOLSCH_WORKSPACE` env var | Per shell session | For users who keep one workspace elsewhere and want it on every command. |
| `~/Documents/kolsch` (default) | Always | Via `platformdirs.user_documents_dir()`. Created by `kolsch setup`. |

The resolver does NOT consult the current working directory. Phase
1's "use CWD" behavior is removed by UX-R2.

### 11.6.3 Failure surface

`resolve_workspace` itself does not raise — it always returns a
`Path`. The downstream "no workspace here" condition is detected by
`workspace_is_initialized()` and surfaced as the `workspace.not_initialized`
error code (Error Model §4.14). The CLI catches the missing-workspace
case before runtime bootstrap and exits 3 with the friendly message
from Phase 1.2 patch §1.4.

---

## Part 11.7 — Model Fallback (Phase 1.3, FB-R6)

The engine's resilience against transient provider failures. An
agent declares `fallback_models: [...]` next to `model:`; the
engine walks the chain on Recoverable `model.*` errors. No
strategy engine, no health memory across exchanges — an ordered
list, traversed sequentially, fresh state per exchange.

Per the Phase 1.3 patch §0: fallback is for *different* endpoints
(connector-level retries against the same endpoint are a connector
concern). Pre-stream only in v1.3 — once the first content event
has fired, the engine commits to truncation rather than swap-and-
replay (patch §2.3).

### 11.7.1 Agent + platform configuration

The `fallback_models` field is documented in Schemas §A.4.2 (FB-R1).
The `defaults.fallback_models` knob is documented in Asset
Management §3.2 (FB-R2). Both accept the three `ModelSpec` input
shapes (canonical object, compact `provider/model` string, alias);
the resolver normalises every entry to canonical form at Layer 1.

Precedence is agent-wins, no merging: an empty agent list (`[]`)
means "no fallback for this agent" even when a platform default is
present.

### 11.7.2 The engine's loop

Pre-flight step 6 (FB-R3) acquires the primary `crewai.LLM`. Step
7 (FB-R3) resolves the fallback chain to canonical `ModelSpec`s
without building clients — clients are built lazily via
`build_llm` only when the chain is actually consulted.

At exchange execution time the model call becomes a loop:

```
attempt 0: call primary
  on Recoverable model.* error:
    emit model.fallback {from=primary, to=fallback[0], reason}
    build_llm(fallback[0]) → next attempt
  on Fatal model.* error:
    abort — fallback does not apply
  on success:
    emit model.response; exchange continues
chain exhausted:
  raise llm.all_models_failed; emit thread.exchange_failed
```

The classification ("does this error trigger fallback?") lives in
a single predicate in `src/engine/_fallback.py`:

```python
def is_fallback_triggering(err: PlatformError) -> bool:
    """Return True iff a Recoverable `model.*` error should
    trigger the engine to consult the next fallback chain entry.

    Exhaustively documented mapping — see Phase 1.3 patch §2.2
    and Error Model §4.15. New `model.*` codes added later MUST
    explicitly decide which side they fall on (AGENTS.md hard
    rule)."""
```

### 11.7.3 The `model.fallback` event

A new event type emitted once per fallback transition. Shape
documented in Phase 1.3 patch §3.1; ordering in §3.2.

```yaml
type: model.fallback
data:
  from:    { provider, model }      # canonical ModelSpec
  to:      { provider, model }      # canonical ModelSpec
  reason:  "model.rate_limited"     # the triggering error code
  reason_detail: "<human readable>"
  attempt: 1                         # 1-indexed; attempt 0 was the primary
```

The event sits between the failing attempt's `model.request` (or
its `model.response` if one was produced before failure) and the
next attempt's `model.request`. Exactly one `model.fallback` per
transition.

### 11.7.4 Failure surface

- `llm.all_models_failed` (Fatal) — chain exhausted.
  `detail.attempts` is the full ordered list of
  `(model, error_code, detail)` triples.
- `config.fallback_duplicate` (Diagnostic — warning surface) —
  Layer 1 validator at agent load. Not an error; the user may
  have intentionally listed the same model twice. See Error
  Model §4.15.
- Fatal `model.*` codes (`model.authentication_failed`,
  `model.bad_request`) — abort without consulting the chain.

Full per-code table: Error Model §4.15.

---

## Part 11.8 — Permissions (Phase 2.0, TOOL-R7)

The runtime + CLI surfaces for live-editing the workspace's
Authorization rule list, the new `prompt` effect, and the rule-id
scheme the CLI commands reference. Patch source of truth:
`process/phase2.0-tools-specs.md` §2 (`prompt` effect) + §4
(live editing).

The Authorization provider's runtime API is documented in §8. This
section covers what's new in Phase 2.0: the third effect value,
the live-edit endpoints on the provider, the deterministic
rule-id scheme, and the module surface the
`kolsch permissions {list, add, remove, show}` CLI calls into.

### 11.8.1 The `prompt` effect value

```python
class AuthzEffect(StrEnum):
    ALLOW  = "allow"
    DENY   = "deny"
    PROMPT = "prompt"   # NEW in Phase 2.0
```

Returned by the existing `allow(principal, action, target=None)`
method (§8.1) inside `AuthorizationResult.effect`. The provider
classifies; the caller dispatches per the table in
`docs/explanation/secrets-sandbox-authz-contract.md` §3.7.2. Non-chat
callers (`kolsch run`, `kolsch server`, tests, golden) collapse
`PROMPT` to `DENY` and emit `authz.prompt_unattended`
(Error Model §4.16).

### 11.8.2 Live-edit API on the Authorization provider

```python
def list_rules() -> list[Rule]:
    """Return the in-memory rule list in evaluation order."""

def get_rule(rule_id: str) -> Rule:
    """Look up one rule by stable id.

    Raises ``authz.rule_not_found`` (Fatal) with the offering
    `available_ids` if no rule has the requested id.
    """

def add_rule(
    rule: Rule,
    *,
    before: str | None = None,
    after: str | None = None,
    first: bool = False,
) -> Rule:
    """Append/insert a rule, then rewrite `platform.yaml` atomically.

    Returns the rule with its assigned id populated. Exactly one
    of ``before``, ``after``, ``first`` may be set; default is
    append (end of list, before the protected default-deny tail).

    Raises ``authz.rule_invalid`` (Fatal) for malformed
    who/what regex, unknown effect, or conflicting position
    selectors.
    """

def remove_rule(rule_id: str) -> None:
    """Remove a rule by id; rewrite `platform.yaml` atomically.

    Raises ``authz.protected_rule`` (Fatal) when called with the
    protected default-deny tail's id (`rl_default_deny`). Raises
    ``authz.rule_not_found`` (Fatal) for unknown ids.
    """
```

**Reload semantics.** Every call to `allow(...)` re-reads the
in-memory rule list, so `add_rule` / `remove_rule` changes apply
to the *very next* authz check — no exchange-boundary reload, no
process restart. This is the "rule changes apply within the same
exchange" guarantee from patch §0; the in-chat modal (Phase 2.1)
depends on it.

**Atomic write.** Both `add_rule` and `remove_rule` rewrite
`platform.yaml` via temp + rename (Phase 1.1 atomicity
discipline). The on-disk file is consistent at every moment.

### 11.8.3 The rule-id scheme

Rule ids are deterministic and stable across reorders:

```python
def assign_rule_id(rule: Rule) -> str:
    # Hash who+what+effect; take first 8 hex chars.
    h = hashlib.blake2b(
        f"{rule.who}|{rule.what}|{rule.effect}".encode(),
        digest_size=4,
    ).hexdigest()
    return f"rl_{h}"
```

Two rules with identical who+what+effect produce the same id —
the canonical-rule semantics rule out duplicates anyway. Reordering
preserves ids; editing a rule's content changes its id.

The default-deny tail is named `rl_default_deny` by convention and
is removal-protected (the only protected name in v1).

### 11.8.4 CLI module surface

```python
# src/cli/permissions.py
def cmd_permissions_list(runtime, args) -> int: ...
def cmd_permissions_add(runtime, args) -> int: ...
def cmd_permissions_remove(runtime, args) -> int: ...
def cmd_permissions_show(runtime, args) -> int: ...
```

Each dispatcher:

1. Resolves the workspace (Part 11.6) and bootstraps the
   Authorization provider.
2. Calls one of `list_rules` / `add_rule` / `remove_rule` /
   `get_rule` per §11.8.2.
3. Renders human-readable output, or `--json` machine-readable.

The CLI does NOT reach into `platform.yaml` directly — it goes
through the Authorization provider so atomicity, validation, and
in-process reload all work uniformly.

### 11.8.5 Failure surface (summary)

| Code | Where |
|---|---|
| `authz.denied` | tools dispatcher |
| `authz.prompt_unattended` | non-chat callers when effect is `prompt` |
| `authz.rule_invalid` | `add_rule` validation |
| `authz.rule_not_found` | `remove_rule` / `get_rule` for unknown id |
| `authz.protected_rule` | `remove_rule rl_default_deny` |

Full per-code table: Error Model §4.16.

---

## Part 11.9 — Chat (Phase 2.1, CHAT-R6)

The `kolsch chat` REPL — a streaming terminal interface that
turns `runtime.engine.execute_stream`'s event sequence into a
conversational view. Patch source of truth:
`process/phase2.1-chat-specs.md` Parts 1–6.

The chat module owns: the command argv contract, the
event-to-render mapping, the four-choice approval modal that
consumes Phase 2.0's prompt callback, the slash-command surface,
and the resume / cancellation / error semantics. The chat module
does NOT own: any authz state, any session state, any rule
list (live edits go through the Phase 2.0 `add_rule` API —
§11.8.2). The chat is a pure consumer; the AGENTS.md hard rule
introduced by CHAT-R9 enforces that property.

### 11.9.1 Command contract

```bash
kolsch chat [<agent_ref>] [--session <SES_ID>] [--model <MODEL_REF>]
            [--root <PATH>] [-v]
```

- `<agent_ref>` — bundle-qualified path (`kolsch/helper`).
  Defaults to the configured starter agent
  (`platform.yaml`'s `defaults.starter_agent`, falling back to
  `kolsch/helper`).
- `--session <SES_ID>` — resume an existing session via the
  Phase 1.2 store. Without this flag, chat creates a fresh
  session named `chat:<agent_ref>`.
- `--model <MODEL_REF>` — session-scoped model override (any
  `ModelSpec` input shape per §A.4.1). NOT persisted to the
  agent YAML; survives until chat exits.
- `--root` / `-v` — same semantics as every other subcommand.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Clean exit (`/exit`, EOF, Ctrl-D). |
| 1 | Invalid invocation (missing agent, bad session id, bad model ref). |
| 2 | Exchange-level Fatal that chat couldn't recover from. |
| 3 | Workspace not initialized (Phase 1.2 §1.4). |

### 11.9.2 Callback consumption

The chat is the canonical consumer of the Phase 2.0 prompt
callback (§11.8.1's `AuthzEffect.PROMPT` resolution). The
callback's runtime shape:

```python
async def chat_prompt_callback(
    *,
    rule_id: str | None,
    action: str,
    target: str | None,
) -> AuthzEffect | tuple[AuthzEffect, Rule]:
    """The chat's modal renders, blocks until the user picks one
    of [y]/[n]/[a]/[d], then returns per the patch §4.4 mapping:

    - [y] → AuthzEffect.ALLOW
    - [n] → AuthzEffect.DENY
    - [a] with pattern P → (AuthzEffect.ALLOW, Rule(who, what=P, effect=ALLOW))
    - [d] with pattern P → (AuthzEffect.DENY, Rule(who, what=P, effect=DENY))
    """
```

When the callback returns the tuple form, Phase 2.0's authz
provider appends the new rule (§11.8.2) AND rewrites
`platform.yaml` atomically before the just-blocked tool call
re-issues. The next matching call within the same exchange hits
the new rule first.

Pattern suggestion is a pure helper in the chat module —
`_suggest_what_pattern(tool, target) -> str` — that maps
`(kolsch/tools/bash, "rg foo")` → `"rg *"` (glob, not regex),
`(kolsch/tools/write_file, "/path/X.py")` → `"/path/*"`, etc. The
user always sees and can edit the suggestion before committing.

### 11.9.3 Slash-command surface

The eight MVP commands per patch §5:

| Command | Purpose |
|---|---|
| `/help` / `/help <cmd>` | List commands; or describe one |
| `/exit` / `/quit` | Exit cleanly (same as Ctrl-D) |
| `/new` | Fresh session, same agent + model |
| `/clear` | Clear visible scrollback (event log untouched) |
| `/model <ref>` | Switch active model; emits `session.model_overridden` |
| `/thinking [on\|off\|budget <n>\|last]` | Extended-thinking display + budget |
| `/tools` | List the agent's available tools + governing rules |
| `/session [<id>]` | Show current session id; or switch sessions |

Slash commands run BEFORE the input goes to the engine; they
are chat-local operations and never produce engine events except
where they explicitly emit one (`/model` → `session.model_
overridden`; `/thinking budget` → `session.thinking_budget_set`).

The static command registry is the single source of truth for
both `/help` output and prompt_toolkit's completion dropdown —
adding a new command means adding one entry that surfaces in
both places.

### 11.9.4 Renderer pure-consumer property

Every visible block in the chat corresponds to an event the
engine emitted. The renderer's only state is the current
exchange's block list; everything else is read from events. If
a UI element needs information the events don't carry, the
engine grows the event vocabulary — the chat does NOT fork
state. The AGENTS.md hard rule introduced by CHAT-R9
mechanically encodes this property; TC-CHAT-18 (in the test
catalog Part 9.9) enforces the "no full-screen mode" corollary
that keeps terminal-native scrolling and selection working.

### 11.9.5 Failure surface (summary)

| Code | Where |
|---|---|
| `chat.agent_not_found` | Startup, before runtime bootstrap |
| `chat.session_not_found` | Startup with `--session` |
| `chat.model_override_invalid` | Startup with `--model` |
| `chat.input_aborted` | Ctrl-C during streaming |
| `chat.callback_resolution_failed` | Modal pattern-edit produced an invalid Rule |

Full per-code table: Error Model §4.17.

---

## Part 11.10 — Agent Envelope (Phase 2.2)

The agent-envelope machinery — Jinja templating, per-agent
permissions, context files, and agent inheritance via `extends:`.
Patch: `process/phase2.2-agent-envelope-specs.md`.

### 11.10.1 `kolsch.tools.permissions`

Two async functions backed by a `ContextVar` bound by the tool
dispatcher (`src/tools/dispatch.py`) around the tool function
body:

```python
permissions.check(who: str, what: str) -> AuthzEffect
permissions.require(who: str, what: str) -> None
```

`who` is the principal pattern (e.g. `"agent.ag_helper"`);
`what` is the action OR target pattern the tool body wants to
gate on (e.g. `"workspace.git.force_push"`). The string is
passed through to the provider as-is — no prefixing.

Walks the **four-level stack** identical to the dispatch-boundary
check: agent permissions → bundle permissions → platform rules
→ default-deny (per qol-tool-permission-discovery-uniform-with-stacking.md
+ qol-tool-permissions-binding-unused.md).

`require` raises `errors.AuthzDenied` on anything other than
`ALLOW`. A `PROMPT`-effect match collapses to deny here —
mid-execution modals would block the tool's coroutine, so the
in-body `require` never triggers the no-rule modal that the
dispatch-boundary check fires. Use `check` instead of `require`
when the tool wants to branch on the effect rather than raise.

Out-of-context calls raise `tool.permissions_no_context`
(typically a unit test that imported a tool directly without
going through the dispatcher). The dispatcher's `bind` / `reset`
pair lives inside the sandbox `_work` closure, so the
ContextVar's lifetime exactly matches the tool body and is
cleared on both the success and exception paths.

The dispatcher constructs a `PermissionsBinding` with five
fields: `provider`, `agent_permissions`, `bundle_permissions`,
`principal`, `tool` — see `src/tools/permissions.py`.

### 11.10.2 The Jinja render context

```python
templating.render.render_prompt(
    template_string: str,
    context: dict[str, Any],
    *,
    template_name: str = "<inline>",
) -> str
```

Pure Jinja2 render harness. Uses `jinja2.Environment` with
`StrictUndefined`, `autoescape=False`. `template_name` identifies
the source for error reporting; the engine pre-flight passes the
agent's YAML path. Render failures surface as
`errors.ConfigTemplateRenderFailed`
(`code = "config.template_render_failed"`, Fatal) with
`detail = {template, reason}`.

The engine pre-flight binds the five standardized variables:

| Key | Type | Default rendering |
|---|---|---|
| `tools` | `ToolsVar` (list-shaped) | `tools.list()` — markdown blocks per tool |
| `docs` | `DocsVar` (list-shaped) | primaries inlined + secondaries listed |
| `workspace` | `WorkspaceVar` (single-object) | `## Active workspace` + path + allow/deny lists |
| `agent` | `AgentVar` (single-object) | `<name> (<id>) — <provider>/<model>` |
| `platform` | `PlatformVar` (single-object) | `Workspace: <path> · Providers: <names> · <N> rules` |

List-shaped variables expose `.list()` / `.summary()` /
`.names()` / `.get(name)` / `.count` plus `__iter__`, `__len__`,
`__getitem__`, `__contains__`.

### 11.10.3 `extends:` chain resolution

`templating.inheritance.resolve_extends_chain(agent_path,
workspace_root) -> list[AgentConfig]` walks the chain from
leaf to root, detecting cycles. Failure modes:
`config.parent_agent_not_found`, `config.agent_inheritance_cycle`.

`templating.inheritance.apply_inheritance(chain) ->
ResolvedAgentConfig` applies wholesale-per-field override from
root toward leaf.

The child's `prompt:` is rendered as a Jinja template with
`parent` bound to the parent's prompt source via a custom
`DictLoader`. Required blocks (`{% block X required %}`) surface
as `config.required_block_missing` when child extends but does
not override.

### 11.10.4 Failure surface (summary)

Eleven codes per Error Model §4.18:

| Code | Level | Where |
|---|---|---|
| `config.context_file_unprefixed` | Fatal | agent-load |
| `context.file_not_found` | Fatal | agent-load |
| `context.file_too_large` | Fatal | agent-load |
| `config.template_render_failed` | Fatal | pre-flight 5.6.6 |
| `config.required_block_missing` | Fatal | pre-flight 5.6.6 |
| `config.agent_inheritance_cycle` | Fatal | pre-flight 5.6.5 |
| `config.parent_agent_not_found` | Fatal | pre-flight 5.6.5 |
| `config.extends_template_without_inheritance` | Fatal | pre-flight 5.6.6 |
| `agent.template_warning` | Recoverable (Warning) | pre-flight 5.6.6 |
| `authz.denied` | Recoverable | pre-dispatch + tool's `permissions.require` |
| `tool.permissions_no_context` | Fatal | tool API called outside dispatcher |

---

## Part 12 — Inter-Module Dependency Matrix

Who calls whom. Rows are callers; columns are callees. Cells list the
specific methods called.

| Caller ↓ \ Callee → | Kernel | Asset Mgmt | Store | Bus | Memory | Secrets | Sandbox | Authz | Tools | Skills | LLM | Workspace |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **CLI / API** | — | initialize, get_asset, build_resolved_config, list_bundles | create_session, get_session, get_thread, delete_session | subscribe | — | resolve, exists | — | — | — | — | registry.{get,known_types,connected_metadata_sources}; connector.{auth_prompt_spec,validate_credentials,discover_models} for connect/refresh | **resolve_workspace, workspace_is_initialized** *(Phase 1.2, UX-R7)* |
| **Asset Mgmt** | resolve_and_validate | (self) | — | — | — | — | — | — | — | — | — | — |
| **Agent Engine** | (via Asset Mgmt) | build_resolved_config | get_leaf, append_event, read_path_to_leaf, write_content | publish | bind_session, (Context.*) | resolve, exists | execute | allow | register_tools_for_agent, invoke_tool | get_enabled_skills, format_skills_listing, force_invoke | **build_llm** *(Phase 1.1, pre-flight step 6 — primary; Phase 1.3 FB-R6 calls it again per fallback chain entry on Recoverable model.\* errors)* | — |
| **Memory Provider** | — | — | read_events | — | (self) | — | — | — | — | — | — | — |
| **Tools Subsystem** | — | get_asset | — | — | — | resolve, exists | (calls engine's sandbox use) | allow | (self) | — | — | — |
| **Skills Subsystem** | — | get_asset | — | — | — | — | — | — | — | (self) | — | — |
| **Secrets/Sandbox/Authz** | — | get_platform_config | — | — | — | — | — | — | — | — | — | — |

**Read this as:** "When the Agent Engine acts, it calls Asset Mgmt's
`build_resolved_config`, the Store's `get_leaf`/`append_event`/etc.,
the Bus's `publish`, Memory's `bind_session`, Secrets's
`resolve`/`exists`, Sandbox's `execute`, Authz's `allow`, Tools'
`register_tools_for_agent`/`invoke_tool`, Skills'
`get_enabled_skills`/`format_skills_listing`/`force_invoke`, and the
LLM module's **`build_llm`** per exchange."

The Agent Engine is the central caller. Subsystems mostly don't call
each other directly — they're called by the engine and by the entry
surfaces.

---

## Part 13 — Common Types (Cross-Module)

Types referenced by multiple modules' APIs. Centralized here for
clarity.

### 13.1 Identifiers

```python
SessionId = NewType("SessionId", str)        # "ses_<uuid>"
ThreadId = NewType("ThreadId", str)          # "th_<uuid>"
EventId = NewType("EventId", str)            # "ev_<uuid>"
AgentId = NewType("AgentId", str)            # "ag_<user-chosen-id>"
MemoryItemId = NewType("MemoryItemId", str)  # "mem_<uuid>"
SubscriptionId = NewType("SubscriptionId", str)
```

All prefixed-UUID identifiers validated at construction. The prefix is
part of the type.

### 13.2 Event

The single canonical Event type, used everywhere:

```python
class Event(BaseModel):
    event_id: EventId
    parent_event_id: EventId | None
    thread_id: ThreadId
    session_id: SessionId
    type: str               # e.g., "tool.called", "thread.exchange_started"
    data: dict              # type-specific payload (schema per event type)
    actor: dict             # {kind: "system"|"agent"|"user", id?: str}
    created_at: datetime
    content_ref: ContentRef | None  # optional sidecar reference
```

Used by Store, Bus, Memory, Agent Engine.

### 13.3 ResolvedAgentConfig

The output of the Kernel's resolve_and_validate, consumed by the Engine
and indirectly by every subsystem:

```python
class ResolvedAgentConfig(BaseModel):
    schema_version: str
    assembly_version: str
    agent_id: AgentId           # optional in YAML; resolver derives ag_<bundle>_<name> when omitted (see Schemas §A.3)
    name: str
    description: str
    model: ModelSpec
    prompt: list[str] | str        # per PromptSpec; pre-resolved content
    io: IOSpec
    subsystems: SubsystemsConfig
    runtime: RuntimeSpec
```

(Sub-types per Phase 1 Schemas §A.)

### 13.4 Error

The canonical Error envelope used in all events carrying error data
(per Error Model spec):

```python
class Error(BaseModel):
    type: str               # dotted code: "tool.unauthorized" etc.
    message: str
    where: str              # dotted locator: "tools.dispatch.invoke"
    detail: dict            # type-specific structured data
    cause: "Error | None"   # recursive cause chain
```

### 13.5 CancelToken

```python
class CancelToken:
    def cancel(self) -> None: ...
    def is_cancelled(self) -> bool: ...
    async def wait_cancelled(self) -> None: ...
```

Used by the Engine for cooperative cancellation.

---

## Part 14 — How to Use This Reference

**For implementing a subsystem:** Find your subsystem's Part. The
listed methods are exactly what your module must expose. Match
signatures, satisfy postconditions, raise the specified errors. Refer
to the originating spec for deeper context.

**For implementing the Agent Engine:** Part 11 plus Part 12's row for
Agent Engine. The dependency matrix shows every other module's method
the engine calls.

**For testing:** Build mock implementations matching the signatures in
each Part. The contract is what the API enumerates.

**When proposing a change:** Update this document AND the originating
spec. The reference and the spec must agree; if they diverge, the
spec wins (it has the prose justification).

**For onboarding:** Read Part 12 first (the dependency matrix). It
shows the system's shape at a glance. Then read the Parts for the
modules you're working with.

---

## Part 15 — Open Items

1. **Type completeness.** This document specifies the *public* API
   surfaces. Internal types (private to each module) are not
   enumerated. Implementations may add as needed.

2. **Async iteration semantics.** Several methods return
   `AsyncIterator`. Implementations must support proper cleanup if the
   iterator is abandoned mid-stream. The contract assumes standard
   Python `async for` discipline.

3. **Type stability.** As subsystems gain variants (Phase 2+), some
   types (`MemoryConfig`, `SandboxConfig`) may become unions. The
   public API methods should remain stable; the type internals may
   grow.

4. **Versioning of this reference.** Tagged `draft-0.1`. Major
   contract changes bump the draft number; minor additions
   (non-breaking) update in place. Track version in any project
   referencing it.

5. **Generated documentation.** This reference is hand-maintained.
   When implementation happens, code-level docstrings should mirror
   the contract sections here. Tools like Sphinx could auto-generate
   from docstrings; this document remains the canonical written form.
