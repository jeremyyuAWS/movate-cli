"""HITL nodes — end-to-end pause + resume.

Three-node workflow ``classify → approve(HUMAN) → notify``. The first
AGENT node runs to completion; the HUMAN node interrupts the graph;
the test then calls ``resume_workflow`` with a payload and verifies
the second AGENT node runs and the workflow completes.

Coverage:

* HUMAN node in workflow.yaml without checkpointer → compile error
  pointing at the missing field.
* HUMAN node without resume_payload_schema → YAML parse error.
* HUMAN node in `runtime: homegrown` → compile error pointing at
  `runtime: langgraph`.
* End-to-end pause+resume with memory checkpointer.
* Resume payload validates against the HUMAN node's schema; bad
  payload raises ResumeError.
* Tenant isolation: cross-tenant resume still returns ResumeNotFound
  even when the run is paused.
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
    validate_for_runtime,
)
from movate.core.workflow.resume import (
    ResumeError,
    ResumeNotFound,
    resume_workflow,
)
from movate.core.workflow.runner import WorkflowRunner
from movate.core.workflow.spec import WorkflowSpecLoadError
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer

_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "classification": {"type": "string"},
        "approved": {"type": "boolean"},
        "reviewer": {"type": "string"},
        "notified": {"type": "boolean"},
    },
}


def _make_agent(agent_dir: Path, *, name: str, output_key: str) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "prompt.md").write_text(f"agent {name}: emit {{output_key}}.\n")
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps({"type": "object", "properties": {"text": {"type": "string"}}})
    )
    (agent_dir / "schema" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": [output_key],
                "additionalProperties": False,
                "properties": {output_key: {"type": "string"}},
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


def _scaffold_hitl_workflow(
    tmp_path: Path,
    *,
    runtime: str = "langgraph",
    checkpointer: str | None = "memory",
    include_resume_schema: bool = True,
) -> Path:
    """`classify (AGENT) → approve (HUMAN) → notify (AGENT)` workflow."""
    workflow_dir = tmp_path / "wf-hitl"
    _make_agent(workflow_dir / "agents" / "classify", name="classify", output_key="classification")
    _make_agent(workflow_dir / "agents" / "notify", name="notify", output_key="notified")
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))

    human_node: dict = {"id": "approve", "type": "human"}
    if include_resume_schema:
        human_node["resume_payload_schema"] = {
            "type": "object",
            "required": ["approved"],
            "properties": {
                "approved": {"type": "boolean"},
                "reviewer": {"type": "string"},
            },
        }

    payload: dict = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": "hitl-demo",
        "version": "0.1.0",
        "runtime": runtime,
        "state_schema": "./state.json",
        "entrypoint": "classify",
        "nodes": [
            {"id": "classify", "type": "agent", "ref": "./agents/classify"},
            human_node,
            {"id": "notify", "type": "agent", "ref": "./agents/notify"},
        ],
        "edges": [
            {"from": "classify", "to": "approve"},
            {"from": "approve", "to": "notify"},
        ],
    }
    if checkpointer is not None:
        payload["checkpointer"] = checkpointer

    yaml_path = workflow_dir / "workflow.yaml"
    yaml_path.write_text(yaml.safe_dump(payload))
    return yaml_path


class _ScriptedProvider(BaseLLMProvider):
    """Returns ``{<key>: "ok"}`` for whichever agent's prompt is being
    rendered. Each agent's output schema requires a specific key."""

    name = "scripted"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        body = request.messages[0].content
        if "classify" in body:
            return CompletionResponse(text='{"classification": "needs-review"}')
        if "notify" in body:
            return CompletionResponse(text='{"notified": "sent"}')
        return CompletionResponse(text="{}")

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
def executor(pricing: PricingTable, storage: InMemoryStorage) -> Executor:
    return Executor(
        provider=_ScriptedProvider(),
        pricing=pricing,
        storage=storage,
        tracer=NullTracer(),
    )


# ---------------------------------------------------------------------------
# YAML / compile-time gates
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_human_node_without_resume_schema_fails_yaml_parse(tmp_path: Path) -> None:
    yaml_path = _scaffold_hitl_workflow(tmp_path, include_resume_schema=False)
    with pytest.raises(WorkflowSpecLoadError, match="resume_payload_schema"):
        load_workflow_spec(yaml_path)


@pytest.mark.unit
def test_human_node_without_checkpointer_fails_compile(tmp_path: Path) -> None:
    """A workflow with HUMAN nodes but no `checkpointer:` field can't
    pause + resume. The dag validator surfaces this with an actionable
    pointer at the YAML field."""
    yaml_path = _scaffold_hitl_workflow(tmp_path, checkpointer=None)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    with pytest.raises(WorkflowCompileError, match="no checkpointer"):
        validate_for_runtime(graph)


