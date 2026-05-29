# Phase 1 — Secrets, Sandbox, Authorization Provider Modules

**Draft 0.1**

> Three Provider Modules, one document. Each follows the Provider Module
> pattern: contract + registry + (later) selection policy. v1 ships one
> variant per module.
>
> Authorization is a real rule-based system from day one (per design
> correction). Secrets is minimal but specifies the consumption pattern for
> Tools and MCP servers. Sandbox is a passthrough placeholder whose interface
> is in place for a real sandbox variant in a later phase.

---

## Part 0 — Shared Framing

### 0.1 Where these modules sit

All three are **platform-level Provider Modules**, configured in the
platform's startup config — NOT in any AgentConfig. They apply globally to
every agent execution. (Memory, Tools, Skills are agent-level subsystems
because their configuration varies per agent; Secrets/Sandbox/Authorization
are properties of the platform installation.)

### 0.2 Common shape

Each module has:
- A formal contract (interface + invariants).
- A registry of variant implementations.
- A v1 variant that ships ready to use.
- A `{variant, config}` wrapper in the platform's startup config.

No selection policy in v1 — exactly one variant registered per module.

### 0.3 Common rule

Per v1 scope §3.4 — no Provider selection policies. The degenerate-case
default rule applies: exactly one provider registered → use it.

---

# Module 1 — Secrets

## 1.1 Purpose

Resolve a secret name to a value. Consumers (Tools, MCP servers, anything
that needs credentials) ask the Secrets provider for values; the provider
returns them or fails.

The Secrets contract itself is minimal — three methods. The complexity of
"how do secrets reach the code that needs them" lives in the consumers,
not in this provider.

## 1.2 Interface

```
async def resolve(name: str) -> str
    """Return the value of the secret. Raises SecretNotFound if absent."""

async def exists(name: str) -> bool
    """Return whether the secret exists, without retrieving its value."""

async def list_names() -> list[str]
    """Return all available secret names. Does NOT return values."""
```

`resolve` returns the value as a string. The contract treats secrets as
opaque strings — binary secrets are out of scope.

`exists` is for validation-time checks. The Tools subsystem uses this to
verify all declared secrets are available at tool-registration time,
before a tool is ever invoked. Catches missing secrets early.

`list_names` exists for diagnostic and IDE-later purposes — "what
secrets does this installation have?" Never returns values.

## 1.3 The Secret Consumption Pattern

How secrets reach the code that needs them — documented here because the
pattern is part of the platform's overall design, even though the
mechanism lives in the Tools subsystem spec.

### 1.3.1 In-process tools (Pattern A)

A tool definition declares what secrets it needs:

```yaml
tools:
  - name: web_search
    secrets_needed: ["SERPAPI_KEY"]
    execute: ...
```

The Agent Engine, at tool registration time:
1. Calls `secrets.exists(name)` for each declared secret. If any is
   missing, registration fails with a clear error.
2. At tool invocation time, calls `secrets.resolve(name)` for each, then
   passes the resolved values into the tool's invocation context.

The tool's code accesses the resolved value via the invocation context.
The tool author never reads `.env` directly; the framework does.

### 1.3.2 MCP server subprocesses (Pattern B)

An MCP server config declares its required secrets and how they map to
environment variables the subprocess expects:

```yaml
mcp_servers:
  - name: github
    command: ["npx", "@modelcontextprotocol/server-github"]
    secrets_needed: ["GITHUB_TOKEN"]
    env_inject:
      GITHUB_PERSONAL_ACCESS_TOKEN: GITHUB_TOKEN
```

When the engine starts the MCP server subprocess, it resolves each
declared secret and sets the corresponding environment variables in the
subprocess's environment. The subprocess reads from its own env as
usual; nothing about it is platform-aware.

### 1.3.3 What this contract owns vs. delegates

This contract owns: the `resolve`, `exists`, `list_names` interface, and
the principle that secrets never appear in events (§1.5 below).

The Tools subsystem spec (later) owns: the `secrets_needed` and
`env_inject` declaration fields, and the registration-time validation
and runtime injection mechanics.

## 1.4 The `dotenv` Variant

