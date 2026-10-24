# API Reference

Generated from the source docstrings via `mkdocstrings`. Each
subsystem below renders its primary public modules — classes,
functions, attributes — with the Google-style docstrings retrofitted
across the codebase.

For the rules the docstrings follow, see the
[code documentation conventions](../process/documentation-conventions.md)
and the [vocabulary reference](../process/vocabulary.md).

Shared cross-module types (identifiers, entity records, events, the
resolved agent config) belong in the
[Schemas reference](../reference/schemas.md), not here.

---

## HTTP API

::: api.app
    options:
      heading_level: 3

::: api.routes
    options:
      heading_level: 3

::: api.views
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

::: assets.assembler
    options:
      heading_level: 3

::: assets.default_rules
    options:
      heading_level: 3

::: assets.platform_config
    options:
      heading_level: 3

::: assets.skill_metadata
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

::: bus.patterns
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

::: cli.llm_commands
    options:
      heading_level: 3

::: cli.nodes
    options:
      heading_level: 3

::: cli.session_picker
    options:
      heading_level: 3

::: cli.skills
    options:
      heading_level: 3

---

### CLI — Chat

::: cli.chat.app
    options:
      heading_level: 4

::: cli.chat.session
    options:
      heading_level: 4

::: cli.chat.approval
    options:
      heading_level: 4

::: cli.chat.cancel
    options:
      heading_level: 4

::: cli.chat.event_adapter
    options:
      heading_level: 4

::: cli.chat.service_registry
    options:
      heading_level: 4

::: cli.chat.keybindings
    options:
      heading_level: 4

::: cli.chat.metrics
    options:
      heading_level: 4

::: cli.chat.spinner
    options:
      heading_level: 4

::: cli.chat.state_tokens
    options:
      heading_level: 4

---

### CLI — Chat Services

::: cli.chat.services.authz_introspection
    options:
      heading_level: 4

::: cli.chat.services.cards_impl
    options:
      heading_level: 4

::: cli.chat.services.compaction_introspection
    options:
      heading_level: 4

::: cli.chat.services.mount_sink
    options:
      heading_level: 4

::: cli.chat.services.prompts_impl
    options:
      heading_level: 4

::: cli.chat.services.workflow_layout
    options:
      heading_level: 4

::: cli.chat.services.workflows_impl
    options:
      heading_level: 4

---

### CLI — Chat Widgets

::: cli.chat.widgets.approval_args_row
    options:
      heading_level: 4

::: cli.chat.widgets.approval_block
    options:
      heading_level: 4

::: cli.chat.widgets.approval_choice_pane
    options:
      heading_level: 4

::: cli.chat.widgets.approval_from_edit
    options:
      heading_level: 4

::: cli.chat.widgets.approval_pattern_edit
    options:
      heading_level: 4

::: cli.chat.widgets.approval_save_confirm
    options:
      heading_level: 4

::: cli.chat.widgets.assistant_prose
    options:
      heading_level: 4

::: cli.chat.widgets.card_body
    options:
      heading_level: 4

::: cli.chat.widgets.card_body.confirm
    options:
      heading_level: 4

::: cli.chat.widgets.card_body.markdown
    options:
      heading_level: 4

::: cli.chat.widgets.card_body.progress
    options:
      heading_level: 4

::: cli.chat.widgets.card_body.workflow
    options:
      heading_level: 4

::: cli.chat.widgets.card_prompt_pane
    options:
      heading_level: 4

::: cli.chat.widgets.card_widget
    options:
      heading_level: 4

::: cli.chat.widgets.compaction_block
    options:
      heading_level: 4

::: cli.chat.widgets.composer
    options:
      heading_level: 4

::: cli.chat.widgets.force_kill_confirm
    options:
      heading_level: 4

::: cli.chat.widgets.info_bar
    options:
      heading_level: 4

::: cli.chat.widgets.model_picker
    options:
      heading_level: 4

::: cli.chat.widgets.node_section
    options:
      heading_level: 4

::: cli.chat.widgets.notice
    options:
      heading_level: 4

::: cli.chat.widgets.notice_slot
    options:
      heading_level: 4

::: cli.chat.widgets.scrollback_cue
    options:
      heading_level: 4

::: cli.chat.widgets.slash_popup
    options:
      heading_level: 4

::: cli.chat.widgets.status_bar
    options:
      heading_level: 4

::: cli.chat.widgets.tool_call_block
    options:
      heading_level: 4

::: cli.chat.widgets.user_chatbox
    options:
      heading_level: 4

::: cli.chat.widgets.workflow_node_row
    options:
      heading_level: 4

::: cli.chat.widgets.working_placeholder
    options:
      heading_level: 4

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

::: engine.assembly_cache
    options:
      heading_level: 3

::: engine.event_observer
    options:
      heading_level: 3

::: engine.mock_framework
    options:
      heading_level: 3

