# Bug — CrewAI output leak past the framework-wrap: cached-reference Rich panels + logging.error during retries

**Draft 0.1** *(picks up where `bug-crewai-stdout-leak-investigation.md` closed at G1/G2. That investigation handled the `  …` streaming-indicator class of leak. This doc handles the cached-Python-reference class — Rich Console singletons + `logging.error` calls — which became dramatically more visible after the retry loop wired up in `bug-platform-internal-error-boundary-swallow.md` Draft 0.3.)*

> Reported symptom (re-triggered post-0.3-boundary-classifier-fix):
> a `kolsch chat` session against a flaky provider now shows
> CrewAI's own error chrome leaking through:
>
> ```
> ERROR root: Failed to connect to OpenAI API: Request timed out.
> ERROR root: OpenAI API call failed: Failed to connect to OpenAI API: Request timed out.
> ╭─ ❌ LLM Error ────────────────────────────────────────────╮
> │  LLM Call Failed                                          │
> │  Error: Failed to connect to OpenAI API: Request timed... │
> ╰───────────────────────────────────────────────────────────╯
> [CrewAIEventsBus] Warning: Ending event 'llm_call_failed' emitted with empty scope stack.
> ```
>
> Three distinct leak channels, each bypassing the existing
> `contextlib.redirect_stdout(io.StringIO())` wrap in
> `crewai_framework.py:204-207`. The retry-loop fix made them
> proportionally more visible: each retry = another set of
> panels + log lines.

---

## Part 0 — Why this matters

Three problems compound:

1. **The chrome is CrewAI's, not kolsch's.** Kolsch already
   renders failure-event capsules with its own consistent
   style (see `qol-chat-capsule-layout.md`). The `❌ LLM Error`
   panels are CrewAI's debug surface bleeding through. The
   user sees two visually inconsistent failure presentations
   for the same event — once from CrewAI, once from kolsch.

2. **Volume scales with retries.** Pre-retry-fix: 1 attempt
   per exchange = 2 panels + 2 ERROR lines per failure.
   Post-retry-fix (current): 3 attempts per exhausted
   exchange = 6 panels + 6 ERROR lines + multiple
   `[CrewAIEventsBus]` warnings. The user-visible noise grew
   3x even though the underlying bug surface got better. This
   is a regression in user perception even when the engine
   is doing the right thing.

3. **The existing fix is provably incomplete.**
   `bug-crewai-stdout-leak-investigation.md` G1 wrapped
   `crew.kickoff_async()` in
   `contextlib.redirect_stdout(io.StringIO())`. That doc's §5.6
   sign-off claimed "G1's wrap already eats any
   tracing-disabled panels." The trace above shows the wrap
   does NOT eat THESE panels. The closing investigation
   didn't test against the retry-triggered failure path
   because the retry path didn't exist yet.

---

## Part 1 — Why the existing wrap fails

`contextlib.redirect_stdout(new_file)` reassigns `sys.stdout`
to `new_file` at context entry, restores at exit. It only
helps callers that read `sys.stdout` at write time
(`print()`, `sys.stdout.write()`).

Three CrewAI write paths cache references **before** kolsch's
context ever runs:

### 1.1 Rich Console singletons (the `❌ LLM Error` panels)

`crewai/events/utils/console_formatter.py:43`:

```python
class ConsoleFormatter:
    def __init__(self, ...):
        self.console = Console(width=None)   # ← captures sys.stdout
```

Rich's `Console(file=None, ...)` copies `sys.stdout` at
construction into `self.file`. Subsequent
`console.print(...)` calls write to `self.file`, NOT a
fresh `sys.stdout` lookup.

`ConsoleFormatter` is instantiated as a singleton at CrewAI
module load:

- `crewai/events/event_context.py:59` → `_console = ConsoleFormatter()`
- `crewai/events/event_bus.py:151` → `self._console = ConsoleFormatter()`
- `crewai/events/event_listener.py:155` → `self.formatter = ConsoleFormatter(verbose=True)`

All three constructions happen at `import crewai` time —
**before** kolsch's `redirect_stdout` context runs. The
cached `self.file` references point at the real terminal's
stdout. Our context swap has no effect on them.

