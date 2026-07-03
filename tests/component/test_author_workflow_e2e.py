"""Phase 3.11 Step 2 — scripted integration tests for the four §17.10 scenarios.

The harness mocks the LLM by replaying a list of scripted responses
(``ToolCallResponse`` or ``TextResponse``); every tool dispatch runs
UNMOCKED against the real engine surface — :func:`validate_workflow`
reaches the real :func:`workflows.validator.validate_workflow`,
:func:`write_file` writes to disk, :func:`introspect_node` imports
the freshly-written ``@node`` module, etc.

Per AC 2.3, each scenario also runs the PRODUCED workflow via the
real :class:`workflows.executor.IterativeExecutor` and asserts on
the terminal output — a green ``validate_workflow`` call alone
wouldn't catch a workflow that validates but doesn't run.

Pattern mirrored from
``tests/component/test_workflow_agent_runner_e2e.py`` (real
SessionStore + AssetManagement + ToolsSubsystem; mocked LLM via the
engine's ``llm`` callable seam) and
``tests/component/test_engine.py`` (the FakeLLM → LLMResponse
adapter).
"""

from __future__ import annotations

import shutil
from pathlib import Path
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
    MinimalFramework,
)
from files import PathPolicy
from providers import (
    NativeSandboxProvider,
    RecentBufferMemoryProvider,
    RuleBasedAuthorizationProvider,
    validate_recent_buffer_config,
)
from providers.secrets import DotenvSecretsProvider
from schemas import EventType
from skills import SkillsSubsystem, make_standard_validator
from store import SessionStore, SqliteBackend
from tests.fixtures.fake_llm import (
    FakeLLM,
    FakeLLMResponse,
    TextResponse,
    ToolCallResponse,
)
from tools import ToolsSubsystem, validate_standard_config
from tools.builtins import make_bash, make_list_files, make_read_file, make_write_file
from tools.decorator import get_tool_definition
from workflows.builtins import register_builtins
from workflows.executor import IterativeExecutor, RunnerRegistry
from workflows.parser import Workflow
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from schemas import Event


_REPO_ROOT = Path(__file__).resolve().parents[2]
_STARTER_BUNDLE = _REPO_ROOT / "src" / "cli" / "_starter_bundle" / "kolsch"

_TARGET_BUNDLE = "my-bundle"
"""Name of the user bundle the meta-agent writes into. The Step 3
config knob lets operators override this; Step 2 hard-codes
``my-bundle`` so the harness doesn't depend on platform.yaml lookup
that hasn't shipped yet."""


_PERMISSIVE_PLATFORM_YAML = """\
secrets: { variant: dotenv, config: { path: .env } }
sandbox: { variant: native, config: {} }
authorization:
  variant: rule_based
  config:
    rules:
      - { who: ".*", what: ".*", effect: allow }
storage: { entity_backend: sqlite, entity_path: .platform/entities.db }
event_bus: { backend: in_process }
file_access:
  allowed_paths: ["bundles/", "data/", "out/"]
  denied_paths: [".env"]
defaults:
  model: { provider: fake, model: fake-model }
"""


# ---------------------------------------------------------------------------
# Workspace + harness
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
    """Lay down a workspace with the kolsch starter bundle + a user bundle.

    The user bundle (``my-bundle``) is where the meta-agent writes
    files. The kolsch bundle ships the meta-agent, the skill, and the
    six tools. A permissive ``platform.yaml`` allows all tool
    invocations so the scripted test doesn't trip authz prompts.
    """
    for sub in ("bundles", "sessions", "content", "logs", "data", "out", ".platform"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "platform.yaml").write_text(
        _PERMISSIVE_PLATFORM_YAML, encoding="utf-8"
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")

    shutil.copytree(_STARTER_BUNDLE, tmp_path / "bundles" / "kolsch")

    user_bundle = tmp_path / "bundles" / _TARGET_BUNDLE
    (user_bundle / "workflows").mkdir(parents=True)
    (user_bundle / "nodes").mkdir(parents=True)
    (user_bundle / "bundle.yaml").write_text(
        f'name: {_TARGET_BUNDLE}\nversion: "0.1"\ndescription: e2e author test bundle\n',
        encoding="utf-8",
    )
    return tmp_path


def _scripted_llm_callable(
    fake: FakeLLM,
) -> Callable[[LLMRequest], Awaitable[LLMResponse]]:
    """Adapt a :class:`FakeLLM` script into the engine's ``LLMRequest``
    → ``LLMResponse`` callable seam.

    Mirrors the adapter in ``tests/component/test_engine.py`` —
    translates ``ToolCallResponse`` into ``LLMResponse(tool_calls=...)``
    and ``TextResponse`` into ``LLMResponse(content=...)``."""

    async def call(request: LLMRequest) -> LLMResponse:
        context: dict[str, object] = {
            "messages": [m.model_dump() for m in request.messages],
            "tools": list(request.tools),
            "model": request.model,
        }
        response = await fake.agenerate(context)
        if isinstance(response, TextResponse):
            return LLMResponse(content=response.text)
        if isinstance(response, ToolCallResponse):
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": response.tool_call_id or f"tc_{uuid4()}",
                        "name": response.tool,
                        "arguments": dict(response.args),
                    },
                ],
            )
        msg = f"unsupported scripted response: {response!r}"
        raise RuntimeError(msg)

    return call


