"""spec-131 Steps 8-9 — end-to-end approval-notify verification.

The spec's headline use case: a hook configured on
`kolsch.prompt.requested` should fire the moment an approval prompt is
requested, BEFORE the user answers it, so an unattended operator gets a
desktop-notification-style side effect while the prompt is still
pending. This module drives that scenario as literally as the spec
describes it — a real `hooks:` rule pointing at a sentinel-file
`touch` command, a real approval-requiring tool call
(`effect: "prompt"`, mirroring the starter bundle's `bash` default —
see spec-131's Technical Context), through a REAL `AgentEngine`
constructed via `bootstrap_runtime` (the one production construction
site — see `tests/platform_runtime/test_bootstrap_hooks_wiring.py`'s
Step 6 module docstring) with a real `InlineApprovalCallback` /
`PromptsImpl` pair (the same wiring `ChatApp.on_mount` does), not a
synthetic `Event` fed to `HookDispatcher.handle()` directly.

**Step 8 finding (historical — fixed by Step 9, kept here for
context):** this did NOT pass as originally built.
`PromptsImpl._append_ui_event` — which is what actually emits the
durable `WIRE_PROMPT_REQUESTED` UI event `InlineApprovalCallback` /
`PromptsImpl.choice()` produce — appends directly to
`SessionStore.append_event` (see
`src/cli/chat/services/prompts_impl.py`). It does NOT go through
`_Exchange._emit` (`src/engine/engine.py`), which is the ONLY
site that calls `queue.put_nowait(appended)` — the sole feed for the
queue `AgentEngine.execute_stream` iterates and hands to the Step 5
observer seam (`self._observer.observe(item)`, called only for items
pulled off that same queue). Concretely: `TOOL_CALLED` IS emitted via
`_emit` (so it DOES reach the observer/hook seam — confirmed by
`test_bootstrap_hooks_wiring.py`'s `tool.failed`-matcher coverage
elsewhere in this spec), but the approval prompt that authz's `prompt`
effect raises INSIDE that tool's dispatch — before `TOOL_RESULT`/
`TOOL_FAILED` — is authored by `PromptsImpl`, a completely different,
engine-external emit path with no queue access at all.

**Step 9 fix:** `PromptsImpl` now takes an optional `runtime:
PlatformRuntime | None` constructor arg (the same `PlatformRuntime`
`ChatApp.on_mount` already holds) and, immediately after each
`SessionStore.append_event` call inside `_append_ui_event`, forwards
the SAME `Event` to `runtime.hook_dispatcher.observe()` for both
`WIRE_PROMPT_REQUESTED` and `WIRE_PROMPT_ANSWERED` — `PlatformRuntime`
now carries `hook_dispatcher` as a first-class field (set in
`bootstrap_runtime`, alongside the identical instance injected into
`AgentEngine(observer=...)`), so CLI-layer code reaches it without
touching the engine's private `_observer`. This is a second,
independent dispatch path alongside Step 5's engine-level seam — NOT a
change to `execute_stream` itself — deliberately scoped to the
UI-event emitters that bypass the queue, per spec-131 Step 9's
"Build" section. `test_hook_fires_before_prompt_is_answered` below is
no longer `xfail`: it passes for real now, driven through the actual
`PromptsImpl`/`InlineApprovalCallback` path.

**Scope caveat on `.answered` — read before trusting "both kinds work":**
Step 9 only wired the ONE code path that actually calls
`PromptsImpl._append_ui_event`. `WIRE_PROMPT_REQUESTED` always goes
through that path (fixed, verified below). `WIRE_PROMPT_ANSWERED`
does NOT always go through it: the common, interactive case — a user
answering a mounted `ApprovalBlock` widget in a real chat session — is
appended by `EventAdapter._bridge_widget_resolution`, a widget-layer
bridge that calls `SessionStore.append_event` directly and NEVER
touches `PromptsImpl`/`_observe_hook_event` at all. That path is
UNFIXED by Step 9 — it's the same "widget-layer prompt-answered echo"
bypass Step 8's scoping pass already named as 1 of 5 known gaps,
explicitly out of this step's scope. The ONLY `.answered` path Step 9
actually fixes is the narrower one that genuinely runs through
`PromptsImpl` itself: bug-114's `_emit_auto_deny` (a workflow
approval-response-window auto-deny). `test_hook_fires_for_prompt_answered_too`
below exercises exactly that narrow path — it is NOT evidence that a
hook fires when a human interactively answers a prompt in `kolsch
chat`. Don't read "both kinds" above as "fully fixed for every real
answered scenario."
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import yaml
from pydantic import BaseModel

from assets.platform_config import default_platform_payload
from cli.chat.approval import InlineApprovalCallback
from cli.chat.services.prompts_impl import PromptsImpl
from engine import ExecutionContext, LLMRequest, LLMResponse
from platform_runtime import bootstrap_runtime
from platform_runtime.hooks import HookDispatcher
from schemas import (
    AgentId,
    EventType,
    IOSpec,
    ModelSpec,
    ResolvedAgentConfig,
    SubsystemConfig,
)
from tests.fixtures.mock_llm import MockLLM, TextResponse, ToolCallResponse
from tools import ToolContext, tool

if TYPE_CHECKING:
    from pathlib import Path

    from tools.services.prompts import PromptSource


class _PingArgs(BaseModel):
    message: str


@tool(name="ping", description="Echo a message.", args_schema=_PingArgs)
async def _ping_tool(args: _PingArgs, ctx: ToolContext) -> dict[str, Any]:
    return {"echoed": args.message}


class _MockApp:
    """Minimal `InlineApprovalCallback` app double — only needs
    `clear_awaiting_approval`, matching `test_approval_callback_refactor.py`'s
    `_MockApp`."""

    def clear_awaiting_approval(self) -> None:
        return None


def _write_platform_yaml_with_hook(root: Path, marker: Path) -> None:
    """A `platform.yaml` with the spec's headline hook rule + a `prompt`
    authz rule for the `ping` tool (standing in for `bash`'s starter-
    bundle `prompt` default — see spec-131 Technical Context). Mirrors
    `test_bootstrap_hooks_wiring.py`'s `_write_platform_yaml` helper,
    plus an explicit `agent.execute.*` allow so bootstrapping an
    exchange doesn't itself require an (unrelated) approval."""
    payload = default_platform_payload()
    payload["authorization"] = {
        "variant": "rule_based",
        "config": {
            "rules": [
                {"who": ".*", "what": "agent.execute.*", "effect": "allow"},
                {"who": ".*", "what": "tool.ping", "effect": "prompt"},
                {"who": ".*", "what": ".*", "effect": "allow"},
            ],
        },
    }
    payload["hooks"] = [
        {
            "event": "kolsch.prompt.requested",
            "matcher": "*",
            "command": f"touch {marker}",
            "timeout_seconds": 10,
        }
    ]
    (root / "platform.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")


