# Bug / Feature — Deny stops the exchange; Esc interrupts at the next cycle boundary

**Draft 0.1** *(three dependency-ordered steps. Both behaviors ship together because they share the cancel-token plumbing — see Part 3.)*

> Reported symptoms (two related UX gaps):
>
> 1. **Deny doesn't stop the agent.** When the user picks `[n]`
>    (deny once) or `[d]` (always deny) in the approval modal,
>    the deny is currently surfaced to the model as a tool error
>    payload (`{"error": ...}`) and the framework keeps looping.
>    The model typically issues another tool call or an
>    apologetic text turn. The user wanted the deny to end the
>    exchange and return control to the prompt.
>
> 2. **No Esc binding during streaming.** Pressing Esc while the
>    agent is working does nothing useful. The existing
>    interrupt path is Ctrl-C
>    (`src/cli/chat.py:415, 459-478` — `KeyboardInterrupt`
>    triggers `cancel.cancel()`, drain, return to prompt). The
>    user wanted Esc to behave the same way: cancel at the next
>    cycle boundary, drain, return to prompt.
>
> Both behaviors are about **returning conversational control to
> the user mid-exchange.** They share the same CancelToken
> wiring; it's cheaper to design them together than ship one
> and re-touch the surface for the other.
>
> Per the framework iteration-cap fix
> (`bug-framework-iteration-cap-removal.md`), cancellation is
> now the only loop-termination mechanism besides the model
> stopping on its own. That makes this fix more load-bearing,
> not less.

---

## Part 0 — Why this matters

Three problems compound:

1. **Deny is a tool error, not a user veto.** Once the model
   sees `"error": "...denied"` in the tool result, it
   reasonably retries, picks a different tool, or generates an
   apology turn. None of those are what the user wanted when
   they picked `[n]`. The user picking `[n]` is the strongest
   "stop and let me redirect" signal in the chat UX; ignoring
   it makes the modal feel toothless.

2. **"Always deny" is doubly broken.** After `[d]`, the rule is
   added — future calls of the same shape auto-deny without
   prompting. Without a loop-stop, the model retries, hits the
   new rule, gets denied again, retries with a slightly
   different shape, etc. The user watches a doom loop of tool
   failures they can't interrupt without Ctrl-C.

3. **Esc is the conventional cancel.** Claude Code, opencode,
   and other terminal agentic CLIs use Esc as the
   "stop-what-you're-doing" key. Users coming from those tools
   reach for Esc first, find nothing happens, then have to
   re-learn Ctrl-C. Ctrl-C also conflicts with the conventional
   "clear current input" meaning at the prompt (which the chat
   honors — `src/cli/chat.py:415-417`), so the dual binding is
   slightly confusing. Esc-during-streaming + Ctrl-C-at-prompt
   is the clean two-key model.

**Design decision (made before this doc):** treat both deny and
Esc as forms of cancellation. They flip the existing
`CancelToken`; the engine's cancellation path
(`asyncio.CancelledError` → `ExchangeCancelled` → emit
`thread.exchange_failed` with `exchange.cancelled`) handles the
rest. The chat surface differentiates the two visually
("[stopped — you denied the tool]" vs. "[interrupted]") but
the underlying mechanism is one path.

---

## Part 1 — What we already know

### Known facts (with citations)

**Cancellation path (existing, working):**
- `ChatSessionState` is the per-session chat state
  (`src/cli/chat.py` near line 395; verify exact line in Step 1).
- Per-exchange wiring: `chat.py:449-454` constructs a
  `CancelToken`, builds the `ExecutionContext`, hands it to
  `engine.execute_stream`.
- Ctrl-C during `renderer.render(event_stream)` is caught at
  `chat.py:459-478` — calls `cancel.cancel()`, writes
  "[interrupted; waiting for engine to cancel...]", drains the
  remaining events so the engine emits
  `thread.exchange_failed` with code `exchange.cancelled`,
  returns to the prompt.
- Framework polls cancellation at the top of each iteration
  (`src/engine/fake_framework.py:88-90`). After the
  iteration-cap removal lands
  (`bug-framework-iteration-cap-removal.md`), this is still the
  only poll site in FakeFramework; CrewAI's adapter threads
  the same callback through `_CrewAILLM`. **"Cancel at the next
  cycle boundary" is already the contract** — this fix just
  routes new sources (deny + Esc) into the same token.

**Modal + authz path:**
- The approval modal lives at `src/cli/_modal.py`. Its
  `__call__` returns `AuthzEffect.ALLOW`, `AuthzEffect.DENY`,
  or `(effect, Rule)` for `[a]/[d]` (lines 190-251).
- The modal is registered against the chat's authz provider
  via `register_prompt_callback` (`src/cli/chat.py:711`) and
  torn down on chat exit (`chat.py:725`).
- On DENY, the authz provider raises `ToolUnauthorized` inside
  `invoke_tool` (`src/tools/subsystem.py:243-245`).
- The observer catches `ToolUnauthorized` as Diagnostic and
  returns the error payload back to the framework so the model
  sees it as a tool result (`src/engine/observers.py:225-237`).
  **This is the current "feed denial to LLM" behavior we want
  to short-circuit.**

**Renderer cancellation surface:**
- `_is_cancellation_code` at `src/cli/_renderer.py:591-592`
  recognizes `engine.cancelled` and `exchange.cancelled` as
  cancellation. The renderer's `_render_cancelled` (lines
  494-510) prints a muted "[cancelled]" sentinel.
