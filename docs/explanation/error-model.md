# Phase 1 — The Error Model

**Draft 0.1**

> Errors are emitted as events on the same append-only log as everything
> else (matches our streaming architecture) AND raised as Python
> exceptions with `__cause__` chaining (matches Python idiom). One model,
> two surfaces — events for the consumer of `execute_stream`, exceptions
> for the caller of `execute`.
>
> Three levels of error severity, classified explicitly. One canonical
> serialization shape used everywhere errors appear in events or APIs.
> A class hierarchy at the implementation level; a flat structured
> envelope at every external surface.

---

## Part 0 — Decisions Locked

- **Three-level error model**: Diagnostic, In-exchange Recoverable,
  Whole-exchange Fatal. Each error type is assigned to exactly one level.
- **Canonical Error shape**: `type` (dotted code), `message`, `where`
  (dotted-string locator), `detail` (type-specific structured data),
  `cause` (chained underlying error, optional).
- **`PlatformError` class hierarchy** at the implementation level. Every
  subclass has a `code` constant. All subclasses serialize to the
  canonical shape via `to_dict()`. Non-platform exceptions caught at the
  engine boundary get wrapped.
- **Errors at the execution boundary are events**, not exceptions. The
  streaming `execute_stream` yields events; the sync `execute` raises
  only on whole-exchange fatal level. Diagnostic and recoverable errors
  never reach the caller as exceptions.
- **The error type catalog** — every error type registered with its
  code, level, and detail shape. Subsystems own their error types.

---

## Part 1 — The Three Levels

Every error encountered during execution falls into exactly one of three
levels. The level determines what happens to the error: where it goes,
who sees it, whether execution continues.

### 1.1 Level 1 — Diagnostic

**What it is:** A tool errored, an MCP call returned a failure, a memory
operation failed in a way the agent can react to. The error is
information the model needs to see, but does not interrupt the exchange.

**Captured as:** `tool.failed` event (for tool errors), `memory.failed`
event (if introduced), or as `is_error: true` on a `tool.result` event
(for tool-result-format errors).

**What happens:** The error appears as data in the event log. The model
sees the failure as a tool result and can adapt its behavior. The
exchange continues.

**Python representation:** A `PlatformError` raised inside the tool's
implementation. The Agent Engine's tool dispatch wrapper catches it,
converts to event form, and emits.

**Examples:**
- A `read` tool called on a non-existent file. `tool.failed` event with
  `type: "tool.execution_failed"`, `detail.cause` carrying the underlying
  `FileNotFoundError`.
- An MCP server returned a 500. `tool.failed` with `type: "mcp.upstream_error"`.
- Memory write to a tag that exceeds a (future) quota.

**Caller sees:** Events in the stream, nothing else. No Python exception.

### 1.2 Level 2 — In-exchange Recoverable

**What it is:** A failure the platform itself retries — a malformed
model response, a transient network failure on a model call, a parse
error in tool argument JSON. The model does not directly see the
underlying failure; the platform's retry policy handles it.

**Captured as:** The failed attempt remains in the tree as siblings of
the eventual successful attempt (per Phase 1 Schemas §B.9 — retries-as-
natural-branching). A `model.response` with error data records the
specific failure; the retry attempt produces a fresh `model.request` →
`model.response` pair.

**What happens:** The platform retries up to `RuntimeSpec.max_retries`.
If a retry succeeds, the exchange continues; the failed attempts are
preserved in the tree but not on the path. If all retries are exhausted,
the error escalates to Level 3.

**Python representation:** Caught inside the engine's retry wrapper.
Logged via the platform's standard logging. Not exposed.

**Examples:**
- Anthropic API returned a 503 on the model call; engine retries.
- The model produced malformed JSON for a tool call; engine prompts the
  model to fix it (a "feed the error back to the model" recovery).
- A streaming model response cut off mid-token; engine retries from the
  last assistant message.

