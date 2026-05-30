# Your first Kolsch agent

A 15-minute tour. You'll define an agent in YAML, run it from the CLI,
inspect the events it emitted, resume the same session, stream it live,
rebuild the database from disk, serve it over HTTP, list the models
your providers expose, and add a fallback for transient provider blips.
By the end you'll have seen the five ideas Kolsch is built on, by
using them.

Steps 1–7 run with the **built-in fake LLM** (a scripted echo, no
credentials needed) so you can follow along offline. Steps 8 onward
connect a real provider; do them when you're ready.

## Before you start

You've built Kolsch (`uv sync` ran clean inside the dev container, or
on your host). Everything in this tutorial runs locally with SQLite and
the built-in fake LLM. No keys, no services.

```bash
uv run kolsch --help
```

If that prints the usage banner, you're ready.

## 0. First run

Run the one-time setup. It creates a workspace, walks an optional
provider connect, ships a starter agent, and confirms the install with
a real exchange.

```bash
uv run kolsch setup
```

The workspace lands at `~/Documents/kolsch` by default. If you want it
elsewhere, set `KOLSCH_WORKSPACE` once in your shell or pass
`--root <path>` per command — every command honours the same chain
(`--root` > `$KOLSCH_WORKSPACE` > `~/Documents/kolsch`).

**What setup produced for your starter agent depends on the provider
step:**

- **You connected a provider** — the starter agent uses that
  provider's first available model. Real answers from the start.
- **You skipped the provider step** — the starter agent ships with
  `provider: fake` (the built-in echo LLM). Every `kolsch run` then
  prints a clearly-labelled warning to stderr so you don't mistake
  the echoed input for a real reply. You can swap in a real model
  any time via `kolsch connect <provider>` + editing the agent's
  `model:` field.

The rest of this tutorial assumes the default workspace location. If
you skipped `kolsch setup` and want to know what it produced, read
step 1. Otherwise, skip to step 2.

---

## 1. What `kolsch setup` produced

`kolsch setup` materialises a workspace identical to what you'd get
by hand. Two files anchor it:

