"""Declared-tool discovery — file import at registration time.

Imports a tool `.py` file at registration time (not at asset-catalog
build — asset discovery stays read-only of Python), finds every
`@tool`-decorated function on the module, and returns them so the
[`ToolsSubsystem`][tools.subsystem.ToolsSubsystem] can register them
like any other tool. The asset-management catalog supplies the path;
discovery is the Tools subsystem's responsibility.
"""

from __future__ import annotations

import importlib.util
import sys
from typing import TYPE_CHECKING, Any

from errors import ToolSetupFailed
from tools.decorator import ToolDefinition

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


def discover_tools_in_file(path: Path) -> list[Callable[..., Awaitable[Any]]]:
    """Import `path` and return every `@tool`-decorated callable.

    Each module is loaded under a synthetic name derived from its
    absolute path so two different bundles with the same filename
    don't collide. The import happens once per call; callers wanting
    persistent registration cache the result.

    Args:
        path: Absolute path to a `.py` file declaring `@tool` functions.

    Returns:
        Every `@tool`-decorated callable found on the imported module.

    Raises:
        ToolSetupFailed: The file is missing, has a syntax error, or
            its import raises — wrapped as a typed `PlatformError` so
            the engine boundary stays clean.
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
    # Register in sys.modules BEFORE exec_module so Pydantic's model_rebuild
    # (called from `model_json_schema()` at tool-registration time) can resolve
    # string annotations like `Literal[...]` from `sys.modules[cls.__module__]`.
    # Mirrors the pattern in builtins.py:323 / :375. Without this, a tool file
    # that uses `from __future__ import annotations` + forward-ref types (e.g.
    # `Literal[...]`) fails registration with PydanticUserError. See bug-088.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ToolSetupFailed(
            tool_name=path.stem,
            reason=f"importing {path} raised {type(exc).__name__}: {exc}",
        ) from exc

    found: list[Callable[..., Awaitable[Any]]] = []
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if hasattr(attr, "_tool_definition") and isinstance(
            attr._tool_definition,
            ToolDefinition,
        ):
            found.append(attr)
    return found
