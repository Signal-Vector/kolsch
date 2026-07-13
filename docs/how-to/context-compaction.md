# Context compaction

Kolsch keeps every chat turn on the event log verbatim, but long
sessions eventually approach the model's context window ceiling.
When that happens two things go wrong: the provider's fallback
chain trips on `ContextWindowExceeded` mid-turn, or important early
context silently falls off the front and the model starts giving
worse answers.

**Compaction** prevents both. When a session's projected message
list crosses a threshold, the platform swaps the middle of the
conversation for a **JSON summary** that carries **references back
into the event log**. The recent tail stays verbatim; the summary
takes the middle's place in the model's request; the model can
still fetch any dropped event with the `context.reference_lookup`
tool if it needs the exact text.

Compaction is:

- **Model-side only.** Your chat transcript still shows every event.
  Only what the platform sends to the LLM shrinks.
- **Per-thread.** Sub-agents get their own thread and their own
  compaction; the parent's context never sees the child's summary.
- **Modular.** The `platform.yaml` surface is `{variant, trigger_at,
  config}` — same shape as `authorization`, `secrets`, `memory`.
  The v1 built-in variant is `reference_summary`.

Compaction is **not** a replacement for the fallback chain — the
chain still catches `ContextWindowExceeded` reactively if a request
slips past the threshold. Compaction is prevention; the chain is
remediation.

---

## Enable compaction in `platform.yaml`

Add a `compaction:` section. Absent = disabled (existing workspaces
upgrade with zero behavior change).

```yaml
compaction:
  variant: reference_summary   # v1 built-in
  trigger_at: 0.80             # fire at 80% of model's context window
  config:
    keep_recent_fraction: 0.20 # preserve the recent 20% verbatim
    summarizer_model:          # optional; falls back to the exchange's model
      provider: anthropic
      model: claude-haiku-4-5
    max_refs: 50               # cap the summary's `refs[]` list
```

**Field reference:**

