"""Workflow test helpers — Phase 3.0 Step 7 (Round 4 item #1).

Ships :func:`make_workflow` — a Pydantic-model construction helper
that produces a minimal valid :class:`workflows.parser.Workflow` with
optional keyword overrides. The canonical-minimum is:

::

    {
        "nodes": {"start": {"type": "kolsch/nodes/noop"}},
        "graph": {"edges": []},
    }

Callers override fields shallowly via keyword args:

::

    wf = make_workflow(
        nodes={"a": {"type": "kolsch/nodes/bash", "command": "echo"}},
    )

Used by validator / parser unit tests that need a workflow without
round-tripping YAML on disk. Round 7 A7-1 also cross-references
``NodeContext.for_test(**overrides)`` as the test-pattern entry point
for tests that need a :class:`workflows.NodeContext`.
"""

from __future__ import annotations

from typing import Any

from workflows.parser import Workflow

__all__ = ["make_workflow"]


_DEFAULT_PAYLOAD: dict[str, Any] = {
    "nodes": {"start": {"type": "kolsch/nodes/noop"}},
    "graph": {"edges": []},
}


def make_workflow(**overrides: Any) -> Workflow:
    """Build a minimal valid :class:`Workflow` with optional overrides.

    Top-level overrides REPLACE the default at the corresponding key
    (shallow merge). For example, passing ``nodes={"foo": {...}}``
    replaces the default ``{"start": ...}`` mapping. Passing
    ``graph=None`` makes the result a node library.
    """
    payload = {**_DEFAULT_PAYLOAD, **overrides}
    return Workflow.model_validate(payload)
