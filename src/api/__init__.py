"""FastAPI surface for the v1 default tier.

The HTTP entry point for clients that drive agents from outside the
CLI (IDE, browser, scripts). Shares the engine, store, and composition
root with `cli` — no logic is duplicated; the surface differs only in
transport.

Two factories are exposed:
[`build_app`][api.app.build_app] takes a pre-built `PlatformRuntime`
and is what tests use; [`make_app`][api.app.make_app] resolves the
workspace, bootstraps a runtime, and builds the app — the intended
entry point for uvicorn factory-mode deployments where no Python
caller has already built a runtime.

See the [file handling contract](../explanation/file-handling.md)
for the `file_ref` boundary validation applied to invocation inputs
before the engine sees them.
"""

from api.app import build_app, make_app

__all__ = ["build_app", "make_app"]
