"""Model-catalog assembly — the data layer under spec-132's `/model` picker.

Pure data assembly, no widget. :func:`assemble_model_choices` walks
`ctx.state.runtime.llm_providers_config` (the session's actually-configured
providers — the same source `apply_run_model_override`
(`cli.main:apply_run_model_override`) already uses to validate `kolsch run
--model`), resolves credentials, and calls `discover_models()` per
provider, pairing each returned `ModelEntry` with its provider's name.

`ModelEntry` (`llm.protocol`) deliberately stays unmodified — no `provider`
field. The pairing lives here, one layer up, in the local
:class:`ModelChoice` dataclass, per spec-132's Scope ("`ModelEntry` stays
unmodified — provider is paired at the UI-assembly layer").

**Credential resolution — required, not hypothetical.** Mirrors
`cli.llm_commands.cmd_refresh`'s pattern (`llm_commands.py:704-719`):
resolve `ProviderConfig.api_key_secret` via the runtime's
`SecretsProvider.resolve()`, build a `Credentials(values={"api_key":
..., "base_url": ...})`, and call `connector.discover_models(credentials,
base_url)` (mirroring `_run_discovery`, `llm_commands.py:340-353`). Unlike
`cmd_refresh` (which stands up its own `Secrets` instance from a raw YAML
payload because it has no full runtime), this module already has
`ctx.state.runtime.secrets` — the same `SecretsProvider` the engine
uses — so no separate construction is needed.

**Per-provider failure isolation.** One misconfigured or unreachable
provider (bad secret, unregistered connector type, network failure) must
not take down the whole picker. Every provider's resolve-construct-discover
sequence is wrapped individually; a failure is logged and that provider
contributes zero entries, but every other configured provider's entries
still come back.

**Testability — injectable seam.** Mirrors `cli.session_picker`'s chooser
seam (`session_picker.py:65-92, 96-100`): production code resolves real
credentials and constructs real connectors via the `llm.registry`, but
:func:`assemble_model_choices` accepts optional `connector_factory` /
`credentials_resolver` callables so tests can stub both without a real
network call or real secrets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from errors import PlatformError
from llm import Credentials
from llm import get as registry_get

if TYPE_CHECKING:
    from collections.abc import Callable

    from cli._slash import SlashContext
    from llm.protocol import ModelEntry, ProviderConfig, ProviderConnector

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ModelChoice — the UI-assembly-layer pairing of a ModelEntry with its
# provider's name. Deliberately NOT added to ModelEntry itself (see the
# module docstring and spec-132's Scope).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelChoice:
    """One selectable row for the model picker: a provider + its model entry.

    `provider` is the `llm_providers.<name>` dictionary key (matches
    `ModelSpec.provider` — the same string `_apply_model` expects), not
    necessarily the connector's `type_name` (they usually match; see
    `ProviderConfig`'s docstring on the `type:` field convention).

    `entry.context_length is None` is the direct, cleanly-checkable signal
    for "no context length" (confirmed: `opencode` entries always have
    this — `llm/providers/opencode.py:261`). The widget layer (Step 3)
    decides how to render that placeholder; this dataclass only carries
    the distinction, it doesn't format it.
    """

    provider: str
    entry: ModelEntry


# ---------------------------------------------------------------------------
# Injectable seam types — mirrors session_picker.py's Chooser seam.
# ---------------------------------------------------------------------------


if TYPE_CHECKING:
    from collections.abc import Awaitable

    ConnectorFactory = Callable[[str], ProviderConnector]
    """`(connector_type) -> ProviderConnector`. Defaults to
    `llm.registry.get`. Raises `LlmConnectorUnknown` for an unregistered
    type — caught per-provider by the assembly loop."""

    CredentialsResolver = Callable[[ProviderConfig], Awaitable[Credentials]]
    """`(provider_config) -> Credentials`. Defaults to
    :func:`_default_resolve_credentials`. Raises `SecretNotFound` (or
    whatever the `SecretsProvider` raises) when the configured
    `api_key_secret` can't be resolved — caught per-provider by the
    assembly loop."""


async def _default_resolve_credentials(
    provider_config: ProviderConfig,
    ctx: SlashContext,
) -> Credentials:
    """Resolve `Credentials` for one provider via the runtime's secrets.

    Mirrors `cli.llm_commands.cmd_refresh`'s resolution
    (`llm_commands.py:704-719`): an absent `api_key_secret` (e.g.
    `claude_code`, which needs no live credential) resolves to an empty
    api_key rather than skipping resolution — `claude_code`'s
    `discover_models` ignores the credentials argument entirely
    (`llm/providers/claude_code.py:560-563`), so this is a no-op for it in
    practice.
    """
    assert ctx.state is not None  # guarded by assemble_model_choices' caller
    api_key: str | None = None
    if provider_config.api_key_secret:
        api_key = await ctx.state.runtime.secrets.resolve(
            provider_config.api_key_secret
        )
    return Credentials(
        values={"api_key": api_key or "", "base_url": provider_config.base_url or ""}
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


async def assemble_model_choices(
    ctx: SlashContext,
    *,
    connector_factory: ConnectorFactory | None = None,
    credentials_resolver: CredentialsResolver | None = None,
) -> list[ModelChoice]:
    """Return every discoverable model across the session's configured providers.

    Iterates `ctx.state.runtime.llm_providers_config` — the session's
    actually-configured providers (the same source
    `apply_run_model_override` uses, `cli.main.py:594-599` —
    NOT `llm.registry.known_types()`, which is the 3 registered connector
    *types*, not what's actually configured). For each provider: resolves
    `Credentials`, constructs the connector, calls `discover_models()`, and
    pairs each returned `ModelEntry` with that provider's dictionary-key
    name into a :class:`ModelChoice`.

    A provider whose connector type is unregistered, whose credentials
    can't be resolved, or whose `discover_models()` call fails is logged
    and skipped — its entries are simply absent from the result, and every
    other configured provider's entries are unaffected. Zero configured
    providers (or every provider failing) returns an empty list, not an
    error.

    Args:
        ctx: The slash-command context; `ctx.state.runtime` supplies both
            `llm_providers_config` and the `secrets` provider used by the
            default credentials resolver. `ctx.state` must not be `None`.
        connector_factory: Injectable seam replacing `llm.registry.get`
            for tests — takes a connector `type` string, returns a
            `ProviderConnector`. `None` uses the real registry.
        credentials_resolver: Injectable seam replacing the real
            secret-resolution + `Credentials` construction for tests —
            takes a `ProviderConfig`, returns a `Credentials`. `None` uses
            :func:`_default_resolve_credentials` (real `Secrets.resolve()`
            calls against `ctx.state.runtime.secrets`).

    Returns:
        A flat list of `ModelChoice`, one per `(provider, ModelEntry)`
        pair, covering every configured provider that discovered
        successfully. Order follows `llm_providers_config`'s iteration
        order (dict insertion order — `platform.yaml`'s `llm_providers`
        key order), then each provider's own `discover_models()` order.
    """
    assert ctx.state is not None  # every current/planned caller sets this
    factory: ConnectorFactory = connector_factory or registry_get
    resolve_credentials: CredentialsResolver = credentials_resolver or (
        lambda provider_config: _default_resolve_credentials(provider_config, ctx)
    )

    choices: list[ModelChoice] = []
    providers = ctx.state.runtime.llm_providers_config
    for provider_name, provider_config in providers.items():
        try:
            connector = factory(provider_config.type)
            credentials = await resolve_credentials(provider_config)
            entries = await connector.discover_models(
                credentials, provider_config.base_url
            )
        except PlatformError as exc:
            _log.warning(
                "model picker: discovery failed for provider %r (type %r): %s",
                provider_name,
                provider_config.type,
                exc,
            )
            continue
        except Exception:  # noqa: BLE001 — one broken provider must not sink the rest
            _log.exception(
                "model picker: unexpected error discovering provider %r (type %r)",
                provider_name,
                provider_config.type,
            )
            continue
        choices.extend(
            ModelChoice(provider=provider_name, entry=entry) for entry in entries
        )
    return choices
