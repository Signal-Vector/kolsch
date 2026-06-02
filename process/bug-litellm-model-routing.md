# Bug — LiteLLM rejects `openai/<non-openai-model>` from the OpenCode connector

**Draft 0.1** *(investigation ticket, not a phase patch)*

> Reported symptom: a user with OpenCode connected tries to run an
> agent against a Qwen model (`qwen3.6-plus`) and CrewAI/LiteLLM
> rejects the model string `openai/qwen3.6-plus`, because LiteLLM's
> native `openai/` provider only recognizes OpenAI-canonical model
> names. The OpenCode connector in `src/llm/providers/opencode.py`
> hard-codes the `openai/` prefix per Phase 1.1 §5.1, on the
> assumption that "OpenAI-compatible" providers can ride the
> `openai/` route. They cannot, for non-canonical model names,
> without LiteLLM installed and configured to skip native-provider
> validation.
>
> This document is for the developer agent (to investigate and fix)
> and the reviewer agent (to verify root cause, minimal fix, no
> regression of the working fake/openrouter paths, and a regression
> test).

---

## Part 0 — Symptom & Reproduction

**Reported by:** user, on `main`.

**Setup:**
1. `kolsch connect opencode` succeeded against an OpenCode endpoint
   serving Qwen models.
2. `bundles/starter/agents/helper.yaml` resolves to model
   `qwen3.6-plus` on provider `opencode` (either via an alias or
   the compact string `opencode/qwen3.6-plus`).

**Action:**
```bash
uv run kolsch run starter/agents/helper --input content="2+2"
```

**Actual output (verbatim from the report):**
```
ERROR crewai.llm: Unable to initialize LLM with model
'openai/qwen3.6-plus'. The model did not match any supported native
provider (openai, anthropic, claude, azure, azure_openai, google,
gemini, bedrock, aws, openrouter, deepseek, ollama, ollama_chat,
hosted_vllm, cerebras, dashscope), and the LiteLLM fallback package
is not installed.

To fix this, either:
  1. Install LiteLLM for broad model support: uv add 'crewai[litellm]'
or
pip install litellm

For more details, see: https://docs.crewai.com/en/learn/llm-connections
ERROR cli: agent execution failed: Agent execution failed at event
ev_4ca2bc00-da4b-48ce-bf49-47ef7faf5162
Traceback (most recent call last):
  File ".../src/cli/main.py", line 346, in _cmd_run
    agent_msg = await runtime.engine.execute(config, ctx)
  File ".../src/engine/engine.py", line 206, in execute
    raise AgentExecutionError(failed_event)
engine.context.AgentExecutionError: Agent execution failed at event
ev_4ca2bc00-da4b-48ce-bf49-47ef7faf5162
```

**Two observable signals in that output:**

1. The model string passed to CrewAI is `openai/qwen3.6-plus` —
   that's the OpenCode connector's `f"openai/{model_id}"` line
   (Phase 1.1 §5.1, copied into `src/llm/providers/opencode.py`).
2. CrewAI's native-provider check fails. CrewAI/LiteLLM's `openai/`
   provider validates the model name against the canonical OpenAI
   model list (`gpt-4*`, `gpt-3.5-*`, etc.). Qwen models are not
   in that list. Without `litellm` installed as a CrewAI extra,
   there is no fallback routing.

---

## Part 1 — Hypotheses (ranked by likelihood)

### H1. The `openai/` prefix is the wrong route for non-canonical models on OpenAI-compatible endpoints

The OpenCode connector's `build_llm` uses `f"openai/{model_id}"`
unconditionally. LiteLLM/CrewAI interprets `openai/<x>` as "ask
OpenAI for model x," then validates `x` against OpenAI's known
list. The correct route for a non-OpenAI model served via an
OpenAI-compatible endpoint is one of:

- `hosted_vllm/<model>` (vLLM-backed OpenAI-compat endpoints).
- `openai/<model>` *with* `litellm` installed AND the connector
  passing `custom_llm_provider="openai"` (LiteLLM then skips its
  native model-name validation).
