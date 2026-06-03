# QoL — Esc watcher loses its asyncio reader after the modal exits

**Draft 0.1** *(P3 / polish — Ctrl-C is the documented fallback.)*

> Surfaced by `bug-chat-deny-stops-and-esc-interrupts.md` Step 1
> (the `inp.attach()` semantics analysis) and confirmed in Step 2
> while reasoning about Esc + modal interaction.

## Symptom

After the user picks `[y]` or `[a]` in the approval modal during
an exchange (paths that ALLOW the tool and continue streaming),
subsequent Esc keystrokes in the same exchange do not flip the
cancel token. Ctrl-C still works.

## Root cause

`prompt_toolkit.input.Input.attach(callback)` installs the
callback via `asyncio.loop.add_reader(fd, callback)`. asyncio's
`add_reader` semantics: a new call with the same `fd` REPLACES
the existing reader; there is no stacking.

The chat's Esc watcher
(`src/cli/chat.py:_watch_esc_during_streaming`) attaches its
reader before `renderer.render(...)`. When a `prompt` rule fires,
`_resolve_prompt` calls the modal, which constructs its own
`PromptSession` and calls `prompt_async`. Internally that attaches
a NEW reader on the same stdin fd, replacing the watcher's.
When the modal exits, its reader is removed — but the watcher's
reader is NOT restored.

After this, the watcher's coroutine is still awaiting on its
`triggered` event, but no callback fires when the user presses
Esc. Cancellation no longer works via Esc until the next
exchange (when a fresh watcher attaches).

## Why this is not blocking

- Ctrl-C still works (signal-based; orthogonal to
  `add_reader`).
- The DENY paths (`[n]`, `[d]`) flip the cancel inside the modal
  before returning, so the most common modal-then-stop case
  doesn't need post-modal Esc.
- The remaining gap (user picks `[y]`, then realizes they want
  to stop) has a 2-key workaround: Ctrl-C.

## Possible fixes

### Option A — Re-attach the watcher after each modal exit

Coordinate between the modal and the chat: after
`PromptSession.prompt_async` returns inside the modal, re-call
`inp.attach(_on_input_ready)` to re-register. Requires either a
hook on the modal's input-fn boundary or a shared "I just used
stdin" signal.

Cost: ~30 LOC + a coordination protocol that has to survive
nested input-fn calls (e.g. the pattern-edit step).

### Option B — Bypass asyncio's reader registry

Run the watcher in a worker thread that uses `select()` on stdin,
calling `loop.call_soon_threadsafe(callback)` on Esc. The
modal's `PromptSession` runs on the main thread and uses
`add_reader`; the worker thread reads from stdin directly so the
two paths don't collide.

Cost: ~50 LOC + a different cancellation contract (worker thread
needs a way to terminate cleanly when the per-exchange `finally`
fires). Cross-platform concerns: Windows lacks `select` on
stdin; `msvcrt.kbhit()` is the analog. The watcher already
no-ops on non-TTY, but a thread-based variant complicates the
cleanup path.

### Option C — Document the limitation

Add a note to `phase2.1-chat-specs.md` §6.1 that Esc is
guaranteed only BEFORE the first modal of an exchange; after a
modal exits, Ctrl-C is the canonical interrupt. Cheapest fix;
loses no functionality the user has today (pre-fix, Esc did
nothing during streaming at all).

## Recommended

Option C now (docs), Option A or B as a P2/P3 follow-up if user
feedback flags the gap.

## Acceptance

When the bug is fixed:

1. After the user picks `[y]` in a modal during an exchange,
   pressing Esc still flips the cancel token.
2. The watcher's lifecycle remains clean: cancelled in the
   per-exchange `finally`; no stale reader past the exchange
   boundary.
3. Existing TC-CHAT-ESC-01/02 pass unchanged; new TC pins
   the post-modal Esc.

## Scope

- ~50 LOC chat.py + ~30 LOC tests.
- No protocol or schema changes.
- Cross-platform validation required if Option B.

## Out of scope

- Mid-tool cancellation polling — separately filed in
  `bug-mid-tool-cancellation-latency.md`.
- Replacing Ctrl-C — Ctrl-C remains the primary interrupt key.
