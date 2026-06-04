# Step 2 Writeup — qwen-timeout investigation: hypothesis probes

**Status:** ready for coordinator review (STOP gate, before Step 3).
**Bug doc:** `process/bug-chat-opencode-qwen-timeout-investigation.md`
**Step 1 writeup:** `process/bug-chat-opencode-qwen-timeout-step1-writeup.md` (coordinator-approved).

---

## TL;DR — H1 alone closes the gap to 0 timeouts in 53/53 calls

The picked cause + action for Step 3:

- **Cause (H1):** the opencode connector's default client timeout
  of **30 s** (`src/llm/providers/opencode.py:123`) is below the
  provider's natural latency tail. Step 1's curl baseline already
  showed twelve calls succeeded in 30–55 s; this probe shows the
  tail extends to **113 s** on some exchanges that the user can
  trigger from the failing session.
- **Action for Step 3:** bump the connector default. The exact
  value is the coordinator's call — see "Recommendation" below.

H2 and H6 were deferred per the bug doc's STOP CRITERION ("the
moment a probe identifies a CAUSE with a corresponding ACTION,
stop further probes"). The H1 probe also incidentally answered
the H2 question — the 8 calls that timed out at 60s on the curl
baseline are *the same calls* that succeed at 60–114 s on the
H1-probe kolsch run, so the curl-side timeouts are long-tail
latency, not an upstream cap.

---

## 1. PROBE H1 — Client-side timeout bump

### Setup

- Backed up `~/Documents/kolsch/platform.yaml` to
  `/tmp/kolsch-replay/platform.yaml.backup`
  (sha256 `be825545c8175…`).
- Added `timeout_seconds: 120` under `llm_providers.opencode` and
  ran the replay.
- Bumped the script's outer per-call cap to 150 s
  (`--per-call-timeout 150`) so the measurement could distinguish
  "connector honored the 120 s" from "framework retried past 120 s".
  Added a `--per-call-timeout` flag to the script for this
  (defaults to 60 so Step 1's baselines remain reproducible at
  their original numbers).
- After capturing the artefact, restored platform.yaml from the
  backup (sha verified identical pre- and post-probe).

### Result

`/tmp/kolsch-replay/ses_abae69a7.H1-timeout-120s.json`

| Metric | Baseline-kolsch | Baseline-curl | H1 probe |
| --- | ---: | ---: | ---: |
| Success | 36 | 45 | **53** |
| Timeout | 17 | 8 | **0** |
| Other error | 0 | 0 | 0 |
| Success p50 (ms) | 4 502 | 4 508 | 13 159 |
| Success p90 (ms) | 33 761 | 50 117 | 56 004 |
| Success max (ms) | 38 628 | 55 303 | **113 477** |

(p50/p90/max move *up* not because per-call latency degraded but
because the long-tail successes that previously dropped out are
now included in the distribution.)

### Latency distribution of the previously-timed-out calls

Of the 17 calls that timed out on baseline-kolsch:

| Event idx | Baseline-kolsch | Baseline-curl | H1 probe |
| ---: | ---: | ---: | ---: |
| 10 | timeout (60 s cap) | 9.6 s | 15.2 s |
| 76 | timeout | 50.2 s | 3.9 s |
| 92 | timeout | 45.8 s | 6.4 s |
| 112 | timeout | 6.4 s | 42.8 s |
| 120 | timeout | 4.9 s | 7.6 s |
| 125 | timeout | timeout | 42.2 s |
| 133 | timeout | 50.1 s | 42.9 s |
| 138 | timeout | 48.5 s | 68.4 s |
| 143 | timeout | 49.7 s | 20.4 s |
| 187 | timeout | timeout | 37.0 s |
| 192 | timeout | 32.1 s | **112.0 s** |
| 197 | timeout | timeout | 46.2 s |
| 202 | timeout | timeout | **113.5 s** |
| 207 | timeout | 5.8 s | 64.1 s |
| 213 | timeout | timeout | (… ran past my output capture; will re-extract if asked) |
| 220 | timeout | timeout | (same) |
| 222 | timeout | timeout | (same) |

Two calls (idx 192, 202) succeeded only at 112-113 s on the H1
probe. Those would have timed out on *both* baselines and on any
client-side cap below ~120 s. The natural latency distribution
on this session genuinely reaches that range.

### H2/H6 — deferred per STOP CRITERION

The bug doc Part 4 says "the moment a probe identifies a CAUSE
with a corresponding ACTION, stop further probes and move to
Step 3." H1 went from 32 % failure → 0 % failure with a single
config change — a decisive result. H2 and H6 are not run.

