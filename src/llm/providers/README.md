# `src/llm/providers/` — Contributor Guide

Adding a new LLM provider to Kolsch.

This directory holds one file per supported provider. The shape is
fixed by the `ProviderConnector` Protocol
(`src/llm/protocol.py`); registration is a single line in
`src/llm/providers/__init__.py`. The CLI commands (`kolsch
connect`, `kolsch refresh`, `kolsch providers`, `kolsch
disconnect`), the engine's `build_llm` helper, and the
`platform.yaml` schema all pick up new providers automatically —
no other file in the platform changes when a provider is added.

See `docs/explanation/llm-providers.md` for the architectural
design (why a Protocol, why the metadata-source extension, why
no shipped model registry). This guide is the operational
checklist.

---

## 1. Adding a provider

### 1.1 Steps

1. **Create `src/llm/providers/<name>.py`.** Implement
   `ProviderConnector` (and `MetadataSourceConnector` if your
   provider's catalog can serve as a cross-provider metadata
   source — only OpenRouter does today).

2. **Register it.** Add one line to
   `src/llm/providers/__init__.py`:

   ```python
   from .anthropic import AnthropicConnector
   register("anthropic", AnthropicConnector)
   ```

3. **Add component tests.** Mirror the file under
   `tests/component/llm/providers/test_<name>.py`. Use
   `tests/component/llm/providers/test_opencode.py` as the
   template:
   - `validate_credentials` against a mocked HTTP transport.
   - `discover_models` against a recorded fixture response.
   - `build_llm` asserts the `crewai.LLM` constructed with the
     expected `model=`, `api_key=`, `base_url=`, etc.

4. **Update integration tests** if your provider has unusual
   auth or client construction. Most providers don't need this —
   the engine doesn't care which provider it's talking to once
   `build_llm` returned a `crewai.LLM`.

5. **Document.** Add a section to
   `docs/explanation/llm-providers.md` Part 5 (the planning
   sketches) describing the provider's auth shape, discovery
   shape, and any quirks.

That's the entire surface. The CLI iterates the registry, so the
new provider shows up in `kolsch providers` automatically; the
config schema accepts it because `llm_providers` is open-shaped;
the engine doesn't know it exists because the engine works with
`crewai.LLM` objects, not connectors.

### 1.2 The checklist (one question per Protocol method)

When writing the connector, the file must answer these six
questions. Each maps to a Protocol method:

- **What does the user type to connect?** →
  `auth_prompt_spec()` returns an `AuthPromptSpec` listing each
  `PromptField` the CLI should walk.
- **How do we know the credentials work?** →
  `validate_credentials()` makes a tiny live API call (a list-
  models, an auth check, a STS GetCallerIdentity, etc.) and
  returns `ValidationResult.ok` or `ValidationResult.failure`
  with a human-readable reason.
- **What models are available?** → `discover_models()` returns
  `list[ModelEntry]`. Each entry has `id` populated; parameter
  fields (context_length, supports_tools, etc.) populated where
  the provider's API exposes them, `None` otherwise.
- **How do we construct a `crewai.LLM` for this provider's
  models?** → `build_llm(model_id, provider_config, api_key,
  parameters, *, context)` returns a configured `crewai.LLM`. For OpenAI-compatible
  providers this is essentially one call:
  `crewai.LLM(model=f"<litellm-prefix>/{model_id}",
  api_key=api_key, base_url=provider_config.base_url, ...)`. For
  providers needing special construction (e.g., Bedrock with AWS
  sigv4), it can be more involved.
- **What does a call-time failure mean?** →
  `classify_call_error(exc, *, provider_config, model_id)` maps an
  exception the connector's own `crewai.LLM` raised while serving a
  call to a catalogued `model.*` error (or `None` if unrecognised).
  It must be pure. The engine's fallback chain only advances on a
  typed error, so this is what makes `fallback_models` work for your
  provider. OpenAI-compatible connectors delegate to
  `classify_openai_compat_exception` in `_errors.py`; a connector
  whose LLM already raises typed `model.*` errors returns `None`
  (see `claude_code.py`).
- **(If applicable)** Can we look up other providers' metadata?
  → implement `MetadataSourceConnector` and its
  `lookup_model_metadata(provider, model_id, credentials)`
  method. Returns a `ModelMetadata` if known, `None` otherwise.

