"""Starter authored-Python node — Phase 3.0 Step 7.

The minimum-viable ``@node``-decorated module: a single ``run``
function with a Pydantic input model, a Pydantic output model, and
the ``@node`` decorator applied. The Phase 3.0 validator's §7.6
introspection check passes against this shape; the Phase 3.1
executor will invoke it.

This file is intentionally small. Authors copy it as a template for
new nodes inside their own bundles.
"""

from __future__ import annotations

from pydantic import BaseModel

from workflows import NodeContext, node


class EchoInputs(BaseModel):
    """Carries the upstream message verbatim."""

    message: str


class EchoOutputs(BaseModel):
    """Returns the message unchanged."""

    message: str


@node(label="Echo")
async def run(inputs: EchoInputs, ctx: NodeContext) -> EchoOutputs:
    """Echo the input message to the output.

    The starter node performs no I/O beyond the trivial passthrough;
    it exists so the workflow validator has a concrete
    ``@node``-decorated module to introspect on a fresh workspace.
    """
    _ = ctx
    return EchoOutputs(message=inputs.message)
