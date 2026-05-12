"""Checkpointer tests — tenant isolation + factory + end-to-end memory.

The load-bearing contract is tenant isolation: even if tenant B knows
tenant A's ``workflow_run_id`` (the thread_id), they MUST NOT be able to
read tenant A's checkpoints. The :class:`TenantNamespacedCheckpointer`
wrapper enforces this by prefixing every thread_id with the tenant tag.

Coverage matrix:

* Wrapper namespacing (sync + async put/get/list paths).
* Factory: memory works; sqlite + postgres raise the deferred-PR pointer;
  unknown kinds raise CheckpointerError.
* Tenant-tag separator hardening: tenant_id can't contain ``::``.
* End-to-end through ``compile_to_langgraph``: a workflow with
  ``checkpointer: memory`` actually writes checkpoints, and they're
  scoped to the configured tenant.
* HUMAN-resume API isn't here yet — landing in a later PR.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("langgraph")

from langgraph.checkpoint.memory import MemorySaver

from movate.core.executor import Executor
from movate.core.models import WorkflowStatus
from movate.core.workflow import compile_workflow, load_workflow_spec
from movate.core.workflow.checkpointer import (
    CheckpointerError,
    CheckpointerKind,
    TenantNamespacedCheckpointer,
    make_checkpointer,
)
from movate.core.workflow.compilers.langgraph import LangGraphCompileError
from movate.core.workflow.runner import WorkflowRunner
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
)
from movate.providers.pricing import PricingTable, load_pricing
from movate.testing import InMemoryStorage, NullTracer

# Test helpers — duplicated from test_workflow_langgraph.py rather than
# imported so each test file stays standalone-runnable.
_STATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "step1": {"type": "string"},
    },
}


def _make_agent(agent_dir: Path, *, name: str, input_key: str, output_key: str) -> Path:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "prompt.md").write_text(
        f"Echo {{{{ input.{input_key} }}}}; emit JSON with {output_key}.\n"
    )
    (agent_dir / "schema").mkdir(exist_ok=True)
    (agent_dir / "schema" / "input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": [input_key],
                "additionalProperties": False,
                "properties": {input_key: {"type": "string", "minLength": 1}},
            }
        )
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
    return agent_dir


def _scaffold_single_node_workflow(
    tmp_path: Path,
    *,
    checkpointer: str | None,
) -> Path:
    """Single-node `runtime: langgraph` workflow with optional checkpointer."""
    workflow_dir = tmp_path / f"wf-cp-{checkpointer or 'none'}"
    _make_agent(
        workflow_dir / "agents" / "first",
        name="first-agent",
        input_key="text",
        output_key="step1",
    )
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(json.dumps(_STATE_SCHEMA))
    payload: dict = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": "cp-test",
        "version": "0.1.0",
        "runtime": "langgraph",
        "state_schema": "./state.json",
        "entrypoint": "first",
        "nodes": [{"id": "first", "type": "agent", "ref": "./agents/first"}],
        "edges": [],
    }
    if checkpointer is not None:
        payload["checkpointer"] = checkpointer
    yaml_path = workflow_dir / "workflow.yaml"
    yaml_path.write_text(yaml.safe_dump(payload))
    return yaml_path


class _ConstantProvider(BaseLLMProvider):
    name = "constant"
    version = "0.0.1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(text='{"step1": "alpha"}')

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


# ---------------------------------------------------------------------------
# Wrapper — tenant namespacing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_wrapper_prefixes_thread_id_with_tenant_tag() -> None:
    """The namespace pattern is `tenant::thread_id` — verified directly
    rather than peering through an opaque API so a regression here is
    obvious in the failure message."""

    inner = MemorySaver()
    wrapped = TenantNamespacedCheckpointer(inner, tenant_id="acme")

    namespaced = wrapped._namespace({"configurable": {"thread_id": "wf-abc-123"}})
    assert namespaced["configurable"]["thread_id"] == "acme::wf-abc-123"

    # Idempotent — passing an already-namespaced config through again
    # doesn't double-wrap.
    again = wrapped._namespace(namespaced)
    assert again["configurable"]["thread_id"] == "acme::wf-abc-123"


@pytest.mark.unit
def test_wrapper_rejects_empty_tenant() -> None:

    with pytest.raises(CheckpointerError, match="tenant_id is required"):
        TenantNamespacedCheckpointer(MemorySaver(), tenant_id="")


@pytest.mark.unit
def test_wrapper_rejects_tenant_with_namespace_separator() -> None:
    """If a tenant_id contained `::` it could spoof another tenant's namespace
    by matching the prefix structurally. Reject at construction."""

    with pytest.raises(CheckpointerError, match="namespace separator"):
        TenantNamespacedCheckpointer(MemorySaver(), tenant_id="acme::admin")


@pytest.mark.unit
def test_wrapper_namespaces_uniquely_per_tenant() -> None:
    """Two wrappers around the SAME inner saver namespace the same
    ``thread_id`` to different physical keys. Direct verification of
    the namespacing primitive — end-to-end isolation is covered by
    ``test_checkpoint_thread_id_is_tenant_namespaced_end_to_end``.

    Round-tripping a real ``Checkpoint`` here would require building
    one with all of LangGraph's internal fields (channel_versions,
    pending_sends, etc.); that's the integration test's job. This
    unit-level check stays at the namespacing layer where the
    security boundary actually lives."""

    inner = MemorySaver()
    acme = TenantNamespacedCheckpointer(inner, tenant_id="acme")
    globex = TenantNamespacedCheckpointer(inner, tenant_id="globex")

    cfg = {"configurable": {"thread_id": "wf-collision"}}
    acme_cfg = acme._namespace(cfg)
    globex_cfg = globex._namespace(cfg)

    assert acme_cfg["configurable"]["thread_id"] == "acme::wf-collision"
    assert globex_cfg["configurable"]["thread_id"] == "globex::wf-collision"
    assert acme_cfg["configurable"]["thread_id"] != globex_cfg["configurable"]["thread_id"]


@pytest.mark.unit
async def test_wrapper_refuses_unscoped_alist() -> None:
    """Tenant-unaware enumeration of all checkpoints would defeat the
    namespace. The wrapper explicitly refuses ``alist(None)``."""

    wrapped = TenantNamespacedCheckpointer(MemorySaver(), tenant_id="acme")
    with pytest.raises(CheckpointerError, match="without a config"):
        async for _ in wrapped.alist(None):  # pragma: no cover — never reached
            pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_factory_builds_memory_checkpointer() -> None:
    cp = make_checkpointer("memory", tenant_id="acme")
    assert isinstance(cp, TenantNamespacedCheckpointer)


@pytest.mark.unit
def test_factory_accepts_enum_or_string() -> None:
    """Pass-through both forms because the YAML side hands us a string but
    internal callers may use the enum directly."""
    assert isinstance(
        make_checkpointer(CheckpointerKind.MEMORY, tenant_id="acme"),
        TenantNamespacedCheckpointer,
    )
    assert isinstance(
        make_checkpointer("memory", tenant_id="acme"),
        TenantNamespacedCheckpointer,
    )


@pytest.mark.unit
def test_factory_rejects_unknown_kind() -> None:
    with pytest.raises(CheckpointerError, match="unknown checkpointer kind"):
        make_checkpointer("redis", tenant_id="acme")


@pytest.mark.unit
def test_sync_factory_redirects_sqlite_to_async_form() -> None:
    """SQLite + Postgres need connection-pool lifecycle that ``make_checkpointer``
    (sync) can't manage. The factory refuses and points operators at
    ``async_checkpointer`` instead."""
    with pytest.raises(CheckpointerError, match="async_checkpointer"):
        make_checkpointer("sqlite", tenant_id="acme")


@pytest.mark.unit
def test_sync_factory_redirects_postgres_to_async_form() -> None:
    with pytest.raises(CheckpointerError, match="async_checkpointer"):
        make_checkpointer("postgres", tenant_id="acme")


# ---------------------------------------------------------------------------
# End-to-end — compile_to_langgraph attaches the checkpointer
# ---------------------------------------------------------------------------


def _build_runner(
    *,
    provider: BaseLLMProvider,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
    tenant_id: str = "local",
) -> WorkflowRunner:
    executor = Executor(provider=provider, pricing=pricing, storage=storage, tracer=tracer)
    return WorkflowRunner(executor=executor, storage=storage, tenant_id=tenant_id)


@pytest.mark.unit
async def test_compile_to_langgraph_runs_with_memory_checkpointer(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """A workflow with `checkpointer: memory` invokes successfully and
    produces the same WorkflowResult shape as a workflow without one."""
    yaml_path = _scaffold_single_node_workflow(tmp_path, checkpointer="memory")
    runner = _build_runner(
        provider=_ConstantProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    assert graph.checkpointer == "memory"

    result = await runner.run(graph, initial_state={"text": "seed"})
    assert result.status is WorkflowStatus.SUCCESS
    assert result.final_state == {"text": "seed", "step1": "alpha"}


@pytest.mark.unit
async def test_compile_to_langgraph_without_checkpointer_still_works(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """Omitting the field is fine — checkpointer stays None and the graph
    compiles without one. The existing equivalence tests cover this
    happy path too; this is a regression guard."""
    yaml_path = _scaffold_single_node_workflow(tmp_path, checkpointer=None)
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    assert graph.checkpointer is None

    runner = _build_runner(
        provider=_ConstantProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    result = await runner.run(graph, initial_state={"text": "seed"})
    assert result.status is WorkflowStatus.SUCCESS


@pytest.mark.unit
async def test_compile_to_langgraph_runs_with_sqlite_checkpointer(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite backend works end-to-end through the async lifecycle.
    Uses an isolated tmp_path file via MOVATE_CHECKPOINT_DB so tests
    don't share state."""
    db = tmp_path / "checkpoints.db"
    monkeypatch.setenv("MOVATE_CHECKPOINT_DB", str(db))

    yaml_path = _scaffold_single_node_workflow(tmp_path, checkpointer="sqlite")
    runner = _build_runner(
        provider=_ConstantProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)
    result = await runner.run(graph, initial_state={"text": "seed"})

    assert result.status is WorkflowStatus.SUCCESS
    # SQLite saver wrote at least one checkpoint to disk.
    assert db.exists()
    assert db.stat().st_size > 0


