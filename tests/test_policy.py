"""``ModelPolicy`` — pure-Python checks + executor + validate integration.

Three concentric layers, each tested independently:

1. **Model checks** (``check_model`` / ``check_agent``) — pure logic; no
   side effects. Bulk of the coverage here so the contract is rock solid.
2. **Executor enforcement** (``Executor._enforce_policy`` via
   ``execute()``) — denied model → terminal ``policy_violation`` error,
   no provider call, no cost incurred. Cost ceiling = min(agent budget,
   policy ceiling).
3. **CLI integration** (``movate validate``) — exits 2 with a clean
   error pointing at ``movate.yaml: policy``; exit 0 with a "✓ compliant"
   hint when there's a policy and the agent satisfies it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.config import ModelPolicy, ProjectConfig, load_project_config
from movate.core.executor import Executor
from movate.core.failures import FailureType
from movate.core.loader import load_agent
from movate.core.models import AgentSpec, ModelConfig, ModelFallback, RunRequest
from movate.providers.mock import MockProvider
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer, scaffold_agent

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


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


def _make_spec(
    *,
    provider: str = "openai/gpt-4o-mini-2024-07-18",
    fallback: list[str] | None = None,
    budget: float = 0.50,
) -> AgentSpec:
    """Helper: build an AgentSpec with the model + budget we want to test."""
    return AgentSpec(
        api_version="movate/v1",
        kind="Agent",
        name="demo",
        version="0.1.0",
        model=ModelConfig(
            provider=provider,
            fallback=[ModelFallback(provider=fb) for fb in (fallback or [])],
        ),
        prompt="./prompt.md",
        schema=dict(input="./schema/input.json", output="./schema/output.json"),  # type: ignore[arg-type]
        budget={"max_cost_usd_per_run": budget},  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# 1. Pure-Python checks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_permissive_default_allows_everything() -> None:
    """An empty policy = no restrictions. ``is_permissive`` flags the
    fast-path so the Executor can skip the check entirely."""
    policy = ModelPolicy()
    assert policy.is_permissive() is True
    assert policy.check_model("openai/gpt-4o-mini") is None
    assert policy.check_agent(_make_spec()) == []
    # Cost ceiling defaults to the agent budget when policy is permissive.
    assert policy.effective_max_cost(1.50) == 1.50


@pytest.mark.unit
def test_allowed_providers_matches_on_prefix() -> None:
    """The ``allowed_providers`` field is a list of *prefixes*
    (the part before ``/``). ``openai/gpt-4o-mini`` matches prefix ``openai``."""
    policy = ModelPolicy(allowed_providers=["openai", "azure"])
    assert policy.is_permissive() is False
    assert policy.check_model("openai/gpt-4o-mini") is None
    assert policy.check_model("azure/gpt-4.1") is None

    err = policy.check_model("anthropic/claude-sonnet-4-6")
    assert err is not None
    assert "anthropic" in err
    assert "allowed_providers" in err


@pytest.mark.unit
def test_deny_models_takes_precedence_over_allowed_providers() -> None:
    """Explicit deny wins. A model in an otherwise-allowed provider can
    still be blocked — useful for pinning out deprecated revisions."""
    policy = ModelPolicy(
        allowed_providers=["openai"],
        deny_models=["openai/gpt-3.5-turbo"],
    )
    assert policy.check_model("openai/gpt-4o-mini") is None
    err = policy.check_model("openai/gpt-3.5-turbo")
    assert err is not None
    assert "deny_models" in err


@pytest.mark.unit
def test_check_agent_aggregates_violations_across_primary_and_fallbacks() -> None:
    """An agent with a denied primary AND a denied fallback gets BOTH
    violations reported in one pass — operator fixes everything at once."""
    policy = ModelPolicy(allowed_providers=["openai"])
    spec = _make_spec(
        provider="anthropic/claude-sonnet-4-6",
        fallback=["openai/gpt-4o-mini-2024-07-18", "google/gemini-pro"],
    )
    violations = policy.check_agent(spec)
    assert len(violations) == 2
    assert any("primary model" in v for v in violations)
    assert any("fallback 'google/gemini-pro'" in v for v in violations)


@pytest.mark.unit
def test_check_agent_flags_budget_above_policy_ceiling() -> None:
    """If the agent's budget is higher than the policy ceiling, the
    operator gets a violation at validate time — they don't ship an
    agent whose authored budget can't actually be exercised."""
    policy = ModelPolicy(max_cost_per_run_usd=0.10)
    spec = _make_spec(budget=0.50)
    violations = policy.check_agent(spec)
    assert len(violations) == 1
    assert "max_cost_usd_per_run=0.5" in violations[0]
    assert "0.1" in violations[0]


