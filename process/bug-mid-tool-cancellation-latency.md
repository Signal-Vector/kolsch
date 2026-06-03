# Bug — Cancellation does not interrupt a mid-flight tool

**Draft 0.1** *(P2 / latent UX issue surfaced on long-running bash.)*

> Surfaced by `bug-framework-iteration-cap-removal.md` Step 1
> (the cancellation latency notes) and reaffirmed by
> `bug-chat-deny-stops-and-esc-interrupts.md` Step 1 (Q6 latency
> model). Both fixes share the same property; this ticket is
> where the tightening lives.

## Symptom

Esc / Ctrl-C during a streaming exchange does NOT interrupt a
tool that's already executing. The cancellation holds until the
tool's `await` returns, after which the framework's
iteration-boundary poll observes the flip and raises.

Concrete user-facing case: the agent issues a `bash` call that
takes 30s (e.g. `pytest -k slow`). The user presses Esc 5s in.
Nothing visibly happens for 25s; the chat looks frozen. When the
tool finishes, the cancellation finally lands and the exchange
ends.

## Root cause

The framework polls cancellation only at iteration boundaries:

- `src/engine/fake_framework.py:78-79` — top of the `while True:`
  loop.
- `src/engine/crewai_framework.py` — via `_CrewAILLM.call`'s
  per-LLM-call check.

There is no poll site INSIDE the tool dispatch loop in
`fake_framework.py:103-135` (the `for call_dict in
response.tool_calls:` block). And the tool's own
`handle.__call__` does not receive the cancel token at all — the
`ToolHandle` Protocol's signature is `(call: ToolCall) -> Any`.
So even if the tool wanted to poll, it couldn't.

## Why deferred from earlier fixes

- `bug-framework-iteration-cap-removal.md` removed the iteration
  cap; cancellation became the only loop terminator. That fix's
  scope was the cap, not the poll latency.
- `bug-chat-deny-stops-and-esc-interrupts.md` routed deny + Esc
  into the existing CancelToken. That fix's scope was the
  upstream key bindings + the modal short-circuit, not the
  downstream poll latency.

Both fixes share the latency model; tightening it is a separate
concern.

## Acceptance criteria

The bug is fixed when:

1. A tool that polls cancellation mid-flight aborts when the
   token flips, without waiting for its own `await` to return.
2. Tools that DON'T poll (legacy / third-party) continue to work
   — backwards-compatible.
3. The `ToolHandle` Protocol grows a cancel-aware variant (or a
   `ToolContext`-attached cancel token) so tool implementations
   can opt in.
4. `bash` tool: SIGINT (or equivalent) is sent to the subprocess
   when the cancel flips; the subprocess's `wait()` raises
   `CancelledError`.
5. New TC: scripted slow tool + cancel flip; tool aborts within
   100ms of the flip.

## Open questions

- **Tool API shape.** Is the cancel token threaded via
  `ToolContext` (today's pattern for `secrets`, `principal`,
  etc.) or via a new `ToolHandle` overload that accepts a
  `cancel_token` kwarg? The first is less invasive but mixes
  per-call state with per-execution state. Step 1 decides.
- **Framework cooperation.** Does the framework's tool-dispatch
  loop also need a poll between calls in a fan-out? Currently
  the modal short-circuits subsequent calls (see
  `bug-chat-deny-stops-and-esc-interrupts.md` TC-CHAT-DENY-03)
  but the framework still walks the loop. Step 1 confirms
  whether the framework-level poll is needed or if the modal
  short-circuit is sufficient.
- **Sandbox / subprocess.** The sandbox provider's process
  lifecycle needs a kill hook. Today
  `providers.sandbox.SandboxProvider` may not surface one
  cleanly — Step 1 audits.

## Scope estimate

Medium. Touches `tools.handle` protocol, `bash` tool, the
sandbox provider's process management, and the engine's tool
seam (`_ObservedToolHandle.__call__`). ~200 LOC + tests.

## Out of scope

- The Esc-during-streaming binding (already in
  `bug-chat-deny-stops-and-esc-interrupts.md`).
- The modal short-circuit (already in
  `bug-chat-deny-stops-and-esc-interrupts.md`).
- Removing the iteration cap (already in
  `bug-framework-iteration-cap-removal.md`).
