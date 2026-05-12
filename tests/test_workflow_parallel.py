"""Parallel fan-out + fan-in — reducers, typed-state, end-to-end routing.

Three layers of coverage matching the design:

1. **Reducer registry + state-schema extraction** — named reducers
   (`append` / `union` / `max` / `min` / `last` / `merge`) behave correctly
   and the JSON Schema walker finds `x-movate-reducer` annotations.
2. **Structural validator** — ``validate_dag`` accepts parallel
   fan-out, enforces the minimum-2-branches rule, and still catches the
   conditional rules layered underneath.
3. **End-to-end** — a workflow with three parallel branches merging at
   a fan-in node executes successfully and reducers combine state
   correctly under ``runtime: langgraph``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("langgraph")

from movate.core.executor import Executor
from movate.core.models import WorkflowStatus
from movate.core.workflow import (
    WorkflowCompileError,
    compile_workflow,
    load_workflow_spec,
    validate_dag,
)
from movate.core.workflow.compilers._typed_state import build_typed_state_class
from movate.core.workflow.reducers import (
    REDUCERS,
    ReducerError,
    extract_reducers,
)
from movate.core.workflow.runner import WorkflowRunner
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer

# ---------------------------------------------------------------------------
# Reducer registry — each named reducer's contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,left,right,expected",
    [
        ("append", [1, 2], [3, 4], [1, 2, 3, 4]),
        ("append", None, [3, 4], [3, 4]),
        ("append", [1, 2], None, [1, 2]),
        ("union", [1, 2, 3], [2, 3, 4], [1, 2, 3, 4]),  # dedup preserves order
        ("union", ["a", "b"], ["b", "c"], ["a", "b", "c"]),
        ("max", 1, 5, 5),
        ("max", 7, 3, 7),
        ("max", None, 5, 5),
        ("min", 1, 5, 1),
        ("min", None, 5, 5),
        ("last", "old", "new", "new"),
        ("merge", {"a": 1}, {"b": 2}, {"a": 1, "b": 2}),
        ("merge", {"a": 1, "b": 2}, {"b": 9}, {"a": 1, "b": 9}),  # right wins
        ("merge", None, {"a": 1}, {"a": 1}),
    ],
)
def test_reducer_registry_implements_documented_semantics(
    name: str, left: object, right: object, expected: object
) -> None:
    reducer = REDUCERS[name]
    assert reducer(left, right) == expected


@pytest.mark.unit
def test_registry_holds_exactly_six_documented_names() -> None:
    """Adding a name is breaking-ish (compiler validation + docs change);
    pin the set so adding one mid-PR surfaces here."""
    assert set(REDUCERS.keys()) == {"append", "union", "max", "min", "last", "merge"}


# ---------------------------------------------------------------------------
# State-schema reducer extraction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_reducers_finds_annotated_properties() -> None:
    schema = {
        "type": "object",
        "properties": {
            "history": {
                "type": "array",
                "x-movate-reducer": "append",
            },
            "score": {
                "type": "number",
                "x-movate-reducer": "max",
            },
            "name": {"type": "string"},  # no reducer
        },
    }
    found = extract_reducers(schema)
    assert set(found) == {"history", "score"}
    # Each entry maps to the registered callable.
    assert found["history"] is REDUCERS["append"]
    assert found["score"] is REDUCERS["max"]


@pytest.mark.unit
def test_extract_reducers_rejects_unknown_name() -> None:
    schema = {
        "type": "object",
        "properties": {
            "history": {"type": "array", "x-movate-reducer": "concat_first"},
        },
    }
    with pytest.raises(ReducerError, match="unknown reducer"):
        extract_reducers(schema)


@pytest.mark.unit
def test_extract_reducers_returns_empty_when_no_annotations() -> None:
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    assert extract_reducers(schema) == {}


@pytest.mark.unit
def test_extract_reducers_handles_missing_or_malformed_schema() -> None:
    """Schemas without `properties` (or with non-dict properties) just
    return empty rather than raising — the schema is still valid JSON
    Schema, just doesn't declare reducers."""
    assert extract_reducers({}) == {}
    assert extract_reducers({"type": "object"}) == {}


