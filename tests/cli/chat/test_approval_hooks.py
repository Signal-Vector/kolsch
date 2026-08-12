"""spec-131 Step 8 — end-to-end approval-notify verification.

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

**Finding (documented here, not swept under the rug):** this does NOT
pass. `PromptsImpl._append_ui_event` — which is what actually emits the
durable `WIRE_PROMPT_REQUESTED` UI event `InlineApprovalCallback` /
`PromptsImpl.choice()` produce — appends directly to
`SessionStore.append_event` (see
`src/cli/chat/services/prompts_impl.py:598-644`, and its own
docstring: "writes WIRE_PROMPT_REQUESTED durably from the renderer
package with no direct engine handle"). It does NOT go through
`_Exchange._emit` (`src/engine/engine.py:3177`), which is the ONLY
site that calls `queue.put_nowait(appended)` — the sole feed for the
queue `AgentEngine.execute_stream` iterates and hands to the Step 5
observer seam (`src/engine/engine.py:493`,
`self._observer.observe(item)`, called only for items pulled off that
same queue). Concretely: `TOOL_CALLED` IS emitted via `_emit` (so it
DOES reach the observer/hook seam — confirmed by
`test_bootstrap_hooks_wiring.py`'s `tool.failed`-matcher coverage
elsewhere in this spec), but the approval prompt that authz's `prompt`
effect raises INSIDE that tool's dispatch — before `TOOL_RESULT`/
`TOOL_FAILED` — is authored by `PromptsImpl`, a completely different,
engine-external emit path with no queue access at all.

This was verified empirically with a throwaway script before writing
this test (real `bootstrap_runtime`-built engine, real `ping` tool with
an `effect: "prompt"` authz rule, real `InlineApprovalCallback` +
`PromptsImpl` registered as the authz prompt callback, a spy
`EventObserver`): `WIRE_PROMPT_REQUESTED` reliably lands in the
DURABLE STORE (the approval flow itself works exactly as designed —
the widget/adapter bridge that consumes it is a live-bus subscriber,
not an `execute_stream` consumer) but is NEVER observed by the
engine-level spy — i.e., it never reaches `execute_stream`'s yielded
items at all. `HookDispatcher.handle()` (Step 4) and `match()`
(Step 2) both work correctly on `kolsch.prompt.requested` in isolation
(see `tests/platform_runtime/test_hooks.py`'s matcher-logic and
dispatcher tests) — no defect there. The gap is structural: the single
choke point Step 5 chose (`execute_stream`'s queue) simply never
receives this specific event, for the same underlying reason
`test_bootstrap_hooks_wiring.py`'s module docstring documents for
`session.opened` (`SessionStore.create_session` writes directly to the
store, bypassing the exchange entirely) — except `session.opened` was
flagged explicitly as a known, accepted, out-of-scope gap, while
`kolsch.prompt.requested` was the spec's literal headline example and
was never verified end-to-end (every existing `kolsch.prompt.requested`
test across Steps 1-7 — `tests/platform_runtime/test_hooks.py` — feeds
a *fabricated* `Event` directly to `match()` / `HookDispatcher.handle()`
by hand; none drives a real engine exchange with a real pending
prompt).

The tests below are written exactly as the spec describes the
scenario (Implementation Step 8's "Tests" bullet + the file-level
smoke-test YAML). `test_hook_fires_before_prompt_is_answered` is
expected to (and does) FAIL given the current architecture — kept as
an executable, precise regression/gap marker rather than only a prose
claim, with `pytest.mark.xfail(strict=True)` so it flips to a loud
failure (not a silent pass) the moment this gap is closed, which would
mean this xfail marker needs to be deleted rather than the test being
otherwise wrong. `test_hook_never_fires_for_pending_prompt_documents_gap`
is the same scenario asserting the CURRENT (gap) behavior directly, so
the suite has a non-xfail witness of what actually happens today, for
CI/coverage purposes.

This is a Step-8-verification-only finding (no production code is
changed by this file) — see the final report for the Decision-needed
framing handed back to the Coordinator.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "spec-131 Step 8 finding: PromptsImpl._append_ui_event appends "
        "WIRE_PROMPT_REQUESTED directly to the store, bypassing "
        "_Exchange._emit — the only site that feeds execute_stream's "
        "queue. The Step-5 observer seam therefore never sees "
        "kolsch.prompt.requested, so a hook configured on it cannot "
        "fire before (or ever, via this path) the prompt is answered. "
        "See this module's docstring for the full empirical trace. "
        "Delete this xfail (and update the docstring) if this is ever "
        "fixed — the assertions below encode the spec's literal "
        "acceptance criterion."
    ),
)
async def test_hook_fires_before_prompt_is_answered(tmp_path: Path) -> None:
    """The spec's headline use case: a `kolsch.prompt.requested` hook
    fires BEFORE the user answers the prompt.

    Currently fails: the sentinel file never appears at all (the hook
    never fires), so `existed_before_answer` is `False` — see the
    module docstring for why.
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


async def test_hook_never_fires_for_pending_prompt_documents_gap(
    tmp_path: Path,
) -> None:
    """Non-xfail witness of the CURRENT behavior (kept green): the
    sentinel file never appears, neither before nor after the prompt
    is answered, because `kolsch.prompt.requested` never reaches the
    observer seam at all through a real approval flow. This is the
    load-bearing regression marker for CI — flips to a real failure
    (not silently skipped) if some other change accidentally makes the
    hook fire, which would be a signal to reconcile this file's
    documented finding, not a bug in this test.
    """
    marker = tmp_path / f"kolsch-approval-hook-gap-{uuid.uuid4().hex}"
    assert not marker.exists()

    existed_before_answer, existed_after = await _drive_exchange_with_pending_approval(
        tmp_path, marker
    )

    assert existed_before_answer is False
    assert existed_after is False