The v1 implementation.

### 1.4.1 Config body

```
secrets:
  variant: dotenv
  config:
    path: ".env"          # path to the .env file; default ".env"
                           # in the platform's working directory
```

### 1.4.2 Behavior

At platform startup, the variant reads the `.env` file once and parses
it into an in-memory dict. Standard `.env` format: `KEY=value` lines,
`#` comments, basic quoting.

`resolve(name)` looks up `name` in the dict. Raises `SecretNotFound` if
absent.

`exists(name)` returns whether `name` is in the dict.

`list_names()` returns the dict's keys.

### 1.4.3 Caching and lifecycle

The variant caches the parsed `.env` for the platform's lifetime — read
once at startup, never re-read. If the user edits `.env` mid-session,
the change is not picked up until the platform restarts. This is
acceptable for v1's local-first single-user model.

A future variant could implement file-watching or `refresh()` methods.
The contract permits this; the v1 variant doesn't bother.

### 1.4.4 What's NOT in v1

- Hierarchical/namespaced secret names. Just flat strings.
- Multiple `.env` files merged. One file.
- Secret-value typing (e.g., "this secret is JSON"). Just strings.
- Cloud secret backends (Vault, AWS Secrets Manager, etc.). Later phase.
- Per-agent secret scoping (this agent gets these secrets, that one
  gets those). All secrets are globally available; access control is
  handled by the Authorization module (Module 3) if needed.

## 1.5 Conformance Invariants

### S-I1 — Secrets never appear in events

The Secrets provider MUST NOT log resolved values. `resolve("KEY")` MUST
NOT produce any log output containing the value. This is enforced at
the provider's own logging discipline.

(The complementary invariant — secrets resolved for tool configuration
MUST NOT appear in the session event log — is enforced at the Agent
Engine layer per Engine Invariant I10. The two invariants together
ensure secrets never leak into observable output.)

### S-I2 — Pure resolution

`resolve(name)` MUST be a pure function of `name` and the provider's
current internal state. No side effects beyond reading from configured
storage. No network calls during resolve.

(A future variant that fetches from a vault may make network calls
during initialization; resolve() reads from the post-initialization
cache.)

### S-I3 — Failure on missing

`resolve` of a missing secret MUST raise `SecretNotFound`. Returning
None or empty string is forbidden — empty strings could be legitimate
secret values, and returning None silently could allow a tool to run
with broken credentials and fail in obscure ways downstream.

### S-I4 — `exists` correctness

`exists(name)` MUST return True if and only if a subsequent
`resolve(name)` would succeed. No race between exists and resolve
within a stable provider state.

### S-I5 — Async semantics

All methods are `async`. v1's `dotenv` variant resolves from an
in-memory dict (effectively synchronous), but the interface is async
for future variants that genuinely need it (vault network calls, etc.).

---

# Module 2 — Sandbox

## 2.1 Purpose

Execute a callable under a policy. v1's `native` variant just runs the
callable. Future variants execute in containers, with resource limits,
with filesystem/network restrictions, etc.

The Sandbox sits in the path of every tool execution per the Agent
Engine contract (§2.2). Every tool invocation goes through the Sandbox
provider, even when the provider is a passthrough. The call site is in
place for when a real sandbox arrives.

## 2.2 Interface

```
async def execute(
    work: Callable[[], Awaitable[T]],
    policy: SandboxPolicy,
) -> T
    """Execute the work under the policy. Return the work's result."""
```

`work` is a zero-argument async callable that performs the actual work
(typically a tool invocation). The sandbox wraps it however the variant
needs to — for `native`, that's just `await work()`. For a future
Docker variant, it would spawn a container, communicate work in, get
the result out.

`policy` is the per-execution sandbox configuration. For `native`, it's
ignored. For real sandbox variants, it specifies resource limits,
isolation requirements, timeout, etc.

`T` is the work's return type. The sandbox is generic over what it
executes — it doesn't interpret the result.

### 2.2.1 Why async-first

Tools can be async (calling LLMs, hitting APIs, doing I/O). A sync-only
sandbox would force all tools to be sync, which is the wrong direction.
Async-first; sync callables are wrapped (the engine does this
adaptation, not the sandbox).

