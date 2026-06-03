# Step 1 writeup — bug-chat-deny-stops-and-esc-interrupts.md

**Status:** verification only; no production code touched.
**Verdict ask:** APPROVE-TO-PROCEED to Step 2.

---

## Q1 — CancelToken sharing shape

**Decision: add `active_cancel: CancelToken | None = None` to `ChatSessionState`.**

Rationale and citations:

- `ChatSessionState` already follows the "transient per-exchange
  flag" pattern with `pending_clear`, `pending_new_session`, and
  `pending_session_id` (`src/cli/_slash.py:123-125`). Adding
  `active_cancel` slots into the same pattern — set before the
  exchange, cleared after — with no new module-level state.
- `SlashContext.state` already plumbs `ChatSessionState` to handlers
  (`src/cli/_slash.py:172`). The modal isn't a slash handler so it
  doesn't read state via `SlashContext`; the cleanest path is to
  pass the state to `ApprovalModal` at construction
  (`ApprovalModal(stdout=..., state=...)`).
- Set site: immediately after `cancel = CancelToken()` at
  `src/cli/chat.py:449`. Clear site: a `try/finally` around
  `await renderer.render(event_stream)` at
  `src/cli/chat.py:458`, with the finally setting
  `state.active_cancel = None`. The existing `KeyboardInterrupt`
  branch (`src/cli/chat.py:459-478`) sits inside the `try` so it
  shares the finally clear.
- `last_cancel_reason: Literal["denied", "interrupted"] | None = None`
  joins the same dataclass for Q3 (see below).

Considered and rejected:

- **Closure captured at modal registration time.** Registration
  happens once at `src/cli/chat.py:711`; the `CancelToken` is
  constructed per-exchange at `src/cli/chat.py:449`. A closure
  captured at registration would need a mutable holder
  (`list[CancelToken | None]` or similar), which is just
  `ChatSessionState.active_cancel` under a different name with
  worse discoverability.
- **Re-register the modal per exchange.** Would mean wrapping
  every prompt with an authz-callback churn — visible in the
  modal-clear semantics (`register_prompt_callback` already
  replaces on re-register per `src/providers/authz.py:562-566`).
  Extra ceremony for no win over a `state.active_cancel` field.

**Import-cycle check.** `cli._modal` currently imports nothing
from `cli._slash` (`src/cli/_modal.py:41-55`); `cli._slash`
imports nothing from `cli._modal` (`src/cli/_slash.py:29-47`).
Adding `from cli._slash import ChatSessionState` to `_modal.py`
under a `TYPE_CHECKING` guard (so only the constructor annotation
references it) keeps both modules acyclic. `chat.py` already
imports both — so the actual runtime flow is
`chat.py → _modal.py` and `chat.py → _slash.py` with no edge
between `_modal` and `_slash`. **Escalation condition (c) does
not fire.**

---

## Q2 — Esc-detection approach + PoC

**Decision: Option B — `prompt_toolkit.input.create_input()`
attached in a background `asyncio.Task` for the lifetime of one
`renderer.render` call.**

Why not A (wrap streaming in a `prompt_toolkit.Application`):
the chat's architectural commitment is the non-full-screen idiom
(`src/cli/chat.py:11-15`, TC-CHAT-18). An `Application` wrap
brings the renderer's output through the Application's output
sink, which is exactly the surface the §2.4 commitment forbids
extending. Heavy, and conflicts with a hard rule.

Why not C (`sys.stdin` + cbreak): POSIX-only, reinvents
prompt_toolkit's parser (we'd have to re-disambiguate bare Esc
from `Esc[A`-style escape sequences), and Windows blows up. No
reason given prompt_toolkit already abstracts the same I/O.

