# Phase 1.1 Patch — LLM Provider Configuration

**Draft 0.2** *(replaces draft 0.1; rescoped to OpenCode + OpenRouter
with a modular connector pattern; drops the standalone `LLMClient`
abstraction in favor of `crewai.LLM` as the standard; drops the shipped
model registry in favor of opt-in OpenRouter metadata lookup.)*

> A retrospective patch closing a gap discovered after Phase 1 shipped:
> the platform's specs never specified how LLM provider configuration
> works, which means an agent can't actually run end-to-end without
> ad-hoc patches.
>
> This patch specifies: (1) the `llm_providers` section in
> `platform.yaml` and how it's populated, (2) the small `build_llm`
> helper that turns a `ModelSpec` into a `crewai.LLM`, (3) the
> `ProviderConnector` Protocol that makes adding new providers a
> single-file change, (4) the OpenCode and OpenRouter reference
> implementations, (5) the CLI commands (`connect`, `disconnect`,
> `providers`, `refresh`), and (6) the planning sketches for four
> additional providers (Anthropic, OpenAI, Bedrock, Llama-local) to
> implement in later patches.

---

## Part 0 — Why This Patch Exists

The Phase 1 specs treated **the model** as a parameter — `ModelSpec`
appears in agent configs as `{provider, model}`, the Agent Engine
"calls the model" — but never specified the layer between "agents say
they want anthropic/claude-opus-4-7" and "the platform actually calls
the model."

Missing:
- Where the API key lives.
- What base URL to use.
- How a `ModelSpec` becomes a usable LLM client.
- How a user *connects* a provider in the first place.

This patch fills the gap. The headline design decisions:

1. **No new LLM client abstraction.** The standard is whatever
   interface CrewAI's `LLM` class and the test Fake LLM both satisfy.
   The engine works with `crewai.LLM` objects directly; we don't
   invent a parallel hierarchy. The patch ships a small `build_llm`
   helper, not a factory.
2. **No shipped model registry.** Platforms shipping registries go
   stale. Instead, the platform discovers what it can from each
   provider's own API at connection time, and (optionally) augments
   that with cross-provider metadata from OpenRouter if the user has
   connected OpenRouter. If neither path yields parameters, models
   work anyway — just less optimally.
3. **Modular providers behind one Protocol.** Adding a new provider
   means writing one file in `src/llm/providers/` and registering it
   in one line. The CLI, config, and engine never grow.
4. **Scoped v1.1 implementation: OpenCode and OpenRouter only.** Two
   reference implementations covering the two main shapes. The four
   other named providers (Anthropic, OpenAI, Bedrock, Llama-local)
   are documented as worked planning examples; their actual
   implementation is later patches following the established pattern.

---

## Part 1 — The `build_llm` Helper

The bridge from `ModelSpec` to a usable LLM. Small, provider-agnostic,
returns a `crewai.LLM`.

### 1.1 Contract

```python
# src/llm/build_llm.py

async def build_llm(
    spec: ModelSpec,
    provider_config: ProviderConfig,
    secrets: SecretsProvider,
) -> crewai.LLM:
    """Construct a CrewAI LLM from a ModelSpec and provider config.

    The returned LLM is a real client (for runtime) or the Fake LLM
    fixture (in tests, which subclasses crewai.LLM or its base). The
    engine treats both identically.
    """
```

**Preconditions:**
- `spec` is canonical-form `{provider, model}` (alias expansion has
  already happened in the kernel resolver — see §2.4).
- `provider_config` is the entry from `platform.yaml`'s
  `llm_providers[spec.provider]`.
- `secrets` provides the resolved API key (if any).

**Postconditions:**
- Returns a configured `crewai.LLM` ready to call.
- The provider's connector (Part 3) decides how to construct it — for
  OpenAI-compatible providers, it's a straightforward `crewai.LLM(...)`
  call; for providers needing special construction (e.g., Bedrock with
  AWS sigv4), the connector knows what to do.

**Raises:**
- `llm.provider_not_configured` (Fatal) — provider absent from
  `llm_providers`.
- `llm.secret_missing` (Fatal) — configured `api_key_secret` not
  resolvable, with `secret.not_found` in the cause chain.
- `llm.client_init_failed` (Fatal) — provider's connector raised
  during construction.

### 1.2 How `build_llm` dispatches

The helper does almost nothing itself; it consults the connector
registry (Part 4):

```python
async def build_llm(spec, provider_config, secrets):
    connector = registry.get(provider_config.type)  # ProviderConnector
    api_key = (
        await secrets.resolve(provider_config.api_key_secret)
        if provider_config.api_key_secret else None
    )
    return await connector.build_llm(spec.model, provider_config, api_key)
```

That's the entire function. Every provider-specific concern lives in
the connector's `build_llm` method. Adding a provider does not
modify this helper.

---

## Part 2 — Configuration in `platform.yaml`

### 2.1 The `llm_providers` section

Populated by `pi-platform connect <provider>` (Part 8) and editable by
hand. One entry per connected provider.