The `❌ LLM Error` panel is rendered at
`console_formatter.py:528`:

```python
self.print_panel(error_content, "❌ LLM Error", "red")
```

The `print_panel` body calls `self.console.print(panel)` —
writing directly to the cached file reference, bypassing
`sys.stdout` entirely.

### 1.2 logging.error → root logger StreamHandler (the `ERROR root:` lines)

`crewai/llms/providers/openai/completion.py:912, 919, 930`:

```python
except APIConnectionError as e:
    error_msg = f"Failed to connect to OpenAI API: {e}"
    logging.error(error_msg)   # ← root logger
    ...
```

`logging.error` routes to the root logger. The default
StreamHandler captures `sys.stderr` at handler creation
time. If the root logger was configured at process start
(which it typically is — `logging.basicConfig` or implicit
default), the StreamHandler holds a stale `sys.stderr`
reference.

`contextlib.redirect_stderr(io.StringIO())` reassigns
`sys.stderr` but the StreamHandler's `self.stream` is
unchanged — same cached-reference problem as Rich's
Console.

### 1.3 CrewAIEventsBus print/warn

`[CrewAIEventsBus] Warning: Ending event 'llm_call_failed'
emitted with empty scope stack.` is from CrewAI's event bus
emitting a diagnostic when scope bookkeeping is
inconsistent. Verification in Step 1 confirms whether this
goes through `print`, `logging`, or the cached Console
singleton. Most likely the Console — same channel as 1.1.

---

## Part 2 — Theory of the solution

The principle:

> **`redirect_stdout`/`redirect_stderr` are insufficient when
> the writers cache references at import time.** Fix it at
> the layer where caching doesn't help — the OS file
> descriptors, plus the Python logging configuration.

Three layers compose:

| Layer | What it catches | How |
|---|---|---|
| **FD-level capture** (OS file descriptors 1 + 2) | Rich Console writes, `print`, anything that ultimately writes to stdout/stderr regardless of cached Python references | `os.dup2(devnull_fd, 1)` and `os.dup2(devnull_fd, 2)` around `crew.kickoff_async`; restore on exit |
| **Logging disable** | `logging.error / .warning` calls regardless of which handler is configured or what stream it caches | `logging.disable(logging.CRITICAL)` inside the context; restore on exit |
| **Existing `redirect_stdout`/`redirect_stderr`** | Late-bound `sys.stdout` reads (defensive; cheap to keep) | Already in place at `crewai_framework.py:204-207`. Keep. |

**Locked recommendation: stack all three.** FD-level is the
primary fix (catches Rich + cached-reference writers). Logging
disable is the secondary fix (catches `logging.error` calls
that bypass FD writes by going through Python logging machinery
with cached stream references — important because some logging
configs write to non-stderr targets too). The existing
`redirect_stdout/stderr` wrap stays as the third line — cheap
defense in depth.

### Why not just FD-level

FD-level alone misses Python `logging` handlers that write to
files or other sinks (e.g., a future CrewAI release that adds
file-logging). `logging.disable` is a one-line guarantee that
log messages don't surface regardless of where they're routed.

### Why not just logging-disable

Logging-disable doesn't touch Rich Console writes — those
don't go through Python logging at all, they go straight to
the cached file reference.

### Risks and tradeoffs

- **FD-level capture eats anything kolsch writes from inside
  the framework call.** Audit: kolsch's event-stream
  publishing is async on the engine's loop, NOT via stdout.
  Engine internal logs go through Python `logging`. So the
  only impact is on accidental debug prints from inside the
  framework call — and those should be removed anyway.
- **Logging-disable is process-wide for the duration.** A
  parallel exchange running concurrently would also have
  its logging silenced. In v1's single-exchange-at-a-time
  chat model this is fine; flag it for Phase 3 multi-thread
  work.
- **`os.dup2` is platform-sensitive.** macOS / Linux work
  the same; Windows file-descriptor handling is different.
  Kolsch v1's supported platforms are macOS + Linux per the
  Containerfile and the qol-setup-evolution docs; Windows
  is out of scope. Document in the spec.

---

