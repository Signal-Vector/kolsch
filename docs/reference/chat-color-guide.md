# Chat color guide

The semantic color palette for the chat TUI, its usage rules, and the
`NO_COLOR` (mono) strategy. This page **documents current conventions and
flags inconsistencies** as a backlog for a later normalization pass — it
does not change any CSS.

The renders on this page are real widget captures produced by the
[widget capture harness](#regenerating-the-gallery). For the spacing,
glyph, and border conventions see the
[chat styleguide](chat-styleguide.md).

!!! note "Source of truth"
    The palette lives in `src/cli/chat/themes/theme.tcss` (color) and
    `src/cli/chat/themes/mono.tcss` (`NO_COLOR`). Theme selection is by
    environment: `_resolve_css_path()` in `src/cli/chat/app.py:157` loads
    `mono.tcss` when `NO_COLOR` is set, else `theme.tcss`.

## Palette

Six semantic variables carry every intentional hue. Widgets reference the
variable, not the hex, so a future theme is one more `.tcss` file.

<div class="chat-swatch" markdown>

| Var | Swatch | Hex | Semantic | Mono |
|---|---|---|---|---|
| `$user-border` | <span style="display:inline-block;width:1.1em;height:1.1em;border:1px solid #888;background:#2ea043"></span> | `#2ea043` | user turn frame (GitHub green) | `ansi_default` |
| `$assistant-fg` | <span style="display:inline-block;width:1.1em;height:1.1em;border:1px solid #888;background:#b0c0d5"></span> | `#b0c0d5` | assistant prose / frame (soft blue-grey) | `ansi_default` |
| `$tool-fg` | <span style="display:inline-block;width:1.1em;height:1.1em;border:1px solid #888;background:#4dd0e1"></span> | `#4dd0e1` | tool calls, done states (cyan-400) | `ansi_default` |
| `$error` | <span style="display:inline-block;width:1.1em;height:1.1em;border:1px solid #888;background:#ff5555"></span> | `#ff5555` | error / failed | `ansi_default` |
| `$warning` | <span style="display:inline-block;width:1.1em;height:1.1em;border:1px solid #888;background:#ffb454"></span> | `#ffb454` | warning / paused / awaiting approval | `ansi_default` |
| `$hint` | <span style="display:inline-block;width:1.1em;height:1.1em;border:1px solid #888;background:#6e7681"></span> | `#6e7681` | hints / disabled / low-emphasis | `ansi_default` |

</div>

Declared in `theme.tcss`: `$user-border` L23, `$assistant-fg` L24,
`$tool-fg` L25, `$error` L28, `$warning` L29, `$hint` L30. The `mono.tcss`
mapping (all six → `ansi_default`) is at `mono.tcss:28–34`.

### Off-palette colors (⚑ backlog)

These carry hue but are **not** part of the six-variable palette. They are
flagged for the normalization pass, not fixed here.

<div class="chat-swatch" markdown>

| Value | Swatch | Where | Issue |
|---|---|---|---|
| `$compact-fg` `#b39ddb` | <span style="display:inline-block;width:1.1em;height:1.1em;border:1px solid #888;background:#b39ddb"></span> | `compaction_block.py:49` | widget-local var; a compaction accent that isn't in the global palette |
| `$card-fg` `#4dd0e1` | <span style="display:inline-block;width:1.1em;height:1.1em;border:1px solid #888;background:#4dd0e1"></span> | `card_widget.py:174` | widget-local var; duplicates `$tool-fg`'s hex under a second name |
| `#1a1d24` | <span style="display:inline-block;width:1.1em;height:1.1em;border:1px solid #888;background:#1a1d24"></span> | `info_bar.py:53`, `slash_popup.py:103` | undeclared background; duplicated literal with no variable |

</div>

## Border-color conventions

Role frames use `round` borders in the owning role's color; dividers use
`heavy`. The `thick` left bar is a **focus accent**: it is guarded by
`:focus-within` and appears **only while the widget holds focus** — it is
not a persistent state frame. When unfocused, `ToolCallBlock` and
`CompactionBlock` convey state through their `CollapsibleTitle` text color
instead (e.g. `tool_call_block.py:65,68`).

| Widget | Declaration | Color | Source |
|---|---|---|---|
| `UserChatbox` | `border: round $user-border` | user green | `user_chatbox.py:50` |
| `AssistantProse` | `border: round $assistant-fg` | assistant blue-grey | `assistant_prose.py:202` |
| `ApprovalBlock` | `border: round $warning` | amber | `approval_block.py:117` |
| `ForceKillConfirm` | `border: round $warning` | amber | `force_kill_confirm.py:80` |
| `CardWidget` (default) | `border: round $card-fg` | cyan (local var) | `card_widget.py:180` |
| `CardWidget.-queued` | `border: round $hint` | dim | `card_widget.py:186` |
| `CardWidget.-paused` | `border: round $warning` | amber | `card_widget.py:187` |
| `CardWidget.-cancelling` | `border: round $card-fg; opacity: 0.7` | dimmed cyan | `card_widget.py:188` |
| `CardWidget.-error` | `border: round $error` | red | `card_widget.py:189` |
| `CardWidget.-cancelled` | `border: round $hint` | dim | `card_widget.py:190` |
| `Composer` | `border-top/bottom: heavy $user-border` | user green dividers | `composer.py:50,51,56,57` |
| `ToolCallBlock` (ok) | `:focus-within { border-left: thick $tool-fg }` | cyan bar — **focus only** | `tool_call_block.py:70` (decl. L71) |
| `ToolCallBlock` (failed) | `.-failed:focus-within { border-left: thick $error }` | red bar — **focus only** | `tool_call_block.py:73` (decl. L74) |
| `CompactionBlock` | `:focus-within { border-left: thick $compact-fg }` | purple bar (local var) — **focus only** | `compaction_block.py:75` (decl. L76) |
| `CompactionBlock.-failed` | `.-failed:focus-within { border-left: thick $error }` | red bar — **focus only** | `compaction_block.py:78` (decl. L79) |

<figure markdown>
![UserChatbox — green round frame](chat-widgets/svg/user-chatbox-color.svg)
![AssistantProse — blue-grey round frame](chat-widgets/svg/assistant-prose-color.svg)
<figcaption>Role frames: user green (<code>$user-border</code>) vs assistant blue-grey (<code>$assistant-fg</code>).</figcaption>
</figure>

<figure markdown>
![CardWidget error — red frame](chat-widgets/svg/card-error-color.svg)
![CardWidget paused — amber frame](chat-widgets/svg/card-paused-color.svg)
![CardWidget queued — dim frame](chat-widgets/svg/card-queued-color.svg)
<figcaption>CardWidget state frames: error red (<code>$error</code>), paused amber (<code>$warning</code>), queued dim (<code>$hint</code>).</figcaption>
</figure>

<figure markdown>
![ToolCallBlock ok — cyan title text](chat-widgets/svg/tool-call-ok-color.svg)
![ToolCallBlock failed — red title text](chat-widgets/svg/tool-call-failed-color.svg)
<figcaption>State-colored <code>CollapsibleTitle</code> text: cyan when ok
(<code>$tool-fg</code>, <code>tool_call_block.py:65</code>), red when failed
(<code>$error</code>, <code>:68</code>). These tiles are captured
<em>unfocused</em>, so the thick left bar — a <code>:focus-within</code>
accent — is not shown.</figcaption>
</figure>

## Semantic usage rules

Hue is meaning, not decoration. Use the variable that matches the role or
state; do not pick a color for its own sake.

| Meaning | Color | Variable |
|---|---|---|
| user turn / user identity | green | `$user-border` |
| assistant prose / assistant frame | blue-grey | `$assistant-fg` |
| tool calls, completed / "done" states | cyan | `$tool-fg` |
| error, failed | red | `$error` |
| warning, paused, awaiting approval | amber | `$warning` |
| hint, disabled, cancelled, low-emphasis | grey | `$hint` |

<figure markdown>
![NoticeWidget info](chat-widgets/svg/notice-info-color.svg)
![NoticeWidget warning](chat-widgets/svg/notice-warning-color.svg)
![NoticeWidget error](chat-widgets/svg/notice-error-color.svg)
<figcaption>Severity mapping on <code>NoticeWidget</code>: info, warning (amber), error (red).</figcaption>
</figure>

## NO_COLOR / mono strategy

Under `NO_COLOR`, `mono.tcss` replaces all six palette variables with
`ansi_default` — the terminal's own default foreground — and role/state is
carried by **text style** instead of hue.

!!! warning "`ansi_default`, not `auto`"
    The mono variables map to `ansi_default`, **not** `auto`. `auto` is
    legal only for text `color:` (it computes an auto-contrast foreground
    against the rendered background); it is **illegal in `border:`**. Since
    the palette variables are used in border properties too (see the table
    above), `auto` made every border-using widget fail stylesheet parse and
    crash under `NO_COLOR`. `ansi_default` is legal for both `color:` and
    `border:` and defers to the terminal's foreground. See
    `process/bug-073-no-color-border-auto-crash.md`. (This was fixed in the
    spec-071 pass, so it is *not* an open item in the backlog below.)

Color → text-style mapping (`mono.tcss:12–21` documents the intent;
widgets do the per-role work in their own `DEFAULT_CSS`):

| Role / state | Text style | Example |
|---|---|---|
| identity / action | `bold` | `card_widget.py:194`, `info_bar.py:63`, `approval_block.py:128` |
| hint / disabled / low-emphasis | `dim` | `compaction_block.py:83`, `tool_call_block.py:78` |
| secondary / de-emphasized | `italic` | `card_widget.py:201`, `notice.py:41` (`dim italic`) |
| focus / selection | `reverse` | `assistant_prose.py:112` (`bold reverse`) |

The `⚠` glyph is retained under `NO_COLOR` as an information carrier
(`mono.tcss` header comment). Syntax highlighting is suppressed too:
`AssistantProse` swaps `MarkdownFence` for a `_PlainFence` that returns
plain dim monospace instead of running Pygments when `NO_COLOR` is set
(`assistant_prose.py:16–20`, `assistant_prose.py:154–167`).

<figure markdown>
![UserChatbox mono](chat-widgets/svg/user-chatbox-mono.svg)
![CardWidget error mono](chat-widgets/svg/card-error-mono.svg)
![ToolCallBlock failed mono](chat-widgets/svg/tool-call-failed-mono.svg)
<figcaption>Mono variants (<code>ansi_default</code> + text-style). The same widgets as above, rendered under <code>NO_COLOR</code>: hue is gone, structure and emphasis remain.</figcaption>
</figure>

## Palette-bypass backlog (⚑)

Widgets that hard-code hue instead of referencing the six palette
variables. Flagged for the normalization pass; **not changed here**.

| Widget | Line(s) | Bypass | Equivalent palette var |
|---|---|---|---|
| `info_bar.py` | 53 | `background: #1a1d24` | — (undeclared) |
| `info_bar.py` | 56 | `color: #6e7681` | `$hint` |
| `info_bar.py` | 62 | `color: #b0c0d5` | `$assistant-fg` |
| `slash_popup.py` | 95–98 | redeclares `$tool-fg`/`$hint`/`$assistant-fg`/`$warning` as literal hex, shadowing the theme vars | the four named vars |
| `slash_popup.py` | 103 | `background: #1a1d24` | — (undeclared; duplicates `info_bar`) |
| `notice.py` | 44 | `color: #ffb454` | `$warning` |
| `notice.py` | 48 | `color: #ff5555` | `$error` |
| `node_section.py` | 39 | `color: #ff5555` | `$error` |
| `workflow_node_row.py` | 274–278 | `#4dd0e1` / `#ff5555` / `#ffb454` / `#6e7681` per state | `$tool-fg` / `$error` / `$warning` / `$hint` |
| `compaction_block.py` | 49 | `$compact-fg: #b39ddb` (widget-local var) | — (no global equivalent) |
| `card_widget.py` | 174 | `$card-fg: #4dd0e1` (widget-local var) | duplicates `$tool-fg` |

Two follow-up themes for the normalization pass: (1) route every literal
hue through the six variables, and (2) decide whether the compaction accent
and the shared card background (`#1a1d24`) deserve their own palette
entries.

## Regenerating the gallery

The SVGs above are a curated subset. To regenerate the full widget gallery
(all 27 widgets × states, color + mono):

```
uv run python -m scripts.chat_styleguide
```

This writes `build/gallery.html` (gitignored). Open it to eyeball every
widget's real render. The harness is documented in spec-071.
