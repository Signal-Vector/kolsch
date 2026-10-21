"""bug-138 — conditional strict policy for OpenAI-compatible tool schemas.

CrewAI 1.14.6's ``OpenAICompletion._convert_tools_for_interference``
forces ``strict: True`` and closes every object schema to
``{properties: {}, additionalProperties: false}``. A free-form map arg
(pydantic ``dict[str, X]``) therefore reached strict-honouring models
as "empty object only", and they sent ``{}``. These tests push real
kolsch tool schemas through the connector-built LLM's tool conversion
(dummy key, no network) and assert what would be handed to the OpenAI
client.
"""

from __future__ import annotations

import copy
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import crewai
import pytest
from crewai.llms.providers.openai.completion import OpenAICompletion
from crewai.llms.providers.openai_compatible.completion import (
    OpenAICompatibleCompletion,
)
from crewai.utilities.pydantic_schema_utils import (
    _STRICT_METADATA_KEYS,
    resolve_refs,
)
from pydantic import BaseModel

from llm import LLMCallContext, ProviderConfig
from llm.providers._openai_compat import (
    KolschOpenAICompatibleCompletion,
    is_strict_compatible,
)
from llm.providers.opencode import OpenCodeConnector
from llm.providers.openrouter import OpenRouterConnector
from llm.user_agent import kolsch_user_agent

if TYPE_CHECKING:
    from types import ModuleType

_TOOLS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "cli"
    / "_starter_bundle"
    / "kolsch"
    / "tools"
)


def _load_tool_module(name: str) -> ModuleType:
    module_name = f"_test_bug138_starter_tool_{name}"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        module_name, _TOOLS_DIR / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# A stand-in for spec-136's ``jev_decide`` args (not on this branch):
# a map of name -> discriminated question model, plus a free-form state.
class _Noul(BaseModel):
    type: Literal["noul"]
    instructions: str


class _Choice(BaseModel):
    type: Literal["choice"]
    instructions: str
    criteria: dict[str, str | None]


class _JevLikeArgs(BaseModel):
    state: str | dict[str, Any]
    questions: dict[str, _Noul | _Choice]
    model: str = "typesafe/jev-1.14"


def _schema(name: str) -> dict[str, Any]:
    if name == "jev_like":
        return _JevLikeArgs.model_json_schema()
    if name == "fetch_url":
        return dict(_load_tool_module("fetch_url").FetchUrlArgs.model_json_schema())
    if name == "run_workflow":
        return dict(
            _load_tool_module("run_workflow").RunWorkflowArgs.model_json_schema()
        )
    if name == "read_file":
        return dict(_load_tool_module("read_file").ReadFileArgs.model_json_schema())
    raise AssertionError(name)


def _spec(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """The shape ``engine._crewai_adapter._tool_to_openai_spec`` hands CrewAI."""
    return {
        "type": "function",
        "function": {"name": name, "description": f"{name} tool", "parameters": schema},
    }


def _config(name: str) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        type=name,
        api_key_secret=f"{name.upper()}_API_KEY",
        base_url=(
            "https://openrouter.ai/api/v1"
            if name == "openrouter"
            else "https://opencode.example.com/v1"
        ),
        timeout_seconds=30,
        extras={},
        models=[],
    )


_CTX = LLMCallContext.for_session("ses_strict_policy")


async def _connector_llm(connector: str) -> Any:
    if connector == "openrouter":
        return await OpenRouterConnector().build_llm(
            "openai/gpt-4o-mini", _config("openrouter"), "sk-dummy", context=_CTX
        )
    return await OpenCodeConnector().build_llm(
        "qwen3.6-plus", _config("opencode"), "sk-dummy", context=_CTX
    )


# (tool, json-pointer-ish path to the open map inside `properties`)
_OPEN_MAPS: list[tuple[str, str]] = [
    ("jev_like", "questions"),
    ("fetch_url", "headers"),
    ("fetch_url", "cookies"),
    ("run_workflow", "inputs"),
]


