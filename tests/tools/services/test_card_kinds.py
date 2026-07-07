"""spec-005 Test 2 — kind registry entries.

Pins the four v1 kinds and their exact `allowed_ops` frozensets. The
registry is a v1 platform invariant (closed set); these asserts stop
a future refactor from silently opening the set or dropping an op
the widget layer relies on.
"""

from __future__ import annotations

from typing import get_args

import pytest

from tools.services import (
    CARD_KIND_REGISTRY,
    CardKindLiteral,
    UnknownCardKind,
    get_kind_spec,
)


def test_registry_has_exactly_four_named_entries() -> None:
    """v1 closed set: exactly `markdown`, `progress`, `confirm`, `workflow`."""
    assert set(CARD_KIND_REGISTRY) == {
        "markdown",
        "progress",
        "confirm",
        "workflow",
    }
    assert len(CARD_KIND_REGISTRY) == 4


def test_markdown_allowed_ops() -> None:
    """`markdown` supports content-oriented ops; no `set_progress`."""
    ops = get_kind_spec("markdown").allowed_ops
    assert "append" in ops
    assert "set" in ops
    assert "set_status" in ops
    assert "set_title" in ops
    assert "close" in ops
    assert "set_progress" not in ops


def test_progress_allowed_ops() -> None:
    """`progress` supports `set_progress`; no free-form `append`."""
    ops = get_kind_spec("progress").allowed_ops
    assert "set_progress" in ops
    assert "append" not in ops


def test_confirm_allowed_ops_is_exactly_set_and_close() -> None:
    """r1 product cut removed `prompt_choice` sugar (AC 5).

    Tools drive confirm cards via `Prompts.choice` directly — the
    handle exposes only `set` (paint the question) and `close`.
    """
    assert get_kind_spec("confirm").allowed_ops == frozenset({"set", "close"})


def test_workflow_allowed_ops_includes_set_progress() -> None:
    """r3 BLOCKER fix (AC 4) — required by WorkflowRenderer header progress bar."""
    ops = get_kind_spec("workflow").allowed_ops
    assert "set_progress" in ops
    # Sanity — the full set the WorkflowRenderer drives.
    assert ops == frozenset(
        {
            "append",
            "set",
            "set_progress",
            "set_status",
            "set_title",
            "close",
        },
    )


def test_workflow_header_glyph_includes_queued_and_cancelling() -> None:
    """Workflow has states no other kind carries — `queued` + `cancelling`."""
    glyphs = get_kind_spec("workflow").header_glyph_by_status
    assert glyphs["queued"] == "⋯"
    assert glyphs["cancelling"] == "⧗"


def test_get_kind_spec_unknown_raises_with_helpful_message() -> None:
    """AC 7 — the miss message must guide the caller.

    Sorted known list + issue-template URL, so a tool author who
    typo'd a kind (or needs a new one) has an actionable next step.
    """
    with pytest.raises(UnknownCardKind) as exc_info:
        get_kind_spec("unknown_kind")
    msg = str(exc_info.value)
    # Sorted known list (all four names present).
    assert "'markdown'" in msg
    assert "'progress'" in msg
    assert "'confirm'" in msg
    assert "'workflow'" in msg
    # Issue-template pointer.
    assert "issues/new" in msg


def test_card_kind_literal_members() -> None:
    """`CardKindLiteral` matches the registry keys.

    A single source of truth: the mypy Literal and the runtime
    registry must not drift apart.
    """
    literal_members = set(get_args(CardKindLiteral))
    assert literal_members == {"markdown", "progress", "confirm", "workflow"}
    assert literal_members == set(CARD_KIND_REGISTRY)
