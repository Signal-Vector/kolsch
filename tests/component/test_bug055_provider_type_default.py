"""Component tests for bug-055 — missing `type:` on `llm_providers` entries.

Covers three surfaces:

1. **Construction sites (`platform_runtime.runtime` and
   `cli.llm_commands._provider_configs_from_payload`)** default a
   missing `type:` to the provider dictionary key so a
   correct-by-convention entry (`opencode: {api_key_secret: ...,
   base_url: ...}` with no `type:` line) resolves to the `opencode`
   connector.
2. **`LlmConnectorUnknown` error** — when the type genuinely can't be
   resolved, the raised message names the offending provider entry,
   the unresolved type string, the registered types list, and points
   at `platform.yaml`.
3. **Workflow node-failure payload** — an agent node whose spawned
   sub-agent terminates with a typed `llm.connector_unknown` (or any
   `thread.exchange_failed` typed error) surfaces the nested error's
   `type` + `message` + `event_id` on the `workflow.node.failed`
   payload's `nested_error` field, alongside the preserved
   `parent_event_id` linkage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest

from assets.catalog import AssetCatalog, AssetEntry
from cli.llm_commands import _provider_configs_from_payload
from engine.context import AgentExecutionError
from errors import LlmConnectorUnknown
from llm import ProviderConfig, registry
from llm.providers.opencode import OpenCodeConnector
from schemas import (
    Actor,
    AgentId,
    Event,
    EventId,
    EventType,
    IOSpec,
    ModelSpec,
    ResolvedAgentConfig,
    SessionId,
    ThreadId,
)
from store import SqliteBackend
from store.session_store import SessionStore
from workflows.builtins.agent import AgentRunner, _extract_nested_error
from workflows.context import NodeContext
from workflows.errors import WorkflowNodeFailed
from workflows.ids import InvocationId, WorkflowRunId
from workflows.parser import AgentNodeDef

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from engine import AgentEngine, ExecutionContext


# ===========================================================================
# Test 1 — Missing `type` defaults to the provider key at both sites
# ===========================================================================


def _build_provider_config_via_runtime_loader(
    raw_llm: dict[str, Any],
) -> dict[str, ProviderConfig]:
    """Mirror the construction body in `platform_runtime.runtime.build`.

    The production loader is inlined inside `build(...)` (it doesn't
    have a standalone module-level entry point), so this helper
    reproduces the shape byte-for-byte. Kept in sync with
    `src/platform_runtime/runtime.py:~307-329`.
    """
    out: dict[str, ProviderConfig] = {}
    for provider_name, raw in raw_llm.items():
        if not isinstance(raw, dict):
            continue
        out[provider_name] = ProviderConfig(
            name=provider_name,
            type=raw.get("type") or provider_name,
            api_key_secret=raw.get("api_key_secret"),
            base_url=raw.get("base_url"),
            timeout_seconds=raw.get("timeout_seconds"),
            extras={
                k: v
                for k, v in raw.items()
                if k
                not in {
                    "type",
                    "api_key_secret",
                    "base_url",
                    "timeout_seconds",
                    "models",
                }
            },
            models=list(raw.get("models") or []),
        )
    return out


def test_provider_config_missing_type_defaults_to_key_runtime_loader() -> None:
    """Runtime loader: a `platform.yaml`-shaped raw dict with no
    `type:` field on the `opencode` entry yields
    `ProviderConfig.type == "opencode"`, and the registry lookup
    resolves it to :class:`OpenCodeConnector`."""
    raw = {
        "opencode": {
            "api_key_secret": "OPENCODE_API_KEY",
            "base_url": "https://opencode.example.com/v1",
        },
    }
    configs = _build_provider_config_via_runtime_loader(raw)
    cfg = configs["opencode"]
    assert cfg.type == "opencode"
    connector = registry.get(cfg.type)
    assert isinstance(connector, OpenCodeConnector)


def test_provider_config_missing_type_defaults_to_key_cli_loader() -> None:
    """CLI loader (`_provider_configs_from_payload`): same shape,
    same guarantee — the loader used by `kolsch connect` and adjacent
    commands must also default a missing `type:` to the key."""
    payload = {
        "llm_providers": {
            "opencode": {
                "api_key_secret": "OPENCODE_API_KEY",
                "base_url": "https://opencode.example.com/v1",
            },
        },
    }
    configs = _provider_configs_from_payload(payload)
    cfg = configs["opencode"]
    assert cfg.type == "opencode"
    connector = registry.get(cfg.type)
    assert isinstance(connector, OpenCodeConnector)


def test_provider_config_explicit_type_still_wins_runtime_loader() -> None:
    """AC 5 regression: an explicit `type:` value is honored — the
    default only fills in when the field is missing or empty."""
    raw = {
        "opencode_prod": {
            "type": "opencode",  # explicit — key differs from type
            "api_key_secret": "OPENCODE_PROD_API_KEY",
            "base_url": "https://opencode-prod.example.com/v1",
        },
    }
    configs = _build_provider_config_via_runtime_loader(raw)
    cfg = configs["opencode_prod"]
    assert cfg.type == "opencode"


def test_provider_config_explicit_type_still_wins_cli_loader() -> None:
    """AC 5 regression (CLI loader mirror)."""
    payload = {
        "llm_providers": {
            "opencode_prod": {
                "type": "opencode",
                "api_key_secret": "OPENCODE_PROD_API_KEY",
                "base_url": "https://opencode-prod.example.com/v1",
            },
        },
    }
    configs = _provider_configs_from_payload(payload)
    cfg = configs["opencode_prod"]
    assert cfg.type == "opencode"


def test_provider_config_empty_string_type_defaults_to_key() -> None:
    """A `type:` field present but empty (`type: ""`) also defaults —
    the `or provider_name` idiom treats empty-string falsy the same
    as missing."""
    raw = {
        "opencode": {
            "type": "",
            "api_key_secret": "OPENCODE_API_KEY",
        },
    }
    configs = _build_provider_config_via_runtime_loader(raw)
    assert configs["opencode"].type == "opencode"
    payload = {"llm_providers": raw}
    cli_configs = _provider_configs_from_payload(payload)
    assert cli_configs["opencode"].type == "opencode"


# ===========================================================================
# Test 2 — Unknown type raises a descriptive, provider-named error
# ===========================================================================


def test_connector_unknown_error_names_provider_and_platform_yaml() -> None:
    """AC 2: a provider with an explicit typo'd `type:` (e.g.
    `type: opncode`) raises an error whose message names the
    provider, the offending type string, the registered types list,
    and mentions `platform.yaml`."""
    with pytest.raises(LlmConnectorUnknown) as exc_info:
        registry.get("opncode", provider_name="opencode")
    msg = str(exc_info.value)
    assert "opencode" in msg  # provider name
    assert "opncode" in msg  # offending type
    assert "platform.yaml" in msg  # config-file pointer
    # Registered types list — check both are named (opencode + openrouter).
    assert "opencode" in msg
    assert "openrouter" in msg
    # Distinguishing "unknown value" vs "missing" — this is the
    # unknown-value branch (type set but unrecognised).
    assert "unknown" in msg.lower()
    # Detail carries the structured provider name for typed consumers.
    assert exc_info.value.detail["provider"] == "opencode"
    assert exc_info.value.detail["type"] == "opncode"


def test_connector_unknown_error_missing_type_branch() -> None:
    """When `type_name` is empty AND a provider name is threaded, the
    message distinguishes "missing" from "unknown value" so operators
    can tell which failure they're looking at."""
    with pytest.raises(LlmConnectorUnknown) as exc_info:
        registry.get("", provider_name="opencode")
    msg = str(exc_info.value)
    assert "missing" in msg.lower()
    assert "opencode" in msg
    assert "platform.yaml" in msg