::: engine.crewai_framework
    options:
      heading_level: 3

::: engine.subprocess_framework
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

::: errors.bridge
    options:
      heading_level: 3

::: errors.workspace
    options:
      heading_level: 3

::: errors.kernel
    options:
      heading_level: 3

::: errors.authz
    options:
      heading_level: 3

::: errors.bus
    options:
      heading_level: 3

::: errors.catalog
    options:
      heading_level: 3

::: errors.fallback
    options:
      heading_level: 3

::: errors.level
    options:
      heading_level: 3

::: errors.llm
    options:
      heading_level: 3

::: errors.memory
    options:
      heading_level: 3

::: errors.recovery
    options:
      heading_level: 3

::: errors.sandbox
    options:
      heading_level: 3

::: errors.secrets
    options:
      heading_level: 3

::: errors.skills
    options:
      heading_level: 3

::: errors.store
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

::: kernel.export
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

::: llm.providers.openrouter
    options:
      heading_level: 3

::: llm.providers.opencode
    options:
      heading_level: 3

::: llm.providers.claude_code
    options:
      heading_level: 3

::: llm.claude_cli
    options:
      heading_level: 3

---

## MCP Server

The stateless, read-only stdio MCP server exposed to external MCP
clients (e.g. Claude Code) for introspecting kolsch sessions. See
also [`tools.bridge.server`](#tools) under Tools for the separate
in-process tool-calling bridge used during agent execution — a
different MCP surface with a different purpose.

::: mcp_server.server
    options:
      heading_level: 3

---

## Platform Runtime

::: platform_runtime
    options:
      heading_level: 3

::: platform_runtime.runtime
    options:
      heading_level: 3

::: platform_runtime.workspace
    options:
      heading_level: 3

::: platform_runtime.hooks
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

::: providers.compaction
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

::: skills.validator
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

::: store.content
    options:
      heading_level: 3

::: store.jsonl
    options:
      heading_level: 3

---

## Templating

::: templating.inheritance
    options:
      heading_level: 3

::: templating.render
    options:
      heading_level: 3

::: templating.context_files
    options:
      heading_level: 3

::: templating.vars.agent
    options:
      heading_level: 3

::: templating.vars.docs
    options:
      heading_level: 3

::: templating.vars.skills
    options:
      heading_level: 3

::: templating.vars.tools
    options:
      heading_level: 3

::: templating.vars.workspace
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

::: tools.observed
    options:
      heading_level: 3

::: tools.bridge.sink
    options:
      heading_level: 3

::: tools.bridge.server
    options:
      heading_level: 3

::: tools.bridge.proxy
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

::: tools.registry
    options:
      heading_level: 3

::: tools.permissions
    options:
      heading_level: 3

::: tools.validator
    options:
      heading_level: 3

::: tools.mcp
    options:
      heading_level: 3

::: tools.context_reference
    options:
      heading_level: 3

::: tools.services
    options:
      heading_level: 3
      members: ["ServiceName", "RequiredServices", "require_services"]

::: tools.services.card_kinds
    options:
      heading_level: 3

::: tools.services.cards
    options:
      heading_level: 3

::: tools.services.exceptions
    options:
      heading_level: 3

::: tools.services.prompts
    options:
      heading_level: 3

::: tools.services.session_view
    options:
      heading_level: 3

::: tools.services.workflows
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

::: workflows.branching
    options:
      heading_level: 3

::: workflows.catalog
    options:
      heading_level: 3

::: workflows.catalog_node
    options:
      heading_level: 3

::: workflows.inputs_scan
    options:
      heading_level: 3

::: workflows.jinja_env
    options:
      heading_level: 3

::: workflows.module_isolation
    options:
      heading_level: 3

::: workflows.orphan_sweep
    options:
      heading_level: 3

::: workflows.run_context
    options:
      heading_level: 3

::: workflows.tool_schema
    options:
      heading_level: 3

::: workflows.builtins.branch
    options:
      heading_level: 3

::: workflows.builtins.browse_url
    options:
      heading_level: 3

::: workflows.builtins.catalog_node_runner
    options:
      heading_level: 3

::: workflows.builtins.compute
    options:
      heading_level: 3

::: workflows.builtins.delete_state
    options:
      heading_level: 3

::: workflows.builtins.filter
    options:
      heading_level: 3

::: workflows.builtins.list_files
    options:
      heading_level: 3

::: workflows.builtins.noop
    options:
      heading_level: 3

::: workflows.builtins.read_file
    options:
      heading_level: 3

::: workflows.builtins.retry
    options:
      heading_level: 3

::: workflows.builtins.set_state
    options:
      heading_level: 3

::: workflows.builtins.timeout
    options:
      heading_level: 3

::: workflows.builtins.user_input
    options:
      heading_level: 3

::: workflows.builtins.write_file
    options:
      heading_level: 3
