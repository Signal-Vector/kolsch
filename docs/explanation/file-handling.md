# Phase 1 — File Handling and `file_ref`

**Draft 0.1**

> How files flow through the platform. The `file_ref` type, the
> destinations tools write to, the path-access policy that guards
> reading and writing outside standard locations.
>
> Builds on Asset Management (the platform's filesystem layout), Tools
> (the builtin file-touching tools), Authorization (the parallel
> action-level rule system), and the Error Model.

---

## Part 0 — Decisions Locked

- **`file_ref` is a path string.** Not an envelope; not a structured
  object; not a hashed reference. Just the path. The TypeSpec value
  `file_ref` indicates "this string is conventionally interpreted as a
  filesystem path."
- **Tool persistent outputs land in `data/`.** Tools producing
  user-facing files write to a path under `data/` and return the
  resulting path as a `file_ref`. The platform doesn't impose
  sub-structure; tools choose sensible paths.
- **Tool ephemeral outputs use OS tempdir.** Standard `tempfile`
  patterns. The platform doesn't care.
- **The content store is engine-internal.** Tools NEVER write to
  `content/`. Only the engine writes there, and only to store oversized
  event-content sidecars (per Tools spec §6.4).
- **Path-access policy.** One allow list plus one deny list. The
  platform pre-populates the allow list on first run with the standard
  folders; after that, the list is the user's. Default-deny for paths
  not matching the allow list and not in the deny list.
- **`bash` does NOT enforce the path policy** in v1's `native` sandbox.
  Honest documented limitation. Sandboxed variants enforce it later.

---

## Part 1 — What `file_ref` Is

`file_ref` is the TypeSpec value used in IOSpec to indicate that a
field's value is a filesystem path. At the Python level, a `file_ref`
is a plain `str`. At the schema level, it's a string typed as
`file_ref` (versus `string`) so that:

- The CLI may surface UX for file inputs (prompting with a file picker
  or path completion).
- The IDE (later) can render it as a file widget.
- The TypeSpec validators can perform path-shape sanity checks (no
  null bytes, not empty, etc.) distinct from generic string validation.

That's the entire reason `file_ref` is its own type and not just a
string with convention. **Functionally, a `file_ref` value IS a string.**
Tools, agents, and the platform handle it as a path.

### 1.1 What `file_ref` does NOT carry

- No metadata (filename, size, mime type).
- No content hash for integrity verification.
- No source discriminator (data vs. content store).
- No embedded content.

If consumers need metadata, they derive it from the path or read it
from the file at use time (mime type from extension, size from `stat`).
This is honest v1 simplicity. A future variant could carry richer
metadata in a structured object; v1 doesn't.

### 1.2 Path conventions

Paths in `file_ref` values:

- May be **relative** (resolved relative to the platform root).
- May be **absolute** (resolved as-is).
- Use forward slashes as separators (works on POSIX; Windows users
  should also use forward slashes — Python's pathlib normalizes).
- Should NOT contain shell expansions (`~`, `$VAR`) — those are user-
  facing conveniences that the platform doesn't expand. Tools that
  want them expand at their own discretion.

The path policy (Part 4) determines which paths are actually accessible.

---

## Part 2 — File Pathways

Three ways files enter the platform's awareness.

### 2.1 User-provided data files

The user places files in `<platform-root>/data/` (or elsewhere in
their filesystem if path policy permits). Agents reference them by
path as `file_ref` inputs or via tools (`read`, `write`).

The platform doesn't track or catalog these — they're just files in
the user's filesystem. When an agent processes a `file_ref` input, the
relevant tool reads the file at the path.

### 2.2 Tool-produced outputs

A tool that generates content writes it to disk and returns the path
as a `file_ref`. Two cases:

**Persistent outputs** — files the user (or another tool/agent in a
subsequent session) will use. Tools write under `<platform-root>/data/`,
choosing a sensible path. Examples:
- A report-generator tool writes `data/reports/2024-04-15-research-summary.md`.
- An image-processor tool writes `data/images/processed/<filename>.png`.

The tool returns the path as a `file_ref` in its result. The path is
relative to platform root (for portability — paths under `data/` work
when the platform installation is copied).

**Ephemeral outputs** — intermediate files the tool needs as scratch
but won't survive beyond the tool's execution. Tools use Python's
`tempfile` module and OS tempdir freely. No platform involvement.
Cleaned up by the tool or by the OS.

### 2.3 Runtime inputs

The caller of `execute` (CLI, API, or — later — a workflow node) passes
inputs in `ExecutionContext.trigger.inputs`. When an input's type is
`file_ref`, the value is a path string that the caller has access to
(typically a path the user typed, or a previously-produced file_ref
from another agent's output).

The platform validates the path against the path policy (Part 4) at
input-validation time. If the path is outside permitted locations, the
input is rejected with `file_access.denied` before the agent runs.

---

## Part 3 — The Content Store, Clarified

The platform's `<platform-root>/content/` directory (per Asset
Management contract §3.6) is the **engine-managed sidecar store** for
oversized event content. It is NOT a destination tools write to.

### 3.1 Why tools don't write to `content/`

Tools produce outputs the user might want to *see, organize, version-
control, share, edit*. Files in `content/` are hash-named immutable
blobs — usable only by code that knows the hash. They're the wrong
shape for tool outputs.

`data/` is for files with meaningful names that the user owns.
`content/` is for content the platform needs to preserve verbatim in
the event log when it's too large to inline.

The distinction matters because:
- A "generate report" tool writing to `data/reports/<name>.md` produces
  a file the user finds, opens, edits. Right shape.
- The same tool writing to `content/<hash>.bin` produces a file the
  user can't find without query, with no name and no extension. Wrong
  shape.

So: tools always write to `data/` (or OS tempdir for ephemerals); the
engine writes to `content/` invisibly when it needs to spill event
payloads. Two different mechanisms, two different consumers, no overlap.

### 3.2 When the engine writes to `content/`

Whenever an event's payload exceeds the inline threshold (~50KB):

- A tool returns a large result → engine writes the full result to
  `content/<hash>.bin`, emits `tool.result` with a truncated inline
  preview plus a `content_ref` pointing at the hash.
- A memory item stores large content → similar mechanism.
- A future tool reads a large file via `file_ref` and the content
  enters an event → similar.

The content store is "spillover" for the event log's inline limits.
It's a Materialization Rule satisfaction mechanism; not a user-facing
filesystem location.

### 3.3 Tools that need to surface large files

A tool processing a large file (say, generating a 10MB PDF) writes the
PDF to `data/reports/<name>.pdf`. The tool's `tool.result` returns the
`file_ref` (a string — the path) — this is small. The actual 10MB PDF
is at the path in `data/`, accessible to whoever wants to open it.

The content store isn't involved here. The event carries the path
(small string); the file lives at the path. Replay re-reads the file
at the path — if it still exists, replay sees what the agent
produced; if not, replay sees the missing-file state.

This is the cost of "files in `data/` are not archived" (Part 5
below) — the user owns `data/` and is responsible for its persistence.

---

## Part 4 — Path-Access Policy

Tools that read or write files are subject to a path-access policy.
This is independent of action-level Authorization (which gates *whether
a tool can be called at all*); this gates *which paths a tool may
touch*.

### 4.1 The policy shape

In `platform.yaml`:

```yaml
file_access:
  allowed_paths:
    - "bundles/"
    - "data/"
    - "sessions/"
    - "content/"
    - "logs/"
  denied_paths:
    - ".env"
    - "platform.yaml"
    - ".platform/"
```

Two lists. Both lists hold path prefixes (relative to platform root)
or absolute paths.

The platform pre-populates `allowed_paths` on first run with the
standard folders (the ones in the example above). After first run, the
list is the user's — adding entries grants access to additional
locations; removing entries revokes access.

### 4.2 Evaluation

Given a requested path `P` (resolved to an absolute path):

1. Normalize `P` (resolve `..`, follow symlinks if policy says to —
   v1 says it does, to prevent symlink-based escape).
2. **Check denied_paths first.** If `P` is under any denied prefix or
   equals any denied path: **deny**.
3. **Check allowed_paths.** If `P` is under any allowed prefix or
   equals any allowed path: **allow**.
4. **Default deny.** Neither matched: **deny**.

Denies always win. The user adding `.env` to `allowed_paths` would NOT
grant access — the deny rule still wins. This is intentional defense-
in-depth for sensitive files.

### 4.3 What's denied by default and why

Three default denies. Each has a real reason.

**`.env`** — secret values. The Secrets Provider is the legitimate
mechanism for tool access to secrets, via `ToolContext.secrets`. Reading
`.env` directly bypasses that mechanism and lets secrets leak into tool
output (which gets captured in `tool.result` events, violating the
"secrets never appear in events" invariant).

**`platform.yaml`** — platform configuration. Contains the
authorization rule list, the path policy itself, potentially provider
configurations with sensitive defaults. A tool reading this could
exfiltrate the security configuration. (And in some future variant
where bundle configs nest in here, could leak more.)

**`.platform/`** — platform-internal state. Caches, indexes,
scratch. Not for tools.

### 4.4 What's allowed by default and why

Five default allows. The user can trim these on the principle of least
privilege.

**`bundles/`** — installed bundles. Tools and skills need to read
their own bundle's supporting files (a tool's helper script, a skill's
reference document). This must work or bundles can't ship code.

**`data/`** — the user's working files. The whole point of `data/`
is that agents and tools access it.

**`sessions/`** — session event logs. Whether tools should read these
is genuinely debatable. The argument FOR: a "review past sessions"
tool needs access. The argument AGAINST: most tools shouldn't see
historical execution data. v1 allows by default; users can remove
this entry if they prefer tighter control.

**`content/`** — engine-managed content store. Tools generally
shouldn't need to read here (they get content inline from
`tool.result`'s `result` field), but some tools that work with
content_refs (e.g., a "render hash content" tool) might. Allowed by
default; removable.

**`logs/`** — platform diagnostic logs. Same logic as `sessions/` —
useful for some tools, mostly not needed. Allowed by default.

### 4.5 Adding entries (user grants)

The user adds paths to `allowed_paths` for access outside the standard
folders. Examples:

```yaml
file_access:
  allowed_paths:
    - "bundles/"
    - "data/"
    - "sessions/"
    - "content/"
    - "logs/"
    - "/Users/me/projects/research/"      # absolute path to a project
    - "../shared-team-data/"               # relative to platform root
  denied_paths: [...]
```

Adding a path grants tool access to anything under it. The path is
matched by prefix — granting `/Users/me/projects/research/` grants
access to everything under that directory.

### 4.6 Where the check happens

The four builtin tools (`read`, `write`, `edit`, `bash`) — see §4.7
for `bash`'s exception — perform the path check at invocation time.
On denial, the tool fails with `file_access.denied` (Error Model
catalog §4.11).

*(Phase 2.0, TOOL-R2.)* The Phase 2.0 builtins ship as
`read_file`, `write_file`, `list_files`, and `bash` in
`bundles/kolsch/tools/` (see `docs/explanation/tools-subsystem.md`
§4.5). `file_access.allowed_paths` constrains the three file-touching
tools (`read_file`, `write_file`, `list_files`) — out-of-scope paths
surface as `file.access_denied` per Error Model §4.16, the same
mechanism described in §4.7 for the Phase 1 set. `bash`'s exemption
from path policy (§4.7 below) carries over unchanged; Phase 2.0's
workspace-only default rule set (patch §3.1) defaults `tool.bash`
to the new `prompt` effect (per Authz Protocol §3 in
`docs/explanation/secrets-sandbox-authz-contract.md`) so the user
authorizes each command interactively rather than the path policy
trying — and failing — to constrain the shell.

For `file_ref` inputs to `execute`, the input validation layer checks
the path before the agent runs. Denial produces an error before the
exchange starts.

For tool-produced `file_ref` outputs (a tool says "I wrote `data/X.md`"),
no additional check is needed — the tool just wrote the file via
`write`, which would have been denied if path wasn't permitted.

**Mechanism.** The path policy is loaded from `platform.yaml` (the
`file_access` section) at platform startup via Asset Management's
platform-bootstrap flow (Asset Management §3.10). Asset Management
exposes the policy via its `get_platform_config` accessor.

The builtin tools (`read`, `write`, `edit`) hold a reference to the
path policy at registration time. Each invocation calls a check
function:

```python
def is_path_allowed(path: str) -> tuple[bool, str | None]:
    # Normalize path (resolve .., follow symlinks).
    # Check denied_paths; return (False, "denied: rule X").
    # Check allowed_paths; return (True, None) if matched.
    # Default: return (False, "default deny").
```

The check function is pure (no side effects, no I/O beyond symlink
resolution). Path normalization MUST resolve `..` segments and follow
symlinks before policy evaluation, to prevent escape via symlink
(per Fh-I4).

### 4.7 The `bash` exception

`bash` runs arbitrary shell commands. A bash command can read or write
anywhere the platform's process can. v1's `native` sandbox does NOT
constrain this.

**Path policy does NOT apply to `bash` in v1.** This is a real
limitation, not a bug.

Practical implication: a user who allows `tool.bash` in their
Authorization rules is accepting that bash bypasses path policy. The
user wanting strict path-based control should:

- Remove `bash` from their agents' tool lists (via the Tools subsystem
  config), OR
