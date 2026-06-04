"""Phase 2.1 Step 24a — input-loop component tests.

Three cases per the build doc's acceptance criterion:

- **TC-CHAT-16**: multi-line input via the prompt_toolkit harness
  with Ctrl-J (the universal newline key) inserting a literal
  ``\\n`` into the submitted text.
- **TC-CHAT-17**: cursor editing — type "hello", Home, type "say ",
  Enter; submitted text is "say hello".
- **TC-CHAT-18**: STATIC analysis — import ``src/cli/chat.py`` and
  assert NO occurrence of ``full_screen=True``, NO call to
  ``alternate_screen``, NO ANSI sequence ``\\x1b[?1049h``. This
  is the regression guard per patch §2.4 / Part 10 item 1.

The input tests drive ``_build_prompt_session`` directly via
prompt_toolkit's ``create_pipe_input`` + ``DummyOutput`` testing
seam — no subprocess. ``test_chat_smoke.py`` covers the
subprocess end-to-end path separately.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cli import chat

if TYPE_CHECKING:
    import pytest

# ===========================================================================
# Helpers — drive the prompt session with piped key sequences
# ===========================================================================


async def _prompt_with_keys(keys: str) -> str:
    """Feed ``keys`` to a fresh prompt session; return the submitted text.

    ``\\r`` is what prompt_toolkit treats as Enter on stdin (it's
    the raw byte the terminal sends for the Return key). ``\\n``
    is Ctrl-J — prompt_toolkit's "newline-in-multiline" binding
    (patch §2.5).
    """
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        session = chat._build_prompt_session(input=pipe_input, output=DummyOutput())
        pipe_input.send_text(keys)
        return await session.prompt_async("> ")


# ===========================================================================
# TC-CHAT-16 — multi-line input via Ctrl-J inserts a literal newline
# ===========================================================================


async def test_tc_chat_16_multiline_input_via_ctrl_j() -> None:
    """TC-CHAT-16: Ctrl-J between two words produces a submitted
    string containing a literal ``\\n``. Enter (``\\r``) submits.

    Per patch §2.5: Shift-Enter on iTerm2 / kitty / Alacritty is
    delivered as Ctrl-J by default — this binding satisfies
    "Shift+Enter is the primary newline key" on those terminals.
    Alt-Enter is exercised in a sibling test below.
    """
    text = await _prompt_with_keys("line1\nline2\r")
    assert text == "line1\nline2", (
        f"expected multi-line input with embedded newline; got {text!r}"
    )


async def test_tc_chat_16_multiline_input_via_alt_enter() -> None:
    """TC-CHAT-16 variant — Alt-Enter (Esc-Enter sequence) ALSO
    inserts newline. Patch §2.5: this is the universal fallback
    for terminals that don't distinguish Shift-Enter (macOS
    Terminal.app, stock gnome-terminal).

    The Esc-Enter sequence on stdin is ``\\x1b\\r``.
    """
    text = await _prompt_with_keys("line1\x1b\rline2\r")
    assert text == "line1\nline2", f"expected Alt-Enter to insert newline; got {text!r}"


# ===========================================================================
# TC-CHAT-17 — cursor editing (Home + insert)
# ===========================================================================


async def test_tc_chat_17_cursor_editing_home_insert() -> None:
    """TC-CHAT-17: type "hello", Home, type "say ", Enter.

    ``\\x1b[H`` is the ANSI escape sequence for the Home key on
    most terminals (xterm and compatibles).
    """
    text = await _prompt_with_keys("hello\x1b[Hsay \r")
    assert text == "say hello", (
        f"expected cursor editing to produce 'say hello'; got {text!r}"
    )


# ===========================================================================
# TC-CHAT-18 — STATIC architectural check: chat module is NOT full-screen
# ===========================================================================


def test_tc_chat_18_chat_module_is_not_full_screen() -> None:
    """TC-CHAT-18: introspect the chat module's source and assert
    NO occurrence of the full-screen / alternate-screen forbidden
    patterns.

    Per patch §2.4 + Part 10 item 1 + the AGENTS.md hard rule
    (CHAT-R9): the chat MUST stay in prompt_toolkit's
    bottom-toolbar idiom so terminal-native scrolling + selection
    keep working. A future change introducing full-screen mode
    fails this test loudly.

    The "scrolling actually works in iTerm2" verification is a
    manual end-to-end check by the reviewer (Phase 2.1 patch §2.4)
    — not automated. This test is the architectural floor.
    """
    source = inspect.getsource(chat)
    forbidden = (
        "full_screen=True",
        "alternate_screen",
        # iTerm2 / xterm DEC private mode 1049 — switch-to-alt-screen-
        # and-save-cursor. Any chat module that ever emitted this byte
        # sequence directly would break native scrolling per §2.4.
        "\x1b[?1049h",
        # The string repr of the same escape, as it might appear in
        # source code without an actual byte:
        "[?1049h",
    )
    hits = [pattern for pattern in forbidden if pattern in source]
    assert not hits, (
        f"src/cli/chat.py contains forbidden full-screen / "
        f"alternate-screen pattern(s) {hits!r}; "
        f"see phase2.1-chat-specs.md §2.4 + AGENTS.md CHAT-R9 "
        f"hard rule"
    )

    # Belt-and-braces — also confirm the module source LIVES at the
    # path the AGENTS.md rule names, so a future contributor moving
    # the file gets a meaningful failure here too.
    chat_path = Path(inspect.getfile(chat))
    assert chat_path.name == "chat.py"
    assert chat_path.parent.name == "cli"


# ===========================================================================
# Internals — _run_input_loop, _print_banner, _format_model_label
# ===========================================================================


async def test_run_input_loop_routes_messages_to_engine(
    chat_workspace: Path,
) -> None:
    """24c contract: each submitted non-slash line is forwarded to
    the engine via ``execute_stream``; the renderer prints the
    EchoLLMDriver's reply. Two messages + EOF here exercises the
    full body of ``_run_input_loop`` AND the renderer's
    happy-path event handling.

    EchoLLMDriver returns ``"Echo: <last user message>"`` so the
    rendered output names the user's text back to them.
    """
    import io as _io

    from platform_runtime import bootstrap_runtime

    runtime = await bootstrap_runtime(root=chat_workspace)
    config = await runtime.build_resolved_config("kolsch/helper")
    session = await runtime.store.create_session(
        str(config.agent_id),
        name="test-run-input-loop",
    )
    threads = await runtime.store.list_threads(session.session_id)
    thread_id = threads[0].thread_id

    out = _io.StringIO()
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        prompt_session = chat._build_prompt_session(
            input=pipe_input, output=DummyOutput()
        )
        pipe_input.send_text("hello\rsecond\r")
        pipe_input.close()
        rc = await chat._run_input_loop(
            prompt_session,
            stdout=out,
            runtime=runtime,
            config=config,
            thread_id=thread_id,
        )
    assert rc == 0
    rendered = out.getvalue()
    # Echo prefix from the default-tier EchoLLMDriver.
    assert "Echo:" in rendered
    # bug-chat-permissions-and-console-leak Step E: chat no
    # longer writes "You: <text>" itself. In production
    # prompt_toolkit's terminal echo lands the line in the
    # scrollback; here DummyOutput discards that echo, so the
    # captured stdout must be free of "You: ..." copies. The
    # agent reply (Echo: ...) below proves the loop ran end to
    # end and routed both submissions through the engine.
    assert "You: hello" not in rendered
    assert "You: second" not in rendered
    assert rendered.count("Echo: hello") == 1
    assert rendered.count("Echo: second") == 1


def test_print_banner_format_matches_patch_2_1_sketch() -> None:
    """``kolsch chat • <agent> • <model> • <session>`` per patch §2.1."""
    import io as _io

    out = _io.StringIO()
    chat._print_banner(
        out,
        agent_name="helper",
        model_label="opus",
        session_id="ses_abc123",
    )
    written = out.getvalue()
    assert "kolsch chat" in written
    assert "helper" in written
    assert "opus" in written
    assert "ses_abc123" in written
    assert "•" in written


def test_format_model_label_handles_modelspec_object_and_alias_string() -> None:
    """``_format_model_label`` accepts both the resolved ModelSpec
    object (``provider`` + ``model`` attributes) and a bare alias
    string."""

    class _ModelLike:
        provider = "anthropic"
        model = "claude-opus-4-7"

    assert chat._format_model_label(_ModelLike()) == "anthropic/claude-opus-4-7"
    assert chat._format_model_label("opus") == "opus"
    assert "object" in chat._format_model_label(object())


# ===========================================================================
# In-process cmd_chat — exercises the body of cmd_chat for coverage
# ===========================================================================


async def test_cmd_chat_in_process_runs_against_initialized_workspace(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """End-to-end of ``cmd_chat`` inside pytest's process: scaffolds a
    workspace via the existing setup runner, then drives cmd_chat
    with piped EOF input. This exercises the cmd_chat body
    (bootstrap_runtime → build_resolved_config → create_session →
    banner → input loop EOF) so pytest-cov sees those statements.
    The subprocess smoke (TC-CLI-SMOKE-CHAT-02) covers the same path
    end-to-end; this test exists to make cov ≥ 80%.
    """
    import argparse
    import io as _io
    import subprocess as _subprocess
    import sys as _sys

    root = tmp_path_factory.mktemp("inproc_chat_ws")
    # Reuse the subprocess setup path — fast (~1s) and avoids
    # re-implementing the workspace scaffolding.
    r = _subprocess.run(  # noqa: ASYNC221 — fully-controlled argv; setup is one-shot
        [_sys.executable, "-m", "cli", "setup", "--non-interactive"],
        env={"KOLSCH_WORKSPACE": str(root), "PATH": ""},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    _ = r

    out = _io.StringIO()
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        # Close the pipe immediately → EOFError on the first prompt →
        # cmd_chat returns 0 after printing the banner.
        pipe_input.close()
        rc = await chat.cmd_chat(
            argparse.Namespace(
                root=root,
                agent_ref="kolsch/helper",
                session=None,
                model=None,
            ),
            out=out,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )

    assert rc == 0
    written = out.getvalue()
    assert "kolsch chat" in written
    assert "helper" in written
    assert "ses_" in written


async def test_cmd_chat_in_process_honors_model_override(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """``--model <ref>`` overrides the banner's model label.

    24e: the override resolves through the same ModelSpec parser
    ``/model`` uses, so bare aliases get expanded against
    ``platform.yaml``'s ``model_aliases``. The compact form
    ``provider/model`` is always valid; the test uses that so
    it doesn't depend on a model_aliases entry being present in
    the workspace.
    """
    import argparse
    import io as _io
    import subprocess as _subprocess
    import sys as _sys

    root = tmp_path_factory.mktemp("inproc_chat_model_ws")
    _subprocess.run(  # noqa: ASYNC221
        [_sys.executable, "-m", "cli", "setup", "--non-interactive"],
        env={"KOLSCH_WORKSPACE": str(root), "PATH": ""},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    out = _io.StringIO()
    with (
        create_pipe_input() as pipe_input,
        create_app_session(input=pipe_input, output=DummyOutput()),
    ):
        pipe_input.close()
        await chat.cmd_chat(
            argparse.Namespace(
                root=root,
                agent_ref=None,  # exercise the _DEFAULT_AGENT_REF fallback
                session=None,
                model="anthropic/claude-opus-4-7",
            ),
            out=out,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )

    assert "kolsch chat" in out.getvalue()
    assert "opus" in out.getvalue()
