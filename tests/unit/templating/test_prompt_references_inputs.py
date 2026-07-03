"""Unit tests for ``prompt_references_inputs`` — bug-agent-inputs-template-scope.

The helper walks the agent-prompt Jinja AST looking for every
top-level ``inputs`` reference — bare, attribute access, subscript
access, and mixed chains. The tuple it returns feeds the
``ConfigInputsInAgentTemplate`` error message so authors see the
exact substrings they wrote.
"""

from __future__ import annotations

from templating.inheritance import prompt_references_inputs


class TestNoReference:
    def test_empty_prompt(self) -> None:
        assert prompt_references_inputs("") == ()

    def test_plain_text_prompt(self) -> None:
        assert prompt_references_inputs("You are a friendly assistant.") == ()

    def test_prompt_with_other_vars_only(self) -> None:
        prompt = (
            "You are {{ agent.name }}, running in workspace {{ workspace.path }}. "
            "You have access to tools: {{ tools }} and skills: {{ skills }}."
        )
        assert prompt_references_inputs(prompt) == ()

    def test_word_input_singular_is_not_flagged(self) -> None:
        # ``input`` is fine; only the specific top-level ``inputs``
        # matters.
        prompt = "The user sends an input; describe it: {{ agent.name }}."
        assert prompt_references_inputs(prompt) == ()

    def test_string_content_that_looks_like_inputs_is_not_flagged(self) -> None:
        # Literal "inputs" inside a rendered string is not a Jinja
        # variable reference — the AST walk only sees Names/Getattrs.
        prompt = "The token 'inputs' will appear here: {{ agent.name }}."
        assert prompt_references_inputs(prompt) == ()


class TestSingleReferenceForms:
    def test_bare_inputs(self) -> None:
        assert prompt_references_inputs("value: {{ inputs }}") == ("inputs",)

    def test_attribute_access(self) -> None:
        assert prompt_references_inputs("value: {{ inputs.foo }}") == ("inputs.foo",)

    def test_nested_attribute_access(self) -> None:
        assert prompt_references_inputs("value: {{ inputs.foo.bar.baz }}") == (
            "inputs.foo.bar.baz",
        )

    def test_string_subscript(self) -> None:
        assert prompt_references_inputs('value: {{ inputs["foo"] }}') == (
            'inputs["foo"]',
        )

    def test_integer_subscript(self) -> None:
        assert prompt_references_inputs("value: {{ inputs[0] }}") == ("inputs[0]",)


class TestMultipleReferences:
    def test_multiple_distinct_references(self) -> None:
        prompt = (
            "You are {{ agent.name }}.\n"
            "The task is: {{ inputs.job_description }}\n"
            "The scoring rubric: {{ inputs.rubric }}\n"
            "Reply as: {{ inputs['reply_format'] }}\n"
        )
        assert prompt_references_inputs(prompt) == (
            "inputs.job_description",
            "inputs.rubric",
            'inputs["reply_format"]',
        )

    def test_mixed_bare_and_attribute(self) -> None:
        prompt = "raw: {{ inputs }}, one: {{ inputs.foo }}"
        assert prompt_references_inputs(prompt) == ("inputs", "inputs.foo")


class TestControlBlocks:
    def test_reference_inside_for_loop(self) -> None:
        prompt = "{% for item in inputs.items %}  - {{ item }}\n{% endfor %}"
        # The ``inputs.items`` iterable AND the bare ``inputs`` chain
        # both surface via the AST walk. Order follows discovery order,
        # which for iterables is the top-most containing Getattr.
        refs = prompt_references_inputs(prompt)
        assert "inputs.items" in refs

    def test_reference_inside_if_block(self) -> None:
        prompt = "{% if inputs.mode == 'urgent' %}URGENT{% endif %}"
        refs = prompt_references_inputs(prompt)
        assert "inputs.mode" in refs


class TestMalformedTemplate:
    def test_parse_error_returns_empty_tuple(self) -> None:
        # Malformed Jinja returns an empty tuple; the ``StrictUndefined``
        # render path surfaces the syntax error separately.
        assert prompt_references_inputs("{{ inputs. }") == ()
        assert prompt_references_inputs("{% for x in") == ()


class TestErrorConstruction:
    def test_error_message_names_references(self) -> None:
        from errors import ConfigInputsInAgentTemplate

        exc = ConfigInputsInAgentTemplate(
            agent_path="kolsch/agents/probe",
            references=("inputs.foo", "inputs.bar"),
        )
        assert exc.code == "config.inputs_in_agent_template"
        assert "kolsch/agents/probe" in str(exc)
        assert "{{ inputs.foo }}" in str(exc)
        assert "{{ inputs.bar }}" in str(exc)
        assert exc.detail["references"] == ["inputs.foo", "inputs.bar"]

    def test_error_message_points_at_classifier(self) -> None:
        """The error names the canonical starter pattern so authors
        know where to look."""
        from errors import ConfigInputsInAgentTemplate

        exc = ConfigInputsInAgentTemplate(
            agent_path="kolsch/agents/probe",
            references=("inputs",),
        )
        assert "classifier" in str(exc)
        assert "io.inputs" in str(exc)
