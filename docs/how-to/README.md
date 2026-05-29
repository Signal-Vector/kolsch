# How-to Guides

Task-oriented guides for using and extending the platform.

- [Workspace and setup](workspace-and-setup.md) — where the workspace
  lives, switching between workspaces, backing up, resetting, and
  scripted setup for CI.
- [Find available models](find-models.md) — list discovered models
  per connected provider so you know what to put in agent YAML's
  `model:` field. Covers filtering, `--verbose`, `--json`, and
  aliasing.
- [Configure model fallback chains](configure-fallbacks.md) —
  declaring per-agent or platform-default backup models so a
  transient 429 doesn't kill an exchange.

Planned:

- How to write a custom tool.
- How to write a new memory variant.
- How to switch to the Postgres entity backend.
- How to enable the Redis event bus backend.
- Editor setup for the dev container.
- Working with the Codanna code index.
