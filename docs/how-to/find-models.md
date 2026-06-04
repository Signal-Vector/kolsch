# Find Available Models

Short recipes for listing the models your connected providers expose,
so you know what to put in an agent's `model:` field. For *why* model
discovery works the way it does, read the [LLM providers
contract](../explanation/llm-providers.md).

## List everything

```bash
kolsch models
```

Prints one section per connected provider, each model on its own line.
Footer reminds you of the canonical form to copy into agent YAML:

```yaml
model: { provider: <provider>, model: <model_id> }
```

…or, if you've defined an alias for a model:

```yaml
model: <alias>
```

## List one provider

```bash
kolsch models opencode
kolsch models openrouter
```

Useful when one provider has a huge catalogue (OpenRouter exposes
200+ models). Exits 1 with a friendly message if the provider isn't
connected — point of friction is `kolsch connect <provider>`, not the
listing.

## Narrow a long list

```bash
kolsch models openrouter --filter haiku
kolsch models --filter gpt-4
```

Substring match on the model id (case-insensitive). Applies across
all providers when no `<provider>` is given.

## See metadata (context length, pricing, capabilities)

```bash
kolsch models --verbose
kolsch models openrouter --filter opus --verbose
```

`--verbose` shows the parameter fields each provider exposed at
discovery time: `context_length`, whether the model supports tool
calling, whether it supports streaming, and per-million-token
pricing. Models discovered without metadata show `?` for those
fields — typical for OpenCode-style discovery which returns ids
only. Run `kolsch refresh <provider>` to re-pull.

`--verbose` also surfaces the `[metadata-source]` flag on providers
configured as a metadata source (OpenRouter typically).

## Script-friendly output

```bash
kolsch models --json | jq '.openrouter.models[] | select(.context_length > 100000)'
```

`--json` produces a stable JSON document keyed by provider name. Each
provider entry has `models` (a list) and `serves_as_metadata_source`
(a boolean). Each model entry carries every field discovery captured,
plus an `alias` field when one matches.

## Aliases — promote a model to a shortcut

Aliases are deployment-shareable strings users can type into agent
YAML in place of the canonical `provider/model` pair. Edit
`platform.yaml` directly:

```yaml
# platform.yaml
model_aliases:
  opus: openrouter/anthropic/claude-opus-4-7
  haiku: openrouter/anthropic/claude-haiku-4-5
  cheap: opencode/qwen2.5-coder-32b
```

`kolsch models` will then show the alias next to its target model.
Aliases don't chain (an alias's target is a canonical
`provider/model`, not another alias) and are validated at agent load
— an agent referencing an unknown alias fails with
`config.unknown_alias` (Fatal).

The `kolsch connect` flow can suggest aliases for the provider it's
setting up; accept the suggestions there or add your own here.

## When models are missing

A provider connected via `kolsch connect` discovers its catalogue at
connect time. If you see `(no models discovered — run kolsch refresh
<provider>)`, the discovery call returned an empty list (provider
side issue or the connect flow failed silently). Re-run:

```bash
kolsch refresh <provider>
```

If the catalogue truly is empty (provider has no public models, or
your account has none), `kolsch models` will still print the provider
header with a zero count — no error, just no rows.

## How agents use what you find here

```yaml
# bundles/your-bundle/your-agent.yaml
schema_version: "0.1"
agent_id: ag_yours
name: yours
model: { provider: openrouter, model: anthropic/claude-opus-4-7 }
# or:
# model: opus
fallback_models:
  - haiku
prompt: …
io: { mode: conversational }
```

`fallback_models` accepts the same three input shapes as `model:`
itself (canonical object / `provider/model` string / alias). See
[configure-fallbacks.md](configure-fallbacks.md) for the chain
semantics.

## Where to read next

- Tutorial: [first-agent.md](../tutorials/first-agent.md) — Step 9
  uses these commands.
- How-to: [configure-fallbacks.md](configure-fallbacks.md) —
  multi-model resilience.
- Spec: [llm-providers.md](../explanation/llm-providers.md) — why
  providers are connected, not configured.
