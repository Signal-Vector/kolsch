"""Error severity levels — spec §1.1-1.3.

Three levels, each with a load-bearing behavioural meaning:

- ``DIAGNOSTIC`` — the model sees a `tool.failed` (or equivalent)
  event and adapts. The exchange continues. Never raises out of
  ``execute``.
- ``RECOVERABLE`` — the platform retries up to
  ``RuntimeSpec.max_retries``. If a retry succeeds the exchange
  continues; if the budget is exhausted, escalates to ``FATAL``.
- ``FATAL`` — the exchange ends. ``execute`` raises
  ``AgentExecutionError`` carrying ``thread.exchange_failed``;
  ``execute_stream`` yields the failed event and ends.

The level is assigned to each error *type* (spec §1.4) — never chosen
at runtime. Implementations MUST NOT emit the same type at different
levels in different code paths (E-I3).
"""

from enum import StrEnum


class Level(StrEnum):
    """The three error severity levels.

    Inherits from :class:`enum.StrEnum` so envelopes serialize the
    value directly (e.g., JSON encoders emit ``"fatal"`` rather than
    ``"Level.FATAL"``). Behaves like a ``str`` everywhere a string is
    expected.
    """

    DIAGNOSTIC = "diagnostic"
    RECOVERABLE = "recoverable"
    FATAL = "fatal"