def _open_map_branches(prop: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the object branches of a property (handles ``X | None``)."""
    branches = prop.get("anyOf", [prop])
    return [b for b in branches if b.get("type") == "object"]


@pytest.mark.parametrize("connector", ["openrouter", "opencode"])
@pytest.mark.parametrize(("tool", "prop"), _OPEN_MAPS)
async def test_connector_open_maps_survive_conversion(
    connector: str, tool: str, prop: str
) -> None:
    """Test 1 / AC 1: open maps keep their ``additionalProperties``; strict off."""
    llm = await _connector_llm(connector)
    schema = _schema(tool)
    # End to end: the `tools` CrewAI hands to the OpenAI client.
    params = llm._prepare_completion_params(
        [{"role": "user", "content": "hi"}],
        tools=[_spec(tool, copy.deepcopy(schema))],
    )
    function = params["tools"][0]["function"]

    assert function["strict"] is False
    # The kolsch schema, structurally identical, with `$ref`s inlined.
    assert function["parameters"] == _ref_resolved(schema)
    assert "$ref" not in json.dumps(function["parameters"])
    branches = _open_map_branches(function["parameters"]["properties"][prop])
    assert branches, f"{tool}.{prop} has no object branch"
    for branch in branches:
        additional = branch.get("additionalProperties")
        assert additional is True or isinstance(additional, dict), branch
        assert branch.get("properties") != {}, branch


def _ref_resolved(schema: dict[str, Any]) -> dict[str, Any]:
    resolved = resolve_refs(copy.deepcopy(schema))
    resolved.pop("$defs", None)
    return resolved


async def test_recursive_schema_sent_verbatim_non_strict() -> None:
    """A recursive schema can't be inlined faithfully: verbatim, $defs kept."""
    schema = {
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "children": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/Node"},
                    },
                },
            }
        },
        "type": "object",
        "properties": {"tree": {"$ref": "#/$defs/Node"}},
    }
    llm = await _connector_llm("openrouter")
    function = llm._convert_tools_for_interference(
        [_spec("tree_tool", copy.deepcopy(schema))]
    )[0]["function"]
    assert function["strict"] is False
    assert function["parameters"] == schema


async def test_metadata_named_property_goes_non_strict_and_survives() -> None:
    """CrewAI's strict pipeline deletes keys named ``title``/``default``/...

    from every dict — including ``properties`` — so such a field must
    not take the strict path.
    """

    class _Doc(BaseModel):
        title: str
        body: str

    schema = _Doc.model_json_schema()
    assert is_strict_compatible(schema) is False
    llm = await _connector_llm("openrouter")
    function = llm._convert_tools_for_interference([_spec("doc", schema)])[0][
        "function"
    ]
    assert function["strict"] is False
    assert set(function["parameters"]["properties"]) == {"title", "body"}


def test_guard_crewai_strict_metadata_keys() -> None:
    """Drift guard: the imported CrewAI key list still names these keys."""
    assert {
        "title",
        "default",
        "examples",
        "example",
        "$comment",
        "readOnly",
        "writeOnly",
        "deprecated",
    } <= set(_STRICT_METADATA_KEYS)


# ---------------------------------------------------------------------------
# Test 2 — strict-compatible tools unchanged
# ---------------------------------------------------------------------------


def _stock_llm(connector: str) -> Any:
    """A plain CrewAI client built exactly as the connectors did pre-bug-138."""
    if connector == "openrouter":
        return crewai.LLM(
            model="openrouter/openai/gpt-4o-mini",
            api_key="sk-dummy",
            base_url="https://openrouter.ai/api/v1",
            timeout=30,
        )
    return crewai.LLM(
        model="hosted_vllm/qwen3.6-plus",
        api_key="sk-dummy",
        base_url="https://opencode.example.com/v1",
        timeout=30,
    )


class _NoArgs(BaseModel):
    """Zero-field args model (like ``hello_card``'s ``HelloCardArgs``)."""


@pytest.mark.parametrize("tool", ["read_file", "no_args"])
@pytest.mark.parametrize("connector", ["openrouter", "opencode"])
async def test_strict_compatible_tool_payload_unchanged(
    connector: str, tool: str
) -> None:
    """Test 2 / AC 2: strict-compatible tools get exactly CrewAI's stock output."""
    schema = _NoArgs.model_json_schema() if tool == "no_args" else _schema("read_file")
    assert is_strict_compatible(schema)
    ours = (await _connector_llm(connector))._convert_tools_for_interference(
        [_spec(tool, copy.deepcopy(schema))]
    )
    stock = _stock_llm(connector)._convert_tools_for_interference(
        [_spec(tool, copy.deepcopy(schema))]
    )
    assert ours == stock
    assert ours[0]["function"]["strict"] is True


