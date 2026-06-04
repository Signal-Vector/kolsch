# Step 1 Writeup — qwen-timeout investigation: replay tool + baselines

**Status:** ready for coordinator review (STOP gate).
**Branch:** working on `fix/agent-id-derive-from-ref` (existing branch — Step 3 will rebranch if a fix lands).
**Bug doc:** `process/bug-chat-opencode-qwen-timeout-investigation.md`

---

## 1. What shipped

- `scripts/replay_session.py` — replay tool with `--mode {kolsch,curl}`,
  `--target {all,first-N,last-N,a,b,c}`, `--output <json>`. SHA-256
  checks the source JSONL before/after the run; aborts if it ever
  changes. Each call is capped at `PER_CALL_TIMEOUT_SECONDS = 60.0`
  via `asyncio.wait_for` (kolsch) or `httpx.AsyncClient(timeout=60)`
  (curl). No tool dispatch; only the LLM call is re-issued.
- `/tmp/kolsch-replay/ses_abae69a7.original.jsonl` — read-only copy of
  the trigger session (`sha256 fbc95784…`).
- `/tmp/kolsch-replay/baseline-kolsch.json` and `…/baseline-curl.json`
  — per-call results for both modes against the full session (53
  `model.request` events each).

The replay tool was confirmed not to write to the source path: both
baselines record `session_sha256_before == session_sha256_after`,
and an out-of-band `shasum` against the workspace original matches
the copy at run end.

### Implementation notes the reviewer should flag if surprising

- The script imports kolsch's runtime (`sys.path.insert(0, src/)`)
  and constructs `DotenvSecretsProvider` + the `ProviderConfig` map
  directly from `~/Documents/kolsch/platform.yaml`. It re-uses
  `engine._crewai_adapter.make_build_llm_factory` so the kolsch path
  exercises the *exact* code the production chat surface does.
- For curl mode, the bug doc's reshape step (event `messages` →
  OpenAI chat-completions JSON) had a subtlety that bit me on the
  first run: kolsch stores `tool_calls` in the engine's flat shape
  (`{id, name, arguments(dict)}`), not the OpenAI wire shape
  (`{id, type:"function", function:{name, arguments(JSON-string)}}`).
  Sending the flat shape directly yields HTTP 400 `Field required:
  …tool_calls.0.function`. The script now reshapes via
  `_to_openai_tool_call` (mirrors what
  `engine._crewai_adapter._engine_tool_call_to_openai` does inside
  litellm). First curl run hit this; the captured run is the
  corrected one.

### Replay-tool invocation reference (Step 2 will reuse this)

```bash
.venv/bin/python scripts/replay_session.py \
  --session /tmp/kolsch-replay/ses_abae69a7.original.jsonl \
  --mode kolsch \
  --target all \
  --output /tmp/kolsch-replay/<TAG>.json
```

`--target first-N` / `--target last-N` / `--target 0,3,7` also work.
The kolsch run is sequential and rate-bound: 53 events take roughly
**18 minutes** end-to-end on the failing config (success p50 ≈ 4.5s
plus 17 × 60s timeouts). Curl is **~8 minutes** (lower per-call cost
but more long-tail successes).

---

## 2. Baseline results

### 2.1 kolsch baseline (production pipeline)

| Metric | Value |
| --- | --- |
| Total calls | 53 |
| Success | 36 (68%) |
| Timeout | **17 (32%)** |
| Other error | 0 |
| Success p50 | 4.5 s |
| Success p90 | 33.8 s |
| Success max | 38.6 s |
| Timeout latency distribution | 17/17 calls at **60.0 ± 0.002 s** |

All 17 timeouts hit the script's 60-second per-call cap exactly.
None failed earlier than the cap. This means the kolsch path *never
chose to give up before 60s* — it sat in a retry / wait loop until
our outer timer killed it.

### 2.2 curl baseline (raw provider)

| Metric | Value |
| --- | --- |
| Total calls | 53 |
| Success | 45 (85%) |
| Timeout | 8 (15%) |
| Other error | 0 |
| Success p50 | 4.5 s |
| Success p90 | 50.1 s |
| Success max | 55.3 s |
| Timeout latency distribution | 8/8 calls at **~60.0 s** (60001–60129 ms) |

The curl baseline shows the **provider's natural latency tail
extends well past 30 s** — twelve calls succeeded between 30 s and
56 s. The same 8/8 timeouts at the 60s cap indicate the provider
itself also has a class of requests that genuinely never complete
within 60 s.

### 2.3 Latency-bucket comparison

| Bucket | kolsch | curl |
| --- | ---: | ---: |
| 0–5 s success | 20 | 25 |
| 5–10 s success | 9 | 8 |
| 10–30 s success | 2 | 0 |
| 30–60 s success | **5** | **12** |
| timeout at 60 s cap | **17** | 8 |
| other_error | 0 | 0 |

The kolsch column has *fewer* 30–60s successes than curl (5 vs 12)
and *more* 60s timeouts (17 vs 8). That's the inverse of what we'd
see if the timeouts were strictly upstream — and exactly what we'd
expect if a client-side cap below 30 s was killing long-but-real
calls before they could complete.

### 2.4 Per-event flip analysis

10 of the 17 kolsch timeouts succeeded on the curl path:

| Event idx | Curl latency |
| ---: | ---: |
| 10 | 9.6 s |
| 76 | 50.2 s |
| 92 | 45.8 s |
| 112 | 6.4 s |
| 120 | 4.9 s |
| 133 | 50.1 s |
| 138 | 48.5 s |
| 143 | 49.7 s |
| 192 | 32.1 s |
| 207 | 5.8 s |

Of those, **7 took >30 s on curl** — i.e. they're calls the
provider does answer, but only after the kolsch connector's 30s
client timeout has already fired and torn the request down.

The 3 fast-succeed-on-curl cases (idx 10, 112, 120, 207 — under
10s) suggest *some* of the kolsch timeouts are not purely
latency-driven; they're caused by what comes *after* the 30s
upstream timeout fires inside crewai/litellm (probably the retry
loop hangs or the timed-out connection never frees a slot before
our 60s cap).

---

## 3. Initial hypothesis-tree narrowing

**Picked starting hypothesis: H1 (client-side 30 s timeout too aggressive).**

Backing data:

- The connector defaults to `_DEFAULT_TIMEOUT_SECONDS = 30.0`
  (`src/llm/providers/opencode.py:123`); `platform.yaml` does not
  override `timeout_seconds` for the user's opencode entry, so the
  30 s default is what flows to `crewai.LLM(timeout=30)`.
- The curl baseline (no kolsch) records **12 successful calls
  between 30 s and 56 s**. Every one of those would be a kolsch-side
  timeout under the current 30 s default.
- All 17 kolsch timeouts hit the outer 60s cap, never anything
  shorter. If the issue were "the provider 400s after 12 s", we'd
  see scattered short-fail latencies. We don't.
- Five kolsch successes landed in the 30–38s range — only possible
  if crewai retried internally after the first 30s timeout fired,
  picking up a faster second attempt.

**H2 (server-side cutoff) is co-tested, not ruled out.**

8 curl timeouts at 60 s tell us that even with no kolsch overhead,
a fraction of calls (~15%) on this session genuinely don't return
inside a minute. Step 2's probe H2 (curl with the same payload and
a longer cap, plus a control "trivial-prompt" curl) will tell us
whether that 15% is a hard upstream cap (~60s) or a long-tail
latency distribution.

**H3 (long-context degradation) — soft evidence, deferred.**

The 12 curl successes ≥30s are clustered in the middle of the
conversation (event indices 76–143, then 183, 218); the very last
exchange (idx 218 onward) also flips heavily into timeouts. That
*could* be a context-length effect, but the same indices show fast
2-5s curl successes interleaved, so it isn't monotonic. Step 2's
H3 probe (first-5 vs last-5 latency comparison) can confirm or
rule out cleanly.

**H4, H5, H6 — not directly supported by baseline yet.**

- H4 (litellm/hosted_vllm overhead): the first call took 8.3 s on
  kolsch vs 3.2 s on curl — ~5 s of framework overhead on a trivial
  exchange. Real but small relative to the 30 s+ tail. Probably a
  secondary contributor at best.
- H5 (provider regional/capacity): both baselines ran back-to-back
  in a single ~30-minute window so time-of-day variation is
  controlled away. Step 2's H5 probe (different time-of-day OR
  different model) is still needed but baselines don't move the
  ranking.
- H6 (max_tokens / request size): no direct evidence either way.
  Step 2's H6 probe is cheap and worth running.

### Proposed Step 2 order

1. **H1 first** — easiest test (bump `platform.yaml` opencode
   `timeout_seconds: 120`, rerun kolsch). Expected if H1 is the
   cause: kolsch timeouts drop from 17 to ≤8 (matching curl) AND
   the 5 ≥30s successes grow to include curl's 30–56 s cohort.
2. **H2 next** — if H1 only partially closes the gap (e.g., 17 →
   12, not 17 → 8), the residual 8 are upstream-bounded. The H2
   probe (trivial-prompt curl + extended-cap curl) decides whether
   that's recoverable or a documented blocker.
3. **H6** alongside H1 — cheap to test (drop max_tokens in the
   agent runtime), and even if it doesn't close on its own, the
   combination with H1 could land Path A in Step 3.
4. **H3 / H4 / H5** only if 1+2+6 don't close it.

---

## 4. STOP — waiting on coordinator approval

Per the bug doc, Step 1 ends here. No fixes have been applied:

- `git status` in `src/` shows only the pre-existing branch state
  (the in-flight `fix/agent-id-derive-from-ref` work). The replay
  tool lives in `scripts/`, not `src/`.
- `~/Documents/kolsch/platform.yaml` is untouched (the H1 probe
  will edit it in Step 2 and revert before handoff).
- The original session file is byte-identical to the workspace
  source (sha256 verified both before and after each run, plus
  out-of-band against `~/Documents/kolsch/sessions/`).

**Question for the coordinator:** approve to proceed with Step 2 in
the order proposed above (H1 → H2 → H6 → fallback to H3/H4/H5)? Or
re-prioritise?

The escalation path is unchanged from the bug doc Part 5; flagging
in advance that if H1 lands a clean fix without H2 evidence I'll
still want to run the H2 probe so the writeup can name whether the
residual 8 curl timeouts are an upstream cap worth documenting.