- The provider-native LiteLLM prefix when one exists (e.g.,
  `dashscope/qwen-…` for Alibaba's actual API, but that's not
  OpenCode).

The correct fix depends on what OpenCode's wire shape actually is.
If it's a vanilla OpenAI-compat shim, `hosted_vllm/<model>` is the
right prefix for arbitrary model names. If we want to keep
`openai/<model>`, we must add `litellm` to deps AND pass
`custom_llm_provider="openai"` to bypass model-name validation.

### H2. The `litellm` extra is simply not in dependencies

CrewAI's error message explicitly says "the LiteLLM fallback
package is not installed." If `pyproject.toml` doesn't include
`crewai[litellm]` (or a direct `litellm` dep), the fallback path
that allows arbitrary OpenAI-compatible model names is unavailable.

Two-part fix possible: add the dep AND change the connector to use
a route LiteLLM accepts.

### H3. OpenCode discovers model IDs in a form LiteLLM doesn't normalize

OpenCode's `/v1/models` returned `qwen3.6-plus`. If the canonical
LiteLLM/Dashscope model ID is `qwen-3.6-plus` or `qwen3-plus`, the
literal discovered ID is the issue. Less likely than H1/H2 (the
error message names exactly the string the connector built, so
this is downstream) but worth confirming — the connector should
not be mutating discovered IDs.

### H4. The OpenAI-compatible route works for OpenRouter but not OpenCode because OpenRouter has a dedicated LiteLLM provider

OpenRouter's connector uses `f"openrouter/{model_id}"` (Phase 1.1
§6.1) which hits LiteLLM's native `openrouter/` provider — and
`openrouter/` accepts arbitrary model strings because OpenRouter
itself routes them. So the OpenRouter path works for the same
reason OpenCode's doesn't: dedicated provider vs. validating
provider. The fix needs to NOT regress the OpenRouter case.

---

## Part 2 — Investigation Strategy

Observational first; do not modify code yet.

### Step A — Confirm the connector is the source of the bad string

```bash
grep -n 'openai/' src/llm/providers/opencode.py
```

Confirm the `f"openai/{model_id}"` line exists at the
documented `build_llm` site. Note any surrounding kwargs
(`api_key`, `base_url`, `timeout`, `custom_llm_provider`?).

### Step B — Inspect `pyproject.toml` for the LiteLLM extra

```bash
grep -nE 'crewai|litellm' pyproject.toml uv.lock | head -40
```

What's pinned? Is `crewai[litellm]` declared? Is `litellm`
pulled in as a transitive dep? If neither, H2 is at minimum a
contributing factor.

### Step C — Reproduce against the fake-LLM path, then against the OpenRouter path

Run the same agent against:
1. `model: {provider: fake, model: fake-model}` — does it
   complete? (Expected: yes, with echo; per Phase 1 contract.)
2. `model: openrouter/anthropic/claude-haiku-4-5` (if a
   reviewer has OpenRouter credentials or a fixture mock) —
   does it complete? (Expected: yes; OpenRouter has its own
   LiteLLM provider that accepts arbitrary models.)
3. `model: opencode/qwen3.6-plus` — fails as reported.

This confirms the failure is specific to OpenCode (and any
other connector that uses the `openai/` prefix for non-canonical
model names).

### Step D — Check what LiteLLM/CrewAI version is pinned and what its `openai/` provider validates against

From Phase 1.1 Open Item 3 (CrewAI version pinning):

> The exact CrewAI version pinned in Step 12b determines the
> exact `LLM` constructor signature; the `build_llm` helper and
> connectors must match.

```bash
grep -nE 'crewai' pyproject.toml
uv pip show crewai | head -10
uv pip show litellm | head -10   # may be "not installed"
```

Then read whichever CrewAI/LiteLLM is installed to confirm the
behavior: does `openai/<x>` validate `<x>` against a fixed list,
or accept any string?

### Step E — Read the working connector (OpenRouter) for contrast