def test_connector_unknown_error_backwards_compat_no_provider_name() -> None:
    """Callers that don't thread `provider_name` (older sites, tests)
    still get the legacy shape — no provider-named phrasing, but
    still typed as :class:`LlmConnectorUnknown` so downstream typed
    catches continue to match."""
    with pytest.raises(LlmConnectorUnknown) as exc_info:
        registry.get("nope")
    assert exc_info.value.code == "llm.connector_unknown"
    assert "nope" in str(exc_info.value)
    assert exc_info.value.detail["type"] == "nope"


# ===========================================================================
# Test 3 — Workflow node failure surfaces the nested typed error
# ===========================================================================


_AGENT_REF = "demo/agents/classifier"


class _MockEngine:
    """Minimal :class:`AgentEngine` stand-in — see
    `tests/component/test_workflow_agent_runner.py` for the base
    pattern; this one always emits a `thread.exchange_failed`
    carrying a typed `llm.connector_unknown` payload so the runner's
    nested-error extraction can be verified.
    """

    def __init__(self, *, failed_event: Event) -> None:
        self.failed_event = failed_event
        self.calls: list[Any] = []

    async def execute_stream(
        self,
        config: ResolvedAgentConfig,
        context: ExecutionContext,
        *,
        inputs: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((config, context, dict(inputs or {})))
        yield self.failed_event


def _as_engine(mock: _MockEngine) -> AgentEngine:
    return cast("AgentEngine", mock)


def _make_resolved_config() -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        agent_id=AgentId("ag_demo_classifier"),
        name="classifier",
        description="A test classifier agent.",
        model=ModelSpec(
            provider="mock",
            model="mock-model",
        ),
        prompt="You are a classifier.",
        io=IOSpec(
            mode="single_shot",
            inputs={"text": {"type": "str"}},
            outputs={"label": {"type": "str"}},
        ),
    )


