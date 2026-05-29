"""FastAPI surface for the v1 default tier — Step 13.

Per v1-scope §2.4: HTTP entry point for clients that want to drive
agents from outside the CLI (IDE, browser, scripts). Same engine,
same store, same composition root (:func:`platform_runtime.bootstrap_runtime`).
No logic duplicated with :mod:`cli`.

Routes (see ``api.routes`` for full shapes):

- ``POST /sessions`` — create a session.
- ``GET  /sessions`` — list sessions (filter by starred/archived).
- ``GET  /sessions/{session_id}/events`` — fetch events for a session.
- ``POST /agents/{agent_ref:path}/execute`` — sync exchange.
- ``POST /agents/{agent_ref:path}/execute_stream`` — SSE stream of
  events (one event-per-message), terminator after the final event.

Boundary validation: ``file_ref`` inputs in execute calls are run
through :func:`files.validate_file_ref` + :func:`files.check_path_allowed`
before the engine sees them (Fh-I8).

Two factories:

- :func:`build_app` — takes a pre-built :class:`PlatformRuntime`.
  What tests use (build a tmp-rooted runtime, hand it in).
- :func:`make_app` — resolves the workspace via Phase 1.2's
  :func:`platform_runtime.resolve_workspace`, bootstraps a runtime,
  builds the app. Raises :class:`WorkspaceNotInitialized` if the
  resolved path has no ``platform.yaml``. The intended entry point
  for uvicorn factory-mode deployments where no Python caller has
  already built a runtime.
"""

from api.app import build_app, make_app

__all__ = ["build_app", "make_app"]
