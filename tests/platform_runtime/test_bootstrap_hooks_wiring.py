"""spec-131 Step 6 — real `HookDispatcher` wired at engine construction.

`src/platform_runtime/runtime.py:bootstrap_runtime` is kolsch's **single**
real `AgentEngine(...)` construction site — verified by grepping the
whole repo for `AgentEngine(` outside `tests/`: the only production hit
is `bootstrap_runtime`'s call. Every live consumer named in the spec's
Acceptance Criterion #7 reaches that one engine instance rather than
constructing its own:

- `kolsch chat` — `src/cli/chat/session.py:build_session` calls
  `bootstrap_runtime` and threads `runtime.engine` into
  `ChatApp(engine=session.runtime.engine, ...)`
  (`src/cli/main.py:1070`).
- `kolsch run` — `src/cli/main.py`'s run command calls
  `bootstrap_runtime` and calls `runtime.engine.execute_stream`/
  `.execute` directly (`main.py:707,716`).
- Sub-agent/`author_workflow` runs — `src/cli/_workflow_runtime.py`
  passes `engine=runtime.engine` into `register_builtins`, which
  threads it into `AgentRunner(engine=engine, ...)`
  (`src/workflows/builtins/__init__.py:256-261`).
- The HTTP API — `src/api/app.py:91` calls `bootstrap_runtime` and
  `register_routes(app, runtime)`; `src/api/routes.py:245`'s
  `execute_agent_stream` route calls `runtime.engine.execute_stream`.

This file tests the one real construction site directly: that
`bootstrap_runtime` builds a real `HookDispatcher` (not `None`) from
`platform.yaml`'s `hooks:` section (via `AssetManagement.hooks_config()`)
and injects it as `AgentEngine(..., observer=...)`. Per-surface "does
this surface reach the same engine" is a code-inspection fact (cited
above) re-checked below by constructing each surface's real objects
(driving `_cmd_chat`, `build_workflow_executor`, and `build_app`
directly, plus reading `runtime.engine` for the `kolsch run` surface)
and asserting identity, rather than 4 separate live-process
round-trips — the spec allows this for the HTTP API specifically when
construction is genuinely centralized (Implementation Step 6), which
it is here.

The true end-to-end fire — a real hook command actually running off a
real `execute_stream` event from the `bootstrap_runtime`-built engine —
is `test_configured_hook_fires_end_to_end_from_bootstrapped_engine`
below, using the same `_config()`/`ExecutionContext` fabrication pattern
`tests/engine/test_event_observer_seam.py` (Step 5) already established,
against a MockLLM so no real LLM call is needed.

**Finding worth flagging to the Reviewer/Coordinator:** the spec's Step
6 smoke-test suggestion ("configure a hook on `session.opened` ...")
does not actually reach the observer seam. `SESSION_OPENED` is written
directly to the durable store by
`store.session_store.SessionStore.create_session` at session-creation
time — it is NOT one of the events `_Exchange.run()` puts on the queue
`execute_stream` iterates (confirmed: `_run_body` starts with
`_preflight()` then `THREAD_EXCHANGE_STARTED`; Step 5's own
`test_event_observer_seam.py::test_observer_none_matches_pre_step5_behavior`
explicitly filters `SESSION_OPENED` OUT of the durable log before
comparing it to what `execute_stream` yielded). A `hooks:` rule
targeting `session.opened` is therefore accepted by config validation
(Step 1) and matched correctly by `match()` (Step 2) but can never
fire in practice, for the same structural reason documented for
`kolsch.prompt.answered`'s non-wildcard matcher case
(`HookDispatcher._warn_unmatchable_prompt_answered_rules`) — except
this one is unconditional, not matcher-dependent, and undocumented.
This is a pre-existing gap from Step 5's design (the observer seam
covers `execute_stream`'s yielded events, and `session.opened` was
never one of them), not something Step 6 introduces or can fix within
its own scope (wiring the dispatcher at construction sites). Tests
below use `thread.exchange_completed` instead, which Step 5's own
tests confirm IS reliably yielded by `execute_stream` on every
exchange.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

import yaml

from assets.platform_config import default_platform_payload
from cli._workflow_runtime import build_workflow_executor
from engine import ExecutionContext, LLMRequest, LLMResponse
from platform_runtime import bootstrap_runtime
from platform_runtime.hooks import HookDispatcher
from schemas import AgentId, EventType, IOSpec, ModelSpec, ResolvedAgentConfig
from tests.fixtures.mock_llm import MockLLM, TextResponse
from workflows.parser import load_workflow
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _llm_callable(
    mock_llm: MockLLM,
) -> Any:
    """Adapt a `MockLLM` into the `Callable[[LLMRequest], Awaitable[LLMResponse]]`
    shape `bootstrap_runtime(llm=...)` expects.

    `MockLLM` (`tests/fixtures/mock_llm.py`) is a Pydantic model with an
    `agenerate(context: dict)` method, not a bare callable — mirrors
    `tests/component/test_engine.py::_build_engine`'s `llm_callable`
    closure exactly (dict-context adaptation + `TextResponse`/
    `ToolCallResponse` translation back to `LLMResponse`).
    """

    async def _call(request: LLMRequest) -> LLMResponse:
        context: dict[str, Any] = {
            "messages": [m.model_dump() for m in request.messages],
            "tools": list(request.tools),
            "model": request.model,
            "system_prompt": next(
                (m.content for m in request.messages if m.role == "system"),
                "",
            ),
        }
        mock_response = await mock_llm.agenerate(context)
        if isinstance(mock_response, TextResponse):
            return LLMResponse(content=mock_response.text)
        msg = f"unsupported mock response in this test helper: {mock_response!r}"
        raise RuntimeError(msg)

    return _call


def _write_platform_yaml(root: Path, overrides: dict[str, Any]) -> None:
    """Write a platform.yaml with setup-time defaults + test overrides.

    Mirrors `tests/component/test_llm_call_timeout_bootstrap.py`'s
    helper — `bootstrap_runtime` requires the mandatory sections, so
    starting from `default_platform_payload()` and merging the
    override at the top level is the minimal valid fixture.
    """
    payload = default_platform_payload()
    payload.update(overrides)
    (root / "platform.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")


def _config() -> ResolvedAgentConfig:
    """A minimal valid agent config, matching `tests/component/test_engine.py`."""
    return ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="1",
        agent_id=AgentId("ag_test"),
        name="test-agent",
        description="A test agent.",
        model=ModelSpec(provider="mock", model="mock-model"),
        prompt="You are a helpful test agent.",
        io=IOSpec(mode="conversational"),
    )


async def _new_session_context(runtime: Any) -> ExecutionContext:
    session = await runtime.store.create_session("ag_test", name="test-session")
    threads = await runtime.store.list_threads(session.session_id)
    return ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": "Hello, agent."},
    )


# ---------------------------------------------------------------------------
# The construction site itself: bootstrap_runtime wires a real HookDispatcher
# ---------------------------------------------------------------------------


async def test_bootstrap_wires_real_hook_dispatcher_with_configured_rules(
    tmp_path: Path,
) -> None:
    """With `hooks:` configured, `runtime.engine`'s observer is a real,
    non-empty `HookDispatcher` built from those rules — not `None`.

    This is a pure config-wiring check (the rule's `event` string
    round-trips into the dispatcher unchanged); it does not assert the
    hook actually fires, so `session.opened` is fine here even though
    it never reaches the observer seam in practice (see the module
    docstring's "Finding" section) — `HookRule` validation and
    `HookDispatcher` construction don't know or care whether an event
    string is reachable, only Step 1/4's own schema constraints.
    """
    _write_platform_yaml(
        tmp_path,
        {
            "hooks": [
                {
                    "event": "session.opened",
                    "command": "true",
                    "timeout_seconds": 5,
                },
            ],
        },
    )

    runtime = await bootstrap_runtime(root=tmp_path)

    observer = runtime.engine._observer
    assert isinstance(observer, HookDispatcher)
    assert len(observer._rules) == 1
    assert observer._rules[0].event == "session.opened"
    assert observer._rules[0].command == "true"


async def test_bootstrap_wires_hook_dispatcher_even_with_zero_rules(
    tmp_path: Path,
) -> None:
    """With `hooks:` absent (the common case), `bootstrap_runtime` still
    constructs a real `HookDispatcher([])`, not `observer=None`.

    Step 6's documented choice: an empty-rules dispatcher is a
    harmless, trivially-cheap no-op (`HookDispatcher.__init__` just
    stores `[]` and loops zero times in its warning scan), so
    constructing it unconditionally avoids a second branch at the
    call site and keeps `runtime.engine._observer` always a real
    `HookDispatcher` instance regardless of whether hooks are
    configured — one less "is this even wired" question when
    debugging. The zero-overhead guarantee (Acceptance Criterion #8)
    comes from `execute_stream`'s own `observer is not None`
    branch-skip (Step 5), not from withholding construction here.
    """
    # No hooks section at all — matches the "absent = off" convention.
    _write_platform_yaml(tmp_path, {})

    runtime = await bootstrap_runtime(root=tmp_path)

    observer = runtime.engine._observer
    assert isinstance(observer, HookDispatcher)
    assert observer._rules == []


async def test_bootstrap_hook_dispatcher_matches_am_hooks_config(
    tmp_path: Path,
) -> None:
    """The rules on the wired dispatcher are exactly
    `AssetManagement.hooks_config()`'s parsed output — confirms Step 6
    reads from the same source Step 1 built, not a second parse path."""
    _write_platform_yaml(
        tmp_path,
        {
            "hooks": [
                {"event": "tool.failed", "matcher": "kolsch/bash", "command": "true"},
                {"event": "session.opened", "command": "false", "timeout_seconds": 3},
            ],
        },
    )

    runtime = await bootstrap_runtime(root=tmp_path)

    expected = runtime.asset_management.hooks_config()
    observer = runtime.engine._observer
    assert isinstance(observer, HookDispatcher)
    assert observer._rules == expected


# ---------------------------------------------------------------------------
# Per-surface confirmation: all 4 live consumers share the ONE engine
# ---------------------------------------------------------------------------


def _write_chat_workspace(tmp_path: Path) -> None:
    """Minimal on-disk workspace + one mock-provider agent for `_cmd_chat`.

    Mirrors `tests/cli/test_chat_resume_picker.py`'s `_write_workspace`
    fixture exactly (including `defaults: {}` rather than
    `default_platform_payload()`'s `defaults.memory` block, which trips
    a Layer 1 `extra_forbidden` validation error against this bundle's
    agent YAML, unrelated to spec-131) — the lightest real setup that
    lets `build_session` (and therefore `bootstrap_runtime`) run for
    real without the cost of a full `kolsch setup --non-interactive`.
    """
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    (tmp_path / "platform.yaml").write_text(
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
                "hooks": [{"event": "session.opened", "command": "true"}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")

    bundle = tmp_path / "bundles" / "acme"
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "acme", "version": "0.1", "description": "demo"}),
        encoding="utf-8",
    )
    (bundle / "agents" / "demo.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": "ag_demo",
                "name": "demo",
                "description": "Demo agent.",
                "model": {"provider": "mock", "model": "mock-model"},
                "prompt": "You are a demo agent.",
                "io": {"mode": "conversational"},
            }
        ),
        encoding="utf-8",
    )


async def test_chat_surface_receives_the_bootstrapped_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`kolsch chat`'s real construction path, driven end to end through
    `_cmd_chat`: `session.py:build_session` calls `bootstrap_runtime`
    and `main.py:1068-1070` binds `ChatApp(engine=session.runtime.engine)`.
    Everything is real except `ChatApp` itself (spied, to avoid launching
    a real Textual app in a test) and the post-run process-exit hooks
    (neutered, matching `test_chat_resume_picker.py`'s established
    pattern) — `build_session`/`bootstrap_runtime` run unmodified."""
    import argparse
    import importlib

    from cli.chat import app as chat_app_module

    main_module = importlib.import_module("cli.main")
    _cmd_chat = main_module._cmd_chat

    _write_chat_workspace(tmp_path)

    captured: dict[str, Any] = {}

    class _SpyChatApp:
        def __init__(
            self, *, session: Any, engine: Any, full_history: bool, **kwargs: Any
        ) -> None:
            captured["engine"] = engine
            self.return_code = 0

        async def run_async(self, **_: Any) -> None:
            return None

    monkeypatch.setattr(chat_app_module, "ChatApp", _SpyChatApp)
    monkeypatch.setattr(chat_app_module, "_EXIT_HOOK", lambda _code: None)
    # `cli.main` must be patched via its module object, not the string
    # form — the string form mis-resolves to the re-exported `main`
    # function (same trap `test_chat_resume_picker.py` documents).
    monkeypatch.setattr(main_module, "_install_chat_hangup_guard", lambda: None)
    monkeypatch.setattr(chat_app_module, "register_chat_slash_commands", lambda: None)

    args = argparse.Namespace(
        root=tmp_path,
        agent_ref="acme/agents/demo",
        session=None,
        model=None,
        full_history=False,
        resume=False,
    )

    rc = await _cmd_chat(args)

    assert rc == 0
    engine = captured["engine"]
    observer = engine._observer
    assert isinstance(observer, HookDispatcher)
    assert len(observer._rules) == 1


async def test_run_surface_uses_runtime_engine_directly(tmp_path: Path) -> None:
    """`kolsch run`'s construction path: `main.py`'s run command calls
    `bootstrap_runtime` then reads `runtime.engine.execute_stream`/
    `.execute` directly — no second `AgentEngine(...)` construction.
    Confirmed here by bootstrapping exactly as `main.py` does and
    checking the observer on the same handle `main.py` would call."""
    _write_platform_yaml(
        tmp_path,
        {"hooks": [{"event": "session.opened", "command": "true"}]},
    )

    runtime = await bootstrap_runtime(root=tmp_path)

    # This IS the object `main.py:707,716` calls
    # `.execute_stream`/`.execute` on — no separate construction.
    observer = runtime.engine._observer
    assert isinstance(observer, HookDispatcher)
    assert len(observer._rules) == 1


_HELLO_WORKFLOW_YAML = """
nodes:
  greet:
    type: kolsch/nodes/bash
    command: "echo Hello"
graph:
  edges: []
"""


async def test_sub_agent_author_workflow_surface_uses_runtime_engine(
    tmp_path: Path,
) -> None:
    """`AgentRunner` (the sub-agent/`author_workflow` consumer) is built
    by `src/cli/_workflow_runtime.py:build_workflow_executor`, which
    passes `engine=runtime.engine` straight into `register_builtins` ->
    `AgentRunner(engine=engine, ...)` (`src/workflows/builtins/__init__.py`).
    This test drives that exact factory (the same one
    `tests/cli/_workflow_runtime/test_build_workflow_executor_kwargs.py`
    exercises) and asserts the `AgentRunner` registered under
    `"kolsch/nodes/agent"` in the executor's registry holds the
    identical engine object `runtime.engine` is — the sharpest of the
    four surfaces per the spec's Background (this was the one the
    original draft missed)."""
    _write_platform_yaml(
        tmp_path,
        {"hooks": [{"event": "session.opened", "command": "true"}]},
    )

    runtime = await bootstrap_runtime(root=tmp_path)

    workflow_path = tmp_path / "hello.yaml"
    workflow_path.write_text(_HELLO_WORKFLOW_YAML, encoding="utf-8")
    workflow = load_workflow(workflow_path)
    run_store = WorkflowRunStore(tmp_path)
    run = run_store.create(workflow_ref="test/hello", inputs={})

    executor = build_workflow_executor(
        runtime=runtime,
        root=tmp_path,
        workflow=workflow,
        run=run,
        store=run_store,
    )

    agent_runner = executor._registry.get("kolsch/nodes/agent", node_name="test-node")
    assert agent_runner._engine is runtime.engine
    observer = agent_runner._engine._observer
    assert isinstance(observer, HookDispatcher)
    assert len(observer._rules) == 1


def _write_api_workspace(tmp_path: Path, marker: Path) -> None:
    """Same shape as `tests/component/test_api.py`'s `_make_platform_root`,
    plus a `thread.exchange_completed` hook writing `marker`.

    `thread.exchange_completed` (not `session.opened` — see the module
    docstring's "Finding" section) is the event `execute_stream`
    reliably yields as the terminal event of a successful exchange.

    Uses `defaults: {}` (matching `test_api.py`'s fixture exactly)
    rather than `default_platform_payload()`'s `defaults.memory`
    block — the latter trips a Layer 1 `extra_forbidden` validation
    error against this bundle's agent YAML, unrelated to spec-131.
    """
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    (tmp_path / "platform.yaml").write_text(
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
                "hooks": [
                    {
                        "event": "thread.exchange_completed",
                        "command": f"touch {marker}",
                        "timeout_seconds": 5,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")

    bundle = tmp_path / "bundles" / "acme"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "acme", "version": "0.1", "description": "demo"}),
        encoding="utf-8",
    )
    (bundle / "agents").mkdir(exist_ok=True)
    (bundle / "agents" / "demo.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": "ag_demo",
                "name": "demo",
                "description": "Demo agent.",
                "model": {"provider": "mock", "model": "mock-model"},
                "prompt": "You are a demo agent.",
                "io": {"mode": "conversational"},
            }
        ),
        encoding="utf-8",
    )


