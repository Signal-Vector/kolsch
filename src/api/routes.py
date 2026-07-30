"""FastAPI route handlers for the v1 default-tier HTTP surface.

All routes share the runtime injected at
[`build_app`][api.app.build_app] time. Request and response shapes are
Pydantic models declared inline so the auto-generated OpenAPI schema
reflects them.

Streaming uses Server-Sent Events (SSE) over
[`AgentEngine.execute_stream`][engine.engine.AgentEngine.execute_stream] —
one event-per-SSE-message, terminated by a synthetic `event: done`
line after the final engine event.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api.views import _SessionView, session_view
from engine import AgentExecutionError, ExecutionContext
from errors import (
    AssetsNotFound,
    ConfigInvalid,
    FileAccessDenied,
    FileAccessInvalidFileRef,
    PlatformError,
    SessionNotFound,
)
from files import check_path_allowed, validate_file_ref
from schemas import SessionId

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from platform_runtime import PlatformRuntime


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class _CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(..., description="ag_<id> for the session's owning agent.")
    name: str | None = None


class _UpdateSessionRequest(BaseModel):
    """Partial update of UI-state fields for `PATCH /sessions/{id}`.

    All fields are optional; only present keys are applied. See the
    [architecture pages](../architecture/index.md)
    for the underlying `entity_backend.update_session` contract.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    starred: bool | None = None
    archived: bool | None = None


class _ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(
        ...,
        description="Existing session id (POST /sessions to create first).",
    )
    trigger_kind: str = Field(
        default="user_message",
        description=("user_message | user_message_with_force_skill | invocation"),
    )
    content: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    force_skills: list[str] = Field(default_factory=list)


