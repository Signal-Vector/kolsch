# Phase 1 — The Kernel/Resolver Contract

**Draft 0.1**

> The kernel: two pieces, both pure. The Config Schema (defined in Phase 1
> Schemas §A) and the Config Resolver (the layered partial-merge function).
> Plus a small validation pipeline that orchestrates three layers of
> validation owned by three different parties.
>
> The kernel depends on nothing. Every other module imports it. It is the
> foundation the rest of the system merges down onto.

---

## Part 0 — Decisions Locked

- **JSON Merge Patch (RFC 7386)** is the merge algorithm. `null` deletes;
  objects deep-merge; non-objects replace; omission inherits.
- **Pydantic models are canonical**; JSON Schema is exported from them when
  external consumers need it.
- **Three-layer validation with three owners:**
  - Layer 1 — structural / schema validation, owned by the kernel.
  - Layer 2 — cross-cutting / system-concern validation, owned by system
    modules that touch specific fields.
  - Layer 3 — subsystem variant validation, owned by the named variant.
- **Layer 1 runs twice** — on each partial before merge, on the resolved
  config after merge.
- **Layer 3 is delegated by name** — the kernel looks up the variant
  validator from a map passed in by the caller.
- **Validators are passed in by the caller.** No global registry. The
  kernel stays pure — no state, no I/O, no dependencies.
- **Validators return structured errors** that identify the layer and the
  specific problem.
- **The resolver operates on dicts**, not Pydantic models, to preserve the
  RFC-7386-critical distinction between "key absent" (inherit) and "key
  present with `null`" (delete). Validation converts dict <-> Pydantic at
  the boundaries.
- **Strict same-version requirement** in v1 — all partials must have the
  same `schema_version`. Migration is deferred to Phase 2 with a reserved
  hook.

---

## Part 1 — The Two Pieces and Their Purity

The kernel consists of:

1. **The Config Schema.** Pydantic v2 model definitions for `AgentConfig`
   and its sub-types (`ModelSpec`, `IOSpec`, `PromptSpec`, `SubsystemConfig`,
   etc.), per Phase 1 Schemas §A.
2. **The Config Resolver.** A pure function that merges an ordered list of
   partial configs into a resolved config, per RFC 7386.

Plus a thin pipeline that orchestrates validation around the resolver.

**Purity property.** The kernel has zero runtime dependencies on other
modules of the platform. It depends only on Pydantic (the library) and
the Python standard library. It performs no I/O — no filesystem access,
no network, no Postgres, no Redis. It holds no state — no caches, no
registries, no global variables.

This purity is load-bearing. Every other module of the platform imports
the kernel. If the kernel had dependencies that mattered, the dependency
graph would cycle or stratify wrong. The kernel's purity is what keeps
the rest of the architecture's downward-dependency rule survivable.

---

## Part 2 — The Schema (Layer 1)

The kernel owns the structural shape of an AgentConfig. The schema is
defined in Phase 1 Schemas §A; this contract does not re-state the
fields. The kernel exports the Pydantic types and validates against them.

### 2.1 What the kernel validates structurally

For any partial or resolved config (treated as a dict), the kernel
checks:

- **Field names exist** in the schema (no unknown top-level fields, no
  unknown fields in known nested objects — unless explicitly an
  open-map type like `parameters`).
- **Types match.** Strings are strings; integers are integers; enums are
  one of the declared values; lists are lists of the declared element
  type.
- **Required fields are present** (only checked on the resolved config;
  partials may omit anything by definition).
- **Identifiers are well-formed.** `id` fields match their prefix
  pattern (`ag_<uuid>`, `ses_<uuid>`, etc.). The kernel knows the
  prefix conventions.
- **Subsystem wrappers are well-formed.** Each entry in `subsystems`
  has a `variant` (string) and a `config` (object). The CONTENTS of
  the `config` body are NOT checked at Layer 1 — that's Layer 3.

### 2.2 What Layer 1 does NOT check

- Whether `model.provider` is a provider this platform supports (Layer 2).
- Whether file paths in `prompt.layers[*].ref` actually resolve to files
  (Layer 2).
- Whether tool or skill names referenced exist in the user's setup
  (Layer 2).
- Whether `subsystems.memory.config` is valid for the `recent_buffer`
  variant (Layer 3 — the Memory module's `recent_buffer` validator).

These are not the kernel's concern. They depend on knowledge the kernel
doesn't have (the running platform's installed providers, the user's
filesystem, the asset catalog).

