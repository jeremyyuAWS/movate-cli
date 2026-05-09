"""FastAPI app factory.

``build_app(storage)`` is the single entry point — tests build one per
test case with an :class:`InMemoryStorage`; ``movate serve`` builds
one with a :class:`SqliteProvider`. Storage is passed in (not built
inside) so the same factory works for every backend without env-var
gymnastics.

v0.5 stage 3a endpoints:

* ``GET /healthz`` — unauthed liveness check.
* ``POST /run`` — queue a job, return ``{"job_id", "status": "queued"}``.
* ``GET /jobs/{id}`` — poll a job; tenant-scoped (a tenant can never
  see another tenant's job, even with a valid key in the wrong env).

Deferred to stage 3b: ``GET /agents`` (needs an agent registry layer)
and ``movate serve`` CLI binding (uvicorn integration).
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import Depends, FastAPI, Request

import movate
from movate.core.models import JobRecord, JobStatus
from movate.runtime.errors import auth_required, not_found
from movate.runtime.middleware import AuthContext, make_auth_dependency
from movate.runtime.schemas import HealthView, JobView, RunAccepted, RunSubmission
from movate.storage.base import StorageProvider


def build_app(storage: StorageProvider) -> FastAPI:
    """Build the FastAPI app bound to ``storage``.

    The app's ``state`` carries the storage so handlers can read it
    without closing over the factory's locals — keeps testability
    clean (override ``app.state.storage`` to swap backends mid-test
    if you really need to).
    """
    app = FastAPI(
        title="movate",
        version=movate.__version__,
        description="Declarative platform for building and running AI agents.",
    )
    app.state.storage = storage

    auth_dep = make_auth_dependency(storage)

    # ------------------------------------------------------------------
    # /healthz — unauthed
    # ------------------------------------------------------------------
    @app.get("/healthz", response_model=HealthView, tags=["meta"])
    async def healthz() -> HealthView:
        """Liveness probe. Cheap on purpose — never hits storage."""
        return HealthView(status="ok", version=movate.__version__)

    # ------------------------------------------------------------------
    # POST /run — queue a job
    # ------------------------------------------------------------------
    @app.post("/run", response_model=RunAccepted, tags=["jobs"], status_code=202)
    async def submit_run(
        body: RunSubmission,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> RunAccepted:
        """Queue a job for the worker to claim.

        Returns ``202 Accepted`` (not ``201 Created``) — the resource
        being created is the *job*, but it's not yet executed; clients
        poll ``/jobs/{id}`` until terminal. The 202 status code makes
        that distinction wire-visible.
        """
        job = JobRecord(
            job_id=str(uuid4()),
            tenant_id=ctx.tenant_id,
            kind=body.kind,
            target=body.target,
            status=JobStatus.QUEUED,
            input=body.input,
            api_key_id=ctx.api_key_id,
        )
        store: StorageProvider = request.app.state.storage
        await store.save_job(job)
        return RunAccepted(job_id=job.job_id, status=job.status)

    # ------------------------------------------------------------------
    # GET /jobs/{id} — poll
    # ------------------------------------------------------------------
    @app.get("/jobs/{job_id}", response_model=JobView, tags=["jobs"])
    async def get_job(
        job_id: str,
        request: Request,
        ctx: AuthContext = Depends(auth_dep),
    ) -> JobView:
        """Return job state. Tenant-scoped: cross-tenant lookups 404,
        not 403 — leaking 403 vs 404 lets a caller probe whether a
        job_id exists in another tenant. ``not_found`` is the safe
        unified response."""
        store: StorageProvider = request.app.state.storage
        record = await store.get_job(job_id)
        if record is None or record.tenant_id != ctx.tenant_id:
            raise not_found("job", job_id)
        return JobView.from_record(record)

    return app


# Re-export for convenience — callers don't have to import the module
# just to suppress an "unused" lint on the auth helper above.
__all__ = ["auth_required", "build_app"]