def _config_with_tool() -> ResolvedAgentConfig:
    return ResolvedAgentConfig(
        schema_version="0.1",
        assembly_version="1",
        agent_id=AgentId("ag_test"),
        name="test-agent",
        description="A test agent.",
        model=ModelSpec(provider="mock", model="mock-model"),
        prompt="You are a helpful test agent.",
        io=IOSpec(mode="conversational"),
        subsystems={
            "tools": SubsystemConfig(variant="standard", config={"builtin": []}),
        },
    )


def _llm_callable(mock_llm: MockLLM) -> Any:
    """Adapt `MockLLM` into the callable `bootstrap_runtime(llm=...)`
    expects — same shape as `test_bootstrap_hooks_wiring.py`'s helper
    and `tests/component/test_engine.py`'s `_build_engine` closure,
    extended to translate `ToolCallResponse` into `LLMResponse.tool_calls`."""

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
        if isinstance(mock_response, ToolCallResponse):
            return LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": mock_response.tool_call_id or f"tc_{uuid4()}",
                        "name": mock_response.tool,
                        "arguments": dict(mock_response.args),
                    },
                ],
            )
        msg = f"unsupported mock response in this test helper: {mock_response!r}"
        raise RuntimeError(msg)

    return _call


async def _drive_exchange_with_pending_approval(
    root: Path,
    marker: Path,
) -> tuple[bool, bool]:
    """Boot a real engine via `bootstrap_runtime`, register a real
    `InlineApprovalCallback`/`PromptsImpl` pair as the authz prompt
    callback (matching `ChatApp.on_mount`'s wiring), fire a real `ping`
    tool call that requires approval, and answer it only AFTER checking
    whether the hook's sentinel file already exists.

    Returns `(marker_existed_before_answering, marker_exists_after)`.
    """
    _write_platform_yaml_with_hook(root, marker)

    mock_llm = MockLLM(
        script=[
            ToolCallResponse(tool="ping", args={"message": "hi"}),
            TextResponse(text="done"),
        ]
    )
    runtime = await bootstrap_runtime(root=root, llm=_llm_callable(mock_llm))
    # Confirm the real HookDispatcher (not None) came up with our rule —
    # a pure wiring sanity check before the behavioral assertion below.
    observer = runtime.engine._observer
    assert isinstance(observer, HookDispatcher)
    assert len(observer._rules) == 1
    assert observer._rules[0].event == "kolsch.prompt.requested"

    await runtime.tools_subsystem.register_for_agent(
        agent_id=AgentId("ag_test"), tools=[_ping_tool]
    )

    session = await runtime.store.create_session("ag_test", name="approval-hook-e2e")
    threads = await runtime.store.list_threads(session.session_id)
    thread_id = threads[0].thread_id

    prompts = PromptsImpl(
        session_store=runtime.store,
        session_id=session.session_id,
        thread_id=thread_id,
        bus=runtime.bus,
        # spec-131 Step 9: this is the wiring `ChatApp.on_mount` does
        # (`runtime=self.session.runtime`) that closes the Step 8 gap —
        # without it, `_append_ui_event` has no dispatcher to observe
        # through and the hook never fires for a real pending prompt.
        runtime=runtime,
    )
    callback = InlineApprovalCallback(_MockApp(), prompts)
    runtime.authz.register_prompt_callback(callback)

    ctx = ExecutionContext(
        thread_id=thread_id,
        trigger={"kind": "user_message", "content": "ping hi"},
    )

    async def _answer_once_pending_is_observed() -> bool:
        """Poll for the in-flight prompt; once found, give the
        fire-and-forget hook task a moment to run, check the marker,
        THEN answer — this is the "before/without answering" ordering
        the spec's headline scenario requires."""
        for _ in range(200):
            await asyncio.sleep(0.02)
            pid = prompts.pending_prompt_id
            if pid is not None:
                # Give the fire-and-forget hook subprocess a beat to
                # run (it's launched as a background task by
                # `HookDispatcher.handle`, independently measured at
                # ~0.0001s to schedule — see `engine.py`'s Step 5
                # comment — but the subprocess itself needs a moment).
                await asyncio.sleep(0.5)
                existed_before_answer = marker.exists()
                prompts.resolve_pending(str(pid), "y")
                return existed_before_answer
        msg = "no pending prompt observed within the polling budget"
        raise AssertionError(msg)

    existed_before_answer, events = await asyncio.wait_for(
        asyncio.gather(
            _answer_once_pending_is_observed(),
            _collect_stream(runtime, ctx),
        ),
        timeout=15.0,
    )
    assert events[-1].type == EventType.THREAD_EXCHANGE_COMPLETED

    # Give any still-in-flight hook subprocess a last bounded window.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if marker.exists():
            break
        await asyncio.sleep(0.05)

    return existed_before_answer, marker.exists()


