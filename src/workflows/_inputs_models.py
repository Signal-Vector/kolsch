"""Receiver `Inputs(BaseModel)` lookup.

Maps a parsed `NodeDef` to the Pydantic `Inputs` model the executor
validates the rendered `bound_inputs` dict against under `strict=True`.

Built-ins dispatch by `node_def.type` against the locked table below.
Authored Python `@node` modules defer to the
[`CatalogNodeRunner`][workflows.builtins.catalog_node_runner.CatalogNodeRunner]'s
introspected input model — the lookup threads through the optional
`catalog_runner` kwarg the executor passes in.
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
    """Return the receiver's typed `Inputs(BaseModel)` model, or `None`.

    Built-ins dispatch by `node_def.type` against the locked table
    above. Authored Python `@node` modules defer to `catalog_runner`
    (typically a
    [`CatalogNodeRunner`][workflows.builtins.catalog_node_runner.CatalogNodeRunner])
    whose `inputs_model_for` method imports the on-disk module and
    introspects the `@node` `run` signature for the first parameter's
    Pydantic annotation.

    Returns `None` when:

    - The `node_def` has no `type:` attribute (`copy_of:` nodes whose
      run-start resolution hasn't landed yet).
    - The catalog runner is absent (callers that don't wire
      authored-Python validation — bash-only tests, scripts).
    - The catalog runner's introspection returned no Pydantic input
      model (the `@node` parameter has no annotation or isn't a
      `BaseModel` subclass; the validator already rejected
      non-conforming signatures at run-start).

    Args:
        node_def: The parsed node definition.
        catalog_runner: Optional runner whose `inputs_model_for` is
            consulted for authored Python nodes.

    Returns:
        The Pydantic `BaseModel` subclass to validate `bound_inputs`
        against, or `None` if no validation applies.
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
