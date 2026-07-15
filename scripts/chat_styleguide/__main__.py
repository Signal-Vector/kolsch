"""Chat widget gallery capture harness.

Run:  uv run python -m scripts.chat_styleguide

Produces: build/gallery.html (gitignored)
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.chat_styleguide.capture import capture_all
from scripts.chat_styleguide.catalog import CATALOG, validate_catalog
from scripts.chat_styleguide.render_gallery import render_gallery

# ---------------------------------------------------------------------------
# Output path
# ---------------------------------------------------------------------------

OUTPUT_PATH = Path("build/gallery.html")
GITIGNORE_COMMENT = "# Chat styleguide gallery (spec-071)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_gitignore(path: Path) -> None:
    """Ensure the output path (or its parent) is in .gitignore.

    Creates .gitignore if missing. Appends the path with a comment
    if not already present.
    """
    gitignore = Path(".gitignore")
    target = f"{path.parent}/"  # e.g. "build/"
    # Values that mean "already ignored": the dir with or without a trailing
    # slash, or the full artifact path. Compare slash-insensitively so a
    # pre-existing `build/` is recognised and not duplicated.
    already = {str(path.parent), str(path)}

    if gitignore.exists():
        content = gitignore.read_text()
        for line in content.splitlines():
            stripped = line.strip()
            if stripped and stripped.rstrip("/") in already:
                print(f"✓ {target} already in .gitignore")
                return

    # Append
    with gitignore.open("a") as f:
        if gitignore.exists() and gitignore.stat().st_size > 0:
            # Ensure we start on a new line
            content = gitignore.read_text()
            if content and not content.endswith("\n"):
                f.write("\n")
        f.write(f"\n{GITIGNORE_COMMENT}\n")
        f.write(f"{target}\n")

    print(f"✓ Added {target} to .gitignore")


@dataclass
class SummaryStats:
    """Summary statistics for the capture run."""

    captured: int
    skipped: dict[str, str]
    errors: list[tuple[str, str, str]]
    catalog_size: int
    families: dict[str, int]
    output_path: Path


def _print_summary(stats: SummaryStats) -> None:
    """Print a formatted summary of the capture run."""
    print()
    print("═" * 55)
    print("kolsch chat — widget gallery capture summary")
    print("═" * 55)
    print()

    # Color vs mono breakdown
    color_count = stats.captured // 2
    mono_count = stats.captured - color_count
    print(
        f"Captured:  {stats.captured} tiles ({color_count} color + {mono_count} mono)"
    )
    print(f"Skipped:   {len(stats.skipped)} states")
    print(f"Errors:    {len(stats.errors)}")
    print()

    # Catalog stats
    print(f"Widgets:   {stats.catalog_size} catalog entries")
    family_str = ", ".join(
        f"{fam} ({count})" for fam, count in sorted(stats.families.items())
    )
    print(f"Families:  {family_str}")
    print()

    # Output
    print(f"Output:    {stats.output_path}")
    print()

    # Skipped details
    if stats.skipped:
        print("Skipped states:")
        for label, reason in sorted(stats.skipped.items()):
            print(f"  · {label} — {reason}")
        print()

    # Error details
    if stats.errors:
        print("Errors:")
        for label, state, error_msg in stats.errors:
            print(f"  ✗ {label} / {state} — {error_msg}")
        print()

    # Success message
    if not stats.errors:
        print("✓ Gallery written successfully.")
    else:
        print("⚠ Gallery written with errors.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the capture harness: validate → capture → render → summarize."""
    # Step 1: Validate catalog
    print("Validating catalog...")
    validate_catalog(CATALOG)

    # Step 2: Determine output path
    output_path = OUTPUT_PATH

    # Step 3: Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Step 4: Run capture
    print(f"Capturing {len(CATALOG)} widgets (color + mono)...")
    print()
    results, skipped, errors = asyncio.run(capture_all(CATALOG))

    # Step 5: Render gallery
    print()
    print(f"Rendering gallery ({len(results)} tiles)...")
    render_gallery(results, output_path, catalog=CATALOG)

    # Step 6: Ensure .gitignore
    _ensure_gitignore(output_path)

    # Step 7: Compute stats and print summary
    families: dict[str, int] = {}
    for entry in CATALOG:
        families[entry.family] = families.get(entry.family, 0) + 1

    _print_summary(
        SummaryStats(
            captured=len(results),
            skipped=skipped,
            errors=errors,
            catalog_size=len(CATALOG),
            families=families,
            output_path=output_path,
        )
    )

    # Exit code
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