def _make_catalog() -> AssetCatalog:
    return AssetCatalog(
        assets={
            _AGENT_REF: AssetEntry(
                ref=_AGENT_REF,
                asset_type="agent",
                bundle="demo",
                path="/dev/null",
            ),
        },
    )


def _make_failed_event_with_typed_error(
    *,
    error_type: str = "llm.connector_unknown",
    error_message: str = (
        "provider 'opencode' in platform.yaml is missing its `type:` field. "
        "Registered types: ['opencode', 'openrouter']."
    ),
) -> Event:
    """Build a `thread.exchange_failed` event with a typed error
    envelope, mirroring what the engine emits when `build_llm` raises
    `LlmConnectorUnknown`."""
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.THREAD_EXCHANGE_FAILED,
        data={
            "exchange_started_id": f"ev_{uuid4()}",
            "error": {
                "type": error_type,
                "message": error_message,
                "where": "llm.registry.get",
                "detail": {
                    "provider": "opencode",
                    "type": "",
                    "registered_types": ["opencode", "openrouter"],
                },
                "cause": None,
            },
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


def test_extract_nested_error_from_thread_exchange_failed_event() -> None:
    """Unit-level check on the extractor — the workflow executor is
    the ultimate consumer, but exercising the helper directly nails
    the shape contract."""
    event = _make_failed_event_with_typed_error()
    nested = _extract_nested_error(event)
    assert nested is not None
    assert nested["type"] == "llm.connector_unknown"
    assert "opencode" in nested["message"]
    assert "platform.yaml" in nested["message"]
    assert nested["event_id"] == str(event.event_id)


def test_extract_nested_error_returns_none_for_missing_shapes() -> None:
    """Defensive: a `None` failed_event, a data dict without `error`,
    or an error missing `type` / `message` all return `None` — the
    executor falls back to the wrapper's message alone in that case.
    """
    assert _extract_nested_error(None) is None

    class _FauxEvent:
        def __init__(self) -> None:
            self.event_id = "ev_x"
            self.data: dict[str, Any] = {}

    assert _extract_nested_error(_FauxEvent()) is None

    class _PartialEvent:
        def __init__(self) -> None:
            self.event_id = "ev_x"
            self.data: dict[str, Any] = {"error": {"type": "x"}}  # missing `message`

    assert _extract_nested_error(_PartialEvent()) is None


@pytest.fixture
async def session_store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    try:
        yield SessionStore(root=tmp_path, backend=backend)
    finally:
        await backend.close()


async def test_workflow_node_failed_surface_carries_nested_error(
    tmp_path: Path,
) -> None:
    """AC 3 (executor-side): a runner that raises
    :class:`WorkflowNodeFailed` with a populated `nested_error`
    surfaces the same dict on the emitted `workflow.node.failed`
    event's payload — verifiable without standing up the full
    `AgentRunner` + engine stack. The `parent_event_id` linkage
    (chained to the node's `workflow.node.started` id) is preserved
    alongside the added `nested_error` field.
    """
    from assets.catalog import AssetCatalog as _AssetCatalog
    from workflows.executor import IterativeExecutor, RunnerRegistry
    from workflows.parser import Workflow
    from workflows.run import WorkflowRunStatus
    from workflows.run_store import WorkflowRunStore

    nested = {
        "type": "llm.connector_unknown",
        "message": (
            "provider 'opencode' in platform.yaml is missing "
            "its `type:` field. Registered types: "
            "['opencode', 'openrouter']."
        ),
        "event_id": f"ev_{uuid4()}",
    }

    class _NestedErrorRunner:
        async def run(self, **kw: Any) -> dict[str, Any]:
            raise WorkflowNodeFailed(
                node=kw["node_name"],
                invocation_id=str(kw["ctx"].invocation_id),
                nested_error=nested,
            )

    wf = Workflow.model_validate(
        {
            "nodes": {"only": {"type": "kolsch/nodes/noop"}},
            "graph": {"edges": []},
        }
    )
    reg = RunnerRegistry()
    reg.register("kolsch/nodes/noop", _NestedErrorRunner())
    store = WorkflowRunStore(tmp_path)
    run = store.create(workflow_ref="demo/workflows/x", inputs={})
    ex = IterativeExecutor(
        workflow=wf,
        run=run,
        store=store,
        registry=reg,
        catalog=_AssetCatalog(),
    )
    status = await ex.run()
    assert status == WorkflowRunStatus.FAILED

    events = list(store.read_events(run.run_id))
    failed = next(e for e in events if e["kind"] == "workflow.node.failed")
    payload = failed["payload"]
    # AC: nested_error propagated onto the payload verbatim.
    assert payload["nested_error"] == nested
    # Existing shape untouched — wrapper message + error_class still
    # present so downstream typed consumers keep working.
    assert payload["error_class"] == "WorkflowNodeFailed"
    assert payload["node_name"] == "only"
    # `ev_...` event linkage: the failed event's `parent_event_id`
    # chains to the node's `workflow.node.started` id (Coordinator
    # note — bug-055 must NOT replace this).
    started = next(e for e in events if e["kind"] == "workflow.node.started")
    assert failed["parent_event_id"] == started["event_id"]


async def test_workflow_agent_runner_node_failed_error_carries_nested(
    tmp_path: Path,
    session_store: SessionStore,
) -> None:
    """AC 3: when the spawned agent terminates with a typed
    `thread.exchange_failed`, :class:`AgentRunner` remaps to
    :class:`WorkflowNodeFailed` with `nested_error` populated from
    the event's `data["error"]`. The executor's failure emit then
    surfaces this in `workflow.node.failed.payload.nested_error`.
    """

    async def _builder(_ref: str) -> ResolvedAgentConfig:
        return _make_resolved_config()

    failed_event = _make_failed_event_with_typed_error()
    engine = _MockEngine(failed_event=failed_event)
    runner = AgentRunner(
        catalog=_make_catalog(),
        session_store=session_store,
        config_builder=_builder,
        engine=_as_engine(engine),
    )

    ctx = NodeContext.for_test(
        workflow_run_id=str(WorkflowRunId(f"wrk_{uuid4()}")),
        invocation_id=str(InvocationId(f"inv_{uuid4()}")),
        bundle="kolsch",
        workspace_root=tmp_path,
    )
    object.__setattr__(ctx, "parent_event_id", EventId(f"ev_{uuid4()}"))

    with pytest.raises(WorkflowNodeFailed) as exc_info:
        await runner.run(
            node_name="classify",
            node_def=AgentNodeDef(
                type="kolsch/nodes/agent", agent=_AGENT_REF, outputs=None
            ),
            bound_inputs={"text": "hello"},
            state={},
            ctx=ctx,
        )

    raised = exc_info.value
    # AC: the nested typed error is carried on the exception so the
    # executor's failure emit can lift it into the payload.
    assert raised.nested_error is not None
    assert raised.nested_error["type"] == "llm.connector_unknown"
    assert "platform.yaml" in raised.nested_error["message"]
    assert raised.nested_error["event_id"] == str(failed_event.event_id)
    # The chained cause is still the `AgentExecutionError` — the
    # existing `ev_...` linkage via `__cause__.failed_event` is
    # preserved (bug-055 does NOT replace it; it adds `nested_error`
    # alongside).
    assert isinstance(raised.__cause__, AgentExecutionError)
    assert raised.__cause__.failed_event is failed_event