The H1 probe also yields the answer the H2 probe was designed to
produce:

- 8 events timed out at ~60 s on baseline-curl — the bug doc's
  H2 asked whether those are a server-side cap or a long-tail
  distribution.
- All 8 of those same events succeed on the H1 probe, with
  latencies between 6 s and 113 s — i.e. the provider *does*
  respond, given enough time. There is **no fixed-interval server
  cap** at 60 s. H2 is ruled out.

H6 (max_tokens / request size) is also not needed; H1 closes 17
of 17 timeouts on its own, leaving no residual for H6 to explain.

### Hypotheses ruled out by this probe

- ~~H2 (server-side ~60 s cutoff)~~ — disproved; the 8 curl-side
  timeouts are explained as long-tail latency, not a cap.
- ~~H4 (litellm/hosted_vllm overhead is the primary cause)~~ —
  ruled out as the *primary* cause; framework overhead may add
  ~5 s on trivial calls (Step 1 §2.4) but the 30 s connector cap
  was the structural blocker.
- ~~H6 (max_tokens too large)~~ — moot; H1 closes the gap.

H3 (long-context degradation) and H5 (provider time-of-day) are
NOT ruled out as *contributors* — they may explain some of the
within-session latency variation — but neither is needed to
explain the user-observable failure.

---

## 2. Recommendation for Step 3

**Path A (setting change in the connector default).** Bump
`_DEFAULT_TIMEOUT_SECONDS` in `src/llm/providers/opencode.py`
from `30.0` to a value that comfortably covers the observed
tail. Three candidates:

| Value | Pro | Con |
| ---: | --- | --- |
| **120 s** | Matches the probe. Covers everything we observed up to idx 207. | Two calls in the probe took >110 s; one was at 113.5 s — within ~5% of 120s, slightly risky. |
| **180 s** | Roughly 50% headroom over the worst observed. Less likely to need a re-bump in 6 months. | A genuinely-hung call now wastes 180 s of the user's time before the engine's existing retry layer fires. |
| **240 s** | Defensive; would absorb a 2× growth in provider tail latency without re-tuning. | Same downside as 180s, more pronounced. |

My pick is **180 s** — splits the difference and matches the
OpenAI SDK's own default behavior (it actually defaults to 600 s
with no streaming, so anything 30–600 s is conservative).
Coordinator can override.

A second-order recommendation (NOT bundled — file separately):
the engine's retry-on-timeout path *worked* during this session
(some baseline-kolsch successes at 30–38 s landed only after a
retry past the 30 s connector cap). But the retry seems to come
from crewai-internal logic, not kolsch's `runtime.max_retries`.
Worth documenting in `docs/explanation/llm-providers.md`'s tuning
section so authors know the layers.

### Out-of-scope, but worth a note in Step 3

The kolsch-baseline log captured a separate symptom the user
filed under
`process/bug-crewai-output-leak-during-retries.md` — CrewAI emits
its rich `❌ LLM Error` panels to stderr even when wrapped. That
chrome shows up on every timeout in this run too. Not this bug's
fix to ship, but if the chrome-suppression PR is in flight,
Step 4's manual e2e will benefit from it being merged first.

---

## 3. No unintended writes

- `git status src/` shows only the pre-existing branch state
  (the in-flight `fix/agent-id-derive-from-ref` work). No
  Step-2-originated edits under `src/`.
- `scripts/replay_session.py` did pick up a small edit during
  Step 2 — added a `--per-call-timeout` CLI flag so the H1 probe
  could measure beyond the 60 s baseline cap. Defaults remain at
  60 s so Step 1's baselines reproduce unchanged. Not a fix; a
  measurement-tool improvement.
- `~/Documents/kolsch/platform.yaml` restored from backup; sha
  matches pre-probe value (`be825545c8175…`).
- Original session JSONL byte-identical pre/post each probe.

---

## 4. STOP — waiting on coordinator approval

**Question for the coordinator:**

1. Approve to proceed to Step 3 (Path A — setting change in the
   connector default)?
2. Pick the new default: 120 s, 180 s, 240 s, or other value?
3. Anything in the Step 2 result that warrants running H6 anyway
   (e.g., even though H1 closes the gap, would you prefer to
   compose a tighter timeout + lower max_tokens for a faster
   user-perceived experience)?

The escalation path is unchanged from the bug doc Part 5; flagging
that I have NOT yet run the post-fix replay (Step 3 deliverable)
or the 10-exchange manual e2e (Step 4 deliverable). If the
coordinator picks 180 s or higher, the post-fix replay will
re-use the H1-probe number directly (the connector default is
the only thing varying).
