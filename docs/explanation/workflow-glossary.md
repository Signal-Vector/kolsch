# Workflow Glossary

This file is the canonical glossary for kolsch workflows. It defines the precise terms used throughout the workflow contract and is cross-referenced from the phase 3.0 master spec (`process/phase3.0-workflow-spec.md`). The companion Reference Map at the bottom enumerates the documents this contract inherits from and the docs that expand on it.

---

## 0. Glossary

These terms have precise meanings in this spec. Defined here; used (and disambiguated) throughout the body.

| Term | Meaning |
|---|---|
| **node** | An entry in a workflow's `nodes:` mapping. The default reading of "node" everywhere in this spec unless qualified. A node is a single invocation site addressable by its mapping key. |
| **node type** | The catalog citizen, addressable via a 3-segment ref (e.g., `kolsch/nodes/bash`). Node types are the reusable definitions; nodes are the per-workflow invocation sites. |
| **captured output** | The value addressable via `nodes.<name>` in Jinja expressions — the structured result a node produced on the most-recent fire. |
| **invocation** | The runtime fire of a node. Owns an `InvocationId`. A single node may produce multiple invocations over a run's lifetime (re-fires, map workers, retries). |
| **node NAME** | The `nodes:` mapping key. The sole identifier for addressing a node from Jinja, edges, `copy_of:`, `map.apply:`, etc. `description:` is a USER AID for display only — NEVER an identifier. |

---

## 21. Reference Map

Companion documents:
- `phase3.0-prior-art-memo.md` — research synthesis informing this spec.
- `docs/explanation/workflows-contract.md` (to be authored in Phase 3.0) — the formal user-facing contract.
- `docs/how-to/authoring-a-workflow.md` (to be authored in Phase 3.1+) — author guide; expanded as new capabilities land.
- `docs/reference/schemas.md` — workflow YAML schema entry added during Phase 3.0.

Existing kolsch contracts this spec inherits from:
- `docs/explanation/agent-engine-contract.md` — agent invocation discipline.
- `docs/explanation/asset-management-contract.md` — asset catalog mechanics.
- `docs/explanation/secrets-sandbox-authz-contract.md` — authz layer walk, secrets subsystem.
- `docs/explanation/tools-subsystem.md` — `@tool` decorator pattern this `@node` mirrors.
- `docs/explanation/error-model.md` — exception-based error discipline.
