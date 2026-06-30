# Step 1 writeup — bug-framework-cancellation-mid-call-latency + bug-mid-tool-cancellation-latency

**Author:** developer
**Status:** PROPOSE-TO-PROCEED.
**Combined ticket** — the two specs cover overlapping ground (same CancelToken, both fixed by introducing a `ToolContext.cancellation` field + framework adapter polling). Shipping as one PR.

---

## 1. Relevance — which gaps from each spec are still live

Cross-referenced both specs against current code per `/tmp/audit`. Result:

### Live (need fixing)

| # | Gap | Site | Spec |
|---|---|---|---|
| 1 | FakeFramework's `await llm(request)` has no cancel-during-await | `src/engine/fake_framework.py:87` | framework-mid-call |
| 2 | FakeFramework's `await handle(ToolCall(...))` has no cancel-during-await | `fake_framework.py:122-128` | framework-mid-call |
| 3 | CrewAI `_CrewAILLM.call`'s `future.result()` has no timeout | `src/engine/crewai_framework.py:332-333` | framework-mid-call |
| 4 | CrewAI `_make_crewai_tool._run`'s `future.result()` has no timeout AND no poll site | `crewai_framework.py:390-403` | framework-mid-call |
| 5 | Tool bodies have no way to see cancellation — no `CancelToken` field on `ToolContext`, `ToolHandle` Protocol signature is `(call: ToolCall) -> Any` | `src/tools/decorator.py:29-72`, `src/engine/framework.py:159` | mid-tool |

### Already fixed (closed by intervening work; spec text now stale)

- **Esc keystroke wired upstream** — `bug-chat-deny-stops-and-esc-interrupts.md` Step 3 added `_watch_esc_during_streaming()` so Esc flips the CancelToken. Both specs reference Esc as a motivating UX case; that wiring is done. What's missing is the latency between flip and observation.
- **Modal deny → cancel flip** — same Step 3. Specs cite this as the "downstream poll latency" concern. The deny flip works; latency tightening is what's left.
- **Framework iteration cap** — `bug-framework-iteration-cap-removal.md` removed the 10-cap. Specs cite this as making cancellation the ONLY terminator, raising the visibility of any latency. Cap is gone; this QoL closes the latency.

### Two specs' acceptance criteria re-mapped

| Spec criterion | Maps to live gap | Test |
|---|---|---|
| framework-mid-call: "cancellation observed during a mid-LLM hang" | Gap 1 (Fake) + Gap 3 (CrewAI) | new TC-CANCEL-MID-LLM-* |
| framework-mid-call: "cancellation observed during a hanging tool call" | Gap 2 (Fake) + Gap 4 (CrewAI) | new TC-CANCEL-MID-TOOL-* |
| mid-tool: "tool that polls cancellation mid-flight aborts when token flips" | Gap 5 (ToolContext field) | new TC-CANCEL-TOOL-POLL |
| mid-tool: "tools that DON'T poll continue to work" | Gap 5 backwards-compat | covered by existing dispatch tests |
| mid-tool: "ToolHandle Protocol grows a cancel-aware variant" | **Rejected — ToolContext approach instead** (see §2 Q1) | n/a |
| mid-tool: "bash tool: SIGINT to subprocess when cancel flips" | Gap 5 + bash integration | new TC-CANCEL-BASH-SIGINT |
| mid-tool: "scripted slow tool + cancel flip; aborts within 100ms" | All gaps | new TC-CANCEL-SLOW-TOOL-100MS |

---

## 2. Resolutions to open design questions

### Q1 (mid-tool spec) — Tool API shape: ToolContext field or ToolHandle overload?

**Decision: `ToolContext.cancellation` field.** Three reasons:

1. **Less invasive** — matches the existing per-call-state pattern alongside `secrets`, `tool_call_id`, `workspace_root`, etc. No Protocol signature change.
2. **Backwards-compatible** — Tools that don't poll continue to work. The field defaults to `None`; tool bodies that ignore it see no change.
3. **Symmetric with how the engine already threads per-call state** — `tool_call_id` was added for the same reason (tools need per-call state the engine knows about). Same pattern.

The spec's alternative (a `ToolHandle` overload accepting `cancel_token` kwarg) was rejected because it bifurcates the Protocol and forces every tool to either ignore the kwarg or annotate it.

