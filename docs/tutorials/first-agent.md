# Your first Kolsch agent

A 15-minute tour. You'll define an agent in YAML, run it, inspect
its events, resume the session, stream events live, rebuild the
entity database from the log, serve the same agent over HTTP,
connect a real LLM provider, and add a fallback model.

Steps 1–7 run with the fake LLM — a scripted echo, no credentials
needed. Steps 8 onward connect a real provider. Do them when you're
ready.

## Before you start

Kolsch is built. `uv sync` ran clean inside the dev container or on
your host.

```bash
uv run kolsch --help
```

If that prints the usage banner, you're ready.

## 0. First run

Run the one-time setup. It creates a workspace, walks an optional
provider connect, ships a starter agent, and confirms the install
with a real exchange.

```bash
uv run kolsch setup
```

The workspace lands at `~/Documents/kolsch` by default. To put it
elsewhere, set `KOLSCH_WORKSPACE` in your shell or pass `--root <path>`
per command. Precedence: `--root` > `$KOLSCH_WORKSPACE` >
`~/Documents/kolsch`.

The starter agent's model depends on the provider step:

- **Provider connected** — the starter uses that provider's first
  available model. Real replies from step 2.
- **Provider skipped** — the workspace's `platform.yaml` gets
  `defaults.model: {provider: fake, model: fake-model}`. Every
  `kolsch run` prints a stderr warning so you don't mistake the
  echo for a model reply. Swap in a real model any time via
  `kolsch connect <provider>` plus an edit to
  `platform.yaml`'s `defaults.model`.

The rest of this tutorial assumes the default workspace location.
If you skipped `kolsch setup` and want to see what it produced,
read step 1. Otherwise skip to step 2.

## 1. What `kolsch setup` produced

Two files anchor the workspace.

`platform.yaml` — the workspace's root config:

```yaml
secrets:
  variant: dotenv
  config: { path: .env }
sandbox:
  variant: native
  config: {}
authorization:
  variant: rule_based
  config:
    rules:
      - { what: "*", pattern: "*", effect: allow }   # tutorial-open
storage:
  entity_backend: sqlite
  entity_path: .platform/entities.db
event_bus:
  backend: in_process
file_access:
  allowed_paths: [ data/, bundles/ ]
  denied_paths:  [ .env ]
defaults:
  model: { provider: fake, model: fake-model }
```

`bundles/kolsch/agents/helper.yaml` — your first agent:

```yaml
schema_version: "0.1"
assembly_version: "1"
agent_id: ag_helper
name: helper
description: A friendly general-purpose helper agent.
# model: intentionally omitted — resolved from platform.yaml's
# defaults.model via the kernel. Add a `model:` field here to
# override for this agent only.
prompt: You are a helpful assistant.
io: { mode: conversational }
```

Plus the standard skeleton: `bundles/`, `sessions/`, `content/`,
`logs/`, `data/`, `.platform/`, and an empty `.env`.

The YAML is the source of truth. Kolsch has nothing else to read.
The rest of the tutorial uses the `kolsch/helper` agent setup
produced; substitute another ref if you've gone off-script.

## 2. Run it

```bash
uv run kolsch run kolsch/helper --input content="hello, kolsch"
```

You'll see one line of JSON: the agent's reply.

- With the fake LLM, the reply is literally `Echo: hello, kolsch`
  and there's a stderr warning naming the fake LLM.
- With a real provider, you get a real reply and no warning.

## 3. See what happened on disk

```bash
ls sessions/
tail -n +1 sessions/*/threads/*.jsonl
```

Every step of the exchange — `thread.exchange_started`,
`user.message`, `model.request`, `model.response`, `agent.message`,
`thread.exchange_completed` — is a line of JSON, appended in order.
The JSONL is ground truth. The SQLite entity DB is an index built
from it.

## 4. Resume the session

Grab the session id:

```bash
uv run kolsch sessions list
```

Continue the conversation:

```bash
uv run kolsch run kolsch/helper \
  --session ses_<paste-id> \
  --input content="say it again"
```

