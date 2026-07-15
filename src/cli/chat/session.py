"""Session lifecycle helpers for the Textual chat surface.

Migrated from the legacy REPL (``src/cli/chat/_legacy.py``). The
functions here are the pure(-ish) helpers the legacy input loop
called; they compose the Textual ``ChatApp``'s startup + slash
handlers without carrying any REPL-specific state.

Migration table (per ``process/spec-textual-chat-ui.md``):

- ``_build_user_trigger`` — pure; unchanged.
- ``_resume_pending_workflow`` — unchanged.
- ``_open_new_session`` — unchanged (takes the ``ChatSessionState``).
- ``_switch_session`` — replay path calls
  ``event_adapter.handle`` for each event instead of the legacy
  ``Renderer.render`` (via the ``replay_session`` helper below).
- ``_replay_session`` — see ``replay_session`` (event-adapter-driven).
- ``_refresh_slash_ctx`` — unchanged.
- ``_parse_startup_model_override`` — unchanged.
- ``_load_model_aliases`` — unchanged.
- ``_snapshot_recent_session_ids`` — unchanged.
- ``_placeholder_session_id`` — unchanged.
- ``_format_model_label`` — unchanged.

The ``build_session`` async helper is new: ``main.py._cmd_chat``
calls it to construct the initial ``ChatApp`` inputs (runtime,
config, session, thread) so the app itself stays a thin shell.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from schemas import SessionId

if TYPE_CHECKING:
    import argparse

    from cli.chat.event_adapter import EventAdapter
    from platform_runtime import PlatformRuntime
    from schemas import ResolvedAgentConfig, ThreadId

_DEFAULT_AGENT_REF = "kolsch/agents/helper"


# ---------------------------------------------------------------------------
# Session bundle for the ChatApp
# ---------------------------------------------------------------------------


@dataclass
class ChatSessionBundle:
    """The set of runtime handles ``ChatApp`` needs.

    Constructed by :func:`build_session`; the app treats this as an
    opaque "session" input. Keeping it as a dataclass rather than a
    Pydantic model lets us stash mutable ``runtime`` /
    ``PromptCallback`` fields alongside the immutable ids.
    """

    runtime: PlatformRuntime
    config: ResolvedAgentConfig
    session_id: SessionId
    thread_id: ThreadId
    agent_ref: str
    root: Path
    model_label: str
    is_resume: bool


async def build_session(
    args: argparse.Namespace,
    *,
    runtime: PlatformRuntime | None = None,
) -> ChatSessionBundle:
    """Bootstrap the runtime, resolve the agent, mint / load a session.

    Mirrors the setup half of ``_legacy.cmd_chat`` (up through the
    session load / create block) but returns a bundle instead of
    entering an input loop.

    When ``runtime`` is supplied the caller has already bootstrapped
    one (e.g. the ``--resume`` picker in ``_cmd_chat`` needs it to
    list sessions) and we reuse it instead of paying the second
    bootstrap cost. Non-picker paths (plain ``kolsch chat``, direct
    ``--session <id>``) pass ``None`` and this function bootstraps
    fresh, matching the pre-``--resume`` behavior.

    Raises ``ValueError`` on user-visible startup failures
    (invalid ``--model`` override; ``--session`` id not loadable).
    ``main.py._cmd_chat`` catches these and prints to stderr.
    """
    from platform_runtime import bootstrap_runtime  # noqa: PLC0415

    agent_ref = args.agent_ref or _DEFAULT_AGENT_REF
    if runtime is None:
        runtime = await bootstrap_runtime(root=args.root)
    config = await runtime.build_resolved_config(agent_ref)

    model_label = _format_model_label(config.model)
    if getattr(args, "model", None):
        try:
            override = _parse_startup_model_override(
                args.model,
                runtime,
                args.root,
            )
        except Exception as exc:
            msg = f"chat: invalid --model {args.model!r}: {exc}"
            raise ValueError(msg) from exc
        config = config.model_copy(update={"model": override})
        model_label = f"{override.provider}/{override.model}"

    is_resume = bool(getattr(args, "session", None))
    if is_resume:
        try:
            session = await runtime.store.get_session(SessionId(str(args.session)))
        except Exception as exc:
            msg = f"chat: --session {args.session!r} not loadable: {exc}"
            raise ValueError(msg) from exc
    else:
        session = await runtime.store.create_session(
            str(config.agent_id),
            name=f"chat:{agent_ref}",
        )

    threads = await runtime.store.list_threads(session.session_id)
    if not threads:
        msg = f"session {session.session_id} has no threads"
        raise RuntimeError(msg)
    thread_id = threads[0].thread_id

    return ChatSessionBundle(
        runtime=runtime,
        config=config,
        session_id=session.session_id,
        thread_id=thread_id,
        agent_ref=agent_ref,
        root=args.root,
        model_label=model_label,
        is_resume=is_resume,
    )


# ---------------------------------------------------------------------------
# Ported helpers
# ---------------------------------------------------------------------------


def _build_user_trigger(text: str, force_skills: list[str]) -> dict[str, Any]:
    """Ported verbatim from `_legacy.py:548`."""
    if force_skills:
        return {
            "kind": "user_message_with_force_skill",
            "content": text,
            "force_skills": list(force_skills),
        }
    return {"kind": "user_message", "content": text}


async def _resume_pending_workflow(
    state: Any,
    *,
    stdout: TextIO,
    response_text: str,
) -> None:
    """Ported verbatim from `_legacy.py:620`."""
    from cli._workflow_chat_runner import (  # noqa: PLC0415
        execute_workflow_with_renderer,
    )
    from workflows.errors import WorkflowError  # noqa: PLC0415
    from workflows.ids import WorkflowRunId  # noqa: PLC0415
    from workflows.parser import load_workflow  # noqa: PLC0415
    from workflows.run import WorkflowRunStatus  # noqa: PLC0415
    from workflows.run_store import WorkflowRunStore  # noqa: PLC0415

    if not state.pending_workflow_run_id:
        state.expecting_user_input_response = False
        return

    runtime = state.runtime
    store = WorkflowRunStore(runtime.root)
    try:
        run = store.read(WorkflowRunId(state.pending_workflow_run_id))
    except (FileNotFoundError, ValueError) as exc:
        stdout.write(f"resume failed: {exc}\n")
        state.expecting_user_input_response = False
        state.pending_workflow_run_id = None
        return

    catalog = runtime.asset_management.catalog()
    entry = catalog.lookup(run.workflow_ref, expected_type="workflow")
    if entry is None:
        stdout.write(f"resume failed: workflow {run.workflow_ref!r} not in catalog.\n")
        state.expecting_user_input_response = False
        return
    try:
        workflow = load_workflow(Path(entry.path))
    except WorkflowError as exc:
        message = getattr(exc, "caller_message", None) or str(exc)
        stdout.write(f"resume failed: {message}\n")
        state.expecting_user_input_response = False
        return

    status = await execute_workflow_with_renderer(
        state=state,
        stdout=stdout,
        store=store,
        workflow=workflow,
        run=run,
        resume_response={"response": response_text},
    )
    state.expecting_user_input_response = status == WorkflowRunStatus.WAITING_FOR_INPUT


async def _open_new_session(
    state: Any,
    *,
    stdout: TextIO,
    agent_ref: str,
) -> None:
    """Ported from `_legacy.py:693`."""
    session = await state.runtime.store.create_session(
        str(state.config.agent_id),
        name=f"chat:{agent_ref}",
    )
    threads = await state.runtime.store.list_threads(session.session_id)
    state.session_id = session.session_id
    state.thread_id = threads[0].thread_id
    state.pending_force_skill.clear()
    stdout.write(
        f"kolsch chat • {state.config.name} • "
        f"{_format_model_label(state.config.model)} • "
        f"{session.session_id}\n\n"
    )


async def _switch_session(
    state: Any,
    *,
    target_session_id: SessionId,
    stdout: TextIO,
) -> None:
    """Ported from `_legacy.py:723`; replay via event adapter now happens outside."""
    threads = await state.runtime.store.list_threads(target_session_id)
    if not threads:
        stdout.write(
            f"/session: session {target_session_id!r} has no threads; "
            "staying in current session.\n"
        )
        return
    state.session_id = target_session_id
    state.thread_id = threads[0].thread_id
    state.pending_force_skill.clear()


async def replay_session(
    *,
    runtime: PlatformRuntime,
    thread_id: ThreadId,
    adapter: EventAdapter,
) -> None:
    """Iterate a thread's events and dispatch each through ``adapter.handle``.

    Replacement for ``_legacy.py::_replay_session`` — the same
    read path (``runtime.store.read_path_to_leaf``) feeds the new
    adapter instead of the legacy ``Renderer``.
    """
    async for event in runtime.store.read_path_to_leaf(thread_id):
        await adapter.handle(event)


def _refresh_slash_ctx(prior: Any, state: Any) -> Any:
    """Ported from `_legacy.py:778`."""
    from dataclasses import replace  # noqa: PLC0415

    return replace(prior, agent_config=state.config)


def _parse_startup_model_override(
    raw: str,
    runtime: PlatformRuntime,
    root: Path,
) -> Any:
    """Ported from `_legacy.py:794`."""
    from assets.assembler import _normalize_model_value  # noqa: PLC0415
    from schemas import ModelSpec  # noqa: PLC0415

    _ = runtime
    aliases = _load_model_aliases(root)
    normalized = _normalize_model_value(raw, aliases)
    if not isinstance(normalized, dict):
        msg = f"unexpected normalization shape: {type(normalized).__name__}"
        raise TypeError(msg)
    return ModelSpec(**normalized)


def _load_model_aliases(root: Path) -> dict[str, str]:
    """Ported from `_legacy.py:816`."""
    from kolsch._yamlio import YAMLError, load  # noqa: PLC0415

    yaml_path = root / "platform.yaml"
    if not yaml_path.is_file():
        return {}
    try:
        payload = load(yaml_path.read_text(encoding="utf-8")) or {}
    except YAMLError:
        return {}
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("model_aliases") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


_RECENT_SESSIONS_LIMIT = 10


async def _snapshot_recent_session_ids(
    runtime: PlatformRuntime,
) -> list[tuple[str, str]]:
    """Ported from `_legacy.py:846`."""
    sessions = await runtime.store.list_sessions(limit=_RECENT_SESSIONS_LIMIT)
    return [
        (
            str(s.session_id),
            (
                f"{s.name or '(unnamed)'} • "
                f"{s.created_at.strftime('%Y-%m-%d %H:%M') if s.created_at else '?'}"
            ),
        )
        for s in sessions
    ]


def _placeholder_session_id() -> SessionId:
    """Ported from `_legacy.py:874`."""
    from uuid import uuid4  # noqa: PLC0415

    return SessionId(f"ses_{uuid4()}")


def _format_model_label(model: object) -> str:
    """Ported from `_legacy.py:959`."""
    provider = getattr(model, "provider", None)
    model_id = getattr(model, "model", None)
    if isinstance(provider, str) and isinstance(model_id, str):
        return f"{provider}/{model_id}"
    if isinstance(model, str):
        return model
    return repr(model)


__all__ = [
    "ChatSessionBundle",
    "_build_user_trigger",
    "_format_model_label",
    "_load_model_aliases",
    "_open_new_session",
    "_parse_startup_model_override",
    "_placeholder_session_id",
    "_refresh_slash_ctx",
    "_resume_pending_workflow",
    "_snapshot_recent_session_ids",
    "_switch_session",
    "build_session",
    "replay_session",
]