**Caller sees:** The retry attempts as events in the tree, if subscribed.
Otherwise the exchange just takes longer and succeeds.

### 1.3 Level 3 — Whole-exchange Fatal

**What it is:** A failure that ends the exchange. Authorization denied
at setup, retry budget exhausted, an unrecoverable error in the engine
itself, user cancellation.

**Captured as:** `thread.exchange_failed` event. This is the terminal
event of the exchange.

**What happens:** The exchange ends. The synchronous `execute` raises
`AgentExecutionError` carrying the `thread.exchange_failed` event in
full. The streaming `execute_stream` yields the `thread.exchange_failed`
and then ends (does not raise).

**Python representation:** `AgentExecutionError(failed_event=Event)`.
The event's `data.error` carries the canonical Error envelope.

**Examples:**
- Agent execution denied by Authorization. `thread.exchange_failed` with
  `type: "agent.unauthorized"`.
- All model-call retries exhausted. `thread.exchange_failed` with
  `type: "model.retry_exhausted"`, `detail.attempts` listing each retry.
- User cancelled via `CancelToken`. `thread.exchange_failed` with
  `type: "exchange.cancelled"`.
- A platform-internal invariant violation (unexpected state, bug).
  `thread.exchange_failed` with `type: "platform.internal_error"`,
  `detail.cause` carrying the original exception.

**Caller sees:** The terminal event (streaming) or the raised exception
(sync) carrying the same event. No information loss between the two
surfaces.

### 1.4 Level Assignment Discipline

Every error type defined in the platform MUST be assigned to exactly one
level. The level is part of the type's definition, not a runtime choice.

A type-level catalog (Part 4) lists every known type and its assigned
level. This is a normative registry — implementations consult it to know
which level to use when emitting.

If an error occurs that doesn't match any catalogued type, it is wrapped
in a generic platform error at Level 3 (fatal). This is the safe
default — unknown errors don't get silently swallowed at lower levels.

---

## Part 2 — The Canonical Error Shape

The serialization format used everywhere errors appear externally — in
events, in API responses, in CLI output, in log entries.

### 2.1 Shape

```
Error
├── type      string         — dotted code identifying the error type
├── message   string         — human-readable description
├── where     string         — dotted locator: "module.component.operation"
├── detail    object         — type-specific structured data
└── cause     Error?         — the underlying cause, recursively (nullable)
```

### 2.2 `type` — the dotted code

A string identifying the error type. Convention: `<scope>.<specific>`
where `<scope>` is the module or domain and `<specific>` is the specific
condition.

Examples:
- `tool.unauthorized`
- `tool.execution_failed`
- `tool.timeout`
- `memory.write_disabled`
- `memory.item_not_found`
- `secret.not_found`
- `config.invalid`
- `config.unknown_variant`
- `agent.unauthorized`
- `model.retry_exhausted`
- `exchange.cancelled`
- `platform.internal_error`

The codes are conventional strings, registered in the catalog (Part 4).
Implementations MUST use the registered codes; ad-hoc strings are a
contract violation.

### 2.3 `message` — human-readable

A short description suitable for display. One sentence preferred. Should
include enough specifics to be useful when read out of context:

- Bad: `"Tool failed"`
- Good: `"Tool 'web_search' failed: connection timeout after 60 seconds"`

The `detail` field carries the structured data; the `message` is the
narrative.

### 2.4 `where` — the dotted locator

A dotted string identifying where in the platform the error originated.
Convention: `<module>.<component>.<operation>`.

Examples:
- `tools.dispatch.invoke`
- `memory.recent_buffer.write`
- `engine.preflight.config_validation`
- `kernel.resolver.merge`
- `secrets.dotenv.resolve`

Free-form by the parser but conventional in practice. Easy to grep, easy
to filter programmatically by prefix (`where startswith "memory."`).

Implementations populate `where` at the throw site. Each error type's
documentation (Part 4) suggests typical `where` values, but the actual
value reflects the call site.

