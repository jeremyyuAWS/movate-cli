"""Eval engine: dataset loading, scoring, family enforcement, aggregation, end-to-end."""

from __future__ import annotations

from pathlib import Path

import pytest

from movate.core.eval import (
    EvalConfigError,
    EvalEngine,
    _subset_match,  # type: ignore[attr-defined]
    aggregate_scores,
    assert_cross_family,
    load_dataset,
    load_judge_config,
)
from movate.core.executor import Executor
from movate.core.loader import load_agent
from movate.core.models import JudgeMethod
from movate.providers import provider_family
from movate.providers.base import BaseLLMProvider
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import (
    InMemoryStorage,
    JudgeStubProvider,
    NullTracer,
    scaffold_agent,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _scaffold(dst: Path, name: str = "demo") -> Path:
    return scaffold_agent(dst, name=name)


@pytest.fixture
def pricing() -> PricingTable:
    return load_pricing()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def tracer() -> NullTracer:
    return NullTracer()


def _executor(provider: BaseLLMProvider, pricing: PricingTable, storage, tracer) -> Executor:
    return Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)


# ---------------------------------------------------------------------------
# Family helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "provider,family",
    [
        ("openai/gpt-4o-mini", "openai"),
        ("azure/gpt-4o", "openai"),
        ("azure_openai/gpt-4o", "openai"),
        ("anthropic/claude-sonnet-4-6", "anthropic"),
        ("gemini/gemini-1.5-pro", "google"),
        ("vertex_ai/gemini-1.5-pro", "google"),
        ("ollama/llama3", "ollama"),
        ("unknown/x", "unknown"),
    ],
)
def test_provider_family(provider: str, family: str) -> None:
    assert provider_family(provider) == family


@pytest.mark.unit
def test_assert_cross_family_rejects_same() -> None:
    with pytest.raises(EvalConfigError, match="same-family"):
        assert_cross_family("openai/gpt-4o", "openai/gpt-4o-mini")


@pytest.mark.unit
def test_assert_cross_family_rejects_azure_vs_openai() -> None:
    """Azure OpenAI shares model family with OpenAI."""
    with pytest.raises(EvalConfigError):
        assert_cross_family("openai/gpt-4o", "azure/gpt-4o")


@pytest.mark.unit
def test_assert_cross_family_accepts_distinct() -> None:
    assert_cross_family("openai/gpt-4o-mini", "anthropic/claude-sonnet-4-6")
    assert_cross_family("anthropic/claude-haiku-4-5-20251001", "gemini/gemini-1.5-pro")


# ---------------------------------------------------------------------------
# Aggregation modes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_aggregate_mean() -> None:
    assert aggregate_scores([1.0, 0.5, 0.0], "mean") == pytest.approx(0.5)


@pytest.mark.unit
def test_aggregate_min() -> None:
    assert aggregate_scores([1.0, 0.5, 0.0], "min") == 0.0


@pytest.mark.unit
def test_aggregate_p10() -> None:
    """p10 is near-worst-case; tolerates one outlier across many samples."""
    assert aggregate_scores([1.0], "p10") == 1.0
    # 10 scores, idx = floor(10 * 0.1) = 1 → second-lowest
    assert aggregate_scores([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9], "p10") == 0.1


@pytest.mark.unit
def test_aggregate_unknown_mode_raises() -> None:
    with pytest.raises(EvalConfigError, match="unknown gate_mode"):
        aggregate_scores([0.5], "weighted")


# ---------------------------------------------------------------------------
# Dataset loader
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_dataset_template(tmp_path: Path) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    cases, digest = load_dataset(bundle)
    assert len(cases) == 2
    assert cases[0].input == {"text": "hello"}
    assert cases[0].expected == {"message": "Hello!"}
    assert len(digest) == 64  # sha256 hex


@pytest.mark.unit
def test_load_dataset_invalid_json(tmp_path: Path) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text("{not json")
    bundle = load_agent(agent_dir)
    with pytest.raises(EvalConfigError, match="invalid JSON"):
        load_dataset(bundle)