@pytest.mark.unit
def test_human_node_in_homegrown_runtime_fails(tmp_path: Path) -> None:
    """HUMAN nodes only make sense under `runtime: langgraph` — the
    homegrown runner has no pause/resume semantics. The validator
    surfaces this with a fix pointer."""
    yaml_path = _scaffold_hitl_workflow(tmp_path, runtime="homegrown", checkpointer=None)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    with pytest.raises(WorkflowCompileError, match="runtime: langgraph"):
        validate_for_runtime(graph)


# ---------------------------------------------------------------------------
# End-to-end pause + resume
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_workflow_pauses_at_human_node(
    tmp_path: Path,
    executor: Executor,
    storage: InMemoryStorage,
) -> None:
    """The 3-node workflow runs node 1, then halts before the HUMAN node.
    WorkflowResult.status is PAUSED; pause_at names the HUMAN node;
    resume_payload_schema is propagated from the node spec."""
    yaml_path = _scaffold_hitl_workflow(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    runner = WorkflowRunner(executor=executor, storage=storage)

    result = await runner.run(graph, initial_state={"text": "seed"})

    assert result.status is WorkflowStatus.PAUSED
    assert result.pause_at == "approve"
    assert result.resume_payload_schema is not None
    assert result.resume_payload_schema["required"] == ["approved"]
    # The first AGENT node ran; its run record is in the result.
    visited = [r.node_id for r in result.runs]
    assert "classify" in visited
    # The second AGENT node did NOT run yet.
    assert "notify" not in visited


@pytest.mark.unit
async def test_resume_continues_workflow_to_completion(
    tmp_path: Path,
    executor: Executor,
    storage: InMemoryStorage,
) -> None:
    """After the HUMAN pause, calling resume_workflow with a valid
    payload runs the post-HUMAN AGENT node(s) and reports SUCCESS."""
    yaml_path = _scaffold_hitl_workflow(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    runner = WorkflowRunner(executor=executor, storage=storage)

    first_run = await runner.run(graph, initial_state={"text": "seed"})
    assert first_run.status is WorkflowStatus.PAUSED
    wf_id = first_run.workflow_run_id

    resumed = await resume_workflow(
        wf_id,
        payload={"approved": True, "reviewer": "alice"},
        graph=graph,
        executor=executor,
        storage=storage,
        tenant_id="local",
    )

    assert resumed.status is WorkflowStatus.SUCCESS
    assert resumed.workflow_run_id == wf_id
    # The payload merged into state — approved + reviewer surface.
    assert resumed.final_state.get("approved") is True
    assert resumed.final_state.get("reviewer") == "alice"
    # The post-HUMAN AGENT ran during resume.
    assert any(r.node_id == "notify" for r in resumed.runs)


@pytest.mark.unit
async def test_resume_with_invalid_payload_raises(
    tmp_path: Path,
    executor: Executor,
    storage: InMemoryStorage,
) -> None:
    """The resume payload must validate against the HUMAN node's
    resume_payload_schema. Missing required fields surface as
    ResumeError before the LangGraph invocation."""
    yaml_path = _scaffold_hitl_workflow(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    runner = WorkflowRunner(executor=executor, storage=storage)

    paused = await runner.run(graph, initial_state={"text": "seed"})
    assert paused.status is WorkflowStatus.PAUSED

    # Payload missing the required `approved` field.
    with pytest.raises(ResumeError, match="schema validation"):
        await resume_workflow(
            paused.workflow_run_id,
            payload={"reviewer": "alice"},
            graph=graph,
            executor=executor,
            storage=storage,
            tenant_id="local",
        )


@pytest.mark.unit
async def test_resume_cross_tenant_returns_not_found_even_when_paused(
    tmp_path: Path,
    executor: Executor,
    storage: InMemoryStorage,
) -> None:
    """Tenant A pauses a workflow; tenant B tries to resume it. Result
    is the same `ResumeNotFound` as if the run never existed — no
    leak of cross-tenant run_ids."""
    yaml_path = _scaffold_hitl_workflow(tmp_path)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    runner = WorkflowRunner(executor=executor, storage=storage, tenant_id="acme")

    paused = await runner.run(graph, initial_state={"text": "seed"})
    assert paused.status is WorkflowStatus.PAUSED

    with pytest.raises(ResumeNotFound):
        await resume_workflow(
            paused.workflow_run_id,
            payload={"approved": True},
            graph=graph,
            executor=executor,
            storage=storage,
            tenant_id="globex",  # different tenant
        )
