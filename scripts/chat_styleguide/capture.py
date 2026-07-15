"""Capture engine for the chat styleguide harness.

Mounts each catalog widget in a headless Textual host, drives it
through its visual states, and exports SVG screenshots for both
color and mono (NO_COLOR) variants.
"""

from __future__ import annotations

import inspect
import os
from typing import TYPE_CHECKING, Any

from textual.app import App, ComposeResult

from cli.chat.app import _resolve_css_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from scripts.chat_styleguide.catalog import CatalogEntry
    from textual.widget import Widget


# ---------------------------------------------------------------------------
# Content-height measurement
# ---------------------------------------------------------------------------

# Generous probe height for the measurement pass. The tight capture uses the
# widget's own rendered height so the tile has no wasted blank rows — and,
# crucially, this works for bottom-docked chrome widgets (StatusBar, InfoBar,
# TurnStatus) too, where "last non-blank row" would measure the whole screen.
_MEASURE_HEIGHT = 60

# Number of event-loop pauses after driving a widget's state. More than one is
# needed for widgets that mount child widgets in response to an event (e.g.
# WorkflowRenderer mounting a WorkflowNodeRow per node) — each mount settles on
# a later pause.
_SETTLE_PAUSES = 5


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


class CaptureResult:
    """One captured widget render.

    Attributes:
        widget_name: Widget class name.
        state: State name (e.g. ``"completed"``).
        source_file: Relative path from ``widgets/``.
        variant: ``"color"`` or ``"mono"``.
        width: Capture width in columns.
        svg: The SVG content string.
        label: Human-readable label for gallery tile.
    """

    __slots__ = (
        "label",
        "source_file",
        "state",
        "svg",
        "variant",
        "widget_name",
        "width",
    )

    def __init__(  # noqa: PLR0913
        self,
        widget_name: str,
        state: str,
        source_file: str,
        variant: str,
        width: int,
        svg: str,
    ) -> None:
        self.widget_name = widget_name
        self.state = state
        self.source_file = source_file
        self.variant = variant
        self.width = width
        self.svg = svg
        self.label = f"{widget_name} · {state} · {source_file} · {variant} · {width}w"


# ---------------------------------------------------------------------------
# Host App
# ---------------------------------------------------------------------------


class _HostApp(App[None]):
    """Minimal host app for mounting a single widget.

    Sets ``CSS_PATH`` to the resolved theme (color or mono based on
    ``NO_COLOR`` environment variable) so widget CSS variables resolve.
    """

    def __init__(self, widget: Widget) -> None:
        self._widget = widget
        super().__init__(css_path=_resolve_css_path())

    def compose(self) -> ComposeResult:
        yield self._widget


# ---------------------------------------------------------------------------
# State driver helper
# ---------------------------------------------------------------------------


async def _drive_state(driver: Callable[[Any], Any], widget: Widget) -> None:
    """Invoke a state driver, handling both sync and async callables."""
    result = driver(widget)
    if inspect.iscoroutine(result):
        await result


async def _render(
    entry: CatalogEntry,
    driver: Callable[[Any], Any] | None,
    width: int,
    height: int,
    *,
    title: str,
) -> tuple[str, int]:
    """Mount a fresh widget, drive it into state, and export the SVG.

    A fresh widget + host is built per call so no state leaks between the
    measurement pass and the tight capture pass. Returns the SVG and the
    widget's rendered height (``outer_size.height``) for height measurement.
    """
    widget = entry.make()
    app = _HostApp(widget)
    async with app.run_test(size=(width, height)) as pilot:
        # Expand collapsibles so the body is visible.
        if entry.expand_collapsible:
            widget.collapsed = False
            await pilot.pause()
        # Drive the widget into the target state (skip for frozen widgets).
        if driver is not None:
            await _drive_state(driver, widget)
        for _ in range(_SETTLE_PAUSES):
            await pilot.pause()
        return app.export_screenshot(title=title), widget.outer_size.height


