"""``movate ci eval`` — discovers + gates every agent in the project.

Contract:

* All agents pass (no regressions) → exit 0.
* Any agent regressed beyond ``--regression-tolerance`` → exit 1.
* Any agent's eval engine errored (bad config, missing dataset) → exit 2.
* Agents without a baseline file are skipped with a notice, not a failure
  — landing a new agent before its baseline is committed shouldn't break
  the build.
* ``--summary-file`` appends a markdown table for ``$GITHUB_STEP_SUMMARY``.

Tests use --mock so they're hermetic. Baselines are written/regenerated
in tmp_path to avoid contaminating the repo's tracked baselines.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from movate.cli.main import app as cli_app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scaffold_simple_agent(agent_dir: Path, *, name: str) -> Path:
    """Minimal agent that's deterministic under MockProvider's default
    response. We use the existing ``classifier`` template style because
    its exact-match eval is the easiest to reason about."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "evals").mkdir(exist_ok=True)
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": name,
                "version": "0.1.0",
                "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
                "evals": {"dataset": "./evals/dataset.jsonl"},
            }
        )
    )
    (agent_dir / "prompt.md").write_text("echo {{ input.text }}\n")
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {"text": {"type": "string", "minLength": 1}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["message"],
                "properties": {"message": {"type": "string"}},
            }
        )
    )
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        # MockProvider's default response is {"message": "mock response"};
        # set expected = same so exact-match yields a perfect 1.0 score —
        # baseline is then locked in at 1.0, and subsequent runs match
        # (no regression).
        json.dumps({"input": {"text": "x"}, "expected": {"message": "mock response"}}) + "\n"
    )
    return agent_dir


def _project(root: Path, agents: list[str]) -> Path:
    """Set up a project directory with the given agents + a baseline-free
    .movate dir. Returns the root."""
    (root / "agents").mkdir(parents=True)
    for name in agents:
        _scaffold_simple_agent(root / "agents" / name, name=name)
    # movate.yaml so agents_dir resolves to ./agents.
    (root / "movate.yaml").write_text("agents_dir: ./agents\n")
    return root


def _write_baseline_via_first_run(
    project_root: Path, agent_name: str, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Generate a baseline by running ``movate eval --output-baseline``
    for one agent. Used by tests that want a baseline in place before
    running ``movate ci eval``."""
    monkeypatch.chdir(project_root)
    baseline_path = project_root / ".movate" / agent_name / "baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    result = runner.invoke(
        cli_app,
        [
            "eval",
            f"./agents/{agent_name}",
            "--mock",
            "--gate",
            "0.0",
            "--output-baseline",
            str(baseline_path),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    return baseline_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ci_eval_passes_when_no_baselines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without any baselines, ``movate ci eval`` runs every agent's
    eval but skips the gate. Exit 0 — landing a new agent before its
    baseline lands shouldn't block the PR.

    Each per-agent line includes a "no baseline" hint pointing at the
    expected file path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _project(tmp_path, agents=["alpha", "beta"])
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli_app, ["ci", "eval", "--mock"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Both agents got the "no baseline" notice on stderr.
    assert "alpha" in result.stderr
    assert "beta" in result.stderr
    assert "no baseline" in result.stderr


@pytest.mark.unit
def test_ci_eval_passes_when_baselines_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baselines generated from the same MockProvider response should
    match the current run exactly — no regression, exit 0."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _project(tmp_path, agents=["alpha"])
    _write_baseline_via_first_run(tmp_path, "alpha", monkeypatch)

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli_app, ["ci", "eval", "--mock"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Success line for the baselined agent.
    assert "alpha" in result.stderr
    assert "all good" in result.stderr


@pytest.mark.unit
def test_ci_eval_fails_when_baseline_regressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forge a "better" baseline by writing one with mean_score=1.0
    when the actual mock-mode run will score 0.0 (because we swap the
    mock response to one that won't satisfy the expected-output check).
    The ci eval gate should exit 1."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _project(tmp_path, agents=["alpha"])

    # Generate a "perfect" baseline first (mock response matches expected).
    baseline_path = _write_baseline_via_first_run(tmp_path, "alpha", monkeypatch)
    baseline = json.loads(baseline_path.read_text())
    assert baseline["mean_score"] == 1.0  # confirm the setup

    # Now sabotage the mock response so the current run scores 0.0.
    monkeypatch.setenv("MOVATE_MOCK_RESPONSE", '{"message": "different"}')
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli_app, ["ci", "eval", "--mock"])

    assert result.exit_code == 1, result.stdout + result.stderr
    assert "REGRESSED" in result.stderr or "regressed" in result.stderr


@pytest.mark.unit
def test_ci_eval_writes_markdown_summary_when_file_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--summary-file path`` appends a GitHub-Step-Summary-compatible
    markdown table. The file accumulates across step invocations — we
    use ``append`` mode so multiple workflow steps each contribute."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _project(tmp_path, agents=["alpha"])
    _write_baseline_via_first_run(tmp_path, "alpha", monkeypatch)

    summary_path = tmp_path / "summary.md"
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        cli_app,
        ["ci", "eval", "--mock", "--summary-file", str(summary_path)],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert summary_path.is_file()
    content = summary_path.read_text()
    assert "## movate ci eval" in content
    assert "alpha" in content
    # Markdown table header.
    assert "| agent | mean_score | pass_rate" in content


@pytest.mark.unit
def test_ci_eval_no_agents_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No agents in agents_dir → exit 2 with a clear error. Different
    from "agents but no baselines" (exit 0) — this is a misconfiguration."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "agents").mkdir()
    (tmp_path / "movate.yaml").write_text("agents_dir: ./agents\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli_app, ["ci", "eval", "--mock"])
    assert result.exit_code == 2
    assert "no agents found" in result.stderr