async def _collect_stream(runtime: Any, ctx: ExecutionContext) -> list[Any]:
    return [ev async for ev in runtime.engine.execute_stream(_config_with_tool(), ctx)]


# ---------------------------------------------------------------------------
# The headline scenario, as literally described by the spec.
# ---------------------------------------------------------------------------


async def test_hook_fires_before_prompt_is_answered(tmp_path: Path) -> None:
    """The spec's headline use case: a `kolsch.prompt.requested` hook
    fires BEFORE the user answers the prompt.

    spec-131 Step 8 found this failed (the sentinel file never
    appeared) because `PromptsImpl._append_ui_event` bypasses
    `execute_stream`'s observer seam entirely — see the module
    docstring. Step 9 closed the gap by giving `PromptsImpl` a direct
    reference to the same `HookDispatcher` (via `runtime.
    hook_dispatcher`) and calling `.observe()` right after the store
    append. This test was `xfail(strict=True)` under Step 8; Step 9
    un-xfails it — it must now pass for real, not be skipped or
    xpass. If this ever regresses, this test fails loudly instead of
    silently — do not re-add an xfail marker without first
    understanding why the fix stopped working.
    """
    marker = tmp_path / f"kolsch-approval-hook-{uuid.uuid4().hex}"
    assert not marker.exists()

    existed_before_answer, _existed_after = await _drive_exchange_with_pending_approval(
        tmp_path, marker
    )

    assert existed_before_answer, (
        "expected the kolsch.prompt.requested hook's sentinel file to "
        "exist BEFORE the pending approval was answered"
    )


