# Bug — `PlatformInternalError` boundary catch-all hides real failure modes

**Draft 0.1** *(follow-up filed from bug-framework-iteration-cap-removal.md.)*

> The engine's top-level try/except at `src/engine/engine.py:364-367`
> wraps **every** non-PlatformError as `PlatformInternalError` —
> surfacing it to the user as `platform.internal_error: Internal
> platform error: <ExceptionClass>`. This is the right behavior for
> genuine engine crashes, but it actively obscures terminal conditions
> the framework chose to raise.

---

## Part 0 — Context

**Filed from:** bug-framework-iteration-cap-removal.md Part 2 final
block ("Out of scope") and Part 0 §2 ("The error code is a lie"). The
parent bug removed the **specific** RuntimeError
(`FakeFrameworkLoopExceededError`) that triggered the symptom, but it
explicitly deferred fixing the broader pattern:

> "Fixing the broader pattern of `PlatformInternalError` swallowing
> non-PlatformError exceptions at `engine.py:364-367`. Removing the
> cause of THIS occurrence is enough; the general improvement is its
> own ticket."

This is that ticket.

---

## Part 1 — The current behavior

```python
# src/engine/engine.py:359-367
try:
    agent_text = await self._run_framework()
except PlatformError as exc:
    await self.emit_exchange_failed(exc)
    return
except Exception as exc:  # noqa: BLE001 — engine boundary
    wrapped = PlatformInternalError.from_exception(exc)
    await self.emit_exchange_failed(wrapped)
    return
```

Any non-PlatformError raised inside the framework call gets
classified as `platform.internal_error`. The error catalog
(`src/errors/engine.py:202-211`) reserves this code for "the engine
itself crashed."

---

## Part 2 — Why this is wrong

Three distinct error classes get conflated:

1. **Real engine crashes** — bugs in our engine code (e.g., a None
   dereference inside `_run_exchange`). These deserve
   `platform.internal_error`. They are the catalog's intent.
2. **Framework-internal terminal conditions** — the framework
   correctly raised a non-PlatformError to indicate a normal-but-
   terminal state (the cap-exhausted case the parent bug fixed was
   one of these). These are NOT engine bugs.
3. **Provider/third-party exceptions** — exceptions from CrewAI's
   internals, litellm timeouts, network errors that escaped the LLM
   driver. These have meaningful types upstream but get flattened
   to `platform.internal_error` with the original class name only
   visible in `original_type`.

All three surface to the user identically. A user seeing
`platform.internal_error: ...SomeError` cannot tell whether the
engine crashed, the framework gave up, or a provider hiccupped
without inspecting the original_type and reasoning about its
semantics.

---

## Part 3 — Where this hurts

| Scenario | Today's classification | Should be |
|---|---|---|
| Real engine crash (e.g. `AttributeError` in assembly) | `platform.internal_error` | `platform.internal_error` ✓ |
| Pre-fix cap exhaustion (`FakeFrameworkLoopExceededError`) | `platform.internal_error` | (fixed by parent bug — class deleted) |
| litellm `RateLimitError` escaping the LLM driver | `platform.internal_error` | `model.upstream_error` |
| CrewAI `OutputParserException` past its retries | `platform.internal_error` | `model.parse_error` or similar |
| Tool wrapper raising `asyncio.TimeoutError` | `platform.internal_error` | `tool.timeout` |

The first row is the only one where today's behavior is correct.

---

## Part 4 — Options (not yet decided)

**Option A — Frameworks must wrap their own.** Each Framework
implementation is responsible for catching its terminal conditions
and re-raising as a PlatformError. The boundary catch-all stays as
the last-resort "real crash" handler.

- **Pro:** keeps the engine boundary simple. Pushes classification
  to the layer that has the most context.
- **Con:** burdens external Framework implementations (acceptable
  per Step 1 §3 — Option B was chosen to be friendly to those
  precisely because the engine boundary should NOT silently
  reclassify their exceptions).

**Option B — Boundary classifier table.** The catch-all matches
against a small set of known exception types (litellm errors,
asyncio errors, etc.) and re-classifies to the right PlatformError.
Unknown types stay as `platform.internal_error`.

- **Pro:** centralizes the policy; no Framework changes needed.
- **Con:** needs ongoing maintenance as upstream libraries evolve;
  risks growing into a fragile lookup.

**Option C — Tighten `platform.internal_error` semantics.** Change
the user-facing message from "Internal platform error: <Class>" to
"Unhandled exception from framework: <Class>" and document the
distinction in the error model. Don't try to reclassify; just label
honestly.

- **Pro:** smallest change; doesn't pretend to know what each
  exception means.
- **Con:** still doesn't help the user pick a recovery strategy
  ("retry? change my prompt? file a bug?").

Recommendation TBD — likely a hybrid of A + C, with A handled in
the CrewAI adapter first (since that's where most of the unknown
exception sources live) and C as a quick-win improvement to the
error message.

---

## Part 5 — Out of scope for this ticket

- **Per-provider exception mapping** (litellm, opencode, openrouter
  specifics) — those belong in the LLM driver layer, not at the
  engine boundary. Filed elsewhere if/when needed.
- **Recoverable-vs-Fatal classification.** The boundary today
  doesn't even attempt this; that's a downstream consequence of
  this ticket, not its target.

---

## Part 6 — Related work

- **Parent bug:** bug-framework-iteration-cap-removal.md — removed
  the specific RuntimeError that triggered the user's symptom but
  left the broader catch-all unchanged.
- **Sibling follow-up:**
  bug-framework-cancellation-mid-call-latency.md — also filed from
  the same parent. Independent surface, also a quality issue.
