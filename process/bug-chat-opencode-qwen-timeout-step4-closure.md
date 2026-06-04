# Step 4 Closure — qwen-timeout investigation: handoff to user

**Status:** ready for user confirmation. PR-ready summary below.
**Bug doc:** `process/bug-chat-opencode-qwen-timeout-investigation.md`
**Investigation writeups:**
- Step 1 (replay tool + baselines): `process/bug-chat-opencode-qwen-timeout-step1-writeup.md`
- Step 2 (hypothesis probes): `process/bug-chat-opencode-qwen-timeout-step2-writeup.md`
- Step 3 (fix): `process/bug-chat-opencode-qwen-timeout-step3-writeup.md`

---

## 1. Original symptom

> Reported, persistent across multiple sessions
> (e.g. `ses_abae69a7-…` at both 14:50 and 21:03 timestamps):
> chat exchanges against `qwen3.6-plus` via the `opencode`
> provider repeatedly fail with `Failed to connect to OpenAI API:
> Request timed out.`
>
> Two prior symptom-management fixes had shipped (boundary
> classification + retry; CrewAI chrome suppression). The user's
> literal report: "I still get the same error." This investigation
> was about the underlying condition, not its presentation.

The user's bar for "fixed" (bug doc Part 0):

> "fixed to me is that either there is a clear blocker, or a
> setting we can change to make it work. I don't want just a
> new error."

## 2. Picked cause and action

**Cause (H1):** the opencode connector's default client timeout of
**30 seconds** (`src/llm/providers/opencode.py:131`) sat below
the provider's natural long-tail latency. The replay tool showed
twelve calls in the user's failing session succeed in the 30-56 s
band and two reach 112-113 s — every one of those a kolsch-side
timeout under the 30 s default.

**Action:** bumped `_DEFAULT_TIMEOUT_SECONDS` to **180 s**. The
override path (`platform.yaml: llm_providers.<name>.timeout_seconds`)
is untouched; users who explicitly tuned the value keep their
tuning.

This is **Path A** in the bug doc's taxonomy: setting change works.
Path B (documented blocker) was not needed.

## 3. Before / after numbers

Replay tool: `scripts/replay_session.py`, sequential replay of all
53 `model.request` events in
`/tmp/kolsch-replay/ses_abae69a7.original.jsonl` (a read-only copy
of the user's failing session; sha256 verified before/after each
run).

| Run | Connector timeout | Success | Timeout | Notes |
| --- | ---: | ---: | ---: | --- |
| baseline-kolsch | 30 s (old default) | 36 / 53 | **17 / 53 (32 %)** | every timeout hit our 60 s outer cap |
| baseline-curl | n/a (raw HTTP) | 45 / 53 | 8 / 53 (15 %) | 12 calls in the 30-56 s band would be killed by any cap <30 s |
| Step 2 H1 probe | 120 s (via platform.yaml) | 53 / 53 | 0 / 53 | with outer cap 150 s |
| **Step 3 post-fix** | **180 s (new default)** | **52 / 53** | **1 / 53 (2 %)** | with outer cap 210 s |

The single residual timeout (idx=187) succeeded in 37 s on the
H1 probe with identical payload — upstream variance, not a defect
in the fix. The engine's existing retry layer
(`bug-platform-internal-error-boundary-swallow.md`,
`runtime.max_retries`) is designed to absorb this single-call
variance in the live chat.

Of the 17 calls that previously timed out under the 30 s default:

- **16 succeed under the 180 s default.** Latencies span 6 s to
  112 s.
- **7 of those 16 succeed in the 60-120 s band** — calls the
  provider genuinely needs >60 s for. Any cap below 120 s would
  kill them.
- The 17th (idx=187) timed out in this particular replay; same
  call succeeded on the Step 2 probe.

Zero regressions: no call that succeeded on baseline started
failing on the post-fix.

## 4. Manual e2e — closure proof

