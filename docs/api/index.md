# API Reference

Generated from the source docstrings via `mkdocstrings`. Each
subsystem below renders its primary public modules — classes,
functions, attributes — with the Google-style docstrings retrofitted
across the codebase.

For the rules the docstrings follow, see the
[code documentation conventions](../process/documentation-conventions.md)
and the [vocabulary reference](../process/vocabulary.md).

Shared cross-module types (identifiers, entity records, events, the
resolved agent config) render in the
[Schemas reference](../reference/schemas.md), not here.

---

## HTTP API

::: api.app
    options:
      heading_level: 3

::: api.routes
    options:
      heading_level: 3

---

## Assets

::: assets.asset_manager
    options:
      heading_level: 3

::: assets.manifest
    options:
      heading_level: 3

::: assets.catalog
    options:
      heading_level: 3

::: assets.refs
    options:
      heading_level: 3

---

## Bus

::: bus.event_bus
    options:
      heading_level: 3

::: bus.in_process
    options:
      heading_level: 3

---

## CLI

::: cli.main
    options:
      heading_level: 3

::: cli.chat
    options:
      heading_level: 3

::: cli.permissions
    options:
      heading_level: 3

::: cli.workflows
    options:
      heading_level: 3

---

## Engine — Execution Context

::: engine.engine
    options:
      heading_level: 3

::: engine.context
    options:
      heading_level: 3

::: engine.framework
    options:
      heading_level: 3

::: engine.assembly
    options:
      heading_level: 3

::: engine.fake_framework
    options:
      heading_level: 3

::: engine.crewai_framework
    options:
      heading_level: 3

::: engine.dev_llm
    options:
      heading_level: 3

---

## Errors

::: errors.base
    options:
      heading_level: 3

::: errors.engine
    options:
      heading_level: 3

::: errors.file_handling
    options:
      heading_level: 3

::: errors.assets
    options:
      heading_level: 3

::: errors.tools
    options:
      heading_level: 3

::: errors.workspace
    options:
      heading_level: 3

::: errors.kernel
    options:
      heading_level: 3

---

## Files

::: files.policy
    options:
      heading_level: 3

---

## Kernel

::: kernel.resolver
    options:
      heading_level: 3

::: kernel.pipeline
    options:
      heading_level: 3

::: kernel.validation
    options:
      heading_level: 3

::: kernel.partials
    options:
      heading_level: 3

::: kernel.findings
    options:
      heading_level: 3

---

## LLM Connectors

::: llm.protocol
    options:
      heading_level: 3

::: llm.build_llm
    options:
      heading_level: 3

::: llm.registry
    options:
      heading_level: 3

---

## Platform Runtime

::: platform_runtime
    options:
      heading_level: 3

---

## Providers

::: providers.memory
    options:
      heading_level: 3

::: providers.secrets
    options:
      heading_level: 3

::: providers.sandbox
    options:
      heading_level: 3

::: providers.authz
    options:
      heading_level: 3

---

## Skills

::: skills.subsystem
    options:
      heading_level: 3

::: skills.models
    options:
      heading_level: 3

---

## Store

::: store.session_store
    options:
      heading_level: 3

::: store.backend
    options:
      heading_level: 3

::: store.sqlite_backend
    options:
      heading_level: 3

---

## Templating

::: templating.inheritance
    options:
      heading_level: 3

---

## Tools

::: tools.decorator
    options:
      heading_level: 3

::: tools.subsystem
    options:
      heading_level: 3

::: tools.builtins
    options:
      heading_level: 3

::: tools.discovery
    options:
      heading_level: 3

::: tools.dispatch
    options:
      heading_level: 3

---

## Workflows

::: workflows.ids
    options:
      heading_level: 3

::: workflows.context
    options:
      heading_level: 3

::: workflows.decorator
    options:
      heading_level: 3

::: workflows.executor
    options:
      heading_level: 3

::: workflows.parser
    options:
      heading_level: 3

::: workflows.validator
    options:
      heading_level: 3

::: workflows.run
    options:
      heading_level: 3

::: workflows.run_store
    options:
      heading_level: 3

::: workflows.builtins.bash
    options:
      heading_level: 3

::: workflows.builtins.agent
    options:
      heading_level: 3

::: workflows.errors
    options:
      heading_level: 3

::: workflows.events
    options:
      heading_level: 3

::: workflows.labels
    options:
      heading_level: 3

::: workflows.builtins.map
    options:
      heading_level: 3

::: workflows.builtins.workflow_sub
    options:
      heading_level: 3
