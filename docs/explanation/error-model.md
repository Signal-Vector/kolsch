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
    "action": "kolsch/tools/bash",
    "principal": "agent.ag_abc123",
    "matched_rule": {"what": "kolsch/tools/bash", "pattern": "*", "effect": "deny"}
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
| `tool.execution_failed` | Diagnostic | `{tool_name, error_class, ...extra_detail}` | `tools.dispatch.execute` |
| `tool.timeout` | Diagnostic | `{tool_name, timeout_seconds}` | `tools.dispatch.execute` |
| `tool.jev_below_threshold` | Diagnostic | `{tool_name, failures, answers}` | `kolsch.tools.jev_decide.check_thresholds` |
| `mcp.server_unreachable` | Diagnostic | `{server_name, transport_error}` | `tools.mcp.spawn` |
| `mcp.upstream_error` | Diagnostic | `{server_name, tool, response}` | `tools.mcp.invoke` |
| `mcp.protocol_error` | Diagnostic | `{server_name, expected, actual}` | `tools.mcp.invoke` |
| `config.unknown_tool` | Fatal | `{ref, available_refs, agent_id}` | `engine.preflight.register_tools` |

**`config.unknown_tool`** (bug-engine-tools-wiring-gap.md) — raised
by engine pre-flight when an agent's
`subsystems.tools.config.declared[*].ref` does not resolve in the
asset-management catalog. The chain is: agent config →
`AssetManagement.get_asset(ref)` raises `assets.not_found` →
engine wraps as `config.unknown_tool` so the user sees a clear
"agent config names a tool you don't have" rather than the
generic asset error. Coordinator resolution (PR 1) enforces this
at engine pre-flight (option (b)); validator-side enforcement at
agent-load time is tracked as FUL-1.

**`tool.execution_failed` `extra_detail`** (spec-136) — a tool may
pass tool-specific structured context through the optional
`extra_detail` constructor argument; it is merged into `detail`
(`tool_name` / `error_class` always win). `jev_decide` uses it to
carry the upstream HTTP `status` and `upstream_message` (for 400 /
413 / 415 / 422, the validation `loc`/`msg` pairs) so the calling
model can correct its question.

**`tool.jev_below_threshold`** (spec-136) — raised by the
`jev_decide` tool when an author-set `fail_below` threshold is
breached. For each question carrying `fail_below`, the tool compares
one metric (`confidence` for `choice`, `score` for `score`, `noul`
for `noul`); any metric strictly below its threshold fails the call.
`failures` is a list of `{question, metric, value, threshold}`;
`answers` is the full upstream answer map, so the caller can still
see what Jev decided. Diagnostic: in chat the model sees it as a
failed tool result and the exchange continues. It is a `tool.*`
code and is **not** part of the model-fallback trigger table in
§4.15 — it never triggers fallback.

### 4.5b MCP tool bridge (spec-120)

The `ToolBridgeServer` + `kolsch mcp bridge` trust boundary. Two
rules govern these codes:

1. **They never reach the external wire.** The bridge answers every
   refusal with a constant generic literal; these types feed the
   server-side log, the CLI exit path, and the catalog contract.
   Their `detail` payloads are internal diagnostics only.
2. **Uniform auth failure.** Bad, expired, and already-bound tokens
   all raise the single `bridge.auth_failed` type (which reason is
   log-only `detail`), so distinct classes cannot become a token
   oracle.

Transport/auth refusals are Recoverable — only the external client's
session is refused; the exchange in the running kolsch process
continues. Security-invariant violations (`bridge.socket_insecure`)
and wire-name catalog corruption (`bridge.name_collision` — two tool
names mapping to one `mcp__<server>__<name>` would mis-route
`tools/call`) are Fatal.

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `bridge.auth_failed` | Recoverable | `{reason}` | `tools.bridge.server.bind` |
| `bridge.not_bound` | Recoverable | `{op}` | `tools.bridge.server.dispatch` |
| `bridge.socket_unavailable` | Recoverable | `{transport_error}` | `tools.bridge.proxy.connect` |
| `bridge.socket_insecure` | Fatal | `{check, path}` | `tools.bridge.server.open` |
| `bridge.frame_too_large` | Recoverable | `{declared, max_bytes}` | `tools.bridge.framing.read` |
| `bridge.frame_malformed` | Recoverable | `{}` | `tools.bridge.framing.read` |
| `bridge.malformed_request` | Recoverable | `{method}` | `tools.bridge.proxy.dispatch` |
| `bridge.request_refused` | Recoverable | `{op}` | `tools.bridge.proxy.request` |
| `bridge.name_collision` | Fatal | `{wire_name, first, second}` | `tools.bridge.server.catalog` |

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

