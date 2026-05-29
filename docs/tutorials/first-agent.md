# Your first Kolsch agent

A 15-minute tour. You'll define an agent in YAML, run it from the CLI,
inspect the events it emitted, resume the same session, stream it live,
rebuild the database from disk, serve it over HTTP, and then connect a
real LLM provider and swap your agent over to it. By the end you'll
have seen the five ideas Kolsch is built on, by using them.

Steps 1–7 use the built-in fake LLM and need zero credentials. Steps
8–9 connect a real provider, but only when you're ready.

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
(`--root` > `$KOLSCH_WORKSPACE` > `~/Documents/kolsch`). The rest of
this tutorial assumes the default location.

If you skipped `kolsch setup` and want to know what it produced, read
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
`acme/agents/hello` (or any other ref) if you've gone off-script.

## 2. Run it

```bash
uv run kolsch run acme/agents/hello --input content="hello, kolsch"
```

You'll see one line of JSON: the agent's reply. Because `provider:
fake`, it echoes back — that's intentional. You just ran a real agent
exchange end-to-end with zero external calls.

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
uv run kolsch run acme/agents/hello \
  --session ses_<paste-id> \
  --input content="say it again"
```

The new exchange lands in the same thread, appended after the first.
`kolsch sessions events ses_<id>` will show the full timeline.

## 5. Stream events as they happen

```bash
uv run kolsch run acme/agents/hello --input content="stream me" --stream
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
curl -s -X POST http://127.0.0.1:8080/agents/acme/agents/hello/run \
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

## 9. Swap your agent over to a real model

Open `bundles/acme/agents/hello.yaml` and change one line:

```yaml
# from:
model: { provider: fake, model: fake-model }
# to:
model: opus
```

That's it — no other field changes. The `opus` alias was added to
`platform.yaml` during step 8 and resolves to
`openrouter/anthropic/claude-opus-4-7`. Aliases mean your agent
definitions don't have to know which provider you're using.

Run it:

```bash
uv run kolsch run acme/agents/hello --input content="explain kölsch in one sentence"
```

You'll get a real answer this time. Same agent, same session machinery,
same event log on disk — only the LLM behind it changed.

Useful follow-ups:

- `uv run kolsch refresh openrouter` — re-pull the catalog when new
  models drop.
- `uv run kolsch disconnect openrouter` — remove the provider; the
  agent falls back to whatever else is configured (or errors with
  `llm.provider_not_configured`, which is the system telling you the
  truth).

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
- `docs/explanation/llm-providers.md` — why providers are connected
  and not configured by hand, and how to add a new one.
- `docs/explanation/v1-scope.md` — what Phase 1 covers and what's
  deliberately out of scope.
- `docs/reference/module-api-reference.md` — every public contract.
- `docs/how-to/` — short recipes for specific tasks as they're added.
