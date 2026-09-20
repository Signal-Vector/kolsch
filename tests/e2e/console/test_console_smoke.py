"""Console e2e smoke suite — drives the real `kolsch chat` binary.

Marked `@pytest.mark.e2e` (module-level `pytestmark`) so the default
`pytest` run and the CI sanity/local full gates all skip it via the
existing `-m "not e2e"` addopts (`pyproject.toml`) — same convention as
`tests/e2e/test_e2e.py`. Not wired into `compose.test.yml` or
`.githooks/pre-push`; run explicitly.

Unlike `tests/e2e/test_e2e.py` (which calls `engine.execute()` directly
with a hand-wired real-LLM adapter), this suite spawns the actual
`kolsch` binary through a real pseudo-terminal (see `harness.py`'s
`ConsoleSession`) and drives it exactly as a user would — keystrokes in,
rendered terminal output out. It exists to catch regressions in the CLI
dispatch layer, the Textual chat app, slash commands, approvals, and the
credential-wiring path itself, none of which the engine-level e2e tier
can see.

This is a harness-proving smoke set, not exhaustive feature coverage —
four golden-path scenarios (send a message, approve a tool call, switch
models, start a fresh session). Broader per-feature e2e coverage is a
deliberately separate follow-up.

Setup — reuses the same two env vars `tests/e2e/test_e2e.py` already
established, so credentials are set once for both e2e tiers:

    E2E_LLM_PROVIDER   one of: claude_code / openrouter / opencode
    E2E_LLM_API_KEY    required for openrouter/opencode. For
                       claude_code any non-empty placeholder works —
                       that provider is subscription-authenticated via
                       `claude auth login`, not an API key, so the
                       value itself is never used.

Run via:

    E2E_LLM_PROVIDER=claude_code E2E_LLM_API_KEY=unused \\
        pytest -m e2e tests/e2e/console/ -v

    E2E_LLM_PROVIDER=openrouter E2E_LLM_API_KEY=sk-... \\
        pytest -m e2e tests/e2e/console/ -v

Each test skips cleanly (not fails) when credentials aren't set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.e2e.console.conftest import discover_model_ref
from tests.e2e.console.harness import ConsoleSession

if TYPE_CHECKING:
    from tests.e2e.console.conftest import ConsoleWorkspace

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]

_PONG_PROMPT = "Reply with exactly the word PONG and nothing else."
_PING_PROMPT = "Reply with exactly the word PING and nothing else."


def test_send_message_gets_real_response(console_workspace: ConsoleWorkspace) -> None:
    """Golden path: boot the console, send a message, get a real reply.

    The cheapest possible real round-trip through the whole stack —
    subprocess boot, credential-backed model call, streamed response
    rendered to the terminal.
    """
    session = ConsoleSession(console_workspace.root, console_workspace.agent_ref)
    try:
        session.send_line(_PONG_PROMPT)
        text = session.wait_idle()
        assert "PONG" in text
    finally:
        session.close()


def test_tool_call_and_approval_flow(console_workspace: ConsoleWorkspace) -> None:
    """A tool call that needs approval renders the prompt and honors "allow once".

    `bash` is `prompt`-gated by both the workspace default rules
    (`src/cli/_setup.py:_workspace_only_default_rules`) and the
    starter agent's own permissions block
    (`src/cli/_starter_bundle/kolsch/agents/helper.yaml`), so asking
    for a shell command reliably surfaces `ApprovalBlock`'s choice
    pane (`src/cli/chat/widgets/approval_choice_pane.py`), which
    renders the literal text `"[y] allow once"` and answers to a bare
    `y` keypress (no Enter).

    Skipped for `claude_code`: live-verified that provider's adapter
    is v1 text-generation only and ignores tool schemas entirely
    (`src/llm/providers/claude_code.py:297`, `:318` — "Ignores `tools`
    — v1 is text-only"), so the model has no function-calling contract
    to invoke `bash` through and just narrates a plausible-looking
    answer without ever calling the tool — no approval prompt ever
    appears, and the test would otherwise hang until `wait_for` times
    out for the wrong reason.
    """
    if console_workspace.provider == "claude_code":
        pytest.skip(
            "claude_code's provider adapter is v1 text-only and ignores "
            "tool schemas (src/llm/providers/claude_code.py) — no real "
            "tool call is ever made, so no approval prompt appears. Run "
            "this scenario with E2E_LLM_PROVIDER=openrouter or opencode."
        )
    session = ConsoleSession(console_workspace.root, console_workspace.agent_ref)
    try:
        session.send_line(
            "Use the bash tool to run `echo KOLSCH_E2E_MARKER` and tell me "
            "its exact output."
        )
        session.wait_for(r"\[y\] allow once")
        session.press("y")
        text = session.wait_idle()
        assert "KOLSCH_E2E_MARKER" in text
    finally:
        session.close()


def test_model_switch_mid_session(console_workspace: ConsoleWorkspace) -> None:
    """`/model <ref>` applies without breaking the session's next turn.

    Discovers a real model ref via `kolsch models --json` (rather than
    hardcoding an id that may not exist for every provider) and
    switches to it — proving the slash command's validate-and-apply
    path works and a subsequent turn still gets a real reply.
    """
    model_ref = discover_model_ref(console_workspace)
    session = ConsoleSession(console_workspace.root, console_workspace.agent_ref)
    try:
        session.send_line(f"/model {model_ref}")
        session.wait_for(r"model set to", timeout=15)
        session.send_line(_PONG_PROMPT)
        text = session.wait_idle()
        assert "PONG" in text
    finally:
        session.close()


def test_new_starts_fresh_session(console_workspace: ConsoleWorkspace) -> None:
    """`/new` opens a fresh session and the console keeps working.

    `"kolsch chat •"` is only ever written by `ChatApp._open_new_session`
    (`src/cli/chat/app.py`) announcing the new session id — a reliable,
    single-purpose marker that the switch actually completed.
    """
    session = ConsoleSession(console_workspace.root, console_workspace.agent_ref)
    try:
        session.send_line(_PONG_PROMPT)
        session.wait_idle()
        session.send_line("/new")
        session.wait_for(r"kolsch chat •", timeout=15)
        session.send_line(_PING_PROMPT)
        text = session.wait_idle()
        assert "PING" in text
    finally:
        session.close()
