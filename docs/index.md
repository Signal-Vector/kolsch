# Documentation

Documentation for Kolsch, organized by the
[Diátaxis](https://diataxis.fr/) framework: each section serves a
distinct need.

New here? Start with the [First agent tutorial](tutorials/first-agent.md)
for a hands-on run, or the [Architecture index](architecture/index.md)
for the map of how the platform is built.

---

## Tutorials — learning-oriented

- [First agent](tutorials/first-agent.md) — scaffold a workspace,
  write an agent, run a chat session end-to-end.

## How-to — task-oriented

Guides for common tasks. See the full list on the
[Guides landing](how-to/README.md).

- [Connect a provider](how-to/connect-a-provider.md)
- [Build a workflow](how-to/build-a-workflow.md)
- [Write a tool](how-to/write-a-tool.md)
- [Write a workflow node](how-to/write-a-workflow-node.md)
- [Chatting effectively](how-to/chatting-effectively.md)
- [Permissions](how-to/permissions.md)

## Architecture — how it's built

Subsystem-by-subsystem tour of the platform. Start at the
[Architecture index](architecture/index.md) for the map, or jump to
a specific subsystem:

- [CLI](architecture/cli.md) · [API](architecture/api.md) ·
  [Workflows](architecture/workflows.md) ·
  [Agent Engine](architecture/engine.md)
- [Tools](architecture/tools.md) · [Skills](architecture/skills.md) ·
  [Assets](architecture/assets.md) · [Kernel](architecture/kernel.md)
- [Templating](architecture/templating.md) ·
  [Store](architecture/store.md) · [Bus](architecture/bus.md) ·
  [Providers](architecture/providers.md)
- [LLM Connectors](architecture/llm.md) ·
  [Files](architecture/files.md) ·
  [Schemas](architecture/schemas.md) ·
  [Errors](architecture/errors.md) ·
  [Platform Runtime](architecture/platform-runtime.md)

## Reference — information-oriented

Precise, complete, lookup-oriented.

- [Vocabulary](reference/vocabulary.md) — which term to pick when
  two synonyms compete.
- [Schemas](reference/schemas.md) — config, event log, and entity
  record shapes.
- [Workflow YAML](reference/workflow-yaml.md) — the shape of every
  built-in node type.

## API

- [Module reference](api/index.md) — auto-generated from source
  docstrings.

## Explanation — understanding-oriented

Design rationale and deeper contracts. Linked from the Architecture
pages where relevant; not surfaced in the top nav.
