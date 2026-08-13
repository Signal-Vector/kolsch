# Guides

Walkthroughs for common tasks: setting up a workspace, configuring
agents, and working with the CLI.

## Start here

- [First agent](../tutorials/first-agent.md) — set up a workspace,
  write an agent YAML, and run a chat session end-to-end.
- [Connect a provider](connect-a-provider.md) — from an empty
  workspace to a real LLM. OpenRouter and OpenCode connectors.
- [Workspace and setup](workspace-and-setup.md) — where the workspace
  lives, switching between workspaces, scripting setup for CI.

## Extend the platform

- [Build a workflow](build-a-workflow.md) — wire the built-in node
  types into a DAG for a real task. Nodes, edges, binds, branching.
- [Write a tool](write-a-tool.md) — author a `@tool`-decorated
  Python function agents can call. Args schema, ToolContext, agent
  declaration.
- [Write a workflow node](write-a-workflow-node.md) — author a
  custom `@node`-decorated Python module that becomes a
  `<bundle>/nodes/<name>` step in workflow YAML.

## Configure agents

- [Extending an agent](extending-an-agent.md) — declare a child agent
  that inherits from a parent via `extends:`.
- [Agent templating](agent-templating.md) — use the six top-level
  variables (`tools`, `skills`, `docs`, `workspace`, `agent`,
  `platform`) in agent prompts.
- [Forcing a skill](forcing-a-skill.md) — pin a skill on a turn so
  the agent can't skip it.
- [Configuring fallbacks](configure-fallbacks.md) — declare backup
  models per agent so a transient 429 doesn't end the exchange.
- [Finding models](find-models.md) — list discovered models per
  connected provider so you know what to put in `model:`.

## Day-to-day

- [Chatting effectively](chatting-effectively.md) — long sessions,
  mid-session model switches, approving bash patterns, resuming with
  `--session`.
- [Permissions](permissions.md) — write tool permission rules, allow
  patterns at runtime with `/perm`, use the four-choice approval
  modal.
- [Event hooks](event-hooks.md) — run a shell command (a desktop
  notification, a webhook) when an approval is needed or a tool call
  fails, via `platform.yaml`'s `hooks:` section.
- [Context compaction](context-compaction.md) — configure the
  `compaction:` section in `platform.yaml`, use `/compact`,
  `/uncompact`, `/compaction-history`, and `/why-compact`, and
  author a custom variant.