- Adding deny-vs-interrupt distinction at the render layer
  means either reading a new event field or having the chat
  layer print the distinct line after the renderer returns.
  The latter keeps the renderer dumb (just an event consumer)
  — Step 1 picks the shape.

### What Step 1 confirms before code lands

- **Where the CancelToken lives across the chat ↔ modal
  boundary.** The modal is registered once at chat startup but
  the `CancelToken` is constructed per-exchange
  (`chat.py:449`). To let the modal flip the token, something
  has to share it: `ChatSessionState.active_cancel`,
  a closure captured at registration time, or a small
  per-exchange re-registration. Step 1 picks the shape.
  Recommendation: `ChatSessionState.active_cancel:
  CancelToken | None`, set before each exchange and cleared
  after. Smallest blast radius, mirrors how
  `pending_clear` / `pending_session_id` already work.

- **How Esc is detected during streaming.** The chat's
  prompt_toolkit `PromptSession` is paused while
  `renderer.render(event_stream)` runs, so prompt_toolkit
  keybindings don't fire. Options:
  - **A)** Wrap the streaming in a `prompt_toolkit.Application`
    that owns the input AND renders the engine's output. Heavy
    refactor — the renderer would need to write through the
    Application's output sink.
  - **B)** Spawn a background `asyncio.Task` that reads stdin
    via `prompt_toolkit.input.create_input()` and watches for
    Esc (`\x1b`). Cancel the task when streaming ends. Modest
    surface; uses prompt_toolkit's cross-platform input
    abstraction without an Application.
  - **C)** Use raw `sys.stdin` + `termios` + a non-blocking
    read loop. POSIX-only; ugly; reinvents what prompt_toolkit
    already abstracts. Skip.
  Recommendation: **B.** Step 1 confirms by reading
  prompt_toolkit's input API and writing a 10-line proof of
  concept that captures Esc without disrupting other terminal
  output.

- **Whether `_modal.py`'s pattern-edit Esc already handles
  KeyboardInterrupt cleanly.** The modal's `_collect_choice`
  loops until a valid choice; if Esc raises a
  KeyboardInterrupt inside the modal, the modal's outer
  `try/except` (or its absence) determines whether the chat
  recovers. Step 1 documents the current behavior so Step 2
  doesn't accidentally regress it.

- **Whether the renderer's existing `_render_cancelled` is
  sufficient for deny.** If the deny path flips the same
  cancel token, the engine emits the same `exchange.cancelled`
  event, and the renderer prints "[cancelled]" — generic. The
  user wanted a distinct visual ("[stopped — you denied the
  tool]"). Step 1 picks the shape:
  - Add a new field to `ExchangeCancelledData` distinguishing
    reason (cleanest, but a schema change).
  - Let the chat print the distinct line AFTER
    `renderer.render` returns, reading
    `state.last_cancel_reason` set by the deny path
    (smallest surface; renderer stays generic).
  Recommendation: the latter.

- **Multiple-tool-call fan-out behavior on deny.** A single LLM
  response can issue multiple tool calls
  (`fake_framework.py:114+` iterates `response.tool_calls`).
  If the user denies the first one, the remaining tool calls
  in the SAME response still fire today (each one prompts the
  modal again or matches a rule). The user-facing behavior we
  want: deny on one tool cancels the whole fan-out at the
  modal level — the chat doesn't prompt for the rest. Step 1
  confirms whether the framework can be made to honor the
  cancellation between tool calls in a fan-out, or whether the
  modal's deny just flips the token and the remaining fan-out
  STILL prompts (which would be a surprising UX). Most likely
  conclusion: the modal flips the token, and as a separate
  guard each subsequent modal call early-returns DENY if
  `state.active_cancel.is_cancelled()` is already True. Tiny
  guard, big UX win.

---

## Part 2 — Acceptance criteria (applies across all steps)

The bug is fixed when **all** hold:

1. **Deny stops the exchange.** When the modal returns
   `AuthzEffect.DENY` (either from `[n]` or `[d]`), the chat
   flips the active `CancelToken`. The current iteration
   completes its in-flight awaitables; the framework's next
   poll observes cancellation and raises; the engine emits
   `thread.exchange_failed` with `exchange.cancelled`; the
   chat returns to the prompt.

2. **The user sees WHY the exchange stopped.** The chat prints
   a distinct line — "[stopped — you denied the tool]" (or
   similarly worded; final wording per Step 2) — instead of the
   generic "[interrupted]". Same code path under the hood,
   different surface message.

3. **`[d]` (always deny) also writes the rule.** The rule is
   persisted (existing modal behavior, unchanged) AND the
   current exchange stops. Both happen; deny doesn't skip the
   rule write.

4. **Mid-fan-out deny stops the whole fan-out.** If a single
   LLM response issued 3 tool calls and the user denies the
   1st, the modal does NOT prompt for the 2nd and 3rd. Each
   subsequent modal call short-circuits to DENY when the
   active cancel token is already flipped. The framework still
   completes the dispatch loop (so the framework's
   bookkeeping stays consistent) but no further user prompts
   appear.

5. **Esc cancels during streaming.** Pressing Esc while the
   renderer is consuming the event stream flips the active
   `CancelToken`. Same downstream path as Ctrl-C: drain,
   return to prompt. Chat prints "[interrupted]".

