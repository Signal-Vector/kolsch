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

## Part 9.5 — LLM Provider Configuration (LLM) — Phase 1.1, LLM-R8

**TC-LLM-01: Alias resolves to canonical**
- Layer: unit
- Setup: `platform.yaml` with `model_aliases: {opus:
  anthropic/claude-opus-4-7}`.
- Action: kernel resolver normalizes a `ModelSpec.model = "opus"`.
- Expected: result is `{provider: "anthropic", model: "claude-opus-4-7"}`.
- Verifies: §10.12; Schemas §A.4.1.

**TC-LLM-02: Unknown alias raises `config.unknown_alias`**
- Layer: unit
- Setup: `model_aliases` does not contain `mystery`.
- Action: kernel resolver normalizes a `ModelSpec.model = "mystery"`.
- Expected: raises `config.unknown_alias` (Fatal) with the available
  aliases in `detail`.
- Verifies: §10.12; Error Model §4.13.

**TC-LLM-03: Unconfigured provider raises `llm.provider_not_configured`**
- Layer: component
- Setup: `llm_providers` empty.
- Action: `build_llm({provider: "anthropic", model: "x"}, ...)`.
- Expected: raises `llm.provider_not_configured` with the
  configured providers list in `detail`.
- Verifies: §10.12; Error Model §4.13.

**TC-LLM-04: Missing API key raises `llm.secret_missing` with cause**
- Layer: component
- Setup: `llm_providers.openrouter.api_key_secret = "MISSING_KEY"`;
  Secrets provider has no such key.
- Action: `build_llm(...)`.
- Expected: raises `llm.secret_missing` (Fatal); `__cause__` is
  `secret.not_found` carrying `MISSING_KEY` in `detail.name`.
- Verifies: §10.12; Error Model §4.13 + cross-module cause chain.

**TC-LLM-05: OpenCodeConnector `discover_models` returns expected shape**
- Layer: component
- Setup: recorded fixture HTTP response (the OpenCode /v1/models
  payload).
- Action: `OpenCodeConnector().discover_models(creds, base_url)`
  with the mocked transport.
- Expected: returns `list[ModelEntry]` with `id` populated and all
  parameter fields `None`.
- Verifies: §10.12; ProviderConnector contract.

**TC-LLM-06: OpenRouterConnector implements both Protocols**
- Layer: component
- Action: `isinstance(OpenRouterConnector(), ProviderConnector)` and
  `isinstance(..., MetadataSourceConnector)` both True; functional
  call to `lookup_model_metadata("anthropic", "claude-opus-4-7")`
  with a fixtured catalog returns a populated `ModelMetadata`.
- Expected: both isinstance checks pass; the metadata lookup
  populates `context_length`, `supports_tools`, `pricing`.
- Verifies: §10.12; the MetadataSourceConnector extension.

**TC-LLM-07: Connect flow augments OpenCode discovery with OpenRouter metadata**
- Layer: integration
- Setup: OpenRouter already connected (in `llm_providers` +
  `serves_as_metadata_source: true`).