```bash
sed -n '1,80p' src/llm/providers/openrouter.py
```

Confirm OpenRouter uses `openrouter/<model_id>` and that this
route accepts arbitrary model strings. This identifies the
asymmetry: OpenRouter has a dedicated LiteLLM provider that
delegates routing; OpenCode rides `openai/` which doesn't.

### Decision criteria

- If Step A confirms the `openai/<model>` string AND Step B shows
  `litellm` not in deps AND Step D confirms the validation
  behavior → **H1 + H2 are both true.** Fix is "add `litellm` extra
  + change the connector to a route that accepts arbitrary models."
- If Step B shows `litellm` IS in deps but the error still
  surfaces → **H1 alone.** Fix is purely the connector route.
- If Step C #2 (OpenRouter) also fails → there's a broader issue;
  scope grows; surface to coordinator.
- If `qwen3.6-plus` turns out to be a hallucinated model ID from
  OpenCode's discovery (verify by hitting `/v1/models` against the
  same endpoint with `curl`), H3 is a contributing factor and the
  user may need a different model — but the core bug remains H1.

---

## Part 3 — Acceptance Criteria

The bug is fixed when **all** of the following hold:

1. A user with a connected OpenCode provider can run an agent
   whose `model:` resolves to any OpenCode-served model and get a
   real LLM response — not a LiteLLM "model did not match any
   supported native provider" error.
2. The OpenRouter path (Phase 1.1 §6 working today) continues to
   work unchanged.
3. The fake-LLM path (`provider: fake`) continues to work unchanged.
4. The connector's choice of LiteLLM route is documented in the
   connector module's docstring (per Phase 1.3 §FB-R10 spirit:
   every routing choice is declared, not implicit).
5. A regression test in `tests/component/llm/providers/test_opencode.py`
   (or equivalent) constructs an `OpenCodeConnector`, calls
   `build_llm("qwen3.6-plus", …)`, and asserts the resulting
   `crewai.LLM`'s `model` field is the route that actually works
   (verified by mocking LiteLLM's resolution layer if needed —
   no real network).
6. If the fix adds `litellm` as a dependency, `pyproject.toml`
   and `uv.lock` are updated and `uv sync` runs cleanly.
7. No existing test in `tests/unit`, `tests/component`,
   `tests/integration`, or `tests/golden` regresses.

---

## Part 4 — Out of Scope

- **Reworking the connector Protocol.** The Protocol from Phase
  1.1 §3 is fine. The bug is in one connector's `build_llm` body.
- **Adding new providers** (Anthropic, OpenAI, Bedrock,
  Llama-local). Those are planned per Phase 1.1 §9 and may share
  this routing choice — note any cross-cutting learning, but
  don't pre-implement.
- **Phase 1.2 / 1.3 work.** Spec'd but not implemented per their
  build plans.
- **Model-name normalization.** If OpenCode discovery returns
  `qwen3.6-plus` and the "real" Qwen ID is `qwen-3.6-plus`, that's
  a user/provider problem, not the connector's. The connector
  passes discovered IDs through unchanged (Phase 1.1 §5.1).

---

## Part 5 — Developer Prompt