```yaml
llm_providers:
  opencode:
    type: opencode
    api_key_secret: OPENCODE_API_KEY
    base_url: https://opencode.example.com/v1
    timeout_seconds: 60
    discovered_at: "2026-05-29T14:23:01Z"
    models:
      - id: gpt-oss-120b
        context_length: 131072         # filled from openrouter (see Part 6)
        supports_tools: true
        supports_streaming: true
      - id: qwen2.5-coder-32b
        context_length: null           # unknown — opencode doesn't report; openrouter has no entry
        supports_tools: null
        supports_streaming: null

  openrouter:
    type: openrouter
    api_key_secret: OPENROUTER_API_KEY
    base_url: https://openrouter.ai/api/v1
    timeout_seconds: 60
    serves_as_metadata_source: true    # the capability flag — see Part 5
    discovered_at: "2026-05-29T14:20:11Z"
    models:                             # the live OpenRouter catalog at connect time
      - id: anthropic/claude-opus-4-7
        context_length: 200000
        supports_tools: true
        # ... openrouter returns full parameters
```

### 2.2 Fields

Common to all connected providers:

| Field | Type | Required | Notes |
|---|---|---|---|
| `type` | string | yes | Identifies the connector (must be a key in the registry — see Part 4). Distinct from the dictionary key, which is the user-facing provider name. Usually they match. |
| `api_key_secret` | string \| null | yes | Name to look up in Secrets. `null` for providers that need no key (Llama-local). |
| `base_url` | string | no | Override or required (Llama-local). |
| `timeout_seconds` | int | no | Per-call timeout. |
| `discovered_at` | ISO timestamp | yes | When `connect` or `refresh` last ran. |
| `models` | list | yes | Discovered model entries (see §2.3). Empty list permitted. |

Connector-specific extensions are allowed — e.g., the OpenRouter
connector adds `serves_as_metadata_source: bool`; a future Bedrock
connector would add `region` and `auth_source`. Each connector
documents its own extra fields.

### 2.3 Model entries

```yaml
- id: gpt-oss-120b
  context_length: 131072            # or null if unknown
  supports_tools: true              # or null
  supports_streaming: true          # or null
  pricing:                          # optional; OpenRouter provides it
    prompt_per_million: 0.50
    completion_per_million: 1.50
```

All parameter fields are nullable. A `null` means "unknown" — the
provider didn't report it AND no metadata source was available to
augment it. The platform runs models with null parameters; it just
can't pre-flight context-overflow checks, cost estimates, etc.

### 2.4 The `ModelSpec` resolution path

`ModelSpec.model` permits three input shapes (small permissiveness
added in this patch):

- Object: `{provider: "opencode", model: "gpt-oss-120b"}`
- Compact string: `"opencode/gpt-oss-120b"`
- Alias string: `"smart"` (defined in `model_aliases` — see §2.5)

The kernel resolver normalizes all three to the canonical object form
during Layer 1 validation. Downstream code sees only `{provider,
model}`.

### 2.5 Model aliases

```yaml
model_aliases:
  smart: opencode/gpt-oss-120b
  cheap: opencode/qwen2.5-coder-32b
```

Aliases are deployment-level (in `platform.yaml`), not per-agent. A
bundle's agent can declare `model: smart` and each deployment defines
what `smart` means. One level of indirection only — aliases don't
chain. Unknown aliases raise `config.unknown_alias` at validation.

---

## Part 3 — The `ProviderConnector` Protocol

The shape every provider implements. Lives in `src/llm/protocol.py`.

### 3.1 The base Protocol

```python
# src/llm/protocol.py

class ProviderConnector(Protocol):
    """One per supported provider. Implementations live in src/llm/providers/."""

    type_name: ClassVar[str]
    """The string used in platform.yaml's `type` field and in the registry."""

    def auth_prompt_spec(self) -> AuthPromptSpec:
        """Declarative description of what to ask the user during `connect`.

        The CLI walks this spec and prompts accordingly. No interactive
        code lives in the connector itself — the connector only describes.
        """

    async def validate_credentials(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> ValidationResult:
        """Verify the credentials work. Typically a tiny live API call.

        Returns ValidationResult.ok with optional metadata (e.g., remaining
        credits) or ValidationResult.failure with a human-readable reason.
        """

    async def discover_models(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> list[ModelEntry]:
        """Return the models available from this provider.

        Each ModelEntry carries whatever parameters the provider exposes
        natively. Unknown parameters are `None`. The platform may augment
        these via a MetadataSourceConnector (§3.3) before writing them
        to platform.yaml.
        """

    async def build_llm(
        self,
        model_id: str,
        provider_config: ProviderConfig,
        api_key: str | None,
    ) -> crewai.LLM:
        """Construct a configured crewai.LLM for the named model.

        For OpenAI-compatible providers this is essentially one
        crewai.LLM(...) call. For providers needing special client
        construction (Bedrock), it can be more involved.
        """
```

### 3.2 Supporting types