- Action: run the `kolsch connect opencode` flow (in-process; the
  CLI's command function is called directly, no subprocess).
- Expected: OpenCode entries are written to `platform.yaml` with
  parameters populated from OpenRouter's catalog where matches
  exist; entries without OpenRouter matches keep `null` parameters.
- Verifies: §10.12; the connect-flow augmentation loop.

**TC-LLM-08: Connect flow runs cleanly without OpenRouter**
- Layer: integration
- Setup: only OpenCode is being connected; no metadata source
  available.
- Action: run the connect flow.
- Expected: completes successfully; all parameter fields are `null`;
  no error / no warning treats the absence of a metadata source as
  failure.
- Verifies: §10.12; metadata augmentation is opt-in, never required.

**TC-LLM-09: Registry rejects duplicate `register` calls**
- Layer: unit
- Action: `registry.register("opencode", X)` then
  `registry.register("opencode", Y)`.
- Expected: second call raises a `PlatformError` naming the type
  collision.
- Verifies: §10.12; registry hygiene.

**TC-LLM-10: API key value never appears in `platform.yaml` or events**
- Layer: integration
- Setup: known API key value (e.g. `"sk-test-canary-12345"`); run
  the connect flow for OpenCode.
- Action: after the flow completes, grep `platform.yaml` and every
  event in the session log for the canary string.
- Expected: zero hits anywhere. The secret NAME appears in
  `platform.yaml` (`api_key_secret: OPENCODE_API_KEY`); the VALUE
  lives only in `.env`.
- Verifies: §10.12; Materialization Rule's "secrets never in events"
  extension to provider config.

---

## Part 9.6 — Workspace Resolution & Setup (UX) — Phase 1.2, UX-R9

**TC-UX-01: `resolve_workspace(--root)` returns the flag value**
- Layer: unit
- Setup: `KOLSCH_WORKSPACE` unset; `cli_root=Path("/tmp/foo")`.
- Action: `resolve_workspace(cli_root=Path("/tmp/foo"))`.
- Expected: returns `Path("/tmp/foo").resolve()`.
- Verifies: §10.13; resolution-chain precedence #1.

**TC-UX-02: `resolve_workspace()` with `KOLSCH_WORKSPACE` set returns the env value**
- Layer: unit
- Setup: `KOLSCH_WORKSPACE=/tmp/from-env`; no `cli_root` passed.
- Action: `resolve_workspace()`.
- Expected: returns `Path("/tmp/from-env").resolve()`.
- Verifies: §10.13; resolution-chain precedence #2.

**TC-UX-03: `resolve_workspace()` with neither set returns the default**
- Layer: unit
- Setup: `KOLSCH_WORKSPACE` unset; no `cli_root` passed.
- Action: `resolve_workspace()`.
- Expected: returns
  `Path(platformdirs.user_documents_dir(), "kolsch").resolve()`.
- Verifies: §10.13; resolution-chain precedence #3.

**TC-UX-04: CLI emits the friendly message on missing workspace, exits 3**
- Layer: integration
- Setup: a temp directory with no `platform.yaml`.
- Action: run `kolsch run <agent>` against that directory via the
  CLI dispatcher (in-process; `argparse` namespace + dispatch call).
- Expected: stderr contains the friendly "No Kolsch workspace at
  …. Run `kolsch setup`" message and NO Python traceback; exit
  code is `3`.
- Verifies: §10.13; Phase 1.2 patch §1.4; error code
  `workspace.not_initialized`.

**TC-UX-05: `kolsch setup --non-interactive` produces a runnable workspace**
- Layer: integration
- Setup: a fresh temp directory; no provider env vars.
- Action: run `kolsch setup --non-interactive --root <tmp>` in
  process.
- Expected: `workspace_is_initialized(<tmp>)` returns `True`; the
  starter agent file exists at
  `bundles/starter/agents/helper.yaml` with `provider: fake`; the
  test-run step succeeds against the fake LLM.
- Verifies: §10.13; Phase 1.2 patch §2.5 (non-interactive
  semantics).

**TC-UX-06: `kolsch setup` re-run is idempotent**
- Layer: integration
- Setup: a workspace previously produced by `kolsch setup
  --non-interactive`.
- Action: re-run `kolsch setup --non-interactive --root <same>`
  and compare file mtimes (or content hashes) before/after.
- Expected: no file written by the first run is modified by the
  second.
- Verifies: §10.13; Phase 1.2 patch §2.3 (idempotency rules).

**TC-UX-07: `kolsch setup` with provider env vars runs the connect flow**
- Layer: integration
- Setup: `KOLSCH_PROVIDER=opencode`,
  `KOLSCH_PROVIDER_KEY="sk-test"`; OpenCode connector wired with a
  mock transport returning a small `/v1/models` payload.
- Action: run `kolsch setup --non-interactive --root <tmp>`.
- Expected: `platform.yaml` contains an `llm_providers.opencode`
  block; `.env` contains `OPENCODE_API_KEY=sk-test`; key value
  does NOT appear in `platform.yaml`.
- Verifies: §10.13; provider-env convention (Asset Mgmt §1.4
  per UX-R5).

**TC-UX-08: starter agent lands at the expected path with the chosen model**
- Layer: integration
- Setup: a fresh temp directory; two scenarios — (a) no provider
  connected, (b) a connected provider exposing a `cheap` alias.
- Action: run `kolsch setup` interactively (driven by a stub
  prompter) selecting the relevant model option.
- Expected: (a) the agent file's `model` field equals
  `{provider: fake, model: fake-model}`; (b) the agent file's
  `model` field equals the user-selected alias string.
- Verifies: §10.13; Phase 1.2 patch §2.4 (starter agent file).

**TC-UX-09: ending setup at step D and re-running picks up at step D**
- Layer: integration
- Setup: a workspace produced by `kolsch setup` aborted (Ctrl-C
  simulation) after step C but before step D wrote the starter
  agent.
- Action: re-run `kolsch setup` against the same workspace.
- Expected: steps A–C report "already done, skipping"; step D
  prompts and runs.
- Verifies: §10.13; Phase 1.2 patch §2.3.

**TC-UX-10: `kolsch run starter/agents/helper` works against a setup-produced workspace**
- Layer: integration
- Setup: a workspace produced by `kolsch setup --non-interactive
  --root <tmp>` (no provider).
- Action: `kolsch run starter/agents/helper --input
  content="hello" --root <tmp>` in process.
- Expected: exit code 0; final `agent.message` event present in
  the session log.
- Verifies: §10.13; end-to-end smoke for the setup output.

---

## Part 9.7 — Model Fallback (FB) — Phase 1.3, FB-R8

**TC-FB-01: Recoverable error triggers fallback; secondary serves the reply**
- Layer: integration
- Setup: agent declares `fallback_models: [haiku]`. Primary
  (Opus) is wired (fake LLM) to raise `model.rate_limited` on
  the first call; Haiku returns success.
- Action: run the agent once.
- Expected: event log contains, in order, `model.request` (Opus),
  `model.fallback` (from=opus, to=haiku, reason=`model.rate_limited`),
  `model.request` (Haiku), `model.response` (success),
  `agent.message`. The `model.response.data.model` field names
  Haiku.
- Verifies: §10.14; engine fallback loop (patch §2.1); event
  shape (patch §3.1).

**TC-FB-02: Chain exhaustion raises `llm.all_models_failed`**
- Layer: integration
- Setup: chain of three models; all three raise Recoverable
  errors (rate-limited, server error, timeout) in sequence.
- Action: run the agent once.
- Expected: two `model.fallback` events; final
  `thread.exchange_failed` with cause `llm.all_models_failed`
  whose `detail.attempts` is the full ordered list of
  `(model, error_code, detail)` triples — primary + every chain
  entry.
- Verifies: §10.14; Error Model §4.15 cause-chain.

**TC-FB-03: Primary succeeds — chain untouched, no lazy clients built**
- Layer: integration
- Setup: agent declares `fallback_models: [haiku, gpt4o]`;
  primary (Opus) returns success on first call.
- Action: run the agent once + count `build_llm` calls.
- Expected: no `model.fallback` events; `build_llm` called
  exactly once (for the primary).
- Verifies: §10.14; lazy client construction (patch §2.1).

**TC-FB-04: Alias entries normalised to canonical at Layer 1**
- Layer: unit
- Setup: agent declares `fallback_models: ["haiku"]` (alias
  string). `model_aliases.haiku` resolves to
  `openrouter/anthropic/claude-haiku-4-5`.
- Action: kernel resolves the agent config.
- Expected: the resolved `fallback_models[0]` equals
  `{provider: "openrouter", model: "anthropic/claude-haiku-4-5"}`.
  Any subsequent `model.fallback` event's `to.model` is the
  canonical string, never the alias.
- Verifies: §10.14; Schemas §A.4.2.

**TC-FB-05: Unknown fallback alias fails at agent load**
- Layer: unit
- Setup: `fallback_models: ["mystery"]`; `model_aliases` has no
  `mystery`.
- Action: kernel resolves the agent config.
- Expected: raises `config.unknown_alias` (Fatal) naming
  `mystery` and the available aliases — at agent-load time, NOT
  at exchange or fallback-trigger time.
- Verifies: §10.14; fail-fast validation timing (patch §1.3).

**TC-FB-06: `model.fallback` event shape matches the canonical schema**
- Layer: unit
- Setup: build a `model.fallback` event payload via the engine's
  event constructor.
- Action: validate against the canonical schema (patch §3.1).
- Expected: event has `data.from {provider, model}`,
  `data.to {provider, model}`, `data.reason` (string),
  `data.reason_detail` (string), `data.attempt` (1-indexed int).
- Verifies: §10.14; event shape conformance.

**TC-FB-07: Streaming after first content event does NOT fall back**
- Layer: integration
- Setup: chain of two models; primary streams one content
  chunk, then raises HTTP 500 mid-stream; secondary would
  succeed if called.
- Action: run the agent with `--stream`.
- Expected: NO `model.fallback` event; the chain is not
  consulted; engine emits `thread.exchange_failed` with the
  partial output preserved in the failing `model.response`'s
  data. The user sees the truncation.
- Verifies: §10.14; streaming boundary (patch §2.3).

**TC-FB-08: Three-entry chain — primary + first fallback fail; second succeeds**
- Layer: integration
- Setup: `fallback_models: [haiku, gpt4o]`. Opus raises
  rate-limited; Haiku raises server-error; GPT4o succeeds.
- Action: run the agent once.
- Expected: two `model.fallback` events in order
  (opus→haiku, haiku→gpt4o); third attempt's `model.response`
  is successful; final `agent.message`.
- Verifies: §10.14; ordered traversal (patch §2.1).

**TC-FB-09: Fatal `model.*` does NOT trigger fallback**
- Layer: integration
- Setup: agent declares `fallback_models: [haiku]`. Primary
  raises `model.authentication_failed` (Fatal).
- Action: run the agent once.
- Expected: NO `model.fallback` event; chain untouched; engine
  emits `thread.exchange_failed` carrying the original Fatal
  error.
- Verifies: §10.14; Fatal-abort discipline (patch §2.2); Error
  Model §4.15.

**TC-FB-10: Reconstruction preserves the fallback chain in the log**
- Layer: integration
- Setup: a JSONL session log containing a complete fallback
  sequence (request, fallback, request, fallback, request,
  response, agent.message). Delete the entity DB.
- Action: run `kolsch reconstruct` against the JSONL.
- Expected: the reconstructed session's events include every
  `model.fallback` in the original order; the
  `model.response.data.model` field still names the final
  model that produced the answer.
- Verifies: §10.14; replay determinism + the no-loss event-log
  guarantee.

---

## Part 9.8 — Tools & Permissions (TOOL) — Phase 2.0, TOOL-R9

**TC-TOOL-01: Four builtins discovered from `bundles/kolsch/tools/`**
- Layer: component
- Setup: a `bundles/kolsch/tools/` directory containing
  `read_file.py`, `write_file.py`, `list_files.py`, `bash.py`
  each defining one `@tool`-decorated function.
- Action: bootstrap the runtime; list the registered tools.
- Expected: exactly the four tool names registered with action
  namespaces `tool.read_file`, `tool.write_file`,
  `tool.list_files`, `tool.bash`. No "builtin registry" inside
  `src/` contributed any tool — verified by grep.
- Verifies: §10.15; AGENTS.md hard rule per TOOL-R10.

**TC-TOOL-02: `read_file` enforces `file_access.allowed_paths`**
- Layer: component
- Setup: workspace with `file_access.allowed_paths: ["data/"]`;
  the agent has `read_file` enabled.
- Action: tool call `read_file(path="/etc/passwd")`.
- Expected: tool fails with `file.access_denied` (Recoverable);
  the model sees the error in the tool-result payload.
- Verifies: §10.15; Fh-I2; Error Model §4.16.

**TC-TOOL-03: `read_file` enforces `max_bytes`**
- Layer: component
- Setup: a workspace file `data/big.txt` whose byte size
  exceeds the default 1 MiB; `read_file` called with default
  `max_bytes`.
- Action: invoke `read_file(path="data/big.txt")`.
- Expected: `file.too_large` (Recoverable) with `detail.size_bytes`
  and `detail.max_bytes`.
- Verifies: §10.15; context-budget guard.

**TC-TOOL-04: `read_file` missing path → `file.not_found`**
- Layer: component
- Setup: workspace without `data/missing.txt`.
- Action: invoke `read_file(path="data/missing.txt")`.
- Expected: `file.not_found` (Recoverable) with `detail.path`.
- Verifies: §10.15.

**TC-TOOL-05: `bash` enforces `timeout_seconds`**
- Layer: component
- Setup: workspace authorizing `tool.bash`; agent invokes
  `bash(command="sleep 10", timeout_seconds=1)`.
- Action: run the agent.
- Expected: `tool.timeout` (Recoverable); the subprocess is
  killed; `tool.result` carries `timed_out: true` and a
  bounded `duration_seconds`.
- Verifies: §10.15; sandbox kill semantics.

**TC-TOOL-06: `list_files` non-recursive by default**
- Layer: component
- Setup: workspace with `data/a.txt`, `data/sub/b.txt`.
- Action: `list_files(path="data")`.
- Expected: entries include `a.txt` and the `sub/` directory;
  `b.txt` is NOT present (non-recursive). Re-running with
  `glob="**/*"` includes `b.txt`.
- Verifies: §10.15; default ergonomics.

**TC-TOOL-07: `write_file` is atomic — failed write leaves prior content intact**
- Layer: component
- Setup: workspace with `data/x.txt` containing `"old"`;
  `write_file` invoked with content that triggers a simulated
  write failure (SIGKILL between temp write and rename, or a
  mocked rename failure).
- Action: invoke `write_file(path="data/x.txt", content="new")`
  with the failure injected.
- Expected: `data/x.txt` still contains `"old"`; no partial
  write visible. (The Phase 1.1 atomicity discipline applies.)
- Verifies: §10.15; atomic write contract.

**TC-TOOL-08: Default rule set after `kolsch setup`**
- Layer: component
- Setup: fresh `kolsch setup --non-interactive` against a
  tmp_path workspace.
- Action: read `platform.yaml`'s `authorization.config.rules`.
- Expected: five rules in order — `rl_read_workspace`,
  `rl_list_workspace`, `rl_write_workspace` (effect: `prompt`),
  `rl_bash_any` (effect: `prompt`), `rl_default_deny`. The
  workspace path in the rules is the resolved absolute path,
  not a `${VAR}` template.
- Verifies: §10.15; patch §3.1 + §3.3.

**TC-TOOL-09: `prompt` effect treated as deny by `kolsch run`**
- Layer: integration
- Setup: workspace with default rule set; Fake LLM scripted to
  emit a `tool_use: bash` request.
- Action: `kolsch run helper --input content="run a command"`.
- Expected: the bash call is blocked; event log contains
  `authz.prompt_requested` (Recoverable) then
  `authz.prompt_unattended` (Recoverable). The exchange
  continues with the model seeing the deny in its tool-result
  payload; no hang.
- Verifies: §10.15; secrets-sandbox-authz §3.7.2.

**TC-TOOL-10: `kolsch permissions add` writes atomically and reloads in-process**
- Layer: component
- Setup: workspace with default rule set; an active runtime in
  the same process.
- Action: invoke `kolsch permissions add --who ".*"
  --what "tool\.bash" --effect allow --first`; immediately
  call `authz.allow("agent.X", "tool.bash", "cat /tmp/x")`.
- Expected: the `authz.allow` call returns
  `effect: ALLOW` matching the just-added rule. The on-disk
  `platform.yaml` reflects the new rule. The runtime did
  not need a restart.
- Verifies: §10.15; live-edit semantics.

**TC-TOOL-11: `kolsch permissions remove rl_default_deny` is protected**
- Layer: component
- Setup: workspace with the default rule set including
  `rl_default_deny` at the end.
- Action: invoke `kolsch permissions remove rl_default_deny`.
- Expected: command exits non-zero with
  `authz.protected_rule` (Fatal); the rule list is unchanged on
  disk and in memory.
- Verifies: §10.15; Error Model §4.16; patch §3.2.

**TC-TOOL-12: Deterministic rule ids across reorders + reloads**
- Layer: unit
- Setup: a `platform.yaml` with four rules.
- Action: load the rule set, snapshot the ids, reorder rules in
  the YAML and reload, snapshot ids again; then edit one
  rule's `effect` and reload, snapshot ids.
- Expected: the first two snapshots are identical (reorder
  preserves ids); the third snapshot differs only in the id
  of the edited rule (its hash changed).
- Verifies: §10.15; Module API Reference Part 11.8.3.

**TC-TOOL-13: `allow_all_test` variant refuses to load in production**
- Layer: component
- Setup: `platform.yaml`'s `authorization.variant: allow_all_test`;
  env var `KOLSCH_TEST_BUILD` UNSET.
- Action: bootstrap the runtime.
- Expected: fatal startup error names the variant and the
  required env var; bootstrap fails before any agent runs.
  Re-running with `KOLSCH_TEST_BUILD=1` loads successfully.
- Verifies: §10.15; AGENTS.md hard rule per TOOL-R10.

**TC-TOOL-14: `kolsch permissions list` matches in-memory order**
- Layer: component
- Setup: workspace with the default rule set; runtime bootstrapped.
- Action: invoke `kolsch permissions list`.
- Expected: stdout enumerates the five default rules in
  evaluation order (silent allows first, prompts next,
  `rl_default_deny` last); each row carries the stable id.
- Verifies: §10.15; Module API Reference Part 11.8.4.

**TC-TOOL-15: In-chat modal "always allow" lands within the same exchange**
- Layer: integration
- Setup: simulated chat surface (no real REPL — a test harness
  that injects an `effect=ALLOW, persist={who, what, effect: allow}`
  response to `handle_prompt`). Fake LLM scripted to call
  `bash` twice with the same command.
- Action: run the exchange.
- Expected: first call fires `authz.prompt_requested`; the
  harness's `handle_prompt` writes the new rule via
  `add_rule(..., before=<prompt_rule_id>)`; the second call
  hits the new ALLOW rule WITHOUT a prompt. Both `tool.bash`
  invocations complete inside one exchange; no restart, no
  exchange boundary.
- Verifies: §10.15; secrets-sandbox-authz §3.7.3; the
  load-bearing "same-exchange" guarantee from patch §0.

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

## Part 11.6.1 — CLI smoke layer (TC-CLI-SMOKE-*) — bug-sessions-namespace-attr-error.md

*(Added after the user-reported bug where `kolsch sessions` with no
op crashed with `AttributeError` because the dispatcher fell through
to a handler reading attributes from a sibling subparser. The
systemic gap: TC-CLI-* covered happy paths but no smoke layer
verified the parser-to-dispatcher wiring for every reachable entry
point.)*

**Policy (HARD RULE — also enshrined in `AGENTS.md`):** every new
CLI command or subcommand added in any future phase MUST come with
a smoke test in `tests/component/test_cli_smoke.py`. No exceptions.
A coverage-gate test (`test_smoke_coverage_enumerates_every_top_
level_subcommand`) refuses to pass when `_DISPATCH` lists a command
that isn't in the smoke module's `_TOP_LEVEL_SUBCOMMANDS` tuple,
so the rule is mechanically enforced — not just a convention.

**Invocation discipline:** smoke tests use
`subprocess.run([sys.executable, "-m", "cli", ...])` — NOT
in-process `cli.main(argv)`. The point is to catch parser-to-
dispatcher wiring bugs that only surface when the CLI is actually
spawned: `sys.exit` handling, stderr formatting, the
`__main__` entry point. In-process invocation would miss exactly
the class of bug the smoke layer is meant to catch (this bug being
exhibit A).

**Fixture discipline:** a session-scoped `_initialized_workspace`
fixture runs `kolsch setup --non-interactive` ONCE per test session
and is reused across every workspace-needing smoke test. No real
network — `KOLSCH_PROVIDER` / `KOLSCH_PROVIDER_KEY` are scrubbed
from the subprocess env so setup falls back to the built-in fake
LLM (the same path the first-agent tutorial uses).

**TC-CLI-SMOKE-01: top-level `--help` exits 0**
- Layer: component (subprocess)
- Action: `kolsch --help`.
- Expected: exit 0; non-empty stdout containing "kolsch"; no
  traceback in stderr.

**TC-CLI-SMOKE-02: every subcommand's `--help` exits 0**
- Layer: component (subprocess, parametrized over every top-level
  subcommand: `run`, `sessions`, `reconstruct`, `server`, `setup`,
  `connect`, `disconnect`, `providers`, `models`, `refresh`,
  `permissions`)
- Action: `kolsch <subcommand> --help`.
- Expected: exit 0; non-empty stdout; no traceback.

**TC-CLI-SMOKE-03: no-arg invocation prints help, exits 1**
- Layer: component (subprocess)
- Action: `kolsch` (no arguments).
- Expected: exit 1; usage info in stdout; no traceback.

**TC-CLI-SMOKE-04: unknown subcommand exits non-zero cleanly**
- Layer: component (subprocess)
- Action: `kolsch not-a-real-command`.
- Expected: exit non-zero; no traceback. (argparse handles this;
  smoke pins the no-crash property.)

**TC-CLI-SMOKE-05: `sessions` (no op) prints help, exits non-zero**
- Layer: component (subprocess)
- Setup: an initialized workspace (the fixture).
- Action: `kolsch sessions` (no operation).
- Expected: exit non-zero; help text mentions all four ops
  (`list`, `events`, `update`, `delete`); no `AttributeError` /
  `Traceback` in stderr. **This is the bug regression** —
  verifies the H1 fix against future regressions.

**TC-CLI-SMOKE-06: `permissions` (no op) prints help, exits non-zero**
- Layer: component (subprocess)
- Setup: an initialized workspace.
- Action: `kolsch permissions` (no operation).
- Expected: exit non-zero; help text mentions all four ops
  (`list`, `add`, `remove`, `show`); no traceback. (Parity with
  TC-CLI-SMOKE-05; before the fix, this returned an awkward
  `"unknown permissions op None"` message — not a crash, but
  inconsistent with the new uniform pattern.)

**TC-CLI-SMOKE-07..13: workspace-needing commands run cleanly**
- Layer: component (subprocess)
- Setup: initialized workspace.
- Actions:
  - 07: `kolsch sessions list` → exit 0
  - 08: `kolsch sessions list --starred --archived --limit 5`
    → exit 0 (flag combination)
  - 09: `kolsch providers` → exit 0 (empty connected set)
  - 10: `kolsch reconstruct` → exit 0
  - 11: `kolsch models` → exit 0 (no providers)
  - 12: `kolsch refresh` → exit 0 OR 1 (legitimately exits 1 when
    nothing's connected; smoke pins no-traceback only)
  - 13: `kolsch permissions list` → exit 0; `rl_default_deny` in
    stdout (Phase 2.0 default rule)
- Expected: no traceback in any of them.

**TC-CLI-SMOKE-14: missing-workspace friendly message**
- Layer: component (subprocess)
- Setup: an empty tmp_path (no `platform.yaml`).
- Action: `kolsch sessions list` against the empty path.
- Expected: exit 3 (the Phase 1.2 workspace-not-initialized exit
  code); stderr contains "No Kolsch workspace at" + "kolsch setup";
  no traceback.

**TC-CLI-SMOKE-15: every nested op exposes `--help` cleanly**
- Layer: component (subprocess, parametrized over
  `sessions list/events/update/delete` and
  `permissions list/add/remove/show`)
- Action: `kolsch <parent> <op> --help`.
- Expected: exit 0; no traceback. (Pins that no nested op's
  argparse wiring is so broken that even `--help` crashes.)

**`test_smoke_coverage_enumerates_every_top_level_subcommand` (gate)**
- Layer: component (in-process)
- Action: introspect `cli.main._DISPATCH` and compare against the
  smoke module's `_TOP_LEVEL_SUBCOMMANDS` tuple.
- Expected: no command in `_DISPATCH` is missing from the tuple.
  A future contributor adding a new subcommand without a
  corresponding smoke test fails this gate.

**Performance.** The suite runs in ~35-40s on a developer laptop
— above the bug ticket's <10s target. The floor is subprocess
startup × test count (~30 tests × ~1s Python import overhead).
The trade-off: AC 4 mandates subprocess invocation (the whole
point of the smoke layer); AC 5's <10s target requires either
reducing the test count (defeats the coverage point) or parallel
execution via `pytest-xdist`. Adding `pytest-xdist` is a future
improvement; the suite is fast enough today not to slow the
inner-loop gate meaningfully (it's <10% of the total suite time).

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
| LLM Provider Configuration | §10.12 | TC-LLM-01..10 (Phase 1.1) |
| Workspace Resolution & Setup | §10.13 | TC-UX-01..10 (Phase 1.2, UX-R9) |
| Model Fallback | §10.14 | TC-FB-01..10 (Phase 1.3, FB-R8) |
| Tools & Permissions | §10.15 | TC-TOOL-01..15 (Phase 2.0, TOOL-R9) |
| CLI | Part 11.6 (cases) | TC-CLI-01..08 — success criteria still to author in handbook §10.15 |
| HTTP API | Part 11.7 (cases) | TC-API-01..10 — success criteria still to author in handbook §10.16 |
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
