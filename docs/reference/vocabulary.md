# Vocabulary

A **writer's reference** for docstrings and in-code comments: which
word to use when, and which look-alikes to keep separate.

For the *definition* of each term — what each thing **is** in the
platform — see the Key Vocabulary in
[`master-index.md`](master-index.md#key-vocabulary). That file is the
noun catalog. This file is the **lane assignment**.

If a term you need isn't here, it's either obvious or wasn't drifting.
Add it the first time you have to make a choice twice.

---

## How to use this file

1. **Writing a docstring? Search this file for the noun you reached for.**
   If it appears under "Use", you picked right.
2. **If it appears under "Avoid" or "Reserved"**, swap it for the
   word in the same row's "Use" column.
3. **If two near-synonyms both show up in code you're documenting**,
   the "Quick rule" at the end of each section tells you how to
   disambiguate without thinking.

---

## Authority — who or what is doing something

Three near-synonyms that mean **different things**. Don't swap them.

| Use | What it means | Where it lives |
|---|---|---|
| **principal** | The authz subject of an authorization check — `user.local`, `agent.<id>`, `system`. | `cli/permissions.py`, `engine/engine.py`, error codes `authz.principal_*` |
| **actor** | The entity record stored on an event (a Pydantic model with `kind` + `id`). | `schemas/entities.py`, every `Event` payload, `engine/observers.py` |
| **caller** | The immediate code path that invoked the current function. | `caller_chain` audit trail in `engine/context.py`, tool subsystem docstrings |

**Quick rule:** *principal → permissions; actor → event log; caller →
call site.*

These three could refer to the same underlying agent at runtime — but
the **word you choose tells the reader which lens you're looking
through**. Mixing them obscures the lens.

---

## Execution layers — what's running things

Four near-synonyms at different layers. Pick by layer, not by
proximity to the verb "execute".

| Use | What it means | Where it lives |
|---|---|---|
| **engine** | The Agent Engine — long-lived, wraps the framework, runs exchanges. | `engine/engine.py` (`AgentEngine`) |
| **framework** | The wrapped library (CrewAI). The engine drives it. | `engine/framework.py`, `engine/crewai_framework.py` |
| **executor** | The workflow-node executor that runs one workflow node at a time. Ephemeral. | `workflows/executor.py` |
| **runner** | A CLI surface that drives an engine to produce output for a human. | `cli/_workflow_chat_runner.py`, `cli/chat.py` |

**Quick rule:** *engine = ours, long-lived; framework = the library
we wrap; executor = per workflow node; runner = the CLI shell on top.*

**Avoid:** writing "runner" for non-CLI code, or "executor" outside
the workflows subsystem.

---

## Work units — what counts as "one unit of work"

Four units at nested scales. Use the innermost word that fits.

| Use | What it means | Scale |
|---|---|---|
| **agent call** | One execution of one configured agent. The top-level unit the platform schedules. | Outermost |
| **exchange** | One user-message → agent → tools → agent cycle ending in a final text response. Event-log unit. | Mid |
| **iteration** | One framework-internal loop step inside an exchange. | Inner |
| **turn** | Conversational synonym for "exchange". **Scoped** — chat subsystem only. | `cli/chat.py` and the chat renderer |

**Quick rule:** *agent call ⊇ exchange ⊇ iteration. Use the innermost
that fits the sentence.*

**Scope rule for "turn":** allowed inside `cli/chat.py` and the chat
renderer code only — those modules talk to a human in conversational
language and "turn" reads naturally there. Everywhere else (engine,
store, workflows, providers, tools) → write "exchange".

---

## Tools — what's invokable from an agent

| Use | When | Where it lives |
|---|---|---|
| **tool** | The default term. Use it everywhere unless one of the rows below applies. | Everywhere |
| **action** | **Reserved** for the workflow node `action` type only. | `workflows/catalog.py`, builtin node defs |
| **function** | Only in generic Python context ("this function does X" — not "this tool"). | Anywhere |

**Quick rule:** *if an agent invokes it through `ctx.tools`, it's a
tool. If a workflow runs it as a node, it's an action. If neither,
it's a function.*

**Migration note:** the HTTP layer in `src/api/` currently uses
"action" for endpoint-level invocations. During the `api/` subsystem
sweep, rename those uses to **request** (the HTTP-layer noun) or
**invocation** (the call we forward to the engine), so "action" stays
reserved for workflow nodes.

---

## Agent-like entities

| Use | When | Where it lives |
|---|---|---|
| **agent** | The default term — both the template (configuration) and its session instances. | Everywhere |
| **assistant** | **Scoped — banned outside `llm/providers/opencode.py`**. Used inside that file because the provider's API uses the word. | `llm/providers/opencode.py` |
| **crew** | **Scoped — banned outside `engine/_crewai_adapter.py` and `engine/crewai_framework.py`**. Used inside those files because the wrapped library calls it that. | `engine/_crewai_adapter.py`, `engine/crewai_framework.py` |

**Quick rule:** *if you're talking about Kolsch concepts, write
"agent". The library's word is allowed inside the file that wraps
that library — nowhere else.*

**Reviewer rule:** `crew` or `assistant` appearing in any file other
than the ones above is a review-blocker. A future `ruff` custom rule
or grep-based pre-commit check will enforce this mechanically.

---

## Pluggable implementations

| Use | When | Where it lives |
|---|---|---|
| **provider** | The default term for swappable modules: memory, secrets, sandbox, authz, LLM. | `providers/`, `llm/providers/`, `bus/` |
| **backend** | **Scoped** — the storage backend, and event-bus backends. The word communicates "where the bytes live". | `store/backend.py`, `bus/in_process.py` |
| **adapter** | **Scoped** — framework adapters. The word communicates "shape-translates between Kolsch and a third-party API". | `engine/_crewai_adapter.py` |

**Quick rule:** *provider = the general case; backend = storage/bus
specifically; adapter = framework translation.*

---

## Records on the event log

| Use | When |
|---|---|
| **event** | The immutable JSONL record. Always use this for the log unit. |
| **record** | Use only inside the store, where "entity record" is the persisted row (Session, Thread, Actor). |

**Quick rule:** *if it's in the JSONL, it's an event. If it's a row
the store maintains, it's a record.*

---

## Sessions, threads, conversations

| Use | When |
|---|---|
| **session** | The conversation instance. One JSONL file per session. |
| **thread** | The persistence unit inside a session. Phase 1: one thread per session; later: many. |
| **conversation** | UI / explanatory text only — never the primary noun in a docstring. |

**Quick rule:** *session is what the user starts; thread is what the
event log writes to.*

---

## Common look-alikes — quick disambiguation

A close-reading checklist for the pairs most often confused:

- **principal vs actor** — principal is the **subject of a permission
  check**; actor is the **identity stamped on an event**. They may
  match at runtime; the words are still different jobs.
- **caller vs principal** — caller is *"what code called this"*;
  principal is *"whose permissions do we check"*.
- **engine vs framework** — engine is ours; framework is the wrapped
  library (CrewAI). If you say "the framework iterates", you mean
  CrewAI, not us.
- **session vs thread** — session is the conversation; thread is the
  persistence unit inside it. They are 1:1 in Phase 1 and will
  diverge later — write docstrings that already respect the
  distinction.
- **agent vs session** — agent is the template; session is the
  instance.
- **tool vs action** — tool runs from an agent; action runs from a
  workflow node.

---

## Voice rules for using tech terms

- **Use the precise tech term in summary lines and section blocks**
  (`Args:`, `Returns:`, `Raises:`). The reader is scanning for
  precision — don't soften it.
- **Gloss the term in plain English on its first appearance** in a
  docstring's extended description, with an em-dash aside:

  > *"The principal — the authenticated identity the call runs as —
  > is checked by the authz provider before the tool body runs."*

  The gloss buys precision and approachability at once.
- **Don't gloss the same term twice in the same file.** If the
  module's been talking about principals for ten classes, just say
  "the principal".
- **Plain English for verbs, precise nouns for nouns.** "The store
  appends" reads better than "the store performs an append
  operation". The action is plain; the artifact is precise.
- **Skip jargon the reader doesn't need.** If a docstring is about
  what a function returns, don't invoke `assembly_version` unless
  the return shape depends on it.

---

## When a term you need isn't here

This file grows on demand, not preemptively. If you're writing a
docstring and reach for a word that has two reasonable variants, and
this file doesn't say which to pick:

1. **Search `src/`** for both variants. Whichever the codebase uses
   more, use that.
2. **Check
   [`master-index.md` Key Vocabulary](master-index.md#key-vocabulary)**
   — if one variant is defined there, that's the winner.
3. **Otherwise, pick the one that's clearer to a new contributor and
   add the entry here.** A two-row table costs nothing; the next
   writer benefits.

---

## See also

- [`docs/process/documentation-conventions.md`](../process/documentation-conventions.md)
  — the docstring shape rules (Google style, MkDocs-readiness).
- [`master-index.md` Key Vocabulary](master-index.md#key-vocabulary)
  — the noun catalog: what each term *is*.
