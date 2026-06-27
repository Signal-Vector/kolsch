"""Receiver ``Inputs(BaseModel)`` lookup — Phase 3.3 Steps 0 + 1.

Maps a parsed ``NodeDef`` to the Pydantic ``Inputs`` model the
executor validates the rendered ``bound_inputs`` dict against under
``strict=True`` (master spec §6.4 + Phase 3.3 build doc Step 0 / Step 1).

Built-ins dispatch by ``node_def.type`` against the locked table
below. Authored Python ``@node`` modules defer to the
:class:`workflows.builtins.catalog_node_runner.CatalogNodeRunner`'s
introspected input model — Step 1 wires the lookup via the optional
``catalog_runner`` kwarg threaded by the executor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from workflows.parser import (
    AgentInputs,
    BashInputs,
    BranchInputs,
    ComputeInputs,
    DeleteStateInputs,
    FilterInputs,
    ListFilesInputs,
    MapInputs,
    NoopInputs,
    ReadFileInputs,
    SetStateInputs,
    UserInputInputs,
    WorkflowSubInputs,
    WriteFileInputs,
)

if TYPE_CHECKING:
    from pydantic import BaseModel


__all__ = ["inputs_model_for"]


_BUILTIN_TO_INPUTS: dict[str, type] = {
    "kolsch/nodes/bash": BashInputs,
    "kolsch/nodes/agent": AgentInputs,
    "kolsch/nodes/noop": NoopInputs,
    "kolsch/nodes/user_input": UserInputInputs,
    "kolsch/nodes/read_file": ReadFileInputs,
    "kolsch/nodes/write_file": WriteFileInputs,
    "kolsch/nodes/list_files": ListFilesInputs,
    "kolsch/nodes/set_state": SetStateInputs,
    "kolsch/nodes/delete_state": DeleteStateInputs,
    "kolsch/nodes/filter": FilterInputs,
    "kolsch/nodes/compute": ComputeInputs,
    "kolsch/nodes/workflow": WorkflowSubInputs,
    "kolsch/nodes/map": MapInputs,
    "kolsch/nodes/branch": BranchInputs,
}


def inputs_model_for(
    node_def: object,
    *,
    catalog_runner: object | None = None,
) -> type[BaseModel] | None:
    """Return the receiver's typed ``Inputs(BaseModel)`` model, or
    ``None`` when no runtime strict-mode validation applies.

    Built-ins dispatch by ``node_def.type`` against the locked table
    above. Authored Python ``@node`` modules defer to ``catalog_runner``
    (typically a
    :class:`workflows.builtins.catalog_node_runner.CatalogNodeRunner`)
    whose ``inputs_model_for`` method imports the on-disk module +
    introspects the ``@node`` ``run`` signature for the first
    parameter's Pydantic annotation.

    Returns ``None`` when:

    - The ``node_def`` has no ``type:`` attribute (``copy_of:`` nodes
      whose run-start resolution hasn't landed yet).
    - The catalog runner is absent (callers that don't wire
      authored-Python validation — bash-only tests, scripts).
    - The catalog runner's introspection returned no Pydantic input
      model (the ``@node`` parameter has no annotation or isn't a
      ``BaseModel`` subclass; the validator already rejected
      non-conforming signatures at run-start).
    """
    type_ref = getattr(node_def, "type", None)
    if not isinstance(type_ref, str):
        return None
    builtin = _BUILTIN_TO_INPUTS.get(type_ref)
    if builtin is not None:
        return builtin
    if catalog_runner is None:
        return None
    lookup = getattr(catalog_runner, "inputs_model_for", None)
    if lookup is None:
        return None
    return lookup(type_ref)
