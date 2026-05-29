# Test Catalog

**Draft 0.2** *(TC-ENG-01..11 reframed as framework-agnostic and
TC-CRW-* reserved per the Step 12 split; see build plan v0.2.)*

> Recommended test cases in English, organized by subsystem, then by
> integration scenario, then end-to-end. Each case names what to set up,
> what to do, what to assert, and which success criterion (Handbook §10)
> or invariant it verifies.
>
> This is a working catalog to implement against — not exhaustive, but
> covering the ground that matters. Implementers add edge cases as they
> surface. Cases are written so a developer or agent can turn each into a
> real test without further design.

---

## How to Read This Catalog

Each case has the form:

```
TC-<MODULE>-<NN>: <title>
  Layer:    unit | component | integration | e2e
  Setup:    preconditions and fixtures
  Action:   what the test does
  Expected: the assertion(s)
  Verifies: the success criterion or invariant this maps to
```

Module codes: KRN (Kernel), ASM (Asset Mgmt), STR (Store), BUS (Event
Bus), ENG (Engine), MEM (Memory), SEC (Secrets), SBX (Sandbox), AUZ
(Authorization), TLS (Tools), SKL (Skills), ERR (Error Model), FIL (File
Handling), CRW (CrewAI Adapter — authored in Step 12b), INT
(Integration), E2E (End-to-end).

---

## Part 1 — Kernel / Resolver (KRN)

**TC-KRN-01: Valid partial passes structural validation**
- Layer: unit
- Setup: a dict with valid fields, all optional fields present.
- Action: `validate_partial(partial, layer=1, source="test")`.
- Expected: `ok == True`, `errors == []`.
- Verifies: basic Layer 1 functional correctness.

**TC-KRN-02: Unknown field rejected**
- Layer: unit
- Setup: a dict containing a field not in the schema.
- Action: `validate_partial(...)`.
- Expected: `ok == False`; an error with a path pointing at the unknown
  field.
- Verifies: structural rejection.

**TC-KRN-03: Multiple errors all reported**
- Layer: unit
- Setup: a dict with three independent problems (unknown field, wrong
  type, malformed id).
- Action: `validate_partial(...)`.
- Expected: `errors` has length 3; all three problems present.
- Verifies: §10.1 — all N errors returned, not just the first.

**TC-KRN-04: RFC 7386 merge vectors**
- Layer: unit
- Setup: the published RFC 7386 test vectors (each: original, patch,
  expected result).
- Action: `resolve([original, patch])` for each vector.
- Expected: output equals the vector's expected result, for 100% of
  vectors.
- Verifies: §10.1 — RFC 7386 conformance.

**TC-KRN-05: null deletes a key**
- Layer: unit
- Setup: partial A `{"model": "x", "temp": 0.5}`, partial B
  `{"temp": null}`.
- Action: `resolve([A, B])`.
- Expected: result has no `temp` key; `model` retained.
- Verifies: merge deletion semantics.

**TC-KRN-06: Determinism across runs**
- Layer: unit
- Setup: a fixed list of partials.
- Action: `resolve(partials)` 1000 times.
- Expected: all 1000 outputs byte-identical.
- Verifies: §10.1 — determinism.

**TC-KRN-07: Purity — no I/O**
- Layer: unit
- Setup: run with filesystem and network access denied (e.g., via a
  sandbox or monkeypatched open/socket).
- Action: run the full kernel test suite.
- Expected: all pass; no I/O attempted.
- Verifies: §10.1 — zero I/O.

**TC-KRN-08: Three-layer dispatch**
- Layer: unit
- Setup: partials plus a subsystem validator and a system validator,
  each instrumented to record invocation.
- Action: `resolve_and_validate(...)`.
- Expected: Layer 1 invoked per partial and on the resolved config;
  Layer 2 validators invoked; Layer 3 subsystem validators invoked for
  each subsystem present.
- Verifies: §10.1 layering; KRN I5.

**TC-KRN-09: Unknown variant rejected**
- Layer: unit
- Setup: a config naming a subsystem variant with no registered
  validator.
- Action: `resolve_and_validate(...)`.
- Expected: raises `config.unknown_variant` naming the subsystem and
  variant.
- Verifies: failure mode.

**TC-KRN-10: Validator that raises is contained**
- Layer: unit
- Setup: a Layer 3 validator that raises an exception internally.
- Action: `resolve_and_validate(...)` with other validators also present.
- Expected: the raising validator's failure is captured as a
  `validator_internal_error`; other validators still run.
- Verifies: failure containment.

---

## Part 2 — Asset Management (ASM)

**TC-ASM-01: Catalog build registers expected assets**
- Layer: component
- Setup: a fixture platform-root with K bundles containing known tools,
  skills, agents, prompts.
- Action: `initialize()` then `list_assets()`.
- Expected: the asset count and refs exactly match the fixture's known
  set.
- Verifies: §10.2 — catalog correctness.

**TC-ASM-02: Duplicate bundle name is fatal**
- Layer: component
- Setup: two bundle directories whose manifests both declare
  `name: "dup"`.
- Action: `initialize()`.
- Expected: raises a fatal startup error naming the conflict; does NOT
  silently pick one.
- Verifies: §10.2; Am-I2.

**TC-ASM-03: Bundle name/directory mismatch is fatal**
- Layer: component
- Setup: a bundle in directory `foo/` whose manifest says `name: "bar"`.
- Action: `initialize()`.
- Expected: raises a fatal error.
- Verifies: Am-I1.

**TC-ASM-04: Reserved name `core` rejected**
- Layer: component
- Setup: a bundle directory named `core/`.
- Action: `initialize()`.
- Expected: rejected at catalog build.
- Verifies: Am-I3.

**TC-ASM-05: Unambiguous shorthand resolves**
- Layer: component
- Setup: catalog with exactly one tool named `web-search`.
- Action: `resolve_ref("web-search", "tool")`.
- Expected: returns the full ref `<bundle>/tools/web-search`.
- Verifies: §10.2 — shorthand.

