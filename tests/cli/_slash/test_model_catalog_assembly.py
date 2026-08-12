"""spec-132 Step 2 — tests for `cli._model_catalog.assemble_model_choices`.

Pure data-assembly function: no widget, no `/model` wiring (that's Step 3+).
Exercises the injectable `connector_factory` / `credentials_resolver` seam
(mirroring `cli.session_picker`'s chooser seam) so these tests stub
connectors/credentials directly — no real network calls, no real secrets.

Covers:
- a mix of providers with entries including one `context_length=None`
  (opencode-shaped) and one with a real context length
- one provider's discovery deliberately raising an exception, asserting
  the OTHER providers' entries still come back (not swallowed by the one
  failure)
- zero configured providers returns an empty list without error
- a provider whose secret can't be resolved (`SecretNotFound`) is skipped
  gracefully, same as a discovery failure
- the default credentials resolver mirrors `llm_commands.cmd_refresh`'s
  pattern: resolves `api_key_secret` via `ctx.state.runtime.secrets`,
  falls back to an empty api_key when `api_key_secret` is unset (the
  `claude_code`-shaped case)
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest

from cli._model_catalog import ModelChoice, assemble_model_choices
from cli._slash import ChatSessionState, SlashContext
from errors import SecretNotFound
from llm import Credentials, ProviderConfig
from llm.protocol import ModelEntry
from schemas import AgentId, IOSpec, ModelSpec, ResolvedAgentConfig, SessionId, ThreadId

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Fixtures — a fake runtime carrying only what assemble_model_choices reads
# (llm_providers_config, secrets), per the injectable-seam pattern.
# ---------------------------------------------------------------------------


class _FakeSecrets:
    """Minimal `SecretsProvider` stand-in — resolves from an in-memory dict."""

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


@dataclass
class _FakeRuntime:
    """Stand-in for `PlatformRuntime` carrying only the fields this module reads."""

    llm_providers_config: dict[str, ProviderConfig] = field(default_factory=dict)
    secrets: _FakeSecrets = field(default_factory=_FakeSecrets)


def _config() -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        agent_id=AgentId("ag_x"),
        name="x",
        description="d",
        model=ModelSpec(provider="openrouter", model="anthropic/claude-3"),
        prompt="You are an agent.",
        io=IOSpec(mode="conversational"),
    )


def _state(
    *,
    llm_providers_config: dict[str, ProviderConfig] | None = None,
    secrets: _FakeSecrets | None = None,
) -> ChatSessionState:
    runtime = _FakeRuntime(
        llm_providers_config=llm_providers_config or {},
        secrets=secrets or _FakeSecrets(),
    )
    return ChatSessionState(
        runtime=cast("Any", runtime),
        config=_config(),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
    )


def _ctx(state: ChatSessionState) -> SlashContext:
    return SlashContext(stdout=io.StringIO(), state=state)


def _provider_config(
    name: str,
    *,
    type_: str | None = None,
    api_key_secret: str | None = None,
    base_url: str | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        type=type_ or name,
        api_key_secret=api_key_secret,
        base_url=base_url,
        timeout_seconds=None,
    )


# ---------------------------------------------------------------------------
# Stub connectors — injected via connector_factory, never touch the network.
# ---------------------------------------------------------------------------


class _StubConnector:
    """A connector whose `discover_models` returns a canned list or raises."""

    type_name = "stub"

    def __init__(
        self,
        entries: list[ModelEntry] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._entries = entries or []
        self._error = error

    async def discover_models(
        self, credentials: Credentials, base_url: str | None
    ) -> list[ModelEntry]:
        _ = credentials, base_url
        if self._error is not None:
            raise self._error
        return self._entries


def _make_factory(
    by_type: dict[str, _StubConnector],
) -> Callable[[str], _StubConnector]:
    def factory(type_name: str) -> _StubConnector:
        return by_type[type_name]

    return factory


async def _stub_credentials_resolver(provider_config: ProviderConfig) -> Credentials:
    _ = provider_config
    return Credentials(values={"api_key": "fake", "base_url": ""})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_configured_providers_returns_empty_list() -> None:
    """No `llm_providers_config` entries → empty list, no error, no calls."""
    state = _state(llm_providers_config={})
    ctx = _ctx(state)

    result = await assemble_model_choices(
        ctx,
        connector_factory=_make_factory({}),
        credentials_resolver=_stub_credentials_resolver,
    )

    assert result == []


@pytest.mark.asyncio
async def test_mix_of_providers_pairs_entries_with_provider_name() -> None:
    """Two configured providers' entries come back paired with their
    provider's dictionary-key name; a `context_length=None` entry
    (opencode-shaped) is distinguishable from a real context length via
    `entry.context_length is None`."""
    opencode_entries = [ModelEntry(id="gpt-oss-120b")]  # context_length=None
    openrouter_entries = [ModelEntry(id="anthropic/claude-3", context_length=200_000)]
    state = _state(
        llm_providers_config={
            "opencode": _provider_config("opencode"),
            "openrouter": _provider_config("openrouter"),
        }
    )
    ctx = _ctx(state)
    factory = _make_factory(
        {
            "opencode": _StubConnector(opencode_entries),
            "openrouter": _StubConnector(openrouter_entries),
        }
    )

    result = await assemble_model_choices(
        ctx, connector_factory=factory, credentials_resolver=_stub_credentials_resolver
    )

    assert result == [
        ModelChoice(provider="opencode", entry=opencode_entries[0]),
        ModelChoice(provider="openrouter", entry=openrouter_entries[0]),
    ]
    # Directly checkable placeholder-marker distinction.
    assert result[0].entry.context_length is None
    assert result[1].entry.context_length == 200_000


@pytest.mark.asyncio
async def test_one_provider_discovery_failure_does_not_sink_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One provider's `discover_models` raising doesn't swallow the other
    providers' entries — they still come back correctly."""
    from errors import LlmDiscoveryFailed

    good_entries = [ModelEntry(id="anthropic/claude-3", context_length=200_000)]
    state = _state(
        llm_providers_config={
            "broken": _provider_config("broken"),
            "openrouter": _provider_config("openrouter"),
        }
    )
    ctx = _ctx(state)
    factory = _make_factory(
        {
            "broken": _StubConnector(
                error=LlmDiscoveryFailed(provider="broken", reason="network down")
            ),
            "openrouter": _StubConnector(good_entries),
        }
    )

    with caplog.at_level(logging.WARNING, logger="cli._model_catalog"):
        result = await assemble_model_choices(
            ctx,
            connector_factory=factory,
            credentials_resolver=_stub_credentials_resolver,
        )

    assert result == [ModelChoice(provider="openrouter", entry=good_entries[0])]
    assert any("broken" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_unexpected_exception_from_one_provider_does_not_sink_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-`PlatformError` exception (e.g. a raw bug in a connector) is
    still isolated per-provider — the assembly as a whole must not crash."""
    good_entries = [ModelEntry(id="gpt-oss-120b")]
    state = _state(
        llm_providers_config={
            "broken": _provider_config("broken"),
            "opencode": _provider_config("opencode"),
        }
    )
    ctx = _ctx(state)
    factory = _make_factory(
        {
            "broken": _StubConnector(error=RuntimeError("boom")),
            "opencode": _StubConnector(good_entries),
        }
    )

    with caplog.at_level(logging.ERROR, logger="cli._model_catalog"):
        result = await assemble_model_choices(
            ctx,
            connector_factory=factory,
            credentials_resolver=_stub_credentials_resolver,
        )

    assert result == [ModelChoice(provider="opencode", entry=good_entries[0])]
    assert any("broken" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_unresolvable_secret_skips_that_provider_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider whose `api_key_secret` isn't present in secrets is
    skipped (via the real credential-resolution path, not the stub
    resolver) while a sibling provider still comes back."""
    good_entries = [ModelEntry(id="gpt-oss-120b")]
    state = _state(
        llm_providers_config={
            "openrouter": _provider_config(
                "openrouter", api_key_secret="OPENROUTER_API_KEY"
            ),
            "opencode": _provider_config("opencode"),
        },
        secrets=_FakeSecrets({}),  # OPENROUTER_API_KEY is NOT present
    )
    ctx = _ctx(state)
    factory = _make_factory(
        {
            "openrouter": _StubConnector([ModelEntry(id="should-not-appear")]),
            "opencode": _StubConnector(good_entries),
        }
    )

    with caplog.at_level(logging.WARNING, logger="cli._model_catalog"):
        # credentials_resolver=None exercises the REAL default resolver,
        # which reads ctx.state.runtime.secrets.
        result = await assemble_model_choices(ctx, connector_factory=factory)

    assert result == [ModelChoice(provider="opencode", entry=good_entries[0])]
    assert any("openrouter" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_default_credentials_resolver_resolves_configured_secret() -> None:
    """The default resolver (no `credentials_resolver` override) pulls the
    api key from `ctx.state.runtime.secrets` via `api_key_secret` and
    threads `base_url` through — mirrors `cmd_refresh`'s pattern."""
    captured: dict[str, Any] = {}

    class _CapturingConnector(_StubConnector):
        async def discover_models(
            self, credentials: Credentials, base_url: str | None
        ) -> list[ModelEntry]:
            captured["creds"] = credentials
            captured["base_url"] = base_url
            return await super().discover_models(credentials, base_url)

    state = _state(
        llm_providers_config={
            "openrouter": _provider_config(
                "openrouter",
                api_key_secret="OPENROUTER_API_KEY",
                base_url="https://openrouter.ai/api/v1",
            ),
        },
        secrets=_FakeSecrets({"OPENROUTER_API_KEY": "sk-real-secret"}),
    )
    ctx = _ctx(state)
    factory = _make_factory({"openrouter": _CapturingConnector([ModelEntry(id="m")])})

    result = await assemble_model_choices(ctx, connector_factory=factory)

    assert len(result) == 1
    assert captured["creds"].values["api_key"] == "sk-real-secret"
    assert captured["creds"].values["base_url"] == "https://openrouter.ai/api/v1"
    assert captured["base_url"] == "https://openrouter.ai/api/v1"


@pytest.mark.asyncio
async def test_default_credentials_resolver_handles_no_api_key_secret() -> None:
    """`claude_code`-shaped provider: `api_key_secret` is unset, so the
    default resolver never calls `secrets.resolve` and passes an empty
    api_key — no crash, no dangling lookup."""

    class _RecordingSecrets(_FakeSecrets):
        def __init__(self) -> None:
            super().__init__({})
            self.calls: list[str] = []

        async def resolve(self, name: str) -> str:
            self.calls.append(name)
            return await super().resolve(name)

    secrets = _RecordingSecrets()
    state = _state(
        llm_providers_config={"claude_code": _provider_config("claude_code")},
        secrets=secrets,
    )
    ctx = _ctx(state)
    factory = _make_factory(
        {
            "claude_code": _StubConnector(
                [ModelEntry(id="sonnet", context_length=200_000)]
            )
        }
    )

    result = await assemble_model_choices(ctx, connector_factory=factory)

    assert result == [
        ModelChoice(
            provider="claude_code",
            entry=ModelEntry(id="sonnet", context_length=200_000),
        )
    ]
    assert secrets.calls == []  # never resolved — api_key_secret was unset
