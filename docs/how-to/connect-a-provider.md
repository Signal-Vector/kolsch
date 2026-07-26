# Connect an LLM provider

This guide takes a fresh Kolsch workspace to a running chat backed
by a real model. Read straight through the first time; skim by
section afterwards.

For the conceptual overview, see
[LLM Connectors](../architecture/llm.md).

## What connecting means

Three things happen atomically when `kolsch connect` succeeds:

1. A new entry lands in `platform.yaml` under
   `llm_providers.<name>` — the `type`, an `api_key_secret` name, an
   optional `base_url`, and a discovered `models` list.
2. The API key value is written to `.env` under the secret name.
   `platform.yaml` never holds the key itself.
3. `discover_models()` runs against the provider and its output is
   cached into the same `platform.yaml` entry, so subsequent
   `kolsch models` calls are offline.

A failed connect leaves both files untouched — writes only happen
after validation and discovery both succeed.

### The `type:` field convention

`llm_providers.<name>.type` names the connector to use for the
entry. When omitted, it **defaults to the dictionary key**, so an
entry keyed `opencode` with no explicit `type:` resolves to the
`opencode` connector automatically. Set `type:` explicitly only
when the connector type differs from the key you chose — for
example, if you want two separate OpenCode-shaped providers
against different endpoints:

```yaml
llm_providers:
  opencode_local:
    type: opencode              # explicit — the key is `opencode_local`
    base_url: http://127.0.0.1:8000/v1
    api_key_secret: OPENCODE_LOCAL_API_KEY
  opencode_prod:
    type: opencode              # explicit — the key is `opencode_prod`
    base_url: https://opencode.example.com/v1
    api_key_secret: OPENCODE_PROD_API_KEY
```

If the `type:` value doesn't match a registered connector, the
runtime raises `llm.connector_unknown` naming the offending
provider entry and pointing back at `platform.yaml`. Run
`kolsch providers` to see the registered types.

## The two shipping connectors

**`openrouter`** — one API key routes to Anthropic, OpenAI, Meta,
and dozens more. It also implements `MetadataSourceConnector`, so
its catalog fills in `context_length`, `supports_tools`, and
`pricing` for models discovered from other providers. This is the
recommended primary. If you want Claude models, this is the
connector — a direct Anthropic connector isn't shipping today.

**`opencode`** — a generic OpenAI-compatible endpoint (Bearer auth,
`/v1/models` for discovery). Use it for OpenAI itself, self-hosted
vLLM, LiteLLM proxies, or any third-party OpenAI-shaped shim. Its
catalog exposes only model IDs; parameter metadata comes from
OpenRouter when both are connected.

## `kolsch connect`

```bash
kolsch connect <provider>
```

No flags. `<provider>` must match a registered `type_name` —
`openrouter` or `opencode` today. Attempts to connect `anthropic`,
`openai`, `bedrock`, or `llama` are recognized as planned and
refused with a pointer to the design doc.

The command runs five phases:

1. **Prompt** the user via the connector's declared
   `auth_prompt_spec()`.
2. **Validate** credentials against a live API call —
   `GET /auth/key` for OpenRouter, `GET /models` for OpenCode. Any
   non-2xx aborts with the HTTP code.
3. **Discover** the model catalog. Aborts on network or parse
   failure.
4. **Augment** discovered entries against connected metadata sources
   (today: OpenRouter) when parameter fields are missing.
5. **Write** `.env` and `platform.yaml` atomically (temp file →
   fsync → rename).

## Where the API key goes

The value is written to `<workspace>/.env` under a name derived by
convention:

```
<PROVIDER_NAME_UPPER>_API_KEY
```

Hyphens and dots become underscores. So `kolsch connect openrouter`
writes `OPENROUTER_API_KEY=...`. The name is recorded in
`platform.yaml` as `llm_providers.<name>.api_key_secret`; the
runtime resolves it back through the workspace's Secrets provider
(dotenv by default). Existing lines in `.env` are preserved
verbatim — the writer rewrites only the matching key or appends a
new line.

## The auth prompt flow

Every connector declares an `AuthPromptSpec` listing the fields it
needs. The CLI walks the fields; the connector never touches stdin.

| Field kind | UI behaviour |
|---|---|
| `secret` | prompted via `getpass` (no echo) |
| `url` / `string` | prompted with the default in `[brackets]`; empty input accepts the default |
| `choice` | pick from a list of options |

**OpenRouter** asks for one field: `api_key`.

**OpenCode** asks for two: `base_url` (defaults to a placeholder —
you must supply your real endpoint) and `api_key`.

Required fields left empty abort the connect with exit code 1.

## Model discovery: `kolsch refresh`

```bash
kolsch refresh              # refresh every connected provider
kolsch refresh <provider>   # refresh one
```

Refresh re-runs `discover_models()` using the API key resolved from
the Secrets provider (no re-prompt), then rewrites the `models` list
in `platform.yaml`. Each entry carries `id` plus, when the source
knows them, `context_length`, `supports_tools`,
`supports_streaming`, and `pricing`. A failure on one provider is
reported and the next continues.

**Self-heal on refresh** (qol-091). `refresh` also:

- Re-runs metadata augmentation against any currently-connected
  `MetadataSourceConnector` (OpenRouter is the shipping one) so
  models from providers that don't publish `context_length`
  themselves — chiefly `opencode` — get the field filled in from
  the metadata source. Useful when a provider was connected BEFORE
  the metadata source, since the connect-time augmentation loop
  would have found no source to consult.
- Retroactively writes `serves_as_metadata_source: true` on any
  provider block whose connector is a metadata source but whose
  entry lacks the flag (older workspaces that predate the marker).

