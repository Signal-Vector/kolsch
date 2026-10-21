"""Shared CrewAI client for the OpenAI-compatible connectors (bug-138).

The `openrouter` and `opencode` connectors both reach CrewAI's
`OpenAICompatibleCompletion` client. Its inherited
`OpenAICompletion._convert_tools_for_interference` (crewai 1.14.6)
re-converts every tool kolsch hands it: it hard-codes
`"strict": True` and runs `sanitize_tool_params_for_openai_strict`,
which closes every object schema to
`{properties: {}, required: [], additionalProperties: false}`. A
free-form map argument — a pydantic `dict[str, X]`, emitted as
`{"type": "object", "additionalProperties": {...}}` — therefore reached
strict-honouring models (gpt-4o-mini, for one) as "an empty object
only", and they sent `{}`. Kolsch's own `model.request` event records
the pre-conversion schema, so the damage was invisible in the log.

**Conditional strict policy.** For each tool,
[`is_strict_compatible`][llm.providers._openai_compat.is_strict_compatible]
decides whether the kolsch-built `parameters` survive strict mode
without losing meaning:

- **Strict-compatible** (no open maps, no untyped values, no
  keywords strict mode rejects): CrewAI's output is used unchanged,
  `strict: true` included. Existing scalar / closed-object tools see
  byte-identical payloads.
- **Otherwise**: the kolsch-built `parameters` are sent with
  `"strict": false`, so the model sees the real map schema.

**Seam.** `crewai.LLM(model="openrouter/<id>")` and
`crewai.LLM(model="hosted_vllm/<id>")` resolve, inside
`crewai.LLM.__new__`, to `OpenAICompatibleCompletion(model=<id>,
provider=<prefix>, **kwargs)`.
[`build_openai_compatible_llm`][llm.providers._openai_compat.build_openai_compatible_llm]
performs that same construction with a kolsch subclass that overrides
only `_convert_tools_for_interference`. The policy is therefore a
property of the LLM instances the two connectors build — no global
CrewAI patch, and `claude_code` / the Mock LLM are untouched. A drift
guard test (`tests/unit/test_tool_schema_strict_policy.py`) fails if
the private CrewAI method disappears, changes signature, or stops
being the conversion call site.

Non-strict schemas are sent with `#/$defs/` refs inlined (some
OpenRouter-routed upstreams reject `$ref`), except recursive schemas,
which go out verbatim.

Known unhandled shapes (pydantic emits neither): a dict-valued
`const` / `enum` containing `"type": "object"`, and a `$ref` with
sibling keys (the siblings are not inspected).

**Request policy + identity (bug-144).** Each OpenAI-compatible
connector declares a
[`OpenAICompatRequestPolicy`][llm.providers._openai_compat.OpenAICompatRequestPolicy]
as its `DEFAULT_REQUEST_POLICY` class attribute;
`llm_providers.<name>.request_policy` in `platform.yaml` overrides it
([`resolve_request_policy`][llm.providers._openai_compat.resolve_request_policy]).
The builder sends every policy `session_headers` name with the
[`LLMCallContext`][llm.protocol.LLMCallContext]'s `conversation_key`
as its value, plus `User-Agent: kolsch/<version>`, as client
`default_headers`. Agent-YAML `parameters` can't override any of them
([`INFRA_KEYS`][llm.providers._openai_compat.INFRA_KEYS]).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from crewai.llms.providers.openai_compatible.completion import (
    OpenAICompatibleCompletion,
)
from crewai.utilities.pydantic_schema_utils import (
    _STRICT_METADATA_KEYS,
    resolve_refs,
)

from errors import LlmClientInitFailed
from llm.user_agent import kolsch_user_agent

if TYPE_CHECKING:
    import crewai
    from crewai.tools.base_tool import BaseTool

    from llm.protocol import LLMCallContext, ProviderConfig

# JSON Schema keywords OpenAI's strict mode rejects outright (e.g.
# "'minProperties' is not permitted"). A schema using any of them is
# sent non-strict so the call isn't refused upstream.
_STRICT_REJECTED_KEYWORDS: Final = frozenset(
    {
        "minProperties",
        "maxProperties",
        "patternProperties",
        "propertyNames",
        "unevaluatedProperties",
        "dependentRequired",
        "dependentSchemas",
        "if",
        "then",
        "else",
        "not",
    }
)
# A node carrying none of these constrains nothing (pydantic `Any`);
# strict mode would have to invent a type for it.
_SHAPE_KEYWORDS: Final = ("type", "anyOf", "oneOf", "allOf", "$ref", "enum", "const")
_DEFS_PREFIX: Final = "#/$defs/"
_STRICT_METADATA_KEYS_SET: Final = frozenset(_STRICT_METADATA_KEYS)


def is_strict_compatible(schema: Mapping[str, Any]) -> bool:
    """Return whether `schema` survives OpenAI strict mode without losing meaning.

    `False` when any reachable node (following `#/$defs/` refs) is:

    - an open map: an object whose `additionalProperties` is a schema
      or `true`, or an object with no `properties` that isn't
      explicitly closed with `additionalProperties: false`;
    - untyped (no `type` / `anyOf` / `oneOf` / `allOf` / `$ref` /
      `enum` / `const`), or an array without a typed `items`;
    - using a keyword strict mode rejects (`minProperties`, ...);
    - an unresolvable or recursive `$ref`.
    """
    defs = schema.get("$defs")
    return _node_ok(schema, defs if isinstance(defs, Mapping) else {}, frozenset())


def _node_ok(
    node: object,
    defs: Mapping[str, Any],
    resolving: frozenset[str],
) -> bool:
    if not isinstance(node, Mapping):
        return False
    if _STRICT_REJECTED_KEYWORDS & node.keys():
        return False
    if "$ref" in node:
        return _ref_ok(node["$ref"], defs, resolving)
    if not any(key in node for key in _SHAPE_KEYWORDS):
        return False
    return _combinators_ok(node, defs, resolving) and _typed_ok(node, defs, resolving)


def _ref_ok(ref: object, defs: Mapping[str, Any], resolving: frozenset[str]) -> bool:
    if not isinstance(ref, str) or not ref.startswith(_DEFS_PREFIX):
        return False
    name = ref.removeprefix(_DEFS_PREFIX)
    if name not in defs or name in resolving:
        return False
    return _node_ok(defs[name], defs, resolving | {name})


def _combinators_ok(
    node: Mapping[str, Any],
    defs: Mapping[str, Any],
    resolving: frozenset[str],
) -> bool:
    for combinator in ("anyOf", "oneOf", "allOf"):
        branches = node.get(combinator, [])
        if not isinstance(branches, list):
            return False
        if not all(_node_ok(branch, defs, resolving) for branch in branches):
            return False
    return True


def _typed_ok(
    node: Mapping[str, Any],
    defs: Mapping[str, Any],
    resolving: frozenset[str],
) -> bool:
    raw_type = node.get("type")
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    if "object" in types and not _object_ok(node, defs, resolving):
        return False
    if "array" in types:
        items = node.get("items")
        if not isinstance(items, Mapping) or not items:
            return False
        return _node_ok(items, defs, resolving)
    return True


def _object_ok(
    node: Mapping[str, Any],
    defs: Mapping[str, Any],
    resolving: frozenset[str],
) -> bool:
    additional = node.get("additionalProperties")
    if additional is True or isinstance(additional, Mapping):
        return False
    if "properties" not in node:
        # `{"type": "object"}` with no field list is a free-form map
        # unless explicitly closed. An explicit `"properties": {}` is a
        # zero-field model (e.g. a no-args tool) — closed and fine.
        return additional is False
    properties = node["properties"]
    if not isinstance(properties, Mapping):
        return False
    # CrewAI's strict pipeline strips these keys from EVERY dict it
    # walks — including a `properties` map — so a field named e.g.
    # `title` or `default` would silently vanish from the schema.
    if _STRICT_METADATA_KEYS_SET & properties.keys():
        return False
    return all(_node_ok(prop, defs, resolving) for prop in properties.values())


def _non_strict_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """The kolsch-built schema for a non-strict tool, with `$ref`s inlined.

    Some OpenRouter-routed (non-OpenAI) upstreams reject `$ref`, so a
    non-recursive schema is sent with every `#/$defs/` ref inlined and
    `$defs` dropped — structurally the same schema. A recursive (or
    dangling-ref) schema can't be inlined faithfully and is sent
    verbatim.
    """
    schema = copy.deepcopy(dict(parameters))
    defs = schema.get("$defs")
    if not isinstance(defs, Mapping) or not defs or _defs_recursive(defs):
        return schema
    try:
        inlined = resolve_refs(schema)
    except KeyError:  # dangling ref — leave the schema as authored
        return schema
    inlined.pop("$defs", None)
    return inlined


def _defs_recursive(defs: Mapping[str, Any]) -> bool:
    """Return whether any `$defs` entry reaches itself through `$ref`s."""

    def refs_in(node: object) -> set[str]:
        found: set[str] = set()
        if isinstance(node, Mapping):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith(_DEFS_PREFIX):
                found.add(ref.removeprefix(_DEFS_PREFIX))
            for value in node.values():
                found |= refs_in(value)
        elif isinstance(node, list):
            for value in node:
                found |= refs_in(value)
        return found

    edges = {name: refs_in(body) for name, body in defs.items()}

    def reaches(start: str, target: str, seen: set[str]) -> bool:
        for nxt in edges.get(start, set()):
            if nxt == target:
                return True
            if nxt not in seen:
                seen.add(nxt)
                if reaches(nxt, target, seen):
                    return True
        return False

    return any(reaches(name, name, set()) for name in edges)


def _kolsch_parameters(tool: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the `parameters` schema kolsch built for an OpenAI-spec tool."""
    function = tool.get("function")
    source = function if isinstance(function, Mapping) else tool
    parameters = source.get("parameters")
    return parameters if isinstance(parameters, Mapping) and parameters else None


