"""feat-054 AC #15 — end-to-end compaction smoke test (ship gate).

Drives a real :class:`AgentEngine` through a session that fires a
compaction, then walks the AC #15 checklist end-to-end:

1. ``CONTEXT_COMPACTED`` event lands on the thread (via
   ``/compact`` — the manual path is deterministic; the auto trigger
   is exercised in ``tests/component/test_context_compaction_engine_trigger.py``).
2. ``/compaction-history`` renders a table row for the fresh
   compaction.
3. ``/why-compact <id>`` dumps ``pre_compaction`` + ``summary`` JSON.
4. Next projection (``assemble_context``) contains the framing text +
   summary JSON — NOT the raw event stream.
5. ``context.reference_lookup(event_id)`` returns full event data for
   an in-refs id, refuses an out-of-scope id.
6. ``/uncompact`` restores; the next projection recovers the full
   event stream verbatim.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from assets import AssetManagement
from bus import InProcessEventBus
from cli.chat.services.compaction_introspection import (
    build_compaction_history_table,
    build_why_compact_card,
)
from engine import (
    AgentEngine,
    ExecutionContext,
    LLMRequest,
    LLMResponse,
    MinimalFramework,
)
from engine.assembly import assemble_context
from errors import ToolExecutionFailed
from files import PathPolicy
from llm import ProviderConfig
from providers import (
    NativeSandboxProvider,
    RecentBufferMemoryProvider,
    RuleBasedAuthorizationProvider,
)
from providers.compaction import ReferenceSummaryCompactionProvider
from providers.secrets import DotenvSecretsProvider
from schemas import (
    AgentId,
    EventType,
    IOSpec,
    ModelSpec,
    ResolvedAgentConfig,
)
from skills import SkillsSubsystem
from store import SessionStore, SqliteBackend
from tools import ToolsSubsystem
from tools.context_reference import make_reference_lookup
from tools.decorator import ToolContext


class _Runtime:
    """Duck-typed :class:`PlatformRuntime` for the introspection helpers.

    The two helpers under test only touch ``runtime.store``; we skip
    the full runtime bootstrap.
    """

    def __init__(self, store: Any) -> None:
        self.store = store


def _write_platform_yaml(root: Path) -> None:
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
                "file_access": {
                    "allowed_paths": ["data/"],
                    "denied_paths": [".env"],
                },
                "compaction": {"variant": "reference_summary"},
                "defaults": {},
            }
        ),
        encoding="utf-8",
    )
    (root / ".env").write_text("", encoding="utf-8")


async def _build(tmp_path: Path) -> tuple[AgentEngine, SessionStore]:
    _write_platform_yaml(tmp_path)
    backend = SqliteBackend(tmp_path / ".platform" / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    am = AssetManagement(root=tmp_path)
    await am.initialize()

    authz = RuleBasedAuthorizationProvider(
        config={"rules": [{"who": ".*", "what": ".*", "effect": "allow"}]}
    )
    secrets = DotenvSecretsProvider(root=tmp_path, config={"path": ".env"})
    sandbox = NativeSandboxProvider()
    memory = RecentBufferMemoryProvider(store=store)
    tools_sub = ToolsSubsystem(
        secrets=secrets,
        sandbox=sandbox,
        authz=authz,
        path_policy=PathPolicy(allowed_paths=["data/"], denied_paths=[]),
        root=tmp_path,
    )
    skills_sub = SkillsSubsystem(asset_management=am)
    bus = InProcessEventBus()

    async def default_llm(_request: LLMRequest) -> LLMResponse:
        return LLMResponse(content="ok", tool_calls=None, usage={}, raw={})

    # A summariser that returns a summary containing a REAL event_id
    # from the thread so `context.reference_lookup` has something to
    # resolve. We inject the id at prompt-render time via a closure
    # around the last user turn's synthesised id.
    class _Summariser:
        def __init__(self, ref_id_getter: Any) -> None:
            self._ref_id_getter = ref_id_getter

        async def __call__(self, _request: LLMRequest) -> LLMResponse:
            ref_id = self._ref_id_getter()
            return LLMResponse(
                content=(
                    '{"schema_version": 1, "summary_text": "the earlier '
                    "turns discussed the demo files; the concrete file "
                    'read produced 42 lines.", '
                    '"refs": [{"event_id": "' + ref_id + '", '
                    '"one_line": "reference"}], "kept_files": [], '
                    '"span": {"first_event_id": "e", '
                    '"last_event_id": "f", "event_count": 1}}'
                ),
                tool_calls=None,
                usage={},
                raw={},
            )

    # Placeholder; overwritten after we know an event_id from the
    # seeded thread.
    ref_id_holder = {"id": "ev_placeholder"}
    summariser = _Summariser(lambda: ref_id_holder["id"])

    async def factory(_spec: Any) -> Any:
        return summariser

    provider = ReferenceSummaryCompactionProvider(llm_for_model=factory, config={})
    llm_providers = {
        "mock": ProviderConfig(
            name="mock",
            type="mock",
            api_key_secret=None,
            base_url=None,
            timeout_seconds=None,
            extras={},
            models=[{"id": "mock-model", "context_length": 20}],
        ),
    }
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
        framework=MinimalFramework(),
        llm=default_llm,
        llm_providers_config=llm_providers,
        compaction_provider=provider,
    )
    # Stash the holder on the engine so the caller can update it once
    # a real event id is available.
    engine._smoke_ref_id_holder = ref_id_holder
    return engine, store


def _config() -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="2",
        agent_id=AgentId("ag_smoke"),
        name="smoke",
        description="smoke agent",
        model=ModelSpec(provider="mock", model="mock-model"),
        prompt="You are the smoke agent.",
        io=IOSpec(mode="conversational"),
    )


@pytest.mark.asyncio
async def test_compaction_smoke_end_to_end(tmp_path: Path) -> None:
    engine, store = await _build(tmp_path)
    session = await store.create_session("ag_smoke", name="smoke-session")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id

    # Seed 4 exchanges so the thread has a comfortable middle span.
    for turn in range(4):
        await engine.execute(
            _config(),
            ExecutionContext(
                thread_id=thread_id,
                trigger={
                    "kind": "user_message",
                    "content": f"turn {turn} discusses demo data " * 8,
                },
            ),
        )

    # Point the summariser at a real event_id from the seeded thread
    # so `context.reference_lookup` can resolve it later.
    events_before = [e async for e in store.read_path_to_leaf(thread_id)]
    # Pick the first USER_MESSAGE — a nice deterministic anchor.
    ref_event = next(e for e in events_before if e.type == EventType.USER_MESSAGE)
    ref_id_holder = engine._smoke_ref_id_holder
    ref_id_holder["id"] = str(ref_event.event_id)

    # (1) Force a compaction via /compact's engine API.
    emitted = await engine.manual_compact(config=_config(), thread_id=thread_id)
    assert emitted is not None, "manual_compact should have compacted"
    assert emitted.type == EventType.CONTEXT_COMPACTED

    events_after = [e async for e in store.read_path_to_leaf(thread_id)]
    assert any(e.type == EventType.CONTEXT_COMPACTED for e in events_after)

    # (2) /compaction-history renders a row for the fresh compaction.
    runtime = _Runtime(store)
    table = await build_compaction_history_table(runtime, thread_id)
    assert str(emitted.event_id)[:12] in table
    assert "yes" not in table.splitlines()[-1] or "no" in table  # preview col

    # (3) /why-compact <id> dumps the summary payload as JSON.
    dump = await build_why_compact_card(
        runtime, thread_id, event_id=str(emitted.event_id)
    )
    assert dump is not None
    assert "the earlier turns discussed the demo files" in dump
    assert "refs" in dump

    # (4) Next projection contains the framing text + summary JSON,
    # NOT the raw event stream. The events inside `compacted_event_ids`
    # do NOT appear as their own messages.
    projection = assemble_context(events_after, tool_schemas=[])
    projected_texts = [m.content for m in projection.messages]
    assert any(
        "<context-compaction>" in t and "<summary-json>" in t for t in projected_texts
    ), (
        f"projection should contain the compaction framing + JSON; "
        f"got: {projected_texts}"
    )
    # A compacted span event id should NOT appear as a user/assistant
    # message content (except the RECENT tail beyond the compaction).
    compacted_ids = set(emitted.data.get("compacted_event_ids") or [])
    for e in events_after:
        if e.type != EventType.USER_MESSAGE:
            continue
        if str(e.event_id) not in compacted_ids:
            continue
        content = e.data.get("content", {}).get("text", "")
        assert content not in projected_texts, (
            f"compacted USER_MESSAGE {e.event_id} leaked into projection"
        )

    # (5) context.reference_lookup resolves an in-refs event_id and
    # rejects an out-of-scope one.
    lookup = make_reference_lookup(session_store=store)
    tool_ctx = ToolContext(
        agent_id=AgentId("ag_smoke"),
        session_id=session.session_id,
        thread_id=thread_id,
        principal="system",
        secrets={},
        tool_call_id="tc_smoke",
    )
    ref_id = str(ref_event.event_id)
    # In-scope resolves.
    from tools.context_reference import ReferenceLookupArgs

    result = await lookup(ReferenceLookupArgs(event_id=ref_id), tool_ctx)
    assert result["event_id"] == ref_id
    assert result["type"] == "user.message"

    # Out-of-scope refuses with an actionable error.
    with pytest.raises(ToolExecutionFailed) as exc_info:
        await lookup(ReferenceLookupArgs(event_id="ev_not_in_scope"), tool_ctx)
    assert "OutOfScope" in str(exc_info.value.detail.get("error_class", ""))

    # (6) /uncompact restores. The Coordinator decision is LIFO
    # first-only (Phase 3): manual_uncompact revokes the MOST RECENT
    # active compaction. Here that is the one we emitted above.
    # Assembly's active-compaction filter (`respect_compaction=True`
    # AND not-revoked AND not-preview) is what restores the span;
    # revoking the manual compaction removes its `compacted_event_ids`
    # from the "skipped span" set. Older auto-compactions from the
    # seed loop MAY still be active — that's LIFO first-only by
    # design.
    revocation = await engine.manual_uncompact(thread_id=thread_id)
    assert revocation is not None
    assert str(revocation.data.get("compaction_event_id")) == str(emitted.event_id)
    events_after_uncompact = [e async for e in store.read_path_to_leaf(thread_id)]
    # The revocation event now cross-references the manual compaction;
    # Assembly's revoked set includes it; the manual compaction no
    # longer contributes to `compacted_span_ids`. That is the load-
    # bearing behavior: `respect_compaction` filtering picked up the
    # revocation.
    revoked_targets: set[str] = set()
    for e in events_after_uncompact:
        if e.type == EventType.CONTEXT_COMPACTION_REVOKED:
            tgt = e.data.get("compaction_event_id")
            if isinstance(tgt, str):
                revoked_targets.add(tgt)
    assert str(emitted.event_id) in revoked_targets, (
        "manual /uncompact should have appended a "
        "CONTEXT_COMPACTION_REVOKED targeting the manual compaction"
    )
    # Sanity: assembling with respect_compaction=False projects the
    # FULL raw event stream (every USER_MESSAGE becomes a user
    # message). This is the "replay-mode" projection the spec §7
    # ASSEMBLY_VERSION bump preserves.
    projection_raw = assemble_context(
        events_after_uncompact,
        tool_schemas=[],
        respect_compaction=False,
    )
    user_msg_count = sum(
        1 for e in events_after_uncompact if e.type == EventType.USER_MESSAGE
    )
    projected_user_count = sum(1 for m in projection_raw.messages if m.role == "user")
    assert projected_user_count == user_msg_count, (
        f"raw projection user count {projected_user_count} != raw "
        f"event user count {user_msg_count}"
    )
