"""Authored Python `@node` returning `tuple[bool, BaseModel]`.

Demonstrates the branchable authored-Python return shape that drives
runtime edge dispatch off the boolean while preserving the
`BaseModel`'s fields as captured output for downstream consumers.

The function classifies an input string as urgent or normal,
returns the urgency flag for branch dispatch, and surfaces a small
structured payload alongside.
"""

from __future__ import annotations

from pydantic import BaseModel

from workflows import NodeContext, node


class ClassifyInputs(BaseModel):
    """Carries the text the classifier inspects."""

    text: str


class ClassifyOutputs(BaseModel):
    """Captured alongside the branchable bool.

    Downstream nodes can consume `category` / `score` via
    `{{ nodes.<name>.X }}`.
    """

    category: str
    score: float


@node(label="Classify")
async def run(inputs: ClassifyInputs, ctx: NodeContext) -> tuple[bool, ClassifyOutputs]:
    """Return `(is_urgent, structured_payload)`.

    The first element drives branch dispatch — outgoing edges with
    `branch: true` fire when `is_urgent` is `True`, `branch: false`
    arms fire otherwise. The catalog node runner stores the captured
    output as `{"value": <bool>, **payload.model_dump()}` so
    downstream consumers can still reference
    `{{ nodes.<name>.category }}` / `score`.
    """
    _ = ctx
    urgent = "urgent" in inputs.text.lower()
    return urgent, ClassifyOutputs(
        category="urgent" if urgent else "normal",
        score=0.95 if urgent else 0.1,
    )
