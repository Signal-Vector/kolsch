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

1. **No new LLM client abstraction (with one exception).** The
   standard is `crewai.LLM`. The platform ships a small `build_llm`
   *helper*, not a parallel client hierarchy; the Agent Engine works
   with `crewai.LLM` objects directly, and tests inject the Mock LLM by
   subclassing `crewai.LLM` (or its base). The one exception is
   `claude_code`, which shells the `claude` CLI instead of going through
   litellm.
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
4. **Three connectors ship: OpenCode, OpenRouter, and Claude Code.**
   OpenCode and OpenRouter are the two OpenAI-compatible reference
   shapes; `claude_code` drives the `claude` CLI under a Claude
   subscription (no API key) and discovers its models from the CLI's
   own local cache rather than an HTTP list endpoint. The other named
   providers (Anthropic-direct, OpenAI-direct, Bedrock, Llama-local)
   remain worked planning examples (Part 5); their implementation is
   later patches.
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
    *,
    configured_providers: list[str] | None = None,
    context: LLMCallContext | None = None,
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
        parameters=spec.parameters or None,
        context=context,
    )
```

That's the whole helper. Every provider-specific concern lives in
the connector's `build_llm` method (Part 3). Adding a provider does
not modify this function. `context` — the owning session's
[`LLMCallContext`](#36-per-call-request-context-request-policy-and-user-agent-bug-144)
— is forwarded verbatim; every production caller passes one.

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
  uses the injected callable. The MinimalFramework + EchoLLMDriver
  bootstrap relies on this so tests don't need any provider config.

A future patch may unify the two paths by deprecating the direct
callable. For now, both seams are first-class.

**How tool schemas are sent (bug-138).** Each tool's arguments are
advertised as the JSON Schema of its pydantic `args_schema`, wrapped
as `{type: "function", function: {name, description, parameters}}`
(this is what the `model.request` event records). For the
OpenAI-compatible connectors (`openrouter`, `opencode`), CrewAI then
re-converts that payload before the HTTP call, and its stock
conversion forces `"strict": true` and closes every object to
`{properties: {}, additionalProperties: false}` — which would turn a
free-form map argument (`dict[str, X]`, e.g. `fetch_url.headers`,
`run_workflow.inputs`) into "empty object only" for models that
honour strict decoding. Kolsch therefore applies a **conditional
strict policy** in the client those connectors build
(`llm.providers._openai_compat`):

| Tool `parameters` | Sent as |
|---|---|
| Strict-compatible: only scalars and closed objects, every value typed, no strict-rejected keyword (`minProperties`, `patternProperties`, ...) | CrewAI's stock strict payload, unchanged (`"strict": true`) |
| Anything else (an open map, an untyped value, a rejected keyword, a field named like a schema keyword such as `title` / `default`, a recursive `$ref`) | The kolsch-built `parameters`, `"strict": false`, with `$ref`s inlined (recursive schemas verbatim) |

Of the starter tools, `browse_url`, `fetch_url` (free-form `headers` /
`cookies` maps) and `run_workflow` (free-form `inputs`) go non-strict;
every other starter tool keeps the stock strict payload.

Validation never depends on strict decoding: dispatch re-validates
every call against the real `args_schema` and returns
`tool.invalid_arguments` to the model on a mismatch. The `claude_code`
connector and the Mock LLM do not go through this conversion.

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
        parameters: Mapping[str, Any] | None = None,
        *,
        context: LLMCallContext | None = None,
    ) -> crewai.LLM:
        """Construct a configured crewai.LLM for the named
        model. `context` identifies the session the LLM serves
        (§3.6)."""

    def classify_call_error(
        self,
        exc: BaseException,
        *,
        provider_config: ProviderConfig,
        model_id: str,
    ) -> PlatformError | None:
        """Map a call-time failure of the LLM `build_llm` returned
        to a catalogued `model.*` error, or None if unrecognised.
        Pure (§3.7)."""
```

**Declared protocol change (bug-144).** The keyword-only `context`
parameter was added to every in-tree connector and test double in
one change. Connectors are registered in-tree only
(`src/llm/providers/__init__.py`), so there is no runtime signature
probing; `src/llm/providers/README.md` §2.7 is the migration note for
anyone carrying a connector of their own. `classify_call_error` is a
second declared change (bug-145, README §2.8).

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
from .claude_code import ClaudeCodeConnector
from .opencode import OpenCodeConnector
from .openrouter import OpenRouterConnector

register("claude_code", ClaudeCodeConnector)
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