```
[standing instructions]

You are investigating a bug: running an agent against an
OpenCode-served model produces a CrewAI/LiteLLM error
"Unable to initialize LLM with model 'openai/<model>'. The
model did not match any supported native provider…".

Read first:
- process/bug-litellm-model-routing.md (this ticket — the
  hypotheses, investigation strategy, acceptance criteria).
- process/phase1.1-llm-specs.md §5.1 (the OpenCode connector
  spec) and §6.1 (OpenRouter, for contrast).
- src/llm/providers/opencode.py and src/llm/providers/
  openrouter.py (the code).
- pyproject.toml (deps; confirm whether `crewai[litellm]` or
  direct `litellm` is present).

WORK IN TWO PHASES — investigation, then fix.

Phase A — Investigation (no code changes yet).

1. Follow §2 of the ticket steps A through E. Record what you
   found for each.

2. Verify against installed CrewAI/LiteLLM. The error text
   explicitly lists the supported native providers; confirm that
   list against the installed CrewAI version. If CrewAI is
   pinned at a version where `openai/<arbitrary>` validation is
   STRICT, H1 is confirmed.

3. Decide on the right route for OpenCode. Options (pick one,
   justify):
     (a) `hosted_vllm/<model>` — accepts arbitrary models with
         base_url; common for OpenAI-compatible self-hosted
         endpoints.
     (b) `openai/<model>` + install `litellm` + pass
         `custom_llm_provider="openai"` to bypass validation.
     (c) A provider-specific route if LiteLLM gains one.
   Justify the choice against the OpenCode wire shape (it IS
   OpenAI-compatible per Phase 1.1 §5; you're picking the route
   that uses that compatibility correctly).

4. Produce a root-cause writeup: which hypotheses (H1–H4) hold
   alone or combined, with file/line citations, a one-paragraph
   explanation of how the bug manifests, and a one-paragraph
   rationale for the chosen fix route.

5. STOP. Surface the writeup before writing any fix.

Phase B — Fix (only after Phase A is reviewed or explicit "go"
from the coordinator).

6. Make the smallest possible change:
   - If H2 is part of the diagnosis: add `crewai[litellm]` (or
     direct `litellm`) to pyproject.toml; update uv.lock.
   - Change `src/llm/providers/opencode.py`'s `build_llm` to the
     chosen route. Update the module docstring with the
     routing-choice rationale (acceptance criterion 4).
   - If the chosen route requires additional kwargs (e.g.,
     `custom_llm_provider`, `api_base` instead of `base_url`),
     add them; document them inline.

7. Update the connector test (TC-LLM-05 expected `crewai.LLM`
   construction kwargs) to reflect the new route.

8. Add a regression test that asserts the BUG specifically:
   construct an `OpenCodeConnector`, call
   `build_llm("qwen3.6-plus", provider_config, api_key)`, and
   assert the returned `crewai.LLM`'s `model` field uses the
   chosen route. If your route is `hosted_vllm/<model>`, the
   model field must be exactly that. The test must FAIL on the
   pre-fix `openai/<model>` code.

9. Sanity-check the OpenRouter path is untouched: run the
   existing OpenRouter component tests. They must pass byte-for-
   byte (the diff should not touch openrouter.py except for
   docstring clarification if the routing-rationale doc pattern
   from acceptance criterion 4 is applied symmetrically — but
   that's optional and you should declare it if you do it).

10. Run the full test suite. Acceptance criterion 7 — zero
    regressions.

Commit message:
  fix(llm): route OpenCode build_llm via <chosen-route> for
    arbitrary LiteLLM models

Body: the root-cause paragraph and the routing rationale. Cite
this ticket. Note any change to pyproject.toml deps so reviewer
knows to look there.

Report:
- The root-cause writeup from Phase A.
- The chosen route and why.
- The connector + dep changes (file + line).
- The regression test you added and what it asserts.
- Test results (suites run, pass/fail).
- Any cross-cutting note for future connectors (Anthropic,
  Bedrock, Llama-local) about routing choice.
```

---

## Part 6 — Reviewer Prompt

