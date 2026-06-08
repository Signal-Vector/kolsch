# Documentation

Documentation for the Agent Platform, organized by the
[Diátaxis](https://diataxis.fr/) framework: each section serves a
distinct need.

New here? The [master index](reference/master-index.md) gives a
recommended reading order, a dependency map, and a topical index of
every concept.

---

## Explanation — understanding-oriented

Architecture and design rationale. The "why" behind the platform.

- [Specification](explanation/specification.md) — consolidated
  architectural skeleton.
- [v1 Scope](explanation/v1-scope.md) — what Phase 1 ships and excludes.
- [Architecture Roadmap](explanation/architecture-roadmap.md) — Phase
  1/2/3 architecture and migration seams.
- [Contract Audit](explanation/contract-audit.md) — inter-module
  consistency audit.

**Subsystem & module contracts:**

- [Kernel / Resolver](explanation/kernel-resolver-contract.md)
- [Session / Run Store](explanation/session-run-store-contract.md)
- [Event Bus](explanation/event-bus-contract.md)
- [Memory Provider](explanation/memory-provider-contract.md)
- [Secrets / Sandbox / Authorization](explanation/secrets-sandbox-authz-contract.md)
- [Tools Subsystem](explanation/tools-subsystem.md)
- [Skills Subsystem](explanation/skills-subsystem.md)
- [Asset Management](explanation/asset-management-contract.md)
- [File Handling](explanation/file-handling.md)
- [Agent Engine](explanation/agent-engine-contract.md)
- [Error Model](explanation/error-model.md)

## Reference — information-oriented

Precise, complete, lookup-oriented.

- [Master Index](reference/master-index.md) — reading order, dependency
  map, topical index, vocabulary.
- [Schemas](reference/schemas.md) — config schema, event log schema,
  entity records.
- [Module API Reference](reference/module-api-reference.md) — the
  gold-standard inter-module API surface.

## Process — how we build and verify

- [Development & Testing Handbook](process/development-testing-handbook.md)
  — engineering principles, Python practices, tooling, git, docs
  discipline, testing strategy, per-subsystem success criteria.
- [Test Catalog](process/test-catalog.md) — recommended test cases in
  English.
- [Test Plan](process/test-plan.md) — per-module test categories and the
  four overall validation tests.
- [Revisions](process/revisions/) — applied revision sets (audit,
  modular storage).

## How-to — task-oriented

Task guides for using and extending the platform. *Added during
implementation.*

- [Chatting Effectively with `kolsch chat`](how-to/chatting-effectively.md)
  — long conversations, mid-session model switches, approving
  bash patterns wisely, using `--session` to resume.
- [Extending an Agent](how-to/extending-an-agent.md) —
  declare a child agent that inherits from an existing one
  via `extends:` (worked butler example).

## Tutorials — learning-oriented

Step-by-step learning paths. *Added during implementation.*
