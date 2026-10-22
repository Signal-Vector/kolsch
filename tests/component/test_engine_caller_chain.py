"""Component tests — the engine exchange's caller chain (bug-158).

The engine builds ONE caller chain per exchange: the agent's own
catalog ref, then the frames that invoked it
(`ExecutionContext.caller_chain`). That object is what every authz
check of the exchange is judged against — the `agent.execute.*`
pre-flight, each model-driven tool call, and the force-invoke skill
read. These tests boot a real runtime against a small workspace, wrap
the shared authz provider's `allow` with a recorder, and pin the exact
tuple each call site passes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml

from engine import ExecutionContext, LLMRequest, LLMResponse
from engine.context import AgentExecutionError
from errors import ConfigDuplicateAgentId
from platform_runtime import bootstrap_runtime
from schemas import AgentId, EventType, derive_agent_id

if TYPE_CHECKING:
    from pathlib import Path

    from platform_runtime import PlatformRuntime
    from schemas import ResolvedAgentConfig

_AGENT_REF = "cc/agents/agent"
_WORKFLOW_REF = "cc/workflows/outer"


def _write_workspace(root: Path, *, rules: list[dict[str, Any]]) -> None:
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(exist_ok=True)
    (root / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
                "sandbox": {"variant": "native", "config": {}},
                "authorization": {
                    "variant": "rule_based",
                    "config": {"rules": rules},
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
    (root / "data" / "note.txt").write_text("hello\n", encoding="utf-8")

    bundle = root / "bundles" / "cc"
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "cc", "version": "0.1", "description": "cc"}),
        encoding="utf-8",
    )
    skill_dir = bundle / "skills" / "forced"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: forced\ndescription: A force-invoked skill.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (bundle / "agents" / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": "ag_cc",
                "name": "agent",
                "description": "caller-chain agent",
                "model": {"provider": "mock", "model": "mock-model"},
                "prompt": "hi",
                "io": {"mode": "conversational"},
                "subsystems": {
                    "tools": {
                        "variant": "standard",
                        "config": {"builtin": ["read"]},
                    },
                    "skills": {
                        "variant": "standard",
                        "config": {"enabled": [{"ref": "cc/skills/forced"}]},
                    },
                },
            }
        ),
        encoding="utf-8",
    )


_ALLOW_ALL = {"what": "*", "pattern": "*", "effect": "allow"}


def _read_then_done(path: str) -> Any:
    """Stub LLM: first turn asks for one `read`, second turn answers."""
    state = {"turn": 0}

    async def _llm(request: LLMRequest) -> LLMResponse:
        _ = request
        state["turn"] += 1
        if state["turn"] == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    {"id": "tc_chain_read", "name": "read", "arguments": {"path": path}}
                ],
            )
        return LLMResponse(content="done")

    return _llm


def _record_allow(
    runtime: PlatformRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str, Any]]:
    """Wrap the shared provider's `allow`; record (principal, action, chain)."""
    calls: list[tuple[str, str, Any]] = []
    original = runtime.authz.allow

    async def _spy(principal: str, action: str, **kwargs: Any) -> Any:
        calls.append((principal, action, kwargs.get("caller_chain", "<omitted>")))
        return await original(principal, action, **kwargs)

    monkeypatch.setattr(runtime.authz, "allow", _spy)
    return calls


async def _context(
    runtime: PlatformRuntime,
    config: ResolvedAgentConfig,
    *,
    trigger: dict[str, Any],
    caller_chain: tuple[str, ...] = (),
) -> ExecutionContext:
    session = await runtime.store.create_session(str(config.agent_id), name="chain")
    threads = await runtime.store.list_threads(session.session_id)
    return ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger=trigger,
        caller_chain=caller_chain,
    )


