"""spec-002 Test 3 — ``src/schemas/wire_kinds.py`` constants + helper.

Pins:

- Each of the 15 platform constants equals its canonical wire string.
- :data:`ALL_PLATFORM_KINDS` is exactly the 15-member frozenset.
- :func:`is_valid_kind` returns True / False across the 7 edge cases
  named in spec-002 AC #8 (plus a bundle-prefixed pass case).
"""

from __future__ import annotations

from schemas.wire_kinds import (
    ALL_PLATFORM_KINDS,
    WIRE_CARD_APPENDED,
    WIRE_CARD_CLOSED,
    WIRE_CARD_NOTICE,
    WIRE_CARD_OPENED,
    WIRE_CARD_SET,
    WIRE_CARD_SET_PROGRESS,
    WIRE_CARD_SNAPSHOT,
    WIRE_CARD_STATUS,
    WIRE_PROMPT_ANSWERED,
    WIRE_PROMPT_CANCELLED,
    WIRE_PROMPT_REQUESTED,
    WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED,
    WIRE_WORKFLOW_NODE_AWAITING_APPROVAL,
    WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
    WIRE_WORKFLOW_PROGRESS,
    is_valid_kind,
)


def test_card_constants_have_canonical_values() -> None:
    """Every card constant equals its wire string exactly. The
    wire-kind module is the source of truth (roadmap invariant #2);
    consumers import these constants rather than raw strings."""
    assert WIRE_CARD_OPENED == "kolsch.card.opened"
    assert WIRE_CARD_APPENDED == "kolsch.card.appended"
    assert WIRE_CARD_SET == "kolsch.card.set"
    assert WIRE_CARD_SET_PROGRESS == "kolsch.card.set_progress"
    assert WIRE_CARD_STATUS == "kolsch.card.status"
    assert WIRE_CARD_CLOSED == "kolsch.card.closed"
    assert WIRE_CARD_SNAPSHOT == "kolsch.card.snapshot"
    assert WIRE_CARD_NOTICE == "kolsch.card.notice"


def test_prompt_constants_have_canonical_values() -> None:
    """Every prompt constant equals its wire string exactly."""
    assert WIRE_PROMPT_REQUESTED == "kolsch.prompt.requested"
    assert WIRE_PROMPT_ANSWERED == "kolsch.prompt.answered"
    assert WIRE_PROMPT_CANCELLED == "kolsch.prompt.cancelled"


def test_workflow_ui_constant_has_canonical_value() -> None:
    """The one workflow-UI kind emitted by WorkflowsImpl."""
    assert WIRE_WORKFLOW_PROGRESS == "kolsch.workflow.progress"


def test_workflow_node_sub_agent_event_has_canonical_value() -> None:
    """spec-020 — the workflow → sub-agent forwarder wire kind."""
    assert (
        WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT
        == "kolsch.workflow.node.sub_agent_event"
    )


def test_all_platform_kinds_has_exactly_fifteen_members() -> None:
    """The aggregate frozenset is exactly the 15 platform constants —
    no duplicates, no extras, no drift. (spec-014 added the two
    workflow-node approval-lifecycle kinds; spec-020 added the
    sub-agent event forwarder kind.)"""
    assert len(ALL_PLATFORM_KINDS) == 15
    assert ALL_PLATFORM_KINDS == frozenset(
        {
            WIRE_CARD_OPENED,
            WIRE_CARD_APPENDED,
            WIRE_CARD_SET,
            WIRE_CARD_SET_PROGRESS,
            WIRE_CARD_STATUS,
            WIRE_CARD_CLOSED,
            WIRE_CARD_SNAPSHOT,
            WIRE_CARD_NOTICE,
            WIRE_PROMPT_REQUESTED,
            WIRE_PROMPT_ANSWERED,
            WIRE_PROMPT_CANCELLED,
            WIRE_WORKFLOW_PROGRESS,
            WIRE_WORKFLOW_NODE_AWAITING_APPROVAL,
            WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED,
            WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
        }
    )


def test_is_valid_kind_accepts_canonical_three_component() -> None:
    assert is_valid_kind("kolsch.card.opened") is True


def test_is_valid_kind_rejects_single_component() -> None:
    assert is_valid_kind("foo") is False


def test_is_valid_kind_rejects_two_component() -> None:
    """Two components (one dot) is one shy of the minimum."""
    assert is_valid_kind("kolsch.card") is False


def test_is_valid_kind_rejects_empty_first_component() -> None:
    """A leading dot yields an empty first component; rejected."""
    assert is_valid_kind(".card.opened") is False


def test_is_valid_kind_rejects_empty_last_component() -> None:
    """A trailing dot yields an empty last component; rejected."""
    assert is_valid_kind("kolsch.card.") is False


def test_is_valid_kind_rejects_leading_digit_component() -> None:
    """``isidentifier`` rejects components whose first char is a digit
    — locks the invariant that wire kinds are Pythonic verbs."""
    assert is_valid_kind("kolsch.card.12invalid") is False


def test_is_valid_kind_accepts_bundle_prefixed_kind() -> None:
    """Bundle-authored kinds are legal at emit time even though they're
    not in :data:`ALL_PLATFORM_KINDS`. Membership checks happen at
    dispatch time (see spec-002 §Technical Context)."""
    assert is_valid_kind("acme.mycard.opened") is True
