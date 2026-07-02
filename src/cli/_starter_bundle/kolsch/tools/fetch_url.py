"""``fetch_url`` — light-tier HTTP GET with main-content extraction.

This file IS the tool. See ``read_file.py`` for the file-IS-the-tool
rationale (TOOL-R10 hard rule). ``src/tools/builtins.py::make_fetch_url``
imports this module's ``fetch_url`` coroutine + ``FetchUrlArgs`` so
the workflow-node companion (a follow-up QoL) consumes the same
implementation without duplication.

Tier 1 of the two-tier web-fetch architecture:
- ``fetch_url`` (this tool) — ``httpx + trafilatura``. Under 5 MB of
  deps. Static / server-rendered pages, JSON APIs, small binaries.
- ``browse_url`` — Playwright / Chromium behind ``kolsch[browse]``.
  For JS-heavy SPAs that ``fetch_url`` sees as an empty shell.

Returns ``{url, final_url, status, content, content_persisted_to,
truncated, content_type}``. ``content`` is truncated to 64 KiB
inline; overflow persists to
``<workspace>/logs/tool-outputs/<session_id>/<call_id>.content.txt``.

Spec: process/qol-fetch-url-tool.md §Technical Context.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, Field

from errors import ToolExecutionFailed, ToolTimeout
from errors.engine import ExchangeCancelled
from tools.decorator import ToolContext, tool

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from types import ModuleType

    import httpx as httpx_mod

_CONTENT_INLINE_BYTES = 64 * 1024  # 64 KiB preview cap before overflow persistence


class FetchUrlArgs(BaseModel):
    """Argument schema for the ``fetch_url`` tool."""

    url: str = Field(
        description="Absolute URL. Must be http:// or https://.",
    )
    format: Literal["text", "html", "bytes_base64"] = Field(
        default="text",
        description=(
            "Return format. 'text' extracts main-content text from HTML via "
            "trafilatura, or falls back to a utf-8 decode for non-HTML "
            "content. 'html' returns the raw response body as a string "
            "(no JS execution — see browse_url). 'bytes_base64' returns "
            "the body base64-encoded, useful for PDFs / images / JSON APIs."
        ),
    )
    headers: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional HTTP request headers. Use for custom User-Agent, "
            "Authorization: Bearer, or any header a target site requires."
        ),
    )
    cookies: dict[str, str] | None = Field(
        default=None,
        description="Optional cookies as {name: value}.",
    )
    follow_redirects: bool = Field(
        default=True,
        description="Follow 3xx redirects. Default True.",
    )
    request_timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=120,
        description=(
            "Per-request budget in seconds. Named request_timeout_seconds "
            "for symmetry with browse_url and to avoid collision with "
            "the workflow node's base timeout_seconds when a companion "
            "node QoL lands."
        ),
    )


@tool(
    name="fetch_url",
    summary="Fetch a URL over HTTP(S) and return extracted text, raw HTML, or bytes.",
    description=(
        "Perform an HTTP GET against a URL and return its response. Use "
        "this tool whenever the user asks you to fetch, read, download, "
        "grab, or pull a web page, docs page, JSON API response, RSS "
        "feed, or small binary payload. "
        "\n\n"
        "With format='text' (default): HTML responses run through "
        "trafilatura's main-content extractor and return clean article "
        "text (no <script> / <nav> cruft). JSON responses come back "
        "pretty-printed. Plain-text and other content types come back "
        "utf-8-decoded. With format='html': the raw response body as a "
        "string, no extraction (NO JavaScript execution — reach for "
        "browse_url when the page requires JS to render). With "
        "format='bytes_base64': the body base64-encoded, for PDFs / "
        "images / anything binary you want to round-trip. "
        "\n\n"
        "DO NOT use this tool when the target is a JS-heavy SPA that "
        'returns an empty shell (<div id="root"></div>) without '
        "server-side rendering — use browse_url. DO NOT use it as a "
        "replacement for read_file on local disk. "
        "\n\n"
        "Returns {url, final_url, status, content, content_persisted_to, "
        "truncated, content_type}. content is truncated to a 64 KiB "
        "preview inline when large; the full body persists to "
        "content_persisted_to on disk. content_type is one of 'text', "
        "'html', 'json', 'bytes_base64' so callers can branch."
    ),
    args_schema=FetchUrlArgs,
)
async def fetch_url(args: FetchUrlArgs, ctx: ToolContext) -> dict[str, Any]:
    """Fetch ``args.url`` and return the response payload.

    :returns: ``{url, final_url, status, content, content_persisted_to,
        truncated, content_type}``.
    :raises tool.execution_failed: URL scheme not http/https, or the
        underlying HTTP client raised a connection / DNS error.
    :raises tool.timeout: request exceeded ``args.request_timeout_seconds``.
    :raises exchange.cancelled: cancelled via ``ctx.cancellation``.
    """
    _validate_url(args.url)

    # Lazy imports so a missing runtime dep surfaces as a Diagnostic on
    # the tool call, not as a module-load failure that pulls down the
    # whole subsystem.
    try:
        import httpx  # noqa: PLC0415 — deferred by design
        import trafilatura  # noqa: PLC0415 — deferred by design
    except ImportError as exc:
        raise ToolExecutionFailed(
            tool_name="fetch_url",
            error_class="RuntimeDepMissing",
        ) from exc

    timeout = httpx.Timeout(float(args.request_timeout_seconds))
    try:
        async with httpx.AsyncClient(
            follow_redirects=args.follow_redirects,
            timeout=timeout,
            headers=args.headers or {},
            cookies=args.cookies or {},
        ) as client:
            resp = await _request_with_cancel(client, args, ctx)
    except ExchangeCancelled:
        raise
    except (httpx.TimeoutException, TimeoutError) as exc:
        # ``httpx.TimeoutException`` covers httpx's own connect / read /
        # write / pool timeout branch. ``TimeoutError`` (= asyncio's
        # ``TimeoutError`` in 3.11+) covers the bare-context fallback
        # in ``_request_with_cancel`` where ``asyncio.wait_for`` is
        # what races the request. Both funnel to the same ToolTimeout.
        raise ToolTimeout(
            tool_name="fetch_url",
            timeout_seconds=args.request_timeout_seconds,
        ) from exc
    except httpx.HTTPError as exc:
        # ConnectError, ReadError, DNS resolution failures, protocol
        # errors — anything below "the server answered with a status".
        raise ToolExecutionFailed(
            tool_name="fetch_url",
            error_class="NetworkError",
        ) from exc

    raw_bytes = resp.content
    ctype_header = resp.headers.get("content-type", "").lower()
    content, content_type = _extract(raw_bytes, ctype_header, args, trafilatura)

    persisted_path, inline_content, truncated = _persist_or_inline(
        content,
        ctx=ctx,
    )

    return {
        "url": args.url,
        "final_url": str(resp.url),
        "status": resp.status_code,
        "content": inline_content,
        "content_persisted_to": persisted_path,
        "truncated": truncated,
        "content_type": content_type,
    }


def _validate_url(url: str) -> None:
    """Raise InvalidUrl for anything that isn't a parseable http(s) URL."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ToolExecutionFailed(
            tool_name="fetch_url",
            error_class="InvalidUrl",
        ) from exc
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ToolExecutionFailed(
            tool_name="fetch_url",
            error_class="InvalidUrl",
        )