6. **Ctrl-C still works.** Existing Ctrl-C path
   (`chat.py:459-478`) is unchanged. Esc is additive, not a
   replacement.

7. **Cancellation propagation is unchanged.** The framework's
   cancellation poll at the iteration boundary
   (`src/engine/fake_framework.py:88-90`) is the same. We do
   NOT add new poll sites in this fix; "finish the current
   cycle, then stop" is the contract.

8. **At-prompt Esc does nothing dangerous.** Esc at the
   `"You: "` prompt (when no exchange is running) should be a
   no-op or have prompt_toolkit's default behavior — NOT
   accidentally cancel a not-yet-started exchange or exit the
   chat.

9. **Regression tests cover all four paths**:
   - Deny once stops the loop + prints the distinct message.
   - Deny always writes the rule AND stops.
   - Esc during streaming cancels (renderer-level test with a
     scripted Esc).
   - Modal short-circuits subsequent calls when the cancel
     token is already flipped.

10. **No regression in existing chat tests.** Specifically
    `tests/component/cli/test_chat_modal.py` and
    `tests/component/cli/test_chat_renderer.py` pass
    unchanged (or are updated only for the new distinct
    cancel message, per the Step 1 shape).

### Explicitly out of scope for this bug

- **Schema change to `ExchangeCancelledData` adding a `reason`
  field.** Step 1's recommendation is to keep the schema as-is
  and put the distinct messaging at the chat surface. If Step
  1 picks otherwise, escalate.
- **Adding new event types** (`authz.deny_initiated`,
  `chat.interrupt_requested`, etc.). Reuse the existing
  `exchange.cancelled` shape. The bug is about closing a UX
  loop, not extending the schema.
- **Per-tool-call cancellation polling inside a fan-out.** The
  fan-out completes its dispatch in the current cycle; what
  changes is that subsequent MODAL calls short-circuit. Adding
  framework-level mid-fan-out poll is a separate concern.
- **Esc at the prompt rebinds anything.** Default
  prompt_toolkit behavior at the input prompt stays the same.
- **Replacing Ctrl-C.** Both Esc and Ctrl-C work; Ctrl-C is
  not deprecated.
- **Server / `kolsch run` cancellation surface.** Non-chat
  callers don't have a modal (per `phase2.0-tools-specs.md`
  §2.2 they get auto-deny), so the deny-stops-loop story
  doesn't apply. Esc binding is chat-only.

---

## Part 3 — Step dependency graph

```
Step 1 (verification + design picks)  ←── STOP gate
    │
    ▼
Step 2 (deny + Esc both routed into the shared CancelToken)
    │
    ▼
Step 3 (regression tests + manual e2e + handoff)
```

| Step | Ships | Reviewable in |
|---|---|---|
| **1** | Writeup naming the CancelToken-sharing shape, the Esc-detection approach, and the cancel-reason rendering shape | ~25 min |
| **2** | Modal flips active token on DENY; Esc background task during streaming; distinct chat message for deny vs. interrupt | ~30 min |
| **3** | Regression tests for all four paths + manual e2e + follow-up handoff | ~25 min |

Steps 2 and 3 are intentionally combined for both features
because the CancelToken plumbing is shared. Splitting deny and
Esc into separate PRs would mean touching `ChatSessionState`
and the per-exchange wiring twice for no review-clarity win.

---

## Step 1 — Verification + design picks

- **Builds:** a writeup that pins (a) the CancelToken-sharing
  shape between modal and chat, (b) the Esc-detection approach
  during streaming, (c) the cancel-reason rendering shape, and
  (d) the modal-short-circuit guard for fan-outs.
- **No code changes.** Pure read-only verification + a tiny
  proof-of-concept for Esc detection (~10 LOC, deleted before
  Step 2).
- **Acceptance:** the writeup is posted to the coordinator with
  file:line citations + working PoC pasted inline.

### Developer prompt

