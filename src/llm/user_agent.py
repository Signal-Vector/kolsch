"""kolsch's HTTP `User-Agent` — `kolsch/<installed version>`.

Gateways such as OpenCode Go ask clients to identify themselves with
their own user agent rather than a generic SDK or HTTP-library name
(bug-144). Every OpenAI-compatible connector sends this value; the
module is public (not underscore-prefixed) so bundle tools that make
their own HTTP calls can reuse it.
"""

from __future__ import annotations

from functools import cache
from importlib.metadata import PackageNotFoundError, version


@cache
def kolsch_user_agent() -> str:
    """Return `kolsch/<version>` for the installed `kolsch` distribution.

    Falls back to `kolsch/unknown` when the distribution metadata is
    unavailable (a source tree that was never installed).
    """
    try:
        return f"kolsch/{version('kolsch')}"
    except PackageNotFoundError:  # pragma: no cover — dev-env oddity only
        return "kolsch/unknown"
