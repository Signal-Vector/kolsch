"""feat-139 — opt-in ``@tool(hide_if_secrets_missing=True)``.

Test 1: registration semantics on both intake channels (programmatic
``tools=`` and declared ``declared_paths=``).
Test 2: the engine writes ONE durable ``tool.unavailable`` per session
per hidden tool — across exchanges and a rebuilt runtime — and the
hidden tool never reaches the model.
Test 3: the prompt ``tools`` variable excludes hidden tools.

Real providers (dotenv secrets, native sandbox, rule-based authz); the
LLM is a recording stub, so no tokens are spent.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from pydantic import BaseModel

from engine import LLMResponse
from engine.context import ExecutionContext
from engine.engine import _Exchange
from errors import SecretNotFound, ToolSetupFailed
from platform_runtime import bootstrap_runtime
from providers import (
    DotenvSecretsProvider,
    NativeSandboxProvider,
    RuleBasedAuthorizationProvider,
)
from schemas import AgentId, EventType
from tests.fixtures.assertions import assert_event_content
from tools import ToolContext, ToolsSubsystem, tool

if TYPE_CHECKING:
    from pathlib import Path

    from engine.framework import LLMRequest

_SECRET_VALUE = "sk-feat139-must-never-appear"
_HINT = "Run `kolsch connect demo`."


class _Args(BaseModel):
    q: str = "x"


@tool(
    name="flagged",
    description="Needs a secret; hidden when it is missing.",
    args_schema=_Args,
    secrets_needed=["NEEDY_KEY"],
    hide_if_secrets_missing=True,
    unavailable_hint=_HINT,
)
async def flagged(args: _Args, ctx: ToolContext) -> dict[str, Any]:
    return {"ok": bool(ctx.secrets.get("NEEDY_KEY"))}


@tool(
    name="unflagged",
    description="Needs a secret; fails registration when it is missing.",
    args_schema=_Args,
    secrets_needed=["NEEDY_KEY"],
)
async def unflagged(args: _Args, ctx: ToolContext) -> dict[str, Any]:
    return {"ok": True}


@tool(name="sibling", description="No secrets.", args_schema=_Args)
async def sibling(args: _Args, ctx: ToolContext) -> dict[str, Any]:
    return {"q": args.q}


def _subsystem(tmp_path: Path, env: dict[str, str]) -> ToolsSubsystem:
    (tmp_path / ".env").write_text(
        "".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8"
    )
    return ToolsSubsystem(
        secrets=DotenvSecretsProvider(root=tmp_path, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(),
        authz=RuleBasedAuthorizationProvider(
            config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]}
        ),
    )


_FLAGGED_SRC = """
from pydantic import BaseModel
from tools import tool


class _Args(BaseModel):
    q: str = "x"


@tool(
    name="flagged",
    description="Needs a secret; hidden when it is missing.",
    args_schema=_Args,
    secrets_needed=["NEEDY_KEY"],
    hide_if_secrets_missing=True,
    unavailable_hint="Run `kolsch connect demo`.",
)
async def flagged(args, ctx):
    return {"ok": bool(ctx.secrets.get("NEEDY_KEY"))}
"""

_UNFLAGGED_SRC = """
from pydantic import BaseModel
from tools import tool


class _Args(BaseModel):
    q: str = "x"


@tool(
    name="unflagged",
    description="Needs a secret.",
    args_schema=_Args,
    secrets_needed=["NEEDY_KEY"],
)
async def unflagged(args, ctx):
    return {"ok": True}
"""

_SIBLING_SRC = """
from pydantic import BaseModel
from tools import tool


class _Args(BaseModel):
    q: str = "x"


@tool(name="sibling", description="No secrets.", args_schema=_Args)
async def sibling(args, ctx):
    return {"q": args.q}
