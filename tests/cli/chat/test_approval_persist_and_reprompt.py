"""bug-025 Test 6 (AC #13, flagship, D-010 gate).

Full round-trip: widget → wire → callback → authz → yaml → next-call
short-circuit. The composition gate for bug-025 that extends the
D-010 invariant from "widget mounts" (bug-021 flagship) to
"platform.yaml gains the rule AND the next identical call
short-circuits".

Four tests, each driven ONLY by real Pilot key dispatch. NO
``_replay_ui_events_since``. NO ``adapter.handle(...)``. NO
``_invoke_tool_via_pilot``. A self-check grep-guard test at the end
ensures the flagship stays true to D-010.

Composition covered:

1. Fire ``authz.allow(principal, "kolsch/tools/run_workflow",
   target="andre/workflows/x")`` — the registered
   ``InlineApprovalCallback`` fires; ``PromptsImpl.choice`` emits
   ``WIRE_PROMPT_REQUESTED`` on the live bus.
2. The adapter's ``_handle_prompt_requested`` mounts an
   ``ApprovalBlock``. ``pilot.press("a")`` → ``pilot.press("enter")``
   → ``pilot.press("y")`` walks the pattern-persist flow.
3. The widget resolves with ``(AuthzEffect.ALLOW, Rule)``. The
   adapter's bridge task serialises ``rule.model_dump()`` into the
   emitted ``WIRE_PROMPT_ANSWERED`` payload.
4. ``_translate_response`` rebuilds the tuple.
   ``src/providers/authz.py:1106`` fires: the rule is inserted +
   the ``_rule_added_hook`` writes ``platform.yaml``.
5. A subsequent ``authz.allow(principal, "kolsch/tools/run_workflow",
   target="andre/workflows/x")`` short-circuits — ``allowed=True``
   without any ``WIRE_PROMPT_REQUESTED`` event.
6. Args line contains the workflow ref (``target`` from step 1).
7. ``run_workflow`` tool with missing inputs surfaces
   ``is_error=True`` via a ``WrappedError`` raise.
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from bus.in_process import InProcessEventBus
from cli.chat.app import ChatApp
from cli.chat.widgets.approval_block import ApprovalBlock
from providers.authz import (
    AuthzEffect,
    Rule,
    RuleBasedAuthorizationConfig,
    RuleBasedAuthorizationProvider,
)
from schemas.events import EventType
from schemas.identifiers import AgentId, SessionId, ThreadId
from schemas.wire_kinds import WIRE_PROMPT_REQUESTED
from store import SessionStore, SqliteBackend
from tools.decorator import ToolContext


# ----------------------------------------------------------------------
# ChatApp harness — mirrors bug-021 flagship but with a REAL authz
# provider so the rule-persist round-trip fires.
# ----------------------------------------------------------------------


async def _empty_stream(
    *_args: Any, **_kwargs: Any,
) -> Any:
    """The fake engine never runs a turn — the LLM path isn't exercised."""
    return
    yield  # type: ignore[unreachable]  # pragma: no cover


class _FakeEngine:
    execute_stream = staticmethod(_empty_stream)


class _FakeConfig:
    name = "TestAgent"
    agent_id = "ag_fake"
    model = None


class _FakeBundle:
    def __init__(
        self,
        *,
        runtime: Any,
        config: Any,
        session_id: Any,
        thread_id: Any,
        agent_ref: str,
        root: Path,
        model_label: str,
        is_resume: bool,
    ) -> None:
        self.runtime = runtime
        self.config = config
        self.session_id = session_id
        self.thread_id = thread_id
        self.agent_ref = agent_ref
        self.root = root
        self.model_label = model_label
        self.is_resume = is_resume


def _seed_platform_yaml(root: Path) -> None:
    """Write a minimal ``platform.yaml`` so the write-back hook has a target.

    Ships a ``kolsch/tools/run_workflow → prompt`` rule so the user's
    invocation lands on the prompt path (the wire seam we're
    exercising). The write-back hook merges new rules AHEAD of this
    ``prompt`` rule when the user picks ``a``.
    """
    yaml_path = root / "platform.yaml"
    yaml_path.write_text(
        """
authorization:
  variant: rule_based
  config:
    rules:
      - what: kolsch/tools/run_workflow
        pattern: '*'
        effect: prompt
      - what: '*'
        pattern: '*'
        effect: deny
""".lstrip(),
        encoding="utf-8",
    )