The new exchange lands in the same thread, appended after the
first. `kolsch sessions events ses_<id>` shows the full timeline.

## 5. Stream events

```bash
uv run kolsch run kolsch/helper --input content="stream me" --stream
```

Same events as before — printed live as the Agent Engine produces
them instead of waiting for the final reply. The HTTP layer streams
the same events; the CLI just renders them to your terminal.

## 6. Rebuild the database from the log

```bash
rm .platform/entities.db
uv run kolsch sessions list   # empty — the DB is gone
uv run kolsch reconstruct
uv run kolsch sessions list   # back
```

The JSONL files contained everything needed to rebuild the entity
index. Losing the DB doesn't lose the truth.

## 7. Serve it over HTTP

```bash
uv run kolsch server --port 8080 &
curl -s http://127.0.0.1:8080/sessions | jq .
curl -s -X POST http://127.0.0.1:8080/agents/kolsch/helper/execute \
  -H 'content-type: application/json' \
  -d '{"inputs": {"content": "hello from http"}}' | jq .
```

The CLI and the server share one runtime, one YAML tree, one event
log.

## 8. Connect a real LLM provider

Connect OpenRouter. One connection covers most major models and
provides model metadata for any other provider you connect later.
OpenCode works the same way — swap the provider name if that's what
you have.

```bash
uv run kolsch connect openrouter
```

The CLI walks you through it:

```
OpenRouter uses API key authentication. Enter API key: ****************

Validating credentials...
✓ Credits remaining: 1,247

Discovering models...
✓ Fetched 218 models from OpenRouter catalog.

Suggest aliases?
  opus    → openrouter/anthropic/claude-opus-4-7
  haiku   → openrouter/anthropic/claude-haiku-4-5
  gpt4o   → openrouter/openai/gpt-4o
Add suggested aliases? [Y/n]
```

Check what you've got:

```bash
uv run kolsch providers
```

Two things happened. `platform.yaml` picked up an
`llm_providers.openrouter` block listing every discovered model with
its context length and pricing. `.env` picked up
`OPENROUTER_API_KEY`. The key value lives only in `.env`;
`platform.yaml` references it by name.

## 9. Pick the model your agent uses

List available models:

```bash
uv run kolsch models
uv run kolsch models openrouter --filter haiku
uv run kolsch models --verbose                    # context_length, pricing
```

Output is grouped by provider; each model shows its id (what goes
in `model.model`) and any matching alias. Use `--json` for
scripting.

Swap your agent over. The cleanest lever is `platform.yaml`'s
`defaults.model` — change it once and every agent that doesn't pin
its own model picks up the new value:

```yaml
# in platform.yaml:
defaults:
  # from (if setup left you on the fake LLM):
  # model: { provider: fake, model: fake-model }
  model: { provider: openrouter, model: anthropic/claude-opus-4-7 }
  # or via an alias:
  # model: opus
```

To switch only the helper (leaving other agents alone), add a
`model:` field to `bundles/kolsch/agents/helper.yaml` — the
agent-level `model:` overrides `defaults.model` for that agent.

Run it:

```bash
uv run kolsch run kolsch/helper --input content="explain kölsch in one sentence"
```

Real answer this time. Same agent, same session machinery, same
event log — only the model changed.

> **Permission prompts in chat.** The helper ships with four canonical
> tools: `read_file`, `write_file`, `list_files`, and `bash`. The
> first two are silently allowed inside your workspace; the last
> two default to `prompt`, which shows a four-choice modal before
> the tool runs:
>
> ```
> ┌─ Permission requested ─────────────────────────────────────┐
> │ Tool:    bash                                              │
> │ Command: rg "build_llm" src/                               │
> │                                                            │
> │ [y] allow once   [a] always allow this pattern             │
> │ [n] deny once    [d] deny always                           │
> └────────────────────────────────────────────────────────────┘
> ```
>
> `[a]` and `[d]` write a new rule to `platform.yaml` so the next
> matching call takes the new path within the same exchange.
> Non-chat surfaces (`kolsch run`, `kolsch server`) treat `prompt`
> as `deny` so unattended runs never hang. The deny is logged as
> `authz.prompt_unattended` so replay shows what would have been
> asked. Recipes live in
> [Permissions](../how-to/permissions.md).

