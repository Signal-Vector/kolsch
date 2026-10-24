# Workflow Policy Model

This is the cross-phase explanation of how policy applies to workflows: which layers exist, in what order they are walked, how the engine decides which layers are eligible, how `from:` scopes a rule by caller chain, and how policy decisions are audited. The validation rules that enforce this model at parse/load time live with the phase that owns workflow parsing (see `process/phase3.0-workflow-spec.md` §13). The shape and semantics described here are stable across phases — the validation surface around them may evolve.

## Terminology

User-facing terminology in this document uses "policy" — the YAML field is `policy:`, the event kind is `policy.decision.made`, the layer names end in `_policy`. The underlying Python module names stay `src/providers/authz.py` etc. until the follow-up rename QoL (`process/qol-policy-rename.md`) ships. The word "authorization" still appears here where it is the natural English word for the concept (the act of permitting or denying an action); the user-facing identifiers are `policy`.

## Node-type refs

**Three-segment qualified form is REQUIRED EVERYWHERE** — tools, agents, workflows, node types, and the `type:` field on workflow nodes. The shape is `<bundle>/<asset_type>/<name>`. Examples: `kolsch/tools/bash`, `kolsch/agents/triager`, `kolsch/workflows/hello`, `kolsch/nodes/parse_classification`. ALL builders route through `src/assets/refs.py:build_ref(bundle, asset_type, name)` — the ONE canonical builder.

(Throughout this document, "node ref" specifically means a ref pointing at a NODE TYPE in the catalog — e.g., `kolsch/nodes/parse_classification`. The `nodes:` mapping key inside a workflow YAML — the "node NAME" — is a workflow-local identifier and not part of the catalog ref grammar.)

1-segment shorthand (`bash`, `agent`, etc.) is REMOVED. A migration codemod is available — see `process/qol-refs-1seg-removal.md`.

The 2-segment form (`<bundle>/<name>`) is DEPRECATED EVERYWHERE. Existing 2-seg authoring in policy rules continues to load through the policy migration shim; new authoring uses 3-seg. This supersedes the earlier `qol-prefix-defaults-and-unified-refs.md` proposal (REVOKED).

- Catalog form: 3-segment `<bundle>/<asset_type>/<name>`.
- Rules `what:` form: same 3-segment shape. Disambiguates by asset_type when a bundle has a tool and a node type with the same name.
- Action namespace: `node.<name>` (system-action style; parallels `tool.<name>`).

Example rules:

```yaml
policy:
  - { what: "kolsch/nodes/parse_classification", effect: "allow" }
  - { what: "kolsch/tools/bash", pattern: "curl *", effect: "allow" }
  - { what: "kolsch/nodes/*", effect: "deny" }                          # all nodes in bundle
```

### Tool-ref migration consideration

Tools authored before this iteration used the two-segment `<bundle>/<name>` form in policy rules. With three-segment as canonical EVERYWHERE per Decision 1, the 2-segment form is now DEPRECATED across the board. Existing 2-seg rules continue to load through the existing migration shim. New tool authoring should use the 3-seg shape `<bundle>/tools/<name>`. Phase 3.x may flip the shim to emit warnings; full removal is a future effort.

## Layer order

Walked first-match-wins per layer; a match at any layer (allow / deny / prompt) ends the walk:

