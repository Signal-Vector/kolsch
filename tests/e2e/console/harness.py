"""``ConsoleSession`` — pty-driven black-box wrapper around ``kolsch chat``.

Spawns the real console entry point through a genuine pseudo-terminal
(via ``pexpect``) rather than Textual's in-process ``Pilot`` API, so
process-boundary bugs — subprocess boot, credential loading, real
terminal I/O — are in scope for the tests that use this harness.

Output is interpreted through a real terminal emulator (``pyte``)
rather than treated as an ever-growing byte stream. A first version
that just stripped ANSI codes from the raw accumulated bytes was
tried and discarded live: Textual's inline rendering repaints
constantly (cursor blink alone), and since raw bytes only ever get
appended, "no new bytes for N seconds" never fires — the harness would
hang until its own timeout on every single turn, blinking cursor or
not. ``pyte`` interprets cursor movement/overwrites into an actual
``rows x cols`` grid, so ``screen_text()`` reflects the CURRENT
rendered screen (bounded size) and "idle" means the rendered grid
itself stopped changing — unaffected by a blinking cursor whose
underlying character doesn't change.

Not a test module itself; imported by ``test_console_smoke.py``.
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import TYPE_CHECKING

import pexpect
import pyte

if TYPE_CHECKING:
    from pathlib import Path

_DEFAULT_TIMEOUT_SECONDS = 90.0
_POLL_INTERVAL_SECONDS = 0.2
_READY_TIMEOUT_SECONDS = 20.0
# The composer's placeholder hint text (only present once Textual has
# mounted and the composer can accept input). Live-verified this
# matters: keystrokes sent immediately after `pexpect.spawn` — before
# the app finishes booting — are silently dropped, so every
# `ConsoleSession` blocks in `__init__` until this appears.
_READY_PATTERN = r"Enter=send"


class ConsoleTimeout(RuntimeError):
    """A ``wait_for``/``wait_idle`` call didn't see what it expected in time.

    Carries the last rendered screen contents so a failing test's
    output shows what the console actually displayed instead of just
    "timed out".
    """

    def __init__(self, message: str, screen: str) -> None:
        super().__init__(f"{message}\n\n--- last screen ---\n{screen}")
        self.screen = screen


class ConsoleSession:
    """Drives a real ``kolsch chat`` subprocess through a pseudo-terminal."""

    def __init__(
        self,
        workspace: Path,
        agent_ref: str,
        *,
        env: dict[str, str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        dimensions: tuple[int, int] = (40, 120),
    ) -> None:
        rows, cols = dimensions
        run_env = dict(os.environ if env is None else env)
        run_env["KOLSCH_WORKSPACE"] = str(workspace)
        self._child = pexpect.spawn(
            sys.executable,
            ["-m", "cli", "chat", agent_ref],
            cwd=str(workspace),
            env=run_env,
            timeout=timeout,
            dimensions=dimensions,
            encoding="utf-8",
            codec_errors="ignore",
        )
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)
        self.wait_for(_READY_PATTERN, timeout=_READY_TIMEOUT_SECONDS)

    # ---- input ---------------------------------------------------------

    def send_line(self, text: str) -> None:
        """Type ``text`` into the composer and submit it (Enter)."""
        self._child.send(text)
        self._child.send("\r")

    def press(self, key: str) -> None:
        """Send a raw keystroke with no trailing Enter.

        Needed for ``ApprovalBlock``'s choice pane
        (``src/cli/chat/widgets/approval_block.py``), which reacts to
        bare ``y``/``n``/``a``/``d``/``esc`` keypresses rather than a
        submitted line.
        """
        self._child.send(key)

    # ---- output ----------------------------------------------------------

    def _pump(self) -> None:
        """Feed any bytes currently available into the terminal emulator."""
        try:
            while True:
                chunk = self._child.read_nonblocking(size=4096, timeout=0)
                if not chunk:
                    break
                self._stream.feed(chunk)
        except (pexpect.TIMEOUT, pexpect.EOF):
            pass

    def screen_text(self) -> str:
        """The currently rendered screen, one line per row, right-trimmed."""
        self._pump()
        return "\n".join(line.rstrip() for line in self._screen.display)

    def wait_for(self, pattern: str, *, timeout: float | None = None) -> str:
        """Poll until ``pattern`` (a regex) matches the rendered screen.

        Returns the full captured screen text once it matches. Raises
        ``ConsoleTimeout`` (with the last captured screen attached) if
        the pattern never appears within ``timeout`` seconds (defaults
        to the session's own timeout).
        """
        effective_timeout = (
            timeout if timeout is not None else float(self._child.timeout)
        )
        deadline = time.monotonic() + effective_timeout
        compiled = re.compile(pattern)
        text = self.screen_text()
        while not compiled.search(text):
            if time.monotonic() >= deadline:
                message = f"timed out waiting for pattern {pattern!r}"
                raise ConsoleTimeout(message, text)
            time.sleep(_POLL_INTERVAL_SECONDS)
            text = self.screen_text()
        return text

    def wait_idle(self, *, quiet_seconds: float = 1.5, timeout: float = 60.0) -> str:
        """Wait for streaming output to go quiet — the "turn finished" heuristic.

        There is no clean sentinel for "the assistant is done" in an
        inline-rendered streaming UI, so this polls until the RENDERED
        SCREEN (not the raw byte stream) stops changing for
        `quiet_seconds`, bounded by `timeout`. Diffing the rendered
        screen (via `pyte`'s terminal emulation) rather than raw bytes
        is load-bearing: an earlier version compared accumulated raw
        byte-stream length, which never went quiet — Textual's cursor
        blink alone keeps writing bytes forever, so "no new bytes"
        never fired and every call timed out. The rendered screen's
        text content is unaffected by a blinking cursor whose
        underlying character doesn't change.
        """
        deadline = time.monotonic() + timeout
        last_text: str | None = None
        quiet_since: float | None = None
        text = self.screen_text()
        while True:
            now = time.monotonic()
            if text == last_text:
                if quiet_since is None:
                    quiet_since = now
                elif now - quiet_since >= quiet_seconds:
                    return text
            else:
                quiet_since = None
            last_text = text
            if now >= deadline:
                raise ConsoleTimeout("timed out waiting for output to go idle", text)
            time.sleep(_POLL_INTERVAL_SECONDS)
            text = self.screen_text()

    # ---- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Exit the chat session and terminate the underlying process.

        Safe to call more than once. ``claude_code`` shells out to
        ``claude -p`` as a grandchild process — ``force=True`` plus the
        ``isalive()`` check covers reaping the common case.
        """
        if not self._child.isalive():
            return
        try:
            self.send_line("/exit")
            self._child.expect(pexpect.EOF, timeout=5)
        except (pexpect.TIMEOUT, pexpect.EOF, OSError):
            pass
        finally:
            if self._child.isalive():
                self._child.close(force=True)