@pytest.mark.parametrize("connector", ["openrouter", "opencode"])
async def test_connector_drops_infra_keys_from_parameters(connector: str) -> None:
    """``provider`` / ``is_litellm`` in agent ``parameters`` are dropped silently."""
    parameters = {"provider": "evil", "is_litellm": True, "temperature": 0.2}
    llm: Any  # typed `crewai.LLM` by the Protocol; really the policy subclass
    if connector == "openrouter":
        llm = await OpenRouterConnector().build_llm(
            "openai/gpt-4o-mini",
            _config("openrouter"),
            "sk-dummy",
            parameters,
            context=_CTX,
        )
        assert llm.provider == "openrouter"
    else:
        llm = await OpenCodeConnector().build_llm(
            "qwen3.6-plus", _config("opencode"), "sk-dummy", parameters, context=_CTX
        )
        assert llm.provider == "hosted_vllm"
    assert isinstance(llm, KolschOpenAICompatibleCompletion)
    assert llm.temperature == 0.2


async def test_mixed_tool_list_keeps_order_and_unchanged_entries() -> None:
    """A strict-compatible tool next to an open-map tool is still stock."""
    llm = await _connector_llm("openrouter")
    tools = [
        _spec("read_file", _schema("read_file")),
        _spec("fetch_url", _schema("fetch_url")),
    ]
    stock = _stock_llm("openrouter")._convert_tools_for_interference(
        copy.deepcopy(tools)
    )
    ours = llm._convert_tools_for_interference(copy.deepcopy(tools))
    assert [t["function"]["name"] for t in ours] == ["read_file", "fetch_url"]
    assert ours[0] == stock[0]
    assert ours[1]["function"]["strict"] is False
    assert ours[1]["function"]["parameters"] == _schema("fetch_url")


# ---------------------------------------------------------------------------
# Test 3 — both connectors, construction equivalence, isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("connector", ["openrouter", "opencode"])
async def test_connector_builds_policy_client_equivalent_to_stock(
    connector: str,
) -> None:
    """Both connectors build the policy subclass, configured like stock CrewAI."""
    ours = await _connector_llm(connector)
    stock = _stock_llm(connector)
    assert isinstance(ours, KolschOpenAICompatibleCompletion)
    assert type(stock) is OpenAICompatibleCompletion
    for attr in ("model", "provider", "base_url", "api_key", "timeout"):
        assert getattr(ours, attr) == getattr(stock, attr), attr
    # bug-144: kolsch adds its User-Agent (and opencode's session
    # header) on top of whatever CrewAI's provider defaults carry.
    expected = dict(stock.default_headers or {})
    expected["User-Agent"] = kolsch_user_agent()
    if connector == "opencode":
        expected["x-opencode-session"] = _CTX.conversation_key
    assert ours.default_headers == expected


def test_isolation_plain_crewai_llm_still_stock() -> None:
    """No global patch: a CrewAI client built outside kolsch still closes maps."""
    llm = _stock_llm("openrouter")
    assert type(llm) is OpenAICompatibleCompletion
    converted = llm._convert_tools_for_interference(
        [_spec("fetch_url", _schema("fetch_url"))]
    )
    function = converted[0]["function"]
    assert function["strict"] is True
    headers = function["parameters"]["properties"]["headers"]
    closed = [b for b in headers.get("anyOf", [headers]) if b.get("type") == "object"]
    assert closed
    assert all(b["additionalProperties"] is False for b in closed)
    assert OpenAICompatibleCompletion._convert_tools_for_interference is (
        OpenAICompletion._convert_tools_for_interference
    )


# ---------------------------------------------------------------------------
# Test 4 — CrewAI drift guard
# ---------------------------------------------------------------------------


