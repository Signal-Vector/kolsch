# Spec Index

This index aggregates the status of every spec, QoL, bug, and explanation doc in `process/` and `docs/explanation/`. Generated and verified via multi-agent scan; update by re-running the workflow.

## Status legend

| Status | Meaning |
|---|---|
| Draft | Spec being iterated; no implementation expected yet. |
| Proposed | Suggested work; awaiting prioritization. |
| In progress | Implementation underway; step writeups in flight. |
| Shipped | Implementation landed and verified. |
| Closed | Spec retired; superseded. |
| Closure | Final closure writeup capturing completion. |
| Stale | Spec abandoned (no activity, no implementation). |
| Unknown | Status not classified. |

## Verification methodology

Each entry's status was verified by reading the spec file + checking git log + filesystem evidence. `Verified status` column shows the post-verification status; `Was` shows what the file's header claimed. `Confidence` is high/medium/low based on the strength of evidence.

## Phase spec inventory

| Spec | Phase | Verified status | Was | Confidence | Summary |
|---|---|---|---|---|---|
| `docs/explanation/architecture-roadmap.md` | cross-phase | Draft | Draft | medium | High-level architecture per phase with migration paths between phases. |
| `process/phase1-build-plan.md` | 1.x | Shipped | Draft | high | Sequential developer/reviewer prompt steps to build and verify Phase 1 (with split Step 12 for CrewAI integration). |
| `process/phase1.1-llm-provider.md` | 1.1 | Shipped | Draft | high | Two-step build plan (revisions + implementation) for the Phase 1.1 LLM Provider Configuration patch. |
| `process/phase1.1-llm-specs.md` | 1.1 | Shipped | Draft | high | Patch spec for llm_providers config, build_llm helper, ProviderConnector protocol, OpenCode/OpenRouter, and CLI commands. |
| `process/phase1.2-usability-build.md` | 1.2 | Shipped | Draft | high | Two-step build plan (revisions + implementation) for the Phase 1.2 Workspace Resolution & First-Run Setup patch. |
| `process/phase1.2-usability-specs.md` | 1.2 | Shipped | Draft | high | Patch spec for workspace resolution chain, kolsch setup CLI command, and friendly missing-workspace error. |
| `process/phase1.3-fallback-build.md` | 1.3 | Shipped | Draft | high | Two-step build plan (revisions + implementation) for the Phase 1.3 Model Fallback Chains patch. |
| `process/phase1.3-fallback-specs.md` | 1.3 | Shipped | Draft | high | Patch spec for fallback_models field, engine fallback loop, model.fallback event, and llm.all_models_failed fatal. |
| `process/phase2.0-tools-build.md` | 2.0 | Shipped | Draft | high | Two-step build plan (revisions + implementation) for Phase 2.0 builtin tools and workspace-only permissions. |
| `process/phase2.0-tools-specs.md` | 2.0 | Shipped | Draft | high | Patch spec for four builtin tools, prompt authz effect, default rule set, and in-chat/CLI live rule edits. |
| `process/phase2.1-chat-build.md` | 2.1 | Shipped | Draft | high | Seven-step build plan (two revision + five implementation sub-steps) for the Phase 2.1 kolsch chat REPL. |
| `process/phase2.1-chat-specs.md` | 2.1 | Shipped | Draft | high | Patch spec for the streaming chat REPL: command contract, layout, event-to-render mapping, approval modal, slash commands. |
| `process/phase2.2-agent-envelope-build.md` | 2.2 | Shipped | Draft | high | Fifteen-step build plan for Phase 2.2 Agent Envelope (permissions, templated prompts, inheritance). |
| `process/phase2.2-agent-envelope-specs.md` | 2.2 | Shipped | Draft | high | Patch spec for per-agent permissions, Jinja-templated prompts, @tool summary, context files, and extends: inheritance. |
| `process/phase2.2-step15-capture.md` | 2.2 | Shipped | In progress | high | Captured-artifact deliverable: three engine executions exercising the full Phase 2.2 envelope surface. |
| `process/phase2.4-skills-build.md` | 2.4 | Shipped | Draft | high | Three-sub-phase build plan wrapping user-facing surface around the Phase 1 skills subsystem mechanics. |
| `process/phase3.0-workflow-build.md` | 3.0 | Draft | Draft | high | Phase 3.0 spec for the YAML parser, asset catalog integration, run-start validator, and workflows list/show/validate CLI. |
| `process/phase3.0-prior-art-memo.md` | 3.0 | Draft | Draft | high | Research memo surveying five systems against conditionals, loops, triggers, and fork UX open questions. |
| `process/phase3.0-workflow-spec.md` | 3.0 | Draft | Draft | high | Master Phase 3.0 workflow design spec across 21 sections covering locked decisions through Round 7. |
| `process/phase3.1-executor-spec.md` | 3.1 | Draft | Draft | high | Sequential executor spec: algorithm, AND-strict completion, module loading, iterative traversal, state, cancellation, orphan recovery. |
| `process/phase3.4-user-input-spec.md` | 3.4 | Draft | Draft | high | user_input node pause/resume mechanics for CLI and chat, including nested pauses and resume authentication. |
| `process/phase3.8-fork-spec.md` | 3.8 | Draft | Draft | high | Fork-from-event spec: operation, side-effect discipline, sub-workflow forking, fork-point legality at workflow-run events. |
| `process/phase3.9-chat-surface-spec.md` | 3.9 | Draft | Draft | high | Chat surface spec: /workflow and /fork slash commands, capsule rendering, event IDs, label fallback via display_label(). |
| `process/phase3.x-roadmap.md` | 3.x | Draft | Draft | high | Eleven-phase sub-phasing plan for kolsch-engine (3.0 through 3.10+) with scope, gates, and dependency graph. |