```
[standing instructions]

Step 1 — Verify the design before any production code lands.

Read first:
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/bug-chat-deny-stops-and-esc-interrupts.md Parts 0-2.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/cli/chat.py
  (especially the input loop around lines 409-478 and
  the modal-registration block around lines 700-725).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/cli/_modal.py
  (ApprovalModal.__call__ at lines 190-251).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/cli/_renderer.py
  (_render_cancelled at lines 494-510, _is_cancellation_code
  at 591-592).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/engine/fake_framework.py
  (cancellation poll at lines 88-90).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/engine/observers.py
  (ToolUnauthorized handling at lines 225-237).

Verification tasks:

1. CancelToken sharing — pick the shape.
   - Read ChatSessionState (chat.py around line 395) to confirm
     the fields it carries (pending_clear, pending_new_session,
     etc.). The pattern for adding a new transient field is
     visible from these.
   - Recommend: add active_cancel: CancelToken | None to
     ChatSessionState; set it at chat.py:449 right after the
     CancelToken is constructed; clear it in a try/finally
     around renderer.render(...). The modal reads it via the
     SlashContext or via a direct reference passed at modal
     construction.
   - If you prefer a different shape (e.g., closure captured
     at modal registration), argue with file:line.

2. Esc-detection during streaming — pick the approach + PoC.
   - Confirm: chat.py's PromptSession is NOT active while
     renderer.render runs (no Application context). Esc via
     prompt_toolkit keybindings won't fire.
   - Option A: Use prompt_toolkit.input.create_input() in a
     background task. The Input.read_keys() / attach() pattern
     can intercept Esc without owning the full output surface.
     Read prompt_toolkit's input module docs / source to
     confirm.
   - Option B: Wrap the streaming in a prompt_toolkit.
     Application that owns input AND output. Heavier; the
     renderer would need to write through Application's output
     sink. Document why you DID or DID NOT pick this.
   - Option C: raw sys.stdin in cbreak mode. Document why
     skipped (cross-platform / ugly).
   - Write a 10-line PoC that prints "ESC PRESSED" when Esc
     is observed during a sleep(5) and does not block other
     output. Paste it inline in the writeup. The PoC is throw-
     away — don't commit it.

3. Cancel-reason rendering — pick the shape.
   - The renderer's _render_cancelled prints "[cancelled]" or
     "[cancelled — {message}]" based on the event's error
     message. Two ways to distinguish deny vs. interrupt:
     (a) Schema change: add reason: Literal["denied", "interrupted",
         "engine_stop"] to ExchangeCancelledData. Out of scope
         per Part 2 unless Step 1 surfaces a hard reason.
     (b) Chat-surface print: a state field
         (state.last_cancel_reason) set when the chat
         initiates the cancel (in deny path or Esc path),
         consumed after renderer.render returns; the chat
         prints "[stopped — you denied the tool]" or
         "[interrupted]" itself. Renderer prints nothing
         distinct.
   - Recommend (b). If you pick (a), escalate first.

4. Modal short-circuit for fan-outs.
   - Confirm: a single LLM response can issue multiple
     tool_calls. Today, each tool_call calls invoke_tool ->
     authz.allow -> modal prompt (if no rule matches). After
     deny on call 1, calls 2-N still prompt.
   - The fix: at the top of ApprovalModal.__call__, check
     state.active_cancel.is_cancelled() (or the equivalent for
     the chosen sharing shape). If True, immediately return
     AuthzEffect.DENY without rendering the panel. Document
     this guard in the writeup with the exact callsite.

5. Esc-at-prompt safety.
   - Read prompt_toolkit's default behavior for Esc inside a
     PromptSession.prompt_async. If Esc does anything dangerous
     (e.g., submits an empty line), document. If it's a no-op
     (likely), confirm.
   - The Esc-during-streaming binding must NOT leak into the
     prompt; the background task should be cancelled BEFORE
     control returns to the prompt's prompt_async call.

6. Existing-cancellation regression risk.
   - Trace what happens when cancel.cancel() is called WHILE
     the framework is mid-iteration (mid-LLM-call or mid-tool-
     call). Confirm: the in-flight awaitable runs to completion,
     and the next iteration's poll site (line 88-90) raises
     CancelledError. Document the latency model the same way
     bug-framework-iteration-cap-removal.md Step 1 documents
     it — these two fixes share that property.

7. Tests to read / consider updating:
     ls tests/component/cli/
     grep -rn "modal\|approval\|cancel\|interrupt" tests/ --include="*.py"
   List each test that exercises the modal or the cancel path;
   note which need updating in Step 2 (likely:
   test_chat_modal.py for the short-circuit and deny-stops
   behavior; test_chat_renderer.py for the new distinct
   message).

8. STOP and post the writeup before any production code lands.

ESCALATION PATH: if Step 1 reveals that:
 (a) prompt_toolkit's Input abstraction does NOT support
     attaching to stdin without owning the full output (so
     Esc-detection requires an Application wrap), or
 (b) the modal's existing pattern-edit step swallows
     KeyboardInterrupts in a way that conflicts with the new
     Esc-during-modal behavior, or
 (c) sharing state.active_cancel between chat.py and _modal.py
     would create an import cycle that can't be resolved with
     a TYPE_CHECKING guard,
escalate via the Coordinator Escalation Log at the end of this
doc before proceeding.

Acceptance: writeup names every shape with rationale + file:line
citations; PoC for Esc detection demonstrates the chosen
approach works; coordinator approves to proceed.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1 — verification writeup.

NO production code to review. The writeup + the throwaway PoC
are the deliverable.

Checks:

1. The CancelToken-sharing shape is named (recommendation:
   state.active_cancel). The writeup cites the exact lines
   where set + clear happen in chat.py. Vague "share via the
   state" is a fail.

2. The Esc-detection approach is named (recommendation: B —
   prompt_toolkit Input in a background task). The writeup
   includes a working PoC inline. Verify the PoC by reading;
   if it uses an API that doesn't exist in the pinned
   prompt_toolkit, fail.

3. The cancel-reason rendering shape is named
   (recommendation: chat-surface print via
   state.last_cancel_reason). If the writeup picks the schema
   change, verify the rationale outweighs the surface cost.

4. The modal short-circuit guard is named with the exact
   callsite (top of ApprovalModal.__call__, before the panel
   render). If it's missing, fail — fan-out without the guard
   is a UX regression.

5. Esc-at-prompt safety is documented. The writeup confirms
   prompt_toolkit's default Esc behavior at the
   prompt is safe (or documents the mitigation).

6. Existing cancellation latency model is documented and
   matches bug-framework-iteration-cap-removal.md Step 1's
   conclusions (these two fixes share that property).

7. Test files to update are listed with file:line.

8. Any of the three escalation conditions are either
   confirmed not-an-issue with citations or escalated.

Verdict: APPROVE-TO-PROCEED or REVISE with specific gaps.
```

---

## Step 2 — Wire deny + Esc into the shared CancelToken

