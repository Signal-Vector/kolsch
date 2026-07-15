# Chat styleguide

Spacing, symbol, metric, and border conventions for the kolsch chat TUI
widgets (`src/cli/chat/widgets/**`). This page **documents the current
conventions and flags the inconsistencies** it found — it does not change
any CSS. Flagged items (⚑) feed a later normalization pass.

For the color palette, border colors, and the NO_COLOR strategy in full,
see the companion [chat color guide](chat-color-guide.md). Every citation
below is `file:line` against the code on branch
`feature/spec-072-chat-styleguide` and was grep-verified when written;
line numbers drift, so re-verify before relying on a specific line.

The renders on this page are SVG tiles from the spec-071 capture harness.
Regenerate them (and the full gallery) with:

```bash
uv run python -m scripts.chat_styleguide   # → build/gallery.html
```

---

## 1. Layout & spacing scale

Three values carry almost all of the widget spacing. They form the de-facto
scale:

```
gutter    padding: 0 1      │ text │           1 col L/R, 0 T/B    ← default text inset
breather  margin:  0 0 1 0                      1 row below block   ← gap between blocks
indent    padding: 0 0 0 1    ↳ nested          1 col left only     ← hierarchy / nesting
```

![UserChatbox — gutter inset inside a round frame, breather below](chat-widgets/svg/user-chatbox-color.svg)

### Value → widgets table

Built from a scan of every `DEFAULT_CSS` under `src/cli/chat/widgets/**`
for `padding:` / `margin:` declarations (single-side variants listed
separately below).

| Value | Role | Widgets (`file:line`) |
|-------|------|------------------------|
| `padding: 0 1` | **gutter** (1 col L/R) | `approval_block.py:118`, `assistant_prose.py:203`, `card_widget.py:181`, `composer.py:52`, `force_kill_confirm.py:81`, `notice.py:40`, `slash_popup.py:104`, `user_chatbox.py:51`, `workflow_node_row.py:273` |
| `margin: 0 0 1 0` | **breather** (1 row below) | `approval_block.py:119`, `assistant_prose.py:204`, `card_widget.py:182` |
| `padding: 0 0 0 1` | **indent** (1 col left) | `approval_block.py:166` |
| `padding: 0` | zero-inset container | `card_body/confirm.py:30`, `card_body/progress.py:30`, `card_prompt_pane.py:36`, `node_section.py:34`, `notice_slot.py:34` |
| `margin: 0` | zero-margin container | `node_section.py:35`, `workflow_node_row.py:273` |

Single-side spacing (used for finer intra-widget layout):

| Value | Widgets (`file:line`) |
|-------|------------------------|
| `margin-left: 1` | `turn_status.py:86`, `turn_status.py:90` |
| `margin-left: 0` | `status_bar.py:77` |
| `margin-left: 2` | `approval_save_confirm.py:54` |
| `margin-right: 1` | `info_bar.py:57` |
| `margin-top: 1` | `approval_block.py:151`, `approval_block.py:155`, `approval_choice_pane.py:61`, `approval_save_confirm.py:66` |

**⚑ Spacing outliers** (feed the normalize pass):

- `force_kill_confirm.py:82` uses `margin: 1 0` (a row *above and below*),
  the only widget with a top breather — every other block uses
  `margin: 0 0 1 0` (below only).
- `approval_save_confirm.py:54` uses `margin-left: 2` (two-col indent),
  divergent from the `padding: 0 0 0 1` one-col indent used elsewhere.

---

## 2. Symbols & glyphs

Every widget looks state glyphs up through
`src/cli/chat/state_tokens.py` — the canonical `STATE_TOKENS` table
maps a single state vocabulary to `(glyph, color, text_style)`.
Legacy widget-specific state names (`completed`, `error`,
`initializing`, `starting`, `pruned`) still resolve via
`_LEGACY_ALIASES` with a `DeprecationWarning` (qol-075).