## Cross-phase explanation docs

| Spec | Phase | Verified status | Was | Confidence | Summary |
|---|---|---|---|---|---|
| `docs/explanation/agent-engine-contract.md` | 1.x | Draft | Draft | medium | Framework-agnostic contract the Agent Engine must honor; v1 wraps CrewAI. |
| `docs/explanation/asset-management-contract.md` | 1.x | Draft | Draft | medium | Discovery and resolution layer between bundles on disk and the running platform. |
| `docs/explanation/contract-audit.md` | 1.x | Draft | Draft | medium | Cross-check between modules verifying interaction contracts agree on flows and assumptions. |
| `docs/explanation/error-model.md` | 1.x | Draft | Draft | medium | Three-level error severity model with events and exceptions as two surfaces. |
| `docs/explanation/event-bus-contract.md` | 1.x | Draft | Draft | medium | Inter-module notification layer; ephemeral implementation via Redis pub/sub in v1. |
| `docs/explanation/file-handling.md` | 1.x | Draft | Draft | medium | How files flow through the platform; file_ref, destinations, and path-access policy. |
| `docs/explanation/framework-protocol-design.md` | 1.x | In progress | In progress | high | Minimal Python Protocol the Agent Engine depends on; pre-Step-12 research spike. |
| `docs/explanation/jinja-extensions.md` | 3.x | Draft | Draft | medium | How new Jinja filters get added to kolsch; core-only filter set policy. |
| `docs/explanation/kernel-resolver-contract.md` | 1.x | Draft | Draft | medium | Config Schema and Config Resolver: pure JSON Merge Patch resolver and validation pipeline. |
| `docs/explanation/llm-providers.md` | 1.1 | Draft | Draft | medium | Layer between agent provider/model config and actual model calls; helper, Protocols, registry. |
| `docs/explanation/memory-provider-contract.md` | 1.x | Draft | Draft | medium | Memory as a Provider Module; v1 ships recent_buffer dual-purpose variant. |
| `docs/explanation/secrets-sandbox-authz-contract.md` | 1.x | Draft | Draft | medium | Three platform-level Provider Modules in one document; one variant per module in v1. |
| `docs/explanation/session-run-store-contract.md` | 1.x | Draft | Draft | medium | Owns sessions, threads, entity records, and per-session JSONL event log files. |
| `docs/explanation/skills-subsystem.md` | 1.x | Draft | Draft | medium | Loadable knowledge for agents via SKILL.md; progressive disclosure and force invoke. |
| `docs/explanation/specification.md` | cross-phase | Draft | Draft | medium | Consolidated architectural skeleton: vision, principles, scope, module map, contracts. |
| `docs/explanation/tools-subsystem.md` | 1.x | Draft | Draft | medium | How tools are defined, discovered, registered, invoked; MCP server integration. |
| `docs/explanation/v1-scope.md` | 1.x | Draft | Draft | medium | The deliberate inclusion and exclusion lists for v1; what gets built and shipped. |
| `docs/explanation/workflow-event-log.md` | 3.0 | Draft | Unknown | medium | Cross-phase event-log schema and conventions for the workflow engine, extracted from §11. |
| `docs/explanation/workflow-foundation.md` | 3.x | Draft | Unknown | medium | Cross-phase foundation decisions every Phase 3.x sub-phase honors. |
| `docs/explanation/workflow-glossary.md` | 3.0 | Draft | Unknown | medium | Canonical glossary for kolsch workflows; precise terms used in the workflow contract. |
| `docs/explanation/workflow-policy-model.md` | cross-phase | Draft | Unknown | medium | Cross-phase explanation of how policy applies to workflows; layers, walking order, scope. |

