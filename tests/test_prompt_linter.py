"""Prompt linter — per-rule unit tests + integration with ``movate validate``.

Each lint rule gets one happy-path + one finding test. The CLI
integration tests assert: lint output renders, errors exit 2,
warnings exit 0 (or 2 with --strict), --no-lint skips entirely.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.loader import load_agent
from movate.core.prompt_linter import LintIssue, lint_prompt
from movate.testing import scaffold_agent

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scaffold_with_prompt(tmp_path: Path, prompt: str) -> Path:
    """Scaffold the default agent then overwrite the prompt with ``prompt``."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    (agent_dir / "prompt.md").write_text(prompt)
    return agent_dir


def _scaffold_with_schemas(
    tmp_path: Path,
    *,
    prompt: str,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
) -> Path:
    """Scaffold + overwrite prompt + (optionally) schemas. Used by the
    undeclared-input-ref / no-output-schema-reference tests where we
    need control over the schema shape."""
    agent_dir = _scaffold_with_prompt(tmp_path, prompt)
    if input_schema is not None:
        (agent_dir / "schema" / "input.json").write_text(json.dumps(input_schema))
    if output_schema is not None:
        (agent_dir / "schema" / "output.json").write_text(json.dumps(output_schema))
    return agent_dir


def _codes(issues: list[LintIssue]) -> set[str]:
    return {i.code for i in issues}


# ---------------------------------------------------------------------------
# Rule 1: EMPTY_PROMPT / TINY_PROMPT
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_prompt_flagged_as_error(tmp_path: Path) -> None:
    """Whitespace-only prompt → EMPTY_PROMPT error. No real agent
    deploys with no instructions, so this is unambiguous."""
    agent_dir = _scaffold_with_prompt(tmp_path, "   \n  \t\n")
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    assert "EMPTY_PROMPT" in _codes(issues)
    empty = next(i for i in issues if i.code == "EMPTY_PROMPT")
    assert empty.severity == "error"
    assert empty.hint  # operator pointer present


@pytest.mark.unit
def test_tiny_prompt_flagged_as_warning(tmp_path: Path) -> None:
    """Sub-40-char prompt → TINY_PROMPT warning. Scaffolding leftover
    that somehow shipped. Reference output field so the
    output-schema-reference rule doesn't also fire."""
    agent_dir = _scaffold_with_prompt(tmp_path, "say message")
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    assert "TINY_PROMPT" in _codes(issues)
    tiny = next(i for i in issues if i.code == "TINY_PROMPT")
    assert tiny.severity == "warning"


@pytest.mark.unit
def test_normal_prompt_no_size_warning(tmp_path: Path) -> None:
    """The scaffolded default prompt is plenty long — no TINY warning."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    assert "TINY_PROMPT" not in _codes(issues)
    assert "EMPTY_PROMPT" not in _codes(issues)


# ---------------------------------------------------------------------------
# Rule 2: UNDECLARED_INPUT_REF
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_undeclared_input_ref_flagged_as_error(tmp_path: Path) -> None:
    """Prompt uses ``{{ input.question }}`` but input schema only
    declares ``text``. At runtime this raises Jinja's
    ``UndefinedError`` (StrictUndefined); catch at validate time."""
    agent_dir = _scaffold_with_schemas(
        tmp_path,
        prompt=(
            "You are a JSON-only assistant. Respond with `message`.\n"
            "Question: {{ input.question }}"  # 'question' not in input schema
        ),
        input_schema={
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
        },
    )
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    refs = [i for i in issues if i.code == "UNDECLARED_INPUT_REF"]
    assert len(refs) == 1
    assert refs[0].severity == "error"
    assert "question" in refs[0].message


@pytest.mark.unit
def test_undeclared_input_ref_lists_every_missing_var(tmp_path: Path) -> None:
    """Multiple undeclared refs → one error per missing var (sorted
    alphabetically for stable output). Operator fixes them all in
    one pass."""
    agent_dir = _scaffold_with_schemas(
        tmp_path,
        prompt=(
            "You are a JSON-only assistant. Respond with `message`.\n"
            "Foo: {{ input.foo }} Bar: {{ input.bar }} Baz: {{ input.baz }}"
        ),
        input_schema={
            "type": "object",
            "properties": {"bar": {"type": "string"}},  # only 'bar' declared
        },
    )
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    refs = [i for i in issues if i.code == "UNDECLARED_INPUT_REF"]
    # 'foo' and 'baz' are undeclared; 'bar' is fine.
    bad = [i.message for i in refs]
    assert any("foo" in m for m in bad)
    assert any("baz" in m for m in bad)
    assert not any("bar" in m for m in bad)


@pytest.mark.unit
def test_declared_input_refs_pass(tmp_path: Path) -> None:
    """All refs declared → no UNDECLARED_INPUT_REF findings."""
    agent_dir = _scaffold_with_schemas(
        tmp_path,
        prompt=(
            "You are a JSON-only assistant. Respond with `message`.\n"
            "Text: {{ input.text }} Lang: {{ input.lang }}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "lang": {"type": "string"},
            },
        },
    )
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    assert "UNDECLARED_INPUT_REF" not in _codes(issues)


@pytest.mark.unit
def test_string_literal_input_dot_x_not_flagged(tmp_path: Path) -> None:
    """``input.foo`` as plain text (not a Jinja expression) doesn't
    false-positive. The AST walker only sees Jinja Getattr nodes,
    not bare strings."""
    agent_dir = _scaffold_with_schemas(
        tmp_path,
        prompt=(
            "You are a JSON-only assistant. Respond with `message`.\n"
            "Example invalid input: input.foo (this is plain text). "
            "Use {{ input.text }}."
        ),
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
        },
    )
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    assert "UNDECLARED_INPUT_REF" not in _codes(issues)


# ---------------------------------------------------------------------------
# Rule 3: MISSING_JSON_INSTRUCTION
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_json_instruction_flagged_as_warning(tmp_path: Path) -> None:
    """Object output schema + no "json" in prompt → warning. Models
    wrap JSON in prose without an explicit instruction."""
    agent_dir = _scaffold_with_prompt(
        tmp_path,
        # No mention of "json" anywhere in this long prompt.
        "You are a helpful assistant. Respond with the user's message text "
        "in a structured object containing the field 'message' as a string.",
    )
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    j = [i for i in issues if i.code == "MISSING_JSON_INSTRUCTION"]
    assert len(j) == 1
    assert j[0].severity == "warning"


@pytest.mark.unit
@pytest.mark.parametrize("variant", ["JSON", "Json", "json"])
def test_json_instruction_case_insensitive(tmp_path: Path, variant: str) -> None:
    """Mentions of "JSON" or "Json" or "json" all satisfy the rule.

    Parametrized (not a loop) so each invocation gets its own
    pytest tmp_path — avoids the "destination exists" collision
    when scaffolding into the same directory twice.
    """
    agent_dir = _scaffold_with_prompt(
        tmp_path,
        f"You are an assistant. Respond with a {variant} object containing 'message'.",
    )
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    assert "MISSING_JSON_INSTRUCTION" not in _codes(issues)


# ---------------------------------------------------------------------------
# Rule 4: NO_OUTPUT_SCHEMA_REFERENCE
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_output_schema_reference_flagged_as_warning(tmp_path: Path) -> None:
    """Prompt mentions zero output field names → warning. Models tend
    to hallucinate field names without a sample.

    Field name ``classification_label`` deliberately picked to avoid
    accidental substring collisions with prompt words (English
    natural-language is full of common nouns that match short field
    names like 'reply' / 'answer' / 'result').
    """
    agent_dir = _scaffold_with_schemas(
        tmp_path,
        prompt="You are a JSON-only assistant. Respond with a single JSON object.",
        output_schema={
            "type": "object",
            "required": ["classification_label"],
            "properties": {"classification_label": {"type": "string"}},
        },
    )
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    out = [i for i in issues if i.code == "NO_OUTPUT_SCHEMA_REFERENCE"]
    assert len(out) == 1
    assert "classification_label" in out[0].message


@pytest.mark.unit
def test_output_schema_field_mentioned_passes(tmp_path: Path) -> None:
    """Prompt mentions at least one output field → no warning."""
    agent_dir = _scaffold_with_schemas(
        tmp_path,
        prompt=(
            "You are a JSON-only assistant. "
            'Respond with `{"message": "..."}` where message is the answer.'
        ),
        output_schema={
            "type": "object",
            "required": ["message"],
            "properties": {"message": {"type": "string"}},
        },
    )
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    assert "NO_OUTPUT_SCHEMA_REFERENCE" not in _codes(issues)


# ---------------------------------------------------------------------------
# Orchestrator: clean prompt has zero findings
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_scaffold_passes_every_rule(tmp_path: Path) -> None:
    """The scaffolded default agent has no findings. Critical: if the
    scaffold itself trips the linter, every `movate init` would
    surface confusing warnings."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    issues = lint_prompt(bundle)
    assert issues == [], f"scaffolded agent should be clean; got {issues}"


# ---------------------------------------------------------------------------
# CLI integration — `movate validate` exit codes + flags
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_validate_passes_clean_agent(tmp_path: Path) -> None:
    """Default scaffold passes lint + validate → exit 0 with the
    `lint: ✓ clean` hint visible."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout


@pytest.mark.unit
def test_cli_validate_exits_2_on_lint_error(tmp_path: Path) -> None:
    """Empty prompt → EMPTY_PROMPT error → exit 2. Schema validation
    already passed; the linter is the gate that catches this."""
    agent_dir = _scaffold_with_prompt(tmp_path, "")
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 2
    assert "EMPTY_PROMPT" in result.stdout


@pytest.mark.unit
def test_cli_validate_warning_does_not_fail_default(tmp_path: Path) -> None:
    """Warnings print but don't fail by default. Operator workflow:
    see the warning, decide if it matters, ship or fix."""
    # No mention of JSON in the prompt → MISSING_JSON_INSTRUCTION.
    agent_dir = _scaffold_with_prompt(
        tmp_path,
        # Long enough to avoid TINY_PROMPT; mentions 'message' (output field)
        # to avoid NO_OUTPUT_SCHEMA_REFERENCE.
        "You are a helpful assistant. Return your message in a structured "
        "format using the 'message' field. Be concise and friendly.",
    )
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout
    assert "MISSING_JSON_INSTRUCTION" in result.stdout


@pytest.mark.unit
def test_cli_validate_strict_promotes_warnings_to_failures(tmp_path: Path) -> None:
    """--strict flips warnings into exit 2. CI gate setting."""
    agent_dir = _scaffold_with_prompt(
        tmp_path,
        "You are a helpful assistant. Return your message in a structured "
        "format using the 'message' field. Be concise and friendly.",
    )
    result = runner.invoke(cli_app, ["validate", str(agent_dir), "--strict"])
    assert result.exit_code == 2
    assert "MISSING_JSON_INSTRUCTION" in result.stdout


@pytest.mark.unit
def test_cli_validate_no_lint_skips_linter(tmp_path: Path) -> None:
    """--no-lint: even an EMPTY_PROMPT passes. Escape hatch for tests
    or one-off `movate validate` against a half-baked agent."""
    agent_dir = _scaffold_with_prompt(tmp_path, "")
    result = runner.invoke(cli_app, ["validate", str(agent_dir), "--no-lint"])
    assert result.exit_code == 0
    assert "EMPTY_PROMPT" not in result.stdout