Idempotent — re-running produces no diff once everything's aligned.

### Cross-provider `context_length` fallback (runtime)

Independent of `refresh`, the engine's runtime `context_length`
lookup falls through to other configured providers when the
caller's own provider entry has no value. Two things happen at
resolution:

1. **Same-provider suffix match** — a short-form spec
   (`kimi-k2.6`) resolves against a same-provider entry with a
   vendor-prefixed id (`moonshotai/kimi-k2.6`), or the reverse.
2. **Cross-provider fallback** — if the caller's provider has
   nothing, iterate every other configured provider in
   `platform.yaml` insertion order looking for the same model (with
   the same suffix-matching). First int-valued hit wins.

If two other providers disagree (different `context_length` for the
same suffix), the first-in-config-order value wins AND a `warning`
log fires naming both candidates + values. `None`-valued candidates
are skipped entirely — they carry no signal. The `mock` provider
short-circuits the fallback (its id space collides with real
providers' suffixes; borrowed metadata would be worse than an
honest `None`).

The upshot for daily use: connect OpenRouter alongside OpenCode (or
any metadata-poor provider), and compaction resolves the correct
context length for opencode-served models without you having to
paste a `context_length` into `platform.yaml` by hand.

## Listing what's available

```bash
kolsch providers                    # registered types + connected providers
kolsch models                       # every provider's cached catalog
kolsch models <provider>            # one provider
kolsch models --filter <substring>  # narrow a long catalog
kolsch models --verbose             # context_length, tool support, pricing
kolsch models --json                # machine-readable
```

`models` reads only the cache — no live API calls. Run `refresh` if
the catalog looks stale.

## Setting a default model

Agents can omit `model:` and inherit from `platform.yaml`'s
`defaults.model`. Two shapes are accepted:

```yaml
defaults:
  model: { provider: openrouter, model: anthropic/claude-opus-4-7 }
```

Or via an alias:

```yaml
defaults:
  model: opus
model_aliases:
  opus: openrouter/anthropic/claude-opus-4-7
```

`kolsch setup` writes this field automatically. Its picker prefers,
in order: an alias called `opus`, `smart`, or `cheap`; then the
first alphabetical alias; then the first model from `openrouter`;
then from `opencode`; then falls back to
`{provider: mock, model: mock-model}` if nothing is connected.

## Disconnecting

```bash
kolsch disconnect <provider>
```

Removes the `llm_providers.<provider>` entry from `platform.yaml`
and deletes the `<PROVIDER>_API_KEY` line from `.env`. Other `.env`
entries are preserved. If the provider wasn't connected, exit code
1 with a note.

`model_aliases` and `defaults.model` are **not** touched — clean
them up by hand if you were pointing at a model from the removed
provider, or the next agent load will fail with
`llm.provider_not_configured`.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `HTTP 401 from /auth/key` (OpenRouter) or `/models` (OpenCode) | Bad or revoked API key | Regenerate the key, re-run `kolsch connect` |
| `HTTP 403` | Key valid but lacks scope, or account suspended | Check the provider's dashboard |
| `HTTP 404` at validation | Wrong `base_url` for OpenCode (default is a placeholder) | Re-run `kolsch connect opencode` with your real endpoint |
| `HTTP 429` during discovery | Rate-limited | Wait, then `kolsch refresh <provider>` |
| `ConnectError` / `ConnectTimeout` | DNS / firewall / proxy | Check corp proxy, VPN, `HTTPS_PROXY` |
| `discovery failed: response payload missing 'data' list` | Endpoint isn't OpenAI-shaped, or an SSO login page is intercepting | Verify `base_url` |
| `unknown provider: 'anthropic'` | Planned but not shipping | Use `openrouter` for Claude |
| Agent fails with `model.authentication_failed` at runtime | Secret rotated after connect | Re-run `kolsch connect <provider>` |
| Agent fails with `llm.provider_not_configured` at runtime | Agent references a `provider:` not in `llm_providers` | `kolsch providers` to see what's connected; fix the agent's `model:` |
| Agent fails with `llm.connector_unknown` naming a provider | `llm_providers.<name>.type` is set to something unregistered (typo) | Fix the `type:` value in `platform.yaml` to a registered type, or delete the line to fall back to the dictionary key (see the `type:` convention above) |

## A worked example — OpenRouter + Claude, from empty

```bash
# 1. Scaffold the workspace, connect a provider, ship a starter agent.
kolsch setup
#   → pick openrouter at the provider prompt
#   → paste your OpenRouter API key
#   → accept the starter-agent and test-run prompts
```

After that, three things exist:

- `./platform.yaml` — `llm_providers.openrouter` populated, models
  cached, `defaults.model` set to the first available.
- `./.env` — `OPENROUTER_API_KEY=sk-or-...`.
- `./bundles/kolsch/` — starter bundle in place.

```bash
# 2. Confirm and inspect the catalog.
kolsch providers
kolsch models openrouter --filter claude --verbose
```

Edit `platform.yaml` to pin Claude Opus:

```yaml
model_aliases:
  opus: openrouter/anthropic/claude-opus-4-7
defaults:
  model: opus
```

Then:

```bash
# 3. Talk to it.
kolsch chat kolsch/agents/helper
```

If any step fails, re-read the troubleshooting table, fix the
underlying issue, and re-run the single failing command.
`kolsch connect` is idempotent — running it a second time on an
already-connected provider re-prompts and overwrites the entry
cleanly.

---

The class reference is at
[API: LLM Connectors](../api/index.md#llm-connectors). For the
architecture pattern (connector protocol, registry, `build_llm`),
see [LLM Connectors](../architecture/llm.md).
