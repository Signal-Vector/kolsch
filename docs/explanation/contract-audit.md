# Phase 1 — Inter-System Contract Audit

**Draft 0.1**

> Cross-check between modules. For each pair of subsystems that interact,
> verify both sides' contracts agree on what flows and what's assumed.
> Surface gaps and produce a list of required spec revisions.
>
> This is a systems-integration audit. It does not redefine contracts;
> it verifies them against each other.

---

> **Historical (pre-implementation).** This audit reflects the Phase-1
> contract state *before* implementation. The gaps it raises (G1–G19) were
> subsequently resolved in shipped code — e.g. the `bus.publish`-after-
> `append_event` ordering, the content store (`src/store/content.py`), and
> the `agent.execute.*` authz pre-flight all landed. Read it for
> provenance, not as a list of open work; the current per-subsystem
> contract docs describe what ships.

---

## Part 0 — Method

For each pair of modules that interact, the audit covers:

- **Direction A → B**: what A calls or expects from B.
- **Direction B → A**: what B calls or expects from A (when bidirectional).
- **Assumption check**: does B's contract actually deliver what A
  assumes? Does A's contract actually meet what B requires?
- **Gap**: any mismatch, ambiguity, or missing specification.

Each gap becomes an entry in Part 12 (required spec revisions).

The modules audited:

1. Kernel/Resolver
2. Asset Management
3. Session/Run Store
4. Event Bus
5. Agent Engine
6. Memory Provider
7. Secrets Provider
8. Sandbox Provider
9. Authorization Provider
10. Tools subsystem
11. Skills subsystem

Plus the cross-cutting concerns (error model, file handling). Pairs
audited are those with actual interaction; not all 55 possible pairs
have meaningful interaction.

---

## Part 1 — Kernel/Resolver Interactions

The kernel is depended on by nearly everything but depends on nothing
of the platform. Its interaction surface is small.

### 1.1 Kernel ↔ Asset Management

**Asset Management → Kernel:** Calls `kernel.resolve_and_validate(partials, subsystem_validators, system_validators)` to merge layered configs.

**What Asset Management assumes:**
- Resolver takes a list of dicts, returns a `ResolvedAgentConfig`.
- Resolver validates structurally before AND after merge.
- Subsystem validators are passed by name; the resolver dispatches.
- Errors are aggregated, raised as `ConfigValidationError`.

**What the Kernel contract delivers** (per Kernel contract Part 5):
- ✓ `resolve_and_validate` signature matches.
- ✓ Layer 1 validation runs on each partial AND on the resolved config.
- ✓ Layer 3 dispatched by name.
- ✓ Errors aggregated; raises with all errors.

**Status:** ✓ No gap.

### 1.2 Kernel ↔ Subsystem Variant Validators

**Kernel → Variant validators:** Calls each variant's validator function for Layer 3 validation. Variant validators are passed in by the caller.

**What the Kernel assumes about variant validators:**
- They are `Callable[[dict], ValidationResult]`.
- They return structured errors with layer/source/path/code/message.
- They do not perform I/O.

**What each subsystem's variant validator delivers:**
- Memory `recent_buffer` validator: validates write_tag (no wildcards), read_tags shape, recent_turns, max_tokens — ✓ documented in Memory contract §5.1.
- Tools `standard` validator: validates builtin names exist, declared refs resolve, MCP server uniqueness — ✓ documented in Tools spec §3.4.
- Skills `standard` validator: validates enabled refs resolve, no duplicates — ✓ documented in Skills spec §4.2.

**Gap G1:** The Memory contract §5.1 specifies validation rules in prose but does not explicitly state the validator function's signature matches the Kernel's `ConfigValidator = Callable[[dict], ValidationResult]`. Same for Tools §3.4 and Skills §4.2. Minor — the rules are clear — but the connection should be explicit.

### 1.3 Kernel ↔ Error Model

**Kernel → Error Model:** The kernel raises `ConfigValidationError` (which is a `PlatformError` per the error model). Its `detail` contains `validation_errors: [ValidationError]` — a different shape than the canonical error envelope.

**What the Error Model expects:**
- All errors serialize to `(type, message, where, detail, cause)`.
- `ConfigValidationError.detail` should follow the catalog (Part 4 of error model).

**The Error Model catalog §4.1 lists:**
- `config.invalid` with `detail: {validation_errors: [ValidationError]}` — ✓ matches.