- Add `who: ".*", what: "tool\\.bash", effect: "deny"` to their
  Authorization rules.

When real sandboxing arrives (a future Sandbox variant), bash will run
inside a sandbox that enforces filesystem isolation matching the path
policy. For v1, this is an honest gap.

The v1 scope document should note this gap. The path policy is
"best-effort for `read`/`write`/`edit`; bypassable via `bash`."

---

## Part 5 — File Fidelity in Events

When a `file_ref` value appears in an event (a `user.message` input, a
`tool.result` output, a `config.set` field), the event captures **the
path string** — not the file's content.

### 5.1 What this means for replay

Replay reads the file at the path. If the file is unchanged since the
session ran, replay sees what the agent saw. If the file was modified
or deleted, replay sees the modified or missing state.

There is **no automatic archival** of referenced files. The platform
does not copy `data/<path>` into `content/` when an agent references
it. Files in `data/` are user-managed; their persistence is the user's
responsibility.

### 5.2 When this matters and when it doesn't

For most v1 use cases, this is fine. The interactive flow is:
- User runs an agent on a file.
- Agent does work, produces output.
- User looks at the output.

Session resumption a week later, with files unchanged, replays
identically. Session resumption after the user has edited the
referenced file replays against the new content.

Where it would matter: forensic-style "what exactly did the agent see
on Tuesday?" replay, after the user has modified files. v1 doesn't
support this fidelity guarantee for `data/` files. A future
archival-on-reference variant could; v1 doesn't.

