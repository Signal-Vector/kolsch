# Phase 1 — The Session/Run Store Contract

**Draft 0.1**

> The module that owns sessions, threads, their entity records, and their
> per-session JSONL event log files. Direct prerequisite of the Agent
> Engine: the engine's "open the session's JSONL file for append, compute
> the leaf, append events" operations live here.
>
> Builds on Phase 1 Schemas draft 0.4 (§A, §B, §C). References the Agent
> Engine contract draft 0.1.

---

## Part 0 — Decisions Locked

- **Two-store split.** Entity records (Session, Thread) in an **entity
  backend** for mutable state; per-session JSONL files on the filesystem
  for the immutable event log.
- **The entity backend is pluggable and matures with scale.** The
  **default is SQLite** (at `.platform/entities.db`) — zero external
  services. **Postgres** is the scale-up backend, adopted when data
  volume or multi-process concurrency genuinely demand it. The contract
  below is backend-agnostic; "the entity backend" means SQLite by default
  and Postgres at scale. The same component test suite MUST pass against
  both backends (see §10 / DB-R1). The filesystem JSONL event log is
  ground truth regardless of entity backend.
- **Leaf is derived from the log**, not stored. Phase 1 derives by
  end-of-file scan; the API is shaped so a tree-walk implementation drops
  in later. Per-call leaf state is the caller's (engine's) call-frame
  variable, not persistent runtime state.
- **Per-session sidecar `.lock` file** gates writes. POSIX `flock(LOCK_EX)`
  on the lock file. Auto-release on process death; no stale-lock recovery.
- **Atomic appends.** `O_APPEND` for writes under 4KB; `fsync` after write
  for larger writes; recovery on open truncates a partial last line if
  the JSON fails to parse.
- **No eager index projection.** Entity records hold only their own
  mutable state. Cross-cutting facts (last activity, exchange count, total
  cost) are NOT eagerly projected into the entity backend; they're computed
  on demand if needed. Sufficient for v1 scale.
- **One thread per session in Phase 1.** The Session/Thread split exists in
  the data model; Phase 1 just doesn't exercise multi-thread sessions.

> **Backend terminology in this contract.** Where the text below says
> "Postgres," read it as "the entity backend." SQLite is the default
> implementation; Postgres is the scale-up implementation. The API,
> invariants, and behavior are identical across backends; only the
> storage engine differs, selected via `platform.yaml`'s
> `storage.entity_backend`.

---

## Part 1 — What the Store Owns

The Session/Run Store owns three things:

1. **The Session entity record** (entity backend) — id, name, starred,
   archived, created_at. Per §C of the schemas.
2. **The Thread entity record** (entity backend) — id, session_id,
   agent_id, created_at. Per §C.
3. **The per-session JSONL event log file** — append-only file holding all
   events for the session, per §B. One file per session.

It also owns the **sidecar `.lock` file** per session, used to serialize
writes (Part 4).

It does NOT own:

- The Agent Engine or any execution.
- Event content or interpretation (it stores and retrieves events; it does
  not understand them).
- The Event Bus (events that need publishing are emitted to the bus
  separately by the engine; the store is the persistence layer only).
- Memory, secrets, tools, or any Provider Module.
- The Assembly Procedure or any context computation. (It serves the events
  the procedure needs; it does not run the procedure.)

The store is the **persistence layer** for execution state. Above it, the
engine reads from it and writes to it via the API in Part 2.

---

## Part 2 — Interface

All methods are `async`. Errors are raised, not returned as status values.

### 2.1 Session lifecycle

```
async def create_session(initial_agent_id: str) -> Session
```
Creates a new session. Allocates `ses_...` id. Creates the JSONL file at
the canonical path (§3.1). Appends the initial `session.opened` event.
Returns the Session entity record.

```
async def get_session(session_id: str) -> Session
```
Returns the entity record for the session. Raises `SessionNotFound` if
absent.

```
async def list_sessions(
    *,
    starred: bool | None = None,
    archived: bool | None = None,
    limit: int | None = None,
    order_by: Literal["created_at", "name"] = "created_at",
    order: Literal["asc", "desc"] = "desc",
) -> list[Session]
```
Returns Session entity records matching the filters. Pure Postgres query
on entity records; does NOT scan logs. No filter or sort on log-derived
fields like cost or last activity in v1 (see §0 — no eager projection).