async def _build_engine(
    workspace: Path,
    *,
    llm: Callable[[LLMRequest], Awaitable[LLMResponse]],
) -> tuple[AgentEngine, SessionStore, AssetManagement]:
    """Stand up a real engine pointed at the test workspace.

    Mirrors ``_build_engine_with_llm`` in
    ``tests/component/test_workflow_agent_runner_e2e.py`` — real
    SessionStore + AssetManagement + ToolsSubsystem + SkillsSubsystem;
    mocked LLM only.
    """
    backend = SqliteBackend(workspace / ".platform" / "entities.db")
    await backend.initialize()
    store = SessionStore(root=workspace, backend=backend)

    am = AssetManagement(root=workspace)
    await am.initialize()

    authz = RuleBasedAuthorizationProvider(
        config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]},
        catalog=am.catalog(),
    )
    secrets = DotenvSecretsProvider(root=workspace, config={"path": ".env"})
    sandbox = NativeSandboxProvider(config={})
    memory = RecentBufferMemoryProvider(store=store)
    path_policy = PathPolicy(
        allowed_paths=["bundles/", "data/", "out/"],
        denied_paths=[".env"],
    )
    tools_sub = ToolsSubsystem(
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        path_policy=path_policy,
        root=workspace,
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


async def _run_author_workflow_session(
    workspace: Path,
    *,
    brief: str,
    scripted_responses: list[FakeLLMResponse],
) -> tuple[FakeLLM, list[Event], AssetManagement]:
    """Drive the ``author_workflow`` meta-agent through one exchange.

    Returns ``(fake_llm, all_events, asset_management)`` so the caller
    can inspect: which tools were invoked + with what args (via
    ``fake.calls`` and the ``TOOL_CALLED`` / ``TOOL_RESULT`` events);
    which files were written (via the workspace itself); the final
    ``AGENT_MESSAGE`` text (via the events).
    """
    fake = FakeLLM(script=list(scripted_responses))
    llm = _scripted_llm_callable(fake)
    engine, store, am = await _build_engine(workspace, llm=llm)

    # Mirror bootstrap_runtime's dispatch shape so the tools subsystem
    # validates the meta-agent's `enabled:` tool refs against the
    # catalog (and the skill ref similarly). Without this dict, the
    # tools never register on the agent and every scripted tool-call
    # would fail with "tool not declared".
    subsystem_validators = {
        "memory": {"recent_buffer": validate_recent_buffer_config},
        "tools": {"standard": validate_standard_config},
        "skills": {"standard": make_standard_validator(am)},
    }
    config = await am.build_resolved_config(
        "kolsch/agents/author_workflow",
        subsystem_validators=subsystem_validators,
    )

    session = await store.create_session(
        str(config.agent_id), name="author-workflow-test"
    )
    threads = await store.list_threads(session.session_id)
    ctx = ExecutionContext(
        thread_id=threads[0].thread_id,
        trigger={"kind": "user_message", "content": brief},
    )

    events: list[Event] = []
    async for event in engine.execute_stream(config, ctx):
        events.append(event)

    return fake, events, am


# ---------------------------------------------------------------------------
# Workflow-runnability helper (AC 2.3)
# ---------------------------------------------------------------------------


async def _run_workflow(
    workspace: Path,
    *,
    workflow_ref: str,
    inputs: dict[str, Any] | None = None,
    llm: Callable[[LLMRequest], Awaitable[LLMResponse]] | None = None,
) -> tuple[str, Path]:
    """Run a produced workflow via the real :class:`IterativeExecutor`.

    The fresh ``IterativeExecutor`` reuses the workspace's catalog +
    tools but threads a separate ``llm`` callable (defaults to one
    that responds ``"urgent"`` — enough for the classifier-based
    scenarios). Returns ``(terminal_status_value, run_events_path)``.
    """
    llm_callable = llm or _scripted_llm_callable(
        FakeLLM(script=[TextResponse(text="urgent")] * 16)
    )
    engine, store, am = await _build_engine(workspace, llm=llm_callable)
    catalog = am.catalog()

    workflow_entry = catalog.lookup(workflow_ref, expected_type="workflow")
    if workflow_entry is None:
        msg = f"workflow {workflow_ref!r} not in catalog"
        raise AssertionError(msg)
    workflow_text = Path(workflow_entry.path).read_text(encoding="utf-8")
    workflow = Workflow.model_validate(yaml.safe_load(workflow_text))

    run_store = WorkflowRunStore(workspace)
    run = run_store.create(
        workflow_ref=workflow_ref,
        inputs=inputs or {},
    )

    runner_policy = PathPolicy(
        allowed_paths=["bundles/", "data/", "out/"], denied_paths=[".env"]
    )
    bash_defn = get_tool_definition(
        make_bash(policy=runner_policy, root=workspace)
    )
    read_defn = get_tool_definition(
        make_read_file(policy=runner_policy, root=workspace)
    )
    write_defn = get_tool_definition(
        make_write_file(policy=runner_policy, root=workspace)
    )
    list_defn = get_tool_definition(
        make_list_files(policy=runner_policy, root=workspace)
    )

    registry = RunnerRegistry()
    register_builtins(
        registry,
        bash_defn=bash_defn,
        read_file_defn=read_defn,
        write_file_defn=write_defn,
        list_files_defn=list_defn,
        authz=RuleBasedAuthorizationProvider(
            config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]},
            catalog=catalog,
        ),
        secrets=DotenvSecretsProvider(root=workspace, config={"path": ".env"}),
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
    return status.value, events_path


