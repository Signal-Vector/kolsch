"""spec-132 Step 6 — tests for `cli._model_catalog.assemble_catalog`.

`assemble_catalog` is the failure-surfacing wrapper Step 6 added
alongside `assemble_model_choices` (Step 2, unchanged — see
`test_model_catalog_assembly.py`): same per-provider
resolve-construct-discover sequence, same failure isolation (log +
skip), but it ALSO returns a `ProviderFailure` for every configured
provider whose discovery didn't complete, so a caller (the picker's
loading screen, via `_slash.py`'s `_open_model_picker_from_slash`) can
show *why* a provider is missing instead of only logging it.

Mirrors `test_model_catalog_assembly.py`'s fixtures (stub
connectors/credentials via the injectable seam — no real network
calls, no real secrets).

Covers:
- all providers succeed → `choices` populated, `failures == []`.
- one provider fails, one succeeds → the surviving provider's entries
  come back AND the failure is captured as a `ProviderFailure` with a
  readable `reason` (not just logged).
- every configured provider fails → `choices == []`,
  `len(failures) == N` (the "all failed" case the picker's inline note
  handles — Step 5 review carry-forward #1).
- `assemble_model_choices`'s return value (already covered
  exhaustively by its own test file) equals `assemble_catalog`'s
  `choices` half for the same inputs — proof the two functions stay
  in lockstep since one now delegates to the other.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest

from cli._model_catalog import (
    ModelChoice,
    assemble_catalog,
    assemble_model_choices,
)
from cli._slash import ChatSessionState, SlashContext
from errors import LlmDiscoveryFailed
from llm import Credentials, ProviderConfig
from llm.protocol import ModelEntry
from schemas import AgentId, IOSpec, ModelSpec, ResolvedAgentConfig, SessionId, ThreadId

if TYPE_CHECKING:
    from collections.abc import Callable


class _FakeSecrets:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = values or {}

    async def resolve(self, name: str) -> str:
        return self._values[name]

    async def exists(self, name: str) -> bool:
        return name in self._values

    async def list_names(self) -> list[str]:
        return list(self._values)


@dataclass
class _FakeRuntime:
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
) -> ChatSessionState:
    runtime = _FakeRuntime(llm_providers_config=llm_providers_config or {})
    return ChatSessionState(
        runtime=cast("Any", runtime),
        config=_config(),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
    )


def _ctx(state: ChatSessionState) -> SlashContext:
    return SlashContext(stdout=io.StringIO(), state=state)


def _provider_config(name: str, *, type_: str | None = None) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        type=type_ or name,
        api_key_secret=None,
        base_url=None,
        timeout_seconds=None,
    )


class _StubConnector:
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
async def test_all_providers_succeed_empty_failures() -> None:
    entries = [ModelEntry(id="anthropic/claude-3", context_length=200_000)]
    state = _state(llm_providers_config={"openrouter": _provider_config("openrouter")})
    ctx = _ctx(state)
    factory = _make_factory({"openrouter": _StubConnector(entries)})

    choices, failures = await assemble_catalog(
        ctx, connector_factory=factory, credentials_resolver=_stub_credentials_resolver
    )

    assert choices == [ModelChoice(provider="openrouter", entry=entries[0])]
    assert failures == []


@pytest.mark.asyncio
async def test_one_provider_fails_captured_as_provider_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The surviving provider's entries still come back, AND the
    failure is captured as data (not just a log line)."""
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
        choices, failures = await assemble_catalog(
            ctx,
            connector_factory=factory,
            credentials_resolver=_stub_credentials_resolver,
        )

    assert choices == [ModelChoice(provider="openrouter", entry=good_entries[0])]
    assert len(failures) == 1
    assert failures[0].provider == "broken"
    assert "network down" in failures[0].reason
    # Still logged too — data surfacing is additive, not a replacement
    # for the existing log line.
    assert any("broken" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_unexpected_exception_captured_with_class_name_fallback() -> None:
    """A non-`PlatformError` (e.g. a raw bug in a connector) still
    yields a `ProviderFailure` — `reason` falls back to the exception
    class name when `str(exc)` would be empty."""
    state = _state(llm_providers_config={"broken": _provider_config("broken")})
    ctx = _ctx(state)
    factory = _make_factory({"broken": _StubConnector(error=RuntimeError())})

    choices, failures = await assemble_catalog(
        ctx, connector_factory=factory, credentials_resolver=_stub_credentials_resolver
    )

    assert choices == []
    assert len(failures) == 1
    assert failures[0].provider == "broken"
    assert failures[0].reason == "RuntimeError"


@pytest.mark.asyncio
async def test_every_configured_provider_fails_all_captured() -> None:
    """N configured, N failed → `choices == []`, `len(failures) == N` —
    the case the picker's inline "no models available" note handles."""
    state = _state(
        llm_providers_config={
            "openrouter": _provider_config("openrouter"),
            "opencode": _provider_config("opencode"),
        }
    )
    ctx = _ctx(state)
    factory = _make_factory(
        {
            "openrouter": _StubConnector(
                error=LlmDiscoveryFailed(provider="openrouter", reason="401")
            ),
            "opencode": _StubConnector(
                error=LlmDiscoveryFailed(provider="opencode", reason="refused")
            ),
        }
    )

    choices, failures = await assemble_catalog(
        ctx, connector_factory=factory, credentials_resolver=_stub_credentials_resolver
    )

    assert choices == []
    assert {f.provider for f in failures} == {"openrouter", "opencode"}


@pytest.mark.asyncio
async def test_zero_configured_providers_empty_choices_empty_failures() -> None:
    state = _state(llm_providers_config={})
    ctx = _ctx(state)

    choices, failures = await assemble_catalog(
        ctx,
        connector_factory=_make_factory({}),
        credentials_resolver=_stub_credentials_resolver,
    )

    assert choices == []
    assert failures == []


@pytest.mark.asyncio
async def test_choices_half_matches_assemble_model_choices_for_same_inputs() -> None:
    """`assemble_model_choices` now delegates to `assemble_catalog` —
    same inputs must produce the identical `choices` list either way."""
    good_entries = [ModelEntry(id="anthropic/claude-3", context_length=200_000)]
    state = _state(
        llm_providers_config={
            "broken": _provider_config("broken"),
            "openrouter": _provider_config("openrouter"),
        }
    )
    factory = _make_factory(
        {
            "broken": _StubConnector(error=RuntimeError("boom")),
            "openrouter": _StubConnector(good_entries),
        }
    )

    choices_only = await assemble_model_choices(
        _ctx(state),
        connector_factory=factory,
        credentials_resolver=_stub_credentials_resolver,
    )
    choices_and_failures, _failures = await assemble_catalog(
        _ctx(state),
        connector_factory=factory,
        credentials_resolver=_stub_credentials_resolver,
    )

    assert choices_only == choices_and_failures
