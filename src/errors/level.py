"""Error severity levels — Diagnostic, Recoverable, Fatal.

The level is assigned to each error *type*, never chosen at runtime —
the same type at different severities in different code paths is a
contract violation. See the
[error model](../../docs/explanation/error-model.md) for the contract
and the engine-side fan-out each level drives.

- `DIAGNOSTIC` — the model sees a `tool.failed` (or equivalent) event
  and adapts. The exchange continues; nothing raises out of `execute`.
- `RECOVERABLE` — the platform retries up to `RuntimeSpec.max_retries`.
  A successful retry continues the exchange; an exhausted budget
  escalates to `FATAL`.
- `FATAL` — the exchange ends. `execute` raises `AgentExecutionError`
  carrying the `thread.exchange_failed` event; `execute_stream` yields
  the failed event and ends.
"""

from enum import StrEnum


class Level(StrEnum):
    """The three error severity levels.

    Inherits from `enum.StrEnum` so envelopes serialize the value
    directly — JSON encoders emit `"fatal"` rather than `"Level.FATAL"`
    — and the value behaves like a `str` everywhere a string is
    expected.
    """

    DIAGNOSTIC = "diagnostic"
    RECOVERABLE = "recoverable"
    FATAL = "fatal"