@pytest.mark.unit
def test_load_dataset_skips_blank_lines(tmp_path: Path) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    ds = agent_dir / "evals" / "dataset.jsonl"
    ds.write_text(
        '\n{"input": {"text": "a"}, "expected": {"message": "A"}}\n\n'
        '{"input": {"text": "b"}, "expected": {"message": "B"}}\n'
    )
    bundle = load_agent(agent_dir)
    cases, _ = load_dataset(bundle)
    assert len(cases) == 2


# ---------------------------------------------------------------------------
# Judge config loader
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_judge_default_is_exact(tmp_path: Path) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    judge = load_judge_config(bundle)
    assert judge.method is JudgeMethod.EXACT
    assert judge.model is None


@pytest.mark.unit
def test_load_judge_from_yaml(tmp_path: Path) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "judge.yaml").write_text(
        "method: llm_judge\n"
        "model:\n"
        "  provider: anthropic/claude-sonnet-4-6\n"
        "rubric: 'be strict'\n"
        "threshold: 0.8\n"
    )
    bundle = load_agent(agent_dir)
    judge = load_judge_config(bundle)
    assert judge.method is JudgeMethod.LLM_JUDGE
    assert judge.model is not None
    assert judge.model.provider == "anthropic/claude-sonnet-4-6"
    assert judge.threshold == 0.8


