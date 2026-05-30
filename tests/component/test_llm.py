"""Component tests for src/llm — TC-LLM-01..10 (Phase 1.1, LLM-R8).

Spec: docs/explanation/llm-providers.md + docs/process/phase1.1-llm-specs.md
+ docs/process/test-catalog.md Part 9.5.

These pin the Phase 1.1 patch's contracts:

- TC-LLM-01..02 — alias resolution at the assembler boundary.
- TC-LLM-03..04 — `build_llm` error paths.
- TC-LLM-05 — OpenCode catalog discovery shape.
- TC-LLM-06 — OpenRouter implements both Protocols + metadata lookup.
- TC-LLM-09 — registry duplicate-registration guard.

HTTP is mocked via :class:`httpx.MockTransport` so no real network
traffic happens in tests.

CLI-driven integration tests (TC-LLM-07, 08, 10) live in
:mod:`tests.integration.test_llm_cli` since they exercise the
``kolsch connect`` flow end-to-end against a temp platform root.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from assets.assembler import pre_resolve_model
from errors import (
    ConfigInvalid,
    ConfigUnknownAlias,
    LlmClientInitFailed,
    LlmConnectorDuplicate,
    LlmConnectorUnknown,
    LlmDiscoveryFailed,
    LlmProviderNotConfigured,
    LlmSecretMissing,
    SecretNotFound,
)
from llm import (
    MetadataSourceConnector,
    ProviderConfig,
    ProviderConnector,
    build_llm,
    registry,
)
from llm.protocol import Credentials, is_metadata_source
from llm.providers.opencode import OpenCodeConnector
from llm.providers.openrouter import OpenRouterConnector
from schemas.config import ModelSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    # httpx.MockTransport accepts either a sync or async callable, but
    # mypy can't unify ``Callable[..., R | Awaitable[R]]`` with httpx's
    # ``Callable[..., R] | Callable[..., Coroutine[..., R]]``. Every
    # test below uses the sync shape — pin the alias to that.
    SyncHandler = Callable[[httpx.Request], httpx.Response]


# ===========================================================================
# Helpers
# ===========================================================================


def _mock_transport(handler: SyncHandler) -> httpx.MockTransport:
    """Build a :class:`httpx.MockTransport` that runs `handler`."""
    return httpx.MockTransport(handler)


def _provider_config(
    *,
    name: str = "opencode",
    type_name: str = "opencode",
    api_key_secret: str | None = "OPENCODE_API_KEY",
    base_url: str | None = "https://opencode.example.com/v1",
    timeout_seconds: int | None = 30,
    extras: dict[str, Any] | None = None,
    models: list[dict[str, Any]] | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        type=type_name,
        api_key_secret=api_key_secret,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        extras=extras or {},
        models=models or [],
    )


class _StubSecrets:
    """Tiny in-memory SecretsProvider for build_llm tests."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = values or {}

    async def resolve(self, name: str) -> str:
        if name not in self._values:
            raise SecretNotFound(name=name)
        return self._values[name]

    async def exists(self, name: str) -> bool:
        return name in self._values

    async def list_names(self) -> list[str]:
        return list(self._values)


# ===========================================================================
# TC-LLM-01 — alias resolves to canonical
# ===========================================================================


def test_tc_llm_01_alias_resolves_to_canonical() -> None:
    partial = {"model": "opus"}
    aliases = {"opus": "anthropic/claude-opus-4-7"}
    out = pre_resolve_model(partial, aliases)
    assert out["model"] == {
        "provider": "anthropic",
        "model": "claude-opus-4-7",
    }


def test_tc_llm_01_compact_string_resolves_to_canonical() -> None:
    out = pre_resolve_model({"model": "opencode/gpt-oss-120b"}, aliases={})
    assert out["model"] == {"provider": "opencode", "model": "gpt-oss-120b"}


def test_tc_llm_01_object_passes_through() -> None:
    obj = {"provider": "anthropic", "model": "claude-haiku-4-5"}
    out = pre_resolve_model({"model": dict(obj)}, aliases={"x": "y/z"})
    assert out["model"] == obj