```
async def update_session(
    session_id: str,
    *,
    name: str | None = None,
    starred: bool | None = None,
    archived: bool | None = None,
) -> Session
```
Updates mutable state on the entity record. Only the named fields are
mutable. Does NOT touch the log.

```
async def delete_session(session_id: str) -> None
```
Deletes the entity record AND the JSONL file AND the `.lock` file. This
is a permanent, irreversible operation. The store provides this method;
whether to expose it through any user-facing interface is a higher-level
decision.

### 2.2 Thread lifecycle

```
async def create_thread(session_id: str, agent_id: str) -> Thread
```
Creates a thread entity record bound to the session and agent. Allocates
`th_...` id. Does NOT append any event by itself — the engine appends
events into the thread on its first `execute` call.

```
async def get_thread(thread_id: str) -> Thread
```
Returns the entity record. Raises `ThreadNotFound`.

```
async def list_threads(session_id: str) -> list[Thread]
```
Returns all threads for a session. In Phase 1 this returns one thread.

### 2.3 Event operations

```
async def append_event(thread_id: str, event: Event) -> Event
```
Appends an event to the session's JSONL file under the per-session lock.

The supplied `event` is expected to be substantially formed by the caller
(the Agent Engine): `id` allocated, `ts` set, `type` and `data`
populated, `thread` set. The store fills in `parent_event_id` by
computing the current leaf inside the lock (so leaf-determination and
append are atomic together). The store appends per the atomic-append
discipline (§5).

Returns the event as-appended (with `parent_event_id` populated). The
returned event is byte-identical to what was written to the JSONL file.

```
async def get_leaf(thread_id: str) -> Event | None
```
Returns the most recent event for the thread, or `None` if the thread has
no events yet (a freshly created thread before any append). Per Phase 1
leaf rule: end-of-file scan back to the most recent event with this
`thread`.

In Phase 2 (with branching), the implementation changes to the tree-walk
rule (most recent *childless* event). The public method signature and
contract are unchanged.

```
async def read_events(
    thread_id: str,
    *,
    from_event_id: str | None = None,
    limit: int | None = None,
) -> AsyncIterator[Event]
```
Yields events for the thread in append order (file order). Optional
`from_event_id` resumes from after a given event id; useful for
incremental consumers. Optional `limit` caps yielded events.

This is the *file-order* read — used for observability, replay, debugging.
For context assembly the engine uses `read_path_to_leaf` below.

```
async def read_session_events_of_type(
    session_id: str,
    event_type: EventType,
) -> list[Event]
```
Returns every event of `event_type` in the session, across all of its
threads, in file order — one pass over the session's JSONL, filtered
by type before anything is kept. Empty when the log is missing or
empty. Used by the engine's `tool.unavailable` once-per-session dedup
(feat-139).

```
async def read_path_to_leaf(thread_id: str) -> AsyncIterator[Event]
```
Yields the events on the path from the thread root to the current leaf,
in chronological order (root first, leaf last). This is what the Assembly
Procedure (§B.5 of the schemas) walks to build the model's context.

In Phase 1 (no branching), the path equals `read_events` output — the
implementation can be identical for now. The separate method exists so
Phase 2's branching-aware implementation drops in without API change.

```
async def write_content(content: bytes | str) -> ContentRef
```
Writes content to `<platform-root>/content/<hash>.bin`, where `<hash>`
is the SHA-256 hex digest of the content. Returns a `ContentRef`:

```
ContentRef
├── hash       string    — SHA-256 hex digest
├── size       int       — byte count
└── stored_at  string    — relative path "content/<hash>.bin"
```

Idempotent: if the file already exists (same hash), no rewrite — returns
the existing ref. (Hash collision is probabilistically impossible.)

Called by the Agent Engine when an event's payload exceeds the inline
threshold (per Tools spec §6.4, Memory Provider §3). The engine embeds
the resulting `ContentRef` in the event before appending.

### 2.4 Notably absent from v1

- No `get_event_by_id`, no `get_children`, no `get_tree`. Phase 2 may add
  them when branching navigation arrives.
- No event-stream subscription. The Event Bus handles real-time delivery;
  the store is for persistence and read-after-append only.
- No analytics methods (`total_cost_by_session`, `sessions_by_activity`).
  v1 doesn't ship analytics; consumers compute on demand by reading.