- **Builds:** the ChatSessionState field; the modal's
  short-circuit guard + deny-flips-token behavior; the Esc
  background task; the distinct chat-surface message for deny
  vs. interrupt.
- **Depends on:** Step 1 approved.
- **Acceptance:** unit + component tests pass; manual smoke
  tests confirm the flow end-to-end.

### Developer prompt

```
[standing instructions]

Step 2 — Wire deny + Esc into the shared CancelToken.

Read first:
- Step 1's writeup in the PR description / coordinator log.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/bug-chat-deny-stops-and-esc-interrupts.md Parts 1-2.
- src/cli/chat.py, src/cli/_modal.py, src/cli/_renderer.py.

Tasks (do them in this order — the state field comes first so
both the modal and the Esc task have something to flip):

1. ChatSessionState (src/cli/chat.py near line 395):
   - Add active_cancel: CancelToken | None = None.
   - Add last_cancel_reason: Literal["denied", "interrupted", "engine_stop"] | None = None.
   (The field names are recommendations; Step 1 may have
   picked otherwise — match Step 1's writeup.)

2. Per-exchange wiring (chat.py:449-478 area):
   - After `cancel = CancelToken()`, immediately
     `state.active_cancel = cancel`.
   - Wrap renderer.render in a try/finally that clears
     state.active_cancel = None and state.last_cancel_reason = None
     when done (success OR exception).
   - The existing Ctrl-C branch (lines 459-478): BEFORE calling
     cancel.cancel(), set state.last_cancel_reason = "interrupted".

3. Modal — short-circuit + deny-flips-token (src/cli/_modal.py):
   - Pass state (ChatSessionState) to ApprovalModal at
     construction. The shape is up to Step 1; recommendation:
     a frozen reference to the state object stored on the
     modal as self._state.
   - At the TOP of ApprovalModal.__call__ (before any panel
     render or input collection), add:
       if self._state.active_cancel is not None and self._state.active_cancel.is_cancelled():
           return AuthzEffect.DENY
     This is the fan-out short-circuit.
   - After collecting the user's choice, when the result will
     be DENY (either single-call [n] or persisted [d]):
       if self._state.active_cancel is not None:
           self._state.last_cancel_reason = "denied"
           self._state.active_cancel.cancel()
     This flip happens BEFORE returning DENY, so by the time
     the authz provider sees DENY and the framework receives
     the tool error, the token is already flipped — the next
     framework iteration observes it.
   - DO NOT skip the rule write for [d]. The rule write happens
     via the existing on_rule_added hook (chat.py:705-709);
     keep that path intact. The exchange stops, but the rule
     persists.

4. Esc background task (src/cli/chat.py near lines 449-478):
   - Implement the Esc listener per Step 1's chosen approach
     (recommendation: prompt_toolkit Input via create_input(),
     started as an asyncio.Task right before renderer.render
     and cancelled in the finally block).
   - When Esc is observed: set state.last_cancel_reason = "interrupted",
     then cancel.cancel().
   - The task MUST be cancelled (asyncio.Task.cancel()) before
     the prompt's prompt_async is awaited next iteration. Do
     this in the finally that clears state.active_cancel.
   - Esc DURING the modal's input collection: out of scope.
     The modal owns its own input; if the user wants to
     abandon the modal, they Ctrl-C it (existing behavior).

5. Distinct chat-surface messaging (chat.py post-render):
   - After renderer.render returns (whether normally or via
     exception), if state.last_cancel_reason is set, print
     the corresponding line:
       "denied"      -> "[stopped — you denied the tool]"
       "interrupted" -> (the existing "[interrupted; waiting
                         for engine to cancel...]" line ALREADY
                         prints during the KeyboardInterrupt
                         branch; this branch just confirms the
                         drain finished — see below)
   - Keep the existing "[interrupted; waiting for engine to
     cancel...]" line on the Ctrl-C / Esc path AS-IS (it's a
     status line printed mid-cancel). The post-render print is
     a confirmation that the cancel landed cleanly.
   - Final wording is a small UX call. Pick concise, parallel
     phrasing. "[stopped — you denied the tool]" and
     "[stopped — interrupted]" is a fine pair. Match the
     renderer's existing tone (muted style).

6. Tests to update (Step 1 listed these):
   - tests/component/cli/test_chat_modal.py:
     - Add a test that constructs a modal with a fake state
       that has active_cancel.is_cancelled() == True; assert
       __call__ returns DENY immediately without invoking
       input_fn.
     - Add a test that the modal flips active_cancel on DENY.
   - tests/component/cli/test_chat_renderer.py:
     - If the distinct cancel message is rendered by the
       renderer (NOT recommended per Step 1), update the
       assertions. If it's printed by the chat layer (the
       recommended shape), no renderer test changes needed.

7. NO change to:
   - The Framework protocol or either Framework implementation.
   - The engine's tool dispatch or observer.
   - The renderer's _render_cancelled (unless Step 1 chose
     the schema-change shape — escalate first).
   - The authz provider or the ToolUnauthorized handling.

Scope: ~120 LOC across chat.py + _modal.py + ~80 LOC tests.

ESCALATION PATH: if implementing the Esc task reveals that
prompt_toolkit's create_input() cannot coexist with the chat's
output writes (e.g., they fight over stdin/stdout owning the
terminal), escalate. The PoC in Step 1 was supposed to surface
this — if it didn't, Step 1's PoC was insufficient and we need
to revise the verification, not patch in Step 2.

Acceptance: existing tests still green; new modal tests pass;
manual smoke (run kolsch chat, deny a tool, confirm the
exchange stops AND prompt returns; press Esc mid-exchange,
confirm same).
```