class KolschOpenAICompatibleCompletion(OpenAICompatibleCompletion):
    """`OpenAICompatibleCompletion` applying kolsch's conditional strict policy.

    Only `_convert_tools_for_interference` is overridden; construction,
    provider config resolution, and every request path are CrewAI's.
    """

    def _convert_tools_for_interference(
        self, tools: list[dict[str, BaseTool]]
    ) -> list[dict[str, Any]]:
        """Keep CrewAI's strict output only for strict-compatible schemas."""
        converted = super()._convert_tools_for_interference(tools)
        result: list[dict[str, Any]] = []
        for tool, stock in zip(tools, converted, strict=True):
            parameters = _kolsch_parameters(cast("Mapping[str, Any]", tool))
            if parameters is None or is_strict_compatible(parameters):
                result.append(stock)
                continue
            function = stock["function"]
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": function["name"],
                        "description": function["description"],
                        "strict": False,
                        "parameters": _non_strict_parameters(parameters),
                    },
                }
            )
        return result


# Keys the connector sets itself when building the client. Agent-YAML
# `ModelSpec.parameters` values that collide with these are dropped —
# the connector always wins on routing so a mistyped YAML can't hijack
# the model, key, base_url, or timeout via a `parameters:` entry.
# `provider` / `is_litellm` steer CrewAI's client construction
# (bug-138); a stray agent-YAML value would otherwise collide with the
# connector's own `provider=` kwarg. The header-bearing kwargs
# (`default_headers`, `extra_headers`, `headers`, and `client_params`,
# which CrewAI merges over the client's `default_headers`) are
# connector-owned too, so agent YAML can't replace the request-policy
# session headers or the kolsch User-Agent (bug-144).
INFRA_KEYS: Final = frozenset(
    {
        "model",
        "api_key",
        "base_url",
        "timeout",
        "provider",
        "is_litellm",
        "default_headers",
        "extra_headers",
        "headers",
        "client_params",
    }
)


