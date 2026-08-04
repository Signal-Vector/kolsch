"""OpenRouter provider — the provider-plus-metadata-source reference.

Implements both
[`ProviderConnector`][llm.protocol.ProviderConnector] (so it can route
real calls) and
[`MetadataSourceConnector`][llm.protocol.MetadataSourceConnector] (so
the connect-flow augmentation loop can use its catalog to fill in
parameter metadata for other providers' models).

Two endpoints carry the surface:

- `GET /api/v1/auth/key` verifies the API key and returns the
  remaining credit balance. Used by `validate_credentials`.
- `GET /api/v1/models` is the public catalog (no key required, but a
  key gets higher rate limits). Used by `discover_models` AND cached
  for `lookup_model_metadata`.

Catalog cache is per-instance: a single provider instance reused
across an augmentation loop only hits `/api/v1/models` once.

**Error classification.**
OpenRouter PROXIES many providers; the same HTTP semantics apply on
the wire but the underlying provider's quirks are flattened into a
unified OpenAI-compat error body. The status-code mapping is identical
to OpenCode's; the body-keyword scan also catches provider-specific
overflow markers (Anthropic's
`invalid_request_error.code = context_window_exceeded` and OpenAI's
`context_length_exceeded` both flatten to the same keywords in
OpenRouter's relay body).

- HTTP 429 -> `model.rate_limited` (Recoverable).
- HTTP 5xx -> `model.server_error` (Recoverable).
- `httpx.TimeoutException` / transport-down -> `model.timeout`
  (Recoverable).
- HTTP 400 with body keyword `context_*` / `too long` / `maximum
  context` -> `model.context_overflow` (Recoverable).
- HTTP 401 or 403 -> `model.authentication_failed` (Fatal).
- HTTP 400 otherwise -> `model.bad_request` (Fatal).

Because OpenRouter is a proxy, an upstream provider's 5xx may surface
as either `model.server_error` (if OpenRouter returns the same 5xx)
or `model.bad_request` (if OpenRouter wraps it). The conservative
reading: 5xx-at-the-wire is always Recoverable; the non-5xx wrap is
Fatal because the chain's other entries likely route through
different providers anyway.

The classification helper
`classify_http_response`
is shared with the other OpenAI-compat providers via
`llm.providers._errors` so a new provider that picks up the same
shape doesn't reimplement it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import crewai
import httpx

from errors import LlmDiscoveryFailed
from llm.protocol import (
    AuthPromptSpec,
    Credentials,
    ModelEntry,
    ModelMetadata,
    ModelPricing,
    PromptField,
    ProviderConfig,
    ValidationResult,
)
from llm.providers._errors import (
    classify_http_response,
    classify_transport_error,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_HTTP_CLIENT_ERROR_MIN = 400
_PER_MILLION = 1_000_000

# Keys the connector sets itself when building `crewai.LLM`. Agent-YAML
# `ModelSpec.parameters` values that collide with these are dropped —
# the connector always wins on routing so a mistyped YAML can't hijack
# the model, key, base_url, or timeout via a `parameters:` entry.
_INFRA_KEYS = frozenset({"model", "api_key", "base_url", "timeout"})


def _extras(parameters: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return `parameters` with `_INFRA_KEYS` filtered out."""
    if not parameters:
        return {}
    return {k: v for k, v in parameters.items() if k not in _INFRA_KEYS}


