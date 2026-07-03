"""Phase 3.9 Step 0 — coverage for shared arg parsers.

Tests the three pure functions added in Step 0:

- :func:`workflows._args.parse_input_pairs` — shared KEY=VALUE parser
  used by CLI ``--input k=v`` AND chat ``with k=v``.
- :func:`cli._workflow_slash_args.parse_slash_workflow_args` —
  splits ``/workflow <ref> [with k=v...]`` into ``(ref, inputs)``.
- :func:`cli._workflow_slash_args.parse_slash_fork_args` — splits
  ``/fork <event_id> [with k=v...]`` into ``(event_id, overrides)``.

The chat surface depends on these raising :class:`ValueError` so the
slash dispatcher can surface a friendly error message; the CLI
adapter (``cli.workflows._input_pairs_or_exit``) translates the
``ValueError`` to ``SystemExit`` to preserve the CLI's exit-on-bad-flag
contract.
"""

from __future__ import annotations

import pytest

from cli._workflow_slash_args import (
    parse_slash_fork_args,
    parse_slash_workflow_args,
)
from workflows._args import parse_input_pairs

# ---------------------------------------------------------------------------
# parse_input_pairs — the shared KEY=VALUE parser
# ---------------------------------------------------------------------------


def test_parse_input_pairs_returns_dict_of_strings() -> None:
    """Multiple KEY=VALUE pairs round-trip to a dict."""
    assert parse_input_pairs(["foo=bar", "baz=qux"]) == {
        "foo": "bar",
        "baz": "qux",
    }


def test_parse_input_pairs_empty_list_returns_empty_dict() -> None:
    """No items → empty dict (not an error — chat ``/workflow ref``
    with no ``with`` clause delegates here with an empty list)."""
    assert parse_input_pairs([]) == {}


def test_parse_input_pairs_value_with_equals_keeps_full_rhs() -> None:
    """Split on the FIRST ``=`` — values may contain ``=`` (e.g., a
    URL query string or base64 payload)."""
    assert parse_input_pairs(["url=https://example.com/?q=foo"]) == {
        "url": "https://example.com/?q=foo",
    }


def test_parse_input_pairs_missing_equals_raises_valueerror() -> None:
    """Missing ``=`` is a parse error — surfaces as ``ValueError``
    so chat handlers can catch it (the CLI adapter re-raises as
    ``SystemExit``)."""
    with pytest.raises(ValueError, match="KEY=VALUE"):
        parse_input_pairs(["foo"])


def test_parse_input_pairs_first_bad_pair_breaks_immediately() -> None:
    """Validation is strict — a single malformed pair aborts the
    parse; downstream pairs are not silently accepted."""
    with pytest.raises(ValueError):
        parse_input_pairs(["foo=bar", "missing-equals", "baz=qux"])


# ---------------------------------------------------------------------------
# parse_slash_workflow_args — ``/workflow <ref> [with k=v...]``
# ---------------------------------------------------------------------------


def test_parse_slash_workflow_args_ref_only() -> None:
    """Bare ref, no ``with`` clause → empty inputs dict."""
    ref, inputs = parse_slash_workflow_args("kolsch/workflows/hello")
    assert ref == "kolsch/workflows/hello"
    assert inputs == {}


def test_parse_slash_workflow_args_with_single_pair() -> None:
    """Single ``with`` pair → ref + one-entry dict."""
    ref, inputs = parse_slash_workflow_args(
        "kolsch/workflows/hello with name=Alice"
    )
    assert ref == "kolsch/workflows/hello"
    assert inputs == {"name": "Alice"}


def test_parse_slash_workflow_args_with_multiple_pairs() -> None:
    """Multiple ``with`` pairs (whitespace-delimited)."""
    ref, inputs = parse_slash_workflow_args(
        "kolsch/workflows/triage with priority=high category=billing"
    )
    assert ref == "kolsch/workflows/triage"
    assert inputs == {"priority": "high", "category": "billing"}


def test_parse_slash_workflow_args_strips_outer_whitespace() -> None:
    """Leading / trailing whitespace from the slash dispatcher is
    tolerated (defense-in-depth — the dispatcher already strips,
    this is belt-and-braces)."""
    ref, inputs = parse_slash_workflow_args(
        "   kolsch/workflows/hello with name=Alice   "
    )
    assert ref == "kolsch/workflows/hello"
    assert inputs == {"name": "Alice"}


def test_parse_slash_workflow_args_empty_text_raises() -> None:
    """Empty text → ValueError with documented message."""
    with pytest.raises(ValueError, match="requires a workflow ref"):
        parse_slash_workflow_args("")


def test_parse_slash_workflow_args_with_no_ref_raises() -> None:
    """``with foo=bar`` without a ref → ValueError."""
    with pytest.raises(ValueError, match="before 'with'"):
        parse_slash_workflow_args(" with foo=bar")


def test_parse_slash_workflow_args_bad_pair_raises() -> None:
    """A malformed pair in the ``with`` clause surfaces the underlying
    ``parse_input_pairs`` error."""
    with pytest.raises(ValueError, match="KEY=VALUE"):
        parse_slash_workflow_args(
            "kolsch/workflows/hello with name=Alice missing-equals"
        )


def test_parse_slash_workflow_args_splits_with_only_once() -> None:
    """A pair value containing the literal substring ``with`` does
    NOT re-split (split on the FIRST ``" with "`` only)."""
    ref, inputs = parse_slash_workflow_args(
        "kolsch/workflows/hello with message=hello"
    )
    # Only the first ' with ' separated ref + clause; ``message=hello``
    # is intact (no spurious re-split on the substring inside the
    # value would survive because there's no literal ' with ' inside).
    assert ref == "kolsch/workflows/hello"
    assert inputs == {"message": "hello"}


# ---------------------------------------------------------------------------
# parse_slash_fork_args — ``/fork <event_id> [with k=v...]``
# ---------------------------------------------------------------------------


def test_parse_slash_fork_args_event_only() -> None:
    """Bare event_id, no ``with`` clause → empty overrides dict."""
    event_id, overrides = parse_slash_fork_args("ev_abcd1234")
    assert event_id == "ev_abcd1234"
    assert overrides == {}


def test_parse_slash_fork_args_with_overrides() -> None:
    """Event_id + one or more overrides."""
    event_id, overrides = parse_slash_fork_args(
        "ev_abcd1234 with check_cmd=false retries=3"
    )
    assert event_id == "ev_abcd1234"
    assert overrides == {"check_cmd": "false", "retries": "3"}


def test_parse_slash_fork_args_empty_text_raises() -> None:
    with pytest.raises(ValueError, match="requires an event_id"):
        parse_slash_fork_args("")


def test_parse_slash_fork_args_with_no_event_id_raises() -> None:
    with pytest.raises(ValueError, match="before 'with'"):
        parse_slash_fork_args(" with override=foo")


def test_parse_slash_fork_args_bad_pair_raises() -> None:
    with pytest.raises(ValueError, match="KEY=VALUE"):
        parse_slash_fork_args("ev_abcd1234 with override=foo bad-no-equals")


def test_parse_slash_fork_args_event_id_is_taken_verbatim() -> None:
    """Step 0 does NOT validate event_id shape — the
    ``WorkflowRunStore`` lookup downstream is the source of truth
    for "does this event exist". Garbage in, ``ValueError`` later
    from the store."""
    event_id, overrides = parse_slash_fork_args("not-an-event-id")
    assert event_id == "not-an-event-id"
    assert overrides == {}