| Glyph | Means | Placement | Canonical state (`state_tokens.py`) | Other users |
|-------|-------|-----------|--------------------------------------|--------------|
| `✓` | terminal success | leading title glyph | `success` | `compaction_block.py:208` (`success`) |
| `✗` | terminal failure | leading title glyph | `failed` | `compaction_block.py:210` (`failure`), `node_section.py:39` (error color) |
| `⧗` | cancelling | leading title glyph | `cancelling` | — |
| `⚠` | paused / warning | leading title glyph | `paused`, `awaiting` | — |
| `─` | pruned / cancelled | leading title glyph | `cancelled` (legacy `pruned` aliases) | — |
| `▸` | running (static) / collapsed | leading glyph **and** collapse chevron | `running` (legacy `starting` aliases) | `approval_args_row.py:65` (`_CHEVRON_COLLAPSED`) — ⚑ **double meaning** |
| `▾` | expanded | collapse chevron | — | `approval_args_row.py:66` (`_CHEVRON_EXPANDED`) — pairs with `▸` |
| `⋯` | queued | leading title glyph | `queued` (legacy `initializing` aliases) | — |
| `…` | truncation ellipsis | trailing, in overflow | — | `approval_args_row.py:67` (`_ELLIPSIS`) |
| `⠋…` (10) | spinner — active turn / active node | leading glyph (animated) | — | `src/cli/chat/spinner.py` (`SPINNER_FRAMES`), imported by `turn_status.py` and `workflow_node_row.py` |
| `↳` | nested / "from" connector | gutter before a hint line | — | `card_body/workflow.py:258,261` (`↳ from <ref>`), `tool_call_block.py:126` (`↳ hint:`) |
| `●` / `○` | breadcrumb done / pending | header breadcrumb | — | `approval_block.py:578,581,584` (`● choose ─ ○ pattern ─ ○ save`) |
| `●` | tool-call / compaction marker | leading title glyph | — | `tool_call_block.py:104` (`● {tool}`), `compaction_block.py:207` (`● Compacting…`) |
| `◎` | compaction preview | leading title glyph | — | `compaction_block.py:209` |
| `↷` / `↶` | compaction skipped / restored | leading title glyph | — | `compaction_block.py:211,212` |

![WorkflowNodeRow failed — ✗ leading glyph](chat-widgets/svg/workflow-node-failed-color.svg)

![CompactionBlock success — ✓ Compacted](chat-widgets/svg/compaction-success-color.svg)

![ApprovalBlock choose — ○ pending breadcrumb steps](chat-widgets/svg/approval-choose-color.svg)

### State vocabulary

The canonical state names (source of truth: `src/cli/chat/state_tokens.py`):

| Canonical state | Glyph | Palette color | Text style | Legacy aliases |
|-----------------|-------|---------------|------------|----------------|
| `success`    | `✓` | `$tool-fg` | `bold` | `completed` |
| `failed`     | `✗` | `$error`   | `bold` | `error` |
| `running`    | `▸` | `$tool-fg` | `bold` | `starting` |
| `queued`     | `⋯` | `$hint`    | `dim`  | `initializing` |
| `paused`     | `⚠` | `$warning` | `bold italic` | — |
| `awaiting`   | `⚠` | `$warning` | `bold italic` | — |
| `cancelling` | `⧗` | `$warning` | `bold italic` | — |
| `cancelled`  | `─` | `$hint`    | `dim`  | `pruned` |

Legacy alias resolution emits a `DeprecationWarning` (see the
`_LEGACY_ALIASES` table); the aliases are the one-release bridge for
widgets whose reactive `state` still uses the historical vocabulary.

### Spinner alphabet

One 10-frame dot-cycle `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` (`src/cli/chat/spinner.py`)
backs every "work in progress" animation on the chat surface — the
active-turn status line and the active workflow-node title both
index into it. See qol-076.

![TurnStatus active — 10-frame spinner (⠙ reading)](chat-widgets/svg/turn-status-active-color.svg)

![WorkflowNodeRow running — 10-frame spinner (⠋ deduper)](chat-widgets/svg/workflow-node-running-color.svg)

### ⚑ `▸` double meaning

`▸` is both the **running-state glyph** (`STATE_TOKENS['running']` in
`src/cli/chat/state_tokens.py`) and the **collapsed-chevron** for
collapsible rows (`_CHEVRON_COLLAPSED`, `approval_args_row.py:65`). Note
that while a node is *animating* it actually renders the braille spinner
(`workflow_node_row.py` picks `_SPINNER_FRAMES` for animated states),
so `▸` is the static/non-animating running glyph — but it still
collides with the collapse chevron's meaning.

![ApprovalArgsRow expanded — ▾ chevron + overflow …](chat-widgets/svg/approval-args-expanded-color.svg)

---

## 3. Metrics

| Metric | Format | Widget · slot (`file:line`) |
|--------|--------|------------------------------|
| Elapsed (turn) | `(Ns)` under 60s, else `(Mm SSs)` — e.g. `(2m 05s)` | `turn_status.py:36` (`_format_elapsed`), rendered in the `#elapsed` segment (`turn_status.py:127`) |
| Elapsed (workflow) | `<int>s` (whole seconds, no minute rollover) | `card_body/workflow.py:251` (progress line), `:1078` (compact summary) |
| Node counts | `[completed/total]` — e.g. `[2/3]` | `card_body/workflow.py:251` (workflow card header) |
| Node counts (compact) | `completed/total` — e.g. `2/3` | `card_body/workflow.py:1078` (`▸ workflow · <ref>  N/M · Ns`) |
| Progress bar | `▓`×filled + `░`×remaining, fixed `_BAR_WIDTH` | `card_body/workflow.py:249,251` |