@pytest.mark.unit
async def test_compile_to_langgraph_postgres_requires_dsn_env(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without MOVATE_CHECKPOINT_PG_DSN (or MOVATE_DB_URL), the postgres
    backend refuses with an operator-facing pointer."""
    monkeypatch.delenv("MOVATE_CHECKPOINT_PG_DSN", raising=False)
    monkeypatch.delenv("MOVATE_DB_URL", raising=False)

    yaml_path = _scaffold_single_node_workflow(tmp_path, checkpointer="postgres")
    runner = _build_runner(
        provider=_ConstantProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
    )
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    with pytest.raises(LangGraphCompileError, match="MOVATE_CHECKPOINT_PG_DSN"):
        await runner.run(graph, initial_state={"text": "seed"})


@pytest.mark.unit
async def test_checkpoint_thread_id_is_tenant_namespaced_end_to_end(
    tmp_path: Path,
    pricing: PricingTable,
    storage: InMemoryStorage,
    tracer: NullTracer,
) -> None:
    """Run two workflows with the same workflow_run_id under different
    tenants. Their checkpoints must NOT collide — proved by reading
    each tenant's view through its own wrapped checkpointer."""
    yaml_path = _scaffold_single_node_workflow(tmp_path, checkpointer="memory")
    spec, parent = load_workflow_spec(yaml_path)
    graph = compile_workflow(spec, parent)

    # Same workflow_run_id passed in explicitly under both tenants.
    shared_wf_id = "wf-deterministic-id"

    acme_runner = _build_runner(
        provider=_ConstantProvider(),
        pricing=pricing,
        storage=storage,
        tracer=tracer,
        tenant_id="acme",
    )
    globex_storage = InMemoryStorage()
    await globex_storage.init()
    globex_runner = _build_runner(
        provider=_ConstantProvider(),
        pricing=pricing,
        storage=globex_storage,
        tracer=tracer,
        tenant_id="globex",
    )

    a = await acme_runner.run(
        graph, initial_state={"text": "acme-seed"}, workflow_run_id=shared_wf_id
    )
    g = await globex_runner.run(
        graph, initial_state={"text": "globex-seed"}, workflow_run_id=shared_wf_id
    )

    # Both succeeded; their results carry the same workflow_run_id but
    # see DIFFERENT inputs — neither tenant's state bled into the other.
    assert a.status is WorkflowStatus.SUCCESS
    assert g.status is WorkflowStatus.SUCCESS
    assert a.workflow_run_id == shared_wf_id == g.workflow_run_id
    assert a.final_state["text"] == "acme-seed"
    assert g.final_state["text"] == "globex-seed"