### 5.3 The content store as fidelity for engine outputs

For files in `content/` — tool results too large to inline — the
content store IS the fidelity guarantee. The content is immutable; the
hash identifies it; replay can reconstruct exactly what the engine
captured.

So fidelity divides cleanly:
- **`content/`** content: byte-exact, immutable, replay-safe.
- **`data/`** content: snapshot-by-reference, user-mutable, replay
  reads current state.

This is honest. The user knows the rules: outputs the platform
generated are preserved; the user's own files are the user's
responsibility.

---

## Part 6 — Conformance Invariants

### Fh-I1 — `file_ref` is a string

The `file_ref` TypeSpec value's runtime representation is a Python
`str` containing a filesystem path. Implementations MUST NOT introduce
structured envelopes or wrappers at the Python level.

### Fh-I2 — Path policy enforcement

The builtin `read`, `write`, and `edit` tools MUST enforce the path-
access policy before performing any filesystem operation. Denied paths
MUST raise `file_access.denied`.

### Fh-I3 — Denies override allows

In path policy evaluation, `denied_paths` MUST be checked before
`allowed_paths`. A path matching both is denied.

### Fh-I4 — Path normalization

Paths MUST be normalized (resolving `..`, following symlinks) before
policy evaluation. Implementations MUST NOT allow symlink-based
bypass — a symlink pointing into a denied location MUST be treated as
the denied location, not the symlink path.