# ---------------------------------------------------------------------------
# TypedDict materialization
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_typed_state_class_has_reducer_annotations() -> None:
    import typing  # noqa: PLC0415 — only used in this test

    schema = {
        "type": "object",
        "properties": {
            "history": {"type": "array", "x-movate-reducer": "append"},
            "name": {"type": "string"},
        },
    }
    reducers = extract_reducers(schema)
    cls = build_typed_state_class(schema, reducers)

    hints = typing.get_type_hints(cls, include_extras=True)
    # `history` is annotated; pulling __metadata__ out of the Annotated
    # wrapper exposes the reducer callable.
    history_hint = hints["history"]
    assert typing.get_origin(history_hint) is not None  # Annotated wrapper present
    assert REDUCERS["append"] in typing.get_args(history_hint)
    # `name` is just `Any` — no Annotated wrapper.
    assert hints["name"] is typing.Any


# ---------------------------------------------------------------------------
# validate_dag — parallel-specific rules
# ---------------------------------------------------------------------------


_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "history": {
            "type": "array",
            "items": {"type": "string"},
            "x-movate-reducer": "append",
        },
    },
}


def _make_agent(agent_dir: Path, *, name: str) -> Path:
    """Scaffold an agent that appends its name to ``history``."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "prompt.md").write_text("echo {{ input.text }}\n")
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            }
        )
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["history"],
                "additionalProperties": False,
                "properties": {
                    "history": {"type": "array", "items": {"type": "string"}},
                },
            }
        )
    )
    (agent_dir / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Agent",
                "name": name,
                "version": "0.1.0",
                "lifecycle": "validated",
                "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
                "prompt": "./prompt.md",
                "schema": {
                    "input": "./schema/input.json",
                    "output": "./schema/output.json",
                },
            }
        )
    )
    return agent_dir


def _make_workflow(
    workflow_dir: Path,
    *,
    nodes: list[dict],
    edges: list[dict],
    runtime: str = "langgraph",
    state_schema: dict | None = None,
) -> Path:
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(state_schema or _STATE_SCHEMA))
    yaml_path = workflow_dir / "workflow.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "api_version": "movate/v1",
                "kind": "Workflow",
                "name": "parallel-test",
                "version": "0.1.0",
                "runtime": runtime,
                "state_schema": "./state.json",
                "entrypoint": "a",
                "nodes": nodes,
                "edges": edges,
            }
        )
    )
    return yaml_path


def _scaffold_fan_out_in(tmp_path: Path) -> Path:
    """A → {B, C, D} → E. Each branch appends to `history` via reducer."""
    workflow_dir = tmp_path / "wf-parallel"
    for name in ("a", "b", "c", "d", "e"):
        _make_agent(workflow_dir / "agents" / name, name=f"{name}-agent")
    return _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "a", "type": "agent", "ref": "./agents/a"},
            {"id": "b", "type": "agent", "ref": "./agents/b"},
            {"id": "c", "type": "agent", "ref": "./agents/c"},
            {"id": "d", "type": "agent", "ref": "./agents/d"},
            {"id": "e", "type": "agent", "ref": "./agents/e"},
        ],
        edges=[
            # Sequential entry
            {"from": "a", "to": "b", "kind": "parallel_fan_out"},
            {"from": "a", "to": "c", "kind": "parallel_fan_out"},
            {"from": "a", "to": "d", "kind": "parallel_fan_out"},
            # Fan-in to E
            {"from": "b", "to": "e", "kind": "parallel_fan_in"},
            {"from": "c", "to": "e", "kind": "parallel_fan_in"},
            {"from": "d", "to": "e", "kind": "parallel_fan_in"},
        ],
    )


@pytest.mark.unit
def test_validate_dag_accepts_fan_out_in(tmp_path: Path) -> None:
    yaml_path = _scaffold_fan_out_in(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    validate_dag(graph)  # should not raise


@pytest.mark.unit
def test_validate_dag_rejects_single_parallel_branch(tmp_path: Path) -> None:
    """A single parallel_fan_out edge is degenerate — operator probably
    meant sequential. Refuse to compile so a typo doesn't silently
    behave like a sequential workflow."""
    workflow_dir = tmp_path / "wf-degenerate"
    for name in ("a", "b"):
        _make_agent(workflow_dir / "agents" / name, name=f"{name}-agent")
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "a", "type": "agent", "ref": "./agents/a"},
            {"id": "b", "type": "agent", "ref": "./agents/b"},
        ],
        edges=[{"from": "a", "to": "b", "kind": "parallel_fan_out"}],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    with pytest.raises(WorkflowCompileError, match="at least 2"):
        validate_dag(graph)


@pytest.mark.unit
def test_validate_dag_rejects_mixed_parallel_and_sequential(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "wf-mixed"
    for name in ("a", "b", "c"):
        _make_agent(workflow_dir / "agents" / name, name=f"{name}-agent")
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "a", "type": "agent", "ref": "./agents/a"},
            {"id": "b", "type": "agent", "ref": "./agents/b"},
            {"id": "c", "type": "agent", "ref": "./agents/c"},
        ],
        edges=[
            {"from": "a", "to": "b", "kind": "parallel_fan_out"},
            {"from": "a", "to": "c", "kind": "sequential"},
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    with pytest.raises(WorkflowCompileError, match="mixes edge kinds"):
        validate_dag(graph)


@pytest.mark.unit
def test_validate_dag_rejects_mixed_parallel_and_conditional(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "wf-mixed-cond"
    for name in ("a", "b", "c"):
        _make_agent(workflow_dir / "agents" / name, name=f"{name}-agent")
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "a", "type": "agent", "ref": "./agents/a"},
            {"id": "b", "type": "agent", "ref": "./agents/b"},
            {"id": "c", "type": "agent", "ref": "./agents/c"},
        ],
        edges=[
            {"from": "a", "to": "b", "kind": "parallel_fan_out"},
            {"from": "a", "to": "c", "kind": "conditional", "when": None},
        ],
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    with pytest.raises(WorkflowCompileError, match="mixes edge kinds"):
        validate_dag(graph)


@pytest.mark.unit
def test_compile_workflow_rejects_bad_reducer_name(tmp_path: Path) -> None:
    """A typo in `x-movate-reducer` fails workflow load, not first parallel
    merge. Operators see the bad name immediately."""
    workflow_dir = tmp_path / "wf-bad-reducer"
    _make_agent(workflow_dir / "agents" / "a", name="a-agent")
    bad_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "history": {
                "type": "array",
                "items": {"type": "string"},
                "x-movate-reducer": "concat_first",  # wrong name
            },
        },
    }
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[{"id": "a", "type": "agent", "ref": "./agents/a"}],
        edges=[],
        state_schema=bad_schema,
    )
    spec, parent = load_workflow_spec(yaml_path)
    with pytest.raises(WorkflowCompileError, match="unknown reducer"):
        compile_workflow(spec, parent)


@pytest.mark.unit
def test_yaml_rejects_parallel_with_when(tmp_path: Path) -> None:
    """`when:` is only meaningful on conditional edges. Combining it with
    `parallel_fan_out` fails at YAML parse time."""
    workflow_dir = tmp_path / "wf-bad-when"
    for name in ("a", "b", "c"):
        _make_agent(workflow_dir / "agents" / name, name=f"{name}-agent")
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "a", "type": "agent", "ref": "./agents/a"},
            {"id": "b", "type": "agent", "ref": "./agents/b"},
            {"id": "c", "type": "agent", "ref": "./agents/c"},
        ],
        edges=[
            {
                "from": "a",
                "to": "b",
                "kind": "parallel_fan_out",
                "when": "$.score > 0",
            },
            {"from": "a", "to": "c", "kind": "parallel_fan_out"},
        ],
    )
    from movate.core.workflow.spec import (  # noqa: PLC0415
        WorkflowSpecLoadError,
    )

    with pytest.raises(WorkflowSpecLoadError, match="parallel_fan_out"):
        load_workflow_spec(yaml_path)


# ---------------------------------------------------------------------------
# End-to-end — fan-out + reducer combines state
# ---------------------------------------------------------------------------


class _BranchAwareProvider(BaseLLMProvider):
    """Returns each agent's name appended to history as the agent's output —
    we infer which agent is calling by inspecting the prompt's `echo`
    text since each agent's prompt has the same shape but different name."""

    name = "branch_aware"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        # All agents share the same prompt template; the only signal is
        # the input.text. For this test we use a deterministic mapping:
        # each agent's output advertises its own name in `history`. We
        # synthesize that by looking at which agent's prompt body we're
        # responding to — but since prompts are identical, we cycle
        # We don't need to discriminate per-agent here — every branch
        # contributes the same marker to `history`. The reducer is what
        # we're verifying, not per-agent identity.
        return CompletionResponse(text='{"history": ["x"]}')

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError


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