**Gap G2:** The kernel's `ValidationError` (with fields layer, source, path, code, message) is not the same as the canonical `Error` envelope (type, message, where, detail, cause). The kernel's internal `ValidationError` is its own thing — a validation finding — wrapped inside the `detail` of the outer `config.invalid` error. This is fine but the relationship should be explicit: `ValidationError` is internal-to-validation, NOT a PlatformError subtype. Worth a small note in the error model spec.

---

## Part 2 — Asset Management Interactions

Asset Management is the discovery and resolution layer. Most subsystems consult it.

### 2.1 Asset Management ↔ Tools

**Tools → Asset Management:** At engine setup, the Tools subsystem asks Asset Management for the tools the agent has enabled. Specifically: given the resolved AgentConfig with `tools.config.declared: [refs]`, Asset Management returns the registered tool definitions.

**What Tools assumes:**
- Asset Management has built a catalog at startup.
- Tool refs (full or shorthand) resolve deterministically.
- The catalog yields `AssetEntry` with path information so Tools can `@tool`-decorated functions.

**What Asset Management contract delivers (Part 4, Part 5):**
- ✓ Catalog built at startup.
- ✓ Refs resolved per §5; shorthand handled per §5.2.
- ✓ `AssetEntry` carries path information.

**Gap G3:** Asset Management §5.3 says "Tools are imported lazily (per Tools subsystem spec §6.1)." The Tools spec §6.1 says "the platform's adapters" do registration. Neither explicitly says *which module* triggers the Python import — Asset Management or the Tools subsystem itself. Consensus needs to be stated. Recommended: the Tools subsystem does the imports at engine setup, using paths supplied by Asset Management. Asset Management is read-only catalog; it does not execute discovered code.

### 2.2 Asset Management ↔ Skills

**Skills → Asset Management:** Skills subsystem queries the catalog for enabled skill refs at engine setup. Asset Management returns the skill metadata (name, description, path, etc.) for each ref.

**What Skills assumes:**
- Catalog includes parsed SKILL.md frontmatter (name, description, allowed-tools, disable-model-invocation).
- Skill bodies are NOT eagerly loaded; only metadata is in the catalog.

**What Asset Management delivers:**
- §4.3 documents AssetEntry as having `content: None at catalog build; populated lazily on first use`.
- §4.3 says skill metadata (name, description) is parsed at catalog build.

**Gap G4:** Asset Management §4.3 says metadata is parsed at catalog build, but doesn't explicitly specify the full set of frontmatter fields the catalog captures. Skills §3.3 enumerates the fields the registry should have. Cross-reference: the catalog's `AssetEntry` for skills should include the SkillMetadata fields per Skills §3.3. This is implicit but should be made explicit.

### 2.3 Asset Management ↔ Agent Engine

**Agent Engine → Asset Management:** At each `execute` call, the engine needs the resolved AgentConfig. Asset Management provides the layer-assembly mechanism (§6) that produces this.

**What the Engine assumes:**
- Asset Management produces a fully resolved, validated AgentConfig.
- Prompt file refs are pre-resolved to content (per Asset Mgmt §6.2).
- Tool and skill refs are NOT pre-resolved (they remain refs).
- The engine receives a dict-equivalent that it passes through.

**What Asset Management delivers (§6):**
- ✓ Three-layer assembly: system defaults → agent definition → call context.
- ✓ Pre-resolution of prompt refs.
- ✓ Tool/skill refs pass through.
- ✓ Calls kernel resolve_and_validate.

**Gap G5:** Asset Management §6.1 says it constructs partials including "system_defaults — from platform.yaml's 'defaults' section." But the platform.yaml schema (Asset Mgmt §3.2) is sketched, not formally specified. The defaults section's structure is implied but not nailed down. Confirmed in §3.2's open item but worth tracking as a known gap.

### 2.4 Asset Management ↔ Path Policy / File Handling

**File Handling → Asset Management:** No direct calls. But the path policy in `platform.yaml` (per File Handling §4) is read by the platform at startup, and that startup is conceptually owned by Asset Management.

**Gap G6:** Who actually reads `platform.yaml` and dispatches its sections to the appropriate modules? Currently:
- Asset Management §3.2 documents the schema.
- Secrets Provider §1.4.1 specifies its config body.
- Authorization §3.4.1 specifies its config body.
- File Handling §4.1 specifies its config section.
- Sandbox §2.3.1 specifies its config body.

