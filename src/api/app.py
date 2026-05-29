"""FastAPI app factory.

The factory takes a :class:`platform_runtime.PlatformRuntime` so the
app shares the engine + store the CLI built — there is no second
composition root.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI

from api.routes import register_routes

if TYPE_CHECKING:
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
