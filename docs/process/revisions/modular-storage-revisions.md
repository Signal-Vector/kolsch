# Spec Revisions — Modular Storage Backends

**Draft 0.1**

> Flags the revisions needed across existing specs to reflect the
> "mature with scale" storage decision: SQLite + in-process bus by
> default, Postgres + Redis as opt-in scale-up backends.
>
> Like the audit revisions, these are flagged here and applied in a
> dedicated pass. Each entry names the target spec, section, and the
> change.

---

## Background

The original specs assumed Postgres + Redis as required infrastructure.
For a single-user local install that is overkill. The corrected stance:

- **Tier 1 (default):** SQLite entity records + in-process event bus.
  Zero external services.
- **Tier 2:** SQLite + Redis (real-time fan-out across processes).
- **Tier 3:** Postgres + Redis (scale demands).

Both datastores already sit behind contracts, so this is a backend
selection, not an architectural change. The revisions make the specs
reflect the tiered, modular reality.

---

## DB-R1. Session/Run Store Contract — Backend Abstraction

**Target:** `phase1-session-run-store-contract-draft-0.1.md`, Part 3
and the framing throughout.

**Change:**
- Reframe "Postgres as cache + CRM" to "entity backend, pluggable."
  The default backend is **SQLite** at `.platform/entities.db`; Postgres
  is the scale-up backend.
- The contract's API (`create_session`, `append_event`, etc.) is
  unchanged — it never referenced Postgres directly.
- Add a short subsection "Entity Backends": the store has a backend
  abstraction; SQLite and Postgres are implementations; selection is via
  `platform.yaml`'s `storage.entity_backend`.
- Note that the filesystem JSONL ground-truth is unchanged across all
  backends — only the *entity record* store varies.
- Invariant addition: the same component test suite MUST pass against
  both SQLite and Postgres backends.

---

## DB-R2. Event Bus Contract — In-Process Default

**Target:** `phase1-event-bus-contract-draft-0.1.md`, Parts 1 and 5.

**Change:**
- The v1 **default** event bus is an **in-process asyncio pub/sub**, not
  Redis. Redis becomes the opt-in backend for cross-process delivery.
- This resolves the prior awkwardness (audit R17) where the bus required
  Redis but had no internal subscribers in v1 — now v1's bus is a
  lightweight in-process component that costs nothing and requires no
  external service.
- The contract's API (`publish`, `subscribe`, `unsubscribe`) is
  unchanged — it never referenced Redis directly.
- Add a subsection "Bus Backends": `in_process` (default) and `redis`
  (scale-up); selection via `platform.yaml`'s `event_bus.backend`.
- Update §5.1 (ephemeral-only, strict-subscriber): the in-process bus is
  inherently single-process; the strict-subscriber pattern (tail the log
  with bookmarks) becomes relevant only at Tier 2+ when Redis enables
  cross-process subscribers.

---

## DB-R3. v1 Scope — Stack Description

**Target:** `phase1-v1-scope-draft-0.1.md`, §2 (stack/deployment).

**Change:**
- Replace any "requires Postgres + Redis" framing with: "Default install
  requires **no external services** — SQLite + in-process bus. Postgres
  and Redis are opt-in scale-up backends."
- Note the three storage tiers briefly, referencing the handbook §6 for
  detail.
- This materially improves the v1 first-run experience: `uv run
  pi-platform` works with nothing else installed.

---

## DB-R4. Asset Management — platform.yaml Schema

**Target:** `phase1-asset-management-contract-draft-0.1.md`, §3.2.

**Change:**
- Add a `storage` section to the platform.yaml schema:
  ```yaml
  storage:
    entity_backend: sqlite          # sqlite | postgres
    entity_path: ".platform/entities.db"
    # entity_dsn: "postgresql://..."  # when postgres
  ```
- Update the `event_bus` section:
  ```yaml
  event_bus:
    backend: in_process             # in_process | redis
    # redis_url: "redis://localhost:6379"  # when redis
  ```
- Note that defaults require no external services.

---

## DB-R5. Phase Architecture Roadmap — Deployment Shapes

**Target:** `phase-architecture-roadmap.md`, Parts 1.5, 3.6, 5.

**Change:**
- Phase 1 deployment shape (§1.5): default is single process + SQLite +
  in-process bus, **no Redis, no Postgres**. Update the "dependencies the
  user runs alongside" list — those become optional scale-up
  dependencies, not requirements.
- Phase 2 (§3.6): Redis likely adopted here (the IDE needs cross-process
  event delivery — a real Tier 2 trigger). Postgres still optional.
- Phase 3 (§5): Postgres + Redis as the hosted-scale default. The tiers
  map naturally onto the phases.
- Add the storage-tier model to the cross-phase principles (§7): "mature
  with scale" is now an explicit architectural principle.

---

## DB-R6. Module API Reference — Backend Notes

**Target:** `phase1-module-api-reference.md`, Parts 3 and 4.

**Change:**
- Session/Run Store (Part 3): note that the API is backend-agnostic;
  SQLite (default) and Postgres are implementations. No signature
  changes.
- Event Bus (Part 4): note in_process (default) and redis backends. No
  signature changes.
- The common types and method signatures are unchanged — this is purely
  a note that implementations vary by configured backend.

---

## Status

Six revisions across six documents. All are reframings and additions;
none changes a public API signature, because both datastores were
already behind backend-agnostic contracts. This is the modularity paying
off — the "mature with scale" decision is a configuration concern, not a
contract change.

Apply in a dedicated pass alongside any other pending revisions.