```python
@dataclass(frozen=True)
class AuthPromptSpec:
    """Declarative auth prompt — the CLI walks this to interact with user."""
    provider_label: str           # "OpenCode", "OpenRouter", ...
    intro: str                    # "OpenCode uses OpenAI-compatible API key auth."
    fields: list[PromptField]


@dataclass(frozen=True)
class PromptField:
    name: str                     # "api_key", "base_url", ...
    label: str                    # human-readable
    kind: Literal["secret", "url", "string", "choice"]
    default: str | None = None
    choices: list[str] | None = None   # for kind="choice"
    required: bool = True


@dataclass(frozen=True)
class Credentials:
    """Result of prompting. Opaque to the platform; passed back to the connector."""
    values: dict[str, str]        # {name: value}, may include "api_key"


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str | None = None     # populated when ok is False
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
```

### 3.3 The `MetadataSourceConnector` extension

A second Protocol, layered on top of `ProviderConnector`, for
connectors that can also serve as a cross-provider metadata source.
Today only OpenRouter implements it.

```python
class MetadataSourceConnector(ProviderConnector, Protocol):
    """A connector that can look up model metadata for OTHER providers' models."""

    async def lookup_model_metadata(
        self,
        provider: str,
        model_id: str,
        credentials: Credentials | None = None,
    ) -> ModelMetadata | None:
        """Try to resolve metadata for a model identified by (provider, model_id).

        Returns the metadata if known, None if the source has no entry.
        Implementations are responsible for translating the platform's
        (provider, model_id) tuple into whatever identifier they use
        internally — e.g., OpenRouter prefixes Anthropic models with
        "anthropic/" while Anthropic itself does not.
        """


@dataclass(frozen=True)
class ModelMetadata:
    """The same shape as ModelEntry's parameter fields, but without `id`."""
    context_length: int | None = None
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    pricing: ModelPricing | None = None
```

Connectors that do not serve as metadata sources simply implement
`ProviderConnector`, not `MetadataSourceConnector`. They never have to
think about the metadata story.

### 3.4 When metadata augmentation runs

In the CLI's `connect` and `refresh` flows — not in a separate
orchestration module. The flow:

```python
# Pseudocode in the connect command
entries = await connector.discover_models(credentials, base_url)

# Optional augmentation: only if a metadata source is connected.
for source in registry.connected_metadata_sources():
    for i, entry in enumerate(entries):
        if entry.has_missing_fields():
            extra = await source.lookup_model_metadata(
                provider=provider_name, model_id=entry.id
            )
            if extra is not None:
                entries[i] = merge_entry(entry, extra)

# Write entries to platform.yaml.
```

This is a few lines in the CLI command — no separate module. If a
second metadata source ever exists, the loop handles it; for now it
just iterates over the one (OpenRouter, when connected) or zero
metadata sources available.

---

## Part 4 — The Connector Registry

One map. One place to register. Lives in `src/llm/registry.py`.

```python
# src/llm/registry.py

_REGISTRY: dict[str, type[ProviderConnector]] = {}

def register(type_name: str, connector_cls: type[ProviderConnector]) -> None:
    """Register a connector class under its type_name."""
    if type_name in _REGISTRY:
        raise PlatformError(...)  # duplicate registration
    _REGISTRY[type_name] = connector_cls

def get(type_name: str) -> ProviderConnector:
    """Return a connector instance for the given type. Raises if unknown."""
    if type_name not in _REGISTRY:
        raise PlatformError(...)  # unknown provider type
    return _REGISTRY[type_name]()

def known_types() -> list[str]:
    """All registered connector types."""
    return sorted(_REGISTRY.keys())

def connected_metadata_sources() -> list[MetadataSourceConnector]:
    """Iterate connected providers whose connectors implement MetadataSourceConnector."""
    # Inspects platform.yaml's current llm_providers and returns instances
    # of any that (a) are connected and (b) implement MetadataSourceConnector.
```

Registration is explicit, in `src/llm/providers/__init__.py`:

```python
# src/llm/providers/__init__.py

from src.llm.registry import register
from .opencode import OpenCodeConnector
from .openrouter import OpenRouterConnector

register("opencode", OpenCodeConnector)
register("openrouter", OpenRouterConnector)

# Future providers add a line here:
# from .anthropic import AnthropicConnector
# register("anthropic", AnthropicConnector)
```

This file is the only place outside `src/llm/providers/` that needs to
change when a provider is added.

---

## Part 5 — Reference Implementation: OpenCode

The basic shape. OpenAI-compatible, paste-an-API-key, live model list
that returns IDs only.

### 5.1 The connector