@pytest.mark.unit
async def test_fan_out_in_workflow_runs_and_reducer_concatenates(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """End-to-end: A → {B, C, D} → E. The `append` reducer concatenates
    each branch's contribution to `history`. Final state has 5 entries
    (one per node a/b/c/d/e), but we relax the assertion because
    LangGraph's parallel ordering isn't guaranteed."""
    yaml_path = _scaffold_fan_out_in(tmp_path)
    executor = Executor(
        provider=_BranchAwareProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    runner = WorkflowRunner(executor=executor, storage=storage)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    result = await runner.run(graph, initial_state={"text": "seed"})
    assert result.status is WorkflowStatus.SUCCESS

    # Every node ran exactly once.
    assert sorted(r.node_id for r in result.runs) == ["a", "b", "c", "d", "e"]

    # `history` reducer (append) accumulates one entry per node — the
    # initial state had no `history`, so the final length equals the
    # number of nodes that wrote to it (all 5).
    history = result.final_state.get("history", [])
    assert isinstance(history, list)
    assert len(history) == 5, f"reducer should accumulate 5 entries, got {history}"


class _OneBranchFailsProvider(BaseLLMProvider):
    """Branch C returns invalid JSON; everyone else returns a clean
    ``{"history": ["<letter>"]}`` based on which agent's prompt is
    being rendered (each agent's prompt body contains its `name`)."""

    name = "one_branch_fails"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        for letter in ("a", "b", "c", "d", "e"):
            # Each scaffolded agent's prompt template embeds its name
            # via the test's _make_agent helper.
            if f"agent {letter}-agent:" in body:
                if letter == "c":
                    return CompletionResponse(text="not valid JSON")
                return CompletionResponse(text=f'{{"history": ["{letter}"]}}')
        return CompletionResponse(text='{"history": ["?"]}')

    async def stream(self, request):  # pragma: no cover
        raise NotImplementedError

    async def embed(self, text, *, model):  # pragma: no cover
        raise NotImplementedError


def _scaffold_failing_branch_workflow(tmp_path: Path) -> Path:
    """A → {B, C, D} → E with custom prompts so the provider can
    discriminate per-agent (each prompt body includes the agent name)."""
    workflow_dir = tmp_path / "wf-branch-fail"

    def _scaffold(name: str) -> None:
        agent_dir = workflow_dir / "agents" / name
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "prompt.md").write_text(f"agent {name}-agent: echo {{{{ input.text }}}}\n")
        (agent_dir / "schema").mkdir(exist_ok=True)
        (agent_dir / "schema" / "input.json").write_text(
            json.dumps({"type": "object", "properties": {"text": {"type": "string"}}})
        )
        (agent_dir / "schema" / "output.json").write_text(
            json.dumps(
                {
                    "type": "object",
                    "required": ["history"],
                    "additionalProperties": False,
                    "properties": {"history": {"type": "array", "items": {"type": "string"}}},
                }
            )
        )
        (agent_dir / "agent.yaml").write_text(
            yaml.safe_dump(
                {
                    "api_version": "movate/v1",
                    "kind": "Agent",
                    "name": f"{name}-agent",
                    "version": "0.1.0",
                    "lifecycle": "validated",
                    "model": {"provider": "openai/gpt-4o-mini-2024-07-18"},
                    "prompt": "./prompt.md",
                    "schema": {
                        "input": "./schema/input.json",
                        "output": "./schema/output.json",
                    },
                }
            )
        )

    for name in ("a", "b", "c", "d", "e"):
        _scaffold(name)

    return _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "a", "type": "agent", "ref": "./agents/a"},
            {"id": "b", "type": "agent", "ref": "./agents/b"},
            {"id": "c", "type": "agent", "ref": "./agents/c"},
            {"id": "d", "type": "agent", "ref": "./agents/d"},
            {"id": "e", "type": "agent", "ref": "./agents/e"},
        ],
        edges=[
            {"from": "a", "to": "b", "kind": "parallel_fan_out"},
            {"from": "a", "to": "c", "kind": "parallel_fan_out"},
            {"from": "a", "to": "d", "kind": "parallel_fan_out"},
            {"from": "b", "to": "e", "kind": "parallel_fan_in"},
            {"from": "c", "to": "e", "kind": "parallel_fan_in"},
            {"from": "d", "to": "e", "kind": "parallel_fan_in"},
        ],
    )


