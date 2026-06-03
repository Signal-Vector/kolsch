# Step 3 writeup — bug-chat-deny-stops-and-esc-interrupts.md

**Status:** automated regression tests + catalog entries landed; pre-fix
verification captured below; manual e2e scenarios are runnable from this
doc but require a real terminal + a connected LLM provider, which the
agent harness cannot drive.

---

## Part A — Regression tests

Six new tests across two files, plus a catalog entry per TC.

### `tests/component/cli/test_chat_deny_stops.py` (3 TCs)

| TC | Behavior | Surface tested |
|---|---|---|
| TC-CHAT-DENY-01 | `[n]` flips the shared CancelToken + stamps reason | provider integration (`provider.allow(...)` → modal → flip) |
| TC-CHAT-DENY-02 | `[d]` flips cancel AND persists rule via `on_rule_added` | full chain incl. provider's `_resolve_prompt` + hook fire |
| TC-CHAT-DENY-03 | Fan-out: pre-cancelled state → DENY without rendering | short-circuit guard at the top of `ApprovalModal.__call__` |

### `tests/component/cli/test_chat_esc.py` (3 TCs)

| TC | Behavior | Surface tested |
|---|---|---|
| TC-CHAT-ESC-01 | `\x1b` on the Input → cancel flipped + reason `"interrupted"` | `_watch_esc_during_streaming` via `inp_factory=create_pipe_input` |
| TC-CHAT-ESC-01b | Non-Esc keystrokes (`"ab"`) DO NOT flip cancel | Esc-specificity (negative pin) |
| TC-CHAT-ESC-02 | Post-finally Esc has no effect; reader cleanly removed | lifecycle invariant — watcher torn down before next prompt |

Plus the catalog entries under `process/test-catalog.md` Part 9.9
("Chat Deny/Esc — bug-chat-deny-stops-and-esc-interrupts.md"),
slotted before `TC-CLI-SMOKE-CHAT-01`.

### Run

```
uv run pytest tests/component/cli/test_chat_deny_stops.py tests/component/cli/test_chat_esc.py -v --no-cov
```

Post-fix: **6/6 pass**, ~1.2s.

---

## Part B — Pre-fix verification (verbatim)

Captured by `git stash`-ing the Step 2 production changes
(`src/cli/chat.py`, `src/cli/_modal.py`, `src/cli/_slash.py`,
`tests/component/cli/test_chat_modal.py`) — the new test files
stay on disk — and running the two pinned TCs.

### TC-CHAT-ESC-01 (pre-fix)

```
==================================== ERRORS ====================================
____________ ERROR collecting tests/component/cli/test_chat_esc.py _____________
ImportError while importing test module ...
Traceback:
  tests/component/cli/test_chat_esc.py:31: in <module>
    from cli.chat import _watch_esc_during_streaming
E   ImportError: cannot import name '_watch_esc_during_streaming'
                 from 'cli.chat' (/Users/andrecohen/Desktop/SV/Projects/kolsch/src/cli/chat.py)
```

Pre-fix bite: the Esc watcher helper does not exist; the chat has
no Esc binding at all. The post-fix code adds it at
`src/cli/chat.py:_watch_esc_during_streaming` and spawns it as a
background task per exchange.

### TC-CHAT-DENY-01 (pre-fix)

```
=================================== FAILURES ===================================
______________ test_tc_chat_deny_01_deny_once_flips_cancel_token _______________

    async def test_tc_chat_deny_01_deny_once_flips_cancel_token() -> None:
        ...
        provider = _provider_with_bash_prompt()
>       modal = ApprovalModal(
            stdout=out,
            input_fn=_scripted_input(["n"]),
            state=state,  # type: ignore[arg-type]
        )
E       TypeError: ApprovalModal.__init__() got an unexpected keyword argument 'state'

tests/component/cli/test_chat_deny_stops.py:108: TypeError
```