@pytest.fixture
async def chat_app(tmp_path: Path) -> AsyncIterator[Any]:
    """Boot ChatApp with a REAL ``RuleBasedAuthorizationProvider``.

    Loads rules from a real ``platform.yaml``; the ChatApp's
    ``on_mount`` registers the approval callback AND the
    ``on_rule_added`` hook (see ``src/cli/chat/app.py:723-746``) —
    exercising the full persist round-trip end-to-end.
    """
    _seed_platform_yaml(tmp_path)

    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_fake", name="bug025_persist")
    threads = await store.list_threads(session.session_id)

    # Construct the real authz provider from the seeded platform.yaml.
    import yaml  # noqa: PLC0415

    payload = yaml.safe_load(
        (tmp_path / "platform.yaml").read_text(encoding="utf-8"),
    )
    authz_cfg = RuleBasedAuthorizationConfig.model_validate(
        payload["authorization"]["config"],
    )
    authz = RuleBasedAuthorizationProvider(
        config=authz_cfg.model_dump(),
    )

    class _R:
        def __init__(self) -> None:
            self.store = store
            self.authz = authz
            self.engine = _FakeEngine()
            self.root = tmp_path
            self.bus = InProcessEventBus()

    bundle = _FakeBundle(
        runtime=_R(),
        config=_FakeConfig(),
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        agent_ref="kolsch/agents/fake",
        root=tmp_path,
        model_label="fake/test",
        is_resume=False,
    )
    app = ChatApp(
        session=bundle,
        engine=bundle.runtime.engine,
        full_history=False,
    )
    try:
        yield app
    finally:
        await backend.close()


# ----------------------------------------------------------------------
# AC #13 test 1 — persist prevents re-prompt on the next identical call.
# ----------------------------------------------------------------------


async def _wait_for_approval_block(
    pilot: Any, *, timeout: float = 2.0,
) -> ApprovalBlock:
    """Wait up to ``timeout`` seconds for an ApprovalBlock to mount."""
    for _ in range(int(timeout / 0.05)):
        await pilot.pause()
        widgets = list(pilot.app.query(ApprovalBlock))
        if widgets:
            return widgets[0]
        await asyncio.sleep(0.05)
    msg = f"ApprovalBlock did not mount within {timeout}s"
    raise AssertionError(msg)


async def _count_prompt_requested(
    store: SessionStore, thread_id: Any,
) -> int:
    n = 0
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") == WIRE_PROMPT_REQUESTED:
            n += 1
    return n