| Field | Default | Meaning |
|---|---|---|
| `variant` | required | Provider name registered via `register_compaction_variant`. `reference_summary` is the v1 built-in. |
| `trigger_at` | `0.80` | Fraction of the model's `context_length` (minus `max_output_tokens`) at which the pre-flight fires compaction. `0.80` gives a ~10–15% cushion for tokenizer variance across providers. |
| `config.keep_recent_fraction` | `0.20` | Fraction of the budget preserved verbatim from the RECENT end of the conversation. The middle gets summarised; a tighter fraction gives the summariser more to work with. |
| `config.max_refs` | `50` | Ceiling on `summary.refs[]`. Overflow is clustered into a single `event_id_range` entry — `context.reference_lookup` refuses ranges and asks the model to pick an exact id from the range. |
| `config.summarizer_model` | (reuse exchange's model) | Optional `{provider, model}` for the summariser LLM. A cheap fast model (Haiku, Flash) is a good fit — summariser calls block the exchange. |

**Per-agent override:** an agent's YAML can override the platform
default via `subsystems.compaction: {variant, config}` — same shape
as `subsystems.memory`. Useful for one agent that needs a different
variant or a tighter `trigger_at`.

**When the model doesn't declare a `context_length`** — many
OpenCode / self-hosted models legitimately don't — compaction is
silently skipped for that exchange. The pre-flight logs a debug
line; nothing fails.

---

## What the model sees when compaction fires

In place of the compacted middle-span events, the projection
injects one system message:

```
<context-compaction>
The following prose summary replaces a span of earlier events
that were compacted to fit the model's context window. Use
`context.reference_lookup` with the event_ids in `refs` to
retrieve original content when needed.
<the prose summary>
<summary-json>{"schema_version": 1, "refs": [...], "span": {...}, ...}</summary-json>
</context-compaction>
```

The `<summary-json>` block carries the same schema you can inspect
via `/why-compact` — see [the JSON schema](#the-summary-json-schema)
below.

**Retrieving originals:** the model can call
`context.reference_lookup(event_id="ev_...")` — a builtin the
engine auto-registers on agents whose compaction is active — with
any `event_id` cited in the summary's `refs[]` OR present in
`compacted_event_ids`. Out-of-scope ids fail loudly so the model
sees the error and adjusts.

---

## Slash commands

Four operator-facing commands, all no-argument except
`/why-compact`:

### `/compact [--preview] [--instructions "<text>"]`

Force a compaction NOW without waiting for the threshold. Useful
before a long paste, or when you want to steer the summary toward a
specific focus.

- `--preview` runs the summariser and emits the compaction event
  as a **diagnostic** (marked `preview: true`) without swapping the
  model's live context. Handy for "what would compaction do
  here?".
- `--instructions "<text>"` appends operator guidance to the
  summariser prompt (e.g. `--instructions "keep the specific error
  messages verbatim"`). The JSON contract is unchanged; only the
  summary's prose is biased.

> **Note.** `--preview` still spends summariser tokens. It's a
> visibility knob, not a cost knob. If you're tuning
> `keep_recent_fraction` and want to see what would land without
> ceding the live context, `--preview` is the tool; if you're
> counting summariser costs, remember each `--preview` invocation
> is a full LLM call.

Example:

```
> /compact --instructions "keep the pytest failure output verbatim"

↳ compacted 34 events (8,420 → 780 tokens · /uncompact to restore · /why-compact for details)
```

### `/uncompact`

Revoke the MOST RECENT active compaction on the current thread. The
next model turn sees the previously-compacted span in full again.

- **LIFO first-only.** `/uncompact` unwinds one compaction at a
  time; rolling back multiple in one shot is a foot-gun. Call it
  again to unwind the next one.
- **Does not restart the current exchange.** The live model has
  already "forgotten" the summary text; only the NEXT turn benefits.
- The revocation is a new event on the log
  (`context.compaction_revoked`) — the compaction itself is
  immutable. Assembly filters both from the projection.

### `/compaction-history`

Compact table of every `CONTEXT_COMPACTED` event on the current
thread — when it fired, tokens_before → tokens_after, span, preview
flag, and the revoking event id if the compaction was `/uncompact`ed.

Example:

```
> /compaction-history

evt            fired      tokens      compacted_range              preview revoked
------------------------------------------------------------------------------
ev_a1b2c3d4    14:22:03   8k→0.8k    ev_1234abcd..ev_5678efgh     no      -
ev_e5f6g7h8    14:41:19   6k→0.6k    ev_9abcdef0..ev_ffff0000     no      ev_deadbeef01
```

### `/why-compact <event_id>`

Dump the summariser's output for one compaction event.
`<event_id>` accepts either the full `ev_...` id or a short prefix
(the 12-char form the history table shows).

Example:

```
> /why-compact ev_a1b2c3d4

/why-compact ev_a1b2c3d4
{
  "event_id": "ev_a1b2c3d4-1234-5678-9abc-def012345678",
  "created_at": "2026-07-12T14:22:03.123456+00:00",
  "trigger": "auto",
  "preview": false,
  "revoked_by": null,
  "revoked_by_event_id": null,
  "tokens_before": 8420,
  "tokens_after": 780,
  "compacted_event_ids": ["ev_...", "ev_..."],
  "pre_compaction": { "head_end": 5, "tail_start": 42, "total_events": 47 },
  "summary": {
    "schema_version": 1,
    "summary_text": "The agent read three files, ran a failing pytest, ...",
    "refs": [
      {"event_id": "ev_...", "event_type": "tool.result", "one_line": "read(src/foo.py) → 240 lines"}
    ],
    "span": {"first_event_id": "ev_...", "last_event_id": "ev_...", "event_count": 47},
    "kept_files": ["src/foo.py"],
    "compactor": {"variant": "reference_summary", "model": {"provider": "anthropic", "model": "claude-haiku-4-5"}}
  }
}
```

When the compaction was revoked via a separate
`CONTEXT_COMPACTION_REVOKED` event (from `/uncompact`), the
`revoked_by_event_id` field cross-references the revocation. The
`revoked_by` alias mirrors it for compatibility with older tooling.

---

## `context.reference_lookup`

Auto-registered as a builtin on any agent whose engine holds a
compaction provider AND whose config has compaction enabled. The
model sees it in its tool list; agents without compaction never see
it.

Signature:

```json
{"event_id": "ev_..."}
```

Returns the full event envelope for the requested id:

```json
{
  "event_id": "ev_...",
  "type": "tool.result",
  "data": { ... },
  "created_at": "2026-07-12T14:22:03.123456+00:00"
}
```

**Scope.** Only event_ids that appear in the `refs[]` OR
`compacted_event_ids` of at least one ACTIVE compaction on the
current thread are addressable. Out-of-scope ids get an actionable
error the model can course-correct on:

```
OutOfScope: event_id 'ev_...' is not addressable —
reference_lookup only serves event_ids cited in an active
context compaction. Check the summary's `refs` and
`compacted_event_ids` lists.
```

**Cluster refs (`event_id_range`).** When the summariser produced
more than `max_refs` entries, overflow is folded into a single
cluster ref with `first_event_id` / `last_event_id` /
`event_count`. `reference_lookup` **refuses** cluster refs — the
model must pick a specific event_id from within the range (both
endpoints are in scope).

---

## The summary JSON schema

Every `CONTEXT_COMPACTED.data.summary` populates this shape:

```json
{
  "schema_version": 1,
  "summary_text": "3–8 sentence prose summary of what happened",
  "refs": [
    {"event_id": "ev_...", "event_type": "tool.result", "one_line": "read(...) → N lines"}
  ],
  "span": {"first_event_id": "ev_...", "last_event_id": "ev_...", "event_count": 47},
  "kept_files": ["src/foo.py"],
  "compactor": {"variant": "reference_summary", "model": {"provider": "...", "model": "..."}}
}
```

Load-bearing fields:

- `schema_version` — v2 evolves safely; v1 today.
- `refs[].event_id` — the whole point. The model uses these with
  `context.reference_lookup`.
- `span.{first,last}_event_id` — Assembly uses these to detect
  which events are "stood in for".
- `summary_text` — the model reads this as context.

Optional / diagnostic: `kept_files`, `compactor.*`.

---

## Failure ladder

Summariser calls fail. When they do:

1. **Tier 1 — retry once.** A single transient failure (network,
   429) is absorbed silently — the exchange proceeds with the
   retried summary.
2. **Tier 2 — mechanical truncation.** If tier 1 also fails, the
   platform emits `context.compaction_failed` with the reason, then
   falls back to **mechanical truncation**: the middle span is
   still dropped from the projection, `refs[]` is populated with
   one-line `{event_id, event_type}` entries so
   `context.reference_lookup` still works, and the summary carries
   `trigger: "auto_truncation"`. The exchange proceeds; the user
   never sees the failure.
3. **Tier 3 — hard-fail.** If mechanical truncation itself can't
   reduce below the model's window (the "user pasted a 200 KB
   blob" case where the recent tail alone exceeds the budget),
   the exchange fails with
   `error.type = "context.overflow_uncompactable"`. The chat
   surface renders an actionable message ("your latest message
   alone exceeds the model's window; drop the paste or move it
   to a `context_files:` reference").

**Non-shrinking guard.** If the produced summary doesn't reduce
projected tokens below `0.90 × tokens_before`, the compaction is
discarded — `context.compaction_skipped` fires with
`reason="no_net_savings"` — and the trigger retries with a tighter
`keep_recent_fraction` (halved, then quartered — max two retries).
If all retries fail to shrink, tier 2 fires.

**Never fails your exchange.** Compaction is background prevention;
your user turn only fails on the tier-3 hard-fail case above.

---

## Add a custom variant

Register a factory that implements the
`ContextCompactionProvider` protocol
(`src/providers/compaction.py`). Minimum surface:

```python
from providers.compaction import (
    register_compaction_variant,
    ContextCompactionProvider,
)

class MyVariant:
    def __init__(self, *, llm_for_model, config):
        ...

    async def compact(
        self,
        *,
        events,
        keep_head,      # always 0 in v1
        keep_tail,      # tokens to preserve verbatim from the tail
        model_context_length,
        thread_id,
        config,         # variant-specific args from platform.yaml
    ) -> ContextCompactedData:
        # produce a ContextCompactedData; the engine emits it.
        ...

register_compaction_variant("my_variant", MyVariant)
```

Then in `platform.yaml`:

```yaml
compaction:
  variant: my_variant
  trigger_at: 0.80
  config:
    # your variant's args
    ...
```

Registration typically runs at bundle import time so
`bootstrap_runtime` picks the variant up.

---

## Sub-agent behavior

Every sub-agent gets its own thread; every thread has its own
budget; **compaction is per-thread**. When a workflow node spawns
an agent, that child's exchanges accumulate into a child thread
and — if configured — compact there. The parent's projected context
never sees the child's summary; it only sees the child's structured
output via the workflow node's `outputs:`.

Workflow runs themselves don't accumulate LLM context (the workflow
executor is a state machine), so compaction doesn't apply at the
workflow level.

---

## Interaction with other features

- **`/why`** (feat-041) walks raw events. When a `/why` target
  falls inside a compacted range, the deny-card renderer badges
  it: *"the referenced call is inside compacted range
  {compaction_event_id}; the LIVE model may not remember it."*
- **`/reload`** (bug-044) rebuilds authz layers. Compaction state
  survives — the reload is orthogonal.
- **`/export`** always exports RAW events, never summaries.
  Fidelity matters for bug reports.
- **`/fork`** (workflow fork) creates a new workflow run, not a new
  chat context. When a forked replay-mode Assembly needs
  byte-identical projection, it passes `respect_compaction=False`
  so `CONTEXT_COMPACTED` events project as no-ops.

---

## Related reading

- [Agent engine contract](../explanation/agent-engine-contract.md)
  — pre-flight, exchange loop, event log semantics.
- [Chatting effectively](chatting-effectively.md) — the
  operator-facing side of long sessions.
- [Configure fallbacks](configure-fallbacks.md) — the reactive
  companion to compaction (`ContextWindowExceeded` handling).
- The spec: `process/feat-054-context-compaction.md` in-repo.