### 4.15 Model Fallback (Phase 1.3, FB-R5)

Errors raised by the connector's call layer and the engine's
fallback loop. The six `model.*` codes are not strictly new — some
existed in Phase 1.1 as generic "the model call failed" without
level distinctions. Phase 1.3 forces each to declare Recoverable
vs Fatal, which is the basis of the fallback trigger decision.
The canonical "does this trigger fallback" table lives in the
Phase 1.3 patch §2.2; the levels below MUST match.

| Code | Level | Detail | Where (typical) |
|------|-------|--------|-----------------|
| `model.rate_limited` | Recoverable | `{provider, model, retry_after_seconds?}` | connector's call layer (HTTP 429) |
| `model.server_error` | Recoverable | `{provider, model, status_code}` | connector's call layer (HTTP 5xx) |
| `model.timeout` | Recoverable | `{provider, model, timeout_seconds}` | connector's call layer |
| `model.context_overflow` | Recoverable | `{provider, model, requested_tokens, max_tokens?}` | connector's call layer |
| `model.not_found` | Recoverable | `{provider, model, status_code, reason}` | connector's call layer (HTTP 404) |
| `model.authentication_failed` | Fatal | `{provider, model, status_code}` | connector's call layer (HTTP 401/403) |
| `model.bad_request` | Fatal | `{provider, model, status_code, reason}` | connector's call layer (HTTP 400) |
| `model.binary_unavailable` | Fatal | `{provider, model, reason}` | native connector's call layer (local CLI missing / logged out) |
| `llm.all_models_failed` | Fatal | `{primary, attempts: [{model, error_code, detail}], reason}` | engine model loop, after exhausting the chain |
| `config.fallback_duplicate` | Diagnostic | `{agent, model, positions}` | Layer 1 validator at agent load (warning surface) |

**Why these levels.** The five Recoverable codes are conditions
another model might handle differently (a rate limit on Opus does
not constrain Haiku; a transient 5xx from one provider is unlikely
to repeat at another; a different model may have a larger context
window; a `model.not_found` is usually catalog drift — the provider
retired or renamed that model — so another chain entry can still
answer, and `kolsch refresh <provider>` re-reads the catalog). The Fatal `model.*` codes are conditions the chain
won't help with: an auth failure with one provider's key won't
succeed against another model on the same key, a malformed
request is broken regardless of which model receives it, and a
`model.binary_unavailable` (a native connector's local CLI is
missing or logged out — e.g. `claude_code`) would fail identically
for any sibling model routed through the same binary. The
engine MUST NOT consult the fallback chain on Fatal `model.*`.

**`llm.all_models_failed`'s `attempts` field** is the full ordered
list of `(model, error_code, detail)` triples the engine tried.
The cause chain on the emitted `thread.exchange_failed` event
references this for replay.

