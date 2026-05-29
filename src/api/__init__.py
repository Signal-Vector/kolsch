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

The public entry point is :func:`build_app`; tests use FastAPI's
``TestClient`` against it. Production runs via ``kolsch server``
(uvicorn-hosted).
"""

from api.app import build_app

__all__ = ["build_app"]
