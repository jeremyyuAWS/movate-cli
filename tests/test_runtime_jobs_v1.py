"""Tests for ``GET /api/v1/jobs`` — filterable job history.

BACKLOG Group G item 74. Extends the legacy ``GET /jobs`` (status
filter only) with ``agent=<name>`` filtering for the Angular UI's
agent-profile "recent runs" tab. Tenant-scoped identical to the
legacy endpoint.

Coverage:

* ``agent=`` filter narrows to one agent's jobs.
* ``status=`` + ``agent=`` combine (AND semantics).
* Tenant scoping enforced (another tenant's jobs don't leak through).
* Limit cap (>100 → 100).
* 401 unauthed.
* Also covers storage providers — the new ``target=`` kwarg threads
  through ``InMemoryStorage`` correctly (sqlite/postgres covered by
  the broader storage-provider test sweep).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from movate.core.auth import ApiKeyEnv, mint_api_key
from movate.core.models import JobKind, JobRecord, JobStatus
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
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="jobs-v1-tests")
    await storage.save_api_key(minted.record)
    header = {"Authorization": f"Bearer {minted.full_key}"}
    return header, tenant_id


async def _seed_job(
    storage: InMemoryStorage,
    *,
    tenant_id: str,
    target: str,
    status: JobStatus = JobStatus.QUEUED,
    input_: dict | None = None,
) -> JobRecord:
    """Drop one JobRecord into storage. Returns it so tests can
    assert against the job_id."""
    job = JobRecord(
        job_id=str(uuid4()),
        tenant_id=tenant_id,
        kind=JobKind.AGENT,
        target=target,
        status=status,
        input=input_ or {"text": "hi"},
        api_key_id="key-test",
        created_at=datetime.now(UTC),
    )
    await storage.save_job(job)
    return job


# ---------------------------------------------------------------------------
# Filter by agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_by_agent_returns_only_matching_jobs(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    await _seed_job(storage, tenant_id=tenant_id, target="faq-bot")
    await _seed_job(storage, tenant_id=tenant_id, target="faq-bot")
    await _seed_job(storage, tenant_id=tenant_id, target="support-triage")

    r = client.get("/api/v1/jobs?agent=faq-bot", headers=auth_header)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert all(j["target"] == "faq-bot" for j in body["jobs"])


@pytest.mark.asyncio
async def test_filter_by_agent_and_status_combines(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    await _seed_job(storage, tenant_id=tenant_id, target="faq-bot", status=JobStatus.QUEUED)
    await _seed_job(storage, tenant_id=tenant_id, target="faq-bot", status=JobStatus.SUCCESS)
    await _seed_job(storage, tenant_id=tenant_id, target="other", status=JobStatus.SUCCESS)

    r = client.get(
        "/api/v1/jobs?agent=faq-bot&status=success",
        headers=auth_header,
    )
    body = r.json()
    assert body["count"] == 1
    assert body["jobs"][0]["target"] == "faq-bot"
    assert body["jobs"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_no_filter_returns_all_tenant_jobs(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    auth_header, tenant_id = auth_setup
    await _seed_job(storage, tenant_id=tenant_id, target="a")
    await _seed_job(storage, tenant_id=tenant_id, target="b")
    await _seed_job(storage, tenant_id=tenant_id, target="c")

    r = client.get("/api/v1/jobs", headers=auth_header)
    body = r.json()
    assert body["count"] == 3


# ---------------------------------------------------------------------------
# Tenant scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_scoping_isolates_jobs(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """Another tenant's jobs MUST NOT leak through the agent filter."""
    auth_header, tenant_id = auth_setup
    other_tenant = uuid4().hex
    await _seed_job(storage, tenant_id=tenant_id, target="faq-bot")
    await _seed_job(storage, tenant_id=other_tenant, target="faq-bot")
    await _seed_job(storage, tenant_id=other_tenant, target="faq-bot")

    r = client.get("/api/v1/jobs?agent=faq-bot", headers=auth_header)
    body = r.json()
    # Only the calling tenant's single faq-bot job comes back.
    assert body["count"] == 1


# ---------------------------------------------------------------------------
# Limit cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_limit_capped_at_100(
    client: TestClient, storage: InMemoryStorage, auth_setup
) -> None:
    """Even when the client asks for limit=500, the response carries
    at most 100 entries. Bound the response size + prevent runaway
    queries on a noisy tenant."""
    auth_header, tenant_id = auth_setup
    for _ in range(105):
        await _seed_job(storage, tenant_id=tenant_id, target="a")

    r = client.get("/api/v1/jobs?limit=500", headers=auth_header)
    body = r.json()
    assert body["count"] == 100


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_jobs_without_auth_returns_401(client: TestClient) -> None:
    r = client.get("/api/v1/jobs")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Storage layer — InMemoryStorage target filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_storage_target_filter() -> None:
    """The new ``target=`` kwarg on list_jobs threads through
    InMemoryStorage. (Sqlite + Postgres providers covered by the
    broader storage-provider sweep — they all share the same
    signature change.)"""
    storage = InMemoryStorage()
    await storage.init()
    tenant_id = "t1"
    await _seed_job(storage, tenant_id=tenant_id, target="alpha")
    await _seed_job(storage, tenant_id=tenant_id, target="beta")
    await _seed_job(storage, tenant_id=tenant_id, target="alpha")

    alpha_only = await storage.list_jobs(tenant_id=tenant_id, target="alpha")
    assert len(alpha_only) == 2
    assert all(j.target == "alpha" for j in alpha_only)

    beta_only = await storage.list_jobs(tenant_id=tenant_id, target="beta")
    assert len(beta_only) == 1

    no_filter = await storage.list_jobs(tenant_id=tenant_id)
    assert len(no_filter) == 3