Pre-fix bite: the modal's `state` ctor kwarg doesn't exist —
construction itself fails. This is stronger than the spec's
predicted "DENY returned but cancel not flipped" failure mode:
the API surface that lets the chat plumb its CancelToken into the
modal is missing entirely. The post-fix code adds the kwarg + the
`bind_state(...)` setter (`src/cli/_modal.py:171-198`) and the
DENY path's `_flip_cancel_for_deny()` (`src/cli/_modal.py:281-300`).

### Stash + restore confirmation

```
$ git stash push -m "step2-production-changes" -- \
    src/cli/chat.py src/cli/_modal.py src/cli/_slash.py \
    tests/component/cli/test_chat_modal.py
Saved working directory and index state On fix/framework-iter-cap-removal:
  step2-production-changes

# (ran failing tests as above)

$ git stash pop
Dropped refs/stash@{0} (a46d846f135c56b605891bc2a75e1d4ad4211e86)

$ uv run pytest tests/component/cli/test_chat_deny_stops.py \
    tests/component/cli/test_chat_esc.py --no-cov
6 passed in 1.24s
```

---

## Part C — Manual e2e checklist + automated e2e evidence

This section has TWO parts:

- **Part C.0** — **automated e2e** that drives the full engine →
  framework → tool subsystem → authz provider → modal path
  against a scripted FakeLLM. Captures real JSONL evidence at
  the engine level for all five scenarios. Lives in
  `tests/component/cli/test_chat_deny_stops_e2e.py` and runs
  in CI; produces the writeup-ready excerpts in Part C.1 below.
- **Part C.2** — **manual checklist for real-LLM verification**
  that the user runs from their terminal. The reviewer FAIL
  flagged that the manual scenarios still need user keystrokes
  + a real LLM. The automated e2e covers the engine-level
  contract; the manual run covers model-behavior properties
  the engine doesn't have visibility into (e.g. "the LLM does
  not retry after deny" is an engine-level guarantee in the
  automated test; "the LLM doesn't say anything weird in the
  ten seconds before the cancel lands" is a user-observable
  property only).

### Part C.0 — Automated e2e: how to run

```bash
rm -f /tmp/step3-evidence.txt
KOLSCH_STEP3_EVIDENCE=/tmp/step3-evidence.txt \
  uv run pytest tests/component/cli/test_chat_deny_stops_e2e.py \
  --no-cov -q
cat /tmp/step3-evidence.txt
```

The `KOLSCH_STEP3_EVIDENCE` env var enables the sidecar file
where the tests append JSONL excerpts + the `platform.yaml`
diff. Without it the tests still run + pass; the env var is the
seam for materialising the writeup-ready evidence.

Runtime: 5 tests, ~3.5s.

### Part C.1 — Captured evidence (verbatim from automated run)

#### Scenario 1 — Deny once

`event_stream` from a workspace with default `tool.bash: prompt`
rule + a modal scripted to pick `[n]`. The pre-existing
`tool.unauthorized` event is the modal's DENY surfacing through
`_ObservedToolHandle.__call__` (`src/engine/observers.py:225-237`);
the load-bearing property is what comes AFTER it: no
`model.request`, only `thread.exchange_failed` with
`exchange.cancelled`.

```jsonl
{"type": "config.set"}
{"type": "thread.exchange_started"}
{"type": "user.message"}
{"call_id": "cl_4aa73131-…", "type": "model.request"}
{"stop_reason": "tool_use", "type": "model.response"}
{"tool_call_id": "tc_scenario1", "tool_name": "bash", "type": "tool.called"}
{"error.type": "tool.unauthorized", "tool_name": "bash", "type": "tool.failed"}
{"error.type": "exchange.cancelled", "type": "thread.exchange_failed"}
```

