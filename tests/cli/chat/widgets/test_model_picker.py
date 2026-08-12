"""Tests for :class:`ModelPickerScreen` (spec-132 Step 3 — shell only).

This step mounts a **given, static** ``list[ModelChoice]`` — no live
`assemble_model_choices()` call, no selection wiring (Step 4), no
`/model` no-arg wiring (Step 5). Covers:

- Row content per column (name / provider / context size), including
  the `—` placeholder for `context_length is None`.
- **Column alignment**: the provider/context columns start at the
  identical character offset on every row, active or not (a real
  off-by-one shipped here originally — see `test_column_offset_*`
  below, which assert the exact offset rather than `startswith`/
  substring checks that let the bug slip through review).
- The fixture "current" entry carries the leading `◆` glyph + bold
  styling, and no other row does.
- **Identity-based `current` matching** (`_matches_current`,
  `(provider, entry.id)`, not full dataclass `==`): a `current` with
  only `provider`/`entry.id` populated (mirroring Step 5's real
  `ctx.state.config.model`-derived source, which never has
  `context_length`/`supports_tools`/etc.) must still mark the
  matching, fully-populated catalog row — and a same-provider,
  different-id row must NOT be marked (matching isn't too loose).
- Construction/mount under `NO_COLOR=1` (mirroring `test_notice.py`'s
  `mock.patch.dict(os.environ, {"NO_COLOR": "1"})` pattern): the
  glyph + bold distinction survives without the literal hue, and the
  missing-context placeholder's `dim` text-style survives too (it is
  applied unconditionally, not gated behind color availability).
- Real keyboard interaction via Pilot: arrow-down moves
  `OptionList.highlighted`; Escape dismisses the modal screen.

``ModalScreen`` is a ``Screen`` (itself a ``Widget`` subclass) but
isn't the kind of single in-isolation widget ``mock_app_factory``
wraps (that fixture composes one widget as a *child*; a `Screen` is
meant to be *pushed*). Tests here use a minimal dedicated `App` that
`push_screen`s the picker in `on_mount`, mirroring how `ChatApp` will
actually use it in Step 5.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from rich.text import Text
from textual.app import App
from textual.widgets import OptionList

from cli._model_catalog import ModelChoice
from cli.chat.widgets.model_picker import (
    _ACTIVE_GLYPH,
    _MISSING_CONTEXT_PLACEHOLDER,
    _NAME_COLUMN_WIDTH,
    ModelPickerScreen,
)
from llm.protocol import ModelEntry

# Character offset where the provider column starts: marker column
# (glyph/blank + 1 separator space, `len(_ACTIVE_GLYPH) + 1` == 2 for
# a single-width glyph) + the name column's fixed width.
_PROVIDER_COLUMN_OFFSET = len(_ACTIVE_GLYPH) + 1 + _NAME_COLUMN_WIDTH


def _fixture_choices() -> list[ModelChoice]:
    """5 rows across 3 fake providers, one `None` context, one 'current'."""
    return [
        ModelChoice(
            provider="claude_code",
            entry=ModelEntry(id="claude-sonnet-5", context_length=200_000),
        ),
        ModelChoice(
            provider="claude_code",
            entry=ModelEntry(id="claude-haiku-5", context_length=200_000),
        ),
        ModelChoice(
            provider="openrouter",
            entry=ModelEntry(id="gpt-5", context_length=400_000),
        ),
        ModelChoice(
            provider="openrouter",
            entry=ModelEntry(id="gemini-3-pro", context_length=1_000_000),
        ),
        ModelChoice(
            provider="opencode",
            entry=ModelEntry(id="local-model", context_length=None),
        ),
    ]


class _PickerHostApp(App[None]):
    """Minimal app that pushes the picker on mount — mirrors real usage."""

    def __init__(self, choices: list[ModelChoice], current: ModelChoice | None) -> None:
        super().__init__()
        self._choices = choices
        self._current = current

    async def on_mount(self) -> None:
        await self.push_screen(ModelPickerScreen(self._choices, current=self._current))


def _option_texts(screen: ModelPickerScreen) -> list[Text]:
    option_list = screen.query_one(OptionList)
    texts: list[Text] = []
    for i in range(option_list.option_count):
        prompt = option_list.get_option_at_index(i).prompt
        # `_row_text` (model_picker.py) always builds a `rich.Text` —
        # `Option.prompt`'s `VisualType` is a wider union only because
        # Textual's own API allows other renderables.
        assert isinstance(prompt, Text)
        texts.append(prompt)
    return texts


def _has_style_containing(text: Text, needle: str) -> bool:
    return any(span.style and needle in str(span.style) for span in text.spans)


# ---------------------------------------------------------------------------
# Row content per column
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mounts_one_row_per_choice() -> None:
    choices = _fixture_choices()
    app = _PickerHostApp(choices, current=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        option_list = screen.query_one(OptionList)
        assert option_list.option_count == len(choices)


@pytest.mark.asyncio
async def test_row_content_includes_name_provider_context() -> None:
    choices = _fixture_choices()
    app = _PickerHostApp(choices, current=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        texts = [t.plain for t in _option_texts(screen)]
        assert len(texts) == 5
        assert "claude-sonnet-5" in texts[0]
        assert "claude_code" in texts[0]
        assert "200000" in texts[0]
        assert "gpt-5" in texts[2]
        assert "openrouter" in texts[2]
        assert "400000" in texts[2]


@pytest.mark.asyncio
async def test_missing_context_length_renders_hint_placeholder() -> None:
    choices = _fixture_choices()
    app = _PickerHostApp(choices, current=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        texts = [t.plain for t in _option_texts(screen)]
        # Row 4 (index 4) is the opencode entry with context_length=None.
        assert "opencode" in texts[4]
        assert "local-model" in texts[4]
        assert _MISSING_CONTEXT_PLACEHOLDER in texts[4]
        # And no digit sequence masquerading as a context size.
        assert not any(ch.isdigit() for ch in texts[4].split("opencode")[-1])


@pytest.mark.asyncio
async def test_missing_context_length_styled_hint_color() -> None:
    """The `—` placeholder carries the `$hint` literal hex under color mode."""
    choices = _fixture_choices()
    app = _PickerHostApp(choices, current=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        opencode_text = _option_texts(screen)[4]
        assert _has_style_containing(opencode_text, "#6e7681"), (
            "expected a hint-colored span on the placeholder row"
        )


@pytest.mark.asyncio
async def test_missing_context_length_dim_style_survives_no_color() -> None:
    """Fix for reviewer finding #3: `dim` must be unconditional, not color-gated.

    Under NO_COLOR, the placeholder previously had ZERO style spans —
    visually identical weight to a real number like "200000". `dim`
    text-style must render regardless of `_use_color()` so the
    redundant-carriers rule is satisfied without hue.
    """
    choices = _fixture_choices()

    with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
        app = _PickerHostApp(choices, current=None)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ModelPickerScreen)
            opencode_text = _option_texts(screen)[4]

    assert _has_style_containing(opencode_text, "dim"), (
        "dim text-style must survive under NO_COLOR"
    )
    # Hue carrier is dropped — no literal hex color anywhere in the spans.
    assert not _has_style_containing(opencode_text, "#6e7681"), (
        "literal hue must NOT be applied under NO_COLOR"
    )


# ---------------------------------------------------------------------------
# Column alignment (reviewer finding #1 — off-by-one regression tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_column_offset_identical_across_active_and_inactive_rows() -> None:
    """The provider column must start at the same offset on every row.

    Regression test for the off-by-one where the active row's marker
    column was 2 chars ("◆ ") but inactive rows' was 3 chars ("   ")
    — visibly shifting the provider/context columns one character
    right on every non-active row. Asserts the actual character
    position, not a substring/`startswith` check (the kind of check
    that let the original bug slip through).
    """
    choices = _fixture_choices()
    current = choices[2]  # gpt-5 / openrouter — the only active row
    app = _PickerHostApp(choices, current=current)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        texts = [t.plain for t in _option_texts(screen)]

    providers = ["claude_code", "claude_code", "openrouter", "openrouter", "opencode"]
    for index, (text, provider) in enumerate(zip(texts, providers, strict=True)):
        segment = text[
            _PROVIDER_COLUMN_OFFSET : _PROVIDER_COLUMN_OFFSET + len(provider)
        ]
        assert segment == provider, (
            f"row {index}: expected provider {provider!r} at offset "
            f"{_PROVIDER_COLUMN_OFFSET}, got {segment!r} in {text!r}"
        )


@pytest.mark.asyncio
async def test_column_offset_identical_with_no_active_row() -> None:
    """Same offset check with `current=None` — no row is active at all."""
    choices = _fixture_choices()
    app = _PickerHostApp(choices, current=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        texts = [t.plain for t in _option_texts(screen)]

    offsets = {
        text[_PROVIDER_COLUMN_OFFSET : _PROVIDER_COLUMN_OFFSET + 1] for text in texts
    }
    # Every row starts its provider column with a non-space alnum char
    # at the identical offset (all fixture provider names are
    # alphabetic with no leading space).
    for text in texts:
        char_at_offset = text[_PROVIDER_COLUMN_OFFSET]
        assert char_at_offset.isalnum(), (
            f"expected a provider-name character at offset "
            f"{_PROVIDER_COLUMN_OFFSET}, got {char_at_offset!r} in {text!r}"
        )
    assert len(offsets) >= 1  # sanity: we actually captured something


# ---------------------------------------------------------------------------
# Active-row marking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_row_carries_glyph_and_bold() -> None:
    choices = _fixture_choices()
    current = choices[2]  # gpt-5 / openrouter
    app = _PickerHostApp(choices, current=current)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        texts = _option_texts(screen)

        current_text = texts[2]
        assert current_text.plain.startswith(_ACTIVE_GLYPH)
        assert _has_style_containing(current_text, "bold"), (
            "current row must carry a bold span"
        )


@pytest.mark.asyncio
async def test_only_current_row_carries_glyph_and_bold() -> None:
    choices = _fixture_choices()
    current = choices[2]
    app = _PickerHostApp(choices, current=current)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        texts = _option_texts(screen)

        for index, text in enumerate(texts):
            if index == 2:
                continue
            assert not text.plain.startswith(_ACTIVE_GLYPH), (
                f"row {index} unexpectedly carries the active glyph"
            )
            assert not _has_style_containing(text, "bold"), (
                f"row {index} unexpectedly rendered bold"
            )


@pytest.mark.asyncio
async def test_no_current_marks_no_row() -> None:
    choices = _fixture_choices()
    app = _PickerHostApp(choices, current=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        texts = _option_texts(screen)
        for text in texts:
            assert not text.plain.startswith(_ACTIVE_GLYPH)


# ---------------------------------------------------------------------------
# Identity-based `current` matching (reviewer finding #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_with_only_provider_and_id_still_marks_full_entry() -> None:
    """The realistic Step 5 shape: `current` has NO metadata populated.

    `ctx.state.config.model` is a `ModelSpec` with only `provider`/
    `model` strings — a `ModelChoice` built from it has every
    `ModelEntry` field besides `id` at its default (`None`). This
    must still mark the fully-populated catalog row for the same
    `(provider, id)` — full dataclass `==` would fail this.
    """
    full_entry = ModelChoice(
        provider="claude_code",
        entry=ModelEntry(
            id="claude-sonnet-5",
            context_length=200_000,
            supports_tools=True,
            supports_streaming=True,
        ),
    )
    other_entry = ModelChoice(
        provider="openrouter",
        entry=ModelEntry(id="gpt-5", context_length=400_000),
    )
    choices = [full_entry, other_entry]

    # Only provider/id populated — no context_length/supports_tools/etc.
    naive_current = ModelChoice(
        provider="claude_code", entry=ModelEntry(id="claude-sonnet-5")
    )
    assert naive_current != full_entry  # sanity: full `==` genuinely fails

    app = _PickerHostApp(choices, current=naive_current)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        texts = _option_texts(screen)

    assert texts[0].plain.startswith(_ACTIVE_GLYPH), (
        "full_entry must be marked despite naive_current lacking metadata"
    )
    assert not texts[1].plain.startswith(_ACTIVE_GLYPH)


@pytest.mark.asyncio
async def test_matching_is_not_too_loose_on_same_provider_different_id() -> None:
    """Same-provider stale vs. fresh entries must not cross-match on id alone."""
    stale_current = ModelChoice(
        provider="openrouter",
        entry=ModelEntry(id="gpt-5", context_length=128_000),
    )
    fresh_same_model = ModelChoice(
        provider="openrouter",
        entry=ModelEntry(id="gpt-5", context_length=400_000),
    )
    different_model_same_provider = ModelChoice(
        provider="openrouter",
        entry=ModelEntry(id="gpt-4", context_length=128_000),
    )
    choices = [fresh_same_model, different_model_same_provider]

    app = _PickerHostApp(choices, current=stale_current)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        texts = _option_texts(screen)

    # Same (provider, id) as stale_current, different metadata → matches.
    assert texts[0].plain.startswith(_ACTIVE_GLYPH), (
        "stale vs fresh entry for the same (provider, id) must still match"
    )
    # Same provider, different id → must NOT match.
    assert not texts[1].plain.startswith(_ACTIVE_GLYPH), (
        "different id under the same provider must not be marked current"
    )


# ---------------------------------------------------------------------------
# NO_COLOR — glyph + bold distinction survives without hue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_color_current_row_keeps_glyph_and_bold_loses_hue() -> None:
    """Under NO_COLOR, glyph + bold carriers remain; the literal hex color drops."""
    choices = _fixture_choices()
    current = choices[0]

    with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
        app = _PickerHostApp(choices, current=current)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ModelPickerScreen)
            texts = _option_texts(screen)

    current_text = texts[0]
    # Carrier 1: leading glyph survives.
    assert current_text.plain.startswith(_ACTIVE_GLYPH)
    # Carrier 2: bold text-style survives.
    assert _has_style_containing(current_text, "bold"), (
        "bold carrier must survive under NO_COLOR"
    )
    # Hue carrier is dropped — no literal hex color anywhere in the spans.
    assert not _has_style_containing(current_text, "#4dd0e1"), (
        "literal hue must NOT be applied under NO_COLOR"
    )


@pytest.mark.asyncio
async def test_no_color_other_rows_still_unmarked() -> None:
    choices = _fixture_choices()
    current = choices[0]

    with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
        app = _PickerHostApp(choices, current=current)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ModelPickerScreen)
            texts = _option_texts(screen)

    for index, text in enumerate(texts):
        if index == 0:
            continue
        assert not text.plain.startswith(_ACTIVE_GLYPH), (
            f"row {index} unexpectedly carries the active glyph under NO_COLOR"
        )


@pytest.mark.asyncio
async def test_no_color_construction_does_not_raise() -> None:
    """Mirrors test_notice.py's construct-under-NO_COLOR smoke check."""
    choices = _fixture_choices()
    with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
        screen = ModelPickerScreen(choices, current=choices[0])
    assert screen is not None


# ---------------------------------------------------------------------------
# Real keyboard interaction (reviewer finding #4 — Pilot-driven, not deferred)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arrow_down_moves_highlighted_option() -> None:
    """Real Pilot key presses move `OptionList.highlighted`."""
    choices = _fixture_choices()
    app = _PickerHostApp(choices, current=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ModelPickerScreen)
        option_list = screen.query_one(OptionList)

        start = option_list.highlighted
        await pilot.press("down")
        await pilot.press("down")
        await pilot.pause()

        assert option_list.highlighted is not None
        assert option_list.highlighted != start
        assert option_list.highlighted == (0 if start is None else start) + 2


@pytest.mark.asyncio
async def test_escape_dismisses_picker_with_no_mutation() -> None:
    """Escape pops the modal screen; no apply-model call exists at this step."""
    choices = _fixture_choices()
    current = choices[0]
    app = _PickerHostApp(choices, current=current)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ModelPickerScreen)

        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, ModelPickerScreen), (
            "Escape must dismiss the picker screen"
        )
