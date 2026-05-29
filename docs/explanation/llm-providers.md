# Phase 1.1 — LLM Provider Configuration

**Draft 0.1**

> The layer between "agent says `{provider: anthropic, model:
> claude-opus-4-7}`" and "the platform actually calls the model."
> Closes a Phase 1 gap: agents couldn't actually run end-to-end
> without ad-hoc LLM wiring.
>
> Five locked decisions; one helper; two Protocols; one registry;
> reference implementations for OpenCode (basic) and OpenRouter
> (dual provider + metadata-source). Four other named providers
> documented as planning sketches for later patches.

---

## Part 1 — Decisions Locked

1. **No new LLM client abstraction.** The standard is
   `crewai.LLM`. The platform ships a small `build_llm` *helper*,
   not a parallel client hierarchy. The Agent Engine works with
   `crewai.LLM` objects directly; tests inject the Fake LLM by
   subclassing `crewai.LLM` (or its base).
2. **No shipped model registry.** Platforms shipping registries go
   stale. Discovery at connect time from each provider's own API,
   augmented (optionally) by cross-provider metadata from
   OpenRouter when the user has connected it. If neither path
   yields parameters, models still work — just less optimally
   (no pre-flight context-overflow checks, no cost estimates).
3. **Modular providers behind one Protocol.** Adding a provider =
   one file in `src/llm/providers/` + one line in
   `src/llm/providers/__init__.py`. The CLI, config schema, and
   engine never grow.
4. **v1.1 ships OpenCode + OpenRouter only.** Two reference shapes
   covering the bulk of the design space. The four other named
   providers (Anthropic, OpenAI, Bedrock, Llama-local) are
   documented as worked planning examples (Part 5); their
   implementation is later patches.
5. **Default install requires no LLM provider.** A fresh `kolsch`
   install runs the CLI, accepts agent definitions, and serves the
   HTTP surface. *Executing* an agent requires a provider —
   attempting to do so without one fails at engine pre-flight with
   `llm.provider_not_configured`.

---

## Part 2 — The `build_llm` Helper

The whole point of the abstraction layer fits in one async function.

```python
# src/llm/build_llm.py

async def build_llm(
    spec: ModelSpec,
    provider_config: ProviderConfig,
    secrets: SecretsProvider,
) -> crewai.LLM:
    """Bridge from ModelSpec to a usable crewai.LLM."""
    connector = registry.get(provider_config.type)
    api_key = (
        await secrets.resolve(provider_config.api_key_secret)
        if provider_config.api_key_secret
        else None
    )
    return await connector.build_llm(
        spec.model, provider_config, api_key,
    )
