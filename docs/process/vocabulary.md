# Vocabulary

A **writer's reference** for docstrings and in-code comments: which
word to use when, and which look-alikes to keep separate.

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
| **principal** | The authz subject of an authorization check — `user.local`, `agent.<id>`, `system`. | `providers/authz.py`, `engine/engine.py`, permission rules |
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
| **framework** | The adapter that runs the agent loop for the engine — `MinimalFramework` (built-in, the default), `CrewAIFramework` (opt-in), or `SubprocessAgentFramework` (external harness). | `engine/framework.py`, `engine/mock_framework.py`, `engine/crewai_framework.py`, `engine/subprocess_framework.py` |
| **executor** | The workflow subsystem's dispatcher that hands each node to its runner. | `workflows/executor.py` (`RunnerRegistry`, `execute_workflow`) |
| **runner** | The per-node-type worker registered on the executor. Also, the CLI shell surfaces at `cli/_workflow_chat_runner.py` etc. | `workflows/executor.py` (`NodeRunner` protocol), `cli/*_runner.py` |

**Quick rule:** *engine = ours, long-lived; framework = the library
we wrap; executor = the workflow dispatcher; runner = the thing
executed by an executor (workflows) or the CLI shell on top.*

---

## Work units — what counts as "one unit of work"

Four units at nested scales. Use the innermost word that fits.

| Use | What it means | Scale |
|---|---|---|
| **agent call** | One execution of one configured agent. The top-level unit the platform schedules. | Outermost |
| **exchange** | One user-message → agent → tools → agent cycle ending in a final text response. Event-log unit. | Mid |
| **iteration** | One framework-internal loop step inside an exchange. | Inner |
| **turn** | Interchangeable with "exchange". Common in engine/framework code that talks about back-and-forth with the model. | Everywhere |

**Quick rule:** *agent call ⊇ exchange ⊇ iteration. Use the innermost
that fits the sentence.*

**"turn" vs "exchange":** they mean the same thing. `turn` reads more
naturally in narrative code that talks about the model (assembly,
framework adapters, mock framework, chat UI); `exchange` reads more
naturally in event-log / store code that treats it as a discrete
recorded unit. Pick by the sentence, not by the module.

---

## Tools — what's invokable from an agent

| Use | When | Where it lives |
|---|---|---|
| **tool** | The invokable — the `@tool`-decorated function an agent calls. | `tools/`, agent YAML `subsystems.tools.config.declared` |
| **action** | The **authz identifier** for a tool invocation. This is the string a permission rule's `what:` matches against — the 3-segment catalog ref (`<bundle>/tools/<name>`). | `providers/authz.py` (`Rule.action`, `allow(principal, action)`), `tools/dispatch.py` (`defn.action_name`) |
| **function** | Only in generic Python context ("this function does X" — not "this tool"). | Anywhere |

**Quick rule:** *tool = the thing that runs; action = the string
permissions match against. `word_count` is the tool; `mybundle/tools/word_count`
is the action.*

---

## Agent-like entities

| Use | When | Where it lives |
|---|---|---|
| **agent** | The default term — both the template (configuration) and its session instances. | Everywhere |
| **assistant** | Reserved for **LLM message roles** — the model side of a request's `messages` list, matching the underlying provider API. | `engine/framework.py`, `engine/_crewai_adapter.py`, `engine/assembly.py`, any LLM message-shape code |
| **crew** | **Scoped — banned outside `engine/_crewai_adapter.py` and `engine/crewai_framework.py`**. Used inside those files because the wrapped library calls it that. | `engine/_crewai_adapter.py`, `engine/crewai_framework.py` |

**Quick rule:** *if you're talking about Kolsch concepts, write
"agent". `assistant` is the message-role word (as in `{"role": "assistant"}`),
not a synonym for agent. `crew` is CrewAI's word, allowed only inside
the adapter.*

**Reviewer rule:** `crew` outside the two adapter files is a
review-blocker. `assistant` as a synonym for agent (rather than as an
LLM message role) is also a review-blocker.

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
| **thread** | The persistence unit inside a session. One thread per session today; the shape allows more. |
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
- **engine vs framework** — engine is ours; framework is the adapter
  that runs the loop (the built-in `MinimalFramework` by default, or
  CrewAI / the subprocess adapter). If you say "the framework iterates",
  you mean the adapter's loop, not us.
- **session vs thread** — session is the conversation; thread is the
  persistence unit inside it. One-to-one today; write docstrings that
  respect the distinction.
- **agent vs session** — agent is the template; session is the
  instance.
- **tool vs action** — tool is what runs; action is the authz string
  a permission rule matches against.
- **executor vs runner** — the executor dispatches; a runner is what
  the executor dispatches *to*.

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
2. **Otherwise, pick the one that's clearer to a new contributor and
   add the entry here.** A two-row table costs nothing; the next
   writer benefits.

---

## See also

- [`documentation-conventions.md`](documentation-conventions.md) — the
  docstring shape rules (Google style, MkDocs-readiness).