"""


def _write_tool_files(tmp_path: Path) -> dict[str, Path]:
    tools_dir = tmp_path / "tool_src"
    tools_dir.mkdir(exist_ok=True)
    paths = {}
    for name, src in (
        ("flagged", _FLAGGED_SRC),
        ("unflagged", _UNFLAGGED_SRC),
        ("sibling", _SIBLING_SRC),
    ):
        path = tools_dir / f"feat139_{name}.py"
        path.write_text(src, encoding="utf-8")
        paths[name] = path
    return paths


async def _register(
    sub: ToolsSubsystem,
    channel: str,
    names: list[str],
    files: dict[str, Path],
) -> list[str]:
    fns = {"flagged": flagged, "unflagged": unflagged, "sibling": sibling}
    if channel == "programmatic":
        infos = await sub.register_for_agent(
            agent_id=AgentId("ag_t"), tools=[fns[n] for n in names]
        )
    else:
        infos = await sub.register_for_agent(
            agent_id=AgentId("ag_t"), declared_paths=[files[n] for n in names]
        )
    return sorted(i.name for i in infos)


# ---------------------------------------------------------------------------
# Test 1 — registration semantics
# ---------------------------------------------------------------------------

_CHANNELS = ["programmatic", "declared"]


@pytest.mark.parametrize("channel", _CHANNELS)
async def test_flagged_tool_with_missing_secret_is_hidden(
    tmp_path: Path, channel: str
) -> None:
    """(a) No raise; not registered; the sibling still registers."""
    sub = _subsystem(tmp_path, {})
    names = await _register(
        sub, channel, ["flagged", "sibling"], _write_tool_files(tmp_path)
    )
    assert names == ["sibling"]
    registered = sorted(i.name for i in sub.list_registered_tools(AgentId("ag_t")))
    assert registered == ["sibling"]
    unavailable = sub.list_unavailable_tools(AgentId("ag_t"))
    assert [u.model_dump() for u in unavailable] == [
        {
            "tool_name": "flagged",
            "missing_secrets": ["NEEDY_KEY"],
            "recovery_hint": _HINT,
        }
    ]
    # The sibling still dispatches normally.
    result = await sub.invoke_tool(
        "sibling",
        {"q": "hi"},
        ToolContext(
            agent_id=AgentId("ag_t"),
            session_id="ses_00000000-0000-0000-0000-000000000000",
            thread_id="th_00000000-0000-0000-0000-000000000000",
            principal="agent.ag_t",
            secrets={},
        ),
    )
    assert result.result == {"q": "hi"}


@pytest.mark.parametrize("channel", _CHANNELS)
async def test_flagged_tool_with_secret_present_registers(
    tmp_path: Path, channel: str
) -> None:
    """(b) Secret present → registers exactly as today."""
    sub = _subsystem(tmp_path, {"NEEDY_KEY": _SECRET_VALUE})
    names = await _register(
        sub, channel, ["flagged", "sibling"], _write_tool_files(tmp_path)
    )
    assert names == ["flagged", "sibling"]
    assert sub.list_unavailable_tools(AgentId("ag_t")) == []


@pytest.mark.parametrize("channel", _CHANNELS)
async def test_unflagged_tool_with_missing_secret_still_fails(
    tmp_path: Path, channel: str
) -> None:
    """(c) Default fail-fast unchanged."""
    sub = _subsystem(tmp_path, {})
    with pytest.raises(ToolSetupFailed) as exc_info:
        await _register(sub, channel, ["unflagged"], _write_tool_files(tmp_path))
    assert isinstance(exc_info.value.__cause__, SecretNotFound)


async def test_reregistration_drops_stale_copy_and_refreshes_unavailable(
    tmp_path: Path,
) -> None:
    """A tool registered earlier is removed when its secret disappears."""
    sub = _subsystem(tmp_path, {"NEEDY_KEY": _SECRET_VALUE})
    assert await _register(sub, "programmatic", ["flagged"], {}) == ["flagged"]
    sub._secrets = DotenvSecretsProvider(root=tmp_path / "empty", config={})
    assert await _register(sub, "programmatic", ["flagged"], {}) == []
    assert [u.tool_name for u in sub.list_unavailable_tools(AgentId("ag_t"))] == [
        "flagged"
    ]
    sub._secrets = DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
    assert await _register(sub, "programmatic", ["flagged"], {}) == ["flagged"]
    assert sub.list_unavailable_tools(AgentId("ag_t")) == []


def test_default_hint_derived_from_secret_names() -> None:
    @tool(
        name="nohint",
        description="d",
        args_schema=_Args,
        secrets_needed=["A_KEY", "B_KEY"],
        hide_if_secrets_missing=True,
    )
    async def nohint(args: _Args, ctx: ToolContext) -> None:
        return None

    defn = nohint._tool_definition
    assert defn.hide_if_secrets_missing is True
    assert defn.unavailable_hint is None
    assert sibling._tool_definition.hide_if_secrets_missing is False


async def test_default_hint_text(tmp_path: Path) -> None:
    @tool(
        name="nohint2",
        description="d",
        args_schema=_Args,
        secrets_needed=["A_KEY", "B_KEY"],
        hide_if_secrets_missing=True,
    )
    async def nohint2(args: _Args, ctx: ToolContext) -> None:
        return None

    sub = _subsystem(tmp_path, {"A_KEY": _SECRET_VALUE})
    await sub.register_for_agent(agent_id=AgentId("ag_t"), tools=[nohint2])
    (item,) = sub.list_unavailable_tools(AgentId("ag_t"))
    assert item.missing_secrets == ["B_KEY"]
    assert item.recovery_hint == "Set B_KEY in the workspace .env."


# ---------------------------------------------------------------------------
# Engine path — workspace + recording LLM
# ---------------------------------------------------------------------------


def _write_workspace(root: Path, *, env: dict[str, str], prompt: str) -> None:
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
    (root / ".env").write_text(
        "".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8"
    )
    bundle = root / "bundles" / "demo"
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "tools").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "demo", "version": "0.1", "description": "demo"}),
        encoding="utf-8",
    )
    (bundle / "tools" / "flagged.py").write_text(_FLAGGED_SRC, encoding="utf-8")
    (bundle / "tools" / "sibling.py").write_text(_SIBLING_SRC, encoding="utf-8")
    (bundle / "agents" / "hider.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": "ag_hider",
                "name": "hider",
                "description": "declares a flagged tool",
                "model": {"provider": "mock", "model": "mock-model"},
                "prompt": prompt,
                "io": {"mode": "conversational"},
                "subsystems": {
                    "tools": {
                        "variant": "standard",
                        "config": {
                            "declared": [
                                {"ref": "demo/tools/flagged"},
                                {"ref": "demo/tools/sibling"},
                            ]
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )


class _RecordingLLM:
    """Returns a fixed text reply; records every request (no tokens)."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(content="ok")