Per the bug doc Step 4 acceptance ("≥10 exchanges with ≤1
failure"):

- Agent: `andre/agents/butler` (the same agent the user's failing
  session used).
- Provider/model: `opencode / qwen3.6-plus` (the failing combo).
- Session: `ses_fa4ee2f9-47db-4db5-8e8f-036975c161e7` (full JSONL
  captured at
  `~/Documents/kolsch/sessions/ses_fa4ee2f9-…jsonl`).
- 10 sequential follow-up exchanges chained against an initial
  exchange (= 11 exchanges total), with prompts that exercise
  multi-step tool work (list_files, bash with find/ls/grep/cat).

Event counts:

| Event | Count |
| --- | ---: |
| `thread.exchange_started` | 11 |
| `thread.exchange_completed` | **11** |
| `thread.exchange_failed` | **0** |
| `model.request` | 24 |
| `model.response` | 24 |
| `model.retry_exhausted` | **0** |
| `platform.internal_error` | **0** |
| `model.upstream_error` | **0** |
| `tool.called` | 13 |
| `tool.result` | 10 |
| `tool.failed` | 3 |

The 3 `tool.failed` events are tool-level failures (the model gave
invalid args or hit authorization rules) — unrelated to this bug;
the model recovered and continued the exchange in every case.

Per-exchange wall-clock: 6, 14, 10, 12, 12, 24, 26, 10, 9, 9
seconds (the first exchange from the smoke test that opened the
session was 6 s too). No exchange came close to the new 180 s
ceiling. The 24-26 s exchanges contained multi-turn tool flows
where the model emitted multiple tool calls — those are the
shape of exchanges that previously timed out under the 30 s cap
and now don't.

## 5. What changed (PR-ready diff summary)

```
 docs/explanation/llm-providers.md                  | +61 / -3
 process/bug-chat-opencode-qwen-timeout-step1-writeup.md  | (new)
 process/bug-chat-opencode-qwen-timeout-step2-writeup.md  | (new)
 process/bug-chat-opencode-qwen-timeout-step3-writeup.md  | (new)
 process/bug-chat-opencode-qwen-timeout-step4-closure.md  | (new)
 process/test-catalog.md                            | +~80 / -0
 scripts/replay_session.py                          | (new, ~470 LOC)
 src/llm/providers/opencode.py                      | +10 / -2
 tests/integration/test_opencode_timeout_default.py | (new, 5 TCs)
```

The actual user-facing code change is 8 net lines in
`src/llm/providers/opencode.py` (one constant + an inline
comment naming this bug). Everything else is investigation
infrastructure (replay tool), tests, and docs.

## 6. Test status

- `tests/integration/test_opencode_timeout_default.py` — 5/5 pass
  in 1.3 s. TC-OPENCODE-TIMEOUT-01..05 added to
  `process/test-catalog.md` under Part 9.5.
- Full LLM surface (component + integration LLM tests, 66 tests
  in scope): all pass.

## 7. Follow-ups (out of scope; file separately if desired)

- The CrewAI `❌ LLM Error` panel chrome still leaks to stderr
  during the brief moments when a litellm-internal retry fires.
  Tracked separately in
  `process/bug-crewai-output-leak-during-retries.md`. Not made
  worse by this fix; in fact made less visible (because fewer
  retries fire under the new default).
- `qwen3.6-flash` exists as a faster alternative on opencode. If
  the long-tail latency on qwen3.6-plus continues to be painful
  for individual users despite the 180 s default, switching the
  model alias is a documented option in
  `docs/explanation/llm-providers.md` Part 6.1 ("when to drop
  the timeout").
- `src/cli/_migrations/` has only stale `__pycache__` — no `.py`
  sources. Either the subsystem is being rebuilt or it should be
  cleaned up. Not blocking this fix (the default change
  propagates without a migration) but worth a separate ticket.

## 8. **User confirmation requested**

Per the bug doc Part 0, this bug is closed by **the user's own
confirmation in a real chat session**. The PR will not be marked
PASS until that confirmation lands.

Please run a real `kolsch chat andre/agents/butler` session (≥10
exchanges, the kind of multi-turn exploratory chat the failing
session was) against opencode/qwen3.6-plus, and report whether:

- Most exchanges complete cleanly (matching the e2e result above).
- Any persistent failures (≥2 in 10) show
  `model.retry_exhausted` or `Failed to connect to OpenAI API:
  Request timed out.` — in which case the fix didn't actually
  close it and I'll escalate per bug doc Part 5.

If the chat feels qualitatively better but you observe occasional
hiccups, that's expected — the engine's retry layer should
recover, and any residual 1-in-10 should land cleanly in the next
exchange.

---

## Appendix A — Replay-tool reproduction

```bash
# baseline (old behaviour — requires checking out pre-fix code)
.venv/bin/python scripts/replay_session.py \
  --session /tmp/kolsch-replay/ses_abae69a7.original.jsonl \
  --mode kolsch --target all \
  --output /tmp/kolsch-replay/baseline-kolsch.json

# post-fix
.venv/bin/python scripts/replay_session.py \
  --session /tmp/kolsch-replay/ses_abae69a7.original.jsonl \
  --mode kolsch --target all \
  --per-call-timeout 210 \
  --output /tmp/kolsch-replay/step3-postfix.json

# raw provider (no kolsch)
.venv/bin/python scripts/replay_session.py \
  --session /tmp/kolsch-replay/ses_abae69a7.original.jsonl \
  --mode curl --target all \
  --output /tmp/kolsch-replay/baseline-curl.json
```

The replay tool is read-only against the session file (sha256
verified) and does not dispatch tool calls — only the LLM call
is re-issued.

## Appendix B — Artefact paths

```
/tmp/kolsch-replay/
  ses_abae69a7.original.jsonl              # READ-ONLY copy of trigger session
  baseline-kolsch.json                     # 53-call baseline, old default
  baseline-curl.json                       # 53-call baseline, raw HTTP
  ses_abae69a7.H1-timeout-120s.json        # Step 2 H1 probe
  ses_abae69a7.step3-postfix.json          # Step 3 post-fix replay
  platform.yaml.backup                     # backup taken before H1 probe
  e2e-{1..10}.{out,err}                    # Step 4 per-exchange stdout/stderr
  e2e-summary.log                          # Step 4 timing summary
  run_e2e.sh                               # the 10-exchange driver script
```