### Reviewer prompt

```
[standing instructions]

Review Step 2 — wire deny + Esc into the shared CancelToken.

Checks:

1. ChatSessionState has the two new fields (active_cancel +
   last_cancel_reason) and they're cleared in a finally block
   around renderer.render. Verify the finally runs on BOTH
   the success path AND the KeyboardInterrupt path.

2. ApprovalModal has the short-circuit guard at the TOP of
   __call__, BEFORE any panel render or input collection.
   Without this guard, fan-out denies prompt the user
   multiple times — verify by reading.

3. The modal flips active_cancel.cancel() on DENY BEFORE
   returning DENY. Order matters: if the cancel flip happens
   AFTER the return, the framework can race ahead and call
   the LLM with the error payload before observing the
   cancel. Verify by reading.

4. For [d]: the rule write still happens (the on_rule_added
   hook is still called). Deny stops the exchange but the
   rule persists. Verify by tracing: modal returns
   (DENY, Rule); authz provider's prompt-callback handling
   inserts the rule + fires the hook (existing path); modal
   ALSO flipped the cancel token (new behavior). All three
   happen.

5. The Esc background task is constructed BEFORE
   renderer.render and cancelled in a finally. If the task
   leaks past the prompt, the next prompt_async would compete
   with the task for stdin — verify by reading the lifecycle.

6. Esc detection uses the approach picked in Step 1 (the PoC
   should match the production code shape). If the production
   code uses something the PoC didn't validate, fail.

7. The distinct post-render message is printed:
   - "denied" branch shows a deny-specific line.
   - "interrupted" branch shows an interrupt-specific line.
   - Neither branch silently no-ops.

8. NO changes to:
   - Framework protocol / implementations.
   - Engine tool dispatch / observer.
   - Renderer's _render_cancelled.
   If any of these are touched, fail and ask for the scope
   to be tightened.

9. Existing test_chat_modal.py and test_chat_renderer.py pass
   without UNRELATED changes. New tests for the short-circuit
   and the deny-flips-token are present.

10. Run the FULL test suite. No regressions.

Verdict: PASS or FAIL with specific defects.
```

---

## Step 3 — Regression tests + manual e2e + handoff

- **Builds:** new regression tests pinning all four behaviors
  + manual verification against a real LLM.
- **Depends on:** Step 2 in main.
- **Acceptance:** new tests pass; manual run shows both deny
  and Esc returning control to the prompt cleanly.

### Developer prompt