**TC-ASM-06: Ambiguous shorthand raises with candidates**
- Layer: component
- Setup: catalog with `web-search` in two bundles.
- Action: `resolve_ref("web-search", "tool")`.
- Expected: raises `assets.ambiguous_shorthand`; the message names both
  candidates; does NOT pick one.
- Verifies: §10.2; Am-I6.

**TC-ASM-07: Prompt refs pre-resolved, tool/skill refs not**
- Layer: component
- Setup: an agent config with prompt refs, tool refs, and skill refs.
- Action: `build_resolved_config(agent_ref)`; inspect the result.
- Expected: prompt field contains inline content (refs replaced); tool
  and skill refs remain as refs in the resolved config.
- Verifies: §10.2; Am-I4, Am-I5.

**TC-ASM-08: platform.yaml sections dispatch correctly**
- Layer: component
- Setup: a platform.yaml where each section carries a distinct marker
  value.
- Action: `initialize()`; each subsystem requests its section via
  `get_platform_config`.
- Expected: each subsystem receives its own section's marker; none
  receives another's.
- Verifies: §10.2; Am-I11.

**TC-ASM-09: Missing bundle.yaml skipped with warning**
- Layer: component
- Setup: a bundles directory with one valid bundle and one directory
  lacking `bundle.yaml`.
- Action: `initialize()`.
- Expected: the valid bundle is registered; the invalid directory is
  skipped with a logged warning; no fatal error.
- Verifies: failure handling (graceful).

**TC-ASM-10: Skill metadata captured in catalog**
- Layer: component
- Setup: a bundle with a skill whose SKILL.md has full frontmatter.
- Action: `initialize()`; `get_asset(skill_ref)`.
- Expected: the AssetEntry includes name, description, license,
  compatibility, metadata, allowed_tools, disable_model_invocation; the
  body is NOT loaded.
- Verifies: Asset Mgmt §4.3 (R11).

**TC-ASM-11: Malformed platform.yaml is fatal**
- Layer: component
- Setup: a platform.yaml with an invalid top-level structure.
- Action: `initialize()`.
- Expected: raises `config.invalid` with a message pointing at the
  offending field; platform refuses to start.
- Verifies: bootstrap failure handling.

**TC-ASM-12: First-run generates default platform.yaml**
- Layer: component
- Setup: a platform-root with no platform.yaml.
- Action: `initialize()`.
- Expected: a platform.yaml is generated with default sections
  (including default `file_access` allowed/denied paths and default
  storage tier); initialization proceeds.
- Verifies: first-run UX; Fh-I7.

---

## Part 3 — Session/Run Store (STR)

**TC-STR-01: Create session writes file and entity**
- Layer: component
- Setup: temp platform-root.
- Action: `create_session(agent_id)`.
- Expected: JSONL file exists at `sessions/<id>.jsonl`; first line is a
  `session.opened` event; entity record exists.
- Verifies: functional create.

