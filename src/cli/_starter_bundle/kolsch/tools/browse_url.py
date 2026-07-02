"""``browse_url`` — heavy-tier fetch that drives headless Chromium.

This file IS the tool. See ``read_file.py`` for the file-IS-the-tool
rationale (TOOL-R10 hard rule). ``src/tools/builtins.py::make_browse_url``
imports this module's ``browse_url`` coroutine + ``BrowseUrlArgs`` so
the workflow-node companion (a follow-up QoL) consumes the same
implementation without duplication.

Tier 2 of the two-tier web-fetch architecture:
- ``fetch_url`` — ``httpx + trafilatura``. Under 5 MB of deps. Static
  / server-rendered pages, JSON APIs, small binaries.
- ``browse_url`` (this tool) — Playwright / Chromium behind
  ``kolsch[browse]``. For JS-heavy SPAs that ``fetch_url`` sees as an
  empty shell. One-shot fetch — no interactive click / type / eval.

Returns ``{url, final_url, status, content, content_persisted_to,
content_length, format, duration_seconds, truncated}``. ``content``
is truncated to 64 KiB inline; overflow persists to
``<workspace>/logs/tool-outputs/<session_id>/<call_id>.content.txt``.

Every fetch launches and closes its own browser (no shared-browser
lifecycle in v1). This keeps the leak surface trivial and matches the
"one-shot fetch" scope. If leak-detection tests are added later, they
must probe by counting running chromium PIDs — not by inspecting
``browser.contexts`` on a torn-down browser (that check is trivially
true and doesn't prove anything).

Spec: process/qol-browse-url-tool.md §Technical Context.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from errors import ToolExecutionFailed, ToolTimeout
from errors.engine import ExchangeCancelled
from tools.decorator import ToolContext, tool

if TYPE_CHECKING:
    from collections.abc import Coroutine

    import playwright.async_api as playwright_mod

_CONTENT_INLINE_BYTES = 64 * 1024  # 64 KiB preview cap before overflow persistence


class BrowseUrlArgs(BaseModel):
    url: str = Field(
        description="Absolute URL to fetch. Must be http:// or https://.",
    )
    format: Literal["text", "html"] = Field(
        default="text",
        description=(
            "Return format. 'text' extracts the rendered document text "
            "(page title + body.innerText). Note: hidden nodes "
            "(display:none, visibility:hidden) and iframe content are "
            "NOT captured — reach for 'html' plus downstream parsing "
            "when you need them. 'html' returns the DOM after JS runs."
        ),
    )
    wait_selector: str | None = Field(
        default=None,
        description=(
            "Optional CSS selector to wait for before capturing. Useful "
            "for JS-heavy pages that hydrate after DOMContentLoaded."
        ),
    )
    headers: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional extra HTTP request headers merged on top of Playwright "
            "defaults. Use for custom User-Agent, Authorization: Bearer, or "
            "any header a target site requires. Case-insensitive; a "
            "caller-supplied 'User-Agent' overrides the default. Applied via "
            "context.set_extra_http_headers() before navigation."
        ),
    )
    cookies: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional session cookies as {name: value}. Applied to the browser "
            "context for the URL's origin before navigation via "
            "context.add_cookies(). Prefer this over stuffing a Cookie: header "
            "manually — Playwright handles domain/path/expiry defaults."
        ),
    )
    request_timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=120,
        description=(
            "Per-fetch budget in seconds. Named request_timeout_seconds "
            "(not timeout_seconds) to match the workflow node's field, since "
            "the workflow subsystem's base NodeDef reserves timeout_seconds "
            "for the executor-level timeout."
        ),
    )


@tool(
    name="browse_url",
    summary="Fetch a URL through headless Chromium and return the rendered content.",
    description=(
        "Load a URL in headless Chromium via Playwright and return the "
        "post-JS rendered content. Use this tool when the target is a "
        "JS-heavy SPA that returns an empty shell without server-side "
        "rendering, or when the useful text only appears after "
        "hydration. Reach for fetch_url first when the page is static "
        "or server-rendered — this tool is materially heavier (browser "
        "launch + Chromium binary) and behind the opt-in kolsch[browse] "
        "extra."
        "\n\n"
        "With format='text' (default): returns the page title followed "
        "by body.innerText. Hidden nodes (display:none, "
        "visibility:hidden) and iframe content are NOT captured — reach "
        "for format='html' plus downstream parsing when they matter. "
        "With format='html': returns the DOM after JS has run "
        "(page.content())."
        "\n\n"
        "Set wait_selector to a CSS selector when the interesting "
        "content hydrates after 'load'. Custom headers and cookies "
        "attach to the browser context BEFORE navigation."
        "\n\n"
        "DO NOT use this tool as a general HTTP client — reach for "
        "fetch_url. DO NOT expect an interactive session (click / type "
        "/ eval): this tool is one-shot. Missing Chromium binary "
        "surfaces as BrowserBinaryMissing with an actionable message; "
        "the tool does not auto-install."
        "\n\n"
        "Returns {url, final_url, status, content, "
        "content_persisted_to, content_length, format, "
        "duration_seconds, truncated}. content is truncated to a 64 "
        "KiB preview inline when large; the full body persists to "
        "content_persisted_to on disk."
    ),
    args_schema=BrowseUrlArgs,
)
async def browse_url(args: BrowseUrlArgs, ctx: ToolContext) -> dict[str, Any]:
    """Fetch ``args.url`` through headless Chromium and return the response.

    :returns: ``{url, final_url, status, content, content_persisted_to,
        content_length, format, duration_seconds, truncated}``.
    :raises tool.execution_failed: URL scheme not http/https; Playwright
        extra not installed; Chromium binary missing; navigation error.
    :raises tool.timeout: fetch exceeded ``args.request_timeout_seconds``.
    :raises exchange.cancelled: cancelled via ``ctx.cancellation``.
    """
    _validate_url(args.url)

    # Lazy import so a missing extra surfaces as a Diagnostic on the
    # tool call, not as a module-load failure that pulls down the
    # whole subsystem.
    try:
        from playwright.async_api import (  # noqa: PLC0415 — deferred by design
            Error as PlaywrightError,
        )
        from playwright.async_api import (  # noqa: PLC0415 — deferred by design
            TimeoutError as PlaywrightTimeoutError,
        )
        from playwright.async_api import (  # noqa: PLC0415 — deferred by design
            async_playwright,
        )
    except ImportError as exc:
        raise ToolExecutionFailed(
            tool_name="browse_url",
            error_class="BrowseUrlExtraNotInstalled",
        ) from exc

    started = time.monotonic()

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except PlaywrightError as exc:
            # "Executable doesn't exist" is Playwright's phrasing when
            # the Chromium binary hasn't been fetched via
            # `playwright install chromium`. Spec: no in-process
            # auto-install — mid-chat 150 MB downloads are user-hostile.
            if "Executable doesn't exist" in str(exc):
                raise ToolExecutionFailed(
                    tool_name="browse_url",
                    error_class="BrowserBinaryMissing",
                ) from exc
            raise ToolExecutionFailed(
                tool_name="browse_url",
                error_class="NavigationError",
            ) from exc

        try:
            context = await browser.new_context()
            if args.headers:
                await context.set_extra_http_headers(args.headers)
            if args.cookies:
                # Playwright cookies attach to an origin; derive from args.url.
                origin = urllib.parse.urlparse(args.url)
                await context.add_cookies(
                    [
                        {
                            "name": k,
                            "value": v,
                            "domain": origin.hostname or "",
                            "path": "/",
                        }
                        for k, v in args.cookies.items()
                    ]
                )
            page = await context.new_page()

            try:
                response = await _navigate_with_cancel(page, args, ctx)
            except PlaywrightTimeoutError as exc:
                raise ToolTimeout(
                    tool_name="browse_url",
                    timeout_seconds=args.request_timeout_seconds,
                ) from exc

            if args.wait_selector:
                try:
                    await page.wait_for_selector(
                        args.wait_selector,
                        timeout=args.request_timeout_seconds * 1000,
                    )
                except PlaywrightTimeoutError as exc:
                    raise ToolTimeout(
                        tool_name="browse_url",
                        timeout_seconds=args.request_timeout_seconds,
                    ) from exc

            status = response.status if response else 0
            final_url = page.url
            if args.format == "text":
                title = await page.title()
                body_text = await page.inner_text("body")
                content = f"{title}\n\n{body_text}" if title else body_text
            else:
                content = await page.content()
        finally:
            # Every fetch launches and closes its own browser — one-shot,
            # no shared lifecycle. Best-effort close on every path so
            # timeouts and cancellation don't leak the Chromium process.
            with contextlib.suppress(Exception):
                await browser.close()

    duration = time.monotonic() - started
    content_length = len(content)
    persisted_path, inline_content, truncated = _persist_or_inline(
        content,
        ctx=ctx,
    )
    return {
        "url": args.url,
        "final_url": final_url,
        "status": status,
        "content": inline_content,
        "content_persisted_to": persisted_path,
        "content_length": content_length,
        "format": args.format,
        "duration_seconds": round(duration, 3),
        "truncated": truncated,
    }


def _validate_url(url: str) -> None:
    """Raise InvalidUrl for anything that isn't a parseable http(s) URL."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise ToolExecutionFailed(
            tool_name="browse_url",
            error_class="InvalidUrl",
        ) from exc
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ToolExecutionFailed(
            tool_name="browse_url",
            error_class="InvalidUrl",
        )