# ---------------------------------------------------------------------------
# Tool-call utilities — convert events into a (name, args) sequence
# ---------------------------------------------------------------------------


def _tool_call_sequence(events: list[Event]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (e.data["tool_name"], dict(e.data.get("arguments", {})))
        for e in events
        if e.type == EventType.TOOL_CALLED
    ]


def _final_agent_message(events: list[Event]) -> str | None:
    for e in reversed(events):
        if e.type == EventType.AGENT_MESSAGE:
            return e.data.get("content", {}).get("text")
    return None


# ===========================================================================
# Phase Test 1 — from-scratch authoring
# ===========================================================================


_PT1_WORKFLOW_YAML = """\
description: Classify a ticket and print URGENT or NORMAL.

nodes:
  classify:
    type: kolsch/nodes/agent
    agent: kolsch/agents/classifier
    description: Classify the inbound ticket text.
  is_urgent:
    type: kolsch/nodes/branch
    description: Branch on the classifier verdict.
    condition: "{{ inputs.label == 'urgent' }}"
  urgent_marker:
    type: kolsch/nodes/bash
    command: "echo URGENT"
  normal_marker:
    type: kolsch/nodes/bash
    command: "echo NORMAL"

graph:
  edges:
    - from: classify
      to: is_urgent
      binds:
        label: "{{ nodes.classify.text }}"
    - from: is_urgent
      to: urgent_marker
      branch: true
    - from: is_urgent
      to: normal_marker
      branch: false
"""


async def test_from_scratch_authoring(tmp_path: Path) -> None:
    """Phase Test 1 — meta-agent drafts a triage workflow from
    scratch, validates it, surfaces the ref. AC 2.3: the produced
    workflow ACTUALLY RUNS via :class:`IterativeExecutor` and the
    URGENT branch fires."""
    workspace = _make_workspace(tmp_path)
    target_yaml = (
        workspace / "bundles" / _TARGET_BUNDLE / "workflows" / "triage.yaml"
    )

    scripted = [
        ToolCallResponse(
            tool="validate_workflow",
            args={"workflow_ref": "kolsch/workflows/hello-agent"},
        ),
        ToolCallResponse(
            tool="write_file",
            args={"path": str(target_yaml), "content": _PT1_WORKFLOW_YAML},
        ),
        ToolCallResponse(
            tool="validate_workflow",
            args={"yaml_path": str(target_yaml)},
        ),
        TextResponse(
            text=(
                "Workflow ready at my-bundle/workflows/triage. "
                'Try it: /workflow my-bundle/workflows/triage with ticket_text="..."'
            )
        ),
    ]

    fake, events, _am = await _run_author_workflow_session(
        workspace, brief="Build a workflow that classifies a ticket.",
        scripted_responses=scripted,
    )

    # AC: tool-call sequence
    calls = _tool_call_sequence(events)
    assert [name for name, _ in calls] == [
        "validate_workflow",
        "write_file",
        "validate_workflow",
    ]

    # AC: file exists at the target bundle
    assert target_yaml.is_file(), f"expected workflow at {target_yaml}"

    # AC: the second validate_workflow result is is_valid: true.
    # Read it off the TOOL_RESULT event for the second validate call.
    validate_results = [
        e for e in events
        if e.type == EventType.TOOL_RESULT
        and e.data.get("tool_name") == "validate_workflow"
    ]
    assert len(validate_results) == 2
    second = validate_results[1].data["result"]
    assert second["is_valid"] is True, f"validate_workflow returned: {second}"

    # AC: final agent message surfaces the ref + the run hint.
    final = _final_agent_message(events) or ""
    assert "my-bundle/workflows/triage" in final
    assert "/workflow" in final

    # AC 2.3 — RUNNABILITY. Run the produced workflow via the real
    # IterativeExecutor; the scripted classifier returns "urgent" so
    # the URGENT branch fires.
    status, events_path = await _run_workflow(
        workspace,
        workflow_ref=f"{_TARGET_BUNDLE}/workflows/triage",
        inputs={"ticket_text": "server on fire"},
    )
    assert status == "succeeded", f"workflow run status: {status}"
    log = events_path.read_text(encoding="utf-8")
    assert "urgent_marker" in log and "normal_marker" in log

    # FakeLLM script should be fully consumed.
    assert not fake.script, f"unused scripted responses: {fake.script}"