## Part 3 — Acceptance criteria

The bug is fixed when **all** hold:

1. **Rich panels suppressed.** Running a chat session against
   a deliberately-failing provider produces ZERO `❌ LLM
   Error` panels in the user-visible output. Verify by
   capturing the terminal output of a 3-retry-exhausted
   exchange.
2. **ERROR root lines suppressed.** Same capture shows ZERO
   `ERROR root:` lines. Verify with `grep`.
3. **CrewAIEventsBus warnings suppressed.** Same capture
   shows ZERO `[CrewAIEventsBus]` lines.
4. **Kolsch's own failure capsules still render.** The
   user-visible output for a retry-exhausted exchange shows
   kolsch's `model.retry_exhausted` event capsule rendered
   in the existing capsule style. Suppression does NOT
   accidentally eat kolsch's own output.
5. **Kolsch event-stream publishing is unaffected.** The
   session JSONL still contains all model.request,
   model.response, tool.called, exchange_failed events as
   before. Suppression only affects user-visible terminal
   output, not the event log.
6. **No platform regression.** The full test suite passes
   on macOS + Linux. Windows is out of scope per Part 2;
   document.
7. **Regression tests pin the suppression.** A unit test
   verifies that `print("LEAK")` inside the framework call
   does NOT appear in captured terminal output. A second
   test verifies that `logging.error("LEAK")` does NOT
   appear. Together they prove the FD-level + logging-disable
   contract.

### Out of scope

- **Forking the CrewAI singletons** (replacing the cached
  Console with a /dev/null Console at import). Brittle, reaches
  into internals — locked decision is FD-level.
- **Patching CrewAI's OpenAI adapter.** Not our code; same
  brittleness reasoning.
- **A user-facing "show CrewAI output" debug toggle.** If
  developers ever want to see the leaked panels (debugging
  CrewAI itself), they can disable the framework wrap
  manually. Don't ship a flag for it.
- **Phase 3 multi-thread `logging.disable` safety.** Logging-
  disable is process-wide; concurrent exchanges in Phase 3
  would interact. Flagged for later; not blocking v1.

---

## Part 4 — Step dependency graph

```
Step 1 (verify the three channels + FD-capture PoC) ←── STOP gate
   │
   ▼
Step 2 (implement: FD-capture + logging-disable in crewai_framework.py)
   │
   ▼
Step 3 (regression tests + manual e2e + handoff)
```

| Step | Ships | Reviewable in |
|---|---|---|
| **1** | Writeup naming each channel's exact write path + a 10-line FD-capture PoC | ~20 min |
| **2** | FD capture + logging disable context manager + integration into `crewai_framework.py` | ~25 min |
| **3** | Regression tests + manual e2e with intentionally-triggered retries | ~25 min |

---

## Step 1 — Verification + FD-capture PoC

### Developer prompt

