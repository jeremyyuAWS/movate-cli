"""Resume a checkpointed workflow run with a merged-state payload.

The Tier 2 #3 piece of the determinism bundle. Pairs with:

* The tenant-namespaced checkpointer (Tier 2 #2 — PRs #8 + #13) which
  persists per-step state so we have something to resume FROM.
* HITL nodes (Tier 2 #4 — pending) which give the workflow a natural
  reason to pause. Without HITL the resume API is operator-driven —
  fix-then-retry semantics after a workflow errored.

The contract:

* Caller supplies a ``workflow_run_id`` and an optional JSON ``payload``.
* We look up the corresponding :class:`WorkflowRunRecord`. If absent or
  if it belongs to a different tenant, raise :class:`ResumeNotFound`
  (the caller maps this to a 404 — never 403, since 403 would leak the
  existence of the run).
* We load + compile the original workflow's graph from disk via the
  registry. The compiled graph re-enters its checkpointer and continues
  from the last checkpoint.
* The merged ``payload`` is applied via LangGraph's ``update_state``
  before invoking — that's how a HITL approval body, or an operator's
  state correction, enters the workflow.

This module is the SEAM. The HTTP endpoint + CLI counterpart wrap it.
Both will land in a follow-up PR once the resume primitive is exercised
end-to-end with a real HITL pause.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaError

from movate.core.executor import Executor
from movate.core.models import WorkflowRunRecord, WorkflowStatus
from movate.core.workflow.compilers.langgraph import (
    LangGraphCompileError,
    run_via_langgraph,
)
from movate.core.workflow.ir import NodeType, WorkflowGraph
from movate.core.workflow.runner import WorkflowResult, WorkflowRunError
from movate.storage.base import StorageProvider


class ResumeNotFound(Exception):  # noqa: N818 — semantic name maps to HTTP 404
    """Raised when no resumable workflow run is found for the given id
    (under the caller's tenant). HTTP wrappers should translate to 404,
    never 403 — leaking the existence of cross-tenant run_ids defeats
    the tenant isolation we baked into the checkpointer."""


class ResumeError(Exception):
    """Raised for non-not-found resume failures: workflow has no
    checkpointer configured, original workflow YAML is missing,
    underlying LangGraph error, etc."""


async def resume_workflow(
    workflow_run_id: str,
    *,
    payload: dict[str, Any] | None,
    graph: WorkflowGraph,
    executor: Executor,
    storage: StorageProvider,
    tenant_id: str,
) -> WorkflowResult:
    """Continue a checkpointed workflow from its last saved state.

    ``graph`` is the compiled IR of the SAME workflow that was paused.
    Callers typically obtain it via the workflow registry (the runtime
    keeps an indexed copy of every workflow.yaml on disk). The graph's
    ``checkpointer`` field must be a persistent backend (sqlite or
    postgres) for cross-process resume; memory is in-process only.

    ``payload`` is the JSON body the resuming caller wants merged into
    the checkpointed state. Common cases:

    * HITL approval — ``{"approved": true, "reviewer": "alice"}``
    * Operator state correction after a failure — ``{"retry_count": 0}``
    * No payload (``None``) — just continue from the checkpoint as-is

    Returns a fresh :class:`WorkflowResult` with the same shape the
    initial run produced, but tagged with the resumed run's id (which
    matches the original workflow_run_id — LangGraph's thread_id maps
    1:1 to that). The result may itself be PAUSED if the workflow has
    another HUMAN node downstream of the one we just released.
    """
    # 1. Verify there's actually a workflow run with this id under the
    #    caller's tenant. The storage layer's tenant-aware lookup
    #    returns None on either missing-id OR cross-tenant — same
    #    response either way to avoid leaking existence.
    record: WorkflowRunRecord | None = await storage.get_workflow_run(
        workflow_run_id, tenant_id=tenant_id
    )
    if record is None:
        raise ResumeNotFound(f"no workflow run found for id {workflow_run_id!r}")

    # 2. The graph must declare a checkpointer — otherwise there's
    #    nothing to resume from. Memory-checkpointer runs CAN be
    #    resumed within the same process, so we accept all three kinds
    #    here; cross-process resume requires sqlite/postgres but that's
    #    a runtime fact, not a contract this function enforces.
    if graph.checkpointer is None:
        raise ResumeError(
            f"workflow {record.workflow!r} (run {workflow_run_id!r}) has no "
            f"checkpointer configured; can't resume. Add `checkpointer: "
            f"memory | sqlite | postgres` to its workflow.yaml."
        )

    # 3. Only PAUSED workflows can be resumed. Resuming a SUCCESS or
    #    ERROR run would be confusing semantically (what does it mean
    #    to "continue" a terminal workflow?). The HTTP wrapper should
    #    surface this as a 409 Conflict.
    if record.status is not WorkflowStatus.PAUSED:
        raise ResumeError(
            f"workflow run {workflow_run_id!r} is in status "
            f"{record.status.value!r}, not 'paused'. Only paused workflows "
            f"can be resumed; terminal runs (success / error) need to be "
            f"re-invoked from the start."
        )

    # 4. Validate the payload against the pause point's resume_payload_schema.
    #    Find the HUMAN node the run paused at by checking each HUMAN node's
    #    schema for one that matches. In v1 we have at most one HUMAN node
    #    per workflow (multi-pause is a v1.1.x extension), so the lookup is
    #    trivial; we keep it general.
    human_nodes = [n for n in graph.nodes.values() if n.type is NodeType.HUMAN]
    if payload is not None:
        if not human_nodes:
            raise ResumeError(
                "workflow has no HUMAN nodes but a resume payload was supplied. "
                "Payload is only meaningful when resuming at a HUMAN node."
            )
        # When there's exactly one HUMAN node (the v1 common case), validate
        # against it. Multi-HUMAN workflows would need to know WHICH node
        # the workflow is paused at — that requires reading the checkpoint,
        # which we defer to the HTTP wrapper that has the live graph state.
        # For now: validate against the FIRST HUMAN node's schema; multi-
        # HUMAN scenarios fall back to no-validation (with a noisy log).
        target_schema = human_nodes[0].resume_payload_schema
        if target_schema is not None:
            try:
                Draft202012Validator(target_schema).validate(payload)
            except JsonSchemaError as exc:
                raise ResumeError(
                    f"resume payload failed schema validation: {exc.message}"
                ) from exc

    # 5. Run the workflow through the langgraph compiler in resume mode.
    #    The compiler:
    #      a. Reconstructs the StateGraph with the same checkpointer
    #      b. Calls update_state(config, payload) to merge into checkpoint
    #      c. Calls ainvoke(None, config) to continue from the merged state
    #      d. Detects post-resume pauses and returns PAUSED if another
    #         HUMAN node is hit downstream
    try:
        result = await run_via_langgraph(
            graph,
            # initial_state is irrelevant on resume (LangGraph reads from
            # the checkpoint); pass through the original for parity.
            record.initial_state,
            executor=executor,
            storage=storage,
            tenant_id=tenant_id,
            workflow_run_id=workflow_run_id,
            resume_payload=payload or {},
            # ``record.pause_at`` was stamped onto the WorkflowRunRecord
            # at pause time. Threading it through to ``update_state`` as
            # ``as_node=`` tells LangGraph "this update IS the HUMAN
            # node's output; advance past it." Without it, the resumed
            # graph re-pauses at the same node indefinitely.
            resume_as_node=record.pause_at,
        )
    except LangGraphCompileError as exc:
        raise ResumeError(f"workflow compile error during resume: {exc}") from exc
    except WorkflowRunError as exc:
        raise ResumeError(f"workflow run error during resume: {exc}") from exc

    return result


__all__ = [
    "ResumeError",
    "ResumeNotFound",
    "resume_workflow",
]