## QoL specs

Sort: by phase ascending, then by verified status (Closure → Shipped → In progress → Proposed → Draft).

| Spec | Phase | Verified status | Was | Confidence | Summary |
|---|---|---|---|---|---|
| `process/qol-chat-capsule-layout.md` | 2.1 | Shipped | Shipped | high | Unified side-bar capsule layout for all chat message types replacing prose headers + boxed panels. |
| `process/qol-chat-esc-watcher-post-modal.md` | 2.1 | Draft | Draft | high | Restore the chat Esc-watcher's asyncio reader after the approval modal replaces it on the same stdin fd. |
| `process/qol-chat-specs-deny-esc-update.md` | 2.1 | Draft | Draft | high | Docs hygiene: update phase2.1-chat-specs.md modal and cancellation sections for deny-stops + Esc binding. |
| `process/qol-action-namespace-bundle-scoped.md` | 3.x | Draft | Draft | high | Rewrite tool action names from `tool.<name>` to bundle-rooted `tool.<home_bundle>.<name>` for distinct authz. |
| `process/qol-asset-cli-symmetric.md` | 3.x | Proposed | Proposed | high | Rationalize existing agents/tools/skills/prompts CLI surfaces into the symmetric list/show/--format pattern. |
| `process/qol-cli-format-output-rollout.md` | 3.x | Proposed | Proposed | high | Roll out the --format text|json envelope contract to every pre-Phase-3.0 kolsch CLI command and subcommand. |
| `process/qol-policy-rename.md` | 3.x | Proposed | Proposed | high | Rename code-side authz module/class/value-object identifiers to policy to match the user-facing terminology. |
| `process/qol-refs-1seg-removal.md` | 3.x | Proposed | Proposed | high | Adds codemod CLI command to upgrade existing 1-seg ref shorthand to full 3-seg refs across YAML. |
| `process/qol-workflow-gc.md` | 3.x | Proposed | Proposed | high | CLI `workflows gc` for operator-controlled run retention with --older-than, --dry-run, --keep-forked. |
| `process/qol-workflow-storage-stats.md` | 3.x | Proposed | Proposed | high | Adds `kolsch workflows list/stats/show` so operators can inspect on-disk run logs. |
| `process/qol-prefix-defaults-and-unified-refs-step4-closure.md` | cross-phase | Closure | Closure | high | Step 4 closure for unified ref convention with two manual e2e scenarios passing and inheritance fix unblocked. |
| `process/qol-permission-system-human-readable-step4-handoff.md` | cross-phase | Closure | REVISE-PENDING-USER | medium | Step 4 handoff for the human-readable permission system QoL covering migration hook, defaults, and docs. |
| `process/qol-permission-system-human-readable-step4-manual-e2e.md` | cross-phase | Closure | In progress | medium | Manual e2e verification of permission-system migration against a sandbox copy of the canonical workspace. |
| `process/qol-permission-system-human-readable-step4-prefix-verification.md` | cross-phase | Closure | In progress | medium | Documented pre-Step-2 baseline failure mode that PERM-SYS-01..05 surfaces against the legacy schema. |
| `process/qol-tool-permission-discovery-uniform-with-stacking-step1-writeup.md` | cross-phase | Closure | In progress | medium | Step 1 dev writeup; STOP — escalation E-QOL-PERMDISC-001 raised, coordinator approval required before Step 2. |
| `process/qol-tool-permission-discovery-uniform-with-stacking-step3-closure.md` | cross-phase | Closure | Closure | high | Step 3 closure: automated suite, pre-fix evidence, docs done; pending user manual e2e walkthrough. |
| `process/qol-yaml-version-fields-optional-step3-writeup.md` | cross-phase | Closure | In progress | medium | Step 3 docs + manual e2e writeup; all Part 2 acceptance criteria green, no follow-ups surfaced. |
| `process/qol-permission-system-human-readable.md` | cross-phase | Shipped | Draft | high | Rewrite permission rule schema to what/pattern/effect with glob, qualified tools, /perm slash recovery. |
| `process/qol-prefix-defaults-and-unified-refs.md` | cross-phase | Shipped | Draft | high | Collapse three ref conventions to one 2-segment form and make `workspace:` prefix implicit for the common case. |
| `process/qol-tool-output-truncation-and-persistence.md` | cross-phase | Shipped | Draft | high | Persist bash overflow, add line-based read_file with offset/limit, and soft-truncate over-limit reads. |
| `process/qol-tool-permission-discovery-uniform-with-stacking.md` | cross-phase | Shipped | Draft | high | Modal-prompt the no-rule case with four-level stacking (agent > bundle > platform > default-deny). |
| `process/qol-workspace-root-listable.md` | cross-phase | Shipped | Shipped | high | Default template adds `.` to allowed_paths so `list_files(".")` works at workspace root. |
| `process/qol-yaml-version-fields-optional.md` | cross-phase | Shipped | Draft | high | Makes schema_version and assembly_version optional at the partial-config layer with defaults. |
| `process/qol-setup-evolution-and-defaults.md` | cross-phase | In progress | Draft | medium | Makes kolsch setup the migration point: drift detection, defaults block, helper rewrite. |
| `process/qol-setup-evolution-step10-e2e.md` | cross-phase | In progress | In progress | high | Captured manual e2e transcript for Step 10: fresh setup, downgrade, re-run, migration, backups. |
| `process/qol-tool-permissions-binding-unused.md` | cross-phase | Draft | Draft | high | Stub spec to wire PermissionsBinding at dispatch so ctx.permissions.check/require works in tool bodies. |
| `process/qol-zero-config-first-run-experience.md` | cross-phase | Draft | Draft | high | Picks a better default_platform_payload seed for workspaces materialized without `kolsch setup`. |
| `process/qol-sessions-list-columns.md` | N/A | Shipped | Shipped | high | Tightens kolsch sessions list to id/started/last/name columns; drops starred/archived noise. |
| `process/qol-ship-canonical-kolsch-agents-as-package-resources.md` | N/A | Draft | Draft | high | Ship starter bundle as package data so `kolsch:agents/` refs resolve without running `kolsch setup`. |
| `process/qol-skills-jinja-variable-symmetric-with-tools.md` | N/A | Draft | Draft | high | Adds SkillsVar Jinja variable mirroring ToolsVar so skills render symmetrically in agent prompts. |