async def test_http_api_surface_fires_configured_hook_end_to_end(
    tmp_path: Path,
) -> None:
    """The strongest form of the HTTP-API-surface check: a real HTTP
    round-trip through `POST .../execute_stream`
    (`src/api/routes.py:245`'s `execute_agent_stream` route, the same
    route `tests/component/test_api.py`'s `TC-API-04` exercises), with
    a `thread.exchange_completed` hook configured in `platform.yaml`.
    If the API route's engine were constructed separately from
    `bootstrap_runtime` (or `observer=None`), the hook would never
    fire — proving this passes confirms `execute_agent_stream` reaches
    the exact same `HookDispatcher`-backed engine every other surface
    does, via a live request/response cycle rather than
    object-identity inspection.

    Uses `httpx.AsyncClient` + `ASGITransport` (not the sync
    `fastapi.testclient.TestClient` `test_api.py` uses elsewhere) so
    the whole request runs on THIS test's event loop: `TestClient`
    drives the ASGI app through an internal `anyio` portal on a
    separate thread/loop, which tears down as soon as the `with
    client.stream(...)` block exits — before `HookDispatcher.handle`'s
    fire-and-forget `asyncio.create_task` (Step 4, deliberately never
    awaited) gets a chance to run to completion. Staying on one loop
    end to end avoids that race entirely.
    """
    import httpx

    from api.app import build_app

    marker = tmp_path / f"kolsch-api-hook-{uuid.uuid4().hex}"
    assert not marker.exists()
    _write_api_workspace(tmp_path, marker)

    runtime = await bootstrap_runtime(root=tmp_path)
    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        session = (
            await client.post(
                "/sessions",
                json={"agent_id": "ag_demo", "name": "hook-test"},
            )
        ).json()

        async with client.stream(
            "POST",
            "/agents/acme/agents/demo/execute_stream",
            json={
                "session_id": session["session_id"],
                "trigger_kind": "user_message",
                "content": "hello",
            },
        ) as resp:
            assert resp.status_code == 200
            # Drain the stream so execute_stream actually runs to
            # completion inside this request — the generator body
            # only executes as it's iterated.
            async for _chunk in resp.aiter_bytes():
                pass

    appeared = False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if marker.exists():
            appeared = True
            break
        await asyncio.sleep(0.05)
    assert appeared, (
        "expected the thread.exchange_completed hook to fire via a "
        "real execute_agent_stream HTTP round-trip"
    )

    marker.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# True end-to-end: a real hook fires off a real execute_stream event,