**TC-STR-02: get_leaf on fresh thread returns None**
- Layer: component
- Setup: a session with a thread but no events beyond session.opened.
- Action: `get_leaf(thread_id)`.
- Expected: returns the last event (or None if thread truly empty per
  the implementation's definition).
- Verifies: leaf semantics.

**TC-STR-03: append populates parent_event_id**
- Layer: component
- Setup: a thread with one existing event E1.
- Action: `append_event(thread_id, E2)`.
- Expected: E2's `parent_event_id == E1.event_id`; E2 has a fresh
  `event_id` and `created_at`.
- Verifies: §10.3; STR I4.

**TC-STR-04: parent chain correct over 1000 events**
- Layer: component
- Setup: a fresh thread.
- Action: append 1000 events sequentially.
- Expected: each event's parent equals the prior event's id; reading
  back yields an unbroken chain.
- Verifies: §10.3.

**TC-STR-05: Concurrent appends serialize (same process)**
- Layer: component
- Setup: a thread; 100 append coroutines scheduled concurrently.
- Action: `await asyncio.gather(*appends)`.
- Expected: all 100 events persisted; the parent chain is linear and
  unbroken (no two events share a parent); no lost events.
- Verifies: §10.3; STR I2.

**TC-STR-06: Concurrent appends serialize (cross-process)**
- Layer: component
- Setup: two separate OS processes appending to the same session.
- Action: each process appends 50 events.
- Expected: all 100 persisted; chain linear; flock serialized them.
- Verifies: §10.3; STR I2 cross-process.

**TC-STR-07: Mid-write kill is recoverable**
- Layer: component
- Setup: a session; begin an append and kill the process mid-write
  (simulate a partial final line).
- Action: reopen the session and read.
- Expected: the partial final line is cleanly handled (truncated/ignored);
  all complete prior events intact; no crash.
- Verifies: §10.3; STR I3.

**TC-STR-08: Corrupt line raises with line number**
- Layer: component
- Setup: a JSONL with a deliberately malformed line in the middle.
- Action: `read_events(thread_id)`.
- Expected: raises `store.corrupt_log` naming the line number.
- Verifies: failure mode.

**TC-STR-09: write_content is idempotent**
- Layer: component
- Setup: temp platform-root.
- Action: `write_content(X)` twice with identical content.
- Expected: both return the same `ContentRef`; the file is written once;
  no rewrite on the second call.
- Verifies: STR I11.

**TC-STR-10: reconstruct rebuilds entities**
- Layer: component
- Setup: several sessions created; then wipe the entity DB (leave JSONL
  files).
- Action: `reconstruct_from_filesystem()`.
- Expected: entity records re-created matching the original session set;
  result counts correct; existing records (if any) not overwritten.
- Verifies: §10.3.

**TC-STR-11: SQLite and Postgres backends pass identically**
- Layer: component
- Setup: the full Store component suite, parameterized over
  `entity_backend: sqlite` and `entity_backend: postgres` (Postgres via
  container).
- Action: run the suite against both.
- Expected: identical pass results.
- Verifies: §10.3 — backend parity; DB-R1.

**TC-STR-12: delete removes file and entity**
- Layer: component
- Setup: a session.
- Action: `delete_session(id)`.
- Expected: JSONL gone; lock file gone; entity record gone; a second
  delete is a no-op (no error).
- Verifies: functional delete.

---

## Part 4 — Event Bus (BUS)

**TC-BUS-01: Published event reaches matching subscriber**
- Layer: component
- Setup: subscribe to `tool.failed`.
- Action: publish a `tool.failed` event.
- Expected: the handler receives the byte-identical event.
- Verifies: §10.4; BUS I1.

**TC-BUS-02: Non-matching subscriber not notified**
- Layer: component
- Setup: subscribe to `tool.failed`.
- Action: publish `thread.exchange_started`.
- Expected: the handler is NOT invoked.
- Verifies: §10.4; BUS I2.

**TC-BUS-03: Prefix wildcard matches**
- Layer: component
- Setup: subscribe to `tool.*`.
- Action: publish `tool.called` and `tool.result`.
- Expected: handler receives both.
- Verifies: pattern matching.

**TC-BUS-04: Attribute filter works**
- Layer: component
- Setup: subscribe to `*` with filter `{thread_id: "th_A"}`.
- Action: publish events for th_A and th_B.
- Expected: only th_A events delivered.
- Verifies: filtering.

**TC-BUS-05: Raising handler does not affect others**
- Layer: component
- Setup: two subscribers to the same topic; the first raises.
- Action: publish a matching event.
- Expected: the second still receives it; the first remains subscribed;
  no crash.
- Verifies: §10.4; BUS I4.

**TC-BUS-06: in_process and redis backends pass identically**
- Layer: component
- Setup: the bus component suite parameterized over `backend:
  in_process` and `backend: redis` (Redis via container).
- Action: run against both.
- Expected: identical pass results.
- Verifies: §10.4 — backend parity; DB-R2.

**TC-BUS-07: Publish failure does not break execution**
- Layer: component
- Setup: a bus backend simulated as unavailable.
- Action: `publish(event)`.
- Expected: does not raise into the caller; logs a warning; returns.
- Verifies: §10.4 — best-effort.

---

## Part 5 — Agent Engine (ENG)

> **Framework-agnostic.** The TC-ENG-01..11 cases were originally
> described with "real CrewAI" in their Setup. After the Step 12 split
> (build plan v0.2), they are framework-agnostic — they run against any
> conforming `Framework` implementation:
>
> - **In Step 12a** they run against `FakeFramework` (no CrewAI; perfect
>   observability).
> - **In Step 12b** the same cases re-run against `CrewAIFramework`
>   (real CrewAI underneath, same golden files expected).
>
> The "Setup" lines below say "the configured Framework" to reflect this.
> CrewAI-specific tests live in Part 11.5 (TC-CRW-*), authored in Step
> 12b.

**TC-ENG-01: Simple exchange golden sequence**
- Layer: integration
- Setup: Fake LLM scripted with one text response; a minimal agent
  config; the configured Framework (FakeFramework in 12a,
  CrewAIFramework in 12b).
- Action: `execute(config, context)`; capture events.
- Expected: normalized event sequence matches the golden file
  (exchange_started → user.message → model.request → model.response →
  agent.message → exchange_completed). Same golden across both
  Framework implementations.
- Verifies: §10.5 — golden sequence.

**TC-ENG-02: Tool-call exchange golden sequence**
- Layer: integration
- Setup: Fake LLM scripted to request a tool call, then a final text
  response; a config with that tool.
- Action: `execute(...)`; capture events.
- Expected: sequence includes tool.called → tool.result before the final
  agent.message; matches golden.
- Verifies: §10.5; ENG I6 ordering.

**TC-ENG-03: Materialization check**
- Layer: integration
- Setup: Fake LLM that records the context it was asked to process; an
  exchange involving memory retrieval and a tool result.
- Action: run the exchange; run the Assembly Procedure on the captured
  event path.
- Expected: the assembled context equals what the Fake LLM was asked to
  process, byte-for-byte (modulo normalized fields), for 3 diverse
  scenarios.
- Verifies: §10.5; ENG I3 (Materialization Rule).

**TC-ENG-04: Diagnostic error does not end exchange**
- Layer: integration
- Setup: Fake LLM requests a tool call; the tool is scripted to raise.
- Action: `execute(...)`.
- Expected: a `tool.failed` (Diagnostic) event is emitted; the exchange
  continues (the model gets to react); no exception raised to the caller.
- Verifies: §10.5; ENG I7; E-I8.

**TC-ENG-05: Fatal error ends exchange and raises (sync)**
- Layer: integration
- Setup: Fake LLM scripted to fail all retries.
- Action: `execute(...)`.
- Expected: `thread.exchange_failed` emitted with
  `model.retry_exhausted`; `execute` raises `AgentExecutionError`
  carrying the failed event.
- Verifies: §10.5; ENG I7; E-I9.

**TC-ENG-06: Fatal error ends stream (streaming)**
- Layer: integration
- Setup: same as TC-ENG-05 but via `execute_stream`.
- Action: iterate the stream.
- Expected: the final yielded event is `thread.exchange_failed`; the
  iterator ends; no exception raised.
- Verifies: ENG streaming failure semantics.

**TC-ENG-07: Authorization denial at pre-flight**
- Layer: integration
- Setup: an authz rule denying `agent.execute.<id>`.
- Action: `execute(...)`.
- Expected: emits `thread.exchange_failed` with `agent.unauthorized`;
  raises; NO model.request or tool events emitted (denied before run).
- Verifies: §10.5; pre-flight authz (R5).

**TC-ENG-08: Statelessness across calls**
- Layer: integration
- Setup: run one exchange; capture the log; construct a fresh engine
  instance (simulating restart).
- Action: run a second exchange on the same thread with the fresh engine.
- Expected: the second exchange continues correctly (leaf computed from
  log; context assembled from log); golden sequence for turn 2 matches.
- Verifies: §10.5; ENG I8.

**TC-ENG-09: Cancellation mid-exchange**
- Layer: integration
- Setup: a CancelToken; Fake LLM with a slow scripted response; cancel
  during execution.
- Action: `execute_stream(...)`, cancel partway.
- Expected: `thread.exchange_failed` with `exchange.cancelled` cause;
  stream ends cleanly.
- Verifies: cancellation.

**TC-ENG-10: Force-invoke emits synthetic read**
- Layer: integration
- Setup: a config with an enabled skill; a trigger of kind
  `user_message_with_force_skill` naming that skill.
- Action: `execute(...)`.
- Expected: before the user message is processed, a synthetic
  `tool.called`/`tool.result` for `read` on the SKILL.md path appears in
  the log; the skill body is in the assembled context.
- Verifies: §10.5; force-invoke (R4, R5 step 7).

**TC-ENG-11: Skills listing appended to system prompt**
- Layer: integration
- Setup: a config with two enabled skills, one with
  `disable-model-invocation: true`.
- Action: run an exchange; inspect the assembled system prompt (via the
  Fake LLM's recorded context).
- Expected: the non-disabled skill appears in the listing; the disabled
  one does not.
- Verifies: §10.5; Skills §5.1 (R5 step 10).

---

## Part 6 — Memory Provider (MEM)

**TC-MEM-01: Cache rebuild equals pre-restart state**
- Layer: component
- Setup: a session with several `memory.written` and one
  `memory.deleted` event in its log.
- Action: bind a context (rebuild from log); snapshot retrieval results;
  bind a fresh context (simulate restart); snapshot again.
- Expected: the two retrieval snapshots are identical.
- Verifies: §10.6; MEM I3.

**TC-MEM-02: Write applies configured tag**
- Layer: component
- Setup: a context bound with `write_tag: "notes/research"`.
- Action: `context.write("content")`.
- Expected: the returned item carries tag `notes/research`.
- Verifies: §10.6; MEM I4.

**TC-MEM-03: User cannot override write tag**
- Layer: component
- Setup: a context bound with a write_tag.
- Action: attempt to write with an overriding tag in metadata.
- Expected: the configured write_tag is applied; the override is ignored
  or rejected.
- Verifies: §10.6; MEM I4.

**TC-MEM-04: Write without write_tag disabled**
- Layer: component
- Setup: a context bound with no write_tag.
- Action: `context.write(...)`.
- Expected: raises `memory.write_disabled`.
- Verifies: failure mode.

**TC-MEM-05: Wildcard write_tag rejected at validation**
- Layer: unit
- Setup: a memory config with `write_tag: "research/*"`.
- Action: run the variant's Layer 3 validator.
- Expected: validation error (wildcards not allowed in write_tag).
- Verifies: §10.6; MEM I5.

**TC-MEM-06: read_tags wildcard matches hierarchically**
- Layer: component
- Setup: items tagged `research/a`, `research/b`, `other/c`; context
  with `read_tags: ["research/*"]`.
- Action: `context.retrieve()`.
- Expected: research/a and research/b returned; other/c not.
- Verifies: tag filtering.

**TC-MEM-07: Session isolation**
- Layer: component
- Setup: two sessions A and B, each with written items.
- Action: retrieve in session A.
- Expected: only A's items appear; none of B's.
- Verifies: §10.6; MEM I7.

**TC-MEM-08: delete unknown item raises**
- Layer: component
- Setup: a bound context.
- Action: `context.delete("mem_nonexistent")`.
- Expected: raises `memory.item_not_found`.
- Verifies: failure mode.

**TC-MEM-09: Memory does not emit events**
- Layer: unit
- Setup: inspect the Memory provider module.
- Action: static check / code inspection that Memory does not import or
  call the store's event-emission methods.
- Expected: no event emission from Memory; the engine emits memory events.
- Verifies: MEM I1.

---

## Part 7 — Secrets / Sandbox / Authorization (SEC / SBX / AUZ)

**TC-SEC-01: resolve returns value**
- Layer: component
- Setup: a `.env` with `KEY=value`; dotenv variant initialized.
- Action: `resolve("KEY")`.
- Expected: returns `"value"`.
- Verifies: §10.7 functional.

**TC-SEC-02: Secret never appears in logs/events**
- Layer: integration
- Setup: a tool needing a secret; run an exchange that invokes it; the
  secret has a distinctive value.
- Action: scan all captured logs and all emitted events for the secret
  value.
- Expected: the value appears nowhere.
- Verifies: §10.7; S-I1.

**TC-SEC-03: Missing secret raises**
- Layer: component
- Action: `resolve("ABSENT")`.
- Expected: raises `secret.not_found`.
- Verifies: failure mode.

**TC-SBX-01: Native sandbox passes result through**
- Layer: component
- Setup: a `work` callable returning a known value.
- Action: `sandbox.execute(work, policy)` vs. calling `work()` directly.
- Expected: identical results.
- Verifies: §10.7; Sb-I1.

**TC-SBX-02: Native sandbox passes exceptions through**
- Layer: component
- Setup: a `work` that raises.
- Action: `sandbox.execute(work, policy)`.
- Expected: the same exception propagates unchanged.
- Verifies: §10.7; Sb-I2.

**TC-AUZ-01: Default allow-all permits**
- Layer: unit
- Setup: rules `[{who: ".*", what: ".*", effect: "allow"}]`.
- Action: `allow("user.local", "agent.execute.x")`.
- Expected: `allowed == True`.
- Verifies: §10.7 functional.

**TC-AUZ-02: Deny before allow wins (first-match)**
- Layer: unit
- Setup: rules `[deny tool.bash, allow .*]`.
- Action: `allow("agent.x", "tool.bash")`.
- Expected: `allowed == False`; matched rule is the deny.
- Verifies: §10.7; first-match-wins.

**TC-AUZ-03: Default deny when no match**
- Layer: unit
- Setup: rules `[allow tool.read]` only.
- Action: `allow("agent.x", "tool.bash")`.
- Expected: `allowed == False`.
- Verifies: §10.7; A-I3.

**TC-AUZ-04: Regex is fullmatch**
- Layer: unit
- Setup: rule `{who: ".*", what: "tool.b", effect: "deny"}`.
- Action: `allow("agent.x", "tool.bash")`.
- Expected: the rule does NOT match (`tool.b` ≠ `tool.bash` under
  fullmatch); falls through.
- Verifies: §10.7; A-I1.

**TC-AUZ-05: Malformed regex fails at init**
- Layer: component
- Setup: a rule with an invalid regex.
- Action: initialize the authz provider.
- Expected: raises `authz.regex_invalid` at init, not at allow-time.
- Verifies: failure mode.

---

## Part 8 — Tools Subsystem (TLS)

**TC-TLS-01: Dispatch order**
- Layer: component
- Setup: a tool; instrument authz, secrets, sandbox, and the tool fn to
  record call order.
- Action: `invoke_tool(...)`.
- Expected: order is validate-args → authz → secrets → sandbox → execute.
- Verifies: §10.8; Tools §6.2.

**TC-TLS-02: Bad args rejected before execution**
- Layer: component
- Setup: a tool with a typed args_schema; invoke with args failing the
  schema.
- Action: `invoke_tool(...)`.
- Expected: raises `tool.invalid_arguments`; the tool function never runs.
- Verifies: §10.8.

**TC-TLS-03: Authorization denial surfaces as diagnostic**
- Layer: integration
- Setup: authz denies `tool.bash`; a config with bash; Fake LLM requests
  a bash call.
- Action: run the exchange.
- Expected: a `tool.failed`/unauthorized Diagnostic event; the exchange
  continues.
- Verifies: §10.8.

**TC-TLS-04: Tool exception becomes tool.execution_failed**
- Layer: integration
- Setup: a tool scripted to raise.
- Action: invoke via an exchange.
- Expected: `tool.failed` with `tool.execution_failed` (Diagnostic);
  exchange continues.
- Verifies: §10.8.

**TC-TLS-05: Missing secret fails registration**
- Layer: component
- Setup: a tool declaring `secrets_needed: ["ABSENT"]`.
- Action: `register_tools_for_agent(...)`.
- Expected: raises `tool.setup_failed` with `secret.not_found` in the
  cause chain.
- Verifies: §10.8; propagation.

**TC-TLS-06: Verbatim result capture**
- Layer: integration
- Setup: a tool returning a known structured value.
- Action: invoke via an exchange; inspect the `tool.result` event.
- Expected: `data.result` equals the tool's return value byte-for-byte
  (for non-oversized results).
- Verifies: §10.8; T-I5.

**TC-TLS-07: Builtin read honors path policy**
- Layer: component
- Setup: path policy allowing `data/` only; attempt to read
  `data/x.txt` (allowed) and `/etc/passwd` (denied).
- Action: invoke `read` on each.
- Expected: allowed read succeeds; denied read raises
  `file_access.denied`.
- Verifies: §10.8; Fh-I2.

**TC-TLS-08: Lazy MCP server starts on first use**
- Layer: component
- Setup: an MCP server configured lazy; a stub MCP server process.
- Action: register (server not started), then invoke an MCP tool.
- Expected: the server starts on first invocation; the tool resolves.
- Verifies: MCP lazy behavior.

---

## Part 9 — Skills Subsystem (SKL)

**TC-SKL-01: Enabled skill in prompt listing**
- Layer: component
- Setup: a config enabling a skill (not disabled).
- Action: `get_enabled_skills(...)`; `format_skills_listing(...)`.
- Expected: the skill's name and description appear in the listing.
- Verifies: §10.9.

**TC-SKL-02: disable-model-invocation excluded from listing**
- Layer: component
- Setup: two enabled skills, one with `disable-model-invocation: true`.
- Action: `format_skills_listing(...)`.
- Expected: the disabled one is absent from the listing; the other is
  present.
- Verifies: §10.9; Sk-I2.

**TC-SKL-03: Disabled skill still force-invocable**
- Layer: integration
- Setup: an enabled skill with `disable-model-invocation: true`; a
  force-invoke trigger naming it.
- Action: run the exchange.
- Expected: force-invoke succeeds (synthetic read emitted); the body is
  in context.
- Verifies: §10.9; Sk-I2.

**TC-SKL-04: Force-invoke of non-enabled skill rejected**
- Layer: integration
- Setup: a force-invoke trigger naming a skill NOT in the enabled list.
- Action: `execute(...)`.
- Expected: raises `skill.forced_not_enabled` (Fatal).
- Verifies: §10.9.

**TC-SKL-05: Force-invoke uses tool.read authorization**
- Layer: integration
- Setup: authz denies `tool.read`; a force-invoke trigger.
- Action: run the exchange.
- Expected: the synthetic read is subject to `tool.read` authorization
  (denied here), not a skill-specific action.
- Verifies: Sk-I4.

---

## Part 10 — Error Model (ERR)

**TC-ERR-01: All subclasses serialize to envelope**
- Layer: unit
- Setup: enumerate all `PlatformError` subclasses.
- Action: construct each (with minimal valid args) and call `to_dict()`.
- Expected: each produces a dict with type, message, where, detail, cause
  keys.
- Verifies: §10.10; E-I1.

**TC-ERR-02: Cause chain serializes recursively**
- Layer: unit
- Setup: error A caused by B caused by C (3-deep, via `raise ... from`).
- Action: `A.to_dict()`.
- Expected: `cause` nests B, whose `cause` nests C, whose `cause` is None.
- Verifies: §10.10; E-I4.

**TC-ERR-03: Non-platform exception wrapped**
- Layer: unit
- Setup: a stdlib `FileNotFoundError`.
- Action: `WrappedError.from_exception(exc, where="...")`; `to_dict()`.
- Expected: type is `platform.wrapped`; detail records the original
  type and args.
- Verifies: §10.10; E-I5.

**TC-ERR-04: Every emitted type is in the catalog**
- Layer: unit
- Setup: the set of registered catalog codes; a collection of all error
  classes' `code` constants.
- Action: assert every class code is in the catalog set.
- Expected: no unregistered codes.
- Verifies: §10.10; E-I2.

**TC-ERR-05: Level is intrinsic to type**
- Layer: unit
- Setup: each error class's `level` constant.
- Action: assert each type has exactly one level and it matches the
  catalog.
- Expected: consistent.
- Verifies: E-I3.

---

## Part 11 — File Handling (FIL)

**TC-FIL-01: Allowed path permitted**
- Layer: unit
- Setup: policy allowing `data/`.
- Action: `is_path_allowed("data/x.txt")`.
- Expected: `(True, None)`.
- Verifies: §10.11.

**TC-FIL-02: Unmatched path default-denied**
- Layer: unit
- Setup: policy allowing `data/` only.
- Action: `is_path_allowed("/tmp/x")`.
- Expected: `(False, "default deny")`.
- Verifies: §10.11; default-deny.

**TC-FIL-03: Deny overrides allow**
- Layer: unit
- Setup: policy with `.env` in denied and platform-root in allowed.
- Action: `is_path_allowed(".env")`.
- Expected: `(False, ...)` — denied even though under an allowed root.
- Verifies: §10.11; Fh-I3.

**TC-FIL-04: Adding .env to allowed does not grant**
- Layer: unit
- Setup: policy where `.env` is in BOTH allowed and denied.
- Action: `is_path_allowed(".env")`.
- Expected: `(False, ...)` — deny wins.
- Verifies: §10.11; Fh-I3.

**TC-FIL-05: Symlink to denied is denied**
- Layer: component
- Setup: a symlink `data/link` → `.env` (denied); policy allowing
  `data/`.
- Action: `is_path_allowed("data/link")`.
- Expected: `(False, ...)` — resolved to the denied target.
- Verifies: §10.11; Fh-I4.

**TC-FIL-06: bash bypasses path policy (documented)**
- Layer: integration
- Setup: a config with bash; a bash command that touches a path outside
  the policy.
- Action: run the exchange.
- Expected: bash executes the command (policy NOT enforced); this is the
  documented v1 limitation, asserted explicitly so a future sandbox
  change flips this test.
- Verifies: §10.8 bash exception; Fh-I6.

**TC-FIL-07: Tools never write to content/**
- Layer: unit
- Setup: code inspection / a runtime guard.
- Action: assert no builtin tool's write path targets `content/`.
- Expected: none do.
- Verifies: Fh-I5.

---

## Part 11.5 — CrewAI Adapter (CRW) — authored in Step 12b

Tests specific to the `CrewAIFramework` adapter that wraps real CrewAI.
These are NOT framework-agnostic — they pin adapter-specific behavior
that wouldn't appear against FakeFramework. Authored when Step 12b is
worked; the exact cases will be informed by Step 12-Prereq's design
note and what the adapter implementation reveals.

The cases below are the minimum required; Step 12b adds more as needed
when concrete CrewAI quirks surface.

**TC-CRW-01: CrewAI lifecycle event ordering matches Protocol**
- Layer: component (CrewAIFramework only; no real LLM via the Fake-LLM
  injection)
- Setup: a scripted Fake LLM injected into CrewAIFramework; a simple
  agent task.
- Action: capture CrewAI's lifecycle events in order; map them to the
  Framework Protocol's hook ordering expectations.
- Expected: CrewAI's actual ordering satisfies the Protocol's
  before/after-call requirements. Any pre-call observability the
  Protocol demands must be delivered by CrewAI (possibly via litellm
  injection or callback) — confirm the mechanism works.
- Verifies: §10.5 + Step 12-Prereq Protocol design.

**TC-CRW-02: Exact message-list capture (CrewAI Materialization)**
- Layer: integration
- Setup: a scenario with system prompt + skills listing + memory
  retrieval + a tool result, driven through CrewAIFramework with the
  Fake LLM injected.
- Action: record the EXACT message list CrewAI hands to the LLM at
  every model call; compare to what the engine's emitted `model.request`
  events captured.
- Expected: byte-for-byte equality across all model calls. If CrewAI
  mutates the message list before sending (template wrapping, role
  normalization, system-prompt manipulation), the engine must capture
  what CrewAI actually sent — not what it was handed. If the mutation
  can't be observed cleanly, that's a finding for human review.
- Verifies: Materialization Rule end-to-end through CrewAI (the
  architectural bet). GATING.

**TC-CRW-03: Retry composition with RuntimeSpec.max_retries**
- Layer: integration
- Setup: a Fake LLM scripted to fail twice then succeed; an agent
  config with `runtime.max_retries: 3`.
- Action: run the exchange.
- Expected: regardless of CrewAI's own internal retry behavior, the
  total attempt count and the emitted retry-sibling events in the log
  match the platform's contract. Document any composition behavior
  (e.g., CrewAI retries internally then we retry on top — or we
  override CrewAI's retry).
- Verifies: Recoverable-level error handling under a real framework.

**TC-CRW-04: Concurrent tool callbacks do not race**
- Layer: integration
- Setup: a Fake LLM scripted to request multiple tool calls in one
  response (if CrewAI supports parallel tool dispatch in the chosen
  version); two scripted tools that take measurable time.
- Action: run the exchange; observe the emitted `tool.called` and
  `tool.result` events.
- Expected: no two events share a `parent_event_id` (linear chain);
  the engine's per-thread serialization handles whatever concurrency
  CrewAI actually produces.
- Verifies: ENG concurrent-callback handling under a real framework.

**Additional cases reserved.** Step 12-Prereq's design note may identify
adapter risks not covered above. Step 12b adds the corresponding test
cases (TC-CRW-05+) before merging.

---

## Part 11.6 — CLI surface (TC-CLI-*) — authored in Step 13

Tests for the `kolsch` console script. The CLI runs in-process
via `cli.main(argv)`; no subprocess spawning. Each test uses a
tmp_path platform root and the v1 default tier (SQLite + in-process
bus + EchoLLM + FakeFramework).

**TC-CLI-01: `kolsch run` end-to-end with EchoLLM**
- Layer: component
- Setup: a tmp_path platform root with one demo agent.
- Action: `kolsch run acme/agents/demo --input content=hello`.
- Expected: exit 0; stdout carries the agent.message JSON with
  `"Echo: hello"` as `content.text`.
- Verifies: v1-scope §2.5 first-run experience.

**TC-CLI-02: `--stream` yields events in engine order**
- Layer: component
- Setup: same as TC-CLI-01.
- Action: same run with `--stream`.
- Expected: each event prints as a JSON line in spec §1.4 order
  (exchange_started → user.message → model.request → model.response
  → agent.message → exchange_completed).
- Verifies: ENG I6 + CLI streaming surface.

**TC-CLI-03: `sessions list` reflects engine runs**
- Layer: component
- Setup: run one exchange via TC-CLI-01.
- Action: `kolsch sessions list`.
- Expected: stdout includes the session id, agent_id, and the
  `cli:<agent_ref>` name.
- Verifies: Session entity surface + CLI session list.

**TC-CLI-04: `reconstruct` rebuilds entities after DB wipe**
- Layer: component
- Setup: run an exchange; delete the SQLite entity DB.
- Action: `kolsch reconstruct`.
- Expected: exit 0; reconstruct result reports
  `sessions_reconstructed >= 1` and `threads_reconstructed >= 1`.
- Verifies: §10.2 (Store reconstruct) + CLI.

**TC-CLI-05: `sessions events <id>` prints the per-session log**
- Layer: component
- Setup: run an exchange; capture the session id from list.
- Action: `kolsch sessions events <session_id>`.
- Expected: stdout contains JSON lines for `session.opened` ...
  `agent.message`.
- Verifies: Store.read_events surface via CLI.

**TC-CLI-06: no-command prints help with exit 1**
- Layer: component
- Action: `kolsch` (no subcommand).
- Expected: stdout contains `usage:`; exit code 1.
- Verifies: CLI ergonomics.

**TC-CLI-07: `sessions update` + `sessions delete`**
- Layer: component
- Setup: run an exchange to create a session.
- Action: `sessions update <id> --name renamed --star`, then
  `sessions delete <id>` twice (verify idempotency).
- Expected: update prints `starred=True name=renamed`; both deletes
  exit 0 (the second is a no-op per Store §2.5).
- Verifies: CRUD-lite surface on the CLI + delete idempotency.

**TC-CLI-08: streaming exit code 2 on Fatal**
- Layer: component
- Setup: platform.yaml with authz denying agent.execute.*; the demo
  agent.
- Action: `kolsch run ... --stream`.
- Expected: exit 2; the streamed JSON includes a
  `thread.exchange_failed` event. (The CLI must distinguish
  stream-success from stream-with-Fatal-final so shell pipelines can
  branch on `$?`.)
- Verifies: §1.6 Fatal semantics surfaced through the CLI.

---

## Part 11.7 — HTTP API surface (TC-API-*) — authored in Step 13

Tests for the FastAPI app built by `api.build_app(runtime)`. Run via
`fastapi.testclient.TestClient` — in-process, no network, no
uvicorn. Same v1 default tier as CLI tests.

**TC-API-01: `GET /health`**
- Layer: component
- Action: GET /health.
- Expected: 200 with `{"status": "ok"}`.

**TC-API-02: Create + list sessions**
- Layer: component
- Action: POST /sessions then GET /sessions.
- Expected: 201 on create with `ses_*` id; list includes the new
  session.

**TC-API-03: Execute agent (sync)**
- Layer: component
- Action: POST /agents/{ref}/execute.
- Expected: 200 with `final_event_type=agent.message` and
  `data.content.text="Echo: hello"`.

**TC-API-04: `execute_stream` emits SSE in engine order**
- Layer: component
- Action: POST /agents/{ref}/execute_stream.
- Expected: response is `text/event-stream`; SSE events arrive in
  engine order; trailing synthetic `event: done` terminator after
  the final engine event.
- Verifies: ENG I6 + SSE seam.

**TC-API-05: `GET /sessions/{id}/events`**
- Layer: component
- Action: run an exchange, then GET events.
- Expected: returns the JSONL contents (including `session.opened`
  and `agent.message`).

**TC-API-06: file_ref input outside path policy → 400**
- Layer: component
- Setup: POST /agents/{ref}/execute with `inputs={"secret_path":
  "/etc/passwd"}`.
- Expected: 400 (FileAccessDenied at the API boundary; the engine
  never sees the input).
- Verifies: Fh-I8 at the HTTP boundary.

**TC-API-07: Unknown agent ref → 404**
- Layer: component
- Action: POST to `/agents/<unknown>/execute`.
- Expected: 404.

**TC-API-08: `PATCH /sessions/{id}`**
- Layer: component
- Action: PATCH with `{name, starred}`.
- Expected: 200 with updated fields; unchanged fields preserved.

**TC-API-09: `DELETE /sessions/{id}` removes the session**
- Layer: component
- Action: DELETE then GET /sessions/{id}/events.
- Expected: 204 on delete; 404 on the subsequent events GET (Store
  no longer recognises the session).

**TC-API-10: Layer 3 validators wired through the API path**
- Layer: component
- Setup: an agent with `subsystems.memory.config.write_tag = "bad/*"`
  (MEM I5 — wildcards forbidden).
- Action: POST /agents/{ref}/execute.
- Expected: 400 with the canonical ConfigInvalid error envelope on
  `detail.error`; the validation errors array contains
  `wildcard_in_write_tag`. The engine never sees the broken config —
  Layer 3 fired before pre-flight.
- Verifies: Steps 8/10/11's Layer 3 validators are actually called
  on the production execute paths (Step 13 review #1).

---

## Part 12 — Integration Scenarios (INT)

Cross-subsystem flows with the Fake LLM and the configured Framework.
These are where contract mismatches surface. Most cases here use
FakeFramework for determinism; TC-INT-07 (backend swap) and the
CrewAI-specific variants run against CrewAIFramework after Step 12b.

**TC-INT-01: Full single-agent exchange end to end (fake LLM)**
- Layer: integration
- Setup: a complete agent config (tools, one skill, memory); Fake LLM
  scripted for a realistic flow (think → tool call → think → answer).
- Action: `execute(...)`; capture everything.
- Expected: events persisted to the log; golden sequence matches; the
  final agent.message is returned; memory and tools integrated correctly.
- Verifies: the whole Phase 1 happy path.

**TC-INT-02: Store + engine — events durable after return**
- Layer: integration
- Setup: an exchange.
- Action: run `execute(...)`; then independently read the JSONL.
- Expected: every event the engine emitted is on disk in order after
  return.
- Verifies: ENG I1 + Store integration.

**TC-INT-03: Memory write/retrieve across a turn**
- Layer: integration
- Setup: a two-turn exchange; turn 1 writes a memory item; turn 2
  retrieves.
- Action: run both turns.
- Expected: turn 2's assembled context includes the item written in
  turn 1.
- Verifies: Memory + engine integration.

**TC-INT-04: Authorization gates a tool mid-exchange**
- Layer: integration
- Setup: authz allows the agent but denies one specific tool; Fake LLM
  tries that tool then an allowed one.
- Action: run.
- Expected: denied tool → Diagnostic tool.failed; allowed tool →
  tool.result; exchange completes.
- Verifies: Authz + Tools + engine.

**TC-INT-05: Error propagation across modules**
- Layer: integration
- Setup: a tool whose setup needs a missing secret.
- Action: attempt to run the agent.
- Expected: setup fails with `tool.setup_failed`, cause chain shows
  `secret.not_found`; surfaced as a Fatal exchange failure with the full
  chain in the event.
- Verifies: Error Model + Secrets + Tools + engine.

**TC-INT-06: Resume after restart continues correctly**
- Layer: integration
- Setup: run turn 1; discard all in-memory state; build fresh
  instances of every module; run turn 2.
- Action: both turns.
- Expected: turn 2 reconstructs leaf, config, and memory from the log;
  proceeds correctly; golden for turn 2 matches.
- Verifies: filesystem-as-truth across the whole stack; ENG I8.

**TC-INT-07: Backend swap is transparent**
- Layer: integration
- Setup: run TC-INT-01 with (a) SQLite + in_process bus, then (b)
  Postgres + redis bus (containers).
- Action: run the same scenario against both tiers.
- Expected: identical event sequences and outcomes.
- Verifies: storage modularity; DB-R1, DB-R2.

---

## Part 13 — End-to-End (E2E)

Whole platform, real infrastructure, real LLM. Slow, few, deliberate.
These map to the overall validation tests in the test plan.

**TC-E2E-01: Feasibility — CrewAIFramework honors the contract end-to-end**
- Layer: e2e
- Setup: the engine wired to `CrewAIFramework` (from Step 12b), real
  CrewAI, real LLM, three diverse agent tasks.
- Action: run each; capture emitted events and the actual LLM inputs
  CrewAI sent.
- Expected: every required event emitted with verbatim content; the
  Assembly Procedure's output matches actual LLM input across all three
  tasks; event ordering holds. This is the architectural gate located
  at the adapter layer — does CrewAI's real hook behavior + message
  handling support our contract end-to-end with a real model?
- Verifies: Test Plan Test 1 (Feasibility). GATING. Companion to
  TC-CRW-02 (which checks the same property with a Fake LLM); this is
  the real-LLM confirmation.

**TC-E2E-02: Soundness — better than baseline**
- Layer: e2e
- Setup: 5–10 representative tasks built as (a) v1 platform agents and
  (b) baseline CrewAI agents; identical inputs.
- Action: run both; human or LLM-judge comparison.
- Expected: v1 outputs better on a majority (more grounded, better tool
  use, better recovery).
- Verifies: Test Plan Test 2 (Soundness).

**TC-E2E-03: Robustness — survives real use**
- Layer: e2e
- Setup: the platform in real use over a defined period / session count.
- Action: track crashes, corruption, resume correctness, memory leaks.
- Expected: zero data corruption; crashes recoverable; long sessions
  don't degrade.
- Verifies: Test Plan Test 3 (Robustness).

**TC-E2E-04: Default install needs no external services**
- Layer: e2e
- Setup: a clean machine with only Python + uv; no Postgres, no Redis.
- Action: install and run a simple agent end to end (default tier).
- Expected: it works — SQLite + in-process bus, zero external services.
- Verifies: the "mature with scale" decision; first-run experience.

---

## Part 14 — Coverage Map

How the catalog maps to the success criteria (Handbook §10):

| Module | Success criteria (§10) | Catalog cases |
|--------|------------------------|---------------|
| Kernel | §10.1 | TC-KRN-01..10 |
| Asset Mgmt | §10.2 | TC-ASM-01..12 |
| Store | §10.3 | TC-STR-01..12 |
| Event Bus | §10.4 | TC-BUS-01..07 |
| Engine | §10.5 | TC-ENG-01..11 (framework-agnostic; run against FakeFramework in 12a and CrewAIFramework in 12b) |
| CLI | §10.12 | TC-CLI-01..08 |
| HTTP API | §10.13 | TC-API-01..10 |
| Memory | §10.6 | TC-MEM-01..09 |
| Secrets/Sandbox/Authz | §10.7 | TC-SEC/SBX/AUZ-* |
| Tools | §10.8 | TC-TLS-01..08 |
| Skills | §10.9 | TC-SKL-01..05 |
| Error Model | §10.10 | TC-ERR-01..05 |
| File Handling | §10.11 | TC-FIL-01..07 |
| CrewAI Adapter | §10.5 (CrewAI conformance) | TC-CRW-01..04+ (authored in Step 12b) |
| Integration | (cross) | TC-INT-01..07 |
| Overall validation | Test Plan §12 | TC-E2E-01..04 |

Every §10 success criterion has at least one catalog case. Every
catalog case names the criterion or invariant it verifies. This
two-way traceability is the point: a reviewer can confirm coverage by
walking either direction.

---

## Part 15 — Using and Extending the Catalog

**Implementing:** turn each case into a real test in the layer named.
The Setup/Action/Expected map directly to arrange/act/assert. Use the
fixtures and assertion helpers from the Handbook §8.5–8.6.

**Extending:** when a bug is found, add a regression case here (and in
code) before fixing — the case reproduces the bug, the fix makes it
pass. New cases get the next number in their module's series.

**Traceability:** keep the `Verifies:` line accurate. If a success
criterion changes, grep the catalog for cases verifying it and update
them. If a new criterion is added (§10), add at least one case here.

**What this catalog is NOT:** exhaustive. It covers the ground that
matters — the contract obligations, the invariants, the integration
seams, the failure modes. Implementers will and should add edge cases,
property-based tests, and fuzzing where valuable. This catalog is the
floor, not the ceiling.