But no spec says *which module* loads the platform.yaml file, parses it, and hands sections to the right providers. This is platform-bootstrap concern, currently unowned.

Recommended: Asset Management owns platform.yaml loading. It reads the file, validates the top-level structure, and provides accessors for each section. Subsystems call e.g. `assets.get_platform_config("authorization")` to get their config slice. This makes Asset Management the platform-bootstrap module.

---

## Part 3 — Session/Run Store Interactions

The Session/Run Store owns sessions, threads, and the JSONL event log. The engine is its primary consumer.

### 3.1 Session/Run Store ↔ Agent Engine

**Agent Engine → Session/Run Store:** At every `execute` call, the engine calls:
- `get_leaf(thread_id)` to find where to append.
- `append_event(thread_id, event)` for every event it emits.
- `read_path_to_leaf(thread_id)` during context assembly.

**What the Engine assumes (per Engine contract §2):**
- `get_leaf` returns the current most-recent event (Phase 1 semantics).
- `append_event` returns the appended event with `parent_event_id` populated.
- Lock acquisition + leaf computation + append are atomic (Store I2).
- `read_path_to_leaf` yields events in chronological order (root first).

**What Session/Run Store delivers:**
- ✓ `get_leaf` per §2.3.
- ✓ `append_event` performs lock-protected leaf + parent + append atomically (§2.3, §4.1, I2).
- ✓ `read_path_to_leaf` yields in chronological order (§2.3, §6 implicit).

**Gap G7:** Engine contract §2.3 ("Hand to the framework") says the engine "appends new events under the leaf" during execution. But CrewAI's event loop is asynchronous — events arrive via callbacks. The store's `append_event` is `async`, and acquiring the file lock is `flock` (blocking). What happens if two callbacks for the same thread fire concurrently inside the engine (say, a model.response and a tool.called near-simultaneously)? The store's lock serializes them, but the engine's callback handlers may have race conditions in their event-construction code. This is an engine-internal correctness concern, but worth flagging that the Engine contract should explicitly state: callback handlers must serialize event construction per thread (or the engine's adapter layer must do this).

### 3.2 Session/Run Store ↔ Memory Provider

**Memory Provider → Session/Run Store:** The Memory `recent_buffer` variant reads events from the session log to build its in-process cache on session open. Specifically: replay `memory.written` and `memory.deleted` events for the session.

**What Memory assumes:**
- It can read events of specific types from the log.
- Events are append-only and stable.

**What Session/Run Store delivers:**
- ✓ `read_events(thread_id)` yields all events (Store §2.3).
- Filtering by event type is the caller's responsibility (acceptable).

**Gap G8:** Memory's `bind_session` reads from the Session/Run Store to rebuild its cache. But Memory's contract (§3.3) doesn't explicitly state that this read happens through the Store's API — could be conflated with "Memory reads files directly." It should be explicit: Memory reads via `store.read_events(thread_id)`, not by reading JSONL files directly. This preserves the principle that the Store mediates all log access.

### 3.3 Session/Run Store ↔ Event Bus

**Session/Run Store → Event Bus:** When the engine appends an event via the store, the event is also published to the bus. Is this:
- A: The store publishes to the bus as part of `append_event`?
- B: The engine publishes after the store returns?
- C: Some other mechanism?

**What each spec says:**
- Event Bus contract §1.3 and §2 say "the Agent Engine, when it appends an event to the log, also publishes that same event to the bus." So the engine publishes, not the store.
- Engine contract §2.4 lists event emission steps but doesn't mention bus publication explicitly.

**Gap G9:** The bus-publication path is unspecified at the engine layer. The Event Bus contract says "the engine publishes" but the Engine contract doesn't say where in the flow. Should be added: after `store.append_event` returns successfully, the engine calls `bus.publish(event)`. The store does NOT publish to the bus (avoids coupling).

### 3.4 Session/Run Store ↔ Asset Management

**Session/Run Store → Asset Management:** The store doesn't directly call Asset Management, but its filesystem layout (sessions/, content/) is defined in Asset Management §3. The store consumes the platform-root convention.

**Gap G10:** The Session/Run Store contract §3.1 says "The store roots all session data at a configurable base directory, defaulting to `~/.<platform-name>/sessions/`." But Asset Management §3 says "platform root = current working directory" (per the latest decision). These conflict. The Session/Run Store contract needs revision: the base directory is `<platform-root>/sessions/`, where platform-root is the CWD. Not `~/.<platform-name>/`.

This is a real contract correction needed in Session/Run Store §3.1.

### 3.5 Session/Run Store ↔ Tools (content store)

**Tools → Session/Run Store:** When a tool result exceeds the inline threshold, the engine writes the full content to `content/<hash>.bin` and the event carries a `content_ref`. Who actually writes the content file?

**What's specified:**
- Tools spec §6.4 says "the platform" stores the content in a sidecar.
- Asset Management §3.6 documents `content/` as the location.
- File Handling §3.1 says "the engine writes there, and only to store oversized event-content sidecars."

**Gap G11:** Three specs touch this but none specifies the precise API. Is it the Session/Run Store's responsibility (since it owns event persistence)? The engine's directly? A separate content-store module?

Recommended: the Session/Run Store provides a `write_content(content) → content_ref` method that hashes, writes to `content/<hash>.bin`, and returns the reference. The engine calls this when it detects oversized payloads, and includes the returned `content_ref` in the event before appending. This puts the content store under the Session/Run Store's purview (it's persistence, after all).

