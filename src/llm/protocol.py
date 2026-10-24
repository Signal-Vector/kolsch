"""Protocols and value types for the LLM provider seam.

Two Protocols:

- [`ProviderConnector`][llm.protocol.ProviderConnector] — every
  provider implements this. Four required methods:
  `auth_prompt_spec`, `validate_credentials`, `discover_models`,
  `build_llm` (per-provider LLM construction).
- [`MetadataSourceConnector`][llm.protocol.MetadataSourceConnector] —
  opt-in extension for providers that ALSO serve as a cross-provider
  metadata source. Today only OpenRouter implements this; the engine
  treats one or any number uniformly via
  [`connected_metadata_sources`][llm.registry.connected_metadata_sources].

The supporting dataclasses are all `@dataclass(frozen=True)` — they
cross the auth/discovery seam and must not mutate.
[`ProviderConfig`][llm.protocol.ProviderConfig] also lives here so
providers don't depend on Asset Management directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal, Protocol

from errors import ConfigInvalid

if TYPE_CHECKING:
    from collections.abc import Mapping

    import crewai

    from errors import PlatformError


# ---------------------------------------------------------------------------
# Auth prompt — the CLI walks this; no interactive code in the provider
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptField:
    """One field the CLI prompts the user for during `kolsch connect`.

    The CLI walks an
    [`AuthPromptSpec`][llm.protocol.AuthPromptSpec]'s field list; each
    `PromptField` describes one input (label, kind, optional default
    and choice list). The provider never reads stdin itself.
    """

    name: str
    label: str
    kind: Literal["secret", "url", "string", "choice"]
    default: str | None = None
    choices: list[str] | None = None
    required: bool = True


@dataclass(frozen=True)
class AuthPromptSpec:
    """Declarative auth prompt the CLI walks during `kolsch connect`.

    The provider returns one of these from
    [`ProviderConnector.auth_prompt_spec`][llm.protocol.ProviderConnector.auth_prompt_spec];
    the CLI renders the prompts and collects responses into a
    [`Credentials`][llm.protocol.Credentials] payload it hands back.
    """

    provider_label: str
    intro: str
    fields: list[PromptField]


@dataclass(frozen=True)
class Credentials:
    """The result of prompting the user.

    Opaque to the platform; passed back to the provider for validation
    and (where applicable) LLM construction.
    """

    values: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of a credential validation check.

    Returned by
    [`ProviderConnector.validate_credentials`][llm.protocol.ProviderConnector.validate_credentials].
    `ok=True` means the credentials work; `ok=False` populates
    `reason`. `metadata` carries optional provider-specific info — for
    example, `{"credits_remaining": 1247}` from OpenRouter.
    """

    ok: bool
    reason: str | None = None
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Model metadata — discover_models + lookup_model_metadata return shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPricing:
    """Per-million-token pricing for one model.

    Populated by providers and metadata sources that expose pricing
    (OpenRouter does; most others don't).
    """

    prompt_per_million: float
    completion_per_million: float


@dataclass(frozen=True)
class ModelEntry:
    """One model from a provider's catalog.

    `id` is always populated; the parameter fields are `None` when the
    provider didn't expose them. The CLI's augmentation loop fills the
    gaps from a connected metadata source.
    """

    id: str
    context_length: int | None = None
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    pricing: ModelPricing | None = None

    def has_missing_fields(self) -> bool:
        """Return `True` when any parameter field is unset.

        Used by the augmentation loop in the CLI's connect / refresh
        flow to decide whether a metadata-source lookup is worth
        running.
        """
        return (
            self.context_length is None
            or self.supports_tools is None
            or self.supports_streaming is None
            or self.pricing is None
        )


@dataclass(frozen=True)
class ModelMetadata:
    """Parameter metadata for one model, without an `id`.

    Mirrors the parameter fields of
    [`ModelEntry`][llm.protocol.ModelEntry]. Returned by
    [`MetadataSourceConnector.lookup_model_metadata`][llm.protocol.MetadataSourceConnector.lookup_model_metadata].
    """

    context_length: int | None = None
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    pricing: ModelPricing | None = None


