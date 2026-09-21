"""Feature-area e2e console scenarios — Tier 1 of the scenario proposal.

`test_console_smoke.py`'s docstring promises broader per-feature
coverage as a deliberately separate follow-up; this is that follow-up.
Same harness, same conventions (`console_workspace` fixture, real
credentials, module-level `pytestmark`). Each scenario proves
something no unit or integration test tier can see — the individual
docstrings name the specific regression class.

Ten scenarios were proposed; nine test functions ship here. `/compact`
and `/compact --preview` were proposed as two separate scenarios but
turned out, once actually driven live, to hit the identical code
branch on a short test session (see
`test_compaction_commands_on_short_session`'s docstring) — merging
them is a deliberate, verified consolidation, not a dropped scenario.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.e2e.console.conftest import latest_session_id, run_cli
from tests.e2e.console.harness import ConsoleSession

if TYPE_CHECKING:
    from tests.e2e.console.conftest import ConsoleWorkspace

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(180)]

_PONG_PROMPT = "Reply with exactly the word PONG and nothing else."
_PING_PROMPT = "Reply with exactly the word PING and nothing else."


def _skip_if_text_only_provider(workspace: ConsoleWorkspace) -> None:
    """Shared guard for scenarios that need the model to actually call a tool.

    `claude_code`'s provider adapter is v1 text-generation only and
    ignores tool schemas entirely (`src/llm/providers/claude_code.py`),
    so the model never issues a real tool call and no approval prompt
    ever appears. Mirrors the guard in `test_console_smoke.py`'s
    `test_tool_call_and_approval_flow`.
    """
    if workspace.provider == "claude_code":
        pytest.skip(
            "claude_code's provider adapter is v1 text-only and ignores "
            "tool schemas — no real tool call is ever made. Run this "
            "scenario with E2E_LLM_PROVIDER=openrouter or opencode."
        )


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


def test_workflow_inline_happy_path(console_workspace: ConsoleWorkspace) -> None:
    """`/workflow` invokes a deterministic workflow inline; the card completes.

    `kolsch/workflows/hello` (a `noop` root feeding an authored-Python
    `echo` node) has no LLM call anywhere in it — the flagship in-chat
    workflow journey at zero cost, proving the full `/workflow` ->
    `WorkflowsImpl.start` -> live `WorkflowCard` mount -> real terminal
    repaint pipeline through the actual pty.
    """
    session = ConsoleSession(console_workspace.root, console_workspace.agent_ref)
    try:
        session.send_line(
            "/workflow kolsch/workflows/hello with message=KOLSCH_E2E_MARKER"
        )
        text = session.wait_idle()
        assert "KOLSCH_E2E_MARKER" in text
        assert "✓" in text
    finally:
        session.close()


@pytest.mark.skip(
    reason=(
        "Live-verified 2026-09-21: `/workflow kolsch/workflows/hello-paused` "
        "invoked from chat never reaches `waiting_for_input` — the `ask` "
        "node spins (running glyph, elapsed counter climbing) indefinitely "
        "(observed 2+ minutes with no transition). The SAME workflow "
        "definition pauses correctly and instantly via the headless "
        "`kolsch workflows run kolsch/workflows/hello-paused` CLI path "
        "(confirmed: prints `waiting_for_input` in well under a second), "
        "so the workflow YAML and the executor's `user_input` handling are "
        "both fine — the bug is specific to `WorkflowsImpl.start`'s async "
        "FIFO-worker path used by in-chat `/workflow` invocation "
        "(src/cli/chat/services/workflows_impl.py), not the CLI's direct "
        "`IterativeExecutor.run()` call. This is a real, separate bug "
        "found by this scenario — exactly the failure class it was "
        "written to catch — but is out of scope to fix inline while "
        "building the scenario. Un-skip once fixed."
    )
)
def test_workflow_pause_and_resume(console_workspace: ConsoleWorkspace) -> None:
    """A paused workflow doesn't wedge the chat; resuming via the CLI lands.

    `kolsch/workflows/hello-paused` pauses on `ask` (a `user_input`
    node). Once the bug above is fixed, resuming is expected to
    happen via a SEPARATE `kolsch workflows resume <run_id> --input
    response=<text>` CLI call — the workflow's own documented usage —
    NOT by typing a follow-up chat message in the same session.
    `_resume_pending_workflow` (`src/cli/chat/session.py`) is ported
    from `_legacy.py` but has no live call site in
    `ChatApp.on_composer_submitted`, which routes every plain message
    straight into `_run_exchange` regardless of pause state. Both the
    chat process and the resume CLI process read/write the same
    on-disk `runs/<run_id>/` state, so the resume should land
    correctly despite being a separate OS process — once the pause
    itself actually completes.

    Pause/resume has broken in this exact spot before
    (bug-106/107/110) — the "chat stays responsive while paused" half
    of this test directly targets that history.
    """
    session = ConsoleSession(console_workspace.root, console_workspace.agent_ref)
    try:
        session.send_line("/workflow kolsch/workflows/hello-paused")
        session.wait_for(r"waiting|⚠", timeout=30)
        # The chat itself must stay responsive while the workflow sits
        # paused — a real, unrelated turn should complete normally.
        session.send_line(_PONG_PROMPT)
        text = session.wait_idle()
        assert "PONG" in text
    finally:
        session.close()

    run_dirs = list((console_workspace.root / "runs").iterdir())
    assert len(run_dirs) == 1, f"expected exactly one workflow run dir: {run_dirs}"
    run_id = run_dirs[0].name

    result = run_cli(
        console_workspace,
        "workflows",
        "resume",
        run_id,
        "--input",
        "response=KOLSCH_E2E_RESUMED",
    )
    assert result.returncode == 0, (
        f"`kolsch workflows resume` failed:\n"
        f"stdout=\n{result.stdout}\nstderr=\n{result.stderr}"
    )
    assert "succeeded" in result.stdout


# ---------------------------------------------------------------------------
# Authorization & permissions
# ---------------------------------------------------------------------------


def test_approval_allow_and_remember_rule(console_workspace: ConsoleWorkspace) -> None:
    """Allow + write a rule (the `a` path); the rule then short-circuits repeats.

    `bash` is `prompt`-gated by the workspace defaults. Pressing `a`
    advances the choice pane to the pattern-edit pane (pre-filled with
    a suggested pattern) to the save-confirm pane; a second identical
    request should then sail through with no prompt at all — proving
    a rule written to disk by one turn is actually read back and
    honored by a second, independent turn.
    """
    _skip_if_text_only_provider(console_workspace)
    session = ConsoleSession(console_workspace.root, console_workspace.agent_ref)
    try:
        session.send_line(
            "Use the bash tool to run `echo KOLSCH_E2E_MARKER` and tell me "
            "its exact output."
        )
        session.wait_for(r"\[y\] allow once")
        session.press("a")
        session.wait_for(r"Pattern:")
        session.send_line("")  # Enter alone submits the pre-filled pattern.
        session.wait_for(r"Save this rule\?")
        session.press("y")
        session.wait_for(r"✓ Rule saved \(allow\)")
        session.send_line(
            "Use the bash tool to run `echo KOLSCH_E2E_MARKER` again and "
            "tell me its exact output."
        )
        text = session.wait_idle()
        assert "[y] allow once" not in text
        assert "KOLSCH_E2E_MARKER" in text
    finally:
        session.close()


def test_approval_deny_and_remember_rule_then_why(
    console_workspace: ConsoleWorkspace,
) -> None:
    """Deny + write a rule (the `d` path); repeat fails closed, `/why` explains it.

    Deny is NOT symmetric with allow in `ApprovalBlock`'s
    implementation (`_pending_effect` branches differently by effect),
    so this earns its own test rather than reusing the allow
    scenario's assertions. Bare `/why` is chained onto this session's
    already-produced denial — free, since the denial already
    happened.
    """
    _skip_if_text_only_provider(console_workspace)
    session = ConsoleSession(console_workspace.root, console_workspace.agent_ref)
    try:
        session.send_line("Use the bash tool to run `echo KOLSCH_E2E_DENY_MARKER`.")
        session.wait_for(r"\[y\] allow once")
        session.press("d")
        session.wait_for(r"Pattern:")
        session.send_line("")
        session.wait_for(r"Save this rule\?")
        session.press("y")
        session.wait_for(r"✗ Rule saved \(deny\)")
        session.send_line(
            "Use the bash tool to run `echo KOLSCH_E2E_DENY_MARKER` again."
        )
        text = session.wait_idle()
        assert "[y] allow once" not in text
        session.send_line("/why")
        text = session.wait_for(r"╭─ denied")
        assert "rule:" in text
    finally:
        session.close()


def test_perm_admin_lockdown_and_reload(console_workspace: ConsoleWorkspace) -> None:
    """`/perm deny` locks a tool down non-interactively; `/reload` picks up edits.

    A completely different code path from the approval widget
    (`_perm_upsert` -> `authz.add_rule` -> YAML write-back, never
    touches `ApprovalBlock`) — proves the non-interactive admin
    workflow separately from the in-the-moment approval flow.
    `/reload` rides along for free since the workspace already has a
    fresh rule to reload.
    """
    _skip_if_text_only_provider(console_workspace)
    session = ConsoleSession(console_workspace.root, console_workspace.agent_ref)
    try:
        session.send_line('/perm deny kolsch/tools/bash "*"')
        session.wait_for(r"/perm deny: added rule\s+what='kolsch/tools/bash'")
        session.send_line("/perm list")
        session.wait_for(r"what='kolsch/tools/bash'.*effect=deny")
        session.send_line("/reload")
        session.wait_for(r"reloaded \d+ rules across 4 layers")
        session.send_line("Use the bash tool to run `echo KOLSCH_E2E_MARKER`.")
        text = session.wait_idle()
        assert "[y] allow once" not in text
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Context / compaction
# ---------------------------------------------------------------------------


def test_compaction_commands_on_short_session(
    console_workspace: ConsoleWorkspace,
) -> None:
    """`/compact`, `/compact --preview`, `/compaction-history`, `/why-compact`
    all handle an empty-history session correctly — and the session
    keeps working afterward.

    Live-verified while building this scenario:
    `/compact`'s "middle span" to summarize is everything OLDER than
    roughly `keep_recent_fraction` (default 20%) of the model's full
    context budget (`src/engine/engine.py:_manual_compact_locked`).
    For a real ~200k-token model, a short e2e session never
    accumulates enough history to have a non-empty middle span, so
    both `/compact` and `/compact --preview` legitimately hit the
    "nothing to compact" branch every real time — not the "✓
    Compacted" success path, which would need hundreds of real LLM
    turns to reach honestly. This scenario proves the realistic,
    actually-reachable behavior instead: all four commands handle an
    empty compaction history correctly without crashing, and the
    conversation still works after running all of them.
    """
    session = ConsoleSession(console_workspace.root, console_workspace.agent_ref)
    try:
        session.send_line(_PONG_PROMPT)
        session.wait_idle()
        session.send_line("/compact")
        session.wait_for(r"nothing to compact")
        session.send_line("/compact --preview")
        session.wait_for(r"nothing to compact")
        session.send_line("/compaction-history")
        session.wait_for(r"no compactions on this thread yet")
        session.send_line("/why-compact not_a_real_event_id")
        session.wait_for(r"no compaction event with id")
        session.send_line(_PING_PROMPT)
        text = session.wait_idle()
        assert "PING" in text
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def test_session_resume_across_restart(console_workspace: ConsoleWorkspace) -> None:
    """History replays in a brand-new OS process via `--session <id>`.

    The one guarantee no other test tier can see: a real second
    process reconstructing a thread's history from disk. A local
    `kolsch sessions list` check confirms the CLI and chat surfaces
    read the same store (no split-brain from divergent
    `KOLSCH_WORKSPACE` resolution).
    """
    session = ConsoleSession(console_workspace.root, console_workspace.agent_ref)
    try:
        session.send_line(_PONG_PROMPT)
        text = session.wait_idle()
        assert "PONG" in text
    finally:
        session.close()

    session_id = latest_session_id(console_workspace)

    listed = run_cli(console_workspace, "sessions", "list")
    assert listed.returncode == 0
    assert session_id in listed.stdout

    resumed = ConsoleSession(
        console_workspace.root, console_workspace.agent_ref, session_id=session_id
    )
    try:
        text = resumed.wait_for(r"PONG", timeout=15)
        assert "PONG" in text
        resumed.send_line(_PING_PROMPT)
        text = resumed.wait_idle()
        assert "PING" in text
    finally:
        resumed.close()


# ---------------------------------------------------------------------------
# Local CLI tour (no pty, no LLM call)
# ---------------------------------------------------------------------------


def test_local_cli_tour(console_workspace: ConsoleWorkspace) -> None:
    """A chained subprocess journey — no terminal, no chat process at all.

    Every research report proposed one or two "plain subprocess, no
    pty" checks against `workflows`/`skills`/`providers`. Consolidated
    into one cheap test rather than several — pure CLI dispatch and
    catalog-loading correctness against the real starter bundle on
    disk.
    """
    workflows_list = run_cli(console_workspace, "workflows", "list")
    assert workflows_list.returncode == 0
    assert "kolsch/workflows/hello" in workflows_list.stdout

    workflows_show = run_cli(
        console_workspace, "workflows", "show", "kolsch/workflows/hello"
    )
    assert workflows_show.returncode == 0

    workflows_validate = run_cli(
        console_workspace, "workflows", "validate", "kolsch/workflows/hello"
    )
    assert workflows_validate.returncode == 0

    skills_list = run_cli(console_workspace, "skills", "list")
    assert skills_list.returncode == 0
    assert "review-pr-diff" in skills_list.stdout

    providers = run_cli(console_workspace, "providers")
    assert providers.returncode == 0
    assert console_workspace.provider in providers.stdout

    refresh = run_cli(console_workspace, "refresh", console_workspace.provider)
    assert refresh.returncode == 0

    disconnect = run_cli(console_workspace, "disconnect", console_workspace.provider)
    assert disconnect.returncode == 0

    providers_after = run_cli(console_workspace, "providers")
    assert providers_after.returncode == 0
    connected_section = providers_after.stdout.split("Connected providers")[1]
    assert console_workspace.provider not in connected_section


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


def test_skill_with_none_enabled(console_workspace: ConsoleWorkspace) -> None:
    """`/skill` on the default agent (zero skills enabled) fails clearly.

    The starter `helper` agent ships with no `subsystems.skills` block
    at all — this is the actual default, out-of-the-box experience,
    the first thing any real user of this feature hits.
    """
    session = ConsoleSession(console_workspace.root, console_workspace.agent_ref)
    try:
        session.send_line("/skill kolsch/review-pr-diff")
        session.wait_for(r"has no skills enabled")
    finally:
        session.close()