- No bulk-import or replay-from-log methods. v1 is single-machine and
  doesn't need them.

### 2.5 Filesystem-Based Reconstruction

```
async def reconstruct_from_filesystem() -> ReconstructionResult
```

Rebuilds Postgres entity records from on-disk session JSONL files. Used
when Postgres has been wiped but session files exist, when migrating
installations, or for recovery from accidental database loss.

Behavior:

1. Scan `<platform-root>/sessions/` for `.jsonl` files.
2. For each session file, read the `session.opened` event (first line).
   Extract session id, agent id, created_at.
3. If no Session entity record exists for that id in Postgres, create
   one with reconstructed fields. UI-state fields (name, starred,
   archived) default to "session id as name, not starred, not archived"
   — these cannot be reconstructed from the filesystem.
4. Scan the file for thread events; reconstruct Thread entity records
   similarly.
5. Return `ReconstructionResult` with counts: sessions found,
   sessions reconstructed, threads found, threads reconstructed,
   warnings (e.g., files with no `session.opened` event).

**Non-destructive.** Existing entity records are NOT overwritten. If
filesystem and Postgres agree on a session's existence, the Postgres
record is left as-is.

**Skip on corruption.** Files with malformed content (corrupt JSONL,
no `session.opened`) are skipped with a warning. v1 does not attempt
heroic recovery — that's a manual user task.

Periodic-or-on-demand operation, not part of the normal `execute`
path. Typically invoked via a CLI command (`kolsch reconstruct`).

---

## Part 3 — Filesystem Layout

### 3.1 Canonical paths

The store roots all session data at `<platform-root>/sessions/`,
where `<platform-root>` is the current working directory at platform
startup. The base path is NOT configurable in v1 — moving the platform
installation means changing directories. Within it:

```
<base>/
├── <session_id>.jsonl        — the event log
├── <session_id>.jsonl.lock   — the sidecar lock file
├── <session_id>.jsonl.tmp    — transient; see §5.3
└── ...
```

One pair of files per session, all in the same directory. No nested
directory structure in v1.

(Pi organizes sessions by working directory; v1 of this platform does NOT
— sessions are identified by id only. Organization by project/topic is
an IDE-layer concern; the store is flat.)

### 3.2 File ownership

The store is the sole writer of `<session_id>.jsonl`. No other module
writes to it. No other module reads it via the filesystem; reads go
through the store's API.

Readers MAY open the file for diagnostic purposes (a human running `cat`
on a session log to inspect it), and this is safe — the file is
append-only, so partial reads are never inconsistent. But programmatic
reads from other modules go through the store.

### 3.3 The `.lock` file

Empty file (or containing the holding process's PID as human-debuggable
content; PID content is informational only, not used for correctness).
Created on first append. Never deleted. Its existence is harmless.

---

## Part 4 — The Per-Session Lock

### 4.1 The mechanism

The store, when about to append an event to a session's JSONL file:

1. Opens the session's `.lock` file (creating it if absent), receiving
   a file descriptor.
2. Calls `flock(fd, LOCK_EX)` — blocks if another holder has the lock;
   returns when acquired.
3. Computes the current leaf by reading the JSONL (the leaf computation
   is now under the lock, ensuring no other writer can append between
   leaf-determination and the new append).
4. Fills in the event's `parent_event_id` from the leaf.
5. Appends the event to the JSONL per the atomic-append discipline (§5).
6. `fsync`s if required (§5).
7. Releases the lock by closing the fd (POSIX semantics: closing releases).

The lock is held for the duration of the leaf-determination + append. It
is NOT held for the duration of the engine's overall `execute` call —
only the brief window of each individual append.

### 4.2 The semantics

`flock` is an *advisory* lock. Cooperating writers honor it; nothing
prevents an uncooperating writer from ignoring it and corrupting the
file. The store is the sole writer per §3.2, so all writers cooperate by
construction.

`flock(LOCK_EX)` is exclusive. Only one holder at a time. Other writers
block until release. The kernel manages waiters; FIFO is not guaranteed
but is typical.

### 4.3 Crash recovery

If a process holding the lock dies, the OS releases the lock as part of
file descriptor cleanup. No manual recovery, no stale-lock cleanup,
nothing to detect. The next acquirer simply gets the lock.

