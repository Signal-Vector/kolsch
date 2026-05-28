"""Declared-tool discovery — spec §6.1 step 1.

Imports a tool ``.py`` file at registration time (NOT at asset catalog
build — Am-I10 forbids that), finds every ``@tool``-decorated function
on the module, and returns them so the :class:`ToolsSubsystem` can
register them like any other tool.

The asset-management catalog supplies the path (per
:meth:`assets.AssetManagement.get_asset`); discovery is the Tools
subsystem's responsibility per the prompt for Step 10.
"""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING

from errors import ToolSetupFailed
from tools.decorator import ToolDefinition

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


def discover_tools_in_file(path: Path) -> list[Callable[..., Awaitable[object]]]:
    """Import ``path`` and return every ``@tool``-decorated callable.

    Phase 1 simplification: each module is loaded under a synthetic
    name derived from its absolute path so two different bundles with
    the same filename don't collide. The import happens once per
    call; callers wanting persistent registration cache the result.

    Raises :class:`ToolSetupFailed` if the file can't be loaded
    (missing, syntax error, import-time crash) — the typed-PlatformError
    wrapping keeps the engine boundary clean per the handbook hard rule.
    """
    if not path.exists() or not path.is_file():
        raise ToolSetupFailed(
            tool_name=path.stem,
            reason=f"tool file does not exist: {path}",
        )

    # Synthetic module name keyed by absolute path — bundle/foo.py and
    # other-bundle/foo.py are distinct modules in the import system.
    module_name = f"_tools_declared_{abs(hash(str(path.resolve())))}_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ToolSetupFailed(
            tool_name=path.stem,
            reason=f"could not build import spec for {path}",
        )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ToolSetupFailed(
            tool_name=path.stem,
            reason=f"importing {path} raised {type(exc).__name__}: {exc}",
        ) from exc

    found: list[Callable[..., Awaitable[object]]] = []
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if hasattr(attr, "_tool_definition") and isinstance(
            attr._tool_definition,
            ToolDefinition,
        ):
            found.append(attr)
    return found
