"""HTTP wire types for the movate runtime.

Kept separate from :mod:`movate.core.models` so the API surface can
evolve independently of the persisted schema. A change to ``JobRecord``
shouldn't force every consumer to upgrade; a change to the wire type
shouldn't force a DB migration.

Convention: every public response that names an entity ends in ``View``
(``JobView``, ``AgentView``) — distinguishes wire shape from DB model
in import sites and at code review.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from movate.core.models import ErrorInfo, JobKind, JobRecord, JobStatus


class RunSubmission(BaseModel):
    """``POST /run`` request body.

    ``kind`` discriminates the dispatch path; ``target`` is the agent
    or workflow name. ``input`` is the run input for an agent kind, or
    the initial state dict for a workflow kind. Validation against the
    target's input schema happens in the worker — accepting any dict
    here keeps the HTTP layer simple and lets schema errors land in
    the persisted ``JobRecord.error`` instead of as a 4xx that's
    invisible to ``/jobs/{id}`` polling.
    """

    model_config = ConfigDict(extra="forbid")

    kind: JobKind
    target: str = Field(..., min_length=1)
    input: dict[str, Any]


class RunAccepted(BaseModel):
    """``POST /run`` response — what the client polls against."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    """Always ``QUEUED`` from this endpoint; included for forward compat
    if we ever add a synchronous ``?wait=true`` mode."""


class JobView(BaseModel):
    """``GET /jobs/{id}`` response.

    Mirror of :class:`JobRecord` minus ``api_key_id`` (audit-only,
    never returned over the wire).
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    kind: JobKind
    target: str
    status: JobStatus
    input: dict[str, Any]
    result_run_id: str | None = None
    error: ErrorInfo | None = None
    created_at: datetime
    claimed_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def from_record(cls, record: JobRecord) -> JobView:
        return cls(
            job_id=record.job_id,
            kind=record.kind,
            target=record.target,
            status=record.status,
            input=record.input,
            result_run_id=record.result_run_id,
            error=record.error,
            created_at=record.created_at,
            claimed_at=record.claimed_at,
            completed_at=record.completed_at,
        )


class HealthView(BaseModel):
    """``GET /healthz`` response — boring on purpose."""

    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    version: str


class AgentView(BaseModel):
    """One entry in the registry response.

    Returns metadata only — never prompt content or schemas. The
    full agent definition lives on disk; ``GET /agents`` is for
    discovery, not migration.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str = ""


class AgentListView(BaseModel):
    """``GET /agents`` response."""

    model_config = ConfigDict(extra="forbid")

    agents: list[AgentView]


__all__ = [
    "AgentListView",
    "AgentView",
    "HealthView",
    "JobView",
    "RunAccepted",
    "RunSubmission",
]
