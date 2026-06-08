# QoL — Symmetric `{{ skills }}` Jinja variable

**Draft 0.1** *(Step 5 follow-up from
`bug-engine-extends-parent-not-rendered.md`. Skeleton; flesh
out before scheduling.)*

## Problem

Tools are exposed to the agent prompt as a Jinja variable
(`{{ tools }}` / `{% for t in tools %}` etc.); skills are
not. Instead the engine post-appends a
`## Available Skills\n…` block to the rendered prompt
unconditionally (see `engine/assembly.py:83-87`).

Consequences:

- **Asymmetric API surface.** Authors writing prompts
  expect to render skills the same way they render tools
  (`{% for s in skills %}- {{ s.name }}{% endfor %}` /
  `{{ skills.summary() }}`); the post-append breaks that
  intuition.
- **Cannot inline or reposition the skills listing.** It
  always appears after everything else. Agent prompts that
  want skills declared up front (e.g. an opening "You have
  access to these skills:" before the operating
  instructions) can't have that.
- **No way to omit the listing.** An agent that uses skills
  but wants its prompt to mention them in narrative form
  (`When the user asks about X, use your literature-review
  skill`) gets the auto-appended listing on top of the
  hand-written narrative.

## Proposed

Define a `SkillsVar` analogous to `ToolsVar`
(`templating/vars/tools.py`):

- List-shaped iterable; same standardized method family
  (`.list()`, `.summary()`, `.names()`, `.get(name)`,
  `.count`) and the Pythonic protocols
  (`__iter__`, `__len__`, `__getitem__`, `__contains__`).
- `__str__` defaults to `.list()` so `{{ skills }}` is
  shorthand for `{{ skills.list() }}` (matches the tools
  default).

Add a `SkillItem` dataclass (`name`, `description`,
`bundle`, `path`, `enabled_in_agent`) — minimum surface for
the typical prompt usage.

Engine wiring (replaces `assembly.py:83-87`):

- `_render_prompt_if_needed` builds `skills_var` from the
  resolved skills list (`subsystems.skills.config.enabled`).
- Adds `"skills": skills_var` to the render context.
- `assemble_context` no longer appends the post-render
  listing; the prompt template is the single source of
  truth.

## Migration

The kolsch starter helper's prompt template adds
`{{ skills }}` (or `{% for s in skills %}…{% endfor %}`)
somewhere natural — probably alongside the existing
`{{ tools }}` block. Any workspace agent that was relying
on the post-append gets a deprecation warning on first
agent-load and instructions to add the variable.

Out of scope: parametric force-invoke surface (`/skill <ref>`
at chat time, force-loaded skills) doesn't need to change.

## Open questions

- Should `SkillsVar` filter by `enabled` (just the
  agent-enabled ones) or expose the whole catalog with a
  per-item `enabled` flag? Tools today filter; matching
  that is the conservative choice.
- Does the engine emit `agent.template_warning` for
  `{{ skills }}` referenced + agent declares no skills (the
  way it does for `{{ tools }}` empty)? Yes — symmetry.

## Sequencing

Independent. No blockers.

## Related

- `bug-engine-extends-parent-not-rendered.md` Part 4 G1/G3
  fixtures pin the field-side skills inheritance contract;
  this QoL doesn't disturb that, just adds the render-side
  variable.
- `engine/assembly.py:83-87` — the post-append site this
  QoL removes.