Session/Run Store contract needs amendment to add this method.

---

## Part 4 — Event Bus Interactions

The bus is the inter-module notification mechanism. v1 ephemeral-only.

### 4.1 Event Bus ↔ Agent Engine

**Agent Engine → Event Bus:** Publishes events after each `append_event` succeeds (per Gap G9 above).

**Other Modules → Event Bus:** Subscribe to topics they care about. v1 has no concrete subscribers in scope (no workflow engine, no IDE, no scheduler).

**Gap G12:** Are there ANY subscribers in v1? If not, the bus is built but unused. Worth confirming: is the bus necessary for v1, or could it be deferred?

Looking at the scope: v1 ships with no workflow engine, no IDE, no scheduler. So no internal subscribers. The bus is *available* for external consumers (a debug tool watching session activity) but isn't used by the platform itself.

If the bus has no internal subscribers in v1, it's effectively dead infrastructure. Options:

- **Keep it.** It's cheap (Redis pub/sub, a few lines of code per module to publish), and external developers may build subscribers. Forward-compatible.
- **Defer it.** No v1 subscribers means no v1 value. Add when first internal subscriber lands.

Recommended: keep it but mark it as "infrastructure available; no internal v1 consumers." The publish overhead is minimal (single Redis PUBLISH call); the optionality is real (external tools could subscribe).

This is a v1 scope clarification rather than a contract gap, but worth surfacing.

### 4.2 Event Bus ↔ Session/Run Store

No direct interaction. The bus doesn't read the store; the store doesn't publish to the bus. They both serve the engine but they're independent.

**Status:** ✓ No interaction, no gap.

---

## Part 5 — Agent Engine Interactions (Outbound)

The engine consumes most subsystems. Already audited the Engine ↔ Store and Engine ↔ Bus above. Continuing.

### 5.1 Agent Engine ↔ Authorization

**Engine → Authorization:** Before executing an agent, call `allow(principal, "agent.execute.<agent_id>")`. Per Engine §2.1 step (implicit) and Authorization §3.5.

**Gap G13:** Engine contract §2.1 lists pre-flight steps. The authorization check is implicit but not explicitly listed. Should be added as a step: "Check `authz.allow(principal, agent_execute_action)`. If denied, raise `agent.unauthorized`, do not proceed."

Also: what's the principal? Engine contract is silent on how the principal is derived. Authorization §3.2.4 specifies `user.local` / `agent.<id>` conventions. The engine needs to know: for the initial execute call, the principal is the user; for sub-tool-calls within the agent's execution, the principal is the agent.

This needs explicit specification in the Engine contract: principal derivation rules. Open item #2 of the Secrets/Sandbox/Authz spec already flags this.

### 5.2 Agent Engine ↔ Memory Provider

**Engine → Memory:** At session setup, `memory.bind_session(session_id, agent_id, config)`. During execution, the MemoryContext is consulted for retrievals; on writes, the engine emits `memory.written` events with the actual content; on compactions, calls `compact()`.

**What the Engine assumes (per §2.2 step 4 — implicit):**
- Memory provides a context per session.
- Context.retrieve() returns content the engine can inject into the model's view.
- Engine emits memory events; Memory does not emit directly.

