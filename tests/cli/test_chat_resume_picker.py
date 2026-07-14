"""feat-057 — ``kolsch chat --resume`` interactive session picker.

Unit tests for the resume-picker wiring in :func:`cli.main._cmd_chat`.
The interactive :func:`~cli.session_picker._default_chooser`
lives behind an injectable seam; these tests drive the flow with a
fake chooser so no real TTY is touched. The five scenarios cover the
acceptance criteria:

- ``selects_and_resumes`` — chooser returns an id, ``args.session`` is
  stamped, the launch path fires (AC #1 / #3).
- ``cancel_no_launch`` — chooser returns ``None``, no launch (AC #4).
- ``empty_list`` — no sessions, message + exit 0, no chooser call
  (AC #5).
- ``conflict`` — both flags set, stderr message + non-zero exit,
  no chooser call (AC #6).
- ``ordering`` — rows passed to the chooser are recency-sorted
  identically to the shared helper (AC #9).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

import importlib

from cli.main import _cmd_chat

# `import cli.main as cli_main` would resolve `cli.main` to the
# ``main()`` function that ``cli/__init__.py`` re-exports (name
# collision on the ``main`` attribute of the ``cli`` package). Use
# importlib to get the actual submodule for monkeypatch targets.
cli_main = importlib.import_module("cli.main")

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from schemas.entities import Session


# ---------------------------------------------------------------------------
# Fixture — minimal on-disk workspace with one agent.
# ---------------------------------------------------------------------------


def _write_workspace(tmp_path: Path) -> Path:
    """Lay down platform.yaml + a bundle with one mock-provider agent.

    Mirrors the minimal fixture used by ``tests/component/test_cli.py``
    (``_make_platform_root``) but simplified: the picker only needs
    ``list_sessions`` + ``create_session`` to work, and the mock
    provider keeps startup cheap.
    """
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    (tmp_path / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "secrets": {"variant": "dotenv", "config": {"path": ".env"}},
                "sandbox": {"variant": "native", "config": {}},
                "authorization": {
                    "variant": "rule_based",
                    "config": {
                        "rules": [
                            {"who": ".*", "what": ".*", "effect": "allow"},
                        ],
                    },
                },
                "storage": {
                    "entity_backend": "sqlite",
                    "entity_path": ".platform/entities.db",
                },
                "event_bus": {"backend": "in_process"},
                "file_access": {
                    "allowed_paths": ["data/", "bundles/"],
                    "denied_paths": [".env"],
                },
                "defaults": {},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("", encoding="utf-8")

    bundle = tmp_path / "bundles" / "acme"
    (bundle / "agents").mkdir(parents=True, exist_ok=True)
    (bundle / "bundle.yaml").write_text(
        yaml.safe_dump({"name": "acme", "version": "0.1", "description": "demo"}),
        encoding="utf-8",
    )
    (bundle / "agents" / "demo.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "assembly_version": "1",
                "agent_id": "ag_demo",
                "name": "demo",
                "description": "Demo agent.",
                "model": {"provider": "mock", "model": "mock-model"},
                "prompt": "You are a demo agent.",
                "io": {"mode": "conversational"},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def platform_root(tmp_path: Path) -> Path:
    return _write_workspace(tmp_path)


# ---------------------------------------------------------------------------
# Helpers — chat-args namespace + spies.
# ---------------------------------------------------------------------------


def _chat_args(
    *,
    root: Path,
    resume: bool = False,
    session: str | None = None,
) -> argparse.Namespace:
    """Build a chat-command Namespace with just the fields _cmd_chat reads."""
    return argparse.Namespace(
        root=root,
        agent_ref="acme/agents/demo",
        session=session,
        model=None,
        full_history=False,
        resume=resume,
    )


class _NeverCalledChooser:
    """Chooser that raises if invoked — for negative-path assertions."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, rows: list[Any]) -> None:  # noqa: ARG002
        self.called = True
        msg = "chooser should not be invoked in this scenario"
        raise AssertionError(msg)


class _RecordingChooser:
    """Chooser that records the rows it was passed and returns ``result``.

    ``result`` may be a :class:`~schemas.SessionId`-shaped string
    (select), ``None`` (cancel), or a callable that computes the return
    value from the rows list at call time.
    """

    def __init__(
        self,
        result: str | None | Callable[[list[Any]], str | None],
    ) -> None:
        self._result = result
        self.captured_rows: list[Any] | None = None
        self.called = False

    async def __call__(self, rows: list[Any]) -> Any:
        self.called = True
        self.captured_rows = list(rows)
        from schemas import SessionId  # noqa: PLC0415

        raw = self._result(rows) if callable(self._result) else self._result
        if raw is None:
            return None
        return SessionId(raw)


# ---------------------------------------------------------------------------
# ChatApp / build_session monkeypatches shared by launch-path tests.
# ---------------------------------------------------------------------------


class _NeverLaunched:
    """Spy that flips when either ``build_session`` or ``ChatApp`` runs.

    Cancel / empty / conflict paths must never touch either.
    """

    def __init__(self) -> None:
        self.build_session_called = False
        self.chat_app_constructed = False


def _install_launch_spy(monkeypatch: pytest.MonkeyPatch) -> _NeverLaunched:
    """Patch ``build_session`` + ``ChatApp`` so no real launch happens.

    Also stubs :func:`register_chat_slash_commands` so invoking
    ``_cmd_chat`` doesn't mutate the process-global ``COMMAND_REGISTRY``
    with ``/export`` — that pollutes ``test_chat_slash`` when both
    files run in the same session.
    """
    spy = _NeverLaunched()

    async def _spy_build_session(
        args: argparse.Namespace,
        *,
        runtime: Any = None,
    ) -> Any:
        # Record what args.session was resolved to; return a stub the
        # ChatApp constructor spy will accept.
        spy.build_session_called = True
        spy.args_seen = args
        spy.runtime_seen = runtime
        return _StubSessionBundle(session_id=str(args.session or ""))

    class _StubChatApp:
        def __init__(self, *, session: Any, engine: Any, full_history: bool) -> None:  # noqa: ARG002
            spy.chat_app_constructed = True
            self.return_code = 0

        async def run_async(self, **_: Any) -> None:
            return None

    monkeypatch.setattr("cli.chat.session.build_session", _spy_build_session)
    monkeypatch.setattr("cli.chat.app.ChatApp", _StubChatApp)
    monkeypatch.setattr(
        "cli.chat.app.register_chat_slash_commands",
        lambda: None,
    )
    return spy


class _StubSessionBundle:
    """Minimal object mirroring :class:`ChatSessionBundle` fields the spy touches."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.runtime = type("_R", (), {"engine": None})()


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


async def test_selects_and_resumes(
    platform_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chooser returns the 2nd session's id; ``args.session`` is stamped
    and the launch path fires (AC #1 / #3).
    """
    from platform_runtime import bootstrap_runtime  # noqa: PLC0415

    # Seed two sessions so the picker has real rows.
    runtime = await bootstrap_runtime(root=platform_root)
    s1 = await runtime.store.create_session("ag_demo", name="chat:one")
    s2 = await runtime.store.create_session("ag_demo", name="chat:two")

    picked_id = str(s2.session_id)
    chooser = _RecordingChooser(result=picked_id)
    monkeypatch.setattr(
        "cli.session_picker._default_chooser",
        chooser,
    )
    spy = _install_launch_spy(monkeypatch)

    args = _chat_args(root=platform_root, resume=True)
    rc = await _cmd_chat(args)

    assert rc == 0
    assert chooser.called
    assert spy.build_session_called
    assert spy.chat_app_constructed
    # The stamped session flows into build_session verbatim; that is
    # what triggers the `is_resume` branch in the real build_session.
    assert args.session == picked_id
    assert spy.args_seen.session == picked_id
    # And both sessions we created appear in the picker's rows.
    row_ids = {str(s.session_id) for s, _ in (chooser.captured_rows or [])}
    assert str(s1.session_id) in row_ids
    assert picked_id in row_ids


async def test_cancel_no_launch(
    platform_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chooser returns ``None``: ``_cmd_chat`` returns 0 and nothing launches (AC #4)."""
    from platform_runtime import bootstrap_runtime  # noqa: PLC0415

    runtime = await bootstrap_runtime(root=platform_root)
    await runtime.store.create_session("ag_demo", name="chat:one")

    chooser = _RecordingChooser(result=None)
    monkeypatch.setattr(
        "cli.session_picker._default_chooser",
        chooser,
    )
    spy = _install_launch_spy(monkeypatch)

    args = _chat_args(root=platform_root, resume=True)
    rc = await _cmd_chat(args)

    assert rc == 0
    assert chooser.called
    assert not spy.build_session_called
    assert not spy.chat_app_constructed
    # Session id was NOT stamped — cancel means we never resolved one.
    assert args.session is None


async def test_empty_list(
    platform_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty session store: message + return 0 + chooser never called (AC #5)."""
    chooser = _NeverCalledChooser()
    monkeypatch.setattr(
        "cli.session_picker._default_chooser",
        chooser,
    )
    spy = _install_launch_spy(monkeypatch)

    args = _chat_args(root=platform_root, resume=True)
    rc = await _cmd_chat(args)

    assert rc == 0
    assert not chooser.called
    assert not spy.build_session_called
    assert not spy.chat_app_constructed
    out = capsys.readouterr().out
    assert "No sessions to resume" in out


async def test_conflict(
    platform_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--resume`` + ``--session`` conflict: non-zero exit, stderr message,
    no launch, chooser never called (AC #6).
    """
    chooser = _NeverCalledChooser()
    monkeypatch.setattr(
        "cli.session_picker._default_chooser",
        chooser,
    )
    spy = _install_launch_spy(monkeypatch)

    args = _chat_args(
        root=platform_root,
        resume=True,
        session="ses_placeholder",
    )
    rc = await _cmd_chat(args)

    assert rc != 0
    assert not chooser.called
    assert not spy.build_session_called
    assert not spy.chat_app_constructed
    err = capsys.readouterr().err
    assert "--resume" in err
    assert "--session" in err


async def test_ordering(
    platform_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Picker rows are ordered identically to the shared recency helper (AC #9).

    Backdates each session's last-event to force a known ordering, then
    asserts the picker's captured rows match the shared helper's output
    row-for-row.
    """
    from platform_runtime import bootstrap_runtime  # noqa: PLC0415

    runtime = await bootstrap_runtime(root=platform_root)
    # Create three sessions.
    _ = await runtime.store.create_session("ag_demo", name="chat:oldest")
    _ = await runtime.store.create_session("ag_demo", name="chat:middle")
    _ = await runtime.store.create_session("ag_demo", name="chat:newest")

    # Force distinct last-activity by monkeypatching
    # _compute_last_event_at to return a deterministic timestamp keyed
    # off the session name. Newest name sorts last alphabetically —
    # we assign it the largest datetime so recency-sort surfaces it
    # first.
    from datetime import UTC, datetime

    def _fake_last(session_name: str) -> datetime:
        rank = {"chat:oldest": 1, "chat:middle": 2, "chat:newest": 3}[session_name]
        return datetime(2026, 1, rank, 12, 0, 0, tzinfo=UTC)

    async def _stubbed_compute(
        rt: Any,  # noqa: ARG001
        session_id: Any,
    ) -> datetime:
        # Look up the name by scanning the runtime store.
        sess = await runtime.store.get_session(session_id)
        return _fake_last(sess.name)

    monkeypatch.setattr(
        cli_main,
        "_compute_last_event_at",
        _stubbed_compute,
    )

    picked_ids: list[str] = []

    async def _capture(rows: list[Any]) -> Any:
        picked_ids.extend(str(s.session_id) for s, _ in rows)
        return None  # cancel — we only care about the row ordering.

    monkeypatch.setattr(
        "cli.session_picker._default_chooser",
        _capture,
    )
    _install_launch_spy(monkeypatch)

    args = _chat_args(root=platform_root, resume=True)
    rc = await _cmd_chat(args)
    assert rc == 0  # cancel path

    # Independently, call the shared helper the same way and expect the
    # same ordering.
    helper_rows = await cli_main._recent_sessions_decorated(runtime, limit=20)
    helper_ids = [str(s.session_id) for s, _ in helper_rows]

    assert picked_ids == helper_ids
    # And the newest-named session sorts first (most-recent first).
    all_sessions: dict[str, Session] = {
        str(s.session_id): s for s, _ in helper_rows
    }
    assert all_sessions[picked_ids[0]].name == "chat:newest"
    assert all_sessions[picked_ids[-1]].name == "chat:oldest"