### Q2 (mid-tool spec) — Framework cooperation: does the framework's tool-dispatch loop need a between-call poll?

**Decision: yes for FakeFramework, no for CrewAI.**

- **FakeFramework**: the inner `for call_dict in response.tool_calls` loop (`fake_framework.py:103-135`) iterates over fan-out tool calls. With Gap 2 fixed, each call's `await handle(...)` aborts on cancel; the loop naturally exits. **No additional between-call poll needed** — the per-call poll covers it.
- **CrewAI**: the framework doesn't expose a per-call loop the engine controls (it's CrewAI-internal). The per-call cancellation lives entirely in `_CrewAILLM.call` + `_make_crewai_tool._run`. Cross-call fan-out cancellation is CrewAI's problem to handle.

### Q3 (mid-tool spec) — Sandbox / subprocess kill hook

**Decision: handle in the bash tool body, not in `SandboxProvider`.** The current `SandboxProvider.execute` API takes a `work` closure and runs it through; lifecycle management of subprocesses spawned INSIDE that closure is the tool's responsibility. Bash uses `asyncio.create_subprocess_shell` + `proc.kill()` (timeout path). The fix: race `proc.communicate()` against `ctx.cancellation.wait_cancelled()`; on cancel, send SIGINT then SIGKILL after a 500ms grace period.

If a future tool needs sandbox-level kill (e.g., docker variant), the sandbox provider can be extended then. v1 keeps the surface minimal.

### Q4 (framework-mid-call spec) — Option A / B / C

**Decision: hybrid — Option B for CrewAI worker threads, custom Option A-style race for FakeFramework.**

- **CrewAI worker threads** are sync threads; they can't `await wait_cancelled()`. Option B (poll loop on `future.result(timeout=0.1)`) is the only mechanism. On cancel, call `future.cancel()` to abort the main_loop coroutine.
- **FakeFramework** is all-async; the cleanest pattern is `asyncio.wait` racing the call against `cancel_token.wait_cancelled()`. On cancel, cancel the call task and raise. This is Option A's idea but using the existing `CancelToken.wait_cancelled()` rather than a new event.

Both paths share a small `_cancel_aware_await` helper in a new `src/engine/_cancellation.py` module so the polling/racing logic is one place.

---

## 3. Implementation plan — files touched

### 3.1 Core plumbing (4 files)

1. **`src/tools/decorator.py`** — `ToolContext` gains `cancellation: CancelToken | None = None`. Field is optional (defaults None) so Phase 1 programmatic test paths stay byte-identical.

2. **`src/engine/_cancellation.py`** (NEW) — small module exposing:
   - `async race_with_cancel(coro, cancel_token, *, what: str) -> Any` — awaits `coro` racing against `cancel_token.wait_cancelled()`. On cancel: cancel the task, raise `ExchangeCancelled(reason=what)`. Used by FakeFramework + bash tool.
   - `def wait_future_with_cancel(future, cancel_token, *, poll_interval: float = 0.1) -> Any` — synchronous; polls `future.result(timeout=poll_interval)` and checks `cancel_token.is_cancelled()` between polls. On cancel: call `future.cancel()`, raise `ExchangeCancelled`. Used by CrewAI worker threads.

3. **`src/engine/observers.py`** — `_ObservedToolHandle.__call__` reads cancellation from `self._state` (already has it via `state.active_cancel`) and threads it into the `ToolContext` it builds at line 217-224.

4. **`src/tools/dispatch.py`** — no signature change; `ToolContext` already flows through to `defn.func(args, ctx)`.

### 3.2 Framework adapters (2 files)

5. **`src/engine/fake_framework.py`** — wrap line 87 `await llm(request)` in `race_with_cancel(llm(request), cancellation, what="llm_call")`. Same for line 122-128 `await handle(ToolCall(...))`. The polls at line 77-79 stay (covers between-iteration case).

6. **`src/engine/crewai_framework.py`** — replace `future.result()` at lines 333 + 393 with `wait_future_with_cancel(future, self.cancellation, ...)`. Lines 319-320's pre-call poll stays.

### 3.3 Bash tool (1 file)

7. **`src/cli/_starter_bundle/kolsch/tools/bash.py`** — race `proc.communicate()` against `ctx.cancellation.wait_cancelled()`. On cancel: `proc.send_signal(signal.SIGINT)`; wait 500ms; if still alive, `proc.kill()` (SIGKILL). Re-raise the `ExchangeCancelled` so the engine sees it as a cancellation, not a tool failure.