async def _run_exchange(root: Path, llm: _RecordingLLM, session_id: Any) -> Any:
    runtime = await bootstrap_runtime(root=root, llm=llm)
    config = await runtime.build_resolved_config("demo/agents/hider")
    if session_id is None:
        session = await runtime.store.create_session(
            str(config.agent_id), name="feat-139"
        )
        session_id = session.session_id
    threads = await runtime.store.list_threads(session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "hello"},
    )
    await runtime.engine.execute(config, ctx)
    return runtime, session_id, threads[0].thread_id


def _jsonl_events(root: Path, session_id: Any) -> list[dict[str, Any]]:
    path = next((root / "sessions").rglob(f"{session_id}*.jsonl"))
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# Test 2 — tool.unavailable event + durable dedup
# ---------------------------------------------------------------------------


async def test_tool_unavailable_emitted_once_per_session_across_restart(
    tmp_path: Path,
) -> None:
    _write_workspace(tmp_path, env={"OTHER": _SECRET_VALUE}, prompt="You help.")
    llm = _RecordingLLM()

    # Two exchanges on one runtime...
    runtime, session_id, thread_id = await _run_exchange(tmp_path, llm, None)
    threads = await runtime.store.list_threads(session_id)
    ctx = ExecutionContext(
        thread_id=thread_id,
        trigger={"kind": "user_message", "content": "again"},
    )
    config = await runtime.build_resolved_config("demo/agents/hider")
    await runtime.engine.execute(config, ctx)
    assert len(threads) == 1

    # ...then a third on a freshly bootstrapped runtime (restart).
    await _run_exchange(tmp_path, llm, session_id)

    events = _jsonl_events(tmp_path, session_id)
    types = [e["type"] for e in events]
    assert types.count("thread.exchange_completed") == 3
    assert "thread.exchange_failed" not in types
    unavailable = [e for e in events if e["type"] == "tool.unavailable"]
    assert len(unavailable) == 1
    assert_event_content(
        unavailable[0],
        tool_name="flagged",
        missing_secrets=["NEEDY_KEY"],
        recovery_hint=_HINT,
    )
    # Written during setup, before the first exchange starts.
    assert types.index("tool.unavailable") < types.index("thread.exchange_started")
    # The hidden tool never reaches the model; the sibling does.
    for request in llm.requests:
        names = {t["name"] for t in request.tools}
        assert names == {"sibling"}
    # No secret VALUE anywhere in the session log.
    assert _SECRET_VALUE not in json.dumps(events)