```

That's the whole helper. Every provider-specific concern lives in
the connector's `build_llm` method (Part 3). Adding a provider does
not modify this function.

**Pre-flight integration.** The Agent Engine calls this at
pre-flight step 6 (engine contract §2.1) for each exchange. The
returned `crewai.LLM` is held for the exchange's lifetime and
passed to the Framework. Failures map to the Fatal
`llm.*` codes (Error Model §4.13).

**Test path vs production path.** The engine constructor accepts an
optional `llm: Callable[[LLMRequest], Awaitable[LLMResponse]]`
parameter alongside `llm_providers_config`. Both seams coexist by
design:

- **Production path** — leave `llm=None`; pass `llm_providers_config`
  populated from `platform.yaml`'s `llm_providers` section. Pre-flight
  step 6 calls `build_llm` per exchange to get a `crewai.LLM`.
- **Test path** — pass `llm=<callable>` directly. Pre-flight step 6
  returns ``None`` without consulting the registry; the framework
  uses the injected callable. The FakeFramework + EchoLLMDriver
  bootstrap relies on this so tests don't need any provider config.

A future patch may unify the two paths by deprecating the direct
callable. For now, both seams are first-class.

---

## Part 3 — The `ProviderConnector` Protocol

The shape every provider implements. Lives in
`src/llm/protocol.py`.

```python
class ProviderConnector(Protocol):
    type_name: ClassVar[str]

    def auth_prompt_spec(self) -> AuthPromptSpec:
        """Declarative description of what to ask the user
        during `kolsch connect`. The CLI walks this spec; no
        interactive code lives in the connector."""

    async def validate_credentials(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> ValidationResult:
        """Verify the credentials work. Typically a tiny live
        API call."""

    async def discover_models(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> list[ModelEntry]:
        """Return the models available from this provider. Each
        ModelEntry carries whatever parameters the provider
        exposes natively; unknowns are None."""

    async def build_llm(
        self,
        model_id: str,
        provider_config: ProviderConfig,
        api_key: str | None,
    ) -> crewai.LLM:
        """Construct a configured crewai.LLM for the named
        model."""
```

### 3.1 Supporting dataclasses

```python
@dataclass(frozen=True)
class AuthPromptSpec:
    provider_label: str           # "OpenCode", "OpenRouter", ...
    intro: str
    fields: list[PromptField]


@dataclass(frozen=True)
class PromptField:
    name: str
    label: str
    kind: Literal["secret", "url", "string", "choice"]
    default: str | None = None
    choices: list[str] | None = None
    required: bool = True


@dataclass(frozen=True)
class Credentials:
    values: dict[str, str]        # opaque to the platform


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str | None = None
    metadata: dict | None = None  # e.g., {"credits_remaining": 1247}


@dataclass(frozen=True)
class ModelEntry:
    id: str
    context_length: int | None = None
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    pricing: ModelPricing | None = None


@dataclass(frozen=True)
class ModelPricing:
    prompt_per_million: float
    completion_per_million: float


@dataclass(frozen=True)
class ModelMetadata:
    """Same shape as ModelEntry's parameter fields, no `id`."""
    context_length: int | None = None
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    pricing: ModelPricing | None = None
```

### 3.2 The `MetadataSourceConnector` extension

A second Protocol, layered on top of `ProviderConnector`, for
connectors that can also serve as a cross-provider metadata
source. Today only OpenRouter implements it; nothing in the
platform assumes more than one will ever exist, but the loop in
the CLI's connect flow handles any number.

```python
class MetadataSourceConnector(ProviderConnector, Protocol):
    async def lookup_model_metadata(
        self,
        provider: str,
        model_id: str,
        credentials: Credentials | None = None,
    ) -> ModelMetadata | None:
        """Try to resolve metadata for (provider, model_id).

        Returns the metadata if known, None if the source has no
        entry. Implementations translate the platform's
        (provider, model_id) into whatever identifier the source
        uses — OpenRouter prefixes Anthropic models with
        `anthropic/`, for example, while Anthropic itself does
        not.
        """
```

Connectors that do not serve as metadata sources simply implement
`ProviderConnector`, not this. They never have to think about the
metadata story.

### 3.3 The connector registry

One process-global map. Lives in `src/llm/registry.py`.

```python
def register(type_name: str, connector_cls: type[ProviderConnector]) -> None:
    """Register a connector class under its type_name."""

def get(type_name: str) -> ProviderConnector:
    """Return a connector instance. Raises if unknown."""

def known_types() -> list[str]: ...

def connected_metadata_sources() -> list[MetadataSourceConnector]:
    """Inspect platform.yaml's current llm_providers and return
    instances of any that (a) are connected and (b) implement
    MetadataSourceConnector."""
```

Registration happens once, in `src/llm/providers/__init__.py`:

```python
from src.llm.registry import register
from .opencode import OpenCodeConnector
from .openrouter import OpenRouterConnector

register("opencode", OpenCodeConnector)
register("openrouter", OpenRouterConnector)
```

This file is the only place outside `src/llm/providers/` that
needs to change when a provider is added.

### 3.4 When metadata augmentation runs

In the CLI's `connect` and `refresh` flows — not in a separate
orchestration module. The flow:

```python
# Pseudocode in the connect command
entries = await connector.discover_models(credentials, base_url)

for source in registry.connected_metadata_sources():
    for i, entry in enumerate(entries):
        if entry.has_missing_fields():
            extra = await source.lookup_model_metadata(
                provider=provider_name, model_id=entry.id,
            )
            if extra is not None:
                entries[i] = merge_entry(entry, extra)

# Write entries to platform.yaml.
```

This is a few lines in the CLI command — no separate module. If a
second metadata source ever exists, the loop handles it
unchanged.

---

## Part 4 — Reference Implementations

### 4.1 OpenCode — the basic shape

OpenAI-compatible API, paste-a-key auth, live model list that
returns IDs only. Template for any future OpenAI-compatible
provider (most of OpenAI, OpenAI forks, third-party hosted models
behind an OpenAI shim).

```python
# src/llm/providers/opencode.py

class OpenCodeConnector:
    type_name = "opencode"

    def auth_prompt_spec(self) -> AuthPromptSpec:
        return AuthPromptSpec(
            provider_label="OpenCode",
            intro="OpenCode uses OpenAI-compatible API key auth.",
            fields=[
                PromptField(
                    name="base_url", label="Base URL", kind="url",
                    default="https://opencode.example.com/v1",
                ),
                PromptField(
                    name="api_key", label="API key", kind="secret",
                ),
            ],
        )

    async def validate_credentials(self, credentials, base_url):
        # Tiny call to /v1/models; 200 means credentials work.
        return ValidationResult(ok=True)

    async def discover_models(self, credentials, base_url):
        # GET {base_url}/models — returns IDs only, no parameters.
        return [ModelEntry(id=m["id"]) for m in models]

    async def build_llm(self, model_id, provider_config, api_key):
        return crewai.LLM(
            model=f"openai/{model_id}",
            api_key=api_key,
            base_url=provider_config.base_url,
            timeout=provider_config.timeout_seconds,
        )
```

The `kolsch connect opencode` flow walks the prompt spec, calls
`validate_credentials`, calls `discover_models`, runs the
augmentation loop if OpenRouter is connected, then writes the
provider config + secret + suggested aliases.

### 4.2 OpenRouter — the provider + metadata-source shape

Implements both `ProviderConnector` and
`MetadataSourceConnector`. OpenRouter is unusual: it is both a
usable provider AND a rich metadata catalog (200+ models across
all major providers, with context_length / pricing /
capabilities). The connector exposes both faces.

```python
# src/llm/providers/openrouter.py

class OpenRouterConnector:
    type_name = "openrouter"

    def auth_prompt_spec(self) -> AuthPromptSpec:
        return AuthPromptSpec(
            provider_label="OpenRouter",
            intro=(
                "OpenRouter uses API key authentication. The "
                "model catalog is public; the key is only needed "
                "to route calls through OpenRouter. If you "
                "connect OpenRouter, the platform will also use "
                "its catalog to look up model parameters for "
                "other providers (you can disable this in "
                "platform.yaml)."
            ),
            fields=[
                PromptField(
                    name="api_key", label="API key", kind="secret",
                ),
            ],
        )

    async def validate_credentials(self, credentials, base_url):
        # GET /api/v1/auth/key — also reports credits.
        return ValidationResult(
            ok=True, metadata={"credits_remaining": credits},
        )

    async def discover_models(self, credentials, base_url):
        # GET /api/v1/models — returns ALL OpenRouter models with
        # rich parameters (id, context_length, pricing, etc.).
        return entries

    async def build_llm(self, model_id, provider_config, api_key):
        return crewai.LLM(
            model=f"openrouter/{model_id}",
            api_key=api_key,
            base_url=provider_config.base_url,
            timeout=provider_config.timeout_seconds,
        )

    # === MetadataSourceConnector extension ===

    async def lookup_model_metadata(
        self, provider, model_id, credentials=None,
    ):
        # Build the OpenRouter ID from (provider, model_id).
        # E.g. (provider="anthropic", model_id="claude-opus-4-7")
        # → openrouter_id = "anthropic/claude-opus-4-7"
        if openrouter_id in cached_catalog:
            entry = cached_catalog[openrouter_id]
            return ModelMetadata(
                context_length=entry.context_length,
                supports_tools=entry.supports_tools,
                supports_streaming=entry.supports_streaming,
                pricing=entry.pricing,
            )
        return None
```

When subsequent `kolsch connect <provider>` calls run, they
automatically benefit from OpenRouter's metadata via the
augmentation loop (Part 3.4).

---

## Part 5 — Planned Providers (later patches)

Worked design sketches for the four named providers, scoped
enough that a future contributor can pick one up by reading this
section alongside the OpenCode/OpenRouter implementations. Each
sketch follows the connector checklist
(`src/llm/providers/README.md` §1.2).

### 5.1 Anthropic

- **Auth shape.** Paste API key (`ANTHROPIC_API_KEY`).
- **`validate_credentials`.** One-token call to `/v1/messages`
  with the smallest possible prompt; check for 200. No model-list
  endpoint to call.
- **`discover_models`.** Anthropic does not publish a model list
  API. Return an empty list. Tell the user "Anthropic does not
  expose a model list; configure models manually in
  `platform.yaml`, or connect OpenRouter for parameter info on
  Anthropic models."
- **`build_llm`.** `crewai.LLM(model=f"anthropic/{model_id}",
  api_key=key, ...)`. Standard.
- **Metadata source?** No.
- **Quirks.** Users typically know the model names from
  Anthropic's docs. A "known model names" hint list could ship in
  the connector — but it goes stale. Prefer leaving model
  discovery to OpenRouter or user knowledge.

### 5.2 OpenAI

- **Auth shape.** Paste API key (`OPENAI_API_KEY`).
- **`validate_credentials`.** `GET /v1/models` (always 200 on a
  valid key).
- **`discover_models`.** `GET /v1/models` returns IDs only. Filter
  to chat-capable models (drop embeddings, image, etc.) by ID
  prefix. Return `ModelEntry(id=...)` for each survivor.
- **`build_llm`.** `crewai.LLM(model=f"openai/{model_id}",
  api_key=key, ...)`. Standard.
- **Metadata source?** No.
- **Quirks.** Non-chat models in the list. Filter or the user sees
  irrelevant entries.

### 5.3 Bedrock

- **Auth shape.** AWS credentials, with a choice of source:
  - "Already configured" — read `AWS_PROFILE` /
    `AWS_ACCESS_KEY_ID` / etc. from environment, store none.
  - "Store in .env" — collect `AWS_ACCESS_KEY_ID`,
    `AWS_SECRET_ACCESS_KEY`, optional `AWS_SESSION_TOKEN`.
  Plus a region (`us-east-1`, etc.).
- **`validate_credentials`.** STS `GetCallerIdentity`. Returns
  account ID on success.
- **`discover_models`.** Bedrock `ListFoundationModels` for the
  region. Returns IDs, modalities, and some capability flags
  (varies by model). Map into `ModelEntry`s; leave
  `context_length` null where Bedrock doesn't report it.
- **`build_llm`.** Bedrock uses AWS sigv4, not bearer tokens.
  `crewai.LLM(model=f"bedrock/{model_id}", aws_region_name=...,
  aws_access_key_id=..., aws_secret_access_key=..., ...)`.
- **Metadata source?** No.
- **Quirks.** "Already configured" auth source means the connector
  reads credentials at `build_llm` time, not at connect time —
  the user's existing AWS setup keeps working. Bedrock model IDs
  are region-prefixed for some models, not for others; the
  connector handles both.

### 5.4 Llama (local)

- **Auth shape.** URL only, no key. Default
  `http://localhost:11434` (Ollama). Optionally detect the runner
  type (Ollama, llama.cpp server, vLLM) from response shape.
- **`validate_credentials`.** Hit the runner's status endpoint
  (e.g., Ollama's `/api/tags`, llama.cpp's `/v1/models`).
- **`discover_models`.** Runner-specific:
  - Ollama: `GET /api/tags` lists locally pulled models.
  - llama.cpp server / vLLM: `GET /v1/models` (OpenAI-compat).
  Parameter info: generally none — context length depends on how
  the model was loaded, not an intrinsic property. Return entries
  with all parameters null.
- **`build_llm`.** Llama via Ollama:
  `crewai.LLM(model=f"ollama/{model_id}", base_url=url, ...)`.
  llama.cpp / vLLM: OpenAI-compat shape.
- **Metadata source?** No.
- **Quirks.**
  - Local runners have no credentials, so `api_key_secret` in the
    config is `null`. The connector must handle this everywhere.
  - The user can run multiple local runners on different ports —
    each is a separate provider entry (e.g., `llama_ollama`,
    `llama_vllm`). `type_name` stays `llama`; the user picks the
    dictionary key in `platform.yaml`.

---

## Part 6 — Failure Modes

Catalog (Error Model §4.13):

| Code | Level | Where |
|---|---|---|
| `llm.provider_not_configured` | Fatal | `build_llm` |
| `llm.secret_missing` | Fatal | `build_llm` (chains `secret.not_found`) |
| `llm.client_init_failed` | Fatal | connector's `build_llm` |
| `config.unknown_alias` | Fatal | kernel resolver |
| `llm.connector_unknown` | Fatal | `registry.get` |
| `llm.discovery_failed` | Recoverable | connector's `discover_models` |
| `llm.validation_failed` | Fatal | CLI connect flow |

---

## Part 7 — Status

Phase 1.1 design draft 0.1. The patch shipped two reference
connectors (OpenCode + OpenRouter) and the four CLI commands.
Anthropic / OpenAI / Bedrock / Llama land as later patches each
following Part 5's sketch + the contributor guide in
`src/llm/providers/README.md`.

After this patch lands and the implementation ships, Phase 1 is
functionally complete: providers configurable via CLI, agents
declare the model they want (with alias convenience), end-to-end
execution actually works.
