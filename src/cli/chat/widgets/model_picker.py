"""``ModelPickerScreen`` — spec-132's `/model` picker (Steps 3+4+6).

Renders a **given, static** ``list[ModelChoice]`` (`cli._model_catalog`)
as a three-column list — name / provider / context size — inside a
Textual ``ModalScreen``. Step 3 built the shell: mounting, rendering,
and the active-row visual design. Step 4 (this revision) wires
selection and cancel: choosing a row dismisses the screen with the
chosen ``ModelChoice``; Escape dismisses with ``None``. The screen
itself stays dumb/testable — it never calls `_apply_model` or touches
a `SlashContext` directly (see :func:`open_model_picker` below for the
caller-side glue, and the class docstring for why). It does **not**
call the live `assemble_model_choices()` (Step 5/2) — the caller
passes fixture or already-assembled data in.

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

**Step 6 — loading-state UX.** `assemble_catalog()` (the Step 6
failure-surfacing wrapper around `assemble_model_choices()`) is async
and can be slow (real network calls for `openrouter`/`opencode`) or
fail per-provider. Chosen design: **block-with-spinner, not
progressive-fill**. `_slash.py`'s `_open_model_picker_from_slash`
(the actual `/model` no-arg call site — this module's own
`open_model_picker` still just takes an already-assembled `choices`
list, unchanged in shape from Step 4/5) pushes a small
:class:`ModelPickerLoadingScreen` first (a spinner + "Loading
models…", reusing `cli.chat.spinner`'s canonical braille alphabet),
awaits the catalog assembly while it's showing, pops the loading
screen, then calls `open_model_picker` to push the real,
fully-populated `ModelPickerScreen`. This was picked over
progressive-fill (mounting rows as each provider's discovery
resolves) because `ModelPickerScreen.__init__` already takes a
**fully-formed** `choices` list by design (Step 4's review confirmed
this doesn't block either approach) — block-with-spinner needed zero
changes to the picker's own state model or to `open_model_picker`'s
existing contract, while progressive-fill would need `OptionList`
rows to be added/removed live and the active-row/column-alignment
logic (both already non-trivial — see the off-by-one this file's
history mentions) to re-run mid-render. Given the catalog is at most
~9-20 entries and a per-provider round-trip is one HTTP call, the
brief blocked window is an acceptable trade for a simpler, more
obviously-correct implementation. If a provider is unusually slow
this still doesn't "freeze" anything — the spinner keeps animating
the whole time, satisfying the acceptance bar ("never looks frozen"),
because `assemble_catalog` is awaited cooperatively, not run inside a
blocking call.

**Step 6 — failure surfacing.** When one or more configured providers'
discovery failed, `open_model_picker` passes the collected
`ProviderFailure` list through to `ModelPickerScreen`, which renders
one dim `$hint`-styled note line per failure above the option list
(`"⚠ <provider>: <reason>"`) — always shown when failures exist, not
just when the picker would otherwise be empty, so a partial failure
(e.g. 1 of 3 providers down) isn't silently invisible either. This
directly answers Step 5 review carry-forward #1: "N providers
configured, all discovery failed" no longer opens a silent, empty
picker — it opens a picker that explains why there's nothing to pick.
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

from cli._model_catalog import ModelChoice
from cli.chat.spinner import SPINNER_FRAMES, spinner_frame

if TYPE_CHECKING:
    from textual.app import App, ComposeResult
    from textual.binding import BindingType
    from textual.timer import Timer

    from cli._model_catalog import ProviderFailure
    from cli._slash import SlashContext

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
_WARNING_HEX = "#ffb454"
"""Literal hex for `$warning`, duplicated from `notice.py`'s
`DEFAULT_CSS` for the same Rich-`Text`-vs-CSS-variable reason
documented above `_TOOL_FG_HEX`/`_HINT_HEX`. Used only for the Step 6
per-provider failure note glyph."""

_FAILURE_GLYPH = "⚠"
"""`state_tokens.py`'s canonical `"paused"`/`"awaiting"` glyph —
reused here for "this provider's discovery failed," a related-enough
meaning (something needs attention) that introducing a distinct
glyph isn't warranted. Unused elsewhere in this file, so no
collision with `_ACTIVE_GLYPH` (`◆`)."""


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


def _failure_note_text(failure: ProviderFailure) -> Text:
    """Build one styled note line for a `ProviderFailure` (Step 6).

    ``"⚠ <provider>: <reason>"`` — glyph + dim text-style are always
    applied (carrier survival under `NO_COLOR`, mirroring `_row_text`'s
    missing-context placeholder); the literal `$warning` hex is gated
    behind `_use_color()`. Built as a `Text`, not markup, for the same
    reason `_row_text` uses `Text`: `reason` may echo untrusted
    exception text (a bad `base_url`, a raw connector error message).
    """
    use_color = _use_color()
    text = Text(no_wrap=False, overflow="fold")
    text.append(f"{_FAILURE_GLYPH} {failure.provider}: {failure.reason}")
    style = "dim " + _WARNING_HEX if use_color else "dim"
    text.stylize(style)
    return text


class ModelPickerScreen(ModalScreen[ModelChoice | None]):
    """Modal picker listing `ModelChoice` rows — name / provider / context.

    Mounts a **given, static** ``list[ModelChoice]`` (no live
    `assemble_model_choices()` call — that's Step 5) and marks whichever
    entry matches ``current`` per the 3-carrier active-row design.

    **Selection/cancel (Step 4).** Choosing a row (Enter, or a mouse
    click on an `Option`) fires Textual's `OptionList.OptionSelected`
    message; `on_option_list_option_selected` below dismisses the
    screen with the corresponding `ModelChoice`. Escape dismisses with
    `None` (`action_dismiss_picker`, bound below) — no mutation,
    matching the "Escape cancels with no mutation" acceptance
    criterion.

    **This screen never calls `_apply_model` itself and takes no
    `SlashContext`.** `Screen.dismiss(result)` returning a typed
    result — `ModelChoice | None` — is Textual's own idiom for a
    modal's answer, and keeping the *widget* limited to "which row did
    the user pick" keeps it trivially unit-testable without a real
    chat session (mount, press keys, assert what `dismiss` received —
    no session/runtime double needed). The actual `_apply_model` call
    (turning the chosen `ModelChoice` into a `ModelSpec` and applying
    it to a `SlashContext`) is the caller's job — see
    :func:`open_model_picker`, the thin glue function below that an
    `App`/`Screen` with a real `SlashContext` calls via
    `push_screen_wait`. Step 5 wires `/model` (no-arg) to invoke
    `open_model_picker`.

    **Failure notes (Step 6).** ``failures`` renders as one dim
    ``⚠ <provider>: <reason>`` line per entry, above the option list —
    always shown when non-empty, whether or not ``choices`` is also
    empty. See the module docstring's "Step 6 — failure surfacing"
    section.
    """

    DEFAULT_CSS = """
    $tool-fg: #4dd0e1;
    $hint: #6e7681;
    $warning: #ffb454;
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

    ModelPickerScreen .picker-failure-note {
        height: auto;
        padding: 0 1;
    }

    ModelPickerScreen .picker-empty-note {
        color: $hint;
        text-style: dim;
        height: auto;
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
        failures: list[ProviderFailure] | None = None,
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
            failures: Providers whose discovery didn't complete
                (Step 6's `assemble_catalog`), rendered as inline note
                lines above the option list. `None`/empty renders no
                notes — the pre-Step-6 shape.
            name: Forwarded to `ModalScreen`.
            id: Forwarded to `ModalScreen`.
            classes: Forwarded to `ModalScreen`.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._choices = list(choices)
        self._current = current
        self._failures = list(failures) if failures else []

    def compose(self) -> ComposeResult:
        """Yield the title, any failure notes, then the option list.

        When `self._choices` is empty (every configured provider
        failed, or none returned any models) and there ARE failures to
        explain why, an additional plain note is shown in place of the
        (otherwise silently empty) `OptionList` — see the module
        docstring's "Step 6 — failure surfacing" section and Step 5's
        carried-forward review item #1.
        """
        with Vertical():
            yield Static("Switch model", classes="picker-title", markup=False)
            for failure in self._failures:
                yield Static(
                    _failure_note_text(failure),
                    classes="picker-failure-note",
                    markup=False,
                )
            if not self._choices and self._failures:
                yield Static(
                    "No models available — every configured provider failed "
                    "to respond (see above).",
                    classes="picker-empty-note",
                    markup=False,
                )
                return
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

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Selecting a row dismisses the screen with its `ModelChoice`.

        `event.option.id` is the `str(index)` `_build_options` set —
        the stable link back into `self._choices` (the same list
        `_matches_current` walks for active-row marking). Applying the
        chosen model (constructing a `ModelSpec`, calling
        `_apply_model`) is deliberately NOT done here — see the class
        docstring's "This screen never calls `_apply_model`" section;
        that's :func:`open_model_picker`'s job once this returns.
        """
        event.stop()
        option_id = event.option.id
        assert option_id is not None  # every Option here is built with an id
        choice = self._choices[int(option_id)]
        self.dismiss(choice)

    def action_dismiss_picker(self) -> None:
        """Escape binding target — close with no side effect (`None`)."""
        self.dismiss(None)


class ModelPickerLoadingScreen(ModalScreen[None]):
    """Step 6's loading-state modal — shown while `assemble_catalog()` runs.

    A minimal spinner + "Loading models…" line in the same visual
    family as `ModelPickerScreen` (flat `$chat-surface`, no heavy
    border). Never returns a result (`ModalScreen[None]`) — it isn't
    interactive; `_slash.py`'s `_open_model_picker_from_slash` pushes
    it, awaits the catalog assembly, pops it, then calls
    `open_model_picker` to push the real picker in its place (see the
    module docstring's "Step 6 — loading-state UX" section for the
    block-with-spinner vs. progressive-fill design decision).

    Escape is intentionally NOT bound here — there is nothing to
    cancel back to mid-assembly that wouldn't leave a dangling
    in-flight `assemble_catalog()` coroutine; the wait is expected to
    be short (at most a couple of per-provider HTTP round-trips), same
    as `refresh`'s existing (unskippable) discovery wait.
    """

    DEFAULT_CSS = """
    $tool-fg: #4dd0e1;
    $chat-surface: #1a1d24;

    ModelPickerLoadingScreen {
        align: center middle;
    }

    ModelPickerLoadingScreen > Vertical {
        width: auto;
        height: auto;
        background: $chat-surface;
        padding: 0 1;
        margin: 0 0 1 0;
    }

    ModelPickerLoadingScreen .picker-title {
        color: $tool-fg;
        text-style: bold;
        height: 1;
        padding: 0 1;
    }

    ModelPickerLoadingScreen .picker-loading-body {
        color: $tool-fg;
        height: 1;
        padding: 0 1;
    }
    """

    _VERB = "Loading models…"

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,  # noqa: A002 - Textual's own param name
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._spinner_index = 0
        self._frame_timer: Timer | None = None
        self._body_static = Static("", classes="picker-loading-body", markup=False)

    def compose(self) -> ComposeResult:
        """Yield the title + a spinner/verb line."""
        with Vertical():
            yield Static("Switch model", classes="picker-title", markup=False)
            yield self._body_static

    def on_mount(self) -> None:
        """Render the first spinner frame and start the tick timer.

        Same ~12.5 Hz cadence as `WorkingPlaceholder`/`TurnStatus`
        (`set_interval(0.08, ...)`) — this file has no prior spinner
        use, so the cadence is copied directly from those two rather
        than invented fresh.
        """
        self._render_frame()
        self._frame_timer = self.set_interval(0.08, self._advance_spinner)

    def on_unmount(self) -> None:
        """Stop the tick timer so a dismissed loading screen costs nothing."""
        self._stop_timer()

    def _advance_spinner(self) -> None:
        self._spinner_index = (self._spinner_index + 1) % len(SPINNER_FRAMES)
        self._render_frame()

    def _render_frame(self) -> None:
        glyph = spinner_frame(self._spinner_index)
        self._body_static.update(f"{glyph} {self._VERB}")

    def _stop_timer(self) -> None:
        if self._frame_timer is not None:
            self._frame_timer.stop()
            self._frame_timer = None


async def open_model_picker(
    ctx: SlashContext,
    app: App[object],
    choices: list[ModelChoice],
    *,
    current: ModelChoice | None = None,
    failures: list[ProviderFailure] | None = None,
) -> None:
    """Push `ModelPickerScreen`, apply the selection (if any) via `_apply_model`.

    The caller-side half of Step 4's selection wiring — see
    `ModelPickerScreen`'s class docstring for why the screen itself
    stays dumb (no `SlashContext`, no `_apply_model` call). This
    function is the seam Step 5 wired `/model`'s no-arg branch to,
    with ``choices`` coming from `assemble_model_choices()`/
    `assemble_catalog()` instead of a fixture list.

    Pushes the screen via `app.push_screen_wait`, Textual's own
    idiom for "show a modal, await its typed `dismiss()` result."
    A `None` result (Escape, or `ModelPickerScreen.action_dismiss_picker`)
    applies nothing and leaves `ctx.state` untouched. A `ModelChoice`
    result is converted straight to a `ModelSpec(provider=..., model=...)`
    — no string round-trip through `provider/model` text — and handed
    to `_apply_model`, the exact same function `/model <ref>` (Step 1)
    uses, so a picker selection and the equivalent typed `/model <ref>`
    produce identical `ChatSessionState.model_override` /
    `ChatSessionState.config.model` results.

    This function does NOT itself call `assemble_catalog()` /
    `assemble_model_choices()` — ``choices``/``failures`` are already
    fully assembled by the time this is called (unchanged from Step
    4/5's contract, and load-bearing for `test_open_model_picker.py`'s
    existing fixture-driven tests, which call this directly with no
    assembly step at all). The Step 6 loading-state UX (pushing
    `ModelPickerLoadingScreen` while assembly is in flight) lives at
    the call site — see `_slash.py`'s `_handler_model` no-arg branch —
    not in here, precisely so this function's contract doesn't need to
    change shape for callers that already have their data.

    Args:
        ctx: The slash-command context `_apply_model` mutates.
            `ctx.state` must not be `None` (same precondition
            `_apply_model` itself asserts).
        app: The running `App` (or any `Screen`/`App`-like object
            exposing Textual's `push_screen_wait`) to push the modal
            onto.
        choices: The rows to render — passed straight through to
            `ModelPickerScreen`.
        current: The active-model marker — passed straight through to
            `ModelPickerScreen`.
        failures: Providers whose discovery didn't complete (Step 6) —
            passed straight through to `ModelPickerScreen` for inline
            display. `None`/omitted renders no failure notes.
    """
    from cli._slash import _apply_model  # noqa: PLC0415
    from schemas import ModelSpec  # noqa: PLC0415

    chosen = await app.push_screen_wait(
        ModelPickerScreen(choices, current=current, failures=failures)
    )
    if chosen is None:
        return
    _apply_model(ctx, ModelSpec(provider=chosen.provider, model=chosen.entry.id))


__all__ = ["ModelPickerLoadingScreen", "ModelPickerScreen", "open_model_picker"]
