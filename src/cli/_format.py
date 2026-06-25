"""CLI render helpers — Phase 3.0 Step 6.

Two responsibilities:

- :func:`render` — the SINGLE place where text vs json branching
  happens for the ``kolsch workflows`` and ``kolsch nodes`` command
  groups (master spec §14.3 + §14.4 envelope). No subcommand prints
  payload directly; all output routes through this helper.
- :func:`render_exception` — R3 #11 generic JSON formatter for any
  :class:`workflows.errors.WorkflowError`. Uses
  ``identifying_names_keys`` and ``caller_message`` ClassVars
  (R3 #8 + R3 #11) to auto-build a uniform per-exception dict so the
  CLI doesn't need per-class formatters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from workflows.errors import WorkflowError

__all__ = ["EnvelopeStatus", "render", "render_exception"]


@dataclass(frozen=True, slots=True)
class EnvelopeStatus:
    """Sentinel status values for the JSON envelope."""

    OK: str = "ok"
    ERROR: str = "error"


_STATUS = EnvelopeStatus()


def render(
    *,
    command: str,
    fmt: str = "text",
    status: str = "ok",
    data: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    text_body: str | None = None,
) -> str:
    """Render a command's terminal output as text or JSON.

    Master spec §14.3 envelope shape (json):

    ::

        {
            "command": "<workflows verb>",
            "status": "ok" | "error",
            "data": {...} | null,
            "errors": [...],
            "warnings": [...]
        }

    Text path: returns ``text_body`` verbatim when provided, else a
    minimal stringification of ``data``. The caller is responsible
    for tabular formatting of the text path.
    """
    if fmt == "json":
        envelope: dict[str, Any] = {
            "command": command,
            "status": status,
            "data": data,
            "errors": errors or [],
            "warnings": warnings or [],
        }
        return json.dumps(envelope, indent=2, sort_keys=False)
    # text
    if text_body is not None:
        return text_body
    if status == "error":
        if errors:
            return "\n".join(
                err.get("summary") or err.get("class", "error") for err in errors
            )
        return "error"
    if data is None:
        return ""
    return str(data)


def render_exception(exc: BaseException) -> dict[str, Any]:
    """R3 #11 generic JSON formatter for a single exception.

    Output dict shape (per master spec §14.4):

    ::

        {
            "class": "<ClassName>",
            "summary": str(exc),
            "caller_message": "<caller_message ClassVar>",
            "identifying_names": {...},
            "attrs": {...},
            "file": str | None,
            "line": int | None,
            "docs_anchor": str | None
        }

    Works generically for any :class:`workflows.errors.WorkflowError`
    by reading ``identifying_names_keys`` + ``caller_message``
    ClassVars; all remaining instance attributes land in ``attrs``.

    For non-WorkflowError exceptions (defensive: e.g., bare
    ``NotImplementedError`` from the run stub), the formatter produces
    a minimal envelope with class + summary populated.
    """
    cls = type(exc)
    out: dict[str, Any] = {
        "class": cls.__name__,
        "summary": str(exc),
        "caller_message": getattr(cls, "caller_message", ""),
        "identifying_names": {},
        "attrs": {},
        "file": None,
        "line": None,
        "docs_anchor": None,
    }

    if not isinstance(exc, WorkflowError):
        return out

    id_keys: tuple[str, ...] = getattr(cls, "identifying_names_keys", ())
    instance_attrs = _public_attrs(exc)
    for key in id_keys:
        if key in instance_attrs:
            out["identifying_names"][key] = instance_attrs.pop(key)
    # file / line are common on YAML-origin error classes; lift them
    # out of the generic ``attrs`` so consumers can find them at a
    # stable location.
    out["file"] = instance_attrs.pop("file", None)
    out["line"] = instance_attrs.pop("line", None)
    out["attrs"] = instance_attrs

    return out


def _public_attrs(exc: BaseException) -> dict[str, Any]:
    """Return the instance's serialisable public attributes.

    Skips dunder names, callables, and any attribute whose name
    starts with an underscore.
    """
    out: dict[str, Any] = {}
    for name in dir(exc):
        if name.startswith("_"):
            continue
        # Skip class attributes (caller_message etc.) — only instance
        # state is interesting.
        if name in type(exc).__dict__:
            continue
        try:
            value = getattr(exc, name)
        except Exception:  # noqa: BLE001 — defensive
            continue
        if callable(value):
            continue
        out[name] = _make_jsonable(value)
    return out


def _make_jsonable(value: Any) -> Any:  # noqa: ANN401
    """Best-effort JSON-serialisable shim.

    Tuples → lists; sets → sorted lists; everything else passes
    through. Non-serialisable values surface as ``str(value)`` so the
    envelope never fails to encode.
    """
    if isinstance(value, tuple):
        return [_make_jsonable(v) for v in value]
    if isinstance(value, set | frozenset):
        return sorted(str(v) for v in value)
    if isinstance(value, dict):
        return {str(k): _make_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_make_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


_ = _STATUS  # exported via dataclass for callers that prefer constants
