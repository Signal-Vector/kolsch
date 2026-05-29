"""Platform runtime — the shared composition root for the CLI + API.

Per the v1-scope §2.4 entry surfaces: both ``kolsch`` (CLI) and
the FastAPI server need the same subsystem wiring. Centralising it
here means there is one place that knows how to stand up the v1
default tier (SQLite + in-process bus + dotenv secrets + native
sandbox + FakeFramework + EchoLLM) with zero external services.

Public surface:

- :class:`PlatformRuntime` — holds the wired subsystems + engine.
- :func:`bootstrap_runtime` — async factory that builds a
  :class:`PlatformRuntime` from a platform root.
- :func:`resolve_workspace`, :func:`workspace_is_initialized` —
  Phase 1.2 (UX-R7) workspace-resolution helpers. The single point
  of truth for "where does this `kolsch` invocation operate?".
"""

from platform_runtime.runtime import PlatformRuntime, bootstrap_runtime
from platform_runtime.workspace import (
    DEFAULT_DIR_NAME,
    KOLSCH_ENV_VAR,
    resolve_workspace,
    workspace_is_initialized,
)

__all__ = [
    "DEFAULT_DIR_NAME",
    "KOLSCH_ENV_VAR",
    "PlatformRuntime",
    "bootstrap_runtime",
    "resolve_workspace",
    "workspace_is_initialized",
]