```
[standing instructions]

Review the OpenCode-LiteLLM routing fix.

Read first:
- process/bug-litellm-model-routing.md (the ticket — Part 3
  acceptance criteria are your checklist).
- The developer's root-cause writeup in the PR description.
- process/phase1.1-llm-specs.md §5.1 (OpenCode spec) and
  §6.1 (OpenRouter spec, for asymmetry context).
- The diff.

Architectural / correctness review:

1. The root-cause writeup names specific hypotheses (H1–H4) with
   file/line citations. If "the connector route is wrong" is
   asserted without a citation of WHY LiteLLM rejects the old
   route (CrewAI version + LiteLLM provider list), fail and
   request the citation.

2. The chosen route makes sense for OpenAI-compatible non-
   canonical-model endpoints. The three candidate routes from
   §2 Step E of the ticket: hosted_vllm/, openai/+litellm+
   custom_llm_provider, or provider-specific. Whichever the
   developer chose, the rationale in the connector docstring
   must explain it — not just "this works."

3. Minimality. The diff should touch:
     - src/llm/providers/opencode.py (the route change +
       docstring update)
     - pyproject.toml + uv.lock (if a dep was added)
     - tests/component/llm/providers/test_opencode.py (route
       assertion update + regression test)
   Anything beyond is over-reach. Flag it.

4. The connector module docstring now documents the routing
   choice. Acceptance criterion 4. If the developer skipped the
   docstring update, fail and request it — undocumented routing
   choices are how the next contributor reintroduces this bug.

5. The OpenRouter connector is unchanged (or changed ONLY for
   parallel docstring clarity per the developer's note). Diff
   openrouter.py: if there's a functional change, the developer
   must justify it; otherwise it should be docstring-only or
   empty.

Functional review:

6. Reproduce the original failure: git stash the fix, run the
   new regression test, confirm it FAILS with an assertion that
   matches the bug (i.e., the route was `openai/qwen3.6-plus`,
   not the new route). Restore the fix, confirm PASS.

7. The fake-LLM path. Run an existing fake-LLM component test;
   confirm it still passes unchanged. Acceptance criterion 3.

8. The OpenRouter path. Run the existing OpenRouter component
   tests; confirm they pass unchanged. Acceptance criterion 2.

9. Dep change verification. If pyproject.toml gained
   `crewai[litellm]` (or direct `litellm`):
     - uv.lock must be regenerated and committed
     - `uv sync` against the new lock must succeed
     - the previously-failing CLI invocation must succeed:
         uv run kolsch run starter/agents/helper \
           --input content="hello"
       (against an OpenCode-mocked or real endpoint;
       use the fixture HTTP layer for the committed test).

10. Run the FULL test suite. Acceptance criterion 7.

11. End-to-end smoke. Mock OpenCode's /v1/chat/completions to
    return a distinctive string ("DISTINCTIVE_REPLY_42"). Run
    the agent; confirm the response is the distinctive string,
    not an error and not an echo.

Forward-looking review:

12. The developer's "cross-cutting note" for future connectors
    (Anthropic, OpenAI, Bedrock, Llama-local — Phase 1.1 §9).
    Read it. Does the routing-choice insight apply to any of
    them? If yes, the note should land in
    src/llm/providers/README.md so the next contributor learns
    from this bug. Flag if missing.

Verdict: PASS or FAIL. On FAIL, name the acceptance criterion
violated and the specific defect (file + line, missing assertion,
undocumented routing choice). On PASS, summarize the chosen route
in one sentence and confirm the regression test reproduces the
original failure on pre-fix code.
```

---

## Part 7 — Notes on Running This Investigation

- **Don't burn tokens.** The regression test uses the fixture
  HTTP layer to mock the OpenCode endpoint. Real-network
  verification against the user's actual OpenCode endpoint is
  the developer's optional manual step, not the committed
  suite.
- **If H2 alone fixes it without a route change**, that's the
  smallest possible fix — but verify on a clean install that
  adding `litellm` alone routes `openai/qwen3.6-plus` correctly
  without throwing the "did not match native provider" error.
  Recent LiteLLM versions still validate `openai/<x>` strictly
  unless you pass `custom_llm_provider`; confirm against the
  installed version, don't trust the error message's "or
  install LiteLLM" guidance blindly.
- **Document the routing choice for every connector going
  forward.** This bug is a teaching moment for the contributor
  guide. Phase 1.1 §7's "checklist" question
  *How do we construct a `crewai.LLM` for this provider's
  models?* should now have a sub-bullet: *and which LiteLLM
  route does that produce, with what validation semantics?*
- **Future-proofing.** If a future connector (e.g., Llama-local
  via vLLM, per Phase 1.1 §9.4) lands using `hosted_vllm/`, this
  fix may have established the pattern. The cross-cutting note
  is how that pattern gets shared.
```