def connector_extras(parameters: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return agent-YAML `parameters` minus the connector-owned keys.

    See [`INFRA_KEYS`][llm.providers._openai_compat.INFRA_KEYS].
    """
    if not parameters:
        return {}
    return {k: v for k, v in parameters.items() if k not in INFRA_KEYS}


@dataclass(frozen=True)
class OpenAICompatRequestPolicy:
    """How an OpenAI-compatible connector attributes requests (bug-144).

    Attributes:
        session_headers: Header names whose value is the call's
            `LLMCallContext.conversation_key` (e.g. OpenCode Go's
            `x-opencode-session`). Names only — a policy can never
            carry a static header value, so no secret can enter config.
    """

    session_headers: tuple[str, ...] = ()


def resolve_request_policy(
    connector_default: OpenAICompatRequestPolicy,
    provider_config: ProviderConfig,
) -> OpenAICompatRequestPolicy:
    """Apply `provider_config.request_policy` over the connector's default.

    A `None` override (or a `None` `session_headers`) keeps the
    connector's default; an explicit list — including `[]` — replaces
    it. The override was validated by
    [`provider_config_from_yaml`][llm.protocol.provider_config_from_yaml].
    """
    override = provider_config.request_policy
    if override is None or override.session_headers is None:
        return connector_default
    return OpenAICompatRequestPolicy(session_headers=override.session_headers)


def request_headers(
    policy: OpenAICompatRequestPolicy,
    context: LLMCallContext | None,
    *,
    provider_name: str,
    model_id: str,
) -> dict[str, str]:
    """Build the client `default_headers` for one LLM construction.

    Returns the policy's session headers (each carrying
    `context.conversation_key`) plus `User-Agent: kolsch/<version>`.

    Raises:
        LlmClientInitFailed: the policy needs a conversation key but
            `context` is `None`. No substitute id is ever minted.
    """
    headers: dict[str, str] = {}
    if policy.session_headers:
        if context is None:
            raise LlmClientInitFailed(
                provider=provider_name,
                model=model_id,
                reason=(
                    "request policy sends session headers "
                    f"{list(policy.session_headers)} but no LLMCallContext "
                    "was supplied; kolsch never invents a conversation key"
                ),
                where="llm.providers._openai_compat.request_headers",
            )
        headers = dict.fromkeys(policy.session_headers, context.conversation_key)
    headers["User-Agent"] = kolsch_user_agent()
    return headers


def build_openai_compatible_llm(  # noqa: PLR0913 — keyword-only builder; one arg per connector concern
    *,
    provider: Literal["openrouter", "hosted_vllm"],
    model_id: str,
    provider_config: ProviderConfig,
    api_key: str | None,
    default_timeout: float,
    parameters: Mapping[str, Any] | None,
    context: LLMCallContext | None,
    default_policy: OpenAICompatRequestPolicy,
) -> crewai.LLM:
    """Build the OpenAI-compatible CrewAI client with kolsch's policies applied.

    Equivalent to `crewai.LLM(model=f"{provider}/{model_id}", ...)`
    — which CrewAI dispatches to
    `OpenAICompatibleCompletion(model=model_id, provider=provider,
    ...)` for both prefixes — except:

    - the instance is
      [`KolschOpenAICompatibleCompletion`][llm.providers._openai_compat.KolschOpenAICompatibleCompletion]
      (bug-138 strict tool-schema policy);
    - `default_headers` carry the resolved request policy's session
      headers and the kolsch User-Agent (bug-144);
    - agent-YAML `parameters` are forwarded minus
      [`INFRA_KEYS`][llm.providers._openai_compat.INFRA_KEYS].

    Raises:
        LlmClientInitFailed: the resolved policy sends session headers
            and `context` is `None`.
    """
    policy = resolve_request_policy(default_policy, provider_config)
    headers = request_headers(
        policy,
        context,
        provider_name=provider_config.name,
        model_id=model_id,
    )
    llm = KolschOpenAICompatibleCompletion(
        model=model_id,
        provider=provider,
        api_key=api_key,
        base_url=provider_config.base_url,
        timeout=(
            provider_config.timeout_seconds
            if provider_config.timeout_seconds is not None
            else default_timeout
        ),
        default_headers=headers,
        **connector_extras(parameters),
    )
    # `crewai.LLM.__new__` returns its native provider instances under
    # the same cast; callers and the `ProviderConnector` Protocol
    # type the result as `crewai.LLM`.
    return cast("crewai.LLM", llm)
