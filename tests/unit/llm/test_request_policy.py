"""bug-144 Test 4 — request-policy validation in ONE factory + infra keys.

- `provider_config_from_yaml` validates `request_policy.session_headers`
  (RFC 7230 token, reserved names, case-insensitive dedupe) and raises
  `ConfigInvalid` naming the provider.
- `cli.llm_commands` builds its configs through the same factory (the
  runtime path is covered in
  `tests/component/test_provider_config_factory_runtime.py`), and no
  other `src/` module constructs `ProviderConfig` directly.
- `INFRA_KEYS` keeps agent-YAML `parameters` away from header kwargs.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from cli.llm_commands import _provider_configs_from_payload
from errors import ConfigInvalid
from llm import ProviderConfig, RequestPolicyConfig, provider_config_from_yaml
from llm.providers._openai_compat import (
    INFRA_KEYS,
    OpenAICompatRequestPolicy,
    connector_extras,
    resolve_request_policy,
)
from llm.providers.opencode import OpenCodeConnector
from llm.providers.openrouter import OpenRouterConnector

_SRC = Path(__file__).resolve().parents[3] / "src"


def _policy(headers: Any) -> dict[str, Any]:
    return {"request_policy": {"session_headers": headers}}


# ---- factory: happy paths --------------------------------------------------


def test_provider_config_factory_defaults_type_and_routes_extras() -> None:
    cfg = provider_config_from_yaml(
        "opencode",
        {
            "api_key_secret": "OPENCODE_API_KEY",
            "base_url": "https://x.invalid/v1",
            "timeout_seconds": 60,
            "models": [{"id": "m"}],
            "serves_as_metadata_source": True,
        },
    )
    assert cfg.type == "opencode"
    assert cfg.timeout_seconds == 60
    assert cfg.models == [{"id": "m"}]
    assert cfg.extras == {"serves_as_metadata_source": True}
    assert cfg.request_policy is None


def test_request_policy_parses_and_dedupes_case_insensitively() -> None:
    cfg = provider_config_from_yaml(
        "p", _policy(["x-session-affinity", "X-Session-Affinity", "X-Session-Id"])
    )
    assert cfg.request_policy == RequestPolicyConfig(
        session_headers=("x-session-affinity", "X-Session-Id")
    )
    assert "request_policy" not in cfg.extras


def test_request_policy_empty_list_is_explicit_disable() -> None:
    cfg = provider_config_from_yaml("p", _policy([]))
    assert cfg.request_policy == RequestPolicyConfig(session_headers=())


def test_request_policy_without_session_headers_keeps_default() -> None:
    cfg = provider_config_from_yaml("p", {"request_policy": {}})
    assert cfg.request_policy == RequestPolicyConfig(session_headers=None)


# ---- factory: rejections ---------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        "Authorization",
        "authorization",
        "Proxy-Authorization",
        "Cookie",
        "User-Agent",
        "user-agent",
        "Content-Type",
        "Content-Length",
        "HOST",
    ],
)
def test_request_policy_rejects_reserved_header(header: str) -> None:
    with pytest.raises(ConfigInvalid) as info:
        provider_config_from_yaml("opencode", _policy([header]))
    (err,) = info.value.detail["validation_errors"]
    assert err["code"] == "forbidden_header"
    assert "llm_providers.opencode" in err["message"]
    assert err["path"] == "/llm_providers/opencode/request_policy/session_headers/0"


@pytest.mark.parametrize("header", ["bad header", "", "x:y", "naïve", 42, None])
def test_request_policy_rejects_invalid_header_name(header: Any) -> None:
    with pytest.raises(ConfigInvalid) as info:
        provider_config_from_yaml("opencode", _policy([header]))
    (err,) = info.value.detail["validation_errors"]
    assert err["code"] == "invalid_header_name"
    assert "llm_providers.opencode" in err["message"]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("x-opencode-session", "not_a_mapping"),
        ({"session_headers": "x-opencode-session"}, "not_a_list"),
        ({"headers": {"Authorization": "Bearer sk"}}, "unknown_key"),
    ],
)
def test_request_policy_rejects_malformed_shape(raw: Any, code: str) -> None:
    with pytest.raises(ConfigInvalid) as info:
        provider_config_from_yaml("opencode", {"request_policy": raw})
    codes = [e["code"] for e in info.value.detail["validation_errors"]]
    assert code in codes


def test_request_policy_reports_every_violation() -> None:
    with pytest.raises(ConfigInvalid) as info:
        provider_config_from_yaml("p", _policy(["Authorization", "bad header", "ok"]))
    assert len(info.value.detail["validation_errors"]) == 2


# ---- the same factory behind `kolsch connect` / `refresh` ------------------


@pytest.mark.parametrize("header", ["Authorization", "bad header", "User-Agent"])
def test_provider_config_factory_llm_commands_path_rejects(header: str) -> None:
    payload = {"llm_providers": {"opencode": _policy([header])}}
    with pytest.raises(ConfigInvalid) as info:
        _provider_configs_from_payload(payload)
    assert (
        "llm_providers.opencode" in info.value.detail["validation_errors"][0]["message"]
    )


def test_provider_config_factory_llm_commands_path_matches_factory() -> None:
    raw = {"type": "opencode", "base_url": "u", **_policy(["x-session-affinity"])}
    built = _provider_configs_from_payload({"llm_providers": {"oc": raw}})
    assert built == {"oc": provider_config_from_yaml("oc", raw)}


def test_provider_config_factory_is_the_only_constructor_in_src() -> None:
    """No `src/` module builds `ProviderConfig(...)` besides the factory."""
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ProviderConfig"
            ):
                rel = path.relative_to(_SRC)
                if str(rel) != "llm/protocol.py":
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, offenders


# ---- policy resolution + connector defaults --------------------------------


def _cfg(policy: RequestPolicyConfig | None) -> ProviderConfig:
    return ProviderConfig(
        name="p",
        type="p",
        api_key_secret=None,
        base_url=None,
        timeout_seconds=None,
        request_policy=policy,
    )


def test_request_policy_resolution() -> None:
    default = OpenAICompatRequestPolicy(session_headers=("x-opencode-session",))
    assert resolve_request_policy(default, _cfg(None)) is default
    assert resolve_request_policy(default, _cfg(RequestPolicyConfig())) is default
    assert resolve_request_policy(
        default, _cfg(RequestPolicyConfig(session_headers=()))
    ) == OpenAICompatRequestPolicy(session_headers=())
    assert resolve_request_policy(
        default, _cfg(RequestPolicyConfig(session_headers=("a",)))
    ) == OpenAICompatRequestPolicy(session_headers=("a",))


def test_request_policy_connector_defaults() -> None:
    assert OpenCodeConnector.DEFAULT_REQUEST_POLICY.session_headers == (
        "x-opencode-session",
    )
    assert OpenRouterConnector.DEFAULT_REQUEST_POLICY.session_headers == ()


# ---- infra keys ------------------------------------------------------------


def test_infra_keys_cover_header_kwargs() -> None:
    assert {
        "default_headers",
        "extra_headers",
        "headers",
        "client_params",
    } <= INFRA_KEYS
    assert {"model", "api_key", "base_url", "timeout", "provider"} <= INFRA_KEYS
    # P2-N1: header/query/routing-shaped CrewAI kwargs are connector-owned.
    assert {"organization", "project", "default_query", "api_base"} <= INFRA_KEYS


def test_infra_keys_dropped_from_agent_parameters() -> None:
    params = {
        "temperature": 0.3,
        "max_tokens": 10,
        "default_headers": {"User-Agent": "evil"},
        "extra_headers": {"x-opencode-session": "hijack"},
        "headers": {"a": "b"},
        "client_params": {"default_headers": {}},
        "base_url": "https://evil.invalid",
        "api_base": "https://evil.invalid",
        "organization": "org-x",
        "project": "p-x",
        "default_query": {"q": "1"},
    }
    assert connector_extras(params) == {"temperature": 0.3, "max_tokens": 10}
    assert connector_extras(None) == {}