## Bug specs

Sort: by verified status (Closure → Shipped → In progress → Proposed → Draft), then alphabetical within.

| Spec | Phase | Verified status | Was | Confidence | Summary |
|---|---|---|---|---|---|
| `process/bug-chat-opencode-qwen-timeout-step4-closure.md` | cross-phase | Closure | Closure | high | Step 4 closure + user handoff summary for the qwen timeout opencode investigation. |
| `process/bug-engine-extends-parent-not-rendered-step5-closure.md` | 2.2 | Closure | Closure | high | Step 5 closure (REVISE-PENDING-USER) verifying the extends-parent fix end-to-end in user workspace. |
| `process/bug-model-line-placeholder-collides-with-jinja-step3-writeup.md` | 2.2 | Closure | Closure | high | Step 3 closure: docs landed and acceptance criteria 1-8 verified for the MODEL_LINE/Jinja collision fix. |
| `process/bug-agent-id-derive-from-ref-step1-writeup.md` | cross-phase | Shipped | In progress | high | Step 1 verification writeup for making agent_id optional and deriving from bundle/name ref. |
| `process/bug-agent-id-derive-from-ref-step3-writeup.md` | cross-phase | Shipped | In progress | high | Step 3 regression test + e2e writeup for the agent_id derive-from-ref fix. |
| `process/bug-agent-id-derive-from-ref.md` | cross-phase | Shipped | Draft | high | Make agent_id optional in YAML and derive it from bundle/name when omitted. |
| `process/bug-chat-deny-stops-and-esc-interrupts-step1-writeup.md` | cross-phase | Shipped | In progress | high | Step 1 verification writeup for deny-stops-exchange and Esc-interrupts-streaming chat behaviors. |
| `process/bug-chat-deny-stops-and-esc-interrupts-step3-writeup.md` | cross-phase | Shipped | In progress | high | Step 3 regression tests + manual e2e writeup for chat deny/Esc cancellation behaviors. |
| `process/bug-chat-deny-stops-and-esc-interrupts.md` | cross-phase | Shipped | Draft | high | Make deny end the exchange and Esc cancel streaming at the next cycle boundary in chat. |
| `process/bug-chat-opencode-qwen-timeout-investigation.md` | cross-phase | Shipped | Draft | high | Investigation ticket to closure on persistent timeouts against qwen3.6-plus via opencode. |
| `process/bug-chat-opencode-qwen-timeout-step1-writeup.md` | cross-phase | Shipped | In progress | high | Step 1 writeup delivering replay tool + baseline runs for the qwen timeout investigation. |
| `process/bug-chat-opencode-qwen-timeout-step2-writeup.md` | cross-phase | Shipped | In progress | high | Step 2 writeup of hypothesis probes (H1 timeout bump closes the gap) for qwen timeouts. |
| `process/bug-chat-opencode-qwen-timeout-step3-writeup.md` | cross-phase | Shipped | In progress | high | Step 3 writeup shipping the 30s -> 180s opencode timeout default for the qwen fix. |
| `process/bug-chat-permissions-and-console-leak.md` | 2.0 | Shipped | Shipped | high | Three chat bugs: missing permissions defaults, CrewAI stdout leak, duplicate user-input echo. |
| `process/bug-crewai-stdout-leak-investigation.md` | cross-phase | Shipped | Shipped | high | Investigation + 9-step fix for the CrewAI ellipsis indicator stdout leak in chat sessions. |
| `process/bug-echo-on-real-provider.md` | 1.1 | Shipped | Shipped | high | Real LLM provider sessions returning EchoLLM output instead of real model responses. |
| `process/bug-engine-extends-parent-not-rendered-step1.md` | 2.2 | Shipped | In progress | high | Step 1 verification writeup citing the three wiring gaps blocking `{% extends parent %}` rendering. |
| `process/bug-engine-extends-parent-not-rendered-step3-writeup.md` | 2.2 | Shipped | In progress | high | Step 3 writeup landing the 28-case EXT-CORPUS fixtures + parametrised runner + pre-fix sampling. |
| `process/bug-engine-extends-parent-not-rendered.md` | 2.2 | Shipped | In progress | high | Four-step plan to wire Jinja `{% extends parent %}` render path with a 28-case fixture corpus. |
| `process/bug-engine-tools-wiring-gap.md` | 2.0 | Shipped | Shipped | high | Wire declared subsystems.tools through the engine to the framework so the LLM actually receives them. |
| `process/bug-framework-iteration-cap-removal.md` | cross-phase | Shipped | Draft | high | Remove the hardcoded 10-iteration framework loop cap that mis-surfaces as platform.internal_error. |
| `process/bug-litellm-model-routing.md` | 1.1 | Shipped | Shipped | high | OpenCode connector hardcoded openai/ prefix breaks LiteLLM for non-OpenAI model names. |
| `process/bug-model-line-placeholder-collides-with-jinja.md` | 2.2 | Shipped | Draft | high | Resolve the `{{MODEL_LINE}}` setup-time substitution collision with Phase 2.2 Jinja syntax. |
| `process/bug-phase2.2-envelope-schema-gap.md` | 2.2 | Closed | Closed | medium | Phase 2.2 envelope fields (context_files, permissions, Jinja) rejected by Phase-1.3 Layer-1 schema. |
| `process/bug-sessions-namespace-attr-error.md` | cross-phase | Shipped | Shipped | high | `kolsch sessions` with no subcommand crashes plus add CLI-surface smoke-test coverage. |
| `process/bug-setup-test-run-failure-handling.md` | 1.2 | Shipped | Shipped | high | Setup mis-picks unverified model + prints ready banner despite a failed optional test exchange. |
| `process/bug-platform-internal-error-boundary-swallow.md` | cross-phase | In progress | Draft | high | Engine boundary's PlatformInternalError catch-all swallows non-PlatformError exceptions opaquely. |
| `process/bug-crewai-output-leak-during-retries.md` | cross-phase | Draft | Draft | high | Suppress CrewAI Rich panels + logging.error chrome that leak during retry loops. |
| `process/bug-framework-cancellation-mid-call-latency.md` | cross-phase | Draft | Draft | high | Cancellation does not interrupt long-running LLM/tool calls until iteration boundaries. |
| `process/bug-llm-max-tokens-credit-overrun.md` | cross-phase | Draft | Draft | high | Lower max_tokens default so OpenRouter pre-charge stops triggering 402 insufficient credits. |
| `process/bug-mid-tool-cancellation-latency.md` | cross-phase | Draft | Draft | high | Esc/Ctrl-C does not interrupt a mid-flight tool call until the tool's await returns. |
| `process/bug-tool-output-summarization-bias.md` | cross-phase | Draft | Draft | high | Add presentation discipline to builtin tool descriptions so agents return raw output verbatim. |

