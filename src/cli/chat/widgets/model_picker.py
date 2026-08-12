"""``ModelPickerScreen`` — spec-132's `/model` picker (Step 3: shell only).

Renders a **given, static** ``list[ModelChoice]`` (`cli._model_catalog`)
as a three-column list — name / provider / context size — inside a
Textual ``ModalScreen``. This step builds only the shell: mounting,
rendering, and the active-row visual design. It does **not** wire
selection to `_apply_model` (Step 4) and does **not** call the live
`assemble_model_choices()` (Step 5/2) — the caller passes fixture or
already-assembled data in.

**Why a `ModalScreen`.** This is kolsch's first Textual dropdown/
picker widget — a repo-wide grep found zero prior uses of `Select` /
`OptionList` / `SelectionList` / `push_screen` / `ModalScreen`. Per
spec-132's Technical Context, no modal/overlay precedent exists in the
styleguide beyond a one-line mention of `SlashPopup` / `ScrollbackCue`
as "transient overlays" — no layout/dismissal/z-index convention to
extend. This widget defaults to Textual's own `ModalScreen` idioms
(layering, dimmed backdrop, `push_screen`/`wait_for_dismiss`) rather
than inventing bespoke overlay machinery, and visually follows
`SlashPopup` (`slash_popup.py`) — flat `$chat-surface` background, no
heavy border — instead of a boxed dialog, consistent with the rest of
the app's "colored-rail-only" spirit.

**Why this never trips TC-CHAT-18.** The blocking-call guard
(`tests/component/cli/test_chat_input.py::test_tc_chat_18_...`) is a
narrow AST check: any `ast.Call` under `src/cli/chat/` whose
`.func.attr` is literally `"run"` or `"run_async"` must carry
`inline=True`. This widget calls neither name — `push_screen` /
`dismiss` / `wait_for_dismiss` are unrelated attribute names — so it
has zero risk of tripping the check and can live under
`src/cli/chat/widgets/` without incident (`session_picker.py`'s
`prompt_toolkit` `.run_async()` is the thing that guard actually
polices; this widget doesn't touch that code path at all).

**Active-row NO_COLOR design (3 carriers).** The row matching the
caller-supplied "current model" marker renders **bold**, in
**`$tool-fg`**, with a leading **`◆`** marker glyph (a blank space
holds the column for every other row so columns stay aligned). `◆`
was chosen after checking `state_tokens.py`'s ten canonical glyphs
(`✓ ✗ ▸ ⋯ ⚠ ⧗ ─ ○ ⊘`) and grepping every leading-glyph use across
`src/cli/chat/` and `docs/reference/chat-styleguide.md`'s glyph table
— `●` (already "breadcrumb done" / "tool-call marker" /
"compaction marker") and `✓` (already the canonical `success` state
glyph) are both actively double-booked despite being the spec's
suggested starting candidates; `▸` is the flagged double-meaning glyph
the spec explicitly says to avoid. `◆` (U+25C6 BLACK DIAMOND) has zero
hits anywhere in `src/` or `docs/`. This gives 3 redundant carriers
(hue + text-style + glyph) in color mode and 2 (text-style + glyph)
under `NO_COLOR` — satisfying the styleguide's redundant-carriers rule
either way, since `NO_COLOR` only swaps the palette-variable theme
file at the App level (`mono.tcss` maps every palette var to
`ansi_default`); this widget's own glyph/bold choices are unconditional
and unaffected by `NO_COLOR`, exactly like `NoticeWidget`'s
`SEVERITY_GLYPHS` (see `notice.py`, `test_notice.py`).

**Missing context size.** `ModelChoice.entry.context_length is None`
(always true for `opencode` entries, `llm/providers/opencode.py:261`)
renders as a `—` placeholder with **`dim` text-style always applied**
plus `$hint`'s literal hex when color is available — mirroring
`mono.tcss`'s own stated convention ("widgets that convey hint / dim
state should add `text-style: dim`") and `NoticeWidget`'s pattern of
never gating the non-hue carrier behind color availability. Gating
`dim` behind `_use_color()` (an earlier version of this file did)
would have left the placeholder with zero style distinction from a
real number under `NO_COLOR` — a redundant-carriers violation; `dim`
is now unconditional, only the literal hex hue is gated.

**Spacing.** Reuses the standard scale from `chat-styleguide.md` §1:
`padding: 0 1` on the outer container (matches `SlashPopup`),
`margin: 0 0 1 0` between the picker and its future sibling chrome.
Nothing forces this automatically for a multi-column widget, so it's
stated explicitly here rather than left to CSS defaults.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType

    from cli._model_catalog import ModelChoice

# See the module docstring's "Active-row NO_COLOR design" section for
# the collision check this glyph was picked against.
#
# The marker COLUMN (glyph/blank + one trailing space separator) is a
# fixed width regardless of active/inactive state — `_ACTIVE_MARKER`
# and `_INACTIVE_MARKER` are both exactly `_ACTIVE_GLYPH_COLUMN_WIDTH`
# characters wide, INCLUDING the separator space, so the provider/
# context columns start at the same character offset on every row.
# (Fixed a real off-by-one here: an earlier version appended a
# *second* trailing space unconditionally on top of the marker,
# shifting inactive rows one column right of the active row.)
_ACTIVE_GLYPH = "◆"
_ACTIVE_GLYPH_COLUMN_WIDTH = len(_ACTIVE_GLYPH) + 1  # glyph + 1 separator space
_ACTIVE_MARKER = _ACTIVE_GLYPH + " "
_INACTIVE_MARKER = " " * _ACTIVE_GLYPH_COLUMN_WIDTH

_MISSING_CONTEXT_PLACEHOLDER = "—"

# Column widths — name gets the most room, provider/context are short
# and roughly fixed-width in practice (provider keys, integer sizes).
_NAME_COLUMN_WIDTH = 32
_PROVIDER_COLUMN_WIDTH = 14

# Literal hex values duplicated from `theme.tcss` (`$tool-fg`,
# `$hint`), mirroring how `SlashPopup`/`_gutter_block.py` redeclare
# the same palette literals in their own `DEFAULT_CSS` so each widget
# renders standalone under Pilot tests that don't load the app
# stylesheet. This module needs the SAME literal values as actual
# Python-side `rich.Style` objects (not CSS) because `OptionList`
# renders `Option.prompt` as a `rich.Text`/`Visual` directly — Textual
# never runs CSS selectors over option content, so a CSS variable
# reference like `"$tool-fg"` inside a `Text` style string is not
# resolvable (confirmed: `rich.style.Style.parse("$tool-fg")` raises
# `ColorParseError` — it is a Textual-only concept). `_use_color()`
# below is this module's `NO_COLOR` gate — the Rich-level equivalent
# of what `mono.tcss` does for CSS-cascaded widgets.
_TOOL_FG_HEX = "#4dd0e1"
_HINT_HEX = "#6e7681"


def _format_context_length(context_length: int | None) -> str:
    """Return the display string for a model's context size.

    Raw integer (e.g. ``"200000"``), matching the existing
    `kolsch models --verbose` convention (`llm_commands.py:609`,
    `str(ctx)` — no K/M formatting exists anywhere in the repo to
    reuse) — or the `—` placeholder when unknown.
    """
    if context_length is None:
        return _MISSING_CONTEXT_PLACEHOLDER
    return str(context_length)


def _matches_current(choice: ModelChoice, current: ModelChoice | None) -> bool:
    """Return whether `choice` is "the same model" as `current`.

    Compares `(provider, entry.id)` only — NOT full dataclass `==`
    equality. `ModelChoice`/`ModelEntry` are frozen dataclasses, so
    `==` is structural over every field including `context_length`,
    `supports_tools`, `supports_streaming`, `pricing`. In Step 5's
    real integration the only available "current model" source is
    `ctx.state.config.model` (a `ModelSpec` with just `provider`/
    `model` strings) — a `ModelChoice` built from it will have every
    other `ModelEntry` field at its default (`None`), so it will
    almost never structurally `==` the fully-populated `ModelChoice`
    a real `discover_models()` call returns for the same model. The
    natural stable identity for "this is the same model" is the
    `(provider, id)` pair regardless of which metadata fields happen
    to be populated on either side — that's what this compares.
    """
    if current is None:
        return False
    return choice.provider == current.provider and choice.entry.id == current.entry.id


def _use_color() -> bool:
    """Return whether hue carriers should render, per `NO_COLOR`.

    Mirrors `cli.chat.app._resolve_css_path`'s check
    (`os.environ.get("NO_COLOR") is not None`) — the same convention
    every other NO_COLOR-aware widget in this codebase uses. Text-style
    (bold) and the leading glyph are unconditional either way (set
    outside this gate); this only controls whether the literal hex
    colors are applied, so a `NO_COLOR` terminal never receives an
    explicit color directive.
    """
    return os.environ.get("NO_COLOR") is None


def _row_text(choice: ModelChoice, *, is_current: bool) -> Text:
    """Build the styled, column-aligned row for one `ModelChoice`.

    Three columns — name (`entry.id`), provider, context size — plus
    a leading marker column reserved for every row (occupied by
    `_ACTIVE_MARKER` on the current-model row, `_INACTIVE_MARKER` on
    every other row — both exactly `_ACTIVE_GLYPH_COLUMN_WIDTH`
    characters, so the provider/context columns start at the same
    character offset on every row regardless of which row is active).
    `Text` (not a marker-up string) is used deliberately: the
    codebase's `markup=False` convention on `Static` exists so
    untrusted content (here, provider-supplied model ids) never runs
    through Rich's console-markup parser — a plain `Text` object gets
    the same safety for free without needing a `markup=` flag, since
    `Text` is never markup-parsed.

    Carrier survival under `NO_COLOR` (per the module docstring's
    "Active-row NO_COLOR design" section):

    - Active row: glyph + bold text-style are ALWAYS applied; the
      literal hex hue is gated behind `_use_color()` — 3 carriers in
      color mode, 2 (`bold` + glyph) under `NO_COLOR`.
    - Missing-context placeholder: `dim` text-style is ALWAYS applied
      (matches `mono.tcss`'s own convention that hint/dim state must
      survive `NO_COLOR` via text-style, not hue alone); the literal
      hex hue is gated behind `_use_color()` the same way.
    """
    marker = _ACTIVE_MARKER if is_current else _INACTIVE_MARKER
    name = choice.entry.id
    provider = choice.provider
    context_str = _format_context_length(choice.entry.context_length)
    use_color = _use_color()

    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(marker)
    text.append(f"{name:<{_NAME_COLUMN_WIDTH}}")
    text.append(f"{provider:<{_PROVIDER_COLUMN_WIDTH}}")
    if choice.entry.context_length is None:
        hint_style = "dim " + _HINT_HEX if use_color else "dim"
        text.append(context_str, style=hint_style)
    else:
        text.append(context_str)

    if is_current:
        active_style = "bold " + _TOOL_FG_HEX if use_color else "bold"
        text.stylize(active_style)
    return text


class ModelPickerScreen(ModalScreen[None]):
    """Modal picker listing `ModelChoice` rows — name / provider / context.

    Step 3 scope: mounts a **given, static** ``list[ModelChoice]`` (no
    live `assemble_model_choices()` call — that's Step 5) and marks
    whichever entry matches ``current`` per the 3-carrier active-row
    design. Selection handling and Escape-cancel semantics beyond
    "close with no side effect" are Step 4's job; this step's
    `OptionList` is fully interactive (arrow keys, Enter highlights)
    but nothing yet consumes an `OptionList.Selected` message.

    Escape dismisses the modal via Textual's own `ModalScreen`
    binding idiom (`BINDINGS` below) — no mutation, matching the
    "Escape cancels with no mutation" acceptance criterion; Step 4
    will confirm this at the apply-model layer once selection is
    wired up.
    """

    DEFAULT_CSS = """
    $tool-fg: #4dd0e1;
    $hint: #6e7681;
    $chat-surface: #1a1d24;

    ModelPickerScreen {
        align: center middle;
    }

    ModelPickerScreen > Vertical {
        width: auto;
        height: auto;
        max-height: 80%;
        background: $chat-surface;
        padding: 0 1;
        margin: 0 0 1 0;
    }

    ModelPickerScreen .picker-title {
        color: $tool-fg;
        text-style: bold;
        height: 1;
        padding: 0 1;
    }

    ModelPickerScreen OptionList {
        border: none;
        background: $chat-surface;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_picker", "Cancel", show=False),
    ]

    def __init__(
        self,
        choices: list[ModelChoice],
        *,
        current: ModelChoice | None = None,
        name: str | None = None,
        id: str | None = None,  # noqa: A002 - Textual's own param name
        classes: str | None = None,
    ) -> None:
        """Build the picker over a **given, static** list of choices.

        Args:
            choices: The rows to render, in display order. Step 3
                takes this as-is (fixture data in tests); Step 5 wires
                a caller that passes `assemble_model_choices()`'s
                result instead.
            current: The `ModelChoice` to mark as "the active model"
                (bold + `$tool-fg` + `_ACTIVE_GLYPH`), or `None` if no
                row should be marked. Matched against each entry in
                `choices` via `_matches_current` — a `(provider,
                entry.id)` comparison, NOT full dataclass `==`
                equality. Callers do NOT need every `ModelEntry`
                field populated on `current`; only `provider` and
                `entry.id` need to be correct. This matters in
                practice: Step 5's real "current model" source
                (`ctx.state.config.model`, a `ModelSpec`) carries no
                `context_length`/`supports_tools`/`supports_streaming`
                /`pricing`, so a `ModelChoice` built from it would
                never structurally `==` the fully-populated
                `ModelChoice` a live `discover_models()` call returns
                for that same model — `_matches_current` is what
                makes the active-row marking work despite that.
            name: Forwarded to `ModalScreen`.
            id: Forwarded to `ModalScreen`.
            classes: Forwarded to `ModalScreen`.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._choices = list(choices)
        self._current = current

    def compose(self) -> ComposeResult:
        """Yield the title + the option list built from `self._choices`."""
        with Vertical():
            yield Static("Switch model", classes="picker-title", markup=False)
            option_list = OptionList(*self._build_options())
            yield option_list

    def _build_options(self) -> list[Option]:
        """Return one `Option` per choice, in `self._choices` order."""
        return [
            Option(
                _row_text(
                    choice,
                    is_current=_matches_current(choice, self._current),
                ),
                id=str(index),
            )
            for index, choice in enumerate(self._choices)
        ]

    def action_dismiss_picker(self) -> None:
        """Escape binding target — close with no side effect (no arg)."""
        self.dismiss(None)


__all__ = ["ModelPickerScreen"]