```python
# src/llm/providers/opencode.py

class OpenCodeConnector:
    """Reference implementation of ProviderConnector.

    OpenCode is OpenAI-compatible: same client shape, same endpoints,
    same auth (Bearer token). The only twists are:
      - The base URL is configurable (no canonical default).
      - The /v1/models endpoint returns IDs but no parameters.

    This connector is also the template for any other OpenAI-compatible
    provider — OpenAI itself, OpenCode forks, third-party hosted models
    behind an OpenAI shim. Most will be near-copies with different
    auth_prompt_spec values.
    """

    type_name = "opencode"

    def auth_prompt_spec(self) -> AuthPromptSpec:
        return AuthPromptSpec(
            provider_label="OpenCode",
            intro="OpenCode uses OpenAI-compatible API key authentication.",
            fields=[
                PromptField(
                    name="base_url",
                    label="Base URL",
                    kind="url",
                    default="https://opencode.example.com/v1",
                    required=True,
                ),
                PromptField(
                    name="api_key",
                    label="API key",
                    kind="secret",
                    required=True,
                ),
            ],
        )

    async def validate_credentials(self, credentials, base_url):
        # Make a tiny call to /v1/models. If it returns 200, credentials work.
        ...
        return ValidationResult(ok=True)

    async def discover_models(self, credentials, base_url):
        # GET {base_url}/models with the API key.
        # OpenCode returns {"data": [{"id": "gpt-oss-120b", ...}, ...]}.
        # Each entry yields a ModelEntry with id only; all parameter fields None.
        ...
        return [ModelEntry(id=m["id"]) for m in models]

    async def build_llm(self, model_id, provider_config, api_key):
        # Standard CrewAI LLM construction.
        return crewai.LLM(
            model=f"openai/{model_id}",     # litellm prefixes OpenAI-compat
            api_key=api_key,
            base_url=provider_config.base_url,
            timeout=provider_config.timeout_seconds,
        )
```

### 5.2 The `connect opencode` user flow

```
$ pi-platform connect opencode
OpenCode uses OpenAI-compatible API key authentication.

Base URL [https://opencode.example.com/v1]:
Enter API key: ****************************

Validating credentials...
✓ Reached /v1/models — credentials valid.

Discovering models...
✓ Found 4 models: gpt-oss-120b, qwen2.5-coder-32b, deepseek-coder-v2,
                  llama-3.3-70b

Model parameters: not available from OpenCode.
   (Run `pi-platform connect openrouter` to enable cross-provider
   metadata lookup, or proceed without parameter info — the platform
   will still work, just less optimally.)

✓ Wrote provider config to platform.yaml
✓ Wrote OPENCODE_API_KEY to .env

Suggest aliases?
  smart  → opencode/gpt-oss-120b
  cheap  → opencode/qwen2.5-coder-32b
Add suggested aliases? [Y/n]
```

If OpenRouter is already connected when this runs:

```
Discovering models...
✓ Found 4 models from OpenCode.
✓ Augmenting with OpenRouter metadata...
  ✓ gpt-oss-120b: context_length=131072, supports_tools=true
  ✓ qwen2.5-coder-32b: context_length=32768, supports_tools=true
  - deepseek-coder-v2: no entry in OpenRouter
  - llama-3.3-70b: no entry in OpenRouter
```

---

## Part 6 — Reference Implementation: OpenRouter

The provider + metadata source shape. Implements both
`ProviderConnector` and `MetadataSourceConnector`.

### 6.1 The connector

```python
# src/llm/providers/openrouter.py

class OpenRouterConnector:
    """Reference implementation of MetadataSourceConnector.

    OpenRouter is unusual: it is both a usable provider (you can call
    models through it) AND a rich metadata source (its public catalog
    has parameters for hundreds of models across all major providers).

    The connector exposes both faces. When connected, it:
      1. Can be used as a normal provider (model: openrouter/anthropic/claude-opus-4-7).
      2. Serves as the metadata source for OTHER providers' models, if
         the user has set serves_as_metadata_source: true (default true
         when connected).

    OpenRouter's /api/v1/models endpoint is public — no API key required
    to query the model list. The connector uses anonymous access for
    metadata lookup, and the user's API key only for actual routing.
    """

    type_name = "openrouter"

    def auth_prompt_spec(self) -> AuthPromptSpec:
        return AuthPromptSpec(
            provider_label="OpenRouter",
            intro=(
                "OpenRouter uses API key authentication. The model catalog "
                "is public; the key is only needed to route calls through "
                "OpenRouter. If you connect OpenRouter, the platform will "
                "also use its catalog to look up model parameters for other "
                "providers (you can disable this in platform.yaml)."
            ),
            fields=[
                PromptField(
                    name="api_key",
                    label="API key",
                    kind="secret",
                    required=True,
                ),
            ],
        )

    async def validate_credentials(self, credentials, base_url):
        # GET /api/v1/auth/key to check credit balance and key validity.
        ...
        return ValidationResult(
            ok=True,
            metadata={"credits_remaining": credits},
        )

    async def discover_models(self, credentials, base_url):
        # GET /api/v1/models (public — no key required, but use it
        # for higher rate limits).
        # Returns rich data: id, context_length, pricing, supported_parameters.
        # Returns ALL the models OpenRouter routes (200+) as ModelEntries
        # with all parameter fields populated.
        ...
        return entries

    async def build_llm(self, model_id, provider_config, api_key):
        # OpenRouter is OpenAI-compatible at the wire level.
        return crewai.LLM(
            model=f"openrouter/{model_id}",
            api_key=api_key,
            base_url=provider_config.base_url,
            timeout=provider_config.timeout_seconds,
        )

    # === MetadataSourceConnector extension ===

    async def lookup_model_metadata(self, provider, model_id, credentials=None):
        # Build the OpenRouter model ID from (provider, model_id).
        # E.g., (provider="anthropic", model_id="claude-opus-4-7")
        #   → openrouter_id = "anthropic/claude-opus-4-7"
        # OpenCode and other OpenAI-compatible providers don't have a
        # canonical mapping; the connector tries a few plausible IDs and
        # returns None if none match.
        ...
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

### 6.2 The `connect openrouter` user flow

```
$ pi-platform connect openrouter
OpenRouter uses API key authentication. The model catalog is public;
the key is only needed to route calls through OpenRouter. If you
connect OpenRouter, the platform will also use its catalog to look up
model parameters for other providers (you can disable this in
platform.yaml).

