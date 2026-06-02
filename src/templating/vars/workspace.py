"""The ``workspace`` template variable — Phase 2.2 Step 5 (spec
§4.4 + Part 7).

Single-object — NO list-shaped methods (the Step 5 reviewer
prompt's load-bearing check). Attributes:

- :attr:`WorkspaceVar.path` — absolute workspace root as a string.
- :attr:`WorkspaceVar.summary` — text from
  ``platform.yaml:workspace_summary`` (Phase 2.2 §7) or ``None``
  when unset. NOT a method — ``workspace.summary`` is an
  attribute access; collides with the spec §4.2 list-shaped
  ``.summary()`` method on purpose so the difference between
  single-object and list-shaped variables is unambiguous.
- :attr:`WorkspaceVar.allowed_paths` /
  :attr:`WorkspaceVar.denied_paths` — string lists from
  ``platform.yaml:file_access``.

``__str__`` produces the default rendering per spec §4.4:

```
## Active workspace

<summary content if set>

Path: <path>
You can read from: <allowed_paths joined>
You cannot access: <denied_paths joined>
```

When ``summary`` is ``None``, the summary block is omitted —
no fallback prose per spec §4.4's "the summary block is omitted
(no fallback prose)" note.

``slots=True`` makes the dataclass reject unknown attribute
access cleanly (the Step 5 reviewer's check 4 — typo'd
attributes like ``workspace.allowd_paths`` raise
:class:`AttributeError`).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class WorkspaceVar:
    """The ``workspace`` template variable."""

    path: str
    summary: str | None
    allowed_paths: list[str]
    denied_paths: list[str]

    def __str__(self) -> str:
        """Default rendering — spec §4.4.

        Composition:

        - ``## Active workspace`` header.
        - The summary content (blank line on each side) ONLY when
          :attr:`summary` is non-``None``.
        - ``Path: <path>`` line.
        - ``You can read from: <allowed_paths>`` — empty list
          renders as ``You can read from: `` (a defensible empty
          line; an agent with no read-allowed paths is a real
          edge case the rendering can surface as-is).
        - ``You cannot access: <denied_paths>`` — same shape.
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