def test_tc_llm_01_fallback_entries_also_normalized() -> None:
    partial = {
        "model": {
            "provider": "anthropic",
            "model": "claude-opus-4-7",
            "fallback": ["cheap", {"provider": "openai", "model": "gpt-4o"}],
        }
    }
    aliases = {"cheap": "opencode/qwen2.5-coder-32b"}
    out = pre_resolve_model(partial, aliases)
    assert out["model"]["fallback"] == [
        {"provider": "opencode", "model": "qwen2.5-coder-32b"},
        {"provider": "openai", "model": "gpt-4o"},
    ]


# ===========================================================================
# TC-LLM-02 — unknown alias raises config.unknown_alias
# ===========================================================================


def test_tc_llm_02_unknown_alias_raises() -> None:
    with pytest.raises(ConfigUnknownAlias) as exc_info:
        pre_resolve_model({"model": "mystery"}, aliases={"opus": "a/b"})
    err = exc_info.value
    assert err.code == "config.unknown_alias"
    assert err.detail["alias"] == "mystery"
    assert err.detail["available_aliases"] == ["opus"]


def test_tc_llm_02_alias_chain_forbidden() -> None:
    # Aliases don't chain — even when the target IS another alias-shaped
    # bare label, surface a ConfigInvalid rather than recursing.
    with pytest.raises(ConfigInvalid):
        pre_resolve_model({"model": "opus"}, aliases={"opus": "smart"})


def test_tc_llm_02_malformed_compact_string_raises() -> None:
    with pytest.raises(ConfigInvalid):
        pre_resolve_model({"model": "anthropic/"}, aliases={})


# ===========================================================================
# TC-LLM-03 — unconfigured provider raises llm.provider_not_configured
# ===========================================================================


async def test_tc_llm_03_unconfigured_provider_raises() -> None:
    spec = ModelSpec(provider="anthropic", model="claude-opus-4-7")
    secrets = _StubSecrets()
    with pytest.raises(LlmProviderNotConfigured) as exc_info:
        await build_llm(
            spec,
            provider_config=None,
            secrets=secrets,
            configured_providers=["opencode", "openrouter"],
        )
    err = exc_info.value
    assert err.code == "llm.provider_not_configured"
    assert err.detail["provider"] == "anthropic"
    assert err.detail["configured_providers"] == ["opencode", "openrouter"]


# ===========================================================================
# TC-LLM-04 — missing API key raises llm.secret_missing with cause
# ===========================================================================


async def test_tc_llm_04_missing_secret_raises_with_cause() -> None:
    spec = ModelSpec(provider="opencode", model="gpt-oss-120b")
    config = _provider_config(api_key_secret="MISSING_KEY")
    secrets = _StubSecrets()  # empty store

    with pytest.raises(LlmSecretMissing) as exc_info:
        await build_llm(spec, config, secrets)
    err = exc_info.value
    assert err.code == "llm.secret_missing"
    assert err.detail["secret_name"] == "MISSING_KEY"
    assert err.detail["provider"] == "opencode"
    # Cause chain — the underlying SecretNotFound is set as __cause__.
    assert isinstance(err.__cause__, SecretNotFound)
    assert err.__cause__.detail["name"] == "MISSING_KEY"


async def test_build_llm_unknown_connector_type_raises() -> None:
    spec = ModelSpec(provider="exotic", model="m")
    config = _provider_config(type_name="exotic_provider", api_key_secret=None)
    with pytest.raises(LlmConnectorUnknown) as exc_info:
        await build_llm(spec, config, _StubSecrets())
    assert exc_info.value.detail["type"] == "exotic_provider"