## 2.3 The `native` Variant

### 2.3.1 Config body

Empty:

```
sandbox:
  variant: native
  config: {}
```

The `native` variant has no configuration.

### 2.3.2 Behavior

```python
async def execute(work, policy):
    return await work()
```

Literally that. The work runs in the same process, with the same
permissions as the platform itself. There is no actual sandboxing.

Why this is acceptable for v1: local-first, single-user. The user runs
the platform on their own machine; the tools the platform invokes are
the tools the user installed and trusts; there is nobody to defend
against except the user themselves. Sandboxing is a Phase 2+ concern
when multi-tenancy, downloaded bundles, or untrusted tool sources
appear.

### 2.3.3 What it does NOT do

- Filesystem isolation. Tools can read/write anywhere the platform's
  process can.
- Resource limits. Tools can consume unbounded CPU/memory/network.
- Network restrictions. Tools can make any network call.
- Timeout. The contract permits a timeout in `policy`; `native` ignores
  it. (A tool that hangs hangs forever. Engine-level timeouts from
  `RuntimeSpec.timeout_seconds` apply at a higher level.)

These are real omissions, accepted for v1 scope.

## 2.4 Conformance Invariants

### Sb-I1 — Result transparency

The sandbox MUST return the work's exact result. The `native` variant
returns it directly; a Docker variant would return what the containerized
work produced, marshalled back. The sandbox does not transform results.

### Sb-I2 — Error transparency

If the work raises, the sandbox MUST re-raise the same exception (or
an equivalent that preserves type and message). The sandbox does not
swallow errors. (A real sandbox MAY wrap the exception in a
`SandboxExecutionError` if the work was killed by the sandbox's own
constraints — timeout, OOM — but normal work exceptions pass through.)

### Sb-I3 — Policy honored

A real sandbox variant MUST honor the policy it was given. The `native`
variant trivially "honors" the empty policy by ignoring it. A Docker
variant ignoring memory limits in its policy would be non-conforming.

### Sb-I4 — Pre-execution validation

The sandbox MAY validate the policy before executing. An invalid policy
(unsupported by the variant) MUST cause execution to fail before the
work runs, raising `InvalidSandboxPolicy`. (The `native` variant has
nothing to validate — empty policy is always valid.)

### Sb-I5 — Async semantics

All sandbox methods are `async`.

---

# Module 3 — Authorization

## 3.1 Purpose

Decide whether a caller (principal) is permitted to perform an action.
v1 uses a rule-based system from day one — real authorization, not a
placeholder.

The Authorization provider is consulted by the Agent Engine before
executing an agent, and may be consulted by tools/subsystems before
performing actions they want gated.

## 3.2 The Rule Model

### 3.2.1 Rule shape

```
Rule
├── who      string      — principal pattern (regex, fullmatch)
├── what     string      — action pattern (regex, fullmatch)
└── effect   enum        — "allow" | "deny"
```

A rule says: principals matching `who`, performing actions matching
`what`, are [allowed | denied].

### 3.2.2 Regex semantics

Both `who` and `what` are regular expressions matched with **fullmatch**
semantics — the regex must match the entire string, not a substring.
This avoids the well-known over-matching footgun (`tool.b` accidentally
matching `tool.bash`).

If a user wants substring or prefix matching, they write it explicitly
with `.*` (e.g., `tool\\..*` matches all tool actions).

Standard Python regex syntax. Implementation uses `re.fullmatch()`.

### 3.2.3 Action namespace

Actions are dotted, hierarchical strings. The namespace is owned by the
subsystem performing the action:

```
tool.<tool-name>              — invoking a tool
mcp.<server>.<tool>           — invoking an MCP server tool
memory.read                   — reading memory
memory.write                  — writing memory
memory.delete                 — deleting a memory item
skill.invoke.<skill-name>     — invoking a specific skill
session.create
session.delete
config.modify                 — modifying agent config mid-execution
```

Subsystems document their action surface. New subsystems extend the
namespace with their own prefix. The contract specifies the convention
(hierarchical dotted strings); concrete action names are documented in
their owning subsystem's spec.

