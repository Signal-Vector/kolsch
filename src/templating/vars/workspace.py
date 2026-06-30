"""The `workspace` template variable — single-object workspace projection.

Single-object — no list-shaped methods. Attributes: `path`
(absolute workspace root as a string), `summary` (text from
`platform.yaml:workspace_summary` or `None` when unset; an attribute,
not a method — the difference from list-shaped `.summary()` methods
is intentional), `allowed_paths` and `denied_paths` (string lists
from `platform.yaml:file_access`).

`__str__` produces the default rendering:

```
## Active workspace

<summary content if set>

Path: <path>
You can read from: <allowed_paths joined>
You cannot access: <denied_paths joined>
```

When `summary` is `None`, the summary block is omitted with no
fallback prose. `slots=True` makes typo'd attributes (e.g.
`workspace.allowd_paths`) raise `AttributeError` cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class WorkspaceVar:
    """The `workspace` template variable — single-object projection.

    See the module docstring for the field surface and the
    `__str__` rendering shape.
    """

    path: str
    summary: str | None
    allowed_paths: list[str]
    denied_paths: list[str]

    def __str__(self) -> str:
        """Render the default `## Active workspace` block.

        Composition: the header, the summary content (blank line on
        each side) only when `summary` is non-`None`, the `Path:`
        line, the `You can read from:` line, and the `You cannot
        access:` line. Empty `allowed_paths` / `denied_paths` render
        as a defensible empty trailing value rather than being
        omitted — an agent with no read-allowed paths is a real edge
        case the rendering surfaces as-is.
        """
        parts: list[str] = ["## Active workspace"]
        if self.summary is not None:
            parts.append("")
            parts.append(self.summary)
        parts.append("")
        parts.append(f"Path: {self.path}")
        parts.append(f"You can read from: {', '.join(self.allowed_paths)}")
        parts.append(f"You cannot access: {', '.join(self.denied_paths)}")
        return "\n".join(parts)
