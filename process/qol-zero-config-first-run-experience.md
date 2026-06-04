# QoL — Zero-config first-run experience

**Draft 0.1** *(spawned by E-BUG-MODELLINE-001 coordinator resolution per `bug-model-line-placeholder-collides-with-jinja.md` Part 5 "Adjacent finding" + Part 6 "Out of scope". This QoL owns the seed-default decision that bug deferred.)*

> The `default_platform_payload` seed at `src/assets/platform_config.py:88-97` ships `defaults.model: {provider: anthropic, model: claude-opus-4-7}` on first run. The starter-pickem flow at `src/cli/_setup.py:600-666` overwrites that seed during `kolsch setup` based on what's actually connected: any alias → preferred alias; any connected provider → first model; nothing connected → `{provider: fake, model: fake-model}`.
>
> The seed therefore matters in exactly one scenario: a workspace materialized WITHOUT `kolsch setup` (e.g., direct import of the package + manual scaffolding, or a fresh workspace touched by a non-setup command). The seed is the "what happens if no one tells us anything" default — and right now that default points at a provider (anthropic) that the picker would not even consider, against a model the user almost certainly has no key for.
>
> This is a small but real UX paper-cut. The bug that surfaced it (E-BUG-MODELLINE-001) explicitly declared the seed value out of scope; this QoL is where the conversation continues.

---

## Part 0 — Why this matters

### 0.1 The seed is reachable

Three paths land an agent against `defaults.model` without going through `kolsch setup`:

1. **Direct programmatic use.** A library consumer instantiating `AssetManagement(root=tmp_path)` against a fresh dir triggers `platform_config.load_or_create`, which writes `default_platform_payload()` to disk and returns it. The agent that loads next inherits `defaults.model: anthropic/claude-opus-4-7`.

2. **First-touch via a non-setup CLI command.** Today the CLI gates non-setup commands behind `workspace_is_initialized()` (exit 3 with a friendly message), so this is currently impossible from the CLI. If that gate ever loosens (or a `--init-on-demand` flag lands), the seed is what the user gets.

3. **Setup partial completion.** `kolsch setup` runs Step B (writes `_setup_platform_payload()` which explicitly sets `defaults: {}` — so the seed's `defaults.model` is wiped here). Then if Step D fails before writing `defaults.model`, the workspace has `defaults: {}` and the agent's resolved config has no model. Pydantic raises `ConfigInvalid: model required`. The user sees a structural error rather than a usable workspace.

The first path (programmatic) is the one this QoL most directly addresses.

### 0.2 Two candidate seed values, two different UX bets

**Option A — keep `anthropic/claude-opus-4-7`** (current state).
- Pro: opinionated default. If the user happens to have `ANTHROPIC_API_KEY` set, they get a real, capable model on first invocation.
- Con: hardcodes a provider the user may not have connected. Loading the agent succeeds (provider name + model name validate); the *first model call* fails with `llm.provider_not_configured` or `model.authentication_failed`. The error is correct but late.

**Option B — switch to `fake/fake-model`** (the picker's fallback).
- Pro: always runnable. The fake LLM echoes input; no external creds needed; user sees the workspace work end-to-end on first run.
- Con: less opinionated. A user pasting `kolsch run` into a fresh workspace gets an echo, not a real answer. The "ah, I need to connect a provider" learning moment is delayed by one step.

**Option C — make the seed conditional on environment.**
- If `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` / `OPENCODE_API_KEY` are present in the env at load time, pick the best alias from the corresponding provider; else fall back to fake.
- Pro: best of both — opinionated when the env supports it, safe when it doesn't.
- Con: introduces env-aware behavior at the platform-config layer, which is currently env-agnostic. Adds a small surface area.

---

## Part 1 — Open questions for the coordinator

1. **Which seed value?** A, B, or C above.
2. **Should `_setup_platform_payload()` stop wiping `defaults: {}`?** Right now it wipes the stock seed entirely (per the long-standing "stock `defaults.memory` doesn't fit the kernel's `ResolvedAgentConfig` shape" comment at `_setup.py:819-822`). After E-BUG-MODELLINE-001 (a-prime), setup writes `defaults.model` explicitly in Step D. So Step B's wipe is fine for `defaults.model` (Step D overwrites). But `defaults.memory` is still dropped on the floor — is that intentional, or a leftover that should be fixed alongside this QoL?
3. **Should the seed change be paired with a `kolsch doctor` advisory** that warns when `defaults.model.provider` doesn't match any connected provider? That would convert the late "first-call-fails" UX into an explicit pre-flight check.

---

## Part 2 — Acceptance criteria (draft, pending coordinator)

The QoL is done when:

1. The chosen seed value (A / B / C) is in `default_platform_payload()` with an inline comment explaining the choice.
2. A unit test in `tests/unit/assets/test_platform_config.py` pins the seed value.
3. A component test seeds a fresh workspace via `load_or_create` (NOT via `kolsch setup`) and verifies that loading the starter helper produces a runnable agent.
4. If option C is chosen: tests cover all four env-state branches (anthropic-only, openrouter-only, opencode-only, none).
5. Docs update: `docs/how-to/workspace-and-setup.md`'s "Where the starter agent's model comes from" section names the seed behavior alongside the setup-picker behavior.
6. If question 2 ("should `_setup.py` stop wiping defaults") is answered yes: `_setup_platform_payload()`'s defaults-wipe is removed; tests around `defaults.memory` flow through (or `defaults.memory` is moved out of the seed if it doesn't belong there).

### Out of scope

- The picker logic in `_pick_starter_model` (`_setup.py:600-666`). That tie-breaker is separately specified by `bug-setup-test-run-failure-handling.md`; this QoL only touches what the seed (no-setup path) does.
- The Jinja vs. setup-substitution collision. That was E-BUG-MODELLINE-001 and shipped under `bug-model-line-placeholder-collides-with-jinja.md`.

---

## Part 3 — Related work

- **E-BUG-MODELLINE-001** (`bug-model-line-placeholder-collides-with-jinja.md`) — the parent bug. Its coordinator resolution deferred this QoL.
- **`bug-setup-test-run-failure-handling.md`** — owns the picker tie-breaker (which provider/alias gets chosen when `kolsch setup` runs).
- **`qol-setup-evolution-and-defaults.md` Step 6** — defines the `defaults.<field>` resolver fallback layer that this QoL's seed plugs into.
- **Adjacent latent issue** (not in scope here): alias strings in `defaults.model` (e.g., `defaults.model: opus`) are not expanded by the resolver — `pre_resolve_model` is defined in `src/assets/assembler.py:165` but never called from the assembly flow. ResolvedAgentConfig.model_validate then fails because ModelSpec requires `{provider, model}` dicts. Surfaced during E-BUG-MODELLINE-001 Step 3 e2e when a setup against a workspace with `model_aliases.opus` triggered a `ConfigInvalid` at test-run time. A separate ticket should own this.