```
[standing instructions]

Step 3 — Regression tests + manual end-to-end.

Read first:
- Step 2's PR.
- tests/component/cli/test_chat_modal.py and
  test_chat_renderer.py as templates.
- process/test-catalog.md.

Tasks:

1. Add tests/component/cli/test_chat_deny_stops.py:

   TC-CHAT-DENY-01 — Deny once stops the active exchange.
     def test_deny_once_flips_cancel_token():
         """Modal returning DENY in single-call mode flips the
         shared CancelToken so the framework observes
         cancellation on next iteration."""
         # Setup: ChatSessionState with a CancelToken assigned to
         # active_cancel; ApprovalModal constructed with the state.
         # Action: await modal(rule_id=None, action="tool.bash",
         #                     target="rm -rf /") with input_fn
         #         scripted to return "n".
         # Expected:
         #   - return value == AuthzEffect.DENY
         #   - state.active_cancel.is_cancelled() == True
         #   - state.last_cancel_reason == "denied"

   TC-CHAT-DENY-02 — Always-deny flips the token AND emits the rule.
     def test_deny_always_flips_cancel_and_returns_rule():
         """Modal returning DENY with a rule ([d] path) flips
         the cancel token AND returns the rule for persistence."""
         # Setup: same as DENY-01 plus a scripted pattern-edit
         #        accepting the suggested pattern.
         # Action: input_fn scripts "d" then "" (accept suggested).
         # Expected:
         #   - return value is (AuthzEffect.DENY, Rule(...))
         #   - state.active_cancel.is_cancelled() == True
         #   - state.last_cancel_reason == "denied"
         #   - the returned Rule has effect="deny" and a
         #     pattern matching what suggest_pattern produced
         #     for ("bash", "rm -rf /")

   TC-CHAT-DENY-03 — Fan-out short-circuit.
     def test_modal_short_circuits_when_cancel_already_flipped():
         """When the modal is invoked AFTER active_cancel was
         already cancelled, it returns DENY immediately without
         calling input_fn."""
         # Setup: ChatSessionState with active_cancel.cancel()
         #        already called; modal constructed with a
         #        SCRIPTED input_fn that would raise if called.
         # Action: await modal(...).
         # Expected:
         #   - return value == AuthzEffect.DENY
         #   - input_fn was never called (assert via a sentinel
         #     or a Mock).

2. Add tests/component/cli/test_chat_esc.py (per Step 1's
   Esc-detection approach):

   TC-CHAT-ESC-01 — Esc binding flips the cancel token.
     def test_esc_during_streaming_flips_cancel_token():
         """An Esc keystroke delivered to the chat's input
         abstraction during a streaming exchange flips
         state.active_cancel."""
         # Setup: ChatSessionState with active_cancel set;
         #        the Esc-listener constructed per Step 2's
         #        implementation; a fake input that delivers
         #        the bytes for Esc (\x1b).
         # Action: start the listener; deliver the Esc bytes;
         #         await briefly.
         # Expected:
         #   - state.active_cancel.is_cancelled() == True
         #   - state.last_cancel_reason == "interrupted"

   TC-CHAT-ESC-02 — Listener is torn down with the exchange.
     def test_esc_listener_stops_when_exchange_ends():
         """When renderer.render completes (success or failure),
         the Esc listener is cancelled. A subsequent Esc has
         no effect on the (no-longer-active) state."""
         # Setup: simulate the chat input-loop iteration's
         #        try/finally around renderer.render.
         # Action: enter the block; exit normally; deliver Esc
         #         AFTER exit.
         # Expected:
         #   - state.active_cancel is None after exit (cleared
         #     in finally).
         #   - delivering Esc after exit does NOT raise and
         #     does NOT touch state.

3. Add the four TCs to process/test-catalog.md under a new
   "Chat Deny/Esc (CHAT-DENY/CHAT-ESC)" section.

4. Pre-fix verification:
   - For TC-CHAT-DENY-01: git-stash Step 2's code; run the
     test; confirm it FAILS because the modal does not flip
     the token (the return value is DENY but
     active_cancel.is_cancelled() is False).
   - For TC-CHAT-ESC-01: confirm it FAILS because no Esc
     listener is wired in pre-fix code.
   - Document the failure outputs in the PR description.

5. Manual end-to-end against a real LLM:
   a. Use ~/Documents/kolsch (or fresh workspace) with a real
      provider connected.
   b. Run `uv run kolsch chat` with the helper agent.

   Scenario 1 — Deny once:
     - Prompt: "run `ls -la /Users/andrecohen/Desktop`"
     - When the modal appears, pick [n].
     - Verify:
       * The chat prints "[stopped — you denied the tool]"
         (or whichever phrasing Step 2 chose).
       * The "You:" prompt returns.
       * The session jsonl shows thread.exchange_failed with
         code=exchange.cancelled.
       * The session jsonl does NOT show another model.request
         after the deny (proof that the LLM was NOT given
         the chance to react to the denial).

   Scenario 2 — Deny always:
     - Prompt that triggers a different tool (e.g.
       "read pyproject.toml").
     - Pick [d] and accept the suggested pattern.
     - Verify:
       * Same stop behavior as Scenario 1.
       * The new rule appears in platform.yaml's authz rules
         after the chat exits / on the next chat start.
       * Re-prompting the same tool action in a new exchange
         auto-denies (no modal) AND stops (the
         already-persisted rule auto-denies; modal short-
         circuit fires).

   Scenario 3 — Esc during streaming:
     - Prompt: "list everything in src/engine and read each
       file" (multi-step work).
     - While the agent is calling tools, press Esc.
     - Verify:
       * The chat prints the interrupt message AFTER the
         current tool / cycle finishes (NOT immediately —
         the cancellation is "at next cycle boundary").
       * The "You:" prompt returns.
       * The session jsonl shows thread.exchange_failed with
         code=exchange.cancelled.

   Scenario 4 — Ctrl-C still works:
     - Repeat Scenario 3 with Ctrl-C instead of Esc. Same
       expected behavior.

   Scenario 5 — Esc at the empty prompt is safe:
     - At the "You:" prompt (no exchange active), press Esc
       a few times.
     - Verify: nothing dangerous happens. Default
       prompt_toolkit behavior (likely a no-op or
       move-cursor-to-beginning) is fine.

   c. Capture relevant JSONL excerpts in the PR description.

6. Document any follow-ups Step 1 noted (cancel-reason schema
   field, mid-fan-out framework-level cancellation polling,
   etc.) as separate process/ tickets — do NOT silently drop
   them.

Scope: ~150 LOC tests + catalog entries + manual report; zero
production code changes (unless Step 5 reveals a hole, in which
case revise Step 2 — don't patch in Step 3).

ESCALATION PATH: if the manual run shows that:
 (a) the deny-stops path is racy (sometimes the model gets
     one more turn after deny), or
 (b) Esc fires reliably but the renderer's output is
     corrupted by the input listener (the PoC missed an
     interaction with terminal output), or
 (c) the rule persistence on [d] is broken by the new flow,
escalate; Step 2 needs rework.

Acceptance: all four TCs pass on post-fix code; pre-fix
verification documented; manual e2e shows clean behavior for
all five scenarios; follow-up tickets filed (or N/A).
```

### Reviewer prompt