**What Memory delivers:**
- ✓ `bind_session` per §3.3.
- ✓ Memory doesn't emit events (I1).
- ✓ Compact returns text the engine can use (§4.2, §5.4).

**Gap G14:** Engine contract §2.2 mentions "Resolve secrets" and "Start eager MCP servers" but does NOT mention "Bind memory context." Memory setup is missing from the engine's pre-flight checklist. Should be added.

Also: §2.4 lists "Memory retrieved" as an observation but doesn't specify WHO triggers the retrieval. Is the engine the one calling `context.retrieve()` during context assembly? Or does some other path? Should be explicit: the engine, during Assembly Procedure step 4 (per Schemas §B.5), calls `memory_context.retrieve(...)` and integrates results.

### 5.3 Agent Engine ↔ Tools

**Engine → Tools:** Many calls. At setup: discovery, validation, registration with framework. At execution: authorization checks, secret resolution, sandbox routing, observation.

**What the Engine assumes:**
- Tools spec details adapter layer (§6.1).
- Adapter performs authz/secrets/sandbox before tool function.

**What Tools delivers:** ✓ Per Tools §6.

**Gap G15:** The Tools spec §6.1 and Engine spec §2.2 both describe the setup flow, but they describe it from different perspectives. Tools §6.1 says "the platform" registers tools; Engine §2.2 says "the engine" registers tools. They mean the same thing but the wording diverges. Reconciliation: the engine is what calls into the Tools subsystem to do the registration work. State this consistently.

### 5.4 Agent Engine ↔ Skills

**Engine → Skills:** At setup, the engine constructs the system prompt section listing available skills (per Skills §5.1). On force-invoke, the engine emits a synthetic `read` call (Skills §6.2).

**What the Engine assumes:**
- Skills subsystem provides metadata for prompt assembly.
- Force-invoke produces standard tool events.

**What Skills delivers:** ✓ Per Skills §5.1 and §6.2.

**Gap G16:** Engine contract §2.2 mentions "Compose the system prompt by concatenating the configured prompt layers." It does NOT mention appending the skills listing. Per Skills §5.1, the engine MUST append a skills listing. This is missing from the engine setup spec. Should be added: after prompt layer composition, append the available-skills listing per Skills §5.1.

Also: the force-invoke mechanism in Skills §6.2 references `ExecutionContext.trigger.force_skills` (a new trigger field). Engine contract §1.3 doesn't show this field. ExecutionContext shape needs to be updated to include `force_skills`.

### 5.5 Agent Engine ↔ Secrets / Sandbox

**Engine → Secrets:** Resolves secrets for tool invocation. Engine §2.2 mentions secrets resolution; Secrets §1.3 specifies the resolve/exists flow.

**Engine → Sandbox:** Wraps tool execution. Engine §2.2 and Sandbox §2.2 align.

**Status:** ✓ No major gaps. Both flows specified.

### 5.6 Agent Engine ↔ Asset Management

Audited in 2.3 above. ✓.

---

## Part 6 — Memory Provider Interactions

Memory is mostly consumed by the engine. Already covered above (5.2).

### 6.1 Memory ↔ Session/Run Store

Covered in 3.2. Gap G8 noted.

### 6.2 Memory ↔ Kernel (validator)

Covered in 1.2. Gap G1 noted (signature reference).

---

## Part 7 — Tools Subsystem Interactions

### 7.1 Tools ↔ Secrets

**Tools → Secrets:** At tool registration, call `secrets.exists(name)` for each `secrets_needed` declaration. At invocation, call `secrets.resolve(name)` and pass into ToolContext.

**What Tools assumes:** Resolve returns the value; exists is a check without resolution.

**What Secrets delivers:** ✓ Per §1.2.

**Status:** ✓ No gap.

### 7.2 Tools ↔ Sandbox

**Tools → Sandbox:** Every tool invocation routes through `sandbox.execute(work, policy)`.

**What Tools assumes:** Sandbox returns the work's result transparently.

**What Sandbox delivers:** ✓ Per §2.2.

**Status:** ✓ No gap.

### 7.3 Tools ↔ Authorization

**Tools → Authorization:** Per tool invocation, `authz.allow(principal, "<bundle>/tools/<name>")` (3-segment catalog ref per qol-action-namespace-bundle-scoped.md).

