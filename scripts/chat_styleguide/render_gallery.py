"""Gallery HTML assembler for the chat styleguide capture harness.

Takes a list of ``CaptureResult`` objects and produces a single
self-contained ``gallery.html`` file with inline SVGs, grouped by
widget family, with a palette-swatch header and variant tabs.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from scripts.chat_styleguide.catalog import CATALOG

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.chat_styleguide.capture import CaptureResult
    from scripts.chat_styleguide.catalog import CatalogEntry


# ---------------------------------------------------------------------------
# Palette swatches (from src/cli/chat/themes/theme.tcss)
# ---------------------------------------------------------------------------


_SWATCHES: list[tuple[str, str]] = [
    ("user-border", "#2ea043"),
    ("assistant-fg", "#b0c0d5"),
    ("tool-fg", "#4dd0e1"),
    ("error", "#ff5555"),
    ("warning", "#ffb454"),
    ("hint", "#6e7681"),
]


# ---------------------------------------------------------------------------
# Family display names + ordering
# ---------------------------------------------------------------------------

_FAMILY_ORDER: list[str] = ["transcript", "card_body", "chrome"]
_FAMILY_LABELS: dict[str, str] = {
    "transcript": "Transcript content",
    "card_body": "Card bodies",
    "chrome": "Chrome",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_family_map(catalog: list[CatalogEntry]) -> dict[str, str]:
    """Return ``{widget_name: family}`` mapping from catalog entries."""
    return {entry.name: entry.family for entry in catalog}


def _render_swatch(name: str, color: str) -> str:
    """Render a single palette swatch HTML fragment."""
    return (
        f'<div class="swatch">'
        f'<div class="swatch-color" style="background:{html.escape(color)}"></div>'
        f"{html.escape(name)}"
        f"</div>"
    )


def _render_tile(result: CaptureResult) -> str:
    """Render a single tile (label + inline SVG)."""
    narrow_class = " narrow" if result.width == 40 else ""
    return (
        f'<div class="tile{html.escape(narrow_class)}">'
        f'<div class="tile-label">{html.escape(result.label)}</div>'
        f"{result.svg}"
        f"</div>"
    )


def _render_family_section(
    family: str,
    results: list[CaptureResult],
) -> str:
    """Render one family section with color/mono variant tabs."""
    label = _FAMILY_LABELS.get(family, family)

    color_tiles = [r for r in results if r.variant == "color"]
    mono_tiles = [r for r in results if r.variant == "mono"]

    # Sort within each variant: by widget_name, then state
    color_tiles.sort(key=lambda r: (r.widget_name, r.state))
    mono_tiles.sort(key=lambda r: (r.widget_name, r.state))

    color_html = "\n".join(_render_tile(r) for r in color_tiles)
    mono_html = "\n".join(_render_tile(r) for r in mono_tiles)

    return f"""
  <div class="family-section">
    <h2 class="family-title">{html.escape(label)}</h2>
    <div class="variant-tabs">
      <div class="variant-tab active" onclick="switchVariant(this, '{html.escape(family)}')">color</div>
      <div class="variant-tab" onclick="switchVariant(this, '{html.escape(family)}')">mono</div>
    </div>
    <div class="family-content active" data-family="{html.escape(family)}" data-variant="color">
      <div class="tile-grid">
        {color_html}
      </div>
    </div>
    <div class="family-content" data-family="{html.escape(family)}" data-variant="mono">
      <div class="tile-grid">
        {mono_html}
      </div>
    </div>
  </div>"""


# ---------------------------------------------------------------------------
# Main renderer
# ---------------------------------------------------------------------------


def render_gallery(
    results: list[CaptureResult],
    output_path: Path,
    *,
    catalog: list[CatalogEntry] | None = None,
) -> None:
    """Assemble captured tiles into a self-contained ``gallery.html``.

    Args:
        results: All ``CaptureResult`` instances from the capture engine.
        output_path: Where to write the gallery HTML.
        catalog: The catalog list (used for name→family mapping).
            Defaults to ``CATALOG`` from the catalog module.
    """
    if catalog is None:
        catalog = CATALOG

    family_map = _build_family_map(catalog)

    # Group results by family
    grouped: dict[str, list[CaptureResult]] = {f: [] for f in _FAMILY_ORDER}
    for result in results:
        family = family_map.get(result.widget_name)
        if family is None:
            # Unknown widget — put in transcript as fallback
            grouped["transcript"].append(result)
        elif family in grouped:
            grouped[family].append(result)
        else:
            # Family not in our known set — skip silently
            pass

    # Render palette swatches
    palette_html = "\n    ".join(
        _render_swatch(name, color) for name, color in _SWATCHES
    )

    # Render family sections
    sections_html = "\n".join(
        _render_family_section(family, grouped[family]) for family in _FAMILY_ORDER
    )

    # Assemble full HTML
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>kolsch chat — widget gallery</title>
  <style>
    body {{
      font-family: system-ui, sans-serif;
      background: #1a1a2e;
      color: #e0e0e0;
      margin: 0;
      padding: 20px;
    }}
    h1 {{
      font-size: 1.5rem;
      border-bottom: 1px solid #333;
      padding-bottom: 8px;
    }}
    .palette {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin: 16px 0;
      padding: 12px;
      background: #16213e;
      border-radius: 6px;
    }}
    .swatch {{
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 0.85rem;
    }}
    .swatch-color {{
      width: 20px;
      height: 20px;
      border-radius: 3px;
      border: 1px solid #444;
    }}
    .family-section {{
      margin: 24px 0;
    }}
    .family-title {{
      font-size: 1.2rem;
      color: #4dd0e1;
      margin-bottom: 12px;
    }}
    .variant-tabs {{
      display: flex;
      gap: 8px;
      margin-bottom: 16px;
    }}
    .variant-tab {{
      padding: 6px 16px;
      border-radius: 4px;
      cursor: pointer;
      background: #16213e;
      user-select: none;
    }}
    .variant-tab.active {{
      background: #4dd0e1;
      color: #000;
    }}
    .family-content {{
      display: none;
    }}
    .family-content.active {{
      display: block;
    }}
    .tile-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
      gap: 16px;
    }}
    .tile {{
      background: #16213e;
      border-radius: 6px;
      overflow: hidden;
      border: 1px solid #333;
    }}
    .tile-label {{
      padding: 8px 12px;
      font-size: 0.8rem;
      color: #b0c0d5;
      border-bottom: 1px solid #333;
      background: #0f3460;
      font-family: ui-monospace, monospace;
    }}
    .tile svg {{
      display: block;
      width: 100%;
      height: auto;
    }}
    .tile.narrow .tile-label {{
      color: #6e7681;
    }}
  </style>
</head>
<body>
  <h1>kolsch chat — widget gallery</h1>

  <div class="palette">
    {palette_html}
  </div>

  {sections_html}

  <script>
    function switchVariant(tab, family) {{
      var section = tab.closest('.family-section');
      section.querySelectorAll('.variant-tab').forEach(function(t) {{
        t.classList.remove('active');
      }});
      tab.classList.add('active');
      var variant = tab.textContent;
      section.querySelectorAll('.family-content').forEach(function(c) {{
        if (c.dataset.variant === variant) {{
          c.classList.add('active');
        }} else {{
          c.classList.remove('active');
        }}
      }});
    }}
  </script>
</body>
</html>
"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_content, encoding="utf-8")


__all__ = ["render_gallery"]
