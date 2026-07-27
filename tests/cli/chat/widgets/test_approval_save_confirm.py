"""Pilot tests for :class:`ApprovalSaveConfirm`.

Framework-native: mount the widget in isolation via ``mock_app_factory``,
press each bound key via ``Pilot.press``, and assert the ``Chosen``
message was posted with the matching key.

Coverage:

- Preview row renders the string passed to the constructor.
- Preview colour class matches the ``deny`` flag.
- Fallback label appears in the choices row.
- ``y``, ``n``, ``escape`` each post a ``Chosen`` with the mapped key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cli.chat.widgets.approval_save_confirm import ApprovalSaveConfirm

if TYPE_CHECKING:
    from textual.app import ComposeResult


@pytest.mark.asyncio
async def test_preview_and_fallback_label_visible(mock_app_factory):
    widget = ApprovalSaveConfirm(
        preview='authz.allow  bash("ls -la")',
        deny=False,
        fallback_label="allow once",
    )
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        preview = str(widget.query_one(".preview").render())
        assert "authz.allow" in preview
        assert "ls -la" in preview
        choices = str(widget.query_one(".choices").render())
        assert "allow once" in choices


@pytest.mark.asyncio
async def test_deny_flag_marks_preview_class(mock_app_factory):
    widget = ApprovalSaveConfirm(
        preview='authz.deny  bash("rm -rf /")',
        deny=True,
        fallback_label="deny once",
    )
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        preview_static = widget.query_one(".preview")
        assert preview_static.has_class("preview-deny")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key_press", "expected"),
    [
        ("y", "y"),
        ("n", "n"),
        ("escape", "esc"),
    ],
)
async def test_posts_chosen_for_each_key(
    mock_app_factory,
    key_press: str,
    expected: str,
) -> None:
    widget = ApprovalSaveConfirm(
        preview='authz.allow  bash("ls")',
        deny=False,
        fallback_label="allow once",
    )
    app = mock_app_factory(widget)

    seen: list[str] = []

    class _RecordingApp(type(app)):
        def compose(self) -> ComposeResult:
            yield widget

        def on_approval_save_confirm_chosen(
            self,
            message: ApprovalSaveConfirm.Chosen,
        ) -> None:
            seen.append(message.key)

    recording_app = _RecordingApp()
    async with recording_app.run_test() as pilot:
        widget.focus()
        await pilot.pause()
        await pilot.press(key_press)
        await pilot.pause()

    assert seen == [expected]
