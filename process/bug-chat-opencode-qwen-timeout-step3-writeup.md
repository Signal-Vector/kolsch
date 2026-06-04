# Step 3 Writeup — qwen-timeout investigation: ship the fix

**Status:** ready for reviewer/coordinator review (STOP gate, before Step 4).
**Bug doc:** `process/bug-chat-opencode-qwen-timeout-investigation.md`
**Step 1 writeup:** `process/bug-chat-opencode-qwen-timeout-step1-writeup.md`
**Step 2 writeup:** `process/bug-chat-opencode-qwen-timeout-step2-writeup.md` (coordinator-approved; path A, default 180 s).

---

## 1. What landed

### 1.1 `src/llm/providers/opencode.py`

`_DEFAULT_TIMEOUT_SECONDS` 30.0 → **180.0**. Inline comment names
the bug doc and the data backing the new value.

```diff
- _DEFAULT_TIMEOUT_SECONDS = 30.0
+ # bug-chat-opencode-qwen-timeout-investigation.md Step 3 — the
+ # original 30s default sat below the observed long-tail of opencode
+ # responses (replay of session ses_abae69a7 showed 12 successful
+ # calls between 30 and 56s, with two reaching 112-113s). Users on
+ # affected models (qwen3.6-plus + others) saw a 32% timeout rate
+ # at 30s; bumping to 180s drops it to ~2% in the same session
+ # (Step 2 H1 probe, Step 3 post-fix replay). Override per-provider
+ # via `llm_providers.<name>.timeout_seconds` in platform.yaml.
+ _DEFAULT_TIMEOUT_SECONDS = 180.0
```

### 1.2 `docs/explanation/llm-providers.md`

New **Part 6 — Tuning** subsection introducing `timeout_seconds`,
explaining when to drop or raise it, and naming the interaction
with crewai's internal retry vs the engine's `runtime.max_retries`.
The pre-existing "Failure Modes" and "Status" sections renumber
to Parts 7 and 8 respectively.

### 1.3 What did NOT land (deliberate)

Per the bug doc Step 3 PATH A instructions:

- **No change to the engine boundary classifier or the retry
  policy.** Those landed in their own tickets; this step is the
  underlying-condition fix.
- **No migration to `src/cli/_migrations/`.** Source-side default
  changes propagate automatically — users without an explicit
  `timeout_seconds` override pick up the new 180 s on next code
  update. Users with an override keep theirs (intentional;
  they explicitly chose). Also, on inspection,
  `src/cli/_migrations/` contains only stale `__pycache__` —
  the migration subsystem doesn't have current `.py` sources, so
  there's no pattern to extend even if a migration were warranted.
- **No regression tests + manual e2e.** Those are Step 4
  deliverables.

---

## 2. Post-fix replay (Step 3 prediction check)

`/tmp/kolsch-replay/ses_abae69a7.step3-postfix.json` — re-ran the
same 53-event replay with the new 180s default in place
(platform.yaml restored to its original form, no override).
Outer per-call cap on the replay tool set to 210 s so we could
measure whether the connector's new 180 s + crewai retries
actually completes calls that needed it.

| Bucket | Baseline-kolsch (30 s default) | Post-fix (180 s default) |
| --- | ---: | ---: |
| 0–5 s success | 20 | 22 |
| 5–10 s success | 9 | 3 |
| 10–30 s success | 2 | 0 |
| 30–60 s success | 5 | 20 |
| 60–120 s success | 0 | **7** |
| 120+ s success | 0 | 0 |
| Timeout at outer cap | 17 | **1** |

**Headline: 16 of the 17 baseline timeouts now succeed. 0
regressions** (no calls that succeeded on baseline have started
failing on the post-fix).

Calls that needed >60 s to complete (i.e. would have been killed
by any cap below 120 s):

| idx | Post-fix latency |
| ---: | ---: |
| 133 | 71.9 s |
| 143 | 72.2 s |
| 192 | 71.2 s |
| 207 | **112.3 s** |
| 213 | 62.5 s |
| (and three more between 60-70s) | |