class OpenRouterConnector:
    """The OpenRouter provider — proxy and cross-provider metadata source.

    Implements [`MetadataSourceConnector`][llm.protocol.MetadataSourceConnector]
    so the CLI's augmentation loop can fill in parameter metadata for
    OTHER providers' models from OpenRouter's catalog.

    Attributes:
        type_name: The registry key — `"openrouter"`.
    """

    type_name: ClassVar[str] = "openrouter"

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Construct with an optional injected transport for tests."""
        self._transport = transport
        # Per-instance catalog cache for lookup_model_metadata. Populated
        # lazily on first call; keyed by the resolved OpenRouter model
        # ID (e.g. "anthropic/claude-opus-4-7"). None means "not yet
        # fetched"; an empty dict means "fetched, no entries".
        self._catalog: dict[str, ModelEntry] | None = None

    # ---- Error classification ---------------------------------------------

    classify_http_response = staticmethod(classify_http_response)
    classify_transport_error = staticmethod(classify_transport_error)

    # ---- ProviderConnector surface ----------------------------------------

    def auth_prompt_spec(self) -> AuthPromptSpec:
        """Return the single API-key prompt (base URL is canonical)."""
        return AuthPromptSpec(
            provider_label="OpenRouter",
            intro=(
                "OpenRouter uses API key authentication. The model "
                "catalog is public; the key is only needed to route "
                "calls through OpenRouter. If you connect OpenRouter, "
                "the platform will also use its catalog to look up "
                "model parameters for other providers (you can disable "
                "this in platform.yaml)."
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

    async def validate_credentials(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> ValidationResult:
        """Verify credentials by GET `{base_url}/auth/key`.

        200 -> `ok=True` with `credits_remaining` in `metadata` when
        the response includes both limit and usage. Any failure ->
        `ok=False` with a reason.
        """
        url = f"{self._base(base_url)}/auth/key"
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
                    f"HTTP {response.status_code} from /auth/key — credentials rejected"
                ),
            )
        meta: dict[str, Any] = {}
        try:
            body = response.json()
        except ValueError:
            body = {}
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict):
            remaining = self._compute_credits_remaining(data)
            if remaining is not None:
                meta["credits_remaining"] = remaining
        return ValidationResult(ok=True, metadata=meta or None)

    async def discover_models(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> list[ModelEntry]:
        """Fetch the catalog by GET `{base_url}/models`.

        Yields one populated `ModelEntry` per `data[]` item. Populates
        the per-instance cache as a side effect.
        """
        entries = await self._fetch_catalog(credentials, base_url)
        # `_fetch_catalog` populates the per-instance cache as a side
        # effect; return it as a list in the order the API returned.
        return list(entries.values())

    async def build_llm(
        self,
        model_id: str,
        provider_config: ProviderConfig,
        api_key: str | None,
        parameters: Mapping[str, Any] | None = None,
    ) -> crewai.LLM:
        """Construct a `crewai.LLM` routed through OpenRouter.

        `parameters` (e.g. `{max_tokens, temperature, top_p}`) is
        forwarded to `crewai.LLM`. Infrastructure keys are dropped
        silently so agent YAML can't accidentally hijack routing —
        see `_INFRA_KEYS` at module level.
        """
        return crewai.LLM(
            model=f"openrouter/{model_id}",
            api_key=api_key,
            base_url=provider_config.base_url,
            timeout=(
                provider_config.timeout_seconds
                if provider_config.timeout_seconds is not None
                else _DEFAULT_TIMEOUT_SECONDS
            ),
            **_extras(parameters),
        )

    # ---- MetadataSourceConnector extension --------------------------------

    async def lookup_model_metadata(
        self,
        provider: str,
        model_id: str,
        credentials: Credentials | None = None,
    ) -> ModelMetadata | None:
        """Look up metadata for `(provider, model_id)` in the cached catalog.

        Default mapping is `{provider}/{model_id}` — the form
        OpenRouter uses for its canonical first-party integrations
        (`anthropic/claude-opus-4-7`, `openai/gpt-4o`, ...). For
        providers without a canonical OpenRouter ID (custom
        OpenAI-compat shims), this returns `None`.

        Args:
            provider: The platform-side provider name.
            model_id: The model id on that provider.
            credentials: Optional credentials; populated when the
                catalog hasn't been fetched yet and the caller wants
                a key included in the request.

        Returns:
            The matched metadata, or `None` when the catalog has no
            entry (the caller decides whether that's a soft miss or
            worth surfacing).
        """
        if self._catalog is None:
            await self._fetch_catalog(credentials or Credentials(), None)
        if self._catalog is None:
            # Fetch failure — already raised LlmDiscoveryFailed unless
            # we swallowed it; treat as "no entry" defensively.
            return None
        candidate = f"{provider}/{model_id}"
        entry = self._catalog.get(candidate)
        if entry is None:
            return None
        return ModelMetadata(
            context_length=entry.context_length,
            supports_tools=entry.supports_tools,
            supports_streaming=entry.supports_streaming,
            pricing=entry.pricing,
        )

    # ---- Internals --------------------------------------------------------

    async def _fetch_catalog(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> dict[str, ModelEntry]:
        """Populate `self._catalog` from `/models`.

        Raises:
            LlmDiscoveryFailed: Network failure or unparseable body.
        """
        url = f"{self._base(base_url)}/models"
        headers: dict[str, str] = {}
        token = credentials.values.get("api_key")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with self._client() as client:
                response = await client.get(url, headers=headers)
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
        catalog: dict[str, ModelEntry] = {}
        for raw in data:
            if not isinstance(raw, dict):
                continue
            entry = self._parse_entry(raw)
            if entry is not None:
                catalog[entry.id] = entry
        self._catalog = catalog
        return catalog

    @staticmethod
    def _parse_entry(raw: dict[str, Any]) -> ModelEntry | None:
        """Map one `/models` row to a populated `ModelEntry`."""
        model_id = raw.get("id")
        if not isinstance(model_id, str) or not model_id:
            return None
        context_length = raw.get("context_length")
        if not isinstance(context_length, int):
            context_length = None
        # `supported_parameters` is the list OpenRouter uses to signal
        # which parameters a model accepts; "tools" and "stream" are
        # the relevant flags for the platform's purposes.
        supported = raw.get("supported_parameters") or []
        supports_tools: bool | None = None
        supports_streaming: bool | None = None
        if isinstance(supported, list):
            supports_tools = "tools" in supported
            supports_streaming = "stream" in supported or "streaming" in supported
        pricing = OpenRouterConnector._parse_pricing(raw.get("pricing"))
        return ModelEntry(
            id=model_id,
            context_length=context_length,
            supports_tools=supports_tools,
            supports_streaming=supports_streaming,
            pricing=pricing,
        )

    @staticmethod
    def _parse_pricing(raw: object) -> ModelPricing | None:
        """Parse OpenRouter's per-token pricing into per-million floats.

        OpenRouter quotes per-token costs as strings; returns `None`
        when either field is missing or unparseable.
        """
        if not isinstance(raw, dict):
            return None
        prompt_raw = raw.get("prompt")
        completion_raw = raw.get("completion")
        if not isinstance(prompt_raw, (int, float, str)):
            return None
        if not isinstance(completion_raw, (int, float, str)):
            return None
        try:
            prompt = float(prompt_raw) * _PER_MILLION
            completion = float(completion_raw) * _PER_MILLION
        except (TypeError, ValueError):
            return None
        return ModelPricing(
            prompt_per_million=prompt,
            completion_per_million=completion,
        )

    @staticmethod
    def _compute_credits_remaining(data: dict[str, Any]) -> float | None:
        """Compute `limit - usage` from `/auth/key`'s response data.

        Returns `None` when either field is missing or non-numeric
        (the `limit` field may be `null` for unlimited keys).
        """
        limit = data.get("limit")
        usage = data.get("usage")
        if not isinstance(limit, (int, float)):
            return None
        if not isinstance(usage, (int, float)):
            return None
        return float(limit) - float(usage)

    def _client(self) -> httpx.AsyncClient:
        """Return an `httpx.AsyncClient` honouring the injected test transport."""
        kwargs: dict[str, Any] = {"timeout": _DEFAULT_TIMEOUT_SECONDS}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    def _base(base_url: str | None) -> str:
        """Return the base URL, falling back to OpenRouter's canonical."""
        return (base_url or _DEFAULT_BASE_URL).rstrip("/")