### 3.2.4 Principal namespace

Principals identify the caller of an action. In v1, there is one
principal: the local user. Reserved convention for v1 and beyond:

```
user.<id>                     — a human user
agent.<id>                    — an agent (action initiated by an agent itself)
system                        — the platform itself (background tasks)
```

v1's single user is `user.local`. Agents acting on the user's behalf
are `agent.<agent_id>`. The principal is passed to authorization by
whoever is checking.

### 3.2.5 Rule evaluation

Given a `(principal, action)` pair, the provider:

1. Walks the rule list in order.
2. For each rule, tests whether `re.fullmatch(rule.who, principal)`
   AND `re.fullmatch(rule.what, action)` both succeed.
3. Returns the first matching rule's `effect`.
4. If no rule matches: **default deny**. Returns "deny".

First-match-wins with default-deny is standard ACL semantics. It
permits both "general allow + specific deny" patterns and "general
deny + specific allow" patterns by ordering rules appropriately.

### 3.2.6 The default deny

The default-deny rule is what makes the system honest: forgetting to
write rules is denial, not silent permission. v1's default config
provides one explicit allow rule to make the local-first single-user
case usable out of the box.

## 3.3 Interface

```
async def allow(
    principal: str,
    action: str,
) -> AuthorizationResult
    """Return whether the principal may perform the action."""

class AuthorizationResult:
    allowed: bool
    matched_rule: Rule | None     # the rule that matched, or None for default-deny
    reason: str                    # human-readable explanation
```

`allow` returns a structured result, not just a bool, so callers can
log which rule applied. The IDE later (and CLI tooling now) can use
this for explainable authorization decisions.

```
async def list_rules() -> list[Rule]
    """Return the configured rule set, for diagnostic purposes."""
```

For a future "why was I denied?" tool. Read-only; rules are not
mutable via the contract in v1.

## 3.4 The `rule_based` Variant

### 3.4.1 Config body

```
authorization:
  variant: rule_based
  config:
    rules:
      - who: ".*"
        what: ".*"
        effect: allow
```

The default v1 rule set: one rule, allow everything for everyone. The
mechanism is in place; the rule is permissive. Restrictive setups edit
the rule list.

Example with restrictions:

```
authorization:
  variant: rule_based
  config:
    rules:
      - who: ".*"
        what: "tool\\.bash"
        effect: deny
      - who: ".*"
        what: ".*"
        effect: allow
```

Deny `tool.bash` for everyone; allow everything else. The deny is listed
first so it matches before the general allow.

### 3.4.2 Behavior

`allow(principal, action)`:

```python
for rule in self.rules:
    if re.fullmatch(rule.who, principal) and re.fullmatch(rule.what, action):
        return AuthorizationResult(
            allowed=(rule.effect == "allow"),
            matched_rule=rule,
            reason=f"matched rule: who={rule.who} what={rule.what} effect={rule.effect}",
        )
return AuthorizationResult(
    allowed=False,
    matched_rule=None,
    reason="no rule matched; default deny",
)
```

Straightforward.

## 3.5 Where Authorization Checks Happen

The contract specifies the interface; *where* checks happen is the
caller's responsibility. The intended call sites in v1:

1. **The Agent Engine, before executing.** Per the Agent Engine
   contract §2.1 pre-flight: check `allow(principal, "agent.execute.<agent_id>")`
   before any work begins. Raise if denied.

2. **The Tools subsystem, before invoking a tool.** Per the Tools
   subsystem spec (to be drafted): check `allow(principal, "tool.<name>")`
   before invocation. Raise — surfaced as `tool.failed` event — if
   denied.

3. **The Memory MemoryContext, on write and read.** Optionally check
   `allow(principal, "memory.write")` etc. v1's default allow-everything
   makes these checks no-ops; they're still placed for future
   restrictive rule sets.

Other subsystems may add their own check sites as they encounter
sensitive operations. The action namespace (§3.2.3) is the place to
document new check points.

## 3.6 Conformance Invariants

### A-I1 — Fullmatch semantics

