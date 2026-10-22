# Schemas reference

The generated field reference for every cross-module Pydantic type
Kolsch defines. For the narrative — what a schema is *for*, how the
pieces relate — see the [Schemas architecture page](../architecture/schemas.md).
For the event-log contract that ties `Event`, `EventType`, and the
`*Data` payloads together, see the
[Event log contract](../explanation/event-log-contract.md).

## Identifiers

Prefixed-id types. Each is a `str` subclass with a structural prefix
Pydantic validates on assignment.

::: schemas.identifiers
    options:
      heading_level: 3
      show_root_heading: false
      show_source: false
      members_order: source

## Entity records

`Session`, `Thread`, `Actor` — the mutable-state records the store
maintains alongside the immutable event log.

::: schemas.entities
    options:
      heading_level: 3
      show_root_heading: false
      show_source: false
      members_order: source

## Events

The `Event` envelope, the `EventType` enum, and the per-type
`*Data` payload models.

::: schemas.events
    options:
      heading_level: 3
      show_root_heading: false
      show_source: false
      members_order: source

## Config

The resolved agent config the kernel produces and the Agent Engine
consumes. `ResolvedAgentConfig`, `ModelSpec`, `IOSpec`, `PromptSpec`,
`SubsystemConfig`, and the runtime knobs.

::: schemas.config
    options:
      heading_level: 3
      show_root_heading: false
      show_source: false
      members_order: source

## Caller chain

The proximate-first agent/workflow frames an authorization decision is
judged against — carried on `ToolContext.caller_chain` and matched by
`from:`-scoped rules. See
[Scoping rules](../how-to/permissions.md#scoping-rules-to-a-specific-agent-or-workflow).

::: schemas.caller_chain
    options:
      heading_level: 3
      show_root_heading: false
      show_source: false
      members_order: source

## Provider config (`llm_providers.<name>`)

`platform.yaml`'s `llm_providers` entries load into
`llm.ProviderConfig` through the single factory
`llm.provider_config_from_yaml`, which also validates the optional
`request_policy.session_headers` override (bug-144). A malformed
`request_policy` on any provider fails runtime boot and
`kolsch connect` / `refresh` for all providers with `config.invalid`.
Field-by-field: the
[platform.yaml layout](../explanation/asset-management-contract.md) and
[LLM providers §6.2](../explanation/llm-providers.md#62-request_policy-per-provider-session-headers);
the per-call `LLMCallContext` is described in
[LLM providers §3.6](../explanation/llm-providers.md#36-per-call-request-context-request-policy-and-user-agent-bug-144).
Class reference: [API: LLM Connectors](../api/index.md#llm-connectors).