# ---------------------------------------------------------------------------
# Capture functions
# ---------------------------------------------------------------------------


async def _capture_one(
    entry: CatalogEntry,
    state_name: str,
    width: int,
    variant: str,
) -> CaptureResult:
    """Capture a single entry x state x width combination.

    Two passes: the first renders at a generous height to measure the
    widget's true content height; the second re-captures tight at that
    height so the tile has no trailing blank rows.
    """
    driver = entry.states.get(state_name)  # None for frozen widgets

    # Use "(default)" in label for frozen widgets with no explicit state
    display_state = "(default)" if state_name == "_default" else state_name
    title = f"{entry.name} · {display_state}"

    # Pass 1 — measure the widget's natural rendered height.
    _, widget_height = await _render(entry, driver, width, _MEASURE_HEIGHT, title=title)
    height = max(1, min(_MEASURE_HEIGHT, widget_height))

    # Pass 2 — tight capture at the measured height.
    svg, _ = await _render(entry, driver, width, height, title=title)

    return CaptureResult(
        widget_name=entry.name,
        state=display_state,
        source_file=entry.source_file,
        variant=variant,
        width=width,
        svg=svg,
    )


async def capture_entry(
    entry: CatalogEntry,
    variant: str,
) -> tuple[list[CaptureResult], dict[str, str], list[tuple[str, str, str]]]:
    """Capture all states x widths for one catalog entry.

    Args:
        entry: The catalog entry to capture.
        variant: ``"color"`` or ``"mono"``.

    Returns:
        Tuple of ``(results, skipped, errors)``.
    """
    results: list[CaptureResult] = []
    skipped: dict[str, str] = {}
    errors: list[tuple[str, str, str]] = []

    for state_name, reason in entry.skip.items():
        label = f"{entry.name} · {state_name} · {variant}"
        skipped[label] = reason

    # Determine which states to capture
    # Frozen widgets (empty states dict) get a single "_default" capture
    state_names = list(entry.states.keys()) if entry.states else ["_default"]

    for width in entry.widths:
        for state_name in state_names:
            if state_name in entry.skip:
                continue
            try:
                result = await _capture_one(entry, state_name, width, variant)
                results.append(result)
            except Exception as exc:  # noqa: BLE001
                label = (
                    f"{entry.name} · {state_name} "
                    f"· {entry.source_file} · {variant} · {width}w"
                )
                errors.append((label, state_name, str(exc)))

    return results, skipped, errors


async def capture_all(
    catalog: list[CatalogEntry],
) -> tuple[list[CaptureResult], dict[str, str], list[tuple[str, str, str]]]:
    """Run the full capture: color pass + mono pass.

    Returns:
        Tuple of ``(results, skipped, errors)`` where:
        - ``results``: all successful ``CaptureResult`` instances.
        - ``skipped``: ``{label: reason}`` for intentionally skipped states.
        - ``errors``: ``[(label, state, error_msg)]`` for failed captures.
    """
    all_results: list[CaptureResult] = []
    all_skipped: dict[str, str] = {}
    all_errors: list[tuple[str, str, str]] = []

    # --- Color pass ---
    for entry in catalog:
        results, skipped, errors = await capture_entry(entry, "color")
        all_results.extend(results)
        all_skipped.update(skipped)
        all_errors.extend(errors)

    # --- Mono pass (NO_COLOR set before host creation) ---
    old_no_color = os.environ.get("NO_COLOR")
    os.environ["NO_COLOR"] = "1"
    try:
        for entry in catalog:
            results, skipped, errors = await capture_entry(entry, "mono")
            all_results.extend(results)
            all_skipped.update(skipped)
            all_errors.extend(errors)
    finally:
        # Restore original NO_COLOR state
        if old_no_color is not None:
            os.environ["NO_COLOR"] = old_no_color
        else:
            os.environ.pop("NO_COLOR", None)

    return all_results, all_skipped, all_errors