### 3.4 Tests (3 files — new TCs added)

8. **`tests/unit/engine/test_cancellation_helpers.py`** (NEW) — unit tests for `race_with_cancel` + `wait_future_with_cancel`. Cover: completes-first, cancels-first, simultaneous, both-already-set.

9. **`tests/integration/test_framework_no_cap.py`** — extend with:
   - `test_fake_framework_observes_cancel_during_llm_await` — scripted FakeLLM that sleeps 5s; cancel after 100ms; assert exchange ends within 200ms.
   - `test_fake_framework_observes_cancel_during_tool_await` — scripted slow tool; cancel after 100ms; assert tool aborts within 200ms.

10. **`tests/component/tools/test_bash_cancellation.py`** (NEW) — bash tool with cancel:
    - `test_bash_subprocess_sigint_on_cancel` — `sleep 30` bash; cancel after 100ms; assert subprocess dies, ExchangeCancelled raised, total wait <500ms.
    - `test_bash_completes_normally_without_cancel` — backwards-compat smoke.

CrewAI integration tests use a marked-as-slow `pytest.mark.slow` or skip-if-no-key pattern; the unit tests for `wait_future_with_cancel` cover the mechanism without needing CrewAI itself.

### 3.5 Backwards compat

- `ToolContext.cancellation` defaults to `None`. Tools that don't read it work unchanged.
- All current ToolContext constructions (engine + tests) silently pass `cancellation=None`; no compile errors.
- Framework adapters' wrap is conditional: when `cancellation is None` (legacy programmatic paths), the wrap is a no-op pass-through.

---

## 4. Acceptance criteria self-check

| Spec criterion | Verifying test |
|---|---|
| framework-mid-call AC: hanging LLM cancellable | `test_fake_framework_observes_cancel_during_llm_await` + `test_cancellation_helpers::race_*` |
| framework-mid-call AC: hanging tool cancellable | `test_fake_framework_observes_cancel_during_tool_await` |
| framework-mid-call AC: CrewAI worker threads pump cancel | `test_cancellation_helpers::wait_future_with_cancel_*` (mechanism; CrewAI integration would need real provider) |
| mid-tool AC: tool that polls aborts on cancel | `test_bash_subprocess_sigint_on_cancel` |
| mid-tool AC: tools that don't poll keep working | All existing dispatch tests + `test_bash_completes_normally_without_cancel` |
| mid-tool AC: ToolContext carries cancellation | `tests/unit/test_tool_context_carries_cancellation` |
| mid-tool AC: bash sends SIGINT | `test_bash_subprocess_sigint_on_cancel` |
| mid-tool AC: scripted slow tool aborts within 100ms | `test_bash_subprocess_sigint_on_cancel` (tightened to <500ms total for SIGINT→SIGKILL grace; the cancel-flip-to-SIGINT delay is bounded by the helper's polling, in practice <50ms) |

---

## 5. Risks

- **R1**: `asyncio.create_subprocess_shell` on macOS doesn't always honor `proc.kill()` cleanly for child processes spawned by the shell. Mitigation: use `os.killpg` against the subprocess group, falling back to `proc.kill()` if the group isn't set up. The bash tool already uses `start_new_session=True` per the audit's quick look — verify in Step 2.
- **R2**: `race_with_cancel`'s task cancellation may leak HTTP connections (litellm). Acceptable per the spec — the connection eventually times out; we don't claim clean shutdown of in-flight provider HTTP.
- **R3**: `CancelToken.wait_cancelled()` is async; we can only race it against the LLM/tool task. Worker threads use the synchronous `is_cancelled()` poll path. Documented in `_cancellation.py`.

---

## 6. Out of scope

- **The PlatformInternalError boundary classification** flagged in framework-mid-call §5. Separate ticket. This QoL doesn't touch the boundary; if a tool raises `ExchangeCancelled` mid-flight, the engine already treats it as a cancellation (the existing path).
- **Streaming token-by-token cancellation of LLM responses.** Today the LLM call is one round-trip; cancelling it mid-stream cancels the whole turn. If/when streaming-token-cancel is needed, separate ticket.

---

**Verdict from developer**: ready for Step 2.