@pytest.mark.unit
async def test_parallel_branch_failure_preserves_sibling_outputs(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """Branch-level failure invalidation (Tier 2 #7).

    When parallel branch C fails, sibling branches that completed
    (B and D, or whichever completed before LangGraph aborted the
    super-step) MUST have their contributions preserved in
    ``final_state``. Before the fix the workflow halted with
    ``final_state`` snapshotted at the failing branch's pre-merge
    input — sibling outputs were dropped on the floor.
    """
    yaml_path = _scaffold_failing_branch_workflow(tmp_path)
    executor = Executor(
        provider=_OneBranchFailsProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    runner = WorkflowRunner(executor=executor, storage=storage)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    result = await runner.run(graph, initial_state={"text": "seed"})

    # Workflow errored; error_node_id points at the failing branch.
    assert result.status is WorkflowStatus.ERROR
    assert result.error_node_id == "c"

    # The fan-in node E never ran (it would need ALL upstream to succeed).
    visited = [r.node_id for r in result.runs]
    assert "e" not in visited

    # A definitely ran (it's the entrypoint, before any branch).
    assert "a" in visited

    # At least ONE of B / D completed and its `history` contribution
    # survived the failure. The exact set depends on LangGraph's
    # super-step ordering, but the post-fix invariant is "any
    # successful branch's writes are in final_state."
    history = result.final_state.get("history", [])
    assert isinstance(history, list)
    assert "a" in history  # A always runs first
    sibling_contribs = {x for x in history if x in ("b", "d")}
    assert sibling_contribs, (
        f"expected at least one of B/D to contribute to history, "
        f"got {history!r}. Pre-fix this would be empty."
    )


@pytest.mark.unit
async def test_pure_sequential_workflow_still_uses_dict_state(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """Regression guard: workflows WITHOUT parallel edges keep the
    existing `StateGraph(dict)` + full-state behaviour. The dict path
    is still tested for new code; this test confirms the dispatch
    correctly identifies non-parallel workflows."""
    workflow_dir = tmp_path / "wf-pure-seq"
    _make_agent(workflow_dir / "agents" / "a", name="a-agent")
    _make_agent(workflow_dir / "agents" / "b", name="b-agent")
    yaml_path = _make_workflow(
        workflow_dir,
        nodes=[
            {"id": "a", "type": "agent", "ref": "./agents/a"},
            {"id": "b", "type": "agent", "ref": "./agents/b"},
        ],
        edges=[{"from": "a", "to": "b"}],
        state_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "history": {"type": "array", "items": {"type": "string"}},
            },
        },
    )
    executor = Executor(
        provider=_BranchAwareProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    runner = WorkflowRunner(executor=executor, storage=storage)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    result = await runner.run(graph, initial_state={"text": "seed"})
    assert result.status is WorkflowStatus.SUCCESS
    # Sequential path: last node's output replaces history (no reducer
    # because the schema doesn't declare one). Only `["x"]` from b.
    assert result.final_state == {"text": "seed", "history": ["x"]}