Confirmed at the pinned version (`prompt_toolkit==3.0.52`, within
`pyproject.toml`'s `>=3.0.43,<4`):

```
Vt100Input.attach        → True
Vt100Input.read_keys     → True
Vt100Input.raw_mode      → True
Vt100Input.flush_keys    → True
Keys.Escape              → "escape"
```

**PoC (throwaway, lives at `/tmp/esc_poc.py`, will be deleted):**

```python
import asyncio
from prompt_toolkit.input import create_input
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.keys import Keys


async def watch_esc(inp, on_esc, ready_evt=None):
    done = asyncio.Event()

    def _ready():
        # ``read_keys`` returns buffered keys + emits Keys.Escape
        # when the parser's disambiguation timer flushes a lone
        # Esc. ``flush_keys`` forces the flush so tests don't wait
        # the parser's ~0.5s timeout.
        for kp in list(inp.read_keys()) + list(inp.flush_keys()):
            if kp.key == Keys.Escape:
                on_esc()
                done.set()
                return

    with inp.raw_mode(), inp.attach(_ready):
        if ready_evt is not None:
            ready_evt.set()
        await done.wait()


async def self_driving_demo():
    fired = asyncio.Event()
    ready = asyncio.Event()
    with create_pipe_input() as pipe_inp:
        watcher = asyncio.create_task(watch_esc(pipe_inp, fired.set, ready))
        await ready.wait()  # let watcher attach() before sending
        print("PoC: streaming-output-1")
        print("PoC: streaming-output-2")
        pipe_inp.send_text("\x1b")  # ASCII Esc
        await asyncio.wait_for(fired.wait(), timeout=2.0)
        print("PoC: ESC PRESSED")
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(self_driving_demo())
```

Output (verified just now under `uv run python /tmp/esc_poc.py`):

```
PoC: streaming-output-1
PoC: streaming-output-2
PoC: ESC PRESSED
```

Two properties the PoC pins for Step 2:

1. **Output is unaffected.** The two `print(...)` calls interleave
   with the watcher running — the watcher only reads from the
   input fd, never touches stdout. So Renderer/Rich writes during
   streaming are untouched. **Escalation condition (a) does not
   fire.**
2. **Pipe-input shape is what tests use.** `create_pipe_input()`
   is the same seam `tests/component/cli/test_chat_input.py`
   already drives PromptSession through. TC-CHAT-ESC-01 (Step 3)
   can use the exact same shape.

Operational notes for Step 2:

- `Input.attach(callback)` is a sync context manager; build the
  listener with `async with contextlib.AsyncExitStack()` to layer
  `raw_mode()` + `attach()` cleanly.
- The watcher task must be cancelled in the `finally` clearing
  `state.active_cancel` (see Q5) so the next `prompt_async` call
  doesn't fight for stdin. Cancel order: `watcher.cancel()` →
  `await watcher` (suppress `CancelledError`) → clear state.
- The lone-Esc parser timer (`Vt100Parser`'s ~0.5s flush) means a
  bare Esc may take up to ~500ms to register in production. That
  is acceptable — Esc is "next cycle boundary" per Acceptance
  criterion 5, not "immediately"; the framework's own iteration
  poll has comparable latency.

---

## Q3 — Cancel-reason rendering shape

**Decision: (b) chat-surface print via
`state.last_cancel_reason`.** No schema change.

- Per Part 2 "Out of scope" #1 and Part 6 bullet 3, the
  `ExchangeCancelledData` schema stays as-is unless Step 1 finds
  a hard reason. Step 1 found none.
- Set sites for `state.last_cancel_reason`:
  - Existing Ctrl-C branch at `src/cli/chat.py:467`: set
    `state.last_cancel_reason = "interrupted"` BEFORE
    `cancel.cancel()`.
  - New Esc-task callback: set `"interrupted"` BEFORE
    `cancel.cancel()`.
  - Modal deny branch (Q4): set `"denied"` BEFORE
    `cancel.cancel()`.
- Consume site: after `await renderer.render(event_stream)` (and
  also in the `except KeyboardInterrupt` drain) the chat checks
  `state.last_cancel_reason` and prints a distinct one-liner:
  - `"denied"` → `"[stopped — you denied the tool]"`
  - `"interrupted"` → existing `"[interrupted; waiting for engine
    to cancel...]"` is already printed mid-cancel; the post-render
    confirmation line is `"[stopped — interrupted]"` for
    consistency with the deny variant.
- The renderer's `_render_cancelled`
  (`src/cli/_renderer.py:494-513`) stays untouched. It still
  prints the generic `"[cancelled]"` muted suffix on the
  in-progress agent block when the engine emits
  `exchange.cancelled`; the chat layer prints the deny-vs-interrupt
  reason line AFTER the renderer returns. Renderer remains a
  pure event consumer (CHAT-R9).
- Clearing: the same `finally` that clears `state.active_cancel`
  also clears `state.last_cancel_reason = None` so the next
  exchange starts fresh.

Tests at `test_chat_renderer.py` need NO updates — the
renderer's behavior is unchanged.

---

## Q4 — Modal short-circuit for fan-outs

**Confirmed:** multi-tool fan-outs do exist in the framework
loop. `FakeFramework.run_exchange` iterates
`response.tool_calls` at `src/engine/fake_framework.py:103`,
awaiting `handle(ToolCall(...))` for each. Each `handle.__call__`
goes through `_ObservedToolHandle.__call__` →
`tools_subsystem.invoke_tool` → `authz.allow` (per
`src/tools/subsystem.py:241`); a `prompt`-effect rule fires the
modal once per tool call (`src/providers/authz.py:486-490`).
Today, after deny on call 1, calls 2-N still prompt — confirmed
by reading `_match_against_rules` / `_resolve_prompt` —
the provider has no notion of an "already-cancelled" gate.

**Guard placement (exact callsite):** at the top of
`ApprovalModal.__call__` (`src/cli/_modal.py:190-212`), BEFORE
`self._render_panel(...)` at line 214:

```python
async def __call__(self, *, rule_id, action, target):
    if (
        self._state is not None
        and self._state.active_cancel is not None
        and self._state.active_cancel.is_cancelled()
    ):
        return AuthzEffect.DENY
    tool_name = action.removeprefix("tool.")
    self._render_panel(...)
    ...
```

The guard returns the bare `AuthzEffect.DENY` shape (no rule),
so the provider's `_resolve_prompt` does NOT
`_insert_rule_before(...)` and does NOT fire the
`on_rule_added` hook (`src/providers/authz.py:491-513`). The
short-circuit purely suppresses additional user-facing prompts;
it does not pollute the rule list.

`self._state` defaults to `None` so all existing tests in
`test_chat_modal.py` (which construct `ApprovalModal(stdout=...)`
without state — `tests/component/cli/test_chat_modal.py:124, 141,
151, 167, 177, 190, 200, 224, 246, 308, 385`) continue to pass
unchanged. Only the new short-circuit tests construct with state.

---

## Q5 — Esc-at-prompt safety

`PromptSession` is built with `multiline=True` and no
`vi_mode=True`
(`src/cli/chat.py:300-307` builds the kwargs dict;
`vi_mode` is never set so prompt_toolkit defaults to Emacs
bindings). In Emacs mode, **bare Esc is the Meta prefix**: alone
it is a no-op until a follow-up key arrives. Pressing Esc and
then doing nothing leaves the buffer unchanged; the prompt does
not submit, exit, or clear.

The chat does already bind `("escape", "enter")` as a
two-key chord for newline insertion at
`src/cli/chat.py:343`. Adding new top-level Esc handling at the
prompt is NOT part of this bug — the new Esc listener only runs
DURING `renderer.render(...)` and is cancelled before control
returns to `prompt_session.prompt_async("You: ")`.

Lifecycle invariant: the Esc background task is created right
before `await renderer.render(event_stream)` and cancelled in
the `finally` that clears `state.active_cancel` — same block.
The `KeyboardInterrupt` branch also goes through the same
`finally`, so the watcher is torn down before the loop's next
iteration calls `prompt_async`. Verified by tracing — the
existing `try/except KeyboardInterrupt` at
`src/cli/chat.py:457-478` will be re-shaped to
`try/except KeyboardInterrupt/finally` so the cancel of the
watcher is shared.

---

## Q6 — Existing-cancellation regression risk

This fix routes two new sources (deny + Esc) into the same
`CancelToken` that Ctrl-C already flips at
`src/cli/chat.py:467`. Latency model — identical for all three
sources:

1. **`cancel.cancel()` sets an `asyncio.Event`** under the hood
   (`src/engine/context.py:34-38`). Synchronous; no await; no
   IO. The flip is instantaneous from the chat side.

2. **In-flight awaitables finish.** Whoever is awaiting at the
   instant `cancel()` fires (the LLM call inside
   `_ObservedLLMHandle.__call__`, a tool's `handle.__call__`,
   or the framework's `await llm(request)` /
   `await handle(ToolCall(...))`) runs to completion. There is
   no mid-await poll. The framework's only poll site is at the
   TOP of each loop iteration
   (`src/engine/fake_framework.py:78-79`), and the engine's
   own retry-loop poll at `src/engine/engine.py:1089`.

3. **Next iteration observes cancellation.** When the in-flight
   call returns, control reaches the top of the next iteration
   (or the engine's retry-loop guard); `cancellation()` returns
   True; `raise asyncio.CancelledError` fires. The engine's
   `except asyncio.CancelledError` at
   `src/engine/engine.py:1112-1117` translates to
   `ExchangeCancelled(cancelled_at_event_id=...)`. The engine
   surfaces this as `thread.exchange_failed` with code
   `exchange.cancelled` (the same shape Ctrl-C already produces
   — verified by TC-CHAT-08 at
   `tests/component/cli/test_chat_lifecycle.py:724-775`).

4. **Renderer drains.** The KeyboardInterrupt block's
   suppression-wrapped second `renderer.render(event_stream)`
   call at `src/cli/chat.py:477-478` consumes the
   `thread.exchange_failed` event. Same logic applies to the Esc
   path; the same drain is reused.

This is **byte-for-byte the same latency model**
`bug-framework-iteration-cap-removal.md` Step 1 documents (its
Q5 "cancellation reach" notes at process file lines 261-270):
in-flight awaitable finishes, next iteration's poll raises. The
two fixes share the property because they share the poll site.
Removing the iteration cap (already merged in
`403a6e0`) means cancellation is now the only non-natural
loop-termination mechanism — making the deny+Esc routing more
load-bearing, not less.

**Mid-tool latency** (e.g., a 30s `bash` call): cancellation
holds until that tool returns. Same as today's Ctrl-C
behavior. Out of scope per Part 7 "Mid-tool-call cancellation."

---

## Q7 — Tests to read / consider updating

`ls tests/component/cli/`:

```
__init__.py
conftest.py
test_chat_input.py
test_chat_lifecycle.py
test_chat_modal.py
test_chat_no_crewai_leak.py
test_chat_renderer.py
test_chat_slash.py
test_chat_smoke.py
test_permissions.py
```

`grep` of `modal|approval|cancel|interrupt` across `tests/`
(top hits):

| File | Test / area | Step 2 impact |
|---|---|---|
| `tests/component/cli/test_chat_modal.py:121-205` | `test_modal_y_returns_allow_no_rule`, `test_modal_n_returns_deny_no_rule`, `[a]`/`[d]` shape tests | All construct `ApprovalModal(stdout=..., input_fn=...)` without `state=...` — still pass once the new `state` ctor kwarg defaults to `None`. **No change needed.** |
| `tests/component/cli/test_chat_modal.py:212-388` | TC-CHAT-03, TC-CHAT-04 (provider integration), pattern-edit abandon (KeyboardInterrupt @ line 383) | Provider-level path unchanged. Abandon test verifies the modal still propagates `KeyboardInterrupt` from `input_fn` — Step 2 must NOT swallow it. **No change needed; keep as a regression pin.** |
| `tests/component/cli/test_chat_modal.py:395-425` | `test_clear_prompt_callback_makes_subsequent_allow_collapse_to_deny` | Deregistration path unchanged. **No change needed.** |
| `tests/component/cli/test_chat_modal.py:428+` | `test_cmd_chat_clears_modal_callback_on_clean_exit` | Verifies the chat lifecycle still clears the modal. **No change needed.** |
| `tests/component/cli/test_chat_lifecycle.py:720-795` | TC-CHAT-08 — pre-cancelled token produces `exchange.cancelled`, and the cancel-token mechanism check | Same path the new fix reuses. **No change needed; this test pins the path the deny + Esc fix routes into.** |
| `tests/component/cli/test_chat_renderer.py` | `_render_cancelled` / `_is_cancellation_code` assertions | Renderer behavior unchanged under Q3's chat-surface-print decision. **No change needed.** |

New tests added in Step 3 (per the parent doc's Part Step 3
section, but listed here for Step 2 awareness):

- `tests/component/cli/test_chat_modal.py` gains TWO tests:
  (1) modal short-circuits to DENY without invoking `input_fn`
  when `state.active_cancel.is_cancelled()` is True; (2) modal
  flips `state.active_cancel` on DENY. (TC-CHAT-DENY-01 and
  TC-CHAT-DENY-03 from Step 3.)
- `tests/component/cli/test_chat_esc.py` (new file): Esc listener
  flips `state.active_cancel`; listener tears down at exchange
  end. (TC-CHAT-ESC-01, TC-CHAT-ESC-02.)
- Optional addition under the existing modal file: TC-CHAT-DENY-02
  for the `[d]` rule-write + cancel-flip pairing.

---

## Escalation conditions — all clear

| Condition | Result |
|---|---|
| (a) prompt_toolkit Input requires owning the full output to attach | **CLEAR.** PoC confirms `Input.attach()` only consumes input fd; concurrent stdout writes are unaffected. |
| (b) Modal's pattern-edit Esc swallows KeyboardInterrupt | **CLEAR.** `test_modal_pattern_edit_esc_abandons_rule_write` (file lines 367-387) shows `KeyboardInterrupt` from `input_fn` propagates out of `ApprovalModal.__call__`. The new Esc-during-streaming path doesn't touch the modal's `_collect_pattern_edit` at all — those are disjoint surfaces. |
| (c) Import cycle between `chat.py` and `_modal.py` via `state.active_cancel` | **CLEAR.** `cli/_modal.py` imports nothing from `cli/_slash.py` today; adding `from cli._slash import ChatSessionState` under `TYPE_CHECKING` introduces no runtime edge. `chat.py` already imports both. |

---

## Concrete edits Step 2 will make (preview)

For reviewer convenience — no code in this PR; this is the surface
Step 2 will touch.

| File | What changes | Lines today |
|---|---|---|
| `src/cli/_slash.py` | Add `active_cancel: CancelToken \| None = None` and `last_cancel_reason: Literal["denied", "interrupted"] \| None = None` to `ChatSessionState` dataclass | after `:125` |
| `src/cli/_modal.py` | Add optional `state: ChatSessionState \| None = None` ctor kwarg (TYPE_CHECKING import); add short-circuit guard at the top of `__call__`; flip `state.active_cancel` + set `last_cancel_reason = "denied"` BEFORE returning DENY (single-call AND tuple-form paths) | ctor at `:171-184`, guard before `:214`, flip before `:220` and before `:251` |
| `src/cli/chat.py` | Pass `state=state` when constructing the modal at `:227`; wrap per-exchange section in `try/finally` clearing `state.active_cancel = None` + `state.last_cancel_reason = None`; set `state.active_cancel = cancel` after `:449`; set `state.last_cancel_reason = "interrupted"` BEFORE `cancel.cancel()` at `:467`; spawn Esc watcher task right before `:458` and cancel in finally; after `renderer.render` returns, print the post-render line per `state.last_cancel_reason` | `:227, 449-478` |

Total Step 2 estimate: ~120 LOC production + ~80 LOC tests, in
line with the parent doc's scope estimate.

---

## Acceptance — ask for APPROVE-TO-PROCEED

- All three escalation conditions verified clear with file:line
  citations.
- CancelToken-sharing shape named with set/clear sites.
- Esc-detection approach named with a working PoC (output
  pasted above).
- Cancel-reason rendering shape named (chat surface, no schema
  change).
- Modal short-circuit guard's exact callsite named.
- Esc-at-prompt safety documented.
- Cancellation latency model documented; matches the
  iteration-cap-removal Step 1 framing.
- Tests-to-update inventory complete.