`platform.yaml` (the workspace's root config):
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
      - { who: ".*", what: ".*", effect: allow }
storage:
  entity_backend: sqlite
  entity_path: .platform/entities.db
event_bus:
  backend: in_process
file_access:
  allowed_paths: [ data/, bundles/ ]
  denied_paths:  [ .env ]
defaults: {}
```

`bundles/starter/agents/helper.yaml` (your first agent):
```yaml
schema_version: "0.1"
assembly_version: "1"
agent_id: ag_helper
name: helper
description: A friendly general-purpose helper agent.
model: { provider: fake, model: fake-model }
prompt: You are a helpful assistant.
io: { mode: conversational }
```

Plus the standard directory skeleton:
`bundles/ sessions/ content/ logs/ data/ .platform/` and an empty
`.env`.

That's the workspace. **The YAML is the source of truth — Kolsch has
nothing else to read.** The rest of the tutorial uses the
`starter/agents/helper` agent the setup command produced; substitute
`starter/agents/helper` (or any other ref) if you've gone off-script.

## 2. Run it

```bash
uv run kolsch run starter/agents/helper --input content="hello, kolsch"
```

You'll see one line of JSON: the agent's reply.

- **If the starter uses `provider: fake`**, you'll also see a stderr
  warning naming the fake LLM, and the reply will literally be
  `Echo: hello, kolsch`. That's intentional — the fake LLM lets
  Phase 1 run end-to-end with zero external calls.
- **If the starter uses a real provider**, you get a real model
  reply and no warning.

Either way, you just ran a real agent exchange end-to-end.

## 3. See what just happened on disk

```bash
ls sessions/
tail -n +1 sessions/*/threads/*.jsonl
```

Every step of the exchange — `thread.exchange_started`, `user.message`,
`model.request`, `model.response`, `agent.message`,
`thread.exchange_completed` — is a line of JSON, appended in order.
**This file is the ground truth.** The SQLite entity DB is an index
built from it.

## 4. Resume the same session

Grab the session id from the listing:

```bash
uv run kolsch sessions list
```

Then continue the conversation:

```bash
uv run kolsch run starter/agents/helper \
  --session ses_<paste-id> \
  --input content="say it again"
```

The new exchange lands in the same thread, appended after the first.
`kolsch sessions events ses_<id>` will show the full timeline.

## 5. Stream events as they happen

```bash
uv run kolsch run starter/agents/helper --input content="stream me" --stream
```

Same six events as before — but printed live as the engine emits them
instead of waiting for the final reply. This is the same stream the
HTTP layer exposes; the CLI just renders it to your terminal.

## 6. Replay: rebuild the database from the log

This is the magic trick:

```bash
rm .platform/entities.db
uv run kolsch sessions list   # empty — DB is gone
uv run kolsch reconstruct
uv run kolsch sessions list   # everything is back
```

The append-only JSONL files contained everything needed to rebuild the
entity index. **Lose the DB, keep the truth.**

## 7. Same agent, now over HTTP

```bash
uv run kolsch server --port 8080 &
curl -s http://127.0.0.1:8080/sessions | jq .
curl -s -X POST http://127.0.0.1:8080/agents/starter/agents/helper/run \
  -H 'content-type: application/json' \
  -d '{"inputs": {"content": "hello from http"}}' | jq .
```

The CLI and the server are two front doors to the same runtime, the
same YAML, the same event log.

## 8. Connect a real LLM provider

You'll connect **OpenRouter** because one connection gives you access
to most major models *and* enriches model metadata for any other
provider you connect later. (OpenCode works the same way — swap the
provider name if that's what you have.)

```bash
uv run kolsch connect openrouter
```

The CLI walks you through it:

```
OpenRouter uses API key authentication. The model catalog is public; the
key is only needed to route calls through OpenRouter. If you connect
OpenRouter, the platform will also use its catalog to look up model
parameters for other providers.

Enter API key: ****************************

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

Now check what you've got:

```bash
uv run kolsch providers
```

Two things happened: `platform.yaml` got an `llm_providers.openrouter`
block listing every discovered model with its context length and
pricing, and `.env` got `OPENROUTER_API_KEY`. The API key value is
stored *only* in `.env` — `platform.yaml` only references it by name.

## 9. Pick the model your agent uses

You need to know which models your provider offers before editing the
agent's `model:` field. List them:

```bash
uv run kolsch models
uv run kolsch models openrouter --filter haiku   # narrow long catalogs
uv run kolsch models --verbose                    # include context_length, pricing
```

The output is plain text by default and grouped by provider; each model
shows its id (what goes in `model.model`) and any matching alias (a
shareable shortcut you can use in place of the canonical
`provider/model` pair). Use `--json` if you're scripting this.

Now swap your agent over. Open
`bundles/starter/agents/helper.yaml` and change the `model:` line:

```yaml
# from (if setup left you on the fake LLM):
model: { provider: fake, model: fake-model }
# to one of:
model: { provider: openrouter, model: anthropic/claude-opus-4-7 }
# or — if you've defined an alias, e.g., via `kolsch connect`:
model: opus
```

Aliases mean your agent definitions don't have to know which provider
you're using. Either form works.

Run it:

```bash
uv run kolsch run starter/agents/helper --input content="explain kölsch in one sentence"
```

You'll get a real answer this time. Same agent, same session machinery,
same event log on disk — only the LLM behind it changed. The
fake-LLM stderr warning is gone.

> **Sidebar — the in-chat permission modal (Phase 2.0).** The helper
> agent ships with four canonical tools: `read_file`, `write_file`,
> `list_files`, and `bash`. The first two are silently allowed
> inside your workspace; the last two default to `prompt`, which
> means the chat surface (Phase 2.1) shows a four-choice modal
> before the tool runs:
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
> `[a]` and `[d]` write a new rule into `platform.yaml` so the
> next matching call takes the new path — within the same
> exchange, no restart. Out-of-chat surfaces (`kolsch run`,
> `kolsch server`) treat `prompt` as `deny` so unattended runs
> never hang; the deny is logged as `authz.prompt_unattended` so
> replay shows what would have been asked. To edit rules
> directly, use `kolsch permissions list/add/remove/show`. The
> recipes live in `docs/how-to/permissions.md`.

Useful follow-ups:

- `uv run kolsch refresh openrouter` — re-pull the catalog when new
  models drop.
- `uv run kolsch disconnect openrouter` — remove the provider; the
  agent then errors with `llm.provider_not_configured` on its next
  run — the system telling you the truth.

## 10. Chat with your agent

So far you've been sending one-shot messages with `kolsch run`.
The day-2 command is `kolsch chat` — a streaming REPL that turns
the same engine you've been using into a real conversation.

```bash
uv run kolsch chat kolsch/agents/helper
```

You drop into a prompt. Type a message and hit Enter; tokens
arrive live (no waiting for the whole reply), tool calls render
as collapsible blocks, and the same session log you've been
inspecting in step 3 keeps growing.

```
kolsch chat • helper • opus • ses_abc123

You: list the markdown files in docs/

  ▼ list_files(path="docs/", glob="**/*.md")
    [42 entries: README.md, index.md, explanation/..., ...]

Helper: There are 42 markdown files under `docs/`. The bulk live
under `explanation/` (the architecture specs) and `process/`
(testing handbook, test catalog, build plans)...

You: ▍
```

A few things to try inside the chat:

- **Tool approval modal.** Ask the helper to run `bash` (e.g.,
  "run `rg build_llm src/` and show me the matches"). Because
  `tool.bash` defaults to `prompt`, you get the four-choice
  modal — `[y]` allow once, `[a]` always allow this pattern,
  `[n]` deny once, `[d]` always deny. Picking `[a]` writes a
  rule into `platform.yaml` immediately; the next matching
  bash call inside the same chat runs without prompting.
- **Slash commands.** `/help` lists them; `/clear` wipes the
  visible scrollback without touching the session log;
  `/model haiku` switches model mid-session (the next exchange
  uses haiku); `/tools` shows what the agent can call;
  `/exit` (or Ctrl-D) leaves cleanly.
- **Resume a conversation.** Grab the session id from
  `kolsch sessions list`, then
  `kolsch chat kolsch/agents/helper --session ses_<id>` —
  the prior conversation replays into the scrollback and you
  continue where you left off. Same JSONL → same rendering;
  the renderer is a pure consumer of the event stream.

Everything you see in chat is also in
`kolsch sessions events <id>` — the chat is a view onto the
event log, not a parallel record. Crashed mid-stream? The
JSONL is intact; re-open the session and the conversation
comes back. Day-to-day recipes (long conversations,
switching models, approving bash patterns wisely) live in
`docs/how-to/chatting-effectively.md`.

## 11. Add a fallback model

Real providers blink — a 429 from Opus shouldn't kill your
conversation. Open `bundles/starter/agents/helper.yaml` and add one
field next to `model:`:

```yaml
model: opus
fallback_models:
  - haiku
```

(Add more entries if you want; the engine walks them in order.)
Run it again with Opus simulated unavailable — for instance, swap
the API key to an obviously-bad value temporarily, or pick a model
that's currently rate-limited:

```bash
uv run kolsch run starter/agents/helper --input content="say hi"
```

You'll see two new things in `kolsch sessions events`:

- A `model.fallback` event recording the transition
  (`from: opus, to: haiku, reason: model.rate_limited`).
- The successful `model.response` attributed to Haiku.

Each `model.fallback` is one transition, in order. If the whole
chain fails, the exchange ends with `llm.all_models_failed` and
the event's `detail.attempts` lists every model that was tried —
exactly the same information that's already in the log, surfaced
once so debuggers don't have to grep.

Fallback fires only before the first content token streams. After
the user has seen partial output, the engine commits to the
truncation rather than swap-and-replay — that's a UX decision,
not a retry one. Mid-stream fallback is a future patch when the
UX is solved.

The exhaustive trigger list (which errors fall back; which abort)
lives in `docs/explanation/error-model.md` §4.15. The how-to
guide is `docs/how-to/configure-fallbacks.md`.

---

## What you just saw

- **YAML is the source of truth** — you defined an agent and ran it.
- **Events are ground truth** — the JSONL on disk is the run.
- **Local-first by default** — SQLite + in-process bus + fake LLM,
  no services required to get started.
- **Same surface everywhere** — CLI, stream, HTTP all read from and
  append to the same log.
- **Providers are pluggable** — `kolsch connect` adds an LLM provider,
  aliases hide the provider choice from your agent files, and the
  agent definition doesn't change when the model behind it does.

## Where next

- Try a different provider: `uv run kolsch connect opencode`. The
  agent file doesn't change; just point an alias at the new model.
- `docs/how-to/find-models.md` — recipes for discovering model ids
  to put in agent YAML.
- `docs/how-to/configure-fallbacks.md` — chains beyond a single
  fallback model.
- `docs/explanation/llm-providers.md` — why providers are connected
  and not configured by hand, and how to add a new one.
- `docs/explanation/v1-scope.md` — what Phase 1 covers and what's
  deliberately out of scope.
- `docs/reference/module-api-reference.md` — every public contract.
- `docs/how-to/` — short recipes for specific tasks as they're added.
