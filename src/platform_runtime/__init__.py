"""Platform runtime — the shared composition root for the CLI and API.

The CLI (`kolsch`) and the FastAPI server both need the same subsystem
wiring. Centralising it here means there is one place that knows how
to stand up the v1 default tier (SQLite entity backend + in-process
event bus + dotenv secrets + native sandbox + MinimalFramework +
EchoLLMDriver) with zero external services required.

See the [v1 scope](../explanation/v1-scope.md) for the
default-tier contract this module implements.
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
