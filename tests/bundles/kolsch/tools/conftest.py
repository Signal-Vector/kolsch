"""Shared fixtures + helpers for the ``bundles/kolsch/tools/`` smoke suite.

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

The suite works around the fact that ``bundles/`` is not on the
Python import path: tools are loaded via
[`discover_tools_in_file`][tools.discovery.discover_tools_in_file],
the first-class discovery API the Tools subsystem itself uses at
registration time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import uuid4

import pytest

from cli.chat.app import ChatApp
from schemas.events import EventType
from schemas.identifiers import AgentId, SessionId, ThreadId
from tools.decorator import ToolContext, get_tool_definition
from tools.discovery import discover_tools_in_file
from tools.dispatch import _invoke_tool

# --------------------------------------------------------------------
# Bundle-tool loading
# --------------------------------------------------------------------

_BUNDLE_TOOLS_DIR = (
    Path(__file__).resolve().parents[4] / "bundles" / "kolsch" / "tools"
)
"""Absolute path to ``bundles/kolsch/tools/`` at the repo root.

Resolved lazily via ``__file__`` so the path is portable across
checkout locations. Four ``.parents[]`` hops:

- ``[0]`` — ``tests/bundles/kolsch/tools/conftest.py``.
- ``[1]`` — ``tests/bundles/kolsch/tools/``.
- ``[2]`` — ``tests/bundles/kolsch/``.
- ``[3]`` — ``tests/bundles/``.
- ``[4]`` — repo root.
"""


def _load_tool(filename: str, tool_name: str) -> Callable[..., Awaitable[Any]]:
    """Import ``bundles/kolsch/tools/<filename>`` and return the tool.

    Args:
        filename: File under ``bundles/kolsch/tools/`` (e.g.
            ``"hello_card.py"``).
        tool_name: The ``@tool(name=...)`` value to select — a single
            file may declare multiple tools.

    Returns:
        The ``@tool``-decorated callable, unmodified. Its
        ``_tool_definition`` attribute carries the ``ToolDefinition``
        metadata the dispatch pipeline reads.
    """
    path = _BUNDLE_TOOLS_DIR / filename
    for fn in discover_tools_in_file(path):
        if get_tool_definition(fn).name == tool_name:
            return fn
    msg = f"tool {tool_name!r} not found in {path}"
    raise LookupError(msg)


@pytest.fixture
def hello_card_tool() -> Callable[..., Awaitable[Any]]:
    """Load the ``hello_card`` example tool."""
    return _load_tool("hello_card.py", "hello_card")


@pytest.fixture
def download_with_progress_tool() -> Callable[..., Awaitable[Any]]:
    """Load the ``download_with_progress`` example tool."""
    return _load_tool("download_with_progress.py", "download_with_progress")


@pytest.fixture
def confirm_dangerous_action_tool() -> Callable[..., Awaitable[Any]]:
    """Load the ``confirm_dangerous_action`` example tool."""
    return _load_tool(
        "confirm_dangerous_action.py", "confirm_dangerous_action",
    )


# --------------------------------------------------------------------
# ChatApp bundle fixtures — mirror `tests/cli/chat/test_cards_impl_registered.py`
# --------------------------------------------------------------------


class _FakeAuthz:
    """Minimal ``AuthorizationProvider`` stub — the smoke tests
    invoke tool bodies directly and do not exercise the authz layer.
    """

    def register_prompt_callback(
        self, callback: Any, *, on_rule_added: Any = None,
    ) -> None:
        _ = callback, on_rule_added
        return None

    def clear_prompt_callback(self) -> None:
        return None


class _FakeEngine:
    """Non-yielding engine stub — the smoke suite never runs a turn."""

    async def execute_stream(self, config: Any, ctx: Any):
        _ = config, ctx
        if False:  # pragma: no cover
            yield


@dataclass
class _FakeConfig:
    name: str = "TestAgent"
    agent_id: str = "ag_fake"
    model: Any = None


@dataclass
class _FakeBundle:
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
    session = await store.create_session("ag_fake", name="c")
    threads = await store.list_threads(session.session_id)

    class _R:
        def __init__(self) -> None:
            self.store = store
            self.authz = _FakeAuthz()
            self.engine = _FakeEngine()
            # feat-018: butler workflow tools need
            # ``WorkflowsImpl`` at ``on_mount``; the impl's constructor
            # builds a ``WorkflowRunStore(self.session.runtime.root)``
            # for cold-read paths. spec-009 example tools don't touch
            # workflows, so exposing ``root`` here is additive.
            self.root = tmp_path

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
        session=bundle,  # type: ignore[arg-type]
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


async def _replay_ui_events_since(
    pilot: Any,
    seen: set[str],
) -> None:
    """Dispatch every fresh ``UI_EVENT`` on the store to the app's adapter.

    ``CardsImpl._drain_stream_queue`` publishes to an in-process bus
    that is a no-op stub in v1 — the adapter's live subscription
    path is not wired yet (spec-008+ / bus wiring). To exercise the
    mount seam end-to-end from a tool call, we read every UI_EVENT
    off the store and dispatch it through the adapter's ``handle``
    manually.

    ``seen`` is a set of event ids the caller mutates across calls
    so repeat drains don't re-mount / re-apply events.
    """
    app = pilot.app
    store = app.session.runtime.store
    thread_id = app.session.thread_id
    async for event in store.read_events(thread_id):
        eid = str(event.event_id)
        if eid in seen:
            continue
        seen.add(eid)
        if event.type != EventType.UI_EVENT:
            continue
        await app.event_adapter.handle(event)


def _headless_ctx() -> ToolContext:
    """Return a ``ToolContext`` with every service field ``None``.

    Callers pass this into a tool body to prove the tool raises
    ``ToolNotAvailable`` when the platform hasn't wired the required
    services. Matches the "headless mode" path the workflow runner
    and one-shot CLI take.
    """
    return ToolContext(
        agent_id=AgentId("ag_fake"),
        session_id=SessionId(f"ses_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        principal="user",
        secrets={},
    )


# Public re-exports so tests can `from tests.bundles.kolsch.tools.conftest
# import _invoke_tool_via_pilot`. Named with the leading underscore per
# spec §Coordinator-Locked; the module boundary makes them public.
__all__ = [
    "_headless_ctx",
    "_invoke_tool_via_pilot",
    "_replay_ui_events_since",
]


# Silence "unused" warning for the asyncio import — kept as a
# discoverable seam for future tests that need to spawn background
# tasks against the pilot loop.
_ = asyncio
