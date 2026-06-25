# QoL: Symmetric catalog-asset discovery CLI (rationalize agents, tools, skills, prompts to match list/show pattern)

**Status:** Proposed (Phase 3.x follow-up after Phase 3.0 lands)
**Effort:** ~1-2 days
**Owner:** TBD
**Depends on:** Phase 3.0 (Round 7 L7-12 establishes the symmetric pattern in master workflow spec §14)

---

## Background

Phase 3.0 (per Round 7 locked decision L7-12) establishes a symmetric CLI pattern for catalog-citizen discovery:

```
kolsch <asset-type> list [--bundle <name>] [--format text|json]
kolsch <asset-type> show <ref>         [--format text|json]
```

Phase 3.0 ships this pattern for two asset types:

- `kolsch workflows list` / `kolsch workflows show` (existing per master spec §14; pre-dates Round 7)
- `kolsch nodes list` / `kolsch nodes show` (new from Round 7 L7-12 — the surface that exposes built-in node types as catalog citizens)

The remaining catalog-citizen asset types — **agents, tools, skills, prompts** — already have CLI surfaces from earlier kolsch work (pre-Phase-3.0). Those surfaces don't fully honor the new symmetric pattern: subcommand names, flag names, output shapes, and ref handling vary across asset types and don't all line up with `list`/`show`/`--format text|json` as the canonical shape.

This QoL is the mechanical rationalization that brings the pre-existing CLI surfaces into alignment with the pattern §14 declares canonical.

## Scope

### In scope

- `kolsch agents list [--bundle <name>] [--format text|json]`
- `kolsch agents show <ref> [--format text|json]`
- `kolsch tools list [--bundle <name>] [--format text|json]`
- `kolsch tools show <ref> [--format text|json]`
- Migrate or wrap the existing `kolsch agents` and `kolsch tools` commands to honor the new pattern. Where a pre-existing command name collides, the new shape wins and the old shape is either retired or deprecated with a clear message (see Risks).
- `--format text|json` envelope per the cross-cutting CLI convention (see `process/qol-cli-format-output-rollout.md`); structured payload routed through `src/cli/_format.py:render()`.
- Per-asset-type data schemas for `list` and `show` outputs:
  - **agents list** row: `ref`, `kind`, `label`/`description`, `bundle`
  - **agents show**: full agent asset structure (prompt source, tool refs, skill refs, permissions, etc. — as already authored in agent YAML)
  - **tools list** row: `ref`, `kind`, `label`/`description`, `bundle`
  - **tools show**: full tool asset structure (executor kind, command/handler, parameter schema, permissions surface)

### Out of scope

- **skills and prompts** — smaller catalog citizens with less developed CLI surfaces today. The decision whether they get the symmetric pattern is deferred to a later QoL (or never), depending on whether real usage shows a need. If/when added, this QoL is the template.
- **Search/filter beyond `--bundle`** — no `--tag`, `--kind`, `--grep`, regex matching, etc. Deferred until real usage shapes the requirements.
- **install / marketplace / registry surfaces** — out of scope at all phases per Round 7 locked decision L7-2 (no plugin ecosystem at v1). `list`/`show` operate strictly on assets already resolved into the workspace catalog.
- **Re-shaping the underlying asset YAML schemas.** The `show` payload reflects the asset as-authored; no schema migration.

## Why this is a QoL, not a feature

Master spec §14 (per Round 7 L7-12) establishes the symmetric `list`/`show`/`--format text|json` pattern as canonical for catalog citizens. Phase 3.0 implements it for `workflows` and `nodes`. This QoL is purely the rationalization that brings the pre-existing agents/tools CLI into alignment. There is no semantic change — same catalog, same assets, same ref grammar. Only the discovery surface is being normalized.

## CLI shape

```
kolsch agents list [--bundle <name>] [--format text|json]
kolsch agents show <ref>             [--format text|json]
kolsch tools  list [--bundle <name>] [--format text|json]
kolsch tools  show <ref>             [--format text|json]
```

Strictly `list` + `show` + `--format`. **No per-asset-type lifecycle commands** in this QoL:

- `workflows` carries `run` / `resume` / `fork` because workflows have execution lifecycle.
- `agents`, `tools`, `nodes` do not have analogous lifecycle commands at the catalog level — they're invoked from inside workflow execution, not standalone. (Note: `kolsch run <agent>` for ad-hoc agent invocation lives elsewhere in the CLI and is unaffected by this QoL.)