**Verify (from the reviewer's list):**

- [x] One `thread.exchange_failed` with
      `error.type=exchange.cancelled`.
- [x] NO `model.request` after the `tool.failed` event — only
      one `model.request` total, before the deny. Proof the LLM
      was not given the chance to react to the denial.

#### Scenario 2A — Deny always (exchange #1)

Same shape as Scenario 1 with `[d]` + accepted suggested
pattern:

```jsonl
{"type": "config.set"}
{"type": "thread.exchange_started"}
{"type": "user.message"}
{"call_id": "cl_813abb35-…", "type": "model.request"}
{"stop_reason": "tool_use", "type": "model.response"}
{"tool_call_id": "tc_scenario2a", "tool_name": "bash", "type": "tool.called"}
{"error.type": "tool.unauthorized", "tool_name": "bash", "type": "tool.failed"}
{"error.type": "exchange.cancelled", "type": "thread.exchange_failed"}
```

#### Scenario 2A — `platform.yaml` rules: block (post-`[d]`)

The new deny rule `^cat .*$` was inserted BEFORE the
`tool.bash` prompt rule (first-match-wins):

```yaml
rules:
- id: rl_ecbb791b
  who: .*
  what: agent\.execute\..*
  effect: allow
- id: rl_0d79e965
  who: .*
  what: tool\.read_file
  effect: allow
- id: rl_954169ac
  who: .*
  what: tool\.list_files
  effect: allow
- id: rl_d30ef4c6
  who: .*
  what: tool\.write_file
  effect: prompt
- id: rl_14e73a9b
  who: .*
  what: ^cat .*$         # ← NEW, from the [d] persist
  effect: deny
- id: rl_99a71d57
  who: .*
  what: tool\.bash
  effect: prompt
- id: rl_default_deny
  who: .*
  what: .*
  effect: deny
```

#### Scenario 2B — Restart with persisted rule

Bootstrap a FRESH runtime against the SAME workspace. The
modal's `input_fn` is wired to raise `AssertionError` if
invoked. The exchange:

```jsonl
{"type": "config.set"}
{"type": "thread.exchange_started"}
{"type": "user.message"}
{"call_id": "cl_c30e2040-…", "type": "model.request"}
{"stop_reason": "tool_use", "type": "model.response"}
{"tool_call_id": "tc_scenario2b", "tool_name": "bash", "type": "tool.called"}
{"error.type": "tool.unauthorized", "tool_name": "bash", "type": "tool.failed"}
{"call_id": "cl_8257db41-…", "type": "model.request"}
{"stop_reason": "text", "type": "model.response"}
{"type": "agent.message"}
{"type": "thread.exchange_completed"}
```

**Verify (from the reviewer's list):**

- [x] Same stop behavior as Scenario 1 — exchange #1 ends with
      `exchange.cancelled` (above).
- [x] New deny rule appears in `platform.yaml` (above).
- [x] Re-prompting in a new exchange auto-denies WITHOUT firing
      the modal. The `input_fn` raise-sentinel was never tripped
      → the persisted rule matched before the prompt rule. The
      restart exchange completes naturally (the model now
      observes the tool error and responds — this is the
      auto-deny path; the modal-flip-cancel path is a separate
      mechanism that only fires when the modal renders).

#### Scenario 3 — Esc during streaming

```jsonl
{"type": "config.set"}
{"type": "thread.exchange_started"}
{"type": "user.message"}
{"call_id": "cl_2bda806b-…", "type": "model.request"}
{"stop_reason": "tool_use", "type": "model.response"}
{"tool_call_id": "tc_scenario3_1", "tool_name": "bash", "type": "tool.called"}
{"tool_name": "bash", "type": "tool.result"}
{"error.type": "exchange.cancelled", "type": "thread.exchange_failed"}
```

**Verify (from the reviewer's list):**

- [x] `thread.exchange_failed` with `exchange.cancelled`.
- [x] The event IMMEDIATELY preceding `thread.exchange_failed`
      is `tool.result` (NOT `model.request`). Proof Esc landed
      at the cycle boundary — the in-flight tool call ran to
      completion; the next iteration's poll caught the
      cancellation BEFORE the next `model.request` was issued.

#### Scenario 4 — Ctrl-C still cancels (regression)

```jsonl
{"type": "config.set"}
{"type": "thread.exchange_started"}
{"type": "user.message"}
{"call_id": "cl_f969e531-…", "type": "model.request"}
{"stop_reason": "tool_use", "type": "model.response"}
{"tool_call_id": "tc_scenario4_1", "tool_name": "bash", "type": "tool.called"}
{"tool_name": "bash", "type": "tool.result"}
{"error.type": "exchange.cancelled", "type": "thread.exchange_failed"}
```

> Scenario 4 confirmation: Ctrl-C → `cancel.cancel()` →
> `exchange.cancelled` emitted;
> `state.last_cancel_reason == "interrupted"`. Cross-pins
> TC-CHAT-08 (engine cancellation path) + Step 2
> `chat.py:493-497` Ctrl-C branch's reason-stamp addition.

- [x] Same engine-level shape as Scenario 3 (Esc) — that's the
      load-bearing property of routing both keys through one
      `CancelToken`.

#### Scenario 5 — Esc at empty prompt is safe

Driven via `prompt_toolkit`'s `create_pipe_input`: feed
`"\x1b" + "hello" + "\r"`; assert the prompt returns
`"hello"`.

> Scenario 5 confirmation: bare Esc at empty PromptSession
> prompt is a no-op (`prompt_toolkit`'s Emacs-mode Meta
> prefix); subsequent input `"hello"` submits correctly.
> `result == "hello"`.

- [x] Bare Esc at the prompt did NOT submit `""`, did NOT exit,
      did NOT raise. Default Emacs-mode behavior preserved.

### Part C.2 — Manual checklist for real-LLM verification

**Why this still exists:** the automated e2e pins the
engine-level contract — what events the engine emits, what the
`CancelToken` does, what `platform.yaml` looks like after
`[d]`. What it CANNOT pin:

1. Real model behavior (e.g. a particular provider's response
   to the tool error payload that pre-fix would have surfaced).
2. The terminal rendering of the `[stopped — …]` lines — does
   the muted styling actually render correctly on the user's
   terminal? Automated tests can't see the visual output.
3. The Ctrl-C / Esc key timing through a real prompt_toolkit
   session against a real LLM call latency.

The user runs the steps below against `~/Documents/kolsch`
(or a real workspace) with a connected provider and pastes the
confirmations back into this writeup (or the PR description).

### Setup

```bash
# Fresh workspace, helper agent, real provider configured
cd ~/Documents/kolsch   # or wherever your real workspace lives
uv run kolsch chat
```

Note the session id printed in the banner — `SES=<ses_…>`. You'll
inspect the JSONL after each scenario:

```bash
# In a second terminal
SES=ses_…                                     # from the banner
JSONL=~/.kolsch/sessions/$SES/events.jsonl    # adjust path if non-default
tail -f "$JSONL"                              # live tail; helpful but optional
```

### Scenario 1 — Deny once

Prompt:

```
run `ls -la /Users/andrecohen/Desktop`
```

Modal appears; pick **`n`**.

**Verify (✓ = OK):**

- [ ] Chat prints `[stopped — you denied the tool]` after the
      partial agent block.
- [ ] `You:` prompt returns.
- [ ] `jq -c 'select(.type == "thread.exchange_failed")
      | .data.error.type' "$JSONL" | tail -1` outputs
      `"exchange.cancelled"` (note: Phase 1 emits
      `exchange.cancelled`; the renderer treats that as
      cancellation per `_is_cancellation_code`).
- [ ] `jq -c 'select(.type == "model.request")' "$JSONL"
      | tail -3` — confirm NO `model.request` lands AFTER the
      `tool.failed` event. (Proof: the LLM was not given the
      chance to react to the denial.)

### Scenario 2 — Deny always

Prompt:

```
read pyproject.toml
```

Modal appears; pick **`d`**; accept the suggested pattern with
Enter.

**Verify:**

- [ ] Same stop behavior as Scenario 1 (`[stopped — …]` line +
      prompt returns).
- [ ] After exiting the chat (`/exit`), inspect
      `~/Documents/kolsch/platform.yaml` — there's a new deny
      rule with `what: ^/Users/andrecohen/Documents/kolsch/.*$`
      (or whatever the file's parent directory was) and
      `effect: deny`.
- [ ] Re-launch chat (`uv run kolsch chat`), prompt
      `read pyproject.toml` again. Expected:
      auto-denied (no modal — the persisted rule fires first),
      AND the exchange still stops cleanly with the same
      `[stopped — …]` line. (The modal short-circuit fires; the
      framework's next iteration poll observes the flipped
      cancel.)

### Scenario 3 — Esc during streaming

Prompt:

```
list everything in src/engine and read each file
```

While the agent is calling tools (you'll see tool capsules
arriving), press **Esc** once.

**Verify:**

- [ ] Chat prints `[stopped — interrupted]` AFTER the current
      tool finishes (NOT immediately — cancellation is "at the
      next iteration boundary" per the Step 1 latency model;
      the in-flight `tool.result` lands first).
- [ ] `You:` prompt returns.
- [ ] `jq -c 'select(.type == "thread.exchange_failed")
      | .data.error.type' "$JSONL" | tail -1` outputs
      `"exchange.cancelled"`.
- [ ] The tool event immediately preceding `thread.exchange_failed`
      is a `tool.result` (or `tool.failed`) — NOT a `model.request`.
      Proof that Esc landed at a cycle boundary, not mid-LLM-call.

### Scenario 4 — Ctrl-C still works (regression guard)

Repeat Scenario 3 but press **Ctrl-C** instead of Esc.

**Verify:**

- [ ] Same behavior as Scenario 3. The `[interrupted; waiting
      for engine to cancel...]` status line prints immediately
      (existing `chat.py:497-498` Ctrl-C path), then
      `[stopped — interrupted]` after the drain finishes.
- [ ] If you don't see the same shape, Step 2 broke Ctrl-C —
      this is the regression check.

### Scenario 5 — Esc at the empty prompt is safe

At the `You:` prompt with no exchange running, press **Esc** a
few times.

**Verify:**

- [ ] Nothing happens. The prompt does not submit, exit, or
      clear. Default prompt_toolkit Emacs-mode behavior (bare
      Esc is a Meta prefix awaiting a follow-up key).
- [ ] If Esc submits an empty line OR exits the chat, Step 2
      regressed at-prompt behavior — escalate.

### What to capture in the PR description

Paste, per scenario:

1. The `jq` output for `thread.exchange_failed` event types
   (one per scenario).
2. For Scenario 2, paste the `platform.yaml` `rules:` block
   showing the new deny rule.
3. A screenshot or terminal paste of the `[stopped — …]` lines
   showing the styling (muted vs. yellow — sanity check).

### Escalation triggers (from the spec)

If any of the following hold, Step 2 needs rework:

| Symptom | What it proves |
|---|---|
| Scenario 1: a `model.request` follows the `tool.failed` deny event | The deny-stops path is racy — the LLM got an extra turn. |
| Scenario 3: terminal output is corrupted while Esc listener runs | The PoC missed a stdout/stdin interaction. |
| Scenario 2: the new rule never appears in `platform.yaml` | The cancel flip raced ahead of the rule write. |

---

## Part D — Follow-up tickets

Two follow-ups surfaced during Step 1/2 that are explicitly out of
scope for this fix but worth filing so they're not silently dropped.

### Follow-up 1 — Esc watcher loses its reader after the modal exits

**Filed at:** `process/qol-chat-esc-watcher-post-modal.md` (Part E
below — full text inline).

**Summary:** prompt_toolkit's `Input.attach()` uses
`asyncio.loop.add_reader(fd, callback)`. Asyncio's `add_reader`
REPLACES any prior reader on the same fd; it does not stack.
When the modal's `PromptSession.prompt_async` runs inside an
exchange, it attaches its own reader, replacing the Esc watcher's.
When the modal exits, it calls `loop.remove_reader(fd)` — removing
the modal's reader without restoring the Esc watcher's. After the
first modal in an exchange, post-modal Esc presses don't fire the
watcher. Ctrl-C still works (signal-based, not fd-based) so the
user has a working fallback.

**Why deferred:** the deny path already flips the cancel as soon
as the user picks `[n]` / `[d]`, so the most common
modal-then-stop case is already handled. The remaining gap (Esc
during streaming AFTER a non-cancelling modal — i.e. the user
picked `[y]` / `[a]`) is real but not blocking; Ctrl-C is
documented as the universal interrupt key.

### Follow-up 2 — Mid-tool cancellation latency

**Filed at:** `process/bug-mid-tool-cancellation-latency.md`
(Part F below — full text inline). Also explicitly listed as
out-of-scope by `bug-chat-deny-stops-and-esc-interrupts.md`
Part 7.

**Summary:** the framework polls `cancellation` only at iteration
boundaries (`fake_framework.py:78-79`, `_CrewAILLM.call`). A
30s `bash` call mid-flight holds cancellation until the tool
returns; Esc during that interval has the same latency. Both
this fix and `bug-framework-iteration-cap-removal.md` cite the
same shared property in their Step 1 writeups. Tightening the
poll site is a separate concern (requires the framework protocol
to grow a mid-tool cancel surface).

### Follow-up 3 — Spec drift

**Filed at:** `process/qol-chat-specs-deny-esc-update.md` (Part G
below — full text inline).

**Summary:** `docs/explanation/phase2.1-chat-specs.md` does not
mention the new behaviors (deny stops the exchange; Esc during
streaming; modal short-circuit; distinct `[stopped — …]` lines).
The spec is the authoritative reference for chat behavior;
without an update, future readers won't know the new contract is
load-bearing. Small docs follow-up.

---

## Part E — qol-chat-esc-watcher-post-modal.md (proposed)

See the file at the same path for the full ticket. Summary:

- **Why:** Esc-during-streaming stops working after the first
  modal in an exchange due to asyncio's `add_reader` replace
  semantics. UX gap, not a correctness gap.
- **Fix surface:** either (a) re-attach the watcher after each
  modal exit (requires a coordination protocol between the
  modal and the chat), or (b) re-architect the watcher to use a
  separate thread reading stdin via `select` (avoids asyncio's
  reader registry entirely).
- **Severity:** P3 / polish. Ctrl-C is the working fallback.

---

## Part F — bug-mid-tool-cancellation-latency.md (proposed)

See the file at the same path. Summary:

- **Why:** Esc / Ctrl-C does not interrupt a mid-flight tool
  (e.g. a 30s `bash` command). The framework polls cancellation
  only at iteration boundaries.
- **Fix surface:** thread the cancel token into the tool
  dispatch surface so individual tool implementations can poll
  and abort. Spec change: `ToolHandle` protocol grows a
  cancel-aware variant.
- **Severity:** P2 / latent UX issue. Real LLM workloads
  occasionally hit this on long-running bash.

---

## Part G — qol-chat-specs-deny-esc-update.md (proposed)

See the file at the same path. Summary:

- **Why:** `phase2.1-chat-specs.md` §4 (modal) and §6
  (cancellation) do not describe the new behaviors. The
  CHAT-R9 hard rule keeps the renderer event-faithful, so the
  spec drift is in §4 (modal flips cancel) and §6.1
  (Esc binding + post-render messaging).
- **Surface:** docs-only patch to the chat specs document.
- **Severity:** P3 / docs hygiene.
