"""Argument parsers for `/workflow` and `/fork` chat slash commands.

Pure parsing, no I/O. Both parsers operate on the free-form text
*after* the slash keyword (the dispatch layer strips the `/workflow `
/ `/fork ` prefix before calling).

The grammar is intentionally minimal:

- `<ref> [with k1=v1 k2=v2 ...]` for `/workflow`.
- `<event_id> [with k1=v1 ...]` for `/fork`.

The `with` separator is whitespace-bounded — split on the first
occurrence of `" with "` only. Values cannot contain spaces (no
shell-style quoting); operators with prose values pass JSON literals
or pre-bind the value through workflow inputs at runtime.

Errors raise `ValueError`; the slash dispatcher surfaces them as
slash-error capsules in chat.
"""

from __future__ import annotations

from typing import Any

from workflows._args import parse_input_pairs

__all__ = ["parse_slash_workflow_args", "parse_slash_fork_args"]


_WITH_SEPARATOR = " with "


def parse_slash_workflow_args(text: str) -> tuple[str, dict[str, Any]]:
    """Parse the args after `/workflow` into `(ref, inputs)`.

    The caller has already stripped the leading `/workflow ` so
    `text` is the remainder (e.g., `"kolsch/workflows/hello with
    name=Alice"` or just `"kolsch/workflows/hello"`).

    Returns:
        `(ref, inputs)` — `ref` is the workflow catalog ref (a
        non-empty string); `inputs` is a dict of bound inputs
        (possibly empty).

    Raises:
        ValueError: Missing ref, empty ref before `with`, or any
            malformed input pair raised by
            [`parse_input_pairs`][workflows._args.parse_input_pairs].
    """
    body = text.strip()
    if not body:
        msg = "'/workflow' requires a workflow ref"
        raise ValueError(msg)
    if body == "with" or body.startswith("with "):
        # Operator typed `/workflow with foo=bar` — no ref. After the
        # outer .strip() the leading separator was eaten so the naive
        # split below would treat `with foo=bar` as the ref.
        msg = "'/workflow' requires a workflow ref before 'with'"
        raise ValueError(msg)
    parts = body.split(_WITH_SEPARATOR, 1)
    ref = parts[0].strip()
    if not ref:
        msg = "'/workflow' requires a workflow ref before 'with'"
        raise ValueError(msg)
    inputs: dict[str, Any] = {}
    if len(parts) == 2:
        pairs = parts[1].split()
        inputs = parse_input_pairs(pairs)
    return ref, inputs


def parse_slash_fork_args(text: str) -> tuple[str, dict[str, Any]]:
    """Parse the args after `/fork` into `(event_id, overrides)`.

    The caller has already stripped the leading `/fork ` so `text` is
    the remainder (e.g., `"ev_abcd1234 with check_cmd=false"`).

    Returns:
        `(event_id, overrides)` — `event_id` is the user-supplied
        event id verbatim (no validation here; the `WorkflowRunStore`
        lookup downstream surfaces a clean error for typos / missing
        events); `overrides` is a dict of input overrides patched
        into the forked run's pre-fork output (possibly empty).

    Raises:
        ValueError: Missing event_id, empty event_id before `with`,
            or any malformed override pair.
    """
    body = text.strip()
    if not body:
        msg = "'/fork' requires an event_id"
        raise ValueError(msg)
    if body == "with" or body.startswith("with "):
        msg = "'/fork' requires an event_id before 'with'"
        raise ValueError(msg)
    parts = body.split(_WITH_SEPARATOR, 1)
    event_id = parts[0].strip()
    if not event_id:
        msg = "'/fork' requires an event_id before 'with'"
        raise ValueError(msg)
    overrides: dict[str, Any] = {}
    if len(parts) == 2:
        pairs = parts[1].split()
        overrides = parse_input_pairs(pairs)
    return event_id, overrides