### 2.5 `detail` — type-specific data

A dict carrying structured data specific to the error type. The schema
of `detail` is part of the error type's definition (Part 4 enumerates
each).

Examples:

**`tool.unauthorized.detail`:**
```
{
    "tool_name": "bash",
    "action": "tool.bash",
    "principal": "agent.ag_abc123",
    "matched_rule": {"who": ".*", "what": "tool\\.bash", "effect": "deny"}
}
```

**`memory.item_not_found.detail`:**
```
{
    "item_id": "mem_xyz789",
    "session_id": "ses_abc123"
}
```

**`config.invalid.detail`:**
```
{
    "validation_errors": [
        {"layer": 1, "source": "kernel", "path": "/model", "code": "missing_required"},
        ...
    ]
}
```

Each error type documents its `detail` keys in the catalog. Consumers
can rely on the documented shape.

### 2.6 `cause` — the chained underlying error

The error that caused this one, if any, in the same canonical Error
shape. Recursive: a cause may itself have a cause.

Maps to Python's `__cause__` chain (set via `raise X from Y` syntax).
When a `PlatformError` is serialized, its `__cause__` (if itself a
`PlatformError`) is serialized as `cause`. Non-platform causes are
wrapped (Part 3.4).

Example chain — a tool registration fails because a secret is missing:

```json
{
    "type": "tool.setup_failed",
    "message": "Failed to register tool 'web_search'",
    "where": "tools.setup.register",
    "detail": {"tool_name": "web_search"},
    "cause": {
        "type": "secret.not_found",
        "message": "Secret 'SERPAPI_KEY' is not available",
        "where": "secrets.dotenv.resolve",
        "detail": {"name": "SERPAPI_KEY"},
        "cause": null
    }
}
```

The cause chain is preserved end-to-end. A reader of the outer error
can drill into the underlying reason without losing detail.

---

## Part 3 — The Python Implementation

### 3.1 The `PlatformError` base class

```python
class PlatformError(Exception):
    code: ClassVar[str] = "platform.unknown"
    level: ClassVar[Level] = Level.FATAL
    
    def __init__(
        self,
        message: str,
        *,
        where: str,
        detail: dict | None = None,
    ):
        super().__init__(message)
        self.where = where
        self.detail = detail or {}
    
    def to_dict(self) -> dict:
        cause = self.__cause__
        cause_dict = None
        if isinstance(cause, PlatformError):
            cause_dict = cause.to_dict()
        elif cause is not None:
            # Non-platform cause; wrap it
            cause_dict = _wrap_external_exception(cause).to_dict()
        
        return {
            "type": self.code,
            "message": str(self),
            "where": self.where,
            "detail": self.detail,
            "cause": cause_dict,
        }
```

Two class-level attributes: `code` (the canonical type string) and
`level` (the assigned severity level — Diagnostic / Recoverable /
Fatal).

**A clarification about ValidationError.** The Kernel's `ValidationError`
(per Kernel §4.2) — with fields `layer, source, path, code, message` —
is a *validation finding*, not a `PlatformError` subtype. ValidationErrors
are values inside the `detail` field of a `ConfigValidationError` (which
IS a PlatformError, with `code: "config.invalid"`).

So the structure is:

```
ConfigValidationError (PlatformError, code="config.invalid")
  .detail.validation_errors: [ValidationError, ...]
    ↑
    ValidationError is NOT a PlatformError; it's a value inside
    ConfigValidationError's detail.
```

This avoids conflating "the kind of error" (validation, at some
layer) with "the canonical platform error envelope."

### 3.2 Subclassing

Subclasses set `code` and `level`, optionally override `__init__` to
require specific `detail` keys.