class _ExecuteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_event_type: str
    data: dict[str, Any]
    event_id: str


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_routes(app: FastAPI, runtime: PlatformRuntime) -> None:
    """Bind every default route on `app` to `runtime`.

    Split out from [`build_app`][api.app.build_app] so a custom app
    (with extra middleware or extra routes) can still call this to
    wire the default surface.

    Args:
        app: The FastAPI app to mount routes on.
        runtime: The composition root every handler closes over.
    """

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # ---- Sessions ---------------------------------------------------

    @app.post("/sessions", response_model=_SessionView, status_code=201)
    async def create_session(req: _CreateSessionRequest) -> _SessionView:
        session = await runtime.store.create_session(
            req.agent_id,
            name=req.name,
        )
        return session_view(session)

    @app.get("/sessions", response_model=list[_SessionView])
    async def list_sessions(
        starred: bool | None = Query(default=None),
        archived: bool | None = Query(default=None),
        limit: int | None = Query(default=None, ge=1),
    ) -> list[_SessionView]:
        sessions = await runtime.store.list_sessions(
            starred=starred,
            archived=archived,
            limit=limit,
        )
        return [session_view(s) for s in sessions]

    @app.patch("/sessions/{session_id}", response_model=_SessionView)
    async def update_session(
        session_id: str,
        req: _UpdateSessionRequest,
    ) -> _SessionView:
        try:
            sid = SessionId(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            updated = await runtime.store.update_session(
                sid,
                name=req.name,
                starred=req.starred,
                archived=req.archived,
            )
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return session_view(updated)

    @app.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str) -> None:
        try:
            sid = SessionId(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # store.delete_session is idempotent — already-deleted is a
        # no-op. Returns no body.
        await runtime.store.delete_session(sid)

    @app.get("/sessions/{session_id}/events")
    async def get_session_events(session_id: str) -> list[dict[str, Any]]:
        try:
            sid = SessionId(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Verify the session still exists first — list_threads
        # returns [] for a deleted session, but the API contract is
        # "404 if the session is gone." get_session raises
        # SessionNotFound, which we map to 404 here.
        try:
            await runtime.store.get_session(sid)
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        threads = await runtime.store.list_threads(sid)

        events: list[dict[str, Any]] = []
        for thread in threads:
            async for event in runtime.store.read_events(thread.thread_id):
                events.append(event.model_dump(mode="json"))
        return events

    # ---- Execute (sync + streaming) ---------------------------------

    @app.post(
        "/agents/{agent_ref:path}/execute",
        response_model=_ExecuteResponse,
    )
    async def execute_agent(
        agent_ref: str,
        req: _ExecuteRequest,
    ) -> _ExecuteResponse:
        ctx = await _prepare_context(runtime, agent_ref, req)
        # Route through runtime.build_resolved_config so Layer 3
        # validators fire — see PlatformRuntime docstring. A
        # ConfigInvalid surfaces here as 400 with the structured
        # validation errors in the response body (the engine never
        # gets the broken config).
        try:
            config = await runtime.build_resolved_config(agent_ref)
        except ConfigInvalid as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": exc.to_dict()},
            ) from exc
        try:
            event = await runtime.engine.execute(config, ctx)
        except AgentExecutionError as exc:
            failed = exc.failed_event
            return _ExecuteResponse(
                final_event_type=str(failed.type.value),
                data=failed.data,
                event_id=str(failed.event_id),
            )
        return _ExecuteResponse(
            final_event_type=str(event.type.value),
            data=event.data,
            event_id=str(event.event_id),
        )

    @app.post("/agents/{agent_ref:path}/execute_stream")
    async def execute_agent_stream(
        agent_ref: str,
        req: _ExecuteRequest,
    ) -> StreamingResponse:
        ctx = await _prepare_context(runtime, agent_ref, req)
        # Route through runtime.build_resolved_config so Layer 3
        # validators fire — see PlatformRuntime docstring.
        try:
            config = await runtime.build_resolved_config(agent_ref)
        except ConfigInvalid as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": exc.to_dict()},
            ) from exc

        async def event_stream() -> AsyncIterator[bytes]:
            try:
                async for event in runtime.engine.execute_stream(config, ctx):
                    payload = event.model_dump(mode="json")
                    yield _sse_format(
                        event=str(event.type.value),
                        data=json.dumps(payload, sort_keys=True, default=str),
                    )
            except Exception as exc:  # noqa: BLE001 — safety net at SSE boundary
                yield _sse_format(
                    event="error",
                    data=json.dumps({"error": str(exc), "type": type(exc).__name__}),
                )
            yield _sse_format(event="done", data="")

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _prepare_context(
    runtime: PlatformRuntime,
    agent_ref: str,
    req: _ExecuteRequest,
) -> ExecutionContext:
    """Validate request inputs and build the engine's `ExecutionContext`.

    `file_ref` inputs on the invocation are validated at this boundary
    — see the [file handling contract](../explanation/file-handling.md).
    The engine trusts inputs once they cross this seam.
    """
    try:
        session_id = SessionId(req.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        threads = await runtime.store.list_threads(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not threads:
        raise HTTPException(
            status_code=409,
            detail=f"session {req.session_id} has no thread",
        )

    # Inputs may carry file_ref values; validate before the engine sees them.
    for key, value in req.inputs.items():
        if isinstance(value, str) and _looks_like_file_ref(key):
            try:
                validate_file_ref(value)
                check_path_allowed(
                    value,
                    policy=runtime.path_policy,
                    root=runtime.root,
                )
            except (FileAccessInvalidFileRef, FileAccessDenied) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Build trigger payload per kind.
    trigger: dict[str, Any]
    if req.trigger_kind == "user_message":
        trigger = {"kind": "user_message", "content": req.content or ""}
    elif req.trigger_kind == "user_message_with_force_skill":
        trigger = {
            "kind": "user_message_with_force_skill",
            "content": req.content or "",
            "force_skills": list(req.force_skills),
        }
    elif req.trigger_kind == "invocation":
        trigger = {"kind": "invocation", "inputs": dict(req.inputs)}
    else:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported trigger_kind {req.trigger_kind!r}",
        )

    # Confirm the agent ref resolves before we hand off to the engine
    # so callers see a clean 404 rather than a 500 from the engine
    # boundary.
    try:
        await runtime.asset_management.get_asset(agent_ref, field_asset_type="agent")
    except AssetsNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PlatformError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ExecutionContext(thread_id=threads[0].thread_id, trigger=trigger)


def _looks_like_file_ref(key: str) -> bool:
    """Return `True` for input keys treated as file references.

    Boundary-validation heuristic: keys ending in `_file`, `_path`, or
    `_ref` are treated as file references. A richer typespec-driven
    dispatch will replace this once `IOSpec.inputs` surfaces the
    `file_ref` type at the API layer.
    """
    return key.endswith(("_file", "_path", "_ref"))


def _sse_format(*, event: str, data: str) -> bytes:
    r"""Render one SSE message.

    Wire shape: `event: <event>\ndata: <data>\n\n`. Multi-line data
    splits across multiple `data:` lines per the W3C SSE spec; we keep
    the data on one line because `json.dumps` already does.
    """
    lines = [f"event: {event}", f"data: {data}", "", ""]
    return ("\n".join(lines)).encode("utf-8")
