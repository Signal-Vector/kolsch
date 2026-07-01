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