If a process dies *during* the append (mid-write), the JSONL file may
have a partial last line. This is handled by the atomic-append recovery
rule in §5.3 — the next `read_events` or `get_leaf` operation detects
and truncates the partial line.

### 4.4 What this serializes

The lock serializes appends to a session's JSONL file. In Phase 1, one
thread per session means this is also per-thread serialization. In Phase
2, multiple threads in one session would share the lock — multiple
agents in one session serialize their appends. This is the correct
behavior: a single JSONL file should have a single writer at a time.

### 4.5 What this does NOT serialize

Reads. Multiple processes can read a session's JSONL file simultaneously
without acquiring the lock. The file is append-only; partial reads at
the tail are handled by the recovery rule. No reader needs the lock.

---

## Part 5 — Atomic Appends and Recovery

### 5.1 The append discipline

The store serializes an event to a JSONL line (one JSON object,
terminated with `\n`). The line is written to the JSONL file with
`O_APPEND` semantics.

**For lines under 4KB** (the POSIX `PIPE_BUF` guarantee on Linux): a
single `write(2)` with `O_APPEND` is atomic with respect to other
appenders — the entire line lands as one unit or not at all. No `fsync`
required for atomicity within the running system; durability across a
machine crash is a separate concern (see 5.2).

**For lines over 4KB**: `O_APPEND` is still used (atomic positioning),
but POSIX does not guarantee atomicity of the write content itself
beyond `PIPE_BUF`. The store writes the line, then calls `fsync(fd)` to
flush to disk before considering the append complete. A crash before
`fsync` may leave a partial line; the recovery rule (§5.3) handles it.

The store does NOT use a write-to-tempfile-and-rename pattern for
appends. JSONL's per-line append-only structure makes rename-based
atomicity unnecessary and would defeat the append-only property.

### 5.2 Durability

`fsync` is called after each append for events the store considers
durability-critical. For Phase 1: events whose loss would mean the agent
"forgets what it just did" should be `fsync`'d. This includes
`agent.message`, `tool.result`, `model.response`, `config.set`. Progress
events (`thread.exchange_started`, etc.) MAY skip `fsync` for performance
if implementation chooses.

The contract: an event the engine has observed as "appended" (i.e.,
`append_event` returned successfully) MUST survive a process crash. The
contract does NOT guarantee survival of an OS-level (kernel panic, power
loss) crash unless the engine chose an `fsync`-required event type.

(This is the standard tradeoff. v1 doesn't promise machine-crash
durability for every event; it promises process-crash durability and
machine-crash durability for the critical event types.)

### 5.3 Recovery on partial last line

When the store opens a session's JSONL file (for read or append), it
checks whether the file ends in a valid JSON line:

1. Read backward from the end of the file to find the most recent newline.
2. Take the bytes from after that newline to the end of the file.
3. Attempt to parse as JSON.
4. If parsing fails (partial write from a crash): truncate the file at
   the position of that newline. The partial line is discarded.
5. If parsing succeeds: file is intact, no recovery needed.

This recovery happens at file open. It's idempotent (running it twice
does nothing the second time). It is the *only* recovery mechanism the
store performs. The discarded partial line is gone — the event that was
being appended is lost. This is acceptable because the engine had not
yet received an "appended successfully" return from `append_event` for
that event, so it had not committed to having appended it.

### 5.4 What recovery does NOT do

- It does NOT repair structural inconsistencies in the file (e.g., events
  with dangling `parent_event_id`). Those are spec violations indicating
  an engine bug; the store does not patch them.
- It does NOT roll back a successfully-appended event. Appends are
  permanent.
- It does NOT reorder events. The file's append order is authoritative.

---

## Part 6 — Conformance Invariants

What any Session/Run Store implementation must honor.

### I1 — Append-only

The JSONL file is append-only. No previously-written event may be
modified, replaced, or deleted by the store. (Whole-file deletion via
`delete_session` is permitted; modifying contents in-place is not.)

### I2 — Lock-serialized appends

Every append to a session's JSONL file is performed under the per-session
exclusive lock (§4). Concurrent writes to the same session file by
distinct callers MUST be serialized by the lock. The lock MUST be held
for the duration of leaf-determination + append, not just the write.

### I3 — Atomic-or-recoverable appends

Every append is either atomic (write completes fully) or recoverable
(partial write is detected and truncated on next open). No append leaves
the file in an unparseable state for more than the duration of the next
open.