# ===========================================================================
# Phase Test 2 — amendment loop
# ===========================================================================


# Adds a `write_classification` bash node downstream of the branch
# (single arm — only the URGENT side writes). Mirrors a realistic
# amendment "also persist the result to out/result.txt".
_PT2_WORKFLOW_YAML = """\
description: Classify, branch, and persist the URGENT verdict to disk.

nodes:
  classify:
    type: kolsch/nodes/agent
    agent: kolsch/agents/classifier
  is_urgent:
    type: kolsch/nodes/branch
    condition: "{{ inputs.label == 'urgent' }}"
  urgent_marker:
    type: kolsch/nodes/bash
    command: "echo URGENT"
  normal_marker:
    type: kolsch/nodes/bash
    command: "echo NORMAL"
  write_result:
    type: kolsch/nodes/write_file
    path: "out/result.txt"
    content: "URGENT\\n"

graph:
  edges:
    - from: classify
      to: is_urgent
      binds:
        label: "{{ nodes.classify.text }}"
    - from: is_urgent
      to: urgent_marker
      branch: true
    - from: is_urgent
      to: normal_marker
      branch: false
    - from: urgent_marker
      to: write_result
"""


async def test_amendment_loop(tmp_path: Path) -> None:
    """Phase Test 2 — seed the workspace with Phase Test 1's produced
    workflow, then drive the meta-agent through an amendment ("also
    write the classification to out/result.txt"). Assert: the workflow
    file is updated, re-validates, runs end-to-end, and the file is
    actually written."""
    workspace = _make_workspace(tmp_path)
    target_yaml = (
        workspace / "bundles" / _TARGET_BUNDLE / "workflows" / "triage.yaml"
    )
    target_yaml.write_text(_PT1_WORKFLOW_YAML, encoding="utf-8")

    scripted = [
        ToolCallResponse(
            tool="read_file", args={"path": str(target_yaml)}
        ),
        ToolCallResponse(
            tool="write_file",
            args={"path": str(target_yaml), "content": _PT2_WORKFLOW_YAML},
        ),
        ToolCallResponse(
            tool="validate_workflow", args={"yaml_path": str(target_yaml)}
        ),
        TextResponse(
            text=(
                "Amended my-bundle/workflows/triage to write out/result.txt. "
                'Re-run: /workflow my-bundle/workflows/triage with ticket_text="..."'
            )
        ),
    ]

    fake, events, _am = await _run_author_workflow_session(
        workspace,
        brief="Also write the classification to a file at out/result.txt.",
        scripted_responses=scripted,
    )

    calls = _tool_call_sequence(events)
    assert [name for name, _ in calls] == [
        "read_file",
        "write_file",
        "validate_workflow",
    ]

    assert target_yaml.read_text(encoding="utf-8") == _PT2_WORKFLOW_YAML
    last_validate = next(
        e for e in reversed(events)
        if e.type == EventType.TOOL_RESULT
        and e.data.get("tool_name") == "validate_workflow"
    )
    assert last_validate.data["result"]["is_valid"] is True

    # AC 2.3 — runnability + file side-effect.
    result_file = workspace / "out" / "result.txt"
    assert not result_file.exists(), "result.txt must NOT exist before the run"
    status, _ = await _run_workflow(
        workspace,
        workflow_ref=f"{_TARGET_BUNDLE}/workflows/triage",
        inputs={"ticket_text": "server on fire"},
    )
    assert status == "succeeded", f"workflow run status: {status}"
    assert result_file.exists(), "amended workflow should write out/result.txt"
    assert result_file.read_text(encoding="utf-8") == "URGENT\n"

    assert not fake.script


# ===========================================================================
# Phase Test 3 — authored Python @node generation
# ===========================================================================


_PT3_NODE_PY = '''\
"""Phase 3.11 Step 2 test fixture — count rows in a CSV file."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from workflows import NodeContext, node


class Inputs(BaseModel):
    csv_path: str


class Outputs(BaseModel):
    row_count: int


@node(label="Count CSV rows")
async def run(inputs: Inputs, ctx: NodeContext) -> Outputs:
    _ = ctx
    text = Path(inputs.csv_path).read_text(encoding="utf-8")
    # Subtract the header row if present; here we count all lines.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return Outputs(row_count=len(lines))
'''