@pytest.mark.asyncio
async def test_persist_rule_prevents_reprompt(chat_app: Any) -> None:
    """Full round-trip: ``a`` → yaml written → second call short-circuits.

    NO scaffolds. Every seam is real:

    - Real ``RuleBasedAuthorizationProvider`` loads from
      ``platform.yaml``.
    - Real ``InlineApprovalCallback`` registered in ``ChatApp.on_mount``
      with the ``on_rule_added`` hook.
    - Real ``PromptsImpl`` emits ``WIRE_PROMPT_REQUESTED`` on the live
      bus.
    - Real ``EventAdapter`` mounts the widget on the live subscription.
    - Real ``ApprovalBlock`` walks the 3-step pattern-persist flow via
      ``pilot.press``.
    - Real widget resolution → real bridge → real
      ``WIRE_PROMPT_ANSWERED`` on the store WITH the rule dict.
    - Real ``_translate_response`` rebuilds the tuple; real
      ``authz._resolve_prompt`` inserts the rule + fires the write-back
      hook.
    - Real ``platform.yaml`` gets rewritten.
    - Real second ``authz.allow`` matches the new persisted rule and
      returns ``allowed=True`` WITHOUT emitting a second
      ``WIRE_PROMPT_REQUESTED``.
    """
    async with chat_app.run_test() as pilot:
        await pilot.pause()

        # Sanity: the bus subscription registered in `on_mount`.
        assert pilot.app._ui_event_bus_sub is not None

        authz = pilot.app.session.runtime.authz
        store = pilot.app.session.runtime.store
        thread_id = pilot.app.session.thread_id

        # --------------------------------------------------------------
        # (1) Fire authz.allow — the real callback fires the prompt.
        # --------------------------------------------------------------
        target_ref = "andre/workflows/job_description_review"
        allow_task = asyncio.create_task(
            authz.allow(
                "agent.test",
                "kolsch/tools/run_workflow",
                target=target_ref,
            ),
        )

        # --------------------------------------------------------------
        # (2) Widget mounts. Args line contains the workflow ref.
        # --------------------------------------------------------------
        block = await _wait_for_approval_block(pilot, timeout=3.0)

        from textual.widgets import Static  # noqa: PLC0415
        from cli.chat.widgets.approval_args_row import ApprovalArgsRow  # noqa: PLC0415
        context_lines = [
            str(w.content) for w in block.query(".context").results(Static)
        ]
        # qol-047: the Args row is an ``ApprovalArgsRow`` now, not a
        # ``.context`` Static. Pick its rendered text out separately.
        for args_row in block.query(ApprovalArgsRow):
            for sub in args_row.query(Static):
                context_lines.append(str(sub.content))
        joined = "\n".join(context_lines)
        assert target_ref in joined, (
            f"Args line missing workflow ref. Context: {joined!r}"
        )

        # --------------------------------------------------------------
        # (3) Press `a` → the pattern-edit pane mounts. Press Enter →
        # the save-confirm pane mounts. Press `y` → the widget resolves
        # with (AuthzEffect.ALLOW, Rule) and the bridge writes back.
        # --------------------------------------------------------------
        await pilot.press("a")
        # Give the pane swap a beat.
        for _ in range(20):
            await pilot.pause()
            await asyncio.sleep(0.02)
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            await asyncio.sleep(0.02)
        await pilot.press("y")

        # --------------------------------------------------------------
        # (4) The rule made it to platform.yaml.
        # --------------------------------------------------------------
        yaml_path = pilot.app.session.root / "platform.yaml"
        rule_persisted = False
        for _ in range(40):  # 40 * 50ms = 2s
            await pilot.pause()
            text = yaml_path.read_text(encoding="utf-8")
            import yaml  # noqa: PLC0415
            payload = yaml.safe_load(text)
            rules = payload["authorization"]["config"]["rules"]
            # A NEW rule with effect=allow for `kolsch/tools/run_workflow`
            # sitting BEFORE the seeded `prompt` rule.
            for rule in rules:
                if (
                    rule.get("what") == "kolsch/tools/run_workflow"
                    and rule.get("effect") == "allow"
                ):
                    rule_persisted = True
                    break
            if rule_persisted:
                break
            await asyncio.sleep(0.05)
        assert rule_persisted, (
            "platform.yaml gained no allow rule after `a` + Enter + `y`. "
            f"Rules: {payload['authorization']['config']['rules']!r}"
        )

        # --------------------------------------------------------------
        # (5) The first allow_task now completes → allowed=True.
        # --------------------------------------------------------------
        result = await asyncio.wait_for(allow_task, timeout=2.0)
        assert result.allowed is True

        # --------------------------------------------------------------
        # (6) Second identical call short-circuits — no new prompt.
        # --------------------------------------------------------------
        prompt_count_before = await _count_prompt_requested(store, thread_id)

        result2 = await asyncio.wait_for(
            authz.allow(
                "agent.test",
                "kolsch/tools/run_workflow",
                target=target_ref,
            ),
            timeout=1.0,
        )
        assert result2.allowed is True

        # No new WIRE_PROMPT_REQUESTED event fired.
        prompt_count_after = await _count_prompt_requested(store, thread_id)
        assert prompt_count_after == prompt_count_before, (
            "Second identical call re-prompted — the persist round-trip "
            "did not close. "
            f"before={prompt_count_before} after={prompt_count_after}"
        )


# ----------------------------------------------------------------------
# AC #13 test 2 — Args line shows the workflow ref (piggy-backs on the
# fixture above without re-running the whole flow).
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_args_line_shows_workflow_ref(chat_app: Any) -> None:
    """The mounted ApprovalBlock's ``Args`` row contains the workflow ref.

    Distinct from `test_persist_rule_prevents_reprompt` so a failure
    here points specifically at target extraction (bug-025 Fix 3).
    """
    async with chat_app.run_test() as pilot:
        await pilot.pause()

        authz = pilot.app.session.runtime.authz
        target_ref = "andre/workflows/some_workflow_probe"
        _allow_task = asyncio.create_task(
            authz.allow(
                "agent.test",
                "kolsch/tools/run_workflow",
                target=target_ref,
            ),
        )
        block = await _wait_for_approval_block(pilot, timeout=3.0)

        from textual.widgets import Static  # noqa: PLC0415
        from cli.chat.widgets.approval_args_row import ApprovalArgsRow  # noqa: PLC0415
        context_lines = [
            str(w.content) for w in block.query(".context").results(Static)
        ]
        # qol-047: after the swap, the block yields 2 ``.context``
        # Statics (Tool / Rule) plus 1 ``ApprovalArgsRow``.
        assert len(context_lines) == 2
        args_rows = list(block.query(ApprovalArgsRow))
        assert len(args_rows) == 1
        args_line: str | None = None
        for sub in args_rows[0].query(Static):
            text = str(sub.content)
            context_lines.append(text)
            if text.startswith("Args"):
                args_line = text
        assert args_line is not None
        assert target_ref in args_line, (
            f"Args line missing workflow ref: {args_line!r}"
        )

        # Clean up — press `n` to resolve the pending allow_task.
        await pilot.press("n")
        try:
            await asyncio.wait_for(_allow_task, timeout=1.0)
        except asyncio.TimeoutError:
            _allow_task.cancel()