def test_guard_crewai_private_tool_conversion_seam() -> None:
    """Fail loudly if the private CrewAI seam bug-138 overrides drifts.

    Pinned against crewai 1.14.6. If this fails after a crewai bump,
    re-verify ``_openai_compat`` against the new conversion code.
    """
    method = getattr(OpenAICompletion, "_convert_tools_for_interference", None)
    assert method is not None, "OpenAICompletion._convert_tools_for_interference gone"
    assert list(inspect.signature(method).parameters) == ["self", "tools"]
    # It must still be the call site the request path uses — otherwise
    # the override silently stops applying.
    source = inspect.getsource(OpenAICompletion._prepare_completion_params)
    assert "self._convert_tools_for_interference(tools)" in source
    # And it must still emit one entry per tool (the override zips them).
    llm = _stock_llm("openrouter")
    tools = [
        _spec("read_file", _schema("read_file")),
        _spec("fetch_url", _schema("fetch_url")),
    ]
    assert len(llm._convert_tools_for_interference(tools)) == len(tools)


# ---------------------------------------------------------------------------
# is_strict_compatible
# ---------------------------------------------------------------------------


def _obj(properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, **extra}


@pytest.mark.parametrize(
    ("label", "schema", "expected"),
    [
        ("scalars", _obj({"a": {"type": "string"}, "b": {"type": "integer"}}), True),
        (
            "nullable scalar",
            _obj({"a": {"anyOf": [{"type": "string"}, {"type": "null"}]}}),
            True,
        ),
        ("enum", _obj({"a": {"enum": ["x", "y"], "type": "string"}}), True),
        (
            "open map schema",
            _obj({"m": {"type": "object", "additionalProperties": {"type": "string"}}}),
            False,
        ),
        (
            "open map true",
            _obj({"m": {"type": "object", "additionalProperties": True}}),
            False,
        ),
        ("bare object", _obj({"m": {"type": "object"}}), False),
        (
            "explicit empty properties (zero-field model)",
            _obj({"m": {"type": "object", "properties": {}}}),
            True,
        ),
        (
            "metadata-named property",
            _obj({"title": {"type": "string"}}),
            False,
        ),
        (
            "explicitly closed empty object",
            _obj({"m": {"type": "object", "additionalProperties": False}}),
            True,
        ),
        (
            "closed nested object",
            _obj({"m": _obj({"x": {"type": "number"}}, additionalProperties=False)}),
            True,
        ),
        (
            "nested open map",
            _obj(
                {"m": _obj({"inner": {"type": "object", "additionalProperties": {}}})}
            ),
            False,
        ),
        (
            "open map inside anyOf",
            _obj(
                {
                    "m": {
                        "anyOf": [
                            {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                            {"type": "null"},
                        ]
                    }
                }
            ),
            False,
        ),
        (
            "array of closed objects",
            _obj({"xs": {"type": "array", "items": _obj({"x": {"type": "string"}})}}),
            True,
        ),
        (
            "array of open maps",
            _obj(
                {
                    "xs": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": True},
                    }
                }
            ),
            False,
        ),
        ("array without items", _obj({"xs": {"type": "array"}}), False),
        ("untyped value (Any)", _obj({"v": {"description": "anything"}}), False),
        (
            "rejected keyword",
            _obj({"m": _obj({"x": {"type": "string"}}, minProperties=1)}),
            False,
        ),
        (
            "root open map",
            {"type": "object", "additionalProperties": {"type": "string"}},
            False,
        ),
    ],
)
def test_is_strict_compatible(
    label: str, schema: dict[str, Any], *, expected: bool
) -> None:
    assert is_strict_compatible(schema) is expected, label


def test_is_strict_compatible_follows_defs_refs() -> None:
    closed = {
        "$defs": {"Item": _obj({"x": {"type": "string"}})},
        **_obj({"item": {"$ref": "#/$defs/Item"}}),
    }
    open_ = {
        "$defs": {
            "Item": {"type": "object", "additionalProperties": {"type": "string"}}
        },
        **_obj({"item": {"$ref": "#/$defs/Item"}}),
    }
    dangling = _obj({"item": {"$ref": "#/$defs/Missing"}})
    recursive = {
        "$defs": {"Node": _obj({"child": {"$ref": "#/$defs/Node"}})},
        **_obj({"root": {"$ref": "#/$defs/Node"}}),
    }
    assert is_strict_compatible(closed) is True
    assert is_strict_compatible(open_) is False
    assert is_strict_compatible(dangling) is False
    assert is_strict_compatible(recursive) is False


@pytest.mark.parametrize("tool", ["jev_like", "fetch_url", "run_workflow"])
def test_real_open_map_schemas_are_not_strict_compatible(tool: str) -> None:
    assert is_strict_compatible(_schema(tool)) is False
