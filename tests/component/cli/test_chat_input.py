"""spec-textual-chat-ui — TC-CHAT-18 regression guard.

Post-Textual-rewrite this file holds a single static architectural
check: every ``.run(...)`` / ``.run_async(...)`` call under
``src/cli/chat/`` MUST pass ``inline=True``, and legacy alt-screen
patterns (``full_screen=True``, ``\\x1b[?1049h``) must not appear
anywhere in the tree.

The pre-Textual REPL tests (TC-CHAT-16 / TC-CHAT-17 multi-line
input via prompt_toolkit, ``_build_prompt_session`` /
``_run_input_loop`` in-process drivers, ``_print_banner`` /
``_format_model_label`` string checks, in-process ``cmd_chat``
coverage) exercised private helpers of the deleted
``src/cli/chat.py`` and its ``prompt_toolkit``-based session — they
were removed as part of the Cleanup phase of
``process/spec-textual-chat-ui.md`` per the coordinator decision
"delete tests that are pure internal-implementation checks of the
old REPL". The Pilot-based per-widget tests under
``tests/cli/chat/widgets/`` and the event-adapter coverage suite
cover the new UI.
"""

from __future__ import annotations

from pathlib import Path

# ===========================================================================
# TC-CHAT-18 — STATIC architectural check: chat module is NOT full-screen
# ===========================================================================


def test_tc_chat_18_chat_module_is_not_full_screen() -> None:
    """TC-CHAT-18: enforce inline-mode + no alt-screen across the
    Textual chat package (``src/cli/chat/``).

    AST-parses every ``.py`` file under ``src/cli/chat/`` and asserts:

    - Every ``.run(...)`` / ``.run_async(...)`` call has an
      ``inline=True`` kwarg. Matches ``App(...).run_async(inline=True)``
      idiom from the Textual 7.5 pin.
    - Legacy forbidden patterns (``full_screen=True``, alt-screen
      escapes) still absent across the tree.

    See AGENTS.md CHAT-R9 paragraph and
    ``process/spec-textual-chat-ui.md`` → Technical Context →
    TC-CHAT-18 rewrite.
    """
    import ast

    chat_dir = Path(__file__).parents[3] / "src" / "cli" / "chat"
    assert chat_dir.is_dir(), f"expected {chat_dir!s} to be a directory"

    for py in chat_dir.rglob("*.py"):
        src = py.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"run", "run_async"}
            ):
                kws = {k.arg for k in node.keywords}
                assert "inline" in kws, (
                    f"{py}:{node.lineno}: App.run/run_async without inline=True kwarg"
                )
                for kw in node.keywords:
                    if kw.arg == "inline":
                        assert (
                            isinstance(kw.value, ast.Constant)
                            and kw.value.value is True
                        ), f"{py}:{node.lineno}: inline must be True"
        # Legacy prompt_toolkit guard, still enforced across the tree.
        for pattern in ("full_screen=True", "\x1b[?1049h", "[?1049h"):
            assert pattern not in src, f"{py}: forbidden pattern {pattern!r}"