Useful follow-ups:

- `kolsch refresh openrouter` — re-pull the catalog when new
  models drop.
- `kolsch disconnect openrouter` — remove the provider; the agent
  then errors with `llm.provider_not_configured` on its next run.

## 10. Chat with your agent

`kolsch run` sends one-shot messages. `kolsch chat` is the streaming
REPL — the same Agent Engine turned into a real conversation.

```bash
uv run kolsch chat kolsch/helper
```

You drop into a prompt. Type a message and hit Enter; tokens arrive
live, tool calls render as collapsible blocks, and the same session
log you inspected in step 3 keeps growing.

```
kolsch chat • helper • opus • ses_abc123

You: list the markdown files in docs/

  ▼ list_files(path="docs/", glob="**/*.md")
    [42 entries: README.md, index.md, explanation/..., ...]

Helper: There are 42 markdown files under `docs/`. The bulk live
under `explanation/` and `how-to/` ...

You: ▍
```

Try a few things:

- **Tool approval.** Ask the helper to run `bash` (e.g. "run
  `rg build_llm src/` and show me the matches"). The four-choice
  modal appears because `kolsch/tools/bash` defaults to `prompt`.
- **Slash commands.** `/help` lists them; `/clear` wipes visible
  scrollback without touching the log; `/model haiku` switches
  model mid-session; `/tools` shows what the agent can call;
  `/exit` (or Ctrl-D) leaves cleanly.
- **Resume.** Get a session id from `kolsch sessions list`, then
  `kolsch chat kolsch/helper --session ses_<id>`. The prior
  conversation replays into the scrollback and you continue where
  you left off.

Everything you see in chat is also in `kolsch sessions events <id>`.
The chat is a view onto the event log, not a parallel record. Day-to-
day recipes live in
[Chatting effectively](../how-to/chatting-effectively.md).

## 11. Add a fallback model

Providers fail sometimes. A 429 from Opus shouldn't end your
conversation. Open `bundles/kolsch/agents/helper.yaml` and add an
explicit `model:` plus a `fallback_models:` list:

```yaml
model: opus
fallback_models:
  - haiku
```

Run it again with Opus simulated unavailable (swap the API key to
a bad value temporarily, or pick a rate-limited model):

```bash
uv run kolsch run kolsch/helper --input content="say hi"
```

Two new events in `kolsch sessions events`:

- A `model.fallback` recording the transition
  (`from: opus, to: haiku, reason: model.rate_limited`).
- The successful `model.response` attributed to Haiku.

Each `model.fallback` is one transition, in order. If the whole
chain fails, the exchange ends with `llm.all_models_failed` and the
event's `detail.attempts` lists every model that was tried.

Fallback fires only before the first content token streams. After
the user has seen partial output, the Agent Engine commits to the
truncation rather than swap-and-replay. Mid-stream fallback is a
future patch.

Full recipes are in
[Configure fallbacks](../how-to/configure-fallbacks.md).

## Where next

- Try a different provider: `kolsch connect opencode`. The agent
  file doesn't change; point an alias at the new model.
- [Architecture overview](../architecture/index.md) — the system
  map.
- [Agent Engine](../architecture/engine.md) — what runs when you
  call `kolsch run`.
- [Store](../architecture/store.md) — how the JSONL and SQLite fit
  together.
- [Find models](../how-to/find-models.md) — discovering model ids
  for agent YAML.
- [Permissions](../how-to/permissions.md) — the four-choice modal,
  rule shape, layer stack.
- [Chatting effectively](../how-to/chatting-effectively.md) — long
  conversations, slash commands, session resume.
- [Configure fallbacks](../how-to/configure-fallbacks.md) — chains
  beyond a single fallback.