Enter API key: ****************************

Validating credentials...
✓ Credits remaining: 1,247

Discovering models...
✓ Fetched 218 models from OpenRouter catalog.

Enable cross-provider metadata lookup? [Y/n]
✓ Set serves_as_metadata_source: true in platform.yaml.

✓ Wrote provider config to platform.yaml
✓ Wrote OPENROUTER_API_KEY to .env

Suggest aliases?
  opus    → openrouter/anthropic/claude-opus-4-7
  haiku   → openrouter/anthropic/claude-haiku-4-5
  gpt4o   → openrouter/openai/gpt-4o
Add suggested aliases? [Y/n]
```

When subsequent `connect <provider>` calls run, they automatically
benefit from OpenRouter's metadata (per the augmentation loop in §3.4).

---

## Part 7 — Adding a New Provider: The Pattern

This section is the in-tree `src/llm/providers/README.md` content
(written so a contributor opening that directory has a checklist).

### 7.1 Steps

1. **Create `src/llm/providers/<name>.py`.** Implement
   `ProviderConnector` (and `MetadataSourceConnector` if relevant).
2. **Register it.** Add one line to `src/llm/providers/__init__.py`:
   ```python
   from .anthropic import AnthropicConnector
   register("anthropic", AnthropicConnector)
   ```
3. **Update the connector tests.** Add a test module under
   `tests/component/llm/providers/` mirroring the file you wrote.
   Follow `tests/component/llm/providers/test_opencode.py` as the
   template — same structure, validate_credentials mocked, discover_models
   against a recorded fixture response, build_llm asserts the
   `crewai.LLM` is constructed with the expected fields.
4. **Update integration tests if the provider has unusual auth or
   client construction.** Most providers don't need this — the engine
   doesn't care which provider it's talking to.
5. **Document.** Add a section to `docs/explanation/llm-providers.md`
   covering the provider's auth shape, discovery shape, and any quirks.

That is the full surface. The CLI commands (`connect`, `disconnect`,
`providers`, `refresh`) pick the new provider up automatically because
they iterate the registry; the config schema accepts it because
`llm_providers` is open-shaped; the engine doesn't know it exists
because the engine works with `crewai.LLM` objects, not connectors.

### 7.2 Checklist

When adding a provider, the connector must answer these questions —
each maps to a Protocol method:

- **What does the user type to connect?** → `auth_prompt_spec()`
- **How do we know the credentials work?** → `validate_credentials()`
- **What models are available?** → `discover_models()`
- **How do we construct a `crewai.LLM` for this provider's models?** →
  `build_llm()`
- **(If applicable)** Can we look up other providers' metadata? →
  implement `MetadataSourceConnector`.

If the answer to any of these is "I don't know yet," the provider
isn't ready — document the open question in the connector module's
docstring and either don't ship it or ship it with the open question
visible.

---

## Part 8 — CLI Commands

The CLI surface for managing LLM providers. Implemented in `src/cli/`
as part of v1.1.

### 8.1 `pi-platform connect <provider>`

Add a provider to the config. The provider name must be a key in the
registry (`pi-platform connect opencode`, `pi-platform connect
openrouter`).

Flow:
1. Look up the connector via `registry.get(<provider>)`.
2. Read `connector.auth_prompt_spec()`; prompt the user for each field.
3. Call `connector.validate_credentials(...)`; abort on failure.
4. Call `connector.discover_models(...)`.
5. If any `MetadataSourceConnector` is currently connected, loop the
   augmentation per §3.4.
6. Write `api_key` (if present) to `.env`. Write provider config and
   models to `platform.yaml`. Both files are created if absent.
7. Offer suggested aliases (if the connector provides any); add on
   user confirmation.

Failures at any step leave both `.env` and `platform.yaml` unchanged.

### 8.2 `pi-platform disconnect <provider>`

Remove a provider. Confirms with the user. Removes the section from
`llm_providers` and the secret from `.env`. Does not remove aliases
that referenced this provider — those become broken aliases the user
sees on the next agent run (`config.unknown_alias` errors point at the
specific alias and provider).

### 8.3 `pi-platform providers`

List configured providers, status, model counts, and last-refresh time.

```
PROVIDER     TYPE          MODELS    METADATA SOURCE   LAST REFRESH
opencode     opencode      4 (live)  yes (openrouter)  2 minutes ago
openrouter   openrouter    218       —                 5 minutes ago
```

### 8.4 `pi-platform refresh [<provider>]`

Re-run `discover_models` for one (`pi-platform refresh opencode`) or
all (`pi-platform refresh`) connected providers. Re-augments with
metadata sources if available. Updates `discovered_at` and `models` in
`platform.yaml`.

### 8.5 Unimplemented providers

If a user runs `pi-platform connect anthropic` (or any of the other
four planned providers), the CLI returns:

```
The 'anthropic' provider is planned for a future patch. See
docs/explanation/llm-providers.md for the design sketch. If you'd
like to contribute the implementation, see
src/llm/providers/README.md for the pattern.
```

The CLI does not silently fail or hang.

---

## Part 9 — Planned Providers (Implement Later)

Worked design sketches for the four named providers, scoped enough
that a future contributor can pick one up by reading this section
alongside the OpenCode/OpenRouter implementations. Each sketch
follows the connector checklist (§7.2).

### 9.1 Anthropic

- **Auth shape.** Paste API key (`ANTHROPIC_API_KEY`).
- **`validate_credentials`.** Make a one-token call to `/v1/messages`
  with the smallest possible prompt; check for 200. Don't list models
  endpoint — there isn't one.
- **`discover_models`.** Anthropic does not publish a model list API.
  Return an empty list. Tell the user "Anthropic does not expose a
  model list; configure models manually in platform.yaml, or connect
  OpenRouter for parameter info on Anthropic models."
- **`build_llm`.** `crewai.LLM(model=f"anthropic/{model_id}",
  api_key=key, ...)`. Standard.
- **Metadata source?** No.
- **Quirks.** Users typically know the model names from Anthropic's
  docs. The connector could *optionally* ship a "known model names"
  hint list (not parameter values, just IDs the user can copy) — but
  this is the kind of thing that goes stale. Prefer leaving it to
  OpenRouter or user knowledge.

### 9.2 OpenAI

- **Auth shape.** Paste API key (`OPENAI_API_KEY`).
- **`validate_credentials`.** `GET /v1/models` (always 200 on a valid
  key) or `POST /v1/chat/completions` with a one-token request.
- **`discover_models`.** `GET /v1/models` returns IDs only — no
  parameters. Return `ModelEntry(id=...)` for each chat-capable model.
  Filter out non-chat models (embeddings, image, etc.) by ID prefix.
- **`build_llm`.** `crewai.LLM(model=f"openai/{model_id}", api_key=key,
  ...)`. Standard.
- **Metadata source?** No.
- **Quirks.** OpenAI's model list includes many non-chat models; the
  connector should filter to chat-capable ones to avoid surfacing
  irrelevant entries.

### 9.3 Bedrock

- **Auth shape.** AWS credentials, with a choice of source:
  - "Already configured" — read `AWS_PROFILE` / `AWS_ACCESS_KEY_ID` /
    etc. from the environment, store none of them.
  - "Store in .env" — collect `AWS_ACCESS_KEY_ID`,
    `AWS_SECRET_ACCESS_KEY`, optional `AWS_SESSION_TOKEN`.
  Plus a region (`us-east-1`, etc.).
- **`validate_credentials`.** Call STS `GetCallerIdentity` with the
  resolved credentials. Returns the account ID on success.
- **`discover_models`.** Call Bedrock `ListFoundationModels` for the
  region. Returns IDs, modalities, and *some* capability flags
  (varies by model). Map into `ModelEntry`s; leave `context_length`
  null for entries where Bedrock doesn't report it.
- **`build_llm`.** Bedrock uses AWS sigv4, not bearer tokens —
  CrewAI/litellm supports this via specific construction. Pattern:
  `crewai.LLM(model=f"bedrock/{model_id}", aws_region_name=region,
  aws_access_key_id=..., aws_secret_access_key=..., ...)`.
- **Metadata source?** No.
- **Quirks.**
  - The "already configured" auth source means the connector reads
    credentials from the environment at `build_llm` time, not at
    connect time — so the user's existing AWS setup keeps working.
  - Bedrock model IDs are region-prefixed for some models, not for
    others. The connector must handle both cases.

### 9.4 Llama (local)

- **Auth shape.** URL only, no key. Default
  `http://localhost:11434` (Ollama). Optionally detect the runner
  type (Ollama, llama.cpp server, vLLM) from the URL behavior.
