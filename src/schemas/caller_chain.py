"""The caller chain an authorization decision is judged against.

A [`CallerChain`][schemas.caller_chain.CallerChain] is the ordered
tuple of catalog refs (`<bundle>/agents/<name>`,
`<bundle>/workflows/<name>`) that led to an action, **proximate
first**: the frame that made the call comes first, the frame that
started the whole thing comes last. A rule's `from:` glob matches when
any frame matches; an empty chain never matches a `from:`-scoped rule.

The chain is built explicitly at every producer — the engine exchange,
workflow nodes, workflow agent nodes, the MCP tool bridge — and carried
to every tool authorization call on
[`ToolContext.caller_chain`][tools.decorator.ToolContext]. It lives in
`schemas` so `tools`, `providers`, `engine` and `workflows` can all
import it without a cycle.

Frames are only checked to be non-empty strings, not canonical 3-seg
refs: production frames always come from catalog refs, while resumed
runs and test fixtures can carry older shapes, and a malformed frame
can never match a valid `from:` glob anyway.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CallerChain:
    """Proximate-first caller frames for one authorization context.

    Construct with at least one frame. An empty chain — "no agent or
    workflow frame is known" — is only available through
    [`unattributed`][schemas.caller_chain.CallerChain.unattributed],
    so every site that cannot attribute a call says so by name.

    Attributes:
        frames: The frames, proximate first. Each is a non-empty
            string; production frames are 3-segment catalog refs.

    Raises:
        ValueError: `frames` is empty or holds a non-string or empty
            string frame (surfaced as a Pydantic `ValidationError`
            when built through a model field).
    """

    frames: tuple[str, ...]

    def __post_init__(self) -> None:
        """Freeze `frames` to a tuple; reject empty chains and bad frames."""
        # A list (or other iterable) input would leave the "frozen"
        # chain mutable and unhashable.
        object.__setattr__(self, "frames", tuple(self.frames))
        if not self.frames:
            msg = "CallerChain needs at least one frame; use CallerChain.unattributed()"
            raise ValueError(msg)
        for frame in self.frames:
            if not isinstance(frame, str) or not frame:
                msg = f"CallerChain frames must be non-empty strings; got {frame!r}"
                raise ValueError(msg)

    @classmethod
    def unattributed(cls) -> CallerChain:
        """Return the empty chain: no agent or workflow frame is known.

        `from:`-scoped rules never match it, so a scoped allow does not
        grant and a scoped deny does not block (fail-closed for allow,
        unchanged fall-through for deny).
        """
        return _UNATTRIBUTED

    def push(self, ref: str) -> CallerChain:
        """Return a new chain with `ref` as the new proximate frame."""
        return CallerChain((ref, *self.frames))


_UNATTRIBUTED = object.__new__(CallerChain)
object.__setattr__(_UNATTRIBUTED, "frames", ())
