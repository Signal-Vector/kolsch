"""Phase 3.2 Step 4 — end-to-end workflow→agent integration tests.

Phase Tests 1-4 from `process/phase3.2-agent-integration-build.md`:

- **Phase Test 1** — `setup --non-interactive` a fresh workspace, then
  `kolsch workflows run kolsch/workflows/hello-agent`. Assert the
  workflow JSONL records the bash + agent node started/completed pair
  AND a spawned-session JSONL exists with `spawned_by` pointing back
  to the workflow's `workflow.node.started` event for `classify`.
- **Phase Test 2** — workflow-side `outputs:` override travels intact
  through the parser + runner without disturbing the node's captured
  output (the engine doesn't yet enforce typed structured outputs;
  the override is the documentary surface).
- **Phase Test 3** — agent failure (the scripted LLM raises) remaps to
  `WorkflowNodeFailed`; the workflow JSONL ends with
  `workflow.node.failed` + `workflow.run.failed`; the agent session
  JSONL ends with `thread.exchange_failed`.
- **Phase Test 4** — every event in BOTH JSONLs carries the same
  `trace_id`. A regression here would mean the spawned session is
  uncorrelatable with the workflow run.
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING, Any

import yaml

from assets import AssetManagement
from bus import InProcessEventBus
from cli.main import main
from engine import (
    AgentEngine,
    LLMRequest,
    LLMResponse,
    MinimalFramework,
)
from files import PathPolicy
from providers import (
    NativeSandboxProvider,
    RecentBufferMemoryProvider,
    RuleBasedAuthorizationProvider,
)
from providers.secrets import DotenvSecretsProvider
from skills import SkillsSubsystem
from store import SessionStore, SqliteBackend
from tools import ToolsSubsystem
from tools.builtins import make_bash
from tools.decorator import get_tool_definition
from workflows.builtins import register_builtins
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    import pytest


# ---------------------------------------------------------------------------
# Shared YAMLs + helpers
# ---------------------------------------------------------------------------


_PERMISSIVE_PLATFORM_YAML = textwrap.dedent(
    """\
    secrets: {variant: dotenv, config: {path: .env}}
    sandbox: {variant: native, config: {}}
    authorization:
      variant: rule_based
      config:
        rules:
          - {what: "*", pattern: "*", effect: "allow"}
    storage: {entity_backend: sqlite, entity_path: .platform/entities.db}
    event_bus: {backend: in_process}
    file_access:
      allowed_paths: ["data/", "bundles/"]
      denied_paths: [".env"]
    defaults:
      model: {provider: mock, model: mock-model}
    """
)

_HELLO_AGENT_YAML = textwrap.dedent(
    """\
    schema_version: "0.1"
    assembly_version: "1"
    agent_id: ag_greeter
    name: greeter
    description: Step 4 starter.
    io:
      mode: single_shot
      inputs:
        context: {type: str}
      outputs:
        text: {type: str}
    prompt: |
      You greet whatever string lands in `context`.
    """
)

_HELLO_AGENT_WORKFLOW_YAML = textwrap.dedent(
    """\
    description: Workflow → agent demo.
    nodes:
      ready:
        type: kolsch/nodes/noop
      prep:
        type: kolsch/nodes/bash
        command: "echo '{{ inputs.greeting }}, world'"
      classify:
        type: kolsch/nodes/agent
        agent: kolsch/agents/greeter
    graph:
      edges:
        - from: ready
          to: prep
          binds:
            greeting: "{{ inputs.greeting | default('hi') }}"
        - from: prep
          to: classify
          binds:
            context: "{{ nodes.prep.stdout }}"
    """
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return rows


def _scaffold_workspace(
    root: Path,
    *,
    workflow_yaml: str = _HELLO_AGENT_WORKFLOW_YAML,
    agent_yaml: str = _HELLO_AGENT_YAML,
) -> None:
    """Lay down a tmp workspace with the hello-agent demo bundle.

    Inline so the test doesn't carry the full starter bundle (other
    tools / skills / authz prompts) — we want an isolated
    workflow→agent exercise.
    """
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "platform.yaml").write_text(_PERMISSIVE_PLATFORM_YAML, encoding="utf-8")
    (root / ".env").write_text("", encoding="utf-8")
    bundle = root / "bundles" / "kolsch"
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "workflows").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        'name: kolsch\nversion: "0.1"\ndescription: step4-e2e\n',
        encoding="utf-8",
    )
    (bundle / "agents" / "greeter.yaml").write_text(agent_yaml, encoding="utf-8")
    (bundle / "workflows" / "hello-agent.yaml").write_text(
        workflow_yaml, encoding="utf-8"
    )


async def _build_engine_with_llm(
    root: Path,
    *,
    llm: Callable[[LLMRequest], Awaitable[LLMResponse]],
) -> tuple[AgentEngine, SessionStore, AssetManagement]:
    """Stand up a real engine + store with a caller-supplied LLM.

    Mirrors `bootstrap_runtime` but bypasses the provider-config
    machinery so the test injects a fully scripted LLM. The agent
    runner consumes the same engine surface chat does.
    """
    backend = SqliteBackend(root / ".platform" / "entities.db")
    await backend.initialize()
    store = SessionStore(root=root, backend=backend)

    am = AssetManagement(root=root)
    await am.initialize()

    authz = RuleBasedAuthorizationProvider(
        config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]},
        catalog=am.catalog(),
    )
    secrets = DotenvSecretsProvider(root=root, config={"path": ".env"})
    sandbox = NativeSandboxProvider(config={})
    memory = RecentBufferMemoryProvider(store=store)
    path_policy = PathPolicy(allowed_paths=["data/", "bundles/"], denied_paths=[".env"])
    tools_sub = ToolsSubsystem(
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        path_policy=path_policy,
        root=root,
    )
    skills_sub = SkillsSubsystem(asset_management=am)
    bus = InProcessEventBus()
    framework = MinimalFramework()

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
        llm=llm,
    )
    return engine, store, am


def _spawned_session_jsonl(workspace: Path) -> Path:
    sessions_dir = workspace / "sessions"
    jsonls = sorted(sessions_dir.glob("ses_*.jsonl"))
    assert len(jsonls) == 1, (
        f"expected exactly one spawned session JSONL; got {[p.name for p in jsonls]}"
    )
    return jsonls[0]


class _ScriptedLLM:
    """A tiny scripted LLM — returns successive text responses, or
    raises a caller-supplied exception on every call when ``always_raise``
    is set (used to exhaust the engine's retry budget so a Fatal
    failure propagates).
    """

    def __init__(
        self,
        *,
        texts: list[str] | None = None,
        always_raise: BaseException | None = None,
    ) -> None:
        self._texts = list(texts or [])
        self._always_raise = always_raise
        self.calls: list[LLMRequest] = []

    async def __call__(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self._always_raise is not None:
            raise self._always_raise
        if not self._texts:
            return LLMResponse(content="ok")
        return LLMResponse(content=self._texts.pop(0))


async def _drive_workflow(
    tmp_path: Path,
    *,
    workflow_yaml: str = _HELLO_AGENT_WORKFLOW_YAML,
    agent_yaml: str = _HELLO_AGENT_YAML,
    llm: Callable[[LLMRequest], Awaitable[LLMResponse]] | None = None,
    inputs: dict[str, Any] | None = None,
) -> tuple[str, Path, Path, SessionStore]:
    """Scaffold a workspace, run the workflow once, return:
    (status_value, workflow_events_path, spawned_session_jsonl, store).
    """
    _scaffold_workspace(tmp_path, workflow_yaml=workflow_yaml, agent_yaml=agent_yaml)
    llm_callable = llm or _ScriptedLLM(texts=["Hello, friend!"])

    engine, store, am = await _build_engine_with_llm(tmp_path, llm=llm_callable)
    catalog = am.catalog()

    workflow_text = (
        tmp_path / "bundles" / "kolsch" / "workflows" / "hello-agent.yaml"
    ).read_text(encoding="utf-8")
    workflow = Workflow.model_validate(yaml.safe_load(workflow_text))

    run_store = WorkflowRunStore(tmp_path)
    run = run_store.create(
        workflow_ref="kolsch/workflows/hello-agent",
        inputs=inputs or {"greeting": "hi"},
    )

    bash_fn = make_bash(
        policy=PathPolicy(allowed_paths=["data/", "bundles/"], denied_paths=[".env"]),
        root=tmp_path,
    )
    bash_defn = get_tool_definition(bash_fn)

    registry = RunnerRegistry()
    register_builtins(
        registry,
        bash_defn=bash_defn,
        authz=RuleBasedAuthorizationProvider(
            config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]},
            catalog=catalog,
        ),
        secrets=DotenvSecretsProvider(root=tmp_path, config={"path": ".env"}),
        sandbox=NativeSandboxProvider(config={}),
        catalog=catalog,
        engine=engine,
        session_store=store,
        config_builder=am.build_resolved_config,
    )

    executor = IterativeExecutor(
        workflow=workflow,
        run=run,
        store=run_store,
        registry=registry,
        catalog=catalog,
    )
    status = await executor.run()

    events_path = run_store.directory(run.run_id).events_path
    session_jsonl = _spawned_session_jsonl(tmp_path)
    return status.value, events_path, session_jsonl, store


# ---------------------------------------------------------------------------
# Phase Test 1 — end-to-end via the live CLI
# ---------------------------------------------------------------------------


def test_phase1_setup_then_run_workflow_succeeds(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 4.6 — manual smoke replicated via in-process CLI call.

    Setup a fresh workspace via `kolsch setup --non-interactive`, then
    invoke `kolsch workflows run kolsch/workflows/hello-agent`. The
    starter `platform.yaml` maps `kolsch/bash` to `prompt` (which
    collapses to `deny` on the headless CLI), so we patch the
    workspace's `platform.yaml` to allow-all before invocation.
    """
    workspace = tmp_path / "ws"
    rc = main(["--root", str(workspace), "setup", "--non-interactive"])
    assert rc == 0
    capsys.readouterr()

    (workspace / "platform.yaml").write_text(
        _PERMISSIVE_PLATFORM_YAML, encoding="utf-8"
    )

    rc = main(
        [
            "--root",
            str(workspace),
            "workflows",
            "run",
            "kolsch/workflows/hello-agent",
            "--input",
            "greeting=hi",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, f"workflow run failed: stdout={captured.out} err={captured.err}"

    runs_dir = workspace / "runs"
    runs = list(runs_dir.iterdir())
    assert len(runs) == 1, f"expected one run; got {[r.name for r in runs]}"
    events = _read_jsonl(runs[0] / "events.jsonl")
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "workflow.run.started"
    assert kinds[-1] == "workflow.run.completed"

    per_node = [
        (e["payload"].get("node_name"), e["kind"])
        for e in events
        if e["kind"].startswith("workflow.node.")
    ]
    assert per_node == [
        ("ready", "workflow.node.started"),
        ("ready", "workflow.node.completed"),
        ("prep", "workflow.node.started"),
        ("prep", "workflow.node.completed"),
        ("classify", "workflow.node.started"),
        ("classify", "workflow.node.completed"),
    ]

    # Agent node's completed payload carries spawned_session_id.
    classify_completed = next(
        e
        for e in events
        if e["kind"] == "workflow.node.completed"
        and e["payload"].get("node_name") == "classify"
    )
    spawned_session_id = classify_completed["payload"].get("spawned_session_id")
    assert spawned_session_id, (
        f"agent node completed payload missing spawned_session_id: "
        f"{classify_completed['payload']!r}"
    )

    # Spawned session JSONL exists and starts with session.opened
    # carrying spawned_by that points back to the workflow node.started.
    session_jsonl = workspace / "sessions" / f"{spawned_session_id}.jsonl"
    assert session_jsonl.exists(), f"missing spawned session file: {session_jsonl}"
    session_events = _read_jsonl(session_jsonl)
    opener = session_events[0]
    assert opener["type"] == "session.opened"
    spawned_by = opener["data"].get("spawned_by")
    assert spawned_by is not None, f"session.opened missing spawned_by: {opener!r}"
    classify_started = next(
        e
        for e in events
        if e["kind"] == "workflow.node.started"
        and e["payload"].get("node_name") == "classify"
    )
    assert spawned_by == {
        "from_workflow_run_id": events[0]["workflow_run_id"],
        "from_invocation_id": classify_started["invocation_id"],
        "from_workflow_event_id": classify_started["event_id"],
    }


# ---------------------------------------------------------------------------
# Phase Test 2 — outputs override
# ---------------------------------------------------------------------------


async def test_phase2_outputs_override_travels_through_runner(
    tmp_path: Path,
) -> None:
    """The workflow-side `outputs:` override on the agent node is
    parsed correctly + carried into the runner's `ResolvedAgentConfig`
    rebuild without disturbing the captured output.

    The engine's `agent.message.data["content"]` is `{"text": ...}`
    regardless of the IOSpec — typed-output enforcement is engine
    work that happens beyond Phase 3.2's scope. The test asserts the
    override is parseable + runnable, leaving the actual structured-
    output validation to a future engine phase. The `outputs:` shape
    is part of the runner's documentary contract today.
    """
    override_workflow_yaml = textwrap.dedent(
        """\
        description: outputs override test.
        nodes:
          ready:
            type: kolsch/nodes/noop
          prep:
            type: kolsch/nodes/bash
            command: "echo '{{ inputs.greeting }}'"
          classify:
            type: kolsch/nodes/agent
            agent: kolsch/agents/greeter
            outputs:
              mode: single_shot
              inputs: {}
              outputs:
                label: {type: str}
        graph:
          edges:
            - from: ready
              to: prep
              binds:
                greeting: "{{ inputs.greeting | default('hi') }}"
            - from: prep
              to: classify
              binds:
                context: "{{ nodes.prep.stdout }}"
        """
    )
    status, events_path, _session_jsonl, _store = await _drive_workflow(
        tmp_path,
        workflow_yaml=override_workflow_yaml,
        llm=_ScriptedLLM(texts=["override-ok"]),
    )

    assert status == "succeeded"

    events = _read_jsonl(events_path)
    completed = next(
        e
        for e in events
        if e["kind"] == "workflow.node.completed"
        and e["payload"].get("node_name") == "classify"
    )
    assert "output" in completed["payload"]
    assert "spawned_session_id" in completed["payload"]


# ---------------------------------------------------------------------------
# Phase Test 3 — agent failure → WorkflowNodeFailed
# ---------------------------------------------------------------------------


async def test_phase3_agent_failure_maps_to_workflow_node_failed(
    tmp_path: Path,
) -> None:
    """Configure the MockLLM to raise an upstream error; the engine
    surfaces this as `AgentExecutionError`; the runner remaps to
    `WorkflowNodeFailed`; the executor's failure path emits
    `workflow.node.failed` then `workflow.run.failed`. The spawned
    agent JSONL ends with `thread.exchange_failed`.
    """
    from errors import ModelUpstreamError

    llm = _ScriptedLLM(
        always_raise=ModelUpstreamError(
            provider="mock", http_status=500, response="forced-failure"
        )
    )
    status, events_path, session_jsonl, _store = await _drive_workflow(
        tmp_path, llm=llm
    )
    assert status == "failed"

    workflow_events = _read_jsonl(events_path)
    workflow_kinds = [e["kind"] for e in workflow_events]
    assert "workflow.node.failed" in workflow_kinds
    assert workflow_kinds[-1] == "workflow.run.failed"
    failed_event = next(
        e for e in workflow_events if e["kind"] == "workflow.node.failed"
    )
    assert failed_event["payload"]["error_class"] == "WorkflowNodeFailed"

    session_events = _read_jsonl(session_jsonl)
    session_types = [e["type"] for e in session_events]
    assert session_types[-1] == "thread.exchange_failed"


# ---------------------------------------------------------------------------
# Phase Test 4 — trace_id propagation across both JSONLs
# ---------------------------------------------------------------------------


async def test_phase4_trace_id_propagates_to_spawned_session(
    tmp_path: Path,
) -> None:
    """Every event in BOTH JSONLs carries the same `trace_id`. The
    workflow run mints the trace_id; the runner passes it into both
    `create_session(trace_id=...)` (for the `session.opened`
    envelope) AND the `ExecutionContext` (which the engine threads
    onto every emitted event via its `_emit` chokepoint).
    """
    status, events_path, session_jsonl, _store = await _drive_workflow(tmp_path)
    assert status == "succeeded"

    workflow_events = _read_jsonl(events_path)
    session_events = _read_jsonl(session_jsonl)

    workflow_trace_ids = {e.get("trace_id") for e in workflow_events}
    assert len(workflow_trace_ids) == 1, (
        f"workflow JSONL carries multiple trace_ids: {workflow_trace_ids}"
    )
    workflow_trace_id = workflow_trace_ids.pop()
    assert workflow_trace_id is not None

    session_trace_ids = {e.get("trace_id") for e in session_events}
    assert len(session_trace_ids) == 1, (
        f"session JSONL carries multiple trace_ids: {session_trace_ids}"
    )
    session_trace_id = session_trace_ids.pop()
    assert session_trace_id == workflow_trace_id, (
        f"trace_id mismatch — workflow={workflow_trace_id} session={session_trace_id}"
    )
