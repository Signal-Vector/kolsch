"""OpenCode connector — patch Part 5 reference implementation.

OpenAI-compatible REST shape (Bearer auth, ``/v1/models`` for discovery).
The base URL is configurable; the catalog endpoint returns model IDs
only (no parameter metadata), so :meth:`discover_models` yields
:class:`ModelEntry` records with every parameter field set to ``None``.
The CLI's augmentation loop fills those in from a metadata source
(OpenRouter today) when one is connected.

This connector is also the reference template for any OpenAI-compatible
provider (OpenAI itself, forks, third-party hosted shims). The four
methods stay near-identical; only ``type_name`` and
:meth:`auth_prompt_spec` change.

## Error Classification (Phase 1.3, FB-R10)

Every provider response the connector might see maps to exactly one
catalogued ``model.*`` code. The mapping is the basis of the engine's
fallback decision (see Error Model §4.15 + Phase 1.3 patch §2.2);
adding a new branch here without updating the trigger table breaks
the AGENTS.md FB-R10 hard rule.

- HTTP 429 → ``model.rate_limited`` (Recoverable).
- HTTP 5xx (500-599) → ``model.server_error`` (Recoverable).
- ``httpx.TimeoutException`` family → ``model.timeout`` (Recoverable).
- HTTP 400 with body markers (``context_length`` / ``context_window``
  / ``too long`` / ``maximum context``) → ``model.context_overflow``
  (Recoverable).
- HTTP 401 or 403 → ``model.authentication_failed`` (Fatal).
- HTTP 400 (any other body) → ``model.bad_request`` (Fatal).

OpenAI-compatible providers return a JSON error body of shape
``{"error": {"message": ..., "code": ..., "type": ...}}``; the
overflow detection scans both ``error.message`` and ``error.code``
for the documented markers. Bodies that don't parse as JSON fall
through to the status-only branches.

Other HTTP statuses (200 with a malformed body, 3xx redirects, etc.)
are NOT covered here — they are the response-parsing layer's
problem and surface as :class:`errors.ModelMalformedResponse` (also
Recoverable). Truly out-of-band failures (DNS resolution, connection
refused) raise ``httpx.HTTPError`` subclasses; this connector wraps
them as ``model.timeout`` since they prevent first-byte response.

The classification helper :func:`classify_http_response` is a pure
function — testable in isolation. A future commit wires it into the
call layer that wraps ``crewai.LLM`` invocation; today the engine
exercises the same mapping through the Fake-LLM test path
(see :mod:`engine._fallback`).
"""

from __future__ import annotations

from typing import Any, ClassVar

import crewai
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
    classify_transport_error,
)

_DEFAULT_TIMEOUT_SECONDS = 30.0
_HTTP_CLIENT_ERROR_MIN = 400


class OpenCodeConnector:
    """Reference :class:`ProviderConnector` for OpenCode.

    Stateless. Construct with `transport=httpx.MockTransport(...)` in
    tests to bypass real HTTP; production uses the registry's
    `cls()` no-arg path.
    """

    type_name: ClassVar[str] = "opencode"

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._transport = transport

    # ---- Phase 1.3 error classification (FB-R10) --------------------------

    classify_http_response = staticmethod(classify_http_response)
    classify_transport_error = staticmethod(classify_transport_error)

    # ---- Protocol surface --------------------------------------------------

    def auth_prompt_spec(self) -> AuthPromptSpec:
        """Two prompts: base URL + API key."""
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
        """GET `{base_url}/models` — 200 → ok; any failure → ok=False."""
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
        """GET `{base_url}/models` and yield one `ModelEntry(id=...)`
        per `data[].id`. Parameter fields stay ``None`` — OpenCode
        does not expose them. Network/parse failure raises
        :class:`LlmDiscoveryFailed`."""
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
    ) -> crewai.LLM:
        """Construct a `crewai.LLM` pointed at OpenCode via litellm's
        OpenAI-compatible adapter."""
        return crewai.LLM(
            model=f"openai/{model_id}",
            api_key=api_key,
            base_url=provider_config.base_url,
            timeout=(
                provider_config.timeout_seconds
                if provider_config.timeout_seconds is not None
                else _DEFAULT_TIMEOUT_SECONDS
            ),
        )

    # ---- Internals ---------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        """Return an `httpx.AsyncClient` honouring the test transport
        when one was injected."""
        kwargs: dict[str, Any] = {"timeout": _DEFAULT_TIMEOUT_SECONDS}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    def _models_url(base_url: str | None, credentials: Credentials) -> str:
        """`{base_url}/models` — falls back to `credentials.values['base_url']`
        when the explicit arg is None (the connect-flow path, where
        platform.yaml hasn't been written yet)."""
        effective = base_url or credentials.values.get("base_url") or ""
        effective = effective.rstrip("/")
        if not effective:
            return "/models"
        return f"{effective}/models"