Refs in `show <ref>` are full 3-segment per Round 3 #5 / the `qol-refs-1seg-removal.md` rules. No 1-seg shorthand acceptance even for one-off operator commands.

## Acceptance Criteria

1. `kolsch agents list` returns one row per agent currently resolvable in the workspace catalog; each row carries `ref`, `kind`, `label`/`description`, and `bundle`. With `--bundle <name>`, results are filtered to that bundle.
2. `kolsch agents show <ref>` returns the full agent asset structure (prompt source, tool refs, skill refs, permissions surface). Invalid ref → clean error per master spec §14.4 exception JSON shape (`AgentNotFound` or equivalent).
3. `kolsch tools list` and `kolsch tools show` work symmetrically with the same field set and the same error contract.
4. Pre-existing `kolsch agents` / `kolsch tools` CLI commands either migrate to the new shape OR are retained behind a deprecation warning that names the new command and the removal phase. No silent breakage.
5. All four commands honor `--format text|json` and route output through `src/cli/_format.py:render()`. Default `text` output is human-friendly; `json` produces the `{command, status, data, errors}` envelope per master spec §14.3.
6. Per-asset-type `data` schemas are documented (mirror the workflows/nodes schemas — added to the CLI command JSON schemas appendix or per-command docs).
7. Tests for each command:
   - `list` with empty workspace (zero matching assets)
   - `list` with N entries (multi-bundle, with and without `--bundle` filter)
   - `show <valid-ref>` (round-trips a known asset)
   - `show <invalid-ref>` (clean error)
   - At least one `--format json` test per command
8. Master spec §14 is updated to mark the agents/tools rows as **shipped** (was "follow-up QoL").

## Effort estimate

- Audit pre-existing CLI for `agents` + `tools` (subcommand names, current flags, current output shapes, callers in tests/docs): ~1-2 hours
- Refactor or wrap to new pattern per asset type: ~3-4 hours × 2 asset types = ~6-8 hours
- `--format json` envelope wiring + schema definition: ~2 hours
- Tests (4 commands × ~4 test cases each = ~16 tests): ~3-4 hours
- Docs (update master spec §14 status, refresh any `--help` text, per-command schema appendix): ~1 hour

**Total: ~1.5-2 days.**

## Risks

- **Existing CLI users with scripts depending on prior output format.** Anyone whose tooling parses `kolsch agents` or `kolsch tools` text output today will see their parsing break if the human-readable shape shifts. Mitigation: ship a deprecation warning on the legacy subcommand path naming the new shape; keep the legacy shape working for one Phase 3.x release before removal. Scripts that need stability should migrate to `--format json` (whose schema is contract-versioned at the §14.3 envelope level).
- **skills/prompts deferral.** Leaving these out leaves the catalog-discovery surface inconsistent — `agents`/`tools` rationalized, `skills`/`prompts` not. Acceptable because skills/prompts are smaller catalog citizens with less operator-facing usage today. If usage grows, a follow-up QoL applies the same template; the cost is one extra rationalization pass, not a redesign.
- **`--bundle` semantics for assets that aren't bundle-scoped.** Built-in/platform-level assets that don't live in a user bundle need a stable label (probably `kolsch` or `<platform>`) in the `bundle` column so `--bundle` filtering is total. Decide during audit; document in the schema appendix.
- **Ref-grammar collisions.** Bundles named `agents` or `tools` are legal (Round 3 #5 freed the 14 built-in type names; bundle names are unconstrained otherwise). The CLI parser must not confuse `kolsch agents show kolsch/agents/foo` with anything else. Click's subcommand routing handles this naturally; flag during audit if not.

## Cross-references

- **Master workflow spec** `process/phase3.0-workflow-spec.md` §14 — Round 7 L7-12 establishes the symmetric `list`/`show`/`--format text|json` pattern and ships it for `workflows` + `nodes`. This QoL extends to agents/tools.
- **Cross-cutting CLI convention** `process/qol-cli-format-output-rollout.md` — the `--format text|json` envelope contract and `src/cli/_format.py` helper. This QoL is one of the rollout's downstream consumers.
- **Ref grammar** `process/qol-refs-1seg-removal.md` — `show <ref>` requires full 3-segment refs.
- **Round 7 L7-2** — no plugin ecosystem at v1 (rationale for keeping install/marketplace surfaces out of scope).
