"""qol-122 — `kolsch run --model` ad-hoc model override.

Unit tests for `cli.main.apply_run_model_override` (the resolve + fail-fast
helper), plus a stubbed-`_cmd_run` check that an unconnected-provider override
exits non-zero without reaching the engine.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from cli.main import _cmd_run, apply_run_model_override
from schemas import AgentId, IOSpec, ModelSpec, ResolvedAgentConfig, ThreadId

if TYPE_CHECKING:
    import pytest

# `cli/__init__.py` exports the `main` FUNCTION, shadowing the `cli.main`
# submodule as a package attribute — so `import cli.main` and dotted monkeypatch
# targets resolve to the function. Reach the real module via sys.modules (keyed
# by the un-shadowed dotted name) to patch its `bootstrap_runtime` global.
_MAIN = sys.modules[_cmd_run.__module__]


class _StubAssetManagement:
    def __init__(self, aliases: dict[str, str] | None) -> None:
        self._aliases = aliases

    def get_platform_config(self, section: str) -> dict[str, Any]:
        # Mirrors the real accessor: a missing section raises KeyError.
        if section != "model_aliases" or self._aliases is None:
            raise KeyError(section)
        return dict(self._aliases)


class _StubRuntime:
    def __init__(
        self, *, providers: list[str], aliases: dict[str, str] | None = None
    ) -> None:
        self.llm_providers_config = {p: object() for p in providers}
        self.asset_management = _StubAssetManagement(aliases)


def _config() -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        agent_id=AgentId("ag_x"),
        name="x",
        description="d",
        model=ModelSpec(provider="openrouter", model="anthropic/claude-3"),
        fallback_models=[ModelSpec(provider="openrouter", model="backup")],
        prompt="You are an agent.",
        io=IOSpec(mode="conversational"),
    )


def _apply(rt: _StubRuntime, ref: str) -> ResolvedAgentConfig | None:
    return apply_run_model_override(_config(), ref, cast("Any", rt))


def test_compact_string_override_applies_and_clears_fallback() -> None:
    out = _apply(_StubRuntime(providers=["opencode", "openrouter"]), "opencode/gpt-oss")
    assert out is not None
    assert out.model == ModelSpec(provider="opencode", model="gpt-oss")
    assert out.fallback_models == []  # override clears the chain


def test_alias_override_resolves_via_model_aliases() -> None:
    rt = _StubRuntime(providers=["openrouter"], aliases={"smart": "openrouter/big"})
    out = _apply(rt, "smart")
    assert out is not None
    assert out.model == ModelSpec(provider="openrouter", model="big")


def test_unconnected_provider_fails_fast(capsys: pytest.CaptureFixture[str]) -> None:
    out = _apply(_StubRuntime(providers=["openrouter"]), "opencode/x")
    assert out is None
    err = capsys.readouterr().err
    assert "not connected" in err
    assert "kolsch connect opencode" in err


def test_unknown_alias_fails_fast(capsys: pytest.CaptureFixture[str]) -> None:
    out = _apply(_StubRuntime(providers=["openrouter"], aliases={}), "nonexistent")
    assert out is None
    assert "invalid" in capsys.readouterr().err


def test_mock_provider_bypasses_connected_check() -> None:
    out = _apply(_StubRuntime(providers=[]), "mock/mock-model")
    assert out is not None
    assert out.model == ModelSpec(provider="mock", model="mock-model")


def test_missing_model_aliases_section_is_tolerated() -> None:
    out = _apply(_StubRuntime(providers=["opencode"], aliases=None), "opencode/x")
    assert out is not None
    assert out.model.provider == "opencode"


def test_override_provider_flows_for_auto_selection() -> None:
    # AC4 input: the override sets config.model.provider — spec-121's AUTO
    # framework selection keys on exactly this field.
    out = _apply(_StubRuntime(providers=["claude_code"]), "claude_code/sonnet")
    assert out is not None
    assert out.model.provider == "claude_code"
    assert out.model.model == "sonnet"


def test_cmd_run_unconnected_override_exits_2_without_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `--model` override fails fast inside `_cmd_run`: an unconnected
    provider exits 2 and the engine is never reached."""

    class _Engine:
        def __init__(self) -> None:
            self.called = False

        async def execute(self, config: object, ctx: object) -> object:
            self.called = True
            raise AssertionError("execute must not run when the override fails fast")

    engine = _Engine()

    class _RT:
        def __init__(self) -> None:
            self.llm_providers_config: dict[str, object] = {}  # nothing connected
            self.asset_management = _StubAssetManagement(None)
            self.engine = engine

        async def build_resolved_config(
            self, ref: str, call_overrides: object = None
        ) -> ResolvedAgentConfig:
            return _config()

    async def _fake_bootstrap(root: object = None) -> object:
        return _RT()

    monkeypatch.setattr(_MAIN, "bootstrap_runtime", _fake_bootstrap)
    args = argparse.Namespace(
        root=None,
        agent_ref="a/b",
        model="bogus-provider/x",
        input=[],
        stream=False,
        session=None,
        skill=[],
    )
    rc = asyncio.run(_cmd_run(args))
    assert rc == 2
    assert engine.called is False


def test_cmd_run_valid_override_reaches_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid `--model` override is applied end-to-end: `engine.execute`
    receives the config with the overridden model + cleared fallback chain."""
    captured: dict[str, Any] = {}

    class _Session:
        session_id = "ses_x"

    class _Thread:
        thread_id = ThreadId(f"th_{uuid4()}")

    class _Msg:
        def __init__(self) -> None:
            self.data = {"text": "ok"}

    class _Store:
        async def create_session(self, agent_id: object, name: object = None) -> object:
            return _Session()

        async def list_threads(self, session_id: object) -> list[object]:
            return [_Thread()]

    class _Engine:
        async def execute(self, config: object, ctx: object) -> object:
            captured["config"] = config
            return _Msg()

    class _RT:
        def __init__(self) -> None:
            self.llm_providers_config: dict[str, object] = {}  # mock bypasses
            self.asset_management = _StubAssetManagement(None)
            self.engine = _Engine()
            self.store = _Store()

        async def build_resolved_config(
            self, ref: str, call_overrides: object = None
        ) -> ResolvedAgentConfig:
            return _config()

    async def _fake_bootstrap(root: object = None) -> object:
        return _RT()

    monkeypatch.setattr(_MAIN, "bootstrap_runtime", _fake_bootstrap)
    args = argparse.Namespace(
        root=None,
        agent_ref="a/b",
        model="mock/echo-model",
        input=[],
        stream=False,
        session=None,
        skill=[],
    )
    rc = asyncio.run(_cmd_run(args))
    assert rc == 0
    cfg = captured["config"]
    assert cfg.model == ModelSpec(provider="mock", model="echo-model")
    assert cfg.fallback_models == []  # override reached execute with the chain cleared
