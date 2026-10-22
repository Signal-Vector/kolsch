"""Shared fixtures + helpers for the ``tests/bundles/kolsch/tools/`` smoke suite.

Two seams reusable across future example-tool tests:

- ``_invoke_tool_via_pilot(pilot, tool_fn, args_dict)`` — invokes a
  ``@tool``-decorated function against a Pilot-controlled
  ``ChatApp``. Validates args via the tool's Pydantic schema, wraps
  the body in ``tools.dispatch._invoke_tool`` so the spec-008
  auto-close finalizer runs, and pulls services from the app's
  ``_service_registry`` per the tool's ``requires`` list.
- ``_headless_ctx()`` — builds a ``ToolContext`` with every service
  field left as ``None``. Callers use this to prove a tool raises
  ``ToolNotAvailable`` when the platform hasn't wired the required
  services.

Bundle tools are not on the Python import path: they are loaded via
[`load_bundle_tool`][tests.fixtures.bundle_paths.load_bundle_tool],
which wraps the first-class discovery API the Tools subsystem itself
uses at registration time. The tutorial example tools come from
``CARD_EXAMPLES_TOOLS_DIR``; shipped tools from ``STARTER_TOOLS_DIR``
(both in ``tests/fixtures/bundle_paths.py``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from cli.chat.app import ChatApp
from schemas.identifiers import AgentId, SessionId, ThreadId
from tests.fixtures.bundle_paths import CARD_EXAMPLES_TOOLS_DIR, load_bundle_tool
from tools.decorator import ToolContext, get_tool_definition
from tools.dispatch import _invoke_tool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

# --------------------------------------------------------------------
# Bundle-tool loading
# --------------------------------------------------------------------


@pytest.fixture
def hello_card_tool() -> Callable[..., Awaitable[Any]]:
    """Load the ``hello_card`` example tool."""
    return load_bundle_tool(CARD_EXAMPLES_TOOLS_DIR, "hello_card.py", "hello_card")


@pytest.fixture
def download_with_progress_tool() -> Callable[..., Awaitable[Any]]:
    """Load the ``download_with_progress`` example tool."""
    return load_bundle_tool(
        CARD_EXAMPLES_TOOLS_DIR, "download_with_progress.py", "download_with_progress"
    )


@pytest.fixture
def confirm_dangerous_action_tool() -> Callable[..., Awaitable[Any]]:
    """Load the ``confirm_dangerous_action`` example tool."""
    return load_bundle_tool(
        CARD_EXAMPLES_TOOLS_DIR,
        "confirm_dangerous_action.py",
        "confirm_dangerous_action",
    )


# --------------------------------------------------------------------
# ChatApp bundle fixtures — mirror `tests/cli/chat/test_cards_impl_registered.py`
# --------------------------------------------------------------------


class _MockAuthz:
    """Minimal ``AuthorizationProvider`` stub — the smoke tests
    invoke tool bodies directly and do not exercise the authz layer.
    """

    def register_prompt_callback(
        self,
        callback: Any,
        *,
        on_rule_added: Any = None,
    ) -> None:
        _ = callback, on_rule_added

    def clear_prompt_callback(self) -> None:
        return None


class _MockEngine:
    """Non-yielding engine stub — the smoke suite never runs a turn."""

    async def execute_stream(self, config: Any, ctx: Any) -> AsyncIterator[Any]:
        _ = config, ctx
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]  # `if False` idiom: makes this an empty async generator


@dataclass
class _MockConfig:
    name: str = "TestAgent"
    agent_id: str = "ag_mock"
    model: Any = None


@dataclass
class _MockBundle:
    runtime: Any
    config: Any
    session_id: Any
    thread_id: Any
    agent_ref: str
    root: Path
    model_label: str
    is_resume: bool


@pytest.fixture
async def chat_app(tmp_path: Path) -> AsyncIterator[ChatApp]:
    """Boot a ``ChatApp`` with a real ``SessionStore`` + services wired.

    Yields the app already mounted under Pilot. Callers press keys /
    query widgets / invoke tools via ``_invoke_tool_via_pilot``.

    A real store is required so ``PromptsImpl`` and ``CardsImpl``
    can construct at ``on_mount`` — their constructors take the
    store, session id, and thread id and would raise if the store
    were a stub without ``append_event``.
    """
    from store import SessionStore, SqliteBackend

    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    store = SessionStore(root=tmp_path, backend=backend)
    session = await store.create_session("ag_mock", name="c")
    threads = await store.list_threads(session.session_id)

    from bus.in_process import InProcessEventBus

    class _R:
        def __init__(self) -> None:
            self.store = store
            self.authz = _MockAuthz()
            self.engine = _MockEngine()
            # feat-018: butler workflow tools need
            # ``WorkflowsImpl`` at ``on_mount``; the impl's constructor
            # builds a ``WorkflowRunStore(self.session.runtime.root)``
            # for cold-read paths. spec-009 example tools don't touch
            # workflows, so exposing ``root`` here is additive.
            self.root = tmp_path
            # bug-020: `CardsImpl`/`WorkflowsImpl` publish onto this
            # bus so `EventAdapter.handle` fires in the SAME session
            # (no manual store drain). Matches the production runtime
            # (`PlatformRuntime.bus` is `InProcessEventBus`).
            self.bus = InProcessEventBus()

    bundle = _MockBundle(
        runtime=_R(),
        config=_MockConfig(),
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        agent_ref="kolsch/agents/mock",
        root=tmp_path,
        model_label="mock/test",
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


# --------------------------------------------------------------------
# _invoke_tool_via_pilot — the reusable helper
# --------------------------------------------------------------------


async def _invoke_tool_via_pilot(
    pilot: Any,
    tool_fn: Callable[..., Awaitable[Any]],
    args_dict: dict[str, Any],
) -> Any:
    """Invoke a ``@tool``-decorated function against a Pilot-driven app.

    Steps:

    1. Read the tool's ``ToolDefinition`` (args schema + declared
       ``requires`` list).
    2. Validate ``args_dict`` against the schema — matches the
       dispatch pipeline's Step 1.
    3. Pull each requested service off the mounted app's
       ``_service_registry``.
    4. Build a fresh ``ToolContext`` with those service fields
       populated (workflow_id + agent metadata come from the app's
       bundle).
    5. Run the body via ``tools.dispatch._invoke_tool`` so the
       spec-008 auto-close finalizer wraps the call — cards opened
       by the tool auto-close on exit exactly as they would in the
       engine path.

    Callers that need to press keys BETWEEN invocation start and
    tool return (e.g. a Prompts.choice waiting for ``y``) should
    wrap this call in ``asyncio.create_task`` and drive the pilot
    while the task is pending.
    """
    app = pilot.app
    defn = get_tool_definition(tool_fn)
    validated_args = defn.args_schema.model_validate(args_dict)

    registry = app._service_registry
    ctx = ToolContext(
        agent_id=AgentId(app.session.config.agent_id),
        session_id=SessionId(str(app.session.session_id)),
        thread_id=ThreadId(str(app.session.thread_id)),
        principal="user",
        secrets={},
        cards=registry.cards if registry is not None else None,
        prompts=registry.prompts if registry is not None else None,
        workflows=registry.workflows if registry is not None else None,
        session=registry.session if registry is not None else None,
    )
    return await _invoke_tool(tool_fn, validated_args, ctx)


def _headless_ctx() -> ToolContext:
    """Return a ``ToolContext`` with every service field ``None``.

    Callers pass this into a tool body to prove the tool raises
    ``ToolNotAvailable`` when the platform hasn't wired the required
    services. Matches the "headless mode" path the workflow runner
    and one-shot CLI take.
    """
    return ToolContext(
        agent_id=AgentId("ag_mock"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="user",
        secrets={},
    )


# Public re-exports so tests can `from tests.bundles.kolsch.tools.conftest
# import _invoke_tool_via_pilot`. Named with the leading underscore per
# spec §Coordinator-Locked; the module boundary makes them public.
#
# bug-020: `_replay_ui_events_since` deleted. The runtime bus now
# subscribes `event_adapter.handle` on `UI_EVENT_TOPIC` from
# `ChatApp.on_mount` (via `CardsImpl` / `WorkflowsImpl` publish
# calls), so tool-driven mounts surface in the live session without
# a manual drain. Callers replaced with a bounded `pilot.pause()`
# poll on the widget query.
__all__ = [
    "_headless_ctx",
    "_invoke_tool_via_pilot",
]


# Silence "unused" warning for the asyncio import — kept as a
# discoverable seam for future tests that need to spawn background
# tasks against the pilot loop.
_ = asyncio