async def test_tool_unavailable_not_emitted_when_secret_present(
    tmp_path: Path,
) -> None:
    _write_workspace(tmp_path, env={"NEEDY_KEY": _SECRET_VALUE}, prompt="You help.")
    llm = _RecordingLLM()
    _, session_id, _ = await _run_exchange(tmp_path, llm, None)
    events = _jsonl_events(tmp_path, session_id)
    assert not [e for e in events if e["type"] == EventType.TOOL_UNAVAILABLE.value]
    assert {t["name"] for t in llm.requests[0].tools} == {"flagged", "sibling"}


# ---------------------------------------------------------------------------
# Test 3 — prompt `tools` variable excludes hidden tools
# ---------------------------------------------------------------------------

_GUARDED = 'You help.{% if "flagged" in tools %} FLAGGED-AVAILABLE{% endif %}'


@pytest.mark.parametrize(
    ("env", "expect_text"),
    [({}, False), ({"NEEDY_KEY": _SECRET_VALUE}, True)],
    ids=["hidden", "available"],
)
async def test_tools_var_hidden_tool_guard(
    tmp_path: Path, env: dict[str, str], *, expect_text: bool
) -> None:
    _write_workspace(tmp_path, env=env, prompt=_GUARDED)
    llm = _RecordingLLM()
    await _run_exchange(tmp_path, llm, None)
    system = next(m for m in llm.requests[0].messages if m.role == "system")
    assert ("FLAGGED-AVAILABLE" in system.content) is expect_text


async def test_tools_var_hidden_guard_is_exact_membership(tmp_path: Path) -> None:
    """A hidden ``flagged`` with a registered ``flagged_batch`` sibling must
    NOT render the guard — ``in tools`` is exact, unlike a substring test on
    the ``tools.names()`` display string (shown for contrast)."""
    prompt = (
        'You help.{% if "flagged" in tools %} EXACT-HIT{% endif %}'
        '{% if "flagged" in tools.names() %} SUBSTRING-HIT{% endif %}'
    )
    _write_workspace(tmp_path, env={}, prompt=prompt)
    bundle = tmp_path / "bundles" / "demo"
    (bundle / "tools" / "flagged_batch.py").write_text(
        _SIBLING_SRC.replace('name="sibling"', 'name="flagged_batch"'),
        encoding="utf-8",
    )
    agent_path = bundle / "agents" / "hider.yaml"
    agent = yaml.safe_load(agent_path.read_text(encoding="utf-8"))
    agent["subsystems"]["tools"]["config"]["declared"].append(
        {"ref": "demo/tools/flagged_batch"}
    )
    agent_path.write_text(yaml.safe_dump(agent), encoding="utf-8")

    llm = _RecordingLLM()
    await _run_exchange(tmp_path, llm, None)
    assert {t["name"] for t in llm.requests[0].tools} == {"sibling", "flagged_batch"}
    system = next(m for m in llm.requests[0].messages if m.role == "system")
    assert "EXACT-HIT" not in system.content
    assert "SUBSTRING-HIT" in system.content  # why `tools.names()` is wrong


