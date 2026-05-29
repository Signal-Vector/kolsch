"""Shared helper to wire an :class:`engine.AgentEngine` for tests.

The component-level test_engine.py and the integration-level
test_engine_crewai.py both construct an engine with the same
subsystem composition; only the framework differs. Centralizing the
builder here avoids drift between the two test layers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

import yaml

from assets import AssetManagement
from bus import InProcessEventBus
from engine import (
    AgentEngine,
    ExecutionContext,
    LLMRequest,
    LLMResponse,
)
from errors import ModelUpstreamError
from files import PathPolicy
from providers import (
    NativeSandboxProvider,
    RecentBufferMemoryProvider,
    RuleBasedAuthorizationProvider,
)
from providers.secrets import DotenvSecretsProvider
from schemas import AgentId
from skills import SkillsSubsystem
from store import SessionStore, SqliteBackend
from tests.fixtures.fake_llm import (
    FakeLLM,
    FakeLLMScriptedError,
    TextResponse,
    ToolCallResponse,
)
from tools import ToolsSubsystem

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from engine.framework import Framework


def write_platform_yaml(root: Path) -> None:
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
                        "rules": [{"who": ".*", "what": ".*", "effect": "allow"}],
                    },
                },
                "storage": {
                    "entity_backend": "sqlite",
                    "entity_path": ".platform/entities.db",
                },
                "event_bus": {"backend": "in_process"},
                "file_access": {
                    "allowed_paths": ["data/", "bundles/"],
                    "denied_paths": [".env"],
                },
                "defaults": {},
            }
        ),
        encoding="utf-8",
    )
    (root / ".env").write_text("", encoding="utf-8")


def make_fake_llm_callable(
    fake_llm: FakeLLM,
) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
    """Adapter — engine LLMHandle → tests/fixtures/fake_llm.FakeLLM.

    Translates :class:`LLMRequest` into the dict context the Fake
    records, and the Fake's response variants back to
    :class:`LLMResponse`. Maps ``FakeLLMScriptedError`` with
    ``error_type="model.upstream_error"`` to :class:`ModelUpstreamError`
    so engine retry logic sees the right Level.
    """

    async def llm_callable(request: LLMRequest) -> LLMResponse:
        context: dict[str, object] = {
            "messages": [m.model_dump() for m in request.messages],
            "tools": list(request.tools),
            "model": request.model,
            "system_prompt": next(
                (m.content for m in request.messages if m.role == "system"),
                "",
            ),
        }
        try:
            fake_response = await fake_llm.agenerate(context)
        except FakeLLMScriptedError as exc:
            if exc.error_type == "model.upstream_error":
                raise ModelUpstreamError(
                    provider="fake",
                    http_status=500,
                    response=str(exc),
                ) from exc
            raise
        if isinstance(fake_response, TextResponse):
            return LLMResponse(content=fake_response.text)
        if isinstance(fake_response, ToolCallResponse):
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": fake_response.tool_call_id or f"tc_{uuid4()}",
                        "name": fake_response.tool,
                        "arguments": dict(fake_response.args),
                    },
                ],
            )
        msg = f"unsupported fake response: {fake_response!r}"
        raise RuntimeError(msg)

    return llm_callable


async def build_engine(
    tmp_path: Path,
    fake_llm: FakeLLM,
    framework: Framework,
    *,
    authz_rules: list[dict[str, str]] | None = None,
    extra_tools: list[Any] | None = None,
) -> tuple[AgentEngine, SessionStore, AssetManagement]:
    """Wire a complete AgentEngine on tmp_path with the given framework."""
    write_platform_yaml(tmp_path)
    backend = SqliteBackend(tmp_path / ".platform" / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)

    am = AssetManagement(root=tmp_path)
    await am.initialize()

    rules_cfg = authz_rules or [{"who": ".*", "what": ".*", "effect": "allow"}]
    authz = RuleBasedAuthorizationProvider(config={"rules": rules_cfg})

    secrets = DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
    sandbox = NativeSandboxProvider()
    memory = RecentBufferMemoryProvider(store=store)

    path_policy = PathPolicy(
        allowed_paths=["data/", "bundles/"],
        denied_paths=[".env"],
    )
    tools_sub = ToolsSubsystem(
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        path_policy=path_policy,
        root=tmp_path,
    )

    if extra_tools:
        await tools_sub.register_for_agent(
            agent_id=AgentId("ag_test"),
            tools=extra_tools,
        )

    skills_sub = SkillsSubsystem(asset_management=am)
    bus = InProcessEventBus()
    llm_callable = make_fake_llm_callable(fake_llm)

    engine = AgentEngine(
        store=store,
        bus=bus,
        asset_management=am,
        tools_subsystem=tools_sub,
        skills_subsystem=skills_sub,
        memory_provider=memory,
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        framework=framework,
        llm=llm_callable,
    )
    return engine, store, am


async def new_session_context(store: SessionStore) -> ExecutionContext:
    session = await store.create_session("ag_test", name="test-session")
    threads = await store.list_threads(session.session_id)
    return ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "Hello, agent."},
    )