_PT3_WORKFLOW_YAML_TEMPLATE = """\
description: Count rows in a CSV via an authored @node (terminal).

nodes:
  ready:
    type: kolsch/nodes/noop
  count:
    type: my-bundle/nodes/count_rows

graph:
  edges:
    - from: ready
      to: count
      binds:
        csv_path: "{csv_path}"
"""
# Single-terminal authored-@node pattern: the validator's v1 doesn't
# introspect authored-@node output schemas, so cross-node Jinja
# references to ``nodes.<authored>.<field>`` raise
# ``WorkflowJinjaRefUnknown(reason="not_declared")``. The skill's
# error-class table flags this; for the integration test the @node
# is the terminal and verification reads ``row_count`` off the run's
# JSONL completion event. ``csv_path`` is bound via the ``noop ->
# count`` edge because authored-Python nodes read their Inputs model
# from ``inputs.<key>`` (set by binds), NOT from direct YAML config
# fields on the node body.


async def test_node_module_generation(tmp_path: Path) -> None:
    """Phase Test 3 — meta-agent drafts a new authored ``@node`` module,
    introspects it, then references it from a workflow. AC 2.3: the
    workflow runs end-to-end and the bash terminal echoes the row
    count the authored ``@node`` returned."""
    workspace = _make_workspace(tmp_path)
    node_py = (
        workspace / "bundles" / _TARGET_BUNDLE / "nodes" / "count_rows.py"
    )
    target_yaml = (
        workspace / "bundles" / _TARGET_BUNDLE / "workflows" / "count.yaml"
    )

    csv_path = workspace / "data" / "rows.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("a,b\n1,2\n3,4\n5,6\n", encoding="utf-8")
    workflow_yaml = _PT3_WORKFLOW_YAML_TEMPLATE.format(csv_path=str(csv_path))

    scripted = [
        ToolCallResponse(
            tool="write_file",
            args={"path": str(node_py), "content": _PT3_NODE_PY},
        ),
        ToolCallResponse(
            tool="introspect_node", args={"py_path": str(node_py)}
        ),
        ToolCallResponse(
            tool="write_file",
            args={"path": str(target_yaml), "content": workflow_yaml},
        ),
        ToolCallResponse(
            tool="validate_workflow", args={"yaml_path": str(target_yaml)}
        ),
        TextResponse(
            text=(
                "Wrote my-bundle/nodes/count_rows.py and "
                "my-bundle/workflows/count. "
                "Try it: /workflow my-bundle/workflows/count"
            )
        ),
    ]

    fake, events, _am = await _run_author_workflow_session(
        workspace,
        brief="Build a workflow that takes a CSV path, counts rows, prints the count.",
        scripted_responses=scripted,
    )

    calls = _tool_call_sequence(events)
    assert [name for name, _ in calls] == [
        "write_file",
        "introspect_node",
        "write_file",
        "validate_workflow",
    ]

    assert node_py.is_file()
    assert target_yaml.is_file()

    introspect_result = next(
        e for e in events
        if e.type == EventType.TOOL_RESULT
        and e.data.get("tool_name") == "introspect_node"
    ).data["result"]
    assert introspect_result["signature_ok"] is True
    assert introspect_result["label"] == "Count CSV rows"

    validate_result = next(
        e for e in events
        if e.type == EventType.TOOL_RESULT
        and e.data.get("tool_name") == "validate_workflow"
    ).data["result"]
    assert validate_result["is_valid"] is True, (
        f"validate_workflow errors: {validate_result.get('errors')}"
    )

    # AC 2.3 — runnability. No LLM needed for this workflow.
    status, events_path = await _run_workflow(
        workspace, workflow_ref=f"{_TARGET_BUNDLE}/workflows/count"
    )
    assert status == "succeeded", f"workflow run status: {status}"
    log = events_path.read_text(encoding="utf-8")
    # The authored @node ran end-to-end; its Outputs(row_count=4)
    # surfaces in the node-completed event payload AND in the
    # workflow run's terminal output dict. (CSV has 4 non-empty
    # lines including the header; the @node counts every non-empty
    # line per its implementation.)
    assert '"row_count": 4' in log or '"row_count":4' in log, (
        "row_count=4 not in events log"
    )

    assert not fake.script


# ===========================================================================
# Phase Test 4 — validation-driven revision (recover from WorkflowBindCollision)
# ===========================================================================


# Two upstream noops both bind the same key `message` into the
# receiver — the classic WorkflowBindCollision pattern.
_PT4_DRAFT1 = """\
description: Bind-collision draft (intentionally broken).

nodes:
  alpha:
    type: kolsch/nodes/noop
  beta:
    type: kolsch/nodes/noop
  sink:
    type: kolsch/nodes/echo

graph:
  edges:
    - from: alpha
      to: sink
      binds:
        message: "from-alpha"
    - from: beta
      to: sink
      binds:
        message: "from-beta"
"""

# Draft 2 renames beta's bind to `also` — the engine's
# WorkflowBindCollision is gone. (echo's Inputs only declares
# `message`; the extra `also` is accepted because the catalog node
# def is open-extra; the workflow validator's job is structural.)
_PT4_DRAFT2 = """\
description: Bind-collision recovery (renamed beta's bind).

nodes:
  alpha:
    type: kolsch/nodes/noop
  beta:
    type: kolsch/nodes/noop
  sink:
    type: kolsch/nodes/echo

graph:
  edges:
    - from: alpha
      to: sink
      binds:
        message: "from-alpha"
    - from: beta
      to: sink
      binds:
        also: "from-beta"
"""