- **`validate_credentials`.** Hit the runner's status endpoint (e.g.,
  Ollama's `/api/tags` or llama.cpp's `/v1/models`). Identify the
  runner from response shape.
- **`discover_models`.** Runner-specific:
  - Ollama: `GET /api/tags` lists locally pulled models.
  - llama.cpp server: `GET /v1/models` (OpenAI-compat) lists loaded
    models.
  - vLLM: same as llama.cpp.
  Parameter info: generally none — context length depends on how
  the model was loaded (`num_ctx` for Ollama), not an intrinsic
  property. Return entries with all parameters null.
- **`build_llm`.** Llama via Ollama: `crewai.LLM(model=f"ollama/{model_id}",
  base_url=url, ...)`. Llama via llama.cpp: OpenAI-compat shape.
- **Metadata source?** No.
- **Quirks.**
  - Local runners don't have credentials, so `api_key_secret` in the
    config is `null`. The connector must handle this cleanly
    everywhere.
  - The user can run multiple local runners on different ports —
    each is a separate provider entry (e.g., `llama_ollama`,
    `llama_vllm`). The connector's `type_name` remains `llama` but
    the user picks the dictionary key in `platform.yaml`.

---

## Part 10 — Failure Modes

New error codes in the catalog. Adds a new §4.13 "LLM Configuration"
to `docs/explanation/error-model.md`.

