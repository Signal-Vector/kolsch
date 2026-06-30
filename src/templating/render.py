"""Pure Jinja2 render harness for agent prompts.

One public function: [`render_prompt`][templating.render.render_prompt].
Takes a template string and a context dict; returns the rendered
string. No I/O, no engine dependencies, no implicit globals — every
variable the template references must be in `context` or it raises.

`StrictUndefined` is configured so typo'd variable names raise at
render time; the `| default("...")` filter still works because Jinja
runs the filter before the `StrictUndefined.__str__` trip-wire.
`autoescape=False` because agent prompts are model context, not HTML;
autoescaping would corrupt the rendered output.

Render failures surface as
[`ConfigTemplateRenderFailed`][errors.ConfigTemplateRenderFailed] with
the template identifier and a `reason` projection so the agent author
can locate the bad reference without reading a Jinja traceback.
"""

from __future__ import annotations

from typing import Any

from jinja2 import Environment, StrictUndefined
from jinja2.exceptions import TemplateError

from errors import ConfigTemplateRenderFailed


def render_prompt(
    template_string: str,
    context: dict[str, Any],
    *,
    template_name: str = "<inline>",
) -> str:
    """Render a Jinja2 template with the given context.

    The engine binds six standardised top-level variables (`tools`,
    `docs`, `workspace`, `agent`, `platform`, `skills`) at agent-load
    time, but this harness accepts any mapping so unit tests can
    render against scalars and the variable objects can plug in
    independently. A template with no Jinja markers renders
    byte-identical to itself.

    Args:
        template_string: The raw prompt source (the agent's `prompt:`
            field after pre-resolution but before render).
        context: The variable map bound into the Jinja environment.
        template_name: Identifier surfaced in the error detail when
            rendering fails. Production callers pass the agent's YAML
            path; tests default to `"<inline>"`.

    Returns:
        The rendered string.

    Raises:
        ConfigTemplateRenderFailed: Rendering surfaced a Jinja
            `UndefinedError` (typo'd variable under `StrictUndefined`),
            `TemplateSyntaxError` (malformed `{{ }}` / `{% %}`
            syntax), or any other `TemplateError` subclass.
    """
    env = Environment(undefined=StrictUndefined, autoescape=False)
    try:
        template = env.from_string(template_string)
        return template.render(context)
    except TemplateError as exc:
        raise ConfigTemplateRenderFailed(
            template=template_name,
            reason=str(exc),
        ) from exc
