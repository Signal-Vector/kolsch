# QoL — Update phase2.1-chat-specs.md for deny-stops + Esc binding

**Draft 0.1** *(P3 / docs hygiene.)*

> Filed alongside `bug-chat-deny-stops-and-esc-interrupts.md`
> Step 3. The bug fix landed; the spec hasn't been updated.

## What's missing from the spec

`docs/explanation/phase2.1-chat-specs.md` describes the chat's
behavior. Two sections have drifted from the implementation
after `bug-chat-deny-stops-and-esc-interrupts.md` Step 2:

### §4 — modal

Pre-fix language: "the modal returns the user's choice through
the Phase 2.0 callback's tuple shape so the authz provider
performs the in-memory rule insert."

Missing: the modal NOW also flips the chat's per-exchange
`CancelToken` on a DENY result (`[n]` or `[d]`), and
short-circuits to DENY without rendering when the token is
already cancelled (fan-out guard). The spec should describe
both behaviors.

Reference: `src/cli/_modal.py:190-251` (the post-fix
`__call__`).

### §6.1 — cancellation

Pre-fix language: "Ctrl-C during streaming flips the cancel
token; the engine emits `thread.exchange_failed` with code
`engine.cancelled`; the renderer surfaces a muted `[cancelled]`
suffix."

Missing: Esc is now ALSO a cancellation key during streaming,
routed into the same CancelToken via
`_watch_esc_during_streaming`. The chat prints distinct
post-render lines based on `state.last_cancel_reason`:
`[stopped — you denied the tool]` for the modal-deny path;
`[stopped — interrupted]` for Esc / Ctrl-C.

Reference: `src/cli/chat.py:_watch_esc_during_streaming` +
the per-exchange try/finally around `renderer.render`.

## Why this is hygiene

The spec is the authoritative reference for chat behavior. A
future contributor reading §4 / §6.1 without grepping the source
would believe pre-fix behavior is current; that's the kind of
gap that silently re-introduces the bug six months later.

## Surface

- Update `docs/explanation/phase2.1-chat-specs.md` §4 to
  describe:
  - The modal's DENY path flips `state.active_cancel`.
  - The fan-out short-circuit (DENY without rendering when
    cancel is already flipped).
- Update §6.1 to describe:
  - Esc as an additive interrupt key (not a replacement for
    Ctrl-C).
  - The distinct `[stopped — …]` post-render lines.
  - The chat-surface (not renderer) ownership of the deny vs.
    interrupt distinction — preserves the renderer's CHAT-R9
    pure-consumer property.
- Reference the test catalog entries (Part 9.9 CHAT-DENY /
  CHAT-ESC) as the contract pin.

## Acceptance

When the QoL is fixed:

1. §4 and §6.1 reflect the post-fix behavior.
2. The catalog entries are cross-referenced.
3. No code change.

## Scope

Tiny. Docs-only patch. ~50 LOC of markdown.

## Out of scope

- Changing any chat behavior.
- Renaming the `[stopped — …]` lines (the wording is a UX call;
  Step 2 picked something concise and parallel).
- Adding a `reason` field to `ExchangeCancelledData` — explicitly
  out of scope per `bug-chat-deny-stops-and-esc-interrupts.md`
  Part 7.