```python
class ToolUnauthorized(PlatformError):
    code = "tool.unauthorized"
    level = Level.DIAGNOSTIC
    
    def __init__(
        self,
        tool_name: str,
        action: str,
        principal: str,
        matched_rule: Rule | None = None,
        where: str = "tools.dispatch.invoke",
    ):
        super().__init__(
            message=f"Authorization denied for tool '{tool_name}'",
            where=where,
            detail={
                "tool_name": tool_name,
                "action": action,
                "principal": principal,
                "matched_rule": matched_rule.to_dict() if matched_rule else None,
            },
        )
```

The subclass enforces detail shape via its constructor signature. The
`detail` dict in `to_dict()` output matches the catalog's documented
shape for `tool.unauthorized`.

### 3.3 Cause chaining

Standard Python `raise X from Y` syntax. The platform conventions:

- **Wrap when meaningful:** if the receiving module's context adds
  meaning, wrap.
  ```python
  try:
      secret_value = await secrets.resolve(name)
  except SecretNotFound as e:
      raise ToolSetupFailed(
          tool_name=tool.name,
          where="tools.setup.resolve_secrets",
      ) from e
  ```

- **Propagate when transparent:** if the receiving module is just a
  pass-through, let the original error propagate. Don't wrap to wrap.

The `cause` chain in the serialized output makes the relationship
visible.

### 3.4 External exception wrapping

