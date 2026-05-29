"""FastAPI app factory.

Two entry points:

- :func:`build_app` — takes a pre-built :class:`PlatformRuntime`.
  Used by tests + by the CLI's ``kolsch server`` (which builds the
  runtime itself).
- :func:`make_app` — resolves the workspace via Phase 1.2's
  :func:`platform_runtime.resolve_workspace`, bootstraps a runtime,
  builds the app. Raises :class:`WorkspaceNotInitialized` when the
  resolved workspace has no ``platform.yaml``. The intended entry
  for uvicorn factory-mode deployments — no friendly-print layer
  here; the operator gets the typed error per Phase 1.2 patch §1.4.
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
    """Build the FastAPI app bound to ``runtime``.

    Tests call this with a tmp-path-bootstrapped runtime + drive via
    :class:`fastapi.testclient.TestClient` so the entire surface is
    exercised in-process without a network roundtrip.
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
    """Resolve the workspace, bootstrap a runtime, return the app.

    Phase 1.2 (UX-R7 / patch §1.3) — the entry uvicorn factory mode
    targets. Raises :class:`WorkspaceNotInitialized` when the
    resolved path has no ``platform.yaml`` so the operator sees a
    typed error rather than a confusing later FileNotFoundError.
    """
    resolved = resolve_workspace(root)
    if not workspace_is_initialized(resolved):
        raise WorkspaceNotInitialized(
            path=str(resolved),
            env_var_set=KOLSCH_ENV_VAR in os.environ,
        )
    runtime = await bootstrap_runtime(root=resolved)
    return build_app(runtime)
