"""Generic ``CatalogNodeDef`` + 3-seg ``type:`` dispatcher — master spec §3.1.

Per Round 4 item #2 — standalone module hosting the catalog-ref
dispatcher (NOT folded into :mod:`workflows.parser`). The parser
imports :data:`BUILTIN_TYPE_NAMES`, :func:`builtin_name_for_ref`,
and :class:`CatalogNodeDef` from here.

The 14 RESERVED catalog slots ``kolsch/nodes/<name>`` belong to the
built-in node types declared in :mod:`workflows.parser`. Authored
Python nodes live at any other ``<bundle>/nodes/<name>``; parse-time
they land in :class:`CatalogNodeDef` carrying only the common modifier
fields. Their type-specific config is validated against the node's
declared input model AFTER catalog lookup at the run-start gate
(Step 5).
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from assets.refs import is_three_segment, parse_three_segment

__all__ = [
    "BUILTIN_TYPE_NAMES",
    "CatalogNodeDef",
    "builtin_name_for_ref",
    "is_builtin_ref",
]


# The 14 reserved built-in catalog slots — master spec §4 +
# Step 2 build doc. ``kolsch/nodes/<name>`` for each ``<name>`` here
# is owned by the built-in node-type model (e.g., ``BashNodeDef``);
# a user authoring ``bundles/kolsch/nodes/bash.py`` collides with
# the reserved slot at catalog scan time (``WorkflowBuiltinShadowed``,
# raised in Step 3).
BUILTIN_TYPE_NAMES: frozenset[str] = frozenset(
    {
        "agent",
        "bash",
        "branch",
        "compute",
        "delete_state",
        "filter",
        "list_files",
        "map",
        "noop",
        "read_file",
        "set_state",
        "user_input",
        "workflow",
        "write_file",
    }
)


def is_builtin_ref(type_ref: str) -> bool:
    """``True`` iff ``type_ref`` is a reserved built-in 3-seg slot
    (``kolsch/nodes/<name>`` for one of the 14 built-in names).
    """
    name = builtin_name_for_ref(type_ref)
    return name is not None


def builtin_name_for_ref(type_ref: str) -> str | None:
    """Return the built-in name (``"bash"``) when ``type_ref`` is one
    of the 14 reserved ``kolsch/nodes/<name>`` slots; ``None``
    otherwise.

    Returns ``None`` for non-3-seg refs, non-``kolsch`` bundles, and
    3-seg refs whose final segment is not in :data:`BUILTIN_TYPE_NAMES`.
    """
    if not isinstance(type_ref, str) or not is_three_segment(type_ref):
        return None
    bundle, segment, name = parse_three_segment(type_ref)
    if bundle != "kolsch" or segment != "nodes":
        return None
    if name not in BUILTIN_TYPE_NAMES:
        return None
    return name


class CatalogNodeDef(BaseModel):
    """Generic parser variant for ``type: <bundle>/nodes/<name>`` refs
    that are NOT reserved built-in slots.

    Round 4 item #3 — *Def symmetry with ``BashNodeDef`` /
    ``MapNodeDef`` / etc. The model carries only the common modifier
    fields per Step 2 spec; type-specific config is validated against
    the node's declared input model AFTER catalog lookup at the
    run-start gate (Step 5).

    The ``type:`` field IS the catalog ref string. There is no ``body:``
    wrapper — the ref alone is the discriminator.

    ``model_config = ConfigDict(extra="allow")`` so type-specific
    config fields land on the model untouched; Step 5 inspects them
    against the loaded node's Pydantic input model.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    type: str
    description: str | None = Field(default=None, min_length=1)
    timeout_seconds: int | None = None
    when: Any = None
    retry: int = 0
    secrets: list[str] = Field(default_factory=list)
    copy_of: str | None = None