**Gap G17:** Who is the principal at tool invocation time? The Tools spec §6.2 doesn't specify; Authorization §3.2.4 conventions suggest `agent.<agent_id>` (since the agent is the actor calling the tool). The Engine's adapter, when calling allow(), should pass the agent_id as the principal. Needs explicit statement.

### 7.4 Tools ↔ File Handling

**Tools → File Handling:** Built-in `read`/`write`/`edit` consult the path policy (per File Handling §4.6).

**What File Handling delivers:** ✓ Policy specified.

**Gap G18:** File Handling §4.6 says builtin tools perform the check. But the *mechanism* — where the policy is held, how the check is invoked — is not specified. Is this a function in File Handling? A method on a policy object? Implementation detail, but the contract needs a small note: file-touching tools consult a shared path policy module/function. Specifically: the path policy is loaded from platform.yaml at startup (per the platform-bootstrap concern of G6), exposed via a function the builtin tools call.

---

## Part 8 — Skills Subsystem Interactions

### 8.1 Skills ↔ Tools (via read)

**Skills → Tools:** Skills are loaded by invoking the `read` tool on SKILL.md paths. This is documented per Skills §5.2 and §6.2.

**What Skills assumes:** The `read` tool works on paths under `bundles/<bundle>/skills/<name>/SKILL.md`.

**What Path Policy delivers:** `bundles/` is in the default allowed_paths (File Handling §4.4). ✓.

**Status:** ✓ No gap.

### 8.2 Skills ↔ Asset Management

Covered in 2.2. Gap G4 noted.

---

## Part 9 — Authorization, Sandbox, Secrets Interactions

These three are platform-level providers with limited interactions beyond their consumers.

### 9.1 Authorization ↔ Tools

Covered in 7.3. Gap G17 noted.

### 9.2 Authorization ↔ Agent Engine

Covered in 5.1. Gap G13 noted.

### 9.3 Sandbox ↔ Tools

Covered in 7.2. ✓.

### 9.4 Secrets ↔ Tools

Covered in 7.1. ✓.

### 9.5 Secrets ↔ MCP servers (via Tools)

**Tools subsystem (acting on behalf of an MCP server config) → Secrets:** Resolve secrets named in `env_inject` mapping. Inject as environment variables when spawning the MCP subprocess.

**What's specified:** Tools §5.3 and Secrets §1.3.2 both cover this. ✓.

---

## Part 10 — Error Model Interactions

### 10.1 Error Model ↔ All Modules

Every module raises platform errors. The Error Model defines the canonical shape and a catalog. Each module's error types must be in the catalog.

**Status of each module's error types in the catalog (Error Model §4):**
- Kernel ✓ (§4.1)
- Session/Run Store ✓ (§4.2)
- Event Bus ✓ (§4.3)
- Agent Engine ✓ (§4.4)
- Tools ✓ (§4.5)
- Memory ✓ (§4.6)
- Secrets ✓ (§4.7)
- Sandbox ✓ (§4.8)
- Authorization ✓ (§4.9)
- Skills ✓ (§4.10)

**Gap G19:** File Handling errors (specifically `file_access.denied` per File Handling §4.6) is not in the Error Model catalog. Needs to be added under a new module section (4.11 maybe) with proper detail shape and level (Diagnostic).

### 10.2 Error Model ↔ Events

The Error Model §5 specifies where errors appear in events. ✓.

---

## Part 11 — File Handling Interactions

### 11.1 File Handling ↔ All Tools

The path policy applies to `read`/`write`/`edit`. Bash is exempt. Per File Handling §4.7.

**Status:** ✓ Documented, with known v1 limitation acknowledged.

### 11.2 File Handling ↔ Asset Management

Asset Management owns the filesystem layout (Asset Mgmt §3) and would naturally own `platform.yaml` loading per the bootstrap concern in Gap G6. File Handling's path policy is one section of platform.yaml.

**Status:** Covered in G6.

---

## Part 12 — Required Spec Revisions

Consolidated list of gaps to fix in existing specs. Each entry names the spec, the section, and the revision needed.

### Spec Revisions in Priority Order

**R1. Session/Run Store contract §3.1** — Path correction. Change "defaulting to `~/.<platform-name>/sessions/`" to "at `<platform-root>/sessions/`, where platform-root is the current working directory."
*(addresses G10)*

**R2. Session/Run Store contract — new section** — Add a content-store API: `write_content(content) → content_ref`. The store writes to `<platform-root>/content/<hash>.bin` and returns the reference. Engine calls this for oversized event payloads.
*(addresses G11)*