async def test_validation_driven_revision(tmp_path: Path) -> None:
    """Phase Test 4 — first draft trips WorkflowBindCollision; the
    meta-agent reads the error class + identifying_names, renames the
    colliding bind, re-validates. Second draft passes."""
    workspace = _make_workspace(tmp_path)
    target_yaml = (
        workspace / "bundles" / _TARGET_BUNDLE / "workflows" / "collide.yaml"
    )

    scripted = [
        ToolCallResponse(
            tool="write_file",
            args={"path": str(target_yaml), "content": _PT4_DRAFT1},
        ),
        ToolCallResponse(
            tool="validate_workflow", args={"yaml_path": str(target_yaml)}
        ),
        ToolCallResponse(
            tool="write_file",
            args={"path": str(target_yaml), "content": _PT4_DRAFT2},
        ),
        ToolCallResponse(
            tool="validate_workflow", args={"yaml_path": str(target_yaml)}
        ),
        TextResponse(
            text=(
                "Renamed beta's bind to resolve the collision. "
                "Try it: /workflow my-bundle/workflows/collide"
            )
        ),
    ]

    fake, events, _am = await _run_author_workflow_session(
        workspace,
        brief="Build a workflow that fans two noops into one echo.",
        scripted_responses=scripted,
    )

    calls = _tool_call_sequence(events)
    assert [name for name, _ in calls] == [
        "write_file",
        "validate_workflow",
        "write_file",
        "validate_workflow",
    ]

    # Assert the first validate returned the collision finding.
    validate_results = [
        e for e in events
        if e.type == EventType.TOOL_RESULT
        and e.data.get("tool_name") == "validate_workflow"
    ]
    first_result = validate_results[0].data["result"]
    assert first_result["is_valid"] is False
    classes = {err["error_class"] for err in first_result["errors"]}
    assert "WorkflowBindCollision" in classes
    collision = next(
        err for err in first_result["errors"]
        if err["error_class"] == "WorkflowBindCollision"
    )
    assert collision["identifying_names"] == {"node": "sink", "bind": "message"}

    # Assert the second validate is clean.
    second_result = validate_results[1].data["result"]
    assert second_result["is_valid"] is True

    # The two write_file payloads differ at the colliding bind key only.
    # (Use the structured args off the FakeLLM call record.)
    write_calls = [r for r in fake.calls if r.response.kind == "tool_call"
                   and r.response.tool == "write_file"]
    assert len(write_calls) == 2
    draft1 = write_calls[0].response.args["content"]
    draft2 = write_calls[1].response.args["content"]
    assert draft1 == _PT4_DRAFT1
    assert draft2 == _PT4_DRAFT2
    # Sanity — diff falls on the second `message:` line.
    assert '        also: "from-beta"' in draft2
    assert '        also: "from-beta"' not in draft1

    # AC 2.3 — runnability. The recovered workflow runs end-to-end.
    status, _ = await _run_workflow(
        workspace, workflow_ref=f"{_TARGET_BUNDLE}/workflows/collide"
    )
    assert status == "succeeded", f"workflow run status: {status}"

    assert not fake.script


# ===========================================================================
# Phase Test 5 — MAX_ATTEMPTS cap (Step 3)
# ===========================================================================


# A draft referencing a non-existent built-in slot. Every attempt
# will fail the same way under `validate_workflow` — perfect for
# verifying the iteration loop caps at 5 without making a 6th call.
_PT5_DRAFT_TEMPLATE = """\
description: Attempt {attempt} — uses a non-existent built-in (intentional).

nodes:
  bad:
    type: kolsch/nodes/quantum_compute
    description: "attempt #{attempt}"

graph:
  edges: []
"""


