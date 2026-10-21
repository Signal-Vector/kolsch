# Web-Fetch Tools

**Draft 0.1**

> Two tools ship in `bundles/kolsch/` for pulling web pages, deliberately
> split into a light default and an opt-in heavy tier. `fetch_url` covers
> the ~70% case with `httpx` + `trafilatura` and no browser install.
> `browse_url` covers the JS-rendered tail with headless Chromium via
> Playwright, gated behind the `kolsch[browse]` extra so the ~150 MB
> browser binary is opt-in per kolsch's zero-dependency-default ethos.
> A 15th reserved built-in workflow node (`kolsch/nodes/browse_url`)
> wraps the heavy tier so workflow YAMLs can drive it.

Related contracts:

- [Tools subsystem](tools-subsystem.md) — the `@tool` decorator, discovery,
  invocation, and the `<bundle>/tools/<name>` action namespace.
- [Workflow foundation](workflow-foundation.md) — how workflow nodes
  are registered and dispatched.
- Setup how-to: [Workspace and setup → Fetching URLs](../how-to/workspace-and-setup.md#fetching-urls).
- Reference: `kolsch/nodes/browse_url` node schema in
  [Workflow YAML](../reference/workflow-yaml.md).

---

## Part 0 — Decisions Locked

- **Two tiers, not one.** `fetch_url` (Tier 1) is always available; the
  Tier 1 dep footprint (`httpx` + `trafilatura`) stays under a few MB.
  `browse_url` (Tier 2) is opt-in through `kolsch[browse]` because
  Playwright pulls a ~150 MB Chromium binary the user should choose.
- **Default authz: `prompt`.** Both tools land in the workspace-only
  default rule set at `src/cli/_setup.py` with `effect: "prompt"` so
  the chat surface asks before an agent hits the open web. Same
  discipline as `bash` and `write_file`.
- **`kolsch[browse]` is a grouping, not per-tool.** Playwright and
  `beautifulsoup4` both live under the extra: Playwright for
  `browse_url`, bs4 as a higher-quality extraction fallback for
  `fetch_url` when trafilatura returns `None`. A future browser-
  driven tool joining the family does not need a new extra.
- **No in-process Chromium auto-install.** The tool raises
  `BrowserBinaryMissing` with an actionable install hint. Mid-chat
  150 MB downloads are user-hostile; the browser install is a
  one-time step the user opts into via
  `playwright install chromium`.
- **Return shapes are stable across tools.** `fetch_url` returns
  seven fields; `browse_url` returns six; the workflow node narrows
  the tool return to the same six. The naming symmetry
  (`headers`, `cookies`, `request_timeout_seconds`, `final_url`,
  `content_persisted_to`, `truncated`) is a real ergonomic win for
  workflow authors and LLMs alike — do not diverge without cause.
- **`request_timeout_seconds`, not `timeout_seconds`.** Both tools
  and the workflow node use `request_timeout_seconds` for the
  per-request budget so the field name is symmetric across
  surfaces and cannot collide with the workflow node base's
  `timeout_seconds` (which bounds the executor-level budget). See
  Part 4 for the disambiguation.

---

## Part 1 — When to use which tier

The tiers exist because JS-rendering is expensive but sometimes
necessary. The decision flow:

1. **Start with `fetch_url`.** It uses `httpx` for the request and
   `trafilatura` to extract main-content text from HTML. For static
   or server-rendered pages, JSON APIs, RSS/XML feeds, and small
   binary payloads (via `format="bytes_base64"`), this is the right
   tool. Sub-second cold start, no browser process, no 150 MB
   install.
2. **Swap to `browse_url` when `fetch_url` returns a shell.** If the
   response body is `<div id="root"></div>` and nothing meaningful
   in the DOM, the page is a single-page app whose content is
   hydrated after `DOMContentLoaded`. `browse_url` drives a real
   headless Chromium, waits for load (and optionally a
   `wait_selector`), and captures the post-hydration DOM.

The two tools deliberately share args (`url`, `headers`, `cookies`,
`request_timeout_seconds`) so switching from one to the other is a
name change, not a rewrite of caller code.

---

## Part 2 — `fetch_url` in detail

**Source:** `src/cli/_starter_bundle/kolsch/tools/fetch_url.py`
(materialized to `<workspace>/bundles/kolsch/tools/fetch_url.py`
on `kolsch setup`). Workflow-runtime factory:
`src/tools/builtins.py::make_fetch_url`.

**Args:** `url`, `format ∈ {"text","html","bytes_base64"}` (default
`"text"`), `headers: dict[str, str] | None`,
`cookies: dict[str, str] | None`, `follow_redirects: bool = True`,
`request_timeout_seconds: int ∈ [1, 120] = 30`.

**Return shape:**

```python
{
    "url": str,                         # requested URL
    "final_url": str,                   # after redirects
    "status": int,                      # HTTP status
    "content": str,                     # extracted / raw / base64
    "content_persisted_to": str | None, # path when >64 KiB
    "truncated": bool,                  # True if preview
    "content_type": Literal["text", "html", "json", "bytes_base64"],
}
```

`content_type` disambiguates from `format`: with `format="text"`
against `Content-Type: application/json`, the tool returns
pretty-printed JSON and reports `content_type="json"` so
downstream authors can branch without parsing the payload.

**Extraction pipeline (format=text):**

- `text/html*` → `trafilatura.extract(raw_bytes)`. When trafilatura
  returns `None` (paywalls, login walls, unusual DOM shapes), we
  try a BeautifulSoup title-plus-body fallback. bs4 is declared
  under the `kolsch[browse]` extra; without the extra installed
  the fallback silently degrades to a raw utf-8 decode. Users who
  care about extraction quality across the tail install
  `kolsch[browse]`.
- `application/json*` → `json.dumps(json.loads(raw_bytes), indent=2)`,
  `content_type="json"`.
- Anything else → `raw_bytes.decode("utf-8", errors="replace")`.

**Cancellation:** the tool races `httpx.AsyncClient.request` against
`ctx.cancellation.wait_cancelled()`. When `ctx.cancellation is None`
(programmatic tests, bare contexts), it falls back to
`asyncio.wait_for` with the same `request_timeout_seconds` budget.
On cancel it raises `ExchangeCancelled(where="kolsch.tools.fetch_url._request_with_cancel")`.

**Errors:**

- `InvalidUrl` (via `ToolExecutionFailed`) when scheme isn't
  `http`/`https` or the URL fails to parse.
- `NetworkError` wrapping `httpx.ConnectError` / DNS failures.
- `ToolTimeout(timeout_seconds=request_timeout_seconds)` on
  `httpx.TimeoutException` or the `asyncio.wait_for` fallback path.
- `ExchangeCancelled` on Esc during a request.

---

## Part 3 — `browse_url` in detail

**Source:** `src/cli/_starter_bundle/kolsch/tools/browse_url.py`
(materialized to `<workspace>/bundles/kolsch/tools/browse_url.py`
on `kolsch setup`). Workflow-runtime factory:
`src/tools/builtins.py::make_browse_url`.

**Args:** `url`, `format ∈ {"text","html"}` (default `"text"`),
`wait_selector: str | None`, `headers: dict[str, str] | None`,
`cookies: dict[str, str] | None`,
`request_timeout_seconds: int ∈ [1, 120] = 30`.

**Return shape:**

```python
{
    "url": str,                         # requested URL
    "final_url": str,                   # after redirects
    "status": int,                      # HTTP status
    "content": str,                     # rendered text or DOM
    "content_persisted_to": str | None, # path when >64 KiB
    "truncated": bool,                  # True if preview
}
```

Note: no `content_type` — `browse_url` always returns rendered text
or the HTML DOM after JS execution.

**Extraction:** with `format="text"`, the tool captures
`await page.title()` prepended (as `"<title>\n\n<body.inner_text>"`)
because the page title is what users routinely want in text mode.
This intentionally omits hidden nodes (`display: none`,
`visibility: hidden`) and iframe content. Use `format="html"` when
either matters.

**Launch discipline:** every fetch launches its own Chromium
context and closes it in a `finally` block. No shared browser
lifecycle in v1 — a shared-browser tier is a natural follow-up when
workflow authors demand throughput.

**Cancellation:** races `page.goto` against
`ctx.cancellation.wait_cancelled()`; on cancel, closes the page and
raises `ExchangeCancelled(where="kolsch.tools.browse_url._navigate_with_cancel")`.
`ctx.cancellation is None` is handled by letting Playwright's own
`timeout` bound the navigation.

**Errors:**

- `InvalidUrl` (via `ToolExecutionFailed`) — same as `fetch_url`.
- `BrowseUrlExtraNotInstalled` — the `kolsch[browse]` extra is not
  installed, so `import playwright.async_api` failed. The failure
  carries a `recovery_hint` (qol-090) with the exact install command
  for the detected install mode (source / tool / pip); see
  [browse install](../how-to/workspace-and-setup.md#enabling-browse_url).
- `BrowserBinaryMissing` — Playwright imported cleanly but Chromium
  isn't on disk. Same `recovery_hint` shape, hint pointing at
  `playwright install chromium` (adjusted for install mode). The
  detection matches either the historical `"Executable doesn't
  exist"` phrase or the surviving `"playwright install"` install-hint
  token, so a Playwright message rewording upstream does not
  silently demote the diagnostic to `NavigationError`.
- `NavigationError` — any other `PlaywrightError` at launch or
  navigation.
- `ToolTimeout` — the tool's `request_timeout_seconds` budget
  expired.
- `ExchangeCancelled` — user pressed Esc during the fetch.

---

## Part 4 — The `browse_url` workflow node

**Reserved slot:** `kolsch/nodes/browse_url`, the 15th entry in
`BUILTIN_TYPE_NAMES` at `src/workflows/catalog_node.py`. Attempting
to author a user file at `bundles/<b>/nodes/browse_url.py` where
`b == "kolsch"` trips the existing `WorkflowBuiltinShadowed` guard
at scan time.

**Runner:** `src/workflows/builtins/browse_url.py::BrowseUrlRunner`
stores the `browse_url` `ToolDefinition` (built by
`make_browse_url` in `_workflow_runtime.py`) and calls
`dispatch_tool()` on each invocation. It reuses the tool's
authorization / secrets / sandbox pipeline end-to-end — the node
does not re-implement the fetch.

**YAML fields** (all Jinja-renderable except header/cookie **keys**):
`url`, `format`, `wait_selector`, `headers`, `cookies`,
`request_timeout_seconds`. Header/cookie **values** are rendered
per-value; keys pass through as literals so a Jinja-generated
string cannot smuggle unintended headers past review.

**Timeout naming — deliberate and load-bearing.** The base
`_BaseNodeDef.timeout_seconds` bounds the workflow-executor budget
(all Playwright ops plus overhead). The node-specific
`request_timeout_seconds` bounds the tool's own per-request
timeout (`page.goto` / `page.wait_for_selector`). A YAML author
writing only `timeout_seconds: 30` on a `browse_url` node almost
always intends the per-request tool budget; the value would
silently land on the executor knob while the tool stayed at its
30s default. The parser refuses the ambiguous case at load time
with a `WorkflowValidationError` that names both knobs — set both
explicitly to disambiguate, or set only `request_timeout_seconds`
if you meant the per-request budget.

**Return-shape narrowing:** the node exposes the same six-field
shape the tool returns:
`{url, final_url, status, content, content_persisted_to, truncated}`.
`format` and other tool-internal fields are intentionally dropped.
Downstream binds via `{{ nodes.<name>.content }}`,
`{{ nodes.<name>.final_url }}`, etc.

**Error routing:** `ToolUnauthorized` (from `dispatch_tool` when
authz denies) is re-wrapped as
`WorkflowNodeFailed(node=<name>, invocation_id=..., cause=exc)`.
`ToolTimeout` and `ExchangeCancelled` propagate to the executor
unchanged so the standard workflow event surface handles them.

---

## Part 5 — Installation

Fresh install:

```bash
uv tool install kolsch                  # or pipx install kolsch
kolsch setup
```

`fetch_url` works out of the box. To enable `browse_url`:

--8<-- "docs/_includes/browse-install.md"

The two-step install is intentional: the pip / uv extra brings in
the Python package, and Playwright's browser download is a separate
step because it's large and belongs in image builds for container
users.

**Authz defaults** land at `src/cli/_setup.py`:

```python
{"what": "kolsch/tools/fetch_url",  "pattern": "*", "effect": "prompt"},
{"what": "kolsch/tools/browse_url", "pattern": "*", "effect": "prompt"},
```

Users override in `platform.yaml` or via `/perm allow` in chat.

---

## Part 6 — Error taxonomy

| Error class | Fires when | Source |
|---|---|---|
| `InvalidUrl` | scheme not http/https or unparseable | both tools |
| `NetworkError` | httpx connect / DNS failure | `fetch_url` |
| `BrowseUrlExtraNotInstalled` | `kolsch[browse]` extra not installed | `browse_url` |
| `BrowserBinaryMissing` | Chromium binary not on disk | `browse_url` |
| `NavigationError` | other `PlaywrightError` at launch/nav | `browse_url` |
| `ToolTimeout` | `request_timeout_seconds` exceeded | both tools |
| `ExchangeCancelled` | Esc pressed / `ctx.cancellation` fired | both tools |
| `WorkflowNodeFailed` (wrapping `ToolUnauthorized`) | authz denied at node dispatch | node only |

All the `ToolExecutionFailed` variants carry `tool_name` +
`error_class`; the underlying exception is preserved via
`__cause__` for diagnostics. Non-2xx HTTP status is NOT an error —
`status` flows through as an integer so callers branch on it.

---

## Part 7 — Explicitly out of scope

The v1 shape is deliberately small. The following are follow-up QoLs,
not oversights:

- **Interactive browser session** (click / type / wait / evaluate JS).
  `browse_url` is one-shot fetch. A stateful session tool ships
  separately when there's a real workflow demanding it.
- **POST / PUT / DELETE** and streaming downloads. `fetch_url` is
  GET-only. `format="bytes_base64"` covers small-binary retrieval;
  full streaming lands later.
- **Multi-step auth flows** (login POSTs, OAuth). `headers` +
  `cookies` cover the "here is my session token" case; anything
  richer is a separate tool.
- **Shared-browser lifecycle** for `browse_url`. Every fetch pays
  ~1–3 s of cold start for its own Chromium context. Fine for
  interactive chat; a shared-browser tier is a natural follow-up
  when workflow authors demand throughput.
- **Rate limiting, robots.txt, SSRF guards, default User-Agent.**
  All belong in a subsequent hardening QoL before public / hosted
  use. The v1 tools trust the workflow author or user to shape
  request patterns responsibly.
- **`fetch_url` workflow node.** Only `browse_url` reserves a
  built-in slot in v1 because the heavy tier is what workflow
  authors have concretely asked for. A `fetch_url` node ships
  when the demand is real, mirroring `BrowseUrlRunner`'s pattern.

---

## Testing

- Unit: `tests/unit/test_fetch_url.py`,
  `tests/unit/test_browse_url.py`,
  `tests/unit/test_browse_url_node_wiring.py`.
- Component: `tests/component/test_fetch_url.py`,
  `tests/component/test_browse_url.py`,
  `tests/component/test_workflow_browse_url.py`.
- Parser: `tests/component/test_workflow_parser.py` (parametrized
  over all 16 built-in node types plus the `browse_url` timeout
  disambiguation validator).

Run the web-fetch-scoped subset via:

```bash
uv run pytest -k "fetch_url or browse_url or builtin_type_name"
```