```
[standing instructions]

Step 1 — Verify the three leak channels and prove the
FD-capture approach works before any production code lands.

Read first:
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/bug-crewai-output-leak-during-retries.md Parts 0-3.
- /Users/andrecohen/Desktop/SV/Projects/kolsch/process/bug-crewai-stdout-leak-investigation.md (for the closed G1/G2 history; do NOT redo their probes).
- /Users/andrecohen/Desktop/SV/Projects/kolsch/src/engine/crewai_framework.py:204-237 (the existing wrap).
- .venv/lib/python3.13/site-packages/crewai/events/utils/console_formatter.py:43 + lines 524-528 (panel render site).
- .venv/lib/python3.13/site-packages/crewai/events/event_context.py:59 (singleton instantiation).
- .venv/lib/python3.13/site-packages/crewai/events/event_bus.py:151 (singleton instantiation).
- .venv/lib/python3.13/site-packages/crewai/llms/providers/openai/completion.py:910-935 (the logging.error sites).

Verification tasks:

1. Channel 1 — Rich Console panels.
   - Confirm the ConsoleFormatter singleton instantiation
     sites (3 of them per Part 1.1).
   - Confirm Rich Console caches sys.stdout at __init__.
     Read .venv/.../rich/console.py around the __init__
     for the file attribute handling. Quote the
     relevant lines in your writeup.
   - Confirm the print_panel → self.console.print path at
     console_formatter.py:528 writes directly to the cached
     file, NOT via sys.stdout.

2. Channel 2 — Python logging.
   - Confirm completion.py:912, 919, 930 call logging.error
     with the captured error_msg.
   - Confirm the root logger's default StreamHandler caches
     sys.stderr. Read .venv/.../logging/__init__.py for the
     StreamHandler.__init__ signature; quote the stream
     attribute initialization.
   - Verify whether logging.disable(logging.CRITICAL) actually
     suppresses ERROR-level messages from being processed.

3. Channel 3 — [CrewAIEventsBus] Warning.
   - grep for "CrewAIEventsBus" or "empty scope stack" in
     .venv/.../crewai/events/.
   - Identify which mechanism emits it (print, logging,
     or self.console).
   - Document.

4. FD-capture PoC — prove it works.
   - Write a 10-line Python script that:
     a. Creates a Rich Console BEFORE entering the context
        (mimicking CrewAI's singleton).
     b. Opens /dev/null, calls os.dup2 on FDs 1 and 2 to
        redirect them.
     c. Calls console.print("LEAK") — should be suppressed.
     d. Calls logging.error("LEAK") — should be suppressed.
     e. Restores FDs via os.dup2 from saved originals.
     f. After the context, prints "OK" — should appear.
   - Paste the PoC in the writeup. Paste the output. The
     output should be only "OK", no "LEAK" lines.

5. Platform sanity check.
   - Confirm the kolsch supported-platforms list (macOS,
     Linux per Containerfile).
   - Document that Windows would need a separate code path
     (probably using io.IOBase or msvcrt) and is explicitly
     out of scope.

6. Existing crewai_framework.py wrap — confirm what stays.
   - The current `with contextlib.redirect_stdout(io.StringIO()),
     contextlib.redirect_stderr(io.StringIO()):` block at
     lines 204-207 stays as-is (defense in depth). Step 2
     ADDS FD capture + logging-disable around it; doesn't
     remove the existing wrap.
   - Confirm this composition order works: outer FD capture
     → middle logging disable → inner redirect_stdout/stderr
     → crew.kickoff_async. Sketch the with-stack in the
     writeup.

7. STOP and post the writeup + PoC.

ESCALATION PATH: if Step 1 finds that:
 (a) one of the three channels uses a mechanism other than
     what's documented above (e.g., the Warning uses a
     subprocess pipe), or
 (b) the FD-capture PoC fails in your environment (e.g.,
     pytest captures FD output in a way that defeats it),
 (c) logging.disable doesn't actually suppress ERROR level
     in the current Python version,
escalate before proceeding.

Acceptance: writeup with citations + working PoC + platform
caveat documented; coordinator approves.
```

### Reviewer prompt

```
[standing instructions]

Review Step 1.

NO production code. Writeup + PoC are the deliverable.

Checks:

1. Each of the three channels has a concrete cite:
   - Rich Console: __init__ caching of sys.stdout, quoted.
   - logging.error: root logger StreamHandler caching of
     sys.stderr, quoted.
   - CrewAIEventsBus: actual mechanism identified.

2. The FD-capture PoC actually runs in the reviewer's
   environment. Don't take the dev's word — paste the same
   PoC into a shell and run it. Output should be ONLY "OK".

3. Compositional order documented: outer FD → middle
   logging disable → inner redirect_stdout → kickoff_async.

4. Platform caveat (macOS/Linux only, Windows out of
   scope) is in the writeup.

5. Any of the three escalation conditions surfaced is
   either confirmed not-an-issue with citations or
   escalated.

Verdict: APPROVE-TO-PROCEED or REVISE.
```

---

## Step 2 — Implement FD capture + logging disable

### Developer prompt