![WorkflowRenderer — `[2/3]` counts + `▓▓▓░░` bar + `Ns`](chat-widgets/svg/workflow-content-color.svg)

**⚑ Metric inconsistencies:**

- Two elapsed formats: `TurnStatus` rolls over to minutes
  (`(2m 05s)`), the workflow renderer never does (`125s`).
- Two count formats: bracketed `[2/3]` in the card header vs bare
  `2/3` in the compact summary.
- **Byte sizes:** no widget currently formats byte/size metrics — none
  found in the `DEFAULT_CSS`/render scan. (Listed here so the absence is
  explicit for the normalize pass.)

---

## 4. Borders & focus

Brief — the color guide holds the full
[border-color conventions](chat-color-guide.md#border-color-conventions).

| Border type | Meaning | Widgets (`file:line`) |
|-------------|---------|------------------------|
| `round` | role / identity frame | `user_chatbox.py:50` (user), `assistant_prose.py:202` (assistant), `approval_block.py:117` / `force_kill_confirm.py:80` (approval, amber), `card_widget.py:180,186-190` (card, by state) |
| `heavy` (top+bottom) | composer divider rails | `composer.py:50,51` (idle), `composer.py:56,57` (focus) |
| `thick` (left) | **focus accent**, `:focus-within` only | `tool_call_block.py:71` / `:74` (under `:focus-within` at `:70,73`), `compaction_block.py:76` / `:79` (under `:focus-within` at `:75,78`) |

The `thick` left bar is **not** an always-on state frame — it appears
only while the widget (or a child) holds focus (`:focus-within`). See the
color guide's [border-color conventions](chat-color-guide.md#border-color-conventions)
for the per-state color mapping.

![Composer idle — heavy top/bottom divider rails](chat-widgets/svg/composer-idle-color.svg)

---

## 5. NO_COLOR / mono

Brief — see the color guide's
[NO_COLOR / mono strategy](chat-color-guide.md#no_color-mono-strategy) for
the full mapping and rationale.

Under `NO_COLOR`, `mono.tcss` replaces the six palette variables with
`ansi_default` (the terminal's own foreground — **not** `auto`, which is
illegal in `border:`; see `process/bug-073-no-color-border-auto-crash.md`).
Role and state are then carried by **text style** instead of hue:

| Color role | Mono text-style |
|------------|------------------|
| identity / action | `bold` |
| hint / disabled | `dim` |
| secondary | `italic` |
| focus | `reverse` |

Information-bearing glyphs (`⚠`, `✓`, `✗`, spinners) are retained under
NO_COLOR — they carry meaning, not decoration.

---

## 6. Inconsistency backlog

Consolidated flags with verified `file:line`, feeding the normalization
pass. (Palette/color bypasses live in the color guide's
[palette-bypass backlog](chat-color-guide.md#palette-bypass-backlog); not
duplicated here.)

| # | Inconsistency | Locations |
|---|---------------|-----------|
| 1 | ~~**Two spinner alphabets**~~ — resolved by qol-076: `src/cli/chat/spinner.py` now owns the single canonical 10-frame table (`SPINNER_FRAMES`); both `turn_status.py` and `workflow_node_row.py` import it. | ~~`turn_status.py:33` vs `workflow_node_row.py:60`~~ |
| 2 | ~~**Divergent state-glyph maps**~~ — resolved by qol-075: `src/cli/chat/state_tokens.py` now owns the single canonical table (`STATE_TOKENS`); widget-specific legacy names resolve via `_LEGACY_ALIASES` with a `DeprecationWarning`. | ~~`workflow_node_row.py:281` vs `card_widget.py:637`~~ |
| 3 | **`▸` double meaning** — running-state glyph and collapsed chevron | `state_tokens.py` (`running`) & `approval_args_row.py:65` |
| 4 | **Two elapsed formats** — minute rollover vs whole-seconds | `turn_status.py:36` vs `card_body/workflow.py:251` |
| 5 | **Two count formats** — `[N/M]` vs `N/M` | `card_body/workflow.py:251` vs `:1078` |
| 6 | **Spacing outliers** — `margin: 1 0` (top+bottom) and `margin-left: 2` indent | `force_kill_confirm.py:82`, `approval_save_confirm.py:54` |
| 7 | **Palette/color bypasses** | see [color guide backlog](chat-color-guide.md#palette-bypass-backlog) |