### Fh-I5 — Tools never write to `content/`

The builtin tools MUST NOT write to `<platform-root>/content/`. Only
the engine writes there, and only for event-content sidecars. This is
a contract obligation on tool authors.

### Fh-I6 — `bash` path-policy gap is documented

Implementations MUST document `bash`'s exemption from path policy in
v1's `native` sandbox. Users MUST be able to understand that allowing
`tool.bash` allows arbitrary filesystem access regardless of path
policy.

### Fh-I7 — Default policy is pre-populated, then user-owned

On first run with no `platform.yaml`, the platform MUST generate one
with the standard `allowed_paths` populated. On subsequent runs, the
platform MUST NOT modify the file's policy lists. The lists are the
user's after first run.

### Fh-I8 — `file_ref` validation at input time

When `file_ref` typed values appear in `execute` inputs (single_shot
mode) or in conversational messages, the platform MUST validate them
against the path policy BEFORE the agent runs. Denied paths produce an
error at the boundary, not during execution.

### Fh-I9 — Tool outputs honor `data/` convention

Builtin tools and platform-shipped tools that produce persistent file
outputs MUST write to paths under `<platform-root>/data/`. Tools MAY
use OS tempdir for ephemeral outputs. Tools MUST NOT write to
`<platform-root>/content/`.