### I4 — `parent_event_id` correctness

For an appended event with a non-null `thread`, the store sets
`parent_event_id` to the thread's current leaf at the moment of the
append, OR (for the first event of a thread) to the thread's id. This
computation MUST be performed under the lock (§4).

### I5 — Leaf derivation

`get_leaf(thread_id)` returns the event whose `parent_event_id` chain
forms a path from the thread root to it, and which has no later events
appended after it (Phase 1: literally the most recent append; Phase 2:
the most recent childless node in the thread's subtree).

### I6 — Entity record consistency

Postgres entity records (Session, Thread) and their corresponding JSONL
files MUST agree on existence. If a Session entity record exists, its
JSONL file exists (or is created on first append). Deletion deletes both.
A JSONL file without an entity record is a recovery situation (the
implementation MAY auto-reconstruct the entity record from the
`session.opened` event in the file, or MAY treat it as an error — Phase 1
treats it as an error).

### I7 — No log-derived state in entity records (v1)

v1 entity records hold only their own intrinsic mutable state (name,
starred, archived, created_at, identity bindings). The store MUST NOT
eagerly project log-derived facts (cost totals, activity timestamps,
exchange counts) into Postgres. This is a v1 constraint to keep the
contract simple; Phase 2 may relax it for query performance.

### I8 — File ownership exclusivity

The store is the sole writer of session JSONL files (and `.lock` files,
and `.tmp` files). Other modules MUST NOT write to them. Diagnostic
reads by humans (cat, tail) are permitted because the append-only file
format is safe to read concurrently with appends.

### I9 — Identifiers

Every entity has a prefixed-UUID id per the schema convention:
- Sessions: `ses_<uuid>`
- Threads: `th_<uuid>`
- Events: `ev_<uuid>` (allocated by the caller, typically the engine;
  the store does not allocate event ids)

### I10 — `async` semantics

All store methods are `async`. The implementation MUST NOT block the
event loop on filesystem or Postgres I/O. Lock waits (during `flock`)
are the one exception — `flock` is a blocking syscall and a worker
holding `append_event` blocks until acquired. In Phase 1 with low
contention this is fine; if it becomes a bottleneck, the implementation
may switch to `flock(LOCK_NB)` with a retry loop yielding to the event
loop.

### I11 — Content store immutability

Files in `<platform-root>/content/` MUST be written exactly once and
never modified. `write_content` MUST treat a write to an existing hash
as a no-op (the content is already immutable and, by hash equality,
identical). The store MUST NOT garbage-collect content files in v1
(eviction policy deferred).

---

## Part 7 — Open Items

1. **Where the Session/Run Store is configured** (which base directory,
   which Postgres database/schema). Probably a top-level configuration
   concern, not store-internal. Specified when the platform-level config
   is drafted.

2. **Postgres schema migrations.** Adding fields to Session/Thread later
   (or projecting log-derived state in Phase 2) requires migrations. The
   migration mechanism is implementation-level (Alembic) and not
   contract-level, but worth flagging that the contract permits adding
   fields, not removing them.

3. **Multi-file sessions** in some future phase (very long sessions
   rotated into multiple JSONL files). Not Phase 1, but the API doesn't
   currently distinguish single-file vs multi-file; this would need a
   contract amendment if it ever lands.

4. **Concurrent `create_session`** — two callers create a session at the
   "same time" with conflicting names or ids. Id collision is
   probabilistically impossible (UUID). Name collision is fine because
   names are not unique. No issue, but worth confirming this isn't a
   problem.

5. **Backup and restore.** A session is a JSONL file + its Postgres
   entity record. Backing up and restoring a session means moving both.
   The store doesn't currently provide a `backup_session` /
   `restore_session` pair; copying files and re-inserting the row works
   manually. A first-class backup API is later phase.

6. **`O_APPEND` portability.** v1 deploys to Linux containers; `O_APPEND`
   semantics are POSIX. Windows-native deployment (not via Docker) would
   need verification; out of scope for Phase 1 (Docker-only deployment).

7. **Per-write `fsync` performance.** §5.2 distinguishes durability-
   critical events from non-critical for `fsync` purposes. The exact
   threshold and per-event-type policy is implementation-level; the
   contract specifies the principle (some events must be `fsync`'d on
   append) but leaves the precise policy to v1's implementation. Likely
   needs measurement.
