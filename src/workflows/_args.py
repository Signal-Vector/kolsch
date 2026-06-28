"""Shared ``KEY=VALUE`` argument parser for workflow input pairs.

Phase 3.9 Step 0 factored this out of the CLI's inline input-pair parser
so the chat slash handlers (``/workflow``, ``/fork``) can reuse the same
grammar without depending on the CLI module. The chat surface needs a
catchable exception type — :class:`ValueError` — because raising
``SystemExit`` from inside a chat handler would kill the whole REPL.

The CLI call sites wrap :func:`parse_input_pairs` and re-raise as
``SystemExit`` so the ``kolsch workflows run`` shell-exit semantics are
preserved. Chat handlers catch :class:`ValueError` and surface the
message in a slash-error capsule.

Value typing: every parsed value is a string. Downstream Pydantic
strict-mode validation on the workflow's ``inputs`` schema coerces or
rejects. Authors who need a list or dict pass a JSON literal
(``tags=["a","b"]``); Pydantic decodes.
"""

from __future__ import annotations

from typing import Any

__all__ = ["parse_input_pairs"]


def parse_input_pairs(items: list[str]) -> dict[str, Any]:
    """Parse ``KEY=VALUE`` strings into a dict.

    Used by both the CLI ``--input k=v`` form (via ``cli.workflows``)
    and the chat ``with k=v`` form (via
    ``cli._workflow_slash_args``). The grammar is intentionally minimal
    — split on the FIRST ``=``; the right-hand side is taken verbatim.

    Args:
        items: a list of ``"KEY=VALUE"`` strings.

    Returns:
        Dict mapping each key to its raw string value. Annotated as
        ``dict[str, Any]`` (not ``dict[str, str]``) so callers that
        merge JSON-decoded values into the same dict don't fight the
        type-checker.

    Raises:
        ValueError: if any item lacks an ``=``.
    """
    out: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            msg = f"input pair must be KEY=VALUE; got {item!r}"
            raise ValueError(msg)
        key, value = item.split("=", 1)
        out[key] = value
    return out