async def test_hook_fires_for_prompt_answered_too(tmp_path: Path) -> None:
    """`kolsch.prompt.answered` reaches the hook dispatcher — ONLY via
    the narrow `PromptsImpl`-internal path (bug-114 auto-deny), NOT via
    the common interactive-widget-answer path. See this module's
    docstring "Scope caveat on `.answered`" section before assuming
    this covers every real-world `.answered` scenario — it doesn't.

    Step 9's fix wires whatever DOES go through `_observe_hook_event`
    for both `WIRE_PROMPT_REQUESTED` and `WIRE_PROMPT_ANSWERED` — this
    test is the `.answered` counterpart to
    `test_hook_fires_before_prompt_is_answered`, replacing the old
    Step-8 `test_hook_never_fires_for_pending_prompt_documents_gap`
    (which documented that NEITHER kind ever fired via `PromptsImpl`;
    that specific gap — the `PromptsImpl`-internal one — is now closed
    for both, so the old test no longer describes real behavior and
    was removed rather than kept green by accident). The SEPARATE,
    still-open gap — `EventAdapter._bridge_widget_resolution`'s direct
    store-append for interactively-answered prompts, which never calls
    `PromptsImpl` at all — is untouched by Step 9 and is NOT what this
    test exercises or proves.

    Production `WIRE_PROMPT_ANSWERED` is normally appended by
    `EventAdapter._bridge_widget_resolution` — a widget-layer bridge
    that needs a mounted `ApprovalBlock` and is out of scope for this
    engine-less test file (see `test_approval_e2e.py` for that
    machinery). The ONE real, non-fabricated `PromptsImpl`-internal
    code path that appends `WIRE_PROMPT_ANSWERED` itself is bug-114's
    `_emit_auto_deny` (the workflow approval-response-window auto-deny
    bridge) — driven here through the real `choice()` public API with
    `approval_response_seconds` configured and a `workflow`-kind
    `source`, exactly as bug-114 exercises it in production. This is
    still "driven through the actual PromptsImpl path, not a
    fabricated Event fed to match() directly," per spec-131 Step 9's
    test requirement — it just uses the real internal path that emits
    `.answered`, rather than requiring the full widget/adapter stack.
    """
    marker = tmp_path / f"kolsch-approval-hook-answered-{uuid.uuid4().hex}"
    root = tmp_path

    payload_hooks = default_platform_payload()
    payload_hooks["hooks"] = [
        {
            "event": "kolsch.prompt.answered",
            "matcher": "*",
            "command": f"touch {marker}",
            "timeout_seconds": 10,
        }
    ]
    (root / "platform.yaml").write_text(yaml.safe_dump(payload_hooks), encoding="utf-8")

    runtime = await bootstrap_runtime(root=root, llm=_llm_callable(MockLLM(script=[])))
    observer = runtime.hook_dispatcher
    assert isinstance(observer, HookDispatcher)
    assert observer is runtime.engine._observer, (
        "runtime.hook_dispatcher must be the SAME instance injected "
        "into the engine (spec-131 Step 9's whole point) — a "
        "coincidentally-equal but distinct instance would defeat it"
    )

    session = await runtime.store.create_session("ag_test", name="answered-hook")
    threads = await runtime.store.list_threads(session.session_id)
    thread_id = threads[0].thread_id

    prompts = PromptsImpl(
        session_store=runtime.store,
        session_id=session.session_id,
        thread_id=thread_id,
        bus=runtime.bus,
        runtime=runtime,
        # bug-114: a positive window + a workflow-kind source below is
        # what makes `choice()` race an internal timer and, on
        # expiry, call `_emit_auto_deny` → `_append_ui_event(
        # WIRE_PROMPT_ANSWERED, ...)` — a real production code path,
        # not a test-only shortcut.
        approval_response_seconds=0.2,
    )
    source: PromptSource = {"kind": "workflow", "id": "run_test"}

    assert not marker.exists()
    response = await asyncio.wait_for(
        prompts.choice("Approve?", ["y", "n"], source=source),
        timeout=10.0,
    )
    assert response == "n"  # bug-114 auto-deny

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if marker.exists():
            break
        await asyncio.sleep(0.05)

    assert marker.exists(), (
        "expected the kolsch.prompt.answered hook's sentinel file to "
        "exist after the approval-window auto-deny appended "
        "WIRE_PROMPT_ANSWERED"
    )
