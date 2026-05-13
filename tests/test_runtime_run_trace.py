"""Tests for ``GET /api/v1/runs/{run_id}/trace``.

BACKLOG Group G item 65. Reconstructed view of a run (single agent or
workflow + per-node children) for the Mova iO Angular trace-viewer
component.

Coverage:

* **Happy path — agent run**: GET returns `kind=agent`, run dict
  populated with metrics + status + I/O, totals = the single run's
  cost/latency.
* **Happy path — workflow run**: GET returns `kind=workflow`,
  workflow dict + per-node children in chronological order, totals
  summed across children.
* **Tenant scoping**: another tenant's run_id returns 404.
* **404** on unknown id.
* **401** unauthed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import (
    JobStatus,
    Metrics,
    RunRecord,
    TokenUsage,
    WorkflowRunRecord,
    WorkflowStatus,
)
from movate.runtime import build_app
from movate.testing import InMemoryStorage


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def agents_path(tmp_path: Path) -> Path:
    p = tmp_path / "agents"
    p.mkdir()
    return p


@pytest.fixture
def client(storage: InMemoryStorage, agents_path: Path) -> TestClient:
    return TestClient(build_app(storage, agents_path=agents_path))


@pytest.fixture
async def auth_setup(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="run-trace-tests")
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


def _make_run(
    *,
    tenant_id: str,
    agent: str = "faq-bot",
    run_id: str | None = None,
    workflow_run_id: str | None = None,
    node_id: str | None = None,
    cost: float = 0.0012,
    latency_ms: int = 245,
    status: JobStatus = JobStatus.SUCCESS,
    created_at: datetime | None = None,
) -> RunRecord:
    """Minimal RunRecord factory — tests pass only what they need to
    assert on, defaults cover the rest."""
    return RunRecord(
        run_id=run_id or str(uuid4()),
        job_id=str(uuid4()),
        tenant_id=tenant_id,
        workflow_run_id=workflow_run_id,
        node_id=node_id,
        agent=agent,
        agent_version="0.1.0",
        provider="openai/gpt-4o-mini-2024-07-18",
        provider_version="2024-07-18",
        pricing_version="v2026.05.01",
        status=status,
        input={"text": "hi"},
        output={"answer": "hello"},
        metrics=Metrics(
            latency_ms=latency_ms,
            cost_usd=cost,
            tokens=TokenUsage(input=100, output=50),
        ),
        prompt_hash="deadbeef" * 8,
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Agent-run trace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_run_trace_returns_kind_agent(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    run = _make_run(tenant_id=tenant_id)
    await storage.save_run(run)

    r = client.get(f"/api/v1/runs/{run.run_id}/trace", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "agent"
    assert body["run"]["run_id"] == run.run_id
    assert body["run"]["agent"] == "faq-bot"
    assert body["run"]["status"] == "success"
    assert body["run"]["output"] == {"answer": "hello"}
    assert body["run"]["metrics"]["cost_usd"] == 0.0012
    assert body["run"]["metrics"]["latency_ms"] == 245
    # Single-agent: workflow + nodes are null/empty
    assert body["workflow"] is None
    assert body["nodes"] == []
    # Totals = the single run's metrics
    assert body["total_cost_usd"] == 0.0012
    assert body["total_latency_ms"] == 245


# ---------------------------------------------------------------------------
# Workflow-run trace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_run_trace_includes_children_in_chronological_order(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    wf_id = str(uuid4())
    workflow = WorkflowRunRecord(
        workflow_run_id=wf_id,
        tenant_id=tenant_id,
        workflow="returns-pipeline",
        workflow_version="0.1.0",
        status=WorkflowStatus.SUCCESS,
        initial_state={"order_id": "abc"},
        final_state={"refunded": True},
    )
    await storage.save_workflow_run(workflow)

    # Three child runs in NON-chronological save order. Engine must
    # sort by created_at ascending.
    t0 = datetime(2026, 5, 13, 10, 0, 0, tzinfo=UTC)
    middle = _make_run(
        tenant_id=tenant_id,
        workflow_run_id=wf_id,
        node_id="middle",
        agent="enrich",
        created_at=t0.replace(second=10),
        cost=0.001,
        latency_ms=100,
    )
    first = _make_run(
        tenant_id=tenant_id,
        workflow_run_id=wf_id,
        node_id="first",
        agent="classify",
        created_at=t0,
        cost=0.002,
        latency_ms=200,
    )
    last = _make_run(
        tenant_id=tenant_id,
        workflow_run_id=wf_id,
        node_id="last",
        agent="respond",
        created_at=t0.replace(second=20),
        cost=0.003,
        latency_ms=300,
    )
    await storage.save_run(middle)
    await storage.save_run(first)
    await storage.save_run(last)

    r = client.get(f"/api/v1/runs/{wf_id}/trace", headers=auth_header)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["kind"] == "workflow"
    assert body["workflow"]["workflow_run_id"] == wf_id
    assert body["workflow"]["workflow"] == "returns-pipeline"

    # Children sorted chronologically (first/middle/last by created_at)
    node_ids = [n["node_id"] for n in body["nodes"]]
    assert node_ids == ["first", "middle", "last"]

    # Totals summed across children
    assert body["total_cost_usd"] == 0.006  # 0.001 + 0.002 + 0.003
    assert body["total_latency_ms"] == 600  # 100 + 200 + 300


# ---------------------------------------------------------------------------
# Tenant scoping + 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_tenants_run_returns_404(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """Cross-tenant id probe returns 404 — never 403 (which would leak
    that the id exists)."""
    auth_header, _ = auth_setup
    other_tenant = uuid4().hex
    run = _make_run(tenant_id=other_tenant)
    await storage.save_run(run)

    r = client.get(f"/api/v1/runs/{run.run_id}/trace", headers=auth_header)
    assert r.status_code == 404


def test_unknown_run_id_returns_404(client: TestClient, auth_setup) -> None:
    auth_header, _ = auth_setup
    r = client.get(
        f"/api/v1/runs/{uuid4()}/trace",
        headers=auth_header,
    )
    assert r.status_code == 404


def test_trace_without_auth_returns_401(client: TestClient) -> None:
    r = client.get(f"/api/v1/runs/{uuid4()}/trace")
    assert r.status_code == 401