```
[standing instructions]

Review Step 3 — regression tests + manual e2e.

Checks:

1. All four TCs exist and pass:
   - TC-CHAT-DENY-01 (deny once flips token)
   - TC-CHAT-DENY-02 (deny always flips token + returns rule)
   - TC-CHAT-DENY-03 (modal short-circuits on already-flipped token)
   - TC-CHAT-ESC-01 (Esc listener flips token)
   - TC-CHAT-ESC-02 (listener torn down at exchange end)
   That's five total; the spec called them "four behaviors"
   but the lifecycle test is essential.

2. Pre-fix verification: the PR description shows TC-CHAT-DENY-01
   and TC-CHAT-ESC-01 FAILING on pre-fix code with the
   expected failure modes. If pre-fix verification is absent
   or shows different failure shapes, fail this review.

3. Manual e2e: the PR description shows JSONL excerpts for
   AT LEAST scenarios 1, 2, and 3:
   - Scenario 1: thread.exchange_failed (exchange.cancelled);
     NO model.request after the deny event.
   - Scenario 2: new rule appears in platform.yaml; behavior
     verified across a chat restart.
   - Scenario 3: thread.exchange_failed at the cycle boundary
     (NOT mid-tool — verify by checking which tool.* event
     immediately precedes the exchange_failed).

4. Scenario 4 (Ctrl-C still works) is verified at least by a
   smoke check in the PR description. We don't want a silent
   regression on the existing path.

5. Scenario 5 (Esc at prompt) is verified by a smoke check.
   If Esc at the prompt now does something destructive (e.g.
   exits the chat), fail.

6. The post-render messages match Step 2's chosen wording.
   Inconsistency between code and tests is a fail.

7. process/test-catalog.md gained the new TC entries in full
   Setup/Action/Expected/Verifies format.

8. Full test suite green. Specifically:
   - tests/component/cli/test_chat_modal.py passes (existing
     + new tests).
   - tests/component/cli/test_chat_renderer.py passes (no
     unrelated changes from Step 2, per the no-schema-change
     scope).
   - tc_eng_* goldens still byte-equal (the engine path was
     not touched).

9. Follow-up tickets named in Step 1 are either filed in
   process/ as new bug- or qol- docs OR explicitly listed in
   the PR description as "deferred and tracked here." Silent
   drops are a fail.

Verdict: PASS or FAIL. On PASS, this bug is closed.
```

---

## Part 4 — How to escalate

```markdown
### Escalation E-BUG-DENYESC-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–3
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:** 2–3 paths.
- **Estimated cost:**

---

### Coordinator resolution — E-BUG-DENYESC-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Step 1 finds prompt_toolkit's Input abstraction cannot
  coexist with the renderer's output writes without an
  Application wrap.
- Step 1 finds the modal's pattern-edit step swallows
  KeyboardInterrupts in a way that conflicts with the new Esc
  behavior.
- Step 1 finds an import cycle between chat.py and _modal.py
  that the recommended state-sharing shape would create.
- Step 2 produces a racy deny-stops path (Scenario 1 manual
  test shows the model gets an extra turn).
- Step 2's Esc binding corrupts terminal output during
  streaming.
- The schema-change-vs-chat-surface decision is non-obvious
  from Step 1's analysis — escalate the call rather than guess.

When NOT to escalate:
- The exact post-render message wording. Pick something tight
  and parallel; document in the PR.
- Whether the lifecycle test (TC-CHAT-ESC-02) lives in
  test_chat_esc.py or somewhere else. Pick based on existing
  test-file layout.
- Minor variations in the modal-short-circuit guard's
  implementation (e.g., reading state.active_cancel vs.
  state.active_cancel.is_cancelled() — both express the same
  intent if active_cancel is None / not-None vs.
  cancelled / not-cancelled).

---

## Part 5 — Coordinator Escalation Log

*(empty — populate as escalations land)*

---

## Part 6 — Notes on running these three steps

- **Strict order: 1 → 2 → 3.** Step 1 is the gate. The Esc
  PoC has to actually run before Step 2's plumbing assumes
  the API exists.
- **Step 2 must ship as one PR.** Deny + Esc share the
  CancelToken plumbing; splitting them doubles the diff on
  ChatSessionState and chat.py's per-exchange wiring with no
  review-clarity gain. Step 3's tests are a separate PR
  because they're additive and reviewable in isolation.
- **Don't extend the event schema "while we're here."** The
  `exchange.cancelled` event is reused for both deny and Esc;
  the distinct messaging lives at the chat surface, NOT in
  ExchangeCancelledData. If a future use case needs the
  reason field in the event (e.g., analytics, replay
  differentiation), that's its own ticket.
- **Don't add a mid-tool cancellation poll.** The contract is
  "finish the current cycle, then stop." Mid-tool poll is a
  separate concern (and a real one — flagged in
  bug-framework-iteration-cap-removal.md Step 1's
  cancellation-latency notes — but not this fix).
- **Don't deprecate Ctrl-C.** Both Esc and Ctrl-C are valid
  interrupt keys. The reviewer's Scenario 4 check is the
  guard against accidentally regressing Ctrl-C.

---

## Part 7 — Out of scope

- **Server / `kolsch run` cancellation surface.** Non-chat
  callers don't have a modal; the deny-stops story doesn't
  apply. Esc is chat-only.
- **Mid-tool-call cancellation.** Framework polls cancellation
  at iteration boundaries only. Tighter polling is a separate
  concern.
- **Cancellation event schema field for reason.** Step 1's
  recommended shape keeps `ExchangeCancelledData` unchanged;
  chat surfaces the reason via session state.
- **Replacing Ctrl-C with Esc.** Both work.
- **Rebinding Esc at the prompt.** Default prompt_toolkit
  behavior at the "You:" prompt stays the same.
- **A "denied; please continue" model-feedback turn.** Some
  agents have a UX where the model gets a "user denied this
  tool — try something else" instruction after a deny. We are
  NOT doing that; the user picking [n] is a stop signal, not
  a redirect signal. If the user wants the model to know
  about the denial, they type a follow-up prompt explaining.
- **A user-input-during-streaming feature** (e.g., type
  guidance while the agent is working). Out of scope; this
  fix only adds Esc as an interrupt key, not a general
  inline-input path.