**qol-091 self-heal on refresh.** `refresh` also runs
`_maybe_augment(...)` before writing entries — so a provider
connected BEFORE the metadata source (which would have found no
source to consult at connect time) picks up the metadata on the
next `refresh` invocation. `refresh` also retroactively writes the
`serves_as_metadata_source: true` marker on any provider block
whose connector is a metadata source but whose entry lacks the
flag. Both operations are idempotent.

### 3.5 Runtime cross-provider `context_length` fallback

Independent of the connect/refresh-time augmentation, the engine's
runtime lookup at `_context_length_for_spec` (`src/engine/engine.py`)
falls through to other configured providers when the caller's own
provider entry has no `context_length` value. Three tiers:

1. **Direct match** — `spec.model == entry.id` in the caller's own
   provider.
2. **Own-provider suffix match** — split each `entry.id` on `/` and
   compare only the last segment (`kimi-k2.6` matches
   `moonshotai/kimi-k2.6`), and the reverse (prefixed spec against
   short-id entry).
3. **Cross-provider fallback** — iterate every OTHER configured
   provider in `platform.yaml` insertion order, applying
   exact-then-suffix match.

`PlatformRuntime.context_length_for` delegates to the same helper so
both call paths return identical values (feat-054 documented the
runtime method as *the* API even before qol-091 wired it through
the fallback).

**Semantics on ambiguity:**

- First int-valued hit wins. Config-file insertion order is stable.
- `None`-valued candidates are skipped entirely — no signal.
- Two int candidates with **different** values → a `warning` log
  fires naming both providers + values. Two candidates in
  agreement → silent.
- `mock` provider short-circuits — its id space (`mock-model`,
  `mock-summariser`) collides too easily with real-provider
  suffixes. Direct-lookup for a mock spec still works.

The upshot: connecting OpenRouter alongside a metadata-poor
provider (chiefly `opencode`) resolves `context_length` for
opencode-served models automatically, without editing
`platform.yaml`. Without a metadata source configured, the lookup
returns `None` and compaction skips silently — see the
compaction docs for the resulting user-facing notice.

### 3.6 Per-call request context, request policy, and User-Agent (bug-144)

Some gateways need to know which conversation a request belongs to.
OpenCode Go rejects chat completions that lack a stable
`x-opencode-session` header (`400 MissingSessionID`) and asks clients
to identify themselves with their own User-Agent. Three pieces make
that provider-neutral.

**`LLMCallContext`** (`llm.protocol`) is a frozen value threaded from
the engine into every `build_llm`:

| Field | Meaning |
|---|---|
| `session_id` | The owning kolsch session. |
| `conversation_key` | The provider-facing conversation identity. **Derived, never minted.** |
| `agent_id` | The agent the call is made for, when known. |

The v1 derivation rule is `conversation_key == session_id`, applied by
`LLMCallContext.for_session(session_id)`. Consequences:

- **Stable within a session.** One context object is built per
  exchange and reused for the pre-flight build and every fallback
  attempt; every exchange in the session derives the same key.
  Compaction's summariser call (per-exchange, manual `/compact`,
  session-open eager) carries the same key.
- **Survives resume.** Resuming a session reuses its `session_id`, so
  the key — and any provider-side prompt cache keyed on it — carries
  over.
- **New on `/new`.** `/new` opens a new session, hence a new key.
- **Per workflow node.** Each workflow agent node runs in its own
  spawned session, so each gets its own key (workflow-wide affinity is
  a planned follow-up).
