"""``movate validate`` checks the AgentRuntime field.

The runtime field (added in Tier-2 #5) lets agents declare
``runtime: native_anthropic`` / ``native_openai`` / ``langchain`` —
but those adapters don't ship until Tier-2 #6/#7/#8. Validate
should reject unwired runtimes at parse time so the operator
learns BEFORE they try to run, not after.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.models import AgentRuntime
from movate.testing import scaffold_agent

runner = CliRunner(mix_stderr=False)


def _set_runtime(agent_dir: Path, runtime: AgentRuntime) -> None:
    yaml_path = agent_dir / "agent.yaml"
    spec = yaml.safe_load(yaml_path.read_text())
    spec["runtime"] = runtime.value
    yaml_path.write_text(yaml.safe_dump(spec))


@pytest.mark.unit
def test_validate_accepts_default_litellm_runtime(tmp_path: Path) -> None:
    """No ``runtime:`` field → defaults to litellm → validate passes
    + shows ``runtime: litellm`` in the success banner."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "runtime:     litellm" in result.stdout


@pytest.mark.unit
def test_validate_accepts_explicit_litellm_runtime(tmp_path: Path) -> None:
    """Explicit ``runtime: litellm`` also passes."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_runtime(agent_dir, AgentRuntime.LITELLM)
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + result.stderr


@pytest.mark.unit
def test_validate_rejects_unwired_runtime_by_simulating_missing_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every runtime in :class:`AgentRuntime` now has an adapter when
    its extra is installed (#5-#8). To still exercise the
    "unsupported runtime" branch, simulate the ``langchain_core``
    import failing — then the validate flow should reject
    ``runtime: langchain`` with the unsupported-runtime banner."""
    import builtins  # noqa: PLC0415

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "langchain_core":
            raise ImportError("simulated: langchain not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_runtime(agent_dir, AgentRuntime.LANGCHAIN)
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "unsupported runtime" in result.stdout
    assert "langchain" in result.stdout
    assert "litellm" in result.stdout


@pytest.mark.unit
def test_validate_accepts_native_anthropic_when_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the ``anthropic`` extra is installed (Tier-2 #6 landed),
    ``runtime: native_anthropic`` should pass validate — same code
    path as the LiteLLM happy case.

    ``monkeypatch.chdir`` isolates this test from the repo's own
    ``movate.yaml`` which now declares ``runtime.allowed: [litellm]``
    as the project-wide stance. Running from tmp_path picks up no
    project config, so the validate flow uses the permissive default."""
    pytest.importorskip("anthropic")
    monkeypatch.chdir(tmp_path)
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_runtime(agent_dir, AgentRuntime.NATIVE_ANTHROPIC)
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "runtime:     native_anthropic" in result.stdout


@pytest.mark.unit
def test_validate_accepts_native_openai_when_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the ``openai`` extra is installed (Tier-2 #7 landed),
    ``runtime: native_openai`` should pass validate (when not gated
    by RuntimePolicy)."""
    pytest.importorskip("openai")
    monkeypatch.chdir(tmp_path)
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_runtime(agent_dir, AgentRuntime.NATIVE_OPENAI)
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "runtime:     native_openai" in result.stdout


@pytest.mark.unit
def test_validate_accepts_langchain_when_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the ``langchain`` extra is installed (Tier-2 #8 landed),
    ``runtime: langchain`` should pass validate. NOTE: the agent's
    ``model.provider`` field on this runtime is an entry-point spec
    (``package.module:function``) — validate doesn't try to import it
    yet (that happens at execute time), so the agent passes even if
    the entry-point doesn't exist."""
    pytest.importorskip("langchain_core")
    monkeypatch.chdir(tmp_path)
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_runtime(agent_dir, AgentRuntime.LANGCHAIN)
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "runtime:     langchain" in result.stdout


@pytest.mark.unit
def test_validate_runtime_policy_blocks_native_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``movate.yaml: runtime.allowed: [litellm]`` enforces 'A by default' —
    an agent that opts into a native runtime is rejected at validate
    time with a clear policy-violation message."""
    pytest.importorskip("anthropic")
    monkeypatch.chdir(tmp_path)
    # Drop a movate.yaml that locks the project to LiteLLM only.
    (tmp_path / "movate.yaml").write_text("runtime:\n  allowed: [litellm]\n")

    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_runtime(agent_dir, AgentRuntime.NATIVE_ANTHROPIC)
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "runtime policy violation" in result.stdout
    # The error names both the agent's runtime and the allowed set.
    assert "native_anthropic" in result.stdout
    assert "litellm" in result.stdout


@pytest.mark.unit
def test_validate_runtime_policy_permissive_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a ``runtime.allowed`` block in movate.yaml, any
    installed runtime is permitted — backwards-compatible default."""
    pytest.importorskip("anthropic")
    monkeypatch.chdir(tmp_path)
    # movate.yaml exists but says nothing about runtime.
    (tmp_path / "movate.yaml").write_text("agents_dir: ./agents\n")

    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _set_runtime(agent_dir, AgentRuntime.NATIVE_ANTHROPIC)
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + result.stderr


@pytest.mark.unit
def test_validate_rejects_unknown_runtime_string(tmp_path: Path) -> None:
    """A string that isn't even a known AgentRuntime value fails
    at YAML load time (Pydantic enum validation) — exit 2 with a
    load-error message, not the runtime-availability message."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    yaml_path = agent_dir / "agent.yaml"
    spec = yaml.safe_load(yaml_path.read_text())
    spec["runtime"] = "telepathy"
    yaml_path.write_text(yaml.safe_dump(spec))

    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 2
    assert "validation failed" in result.stdout
