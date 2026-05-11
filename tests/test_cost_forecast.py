"""Cost forecast — pure-math estimate + integration with `movate validate`.

The point: catch the "this eval would cost $3" surprise BEFORE the
engineer runs it. We test:

1. The estimate sums dataset cases x avg tokens x model price.
2. ``None`` is returned (gracefully) when info is missing.
3. ``movate validate`` prints the forecast line on the happy path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.cost_forecast import estimate_eval_cost
from movate.core.loader import load_agent
from movate.providers.pricing import ModelPrice, PricingTable, load_pricing
from movate.testing import scaffold_agent

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pricing(provider: str, *, input_per_1k: float, output_per_1k: float) -> PricingTable:
    """One-model pricing table for deterministic-cost tests."""
    return PricingTable(
        version="test-1",
        last_verified="2026-05-10",
        models={
            provider: ModelPrice(
                input_per_1k=input_per_1k,
                output_per_1k=output_per_1k,
            )
        },
    )


def _write_dataset(agent_dir: Path, cases: list[dict]) -> Path:
    """Replace the scaffolded dataset with N hand-crafted cases."""
    p = agent_dir / "evals" / "dataset.jsonl"
    p.write_text("\n".join(json.dumps(c) for c in cases))
    return p


# ---------------------------------------------------------------------------
# 1. Pure-math estimate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_forecast_returns_none_when_dataset_missing(tmp_path: Path) -> None:
    """No dataset.jsonl on disk → None. Silent skip — operators see
    nothing rather than a confusing 'couldn't estimate' message."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    # Delete the scaffolded dataset so the path doesn't resolve.
    (agent_dir / "evals" / "dataset.jsonl").unlink()
    bundle = load_agent(agent_dir)
    assert estimate_eval_cost(bundle, pricing=load_pricing()) is None


@pytest.mark.unit
def test_forecast_returns_none_when_no_pricing(tmp_path: Path) -> None:
    """Model not in the pricing table → None. A custom provider /
    fine-tuned model that's not in our packaged prices.yaml."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    bundle = load_agent(agent_dir)
    empty_pricing = PricingTable(version="test", last_verified="2026", models={})
    assert estimate_eval_cost(bundle, pricing=empty_pricing) is None


@pytest.mark.unit
def test_forecast_returns_none_when_dataset_empty(tmp_path: Path) -> None:
    """Dataset file exists but has 0 cases → None. Math is undefined
    over zero samples."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text("")
    bundle = load_agent(agent_dir)
    assert estimate_eval_cost(bundle, pricing=load_pricing()) is None


@pytest.mark.unit
def test_forecast_computes_cost_for_known_pricing(tmp_path: Path) -> None:
    """End-to-end math sanity. With known per-1k prices + known
    rendered prompt length, the forecast matches hand-computed cost."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    # Five identical cases so the math is deterministic. The default
    # template is small (~150 chars); each rendered case adds the
    # 'text' field interpolation. We don't care about the absolute
    # number; we DO care that the math reflects the input/output
    # per-1k rates.
    _write_dataset(agent_dir, [{"input": {"text": "hello world"}} for _ in range(5)])
    bundle = load_agent(agent_dir)
    pricing = _make_pricing(bundle.spec.model.provider, input_per_1k=10.0, output_per_1k=20.0)

    forecast = estimate_eval_cost(bundle, pricing=pricing)
    assert forecast is not None
    assert forecast.cases == 5
    # The cost-per-call is input_tokens * 10/1000 + output_tokens *
    # 20/1000. We can re-derive it from the reported token counts.
    expected = (
        forecast.input_tokens_per_call / 1000.0 * 10.0
        + forecast.output_tokens_per_call / 1000.0 * 20.0
    )
    assert forecast.cost_per_call_usd == pytest.approx(expected, rel=1e-3)
    assert forecast.total_cost_usd == pytest.approx(expected * 5, rel=1e-3)


@pytest.mark.unit
def test_forecast_uses_default_output_tokens_when_unset(tmp_path: Path) -> None:
    """No ``max_tokens`` in agent.yaml → 500-token default for output
    budget. Caught here so a missing param doesn't crash the
    forecast (and so we can change the default centrally)."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    # Strip max_tokens from agent.yaml to exercise the fallback.
    yaml_path = agent_dir / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("max_tokens: 1024", ""))
    _write_dataset(agent_dir, [{"input": {"text": "hi"}}])
    bundle = load_agent(agent_dir)
    forecast = estimate_eval_cost(bundle, pricing=load_pricing())
    assert forecast is not None
    assert forecast.output_tokens_per_call == 500


@pytest.mark.unit
def test_forecast_respects_max_tokens_from_agent_yaml(tmp_path: Path) -> None:
    """``max_tokens: 2048`` in agent.yaml → output budget = 2048."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    yaml_path = agent_dir / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("max_tokens: 1024", "max_tokens: 2048"))
    _write_dataset(agent_dir, [{"input": {"text": "hi"}}])
    bundle = load_agent(agent_dir)
    forecast = estimate_eval_cost(bundle, pricing=load_pricing())
    assert forecast is not None
    assert forecast.output_tokens_per_call == 2048


@pytest.mark.unit
def test_forecast_skips_cases_with_invalid_inputs(tmp_path: Path) -> None:
    """A case whose input refs a missing schema field can't be rendered.
    Forecast skips it gracefully — the prompt linter is the right
    diagnostic for that bug."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    _write_dataset(
        agent_dir,
        [
            {"input": {"text": "valid case"}},
            {"input": {}},  # missing 'text' → StrictUndefined raises in render
            {"input": {"text": "another valid case"}},
        ],
    )
    bundle = load_agent(agent_dir)
    forecast = estimate_eval_cost(bundle, pricing=load_pricing())
    assert forecast is not None
    # Two valid cases (case-with-missing-input was skipped).
    assert forecast.cases == 2


@pytest.mark.unit
def test_forecast_scales_linearly_with_case_count(tmp_path: Path) -> None:
    """10 cases → 2x the total cost of 5 cases (same template)."""

    def _forecast_n_cases(n: int) -> float:
        agent_dir = scaffold_agent(tmp_path / f"demo_{n}", name="demo")
        _write_dataset(agent_dir, [{"input": {"text": "hi"}} for _ in range(n)])
        bundle = load_agent(agent_dir)
        f = estimate_eval_cost(bundle, pricing=load_pricing())
        assert f is not None
        return f.total_cost_usd

    five = _forecast_n_cases(5)
    ten = _forecast_n_cases(10)
    # 10/5 = 2.0, exactly. ``rel=1e-3`` tolerates float rounding at
    # six-decimal precision.
    assert ten / five == pytest.approx(2.0, rel=1e-3)


# ---------------------------------------------------------------------------
# 2. CLI integration — `movate validate` prints the forecast
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_validate_prints_cost_forecast_when_dataset_present(tmp_path: Path) -> None:
    """Default scaffold ships a dataset.jsonl + a priced model →
    validate prints an ``eval cost:`` line."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout
    assert "eval cost" in result.stdout


@pytest.mark.unit
def test_cli_validate_omits_forecast_when_no_dataset(tmp_path: Path) -> None:
    """Agent without a dataset file → no forecast line. Silent skip,
    not 'unable to estimate' noise."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    (agent_dir / "evals" / "dataset.jsonl").unlink()
    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0
    assert "eval cost" not in result.stdout