# from the bootstrap_runtime-constructed engine (not a hand-built one).
# ---------------------------------------------------------------------------


async def test_configured_hook_fires_end_to_end_from_bootstrapped_engine(
    tmp_path: Path,
) -> None:
    """The headline proof: a `hooks:` rule configured in `platform.yaml`,
    loaded by `bootstrap_runtime`, actually fires a real subprocess when
    `runtime.engine.execute_stream` yields the matching event — driven
    entirely through the real construction path, not a hand-wired
    `AgentEngine(observer=...)` like Steps 4/5's tests use.

    Targets `thread.exchange_completed` (not `session.opened` — see the
    module docstring's "Finding" section) since that's the event
    `execute_stream` reliably yields at the end of a successful
    exchange.
    """
    marker = tmp_path / f"kolsch-bootstrap-hook-{uuid.uuid4().hex}"
    assert not marker.exists()

    _write_platform_yaml(
        tmp_path,
        {
            "hooks": [
                {
                    "event": "thread.exchange_completed",
                    "command": f"touch {marker}",
                    "timeout_seconds": 5,
                },
            ],
        },
    )

    runtime = await bootstrap_runtime(
        root=tmp_path,
        llm=_llm_callable(MockLLM(script=[TextResponse(text="Hi back.")])),
    )
    ctx = await _new_session_context(runtime)

    received = [event async for event in runtime.engine.execute_stream(_config(), ctx)]
    assert received[-1].type == EventType.THREAD_EXCHANGE_COMPLETED

    appeared = False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if marker.exists():
            appeared = True
            break
        await asyncio.sleep(0.05)
    assert appeared, (
        "expected the thread.exchange_completed hook to touch the marker file"
    )

    marker.unlink(missing_ok=True)