```
[standing instructions]

Step 2 — Suppress the three leak channels.

Read first:
- Step 1's writeup + PoC.
- src/engine/crewai_framework.py around lines 204-237.

Tasks:

1. Add a new helper at src/engine/_output_capture.py:

   import contextlib
   import logging
   import os

   @contextlib.contextmanager
   def suppress_framework_output():
       """Triple-layer suppression for CrewAI noise during
       crew.kickoff_async. Catches:
       1. Cached Rich Console writes (FD-level dup2).
       2. logging.error / .warning bypassing FD redirect
          (logging.disable).
       3. Late-bound sys.stdout / sys.stderr reads
          (the existing redirect_stdout/stderr is composed
          on top by the caller, not here — this helper
          handles 1 and 2).
       """
       devnull_fd = os.open(os.devnull, os.O_WRONLY)
       saved_stdout_fd = os.dup(1)
       saved_stderr_fd = os.dup(2)
       try:
           os.dup2(devnull_fd, 1)
           os.dup2(devnull_fd, 2)
           # Suppress logging at CRITICAL — silences everything
           # at or below. Restored in finally.
           logging.disable(logging.CRITICAL)
           try:
               yield
           finally:
               logging.disable(logging.NOTSET)
       finally:
           os.dup2(saved_stdout_fd, 1)
           os.dup2(saved_stderr_fd, 2)
           os.close(saved_stdout_fd)
           os.close(saved_stderr_fd)
           os.close(devnull_fd)

   NOTE: avoid catching KeyboardInterrupt or system exits;
   the finally block runs regardless.

2. Integrate into crewai_framework.py.
   - The existing wrap at lines 204-207:
       with (
           contextlib.redirect_stdout(io.StringIO()),
           contextlib.redirect_stderr(io.StringIO()),
       ):
           try:
               result = await crew.kickoff_async()
   - Change to:
       from engine._output_capture import suppress_framework_output
       with (
           suppress_framework_output(),
           contextlib.redirect_stdout(io.StringIO()),
           contextlib.redirect_stderr(io.StringIO()),
       ):
           try:
               result = await crew.kickoff_async()
   - Order matters per Step 1's writeup: FD + logging outer,
     redirect_stdout inner. The FD capture catches Rich;
     redirect_stdout catches late-bound sys.stdout reads.

3. DO NOT remove the redirect_stdout/stderr block. The two
   layers compose; removing the inner layer would force
   future debugging to debug both layers together.

4. Unit tests in tests/unit/test_output_capture.py:
   - With suppress_framework_output() active:
     * print("LEAK") writes nothing to stdout (capture via
       a real subprocess, NOT capsys — capsys monkey-patches
       sys.stdout and won't observe FD-level writes).
     * logging.error("LEAK") writes nothing.
     * sys.stdout.write("LEAK") writes nothing.
   - After exit:
     * print("OK") writes to stdout normally.
     * logging.error("OK") goes to the root logger normally.
     * Exception inside the context still propagates and FDs
       still restore.

   Use subprocess.run with python -c to test FD behavior
   reliably:

   def test_fd_capture_suppresses_print():
       result = subprocess.run(
           [sys.executable, "-c", """
import sys
sys.path.insert(0, '/path/to/src')
from engine._output_capture import suppress_framework_output
with suppress_framework_output():
    print("LEAK")
print("OK")
"""],
           capture_output=True,
           text=True,
       )
       assert "LEAK" not in result.stdout
       assert "OK" in result.stdout

5. Document in the module docstring that this helper is
   macOS/Linux only and Windows requires a separate code
   path.

6. NO change to:
   - The retry loop or boundary classifier (those are the
     other ticket).
   - Engine's emit_exchange_failed or renderer.
   - Any logging configuration outside the suppress context.

Scope: ~50 LOC code + ~80 LOC tests.

ESCALATION PATH: if the subprocess-based tests can't easily
run in CI (e.g., the test env doesn't have a writable
/dev/null), document and escalate.

Acceptance: tests pass; manual smoke (run a chat session
against a misconfigured provider that errors) shows no
panels, no ERROR root, no Bus warnings.
```

### Reviewer prompt