0. Locked platform rules — `platform.yaml`'s `authorization.config.locked_rules`. Deny-only and evaluated before every layer below, so a `policy:` allow can't override them (see the [authz contract §3.8.4](secrets-sandbox-authz-contract.md#384-locked-platform-rules)).
1. `workflow_policy` — workflow YAML's top-level `policy:` block.
2. `agent_policy` — existing agent-level rules.
3. `bundle_policy` — existing bundle-level rules.
4. Platform `_rules` — existing platform-level rules.
5. Default deny.

Layers 1–3 sit above the platform on purpose: a workflow `policy:` allow overrides a platform deny for that workflow's builtin nodes. Only stage 0 is beyond any layer's reach.

**`policy:` is NOT inherited across `extends:` (R6 S5).** When workflow `B` `extends:` workflow `A`, `B`'s `workflow_policy` layer is sourced from `B`'s OWN `policy:` block only — `A`'s `policy:` rules do NOT contribute. The reasoning is the same as the proximate-caller rule below: extending a workflow is a structural composition of nodes + edges; it should NOT silently grant the extender whatever elevations the extended workflow declared. If `A` had `policy: [{what: kolsch/tools/bash, effect: allow}]` and `B` `extends: [acme/workflows/A]`, then a node in `B` firing `bash` does NOT inherit that allowance — `B` must declare its own `bash` allowance or fall back through `agent_policy` / `bundle_policy` / platform `_rules`.

Shared policy patterns belong in `bundle.yaml` or `platform.yaml`: those layers ARE walked naturally by the proximate-caller rule, so a rule in `bundle.yaml` applies to every workflow in that bundle without needing per-workflow `extends:` plumbing. An extended workflow's `policy:` rules still apply when THAT workflow is invoked DIRECTLY — the rules aren't deleted, they're just confined to the workflow that declares them.

### Engine-side layer selection (escalation prevention; proximate-caller rule)

The engine — not the matcher — decides WHICH layers are included in the walk based on the PROXIMATE CALLER frame. "Proximate caller" means the entity that ACTUALLY initiated the tool call at the moment of the call, not the entity at the top of the caller chain.

- A workflow's own node fires a tool → the proximate caller is the workflow → engine includes `workflow_policy` in the walk (with `agent_policy` / `bundle_policy` / platform `_rules` following).
- An EMBEDDED AGENT (an `agent`-type node spawned by the workflow, now mid-thread) fires a tool → the proximate caller is the agent → engine OMITS `workflow_policy` from the walk; the agent's own `agent_policy` becomes the first layer (with `bundle_policy` / platform `_rules` following).

This is structural at the call site — no rule field is required, no `from:` declaration is involved. It prevents embedded agents from inheriting the workflow's elevations: a workflow that's allowed `bash` does NOT silently grant the embedded agent `bash`; the agent must declare its own `bash` allowance via `agent_policy`. The caller chain (below) is still RECORDED for forensics and `from:` matching, but layer SELECTION is purely a proximate-caller decision.

### `from:` field on Rule (caller-chain scoping)

Optional `from:` glob on any Rule, constrained at v1 to match agent and workflow refs ONLY:

- **Form**: `from: <bundle>/{agents,workflows}/<name>` (glob OK in any segment: `from: acme/agents/*`, `from: */workflows/triage`).
- **Matching**: against the caller chain — transitive (chain-aware): the rule fires if ANY frame in the chain matches the glob.
- **Empty caller chain + `from:` set** → FAIL-CLOSED (rule does not match).

The `from:` grammar is fully strict at parse/validate time (R4 #13 lock) — the specific hard-error classes the parser raises for malformed `from:` values live in the phase that owns parsing (see `process/phase3.0-workflow-spec.md` §13.7).

Other rules:

- `core/principals/{user,system}` is NOT added at v1 (deferred to Phase 4+).
- Principal strings (`user.local`, `system`) remain at their existing engine sites as TELEMETRY only; they NEVER feed the matcher.

The matcher consults `from:` first: if a rule has `from:` set and the caller chain is empty, the rule fails closed; otherwise the rule matches only if some frame in the chain matches the glob. The `caller_chain` reaches the matcher via `allow(..., caller_chain: tuple[str, ...] = ())`.

### Caller chain construction

Ordering: PROXIMATE FIRST (`chain[0]` is closest to the tool call), distal last.

Push events:
- Agent execution starts → push `<bundle>/agents/<name>`.
- Workflow execution starts → push `<bundle>/workflows/<name>`.
- A sub-workflow node invokes a child workflow → push the child workflow's frame.
- An agent invokes a sub-agent → push the callee agent's frame.

Pop on return or error.

Edge cases:
- Engine bootstrap (no agent or workflow active yet) → `chain = ()`.
- Synthetic skill read fired during agent `X` → `chain = [<X-ref>]` (engine threads the current agent frame; the `principal="system"` telemetry stays where it is and is not the matcher input).

**Persistence.** The caller chain is no longer in-memory only. The events `workflow.tool.called`, `workflow.node.started`, and `model.request.sent` carry the resolved `caller_chain: list[str]` in their payload. This enables "who authorized this?" forensics after a run completes — the chain at decision time is recoverable from the JSONL alone.

### Refs unification (single grammar across agents, tools, workflows, nodes)

One module owns three-segment ref operations: `src/assets/refs.py` (extending the existing module that already has `_TYPE_TO_SEGMENT`).

**`AssetCatalog.lookup()` is the chokepoint for catalog access.** All catalog access in `src/workflows/*`, `src/providers/policy.py` (legacy `src/providers/authz.py` until the rename QoL ships), and other call sites routes through `AssetCatalog.lookup(ref, expected_type)`. Direct dict access (`catalog.assets[ref]`) is permitted ONLY inside `src/assets/catalog.py` itself. The chokepoint discipline gives one place where ref shape validation, missing-entry behavior, and type-checking expectations are centralized — call sites never invent their own catalog access pattern.

**Surface (per Decision 1):**
- `build_ref(bundle, asset_type, name) -> str` — the ONE canonical builder. ALL call sites use this.
- `parse_three_segment(ref) -> tuple[str, str, str]` — pure shape check returning `(bundle, asset_type, name)`. Raises if the ref isn't 3-segment.
- `is_three_segment(ref) -> bool` — cheap predicate (no exceptions).
- `_TYPE_TO_SEGMENT` gains `"workflow": "workflows"` and `"node": "nodes"`.

`AssetCatalog` gains `lookup(ref, expected_type=None) -> AssetEntry | None`, wrapping `parse_three_segment + dict get`.

Verdict: agents, tools, workflows, and nodes all share ONE ref grammar, ONE builder, ONE shape-parser, ONE catalog lookup, ONE matcher.

## Modal integration

The existing `prompt` effect + no-rule modal callback fire at every layer. Workflow nodes route through the existing chat modal when in interactive chat context; non-chat callers (CLI batch) get default-deny per existing semantics.

## Secrets

`@node(secrets_needed=[...])` declarations parsed at module load. Run-start gate verifies each name exists in the secrets subsystem. Pre-resolved values injected into `ctx.secrets[name]` at node fire time.

## Configurable policy audit emission

Every policy decision the engine makes (during the workflow's tool-dispatch wrapper) MAY emit a `policy.decision.made` event. The emission rate is configurable via `platform.yaml` under the `policy:` block:

```yaml
policy:
  config:
    audit_level: rule_matched  # DEFAULT — emit only when an explicit rule fired (allow or deny)
    # audit_level: all          # full audit — emit on every decision (compliance use)
```

(The configuration block is named `policy:` to align with the user-facing rename; the underlying provider module remains `src/providers/authz.py` until the rename QoL ships. The legacy `authorization:` block continues to load via the migration shim.)

**`audit_level` values (typed as `str`, not `Literal`):**

| Value | Meaning |
|---|---|
| `all` | Persist every policy decision. |
| `rule_matched` | Persist only decisions where an explicit rule fired (allow or deny). |

`audit_level` is typed as `str` (not `Literal`) so the field stays forward-compatible — adding a new policy value is an additive change. The documented value table above is the contract; tests assert on these exact strings.

**Default is `rule_matched`** (R4 #12 lock; changed from earlier `all`). Compliance-driven operators set `audit_level: all` explicitly in their `platform.yaml` or `bundle.yaml`; the default keeps high-throughput workflow runs from saturating the JSONL with default-path emissions.

Event payload:

```json
{
  "kind": "policy.decision.made",
  "ts": "...",
  "workflow_run_id": "wrk_...",
  "trace_id": "tr_...",
  "action": "kolsch/tools/bash",
  "target": "rg --files",
  "effect": "allow",
  "rule_id": "<rule id or '_default'>",
  "layer": "workflow_policy",
  "caller_chain": ["kolsch/workflows/triage", ...]
}
```

The Rule-based policy provider's config Pydantic model gains `audit_level: str = "rule_matched"` (validated against the documented value table). The workflow engine's tool-dispatch wrapper reads the setting and emits accordingly.