async def test_build_llm_connector_failure_wraps_as_client_init() -> None:
    """A connector that raises during `build_llm` is wrapped as
    :class:`LlmClientInitFailed`. The original error is preserved on
    ``__cause__``."""
    registry._clear_for_tests()
    try:

        class _FailingConnector:
            type_name = "failing"

            def auth_prompt_spec(self) -> object:  # pragma: no cover - unused
                raise NotImplementedError

            async def validate_credentials(
                self, credentials: object, base_url: object
            ) -> object:  # pragma: no cover - unused
                raise NotImplementedError

            async def discover_models(
                self, credentials: object, base_url: object
            ) -> object:  # pragma: no cover - unused
                raise NotImplementedError

            async def build_llm(
                self, model_id: str, provider_config: object, api_key: object
            ) -> object:
                msg = "boom"
                raise RuntimeError(msg)

        registry.register("failing", _FailingConnector)  # type: ignore[arg-type]
        spec = ModelSpec(provider="failing", model="m")
        config = _provider_config(
            type_name="failing", api_key_secret=None, base_url=None
        )
        with pytest.raises(LlmClientInitFailed) as exc_info:
            await build_llm(spec, config, _StubSecrets())
        assert "RuntimeError" in exc_info.value.detail["reason"]
        assert isinstance(exc_info.value.__cause__, RuntimeError)
    finally:
        registry._clear_for_tests()
        # Restore production registrations for subsequent tests — direct
        # `import llm.providers` is a no-op when the module is already
        # cached, so re-register explicitly.
        registry.register(OpenCodeConnector.type_name, OpenCodeConnector)
        registry.register(OpenRouterConnector.type_name, OpenRouterConnector)


# ===========================================================================
# TC-LLM-05 — OpenCodeConnector.discover_models returns expected shape
# ===========================================================================