@pytest.mark.unit
def test_effective_max_cost_returns_min_of_agent_and_policy() -> None:
    """The runtime ceiling is whichever is lower: agent budget vs policy.
    Policy can tighten but never relax."""
    # Policy ceiling tighter than agent budget → policy wins.
    assert ModelPolicy(max_cost_per_run_usd=0.10).effective_max_cost(0.50) == 0.10
    # Agent budget tighter than policy ceiling → agent wins.
    assert ModelPolicy(max_cost_per_run_usd=1.00).effective_max_cost(0.50) == 0.50
    # Equal → either value (use the value).
    assert ModelPolicy(max_cost_per_run_usd=0.50).effective_max_cost(0.50) == 0.50


@pytest.mark.unit
def test_policy_loads_from_movate_yaml(tmp_path: Path, monkeypatch) -> None:
    """A ``movate.yaml`` with a ``policy:`` block round-trips through
    ``load_project_config``."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "movate.yaml").write_text(
        "policy:\n"
        "  allowed_providers: [openai, anthropic]\n"
        "  deny_models:\n"
        "    - openai/gpt-3.5-turbo\n"
        "  max_cost_per_run_usd: 0.25\n"
    )
    cfg = load_project_config()
    assert cfg.policy.allowed_providers == ["openai", "anthropic"]
    assert cfg.policy.deny_models == ["openai/gpt-3.5-turbo"]
    assert cfg.policy.max_cost_per_run_usd == 0.25


@pytest.mark.unit
def test_policy_absent_from_movate_yaml_is_permissive(tmp_path: Path, monkeypatch) -> None:
    """No ``policy:`` block = permissive default. Projects that don't
    set a policy see no behavior change."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "movate.yaml").write_text("agents_dir: ./agents\n")
    cfg = load_project_config()
    assert cfg.policy.is_permissive() is True


@pytest.mark.unit
def test_project_config_round_trips_policy() -> None:
    """``ProjectConfig`` accepts a populated policy at the Pydantic layer
    too — useful for programmatic construction in tests / embeds."""
    cfg = ProjectConfig(policy=ModelPolicy(allowed_providers=["openai"]))
    assert cfg.policy.allowed_providers == ["openai"]