Non-PlatformError exceptions (e.g., a stdlib `FileNotFoundError`, a
third-party library's exception) caught at the engine boundary get
wrapped in a generic `WrappedError`:

```python
class WrappedError(PlatformError):
    code = "platform.wrapped"
    level = Level.DIAGNOSTIC   # default; may be reassigned by the wrapper
    
    @classmethod
    def from_exception(cls, exc: Exception, where: str) -> "WrappedError":
        return cls(
            message=f"{type(exc).__name__}: {exc}",
            where=where,
            detail={
                "original_type": f"{type(exc).__module__}.{type(exc).__name__}",
                "args": list(exc.args),
            },
        )
```

The wrapper records the original exception's type and message in
`detail`. The caller can still inspect `__cause__` for the underlying
Python object if needed in-process.

### 3.5 The Level enum

```python
from enum import Enum

class Level(str, Enum):
    DIAGNOSTIC = "diagnostic"          # Level 1
    RECOVERABLE = "recoverable"        # Level 2
    FATAL = "fatal"                    # Level 3
```

String-valued enum so it serializes naturally.

---

## Part 4 — The Error Type Catalog

Every known error type, organized by owning module. Each entry specifies
`code`, `level`, `detail` shape, and typical `where`.

This is a normative registry. New error types are added by amending this
catalog as new modules ship. Implementations MUST use these registered
types and MUST NOT invent ad-hoc codes.

### 4.1 Kernel / Resolver

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `config.invalid` | Fatal | `{validation_errors: [ValidationError]}` | `kernel.resolver.validate` |
| `config.unknown_variant` | Fatal | `{subsystem, variant}` | `kernel.resolver.validate` |
| `config.version_mismatch` | Fatal | `{expected, actual}` | `kernel.resolver.validate` |
| `config.merge_failed` | Fatal | `{layer_index, key_path}` | `kernel.resolver.merge` |

### 4.2 Session/Run Store

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `session.not_found` | Fatal | `{session_id}` | `store.session.get` |
| `thread.not_found` | Fatal | `{thread_id}` | `store.thread.get` |
| `store.lock_timeout` | Fatal | `{session_id, timeout_seconds}` | `store.events.append` |
| `store.write_failed` | Fatal | `{session_id, reason}` | `store.events.append` |
| `store.corrupt_log` | Fatal | `{session_id, line_number, parse_error}` | `store.events.read` |

### 4.3 Event Bus

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `bus.publish_failed` | Diagnostic | `{topic, transport_error}` | `bus.publish` |
| `bus.unavailable` | Diagnostic | `{reason}` | `bus.connect` |

(Note: bus errors are Diagnostic because the bus is best-effort —
losing a bus message doesn't fail an exchange.)

### 4.4 Agent Engine

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `agent.unauthorized` | Fatal | `{agent_id, action, principal, matched_rule}` | `engine.preflight.authorize` |
| `agent.capability_mismatch` | Fatal | `{required, available}` | `engine.preflight.check_capabilities` |
| `model.retry_exhausted` | Fatal | `{attempts: [Error]}` | `engine.execute.model_call` |
| `model.malformed_response` | Recoverable | `{raw_response, parse_error}` | `engine.execute.model_call` |
| `model.upstream_error` | Recoverable | `{provider, http_status, response}` | `engine.execute.model_call` |
| `exchange.cancelled` | Fatal | `{cancelled_at_event_id}` | `engine.execute.loop` |
| `exchange.timeout` | Fatal | `{timeout_seconds, elapsed_seconds}` | `engine.execute.loop` |
| `assembly.procedure_failed` | Fatal | `{assembly_version, failed_step}` | `engine.assemble_context` |
| `platform.internal_error` | Fatal | `{original_type, args}` | (varies) |

### 4.5 Tools

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `tool.unauthorized` | Diagnostic | `{tool_name, action, principal, matched_rule}` | `tools.dispatch.invoke` |
| `tool.not_found` | Fatal | `{tool_name, agent_id}` | `tools.setup.register` |
| `tool.setup_failed` | Fatal | `{tool_name, reason}` | `tools.setup.register` |
| `tool.invalid_arguments` | Diagnostic | `{tool_name, validation_errors}` | `tools.dispatch.invoke` |
| `tool.execution_failed` | Diagnostic | `{tool_name, error_class}` | `tools.dispatch.execute` |
| `tool.timeout` | Diagnostic | `{tool_name, timeout_seconds}` | `tools.dispatch.execute` |
| `mcp.server_unreachable` | Diagnostic | `{server_name, transport_error}` | `tools.mcp.spawn` |
| `mcp.upstream_error` | Diagnostic | `{server_name, tool, response}` | `tools.mcp.invoke` |
| `mcp.protocol_error` | Diagnostic | `{server_name, expected, actual}` | `tools.mcp.invoke` |

### 4.6 Memory

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `memory.write_disabled` | Diagnostic | `{agent_id, reason}` | `memory.context.write` |
| `memory.item_not_found` | Diagnostic | `{item_id, session_id}` | `memory.context.delete` |
| `memory.session_unbound` | Fatal | `{session_id}` | `memory.context.operation` |
| `memory.compaction_failed` | Recoverable | `{events_count, target_tokens, cause}` | `memory.context.compact` |
| `memory.invalid_tag` | Fatal | `{tag, reason}` | `memory.validate` |

### 4.7 Secrets

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `secret.not_found` | Fatal | `{name}` | `secrets.dotenv.resolve` |
| `secret.read_failed` | Fatal | `{path, os_error}` | `secrets.dotenv.load` |

### 4.8 Sandbox

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `sandbox.policy_invalid` | Fatal | `{policy, reason}` | `sandbox.execute` |
| `sandbox.execution_denied` | Diagnostic | `{reason}` | `sandbox.execute` |
| `sandbox.resource_exceeded` | Diagnostic | `{resource, limit, observed}` | `sandbox.execute` |

(Most sandbox errors are Diagnostic because they surface as tool
failures the model can react to. Policy validation errors are Fatal
because they're config issues that prevent setup.)

### 4.9 Authorization

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `authz.malformed_rule` | Fatal | `{rule_index, error}` | `authz.rule_based.load` |
| `authz.regex_invalid` | Fatal | `{rule_index, field, regex, error}` | `authz.rule_based.compile` |

(Note: authorization denial itself is NOT an error type here — it is a
structured `AuthorizationResult`. Only configuration problems in the
auth provider produce errors.)

### 4.10 Skills

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `skill.not_found` | Fatal | `{ref}` | `skills.discovery` |
| `skill.malformed` | Fatal | `{path, parse_error}` | `skills.discovery.parse` |
| `skill.frontmatter_invalid` | Fatal | `{path, validation_errors}` | `skills.discovery.validate` |
| `skill.forced_not_enabled` | Fatal | `{ref, agent_id}` | `engine.preflight.force_invoke` |

### 4.11 File Handling

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `file_access.denied` | Diagnostic | `{path, reason, matched_rule}` | `tools.builtin.<read\|write\|edit>` |
| `file_access.path_normalization_failed` | Diagnostic | `{path, os_error}` | `tools.builtin.<read\|write\|edit>` |
| `file_access.policy_load_failed` | Fatal | `{config_section, error}` | `assets.bootstrap.load_file_policy` |

### 4.12 Generic platform

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `platform.wrapped` | (varies) | `{original_type, args}` | (varies) |
| `platform.internal_error` | Fatal | `{original_type, args, traceback?}` | (varies) |

### 4.13 LLM Configuration (Phase 1.1, LLM-R6)

Errors from the `build_llm` helper, the connector registry, and the
CLI `connect`/`refresh` flows that wire LLM providers into the
platform. The kernel resolver also emits `config.unknown_alias` here
because alias resolution is part of `ModelSpec` Layer 1 validation
(Schemas §A.4.1).

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `llm.provider_not_configured` | Fatal | `{provider, configured_providers}` | `llm.build_llm` |
| `llm.secret_missing` | Fatal | `{provider, secret_name}` | `llm.build_llm` (chains `secret.not_found`) |
| `llm.client_init_failed` | Fatal | `{provider, model, reason}` | connector's `build_llm` |
| `config.unknown_alias` | Fatal | `{alias, available_aliases}` | `kernel.resolver.normalize_model_spec` |
| `llm.connector_unknown` | Fatal | `{type, registered_types}` | `llm.registry.get` |
| `llm.discovery_failed` | Recoverable | `{provider, reason}` | connector's `discover_models` (CLI `connect`/`refresh`) |
| `llm.validation_failed` | Fatal | `{provider, reason}` | CLI `connect` flow (`validate_credentials` returned ok=False) |

### 4.14 Workspace (Phase 1.2, UX-R6)

Errors surfacing from the workspace resolver and the `kolsch setup`
first-run flow. `workspace.not_initialized` is the **only** error a
user should ever see from a missing workspace — the CLI intercepts
it before runtime bootstrap and emits a friendly "run `kolsch setup`"
message (exit code 3, no traceback) per Phase 1.2 patch §1.4.

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `workspace.not_initialized` | Fatal | `{path, env_var_set}` | CLI dispatch (pre-bootstrap) |
| `workspace.permission_denied` | Fatal | `{path, operation}` | `kolsch setup` step B; runtime bootstrap |
| `setup.aborted` | Recoverable | `{step, reason}` | `kolsch setup` after a user Ctrl-C — informational only |
| `setup.starter_exists` | Recoverable | `{path}` | `kolsch setup` step D when the target file exists and the user declines overwrite |

---

## Part 5 — How Errors Appear in Events

Three event types in the Phase 1 Schemas carry error data. Each uses
the canonical Error shape.

### 5.1 `tool.failed.data.error`

A tool invocation failed at Diagnostic level. The error tells the model
what went wrong; the model can adjust.

```
data:
  tool_call_id   string
  tool_name      string
  error          Error          — canonical shape
```

Example:

```json
{
    "type": "tool.failed",
    "data": {
        "tool_call_id": "call_abc",
        "tool_name": "web_search",
        "error": {
            "type": "tool.execution_failed",
            "message": "Connection timed out after 60 seconds",
            "where": "tools.dispatch.execute",
            "detail": {"tool_name": "web_search", "error_class": "TimeoutError"},
            "cause": null
        }
    }
}
```

### 5.2 `thread.exchange_failed.data.error`

The exchange ended at Fatal level. The error explains why.

```
data:
  exchange_started_id   string
  error                 Error    — canonical shape
```

Example:

```json
{
    "type": "thread.exchange_failed",
    "data": {
        "exchange_started_id": "ev_xyz",
        "error": {
            "type": "model.retry_exhausted",
            "message": "All 3 retry attempts failed",
            "where": "engine.execute.model_call",
            "detail": {
                "attempts": [
                    {"type": "model.upstream_error", "message": "503", ...},
                    {"type": "model.upstream_error", "message": "503", ...},
                    {"type": "model.malformed_response", "message": "Truncated", ...}
                ]
            },
            "cause": null
        }
    }
}
```

The attempts are nested in `detail`, not as a `cause` chain — they're
sibling failures, not a causal sequence.

### 5.3 `model.response.data` (when stop_reason is `"error"`)

A model call returned an error rather than a normal response. This is a
Recoverable-level event when followed by a retry; the same event type
appears whether the exchange continues or escalates.

```
data:
  call_id      string
  stop_reason  "error"
  error        Error              — when stop_reason is "error"
  usage        object?            — usage data if partial
  latency_ms   integer
```

### 5.4 Other locations

Error data may also appear in:
- API responses (when a sync `execute` raised, the HTTP response
  carries the Error envelope).
- CLI output (formatted from the envelope into human-readable text).
- Log entries (the structured form used directly).

The canonical envelope is the source of truth. UI surfaces format it
for display.

---

## Part 6 — Propagation Rules

How errors flow across module boundaries.

### 6.1 The discipline

When a module receives an exception from another module:

1. **If the error is meaningful in the receiving module's context** —
   wrap with `raise NewError(...) from original`. The new error
   describes the receiving module's failure; the cause chain preserves
   the underlying detail.

2. **If the receiving module is a transparent pass-through** — let the
   original propagate unchanged. Don't wrap to wrap.

3. **At the engine boundary** — any uncaught Python exception is
   wrapped in `platform.internal_error` (Fatal). This is the safety net;
   the original is preserved in `cause`.

### 6.2 Examples

**Wrap example** — the Tools subsystem catches a `SecretNotFound` while
setting up tools:

```python
try:
    await secrets.resolve(name)
except SecretNotFound as e:
    raise ToolSetupFailed(
        tool_name=tool.name,
        reason=f"required secret '{name}' is missing",
        where="tools.setup.resolve_secrets",
    ) from e
```

The serialized error has `type: tool.setup_failed`, with `cause.type:
secret.not_found`. The reader sees both layers.

**Pass-through example** — a tool's implementation raises a
`FileNotFoundError`. The Tools subsystem's dispatch wrapper catches it
and turns it into a `tool.execution_failed` event. The `FileNotFoundError`
is non-platform; it gets wrapped via `WrappedError` and placed in `cause`.

```python
try:
    result = await tool.execute(args, ctx)
except PlatformError as e:
    # Already platform shape; use directly
    emit_tool_failed(error=e)
except Exception as e:
    # Non-platform; wrap
    wrapped = WrappedError.from_exception(e, where="tools.dispatch.execute")
    error = ToolExecutionFailed(
        tool_name=tool.name,
        error_class=type(e).__name__,
    ) from wrapped  # via __cause__
    emit_tool_failed(error=error)
```

### 6.3 At the engine boundary

The Agent Engine's `execute` and `execute_stream` are the platform's
external boundary for execution. Any error that reaches them must be
either (a) emitted as an event at the appropriate level, or (b) raised
as `AgentExecutionError` (sync) carrying the terminal event.

Specifically:
- Diagnostic errors emitted during execution → continue, event is in
  the log.
- Recoverable errors → engine handles retry; either continues
  (success) or escalates to Fatal.
- Fatal errors → emit `thread.exchange_failed`, then raise (sync) or
  end stream (streaming).

Uncaught non-platform exceptions → wrapped as `platform.internal_error`
at Fatal level, then handled per above.

---

## Part 7 — Conformance Invariants

What any implementation must honor.

### E-I1 — Canonical shape

Every error appearing in events, API responses, or external surfaces
MUST use the canonical Error shape (Part 2.1). Implementations MUST NOT
emit ad-hoc error structures.

### E-I2 — Registered types only

Every error's `type` field MUST be a value registered in the catalog
(Part 4) or the generic platform codes (4.11). Ad-hoc strings are a
contract violation.

### E-I3 — Level assignment is intrinsic

An error type's level is part of its definition, not a runtime choice.
Implementations MUST NOT emit the same type at different levels in
different code paths.

### E-I4 — Cause chain preservation

When wrapping an error, the original MUST be preserved in `__cause__`
(Python) and `cause` (serialized). Lossy wrapping (catching and
re-raising without `from e`) is a contract violation.

### E-I5 — External exceptions wrapped

Non-PlatformError exceptions reaching the engine boundary MUST be
wrapped in `WrappedError` or a more specific subclass. Raw stdlib or
third-party exceptions MUST NOT appear in the serialized output.

### E-I6 — `where` populated

Every error MUST have a `where` value identifying its origin. Empty
strings are forbidden.

### E-I7 — `detail` matches the catalog

The `detail` keys for each error type MUST match the schema documented
in the catalog (Part 4). Implementations MUST NOT add or omit keys
arbitrarily — the schema is the contract.

### E-I8 — Diagnostic doesn't raise to the caller

A Diagnostic-level error MUST be emitted as a `tool.failed` (or
equivalent) event and MUST NOT raise out of `execute` or end the
stream. The exchange continues.

### E-I9 — Fatal terminates

A Fatal-level error MUST emit `thread.exchange_failed` and MUST end the
exchange. `execute` raises `AgentExecutionError`; `execute_stream`
ends.

### E-I10 — Recoverable retried before escalation

A Recoverable-level error MUST be subject to the retry policy in
`RuntimeSpec.max_retries`. Only after the retry budget is exhausted may
it escalate to Fatal.

---

## Part 8 — Open Items

1. **Error message localization.** v1 messages are English-only. A
   future `message_key` field for localized rendering is reserved but
   not specified. Out of v1 scope.

2. **Traceback inclusion.** v1's `platform.internal_error.detail` mentions
   `traceback?` (optional). Whether tracebacks are included by default
   (developer mode) or omitted (user mode) needs a policy. v1
   recommendation: include in dev/debug deployments; omit in production
   for noise reduction. Implementation-level configuration.

3. **Error rate limiting.** A misbehaving tool that fails 1000 times in
   a single exchange would produce 1000 `tool.failed` events. v1
   doesn't rate-limit error emission; the engine's `max_retries` and
   exchange-level `timeout_seconds` provide implicit bounds. If
   pathological patterns appear, dedicated rate limiting may be added.

4. **The `cause` depth limit.** A pathologically deep cause chain
   (cause of cause of cause...) would produce unbounded JSON. v1
   doesn't cap depth; expected chains are 1-3 levels. If a real case
   produces deeper chains, a cap (with truncation indicator) can be
   added.

5. **Adding a new error type in a bundle.** v1's catalog is platform-
   internal — bundles can't add new error types. A future "bundle-
   contributed error registry" mechanism would let bundles register
   their own types. Deferred.

6. **The relationship between `tool.failed` and `is_error: true` on
   `tool.result`.** Two ways the model sees a tool failure. The
   distinction: `tool.failed` means the platform never got a result
   (tool raised, was denied, timed out); `tool.result` with `is_error:
   true` means the tool ran and returned a structured error result (a
   web search that returned "no results found," for instance). Worth
   making this distinction explicit in the schema event-type spec
   when revised.

7. **Error-driven retry feedback.** Pi's pattern of feeding error
   messages back to the model as user messages for self-correction
   (the "Your previous response failed with X; please try again"
   pattern) is referenced in §1.2 but its mechanics aren't fully
   specified. Belongs in the engine's retry policy detail. Flagged for
   the engine contract revision.
