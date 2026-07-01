# Code Documentation Conventions

Rules for **in-code documentation** in Kolsch: docstrings, module
headers, and inline comments meant for human readers and for the future
generated API site.

Process / spec writing conventions live elsewhere; this file is code
only.

---

## Why this exists, and why now

The codebase already uses Google-style docstrings informally
(`src/engine/context.py`, `src/tools/decorator.py`, and others).
[MkDocs](https://www.mkdocs.org/) with the
[`mkdocstrings`](https://mkdocstrings.github.io/) plugin is on the
roadmap for generated API docs but is **not yet wired up**.

Codifying the rules now means that when MkDocs lands, the existing
docstrings render without rewrites — no autodoc landmines, no
heading-level breakage, no orphaned cross-references.

The two guideposts:

- **Google style** — the canonical docstring shape we follow. See the
  [Google Python Style Guide §3.8][google-style] for the upstream spec.
- **MkDocs-readiness** — every rule below is compatible with
  `mkdocstrings` defaults. Where the two could conflict, we pick the
  shape that survives the MkDocs migration.

[google-style]: https://google.github.io/styleguide/pyguide.html#38-comments-and-docstrings

---

## Module docstrings

Every module under `src/` opens with a docstring. Shape:

```python
"""One-line summary of what this module is.

Optional paragraph naming the subsystem contract this module implements
or extends — link to the contract doc rather than re-stating its
content. Keep this header short (≤ ~6 lines); long explanations belong
in the contract, not the module.
"""
```

What it should and shouldn't say:

- **Do** name the subsystem contract — e.g. *"Implements the Tools
  subsystem per `docs/explanation/tools-subsystem.md`."*
- **Don't** cite spec sections inline (`§11.2`, `(spec §1.6)`, etc.).
  The specs are being deprecated; citations rot. If a module maps to
  a specific contract clause, link to the contract doc by markdown
  path instead — `docs/explanation/<contract>.md`. See the
  *Cross-references* section below.
- **Don't** list the module's classes / functions — `mkdocstrings`
  generates that index automatically.
- **Don't** restate the package docstring.

---

## Function and method docstrings

Google style. The shape, in order:

1. **Summary line.** Imperative mood, ends with a period, ≤ 80 chars.
   Fits on one line.
2. **Blank line.**
3. **Extended description** (optional). Multiple paragraphs allowed.
4. **Section blocks** in this order, each preceded by a blank line:
   `Args:`, `Returns:` (or `Yields:` for generators), `Raises:`,
   `Example:` (or `Examples:`).

```python
def append_event(thread_id: ThreadId, event: Event) -> EventId:
    """Append a single event to the thread's JSONL.

    The append is atomic — concurrent writers from the same process
    serialize on a per-thread asyncio lock.

    Args:
        thread_id: The target thread. Must already exist on disk.
        event: The event payload. ``parent_event_id`` is set by the
            store; callers leave it ``None``.

    Returns:
        The assigned event id (monotonic per thread).

    Raises:
        StoreNotFound: The thread directory does not exist.
        StoreCorrupted: The JSONL tail is malformed and the store
            cannot safely resume.
    """
```

### Section rules

- **Section labels are exact.** `Args:`, `Returns:`, `Yields:`,
  `Raises:`, `Example:` / `Examples:`. `mkdocstrings` parses them by
  literal match — `Arguments:` or `Parameters:` will be rendered as
  prose.
- **Each `Args:` entry is `name: description`.** Wrap continuations
  with a four-space indent. **Don't restate the type** — the signature
  is the type source; repeating it splits the source of truth and
  rots.
- **`Raises:` lists `PlatformError` subclasses by name.** This matches
  the project's hard rule that bare exceptions never escape public
  methods (see `AGENTS.md` — Hard rules).
- **`Returns:` for `None`** — omit the section. Don't write
  `Returns: None`.

---

## Class docstrings

Same shape as functions, but the body describes the **contract** the
class fulfills, not the implementation.

```python
class CancelToken:
    """Cooperative cancellation handle for engine execution.

    Polled by the engine at safe points between framework iterations
    and inside the LLM seam; the framework receives the polling
    callback so it can abort mid-loop.

    Attributes:
        _event: Internal asyncio Event. Private; use ``cancel()`` /
            ``is_cancelled()`` instead.
    """
```

- **`Attributes:` is optional.** For Pydantic models and dataclasses
  with explanatory field docstrings, skip it — duplication rots.
  Include it when an attribute's meaning isn't obvious from its name
  and type.
- **Don't document `__init__` separately** unless the constructor has
  non-trivial behavior beyond field assignment. `mkdocstrings` merges
  the class docstring and `__init__` signature on the rendered page.

---

## What to leave out

Anti-rules — these add noise and rot:

- **No restating the signature.** *"Takes a thread id and an event."*
  The signature already says that.
- **No "this function does X."** The verb summary already says that.
- **No version tags, no `@author`, no change-log entries.** Git owns
  those.
- **No `TODO` / `FIXME` inside docstrings.** Put them as comments
  next to the line they refer to, or open an issue.
- **No type repetition.** The signature is the type source.

---

## Cross-references: what survives MkDocs

This is the MkDocs-readiness core. Three forms are acceptable; one is
discouraged.

### Markdown links to repo paths — preferred for docs

```
See [tools subsystem](../explanation/tools-subsystem.md) for the
contract.
```

Renders as a link both as raw markdown and through `mkdocstrings`.
Paths are relative to the file containing the link.

### `mkdocstrings` cross-refs for code symbols — preferred for code

```
Drives [`AgentEngine.execute`][engine.AgentEngine.execute] through
the run loop.
```

Once `mkdocstrings` is wired up, the `[label][target]` form resolves
to the generated API page. Until then, it renders as plain code text —
which is fine. Write these now; they'll start working later.

### reST roles (`:meth:`, `:class:`) — acceptable, not required

The existing codebase sprinkles `:meth:` and `:class:` references
(see `src/engine/context.py`). `mkdocstrings` supports them through
its `crossrefs` extension, so we don't ban them — but new code should
prefer the bracket form above.

### `§X.Y` spec citations — **don't use**

The codebase historically cites spec sections inline (e.g. *"per
§11.2"*, *"(spec §1.6)"*). **Don't write new ones**, and strip them
when you touch a docstring. The specs are being deprecated; citation
numbers rot the fastest. If a docstring needs to point at a contract,
link to the contract doc by markdown path
(`docs/explanation/<contract>.md`) — that link survives renumbering.

### What to avoid

- **Bare URLs without link text.** Use `[label](url)`.
- **Wiki-style `[[link]]` syntax.** Not supported by MkDocs default
  parser.
- **Sphinx-only roles like `:ref:` and `:doc:`** — these don't have
  drop-in `mkdocstrings` equivalents.

---

## Headings inside docstrings

**Don't use `#`-prefixed headings inside a docstring.** `mkdocstrings`
nests docstrings under page headings, and `#` levels inside docstrings
collide with the page's heading hierarchy — the rendered output ends
up with `<h1>` mid-section.

Use Google section labels (`Args:`, `Returns:`, etc.) for structured
sections.

For prose subsections inside an extended description, use **bold
lead-ins**:

```
**Threading.** The provider is thread-safe; callers may share an
instance across asyncio tasks.

**Cancellation.** The provider checks ``ctx.cancellation`` between
each retry…
```

Not:

```
### Threading                 # ← collides with page hierarchy
The provider is thread-safe…
```

---

## Code blocks inside docstrings

Always fence with triple backticks and a language tag:

````
```python
result = await engine.execute(context)
```
````

`mkdocstrings` renders fenced blocks consistently; naked indented
blocks are interpreted differently across renderers and produce
inconsistent output.

For shell snippets, use ` ```bash `. For YAML, ` ```yaml `. Tag
everything — the syntax highlighter needs the hint.

---

## Public vs. private API

Default rule: **underscore prefix = private; everything else is
public**. This is the convention `mkdocstrings`' default filter uses
out of the box, so following it means no extra config later.

- Public symbols **require** a docstring.
- Private symbols (`_helper`, `_InternalState`) may have a docstring
  if the *why* is non-obvious. They don't need one for the API site.
- Don't introduce custom markers (`@public`, `# api:`) — the project
  hasn't chosen one yet, and adopting one prematurely creates churn
  when `mkdocstrings` config lands.

---

## Inline comments

Separate from docstrings — different audience, different rules. See
the existing project rule in `AGENTS.md` (Hard
rules): comments explain the *why* when it's non-obvious; the *what*
should be in the code and the *who/when* is in git.

Short version: **don't write a comment if a future reader could
recover it from the code or the commit message**.

---

## Two reference templates — the house voice, locked

These are the worked examples the docstring sweep produces and the
ones every subsequent docstring should mirror. They land the shape
decisions: short verb-first summary; one explainer paragraph (plus a
sibling-comparison sentence only when there's actually a sibling to
choose between); `mkdocstrings` bracket cross-refs; one-line section
entries with an explainer clause only where it earns its keep; no
spec section citations.

### Function — `AgentEngine.execute`

```python
async def execute(
    self,
    config: ResolvedAgentConfig,
    context: ExecutionContext,
    *,
    inputs: dict[str, Any] | None = None,
) -> Event:
    """Run one exchange and return the final agent.message event.

    The synchronous entry point — drives the exchange through the
    framework, collects the event stream, and returns the closing
    `agent.message` once the framework signals completion. For
    callers that need to react event-by-event, use
    [`execute_stream`][engine.AgentEngine.execute_stream] instead.

    Unlike `execute_stream`, this method **raises** on Fatal failure
    rather than yielding a failed event as the last item.

    Args:
        config: The resolved agent configuration for this exchange.
        context: The runtime payload — thread id, trigger, optional
            cancellation token. See
            [`ExecutionContext`][engine.context.ExecutionContext].
        inputs: Single-shot mode inputs, forwarded to the framework
            unchanged. `None` for conversational triggers.

    Returns:
        The `agent.message` event closing the exchange.

    Raises:
        AgentExecutionError: Fatal exchange failure. Carries the
            `thread.exchange_failed` event.
        PlatformInternalError: Engine invariant violation — should
            be unreachable.
    """
```

### Class — `ExecutionContext`

```python
class ExecutionContext(BaseModel):
    """The per-execute payload carrying the thread and trigger.

    Every call to [`AgentEngine.execute`][engine.AgentEngine.execute]
    takes one of these. The payload identifies which thread the
    exchange runs against and what kicked it off — a user message, a
    force-invoke message that pins a skill, or single-shot inputs.

    Three trigger shapes are valid; the engine dispatches on
    `trigger["kind"]`:

    ```python
    # Conversational
    {"kind": "user_message", "content": <typed>}

    # Conversational with force-invoke
    {"kind": "user_message_with_force_skill",
     "content": <typed>,
     "force_skills": ["<bundle>/skills/<name>", ...]}

    # Single-shot
    {"kind": "invocation", "inputs": <typed>}
    ```

    The remaining fields are optional cross-call threading:
    `cancellation` for cooperative cancellation (the engine constructs
    an internal no-op token when omitted), `trace_id` for
    workflow→agent trace propagation, and `caller_chain` for authz
    audit framing.
    """
```

### What these examples lock in

- **Summary line:** verb-first, ≤ 80 chars, period-terminated.
- **Extended description:** one paragraph. Add a second short
  paragraph (or a comparison sentence) only when there's a sibling
  to differentiate or a non-obvious behavior to call out — `execute`
  vs. `execute_stream` is the canonical case. Don't pad.
- **Cross-references:** `mkdocstrings` bracket form
  (`` [`name`][module.path] ``) for code symbols; markdown links for
  doc paths.
- **No spec citations.** No `§X.Y`, no `(spec §X.Y)`. Link to the
  contract doc by path if you need to point at one.
- **Section entries:** one line. Add an explainer clause only when
  it tells the reader something the type and name don't.
- **Concrete shape examples** (like the three trigger shapes on
  `ExecutionContext`) belong inside the docstring when the runtime
  contract is "this dict has these shapes" — that's source-of-truth
  tightness worth the lines. Skip them for plain function args
  whose types already explain everything.

---

## Future enforcement (intentionally not enabled here)

`ruff` ships [pydocstyle rules][ruff-d] under the `D` category that can
enforce Google-style docstrings. We don't enable them in this change,
but the path is:

```toml
# pyproject.toml — not enabled yet
[tool.ruff.lint]
select = ["D"]

[tool.ruff.lint.pydocstyle]
convention = "google"
```

Adding this would surface every existing module that violates the
rules above. That sweep is its own follow-up — out of scope here.

[ruff-d]: https://docs.astral.sh/ruff/rules/#pydocstyle-d

---

## What this file deliberately does **not** cover

- Spec / process file conventions (the multi-part narrative shape in
  `process/*.md` and the spec-writer template).
- MkDocs build configuration (`mkdocs.yml`, plugin list, nav, theme).
- API-doc page layout or `mkdocstrings` filter config.
- Retroactive cleanup of existing docstrings — these rules apply to
  new code and to docstrings touched in flight.

Each is a follow-up. This file is forward-compatible with all of
them.
