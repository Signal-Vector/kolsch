"""bug-116 — `kolsch chat` hard-exits instead of hanging at 100% CPU.

When the controlling terminal goes away, stdin becomes a dead tty at
permanent EOF and Textual's inline-driver input reader — a NON-daemon
thread — busy-loops (`select()` returns instantly on EOF). Because it is
non-daemon the interpreter blocks in `Py_Finalize`'s `threading._shutdown()`
join and never exits, pinning a CPU core for days.

The fix guarantees the chat process terminates at its boundary:

- `cli.chat.app.force_process_exit(code)` routes through the existing
  `_EXIT_HOOK` (`os._exit` in prod, spy in tests) — Test A.
- `cli.main._cmd_chat` hard-exits with the app's return code the moment
  `run_async()` returns, so the interpreter never reaches the join —
  Test B.
- `cli.main._install_chat_hangup_guard` installs a SIGHUP handler that
  hard-exits at once on terminal loss — Tests C/D, and end-to-end in a
  real subprocess receiving a real SIGHUP (returns 128+SIGHUP) — Test E.

The bug-067 shutdown watchdog only covers the `on_unmount` teardown
window and disarms before interpreter finalization; these paths cover the
later `run_async()`-returned → finalize window.

Manual repro of the original zombie (not automated — needs a real
terminal): `kolsch chat <agent>`, close the terminal window, then
`ps -o pid,tty,%cpu` — pre-fix the python process shows `tty ??` at
`%cpu 100`; post-fix it is gone.
"""

from __future__ import annotations

import argparse
import importlib
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from cli.chat import app as chat_app_module
from cli.chat.app import _set_exit_hook, force_process_exit
from cli.main import _cmd_chat, _install_chat_hangup_guard

if TYPE_CHECKING:
    from collections.abc import Iterator


class _ExitHookSpy:
    """Records exit-hook invocations without killing the pytest process."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, code: int) -> None:
        self.calls.append(code)


@pytest.fixture
def exit_hook_spy() -> Iterator[_ExitHookSpy]:
    """Install a spy for `cli.chat.app._EXIT_HOOK`; restore on teardown."""
    spy = _ExitHookSpy()
    original = chat_app_module._EXIT_HOOK
    _set_exit_hook(spy)
    try:
        yield spy
    finally:
        _set_exit_hook(original)


@pytest.fixture
def restore_sighup() -> Iterator[None]:
    """Snapshot + restore the process SIGHUP handler around a test.

    Tests here install a real SIGHUP handler; without restoration they
    would leak that handler into the rest of the pytest session.
    """
    if not hasattr(signal, "SIGHUP"):
        yield
        return
    previous = signal.getsignal(signal.SIGHUP)
    try:
        yield
    finally:
        signal.signal(signal.SIGHUP, previous)


# ---------------------------------------------------------------------------
# Test A — the exit seam
# ---------------------------------------------------------------------------


def test_force_process_exit_routes_through_exit_hook(
    exit_hook_spy: _ExitHookSpy,
) -> None:
    """`force_process_exit(code)` calls `_EXIT_HOOK(code)` verbatim."""
    force_process_exit(3)
    assert exit_hook_spy.calls == [3]


# ---------------------------------------------------------------------------
# Test B — `_cmd_chat` hard-exits with the app's return code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_chat_hard_exits_with_app_return_code(
    exit_hook_spy: _ExitHookSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After `run_async()` returns, `_cmd_chat` force-exits with the code.

    Stubs out session/app construction so the test exercises ONLY the
    entrypoint's exit wiring. The exit hook is a spy, so `force_process_exit`
    returns instead of killing pytest and `_cmd_chat`'s trailing `return`
    is reached — both the spy call and the returned code are asserted.
    """

    class _FakeApp:
        def __init__(
            self,
            *,
            session: Any,
            engine: Any,
            full_history: bool,
            auto_approve: bool = False,
        ) -> None:
            _ = (session, engine, full_history, auto_approve)
            self.return_code = 7

        async def run_async(self, **kwargs: Any) -> None:
            _ = kwargs

    async def _fake_resolve(_args: Any) -> None:
        return None

    async def _fake_build_session(_args: Any, *, runtime: Any = None) -> Any:
        _ = runtime
        return SimpleNamespace(runtime=SimpleNamespace(engine=object()))

    # Patch the module objects directly: the string form "cli.main.X"
    # mis-resolves because `cli/__init__.py` re-exports the `main`
    # function, shadowing the `cli.main` submodule as a package attribute.
    cli_main = importlib.import_module("cli.main")
    cli_chat_app = importlib.import_module("cli.chat.app")
    cli_chat_session = importlib.import_module("cli.chat.session")

    monkeypatch.setattr(cli_main, "_resolve_resume_session", _fake_resolve)
    monkeypatch.setattr(cli_main, "_install_chat_hangup_guard", lambda: None)
    monkeypatch.setattr(cli_chat_session, "build_session", _fake_build_session)
    monkeypatch.setattr(cli_chat_app, "ChatApp", _FakeApp)
    monkeypatch.setattr(cli_chat_app, "register_chat_slash_commands", lambda: None)

    result = await _cmd_chat(argparse.Namespace(full_history=False))

    assert exit_hook_spy.calls == [7]
    assert result == 7


# ---------------------------------------------------------------------------
# Tests C/D — the SIGHUP guard
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
def test_hangup_guard_hard_exits_on_sighup(
    exit_hook_spy: _ExitHookSpy,
    restore_sighup: None,
) -> None:
    """The installed SIGHUP handler hard-exits with 128 + SIGHUP."""
    _ = restore_sighup
    _install_chat_hangup_guard()

    handler = signal.getsignal(signal.SIGHUP)
    assert callable(handler)
    handler(signal.SIGHUP, None)  # (signum, frame)

    assert exit_hook_spy.calls == [128 + int(signal.SIGHUP)]


def test_hangup_guard_safe_off_main_thread(exit_hook_spy: _ExitHookSpy) -> None:
    """Installing the guard off the main thread is a swallowed no-op.

    `signal.signal` raises `ValueError` off the main thread; the guard
    suppresses it so a non-main-thread caller neither crashes nor exits.
    """
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            _install_chat_hangup_guard()
        except BaseException as exc:  # noqa: BLE001 — record any leak
            errors.append(exc)

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert exit_hook_spy.calls == []  # the guard install must not exit


# ---------------------------------------------------------------------------
# Test E — end-to-end: a real subprocess dies on a real SIGHUP
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
def test_real_subprocess_exits_on_sighup() -> None:
    """A process with the guard installed exits 128+SIGHUP on real SIGHUP.

    Exercises the true production path — `_install_chat_hangup_guard` +
    `force_process_exit` = `os._exit` — in a real interpreter receiving a
    real signal (no spy). We deliver SIGHUP directly rather than via a pty
    controlling-terminal dance so the test is deterministic across
    platforms; terminal-close → SIGHUP delivery is the OS's job.
    """
    script = (
        "import time\n"
        "from cli.main import _install_chat_hangup_guard\n"
        "_install_chat_hangup_guard()\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        ready = proc.stdout.readline().strip()
        assert ready == "ready", f"child never signalled ready (got {ready!r})"
        proc.send_signal(signal.SIGHUP)
        returncode = proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert returncode == 128 + int(signal.SIGHUP)
