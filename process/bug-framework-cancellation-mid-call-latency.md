# Bug — Cancellation only observed at iteration boundaries

**Draft 0.1** *(follow-up filed from bug-framework-iteration-cap-removal.md Step 1.)*

> The user-facing symptom would be: pressing Esc during a long-running
> tool call or LLM streaming response does not stop the exchange until
> the call returns on its own. With the framework iteration cap removed
> (per bug-framework-iteration-cap-removal.md), this becomes the only
> escape hatch for runaway loops, so the worst-case wait is "until the
> current LLM/tool call returns."

---

## Part 0 — Context

**Filed from:** bug-framework-iteration-cap-removal.md Step 1 writeup
§5 ("Cancellation latency model") and §8 ("Escalation path c"). The
parent bug doc deferred this work explicitly:

> "Improving mid-tool cancellation responsiveness. If Step 1 notes
> that cancellation only fires at iteration boundaries, that becomes
> a follow-up ticket, NOT a Step in this bug."
> — bug-framework-iteration-cap-removal.md Part 2 "Out of scope" §4.

The parent bug removed the 10-iteration cap. Cancellation is now the
sole termination mechanism for the framework loop, which makes its
latency profile more user-visible than before.

---

## Part 1 — What we know (from Step 1 verification)

### FakeFramework (`src/engine/fake_framework.py`)
- Cancellation polled at the top of each loop iteration
  (now at `fake_framework.py:77-79` after Step 2).
- **Gap**: `await llm(request)` and `await handle(...)` do not poll
  cancellation themselves. A hanging LLM call or tool call will not
  observe cancellation until it returns.

### CrewAIFramework (`src/engine/crewai_framework.py`)
- Cancellation polled at `_CrewAILLM.call:281-282`, BEFORE each LLM
  network call.
- **Gap 1**: `_CrewAILLM.call` does
  `future = asyncio.run_coroutine_threadsafe(coro, self.main_loop)`
  then `future.result()` — the worker thread blocks indefinitely on
  the future. If the engine's `LLMHandle` hangs on the main loop
  (e.g., a slow network read), no cancellation is observed.
- **Gap 2**: `_make_crewai_tool._run` does the same pattern —
  schedules the engine's async `ToolHandle` and blocks on
  `future.result()`. A hanging tool produces the same wait.

### Cancellation latency model (worst-case)

| Scenario | Pre-fix latency | Post-cap-removal latency |
|---|---|---|
| Healthy multi-tool loop | <1 iteration | <1 iteration |
| Hanging LLM call (no streaming) | bounded by 10 iter | bounded by **0 — never observed** until the call returns |
| Hanging tool call | bounded by 10 iter | bounded by **0 — never observed** until tool returns |

In other words: with the cap removed, Esc-during-hang waits for the
hanging operation to return on its own, with no upper bound. Before
the cap was removed, the cap would have fired at the 10th iteration
and ended the exchange (via the boundary mis-wrap). That fallback no
longer exists — by design, because cancellation was always the
correct mechanism.

---

## Part 2 — Why this matters

1. **Removed-cap intent.** The user's design decision in the parent
   bug was "rely on Esc the way Claude Code / opencode do." That
   contract is only as good as cancellation's latency. If Esc visibly
   does nothing for 30+ seconds during a hung call, the user
   experience regresses past what the cap accidentally provided.
2. **Not blocking the parent bug.** The parent bug's acceptance
   criteria are met without this fix — the original
   `platform.internal_error` repro is gone. This is a quality-of-life
   improvement, not a correctness gap.
3. **Bounded blast radius.** This is observable only when a real
   LLM or tool hangs; the FakeLLM + FakeTool paths don't exhibit it.
   The fix surface is small: the LLMHandle / ToolHandle wait points.

---

## Part 3 — Options (not yet decided)

**Option A — Timeout-based poll wrapper.** Wrap the engine's
`LLMHandle.__call__` body with a coroutine that races the actual
call against an `asyncio.Event` set by the cancellation source. On
event-set, cancel the underlying call (`task.cancel()`).

- **Pro:** zero changes to the framework adapter; the seam already
  exists.
- **Con:** the underlying litellm / provider HTTP call may not
  honor cancellation cleanly. Need to verify the timeout-mid-stream
  behavior per provider.

**Option B — Pump cancellation across the threadsafe boundary.**
In `_CrewAILLM.call` and `_make_crewai_tool._run`, replace
`future.result()` with a poll loop:
```python
while not future.done():
    if self.cancellation is not None and self.cancellation():
        future.cancel()
        raise ExchangeCancelled(...)
    try:
        return future.result(timeout=0.1)
    except concurrent.futures.TimeoutError:
        continue
```

- **Pro:** localized; doesn't change the engine's public seam.
- **Con:** adds a 100ms cancellation latency floor; need to verify
  `future.cancel()` actually unwinds the engine-side coroutine.

**Option C — Threaded `Event` signaled from the engine.** The
engine sets a `threading.Event` whenever its `CancelToken` flips;
the worker thread's `future.result(timeout=...)` loops on that
event. Cleanest mechanism but requires the engine to expose the
event on `ExecutionContext` — minor public surface change.

Recommendation TBD — requires a quick spike to confirm provider
HTTP cancellation semantics before committing.

---

## Part 4 — How to escalate

If this is needed urgently (e.g., a user reports Esc-during-hang as a
blocker), escalate to a human with a captured `kolsch chat` session
that demonstrates the hang. Otherwise this sits in the backlog as a
quality-of-life ticket.

---

## Part 5 — Related work

- **Parent bug:** bug-framework-iteration-cap-removal.md (Step 1
  §5 + §8c documented this gap; Step 3 filed this ticket).
- **PlatformInternalError boundary swallowing**
  (`engine.py:364-367`) is a separate concern also tracked as a
  future ticket — removing the specific RuntimeError in the parent
  bug eliminated the user's symptom, but the broader pattern of
  classifying every non-PlatformError as `platform.internal_error`
  remains a misleading boundary behavior.
