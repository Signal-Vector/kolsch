# QoL — `kolsch sessions list` column overhaul

## Context

`kolsch sessions list` currently prints one line per session in `key=value`
form: `session_id`, `agent=<ag_id>`, `name=<cli:agent_ref>`, `starred=<bool>`,
`archived=<bool>`. Two columns are noise (`starred`/`archived` are almost
always false in practice), one column is redundant (`agent=` duplicates
what's already in `name=`), and two genuinely useful pieces of information
— *when was this session started* and *how recent was the last activity* —
aren't shown at all.

This change tightens the default output to what a user actually wants
when listing sessions:

| Before | After |
|---|---|
| `ses_xxx  agent=ag_demo  name=cli:acme/agents/demo  starred=False  archived=False` | `ses_xxx  2025-05-29 14:23  30m  cli:acme/agents/demo` |

Single-file change in the CLI layer. No schema, no store API, no engine
work.

---

## Scope

| Change | File | Type |
|---|---|---|
| Replace output format in `_cmd_sessions_list` with aligned column form | `src/cli/main.py:372–384` | edit |
| Add private helpers: `_format_delta(now, then)`, `_compute_last_event(runtime, session)` | same file | add |
| Sort by last-event time descending (most recent first) | same file | small addition |
| Update `TC-CLI-03` assertions in tests | `tests/component/test_cli.py` (around lines 290–310) | edit |
| Update `TC-CLI-03` `Expected` line in test catalog | `process/test-catalog.md` | edit |

**Not touched:** filter flags `--starred` / `--archived` stay on the
subparser. They still filter the underlying query; they just no longer
have columns in the default output. Removing them entirely would be
scope creep and would require an extra deprecation note.

---

## Design

### New output format

```
SESSION_ID      STARTED              LAST    NAME
ses_a1b2c3d4    2025-05-29 14:23     30m     cli:acme/agents/demo
ses_5e6f7a8b    2025-05-28 09:11     2h      cli:kolsch/agents/helper
ses_xyz98765    2025-05-19 18:42     10d     cli:reference/agents/openclaw
```

Plain f-string padding; no new dependency.

Column widths:
- `SESSION_ID` — 14 chars (`ses_` + 10).
- `STARTED` — 16 chars (`YYYY-MM-DD HH:MM`, no timezone — local).
- `LAST` — 6 chars (max-width like `999d`).
- `NAME` — unbounded, rest of line.

When no sessions exist: print nothing (matches current behavior; not
a new empty-state hint).

### Delta formatter

```python
def _format_delta(delta: timedelta) -> str:
    """Render a timedelta as a compact ago-style string."""
    s = int(delta.total_seconds())
    if s < 60:        return f"{s}s"
    if s < 3600:      return f"{s // 60}m"
    if s < 86_400:    return f"{s // 3600}h"
    return f"{s // 86_400}d"
```

No "1y" cap — `1095d` is honest and rare. No "just now" or relative
prose; the column is fixed-width and grep-friendly.

### Last-event computation

Already-available primitives:

- `runtime.store.list_threads(session_id)` — returns the session's
  threads.
- `read_last_event_for_thread(jsonl_path, thread_id)` from
  `src/store/jsonl.py:223` — returns the most recent event for a
  thread.

`_compute_last_event(runtime, session)` walks the threads, takes the
max `event.created_at` across all of them, returns it (or `None` if
the session has no events yet — rare, show as `—` in the column).

Cost: one JSONL tail-scan per thread per session. For a typical
workspace (<100 sessions, 1 thread each) this is fast; for
pathological cases (>10k sessions) noticeable. Flagged as deferred
optimization — not a blocker.

### Sort order

Sort sessions by `last_event_at` descending (most recent first).
Sessions with no events sort to the bottom. Today the store returns
them in insertion order; the CLI sorts after fetching. Single
`sorted(...)` call, no store change.

### Filter flags stay

`--starred`, `--archived`, `--limit` remain on the subparser. They
still pass through to `runtime.store.list_sessions(...)`. The
behavior change is purely on the display side.

---

## Critical files

- **`src/cli/main.py`** — `_cmd_sessions_list` at lines 372–384. Add
  the two helpers above this function or in a small `_format` block
  at file bottom.
- **`tests/component/test_cli.py`** — `test_tc_cli_03_sessions_list`
  around lines 290–310. Current assertions use substring checks
  (`"ag_demo" in out`); the `"ag_demo"` substring goes away.
- **`process/test-catalog.md`** — TC-CLI-03 `Expected` line
  reads: *"stdout includes the session id, agent_id, and the
  `cli:<agent_ref>` name."* Update to: *"stdout includes the session
  id, the ISO start time, a compact last-event delta (e.g., `30m`),
  and the `cli:<agent_ref>` name. The agent_id is no longer in the
  default output."*

---

## Existing utilities reused

- `read_last_event_for_thread(path, thread_id)` —
  `src/store/jsonl.py:223–243`.
- `runtime.store.list_threads(session_id)` — existing store method.
- `runtime.store.list_sessions(starred, archived, limit)` —
  existing store method, unchanged signature.
- `Session.session_id`, `Session.created_at`, `Session.name` —
  `src/schemas/entities.py:30–48`.
- `Event.created_at` — `src/schemas/events.py:315–335`.

No new dependencies.

---

## Verification

### Local smoke

```bash
# Inside the dev container or with the venv active.
uv run kolsch run kolsch/agents/helper --input content="hello"
uv run kolsch run kolsch/agents/helper --input content="another"

uv run kolsch sessions list
# Expect:
#   SESSION_ID      STARTED              LAST    NAME
#   ses_xxx         2026-05-30 14:23     5s      cli:kolsch/agents/helper
#   ses_yyy         2026-05-30 14:20     3m      cli:kolsch/agents/helper
```

### Tests

```bash
podman-compose exec dev uv run pytest tests/component/test_cli.py::test_tc_cli_03_sessions_list -v
podman-compose exec dev uv run pytest tests/component/test_cli.py -q
podman-compose exec dev uv run pytest tests/integration -q   # golden tests for events untouched
```

### Edge cases worth eyeballing manually

- `kolsch sessions list` with zero sessions → empty output, exit 0.
- A session with no events yet (created but never run) → the `LAST`
  column shows `—`.
- `kolsch sessions list --starred` still filters by starred (even
  though the column is gone).
- `kolsch sessions list --limit 1` returns the most-recently-active
  session (verifies sort order is what users expect).

---

## Out of scope

- Removing the `--starred` / `--archived` filter flags.
- Adding a `--verbose` mode that shows the removed columns.
- Rich-table output (Phase 2.1 will pull in Rich; pre-emptive use
  here is scope creep).
- A new store method `last_event_at(session_id)` for performance —
  the per-thread tail-scan is fine for v1.
- Pagination beyond `--limit`.
