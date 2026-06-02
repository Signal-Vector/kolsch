"""Pure Jinja2 render harness — Phase 2.2 Step 2 (ENV §3).

One public function: :func:`render_prompt`. Takes a template string
and a context dict; returns the rendered string. No I/O, no engine
dependencies, no implicit globals — every variable the template
references must be in ``context`` or it raises.

Configuration:

- :class:`jinja2.StrictUndefined` — typo'd variable names raise at
  render time. The ``| default("…")`` filter still works because
  Jinja's filter system runs `default` BEFORE the
  ``StrictUndefined.__str__`` trip-wire (verified against jinja2
  3.1.6 in Step 2 — escalation gate clear).
- ``autoescape=False`` — agent prompts are model context, not HTML;
  autoescaping would corrupt the rendered output.

Errors surface as :class:`errors.ConfigTemplateRenderFailed`
(``config.template_render_failed``) — the Fatal envelope §4.18
mandates for any agent-load rendering failure. The exception's
``detail`` carries the ``template`` source identifier the caller
passed plus the ``reason`` (the Jinja exception's ``str``
projection), so the agent author can locate the bad reference
without reading a Jinja traceback.

The engine's pre-flight step 5.6.6 is the production caller; tests
and Step 14's wiring exercise the same surface.
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

    :param template_string: the raw prompt source (the agent's
        ``prompt:`` field after pre-resolution but before render).
    :param context: the variable map. Phase 2.2 §4 standardises the
        five top-level keys (``tools`` / ``docs`` / ``workspace`` /
        ``agent`` / ``platform``) that the engine populates; this
        harness accepts any mapping so unit tests can render against
        scalars and Steps 3–7's variable objects can plug in
        independently.
    :param template_name: identifier surfaced in the error detail
        when rendering fails. Production callers pass the agent's
        YAML path; tests default to ``"<inline>"``.

    :returns: the rendered string. A template with no Jinja markers
        (no ``{{ }}`` or ``{% %}``) renders byte-identical to itself,
        preserving Phase 2.2 §3.2's backward-compatibility contract.

    :raises ConfigTemplateRenderFailed: when rendering surfaces a
        Jinja ``UndefinedError`` (typo'd variable under
        StrictUndefined), ``TemplateSyntaxError`` (malformed
        ``{{ }} / {% %}`` syntax), or any other ``TemplateError``
        subclass. The Fatal envelope's ``detail`` carries
        ``{template, reason}``.
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