**R3. Session/Run Store contract — new method** — Add `reconstruct_from_filesystem()` for the case of Postgres-missing-but-files-present recovery (per the earlier conversation).
*(known pending revision, also referenced in the Event Bus spec for durable subscribers)*

**R4. Agent Engine contract §1.3** — Update ExecutionContext to include `trigger.force_skills: list[str]` for force-invoke. The trigger union now has a kind that includes force skills.
*(addresses G16 partial)*

**R5. Agent Engine contract §2.1** — Add explicit pre-flight steps:
- Authorization check on `agent.execute.<agent_id>` (G13)
- Memory context binding (G14)
- After prompt composition: append skills listing (G16)
*(addresses G13, G14, G16)*

**R6. Agent Engine contract §2.4** — Add: "After `store.append_event` returns, the engine calls `bus.publish(event)`." Document the bus publication step.
*(addresses G9)*

**R7. Agent Engine contract — new subsection** — Specify principal derivation: user.local for top-level execute; agent.<agent_id> for tool invocations within. Reference Authorization §3.2.4.
*(addresses G13 partial, G17)*

**R8. Agent Engine contract §2.3 — note added** — "Callback handlers from the framework must serialize event construction per thread; race conditions in concurrent callback delivery are the engine adapter's responsibility."
*(addresses G7)*

**R9. Asset Management contract — new subsection on platform bootstrap** — Document that Asset Management owns platform.yaml loading and provides accessors for subsystems to retrieve their config sections.
*(addresses G6)*

**R10. Asset Management §5.3** — Clarify: Asset Management is read-only; tool imports happen in the Tools subsystem using paths from the catalog. Asset Management does not execute discovered code.
*(addresses G3)*

**R11. Asset Management §4.3** — Add: AssetEntry for skills includes the full SkillMetadata per Skills §3.3 — name, description, license, compatibility, metadata, allowed_tools, disable_model_invocation.
*(addresses G4)*

**R12. Memory Provider contract §3.3** — Add: "The bind_session implementation reads `memory.written` and `memory.deleted` events via `store.read_events(thread_id)`, NOT by reading JSONL files directly."
*(addresses G8)*

**R13. Memory Provider §5.1, Tools §3.4, Skills §4.2** — Each variant validator's spec should explicitly state its function signature matches `ConfigValidator = Callable[[dict], ValidationResult]` per the Kernel contract.
*(addresses G1)*

**R14. Error Model §4 — new section 4.11** — File Handling error types: `file_access.denied` (Diagnostic), with detail shape per File Handling §4.6.
*(addresses G19)*

**R15. Error Model §3** — Note that `ValidationError` (the kernel's internal validation finding) is NOT a PlatformError subtype; it's the internal-to-validation data shape carried inside `ConfigValidationError.detail`.
*(addresses G2)*

**R16. File Handling §4.6** — Specify the mechanism: path policy loaded from platform.yaml at startup (via Asset Management's bootstrap, per R9), exposed via a check-function the builtin tools call.
*(addresses G18)*

**R17. v1 scope document §3.4** — Add a note: "v1 ships the Event Bus infrastructure for ephemeral notifications, but has no internal subscribers. The bus is available for external consumers (debug tools, future IDE) but the v1 platform does not depend on it."
*(addresses G12)*

**R18. Phase 1 Schemas §A.6** — PromptSpec simplification per the Asset Management contract revision: PromptSpec = `list[string]` or `string`, per Asset Management §6.4.
*(noted in Asset Management open item #8; consistency)*

---

## Part 13 — Status Summary

19 gaps identified (G1-G19). All have proposed resolutions (R1-R18, with some Rs addressing multiple gaps).

Most gaps are minor — cross-references missing, mechanisms unspecified, sections not yet written. Three are slightly more substantive:

- **G6/R9** — platform bootstrap ownership wasn't assigned to any module. Asset Management is the natural owner; this is now made explicit.
- **G11/R2** — the content store had no API. Adding it to Session/Run Store.
- **G14/R5** — engine pre-flight missing memory binding and skills listing. Substantive additions to engine setup.

No fundamental contract conflicts found. The architecture coheres; the gaps are detail-level coordination work between specs.

After applying the revisions, the spec suite should be internally consistent. The test plan (next document) will verify the corrected contracts hold in practice.