```
[standing instructions]

Review Step 2.

Checks:

1. _output_capture.py exists with the suppress_framework_output
   helper. Implementation uses os.dup2 (NOT redirect_stdout),
   logging.disable (NOT removing handlers), and restores all
   three on exit via finally.

2. The crewai_framework.py wrap composes:
   - Outer: suppress_framework_output()
   - Inner: redirect_stdout(StringIO()) + redirect_stderr(StringIO())
   Reverse order would mean the redirect runs FIRST and the
   FD capture wraps a StringIO-pointed sys.stdout — verify
   reading.

3. Existing wrap NOT removed. Defense in depth per spec
   Part 2.

4. Tests use subprocess.run, not capsys. capsys monkey-patches
   sys.stdout AT THE PYTHON LEVEL and would not observe FD
   writes — using it would let a broken implementation pass.

5. Tests verify both that LEAK is suppressed inside the
   context AND that normal output works after.

6. An exception-in-context test verifies the finally block
   runs and FDs are restored even on raise.

7. Module docstring documents the macOS/Linux limitation.

8. Full test suite green. tc_eng_* goldens still byte-equal.

9. Manual smoke in the PR description: a chat session that
   forces the failure path shows ZERO panels / ERROR / Bus
   warnings.

Verdict: PASS or FAIL.
```

---

## Step 3 — Regression tests + manual e2e

### Developer prompt

```
[standing instructions]

Step 3 — Lock in the suppression with regression tests + a
real-terminal manual e2e.

Read first:
- Step 2's PR.
- tests/component/cli/ for the closest existing
  end-to-end-chat test pattern.

Tasks:

1. Integration test
   tests/integration/test_crewai_output_suppression.py:

   TC-CREWAI-LEAK-01 — Retry-exhausted exchange produces no panels.
     def test_retry_exhausted_does_not_leak_panels():
         """A chat session driven into 3 retry attempts (all
         fail with ConnectionError carrying an APITimeoutError
         cause) produces ZERO CrewAI panel chars in the
         captured terminal output."""
         # Setup: a fault-injecting fake LLM that always raises
         # a ConnectionError with the APITimeoutError __cause__.
         # Config: runtime.max_retries=2.
         # Action: run an exchange end-to-end via the chat
         # surface (or _run_framework directly), capturing
         # stdout AT THE FD LEVEL (subprocess.run with the
         # captured output).
         # Expected:
         #   - No "❌ LLM Error" in captured stdout.
         #   - No "ERROR root:" in captured stdout/stderr.
         #   - No "[CrewAIEventsBus]" in captured output.
         #   - Kolsch's exchange_failed capsule DOES appear
         #     (one capsule for the model.retry_exhausted).

   TC-CREWAI-LEAK-02 — Successful exchange unaffected.
     def test_successful_exchange_renders_normally():
         """A normal chat exchange shows kolsch's own capsules
         (user message → agent message). No suppression
         artefacts."""

   TC-CREWAI-LEAK-03 — Mid-retry transient produces no leaks.
     def test_transient_retry_then_success_no_leaks():
         """An exchange that fails on attempt 1 and succeeds
         on attempt 2 still suppresses CrewAI's failure
         panel for the first attempt."""

2. Add to process/test-catalog.md under a new
   "CrewAI Output Leak (CREWAI-LEAK)" section.

3. Pre-fix verification:
   - git-stash Step 2's code; run TC-CREWAI-LEAK-01.
   - Confirm the test FAILS — captured stdout DOES contain
     "❌ LLM Error" and "ERROR root:" lines.
   - Document the failure output in the PR description.

4. Manual e2e (real terminal — required per the discipline
   bug-crewai-stdout-leak-investigation.md §5.10 raised):
   a. In a real terminal (not pty.fork), run `kolsch chat`
      against a misconfigured provider (e.g., point
      base_url at https://invalid.example.com:65535/v1
      to force connection failures).
   b. Send a message that triggers an exchange.
   c. Verify by EYE:
      - No `❌ LLM Error` panels.
      - No `ERROR root:` lines.
      - No `[CrewAIEventsBus]` warnings.
      - Kolsch's exchange_failed capsule appears once with
        the model.retry_exhausted shape (3 attempts).
   d. Use `script` (BSD/macOS) or `tee` to capture the
      terminal output to a file; grep for the four strings
      above; paste counts in the PR description (all should
      be zero except the kolsch capsule).

5. Document any follow-ups Step 1 surfaced (Windows path,
   per-exchange logging-disable safety in Phase 3
   multi-thread, etc.) as separate tickets.

Scope: ~120 LOC tests + catalog + manual report.

ESCALATION PATH: if the manual run STILL shows any of the
three leak signatures, Step 2 didn't fully land. Escalate
with the captured terminal output.

Acceptance: all three TCs pass; pre-fix verification
documented; real-terminal manual e2e shows ZERO leaks
across all four signatures.
```