# ---------------------------------------------------------------------------
# Provider config — the platform.yaml entry the engine + CLI pass around
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestPolicyConfig:
    """The validated `llm_providers.<name>.request_policy` override.

    Overrides a connector's declared request policy (bug-144). v1 has
    one field, `session_headers`: the header NAMES that carry the
    session's derived conversation key. `None` keeps the connector's
    default; an empty tuple disables session headers. There is
    deliberately no way to set a header VALUE here — values always come
    from the [`LLMCallContext`][llm.protocol.LLMCallContext], so no
    secret can land in `platform.yaml`.

    Built only by
    [`provider_config_from_yaml`][llm.protocol.provider_config_from_yaml],
    which validates the names.
    """

    session_headers: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ProviderConfig:
    """One entry from `platform.yaml`'s `llm_providers` section.

    Carries the dictionary key as `name`, the canonical fields the
    seam uses (`type`, `api_key_secret`, `base_url`, `timeout_seconds`),
    and provider-specific keys under `extras` — for example,
    OpenRouter's `serves_as_metadata_source`. Providers receive this
    in `build_llm` and may read from `extras` directly.

    `type` defaults to the dictionary key at both construction sites
    (`platform_runtime.runtime` and `cli.llm_commands`) so an entry
    keyed `opencode` with no explicit `type:` resolves to the
    `opencode` connector. Set `type:` explicitly only when the
    connector type differs from the key you chose (e.g. two OpenCode
    endpoints keyed `opencode_local` / `opencode_prod`, both with
    `type: opencode`). See bug-055 and
    `docs/how-to/connect-a-provider.md#the-type-field-convention`.

    `request_policy` is the validated per-provider request-policy
    override (bug-144), or `None` to use the connector's default. Build
    instances from YAML with
    [`provider_config_from_yaml`][llm.protocol.provider_config_from_yaml]
    so both construction sites validate identically.
    """

    name: str
    type: str
    api_key_secret: str | None
    base_url: str | None
    timeout_seconds: int | None
    extras: dict[str, Any] = field(default_factory=dict)
    models: list[dict[str, Any]] = field(default_factory=list)
    request_policy: RequestPolicyConfig | None = None


# Keys `provider_config_from_yaml` maps onto named fields; every other
# key in the entry lands in `ProviderConfig.extras`.
_CANONICAL_KEYS: Final = frozenset(
    {
        "type",
        "api_key_secret",
        "base_url",
        "timeout_seconds",
        "models",
        "request_policy",
    }
)
_REQUEST_POLICY_KEYS: Final = frozenset({"session_headers"})
# RFC 7230 `token` — the legal characters of an HTTP header field name.
_HEADER_TOKEN: Final = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
# Header names a session-header policy may never set: credentials,
# cookies, identity (`User-Agent` is kolsch's own), and framing.
_FORBIDDEN_SESSION_HEADERS: Final = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "user-agent",
        "content-type",
        "content-length",
        "host",
    }
)


def provider_config_from_yaml(name: str, raw: Mapping[str, Any]) -> ProviderConfig:
    """Build a [`ProviderConfig`][llm.protocol.ProviderConfig] from one YAML entry.

    The ONE factory both `platform_runtime.runtime` and
    `cli.llm_commands` use, so `kolsch connect` / `refresh` and the
    runtime see the same config and the same validation (bug-144).

    `type:` defaults to the dictionary key (bug-055). Unknown keys land
    in `extras`. `request_policy` is validated: only `session_headers`
    is accepted, as a list of RFC 7230 header names, none of which may
    be `Authorization`, `Proxy-Authorization`, `Cookie`, `User-Agent`,
    `Content-Type`, `Content-Length` or `Host` (case-insensitive);
    duplicates are dropped case-insensitively (first spelling wins).

    Args:
        name: The `llm_providers.<name>` dictionary key.
        raw: The entry's mapping as loaded from YAML.

    Returns:
        The validated provider config.

    Raises:
        ConfigInvalid: `request_policy` is malformed. Each validation
            error names the provider and the offending path.
    """
    return ProviderConfig(
        name=name,
        type=raw.get("type") or name,
        api_key_secret=raw.get("api_key_secret"),
        base_url=raw.get("base_url"),
        timeout_seconds=raw.get("timeout_seconds"),
        extras={k: v for k, v in raw.items() if k not in _CANONICAL_KEYS},
        models=list(raw.get("models") or []),
        request_policy=_request_policy_from_yaml(name, raw.get("request_policy")),
    )