@pytest.mark.unit
def test_load_judge_subset_match_no_model_required(tmp_path: Path) -> None:
    """``subset_match`` is deterministic — no LLM judge needed, so the
    YAML can omit ``model`` and ``rubric``. ``_validate_judge`` should
    accept this shape without complaint (the LLM_JUDGE-only requirement
    of ``model + rubric`` doesn't apply)."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "judge.yaml").write_text("method: subset_match\nthreshold: 0.7\n")
    bundle = load_agent(agent_dir)
    judge = load_judge_config(bundle)
    assert judge.method is JudgeMethod.SUBSET_MATCH
    assert judge.model is None
    assert judge.threshold == 0.7


# ---------------------------------------------------------------------------
# _subset_match — pure scoring function
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_subset_match_perfect_when_all_expected_keys_match() -> None:
    """Every key in ``expected`` appears in ``actual`` with the same
    value — extra keys in ``actual`` are tolerated."""
    actual = {"tone": "positive", "headline": "Approved!", "body": "..."}
    expected = {"tone": "positive"}
    score, rationale = _subset_match(actual, expected)
    assert score == 1.0
    assert rationale == "subset match"


@pytest.mark.unit
def test_subset_match_zero_when_a_key_is_missing() -> None:
    actual = {"headline": "Approved", "body": "..."}  # no `tone`
    expected = {"tone": "positive"}
    score, rationale = _subset_match(actual, expected)
    assert score == 0.0
    assert "missing" in rationale
    assert "tone" in rationale


@pytest.mark.unit
def test_subset_match_zero_when_value_differs() -> None:
    """Wrong value gets a detailed rationale naming the field, the
    actual value, and the expected — CI diffs can point at the
    specific regression."""
    actual = {"tone": "neutral", "extra": "fine"}
    expected = {"tone": "positive"}
    score, rationale = _subset_match(actual, expected)
    assert score == 0.0
    assert "tone" in rationale
    assert "'neutral'" in rationale
    assert "'positive'" in rationale


@pytest.mark.unit
def test_subset_match_lists_multiple_failures() -> None:
    """Multiple failing fields all appear in the rationale — not just
    the first — so the operator sees the whole shape of the regression."""
    actual = {"tone": "neutral"}  # missing decision_label; wrong tone
    expected = {"tone": "positive", "decision_label": "approve"}
    score, rationale = _subset_match(actual, expected)
    assert score == 0.0
    assert "tone" in rationale
    assert "decision_label" in rationale


@pytest.mark.unit
def test_subset_match_empty_expected_always_passes() -> None:
    """Edge case: empty expected dict means "no constraints" → always
    passes. Documented behaviour, not a bug."""
    score, _ = _subset_match({"anything": "goes"}, {})
    assert score == 1.0


# ---------------------------------------------------------------------------
# subset_match through the eval engine end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_judge_invalid_yaml_raises(tmp_path: Path) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "judge.yaml").write_text("method: not-a-method")
    bundle = load_agent(agent_dir)
    with pytest.raises(EvalConfigError, match="invalid judge config"):
        load_judge_config(bundle)


# ---------------------------------------------------------------------------
# Engine — exact-match scoring
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_engine_exact_match_pass(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    # MockProvider returns exactly what the dataset's first case expects.
    provider = MockProvider(response='{"message": "Hello!"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    summary = await engine.run(bundle)
    assert summary.sample_count == 2
    # First case matches → 1.0; second ("4") doesn't → 0.0.
    assert summary.cases[0].aggregated_score == 1.0
    assert summary.cases[0].passed
    assert summary.cases[1].aggregated_score == 0.0
    assert not summary.cases[1].passed
    assert summary.mean_score == 0.5
    assert summary.pass_rate == 0.5
    # Default per-case threshold is 0.7 → second case fails → overall fail.
    assert not summary.overall_pass


@pytest.mark.unit
async def test_engine_subset_match_passes_when_expected_keys_in_actual(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """The whole reason ``subset_match`` exists: an agent's output has
    MORE fields than the dataset pins. Exact-match would always fail;
    subset-match passes iff the pinned subset matches.

    Mimics case-reasoner's shape: agent outputs {message, extra};
    dataset pins {message: 'hello'}. Score = 1.0."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "judge.yaml").write_text("method: subset_match\nthreshold: 0.7\n")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        # Dataset pins only `message`; agent's full output will have more.
        '{"input": {"text": "hi"}, "expected": {"message": "hello"}}\n'
    )
    # Allow the agent's output schema to carry extra fields (the whole
    # point of subset_match — agent output is richer than the pin).
    (agent_dir / "schema" / "output.json").write_text(
        '{"$schema": "https://json-schema.org/draft/2020-12/schema",'
        '"type": "object", "additionalProperties": true,'
        '"required": ["message"],'
        '"properties": {"message": {"type": "string"}}}'
    )
    bundle = load_agent(agent_dir)
    provider = MockProvider(response='{"message": "hello", "extra_field": "fine"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    summary = await engine.run(bundle)
    assert summary.sample_count == 1
    assert summary.cases[0].aggregated_score == 1.0
    assert summary.cases[0].passed


@pytest.mark.unit
async def test_engine_subset_match_fails_on_value_drift(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """Subset_match still catches the regression that matters:
    expected key present in actual but with a different value → 0.0
    with a detailed rationale identifying the field."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "judge.yaml").write_text("method: subset_match\nthreshold: 0.7\n")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "wanted"}}\n'
    )
    bundle = load_agent(agent_dir)
    provider = MockProvider(response='{"message": "got_something_else"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    summary = await engine.run(bundle)
    assert summary.cases[0].aggregated_score == 0.0
    # Rationale captured in run details — the operator should be able
    # to see *what* field drifted (CI diff would name it).
    case_rationale = summary.cases[0].runs[0].rationale
    assert "message" in case_rationale


@pytest.mark.unit
async def test_engine_exact_match_all_pass_with_perfect_provider(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """Provider returning per-case expected output → 100% pass."""
    agent_dir = _scaffold(tmp_path / "demo")
    # Single-case dataset for determinism.
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}}\n'
    )
    bundle = load_agent(agent_dir)

    provider = MockProvider(response='{"message": "ok"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    summary = await engine.run(bundle)
    assert summary.overall_pass
    assert summary.pass_rate == 1.0
    assert summary.mean_score == 1.0


# ---------------------------------------------------------------------------
# Engine — N runs + aggregation modes
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_engine_runs_per_case_aggregates_correctly(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    """N runs through MockProvider give N identical 1.0 scores → mean stays 1.0."""
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}}\n'
    )
    bundle = load_agent(agent_dir)
    provider = MockProvider(response='{"message": "ok"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider, runs_per_case=3)

    summary = await engine.run(bundle)
    assert summary.runs_per_case == 3
    assert len(summary.cases[0].runs) == 3
    assert summary.cases[0].aggregated_score == 1.0


@pytest.mark.unit
def test_engine_rejects_zero_runs() -> None:
    with pytest.raises(EvalConfigError, match="runs_per_case must be"):
        EvalEngine(executor=None, provider=MockProvider(), runs_per_case=0)  # type: ignore[arg-type]


@pytest.mark.unit
def test_engine_rejects_unknown_gate_mode() -> None:
    with pytest.raises(EvalConfigError, match="gate_mode"):
        EvalEngine(executor=None, provider=MockProvider(), gate_mode="weighted")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Engine — LLM-as-judge path with cross-family enforcement
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_engine_llm_judge_happy_path(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "dataset.jsonl").write_text(
        '{"input": {"text": "hi"}, "expected": {"message": "ok"}}\n'
    )
    (agent_dir / "evals" / "judge.yaml").write_text(
        "method: llm_judge\n"
        "model:\n  provider: anthropic/claude-sonnet-4-6\n"
        "rubric: 'be strict'\n"
        "threshold: 0.8\n"
    )
    bundle = load_agent(agent_dir)

    provider = JudgeStubProvider(agent_response='{"message": "good"}', judge_score=0.95)
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider, runs_per_case=2)

    summary = await engine.run(bundle)
    assert summary.judge_provider == "anthropic/claude-sonnet-4-6"
    assert summary.cases[0].aggregated_score == pytest.approx(0.95)
    assert summary.cases[0].passed
    # Engine called both the agent provider (openai) and judge provider (anthropic).
    assert any(c.startswith("openai/") for c in provider.calls)
    assert any(c.startswith("anthropic/") for c in provider.calls)


@pytest.mark.unit
async def test_engine_rejects_same_family_judge(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "judge.yaml").write_text(
        "method: llm_judge\nmodel:\n  provider: openai/gpt-4o-2024-08-06\nrubric: 'x'\n"
    )
    bundle = load_agent(agent_dir)
    provider = JudgeStubProvider(agent_response='{"message": "x"}', judge_score=1.0)
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)

    with pytest.raises(EvalConfigError, match="same-family"):
        await engine.run(bundle)


@pytest.mark.unit
async def test_engine_llm_judge_requires_rubric(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    agent_dir = _scaffold(tmp_path / "demo")
    (agent_dir / "evals" / "judge.yaml").write_text(
        "method: llm_judge\nmodel:\n  provider: anthropic/claude-sonnet-4-6\n"
    )
    bundle = load_agent(agent_dir)
    provider = JudgeStubProvider(agent_response='{"message": "x"}', judge_score=1.0)
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)
    with pytest.raises(EvalConfigError, match="requires both 'model' and 'rubric'"):
        await engine.run(bundle)


# ---------------------------------------------------------------------------
# EvalSummary → EvalRecord
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_summary_to_record_round_trips(
    tmp_path: Path, pricing: PricingTable, storage, tracer
) -> None:
    bundle = load_agent(_scaffold(tmp_path / "demo"))
    provider = MockProvider(response='{"message": "Hello!"}')
    executor = _executor(provider, pricing, storage, tracer)
    engine = EvalEngine(executor=executor, provider=provider)
    summary = await engine.run(bundle)

    record = summary.to_record()
    assert record.agent == "demo"
    assert record.judge_method is JudgeMethod.EXACT
    assert record.judge_provider is None
    assert record.runs_per_case == 1
    assert record.gate_mode == "mean"
    assert record.sample_count == 2