| Code | Level | Detail | Where (typical) |
|---|---|---|---|
| `llm.provider_not_configured` | Fatal | `{provider, configured_providers}` | `build_llm` |
| `llm.secret_missing` | Fatal | `{provider, secret_name}` | `build_llm` |
| `llm.client_init_failed` | Fatal | `{provider, model, reason}` | connector's `build_llm` |
| `config.unknown_alias` | Fatal | `{alias, available_aliases}` | kernel resolver normalization |
| `llm.connector_unknown` | Fatal | `{type, registered_types}` | `registry.get` |
| `llm.discovery_failed` | Recoverable | `{provider, reason}` | connector's `discover_models` |
| `llm.validation_failed` | Fatal | `{provider, reason}` | CLI connect flow |

Pre-flight changes to the Agent Engine (§2.1, after step 5 memory
bind, before step 6 config.set):

> **5.5. Acquire LLM client.** Call `build_llm(config.model,
> provider_config, secrets)`. Hold the client for the exchange's
> lifetime. Pass it to the Framework via the Framework Protocol. On
> any `llm.*` Fatal, emit `thread.exchange_failed` and raise.

---

## Part 11 — Required Spec Revisions

Mechanical edits to existing docs. Apply as a single revision pass.

### LLM-R1. `docs/reference/schemas.md` §A

Update `ModelSpec` definition to permit three input shapes (object,
`provider/model` string, alias string), normalized to canonical
object form in Layer 1.

### LLM-R2. `docs/explanation/asset-management-contract.md` §3.2

Add `llm_providers` and `model_aliases` sections to the `platform.yaml`
schema per Part 2. Embedding providers section dropped (per scope
decision: no consumer in v1, skip the inert schema).

### LLM-R3. `docs/explanation/agent-engine-contract.md` §2.1

Insert pre-flight step 5.5 "Acquire LLM client" per §10. Renumber
subsequent steps.

### LLM-R4. `docs/explanation/agent-engine-contract.md` §1

Add the `build_llm` helper (and the connector registry it consults)
to the engine's listed dependencies.

### LLM-R5. `docs/reference/module-api-reference.md`

Add Part 11.5 "LLM Provider Configuration" covering: `build_llm`
helper, `ProviderConnector` Protocol, `MetadataSourceConnector`
Protocol, the registry. Update the dependency matrix (Part 12) to add
an `LLM` column showing the Agent Engine calls `build_llm`.

### LLM-R6. `docs/explanation/error-model.md` Part 4

Add §4.13 "LLM Configuration" with the seven codes from Part 10.

### LLM-R7. `docs/process/development-testing-handbook.md` §10

Add success criteria §10.12 "LLM Provider Configuration":
- The registry rejects duplicate registration.
- `build_llm` resolves alias → canonical → connector → `crewai.LLM`.
- A connected `MetadataSourceConnector` augments other providers'
  `discover_models` results during the connect flow.
- Provider connectors register independently; adding a connector
  requires no changes outside `src/llm/providers/`.
- API key values never appear in logs, events, or `platform.yaml`
  (only secret names do).

### LLM-R8. `docs/process/test-catalog.md`

Add Part 9.5 "LLM Provider Configuration (LLM)" with cases:
- TC-LLM-01: alias resolves to canonical (`opus` →
  `anthropic/claude-opus-4-7`).
- TC-LLM-02: unknown alias raises `config.unknown_alias`.
- TC-LLM-03: unconfigured provider raises `llm.provider_not_configured`
  from `build_llm`.
- TC-LLM-04: missing API key raises `llm.secret_missing` with
  `secret.not_found` in the cause chain.
- TC-LLM-05: OpenCodeConnector's `discover_models` returns expected
  shape from a recorded fixture response.
- TC-LLM-06: OpenRouterConnector implements both Protocols
  (isinstance check + functional test of `lookup_model_metadata`).
- TC-LLM-07: connect flow augments OpenCode's discovery with
  OpenRouter metadata when OpenRouter is connected.
- TC-LLM-08: connect flow runs cleanly without OpenRouter (no
  augmentation, null parameter fields).
- TC-LLM-09: registry rejects duplicate `register` calls.
- TC-LLM-10: API key value does not appear in `platform.yaml` or any
  log/event after `connect`.

### LLM-R9. `docs/explanation/v1-scope.md` §2.4