If the answer to any of the first five is "I don't know yet,"
the provider isn't ready — document the open question in the
connector module's docstring and either don't ship it or ship
with the open question visible.

**Never put credentials in a URL query string.** httpx logs every
request's full URL at INFO, and `kolsch` writes INFO records to
`<workspace>/logs/kolsch.log` (and to stderr under `kolsch -v`). Send
keys in headers (`Authorization`, `x-api-key`, …) only.

---

## 2. The pattern in one screen

```python
# src/llm/providers/<name>.py

from collections.abc import Mapping
from typing import Any, ClassVar
import crewai

from errors import PlatformError
from llm.protocol import (
    AuthPromptSpec, PromptField,
    Credentials, ValidationResult,
    LLMCallContext,
    ModelEntry,
    ProviderConfig,
)


class <Name>Connector:
    """One-line description of what makes this provider distinct.

    Auth shape: ...
    Discovery shape: ...
    Quirks: ...
    """

    type_name: ClassVar[str] = "<name>"

    def auth_prompt_spec(self) -> AuthPromptSpec:
        return AuthPromptSpec(
            provider_label="<Display Name>",
            intro="<One-sentence explanation for the user>.",
            fields=[
                # PromptField entries — one per credential the
                # CLI should collect.
            ],
        )

    async def validate_credentials(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> ValidationResult:
        # Tiny live API call; return ok/failure.
        ...

    async def discover_models(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> list[ModelEntry]:
        # Query the provider's catalog; map to ModelEntry list.
        ...

    async def build_llm(
        self,
        model_id: str,
        provider_config: ProviderConfig,
        api_key: str | None,
        parameters: Mapping[str, Any] | None = None,
        *,
        context: LLMCallContext | None = None,
    ) -> crewai.LLM:
        # `context` (bug-144) identifies the owning session; forward
        # `context.conversation_key` as provider session metadata if
        # the provider wants it, otherwise ignore it.
        return crewai.LLM(
            model=f"<litellm-prefix>/{model_id}",
            api_key=api_key,
            base_url=provider_config.base_url,
            timeout=provider_config.timeout_seconds,
        )

    def classify_call_error(
        self,
        exc: BaseException,
        *,
        provider_config: ProviderConfig,
        model_id: str,
    ) -> PlatformError | None:
        # Pure: map a call-time failure of the LLM above to a
        # catalogued `model.*` error, or `None` if unrecognised.
        ...
```

And the registration:

```python
# src/llm/providers/__init__.py

from .opencode import OpenCodeConnector
from .openrouter import OpenRouterConnector
from .<name> import <Name>Connector   # add this line

from llm.registry import register

register("opencode", OpenCodeConnector)
register("openrouter", OpenRouterConnector)
register("<name>", <Name>Connector)   # and this line
```

---

## 2.5. Picking the right `crewai.LLM` route

*(Added after [bug-litellm-model-routing.md](../../process/bug-litellm-model-routing.md):
the OpenCode connector originally used ``openai/<model>`` and
LiteLLM rejected non-canonical model names with a hard import
error.)*

`crewai.LLM(model=...)` takes a `<prefix>/<model_id>` string. The
prefix selects a CrewAI native provider class; the `model_id` is
validated against that provider's known-model list. Two
properties decide whether your provider's models will work:

1. **Does the validator accept arbitrary names?** Some prefixes
   (`openai/`, `anthropic/`, `bedrock/`) validate the model_id
   against a hardcoded list of canonical names. Others
   (`openrouter/`, `hosted_vllm/`, `ollama/`, `cerebras/`) return
   `True` unconditionally — see `crewai/llm.py:432-497` in the
   pinned CrewAI version.
2. **Does the resulting native class speak the wire shape your
   provider serves?** Most "OpenAI-compatible" providers should
   land on `OpenAICompatibleCompletion`
   (`crewai/llm.py:595-609`), which speaks Bearer auth +
   `/v1/chat/completions`. The prefixes that route there are:
   `openrouter`, `deepseek`, `ollama`, `ollama_chat`,
   `hosted_vllm`, `cerebras`, `dashscope`.

The decision matrix for new connectors:

| Your provider              | Use prefix       | Why                                         |
|----------------------------|------------------|---------------------------------------------|
| OpenAI itself (canonical models only) | `openai/`        | Native client; validator passes for `gpt-*`/`o1-*`. |
| OpenRouter (proxy)         | `openrouter/`    | Permissive validator; OpenAI-compat client. |
| Anthropic API (canonical)  | `anthropic/`     | Native validator passes for `claude-*`.     |
| Bedrock (AWS sigv4)        | `bedrock/`       | Native handles sigv4; validator passes.     |
| Llama-local (Ollama)       | `ollama/`        | Permissive; talks to local Ollama daemon.   |
| Llama-local (llama.cpp / vLLM, OpenAI-compat shim) | `hosted_vllm/`   | Permissive validator + OpenAI-compat client. **Same route as OpenCode**. |
| OpenCode-style (any OpenAI-compat endpoint serving non-OpenAI model names) | `hosted_vllm/`   | The OpenCode bug-fix path. |

**The trap**: `<provider>/<model>` looks like it should "just
work" for any OpenAI-compatible endpoint, but `openai/` is the
strict prefix and ships with a model-name validator that rejects
anything not on OpenAI's actual list. Don't pick a prefix because
its name matches your provider's marketing — pick one because its
validator and client behaviour match your provider's actual wire
shape.

`hosted_vllm/` is misleadingly named: it isn't only for vLLM
deployments. It's CrewAI's standing idiom for "OpenAI-compatible
endpoint serving arbitrary model IDs." OpenCode uses it; the
planned Llama-local connector (llama.cpp / vLLM branch) should
use it; any future OpenAI-compatible shim should use it unless
its catalogue is canonical OpenAI.

If a future contributor "fixes" `hosted_vllm/` back to `openai/`
on a connector serving non-OpenAI models, the failure mode is the
same `ImportError("Unable to initialize LLM…the model did not
match any supported native provider…")`. The
[bug-litellm-model-routing.md](../../process/bug-litellm-model-routing.md)
ticket walks through the diagnostic; the OpenCode connector's
module docstring explains the choice inline.

### 2.6. OpenAI-compatible connectors: use `build_openai_compatible_llm`

*(bug-138, bug-144.)* A connector that lands on
`OpenAICompatibleCompletion` (the `openrouter/` and `hosted_vllm/`
routes above) should build its client with
`build_openai_compatible_llm(...)` from `_openai_compat.py`, not
`crewai.LLM(...)` directly:

```python
class <Name>Connector:
    type_name: ClassVar[str] = "<name>"
    # Header NAMES that carry the session's conversation key; `()` for
    # none. Users override it per provider with
    # `llm_providers.<name>.request_policy.session_headers`.
    DEFAULT_REQUEST_POLICY: ClassVar[OpenAICompatRequestPolicy] = (
        OpenAICompatRequestPolicy(session_headers=("x-<name>-session",))
    )

    async def build_llm(self, model_id, provider_config, api_key,
                        parameters=None, *, context=None) -> crewai.LLM:
        return build_openai_compatible_llm(
            provider="hosted_vllm",          # or "openrouter"
            model_id=model_id,
            provider_config=provider_config,
            api_key=api_key,
            default_timeout=_DEFAULT_TIMEOUT_SECONDS,
            parameters=parameters,
            context=context,
            default_policy=self.DEFAULT_REQUEST_POLICY,
        )

    def classify_call_error(self, exc, *, provider_config, model_id):
        return classify_openai_compat_exception(
            exc,
            provider=provider_config.name,
            model=model_id,
            timeout_seconds=<the same effective timeout build_llm used>,
        )
```

The builder:

- resolves the request policy (connector default, overridden by
  `request_policy` in `platform.yaml`) and sends each session header
  with `context.conversation_key` as its value. If the policy has
  session headers and `context` is `None` it raises
  `llm.client_init_failed` — never mint a substitute id;
- sends `User-Agent: kolsch/<version>` (`llm.user_agent.kolsch_user_agent`);
- drops agent-YAML `parameters` that collide with connector-owned
  kwargs (`INFRA_KEYS`: routing keys plus `default_headers`,
  `extra_headers`, `headers`, `client_params`, `organization`,
  `project`, `default_query`, `api_base`), so YAML can't hijack
  routing, the session headers, or the User-Agent;