# ---------------------------------------------------------------------------
# Review fix pass — concurrency, leak, multi-thread
# ---------------------------------------------------------------------------

_LABEL: contextvars.ContextVar[str] = contextvars.ContextVar("feat139_label")


class _LabelledSecrets:
    """Secrets provider whose view depends on which exchange is asking.

    Exchange "A" sees NEEDY_KEY missing; exchange "B" sees it present.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def exists(self, name: str) -> bool:
        if name == "NEEDY_KEY":
            return _LABEL.get() == "B"
        return bool(await self._inner.exists(name))

    async def resolve(self, name: str) -> str:
        if name == "NEEDY_KEY" and _LABEL.get() == "B":
            return _SECRET_VALUE
        return str(await self._inner.resolve(name))

    async def list_names(self) -> list[str]:
        return list(await self._inner.list_names())


async def test_tool_unavailable_snapshot_survives_concurrent_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another session's registration between A's registration and A's emit
    must not erase A's hidden-tool record.

    The per-agent registration is shared by every session. Session B's
    registration (secret present → nothing hidden) is forced to complete
    inside A's window between ``register_for_agent`` and the emit; A must
    still write its ``tool.unavailable`` from its own snapshot.
    """
    _write_workspace(tmp_path, env={}, prompt="You help.")
    llm = _RecordingLLM()
    runtime = await bootstrap_runtime(root=tmp_path, llm=llm)
    tools_sub = runtime.engine._tools
    tools_sub._secrets = _LabelledSecrets(tools_sub._secrets)

    b_registered = asyncio.Event()
    real_register = tools_sub.register_for_agent

    async def _register(**kwargs: Any) -> Any:
        result = await real_register(**kwargs)
        if _LABEL.get() == "B":
            b_registered.set()
        return result

    monkeypatch.setattr(tools_sub, "register_for_agent", _register)
    real_render = _Exchange._render_prompt_if_needed

    async def _gated_render(self: _Exchange) -> None:
        if _LABEL.get() == "A":
            await asyncio.wait_for(b_registered.wait(), timeout=10)
        await real_render(self)

    monkeypatch.setattr(_Exchange, "_render_prompt_if_needed", _gated_render)

    config = await runtime.build_resolved_config("demo/agents/hider")

    async def _run(label: str) -> Any:
        _LABEL.set(label)
        session = await runtime.store.create_session(
            str(config.agent_id), name=f"feat-139-{label}"
        )
        (thread,) = await runtime.store.list_threads(session.session_id)
        await runtime.engine.execute(
            config,
            ExecutionContext(
                thread_id=thread.thread_id,
                trigger={"kind": "user_message", "content": "hi"},
            ),
        )
        return session.session_id

    session_a, session_b = await asyncio.gather(_run("A"), _run("B"))

    events_a = _jsonl_events(tmp_path, session_a)
    events_b = _jsonl_events(tmp_path, session_b)
    assert [
        e["data"]["tool_name"] for e in events_a if e["type"] == "tool.unavailable"
    ] == ["flagged"]
    assert not [e for e in events_b if e["type"] == "tool.unavailable"]


_TWO_SECRET_SRC = """
from pydantic import BaseModel
from tools import tool


class _Args(BaseModel):
    q: str = "x"


@tool(
    name="flagged",
    description="Needs two secrets.",
    args_schema=_Args,
    secrets_needed=["A_KEY", "B_KEY"],
    hide_if_secrets_missing=True,
)
async def flagged(args, ctx):
    return {"ok": True}
"""