async def test_chat_style_exchange_chain_is_agent_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat / `kolsch run` / API shape (no callers): the pre-flight and
    the model-driven tool call both see `(A,)`."""
    _write_workspace(tmp_path, rules=[_ALLOW_ALL])
    runtime = await bootstrap_runtime(
        root=tmp_path, llm=_read_then_done(str(tmp_path / "data" / "note.txt"))
    )
    config = await runtime.build_resolved_config(_AGENT_REF)
    calls = _record_allow(runtime, monkeypatch)
    ctx = await _context(
        runtime, config, trigger={"kind": "user_message", "content": "read it"}
    )

    events = [e async for e in runtime.engine.execute_stream(config, ctx)]

    assert any(e.type == EventType.TOOL_RESULT for e in events)
    assert calls == [
        ("user.local", "agent.execute.ag_cc", (_AGENT_REF,)),
        ("agent.ag_cc", "kolsch/tools/read", (_AGENT_REF,)),
    ]


async def test_callers_follow_the_agent_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ExecutionContext.caller_chain` holds the callers only; the engine
    prepends the agent's own ref, for the pre-flight and tool calls."""
    _write_workspace(tmp_path, rules=[_ALLOW_ALL])
    runtime = await bootstrap_runtime(
        root=tmp_path, llm=_read_then_done(str(tmp_path / "data" / "note.txt"))
    )
    config = await runtime.build_resolved_config(_AGENT_REF)
    calls = _record_allow(runtime, monkeypatch)
    ctx = await _context(
        runtime,
        config,
        trigger={"kind": "user_message", "content": "read it"},
        caller_chain=(_WORKFLOW_REF,),
    )

    _ = [e async for e in runtime.engine.execute_stream(config, ctx)]

    assert [chain for _p, _a, chain in calls] == [
        (_AGENT_REF, _WORKFLOW_REF),
        (_AGENT_REF, _WORKFLOW_REF),
    ]


async def test_force_invoke_skill_read_uses_exchange_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The force-invoke SKILL.md read (`principal="system"`) is judged
    against the same `(A,)` chain."""
    _write_workspace(tmp_path, rules=[_ALLOW_ALL])

    async def _done(request: LLMRequest) -> LLMResponse:
        _ = request
        return LLMResponse(content="ok")

    runtime = await bootstrap_runtime(root=tmp_path, llm=_done)
    config = await runtime.build_resolved_config(_AGENT_REF)
    calls = _record_allow(runtime, monkeypatch)
    ctx = await _context(
        runtime,
        config,
        trigger={
            "kind": "user_message_with_force_skill",
            "content": "Use the skill.",
            "force_skills": ["cc/skills/forced"],
        },
    )

    _ = [e async for e in runtime.engine.execute_stream(config, ctx)]

    assert ("system", "kolsch/tools/read", (_AGENT_REF,)) in calls


async def test_scoped_deny_blocks_force_invoke_read_for_that_agent(
    tmp_path: Path,
) -> None:
    """End to end: a `from: A` deny on `read` now blocks A's
    force-invoke read, with the chain recorded on the denial."""
    _write_workspace(
        tmp_path,
        rules=[
            # `what: "*"` + a SKILL.md path pattern: this workspace has
            # no catalogued `kolsch/tools/read` (the builtin is not a
            # bundle file), and a literal `what:` ref must resolve.
            {
                "what": "*",
                "pattern": "*/SKILL.md",
                "from": _AGENT_REF,
                "effect": "deny",
            },
            _ALLOW_ALL,
        ],
    )

    async def _done(request: LLMRequest) -> LLMResponse:
        _ = request
        return LLMResponse(content="ok")

    runtime = await bootstrap_runtime(root=tmp_path, llm=_done)
    config = await runtime.build_resolved_config(_AGENT_REF)
    ctx = await _context(
        runtime,
        config,
        trigger={
            "kind": "user_message_with_force_skill",
            "content": "Use the skill.",
            "force_skills": ["cc/skills/forced"],
        },
    )

    with pytest.raises(AgentExecutionError):
        await runtime.engine.execute(config, ctx)

    events = [e async for e in runtime.store.read_path_to_leaf(ctx.thread_id)]
    (failed,) = [e for e in events if e.type == EventType.TOOL_FAILED]
    assert failed.data["error"]["type"] == "tool.unauthorized"
    assert failed.data["error"]["detail"]["caller_chain"] == [_AGENT_REF]
    assert failed.data["error"]["detail"]["matched_rule"]["from_"] == _AGENT_REF


async def test_non_catalog_agent_uses_callers_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent id no catalog entry maps to contributes no frame: the
    chain is the callers alone, and unattributed when there are none."""
    _write_workspace(tmp_path, rules=[_ALLOW_ALL])

    async def _done(request: LLMRequest) -> LLMResponse:
        _ = request
        return LLMResponse(content="ok")

    runtime = await bootstrap_runtime(root=tmp_path, llm=_done)
    built = await runtime.build_resolved_config(_AGENT_REF)
    config = built.model_copy(update={"agent_id": AgentId("ag_synthetic")})
    assert runtime.asset_management.get_ref_for_agent_id("ag_synthetic") is None
    calls = _record_allow(runtime, monkeypatch)

    for callers in ((), (_WORKFLOW_REF,)):
        ctx = await _context(
            runtime,
            config,
            trigger={"kind": "user_message", "content": "hi"},
            caller_chain=callers,
        )
        _ = [e async for e in runtime.engine.execute_stream(config, ctx)]

    assert calls == [
        ("user.local", "agent.execute.ag_synthetic", ()),
        ("user.local", "agent.execute.ag_synthetic", (_WORKFLOW_REF,)),
    ]