async def test_tc_llm_05_opencode_discover_models_shape() -> None:
    fixture = {
        "object": "list",
        "data": [
            {"id": "gpt-oss-120b", "object": "model"},
            {"id": "qwen2.5-coder-32b", "object": "model"},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        assert request.headers["Authorization"] == "Bearer sk-test"
        return httpx.Response(200, json=fixture)

    connector = OpenCodeConnector(transport=_mock_transport(handler))
    creds = Credentials(values={"api_key": "sk-test"})
    entries = await connector.discover_models(creds, "https://opencode.example.com/v1")
    assert len(entries) == 2
    assert entries[0].id == "gpt-oss-120b"
    # All parameter fields stay None — OpenCode doesn't expose them.
    for entry in entries:
        assert entry.context_length is None
        assert entry.supports_tools is None
        assert entry.supports_streaming is None
        assert entry.pricing is None
        assert entry.has_missing_fields()


async def test_opencode_validate_credentials_ok() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    connector = OpenCodeConnector(transport=_mock_transport(handler))
    creds = Credentials(values={"api_key": "sk-test"})
    result = await connector.validate_credentials(creds, "https://example/v1")
    assert result.ok is True


async def test_opencode_validate_credentials_rejected_on_401() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    connector = OpenCodeConnector(transport=_mock_transport(handler))
    result = await connector.validate_credentials(
        Credentials(values={"api_key": "bad"}), "https://example/v1"
    )
    assert result.ok is False
    assert "401" in (result.reason or "")


async def test_opencode_discover_models_network_failure_raises() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        msg = "down"
        raise httpx.ConnectError(msg)

    connector = OpenCodeConnector(transport=_mock_transport(handler))
    with pytest.raises(LlmDiscoveryFailed) as exc_info:
        await connector.discover_models(
            Credentials(values={"api_key": "k"}),
            "https://example/v1",
        )
    assert exc_info.value.detail["provider"] == "opencode"


# ===========================================================================
# TC-LLM-06 — OpenRouterConnector implements both Protocols + metadata lookup
# ===========================================================================


def test_tc_llm_06_openrouter_implements_both_protocols() -> None:
    instance = OpenRouterConnector()
    # `Protocol` with no @runtime_checkable can't be used in isinstance;
    # the spec's intent is structural conformance — check the methods.
    assert hasattr(instance, "auth_prompt_spec")
    assert hasattr(instance, "validate_credentials")
    assert hasattr(instance, "discover_models")
    assert hasattr(instance, "build_llm")
    assert hasattr(instance, "lookup_model_metadata")
    # The runtime helper used by the registry's metadata-source filter
    # picks it out.
    assert is_metadata_source(instance) is True
    # OpenCode is NOT a metadata source.
    assert is_metadata_source(OpenCodeConnector()) is False


async def test_tc_llm_06_openrouter_lookup_returns_populated_metadata() -> None:
    catalog = {
        "data": [
            {
                "id": "anthropic/claude-opus-4-7",
                "context_length": 200000,
                "supported_parameters": ["tools", "stream"],
                "pricing": {
                    "prompt": "0.000015",
                    "completion": "0.000075",
                },
            },
            {
                "id": "openai/gpt-4o",
                "context_length": 128000,
                "supported_parameters": ["tools"],
                "pricing": {
                    "prompt": "0.0000025",
                    "completion": "0.00001",
                },
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json=catalog)

    connector = OpenRouterConnector(transport=_mock_transport(handler))
    metadata = await connector.lookup_model_metadata("anthropic", "claude-opus-4-7")
    assert metadata is not None
    assert metadata.context_length == 200000
    assert metadata.supports_tools is True
    assert metadata.supports_streaming is True
    assert metadata.pricing is not None
    assert metadata.pricing.prompt_per_million == pytest.approx(15.0)
    assert metadata.pricing.completion_per_million == pytest.approx(75.0)

    # A miss returns None, not an exception.
    miss = await connector.lookup_model_metadata("opencode", "gpt-oss-120b")
    assert miss is None


async def test_openrouter_discover_models_populates_metadata() -> None:
    catalog = {
        "data": [
            {
                "id": "anthropic/claude-haiku-4-5",
                "context_length": 200000,
                "supported_parameters": ["tools", "stream"],
                "pricing": {
                    "prompt": "0.000001",
                    "completion": "0.000005",
                },
            }
        ]
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=catalog)

    connector = OpenRouterConnector(transport=_mock_transport(handler))
    entries = await connector.discover_models(
        Credentials(values={"api_key": "sk-or"}), None
    )
    assert len(entries) == 1
    assert entries[0].context_length == 200000
    assert entries[0].supports_tools is True


async def test_openrouter_validate_credentials_surfaces_credits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/auth/key" in str(request.url)
        return httpx.Response(
            200,
            json={"data": {"limit": 5.0, "usage": 1.25, "label": "free"}},
        )

    connector = OpenRouterConnector(transport=_mock_transport(handler))
    result = await connector.validate_credentials(
        Credentials(values={"api_key": "sk-or"}), None
    )
    assert result.ok is True
    assert result.metadata is not None
    assert result.metadata["credits_remaining"] == pytest.approx(3.75)


# ===========================================================================
# TC-LLM-09 — registry rejects duplicate `register` calls
# ===========================================================================


def test_tc_llm_09_registry_rejects_duplicate() -> None:
    registry._clear_for_tests()
    try:

        class _A:
            type_name = "dupe"

        class _B:
            type_name = "dupe"

        registry.register("dupe", _A)  # type: ignore[arg-type]
        with pytest.raises(LlmConnectorDuplicate) as exc_info:
            registry.register("dupe", _B)  # type: ignore[arg-type]
        assert exc_info.value.detail["type"] == "dupe"
    finally:
        registry._clear_for_tests()
        registry.register(OpenCodeConnector.type_name, OpenCodeConnector)
        registry.register(OpenRouterConnector.type_name, OpenRouterConnector)


# ===========================================================================
# Registry — metadata-source filter
# ===========================================================================


def test_connected_metadata_sources_picks_openrouter() -> None:
    configs = {
        "opencode": _provider_config(name="opencode", type_name="opencode"),
        "openrouter": _provider_config(
            name="openrouter", type_name="openrouter", api_key_secret="OR_KEY"
        ),
    }
    sources = registry.connected_metadata_sources(configs)
    assert len(sources) == 1
    assert sources[0].type_name == "openrouter"


def test_connected_metadata_sources_empty_when_no_config() -> None:
    assert registry.connected_metadata_sources({}) == []
    assert registry.connected_metadata_sources(None) == []


def test_connected_metadata_sources_skips_unknown_type() -> None:
    configs = {
        "exotic": _provider_config(name="exotic", type_name="not_registered"),
    }
    assert registry.connected_metadata_sources(configs) == []


# ===========================================================================
# Smoke — Protocol structural conformance for the two reference connectors
# ===========================================================================


def test_opencode_satisfies_provider_connector_shape() -> None:
    instance = OpenCodeConnector()
    for method in (
        "auth_prompt_spec",
        "validate_credentials",
        "discover_models",
        "build_llm",
    ):
        assert callable(getattr(instance, method))


def test_protocol_imports_are_callable_types() -> None:
    # Sanity that both Protocols are importable + named correctly.
    assert ProviderConnector.__name__ == "ProviderConnector"
    assert MetadataSourceConnector.__name__ == "MetadataSourceConnector"


# ===========================================================================
# Smoke — assembler pre_resolve_model leaves non-model partials alone
# ===========================================================================


def test_pre_resolve_model_no_model_field_passthrough() -> None:
    partial = {"prompt": "hi", "subsystems": {}}
    out = pre_resolve_model(partial, aliases={"x": "y/z"})
    assert out is partial or out == partial


def test_pre_resolve_model_none_aliases_treated_as_empty() -> None:
    with pytest.raises(ConfigUnknownAlias):
        pre_resolve_model({"model": "bare_alias"}, aliases=None)


# ===========================================================================
# Defensive — confirm protocol json payload parsing tolerates oddities
# ===========================================================================


async def test_opencode_discover_models_tolerates_extra_keys() -> None:
    fixture = {
        "data": [
            {"id": "m1", "extra": "ignored"},
            {"id": "", "object": "model"},  # empty id is skipped
            "not-a-dict",  # non-dict entry is skipped
            {"object": "model"},  # missing id is skipped
        ]
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture)

    connector = OpenCodeConnector(transport=_mock_transport(handler))
    entries = await connector.discover_models(
        Credentials(values={"api_key": "k"}), "https://example/v1"
    )
    assert [e.id for e in entries] == ["m1"]


async def test_openrouter_lookup_returns_none_on_fetch_failure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    connector = OpenRouterConnector(transport=_mock_transport(handler))
    # First call raises LlmDiscoveryFailed because we couldn't fetch.
    with pytest.raises(LlmDiscoveryFailed):
        await connector.lookup_model_metadata("anthropic", "claude-opus-4-7")


async def test_openrouter_parse_entry_tolerates_missing_fields() -> None:
    """_parse_entry handles bare ids, missing context, unparseable pricing,
    non-list supported_parameters — all without raising."""
    fixture = {
        "data": [
            # Bare id only
            {"id": "minimal/model"},
            # Empty id is skipped
            {"id": ""},
            # Non-string id is skipped
            {"id": 42},
            # context_length not an int → None
            {"id": "weird/model", "context_length": "huge"},
            # supported_parameters not a list → None flags
            {"id": "scalar/model", "supported_parameters": "tools"},
            # Pricing fields not numeric → None
            {
                "id": "no_price/model",
                "pricing": {"prompt": None, "completion": None},
            },
            # Pricing dict with bad string → None
            {
                "id": "bad_price/model",
                "pricing": {"prompt": "not-a-number", "completion": "0.0"},
            },
            # Pricing not a dict → None
            {"id": "scalar_price/model", "pricing": "0.0"},
        ]
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture)

    connector = OpenRouterConnector(transport=_mock_transport(handler))
    entries = await connector.discover_models(Credentials(), None)
    ids = {e.id for e in entries}
    assert "minimal/model" in ids
    assert "" not in ids  # empty id skipped
    bad_price = next(e for e in entries if e.id == "bad_price/model")
    assert bad_price.pricing is None
    no_price = next(e for e in entries if e.id == "no_price/model")
    assert no_price.pricing is None
    scalar_price = next(e for e in entries if e.id == "scalar_price/model")
    assert scalar_price.pricing is None
    weird = next(e for e in entries if e.id == "weird/model")
    assert weird.context_length is None
    scalar_supported = next(e for e in entries if e.id == "scalar/model")
    assert scalar_supported.supports_tools is None


async def test_openrouter_discover_models_rejects_non_list_data() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": "not a list"})

    connector = OpenRouterConnector(transport=_mock_transport(handler))
    with pytest.raises(LlmDiscoveryFailed):
        await connector.discover_models(Credentials(), None)


async def test_openrouter_validate_credentials_network_failure_returns_ok_false() -> (
    None
):
    def handler(_: httpx.Request) -> httpx.Response:
        msg = "boom"
        raise httpx.ConnectError(msg)

    connector = OpenRouterConnector(transport=_mock_transport(handler))
    result = await connector.validate_credentials(Credentials(), None)
    assert result.ok is False
    assert "network error" in (result.reason or "")


async def test_openrouter_validate_credentials_no_data_no_credits() -> None:
    """When /auth/key returns 200 but the body has no usable `data`,
    metadata stays None (credits couldn't be computed)."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": "not-a-dict"})

    connector = OpenRouterConnector(transport=_mock_transport(handler))
    result = await connector.validate_credentials(Credentials(), None)
    assert result.ok is True
    assert result.metadata is None


async def test_openrouter_compute_credits_handles_missing_keys() -> None:
    """`limit`/`usage` of unexpected types → no credits_remaining."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"limit": None, "usage": 5.0}},
        )

    connector = OpenRouterConnector(transport=_mock_transport(handler))
    result = await connector.validate_credentials(Credentials(), None)
    assert result.ok is True
    assert result.metadata is None


async def test_openrouter_build_llm_emits_litellm_prefixed_model() -> None:
    """Smoke: build_llm produces a crewai.LLM with model='openrouter/...'."""
    connector = OpenRouterConnector()
    config = ProviderConfig(
        name="openrouter",
        type="openrouter",
        api_key_secret="OPENROUTER_API_KEY",
        base_url=None,
        timeout_seconds=60,
        extras={},
        models=[],
    )
    llm = await connector.build_llm("anthropic/claude-opus-4-7", config, "sk-x")
    # crewai.LLM normalises `model=` into (provider, model) — verify both.
    assert llm.provider == "openrouter"
    assert llm.model == "anthropic/claude-opus-4-7"


async def test_opencode_build_llm_emits_hosted_vllm_prefixed_model() -> None:
    """TC-LLM-05 route assertion — updated post bug-litellm-model-routing.md
    (H1+H2). Pre-fix the connector emitted ``openai/<model_id>`` which
    CrewAI 1.14.6 rejects for non-canonical model names without the
    LiteLLM extra installed. The connector now uses ``hosted_vllm/``
    — CrewAI's idiom for an OpenAI-compatible endpoint serving
    arbitrary models. See the OpenCode connector module docstring for
    the routing-choice rationale."""
    connector = OpenCodeConnector()
    config = ProviderConfig(
        name="opencode",
        type="opencode",
        api_key_secret="OPENCODE_API_KEY",
        base_url="https://opencode.example.com/v1",
        timeout_seconds=None,
        extras={},
        models=[],
    )
    llm = await connector.build_llm("gpt-oss-120b", config, "sk-y")
    assert llm.provider == "hosted_vllm"
    assert llm.model == "gpt-oss-120b"


async def test_openrouter_validate_credentials_handles_json_failure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    connector = OpenRouterConnector(transport=_mock_transport(handler))
    result = await connector.validate_credentials(
        Credentials(values={"api_key": "k"}), None
    )
    # ok stays True (200 means key is valid) but metadata is empty.
    assert result.ok is True
    assert result.metadata is None


# ===========================================================================
# Helpers reused below — keep test data co-located with the test file.
# ===========================================================================


def _sample_catalog_payload() -> str:
    """Return a small `/api/v1/models` payload as a string."""
    return json.dumps(
        {
            "data": [
                {
                    "id": "openai/gpt-4o",
                    "context_length": 128000,
                    "supported_parameters": ["tools"],
                    "pricing": {
                        "prompt": "0.0000025",
                        "completion": "0.00001",
                    },
                }
            ]
        }
    )
