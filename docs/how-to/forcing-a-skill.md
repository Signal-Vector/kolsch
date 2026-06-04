# Forcing a skill

Sometimes you already know which skill the model should use, and
you don't want to wait for it to read the available-skills
listing and decide. Force-invoke pre-loads a skill's `SKILL.md`
into the model's context before your next exchange starts. The
agent reads the skill body the same way it would have if it had
chosen the skill itself — there's no separate "force" channel,
just an earlier read.

This page covers the two surfaces: the chat slash command
(`/skill`) and the run flag (`--skill`). Both share the same
contract: the agent's `subsystems.skills.config.enabled` list
gates which refs you can force-invoke.

## When to use it

- The user knows which skill applies. ("Run the literature-review
  skill on transformer attention.")
- The skill is marked `disable-model-invocation: true` and
  cannot be discovered by the model — force-invoke is the only
  way to load it.
- The user wants to combine multiple skills for one exchange.

If you're not sure which skill applies, just describe what you
need; the model reads the available-skills block and decides.
Force-invoke is the override, not the default.

## In `kolsch chat`

```
You: /skill kolsch/literature-review
queued kolsch/literature-review — force-load runs before your next message.
You: write a literature review on transformer attention variants
```

What happens under the hood, in order:

1. The `/skill` handler validates the ref against the agent's
   enabled list. A typo prints a friendly error and leaves your
   pending queue untouched — your next message is a regular
   user message, no force-invoke.
2. Your next non-slash submission flips the engine's
   `ExecutionContext.trigger` from `user_message` to
   `user_message_with_force_skill`.
3. The engine's pre-flight step 9 emits a synthetic `tool.called`
   for `read` on the SKILL.md path (principal `system`), runs
   the read through the regular `tool.read` authorization, and
   emits the `tool.result` carrying the body.
4. The exchange continues normally — the model sees the skill
   body as if it had read the file itself.

Multiple `/skill` calls before the next message accumulate; one
exchange consumes them all in declaration order. `/skill` alone
(no argument) prints what's queued so you can check before
submitting.

## In `kolsch run`

```
$ kolsch run kolsch/researcher \
    --input content="literature review on transformer attention" \
    --skill kolsch/literature-review
```

`--skill` is repeatable. Each occurrence adds one ref to the
trigger's `force_skills` list, in flag order. Refs not in the
agent's enabled list fail with exit code 2 and a stderr message
before the engine is invoked — no exchange is started, no events
land in the session.

## Authorization

Force-invoke uses the same `tool.read` authorization path as a
model-initiated read. If your workspace's authz rules deny
`tool.read` for the SKILL.md path, force-invoke surfaces as a
`tool.failed` event (`error.type == "tool.unauthorized"`) and
the exchange ends with `thread.exchange_failed`. The skills
subsystem has no separate authorization namespace — it
deliberately reuses `tool.read` so authorization stays
consistent with every other file read the agent does.

## What this how-to does NOT cover

- **Writing skills.** This page assumes the skill already exists
  in a bundle. The Skills subsystem spec covers SKILL.md format,
  frontmatter, supporting files.
- **Enabling skills.** Each ref you force-invoke must already be
  in the agent's `subsystems.skills.config.enabled` list. That's
  agent-config work, not a runtime surface.
- **Replacing the model's judgement.** Force-invoke pre-loads a
  skill; the model still drives the conversation. It might
  ignore the loaded skill if your message doesn't connect to it.

## Where next

- [`docs/explanation/skills-subsystem.md`](../explanation/skills-subsystem.md) — the contract: SKILL.md format, the discovery
  scan, force-invoke mechanics, conformance invariants.
- [`docs/how-to/chatting-effectively.md`](chatting-effectively.md) — how `/skill` fits with the other chat slash commands.
- [`docs/how-to/permissions.md`](permissions.md) — when authz
  blocks the synthetic SKILL.md read, this is the surface that
  unblocks it.
