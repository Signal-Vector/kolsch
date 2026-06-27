"""Receiver ``Inputs(BaseModel)`` lookup — Phase 3.3 Step 0.

Maps a parsed ``NodeDef`` to the Pydantic ``Inputs`` model the
executor validates the rendered ``bound_inputs`` dict against under
``strict=True`` (master spec §6.4 + Phase 3.3 build doc Step 0).

Authored Python ``@node`` modules return ``None`` here in Step 0 —
Step 1 of Phase 3.3 wires the
:class:`workflows.builtins.catalog_node_runner.CatalogNodeRunner`'s
introspected input model in alongside the branching dispatcher work.
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


def inputs_model_for(node_def: object) -> type[BaseModel] | None:
    """Return the receiver's typed ``Inputs(BaseModel)`` model, or
    ``None`` when no runtime strict-mode validation applies yet.

    Built-ins dispatch by ``node_def.type`` against the locked table
    above. Authored Python ``@node`` modules return ``None`` here —
    Phase 3.3 Step 1 wires the
    :class:`workflows.builtins.catalog_node_runner.CatalogNodeRunner`
    introspected input model alongside the branching dispatcher. The
    catalog runner ALREADY validates ``bound_inputs`` against the
    declared input model at run time (non-strict), so the Step 0 gap
    doesn't leave authored nodes wholly unchecked — only the strict
    contract waits one step.

    ``copy_of`` nodes resolve their ``type:`` at run-start; until that
    machinery lands they fall through to ``None``.
    """
    type_ref = getattr(node_def, "type", None)
    if not isinstance(type_ref, str):
        return None
    return _BUILTIN_TO_INPUTS.get(type_ref)