def _policy_error(name: str, path: str, code: str, message: str) -> dict[str, Any]:
    return {
        "layer": 2,
        "source": "llm.provider_config",
        "path": f"/llm_providers/{name}/request_policy{path}",
        "code": code,
        "message": f"llm_providers.{name}.request_policy: {message}",
    }


def _request_policy_from_yaml(name: str, raw: object) -> RequestPolicyConfig | None:
    """Validate `llm_providers.<name>.request_policy`; `None` when absent."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigInvalid(
            validation_errors=[
                _policy_error(name, "", "not_a_mapping", "must be a mapping")
            ],
            where="llm.protocol.provider_config_from_yaml",
        )
    errors = [
        _policy_error(
            name,
            f"/{key}",
            "unknown_key",
            f"unknown key {key!r}; allowed: {sorted(_REQUEST_POLICY_KEYS)}",
        )
        for key in sorted(map(str, raw))
        if key not in _REQUEST_POLICY_KEYS
    ]
    session_headers: tuple[str, ...] | None = None
    if "session_headers" in raw and raw["session_headers"] is not None:
        headers_raw = raw["session_headers"]
        if not isinstance(headers_raw, list):
            errors.append(
                _policy_error(
                    name,
                    "/session_headers",
                    "not_a_list",
                    "session_headers must be a list of header names",
                )
            )
        else:
            session_headers = _validate_session_headers(name, headers_raw, errors)
    if errors:
        raise ConfigInvalid(
            validation_errors=errors,
            where="llm.protocol.provider_config_from_yaml",
        )
    return RequestPolicyConfig(session_headers=session_headers)


def _validate_session_headers(
    name: str,
    headers_raw: list[Any],
    errors: list[dict[str, Any]],
) -> tuple[str, ...]:
    """Check each header name; append violations to `errors`; dedupe."""
    accepted: list[str] = []
    seen: set[str] = set()
    for index, header in enumerate(headers_raw):
        path = f"/session_headers/{index}"
        if not isinstance(header, str) or not _HEADER_TOKEN.fullmatch(header):
            errors.append(
                _policy_error(
                    name,
                    path,
                    "invalid_header_name",
                    f"{header!r} is not a valid HTTP header name",
                )
            )
            continue
        lowered = header.lower()
        if lowered in _FORBIDDEN_SESSION_HEADERS:
            errors.append(
                _policy_error(
                    name,
                    path,
                    "forbidden_header",
                    f"{header!r} cannot carry the session key "
                    f"(reserved: {sorted(_FORBIDDEN_SESSION_HEADERS)})",
                )
            )
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        accepted.append(header)
    return tuple(accepted)


# ---------------------------------------------------------------------------
# Per-call request context — who an LLM call is made on behalf of
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMCallContext:
    """Per-call request context threaded into every `build_llm`.

    Identifies the conversation an LLM construction serves so a
    connector can forward it as provider session / cache-affinity
    metadata (for example OpenCode Go's `x-opencode-session` header).
    The engine builds one per exchange and reuses it for the pre-flight
    build and every fallback attempt; compaction builds one for the
    session it compacts.

    `conversation_key` is DERIVED, never minted: the v1 rule is
    `conversation_key == session_id` (apply it via
    [`for_session`][llm.protocol.LLMCallContext.for_session]). It is
    therefore stable across resume, new on `/new`, and distinct per
    spawned workflow-node session. It is never a secret. Connectors
    MAY forward it; they MUST NOT invent a substitute when the context
    is absent.

    Attributes:
        session_id: The owning kolsch session id.
        conversation_key: The provider-facing conversation identity,
            derived from `session_id`.
        agent_id: The agent the call is made for, when known.
    """

    session_id: str
    conversation_key: str
    agent_id: str | None = None

    @classmethod
    def for_session(
        cls,
        session_id: str,
        *,
        agent_id: str | None = None,
    ) -> LLMCallContext:
        """Build a context for `session_id` applying the v1 key rule.

        Args:
            session_id: The owning kolsch session id.
            agent_id: The agent the call is made for, when known.

        Returns:
            A context whose `conversation_key` equals `session_id`.
        """
        return cls(
            session_id=session_id,
            conversation_key=session_id,
            agent_id=agent_id,
        )


# ---------------------------------------------------------------------------
# The Protocols
# ---------------------------------------------------------------------------


class ProviderConnector(Protocol):
    """The shape every LLM provider implements.

    One implementation per provider, living in
    `src/llm/providers/<name>.py`. Five required methods —
    `auth_prompt_spec`, `validate_credentials`, `discover_models`,
    `build_llm`, `classify_call_error` — answer the questions
    documented in `src/llm/providers/README.md`.

    Attributes:
        type_name: The string used in `platform.yaml`'s `type` field
            and in the registry. Distinct from the dictionary key (the
            user-facing provider name); usually they match.
    """

    type_name: ClassVar[str]

    def auth_prompt_spec(self) -> AuthPromptSpec:
        """Describe what to ask the user during `kolsch connect`.

        The CLI walks the returned spec; no interactive code lives in
        the provider itself.
        """
        ...

    async def validate_credentials(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> ValidationResult:
        """Verify the credentials work via a tiny live API call."""
        ...

    async def discover_models(
        self,
        credentials: Credentials,
        base_url: str | None,
    ) -> list[ModelEntry]:
        """Return the models available from this provider."""
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
        """Construct a configured `crewai.LLM` for the named model.

        `parameters` carries the agent's
        [`ModelSpec.parameters`][schemas.config.ModelSpec] map — extra
        kwargs like `max_tokens` / `temperature` / `top_p` the caller
        wants forwarded to `crewai.LLM`. Connectors spread these into
        the constructor kwargs but always let the connector's own
        infrastructure keys (`model`, `api_key`, `base_url`, `timeout`)
        win, so a YAML can't accidentally clobber routing.

        `context` is the
        [`LLMCallContext`][llm.protocol.LLMCallContext] for the session
        the LLM serves. Every production caller passes one; `None`
        appears only from test doubles. A connector that needs the
        conversation key (e.g. to send a session header) and receives
        `None` raises a typed `PlatformError` rather than minting an
        id. Connectors that have no use for it accept and ignore it.
        (Protocol change, bug-144: every in-tree connector takes this
        keyword.)
        """
        ...

    def classify_call_error(
        self,
        exc: BaseException,
        *,
        provider_config: ProviderConfig,
        model_id: str,
    ) -> PlatformError | None:
        """Map a call-time failure of this connector's LLM to a `model.*` error.

        Called at the LLM call seam with an exception that is NOT a
        `PlatformError`, raised by the `crewai.LLM` this connector's
        `build_llm` returned (for `provider_config` / `model_id`) while
        serving a call. Returns a catalogued `model.*` `PlatformError`
        (the caller raises it `from exc`), or `None` when the exception
        is not one the connector recognises (the caller re-raises the
        original). The returned code decides whether the engine's
        fallback chain advances, so an unrecognised provider failure
        never engages `fallback_models`.

        Must be pure: no I/O, no network, no mutation, and it must not
        raise. A connector whose LLM already raises typed `model.*`
        errors (e.g. `claude_code`) returns `None`. (Protocol change,
        bug-145: every in-tree connector implements this method.)
        """
        ...


class MetadataSourceConnector(ProviderConnector, Protocol):
    """A provider that can also look up metadata for OTHER providers' models.

    Today only OpenRouter implements this. The CLI's augmentation loop
    consults every connected provider matching this Protocol to fill in
    parameter fields the upstream catalog left unset.
    """

    async def lookup_model_metadata(
        self,
        provider: str,
        model_id: str,
        credentials: Credentials | None = None,
    ) -> ModelMetadata | None:
        """Resolve metadata for a `(provider, model_id)` pair.

        Returns the metadata when known, `None` when the source has no
        entry. Implementations translate the platform's
        `(provider, model_id)` tuple into whatever identifier the
        source uses — for example, OpenRouter prefixes Anthropic
        models with `anthropic/`.
        """
        ...


# ---------------------------------------------------------------------------
# Runtime helpers used by the registry's metadata-source filter
# ---------------------------------------------------------------------------


def is_metadata_source(connector: ProviderConnector) -> bool:
    """Return `True` when `connector` implements `MetadataSourceConnector`.

    Used by
    [`connected_metadata_sources`][llm.registry.connected_metadata_sources]
    to pick out the providers whose catalog the connect-flow
    augmentation loop should consult.
    """
    return callable(getattr(connector, "lookup_model_metadata", None))
