"""bug-144 Test 4 (runtime half) — `bootstrap_runtime` validates `request_policy`.

The runtime reads `llm_providers` through the same
`provider_config_from_yaml` factory `kolsch connect` / `refresh` use
(unit-tested in `tests/unit/llm/test_request_policy.py`), so an invalid
session-header name fails the runtime boot with the same typed error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml

from errors import ConfigInvalid
from llm import RequestPolicyConfig
from platform_runtime import bootstrap_runtime

if TYPE_CHECKING:
    from pathlib import Path


def _workspace(root: Path, opencode_entry: dict[str, Any]) -> None:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
                "sandbox": {"variant": "native", "config": {}},
                "authorization": {
                    "variant": "rule_based",
                    "config": {
                        "rules": [{"who": ".*", "what": ".*", "effect": "allow"}]
                    },
                },
                "storage": {
                    "entity_backend": "sqlite",
                    "entity_path": ".platform/entities.db",
                },
                "event_bus": {"backend": "in_process"},
                "file_access": {"allowed_paths": ["data/"], "denied_paths": [".env"]},
                "defaults": {},
                "llm_providers": {"opencode": opencode_entry},
            }
        ),
        encoding="utf-8",
    )
    (root / ".env").write_text("", encoding="utf-8")


@pytest.mark.parametrize("header", ["Authorization", "bad header", "User-Agent"])
async def test_provider_config_factory_runtime_path_rejects(
    tmp_path: Path, header: str
) -> None:
    _workspace(
        tmp_path,
        {
            "base_url": "https://x.invalid/v1",
            "request_policy": {"session_headers": [header]},
        },
    )
    with pytest.raises(ConfigInvalid) as info:
        await bootstrap_runtime(root=tmp_path)
    (err,) = info.value.detail["validation_errors"]
    assert "llm_providers.opencode" in err["message"]


async def test_provider_config_factory_runtime_path_accepts_override(
    tmp_path: Path,
) -> None:
    _workspace(
        tmp_path,
        {
            "base_url": "https://x.invalid/v1",
            "request_policy": {"session_headers": ["x-session-affinity"]},
        },
    )
    runtime = await bootstrap_runtime(root=tmp_path)
    cfg = runtime.llm_providers_config["opencode"]
    assert cfg.request_policy == RequestPolicyConfig(
        session_headers=("x-session-affinity",)
    )
