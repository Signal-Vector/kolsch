"""Strip leading `<think>…</think>` reasoning blocks from LLM content.

Some inline-reasoning providers (e.g. openrouter's ``minimax-m3``)
emit their chain-of-thought as a leading ``<think>…</think>`` block
inside the ``content`` field rather than on a separate reasoning
channel. That breaks downstream consumers that expect the content
to start with ``{`` — the autoply synthesizer walking
``nodes.<agent>.text``, a compute-node JSON parse, or any
structured-field access. LiteLLM's own preprocessing recommendation
is *"strip and separate thinking tags from final content"*; kolsch
does not use LiteLLM (see bug-087), so we do it at the engine LLM
handle — the single framework-agnostic point every response crosses,
and the only site with access to the ``model.response`` event where
the reasoning belongs.

Only a **leading** block (optionally preceded by whitespace) is
stripped, so legitimate mid-content ``<think>`` in code samples or
transcripts survives. On an unterminated leading ``<think>`` (cut
off by ``max_tokens`` or a timeout), we treat the whole payload as
reasoning and return empty content — deterministic and safer than
leaking partial CoT downstream.

Streaming caveat: this runs on the final response payload. A future
streaming connector would need delta-aware stripping (deltas bypass
this seam).
"""

from __future__ import annotations

import re

# Anchored at start: optional whitespace, then `<think>…</think>`
# (non-greedy so a legitimate later `<think>…</think>` survives),
# then any whitespace between `</think>` and the real content.
_LEADING_THINK = re.compile(r"\A\s*<think>(.*?)</think>\s*", re.DOTALL)
# Matches a leading `<think>` with no closing tag — truncated case.
_LEADING_THINK_UNCLOSED = re.compile(r"\A\s*<think>", re.DOTALL)


def strip_leading_think(text: str | None) -> tuple[str | None, str | None]:
    """Return ``(clean_text, reasoning)`` after stripping a leading `<think>` block.

    - ``text is None`` or no leading ``<think>``: byte-identical
      passthrough — returns ``(text, None)``.
    - Leading ``<think>…</think>``: returns the remainder (with
      whitespace between ``</think>`` and the first real character
      dropped) plus the reasoning (inner text of the block).
    - Leading ``<think>`` with no ``</think>`` (truncated): returns
      ``("", <text-after-<think>>)`` — the whole payload is treated
      as reasoning; content becomes empty.
    """
    if text is None:
        return None, None
    match = _LEADING_THINK.match(text)
    if match is not None:
        return text[match.end() :], match.group(1)
    unclosed = _LEADING_THINK_UNCLOSED.match(text)
    if unclosed is not None:
        return "", text[unclosed.end() :]
    return text, None
