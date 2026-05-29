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