- **Never a secret, never invented.** It is an internal `ses_<uuid>`.
  A connector that needs a key and receives `context=None` raises
  `llm.client_init_failed`; no connector ever mints a substitute id
  (it would be state the event log can't reconstruct).
- **Not on `model.request` yet.** The key equals the envelope's
  `session_id`, which every event already carries. The ticket that
  first makes the key differ from `session_id` (e.g. workflow-wide
  affinity) adds a `conversation_key` field to `model.request` in the
  same change.

The context is also exposed read-only on the engine's
`FallbackLLMHandle.context`, so a framework that never calls
`build_llm` (the `claude_code` subprocess harness) can read the key.

**Request policy.** Each OpenAI-compatible connector declares a
`DEFAULT_REQUEST_POLICY` (`OpenAICompatRequestPolicy`), whose v1 field
`session_headers` names the headers that carry the conversation key:

| Connector | Default `session_headers` |
|---|---|
| `opencode` | `["x-opencode-session"]` |
| `openrouter` | `[]` |
| `claude_code` | not applicable (accepts and ignores `context`) |

Any provider entry can override it in `platform.yaml` without new code
(see Part 6.2). The policy holds header **names** only — values always
come from the context — so no secret can enter config.

**User-Agent.** Every OpenAI-compatible request sends
`User-Agent: kolsch/<installed version>` (from the public helper
`llm.user_agent.kolsch_user_agent()`), replacing the OpenAI SDK's
default. Agent-YAML `parameters` cannot override the session headers
or the User-Agent: header-, query- and routing-shaped keys
(`default_headers`, `extra_headers`, `headers`, `client_params`,
`organization`, `project`, `default_query`, `api_base`, plus the
routing keys) are dropped before the client is built.

### 3.7 Call-time error classification (bug-145)

`build_llm` only constructs the client. Failures happen later, when
the engine calls it. The connector that built the client is the one
that understands its errors, so it classifies them through
`classify_call_error`.

**Where it runs.** `make_build_llm_factory` binds the hook per model
spec through `llm.call_error_classifier(spec, provider_config)`. That
helper resolves the connector exactly as `build_llm` does. The LLM
callable (`engine._crewai_adapter.make_llm_callable`) then invokes the
hook on any exception from `crewai.LLM.call` that is not already a
`PlatformError`:

- It raises the returned error `from` the original.
- If the hook returns `None`, the original exception is re-raised
  unchanged.
- If the hook itself raises, the failure is logged and the original
  is re-raised.
- `PlatformError` and `asyncio.CancelledError` pass through untouched.

The engine's fallback handle, the compaction ladder and the event log
all sit behind this callable, so they all see the typed code.

**Why fallback depends on it.** `FallbackLLMHandle` advances the chain
only on a typed, Recoverable `model.*` error. Before bug-145 every
real provider failure reached it as a raw SDK exception. The
`model.response` event recorded `error: null`, and `fallback_models`
was never consulted, even for 429 or 5xx. The exception then surfaced
as `platform.internal_error`.

**What it must handle.** CrewAI re-wraps SDK exceptions before they
reach the hook:

- `ValueError("Model … not found") from NotFoundError` for a 404.
- `ConnectionError(…) from APIConnectionError` (and `APITimeoutError`).
- `LLMContextLengthExceededError(…) from <status error>` when the
  message matches CrewAI's context-limit phrases.
- `raise e from e` for every other status: a self-cycle on
  `__cause__`.

The OpenAI-compatible connectors delegate to one shared, SDK-import-free
mapper, `llm.providers._errors.classify_openai_compat_exception`. It
walks `__cause__` with a cycle guard and a 10-link cap, recognises
links by duck typing (an integer `status_code` plus a `response`) or by
qualified class name, and maps every HTTP status >= 400 per
[Error Model §4.15](error-model.md). `claude_code` returns `None`: its
LLM already raises typed `model.*` errors in its own layer.

| Connector | `classify_call_error` |
|---|---|
| `opencode` | shared OpenAI-compatible mapper (default timeout 180 s) |
| `openrouter` | shared OpenAI-compatible mapper (default timeout 30 s) |
| `claude_code` | `None` (already typed) |

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

    # bug-144: OpenCode Go routes on a per-conversation header.
    DEFAULT_REQUEST_POLICY = OpenAICompatRequestPolicy(
        session_headers=("x-opencode-session",),
    )

    async def build_llm(self, model_id, provider_config, api_key,
                        parameters=None, *, context=None):
        # CrewAI's `hosted_vllm` route via kolsch's shared builder:
        # strict tool-schema policy (bug-138), session headers +
        # kolsch User-Agent (bug-144), agent parameters filtered.
        return build_openai_compatible_llm(
            provider="hosted_vllm", model_id=model_id,
            provider_config=provider_config, api_key=api_key,
            default_timeout=180.0, parameters=parameters,
            context=context, default_policy=self.DEFAULT_REQUEST_POLICY,
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

    DEFAULT_REQUEST_POLICY = OpenAICompatRequestPolicy()  # no session header

    async def build_llm(self, model_id, provider_config, api_key,
                        parameters=None, *, context=None):
        return build_openai_compatible_llm(
            provider="openrouter", model_id=model_id,
            provider_config=provider_config, api_key=api_key,
            default_timeout=30.0, parameters=parameters,
            context=context, default_policy=self.DEFAULT_REQUEST_POLICY,
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

## Part 6 — Tuning

The connector defaults are picked to work out-of-the-box on the
two reference providers we ship. A few knobs are commonly worth
adjusting when a workload's profile differs.

### 6.1 `timeout_seconds` (per-provider, default 180 s for OpenCode)

OpenCode (and any OpenAI-compatible provider going through the same
connector template) uses a single client-side timeout for the
underlying HTTP call. It is set via:

```yaml
# platform.yaml
llm_providers:
  opencode:
    type: opencode
    base_url: https://opencode.example.com/v1
    api_key_secret: OPENCODE_API_KEY
    timeout_seconds: 180   # optional; defaults to the connector's value
```

The current default is **180 seconds**. The previous default was
30 seconds — `bug-chat-opencode-qwen-timeout-investigation.md`
documents how that value caused a 32% timeout rate on a long
multi-turn tool-using session with `qwen3.6-plus`. The replay
captured natural latency in the 30-113s range on individual
calls, so anything under ~120s will clip the tail; 180s gives a
50% headroom over the worst observed.

When to tune:

- **Drop it (e.g., 60 s)** if the model you point at is a short,
  fast model that never legitimately takes that long, and you'd
  rather fail-fast than wait — the engine's retry layer
  (`runtime.max_retries`) will pick up the failure and try again.
- **Raise it (e.g., 300 s+)** if your provider serves slow large
  models (qwen-max, claude-opus on long reasoning, etc.) and you
  observe the kolsch chat surfacing timeout errors despite the
  default. The longest acceptable value is bounded by what your
  user is willing to wait staring at a "thinking..." spinner;
  values above 300 s usually mean a better solution is to swap
  the model or split the work.

Interaction with retry layers:

- The connector timeout caps a **single HTTP attempt**. crewai/
  litellm may retry internally a small number of times — that
  amplifies the effective wall-clock budget. Reflected in
  observed kolsch latencies that exceed the connector timeout
  by 1-2×.
- The engine's `runtime.max_retries` on the agent spec retries
  the entire exchange (full message reassembly). Independent of
  the connector timeout.

### 6.2 `request_policy` (per-provider session headers)

Override a connector's default session headers (§3.6) per provider:

```yaml
# platform.yaml
llm_providers:
  my_gateway:
    type: opencode               # any OpenAI-compatible connector
    base_url: https://gateway.example.com/v1
    api_key_secret: MY_GATEWAY_API_KEY
    request_policy:
      session_headers: ["x-session-affinity"]   # replaces the default
```

- Each listed header is sent with the session's conversation key as
  its value. `[]` sends no session header; omitting `request_policy`
  (or `session_headers`) keeps the connector default.
- Names must be RFC 7230 header tokens and may not be
  `Authorization`, `Proxy-Authorization`, `Cookie`, `User-Agent`,
  `Content-Type`, `Content-Length` or `Host` (case-insensitive).
  Duplicates are dropped case-insensitively. `session_headers` is the
  only accepted key — there is deliberately no way to set a static
  header value.
- Validation is **fail-fast and global**: a malformed `request_policy`
  on any one provider raises `config.invalid` (naming the provider and
  path) when the runtime boots and in `kolsch connect` / `refresh`,
  which blocks every provider until it is fixed. The runtime, the CLI
  and tooling all build provider entries through the single factory
  `llm.provider_config_from_yaml`, so they can't disagree.

---

## Part 7 — Failure Modes

Catalog (Error Model §4.13):

| Code | Level | Where |
|---|---|---|
| `llm.provider_not_configured` | Fatal | `build_llm` |
| `llm.secret_missing` | Fatal | `build_llm` (chains `secret.not_found`) |
| `llm.client_init_failed` | Fatal | connector's `build_llm` (incl. a session-header policy built with no `LLMCallContext`) |
| `config.invalid` | Fatal | `provider_config_from_yaml` (malformed `request_policy`) |
| `config.unknown_alias` | Fatal | kernel resolver |
| `llm.connector_unknown` | Fatal | `registry.get` |
| `llm.discovery_failed` | Recoverable | connector's `discover_models` |
| `llm.validation_failed` | Fatal | CLI connect flow |

Call-time `model.*` codes come from `classify_call_error` (§3.7). Their
status mapping and levels are in Error Model §4.15.

---

## Part 8 — Status

Phase 1.1 design draft 0.1. The patch shipped two reference
connectors (OpenCode + OpenRouter) and the four CLI commands.
Anthropic / OpenAI / Bedrock / Llama land as later patches each
following Part 5's sketch + the contributor guide in
`src/llm/providers/README.md`.

After this patch lands and the implementation ships, Phase 1 is
functionally complete: providers configurable via CLI, agents
declare the model they want (with alias convenience), end-to-end
execution actually works.