Add the four CLI commands (`connect`, `disconnect`, `providers`,
`refresh`) to the v1 CLI surface. Note that v1.1 implements them for
`opencode` and `openrouter`; other providers are planned.

### LLM-R10. `docs/explanation/v1-scope.md` §2.5

Add to deployment: "Default install requires no LLM provider — agents
can be defined and the CLI runs, but actually executing requires
running `pi-platform connect <provider>` first."

### LLM-R11. `AGENTS.md`

Add to "Hard rules": "LLM provider config lives in `platform.yaml`'s
`llm_providers` section, populated via `pi-platform connect`. Never
embed API keys in agent configs or in code paths other than the
connector's `build_llm` method."

### LLM-R12. New doc: `docs/explanation/llm-providers.md`

New explanation-type doc covering: the architectural choice of "no
separate LLM client abstraction"; the `ProviderConnector` Protocol
and why it's a Protocol; the metadata-source pattern and why
OpenRouter is treated specially; the planning sketches from Part 9.

### LLM-R13. New doc: `src/llm/providers/README.md`

The in-tree contributor guide for adding providers — content per
Part 7.

---

## Part 12 — Explicitly Deferred

Same list as draft 0.1, unchanged:

1. **Cost / token quota enforcement.** Records via `usage` field;
   enforcement is Phase 3.
2. **Observability / OpenTelemetry / Prometheus.** v1 has structured
   logs in `logs/`; richer observability is Phase 3.
3. **Completion caching.** Useful for development; interacts with
   replay determinism. Deferred until a clear v1 use case demands it.
4. **Provider-side rate-limit awareness.** v1 relies on provider 429
   + retry. Proactive backoff is a future patch.
5. **Multi-region / failover providers.** Phase 3 hosted concern.
6. **Per-call model overrides via `ExecutionContext.trigger`.** Out
   of scope.
7. **Embedding providers section in platform.yaml.** No v1 consumer
   (semantic memory is Phase 2). Specify when the consumer arrives.

---

## Part 13 — Open Items

1. **OpenRouter's anonymous catalog query for metadata.** The OpenRouter
   `/api/v1/models` endpoint is public, but OpenRouter rate-limits
   anonymous access more aggressively than authenticated. When the
   user has connected OpenRouter, the lookup uses their key. When the
   user has NOT connected OpenRouter, we don't query at all — the
   metadata-source pattern is gated by connection status. Confirm
   this is the right framing; we could alternatively allow anonymous
   metadata lookup for users who haven't connected OpenRouter (free
   functionality but added rate-limit and third-party-call concerns).

2. **Identifier mapping in `lookup_model_metadata`.** OpenRouter
   prefixes Anthropic models with `anthropic/`; Anthropic's API does
   not. The connector handles this translation, but the mapping is
   per-target-provider and adds a small ongoing maintenance surface
   inside OpenRouterConnector. Acceptable; flagged.

3. **CrewAI version pinning.** This patch assumes CrewAI's `LLM`
   class is the standard. The exact CrewAI version pinned in Step 12b
   determines the exact `LLM` constructor signature; the `build_llm`
   helper and connectors must match. A CrewAI upgrade is a coordinated
   change touching this code.

4. **Fake LLM and the registry.** Tests inject the Fake LLM by
   bypassing `build_llm` and constructing it directly (per the test
   strategy in the handbook). The registry is not involved in test
   wiring. This works but means the test path doesn't exercise
   `build_llm` — a small gap covered by TC-LLM-* component tests.

5. **Aliases referencing providers not yet connected.** A
   `platform.yaml` with `aliases: {smart: anthropic/claude-opus-4-7}`
   but no Anthropic in `llm_providers` is malformed. Validation should
   flag this at startup as a warning (not fatal — the alias is unused
   if no agent references it) and as fatal at agent-run time if used.

---

## Part 14 — Status

This is **Phase 1.1 retroactive patch draft 0.2**, replacing draft 0.1.
The patch:

- Closes the blocking gap (agents couldn't actually run without LLM
  provider config).
- Establishes the modular connector pattern (`ProviderConnector` +
  `MetadataSourceConnector`) so future provider additions are
  single-file changes.
- Ships **OpenCode and OpenRouter** as the two reference
  implementations covering the basic and metadata-source shapes.
- Documents the other four named providers (Anthropic, OpenAI,
  Bedrock, Llama-local) as planning sketches ready to implement in
  later patches following the established pattern.

**Apply in two passes:**

1. **Revisions.** LLM-R1 through LLM-R13 mechanically edit existing
   docs and create the two new docs. Same pattern as audit-revisions
   and modular-storage-revisions before.
2. **Implementation.** Add `src/llm/` with the helper, Protocols,
   registry, and two connector implementations; add the four CLI
   commands; add TC-LLM-* tests. A single focused developer-agent
   branch, reviewed by a reviewer-agent against the success criteria
   in §10.12 and the test cases in Part 9.5.

After this patch lands and the implementation ships, Phase 1 is
functionally complete: the platform can configure providers via CLI,
agents declare the model they want (with alias convenience), and
end-to-end execution actually works.