async def test_zero_hooks_configured_no_behavior_change(tmp_path: Path) -> None:
    """Acceptance Criterion #8 at the bootstrap level: with no `hooks:`
    section, a real exchange through the bootstrapped engine behaves
    identically to the pre-spec-131 baseline (same terminal event),
    even though the observer is a real (empty-rules) `HookDispatcher`
    rather than `None`."""
    _write_platform_yaml(tmp_path, {})

    runtime = await bootstrap_runtime(
        root=tmp_path,
        llm=_llm_callable(MockLLM(script=[TextResponse(text="Hi back.")])),
    )
    ctx = await _new_session_context(runtime)

    received = [event async for event in runtime.engine.execute_stream(_config(), ctx)]

    assert received
    assert received[-1].type == EventType.THREAD_EXCHANGE_COMPLETED


# ---------------------------------------------------------------------------
# Short-lived-process fix: `kolsch run`'s `asyncio.run()` teardown race.
#
# Real, empirically-reproduced finding (not present in the spec):
# `kolsch run` is a short-lived process — its `asyncio.run(...)` call
# returns and starts tearing down the event loop the instant the
# command handler returns. If a configured hook's fire-and-forget
# subprocess task (`HookDispatcher.handle`, Step 4 — deliberately
# never awaited) is still suspended inside `proc.communicate()` at
# that point, cancelling it during shutdown does not reliably deliver
# on macOS + CPython 3.13 (confirmed via a minimal, kolsch-independent
# repro script — completely unrelated to this codebase's own code —
# that hangs 100% of the time under the same conditions). Without a
# fix, `kolsch run` with ANY hook configured on an event
# `execute_stream` actually yields (e.g. `thread.exchange_completed`)
# would wedge the whole process indefinitely instead of exiting with
# its result. `kolsch chat`/the HTTP API are long-lived and never hit
# this. The fix — `_run_handler_and_drain_hooks` wrapping every CLI
# subcommand's dispatch in `main()` — is exercised end to end here via
# the REAL `cli.main.main` entry point (not a hand-built asyncio.run
# call), matching `tests/component/test_setup.py`'s
# `test_tc_ux_10_end_to_end_run_against_setup_workspace` pattern:
# defined as a SYNC test on purpose, since `main()` internally calls
# `asyncio.run` and would fail from inside an existing event loop.
# ---------------------------------------------------------------------------