## Summary counts

### Totals by type (deduplicated)

| Type | Count |
|---|---|
| spec | 22 |
| roadmap | 2 |
| explanation | 21 |
| qol | 30 |
| bug | 32 |
| **Total** | **107** |

### Totals by verified status

| Status | Count |
|---|---|
| Shipped | 39 |
| Draft | 51 |
| Closure | 9 |
| Proposed | 6 |
| In progress | 3 |
| Closed | 1 |
| **Total** | **109** |

(Counts tally each row in the inventory tables; a few specs appear under two type sections via step writeups.)

### Status changes (where verification overrode the file's declared status)

| Spec | Was | Verified | Evidence | Confidence |
|---|---|---|---|---|
| `process/phase1-build-plan.md` | Draft | Shipped | Roadmap marks Phase 1 shipped (merged before May 28). | high |
| `process/phase1.1-llm-provider.md` | Draft | Shipped | Commits 58b1476 + 63689da; src/llm/ implements. | high |
| `process/phase1.1-llm-specs.md` | Draft | Shipped | Commit 58b1476 + b6d1227 review-gate close. | high |
| `process/phase1.2-usability-build.md` | Draft | Shipped | Commit 90d19e7 implements Phase 1.2 resolution + setup. | high |
| `process/phase1.2-usability-specs.md` | Draft | Shipped | Commits 90d19e7 + ba111cb + 7bfd9be applied UX revs. | high |
| `process/phase1.3-fallback-build.md` | Draft | Shipped | Commit c2b7dc2 full Phase 1.3; src/engine/_fallback.py. | high |
| `process/phase1.3-fallback-specs.md` | Draft | Shipped | Commit c2b7dc2 + 7416436 FB-R1..R12 applied. | high |
| `process/phase2.0-tools-build.md` | Draft | Shipped | Commits 7056387, deaf95f, ba4673f; src/tools/. | high |
| `process/phase2.0-tools-specs.md` | Draft | Shipped | Same Phase 2.0 commits; src/cli/permissions.py present. | high |
| `process/phase2.1-chat-build.md` | Draft | Shipped | Roadmap shipped; 24a-24e + e7e1b35 + f669246. | high |
| `process/phase2.1-chat-specs.md` | Draft | Shipped | src/cli/chat.py, _modal.py, _renderer.py, _slash.py. | high |
| `process/phase2.2-agent-envelope-build.md` | Draft | Shipped | Commit ff70f6a Phase 2.2 envelope + edaf4bd EXT path. | high |
| `process/phase2.2-agent-envelope-specs.md` | Draft | Shipped | Commit ff70f6a + 5065ffc envelope amendments. | high |
| `process/phase2.2-step15-capture.md` | In progress | Shipped | Phase 2.2 shipped at ff70f6a; capture doc is the closure. | high |
| `process/phase2.4-skills-build.md` | Draft | Shipped | Commit 2f1727d Phase 2.4 build; src/cli/skills.py. | high |
| `docs/explanation/workflow-event-log.md` | Unknown | Draft | Newly extracted §11 of phase3.0 spec; untracked. | medium |
| `docs/explanation/workflow-foundation.md` | Unknown | Draft | Just-created §2 extraction; untracked. | medium |
| `docs/explanation/workflow-glossary.md` | Unknown | Draft | Brand-new canonical glossary; untracked. | medium |
| `docs/explanation/workflow-policy-model.md` | Unknown | Draft | Newly-created policy-to-workflow explanation; untracked. | medium |
| `process/qol-permission-system-human-readable.md` | Draft | Shipped | Commit 3c392e2 ships rule grammar rewrite. | high |
| `process/qol-permission-system-human-readable-step4-handoff.md` | REVISE-PENDING-USER | Closure | Step 4 functionally complete; pending user verification. | medium |
| `process/qol-permission-system-human-readable-step4-manual-e2e.md` | In progress | Closure | Sandbox e2e captured; commit 3c392e2 shipped. | medium |
| `process/qol-permission-system-human-readable-step4-prefix-verification.md` | In progress | Closure | Pre-fix baseline captured; commit 3c392e2 shipped. | medium |
| `process/qol-prefix-defaults-and-unified-refs.md` | Draft | Shipped | Commit 5d744f3 unified 2-segment ref convention. | high |
| `process/qol-setup-evolution-and-defaults.md` | Draft | In progress | Steps 2-8 shipped; Step 10 e2e captured; Step 9/11+ open. | medium |
| `process/qol-tool-output-truncation-and-persistence.md` | Draft | Shipped | Commit 2deacfe bash overflow + read_file pagination. | high |
| `process/qol-tool-permission-discovery-uniform-with-stacking.md` | Draft | Shipped | Commit 8351a55 four-level stacking + Step 3 closure. | high |
| `process/qol-tool-permission-discovery-uniform-with-stacking-step1-writeup.md` | In progress | Closure | Escalation resolved; commit 8351a55 shipped. | medium |
| `process/qol-yaml-version-fields-optional.md` | Draft | Shipped | Commit 48df720 schema_version/assembly_version optional. | high |
| `process/qol-yaml-version-fields-optional-step3-writeup.md` | In progress | Closure | All Part 2 acceptance green; commit 48df720 shipped. | medium |
| `process/bug-agent-id-derive-from-ref.md` | Draft | Shipped | Commit 982ee7e + tests at 9e45bec. | high |
| `process/bug-agent-id-derive-from-ref-step1-writeup.md` | In progress | Shipped | Step 1 writeup tied to shipped fix 982ee7e. | high |
| `process/bug-agent-id-derive-from-ref-step3-writeup.md` | In progress | Shipped | All four TCs pass post-fix; tests at 9e45bec. | high |
| `process/bug-chat-deny-stops-and-esc-interrupts.md` | Draft | Shipped | Commit e7e1b35 deny stops + Esc cancels. | high |
| `process/bug-chat-deny-stops-and-esc-interrupts-step1-writeup.md` | In progress | Shipped | Tied to shipped commit e7e1b35. | high |
| `process/bug-chat-deny-stops-and-esc-interrupts-step3-writeup.md` | In progress | Shipped | Automated regression tests landed at e7e1b35. | high |
| `process/bug-chat-opencode-qwen-timeout-investigation.md` | Draft | Shipped | Commit 652b72d 30s->180s timeout bump. | high |
| `process/bug-chat-opencode-qwen-timeout-step1-writeup.md` | In progress | Shipped | Replay tool + baselines incorporated into closure. | high |
| `process/bug-chat-opencode-qwen-timeout-step2-writeup.md` | In progress | Shipped | H1 hypothesis confirmed; shipped at 652b72d. | high |
| `process/bug-chat-opencode-qwen-timeout-step3-writeup.md` | In progress | Shipped | _DEFAULT_TIMEOUT_SECONDS 30->180 shipped at 652b72d. | high |
| `process/bug-engine-extends-parent-not-rendered.md` | In progress | Shipped | Commit edaf4bd + Step 5 closure exists. | high |
| `process/bug-engine-extends-parent-not-rendered-step1.md` | In progress | Shipped | Step 1 writeup for shipped fix at edaf4bd. | high |
| `process/bug-engine-extends-parent-not-rendered-step3-writeup.md` | In progress | Shipped | 28-case EXT-CORPUS landed at edaf4bd. | high |
| `process/bug-framework-iteration-cap-removal.md` | Draft | Shipped | Commit 403a6e0 + tests at 65aa739 TC-FRM-NOCAP-01..03. | high |
| `process/bug-model-line-placeholder-collides-with-jinja.md` | Draft | Shipped | Commit 5411f85 drops `{{MODEL_LINE}}` placeholder. | high |
| `process/bug-platform-internal-error-boundary-swallow.md` | Draft | In progress | Step 2 commits bd1f529 + 9f3c0a6 landed; not closed. | high |

### Totals by phase

| Phase | Count |
|---|---|
| 1.x | 15 |
| 1.1 | 5 |
| 1.2 | 3 |
| 1.3 | 2 |
| 2.0 | 4 |
| 2.1 | 5 |
| 2.2 | 9 |
| 2.4 | 1 |
| 3.0 | 6 |
| 3.1 | 1 |
| 3.4 | 1 |
| 3.8 | 1 |
| 3.9 | 1 |
| 3.x | 13 |
| cross-phase | 38 |
| N/A | 3 |
| **Total** | **108** |