### 2.3 JSON Schema export

The Pydantic models expose JSON Schema via `model_json_schema()`. The
exported JSON Schema is the machine-readable artifact other tools can
consume — e.g., the built-in agent's understanding of valid YAML, or
external clients in non-Python contexts. Phase 1 ships the JSON Schema
as a published artifact alongside the platform.

---

## Part 3 — The Resolver

### 3.1 Signature

```
def resolve(partials: list[dict]) -> dict
```

A pure function. Takes an ordered list of partial configs (each a dict).
Returns the resolved dict. Does not validate (validation is a separate
step in the pipeline, Part 4).

### 3.2 Merge semantics (RFC 7386)

The resolver merges partials left-to-right; later partials override
earlier. The merge is applied recursively to nested objects.

Per RFC 7386:
- If both values are objects, merge recursively, key by key.
- If a later value is `null`, the key is deleted from the result.
- Otherwise, the later value replaces the earlier (scalars, arrays, and
  mixed-type cases all replace wholesale — no deep merge of arrays).
- A key absent from a later partial is unchanged (inherits the earlier
  value).

### 3.3 Why dicts, not Pydantic models

The resolver operates on dicts (Python `dict` instances after
JSON/YAML parse), not Pydantic models. This is a deliberate choice.

The critical RFC 7386 distinction is:
- **Absent key**: inherit from a lower layer.
- **Key present with value `null`**: delete (override-to-absent).

Dicts preserve this naturally (`"key" not in d` vs.
`d["key"] is None`). Pydantic models *lose* this distinction by default
— a model with `model: Optional[ModelSpec] = None` cannot tell the
difference between "the user didn't specify model" and "the user
specified `model: null`".

So validation is the boundary: a dict comes in, is validated against
the Pydantic schema (which preserves enough information to round-trip),
gets merged as a dict, and is validated again on output. The merge step
itself is dict-on-dict.

(Pydantic's `model_dump(exclude_unset=True)` is the conversion that
preserves the distinction — fields that weren't set in the input are
omitted from the dump, distinct from fields set to `None`.)

### 3.4 Determinism

The resolver is deterministic: given the same `partials` list, it
produces the same output, byte-identical. No randomness, no clock,
no environment-dependent behavior.

This is enforceable by the resolver being a pure function over inputs.
Implementations MUST NOT introduce non-determinism (e.g., by iterating
dict keys in insertion order on one platform and hash order on another;
Python 3.7+ guarantees insertion-order iteration which makes this
non-issue, but worth stating).

### 3.5 N-layer composition

The resolver takes any number of partials. Phase 1 typically resolves
three layers: `[system_defaults, agent_definition, call_context]`.
Phase 2 typically resolves five:
`[system_defaults, agent_definition, workflow_definition, workflow_instantiation, call_context]`.

The resolver code is identical; only the list length differs. Callers
construct the list in the appropriate order for their phase.

---

## Part 4 — The Validation Pipeline

The orchestrated flow that produces a validated resolved config. The
kernel performs Layer 1; delegates Layer 3 to subsystem variant
validators by name; runs Layer 2 system validators as supplied by the
caller.

### 4.1 The pipeline

```
def resolve_and_validate(
    partials: list[dict],
    subsystem_validators: dict[str, dict[str, ConfigValidator]],
    system_validators: list[ConfigValidator] = [],
) -> ResolvedAgentConfig
```

Where `ConfigValidator = Callable[[dict], ValidationResult]`.

The flow:

