"""bug-144 Test 2 (Phase 1 part) — `LLMCallContext` shape + caller guard.

- The value type applies the v1 derivation rule and is frozen.
- `FallbackLLMHandle` hands its one context to every factory attempt.
- `make_build_llm_factory` and `llm.build_llm` forward it verbatim.
- An AST guard proves every production LLM-construction call site under
  `src/` passes an explicit, non-`None` context — a new caller that
  drops it fails here rather than silently sending no conversation key.

The "connector with a session-header policy raises on `context=None`"
half of Test 2 lands with the request policy in Phase 2.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from engine import LLMRequest, LLMResponse
from engine._fallback import FallbackLLMHandle
from engine.framework import Message
from errors import ModelServerError
from llm import LLMCallContext, ProviderConfig, provider_config_from_yaml, registry
from schemas import ModelSpec

_SRC = Path(__file__).resolve().parents[3] / "src"


def test_llm_call_context_for_session_derives_key() -> None:
    ctx = LLMCallContext.for_session("ses_abc", agent_id="ag_x")
    assert ctx.session_id == "ses_abc"
    assert ctx.conversation_key == "ses_abc"
    assert ctx.agent_id == "ag_x"
    assert LLMCallContext.for_session("ses_abc").agent_id is None


def test_llm_call_context_is_frozen() -> None:
    ctx = LLMCallContext.for_session("ses_abc")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.conversation_key = "other"  # type: ignore[misc]


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[Message(role="user", content="hi")],
        tools=[],
        model="p/m",
        parameters={},
    )


async def _noop_emit(**_: Any) -> None:
    return None


async def test_llm_call_context_handle_reuses_one_object_per_attempt() -> None:
    primary = ModelSpec(provider="p", model="primary")
    backup = ModelSpec(provider="p", model="backup")
    ctx = LLMCallContext.for_session("ses_handle")
    seen: list[LLMCallContext | None] = []

    async def failing(_: LLMRequest) -> LLMResponse:
        raise ModelServerError(provider="p", model="primary", status_code=503)

    async def ok(_: LLMRequest) -> LLMResponse:
        return LLMResponse(content="ok")

    async def factory(spec: ModelSpec, *, context: LLMCallContext | None) -> Any:
        seen.append(context)
        return failing if spec == primary else ok

    handle = FallbackLLMHandle(
        emit=_noop_emit,
        agent_id="ag_x",
        primary_spec=primary,
        chain=[backup],
        llm_factory=factory,
        config_event_id=f"ev_{uuid4()}",
        context=ctx,
    )
    response = await handle(_request())

    assert response.content == "ok"
    assert handle.context is ctx
    assert len(seen) == 2
    assert all(c is ctx for c in seen)


async def test_llm_call_context_make_build_llm_factory_forwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import llm
    from engine._crewai_adapter import make_build_llm_factory

    captured: dict[str, Any] = {}

    async def fake_build_llm(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(llm, "build_llm", fake_build_llm)
    # bug-145: the factory binds the connector's classifier once
    # `build_llm` returned, so the entry must exist; the registry is
    # empty in this unit test, so the binding is stubbed too.
    monkeypatch.setattr(llm, "call_error_classifier", lambda spec, cfg: None)
    cfg = ProviderConfig(
        name="real",
        type="opencode",
        api_key_secret=None,
        base_url=None,
        timeout_seconds=None,
    )
    factory = make_build_llm_factory(
        llm_providers_config={"real": cfg}, secrets=object()
    )
    ctx = LLMCallContext.for_session("ses_factory")

    await factory(ModelSpec(provider="real", model="m"), context=ctx)

    assert captured["context"] is ctx


async def test_llm_call_context_build_llm_forwards_to_connector() -> None:
    from llm import build_llm

    received: dict[str, Any] = {}

    class _Connector:
        type_name = "ctx_unit_bug144"

        def classify_call_error(
            self, exc: BaseException, *, provider_config: Any, model_id: str
        ) -> Any:  # pragma: no cover — unused (bug-145 protocol)
            return None

        async def build_llm(
            self,
            model_id: str,
            provider_config: ProviderConfig,
            api_key: str | None,
            parameters: Any = None,
            *,
            context: LLMCallContext | None = None,
        ) -> Any:
            received["context"] = context
            return object()

    cfg = ProviderConfig(
        name="ctxp",
        type="ctx_unit_bug144",
        api_key_secret=None,
        base_url=None,
        timeout_seconds=None,
    )
    ctx = LLMCallContext.for_session("ses_build")
    registry.register("ctx_unit_bug144", _Connector)
    try:
        await build_llm(
            ModelSpec(provider="ctxp", model="m"),
            cfg,
            object(),
            context=ctx,
        )
    finally:
        registry._REGISTRY.pop("ctx_unit_bug144", None)

    assert received["context"] is ctx


# ---------------------------------------------------------------------------
# AST guard — production call sites always pass a real context
# ---------------------------------------------------------------------------

#: call target name → the keyword that must carry the context.
_GUARDED_CALLS = {
    "build_llm": "context",
    "_llm_factory": "context",
    "_llm_for_model": "context",
    "FallbackLLMHandle": "context",
    "compact": "llm_context",
}


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _guarded_sites() -> list[tuple[str, int, str, ast.Call]]:
    sites: list[tuple[str, int, str, ast.Call]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name in _GUARDED_CALLS:
                rel = str(path.relative_to(_SRC))
                sites.append((rel, node.lineno, name, node))
    return sites


def test_llm_call_context_guard_finds_the_known_sites() -> None:
    """The guard is not vacuous — it sees the call sites bug-144 wired."""
    found = {(rel, name) for rel, _, name, _ in _guarded_sites()}
    assert ("engine/engine.py", "build_llm") in found
    assert ("engine/engine.py", "FallbackLLMHandle") in found
    assert ("engine/engine.py", "compact") in found
    assert ("engine/_fallback.py", "_llm_factory") in found
    assert ("engine/_crewai_adapter.py", "build_llm") in found
    assert ("llm/build_llm.py", "build_llm") in found
    assert ("providers/compaction.py", "_llm_for_model") in found


def test_llm_call_context_every_production_site_passes_context() -> None:
    offenders: list[str] = []
    for rel, lineno, name, node in _guarded_sites():
        kw = _GUARDED_CALLS[name]
        match = [k for k in node.keywords if k.arg == kw]
        if not match:
            offenders.append(f"{rel}:{lineno} {name}(...) missing `{kw}=`")
            continue
        value = match[0].value
        if isinstance(value, ast.Constant) and value.value is None:
            offenders.append(f"{rel}:{lineno} {name}(...) passes `{kw}=None`")
    assert not offenders, "\n".join(offenders)


# ---------------------------------------------------------------------------
# Phase 2 — a session-header policy refuses to run without a context
# ---------------------------------------------------------------------------


async def test_llm_call_context_none_raises_for_session_header_policy() -> None:
    from errors import LlmClientInitFailed
    from llm.providers.opencode import OpenCodeConnector

    cfg = provider_config_from_yaml("opencode", {"base_url": "https://x.invalid/v1"})
    with pytest.raises(LlmClientInitFailed) as info:
        await OpenCodeConnector().build_llm("m", cfg, "sk-test", context=None)
    assert "x-opencode-session" in info.value.detail["reason"]
    assert info.value.detail["provider"] == "opencode"


async def test_llm_call_context_none_bubbles_typed_through_build_llm() -> None:
    """`llm.build_llm` re-raises the connector's typed error unchanged."""
    from errors import LlmClientInitFailed
    from llm import build_llm

    cfg = provider_config_from_yaml("opencode", {"base_url": "https://x.invalid/v1"})
    with pytest.raises(LlmClientInitFailed) as info:
        await build_llm(
            ModelSpec(provider="opencode", model="m"),
            cfg,
            object(),
            context=None,
        )
    assert "LLMCallContext" in info.value.detail["reason"]
