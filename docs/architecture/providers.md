# Providers

Four parts of the platform are swappable: memory, secrets, sandbox,
and authorization. Each sits behind a Python protocol; each has a
default implementation that ships with Kolsch and a path to drop in
something else.

```mermaid
graph LR
    Engine[Agent Engine] --> Memory
    Engine --> Authz
    Tools[Tools dispatch] --> Authz
    Tools --> Secrets
    Tools --> Sandbox
    Memory ==> Store[(Event Log)]

    click Engine "../engine/"
    click Tools "../tools/"
    click Store "../store/"
```

The Agent Engine reads memory and runs authz checks per exchange. The
Tools dispatch resolves secrets before a tool body runs and routes
sandboxed work through the sandbox provider.

## Memory

`MemoryContext` is the protocol — a session-bound handle the Agent
Engine uses for memory reads, writes, and queries.
`RecentBufferMemoryProvider` is the implementation that ships today;
it exposes a fixed-size window over the most recent events on the
thread. Custom providers (semantic, vector-backed) implement the
protocol and slot in by configuration.

Memory writes are events: every `memory.written` and
`memory.deleted` lands on the log so the in-memory cache is
reconstructable on restart.

## Secrets

`SecretsProvider` resolves the names a tool declared in
`secrets_needed=` into actual values. `DotenvSecretsProvider` is
the implementation that ships today; it reads from `.env` in the
workspace root. Tools never reach for env vars themselves; the
dispatch hands them a `ToolContext.secrets` dict already populated.

## Sandbox

`SandboxProvider` is the protocol meant to isolate untrusted code.
The `native` variant that ships today is a pass-through — it runs
the work in the calling process with no isolation. Real isolation
(subprocess limits, container-backed execution) is a planned future
variant. Tools that shell out (`bash`, the workflow `bash` runner)
still go through the sandbox so the integration point is in place
when a real implementation lands.

## Authorization

`AuthorizationProvider` decides whether a tool call is allowed. The
current implementation is rule-based — a list of records compiled
into a fast matcher. Each rule has three required fields and one
optional field:

| Field | Type | Semantics |
|---|---|---|
| `what` | `str` | The tool's fully-qualified ref (`kolsch/tools/bash`), a system action namespace (`agent.execute.*`), or `*` for any. |
| `pattern` | `str` | A glob matched against the target — for `kolsch/tools/bash` the target is the command string, for `kolsch/tools/read_file` it's the path, etc. Defaults to `*`. |
| `effect` | `"allow" \| "deny" \| "prompt"` | The decision, or a prompt to the user. |
| `from` | `str \| None` | Optional 3-seg caller-chain glob (`kolsch/agents/triage`, `kolsch/workflows/*`). When set, the rule fires only if at least one frame in the caller-chain matches; empty chain fails closed. Defaults to `None` (rule fires regardless of caller). |

A concrete rule set:

```yaml
authorization:
  variant: rule_based
  config:
    rules:
      # Allow the agent to read anything under the workspace
      - what: kolsch/tools/read_file
        pattern: "workspace:**"
        effect: allow

      # Only the triage agent gets to grep — nobody else
      - what: kolsch/tools/bash
        pattern: "rg *"
        from: kolsch/agents/triage
        effect: allow

      # Allow git commands but nothing else that shells out
      - what: kolsch/tools/bash
        pattern: "git *"
        effect: allow

      # Prompt on any other bash — surfaces the four-choice modal
      - what: kolsch/tools/bash
        pattern: "*"
        effect: prompt

      # Hard-deny writes to .env
      - what: kolsch/tools/write_file
        pattern: "**.env"
        effect: deny
```

A `prompt` effect surfaces the call to the user in `kolsch chat`
with four choices: **allow once**, **allow always** (writes a new
`allow` rule and remembers), **deny once**, **deny always** (writes
a new `deny` rule and remembers). Rules written via **allow always**
/ **deny always** are auto-scoped to the current agent via `from:`
— so approving `bash rg *` while chatting with agent `X` grants
that permission to agent `X` alone, not every future caller. Type
`*` at the `from:` prompt to broaden.

Rules live in four layers: the workflow's `policy:` block (when a
workflow node is firing the action), each agent's `permissions:`
block, each bundle's `bundle.yaml` `permissions:` block, and the
platform-wide `platform.yaml` `authorization.config.rules`. The
authz provider walks them first-match-wins in that order —
after any locked rules (below), workflow first (when applicable),
then agent, then bundle, then platform, then default-deny. Because matching stops at the first
rule that fires, an earlier-layer `allow` rule can grant access a
later layer would have denied. The layering is precedence, not
enforced narrowing: nothing in the code stops a bundle from
effectively widening the platform policy. Workspace owners
installing untrusted bundles have to audit each bundle's
`permissions:` block themselves.

**Locked rules — the operator-absolute exception.** The one control no
layer can widen past is `platform.yaml`'s
`authorization.config.locked_rules`: deny-only rules the provider
evaluates *before* the workflow, agent and bundle layers, so a
matching locked rule denies whatever any layer allows. They are
opt-in (an absent list changes nothing), managed with
`kolsch permissions add --locked`, and only as strong as write access
to `platform.yaml` itself. See
[Manage Permissions — Locked rules](../how-to/permissions.md#locked-rules).

**Workflow-agent escalation guarantee.** When a workflow node
invokes an embedded agent, the workflow layer is **omitted** from
the walk for the agent's own tool calls — so an agent embedded in
a workflow can never inherit the workflow's broader permissions.
The workflow layer only fires when the workflow node itself is
directly invoking a tool.

**Caller-chain.** The engine assembles a proximate-first tuple of
caller frames (`(kolsch/agents/X, kolsch/workflows/W)` for
`W → X → tool.bash`) and threads it into every `allow` call. The
`from:` filter matches transitively against this chain — any frame
that matches is enough. An empty chain (engine bootstrap) fails
closed for any `from:`-filtered rule. See
[`docs/how-to/permissions.md`](../how-to/permissions.md#scoping-rules-to-a-specific-agent-or-workflow)
for the operator-facing guide.

## Principles

**Protocols, not base classes.** Each provider is a Python
`Protocol`. Anyone can implement one without subclassing or
importing from `providers/`. Test mocks and production
implementations sit at the same level.

**Defaults work without configuration.** A fresh workspace runs
with the shipped implementations of all four providers and never
has to configure them. Swapping in custom ones is a per-workspace
decision.

**Provider state is reconstructable.** Memory writes are events,
authz decisions are events. Restarting a process and replaying the
log produces the same memory and the same authz outcome.

**Each provider knows only its own concern.** Memory doesn't know
about secrets, secrets doesn't know about authz, authz doesn't know
about sandboxing. The Agent Engine wires them together; the
providers don't talk to each other directly.

---

The class reference is at [API: Providers](../api/index.md#providers).
The [Memory provider contract](../explanation/memory-provider-contract.md)
covers the memory protocol in detail. The [Secrets / Sandbox / Authz
contract](../explanation/secrets-sandbox-authz-contract.md) covers
the other three.