async def _navigate_with_cancel(
    page: playwright_mod.Page,
    args: BrowseUrlArgs,
    ctx: ToolContext,
) -> playwright_mod.Response | None:
    """Race ``page.goto`` against ``ctx.cancellation`` when present.

    ``ctx.cancellation`` may be ``None`` (programmatic tests, or a
    ``ToolContext`` constructed without one). In that case we let
    Playwright's own ``timeout`` argument bound the navigation.
    """
    goto_coro: Coroutine[Any, Any, playwright_mod.Response | None] = page.goto(
        args.url,
        wait_until="load",
        timeout=args.request_timeout_seconds * 1000,  # Playwright expects ms
    )
    cancellation = getattr(ctx, "cancellation", None)
    if cancellation is None:
        return await goto_coro

    goto_task: asyncio.Task[playwright_mod.Response | None] = asyncio.create_task(
        goto_coro
    )
    cancel_task = asyncio.create_task(cancellation.wait_cancelled())
    try:
        done, _pending = await asyncio.wait(
            {goto_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if not cancel_task.done():
            cancel_task.cancel()
    if cancel_task in done and not goto_task.done():
        goto_task.cancel()
        with contextlib.suppress(BaseException):
            await goto_task
        raise ExchangeCancelled(
            cancelled_at_event_id="",
            where="kolsch.tools.browse_url._navigate_with_cancel",
        )
    return await goto_task


def _persist_or_inline(
    content: str,
    *,
    ctx: ToolContext,
) -> tuple[str | None, str, bool]:
    """Persist ``content`` to disk when it exceeds the 64 KiB inline cap.

    Returns ``(persisted_path, inline_content, truncated)``.

    Local copy of ``bash.py`` / ``fetch_url.py``'s overflow-persistence
    helper. A shared ``_tool_helpers`` module lands in a follow-up QoL
    — keeping the logic local for now avoids widening the diff.
    """
    encoded = content.encode("utf-8")
    if len(encoded) <= _CONTENT_INLINE_BYTES:
        return None, content, False

    persisted_path: str | None = None
    if ctx.workspace_root is not None:
        outputs_root = (
            ctx.workspace_root / "logs" / "tool-outputs" / str(ctx.session_id)
        )
        try:
            outputs_root.mkdir(parents=True, exist_ok=True)
            call_id = uuid4().hex
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
        f"{persisted_path or '<workspace_root not available>'} ]"
    )
    return persisted_path, head + footer, True


# The workspace-materialized copy of this file (rebuilt on each
# ``kolsch setup``) is where the platform actually loads the tool
# from at runtime. Keeping the module-level ``_resolve`` helper
# nearby matches ``fetch_url.py``'s shape so future contributors
# can copy either template safely.
def _resolve(path: str, root: Path) -> Path:
    """Resolve a workspace-relative path against the workspace root."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate
