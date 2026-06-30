"""FastAPI app factory.

Two entry points cover the two callers. [`build_app`][api.app.build_app]
takes a pre-built `PlatformRuntime` and is what tests plus the CLI's
`kolsch server` use — both build the runtime themselves.
[`make_app`][api.app.make_app] resolves the workspace, bootstraps a
runtime, then delegates to `build_app`; it raises
[`WorkspaceNotInitialized`][errors.WorkspaceNotInitialized] when the
resolved workspace has no `platform.yaml` so the operator sees a typed
error rather than a confusing later `FileNotFoundError`.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi import FastAPI

from api.routes import register_routes
from errors import WorkspaceNotInitialized
from platform_runtime import (
    KOLSCH_ENV_VAR,
    bootstrap_runtime,
    resolve_workspace,
    workspace_is_initialized,
)

if TYPE_CHECKING:
    from pathlib import Path

    from platform_runtime import PlatformRuntime


def build_app(runtime: PlatformRuntime) -> FastAPI:
    """Build the FastAPI app bound to `runtime`.

    Tests call this with a tmp-path-bootstrapped runtime and drive the
    app via `fastapi.testclient.TestClient` so the entire surface is
    exercised in-process without a network roundtrip.

    Args:
        runtime: The composition root the routes resolve against. All
            handlers close over this instance.

    Returns:
        A `FastAPI` app with every default route registered.
    """
    app = FastAPI(
        title="Agent Platform",
        version="0.1",
        description=(
            "v1 default-tier HTTP surface for the Agent Platform. "
            "Backed by SQLite + in-process event bus by default; "
            "production tiers swap in Postgres + Redis."
        ),
    )
    register_routes(app, runtime)
    return app


async def make_app(root: Path | None = None) -> FastAPI:
    """Resolve the workspace, bootstrap a runtime, and return the app.

    The entry uvicorn factory mode targets. Unlike
    [`build_app`][api.app.build_app], which trusts the caller to have
    built a runtime, this factory does the full resolve-then-bootstrap
    dance and surfaces an actionable error when the workspace is
    missing its `platform.yaml`.

    Args:
        root: Optional explicit workspace root. When `None`, the
            workspace is resolved from the `KOLSCH_WORKSPACE`
            environment variable or the current working directory.

    Returns:
        A `FastAPI` app bound to a freshly bootstrapped runtime.

    Raises:
        WorkspaceNotInitialized: The resolved path has no
            `platform.yaml`. The error payload records whether the
            workspace env var was set so the operator can tell whether
            their override took effect.
    """
    resolved = resolve_workspace(root)
    if not workspace_is_initialized(resolved):
        raise WorkspaceNotInitialized(
            path=str(resolved),
            env_var_set=KOLSCH_ENV_VAR in os.environ,
        )
    runtime = await bootstrap_runtime(root=resolved)
    return build_app(runtime)