That 112-second tail is real and reproducible — the H1 probe also
showed two calls at 112-113 s. The 180 s default is the right
number to comfortably absorb that.

### 2.1 The single residual timeout

idx=187 timed out at 210 s on the post-fix replay (hit our outer
per-call cap, meaning the call would have taken even longer if
allowed).

Honest framing of this result:

- This same call (idx=187) **succeeded in 37.0 s on the Step 2 H1
  probe** (which used a 120 s connector + 150 s outer cap). The
  call's payload didn't change between probe and post-fix replay;
  the only thing that varied is the live provider state ~50 min
  apart.
- Conclusion: this is **upstream variance**, not a regression in
  the fix. The provider occasionally responds in 37 s and
  occasionally takes >210 s for the same payload.
- This is exactly the case the **engine's existing retry layer**
  (`runtime.max_retries` on the agent spec) is designed to catch.
  The retry layer is **explicitly out of scope** for this fix
  (bug doc Part 3: "NO change to … the retry policy"); it landed
  in `bug-platform-internal-error-boundary-swallow.md`. In the
  live chat surface, idx=187 would retry on attempt 2 and very
  likely succeed.

Step 4's manual e2e (10 sequential exchanges with ≤1 unrecovered
failure) is the right place to validate this end-to-end. If
that test surfaces persistent retry-exhausted failures, this fix
hasn't actually closed the bug and I'll escalate per the doc's
escalation path. The replay-tool number alone (52/53 = 98 %)
is in line with that bar but isn't proof of it.

### 2.2 Comparison with the Step 2 prediction

Step 2 said: "53/53 success, 0 timeouts."
Step 3 actual: 52/53 success, 1 timeout.

Small mismatch (one call) explained by upstream variance — see
above. The substantive prediction (the 17 baseline timeouts
should largely convert to 30-120 s successes) lands: 16 of 17 do
exactly that.

---

## 3. Scope check

Per the bug doc reviewer prompt for Step 3 ("Diff src/ — the only
edits should be the picked-fix surface + tests + docs"):

```
git diff --stat fix/agent-id-derive-from-ref (since the branch was checked out)
```

Step 3's own diff is two files:

- `src/llm/providers/opencode.py` — the default change + the
  comment.
- `docs/explanation/llm-providers.md` — the new tuning subsection.

Plus `scripts/replay_session.py` — the replay tool itself, which
landed in Step 1 and gained a `--per-call-timeout` flag in
Step 2. Not under `src/`; not in the production code path.

The rest of the branch's working tree (the modified `src/cli/_*`
and `src/engine/*` files visible in `git status`) is **pre-existing
work on this branch** — the in-flight
`fix/agent-id-derive-from-ref` patch this branch was carrying.
None of it was modified during Steps 1-3.

Original session file byte-identical pre/post every replay
(sha256 verified after each run).

---

## 4. STOP — waiting on reviewer/coordinator approval

**Questions for the reviewer:**

1. Diff approval — is the inline comment on
   `_DEFAULT_TIMEOUT_SECONDS = 180.0` appropriate, or would you
   prefer the explanation lives only in the module docstring
   (and the constant is a bare line)?
2. The `docs/explanation/llm-providers.md` Part 6 renumber bumped
   Part 6 (Failure Modes) → Part 7 and Part 7 (Status) → Part 8.
   Any external doc/index that references "Part 6 / Part 7" by
   number is now out-of-sync — flag if there are any.
3. Path A as specified by Step 2's pick is "180 s." If you'd
   prefer 120 / 240 / other now that the post-fix shows the
   actual user-perceived latency distribution (7 calls in the
   60-120 s bucket), happy to change.
4. The single residual timeout — okay to accept as
   upstream-variance + retry-layer territory for Step 4 to
   validate? Or do you want me to investigate idx=187's
   specifics further before Step 4?

If all four are OK, I'll move to Step 4 (regression tests +
manual e2e + closure writeup + handoff to the user).