async def test_max_attempts_cap(tmp_path: Path) -> None:
    """Phase Test 5 — five draft → validate cycles all return errors
    (the type ref doesn't exist). The agent surfaces the closest
    draft + remaining errors WITHOUT a sixth validate call.

    The cap (5) is enforced by the skill's iteration recipe — the
    test verifies the scripted sequence matches that contract
    exactly (5 validates, then escalation text).
    """
    workspace = _make_workspace(tmp_path)
    target_yaml = (
        workspace / "bundles" / _TARGET_BUNDLE / "workflows" / "doomed.yaml"
    )

    scripted: list[FakeLLMResponse] = []
    for attempt in range(1, 6):
        scripted.append(
            ToolCallResponse(
                tool="write_file",
                args={
                    "path": str(target_yaml),
                    "content": _PT5_DRAFT_TEMPLATE.format(attempt=attempt),
                },
            )
        )
        scripted.append(
            ToolCallResponse(
                tool="validate_workflow",
                args={"yaml_path": str(target_yaml)},
            )
        )
    scripted.append(
        TextResponse(
            text=(
                "Hit MAX_ATTEMPTS (5) without a green validation. "
                "Closest draft is at my-bundle/workflows/doomed. "
                "Remaining error: kolsch/nodes/quantum_compute is not "
                "a known catalog ref. Direction needed: should this be "
                "a new authored @node, or did you mean a different "
                "built-in type?"
            )
        )
    )

    fake, events, _am = await _run_author_workflow_session(
        workspace,
        brief="Use a node type called kolsch/nodes/quantum_compute.",
        scripted_responses=scripted,
    )

    # AC 3.1 — exactly 5 validate_workflow calls; the 6th attempt
    # is replaced by the escalation text.
    validate_calls = [
        name for name, _ in _tool_call_sequence(events)
        if name == "validate_workflow"
    ]
    assert len(validate_calls) == 5, (
        f"expected 5 validate_workflow calls (MAX_ATTEMPTS); got "
        f"{len(validate_calls)}"
    )

    # Every validate result must be is_valid: false — the test fails
    # closed if any earlier draft accidentally passed.
    validate_results = [
        e for e in events
        if e.type == EventType.TOOL_RESULT
        and e.data.get("tool_name") == "validate_workflow"
    ]
    assert all(
        r.data["result"]["is_valid"] is False for r in validate_results
    ), "every validate call must return is_valid: false in this scenario"

    # The final agent message includes the cap-reached escalation.
    final = _final_agent_message(events) or ""
    assert "MAX_ATTEMPTS" in final or "5" in final
    assert "doomed" in final
    assert "Direction needed" in final or "different" in final

    # Script fully consumed — confirms no 6th attempt slipped in.
    assert not fake.script


# ===========================================================================
# Phase Test 6 — target-bundle isolation (Step 3)
# ===========================================================================


def _snapshot_bundle(root: Path) -> dict[str, bytes]:
    """Capture every author-controlled file under ``root`` as
    ``{relpath: content-bytes}``.

    Used to verify the ``kolsch`` package bundle is byte-identical
    before and after the meta-agent runs (AC 3.2 — the package
    bundle is read-only by convention). Skips ``__pycache__`` —
    Python writes per-import bytecode there with embedded mtimes,
    which churns on every test run and is NOT an authored mutation.
    """
    snapshot: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        snapshot[rel] = path.read_bytes()
    return snapshot


async def test_writes_to_target_bundle_only(tmp_path: Path) -> None:
    """Phase Test 6 — set ``author_workflow.target_bundle: my-bundle``
    in ``platform.yaml``; drive Phase Test 1's brief; verify EVERY
    ``write_file`` invocation lands under ``bundles/my-bundle/`` AND
    the ``bundles/kolsch/`` bundle is byte-identical before/after.
    """
    workspace = _make_workspace(tmp_path)

    # Patch platform.yaml to declare the target bundle explicitly.
    pyaml = workspace / "platform.yaml"
    pyaml.write_text(
        pyaml.read_text(encoding="utf-8")
        + "\nauthor_workflow:\n  target_bundle: my-bundle\n",
        encoding="utf-8",
    )

    kolsch_bundle = workspace / "bundles" / "kolsch"
    before = _snapshot_bundle(kolsch_bundle)

    target_yaml = (
        workspace / "bundles" / _TARGET_BUNDLE / "workflows" / "triage.yaml"
    )

    scripted = [
        ToolCallResponse(
            tool="validate_workflow",
            args={"workflow_ref": "kolsch/workflows/hello-agent"},
        ),
        ToolCallResponse(
            tool="write_file",
            args={"path": str(target_yaml), "content": _PT1_WORKFLOW_YAML},
        ),
        ToolCallResponse(
            tool="validate_workflow",
            args={"yaml_path": str(target_yaml)},
        ),
        TextResponse(
            text=(
                "Workflow ready at my-bundle/workflows/triage. "
                "Try it: /workflow my-bundle/workflows/triage"
            )
        ),
    ]

    fake, events, _am = await _run_author_workflow_session(
        workspace,
        brief="Build a workflow that classifies a ticket.",
        scripted_responses=scripted,
    )

    # AC 3.2 — every write_file invocation targets bundles/my-bundle/.
    # No accidental writes into bundles/kolsch/ or anywhere else.
    write_calls = [
        (name, args) for name, args in _tool_call_sequence(events)
        if name == "write_file"
    ]
    assert write_calls, "expected at least one write_file invocation"
    kolsch_prefix = (workspace / "bundles" / "kolsch").as_posix()
    target_prefix = (workspace / "bundles" / _TARGET_BUNDLE).as_posix()
    for _name, args in write_calls:
        path = args.get("path", "")
        assert not path.startswith(kolsch_prefix), (
            f"write_file targeted the kolsch package bundle: {path}"
        )
        assert path.startswith(target_prefix), (
            f"write_file targeted outside the configured target "
            f"bundle: {path} (expected prefix {target_prefix!r})"
        )

    # AC 3.2 — the kolsch bundle is byte-identical (file set + content).
    after = _snapshot_bundle(kolsch_bundle)
    assert set(after) == set(before), (
        f"kolsch bundle file set changed: "
        f"added={set(after) - set(before)} "
        f"removed={set(before) - set(after)}"
    )
    for rel, content in before.items():
        assert after[rel] == content, (
            f"kolsch bundle file {rel!r} mutated; write to the "
            "package bundle is a P0 violation"
        )

    # Sanity — the target file IS where we expect it.
    assert target_yaml.is_file()
    assert not fake.script