async def test_get_ref_for_agent_id_explicit_and_derived(tmp_path: Path) -> None:
    """`get_ref_for_agent_id` maps an explicit `agent_id:` through the
    `build_resolved_config` cache, and a derived id through the
    catalog walk."""
    _write_workspace(tmp_path, rules=[_ALLOW_ALL])
    derived = tmp_path / "bundles" / "cc" / "agents" / "plain.yaml"
    derived.write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "name": "plain",
                "description": "derived-id agent",
                "model": {"provider": "mock", "model": "mock-model"},
                "prompt": "hi",
                "io": {"mode": "conversational"},
            }
        ),
        encoding="utf-8",
    )
    runtime = await bootstrap_runtime(root=tmp_path)
    assets = runtime.asset_management

    # Explicit id: not derivable from the ref, so only the cache knows it.
    assert assets.get_ref_for_agent_id("ag_cc") is None
    await runtime.build_resolved_config(_AGENT_REF)
    assert assets.get_ref_for_agent_id("ag_cc") == _AGENT_REF

    # Derived id: found by the catalog walk before any build, then by
    # the cache once built.
    derived_id = str(derive_agent_id("cc", "plain"))
    assert assets.get_ref_for_agent_id(derived_id) == "cc/agents/plain"
    plain = await runtime.build_resolved_config("cc/agents/plain")
    assert str(plain.agent_id) == derived_id
    assert assets.get_ref_for_agent_id(derived_id) == "cc/agents/plain"
    assert assets.get_ref_for_agent_id("ag_nobody") is None


def _write_agent(root: Path, name: str, *, agent_id: str | None) -> None:
    doc: dict[str, Any] = {
        "schema_version": "0.1",
        "assembly_version": "1",
        "name": name,
        "description": f"{name} agent",
        "model": {"provider": "mock", "model": "mock-model"},
        "prompt": "hi",
        "io": {"mode": "conversational"},
    }
    if agent_id is not None:
        doc["agent_id"] = agent_id
    (root / "bundles" / "cc" / "agents" / f"{name}.yaml").write_text(
        yaml.safe_dump(doc), encoding="utf-8"
    )


async def test_duplicate_agent_id_across_two_agents_is_refused(tmp_path: Path) -> None:
    """S4 — a second agent claiming an already-mapped `agent_id` cannot
    be built, so it can never be authorized as (or attributed to) the
    first agent's ref. Rebuilding the same agent stays fine."""
    _write_workspace(tmp_path, rules=[_ALLOW_ALL])
    _write_agent(tmp_path, "impostor", agent_id="ag_cc")
    runtime = await bootstrap_runtime(root=tmp_path)

    await runtime.build_resolved_config(_AGENT_REF)
    await runtime.build_resolved_config(_AGENT_REF)  # same agent: fine

    with pytest.raises(ConfigDuplicateAgentId) as excinfo:
        await runtime.build_resolved_config("cc/agents/impostor")
    assert excinfo.value.detail == {
        "agent_id": "ag_cc",
        "refs": [_AGENT_REF, "cc/agents/impostor"],
    }
    assert "'ag_cc'" in str(excinfo.value)
    assert _AGENT_REF in str(excinfo.value)
    assert "cc/agents/impostor" in str(excinfo.value)
    # The original mapping is untouched.
    assert runtime.asset_management.get_ref_for_agent_id("ag_cc") == _AGENT_REF


async def test_distinct_agent_ids_build_side_by_side(tmp_path: Path) -> None:
    _write_workspace(tmp_path, rules=[_ALLOW_ALL])
    _write_agent(tmp_path, "other", agent_id=None)
    runtime = await bootstrap_runtime(root=tmp_path)

    await runtime.build_resolved_config(_AGENT_REF)
    other = await runtime.build_resolved_config("cc/agents/other")

    assets = runtime.asset_management
    assert assets.get_ref_for_agent_id("ag_cc") == _AGENT_REF
    assert assets.get_ref_for_agent_id(str(other.agent_id)) == "cc/agents/other"