async def _request_with_cancel(
    client: httpx_mod.AsyncClient,
    args: FetchUrlArgs,
    ctx: ToolContext,
) -> httpx_mod.Response:
    """Race the HTTP request against ``ctx.cancellation`` when present.

    ``ctx.cancellation`` may be ``None`` (programmatic tests, or a
    ``ToolContext`` constructed without one). In that case we fall
    back to ``asyncio.wait_for`` so the request is still bounded by
    ``args.request_timeout_seconds``.
    """
    request_coro: Coroutine[Any, Any, httpx_mod.Response] = client.get(args.url)
    cancellation = getattr(ctx, "cancellation", None)
    if cancellation is None:
        resp: httpx_mod.Response = await asyncio.wait_for(
            request_coro,
            timeout=args.request_timeout_seconds,
        )
        return resp

    request_task = asyncio.create_task(request_coro)
    cancel_task = asyncio.create_task(cancellation.wait_cancelled())
    try:
        done, _pending = await asyncio.wait(
            {request_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if not cancel_task.done():
            cancel_task.cancel()
    if cancel_task in done and not request_task.done():
        request_task.cancel()
        with contextlib.suppress(BaseException):
            await request_task
        raise ExchangeCancelled(
            cancelled_at_event_id="",
            where="kolsch.tools.fetch_url._request_with_cancel",
        )
    final_resp: httpx_mod.Response = await request_task
    return final_resp


def _extract(
    raw_bytes: bytes,
    ctype_header: str,
    args: FetchUrlArgs,
    trafilatura: ModuleType,
) -> tuple[str, Literal["text", "html", "json", "bytes_base64"]]:
    """Route the body to the extractor implied by ``format`` + ``Content-Type``."""
    if args.format == "bytes_base64":
        return base64.b64encode(raw_bytes).decode("ascii"), "bytes_base64"
    if args.format == "html":
        return raw_bytes.decode("utf-8", errors="replace"), "html"
    # From here on, args.format is "text"; route by Content-Type.
    if "text/html" in ctype_header:
        extracted = trafilatura.extract(raw_bytes)
        if not extracted:
            extracted = _bs4_fallback(raw_bytes)
        return extracted, "text"
    if "application/json" in ctype_header:
        try:
            parsed = json.loads(raw_bytes)
        except (ValueError, UnicodeDecodeError):
            return raw_bytes.decode("utf-8", errors="replace"), "text"
        return json.dumps(parsed, indent=2), "json"
    return raw_bytes.decode("utf-8", errors="replace"), "text"


def _bs4_fallback(raw_bytes: bytes) -> str:
    """Title + body-text fallback when trafilatura returns None.

    Paywalls, login walls, and pages with unusual DOM shapes can
    trip trafilatura. BeautifulSoup ships with trafilatura (via
    ``lxml`` / ``lxml_html_clean``) so this fallback has no
    additional dep footprint.
    """
    try:
        from bs4 import BeautifulSoup  # noqa: PLC0415 — deferred by design
    except ImportError:
        return raw_bytes.decode("utf-8", errors="replace")
    soup = BeautifulSoup(raw_bytes, "html.parser")
    title_el = soup.find("title")
    title: str = title_el.get_text(strip=True) if title_el else ""
    body: str = soup.get_text(separator="\n", strip=True)
    if title:
        return f"{title}\n\n{body}"
    return body


def _persist_or_inline(
    content: str,
    *,
    ctx: ToolContext,
) -> tuple[str | None, str, bool]:
    """Persist ``content`` to disk when it exceeds the 64 KiB inline cap.

    Returns ``(persisted_path, inline_content, truncated)``.

    Local copy of ``bash.py``'s overflow-persistence helper. A shared
    ``_tool_helpers`` module lands in a follow-up QoL. Convention
    aligned to ``bash.py``:

    - Filename uses ``ctx.tool_call_id`` (with ``tc_anon_<uuid4>``
      fallback for out-of-engine invocations) so overflow paths are
      correlated to the tool-call event that produced them.
    - When ``ctx.workspace_root`` is missing (bare-context tests), we
      fall back to ``<cwd>/.tool-outputs/`` so the persisted file is
      still addressable rather than silently dropped.
    """
    encoded = content.encode("utf-8")
    if len(encoded) <= _CONTENT_INLINE_BYTES:
        return None, content, False

    call_id = ctx.tool_call_id or f"tc_anon_{uuid4()}"
    session = str(ctx.session_id) if ctx.session_id else "anon"
    if ctx.workspace_root is not None:
        outputs_root = ctx.workspace_root / "logs" / "tool-outputs" / session
    else:
        outputs_root = Path(".tool-outputs") / session

    persisted_path: str | None = None
    try:
        outputs_root.mkdir(parents=True, exist_ok=True)
        target = outputs_root / f"{call_id}.content.txt"
        target.write_text(content, encoding="utf-8")
        persisted_path = str(target)
    except OSError:
        # Best-effort persistence — an unwriteable workspace still
        # returns a truncated preview so the caller isn't blocked.
        persisted_path = None

    head_bytes = encoded[:_CONTENT_INLINE_BYTES]
    head = head_bytes.decode("utf-8", errors="replace")
    remaining = len(encoded) - _CONTENT_INLINE_BYTES
    footer = (
        f"\n[... truncated {remaining} bytes; full content at "
        f"{persisted_path or '<persistence failed>'} ]"
    )
    return persisted_path, head + footer, True


# The workspace-materialized copy of this file (rebuilt on each
# ``kolsch setup``) is where the platform actually loads the tool
# from at runtime. Keeping the module-level ``_resolve`` helper
# nearby matches ``read_file.py``'s shape so future contributors
# can copy either template safely.
def _resolve(path: str, root: Path) -> Path:
    """Resolve a workspace-relative path against the workspace root."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate
