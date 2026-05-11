"""Loader tests: agent dir → AgentBundle, with strict early failures."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from movate.core.loader import AgentLoadError, load_agent

_TEMPLATE = Path(__file__).parent.parent / "src" / "movate" / "templates" / "agent_init"


def _scaffold_agent(dst: Path, name: str = "test-agent") -> Path:
    shutil.copytree(_TEMPLATE, dst)
    yaml_path = dst / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("__AGENT_NAME__", name))
    return dst


@pytest.mark.unit
def test_load_template_agent(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    assert bundle.spec.name == "demo"
    assert bundle.prompt_hash  # sha256 hex
    assert bundle.input_schema["required"] == ["text"]
    assert bundle.output_schema["required"] == ["message"]


@pytest.mark.unit
def test_render_prompt_with_input(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    rendered = bundle.render_prompt({"text": "ping"})
    assert "ping" in rendered


@pytest.mark.unit
def test_render_prompt_undefined_variable_fails(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    # StrictUndefined → missing namespace raises.
    with pytest.raises(Exception):
        bundle.render_prompt({})


@pytest.mark.unit
def test_render_messages_single_message_for_unsplit_template(tmp_path: Path) -> None:
    """Templates without ``{% block system %}`` keep the existing
    behavior: render the entire template as one user-role message.

    Back-compat: every shipped template except chatbot does this."""
    agent_dir = _scaffold_agent(tmp_path / "demo")
    bundle = load_agent(agent_dir)
    messages = bundle.render_messages({"text": "ping"})
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "ping" in messages[0]["content"]


@pytest.mark.unit
def test_render_messages_splits_system_and_user_blocks(tmp_path: Path) -> None:
    """Templates with ``{% block system %}`` and ``{% block user %}``
    produce two messages — system instructions first, current-turn
    user content second. This is the token-efficient chat path:
    multi-turn conversations send the system message ONCE rather than
    duplicating it inside every user turn."""
    agent_dir = _scaffold_agent(tmp_path / "demo")
    # Replace the prompt with a split version.
    (agent_dir / "prompt.md").write_text(
        "{% block system %}\nYou are a strict echo bot.\n{% endblock %}\n"
        "{% block user %}\n{{ input.text }}\n{% endblock %}\n"
    )
    # Reload to pick up the new prompt.
    bundle = load_agent(agent_dir)
    messages = bundle.render_messages({"text": "ping"})
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "echo bot" in messages[0]["content"]
    assert "ping" not in messages[0]["content"]  # user content stays out
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "ping"


@pytest.mark.unit
def test_render_messages_system_only_block_falls_through(tmp_path: Path) -> None:
    """Halfway-converted template (system block defined, user block
    missing) shouldn't produce an empty user message. We render the
    whole template as the user content as a safety net so the agent
    still works while the operator finishes splitting."""
    agent_dir = _scaffold_agent(tmp_path / "demo")
    (agent_dir / "prompt.md").write_text(
        "{% block system %}\nYou are a bot.\n{% endblock %}\nAdditional context: {{ input.text }}\n"
    )
    bundle = load_agent(agent_dir)
    messages = bundle.render_messages({"text": "ping"})
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    # User content is the WHOLE rendered template — includes the
    # system block too. Wasteful, but functional.
    assert "ping" in messages[1]["content"]
    assert "You are a bot" in messages[1]["content"]


@pytest.mark.unit
def test_load_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(AgentLoadError, match="not a directory"):
        load_agent(tmp_path / "does-not-exist")


@pytest.mark.unit
def test_load_missing_agent_yaml(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(AgentLoadError, match=r"agent\.yaml not found"):
        load_agent(tmp_path / "empty")


@pytest.mark.unit
def test_load_invalid_yaml(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    (agent_dir / "agent.yaml").write_text("this: is: not: yaml")
    with pytest.raises(AgentLoadError):
        load_agent(agent_dir)


@pytest.mark.unit
def test_load_validation_error_surfaces(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    yaml_path = agent_dir / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("0.1.0", "not-a-version"))
    with pytest.raises(AgentLoadError, match=r"agent\.yaml validation failed"):
        load_agent(agent_dir)


@pytest.mark.unit
def test_load_missing_prompt(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    (agent_dir / "prompt.md").unlink()
    with pytest.raises(AgentLoadError, match="prompt file not found"):
        load_agent(agent_dir)


@pytest.mark.unit
def test_load_invalid_input_schema(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "potato"})
    )
    with pytest.raises(AgentLoadError, match="invalid JSON schema"):
        load_agent(agent_dir)


@pytest.mark.unit
def test_prompt_hash_is_stable(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    a = load_agent(agent_dir)
    b = load_agent(agent_dir)
    assert a.prompt_hash == b.prompt_hash


@pytest.mark.unit
def test_prompt_hash_changes_when_prompt_changes(tmp_path: Path) -> None:
    agent_dir = _scaffold_agent(tmp_path / "demo")
    before = load_agent(agent_dir).prompt_hash
    (agent_dir / "prompt.md").write_text("changed")
    after = load_agent(agent_dir).prompt_hash
    assert before != after
