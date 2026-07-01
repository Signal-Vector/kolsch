# Guides

Walkthroughs for common tasks: setting up a workspace, configuring
agents, and working with the CLI.

## Start here

- [First agent](../tutorials/first-agent.md) — set up a workspace,
  write an agent YAML, and run a chat session end-to-end.
- [Workspace and setup](workspace-and-setup.md) — where the workspace
  lives, switching between workspaces, scripting setup for CI.

## Extend the platform

- [Write a workflow node](write-a-workflow-node.md) — author a
  custom `@node`-decorated Python module that becomes a
  `<bundle>/nodes/<name>` step in workflow YAML.

## Configure agents

- [Extending an agent](extending-an-agent.md) — declare a child agent
  that inherits from a parent via `extends:`.
- [Agent templating](agent-templating.md) — use the five top-level
  variables (`tools`, `skills`, `docs`, `workspace`, `agent`) in
  agent prompts.
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