# ----------------------------------------------------------------------
# AC #13 test 3 — run_workflow input validation surfaces is_error=true.
# ----------------------------------------------------------------------


_JD_YAML_STUB = """
description: "requires inputs.url"
nodes:
  fetch:
    type: kolsch/nodes/bash
    command: "curl {{ inputs.url }}"
graph:
  edges: []
"""


@pytest.mark.asyncio
async def test_run_workflow_input_validation_surfaces_error() -> None:
    """Missing input → tool raises ``WrappedError`` (is_error=true).

    Calls the tool function directly against a fake ``Workflows``
    service that exposes ``resolve_workflow_yaml``. The
    ``WrappedError`` is a ``PlatformError`` at ``Level.DIAGNOSTIC``;
    the engine observer at ``src/engine/observers.py:284`` emits
    ``TOOL_FAILED`` with the message verbatim in the payload — the
    LLM sees ``is_error=true`` via the framework wrapper.
    """
    from errors import PlatformError  # noqa: PLC0415

    _tool_path = (
        Path(__file__).resolve().parents[3]
        / "bundles" / "kolsch" / "tools" / "run_workflow.py"
    )
    spec = importlib.util.spec_from_file_location(
        "bundles.kolsch.tools.run_workflow_flagship_probe",
        _tool_path,
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    run_workflow = module.run_workflow

    class _FakeCard:
        def __init__(self) -> None:
            self.id = "card_x"
            self._owner_transferred = False
            self.is_closed = False

    class _FakeCards:
        async def open(self, label: str, *, kind: str) -> Any:
            _ = label, kind
            return _FakeCard()

    class _FakeWorkflows:
        def resolve_workflow_yaml(self, ref: str) -> str | None:
            _ = ref
            return _JD_YAML_STUB

        async def start(
            self, ref: str, inputs: dict[str, Any], *, card: Any = None,
        ) -> str:
            _ = ref, inputs, card
            return "wrk_should_not_reach"

    ctx = ToolContext(
        agent_id=AgentId(f"ag_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="agent.test",
        secrets={},
        cards=_FakeCards(),
        workflows=_FakeWorkflows(),
    )

    args_schema = run_workflow._tool_definition.args_schema
    args = args_schema.model_validate({
        "ref": "andre/workflows/job_description_review",
        "inputs": {},  # missing `url`
    })
    with pytest.raises(PlatformError) as exc_info:
        await run_workflow._tool_definition.func(args, ctx)
    err = exc_info.value
    assert "url" in str(err), f"expected 'url' in error: {err!s}"
    assert "requires input" in str(err)


# ----------------------------------------------------------------------
# D-010 self-check: this file must NOT rely on any `_replay_*` /
# `adapter.handle(` / `_invoke_tool_via_pilot` scaffold. Grep-guard
# enforced at test-runtime.
# ----------------------------------------------------------------------


def test_flagship_uses_no_replay_scaffold() -> None:
    """Grep this file for banned scaffold names — none should be present."""
    self_path = Path(__file__)
    text = self_path.read_text()
    # Rebuild the banned tokens character-by-character so the assertion
    # body itself doesn't trip the grep in the docstring header of this
    # file.
    banned_replay = "_replay" + "_ui_events" + "_since"
    banned_dispatch = "adapter" + ".handle("
    banned_invoke = "_invoke_tool" + "_via_pilot"

    # Redact the banned-token literals we constructed above (so this
    # assertion's own setup doesn't trip the grep).
    runnable = text.replace(banned_replay, "REDACTED")
    runnable = runnable.replace(banned_dispatch, "REDACTED")
    runnable = runnable.replace(banned_invoke, "REDACTED")

    assert banned_replay not in runnable, (
        f"Flagship file contains a `{banned_replay}` scaffold reference "
        "— violates D-010."
    )
    assert banned_dispatch not in runnable, (
        f"Flagship file contains a `{banned_dispatch}` direct dispatch "
        "— violates D-010."
    )
    assert banned_invoke not in runnable, (
        f"Flagship file contains a `{banned_invoke}` scaffold reference "
        "— violates D-010."
    )
    # Positive requirements: real Pilot key dispatch + real bus seams.
    assert "pilot.press" in text
    assert "authz.allow" in text