### Reviewer prompt

```
[standing instructions]

Review Step 3.

Checks:

1. All three TCs exist and pass.

2. TC-CREWAI-LEAK-01 captures stdout/stderr at the FD level
   (subprocess.run), NOT via pytest's capsys. A capsys-based
   capture would let a broken implementation pass.

3. Pre-fix verification: PR shows TC-CREWAI-LEAK-01 FAILING
   on pre-fix code, with the captured output containing the
   leaked panels/ERROR lines. If the pre-fix verification is
   absent, fail.

4. Manual e2e in the PR description uses `script` or `tee`
   (real terminal capture), not pty.fork or scripted python.
   The discipline § from the prior crewai-leak investigation
   matters — pty-driven evidence is NOT the same as a real
   terminal.

5. The captured terminal file is grepped for the four
   signatures and counts pasted. The kolsch capsule count
   should be > 0 (exactly 1 per failed exchange); all other
   counts should be 0.

6. process/test-catalog.md gained the CREWAI-LEAK entries.

7. Full test suite green.

8. Follow-up tickets named in Step 1 are filed.

Verdict: PASS or FAIL.
```

---

## Part 5 — How to escalate

```markdown
### Escalation E-BUG-LEAK-<NNN> — <one-line summary>

- **Raised by:** developer / reviewer
- **Step:** 1–3
- **Trigger:** concrete observation; file:line if applicable.
- **Why this is not a guess:**
- **Options:** 2–3 paths.
- **Estimated cost:**

---

### Coordinator resolution — E-BUG-LEAK-<NNN>

- **Decided by:** human
- **Decision:**
- **Rationale:**
- **Resume instruction:**
```

When to escalate:
- Step 1 finds the FD-capture PoC fails in the dev environment.
- Step 1 finds `logging.disable(CRITICAL)` does not actually
  suppress ERROR-level in the current Python version.
- Step 2 finds tests cannot run subprocess-based FD captures
  in the CI environment.
- Step 3's manual e2e still shows leaks.

When NOT to escalate:
- The exact ordering of close/dup2 calls in the helper.
- Whether the helper lives in `src/engine/_output_capture.py`
  or `src/engine/utils/output_capture.py`. Pick based on
  existing layout.

---

## Part 6 — Coordinator Escalation Log

*(empty — populate as escalations land)*

---

## Part 7 — Out of scope (consolidated)

- **CrewAI singleton patching** (replacing the cached
  Console at module-import time). Brittle; FD-level is the
  locked choice.
- **A debug flag to show CrewAI panels.** Disable the wrap
  manually if you need them.
- **Phase 3 multi-thread `logging.disable` safety.** Process-wide
  disable is fine for v1's single-exchange chat; revisit
  when concurrent exchanges land.
- **Windows support.** macOS + Linux only. Windows would
  need an alternative FD strategy (msvcrt or
  os.set_handle_inheritable).
- **A general "suppress all third-party output" mechanism.**
  This fix is scoped to the CrewAI framework call boundary.
  Other suppression needs file their own tickets.

---

## Part 8 — Related work

- **Closes the gap left by:**
  `bug-crewai-stdout-leak-investigation.md` — that doc's G1
  (the `redirect_stdout` wrap) handles late-bound writes;
  this doc handles cached-reference writes. Together they
  cover the spectrum.
- **Triggered by:**
  `bug-platform-internal-error-boundary-swallow.md` Draft 0.3
  — the retry loop fix multiplied the per-exchange noise
  3x, making this latent issue visibly painful.
- **Same discipline as:** the crewai-leak investigation's
  §5.10 raised "manual real-terminal verification is
  mandatory." Step 3 honors that.
