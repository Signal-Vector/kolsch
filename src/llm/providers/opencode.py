"""OpenCode provider — OpenAI-compatible reference implementation.

OpenAI-compatible REST shape (Bearer auth, `/v1/models` for discovery).
The base URL is configurable; the catalog endpoint returns model IDs
only (no parameter metadata), so
[`discover_models`][llm.providers.opencode.OpenCodeConnector.discover_models]
yields [`ModelEntry`][llm.protocol.ModelEntry] records with every
parameter field set to `None`. The CLI's augmentation loop fills those
in from a metadata source (OpenRouter today) when one is connected.

This provider is also the reference template for any OpenAI-compatible
provider (OpenAI itself, forks, third-party hosted shims). The four
methods stay near-identical; only `type_name` and `auth_prompt_spec`
change.

**CrewAI routing.**
[`build_llm`][llm.providers.opencode.OpenCodeConnector.build_llm]
constructs the client `crewai.LLM(model=f"hosted_vllm/{model_id}", ...)`
would (via `build_openai_compatible_llm`, which adds bug-138's
conditional strict tool-schema policy — see `_openai_compat.py`). The
prefix is deliberately NOT `openai/` even though OpenCode is
OpenAI-compatible at the wire level. CrewAI's native `openai/`
provider validates the model part against OpenAI's canonical model
list (`gpt-4*`, `gpt-3.5-*`, `o1-*`, ...); for non-canonical model
names like `qwen3.6-plus` the validator returns False and CrewAI
tries to fall back to LiteLLM. With LiteLLM not installed (no
`crewai[litellm]` extra in `pyproject.toml`) the fallback raises
`ImportError`.

`hosted_vllm` is the only native provider that pairs (a) a permissive
model-name validator — `_validate_model_in_constants` returns `True`
for any string when the provider is `hosted_vllm`, with the comment
"vLLM serves any model" in CrewAI's source — and (b) the same
OpenAI-compat client class `OpenAICompatibleCompletion` that the
`openai/` route would have used had validation passed. It accepts
`base_url`, `api_key`, and `timeout` with identical names, so the
kwargs the OpenCode provider produces pass through unchanged.

The prefix name is misleading. `hosted_vllm` is CrewAI's idiom for
"OpenAI-compatible endpoint serving arbitrary models" — it reflects
CrewAI's history (originally added for vLLM deployments), not
OpenCode's underlying implementation. An OpenCode endpoint is not
literally a vLLM server; it is any OpenAI-compatible HTTP shim. The
prefix is the right choice for this provider because of the validator
plus client behaviour, not because OpenCode runs vLLM. Future
contributors: do NOT "fix" this back to `openai/` — the alternative
is adding the ~100 MB `litellm` extra and passing
`custom_llm_provider="openai"` to bypass the same validator, which is
strictly worse.

OpenRouter's provider (sibling file) uses `openrouter/<model>` because
that prefix has an analogously permissive validator; the lesson
generalises — see `src/llm/providers/README.md` for the rule that
lets future providers (Llama-local, Bedrock, Anthropic, OpenAI
canonical) pick the right route on first try.

**Error classification.**
Every provider response the connector might see maps to exactly one
catalogued `model.*` code. The mapping is the basis of the engine's
fallback decision; adding a new branch here without updating the
trigger table breaks an AGENTS.md hard rule.

- HTTP 429 -> `model.rate_limited` (Recoverable).
- HTTP 5xx (500-599) -> `model.server_error` (Recoverable).
- `httpx.TimeoutException` family -> `model.timeout` (Recoverable).
- HTTP 400 with body markers (`context_length` / `context_window` /
  `too long` / `maximum context`) -> `model.context_overflow`
  (Recoverable).
- HTTP 401 or 403 -> `model.authentication_failed` (Fatal).
- HTTP 400 (any other body) -> `model.bad_request` (Fatal).

OpenAI-compatible providers return a JSON error body of shape
`{"error": {"message": ..., "code": ..., "type": ...}}`; the overflow
detection scans both `error.message` and `error.code` for the
documented markers. Bodies that don't parse as JSON fall through to
the status-only branches.

Other HTTP statuses (200 with a malformed body, 3xx redirects, etc.)
are NOT covered here — they are the response-parsing layer's problem
and surface as `errors.ModelMalformedResponse` (also Recoverable).
Truly out-of-band failures (DNS resolution, connection refused) raise
`httpx.HTTPError` subclasses; this provider wraps them as
`model.timeout` since they prevent a first-byte response.

The classification helper
`classify_http_response`
is pure and testable in isolation. A future commit wires it into the
call layer that wraps `crewai.LLM` invocation; today the engine
exercises the same mapping through the Mock-LLM test path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from errors import LlmDiscoveryFailed
from llm.protocol import (
    AuthPromptSpec,
    Credentials,
    ModelEntry,
    PromptField,
    ProviderConfig,
    ValidationResult,
)
from llm.providers._errors import (
    classify_http_response,
    classify_openai_compat_exception,
    classify_transport_error,
)
from llm.providers._openai_compat import (
    OpenAICompatRequestPolicy,
    build_openai_compatible_llm,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import crewai

    from errors import PlatformError
    from llm.protocol import LLMCallContext

# The 30s default sits below the observed long-tail of opencode
# responses (replay showed 12 successful calls between 30 and 56s,
# with two reaching 112-113s). Users on affected models saw a 32%
# timeout rate at 30s; bumping to 180s drops it to 0% in the same
# session. Override per-provider via
# `llm_providers.<name>.timeout_seconds` in platform.yaml.
_DEFAULT_TIMEOUT_SECONDS = 180.0
_HTTP_CLIENT_ERROR_MIN = 400


class OpenCodeConnector:
    """The OpenCode provider — OpenAI-compatible REST.

    Stateless. Construct with `transport=httpx.MockTransport(...)` in
    tests to bypass real HTTP; production uses the registry's
    no-argument `cls()` path.

    Attributes:
        type_name: The registry key — `"opencode"`.
        DEFAULT_REQUEST_POLICY: The connector's request policy (bug-144);
            `llm_providers.<name>.request_policy` overrides it.
    """

    type_name: ClassVar[str] = "opencode"
    # bug-144: OpenCode Go rejects chat completions without a stable
    # per-conversation `x-opencode-session` (400 MissingSessionID).
    # Override per provider via `llm_providers.<name>.request_policy`.
    DEFAULT_REQUEST_POLICY: ClassVar[OpenAICompatRequestPolicy] = (
        OpenAICompatRequestPolicy(
            session_headers=("x-opencode-session",),
        )
    )

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Construct with an optional injected transport for tests."""
        self._transport = transport

    # ---- Error classification ---------------------------------------------

    classify_http_response = staticmethod(classify_http_response)
    classify_transport_error = staticmethod(classify_transport_error)

    # ---- Protocol surface --------------------------------------------------

    def auth_prompt_spec(self) -> AuthPromptSpec:
        """Return the two prompts: base URL and API key."""
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

    async def validate_credentials(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> ValidationResult:
        """Verify credentials by GET `{base_url}/models`.

        200 -> `ok=True`. Any failure (network error or non-2xx) ->
        `ok=False` with a reason summarising the failure.
        """
        url = self._models_url(base_url, credentials)
        token = credentials.values.get("api_key", "")
        try:
            async with self._client() as client:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            return ValidationResult(
                ok=False,
                reason=f"network error: {type(exc).__name__}: {exc}",
            )
        if response.status_code >= _HTTP_CLIENT_ERROR_MIN:
            return ValidationResult(
                ok=False,
                reason=(
                    f"HTTP {response.status_code} from /models — credentials rejected"
                ),
            )
        return ValidationResult(ok=True)

    async def discover_models(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> list[ModelEntry]:
        """Fetch the catalog by GET `{base_url}/models`.

        Yields one `ModelEntry(id=...)` per `data[].id` entry. Parameter
        fields stay `None` — OpenCode does not expose them.

        Raises:
            LlmDiscoveryFailed: Network failure or unparseable body.
        """
        url = self._models_url(base_url, credentials)
        token = credentials.values.get("api_key", "")
        try:
            async with self._client() as client:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LlmDiscoveryFailed(
                provider=self.type_name,
                reason=f"{type(exc).__name__}: {exc}",
            ) from exc

        data = payload.get("data", [])
        if not isinstance(data, list):
            raise LlmDiscoveryFailed(
                provider=self.type_name,
                reason="response payload missing 'data' list",
            )
        out: list[ModelEntry] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            out.append(ModelEntry(id=model_id))
        return out

    async def build_llm(
        self,
        model_id: str,
        provider_config: ProviderConfig,
        api_key: str | None,
        parameters: Mapping[str, Any] | None = None,
        *,
        context: LLMCallContext | None = None,
    ) -> crewai.LLM:
        """Construct a `crewai.LLM` pointed at OpenCode.

        Routes through CrewAI's `hosted_vllm` prefix — see the module
        docstring for the routing-choice rationale. `parameters`
        (e.g. `{max_tokens, temperature, top_p}`) is forwarded to
        `crewai.LLM`. Infrastructure keys are dropped silently so
        agent YAML can't accidentally hijack routing.

        `context` is the owning session's `LLMCallContext` (bug-144):
        each `DEFAULT_REQUEST_POLICY` (or `request_policy` override)
        session header carries its `conversation_key`, and the request
        identifies as `kolsch/<version>`. A policy with session headers
        and `context=None` raises `LlmClientInitFailed`.
        """
        # bug-138: same client `crewai.LLM(model="hosted_vllm/...")` builds,
        # with kolsch's conditional strict tool-schema policy applied.
        return build_openai_compatible_llm(
            provider="hosted_vllm",
            model_id=model_id,
            provider_config=provider_config,
            api_key=api_key,
            default_timeout=_DEFAULT_TIMEOUT_SECONDS,
            parameters=parameters,
            context=context,
            default_policy=self.DEFAULT_REQUEST_POLICY,
        )

    def classify_call_error(
        self,
        exc: BaseException,
        *,
        provider_config: ProviderConfig,
        model_id: str,
    ) -> PlatformError | None:
        """Map a call-time failure of this connector's `crewai.LLM` (bug-145).

        Delegates to the shared OpenAI-compatible mapper
        `llm.providers._errors.classify_openai_compat_exception`,
        stamping `provider_config.name` and the same effective timeout
        `build_llm` configured.
        """
        timeout = (
            float(provider_config.timeout_seconds)
            if provider_config.timeout_seconds is not None
            else _DEFAULT_TIMEOUT_SECONDS
        )
        return classify_openai_compat_exception(
            exc,
            provider=provider_config.name,
            model=model_id,
            timeout_seconds=timeout,
        )

    # ---- Internals ---------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        """Return an `httpx.AsyncClient` honouring the injected test transport."""
        kwargs: dict[str, Any] = {"timeout": _DEFAULT_TIMEOUT_SECONDS}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    def _models_url(base_url: str | None, credentials: Credentials) -> str:
        """Build the `/models` URL, falling back to `credentials.values['base_url']`.

        The credentials fallback covers the connect-flow path, where
        platform.yaml hasn't been written yet so the explicit
        `base_url` argument is `None`.
        """
        effective = base_url or credentials.values.get("base_url") or ""
        effective = effective.rstrip("/")
        if not effective:
            return "/models"
        return f"{effective}/models"