# ---------------------------------------------------------------------------
# 2. Executor enforcement
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_executor_with_permissive_policy_runs_normally(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """Sanity: a permissive policy is indistinguishable from no policy."""
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    executor = Executor(
        provider=MockProvider(response='{"message": "ok"}'),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
        policy=ModelPolicy(),  # explicit permissive
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "success"


@pytest.mark.unit
async def test_executor_rejects_denied_primary_model_before_invoking(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """A denied model never reaches the provider — no LLM call, no cost,
    failure logged as ``policy_violation``."""
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))

    class ShouldNeverBeCalled(MockProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def complete(self, request):  # type: ignore[override]
            self.calls += 1
            return await super().complete(request)

    provider = ShouldNeverBeCalled()
    # Deny exactly the model the scaffolded agent uses.
    executor = Executor(
        provider=provider,
        pricing=pricing,
        storage=storage,
        tracer=tracer,
        policy=ModelPolicy(deny_models=["openai/gpt-4o-mini-2024-07-18"]),
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))

    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == FailureType.POLICY_VIOLATION.value
    # No provider call fired — denial is short-circuit.
    assert provider.calls == 0
    # Failure persisted to the failures table for audit.
    assert len(storage.failures) == 1
    assert storage.failures[0].failure_type == FailureType.POLICY_VIOLATION.value


@pytest.mark.unit
async def test_executor_rejects_provider_not_in_allowlist(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """``allowed_providers=[anthropic]`` blocks the openai-using
    scaffolded agent."""
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    executor = Executor(
        provider=MockProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
        policy=ModelPolicy(allowed_providers=["anthropic"]),
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == FailureType.POLICY_VIOLATION.value
    assert "allowed_providers" in response.error.message


@pytest.mark.unit
async def test_executor_rejects_denied_fallback_even_if_primary_allowed(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """A denied fallback also blocks the run — operator can't slip a
    forbidden model in via the fallback chain."""
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    # The scaffolded agent's fallback is anthropic/claude-haiku-4-5-20251001.
    executor = Executor(
        provider=MockProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
        policy=ModelPolicy(deny_models=["anthropic/claude-haiku-4-5-20251001"]),
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == FailureType.POLICY_VIOLATION.value
    assert "fallback" in response.error.message


@pytest.mark.unit
async def test_executor_model_override_skips_fallback_policy_check(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """``bench`` uses ``model_override`` which disables the fallback
    chain. Policy enforcement mirrors this: only the override is
    checked, not the (skipped) fallbacks."""
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    executor = Executor(
        provider=MockProvider(response='{"message": "ok"}'),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
        # The agent's fallback (anthropic/claude-...) IS denied, but
        # ``model_override`` disables the chain — so the run succeeds.
        policy=ModelPolicy(deny_models=["anthropic/claude-haiku-4-5-20251001"]),
    )
    override = ModelConfig(provider="openai/gpt-4o-mini-2024-07-18")
    response = await executor.execute(
        bundle,
        RunRequest(agent="demo", input={"text": "hi"}),
        model_override=override,
    )
    assert response.status == "success", response.error


@pytest.mark.unit
async def test_executor_policy_ceiling_tightens_budget(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """The agent's budget says 0.50; the policy ceiling tightens to a
    tiny value; a budget breach surfaces as
    ``cost_budget_exceeded``."""
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    # The MockProvider reports nonzero tokens so cost > 0; ceiling
    # below cost will trip the BudgetExceededError after the call.
    executor = Executor(
        provider=MockProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
        policy=ModelPolicy(max_cost_per_run_usd=0.0000001),
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    # Policy ceiling check at executor entry catches the
    # agent-budget-above-ceiling case first (agent budget 0.50, policy
    # ceiling 0.0000001 → policy_violation). This is intentional: the
    # operator should know NOW, not after spending the call.
    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == FailureType.POLICY_VIOLATION.value


@pytest.mark.unit
async def test_executor_policy_ceiling_below_agent_budget_short_circuits(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """When the agent's authored budget exceeds the policy ceiling, the
    policy check at executor entry surfaces it as
    ``policy_violation`` — operator sees the misconfig immediately."""
    bundle = load_agent(scaffold_agent(tmp_path / "demo", name="demo"))
    # Agent budget defaults to 0.50; policy ceiling 0.10 → violation.
    executor = Executor(
        provider=MockProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
        policy=ModelPolicy(max_cost_per_run_usd=0.10),
    )
    response = await executor.execute(bundle, RunRequest(agent="demo", input={"text": "hi"}))
    assert response.status == "error"
    assert response.error is not None
    assert response.error.type == FailureType.POLICY_VIOLATION.value
    assert "0.5" in response.error.message
    assert "0.1" in response.error.message


# ---------------------------------------------------------------------------
# 3. CLI integration — `movate validate`
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_validate_passes_when_policy_compliant(tmp_path: Path, monkeypatch) -> None:
    """``movate validate <agent>`` with a compliant policy: exit 0,
    "✓ compliant" hint in the table."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "movate.yaml").write_text("policy:\n  allowed_providers: [openai, anthropic]\n")
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "demo" in result.stdout
    assert "compliant" in result.stdout


@pytest.mark.unit
def test_cli_validate_fails_when_provider_not_allowed(tmp_path: Path, monkeypatch) -> None:
    """``movate validate`` flags a primary-model violation with exit 2
    and a pointer to the policy file."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "movate.yaml").write_text("policy:\n  allowed_providers: [anthropic]\n")
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "policy violation" in result.stdout
    # Operator pointer present so the error is self-fixing.
    assert "movate.yaml" in result.stdout


@pytest.mark.unit
def test_cli_validate_fails_when_model_in_deny_list(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "movate.yaml").write_text(
        "policy:\n  deny_models:\n    - openai/gpt-4o-mini-2024-07-18\n"
    )
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 2
    assert "deny_models" in result.stdout


@pytest.mark.unit
def test_cli_validate_no_policy_block_acts_as_permissive(tmp_path: Path, monkeypatch) -> None:
    """Projects without a ``policy:`` block see no behavior change —
    validate passes silently, no "compliant" hint (no policy ≠ "✓
    compliant"; one means there's a policy passing, the other means
    there's nothing to check)."""
    monkeypatch.chdir(tmp_path)
    # No movate.yaml at all → permissive default.
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 0
    assert "compliant" not in result.stdout


@pytest.mark.unit
def test_cli_validate_fails_when_budget_above_ceiling(tmp_path: Path, monkeypatch) -> None:
    """The agent's authored budget exceeds the policy ceiling →
    flagged at validate time, exit 2."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "movate.yaml").write_text("policy:\n  max_cost_per_run_usd: 0.10\n")
    # Scaffolded default budget is 0.50 — above the 0.10 ceiling.
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    result = runner.invoke(cli_app, ["validate", str(agent_dir)])
    assert result.exit_code == 2
    assert "max_cost_usd_per_run" in result.stdout