**`config.fallback_duplicate`** is a warning, not an error — the
user may have intentionally listed the same model twice to retry
it (Phase 1.3 patch §2.4). It surfaces at `kolsch run` startup so
the user sees it, not silently in logs. The level is `Diagnostic`
because no Warning level exists in the catalog today; if a Warning
tier lands later, this code moves there. *(Flagged for spec
review — Phase 1.3 introduced a "warning" concept that the Error
Model doesn't yet have a first-class level for.)*

### 4.16 Tools & Permissions (Phase 2.0, TOOL-R5)

Errors from the Phase 2.0 builtin tools (`bundles/kolsch/tools/`),
the `prompt`-effect dispatch in the Tools subsystem call layer, and
the `kolsch permissions {list, add, remove, show}` CLI. The patch
source of truth is `process/phase2.0-tools-specs.md` Part 5;
the canonical "what each effect does" mapping for callers is
`docs/explanation/secrets-sandbox-authz-contract.md` §3.7.

| Code | Level | Detail | Where (typical) |
|---|---|---|---|
| `authz.denied` | Recoverable | `{tool, target, rule_id}` | Tools dispatcher when effect is `deny` (registered; the dispatcher actually wraps every authz denial as `tool.unauthorized` at the tool boundary — see "Wrapped at the tool boundary" below) |
| `authz.prompt_unattended` | Recoverable | `{tool, target, rule_id, caller}` | Non-chat callers when effect is `prompt` (collapsed to deny) |
| `authz.rule_invalid` | Fatal | `{position, reason}` | `kolsch permissions add` validating user input |
| `authz.rule_not_found` | Fatal | `{id, available_ids}` | `kolsch permissions remove` for unknown id |
| `authz.protected_rule` | Fatal | `{id, reason}` | `kolsch permissions remove rl_default_deny` (the default-deny tail is removal-protected) |
| `authz.bare_tool_ref` | Fatal | `{rule_index, bare_name, candidates}` | Provider construction when a rule's `what` is a bare tool name (e.g., `bash` instead of `kolsch/tools/bash`) — qol-permission-system-human-readable.md + qol-action-namespace-bundle-scoped.md |
| `authz.pattern_invalid` | Fatal | `{rule_index, pattern, forbidden_chars}` | Provider construction when a rule's `pattern` contains regex characters (`^`, `$`, `[`, …) — qol-permission-system-human-readable.md |
| `authz.tool_ref_unknown` | Fatal | `{rule_index, tool_ref}` | Provider construction when a qualified `what` doesn't resolve to a catalogued tool |
| `config.deprecated_regex_rule` | Recoverable | `{rule_index, what, reason}` | Setup-runner migration when a legacy regex rule can't be auto-converted; the rule stays in legacy form |
| `file.access_denied` | Recoverable | `{path, allowed_paths}` | `read_file` / `write_file` / `list_files` when the path resolves outside `file_access.allowed_paths` |
| `file.too_large` | Recoverable | `{path, size_bytes, max_bytes}` | `read_file` when the target exceeds the per-call `max_bytes` budget |
| `file.not_found` | Recoverable | `{path}` | `read_file` / `list_files` when the path does not exist |
| `tool.timeout` | Recoverable | `{tool, timeout_seconds}` | `bash` when the command exceeds `timeout_seconds` |

**Why these levels.** The six Recoverable codes (`authz.denied`,
`authz.prompt_unattended`, and the four file/tool codes) surface
to the model as tool-result payloads — the model can react (try a
different path, retry with smaller bytes budget, give up). The
three Fatal codes (`authz.rule_invalid`, `authz.rule_not_found`,
`authz.protected_rule`) are CLI-level configuration problems where
the user typed something wrong — the command fails loudly rather
than the model trying to recover from a structural issue.

**`authz.denied` vs `authz.prompt_unattended`.** Both surface to
the model as Recoverable. `authz.denied` means a rule explicitly
denied the call. `authz.prompt_unattended` means a rule asked to
prompt the user but the caller is not interactive — replay sees
the distinction so a future interactive run can answer the same
question.

**Wrapped at the tool boundary.** The tools dispatcher at
`src/tools/dispatch.py` raises `ToolUnauthorized` (code
`tool.unauthorized`) when the authz provider returns a denial —
not `AuthzDenied` directly. The wrap is intentional: from the
model's POV the failure is at the tool boundary
(`tool.<name>` failed to invoke), not at the provider boundary.
`AuthzDenied` is registered for completeness but never appears
in `tool.failed` events; the renderer + the
`recovery_hint` machinery key off `tool.unauthorized` because
that's what crosses the wire. The QoL Step 4 doc cascade
reconciles wording across both names; future cleanup may
delete the unused `AuthzDenied` class altogether.

**The QoL permission-system codes (Step 4).** The four codes
above the line —
`authz.bare_tool_ref` / `authz.pattern_invalid` /
`authz.tool_ref_unknown` / `config.deprecated_regex_rule` —
ship with `qol-permission-system-human-readable.md`. The first
three are workspace-load Fatals: the user wrote a rule the
schema rejects, the platform refuses to start, the error names
the rule index + the specific input that failed validation.
`config.deprecated_regex_rule` is Recoverable: it emits at
`kolsch setup` re-run when a legacy regex rule can't be
auto-converted; the rule stays in legacy form and `kolsch
permissions list` marks it with `[legacy regex — please convert]`.

**Path overlap with §4.11.** Phase 1's `file_access.denied` (in
§4.11) and Phase 2.0's `file.access_denied` (here) are the same
condition; the dotted code matches the Phase 2.0 builtin
nomenclature (`read_file` / `write_file` / `list_files`). Code
emitting from the new builtins uses the §4.16 code; legacy
emission from the Phase 1 builtins continues under §4.11 until
Phase 2.3's drift detection ports them. Both serialize to the
canonical envelope identically — readers parsing the event log
treat them as synonyms.

### 4.17 Chat (Phase 2.1, CHAT-R5)

