"""TC-CREWAI-NOLEAK-01 — bug-crewai-stdout-leak-investigation Step H.

End-to-end regression pin: a ``kolsch chat`` subprocess driven with
piped stdin must NOT emit any of CrewAI 1.14.6's listener-side
prose to its captured ``stdout`` / ``stderr``. The set of "CrewAI
signatures" comes from the investigation doc's Probe 1 + Probe 2:

- ``Thinking`` — ``ConsoleFormatter:889`` literal "Thinking...\\n"
- ``Crew Execution`` — verbose=True crew-start panel header
- ``Agent Started`` — verbose=True agent-step panel
- ``Task Started`` — verbose=True task-start panel
- ``[CrewAIEventsBus]`` — the EventsBus pairing-mismatch ``print()``
  that fires on the failure path (LLM raise → unpaired event)
- ``Working Agent`` — another known agent-start phrase
- ``╭`` / ``╰`` — Rich panel border chars. **NOT in the signature
  list here** because the chat renderer's OWN panels (the
  permission modal, the tool-call panel, the tool-result panel)
  use the same chars; the in-process test
  ``test_run_exchange_suppresses_crewai_stdout_leakage`` is the
  load-bearing pin for "no Rich panel from CrewAI specifically"
  (it can distinguish via outer-redirect-buffer scoping).

Run-mode: fake LLM via ``--model fake/fake-model`` (no real
network). Per the investigation doc's Step E captures, the fake
LLM produces zero signatures on both pre-fix and post-fix code
in the healthy path — so this test's literal value is
"PR-author-asserts no signatures appear during normal chat
operation," not "this test fails on pre-fix code." The
load-bearing pre-fix bite lives in
``test_run_exchange_suppresses_crewai_stdout_leakage`` (in-process
test of the wrap mechanism itself); the test catalog entry below
records both.

The escalation in Step H's investigation report explains why
fake-LLM-via-subprocess can't reproduce CrewAI's failure-path
EventsBus warnings: the EchoLLMDriver returns cleanly, no
event-pair mismatch fires, no ``print()`` calls happen at all in
the healthy path. To get pre-fix bite in subprocess form we'd
need either a real LLM that errors (rejected — no network in
component tier) or a deliberately-failing fake LLM driver added
to the dev-LLM module (deferred — coordinator follow-up).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# Per the module docstring: the signature list excludes the panel
# border chars ╭/╰ because the chat renderer's own modal +
# tool-call panels share those characters. The in-process test
# (test_crewai_adapter.py) is the right place for the structural
# panel-chars contract.
_CREWAI_SIGNATURES: tuple[str, ...] = (
    "Thinking",
    "Crew Execution",
    "Agent Started",
    "Task Started",
    "[CrewAIEventsBus]",
    "Working Agent",
)


def test_chat_does_not_leak_crewai_output(chat_workspace: Path) -> None:
    """TC-CREWAI-NOLEAK-01 — drive a fake-LLM chat session via
    subprocess; assert no CrewAI signatures appear in the captured
    stdout or stderr.

    Pinning end-to-end: future code paths that bypass the G1 wrap
    (e.g., a new sync entry-point that doesn't go through
    ``run_exchange``) would surface here even though they wouldn't
    surface in the in-process unit test. Subprocess + piped stdin +
    captured streams gives us the closest approximation to "what
    the user sees" that doesn't require a real TTY.
    """
    env = dict(os.environ)
    env["KOLSCH_WORKSPACE"] = str(chat_workspace)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli",
            "chat",
            "--model",
            "fake/fake-model",
        ],
        env=env,
        input="say hi\n/exit\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    # Combined buffer — Step G1's wrap captures BOTH stdout and
    # stderr inside the adapter, so the assertion holds against
    # either stream.
    combined = result.stdout + result.stderr
    for signature in _CREWAI_SIGNATURES:
        assert signature not in combined, (
            f"CrewAI signature {signature!r} leaked into the chat "
            f"subprocess's output — G1's redirect_stdout/redirect_stderr "
            f"wrap at crewai_framework.py:run_exchange has regressed, "
            f"OR a new code path that bypasses run_exchange has been "
            f"added without applying the same wrap. "
            f"Captured stdout (first 500 chars):\n{result.stdout[:500]!r}\n"
            f"Captured stderr (first 500 chars):\n{result.stderr[:500]!r}"
        )