async def test_tool_unavailable_never_leaks_a_present_secret_value(
    tmp_path: Path,
) -> None:
    """One secret present (distinct value), one missing: the present
    value appears nowhere — not in the raw JSONL, not under ``logs/``."""
    present_value = "sk-feat139-PRESENT-A-KEY-7f3c"
    _write_workspace(tmp_path, env={"A_KEY": present_value}, prompt="You help.")
    (tmp_path / "bundles" / "demo" / "tools" / "flagged.py").write_text(
        _TWO_SECRET_SRC, encoding="utf-8"
    )
    llm = _RecordingLLM()
    _, session_id, _ = await _run_exchange(tmp_path, llm, None)

    path = next((tmp_path / "sessions").rglob(f"{session_id}*.jsonl"))
    raw = path.read_text(encoding="utf-8")
    assert present_value not in raw
    for log_file in (tmp_path / "logs").rglob("*"):
        if log_file.is_file():
            assert present_value not in log_file.read_text(
                encoding="utf-8", errors="replace"
            ), log_file
    (event,) = [
        e
        for e in _jsonl_events(tmp_path, session_id)
        if e["type"] == "tool.unavailable"
    ]
    assert_event_content(
        event,
        tool_name="flagged",
        missing_secrets=["B_KEY"],
        recovery_hint="Set B_KEY in the workspace .env.",
    )


async def test_tool_unavailable_once_across_threads_of_one_session(
    tmp_path: Path,
) -> None:
    """A session with two threads gets ONE ``tool.unavailable`` in total —
    also after a restart."""
    _write_workspace(tmp_path, env={}, prompt="You help.")
    llm = _RecordingLLM()
    runtime, session_id, first_thread = await _run_exchange(tmp_path, llm, None)
    config = await runtime.build_resolved_config("demo/agents/hider")
    second = await runtime.store.create_thread(session_id, config.agent_id)
    await runtime.engine.execute(
        config,
        ExecutionContext(
            thread_id=second.thread_id,
            trigger={"kind": "user_message", "content": "second thread"},
        ),
    )
    # Fresh process, the other thread again.
    runtime2 = await bootstrap_runtime(root=tmp_path, llm=llm)
    await runtime2.engine.execute(
        config,
        ExecutionContext(
            thread_id=second.thread_id,
            trigger={"kind": "user_message", "content": "after restart"},
        ),
    )

    events = _jsonl_events(tmp_path, session_id)
    assert {e["thread_id"] for e in events} >= {
        str(first_thread),
        str(second.thread_id),
    }
    assert len([e for e in events if e["type"] == "tool.unavailable"]) == 1
    assert [e["type"] for e in events].count("thread.exchange_completed") == 3


async def test_session_scan_skipped_once_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log is scanned once per session per process, not per exchange."""
    _write_workspace(tmp_path, env={}, prompt="You help.")
    llm = _RecordingLLM()
    runtime = await bootstrap_runtime(root=tmp_path, llm=llm)
    scans: list[str] = []
    real_scan = runtime.store.read_session_events_of_type

    async def _counting(session_id: Any, event_type: Any) -> Any:
        scans.append(str(session_id))
        return await real_scan(session_id, event_type)

    monkeypatch.setattr(runtime.store, "read_session_events_of_type", _counting)
    config = await runtime.build_resolved_config("demo/agents/hider")
    session = await runtime.store.create_session(str(config.agent_id), name="x")
    (thread,) = await runtime.store.list_threads(session.session_id)
    for content in ("one", "two", "three"):
        await runtime.engine.execute(
            config,
            ExecutionContext(
                thread_id=thread.thread_id,
                trigger={"kind": "user_message", "content": content},
            ),
        )
    assert scans == [str(session.session_id)]