Errors emitted by the `kolsch chat` REPL — startup-time failures
that exit cleanly with a friendly one-line message (same
discipline as Phase 1.2's `workspace.not_initialized`) plus
mid-session conditions the chat surfaces without ending the
session. Patch source of truth: `process/phase2.1-chat-
specs.md` Part 7.

| Code | Level | Detail | Where (typical) |
|---|---|---|---|
| `chat.agent_not_found` | Fatal | `{agent_ref}` | `kolsch chat <agent_ref>` startup, before runtime bootstrap |
| `chat.session_not_found` | Fatal | `{session_id}` | `kolsch chat --session <id>` startup |
| `chat.model_override_invalid` | Fatal | `{model_ref, reason}` | `kolsch chat --model <ref>` startup |
| `chat.input_aborted` | Recoverable | `{}` | Ctrl-C during streaming — surfaces as the engine's `engine.cancelled` cause in the corresponding `thread.exchange_failed`; chat continues |
| `chat.callback_resolution_failed` | Fatal | `{tool, reason}` | Modal callback couldn't construct a valid rule (e.g., user typed an invalid regex in the `[a]`/`[d]` pattern edit) |

**Why these levels.** The three startup-time Fatals print one
short message and exit before the runtime is even bootstrapped
— there's nothing recoverable to do beyond fixing the
invocation. `chat.input_aborted` is Recoverable because the
chat is a long-running REPL: a cancelled exchange ends THAT
exchange (with the partial agent block styled
``[cancelled]``), not the chat session. The fifth Fatal —
`chat.callback_resolution_failed` — is raised when the user's
pattern edit produces a malformed `Rule`; the immediate tool
call falls back to `deny-once` semantics and the user is told
why their pattern was rejected.

### 4.18 Agent Envelope (Phase 2.2, ENV-R6)

Errors emitted by the Phase 2.2 envelope machinery — context-file
resolution, Jinja rendering, `extends:` chain walking, and the
per-agent permissions API for tool authors.

| Code | Level | Detail | Where (typical) |
|---|---|---|---|
| `config.context_file_unprefixed` | Fatal | `{path}` | agent-load — a `context_files[].path` or `extends:` value lacks the `kolsch:` / `workspace:` prefix |
| `context.file_not_found` | Fatal | `{path, resolved}` | agent-load — `kolsch:` / `workspace:` resolved to an absolute path that does not exist |
| `context.file_too_large` | Fatal | `{path, size_bytes, max_bytes}` | agent-load — context file exceeds the 64 KiB cap |
| `config.template_render_failed` | Fatal | `{template, reason}` | engine pre-flight (5.6.6) — Jinja render raised `UndefinedError` / `TemplateSyntaxError` / etc. |
| `config.required_block_missing` | Fatal | `{block_name, agent_path}` | engine pre-flight (5.6.6) — child uses `{% extends parent %}` and does not override a `{% block X required %}` |
| `config.agent_inheritance_cycle` | Fatal | `{cycle: [path1, path2, ...]}` | engine pre-flight (5.6.5) — `extends:` chain contains a cycle |
| `config.parent_agent_not_found` | Fatal | `{path, resolved}` | engine pre-flight (5.6.5) — `extends:` points at a missing file |
| `config.extends_template_without_inheritance` | Fatal | `{agent_path}` | engine pre-flight (5.6.6) — child prompt uses `{% extends parent %}` but its YAML has no `extends:` field |
| `agent.template_warning` | Recoverable (Warning) | `{reference, reason, hint}` | engine pre-flight (5.6.6) — template-config consistency walker (Phase 2.2 §9) flags an empty-data variable access |
| `authz.denied` | Recoverable | `{tool, target, rule_id}` | engine pre-dispatch OR tool's `permissions.require` call when the composed rules resolve to `DENY` / `PROMPT`-without-callback |
| `tool.permissions_no_context` | Fatal | `{tool, caller}` | tool's `permissions.check` / `.require` called outside the engine's dispatch ContextVar (e.g. a unit test that imported the tool directly) |

**Why these levels.** The seven `config.*` codes plus the two
`context.*` codes are Fatals because the agent cannot load.
`agent.template_warning` is the deliberate exception: empty
rendering is sometimes intentional (a parent template
references `docs`, and child agents may legitimately declare
none), so a hard error would prevent legitimate scaffolding.
`authz.denied` keeps its Phase 2.0 Recoverable level — a denied
tool call ends the call, not the exchange.
`tool.permissions_no_context` is Fatal because it indicates a
developer mistake (a tool calling the API outside the engine's
dispatch path); the fix is in the test harness, not the
runtime.

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
