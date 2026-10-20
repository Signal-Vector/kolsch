# Use Jev for decisions

**Jev** is TypeSafe AI's "System One" decision model. It is not a chat
model: you send it some content (the `state`) and one or more named,
typed questions, and it returns typed answers with calibrated
probabilities, usually in 100–500 ms. Use it for bounded judgements —
classify, route, rate, or yes/no — where you want a fast, cheap,
structured answer instead of parsing a chat model's prose. Every call
lands in the session's JSONL event log as an ordinary tool call, so
decisions can be audited and replayed.

Kolsch ships Jev as the `kolsch/tools/jev_decide` tool.

## 1. Connect OpenRouter

Jev is reached through OpenRouter in this version, using the same key
as the OpenRouter LLM provider. If you have already run
`kolsch connect openrouter`, there is nothing to do: the key is in your
workspace's `.env` as `OPENROUTER_API_KEY`.

```bash
kolsch connect openrouter
```

See [Connect a provider](connect-a-provider.md) for the details. There
is no separate Jev key and no `platform.yaml` section for it.

Until the key is present, `jev_decide` is **hidden** rather than an
error: agents that declare it keep working, the model never sees the
tool, and the session log records one `tool.unavailable` event with
the hint "Run `kolsch connect openrouter`." (The tool opts into
`hide_if_secrets_missing`; see
[Write a tool](write-a-tool.md#optional-integrations-hide_if_secrets_missing).)

## 2. Use it from chat

The starter `helper` agent declares `jev_decide`. Once OpenRouter is
connected, just ask in plain language — you don't need to name the
tool:

```text
classify this ticket as billing, outage or feature request, with
confidence: "the site is returning 502s"
```

The helper calls `jev_decide` with a `choice` question, then answers
with the chosen option and its confidence. The call shows up in chat
as an ordinary tool-call block (approve it, or allow the rule — see
below). The helper's prompt only mentions Jev when the tool is
available (`{% if "jev_decide" in tools %}`), so a workspace
without OpenRouter sees no reference to it.

## 3. Give your own agent the tool

Declare the tool in the agent's `subsystems.tools.config.declared`
list:

```yaml
subsystems:
  tools:
    variant: standard
    config:
      declared:
        - ref: kolsch/tools/jev_decide
```

New workspaces include a default authorization rule that **prompts**
before each `jev_decide` call, because the tool sends workspace content
to a third party:

```yaml
- { what: kolsch/tools/jev_decide, pattern: "*", effect: prompt }
```

Change it to `allow` with `/perm` or `kolsch permissions` if you trust
the agent with it.

## 4. Ask questions

The tool takes three arguments:

- `state` — the content every question refers to: a string, or a JSON
  object or array.
- `questions` — one or more named questions. Batch related questions
  into one call; that is cheaper and faster than several calls.
- `model` — optional; defaults to the pinned `typesafe/jev-1.14`.

Each question has a `type`, `instructions`, and type-specific
`criteria`:

| Type | `criteria` | Answer |
|---|---|---|
| `choice` | map of 1–255 option names to a description (or `null`) | `{type, choice, confidence, probabilities}` |
| `score` | ordered list of 1–10 level descriptions, lowest first | `{type, score, confidence, legend, probabilities}` — `score` is the 0-based expected level and may be fractional |
| `noul` | optional `{true: ..., false: ...}` | `{type, noul}` — the probability of yes |

An example call, as the model would make it:

```json
{
  "state": "I was charged twice for my subscription this month.",
  "questions": {
    "team": {
      "type": "choice",
      "instructions": "Which team should handle this ticket?",
      "criteria": {
        "billing": "Charges, refunds, invoices",
        "outage": "The service is down or erroring",
        "feature": "A request for new functionality"
      }
    },
    "urgency": {
      "type": "score",
      "instructions": "How urgent is this ticket?",
      "criteria": ["Can wait", "This week", "Today"]
    },
    "refund_requested": {
      "type": "noul",
      "instructions": "Is the customer asking for a refund?"
    }
  }
}
```

The tool returns the dated model id that actually answered, the answers
keyed by question name, and token usage (`usage.cost` in USD when
OpenRouter reports it):

```json
{
  "model": "typesafe/jev-1.14-20261014",
  "answers": {
    "team": {"type": "choice", "choice": "billing", "confidence": 0.93,
             "probabilities": {"billing": 0.93, "outage": 0.02, "feature": 0.05}},
    "urgency": {"type": "score", "score": 1.4, "confidence": 0.71, "...": "..."},
    "refund_requested": {"type": "noul", "noul": 0.81}
  },
  "usage": {"input_tokens": 212, "output_tokens": 30, "cost": 0.0000089}
}
```

The `box` question type and image inputs are not supported.

## 5. Fail on low certainty with `fail_below`

Add `fail_below` to any question to turn a low-certainty answer into a
failure. The tool compares one metric per question type:

| Type | Metric compared | Range |
|---|---|---|
| `choice` | `confidence` | 0–1 |
| `score` | `score` | ≥ 0 |
| `noul` | `noul` | 0–1 |

```json
"team": {
  "type": "choice",
  "instructions": "Which team should handle this ticket?",
  "criteria": {"billing": null, "outage": null, "feature": null},
  "fail_below": 0.6
}
```

If any metric is **strictly** below its `fail_below` (equal passes),
the call fails with `tool.jev_below_threshold`. The error lists each
failing question (`question`, `metric`, `value`, `threshold`) and still
carries the full `answers`. In chat, the model sees the failure as a
tool result and can ask the user or fall back. There is no default
threshold: without `fail_below`, the tool never fails on answer values.

## Errors

| Code | When |
|---|---|
| `tool.invalid_arguments` | Malformed questions (unknown or `box` type, empty `questions`, too many options or levels, `fail_below` out of range). Nothing is sent. |
| `tool.unavailable` (event, not an error) | `OPENROUTER_API_KEY` is not in `.env`, so the tool is hidden. Run `kolsch connect openrouter`. |
| `secret.not_found` | `OPENROUTER_API_KEY` is in `.env` but empty: the tool registers (the name exists), then the call fails. Run `kolsch connect openrouter`. |
| `tool.execution_failed` | The upstream answered with an error (`UpstreamHTTP<status>`; 401/403 carry the recovery hint `kolsch connect openrouter`, 400/413/415/422 carry the upstream validation message), a network error, or a malformed response. 429 and 529 are retried up to 3 attempts first. |
| `tool.timeout` | A request took longer than 30 seconds. |
| `tool.jev_below_threshold` | A `fail_below` was breached. |

See the [error model](../explanation/error-model.md) and
[Tools subsystem §4.5.7](../explanation/tools-subsystem.md) for the
contracts.