Both `who` and `what` MUST be matched with `re.fullmatch()` semantics —
the regex must match the entire string. Implementations MUST NOT use
`re.match()` (which is prefix-match) or `re.search()` (which is
substring-match) — those would produce over-matching.

### A-I2 — First match wins

Rules are evaluated in order; the first rule matching both `who` and
`what` determines the result. Implementations MUST NOT reorder rules
or apply "most specific match wins" or any other heuristic. Order is
data; the user writes rules in the order they should be evaluated.

### A-I3 — Default deny

If no rule matches, the result MUST be deny. Implementations MUST NOT
silently allow.

### A-I4 — Pure decision

`allow(principal, action)` MUST be a pure function of the principal,
the action, and the current rule set. No I/O, no network calls, no
external state consultation.

### A-I5 — Result transparency

The `AuthorizationResult` MUST include the rule that matched (or None
for default deny). Implementations MUST NOT hide which rule applied.
Explainability is required.

### A-I6 — Async semantics

All methods are `async`, consistent with the rest of the platform.

---

## Part 4 — How These Modules Compose

### 4.1 Typical flow at a tool invocation

When the Agent Engine invokes a tool, all three modules participate:

1. **Authorization:** check `allow(agent_principal, "tool.<name>")`.
   If denied, emit `tool.failed` with cause=unauthorized, no further
   action.

2. **Secrets:** resolve any `secrets_needed` declared by the tool.
   Resolved values are placed in the tool's invocation context.

3. **Sandbox:** wrap the tool's execution in
   `sandbox.execute(work=lambda: tool.run(args, ctx), policy=...)`.
   The native sandbox just runs it; a real sandbox would isolate it.

The Tools subsystem (next document in queue) details the mechanics;
this composition is documented here for context.

### 4.2 Why platform-level

All three modules apply uniformly to every agent in a platform
installation. They're not per-agent configuration. An agent can't
say "use a different secrets provider for me" or "exempt me from
authorization" — these are properties of the installation, not the
agent.

The platform's startup configuration carries them:

```yaml
platform:
  secrets:
    variant: dotenv
    config:
      path: ".env"

  sandbox:
    variant: native
    config: {}

  authorization:
    variant: rule_based
    config:
      rules:
        - who: ".*"
          what: ".*"
          effect: allow
```

(The platform-level config schema is not yet specified as a formal
artifact; it's referenced here to show where these modules are
configured.)

---

## Part 5 — Open Items

1. **Platform-level config schema.** §4.2 sketches the structure but no
   formal spec exists. To be drafted alongside the Asset Management
   contract (which handles config file composition more broadly).

2. **Principal derivation.** §3.2.4 establishes the principal namespace
   (`user.<id>`, `agent.<id>`, `system`). Who decides which principal
   to use for a given action — and how it's threaded through the
   system — needs specification. For v1's single-user case, the
   answer is "always `user.local` from the engine, `agent.<id>` from
   tools the agent invokes," but the threading mechanism isn't
   detailed.

3. **Action namespace registry.** §3.2.3 lists known actions, but the
   list will grow. A formal registry document (or a section of each
   subsystem's spec) is needed so users writing rules know what
   actions exist.

4. **Authorization for the IDE-later.** When the IDE arrives, it will
   need its own actions (e.g., `session.view`, `session.export`).
   Reserved; not v1.

5. **Provider selection policies.** v1 has one variant each; no policy.
   The Provider Module pattern (per the consolidated spec) reserves
   selection policies for later. Not specified here.

6. **Sandbox policy schema for future variants.** §2.2 takes a
   `policy` parameter; its shape is variant-specific. The native
   variant ignores it. When a real sandbox variant arrives, that
   variant defines its policy schema. The contract makes no claims
   about the shape of future policies.

7. **The `env_inject` mechanism for MCP servers (§1.3.2).** The
   declared shape — a map of env-var-name to secret-name — is sketched
   here but its formal validation lives in the Tools subsystem spec.
   Flagged for cross-reference when that spec is drafted.

8. **Secret-value typing.** v1 treats all secrets as strings. Some
   future use cases want structured secrets (JSON credentials, etc.).
   The contract permits a variant to return non-string types, but v1's
   `dotenv` doesn't.