### Fh-I10 — No automatic archival

The platform MUST NOT automatically copy files referenced via
`file_ref` into the content store or any other archival location.
Replay sees current file state.

---

## Part 7 — Open Items

1. **Path policy and `bash`.** §4.7 documents the gap. A real sandbox
   variant (Phase 2+) closes it. Worth noting in the v1 scope's
   "explicitly excludes" list.

2. **Symlink handling subtleties.** §4.4 mandates symlink resolution
   before policy check. But what if a symlink at `data/<link>` points
   to `/etc/passwd` (denied)? The resolution leads to denial — correct.
   What about a symlink that resolves to *outside* anything in the
   policy lists? Default-deny catches it. What about race conditions
   between symlink-resolution and file-open (TOCTOU)? v1 accepts the
   small TOCTOU window; future variants in real sandboxes close it.

3. **First-run policy file generation.** §4.1 says the platform
   generates `platform.yaml` on first run. The exact mechanism (auto-
   generate on startup if missing, or require a `init` command, or
   ship a template) is v1 implementation detail. Defer.

4. **Cross-platform paths.** v1 uses forward slashes; Python's pathlib
   normalizes on Windows. Edge cases (drive letters, UNC paths) are
   implementation-tested rather than spec-defined.

5. **`file_ref` validation depth.** v1 validates only that the path is
   a valid string and matches policy. It does NOT validate that the
   file exists, is readable, has expected mime type, etc. Tools that
   need those guarantees check at use time and surface errors as tool
   failures. Worth noting so a stricter future variant has a clear
   path.

6. **Future structured file_ref.** §1.1 leaves the door open for a
   richer file_ref envelope with metadata and hashes. Non-breaking
   addition: a future variant could be `file_ref_v2` or could
   accept both string and object forms. Not needed in v1.

7. **`bash` policy enforcement at a higher level.** Even though `bash`
   itself can't enforce path policy, a wrapper tool could (running
   `bash` in a chrooted subprocess, for instance). This is a tool-
   level concern, not a platform-level one. Tool authors who want
   sandboxed-bash can build it.
