"""The templating subsystem — prompt rendering with Jinja2.

Public surface: [`render_prompt`][templating.render.render_prompt], the
pure Jinja2 render harness with `StrictUndefined`. The variable objects
(`tools`, `docs`, `workspace`, `agent`, `platform`, `skills`) live in
[`templating.vars`][templating.vars]; the `context_files` resolver and
the `extends:` chain resolver live in
[`templating.context_files`][templating.context_files] and
[`templating.inheritance`][templating.inheritance]. The engine's
pre-flight wires them together at agent-load time.
"""

from templating.render import render_prompt

__all__ = ["render_prompt"]
