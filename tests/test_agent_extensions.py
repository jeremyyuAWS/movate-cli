"""Tests for the v0.7-forward AgentSpec extension fields.

Covers the new ``goals`` / ``objectives`` / ``examples`` fields added
per Deva's strategic feedback (May 2026). All three are optional with
empty-list defaults — existing agent.yaml files that don't declare
them continue to load unchanged. These tests assert that:

* Empty-default backwards-compat holds.
* Each new field accepts well-formed values.
* Validation rejects malformed inputs (bad objective id, duplicate
  objective ids, etc.).
* The fields survive round-tripping through model_dump → model_validate.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from movate.core.models import AgentSpec, Example, Objective


def _base_spec_dict() -> dict:
    """A minimal valid agent.yaml dict — same shape every test starts from."""
    return {
        "api_version": "movate/v1",
        "name": "test-agent",
        "version": "0.1.0",
        "model": {"provider": "openai/gpt-4o-mini"},
        "prompt": "./prompt.md",
        "schema": {
            "input": "./schema/input.json",
            "output": "./schema/output.json",
        },
    }


# ---------------------------------------------------------------------------
# Backwards-compat: empty defaults
# ---------------------------------------------------------------------------


def test_existing_agents_without_new_fields_still_load() -> None:
    """Critical: every shipped agent.yaml in customer repos predates these
    fields. Loading must succeed with empty defaults."""
    spec = AgentSpec.model_validate(_base_spec_dict())
    assert spec.goals == []
    assert spec.objectives == []
    assert spec.examples == []


def test_empty_lists_explicit_also_work() -> None:
    data = _base_spec_dict() | {"goals": [], "objectives": [], "examples": []}
    spec = AgentSpec.model_validate(data)
    assert spec.goals == []
    assert spec.objectives == []
    assert spec.examples == []


# ---------------------------------------------------------------------------
# goals field
# ---------------------------------------------------------------------------


def test_goals_accepts_free_form_strings() -> None:
    data = _base_spec_dict() | {"goals": ["Be helpful.", "Be accurate."]}
    spec = AgentSpec.model_validate(data)
    assert spec.goals == ["Be helpful.", "Be accurate."]


# ---------------------------------------------------------------------------
# objectives field
# ---------------------------------------------------------------------------


def test_objective_accepts_minimal_form() -> None:
    data = _base_spec_dict() | {
        "objectives": [{"id": "routing-accuracy"}],
    }
    spec = AgentSpec.model_validate(data)
    assert len(spec.objectives) == 1
    obj = spec.objectives[0]
    assert obj.id == "routing-accuracy"
    assert obj.threshold == 0.7  # default
    assert obj.judge == "exact"  # default
    assert obj.description == ""


def test_objective_accepts_full_form() -> None:
    data = _base_spec_dict() | {
        "objectives": [
            {
                "id": "answer-quality",
                "description": "Free-form prose quality vs the rubric",
                "threshold": 0.85,
                "judge": "llm_judge",
            }
        ],
    }
    spec = AgentSpec.model_validate(data)
    obj = spec.objectives[0]
    assert obj.id == "answer-quality"
    assert obj.threshold == 0.85
    assert obj.judge == "llm_judge"
    assert "rubric" in obj.description


def test_objective_id_must_be_slug_shape() -> None:
    """IDs are used in CLI flags like `mdk eval --objective routing-accuracy`;
    they must be safe to type and stable."""
    data = _base_spec_dict() | {
        "objectives": [{"id": "Has Spaces"}],
    }
    with pytest.raises(ValidationError, match="lowercase alphanumeric"):
        AgentSpec.model_validate(data)


def test_objective_threshold_bounded_0_to_1() -> None:
    data = _base_spec_dict() | {
        "objectives": [{"id": "x", "threshold": 1.5}],
    }
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(data)


def test_objective_judge_rejects_unknown_method() -> None:
    data = _base_spec_dict() | {
        "objectives": [{"id": "x", "judge": "vibe_check"}],
    }
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(data)


def test_objective_ids_must_be_unique_within_agent() -> None:
    """Two objectives with the same id make `--objective <id>` ambiguous."""
    data = _base_spec_dict() | {
        "objectives": [
            {"id": "quality"},
            {"id": "quality"},
        ],
    }
    with pytest.raises(ValidationError, match="duplicate objective id"):
        AgentSpec.model_validate(data)


def test_objectives_extra_keys_rejected() -> None:
    """Forward-compatibility safety: typo in a field name fails loudly
    rather than silently dropping the value (model_config extra='forbid')."""
    data = _base_spec_dict() | {
        "objectives": [
            {"id": "x", "tresholde": 0.5},  # threshold typo
        ],
    }
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(data)


# ---------------------------------------------------------------------------
# examples field
# ---------------------------------------------------------------------------


def test_example_accepts_input_only() -> None:
    """Examples may omit ``output`` for non-deterministic agents — the
    example is still useful as input documentation + test seed."""
    data = _base_spec_dict() | {
        "examples": [{"input": {"question": "What is movate?"}}],
    }
    spec = AgentSpec.model_validate(data)
    assert len(spec.examples) == 1
    assert spec.examples[0].input == {"question": "What is movate?"}
    assert spec.examples[0].output == {}


def test_example_accepts_input_and_output() -> None:
    data = _base_spec_dict() | {
        "examples": [
            {
                "input": {"question": "Where is HQ?"},
                "output": {"answer": "Plano, TX", "confidence": 0.95},
                "description": "Canonical example used in onboarding",
            }
        ],
    }
    spec = AgentSpec.model_validate(data)
    ex = spec.examples[0]
    assert ex.output["answer"] == "Plano, TX"
    assert ex.description.startswith("Canonical")


def test_example_input_required() -> None:
    data = _base_spec_dict() | {
        "examples": [{"output": {"answer": "no question"}}],
    }
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(data)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_full_spec_round_trips_through_serialization() -> None:
    """All three fields serialize + deserialize without loss — important
    for `mdk show` output and any future export tooling."""
    data = _base_spec_dict() | {
        "goals": ["Help users", "Stay grounded"],
        "objectives": [
            {"id": "accuracy", "threshold": 0.9, "judge": "exact"},
            {"id": "groundedness", "threshold": 0.8, "judge": "llm_judge"},
        ],
        "examples": [
            {
                "input": {"q": "x"},
                "output": {"a": "y"},
            },
        ],
    }
    spec = AgentSpec.model_validate(data)
    round_tripped = AgentSpec.model_validate(spec.model_dump())
    assert round_tripped.goals == spec.goals
    assert [o.id for o in round_tripped.objectives] == [o.id for o in spec.objectives]
    assert round_tripped.examples[0].input == spec.examples[0].input


# ---------------------------------------------------------------------------
# Submodel direct construction (used by importers + scaffolders)
# ---------------------------------------------------------------------------


def test_objective_constructed_directly() -> None:
    obj = Objective(id="x", threshold=0.5)
    assert obj.judge == "exact"  # default


def test_example_constructed_directly() -> None:
    ex = Example(input={"q": "x"})
    assert ex.output == {}
    assert ex.description == ""