# ===========================================================================
# Phase Test 7 — kolsch-only workspace surfaces escalation (Step 3 follow-up)
# ===========================================================================


def _make_workspace_kolsch_only(tmp_path: Path) -> Path:
    """Variant of :func:`_make_workspace` that ships ONLY the kolsch
    package bundle — no user bundle at all.

    Used by Phase Test 7 to exercise the skill's ``"Where to write"``
    escalation path. ``platform.yaml`` stays permissive but omits
    ``author_workflow.target_bundle`` so the default resolution
    (auto-pick primary user bundle) trips the "only kolsch exists"
    guard the skill documents at SKILL.md §"Where to write" step 3.
    """
    for sub in ("bundles", "sessions", "content", "logs", "data", "out", ".platform"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "platform.yaml").write_text(
        _PERMISSIVE_PLATFORM_YAML, encoding="utf-8"
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")
    shutil.copytree(_STARTER_BUNDLE, tmp_path / "bundles" / "kolsch")
    return tmp_path


async def test_kolsch_only_workspace_surfaces_escalation(
    tmp_path: Path,
) -> None:
    """Phase Test 7 — a workspace containing ONLY the kolsch package
    bundle (no user bundle) must NOT be written to by the meta-agent.

    The skill's ``"Where to write"`` section (SKILL.md, step 3)
    documents the error path: absent
    ``author_workflow.target_bundle`` AND no user bundle → surface
    ``"No user bundle is configured. Either set
    author_workflow.target_bundle in platform.yaml, or create a bundle
    with kolsch bundles init <name>."`` and stop. The scripted
    response models this: no tool calls, just the escalation text.

    Like Phase Test 6, this verifies the CONTRACT (workspace shape +
    "no writes, only escalation" response leaves ``bundles/kolsch/``
    byte-identical). It does NOT verify a real LLM would obey the
    skill's prescription — that's on the model + prompt + skill to
    get right; a scripted-LLM test can only assert the infrastructure
    supports the contract. Phase Test 6 covers the happy path (user
    bundle present → writes go there); Phase Test 7 covers the error
    path (only kolsch present → no writes, escalation surfaced).
    """
    workspace = _make_workspace_kolsch_only(tmp_path)

    # Sanity — the workspace really only has the kolsch bundle.
    other_bundles = [
        p
        for p in (workspace / "bundles").iterdir()
        if p.is_dir() and p.name != "kolsch"
    ]
    assert not other_bundles, (
        f"expected only the kolsch bundle; found: {other_bundles!r}"
    )

    kolsch_bundle = workspace / "bundles" / "kolsch"
    before = _snapshot_bundle(kolsch_bundle)

    # Scripted response models the skill's "Where to write" step 3
    # verbatim — the model reads the skill, sees no user bundle, and
    # surfaces the escalation message without invoking any tool.
    scripted = [
        TextResponse(
            text=(
                "No user bundle is configured. Either set "
                "author_workflow.target_bundle in platform.yaml, or "
                "create a bundle with 'kolsch bundles init "
                "<name>'. I won't write into bundles/kolsch/ — it "
                "ships with the engine and is read-only by "
                "convention."
            )
        ),
    ]

    fake, events, _am = await _run_author_workflow_session(
        workspace,
        brief="Build a workflow that classifies a ticket.",
        scripted_responses=scripted,
    )

    # AC — no write_file (or any other mutating tool) invocations.
    tool_calls = [name for name, _args in _tool_call_sequence(events)]
    assert "write_file" not in tool_calls, (
        f"expected zero write_file calls in the escalation path; "
        f"got: {tool_calls}"
    )

    # AC — final agent message carries the prescribed escalation
    # markers so operators know exactly how to fix the workspace.
    final = _final_agent_message(events) or ""
    assert "No user bundle is configured" in final
    assert "author_workflow.target_bundle" in final
    assert "kolsch bundles init" in final

    # AC — kolsch bundle byte-identical (no accidental writes landed
    # while the agent was surfacing the escalation).
    after = _snapshot_bundle(kolsch_bundle)
    assert set(after) == set(before), (
        f"kolsch bundle file set changed: "
        f"added={set(after) - set(before)} "
        f"removed={set(before) - set(after)}"
    )
    for rel, content in before.items():
        assert after[rel] == content, (
            f"kolsch bundle file {rel!r} mutated; write to the "
            "package bundle is a P0 violation"
        )

    # Sanity — the single scripted response was consumed exactly once.
    assert not fake.script
