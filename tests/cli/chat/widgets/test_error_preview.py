"""spec-013 Test 5 — Error rendering on `WorkflowNodeRow`.

REWRITTEN for bug-063:

Pre-bug-063, this file encoded the pre-bug-063 contract: the error was
tail-appended (truncated to 60 chars) onto ``row.title``. bug-063
moves the full, un-re-truncated error into a first-class Error
:class:`~cli.chat.widgets.node_section.NodeSection` body — the title
carries only a short ``✗ <node>`` marker.

Two passes (new contract):

1. Long error (>60 chars) — appears IN FULL in the Error section body
   and is NOT in the row title.
2. Short error (<60 chars) — appears in the Error section body; still
   not tail-appended to the title.

The row's ``_truncate_preview`` helper is retained on the class for
now (it's unused in production; the test in this file used to poke
it directly). We drop those direct assertions so a future cleanup
that deletes the helper doesn't break this file.
"""

from __future__ import annotations

import pytest
from textual.widgets import Collapsible, Static

from cli.chat.widgets.node_section import NodeSection
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow


def _error_section_body(row: WorkflowNodeRow) -> str:
    contents = row.query_one(Collapsible.Contents)
    sections = list(contents.query(NodeSection))
    error_sections = [s for s in sections if "error" in str(s.title)]
    assert len(error_sections) == 1, (
        f"expected exactly one Error section; got titles="
        f"{[str(s.title) for s in sections]!r}"
    )
    return "\n".join(
        str(getattr(child, "content", "") or child.render())
        for child in error_sections[0].walk_children()
        if isinstance(child, Static)
    )


@pytest.mark.asyncio
async def test_long_error_message_full_in_error_section_body(
    mock_app_factory,
) -> None:
    """A 200-char error appears IN FULL in the Error section body.

    bug-063 contract: no re-truncation. The pre-bug-063 60-char cap
    only applied to the title; the Error section body carries the
    full ``_error_preview`` string.
    """
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        long_msg = "E" * 200
        row.set_state("failed", error_preview=long_msg)
        await pilot.pause()

        assert row.state == "failed"
        # Title: short marker only.
        assert "✗" in row.title
        assert "n" in row.title
        assert long_msg not in row.title
        # No "..." truncation suffix anywhere in the title.
        assert "..." not in row.title

        # Body: full error, un-re-truncated.
        body = _error_section_body(row)
        assert long_msg in body
        # And its full length survives.
        assert len(long_msg) == 200
        assert body.count("E") >= 200


@pytest.mark.asyncio
async def test_short_error_message_in_error_section_body(
    mock_app_factory,
) -> None:
    """A short error appears in the Error section body (not the title)."""
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        short_msg = "boom: filesystem read denied"  # 28 chars
        assert len(short_msg) < 60
        row.set_state("failed", error_preview=short_msg)
        await pilot.pause()

        assert row.state == "failed"
        # Short marker in title; the error text itself is NOT in title.
        assert "✗" in row.title
        assert short_msg not in row.title

        body = _error_section_body(row)
        assert short_msg in body