def test_kolsch_run_with_configured_hook_exits_cleanly_not_hung(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression test for the short-lived-process race documented
    above. Runs a real `kolsch setup --non-interactive` workspace, adds
    a `thread.exchange_completed` hook writing a sentinel file, then
    calls the REAL `cli.main.main(["run", ...])` entry point — the
    exact code path a real `kolsch run` invocation goes through,
    `asyncio.run()` and all. Before the `drain_background_hook_tasks`
    fix, this call would never return (a live, non-pytest reproduction
    hung indefinitely under the identical scenario); pytest itself
    doesn't reproduce the platform-level hang (its own event loop
    management differs from a bare `asyncio.run()` at process exit),
    so this test's primary value is pinning the WIRING (main() calls
    the drain wrapper, the hook fires, the marker appears) rather than
    reproducing the hang itself — the hang was verified live, outside
    pytest, and is documented in this module's + `drain_background_hook_tasks`'s
    docstrings.
    """
    import yaml

    from cli.main import main

    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    monkeypatch.delenv("KOLSCH_WORKSPACE", raising=False)

    rc_setup = main(["--root", str(tmp_path), "setup", "--non-interactive"])
    assert rc_setup == 0
    capsys.readouterr()  # drain setup's stdout

    marker = tmp_path / f"kolsch-cli-drain-e2e-{uuid.uuid4().hex}"
    assert not marker.exists()
    platform_yaml_path = tmp_path / "platform.yaml"
    payload = yaml.safe_load(platform_yaml_path.read_text(encoding="utf-8"))
    payload["hooks"] = [
        {
            "event": "thread.exchange_completed",
            "command": f"touch {marker}",
            "timeout_seconds": 5,
        }
    ]
    platform_yaml_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    rc_run = main(
        [
            "--root",
            str(tmp_path),
            "run",
            "kolsch/agents/helper",
            "--input",
            "content=hello",
        ]
    )

    # The call above returning at all (not hanging pytest itself) is
    # already load-bearing signal; the explicit assertions below pin
    # the observable contract.
    assert rc_run == 0
    appeared = False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if marker.exists():
            appeared = True
            break
        time.sleep(0.05)
    assert appeared, (
        "expected the thread.exchange_completed hook to fire during a "
        "real `kolsch run` invocation via the actual cli.main.main entry point"
    )


def test_kolsch_run_with_slow_hook_exit_time_stays_near_drain_bound_not_hook_duration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression test for the review-caught drain/reap defect (spec-131
    Step 6, second review round).

    Two independent reviewers found that `drain_background_hook_tasks`'s
    documented ~2s bound was not real: if the drain's own timeout
    expired with a hook task still pending, `asyncio.run()`'s shutdown
    cancellation would then hit an UNBOUNDED `proc.wait()`/
    `proc.communicate()` inside the kill/reap path
    (`_kill_with_grace` / `_run_hook_command_inner`'s `finally`), which
    blocks until every process holding the subprocess's stdout/stderr
    pipe exits — including a grandchild the hook's shell command
    spawned that `SIGKILL` to the direct child never touches. Wall-clock
    exit time tracked the HOOK's duration (up to its `timeout_seconds`
    ceiling), not the drain's short window — the exact defect
    `test_kolsch_run_with_configured_hook_exits_cleanly_not_hung`
    (above) did NOT catch, because its hook is an instant `touch` with
    nothing left running afterward.

    This test uses a hook whose command backgrounds a grandchild that
    outlives BOTH the drain's default 2s window and the reap's 1.5s
    bound (`sleep 6 &`, in a subshell so the direct child exits almost
    immediately) and asserts real wall-clock exit time stays close to
    the drain+reap bound (a few seconds), not anywhere near the
    grandchild's 6s lifetime.
    """
    import time as time_module

    import yaml

    from cli.main import main

    monkeypatch.delenv("KOLSCH_PROVIDER", raising=False)
    monkeypatch.delenv("KOLSCH_PROVIDER_KEY", raising=False)
    monkeypatch.delenv("KOLSCH_WORKSPACE", raising=False)

    rc_setup = main(["--root", str(tmp_path), "setup", "--non-interactive"])
    assert rc_setup == 0
    capsys.readouterr()  # drain setup's stdout

    platform_yaml_path = tmp_path / "platform.yaml"
    payload = yaml.safe_load(platform_yaml_path.read_text(encoding="utf-8"))
    # `(sleep 6 &)`: a subshell that backgrounds `sleep 6` and exits
    # immediately itself — the DIRECT child (the subshell) is gone in
    # milliseconds, but the grandchild `sleep 6` inherits and holds
    # open the stdout/stderr pipes for a further ~6s. This is exactly
    # the shape that defeated the pre-fix drain/reap bound.
    payload["hooks"] = [
        {
            "event": "thread.exchange_completed",
            "command": "(sleep 6 &)",
            "timeout_seconds": 30,
        }
    ]
    platform_yaml_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    started = time_module.monotonic()
    rc_run = main(
        [
            "--root",
            str(tmp_path),
            "run",
            "kolsch/agents/helper",
            "--input",
            "content=hello",
        ]
    )
    elapsed = time_module.monotonic() - started

    assert rc_run == 0
    # Bound is drain_timeout (2s default) + post-kill reap timeout
    # (1.5s) + normal CLI/bootstrap overhead. 5.5s is a generous
    # ceiling that comfortably distinguishes "bounded exit" from
    # "tracked the grandchild's 6s lifetime" (which this test would
    # measure as >= 6s) while tolerating this machine's own overhead —
    # matching `test_runner_timeout_with_grandchild_holding_pipe_still_returns_promptly`'s
    # analogous bound in `tests/platform_runtime/test_hooks.py`.
    assert elapsed < 5.5, (
        f"kolsch run took {elapsed:.2f}s with a hook backgrounding a "
        "6s grandchild — expected well under 5.5s (bounded by the "
        "drain + reap timeouts), not tracking the grandchild's 6s "
        "lifetime (the pre-fix defect both reviewers caught)"
    )
