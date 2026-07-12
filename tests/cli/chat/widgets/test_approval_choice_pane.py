"""Pilot tests for `ApprovalChoicePane`.

Framework-native: mount the widget in isolation via `mock_app_factory`,
press each bound key via `Pilot.press`, and assert the `Chosen`
message was posted with the matching key.

See process/spec-textual-chat-ui.md → Technical Context → Approval
flow → ApprovalChoicePane for the widget's contract.
"""

from __future__ import annotations

import pytest

from cli.chat.widgets.approval_choice_pane import ApprovalChoicePane


@pytest.mark.asyncio
async def test_pane_shows_all_four_choices_and_prompt(mock_app_factory):
    """The pane renders a single choices line + a prompt cursor line.

    The choices row is one ``Static`` (rendered ``markup=False`` so
    brackets stay literal), and a second ``Static`` shows the ``› ▊``
    prompt affordance — every label must be present in the choices
    line. The earlier one-Static-per-choice layout clipped to just
    ``[y]`` on narrow terminals; this test guards the fix.
    """
    pane = ApprovalChoicePane()
    app = mock_app_factory(pane)
    async with app.run_test() as pilot:
        await pilot.pause()
        choices_text = str(pane.query_one(".choices").render())
        prompt_text = str(pane.query_one(".prompt").render())
        for label in ("[y] allow once", "[a] allow + rule", "[n] deny once", "[d] deny + rule"):
            assert label in choices_text, f"missing {label!r} from choices row"
        assert "›" in prompt_text
        assert "▊" in prompt_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key_press", "expected"),
    [
        ("y", "y"),
        ("a", "a"),
        ("n", "n"),
        ("d", "d"),
        ("escape", "esc"),
    ],
)
async def test_pane_posts_chosen_message_for_each_key(
    mock_app_factory, key_press: str, expected: str
) -> None:
    """Each bound key posts a `Chosen(key=...)` message with the mapped key.

    Escape maps to `"esc"` (not `"n"`) so the parent `ApprovalBlock`
    can distinguish an explicit deny from an abandoned rule write.
    """
    pane = ApprovalChoicePane()
    app = mock_app_factory(pane)

    seen: list[str] = []

    class _Host(type(app)):
        def on_approval_choice_pane_chosen(
            self, message: ApprovalChoicePane.Chosen
        ) -> None:
            seen.append(message.key)

    # Rebuild with the recording host subclass so the app-level
    # handler receives the bubbled message.
    from textual.app import ComposeResult

    class _RecordingApp(_Host):
        def compose(self) -> ComposeResult:
            yield pane

    recording_app = _RecordingApp()

    async with recording_app.run_test() as pilot:
        pane.focus()
        await pilot.pause()
        await pilot.press(key_press)
        await pilot.pause()

    assert seen == [expected]
