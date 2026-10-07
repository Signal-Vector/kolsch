# Roadmap

Planned features and architectural direction for Kolsch.

## Now

- **Additional LLM connectors** — Anthropic direct, AWS Bedrock, and local model support (Ollama, vLLM). The connector protocol is stable; each new provider is a new implementation.
- **More workflow node types** — expanding the built-in node catalog beyond the current 15 types.
- **YAML schema stabilization** — locking down agent and workflow YAML shapes so files don't break between releases.
- **Bundle tooling** — `kolsch bundle init`, `kolsch bundle validate`, and better drift detection for the starter bundle.

## Next

- **Semantic memory** — vector-store-backed memory with similarity retrieval, as a new provider variant.
- **Docker sandbox** — tool execution in isolated containers; real filesystem and network isolation.
- **Vault and OS secrets** — pull from Vault, AWS Secrets Manager, 1Password, or OS environment.
- **Policy-based authorization** — declarative policies beyond regex rules (OPA or Cedar-based).
- **Multi-thread sessions** — parallel workflow nodes under one session.
- **Cross-session memory** — workflow-scoped memory so agents can share context within a run.

## Later

- **Web IDE** — browser-based UI for building agents, designing workflows, and watching sessions live.
- **Bundle registry** — curated distribution with discovery, versioning, and one-command install.
- **Server/runner separation** — long-running server + short-lived runner processes for horizontal scaling.
- **Multi-tenancy** — per-user principals, data isolation, and authorization per user or group.
- **Observability** — Prometheus metrics and OpenTelemetry tracing derived from the event stream.
