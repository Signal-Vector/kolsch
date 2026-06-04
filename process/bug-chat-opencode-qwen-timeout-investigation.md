# Bug — Chat against qwen3.6-plus via opencode keeps timing out; investigate to closure

**Draft 0.1** *(open-ended empirical investigation, not a pre-decided fix. The closure criterion is binary: either a setting change that demonstrably reduces timeout rate below a pinned threshold, OR a documented blocker the user can act on. A new error class with the same user experience does NOT count as fixed.)*

> Reported symptom (persistent, multiple sessions, including
> `ses_abae69a7-713c-48ac-9ec3-004de51ffe8c` re-runs at both
> the original failure timestamp and post-patch
> `2026-06-03T21:03`): chat exchanges against `qwen3.6-plus`
> via the `opencode` provider repeatedly fail with
> `Failed to connect to OpenAI API: Request timed out.`
>
> Two prior fixes have shipped against the *symptom*:
> `bug-platform-internal-error-boundary-swallow.md` (0.3 —
> classify + retry) and `bug-crewai-output-leak-during-retries.md`
> (suppress CrewAI's noisy chrome). They make the failure
> *better-classified* and *quieter*, but they don't make the
> chat usable. The user's literal report: "I still get the same
> error." This investigation is about **the underlying
> condition**, not its presentation.

---

## Part 0 — What "fixed" means

The user defined the bar explicitly:

> "fixed to me is that either there is a clear blocker, or a
> setting we can change to make it work. I don't want just a
> new error."

Two acceptable outcomes; nothing else counts:

| Outcome | What it looks like | Evidence required |
|---|---|---|
| **A — Setting change works** | A documented config diff (in `platform.yaml`, agent YAML, or connector default) that drops the user-observed failure rate from "frequent / blocking" to "rare / absorbed by retries." | Repro showing ≥10 consecutive exchanges complete with ≤1 retry-exhausted failure. |
| **B — Documented blocker** | A specific upstream / network / model condition the user cannot work around in code, with a recommended workaround (different provider, different model, off-hours scheduling, etc.). | Repro showing the same failure persists across every reasonable setting permutation; isolation evidence (e.g., direct curl to the provider) pointing at the responsible layer. |

**Explicitly not "fixed":**
- A better error message.
- More retries that all still fail.
- A new error code with the same outcome.
- Defensive shims that hide the problem.

If the investigation runs out of options without either A or B,
escalate. Don't ship a third symptom-management patch.

---

## Part 1 — Hypotheses (the open list)

Investigation tasks against these. Some are mutually exclusive,
some compose. Update this list as the investigation proceeds —
when one is ruled out, mark it; when a new one is discovered,
add it.

### H1 — Client-side timeout is too aggressive

`_DEFAULT_TIMEOUT_SECONDS = 30.0` in
`src/llm/providers/opencode.py:123` is shorter than
qwen3.6-plus's typical latency for multi-turn tool-calling
exchanges through opencode. The OpenAI SDK's own default is
600s; kolsch's 30s default was likely picked for simple
completions.

- **Test**: bump `platform.yaml` →
  `llm_providers.opencode.timeout_seconds: 120`. Repro.
- **Expected if true**: failures drop substantially.
- **Status**: untested.

### H2 — Server-side / network cutoff at a specific interval

`opencode.ai/zen/go/v1` may have its own request-duration cap
that's tighter than our client timeout. Or the user's network
path has a router / proxy that drops idle connections at a
known interval.

- **Test**: a direct `curl` to opencode (bypassing kolsch +
  litellm + crewai) with the same payload from the failing
  session. Measure time-to-first-byte and time-to-complete.
- **Expected if true**: the curl call also times out at the
  same interval; the limit is upstream of kolsch.
- **Status**: untested.

### H3 — qwen3.6-plus is genuinely slow on long contexts

Multi-step tool-calling exchanges accumulate context. By the
~5th turn the prompt may be long enough that qwen3.6-plus
takes >30s to first token. The failure rate would correlate
with conversation length.

- **Test**: replay the failing session's prompts in order;
  log per-call latency; check if failures cluster at high
  turn counts.
- **Expected if true**: early turns succeed, late turns fail
  monotonically. Solution: limit context per call OR switch
  model.
- **Status**: untested.

### H4 — Routing issue (litellm / hosted_vllm path)

The opencode connector builds `crewai.LLM(model="hosted_vllm/...")`
per the `bug-litellm-model-routing.md` fix. The `hosted_vllm/`
route adds overhead (header transforms, prompt re-formatting)
that could push borderline-latency calls past the timeout.

- **Test**: capture the actual HTTP request litellm sends
  upstream (request body, headers, URL); compare against a
  vanilla OpenAI-compatible request to the same endpoint.
- **Expected if true**: litellm's request differs from a
  baseline request in ways that explain the latency delta.
- **Status**: untested.

### H5 — Provider regional / capacity issue

opencode.ai is an aggregator. The user's failure may correlate
with time-of-day (regional capacity) or a specific
qwen-routing decision. Other models on opencode may not have
the same issue.

- **Test**: repro at different times of day; try other models
  on opencode (qwen3.6-flash, deepseek, etc.).
- **Expected if true**: failure rate varies by time; other
  models succeed cleanly.
- **Status**: untested.

### H6 — Request size: max_tokens or message bloat

`bug-llm-max-tokens-credit-overrun.md` capped max_tokens at
4096. Per-call overhead of negotiating a max_tokens=4096
response on a long context might push latency past 30s even
when the actual completion is short.

- **Test**: drop max_tokens to 1024 in the agent's runtime;
  repro.
- **Expected if true**: failure rate drops with smaller cap.
- **Status**: untested.

### Hypotheses ruled out by prior work

- ~~Boundary mis-classification~~ (`bug-platform-internal-error-boundary-swallow.md` 0.3 — handles classification; assumes the timeout is genuine).
- ~~Output leak being the actual problem~~ (`bug-crewai-output-leak-during-retries.md` — handles presentation; doesn't claim to fix the underlying call).

---

## Part 2 — Replay infrastructure (Step 1's primary deliverable)

The investigation needs a deterministic way to repro the
failure without depending on chat-typing the same prompts.
The session JSONL already contains every model.request
payload — we can extract and re-send.

### 2.1 Session copy convention

Never mutate the original session. Copy first:

```bash
mkdir -p /tmp/kolsch-replay
cp ~/Documents/kolsch/sessions/ses_abae69a7-713c-48ac-9ec3-004de51ffe8c.jsonl \
   /tmp/kolsch-replay/ses_abae69a7.original.jsonl
```

Each replay run gets its own copy with a timestamp + a
short tag for what's being tested:

```
/tmp/kolsch-replay/
  ses_abae69a7.original.jsonl       # never edited
  ses_abae69a7.H1-timeout-120s.jsonl
  ses_abae69a7.H2-curl-isolated.jsonl
  ...
```

### 2.2 What to extract

The session has 49 `model.request` events. For each, the
`data` field carries:

- `messages`: the full message list at that call
- `tools`: the tool schemas
- `model`: `{provider, model, parameters, fallback}`
- `call_id`

Step 1 builds a small Python script that:

1. Reads a session JSONL.
2. For each `model.request`, optionally re-issues the payload
   directly to opencode via the connector's `build_llm`
   pipeline (kolsch surface) AND via raw httpx/curl (isolated
   surface) — Step 1 picks which based on what we want to
   measure.
3. Records per-call: latency to first byte, latency to
   complete, success/timeout/error, retry count.
4. Outputs a summary table.

### 2.3 What NOT to do in the replay

- Don't replay tool execution — the tools mutated workspace
  state in the original session (file_access.denied,
  file.too_large, etc.). Replays only re-issue the LLM
  request; tool dispatch is short-circuited or stubbed.
- Don't write the replay's events back into the original
  session file. The replay's events go to a new file under
  `/tmp/kolsch-replay/`.
- Don't trigger sandbox / authz writes during replay.

---

## Part 3 — Acceptance criteria

The bug is closed when **one** of these holds and is fully
documented:

**A — Setting fix**:
1. A specific config change (file + key + value) is named.
2. Repro: ≥10 sequential exchanges via `kolsch chat` against
   the same provider + model complete without
   `model.retry_exhausted`. Captured JSONL.
3. The change lands as a doc update OR a default change in
   the connector OR a `kolsch setup` migration. The user does
   not need to manually edit YAML to benefit (unless the
   change is opt-in by intent).

**B — Documented blocker**:
1. A specific upstream condition is identified with isolation
   evidence (curl call to the provider showing the same
   failure).
2. A workaround is named (different provider, different
   model, different timing, etc.) AND verified in repro.
3. The blocker is documented in `docs/explanation/llm-providers.md`
   under a "Known issues" section so users hit the same wall
   find the workaround.

**Both A and B require:**
- Pre-fix baseline: a captured count of failures over N
  exchanges with the current config.
- Post-fix verification: the same count under the new
  config / model / provider showing measurable improvement
  OR clean failure with workaround documented.

### Explicitly out of scope

- Re-architecting the LLM driver layer (Phase 1.3 fallback
  + Phase 4 future work).
- Implementing a streaming response surface to reduce
  perceived latency.
- Building a model-quality benchmark or replacing
  qwen3.6-plus globally.
- Switching kolsch's default provider away from opencode.
  (Workaround for an individual user is fine; default change
  is its own phase decision.)
- Adding a retry-with-backoff strategy beyond what
  `runtime.max_retries` already provides.

---

## Part 4 — Step dependency graph

```
Step 1 (replay infrastructure + baseline)  ←── STOP gate
   │
   ▼
Step 2 (hypothesis probes: H1-H6)
   │
   ▼
Step 3 (apply the winning fix or document the blocker)
   │
   ▼
Step 4 (regression test + handoff)
```

| Step | Ships | Reviewable in |
|---|---|---|
| **1** | Replay tool + baseline measurement | ~40 min |
| **2** | Per-hypothesis probe results | ~60-90 min (depends on how many H ruled out) |
| **3** | Either the fix or the blocker writeup | ~30 min |
| **4** | Regression test + handoff | ~20 min |

Step 2 is the bulk of the work. The dev MUST NOT skip to
Step 3 without testing each hypothesis (or explicitly ruling
out testability — e.g., "H5 requires waiting 12+ hours to
sample two times of day; deferred pending coordinator
approval").

---

## Step 1 — Build the replay tool + capture baseline

### Developer prompt

```
[standing instructions]

Step 1 — Build the replay tool and measure the current
failure rate. No fix attempts yet; this is observation.

Read first:
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/bug-chat-opencode-qwen-timeout-investigation.md Parts 0-3.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/llm/providers/opencode.py (connector).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/engine/_crewai_adapter.py:47-110 (the build_llm-wrapping callable).
- The trigger session at
  /Users/andrecohen/Documents/kolsch/sessions/ses_abae69a7-713c-48ac-9ec3-004de51ffe8c.jsonl
  (READ ONLY — never write).

Tasks:

1. Session-copy convention.
   - mkdir -p /tmp/kolsch-replay
   - Copy the trigger session to /tmp/kolsch-replay/ses_abae69a7.original.jsonl.
   - Every subsequent test gets its own copy under
     /tmp/kolsch-replay/ses_abae69a7.<TAG>.jsonl where TAG
     names the variant (e.g., H1-timeout-120s).

2. Replay tool.
   Build scripts/replay_session.py with the following CLI:

     scripts/replay_session.py --session <path> --mode {kolsch,curl} \
       --target <which-model.request-events> --output <results-json>

   Behavior:
   - --session: path to a session JSONL.
   - --mode:
     * kolsch: re-issue the payload through the same code
       path the chat uses — call the engine's
       make_build_llm_factory → connector → crewai.LLM
       pipeline. Use the connector under test (opencode);
       measure end-to-end latency.
     * curl: skip kolsch entirely. Take the model.request
       payload, transform to OpenAI-compatible JSON
       (matching hosted_vllm/qwen3.6-plus shape), POST
       directly to https://opencode.ai/zen/go/v1/chat/completions
       via httpx. Measure raw provider latency.
   - --target: which model.request events to replay. Options:
     * all: every model.request in the session.
     * first-N: the first N events.
     * last-N: the last N (capturing the failure tail).
     * indexes 0,3,7: explicit indices.
   - --output: path to a JSON file recording per-call:
     * call_id (from the original event)
     * event_index in the session
     * mode (kolsch / curl)
     * latency_ms (None on timeout)
     * outcome: success | timeout | other_error
     * error_message (truncated to 500 chars)
     * timestamp at call start

   Implementation notes:
   - The script does NOT touch the original session file.
   - The script does NOT execute tool calls. When a
     model.request payload contains tools, replay only the
     LLM call; ignore the LLM's tool_use response (record
     the bytes returned, but don't dispatch).
   - Run replays sequentially (not concurrently) — concurrent
     calls might mask rate limits.
   - Use a 60-second timeout PER CALL for measurement (longer
     than the current 30s; we want to capture how long
     "timeout" calls actually take).

3. Baseline measurement.
   Run:
     python scripts/replay_session.py \
       --session /tmp/kolsch-replay/ses_abae69a7.original.jsonl \
       --mode kolsch \
       --target all \
       --output /tmp/kolsch-replay/baseline-kolsch.json

   Capture:
   - Total calls.
   - Success count.
   - Timeout count + the latency distribution of timeouts
     (do they all hit ~30s, suggesting a hard limit? or
     scattered, suggesting upstream variation?).
   - Other-error count + error breakdown.

   Then run the same with --mode curl:
     ... --mode curl --output /tmp/kolsch-replay/baseline-curl.json

   Capture the same metrics for the curl path. This isolates
   "kolsch + crewai + litellm overhead" from "raw provider
   behavior."

4. Diff the two baselines.
   - If kolsch fails at a rate substantially higher than
     curl: the overhead in kolsch's path is meaningful.
     Hypothesis tree narrows toward H4 (routing) + H6
     (request shape).
   - If kolsch and curl fail at similar rates: the issue is
     upstream of kolsch. Hypothesis tree narrows toward H2
     (server cutoff) + H5 (provider capacity).
   - Either way, Step 2 has a starting hypothesis to probe.

5. Writeup deliverable.
   Produce process/bug-chat-opencode-qwen-timeout-step1-writeup.md
   containing:
   - The two baseline result tables.
   - Latency distribution of timeouts (histogram or sorted
     list).
   - Initial hypothesis-tree narrowing call.
   - Replay tool invocation reference for Step 2.

6. STOP. No fixes, no probes beyond the two baselines. Post
   the writeup and wait for coordinator approval before
   Step 2.

ESCALATION PATH: if the replay tool cannot be built within
~3 hours (e.g., the build_llm path is hard to invoke outside
the chat context, or curl-side authentication is non-trivial),
escalate. Don't ship a partial tool that runs unreliably.

Acceptance: replay tool runs cleanly on both modes; the two
baselines are captured; the writeup names a starting
hypothesis with evidence.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1.

NO production code (under src/). The deliverables are
scripts/replay_session.py + the writeup + the baseline data.

Checks:

1. scripts/replay_session.py works on both --mode kolsch and
   --mode curl. Re-run the dev's exact invocations and
   confirm the output JSON has the same shape.

2. The script does NOT mutate the original session. Verify
   the original SHA-256 before and after the dev's run.

3. The script does NOT dispatch tools or write to the user's
   workspace. Grep for any subsystem.invoke_tool or path-
   writing calls.

4. Two baselines exist:
   /tmp/kolsch-replay/baseline-kolsch.json
   /tmp/kolsch-replay/baseline-curl.json
   Both have all 49 calls (or whatever the session has).
   Both have non-trivial outcome distributions (if curl is
   100% success, that's suspicious — verify the auth /
   payload shape isn't lying about success).

5. The writeup names a starting hypothesis with evidence.
   "Probably H1" with no data is a fail. The hypothesis must
   be backed by a specific number from the baselines.

6. Timeout-latency distribution is present and interpretable.
   If all timeouts cluster at ~30.0s ± 0.5s → strong signal
   for H1 (client timeout). If scattered → not H1.

7. No fixes have been applied. git status under src/ should
   show no edits.

Verdict: APPROVE-TO-PROCEED or REVISE.
```

---

## Step 2 — Hypothesis probes

### Developer prompt

```
[standing instructions]

Step 2 — Test the hypotheses in order. Stop the moment one is
demonstrably the cause AND a setting / workaround is in hand.

Read first:
- Step 1's writeup (coordinator-approved).
- The hypothesis list in
  process/bug-chat-opencode-qwen-timeout-investigation.md Part 1.

For each hypothesis you test, produce:

- A short writeup (1-2 paragraphs) in the Step 2 PR
  description naming the probe + the result.
- A replay artefact: a new
  /tmp/kolsch-replay/ses_abae69a7.<TAG>.json result file
  capturing the probe's run.

The order below is the recommended priority based on
"cheapest to test" first. Adjust based on Step 1's narrowing.

PROBE H1 — Client-side timeout.
- Test: temporarily edit
  ~/Documents/kolsch/platform.yaml under
  llm_providers.opencode add `timeout_seconds: 120`.
- Run the replay with --mode kolsch, output to
  /tmp/kolsch-replay/ses_abae69a7.H1-timeout-120s.json.
- Compare timeout rate vs. baseline-kolsch.
- If rate drops dramatically AND the latency distribution
  shows successful calls completing in 30-120s window → H1 is
  the cause. Move to Step 3 with H1 as the picked solution.
- If rate barely changes → H1 is not the cause (or not the
  only cause); proceed to H2.

PROBE H2 — Server-side cutoff.
- Test: run --mode curl with the timeout-120s session config.
  If curl ALSO fails at ~30s consistently (with no client
  timeout), it's an upstream cap.
- Test: try a direct curl to opencode with an artificially
  short prompt (e.g., "say hi"). Measure typical latency.
  If a trivial prompt also takes >5s consistently, the
  provider has chronic latency, not a bug per se.
- Outcomes:
  * Curl times out at fixed interval too → upstream cap.
    Move to Step 3 with H2 as documented blocker.
  * Curl succeeds reliably even when kolsch fails → not H2.

PROBE H3 — qwen3.6-plus on long contexts.
- Test: replay only the first 5 calls (--target first-5) vs
  the last 5 calls (--target last-5). Compare.
- If early calls succeed and late calls fail monotonically →
  H3 is the cause. Step 3 considers context-trimming or
  model swap.
- If failures are scattered → not H3.

PROBE H4 — litellm / hosted_vllm routing overhead.
- Test: log litellm's actual outbound request. Set
  LITELLM_LOG=DEBUG; capture stdout from a failing call.
  Compare the request body to what curl --mode sends.
- If litellm adds substantive overhead (large extra headers,
  re-formatted body, unusual URL path), document.
- Outcome: this is investigative; the fix would be a
  litellm-config change or a routing change. Probably not the
  primary cause.

PROBE H5 — Provider / time-of-day.
- Test: try another model on opencode (qwen3.6-flash or
  deepseek-v3 or whatever's available). Run --mode kolsch
  replay. Compare timeout rate.
- If other models succeed cleanly → it's a qwen3.6-plus
  specific issue with opencode's routing.
- Test: schedule the replay to run at 3 different times
  (now, 4 hours later, 8 hours later). Compare.
- If rate varies dramatically by time → capacity issue.
- For closure, "use qwen3.6-flash" might be a clean
  workaround.

PROBE H6 — Request size.
- Test: edit the agent's runtime block to set
  max_tokens: 1024. Replay --mode kolsch.
- If timeout rate drops → request-shape contributes. Step 3
  considers reducing the default.

STOP CRITERION:
- The moment a probe identifies a CAUSE with a corresponding
  ACTION (setting change or workaround), stop further
  probes and move to Step 3.
- If two probes both reduce failures (e.g., H1 + H6), test
  the combination and pick the smallest user-facing change.
- If all six probes run without finding a cause, escalate.
  Don't ship "we tried things and they all kinda helped."

DELIVERABLE:
- process/bug-chat-opencode-qwen-timeout-step2-writeup.md
  with: per-probe result, the picked-cause hypothesis (or
  the escalation if none), and the proposed Step 3 fix.

ESCALATION PATH:
- If all six probes complete without isolating a fix or a
  blocker, escalate.
- If a probe requires waiting (H5 time-of-day), document and
  decide whether to wait or escalate.
- If the kolsch vs curl baselines diverge in a way that
  suggests a kolsch-specific bug (e.g., litellm sending
  malformed requests), that becomes a separate bug doc, NOT
  a fix folded into this one.

Acceptance: writeup names the picked cause + the proposed
action; the action's hypothesis is backed by a replay
artefact showing measurable improvement OR isolation
evidence.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2.

Checks:

1. Each hypothesis the dev tested has:
   - A short writeup naming the probe.
   - A replay artefact file at /tmp/kolsch-replay/.
   - Numbers (failure counts, latency distributions), not
     vibes.
   If "I tried H1 and it didn't seem to work" is the level of
   detail, fail.

2. If H1 was tested: the platform.yaml diff was reverted
   after the probe (the original config wasn't permanently
   modified during testing). Verify the dev didn't leave
   timeout_seconds: 120 in the user's actual config — that
   change belongs to Step 3 if H1 wins.

3. The picked-cause hypothesis is supported by the data.
   E.g.: "H1 is the cause" requires the replay artefact to
   show timeout rate dropping from X% to ≤Y% with the
   timeout bump.

4. Stop criterion respected: if the dev tested all 6 without
   finding a cause, they escalated (didn't keep poking).

5. No probes outside the H1-H6 list without justification.
   Inventing H7-H10 mid-step without coordinator input is a
   fail.

6. Probe artefacts are reproducible: re-run one of them and
   confirm the outcome.

7. No fixes have landed in src/ yet. Step 2 is observation +
   diagnosis. Edits to platform.yaml inside the workspace
   are fine if reverted; edits to src/ are a fail.

Verdict: APPROVE-TO-PROCEED or REVISE.
```

---

## Step 3 — Apply the fix or document the blocker

### Developer prompt

```
[standing instructions]

Step 3 — Land the picked-cause solution.

Read first:
- Step 2's writeup naming the cause + action.

Branch based on the picked cause:

PATH A — Setting change works.
1. Implement the change in the smallest surface that benefits
   all users:
   - If it's a connector default (e.g., timeout 30 → 120):
     edit src/llm/providers/opencode.py
     (_DEFAULT_TIMEOUT_SECONDS).
   - If it's a per-agent runtime knob:
     update the agent schema OR document the override in
     docs/how-to/.
   - If it's a platform-defaults block update: edit
     src/cli/_starter_bundle/kolsch/bundle.yaml OR the
     setup migration framework so existing users pick it up
     on re-setup.
2. Document the change in docs/explanation/llm-providers.md
   under a "tuning" subsection.
3. If the change affects existing user workspaces, add a
   migration to src/cli/_migrations/ that bumps the relevant
   key on `kolsch setup` re-run. Match the pattern from
   qol-setup-evolution Step 6.
4. NO change to the engine boundary classifier or the
   retry policy. Those landed in their own tickets; this
   step is the underlying-condition fix.

PATH B — Documented blocker.
1. Add a "Known issues" subsection to
   docs/explanation/llm-providers.md naming:
   - The exact failure condition (provider + model + symptom).
   - The isolation evidence (curl shows the same failure).
   - The recommended workaround (different provider, different
     model, timing, etc.).
2. If a workaround is a different model on opencode that
   works cleanly: add a model alias in
   src/cli/_starter_bundle/kolsch/bundle.yaml's defaults OR
   update the setup flow to recommend that model first.
3. If the workaround is "use openrouter instead": document
   how to switch providers, but DON'T change the kolsch
   default — that's a separate phase decision.

BOTH PATHS:
- Tests: a small unit test pinning the changed value (e.g.,
  `assert OpenCodeConnector.DEFAULT_TIMEOUT_SECONDS == 120`)
  AND a docs lint to ensure the "Known issues" subsection
  exists (if PATH B).
- Run the replay one final time with the post-fix config to
  confirm the result matches what Step 2 predicted.
- NO bundling of unrelated changes. If the dev finds a
  second papercut during this work, file a new bug doc.

Scope: PATH A ~30 LOC + ~20 LOC tests + ~30 LOC docs.
       PATH B ~50 LOC docs + maybe an alias add.

ESCALATION PATH:
- If implementing the picked fix touches more surface than
  expected (>~100 LOC), escalate to confirm scope.
- If the fix's manual repro doesn't reproduce Step 2's
  improvement, the cause wasn't actually isolated — return
  to Step 2 with a note about what changed.

Acceptance: post-fix config is in place; the replay artefact
under /tmp/kolsch-replay/ shows the improvement;
docs/explanation/llm-providers.md is updated.
```

### Reviewer prompt

```
[standing instructions]

Review Step 3.

Checks:

1. The picked PATH (A or B) is the one Step 2 named. If the
   dev silently switched paths between steps, fail.

2. PATH A: the change is in the smallest surface that
   benefits all users (not a one-line patch to platform.yaml
   that only fixes the reporter's workspace).

3. PATH B: the "Known issues" subsection exists in
   docs/explanation/llm-providers.md with all three parts
   (condition, isolation evidence, workaround).

4. The post-fix replay artefact is captured AND shows the
   improvement Step 2 predicted. If Step 2 said "H1 reduces
   timeout rate from 80% to 10%," Step 3's artefact should
   show ~10%, not "I think it's better."

5. No bundled changes. Diff src/ — the only edits should be
   the picked-fix surface + tests + docs. Anything else
   needs justification or its own bug doc.

6. The retry / boundary-classifier work from the other
   tickets is unchanged. This step is the underlying-condition
   fix, not a re-tweak of the symptom-management layers.

7. Migration (if applicable): existing user workspaces pick
   up the new default on `kolsch setup` re-run. Test by
   running setup against a fixture workspace.

Verdict: PASS or FAIL.
```

---

## Step 4 — Regression test + handoff

### Developer prompt

```
[standing instructions]

Step 4 — Lock the fix in with tests + manual e2e + handoff
to the user.

Read first:
- Step 3's PR.

Tasks:

1. Regression tests in
   tests/integration/test_opencode_timeout_default.py (PATH A)
   OR
   tests/unit/test_llm_providers_doc.py (PATH B):

   PATH A:
   - Test: the opencode connector's default timeout matches
     the new value.
   - Test: a chat-style invocation against a fake provider
     that delays by N seconds completes when N < new default
     and times out when N > new default. Use httpx
     MockTransport.

   PATH B:
   - Test: the docs/explanation/llm-providers.md file
     contains the "Known issues" header and the workaround
     name.

2. Add the TCs to process/test-catalog.md under a new
   "Opencode Timeout (OPENCODE-TIMEOUT)" section.

3. Manual e2e — the closure proof for the user:
   a. With the post-fix config (PATH A) OR the
      workaround applied (PATH B), run a real `kolsch chat`
      session — at least 10 sequential exchanges with
      multi-step tool work.
   b. Verify: at most 1 of the 10 exchanges fails with
      model.retry_exhausted (matching the
      bug-platform-internal-error-boundary-swallow.md
      surface; the underlying timeout, if it happens, is
      retried transparently).
   c. Capture the session JSONL + the
      exchange_started/_completed/_failed counts in the PR
      description.
   d. If the chat is still painful — the fix didn't actually
      close the bug. Don't declare closure; escalate.

4. Final writeup in
   process/bug-chat-opencode-qwen-timeout-step4-closure.md:
   - Restate the original symptom.
   - Name the picked cause + action.
   - Show the before/after numbers from the replay artefacts.
   - Show the manual chat-session result.
   - Name any follow-ups (e.g., "qwen3.6-flash is a faster
     alternative; documented in how-to-find-models").

5. Hand off to the user. The PR description must explicitly
   ask the user to confirm via their own chat session that
   the bug is fixed. The reviewer's check #6 below is the
   gate before closure.

Scope: ~80 LOC tests + writeup + manual report.

ESCALATION PATH: if the manual e2e shows the chat is still
painful, do NOT declare closure. Return to Step 2 with the
new data point. The bar (Part 0) is binary: chat is usable,
or a blocker is documented + worked around — not "we tried."

Acceptance: tests pass; manual e2e shows ≥10 exchanges with
≤1 failure; closure writeup posted; user is asked to confirm.
```

### Reviewer prompt

```
[standing instructions]

Review Step 4.

Checks:

1. Regression tests exist and pass for the picked PATH.

2. process/test-catalog.md gained the OPENCODE-TIMEOUT
   entries.

3. Manual e2e: PR description has the captured session
   JSONL counts. The ratio of failures should be ≤1/10.
   If it's higher, the bug is NOT closed — fail.

4. The closure writeup explicitly references Part 0's bar
   ("setting that works" OR "documented blocker") and shows
   which one was hit with evidence.

5. The user-confirmation request is in the PR description.
   The reviewer SHOULD NOT mark this PR PASS until the user
   confirms via a real chat session. If the user has not
   yet confirmed, the verdict is REVISE-PENDING-USER, not
   PASS.

6. NO follow-up patches snuck in. If the dev decided to
   "also fix" something else, that's its own PR.

7. Full test suite green. tc_eng_* goldens still byte-equal.

Verdict: PASS (only after user confirms), REVISE, or FAIL.
```

---

## Part 5 — How to escalate

```markdown
### Escalation E-BUG-QWENTIMEOUT-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–4
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:** 2–3 paths.
- **Estimated cost:**

---

### Coordinator resolution — E-BUG-QWENTIMEOUT-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Step 1's replay tool can't be built in a reasonable
  window.
- Step 1's baseline shows kolsch and curl fail at the same
  rate (eliminates kolsch-side causes; H2/H5 are the only
  viable remaining and require provider engagement).
- Step 2 completes all 6 probes without isolating a cause.
- Step 2 finds a kolsch-specific bug (e.g., litellm
  malforming requests) — that's a separate doc.
- Step 3's post-fix repro doesn't match Step 2's prediction.
- Step 4's manual e2e shows the chat is still painful.

When NOT to escalate:
- A single probe runs slower than expected. Just wait.
- Probe results disagree with the Part 1 hypothesis ranking.
  That's the point of the investigation — update Part 1's
  status notes and proceed with the data.
- Wording of the docs/explanation/llm-providers.md "Known
  issues" subsection. Use editor judgment.

---

## Part 6 — Coordinator Escalation Log

*(empty — populate as escalations land)*

---

## Part 7 — Out of scope (consolidated)

- A new error class. The user explicitly said no.
- Re-architecting the LLM driver layer.
- Replacing qwen3.6-plus globally.
- Streaming responses for perceived-latency improvement.
- Switching default provider away from opencode.
- Adding retry backoff strategies.
- Anything that doesn't either fix the user's chat OR
  document a clear blocker with a workaround.

---

## Part 8 — Related work

- **Symptom-management layer 1**:
  `bug-platform-internal-error-boundary-swallow.md` Draft 0.3
  — classifies + retries. Necessary, not sufficient.
- **Symptom-management layer 2**:
  `bug-crewai-output-leak-during-retries.md` — suppresses
  CrewAI's chrome during retries. Necessary, not sufficient.
- **This doc**: the underlying-condition layer. Closes the
  loop opened by the two above.
- **Related provider configuration**:
  `bug-litellm-model-routing.md` (the hosted_vllm/ route
  choice) is a possible contributor to H4 if litellm's
  routing adds overhead.
- **Related credit / quota work**:
  `bug-llm-max-tokens-credit-overrun.md` (max_tokens=4096
  default) intersects with H6 if request shape contributes.
- **Trigger session**:
  `~/Documents/kolsch/sessions/ses_abae69a7-713c-48ac-9ec3-004de51ffe8c.jsonl`
  — the empirical reference. The replay tool reads from this
  (copies, never mutates).