- builds the client as `KolschOpenAICompatibleCompletion`, which
  applies kolsch's **conditional strict tool-schema policy**:

- CrewAI's stock conversion forces `"strict": true` and closes every
  object schema to `{properties: {}, additionalProperties: false}`.
  A free-form map argument (pydantic `dict[str, X]`, e.g.
  `fetch_url.headers` or `run_workflow.inputs`) then reaches a
  strict-honouring model as "empty object only", and it sends `{}`.
- So, per tool: if `is_strict_compatible(parameters)` (no open maps,
  no untyped values, no strict-rejected keywords such as
  `minProperties`), the stock strict payload is sent unchanged.
  Otherwise the kolsch-built `parameters` go out with
  `"strict": false` and `$ref`s inlined (recursive schemas verbatim).

The override is per subclass instance (no global CrewAI patch), so a
`crewai.LLM(...)` built elsewhere keeps stock behaviour.
`tests/unit/test_tool_schema_strict_policy.py` guards the private
CrewAI seam (`OpenAICompletion._convert_tools_for_interference`) —
re-check `_openai_compat.py` whenever that guard fails after a crewai
bump. `tests/component/test_llm_session_headers.py` proves the session
headers and User-Agent reach the wire.

### 2.7. Migration note — `build_llm(..., *, context)` (bug-144)

`ProviderConnector.build_llm` gained a keyword-only
`context: LLMCallContext | None = None`. This is a declared protocol
change: connectors are registered in-tree only, so every connector
(and test double) was updated in the same change and there is no
runtime signature probing. A connector that has no use for
per-conversation metadata accepts and ignores it (see
`claude_code.py`); one that needs the conversation key reads
`context.conversation_key` and raises a typed `PlatformError` when
`context` is `None`.

### 2.8. Migration note — `classify_call_error` (bug-145)

`ProviderConnector` gained a required
`classify_call_error(exc, *, provider_config, model_id) -> PlatformError | None`.
This is a declared protocol change: connectors are registered in-tree
only, so every connector (and test double) was updated in the same
change and nothing probes for the method with `getattr`. The shared
OpenAI-compatible mapper is total over HTTP status >= 400 and walks
CrewAI's re-wrapped shapes (`ValueError … from NotFoundError`,
`ConnectionError … from APIConnectionError`,
`LLMContextLengthExceededError`, and the `raise e from e` self-cycle),
so an OpenAI-compatible connector only delegates to it.

---

## 3. Things to NOT do

- **Don't invent a new abstraction.** The standard is
  `crewai.LLM`. If you find yourself wrapping the return value
  of `build_llm`, stop — the engine wraps it inside the Framework
  Protocol's `LLMHandle`; the engine's wrapper is the only one
  that should exist.
- **Don't ship a static model catalog.** Use the provider's API
  at connect time. If the provider has no list-models endpoint
  (Anthropic), return an empty list and tell the user.
- **Don't write API key values to `platform.yaml` or any event.**
  Only the secret NAME (`api_key_secret: ANTHROPIC_API_KEY`)
  goes in `platform.yaml`. The VALUE lives in `.env`. This is a
  hard rule (AGENTS.md "Hard rules"); TC-LLM-10 enforces it.
- **Don't fail silently when the provider's API is unreachable.**
  Raise `llm.discovery_failed` (Recoverable — the user can
  retry) for transient issues; raise `llm.validation_failed`
  (Fatal) when credentials are demonstrably wrong.
- **Don't take initiative beyond the connector's Protocol
  surface.** No orchestration. No retries (that's the engine's
  domain). No model selection logic. The connector answers the
  questions in section 1.2 and nothing more.

---

## 4. Where to look next

- **Architecture & rationale:** `docs/explanation/llm-providers.md`.
- **Protocol shapes:** `src/llm/protocol.py`.
- **Reference implementations:** `opencode.py` (basic shape),
  `openrouter.py` (provider + metadata-source shape).
- **Error catalog:** `docs/explanation/error-model.md`.
- **Success criteria:** `docs/process/development-testing-handbook.md`.
- **Test cases:** `docs/process/test-catalog.md` Part 9.5.