1. **Layer 1 (structural) on each partial.** For each partial in
   `partials`, validate against the Pydantic schema treating all fields
   as optional (it's a partial). Reject if any partial is malformed
   structurally.
2. **Same-version check.** All partials with a `schema_version` field
   must have the same value. v1 does not migrate between versions.
3. **Merge.** Call `resolve(partials)` to produce the resolved dict.
4. **Layer 1 on the resolved config.** Validate against the full
   Pydantic schema (required fields now enforced). Reject if missing
   required fields or any structural issue.
5. **Layer 3 (subsystem variants).** For each entry in `resolved.subsystems`:
   - Look up `subsystem_validators[<subsystem_name>][<variant_name>]`.
   - If absent: reject with an error naming the unknown variant.
   - Call the validator with the entry's `config` body. Reject on
     validator failure.
6. **Layer 2 (system concerns).** For each validator in
   `system_validators`, call it with the resolved config. Reject on any
   validator failure.
7. **Return.** A `ResolvedAgentConfig` — a Pydantic instance constructed
   from the resolved dict, with all validation passed.

### 4.2 Error aggregation

Validators return `ValidationResult`:

```
ValidationResult
├── ok          bool
└── errors      list[ValidationError]    — empty if ok

ValidationError
├── layer       Literal[1, 2, 3]
├── source      string    — e.g. "kernel", "memory.recent_buffer",
│                           "agent_engine.provider_check"
├── path        string    — JSON Pointer to the field, e.g.
│                           "/subsystems/memory/config/scope"
├── code        string    — short machine-readable code, e.g.
│                           "missing_required", "unknown_variant",
│                           "invalid_provider"
└── message     string    — human-readable description
```

The kernel aggregates errors across layers. Validation does NOT fail
fast: if Layer 1 succeeds on the resolved config, the kernel runs ALL
Layer 3 validators and ALL Layer 2 validators before returning, so the
user sees every problem at once. (Layer 1 on partials does fail fast —
a malformed partial is rejected before merging is attempted.)

The aggregated `ValidationResult` is raised as `ConfigValidationError`
if any errors exist; otherwise the resolved config is returned.

### 4.3 Validator contract

A `ConfigValidator` is a function `(input: dict) -> ValidationResult`.
What `input` is depends on the layer:

- **Layer 3 validators** receive the `config` body of a subsystem entry
  (the dict inside `{variant, config}`). They check that body for their
  variant's requirements.
- **Layer 2 validators** receive the full resolved config dict. They
  may inspect any field.

Validators are pure functions in spirit — they should not perform
external I/O (no filesystem access, no network) and should produce the
same result given the same input. The contract permits a Layer 2
validator to read from a stable in-memory catalog (e.g., "list of
supported providers"), but it MUST NOT make network calls or block.
Validation is fast and deterministic.

### 4.4 Who supplies the validators

The kernel takes validators as parameters. The caller — typically the
asset-management harness when it loads a setup, or the Agent Engine
when it prepares a config for execution — assembles them.

Concretely:
- Each subsystem (Memory, Tools, Skills) ships variant validators
  alongside its variant implementations. The caller registers the
  validators it needs based on what's installed.
- System validators are registered by the modules that own them. The
  Agent Engine contributes a provider-check validator. The asset
  manager contributes a file-path-resolution validator. The caller
  composes the list.

This keeps the kernel stateless: no global registry of validators, no
side-effecting registration step. The kernel is a pure tool; the
caller chooses how it's used.

---

## Part 5 — Interface

All functions are synchronous (no I/O, nothing to await).

```
# Layer 1 — structural validation owned by the kernel
def validate_partial(partial: dict) -> ValidationResult
    """Validate a partial against the schema (all fields optional)."""

def validate_resolved(resolved: dict) -> ValidationResult
    """Validate a resolved config against the schema (required fields enforced)."""

# The pure merge
def resolve(partials: list[dict]) -> dict
    """RFC 7386 merge of an ordered list of partial dicts."""

# The orchestrated pipeline
def resolve_and_validate(
    partials: list[dict],
    subsystem_validators: dict[str, dict[str, ConfigValidator]],
    system_validators: list[ConfigValidator] = [],
) -> ResolvedAgentConfig
    """Validate partials, merge, validate resolved, run Layer 3 and Layer 2,
    return a validated ResolvedAgentConfig. Raises ConfigValidationError
    with aggregated errors on failure."""

# JSON Schema export
def export_json_schema() -> dict
    """Export the AgentConfig Pydantic schema as JSON Schema for external
    consumers (e.g., YAML linters, the built-in agent)."""
```

`ResolvedAgentConfig` is the Pydantic model instance corresponding to
the validated resolved config. It is what the Agent Engine receives.

### 5.1 What's not in the interface

- No `migrate(partial, from_version, to_version)`. Migration is Phase 2.
  The kernel raises on version mismatch in v1.
- No `register_validator(...)`. No global registry; validators are
  passed per call.
- No async variants. The kernel is pure-CPU; awaiting it is unnecessary.

---

## Part 6 — Conformance Invariants

What any kernel implementation must honor.

### I1 — Purity

The kernel MUST be a pure library with no runtime I/O, no external
state, no platform-module dependencies. It depends only on Pydantic and
the Python standard library. Tests run without any infrastructure.

### I2 — RFC 7386 conformance

The `resolve` function MUST implement JSON Merge Patch exactly. Compliance
is verifiable against the RFC's test vectors plus the additional cases
relevant to this platform (subsystem deletion via `null`, deeply nested
objects, mixed scalar/object overrides).

### I3 — Dict-based merge

`resolve` MUST operate on dicts (preserving the `absent` vs `null`
distinction). Implementations that convert through Pydantic models in
the middle of merging would lose this distinction and corrupt the
override semantics; this is forbidden.

### I4 — Determinism

`resolve(partials)` MUST produce byte-identical output for byte-identical
input. No clock reads, no environment-dependent ordering, no
implementation-specific variation.

### I5 — Three-layer validation owner discipline

The kernel MUST perform Layer 1 (structural). It MUST delegate Layer 3
to subsystem variant validators by name. It MUST run Layer 2 validators
supplied by the caller. It MUST NOT perform validation it doesn't own
(e.g., the kernel MUST NOT check whether a provider name is valid; that
is Layer 2).

### I6 — Layer 1 runs twice

The kernel MUST validate every partial structurally before merging, and
MUST validate the resolved config structurally after merging. The first
catches malformed partials at their source; the second ensures the
merge produced a valid output.

### I7 — Error aggregation across layers

When validation fails, the kernel MUST aggregate errors across all
layers run, not fail fast at the first error (except for the per-partial
Layer 1 check, which fails fast — a malformed partial is rejected
before merging is attempted). The user sees every problem in the
resolved-config validation at once.

### I8 — Same-version enforcement (v1)

All partials passed to `resolve_and_validate` MUST have the same
`schema_version` (or omit it, in which case they inherit). Mismatched
versions raise a `SchemaVersionMismatch` error. Migration between
versions is deferred to Phase 2.

### I9 — Validators are pure-in-spirit

Validators passed to `resolve_and_validate` SHOULD be pure functions
(no I/O, no blocking). The kernel does not enforce this at the type
level, but the contract requires it. A validator that violates this
breaks the kernel's overall purity property.

### I10 — Structured errors

Validators MUST return `ValidationResult` with structured
`ValidationError` entries (layer, source, path, code, message). The
kernel MUST NOT collapse these into bare strings. Tooling, the built-in
agent, and humans all rely on the structure to find the actual problem.

---

## Part 7 — Open Items

1. **Layer 1 strictness on unknown fields.** Should the kernel reject a
   partial that contains a field not in the schema, or accept it
   silently (forward compatibility with future schema additions)? v1
   recommendation: reject unknown fields at the top level of AgentConfig
   and its known sub-types, but allow them in open-map fields
   (`model.parameters`, subsystem `config` bodies). This balances
   strictness with future flexibility. Confirm before implementation.

2. **JSON Pointer format for error paths.** §4.2 uses JSON Pointer
   syntax (`/subsystems/memory/config/scope`). Pydantic's own error
   format is `loc` tuples (`["subsystems", "memory", "config", "scope"]`).
   The kernel translates between them. Implementation detail; flagged
   for awareness.

3. **The migration hook for Phase 2.** A future `migrate_partial(partial,
   target_version) -> dict` function would be added when the schema
   evolves. Its signature is reserved; v1 doesn't implement it. Whether
   migrations are owned by the kernel or by a separate module is a
   Phase 2 question.

4. **YAML loading.** The kernel takes dicts — it doesn't know about YAML
   files. YAML loading is the Asset Management harness's responsibility.
   The kernel is YAML-agnostic. Worth flagging because users author in
   YAML and may expect the kernel to handle it; it doesn't.

5. **Performance.** Layer 1 validation runs N+1 times (N partials + the
   resolved). For typical Phase 1 N=3 this is fine. If config-resolution
   ever becomes a hot path (many resolutions per second), caching could
   be added — Pydantic models can be reused, the dicts are small.
   Premature; flagged.

6. **The `ResolvedAgentConfig` type.** The Pydantic model returned by
   `resolve_and_validate`. Whether it's the same Pydantic type as
   `AgentConfig` or a separate type that signals "this has been
   validated through the full pipeline" is a small design choice. v1
   recommendation: same type, treated as "validated" by convention
   (the kernel's contract is that what it returns has been validated).

7. **Validator failure handling.** A validator function that itself
   *raises* an exception (rather than returning a `ValidationResult`
   with `ok=False`) is misbehaving. The kernel SHOULD catch such
   exceptions, convert them into a `ValidationError` with a special
   `code = "validator_internal_error"`, and continue running the
   remaining validators. Worth specifying — defensive against buggy
   validators.
