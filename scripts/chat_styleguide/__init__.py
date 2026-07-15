"""Chat styleguide capture harness.

This package provides tools to capture every chat widget's rendered
output (color + mono) into a gallery for visual analysis.
"""

from scripts.chat_styleguide.capture import (
    CaptureResult,
    capture_all,
    capture_entry,
)
from scripts.chat_styleguide.catalog import CATALOG, CatalogEntry
from scripts.chat_styleguide.render_gallery import render_gallery

__all__ = [
    "CATALOG",
    "CaptureResult",
    "CatalogEntry",
    "capture_all",
    "capture_entry",
    "render_gallery",
]
